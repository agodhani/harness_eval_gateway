"""Background-task registry + startup sweep (F2).

API handlers only *register* ``asyncio.Task``s here; all orchestration lives
in ``orchestrator/``. Tasks are in-process — an api restart kills them, so
:func:`startup_sweep` (run from the FastAPI lifespan) reconciles the DB:

- ``submitted`` runs with no rubric → respawn the rubric task.
- ``provisioning`` / ``running`` / ``evaluating`` → ``failed`` (+ sandboxes
  ``failed``/``error``), best-effort ``docker rm`` by the ``heg.run_id``
  label — accepted MVP limitation, documented in the plan (§6).
- ``rubric_review`` is untouched (human gate).
- ``queued`` (PR15 pacing) is untouched too — a parked run never had an
  in-process task to lose. After the sweep, :func:`promote_next_queued`
  runs once so a run parked behind work the api lost (or behind a run that
  finished while it was down) gets its slot.

``session_factory``/``settings``/``runtime`` are injectable for tests; the
defaults resolve lazily from ``api.*`` so importing this module never pulls
in the app.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from db.models import Rubric, Run, Sandbox
from sandbox.runtime import SandboxRuntime

from . import lifecycle, pacing, state_machine
from . import rubric as rubric_mod
from .events import emit

log = logging.getLogger(__name__)

#: (run_id, task kind) -> in-flight orchestration task. Keyed by kind so an
#: approve landing while the rubric task is finishing doesn't swallow the
#: lifecycle task.
_TASKS: dict[tuple[uuid.UUID, str], asyncio.Task[None]] = {}

_runtime: SandboxRuntime | None = None

#: False once shutdown() starts — queue promotion stops so a task finishing
#: during teardown can't launch a lifecycle that outlives the process.
#: startup_sweep re-arms it so a same-process stop/start cycle keeps working.
_accepting = True

#: Claim retries inside promote_next_queued — a transient DB failure right as
#: the slot frees must not strand the queue (no other in-session re-trigger).
_CLAIM_ATTEMPTS = 3
_CLAIM_RETRY_DELAY_SECONDS = 1.0


# ---------------------------------------------------------------------------
# lazy defaults (avoid import cycles: api.* imports orchestrator.*)
# ---------------------------------------------------------------------------


def _settings() -> Any:
    from api.config import get_settings

    return get_settings()


def _session_factory() -> sessionmaker[Session]:
    from api.deps import get_session_factory

    return get_session_factory()


def get_runtime() -> SandboxRuntime:
    """Process-wide SandboxRuntime built from api settings (artifact root)."""
    global _runtime
    if _runtime is None:
        settings = _settings()
        _runtime = SandboxRuntime(
            artifact_root=settings.ARTIFACT_ROOT,
            host_data_root=settings.HOST_DATA_ROOT or None,
        )
    return _runtime


def reset_runtime() -> None:
    """Test hook: drop the cached runtime."""
    global _runtime
    _runtime = None


# ---------------------------------------------------------------------------
# task registry
# ---------------------------------------------------------------------------


def _track(kind: str, run_id: uuid.UUID, coro: Any) -> asyncio.Task[None]:
    key = (run_id, kind)
    existing = _TASKS.get(key)
    if existing is not None and not existing.done():
        return existing
    task = asyncio.create_task(coro)
    _TASKS[key] = task

    def _discard(t: asyncio.Task[None]) -> None:
        if _TASKS.get(key) is t:
            _TASKS.pop(key, None)

    task.add_done_callback(_discard)
    return task


def start_rubric_task(
    run_id: uuid.UUID,
    *,
    session_factory: sessionmaker[Session] | None = None,
    settings: Any | None = None,
) -> asyncio.Task[None]:
    """Register the rubric-draft background task for a submitted run."""
    return _track(
        "rubric",
        run_id,
        rubric_mod.draft_rubric(
            run_id,
            session_factory=session_factory or _session_factory(),
            settings=settings or _settings(),
        ),
    )


def start_lifecycle_task(
    run_id: uuid.UUID,
    *,
    session_factory: sessionmaker[Session] | None = None,
    settings: Any | None = None,
    runtime: SandboxRuntime | None = None,
    evaluator_fn: Any | None = None,
) -> asyncio.Task[None]:
    """Register the post-approval lifecycle task for a run.

    The task is :func:`_lifecycle_then_promote` — the run's own lifecycle
    followed by a queue-promotion attempt (PR15 pacing), so a finishing run
    hands the execution slot to the oldest queued run without a poller.
    """
    sf = session_factory or _session_factory()
    st = settings or _settings()
    rt = runtime or get_runtime()
    return _track(
        "lifecycle",
        run_id,
        _lifecycle_then_promote(
            run_id,
            session_factory=sf,
            settings=st,
            runtime=rt,
            evaluator_fn=evaluator_fn,
        ),
    )


async def _lifecycle_then_promote(
    run_id: uuid.UUID,
    *,
    session_factory: sessionmaker[Session],
    settings: Any,
    runtime: SandboxRuntime,
    evaluator_fn: Any | None,
) -> None:
    """Drive one run to terminal, then free the execution slot (PR15)."""
    try:
        await lifecycle.run_lifecycle(
            run_id,
            session_factory=session_factory,
            runtime=runtime,
            settings=settings,
            evaluator_fn=evaluator_fn,
        )
    finally:
        # run_lifecycle never raises, but cancellation/teardown also frees
        # the slot — promote on ANY exit. promote_next_queued self-guards:
        # it claims only when no other run is active.
        await promote_next_queued(
            session_factory=session_factory,
            settings=settings,
            runtime=runtime,
            evaluator_fn=evaluator_fn,
        )


async def promote_next_queued(
    *,
    session_factory: sessionmaker[Session],
    settings: Any,
    runtime: SandboxRuntime,
    evaluator_fn: Any | None = None,
) -> None:
    """Claim the oldest queued run and start its lifecycle task.

    No-op while shutting down, when a run still holds the slot, or when the
    queue is empty — the DB claim in :func:`pacing.claim_next_queued` is the
    single-winner gate, so concurrent/duplicate calls are safe.
    """
    nxt: uuid.UUID | None = None
    for attempt in range(_CLAIM_ATTEMPTS):
        if not _accepting:
            return
        try:
            nxt = pacing.claim_next_queued(session_factory)
            break
        except Exception:
            if attempt + 1 >= _CLAIM_ATTEMPTS:
                log.exception("pacing: queue claim failed")
                return
            log.warning("pacing: queue claim failed; retrying", exc_info=True)
            await asyncio.sleep(_CLAIM_RETRY_DELAY_SECONDS)
    if nxt is None:
        return
    log.info("pacing: promoting queued run %s -> provisioning", nxt)
    start_lifecycle_task(
        nxt,
        session_factory=session_factory,
        settings=settings,
        runtime=runtime,
        evaluator_fn=evaluator_fn,
    )


async def shutdown() -> None:
    """Cancel in-flight tasks on api shutdown (lifespan)."""
    global _accepting
    _accepting = False
    tasks = [t for t in _TASKS.values() if not t.done()]
    for t in tasks:
        t.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    _TASKS.clear()


# ---------------------------------------------------------------------------
# startup sweep
# ---------------------------------------------------------------------------


def _stale_runs(
    session_factory: sessionmaker[Session],
) -> list[tuple[uuid.UUID, str, bool]]:
    """(run_id, status, has_rubric) for runs in non-terminal pre-eval states."""
    with session_factory() as session:
        rows = session.execute(
            select(Run.id, Run.status)
            .where(Run.status.in_(["submitted", "provisioning", "running", "evaluating"]))
            .order_by(Run.created_at)
        ).all()
        out = []
        for run_id, status in rows:
            has_rubric = (
                session.execute(
                    select(Rubric.id).where(Rubric.run_id == run_id).limit(1)
                ).first()
                is not None
            )
            out.append((run_id, status, has_rubric))
        return out


def _fail_run_and_sandboxes(
    session_factory: sessionmaker[Session], run_id: uuid.UUID
) -> None:
    """Mark a stale run failed + its non-terminal sandboxes failed/error."""
    with session_factory() as session:
        run = session.get(Run, run_id)
        if run is not None and run.status not in state_machine.RUN_TERMINAL:
            try:
                state_machine.transition(session, run, "failed")
            except state_machine.InvalidTransition:
                run.status = "failed"
            run.finished_at = datetime.now(UTC)
        sandboxes = session.execute(
            select(Sandbox).where(
                Sandbox.run_id == run_id,
                Sandbox.status.in_(["provisioning", "running", "terminating"]),
            )
        ).scalars().all()
        for sb in sandboxes:
            try:
                state_machine.transition(session, sb, "failed")
            except state_machine.InvalidTransition:
                sb.status = "failed"
            sb.terminal_reason = sb.terminal_reason or "error"
            sb.finished_at = datetime.now(UTC)
        if run is not None:
            emit(
                session,
                run_id=run.id,
                event_type="error",
                payload={
                    "stage": "startup_sweep",
                    "message": "run was mid-flight at api restart — marked failed",
                },
            )
        session.commit()


async def startup_sweep(
    *,
    session_factory: sessionmaker[Session] | None = None,
    settings: Any | None = None,
    runtime: SandboxRuntime | None = None,
) -> None:
    """Reconcile DB state with reality after an api restart."""
    global _accepting
    _accepting = True

    sf = session_factory or _session_factory()
    settings = settings or _settings()
    rt = runtime or get_runtime()

    try:
        stale = _stale_runs(sf)
    except Exception:
        log.exception("startup_sweep: could not list stale runs")
        return

    for run_id, status, has_rubric in stale:
        if status == "submitted" and not has_rubric:
            log.info("startup_sweep: respawning rubric task for run %s", run_id)
            start_rubric_task(run_id, session_factory=sf, settings=settings)
            continue
        if status == "submitted":
            # submitted WITH a rubric shouldn't happen; treat as rubric_review?
            # Leave it — a human can still approve once it lands there. Skip.
            continue
        # provisioning/running/evaluating: in-process state is gone — fail.
        log.warning(
            "startup_sweep: run %s was %r at restart — marking failed", run_id, status
        )
        _fail_run_and_sandboxes(sf, run_id)
        try:
            await asyncio.to_thread(rt.remove_run_containers, str(run_id))
        except Exception:
            log.exception("startup_sweep: docker cleanup failed for run %s", run_id)

    # PR15 pacing: kick the queue — a run may be parked behind work the
    # sweep just retired (or behind a run that finished while the api was
    # down). No-op when the slot is still taken or the queue is empty.
    await promote_next_queued(
        session_factory=sf, settings=settings, runtime=rt
    )
