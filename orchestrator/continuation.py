"""Sandbox continuations (PR16) — "add next prompt" multi-turn runs.

A continuation respawns a FRESH container for an existing terminal Sandbox
row on its still-live workspace bind-mount — **resume-in-place at the
filesystem level**: ``data/sandboxes/<sid>/workspace`` persists after
``docker rm``, so the new phase continues byte-for-byte on the agent's files
(no stale artifact copy-back). The harness process itself starts cold —
conversation context does NOT carry over — so the follow-up prompt is
wrapped with the original task and a continuation note (``wrap_task_prompt``).

Phase model: the FIRST execution is phase 1; each accepted follow-up
increments it. The same Sandbox row flows ``done|failed → provisioning →
running → terminal`` again (state_machine), the timeline gets new
``continuation_started``/``continuation_completed`` checkpoints, and the
workspace is re-snapshotted under ``continuations/<phase>/`` — a NEW
artifact path per phase (monitor.finalize_sandbox(phase=…)). The run
re-holds the single pacing slot for the duration (done|failed →
provisioning → running → evaluating → done), re-scored per-sandbox via
``lifecycle._evaluate_all(sandbox_ids=…)`` so unchanged siblings don't
drift on judge nondeterminism.

Concurrency: several sandboxes of one run may run continuations at once —
the API admits a new one while every non-terminal sibling is itself a
continuation; the run returns to terminal when the LAST finisher's
:func:`close_run_if_idle` commit wins (single-session check+transition —
same claim pattern as ``pacing.claim_next_queued``).
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from db.models import Event, Run, Sandbox, Score
from gateway.metering import tokens_used
from sandbox.runtime import SandboxRuntime
from skillpack_import.store import imported_root

from . import lifecycle, monitor, pacing, provision, state_machine
from .adapters import adapter_image, get_adapter
from .events import emit

log = logging.getLogger(__name__)

#: Sandbox statuses eligible for a follow-up (terminal only).
CONTINUABLE_SANDBOX_STATES = ("done", "failed")
#: Non-terminal sandbox statuses — used to detect "still-active" phases.
ACTIVE_SANDBOX_STATES = ("provisioning", "running", "terminating")


def _now() -> datetime:
    return datetime.now(UTC)


def next_phase(harness_config: dict[str, Any] | None) -> int:
    """Phase number of the NEXT continuation (first follow-up → 2)."""
    return len((harness_config or {}).get("continuations") or []) + 2


def wrap_task_prompt(task_prompt: str, follow_up: str, phase: int) -> str:
    """TASK_PROMPT for a continuation phase.

    The agent process is cold-started each phase, so the follow-up is
    grounded with the original task plus a note that the workspace already
    contains prior work.
    """
    return (
        f"{task_prompt.rstrip()}\n\n"
        f"[continuation — phase {phase}]\n"
        "The workspace already contains the results of a previous agent "
        "session working on the task above — build on that existing work "
        "rather than starting over.\n"
        f"Follow-up instruction: {follow_up.strip()}"
    )


# ---------------------------------------------------------------------------
# DB helpers (sync — same convention as lifecycle/monitor)
# ---------------------------------------------------------------------------


def _load_run(session_factory: sessionmaker[Session], run_id: uuid.UUID) -> Run | None:
    with session_factory() as session:
        return session.get(Run, run_id)


def _load_sandbox(
    session_factory: sessionmaker[Session], sandbox_id: uuid.UUID
) -> Sandbox | None:
    with session_factory() as session:
        return session.get(Sandbox, sandbox_id)


def _run_tokens_used(session_factory: sessionmaker[Session], run_id: uuid.UUID) -> int:
    with session_factory() as session:
        return tokens_used(session, run_id)


def _max_sandbox_event_id(
    session_factory: sessionmaker[Session], sandbox_id: uuid.UUID
) -> int:
    """Phase boundary for the monitor's done/budget scoping: every event id
    above this belongs to the NEW phase. Event ids are monotonic — exact
    where a timestamp cutoff isn't (sqlite truncates to seconds)."""
    with session_factory() as session:
        return int(
            session.execute(
                select(func.max(Event.id)).where(Event.sandbox_id == sandbox_id)
            ).scalar_one()
            or 0
        )


def _emit(
    session_factory: sessionmaker[Session],
    run_id: uuid.UUID,
    *,
    sandbox_id: uuid.UUID | None = None,
    event_type: str = "checkpoint",
    payload: dict[str, Any],
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


def _mark_respawned(
    session_factory: sessionmaker[Session],
    *,
    sandbox_id: uuid.UUID,
    container_id: str,
) -> None:
    """Sandbox → running with fresh per-phase fields (like _mark_spawned)."""
    with session_factory() as session:
        sb = session.get(Sandbox, sandbox_id)
        if sb is None:
            return
        sb.docker_container_id = container_id
        sb.started_at = _now()
        sb.finished_at = None
        sb.terminal_reason = None
        try:
            state_machine.transition(session, sb, "running")
        except state_machine.InvalidTransition:
            sb.status = "running"
        session.commit()


def _mark_spawn_failed(
    session_factory: sessionmaker[Session],
    *,
    run_id: uuid.UUID,
    sandbox_id: uuid.UUID,
    message: str,
) -> None:
    with session_factory() as session:
        sb = session.get(Sandbox, sandbox_id)
        if sb is not None and sb.status not in state_machine.SANDBOX_TERMINAL:
            try:
                state_machine.transition(session, sb, "failed")
            except state_machine.InvalidTransition:
                sb.status = "failed"
            sb.terminal_reason = "error"
            sb.finished_at = _now()
        emit(
            session,
            run_id=run_id,
            sandbox_id=sandbox_id,
            event_type="error",
            payload={"stage": "continuation_spawn", "message": message},
        )
        session.commit()


def _set_run_status(
    session_factory: sessionmaker[Session],
    run_id: uuid.UUID,
    to_status: str,
    *,
    finished: bool = False,
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
        session.commit()


def close_run_if_idle(
    session_factory: sessionmaker[Session], run_id: uuid.UUID
) -> bool:
    """Return the run to terminal when no sandbox phase is still active.

    One committed session = the claim — the LAST finishing continuation
    flips ``running|provisioning → evaluating → done|failed``; earlier
    finishers see non-terminal siblings and leave the status alone, and a
    second call after the close sees a terminal run and no-ops. Terminal
    verdict mirrors run_lifecycle's: ``done`` iff ≥1 Score row exists.
    """
    with session_factory() as session:
        run = session.get(Run, run_id)
        if run is None or run.status in state_machine.RUN_TERMINAL:
            return True
        active = session.scalar(
            select(func.count(Sandbox.id)).where(
                Sandbox.run_id == run_id,
                Sandbox.status.in_(ACTIVE_SANDBOX_STATES),
            )
        )
        if active:
            return False
        try:
            state_machine.transition(session, run, "evaluating")
        except state_machine.InvalidTransition:
            run.status = "evaluating"
        scores = int(
            session.execute(
                select(func.count(Score.id))
                .select_from(Score)
                .join(Sandbox, Score.sandbox_id == Sandbox.id)
                .where(Sandbox.run_id == run_id)
            ).scalar_one()
        )
        if scores > 0:
            to_status = "done"
            event_type, payload = "checkpoint", {
                "stage": "run_completed",
                "scores": scores,
                "continuation": True,
            }
        else:
            to_status = "failed"
            event_type, payload = "error", {
                "stage": "evaluation",
                "message": "evaluation produced no scores",
            }
        try:
            state_machine.transition(session, run, to_status)
        except state_machine.InvalidTransition:
            run.status = to_status
        run.finished_at = _now()
        emit(session, run_id=run_id, event_type=event_type, payload=payload)
        session.commit()
        return True


# ---------------------------------------------------------------------------
# main entry — drive one continuation phase for one sandbox
# ---------------------------------------------------------------------------


async def run_continuation(
    run_id: uuid.UUID,
    sandbox_id: uuid.UUID,
    *,
    session_factory: sessionmaker[Session],
    runtime: SandboxRuntime,
    settings: Any,
    evaluator_fn: Any | None = None,
) -> None:
    """Respawn + monitor + re-score one continued sandbox. Never raises."""
    try:
        await _run_continuation(
            run_id,
            sandbox_id,
            session_factory=session_factory,
            runtime=runtime,
            settings=settings,
            evaluator_fn=evaluator_fn,
        )
    except Exception as exc:
        log.exception(
            "run_continuation: sandbox %s of run %s crashed", sandbox_id, run_id
        )
        try:
            _mark_spawn_failed(
                session_factory,
                run_id=run_id,
                sandbox_id=sandbox_id,
                message=f"{type(exc).__name__}: {exc}",
            )
            close_run_if_idle(session_factory, run_id)
        except Exception:
            log.exception("run_continuation: cleanup failed for %s", sandbox_id)


async def _run_continuation(
    run_id: uuid.UUID,
    sandbox_id: uuid.UUID,
    *,
    session_factory: sessionmaker[Session],
    runtime: SandboxRuntime,
    settings: Any,
    evaluator_fn: Any | None,
) -> None:
    run = _load_run(session_factory, run_id)
    sb = _load_sandbox(session_factory, sandbox_id)
    if run is None or sb is None:
        log.warning(
            "run_continuation: run %s / sandbox %s not found", run_id, sandbox_id
        )
        return

    hc = dict(sb.harness_config or {})
    latest = (hc.get("continuations") or [{}])[-1]
    phase = int(latest.get("phase") or next_phase(hc))
    prompt = str(latest.get("prompt") or "")

    # Per-phase budget: the gateway enforces cred max_tokens against the
    # RUN's cumulative spend, so the phase cred ceiling is tokens already
    # used + the configured per-run allowance (same trick as eval creds).
    # The adjusted budget also goes to the monitor's run-wide check so the
    # phase gets a full fresh allowance instead of insta-terminating.
    budgets = provision.budgets_for(run)
    budgets["token_budget"] = _run_tokens_used(session_factory, run_id) + int(
        budgets["token_budget"]
    )

    sid = str(sandbox_id)
    try:
        adapter = get_adapter(str(hc.get("adapter") or "miniloop"))
        # Restage the sandbox's pack selection into the same staged dir —
        # idempotent and covers a wiped data/sandboxes tree.
        await asyncio.to_thread(
            provision.stage_skillpacks,
            list(hc.get("skill_packs") or []),
            runtime.state_dir / sid / "skillpacks",
            source_root=settings.SKILLPACKS_DIR,
            extra_roots=(imported_root(settings),),
        )
        # Reap any straggler carrying the same container name before
        # re-spawning (finalize normally removes it; crash paths may not).
        try:
            await asyncio.to_thread(runtime.remove, sid)
        except Exception:
            log.debug(
                "continuation: no container to remove for %s", sid, exc_info=True
            )

        headroom = await pacing.wait_for_headroom(
            session_factory=session_factory,
            run_id=run_id,
            sandbox_id=sandbox_id,
            settings=settings,
        )
        if not headroom:
            _mark_spawn_failed(
                session_factory,
                run_id=run_id,
                sandbox_id=sandbox_id,
                message=(
                    "continuation deferred: host memory stayed below "
                    "SPAWN_MIN_FREE_MEM_MB past SPAWN_MEM_WAIT_SECONDS"
                ),
            )
        else:
            cred = provision.mint_sandbox_cred(
                run_id=run_id,
                sandbox_id=sandbox_id,
                secret=settings.GATEWAY_CRED_SECRET,
                budgets=budgets,
            )
            env = provision.build_env(
                adapter,
                cred=cred,
                task_prompt=wrap_task_prompt(run.task_prompt, prompt, phase),
                model_name=run.model_name,
                max_iters=budgets["max_iters"],
                system_prompt_extra=hc.get("system_prompt_extra"),
            )
            spec = provision.build_spec(
                sandbox_id=sandbox_id,
                run=run,
                harness_config=hc,
                runtime=runtime,
                image=adapter_image(adapter, settings.SANDBOX_IMAGE),
                env=env,
            )
            result = await asyncio.to_thread(runtime.spawn, spec)
            _mark_respawned(
                session_factory, sandbox_id=sandbox_id, container_id=result.container_id
            )
            log.info(
                "run_continuation: sandbox %s phase %s -> %s",
                sid,
                phase,
                result.container_name,
            )
    except Exception as exc:
        log.exception("run_continuation: respawn of %s failed", sid)
        try:
            await asyncio.to_thread(runtime.remove, sid)
        except Exception:
            log.debug(
                "continuation: no container to remove for %s", sid, exc_info=True
            )
        _mark_spawn_failed(
            session_factory,
            run_id=run_id,
            sandbox_id=sandbox_id,
            message=f"{type(exc).__name__}: {exc}",
        )

    # The run is executing again regardless of spawn outcome — the close
    # path below restores the terminal verdict.
    _set_run_status(session_factory, run_id, "running")

    # Monitor to terminal — watch_sandbox returns at once on a failed spawn
    # (sandbox row already terminal). The event-id boundary scopes its
    # done/budget checks to this phase (a prior phase's signals must not
    # terminate it); phase lands the snapshot under continuations/<n>/.
    reason = await monitor.watch_sandbox(
        session_factory=session_factory,
        runtime=runtime,
        settings=settings,
        run_id=run_id,
        sandbox_id=sandbox_id,
        budgets=budgets,
        phase=phase,
        since_event_id=_max_sandbox_event_id(session_factory, sandbox_id),
    )
    _emit(
        session_factory,
        run_id,
        sandbox_id=sandbox_id,
        payload={
            "stage": "continuation_completed",
            "phase": phase,
            "terminal_reason": reason,
        },
    )

    # Re-score ONLY this sandbox, and only when the phase actually produced
    # a fresh artifact — a spawn-failed or pre-snapshot-dead continuation
    # still points at the phase-1 dir, and re-judging an unchanged artifact
    # just drifts its scores on judge nondeterminism.
    sb = _load_sandbox(session_factory, sandbox_id)
    artifact_dir = str(getattr(sb, "artifact_path", "") or "")
    if artifact_dir.endswith(f"continuations/{phase}"):
        run = _load_run(session_factory, run_id)
        if run is not None:
            await lifecycle._evaluate_all(
                session_factory=session_factory,
                runtime=runtime,
                settings=settings,
                run=run,
                evaluator_fn=evaluator_fn,
                sandbox_ids={sandbox_id},
            )

    close_run_if_idle(session_factory, run_id)
