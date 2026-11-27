"""Run lifecycle orchestration (F2) — the approve-triggered background task.

``run_lifecycle(run_id)`` sequences the whole post-approval flow:

1. Provision one sandbox per expanded matrix cell —
   ``harness_configs × participant models`` (PR14; sequential — cheap,
   simpler DB handling; spawn failures are per-sandbox).
2. ``running`` → one monitor task per spawned sandbox (``asyncio.gather``).
3. All terminal → ``evaluating`` → per-sandbox evaluator calls (contract in
   docs/orchestrator-plan.md §5, implemented by F7 in ``evaluator/``).
4. ``done`` when ≥1 Score row exists for the run, else ``failed``.

``rescore_run(run_id)`` (PR5) re-enters ``evaluating`` from a terminal
state and re-runs the same evaluator pass on the stored artifacts —
optionally against an explicit rubric version, a subset of sandboxes, or a
different judge model. Old Score rows are kept; new rows carry their own
``rubric_id``/``rubric_version`` provenance.

The evaluator is imported lazily so the api works while F7 is in flight — a
missing/broken evaluator degrades to per-sandbox error events and a failed
run (zero scores), never a crash.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import Awaitable, Callable, Collection
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from db.models import CheckerResult, Event, Rubric, Run, Sandbox, Score
from gateway.creds import mint_cred
from gateway.metering import tokens_used
from sandbox.runtime import SandboxRuntime
from scoring import checkers

from . import monitor, pacing, prompt_cell, provision, state_machine
from .events import emit
from .matrix import expanded_configs, judge_models

log = logging.getLogger(__name__)

#: evaluator.evaluate_sandbox kwargs contract (plan §5) — F7 implements.
EvaluatorFn = Callable[..., Awaitable[Any]]

#: Headroom for the outer eval wait beyond the evaluator's own deadline —
#: equal timeouts would let cancellation preempt evaluate_sandbox's
#: soft-stop + scores.json salvage path.
_EVAL_TIMEOUT_MARGIN_SECONDS = 60.0

#: Poll cadence while a rescore waits for the execution slot — the same
#: lane the PR15 submission queue serializes (pacing.ACTIVE_RUN_STATES).
_RESCORE_SLOT_POLL_SECONDS = 5.0


def _now() -> datetime:
    return datetime.now(UTC)


# ---------------------------------------------------------------------------
# DB helpers (sync — called directly on the event-loop thread; see
# db/session.py for why orchestrator DB work never goes through to_thread)
# ---------------------------------------------------------------------------


def _load_run(session_factory: sessionmaker[Session], run_id: uuid.UUID) -> Run | None:
    with session_factory() as session:
        return session.get(Run, run_id)


def _sandbox_configs(run: Run) -> list[dict[str, Any]]:
    """Expanded run matrix — one config dict per sandbox.

    PR14: ``harness_configs × participant models``. ``run.config.models``
    holds the effective participant set ([model_name] + extras); each cell
    gets its model injected under ``model`` so spawn/results/analytics can
    attribute it. Single-model runs return the raw harness_configs verbatim.
    """
    cfg = run.config or {}
    # PR27: run.trials (column) wins; config["trials"] covers specs written
    # before the column existed — either way an unset value means 1.
    trials = getattr(run, "trials", None) or cfg.get("trials") or 1
    return expanded_configs(
        cfg.get("harness_configs") or [],
        cfg.get("models"),
        run.model_name,
        trials=trials,
    )


def _running_sandbox_ids(
    session_factory: sessionmaker[Session], run_id: uuid.UUID
) -> list[uuid.UUID]:
    with session_factory() as session:
        rows = session.execute(
            select(Sandbox.id)
            .where(Sandbox.run_id == run_id, Sandbox.status == "running")
            .order_by(Sandbox.id)
        ).scalars().all()
        return list(rows)


def _sandboxes_with_artifacts(
    session_factory: sessionmaker[Session],
    run_id: uuid.UUID,
    sandbox_ids: Collection[uuid.UUID] | None = None,
) -> list[Sandbox]:
    stmt = select(Sandbox).where(
        Sandbox.run_id == run_id, Sandbox.artifact_path.is_not(None)
    )
    if sandbox_ids is not None:
        stmt = stmt.where(Sandbox.id.in_(list(sandbox_ids)))
    with session_factory() as session:
        return list(
            session.execute(stmt.order_by(Sandbox.id)).scalars().all()
        )


def _latest_approved_rubric(
    session_factory: sessionmaker[Session], run_id: uuid.UUID
) -> Rubric | None:
    with session_factory() as session:
        return session.execute(
            select(Rubric)
            .where(Rubric.run_id == run_id, Rubric.status == "approved")
            .order_by(Rubric.version.desc())
            .limit(1)
        ).scalar_one_or_none()


def _rubric_for_run(
    session_factory: sessionmaker[Session],
    run_id: uuid.UUID,
    rubric_id: uuid.UUID,
) -> Rubric | None:
    """Fetch one rubric row — only when it belongs to ``run_id``."""
    with session_factory() as session:
        return session.execute(
            select(Rubric).where(Rubric.id == rubric_id, Rubric.run_id == run_id)
        ).scalar_one_or_none()


def _rubric_score_count(
    session_factory: sessionmaker[Session],
    run_id: uuid.UUID,
    rubric_id: uuid.UUID,
) -> int:
    """Score rows for one rubric version — rescore progress reporting."""
    with session_factory() as session:
        return int(
            session.execute(
                select(func.count(Score.id))
                .select_from(Score)
                .join(Sandbox, Score.sandbox_id == Sandbox.id)
                .where(Sandbox.run_id == run_id, Score.rubric_id == rubric_id)
            ).scalar_one()
        )


def _run_tokens_used(
    session_factory: sessionmaker[Session], run_id: uuid.UUID
) -> int:
    with session_factory() as session:
        return tokens_used(session, run_id)


def _score_count(session_factory: sessionmaker[Session], run_id: uuid.UUID) -> int:
    with session_factory() as session:
        return int(
            session.execute(
                select(func.count(Score.id))
                .select_from(Score)
                .join(Sandbox, Score.sandbox_id == Sandbox.id)
                .where(Sandbox.run_id == run_id)
            ).scalar_one()
        )


def _sandbox_score_count(
    session_factory: sessionmaker[Session], sandbox_id: uuid.UUID
) -> int:
    with session_factory() as session:
        return int(
            session.execute(
                select(func.count(Score.id)).where(Score.sandbox_id == sandbox_id)
            ).scalar_one()
        )


def _last_eval_scores_error(
    session_factory: sessionmaker[Session], sandbox_id: uuid.UUID
) -> str | None:
    """Latest ``eval_scores_failed`` detail for a sandbox — retry feedback.

    The evaluator emits that error event when the judge's scores.json is
    missing/unusable; feeding the detail into the retry's EVAL_PROMPT.md
    (``prior_error``) tells the respawned judge exactly which output shape
    failed instead of letting it flake identically.
    """
    with session_factory() as session:
        row = session.execute(
            select(Event.payload)
            .where(
                Event.sandbox_id == sandbox_id,
                Event.type == "error",
                Event.payload["phase"].as_string() == "eval_scores_failed",
            )
            .order_by(Event.id.desc())
            .limit(1)
        ).first()
    if not row:
        return None
    payload = row[0] or {}
    parts = [
        str(payload.get(k) or "").strip() for k in ("reason", "detail")
    ]
    parts = [p for p in parts if p]
    return ": ".join(parts) or None


def _has_rescore_marker(
    session_factory: sessionmaker[Session], run_id: uuid.UUID
) -> bool:
    """True when a rescore_requested/rescore_started checkpoint exists — the
    marker pair that distinguishes a re-score pass from a first-pass eval
    (runner.startup_sweep rescues on the same pair — keep in sync)."""
    with session_factory() as session:
        return (
            session.execute(
                select(Event.id)
                .where(
                    Event.run_id == run_id,
                    Event.type == "checkpoint",
                    Event.payload["stage"]
                    .as_string()
                    .in_(("rescore_requested", "rescore_started")),
                )
                .limit(1)
            ).first()
            is not None
        )


def _set_run_status(
    session_factory: sessionmaker[Session],
    run_id: uuid.UUID,
    to_status: str,
    *,
    finished: bool = False,
    event_payload: dict[str, Any] | None = None,
) -> None:
    with session_factory() as session:
        run = session.get(Run, run_id)
        if run is None:
            return
        try:
            state_machine.transition(session, run, to_status)
        except state_machine.InvalidTransition:
            run.status = to_status
        if finished:
            run.finished_at = _now()
        if event_payload:
            emit(
                session,
                run_id=run.id,
                event_type=event_payload.pop("type", "checkpoint"),
                payload=event_payload,
            )
        session.commit()


def _emit_run_event(
    session_factory: sessionmaker[Session],
    run_id: uuid.UUID,
    *,
    event_type: str,
    payload: dict[str, Any],
    sandbox_id: uuid.UUID | None = None,
) -> None:
    with session_factory() as session:
        emit(
            session,
            run_id=run_id,
            sandbox_id=sandbox_id,
            event_type=event_type,
            payload=payload,
        )
        session.commit()


# ---------------------------------------------------------------------------
# evaluation
# ---------------------------------------------------------------------------


def _import_evaluator() -> EvaluatorFn:
    """Lazy import so the api runs even while F7's evaluator is in flight."""
    from evaluator import evaluate_sandbox  # type: ignore[attr-defined]

    return evaluate_sandbox


# ---------------------------------------------------------------------------
# deterministic suite checks (PR26)
# ---------------------------------------------------------------------------


def _suite_check_specs(
    run: Run, settings: Any
) -> tuple[list[dict[str, Any]], Path | None]:
    """The deterministic checks declared by the run's suite task.

    Returns ``([], None)`` for non-suite runs, an unresolvable/renamed
    suite, a dropped task index, or a task with no ``check:`` block —
    checkers are strictly opt-in per suite task. The suite is re-read on
    every eval pass so a rescore measures the CURRENT suite.yaml spec.
    """
    if not getattr(run, "suite_ref", None) or run.suite_index is None:
        return [], None
    from api import suites as suite_store  # lazy — keep api deps out of module import

    root = getattr(settings, "BENCHMARK_SUITES_DIR", None) or "benchmark_suites"
    suite = suite_store.get_suite(root, run.suite_ref)
    if suite is None:
        log.warning(
            "run %s: suite %r no longer resolvable — checks skipped",
            run.id,
            run.suite_ref,
        )
        return [], None
    task = suite.task(run.suite_index)
    if task is None:
        log.warning(
            "run %s: suite %r has no task %s — checks skipped",
            run.id,
            run.suite_ref,
            run.suite_index,
        )
        return [], None
    return task.checks(), suite.suite_dir


def _suite_pass_score_row(
    session: Session,
    *,
    sandbox_id: uuid.UUID,
    outcomes: list[checkers.CheckOutcome],
) -> Score | None:
    """The ``suite_pass`` Score row — pass rate on the 0–10 scale every
    other dimension uses. Rubric-free (``rubric_id`` NULL): the verdict is
    the suite.yaml spec's, not the judge's, and the row exists even when
    no judge ever ran. ``seq`` stamps above the sandbox's current max so
    re-measurement merges newest-wins like a re-score. Returns None when
    nothing measured (all skipped/error — no verdict to report)."""
    roll = checkers.summarize(outcomes)
    if not roll["measured"]:
        return None
    max_seq = session.scalar(
        select(func.max(Score.seq)).where(Score.sandbox_id == sandbox_id)
    ) or 0
    return Score(
        sandbox_id=sandbox_id,
        rubric_id=None,
        rubric_version=None,
        seq=max_seq + 1,
        dimension=checkers.SUITE_PASS_DIMENSION,
        score=round(10.0 * roll["pass_rate"], 2),
        rationale=(
            f"{roll['passed']}/{roll['measured']} deterministic suite "
            f"checks passed"
        ),
        # PR29 provenance — deterministic measurement, not a judge: it
        # lands in its own merge bucket and carries no model.
        source="checker",
        judge_model=None,
    )


async def _run_suite_checks(
    *,
    session_factory: sessionmaker[Session],
    run: Run,
    sandbox_id: uuid.UUID,
    artifact_dir: Path,
    entries: list[dict[str, Any]],
    suite_dir: Path | None,
) -> None:
    """Measure the suite task's deterministic checks on one artifact —
    post-snapshot, pre-judge. Blocking docker-wait work runs in a worker
    thread; rows + the checkpoint event are written on the loop thread
    (the sqlite :memory: single-connection rule). A red check is a
    measurement — it lands as a ``fail`` row, never an exception, and
    never blocks the eval pass.

    Also writes ``<artifact>/suite_checks.json`` — the evaluator folds it
    into ``analysis.json``/``ANALYSIS.md`` as the ``suite`` signal — and
    persists the ``suite_pass`` Score row (pass rate ×10).
    """
    try:
        outcomes = await asyncio.to_thread(
            checkers.run_checks,
            entries,
            artifact_dir=artifact_dir,
            suite_dir=suite_dir,
        )
    except Exception as exc:  # noqa: BLE001 — run_checks traps per-entry;
        # this is a catastrophic guard (e.g. thread-pool failure), so the
        # checker subsystem can never take down the eval pass.
        log.warning("suite checks crashed for sandbox %s: %s", sandbox_id, exc)
        _emit_run_event(
            session_factory,
            run.id,
            sandbox_id=sandbox_id,
            event_type="error",
            payload={
                "stage": "suite_checks",
                "message": f"{type(exc).__name__}: {exc}",
            },
        )
        return

    # Artifact-side measurement file — judge evidence + the analysis.json
    # signal source. Best-effort: a bad artifact dir must not eat the rows.
    try:
        (artifact_dir / checkers.SUITE_CHECKS_FILENAME).write_text(
            json.dumps(
                checkers.suite_checks_doc(
                    outcomes,
                    suite_ref=run.suite_ref or "",
                    suite_index=int(run.suite_index or 0),
                ),
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        log.warning("could not write suite_checks.json: %s", exc)

    counts = {"pass": 0, "fail": 0, "skipped": 0, "error": 0}
    for o in outcomes:
        counts[o.status] = counts.get(o.status, 0) + 1
    with session_factory() as session:
        for o in outcomes:
            session.add(
                CheckerResult(
                    run_id=run.id,
                    sandbox_id=sandbox_id,
                    check_type=o.check_type,
                    spec=o.spec,
                    status=o.status,
                    passed=o.passed,
                    detail=o.detail,
                )
            )
        score_row = _suite_pass_score_row(
            session, sandbox_id=sandbox_id, outcomes=outcomes
        )
        if score_row is not None:
            session.add(score_row)
        emit(
            session,
            run_id=run.id,
            sandbox_id=sandbox_id,
            event_type="checkpoint",
            payload={
                "stage": "suite_checks",
                "passed": counts["pass"],
                "failed": counts["fail"],
                "skipped": counts["skipped"],
                "errors": counts["error"],
                "checks": [
                    {
                        "check": o.check_type,
                        "status": o.status,
                        "detail": o.detail,
                    }
                    for o in outcomes
                ],
            },
        )
        # py_test results additionally mirror the test_execution convention
        # (eval_exec / generated_tests emit it) so the timeline shows the
        # locked-container measurement the same way.
        for o in outcomes:
            if o.test_summary:
                s = o.test_summary
                emit(
                    session,
                    run_id=run.id,
                    sandbox_id=sandbox_id,
                    event_type="test_execution",
                    payload={
                        "phase": "suite_check",
                        "ok": bool(s.get("ok")),
                        "collected": s.get("collected", 0),
                        "passed": s.get("passed", 0),
                        "failed": s.get("failed", 0),
                        "errors": s.get("errors", 0),
                        "skipped": s.get("skipped", 0),
                        "duration_seconds": s.get("duration_seconds"),
                    },
                )
        session.commit()


async def _evaluate_all(
    *,
    session_factory: sessionmaker[Session],
    runtime: SandboxRuntime,
    settings: Any,
    run: Run,
    evaluator_fn: EvaluatorFn | None,
    rubric: Rubric | None = None,
    sandbox_ids: Collection[uuid.UUID] | None = None,
    model: str | None = None,
) -> None:
    """Score every sandbox that produced artifacts; per-sandbox try/except.

    ``rubric``/``sandbox_ids``/``model`` are the PR5 re-score overrides —
    defaults reproduce the original lifecycle pass (latest approved rubric,
    all artifact-bearing sandboxes, run's evaluator_model).
    """
    if rubric is None:
        rubric = _latest_approved_rubric(session_factory, run.id)
    if rubric is None:
        _emit_run_event(
            session_factory,
            run.id,
            event_type="error",
            payload={"stage": "evaluation", "message": "no approved rubric found"},
        )
        return

    if evaluator_fn is None:
        try:
            evaluator_fn = _import_evaluator()
        except Exception as exc:  # noqa: BLE001 — evaluator module absent/broken
            log.warning("evaluator unavailable: %s", exc)
            _emit_run_event(
                session_factory,
                run.id,
                event_type="error",
                payload={
                    "stage": "evaluation",
                    "message": f"evaluator unavailable: {type(exc).__name__}: {exc}",
                },
            )
            return

    # PR29 — judge panel: an explicit ``model`` override (re-score)
    # re-judges with that ONE judge; otherwise the run's panel
    # (config.evaluator_models — evaluator_model is panel[0]) each scores
    # every artifact independently, on its own cred. model_name is the
    # final fallback so a panel can never be empty on a well-formed run.
    judges = (
        [model]
        if model
        else (judge_models(run.config, run.model_name) or [run.model_name])
    )
    timeout = float(settings.EVAL_TIMEOUT_SECONDS)

    sandboxes = _sandboxes_with_artifacts(session_factory, run.id, sandbox_ids)
    # Eval workspaces must live under the data root so the sandbox runtime's
    # host-path translation (HOST_DATA_ROOT in compose) covers their -v
    # sources — the evaluator's own default anchors at the repo root, which
    # inside the api container is /app (not daemon-visible).
    eval_root = Path(runtime.artifact_root).parent / "eval"
    retries = int(getattr(settings, "EVAL_SCORE_RETRIES", 1))
    # PR26: deterministic checks the run's suite task declares — resolved
    # once per eval pass (a rescore re-reads the current suite.yaml).
    check_entries, check_suite_dir = _suite_check_specs(run, settings)
    for sb in sandboxes:
        artifact_dir = Path(sb.artifact_path)  # type: ignore[arg-type]
        if check_entries:
            # Post-artifact step — measured before the judge runs so the
            # objective verdict is persisted even if eval later fails.
            await _run_suite_checks(
                session_factory=session_factory,
                run=run,
                sandbox_id=sb.id,
                artifact_dir=artifact_dir,
                entries=check_entries,
                suite_dir=check_suite_dir,
            )
        kwargs = {
            "run_id": run.id,
            "sandbox_id": sb.id,
            "rubric_id": rubric.id,
            "rubric_version": rubric.version,
            "rubric": rubric.content,
            "artifact_dir": artifact_dir,
            "workspace_dir": artifact_dir / "workspace",
            # PR25: output-only submissions — the evaluator stages
            # output.txt as the judge's submission instead of a workspace
            # (ignored for container cells).
            "prompt_output_path": (
                artifact_dir / prompt_cell.OUTPUT_FILENAME
                if prompt_cell.is_prompt_config(sb.harness_config)
                else None
            ),
            "transcript_path": artifact_dir / "transcript.log",
            "terminal_reason": sb.terminal_reason,
            "model": "",  # set per judge inside the panel loop below
            "gateway_base_url": settings.GATEWAY_URL,
            "session_factory": session_factory,
            "timeout_seconds": timeout,
            # F7 DI knobs (accepted extras beyond the §5 contract):
            "eval_root": eval_root,
            # Same slack the outer wait_for grants — lets the evaluator
            # bound its direct-judge fallback inside the attempt deadline.
            "deadline_margin_seconds": _EVAL_TIMEOUT_MARGIN_SECONDS,
            "image": settings.SANDBOX_IMAGE,
            # Judge per-request output cap — thinking judges burn tokens on
            # reasoning before scores.json; the 4096 default truncates.
            "judge_max_tokens": int(settings.EVAL_JUDGE_MAX_TOKENS),
        }
        # One judge at a time (sequential — the eval sandbox id + staged
        # workspace are per-sandbox, not per-judge). Each judge gets its
        # own cred + retry budget; one judge's failure mustn't zero the
        # others' scores, so baselines and failure markers are per judge.
        for judge in judges:
            kwargs["model"] = judge
            kwargs["local_judge"] = pacing.provider_for_model(settings, judge) in pacing.LOCAL_PROVIDERS
            # Baseline BEFORE the first attempt: a re-score can target an
            # already-judged sandbox, so "any Score rows" ≠ "this pass
            # scored". Retry progress is measured by the DELTA — otherwise
            # a flaky empty re-score would satisfy the check on stale rows
            # and never retry. Re-snapshotted PER JUDGE on the total count
            # (not judge-scoped): judge A's rows can't satisfy judge B's
            # delta, and unstamped/legacy rows still count as output.
            baseline = _sandbox_score_count(session_factory, sb.id)
            attempts = 0
            for attempt in range(1, retries + 2):
                attempts = attempt
                try:
                    # Fresh cred per attempt: a single cred's TTL can't
                    # span N sequential evals — and a panel never shares
                    # one cred across judges (PR29). max_tokens = tokens
                    # already spent + EVAL_TOKEN_BUDGET so the judge keeps
                    # its own allowance instead of 429ing on participant
                    # spend.
                    kwargs["gateway_cred"] = mint_cred(
                        run_id=run.id,
                        sandbox_id=sb.id,
                        secret=settings.GATEWAY_CRED_SECRET,
                        ttl_seconds=int(settings.EVAL_TIMEOUT_SECONDS) + 600,
                        max_tokens=_run_tokens_used(session_factory, run.id)
                        + int(settings.EVAL_TOKEN_BUDGET),
                    )
                    # Error-feedback retry: the previous attempt's
                    # eval_scores_failed detail lands in the retry's
                    # EVAL_PROMPT.md (prior_error) — a blind respawn with the
                    # same prompt flakes identically. Queried fresh per attempt
                    # so a second retry sees the latest failure.
                    kwargs["prior_error"] = (
                        _last_eval_scores_error(session_factory, sb.id)
                        if attempt > 1
                        else None
                    )
                    await asyncio.wait_for(
                        evaluator_fn(**kwargs),
                        timeout=timeout + _EVAL_TIMEOUT_MARGIN_SECONDS,
                    )
                except Exception as exc:  # noqa: BLE001 — per-sandbox isolation
                    log.warning(
                        "evaluation failed for sandbox %s (judge %s): %s",
                        sb.id,
                        judge,
                        exc,
                    )
                    _emit_run_event(
                        session_factory,
                        run.id,
                        sandbox_id=sb.id,
                        event_type="error",
                        payload={
                            "stage": "evaluation",
                            "judge_model": judge,
                            "message": f"{type(exc).__name__}: {exc}",
                        },
                    )
                    break  # infra failure is deterministic — don't retry
                if _sandbox_score_count(session_factory, sb.id) > baseline:
                    break  # this pass produced scores — done with this judge
                if attempt <= retries:
                    # Judge ran but produced no usable scores.json — the
                    # observed failure mode of small local judge models
                    # (malformed JSON, stuck-loop wander). Bounded re-spawn,
                    # event-visible.
                    _emit_run_event(
                        session_factory,
                        run.id,
                        sandbox_id=sb.id,
                        event_type="checkpoint",
                        payload={
                            "stage": "eval_retry",
                            "judge_model": judge,
                            "attempt": attempt + 1,
                            "reason": "no scores produced",
                        },
                    )
            if _sandbox_score_count(session_factory, sb.id) == baseline:
                # THIS judge produced nothing across its attempts — the
                # per-judge eval-failure marker (PR29: judge A scoring
                # must not mask judge B's total failure, so the check is
                # "no delta for this judge", not "no rows at all").
                # Per-attempt failures leave their own error events, but
                # nothing else distinguishes "gave up" from "retry pending"
                # on the timeline, and the results API derives its
                # per-sandbox eval_error from these.
                _emit_run_event(
                    session_factory,
                    run.id,
                    sandbox_id=sb.id,
                    event_type="error",
                    payload={
                        "stage": "evaluation",
                        "phase": "eval_failed",
                        "judge_model": judge,
                        "message": (
                            f"judge {judge} produced no usable scores after "
                            f"{attempts} attempt(s)"
                        ),
                        "attempts": attempts,
                    },
                )


# ---------------------------------------------------------------------------
# local-run pacing — capped watcher + FIFO promotion
# ---------------------------------------------------------------------------


async def _watch_cells_paced(
    *,
    session_factory: sessionmaker[Session],
    runtime: SandboxRuntime,
    settings: Any,
    run: Run,
    budgets: dict[str, Any],
    seed: Any,
    cap: int,
    running_ids: list[uuid.UUID],
) -> None:
    """Monitor spawned cells and promote queued ones as slots free.

    Task-set pacing: each task owns one cell — either ``watch_sandbox``
    on an already-running sandbox or ``spawn_queued`` + ``watch_sandbox``
    for a claimed cell. A task completes only after its cell is terminal
    (or its launch failed), so a completion frees one pacing slot and the
    oldest queued cell claims it via ``claim_queued_sandbox``. The claim
    runs synchronously on the loop thread between ``asyncio.wait``s, so
    FIFO order is deterministic (oldest ``sandbox_queued`` event first).
    """

    async def _launch_then_watch(sid: uuid.UUID) -> None:
        spawned = await provision.spawn_queued(
            session_factory=session_factory,
            runtime=runtime,
            run=run,
            budgets=budgets,
            sandbox_id=sid,
            settings=settings,
            seed=seed,
        )
        if spawned is not None:
            await monitor.watch_sandbox(
                session_factory=session_factory,
                runtime=runtime,
                settings=settings,
                run_id=run.id,
                sandbox_id=sid,
                budgets=budgets,
            )

    watch: set[asyncio.Task[None]] = {
        asyncio.create_task(
            monitor.watch_sandbox(
                session_factory=session_factory,
                runtime=runtime,
                settings=settings,
                run_id=run.id,
                sandbox_id=sid,
                budgets=budgets,
            )
        )
        for sid in running_ids
    }
    while True:
        # Fill freed slots FIFO — each claim flips queued→provisioning
        # (holding the slot) before its launch task starts.
        while len(watch) < cap:
            sid = pacing.claim_queued_sandbox(session_factory, run.id, cap=cap)
            if sid is None:
                break
            watch.add(asyncio.create_task(_launch_then_watch(sid)))
        if not watch:
            return
        _done, watch = await asyncio.wait(
            watch, return_when=asyncio.FIRST_COMPLETED
        )


# ---------------------------------------------------------------------------
# main entry
# ---------------------------------------------------------------------------


async def run_lifecycle(
    run_id: uuid.UUID,
    *,
    session_factory: sessionmaker[Session],
    runtime: SandboxRuntime,
    settings: Any,
    evaluator_fn: EvaluatorFn | None = None,
) -> None:
    """Drive one approved run to a terminal state. Never raises."""
    try:
        await _run_lifecycle(
            run_id,
            session_factory=session_factory,
            runtime=runtime,
            settings=settings,
            evaluator_fn=evaluator_fn,
        )
    except Exception as exc:
        log.exception("run_lifecycle: run %s crashed", run_id)
        try:
            _set_run_status(
                session_factory,
                run_id,
                "failed",
                finished=True,
                event_payload={
                    "type": "error",
                    "stage": "lifecycle",
                    "message": f"{type(exc).__name__}: {exc}",
                },
            )
        except Exception:
            log.exception("run_lifecycle: could not mark run %s failed", run_id)


async def _run_lifecycle(
    run_id: uuid.UUID,
    *,
    session_factory: sessionmaker[Session],
    runtime: SandboxRuntime,
    settings: Any,
    evaluator_fn: EvaluatorFn | None,
) -> None:
    run = _load_run(session_factory, run_id)
    if run is None:
        log.warning("run_lifecycle: run %s not found", run_id)
        return
    if run.status != "provisioning":
        # Bail rather than spawn: a run only reaches this task via
        # place_run/claim_next_queued (both leave it 'provisioning'), so any
        # other status means the run was cancelled or swept between the
        # claim and task start — spawning sandboxes now would defy that.
        log.warning(
            "run_lifecycle: run %s is %r, expected 'provisioning' — aborting",
            run_id,
            run.status,
        )
        return

    # 1) Provision one sandbox per expanded matrix cell (sequential awaits).
    # PR25: cells selecting the prompt pseudo-adapter skip docker entirely —
    # prompt_cell runs the single gateway call + finalize inline and lands
    # an already-terminal, artifact-bearing sandbox row by the time it
    # returns.
    configs = _sandbox_configs(run)

    # PR24 — materialize the task-input seed ONCE for the run before any
    # cell spawns (per-run idempotent — every cell copies the same tree).
    # A failing seed (dead remote, unsafe archive) fails the run fast here
    # instead of retrying from scratch in each sequential cell while the
    # run holds the pacing slot. Prompt cells have no workspace — the seed
    # is only fetched when at least one container cell exists.
    seed = None
    if any(not prompt_cell.is_prompt_config(hc) for hc in configs):
        try:
            seed = await provision.prepare_seed(
                run=run, runtime=runtime, settings=settings
            )
        except Exception as exc:  # noqa: BLE001 — any prep failure fails the run
            _set_run_status(
                session_factory,
                run_id,
                "failed",
                finished=True,
                event_payload={
                    "type": "error",
                    "stage": "seed",
                    "message": f"{type(exc).__name__}: {exc}",
                },
            )
            return

    # Local-run pacing: an all-local run caps CONCURRENTLY-ACTIVE sandboxes
    # (SANDBOX_MAX_CONCURRENT_LOCAL, 0 = off). Cells past the cap park as
    # status="queued" BEFORE docker run and promote FIFO when siblings go
    # terminal — the paced watcher in step 2 drives the claims.
    cap = pacing.local_sandbox_cap(settings, run)
    for hc in configs:
        if prompt_cell.is_prompt_config(hc):
            # Prompt cells hold no pacing slot: they never spawn a
            # container and finish inline before the monitor exists.
            await prompt_cell.run_prompt_cell(
                session_factory=session_factory,
                runtime=runtime,
                run_id=run_id,
                harness_config=hc,
                settings=settings,
            )
        elif cap and pacing.active_sandbox_count(session_factory, run_id) >= cap:
            provision.queue_sandbox(
                session_factory,
                run_id,
                hc,
                cap=cap,
            )
        else:
            await provision.spawn_one(
                session_factory=session_factory,
                runtime=runtime,
                run_id=run_id,
                harness_config=hc,
                settings=settings,
                seed=seed,
            )

    running_ids = _running_sandbox_ids(session_factory, run_id)
    artifact_sandboxes = _sandboxes_with_artifacts(session_factory, run_id)
    queued_count = pacing.queued_sandbox_count(session_factory, run_id) if cap else 0
    if not running_ids and not artifact_sandboxes and not queued_count:
        _set_run_status(
            session_factory,
            run_id,
            "failed",
            finished=True,
            event_payload={
                "type": "error",
                "stage": "provisioning",
                "message": "all sandbox spawns failed",
            },
        )
        return

    # 2) running → monitor all spawned sandboxes to terminal (prompt cells
    # are already terminal with artifacts — only containers enter the
    # monitor).
    prompt_done = sum(
        1 for sb in artifact_sandboxes if prompt_cell.is_prompt_config(sb.harness_config)
    )
    event_payload: dict[str, Any] = {
        "stage": "sandboxes_running",
        "count": len(running_ids),
    }
    if prompt_done:
        event_payload["prompt_cells"] = prompt_done
    if queued_count:
        event_payload["queued"] = queued_count
        event_payload["sandbox_cap"] = cap
    _set_run_status(session_factory, run_id, "running", event_payload=event_payload)
    budgets = provision.budgets_for(run)
    if cap:
        # Paced watcher: monitors run for spawned cells; each terminal
        # sibling frees a pacing slot and the oldest queued cell claims it
        # (provision.launch + monitor) until the queue drains.
        await _watch_cells_paced(
            session_factory=session_factory,
            runtime=runtime,
            settings=settings,
            run=run,
            budgets=budgets,
            seed=seed,
            cap=cap,
            running_ids=running_ids,
        )
    else:
        await asyncio.gather(
            *(
                monitor.watch_sandbox(
                    session_factory=session_factory,
                    runtime=runtime,
                    settings=settings,
                    run_id=run_id,
                    sandbox_id=sid,
                    budgets=budgets,
                )
                for sid in running_ids
            )
        )

    # 3) evaluating.
    _set_run_status(
        session_factory,
        run_id,
        "evaluating",
        event_payload={"stage": "evaluation_started"},
    )
    # Re-load: run.config is already loaded; refresh for updated fields not needed.
    await _evaluate_all(
        session_factory=session_factory,
        runtime=runtime,
        settings=settings,
        run=run,
        evaluator_fn=evaluator_fn,
    )

    # 4) done iff the run produced ≥1 Score row.
    score_count = _score_count(session_factory, run_id)
    if score_count > 0:
        _set_run_status(
            session_factory,
            run_id,
            "done",
            finished=True,
            event_payload={"stage": "run_completed", "scores": score_count},
        )
    else:
        _set_run_status(
            session_factory,
            run_id,
            "failed",
            finished=True,
            event_payload={
                "type": "error",
                "stage": "evaluation",
                "message": "evaluation produced no scores",
            },
        )


# ---------------------------------------------------------------------------
# re-score (PR5)
# ---------------------------------------------------------------------------


async def _await_rescore_slot(
    session_factory: sessionmaker[Session], run_id: uuid.UUID
) -> None:
    """Park until no OTHER run holds the execution slot.

    The API's ``_prepare_rescore`` moves the run to ``evaluating`` at request
    time, so the rescore already owns a queue position — only OTHER runs in
    ``pacing.ACTIVE_RUN_STATES`` can be ahead of us. ``queued`` runs never
    overtake (our ``evaluating`` status blocks ``claim_next_queued``) and
    promote when this pass finishes — the runner's ``finally`` calls
    ``promote_next_queued`` just like ``_lifecycle_then_promote`` does.

    Emits one ``rescore_queued`` checkpoint when the wait actually parks —
    the timeline-visible counterpart to pacing's ``run_queued``. A vanished
    run (hard delete racing a direct, non-API call) exits the wait quietly.
    """
    parked = False
    while True:
        with session_factory() as session:
            if session.get(Run, run_id) is None:
                log.warning("rescore_run: run %s vanished mid-wait", run_id)
                return
            active = int(
                session.scalar(
                    select(func.count(Run.id)).where(
                        Run.id != run_id,
                        Run.status.in_(pacing.ACTIVE_RUN_STATES),
                    )
                )
                or 0
            )
        if active == 0:
            return
        if not parked:
            parked = True
            _emit_run_event(
                session_factory,
                run_id,
                event_type="checkpoint",
                payload={
                    "stage": "rescore_queued",
                    "ahead": active,
                    "reason": "another run holds the execution slot",
                },
            )
        await asyncio.sleep(_RESCORE_SLOT_POLL_SECONDS)


def _enter_evaluating(
    session_factory: sessionmaker[Session],
    run_id: uuid.UUID,
    payload: dict[str, Any],
) -> None:
    """done|failed → evaluating + clear finished_at + event, in one tx."""
    with session_factory() as session:
        run = session.get(Run, run_id)
        if run is None:
            return
        try:
            state_machine.transition(session, run, "evaluating")
        except state_machine.InvalidTransition:
            run.status = "evaluating"
        run.finished_at = None
        emit(session, run_id=run.id, event_type="checkpoint", payload=payload)
        session.commit()


def _finalize_rescore(
    session_factory: sessionmaker[Session],
    run_id: uuid.UUID,
    *,
    rubric: Rubric | None,
    message: str | None = None,
) -> None:
    """Close a rescore pass: done iff ≥1 Score row exists for the run (the
    lifecycle rule — old scores still count, so a no-op rescore restores the
    previous terminal state). The ``rescore_finished`` checkpoint is emitted
    unconditionally — on ``message`` it carries the error in the payload and
    a separate error event preserves the type=error/stage=rescore signal, so
    timeline consumers always see the terminal checkpoint."""
    total = _score_count(session_factory, run_id)
    to_status = "done" if total > 0 else "failed"
    payload: dict[str, Any] = {
        "stage": "rescore_finished",
        "scores_total": total,
        "to_status": to_status,
    }
    if rubric is not None:
        payload["rubric_id"] = str(rubric.id)
        payload["rubric_version"] = rubric.version
        payload["scores_for_rubric"] = _rubric_score_count(
            session_factory, run_id, rubric.id
        )
    if message is not None:
        payload["error"] = message
        _emit_run_event(
            session_factory,
            run_id,
            event_type="error",
            payload={"stage": "rescore", "message": message},
        )
    _set_run_status(
        session_factory,
        run_id,
        to_status,
        finished=True,
        event_payload=payload,
    )


async def rescore_run(
    run_id: uuid.UUID,
    *,
    session_factory: sessionmaker[Session],
    runtime: SandboxRuntime,
    settings: Any,
    evaluator_fn: EvaluatorFn | None = None,
    rubric_id: uuid.UUID | None = None,
    sandbox_ids: Collection[uuid.UUID] | None = None,
    model: str | None = None,
) -> None:
    """Re-run the evaluator on stored artifacts. Never raises.

    ``rubric_id`` targets a specific rubric version (default: latest
    approved); ``sandbox_ids`` limits which artifacts get re-judged;
    ``model`` overrides the run's evaluator_model for this pass only.
    """
    try:
        await _rescore_run(
            run_id,
            session_factory=session_factory,
            runtime=runtime,
            settings=settings,
            evaluator_fn=evaluator_fn,
            rubric_id=rubric_id,
            sandbox_ids=sandbox_ids,
            model=model,
        )
    except Exception as exc:
        log.exception("rescore_run: run %s crashed", run_id)
        try:
            _finalize_rescore(
                session_factory,
                run_id,
                rubric=None,
                message=f"{type(exc).__name__}: {exc}",
            )
        except Exception:
            log.exception("rescore_run: could not finalize run %s", run_id)


async def _rescore_run(
    run_id: uuid.UUID,
    *,
    session_factory: sessionmaker[Session],
    runtime: SandboxRuntime,
    settings: Any,
    evaluator_fn: EvaluatorFn | None,
    rubric_id: uuid.UUID | None,
    sandbox_ids: Collection[uuid.UUID] | None,
    model: str | None,
) -> None:
    run = _load_run(session_factory, run_id)
    if run is None:
        log.warning("rescore_run: run %s not found", run_id)
        return

    # Cancelled runs never spawned — there is nothing to re-score. They are
    # in RUN_TERMINAL for sweep/delete semantics, but a rescore pass here
    # would force cancelled→evaluating then land the run on "failed".
    if run.status == "cancelled":
        log.warning("rescore_run: run %s is cancelled — nothing to re-score", run_id)
        _emit_run_event(
            session_factory,
            run_id,
            event_type="error",
            payload={
                "stage": "rescore",
                "message": "run is cancelled — nothing to re-score",
            },
        )
        return

    # Re-score only applies to terminal runs (the API 409s earlier; this is
    # defense-in-depth for non-API callers). The one legitimate non-terminal
    # state is ``evaluating`` WITH a rescore marker — _prepare_rescore moves
    # the run into evaluating + stamps rescore_requested in the request tx,
    # before the task ever runs. A bare mid-flight state (running,
    # provisioning, or a first-pass evaluating with no marker) must NOT be
    # hijacked — _enter_evaluating force-sets status on InvalidTransition,
    # so the transition table alone is not the guard.
    if run.status not in state_machine.RUN_TERMINAL and not (
        run.status == "evaluating"
        and _has_rescore_marker(session_factory, run_id)
    ):
        log.warning(
            "rescore_run: run %s is %r — re-score only applies to terminal runs",
            run_id,
            run.status,
        )
        _emit_run_event(
            session_factory,
            run_id,
            event_type="error",
            payload={
                "stage": "rescore",
                "message": (
                    f"run is {run.status!r} — re-score only applies "
                    "to terminal runs"
                ),
            },
        )
        return

    if rubric_id is not None:
        rubric = _rubric_for_run(session_factory, run_id, rubric_id)
        if rubric is None:
            _emit_run_event(
                session_factory,
                run_id,
                event_type="error",
                payload={
                    "stage": "rescore",
                    "message": f"rubric {rubric_id} not found for run",
                },
            )
            _finalize_rescore(session_factory, run_id, rubric=None)
            return
    else:
        rubric = _latest_approved_rubric(session_factory, run_id)
        if rubric is None:
            _emit_run_event(
                session_factory,
                run_id,
                event_type="error",
                payload={"stage": "rescore", "message": "no approved rubric found"},
            )
            _finalize_rescore(session_factory, run_id, rubric=None)
            return

    # Slot discipline (same lane as the PR15 submission queue): a rescore
    # triggered while another run executes must NOT run its evaluator
    # concurrently — park until the slot frees. The rescore's own run is
    # already `evaluating` (API path), so it can't be overtaken by queued
    # runs; they promote from this pass's finally instead.
    await _await_rescore_slot(session_factory, run_id)

    # The wait may have raced a delete/cancel of this run — re-check before
    # spending judge calls on a gone/dead run.
    run = _load_run(session_factory, run_id)
    if run is None:
        return

    # PR29 — an explicit ``model`` re-judges with that one judge;
    # otherwise a rescore re-runs the run's whole panel.
    rescore_judges = (
        [model]
        if model
        else (judge_models(run.config, run.model_name) or [run.model_name])
    )
    _enter_evaluating(
        session_factory,
        run_id,
        {
            "stage": "rescore_started",
            "rubric_id": str(rubric.id),
            "rubric_version": rubric.version,
            "model": rescore_judges[0],
            "judge_models": rescore_judges,
            "sandbox_ids": (
                [str(s) for s in sorted(sandbox_ids)] if sandbox_ids else "all"
            ),
        },
    )
    await _evaluate_all(
        session_factory=session_factory,
        runtime=runtime,
        settings=settings,
        run=run,
        evaluator_fn=evaluator_fn,
        rubric=rubric,
        sandbox_ids=sandbox_ids,
        model=model,
    )
    _finalize_rescore(session_factory, run_id, rubric=rubric)
