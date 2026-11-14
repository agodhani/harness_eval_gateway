"""Run lifecycle orchestration (F2) — the approve-triggered background task.

``run_lifecycle(run_id)`` sequences the whole post-approval flow:

1. Provision one sandbox per ``harness_configs`` entry (sequential — cheap,
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
import logging
import uuid
from collections.abc import Awaitable, Callable, Collection
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from db.models import Rubric, Run, Sandbox, Score
from gateway.creds import mint_cred
from gateway.metering import tokens_used
from sandbox.runtime import SandboxRuntime

from . import monitor, provision, state_machine
from .events import emit

log = logging.getLogger(__name__)

#: evaluator.evaluate_sandbox kwargs contract (plan §5) — F7 implements.
EvaluatorFn = Callable[..., Awaitable[Any]]

#: Headroom for the outer eval wait beyond the evaluator's own deadline —
#: equal timeouts would let cancellation preempt evaluate_sandbox's
#: soft-stop + scores.json salvage path.
_EVAL_TIMEOUT_MARGIN_SECONDS = 60.0


def _now() -> datetime:
    return datetime.now(UTC)


# ---------------------------------------------------------------------------
# DB helpers (sync — called directly on the event-loop thread; see
# db/session.py for why orchestrator DB work never goes through to_thread)
# ---------------------------------------------------------------------------


def _load_run(session_factory: sessionmaker[Session], run_id: uuid.UUID) -> Run | None:
    with session_factory() as session:
        return session.get(Run, run_id)


def _harness_configs(run: Run) -> list[dict[str, Any]]:
    return list((run.config or {}).get("harness_configs") or [])


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

    evaluator_model = model or (run.config or {}).get("evaluator_model") or run.model_name
    timeout = float(settings.EVAL_TIMEOUT_SECONDS)

    sandboxes = _sandboxes_with_artifacts(session_factory, run.id, sandbox_ids)
    # Eval workspaces must live under the data root so the sandbox runtime's
    # host-path translation (HOST_DATA_ROOT in compose) covers their -v
    # sources — the evaluator's own default anchors at the repo root, which
    # inside the api container is /app (not daemon-visible).
    eval_root = Path(runtime.artifact_root).parent / "eval"
    retries = int(getattr(settings, "EVAL_SCORE_RETRIES", 1))
    for sb in sandboxes:
        artifact_dir = Path(sb.artifact_path)  # type: ignore[arg-type]
        kwargs = {
            "run_id": run.id,
            "sandbox_id": sb.id,
            "rubric_id": rubric.id,
            "rubric_version": rubric.version,
            "rubric": rubric.content,
            "artifact_dir": artifact_dir,
            "workspace_dir": artifact_dir / "workspace",
            "transcript_path": artifact_dir / "transcript.log",
            "terminal_reason": sb.terminal_reason,
            "model": evaluator_model,
            "gateway_base_url": settings.GATEWAY_URL,
            "session_factory": session_factory,
            "timeout_seconds": timeout,
            # F7 DI knobs (accepted extras beyond the §5 contract):
            "eval_root": eval_root,
            "image": settings.SANDBOX_IMAGE,
        }
        for attempt in range(1, retries + 2):
            try:
                # Fresh cred per attempt: a single cred's TTL can't span N
                # sequential evals. max_tokens = tokens already spent +
                # EVAL_TOKEN_BUDGET so the judge keeps its own allowance
                # instead of 429ing on participant spend.
                kwargs["gateway_cred"] = mint_cred(
                    run_id=run.id,
                    sandbox_id=sb.id,
                    secret=settings.GATEWAY_CRED_SECRET,
                    ttl_seconds=int(settings.EVAL_TIMEOUT_SECONDS) + 600,
                    max_tokens=_run_tokens_used(session_factory, run.id)
                    + int(settings.EVAL_TOKEN_BUDGET),
                )
                await asyncio.wait_for(
                    evaluator_fn(**kwargs),
                    timeout=timeout + _EVAL_TIMEOUT_MARGIN_SECONDS,
                )
            except Exception as exc:  # noqa: BLE001 — per-sandbox isolation
                log.warning("evaluation failed for sandbox %s: %s", sb.id, exc)
                _emit_run_event(
                    session_factory,
                    run.id,
                    sandbox_id=sb.id,
                    event_type="error",
                    payload={
                        "stage": "evaluation",
                        "message": f"{type(exc).__name__}: {exc}",
                    },
                )
                break  # infra failure is deterministic — don't retry
            if _sandbox_score_count(session_factory, sb.id) > 0:
                break  # scored — done with this sandbox
            if attempt <= retries:
                # Judge ran but produced no usable scores.json — the observed
                # failure mode of small local judge models (malformed JSON,
                # stuck-loop wander). Bounded re-spawn, event-visible.
                _emit_run_event(
                    session_factory,
                    run.id,
                    sandbox_id=sb.id,
                    event_type="checkpoint",
                    payload={
                        "stage": "eval_retry",
                        "attempt": attempt + 1,
                        "reason": "no scores produced",
                    },
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

    # 1) Provision one sandbox per harness config (sequential awaits).
    configs = _harness_configs(run)
    for hc in configs:
        await provision.spawn_one(
            session_factory=session_factory,
            runtime=runtime,
            run_id=run_id,
            harness_config=hc,
            settings=settings,
        )

    running_ids = _running_sandbox_ids(session_factory, run_id)
    if not running_ids:
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

    # 2) running → monitor all spawned sandboxes to terminal.
    _set_run_status(
        session_factory,
        run_id,
        "running",
        event_payload={"stage": "sandboxes_running", "count": len(running_ids)},
    )
    budgets = provision.budgets_for(run)
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
    previous terminal state). Emits rescore_finished (+ error on message)."""
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
    _set_run_status(
        session_factory,
        run_id,
        to_status,
        finished=True,
        event_payload=(
            payload
            if message is None
            else {"type": "error", "stage": "rescore", "message": message}
        ),
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

    evaluator_model = model or (run.config or {}).get("evaluator_model") or run.model_name
    _enter_evaluating(
        session_factory,
        run_id,
        {
            "stage": "rescore_started",
            "rubric_id": str(rubric.id),
            "rubric_version": rubric.version,
            "model": evaluator_model,
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
