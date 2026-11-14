"""Background-task registry + startup sweep (F2).

API handlers only *register* ``asyncio.Task``s here; all orchestration lives
in ``orchestrator/``. Tasks are in-process — an api restart kills them, so
:func:`startup_sweep` (run from the FastAPI lifespan) reconciles the DB:

- ``submitted`` runs with no rubric → respawn the rubric task.
- ``provisioning`` / ``running`` / ``evaluating`` → ``failed`` (+ sandboxes
  ``failed``/``error``), best-effort ``docker rm`` by the ``heg.run_id``
  label — accepted MVP limitation, documented in the plan (§6).
- ``rubric_review`` is untouched (human gate).

``session_factory``/``settings``/``runtime`` are injectable for tests; the
defaults resolve lazily from ``api.*`` so importing this module never pulls
in the app.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable, Collection, Coroutine
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from db.models import Event, Rubric, Run, Sandbox, Score
from sandbox.runtime import SandboxRuntime

from . import lifecycle, state_machine
from . import rubric as rubric_mod
from .events import emit

log = logging.getLogger(__name__)

#: (run_id, task kind) -> in-flight orchestration task. Keyed by kind so an
#: approve landing while the rubric task is finishing doesn't swallow the
#: lifecycle task.
_TASKS: dict[tuple[uuid.UUID, str], asyncio.Task[None]] = {}

_runtime: SandboxRuntime | None = None


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


def _track(
    kind: str, run_id: uuid.UUID, make_coro: Callable[[], Coroutine[Any, Any, None]]
) -> asyncio.Task[None]:
    key = (run_id, kind)
    existing = _TASKS.get(key)
    if existing is not None and not existing.done():
        return existing
    # coroutine built only when a task actually spawns — dedup returns early
    # so nothing is left unawaited.
    task = asyncio.create_task(make_coro())
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
        lambda: rubric_mod.draft_rubric(
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
    """Register the post-approval lifecycle task for a run."""
    return _track(
        "lifecycle",
        run_id,
        lambda: lifecycle.run_lifecycle(
            run_id,
            session_factory=session_factory or _session_factory(),
            runtime=runtime or get_runtime(),
            settings=settings or _settings(),
            evaluator_fn=evaluator_fn,
        ),
    )


def start_rescore_task(
    run_id: uuid.UUID,
    *,
    rubric_id: uuid.UUID | None = None,
    sandbox_ids: Collection[uuid.UUID] | None = None,
    model: str | None = None,
    session_factory: sessionmaker[Session] | None = None,
    settings: Any | None = None,
    runtime: SandboxRuntime | None = None,
    evaluator_fn: Any | None = None,
) -> asyncio.Task[None]:
    """Register a re-score task (PR5) — re-runs the evaluator on stored
    artifacts. Deduped by the ``rescore`` task kind: a second trigger while
    one is in flight returns the existing task."""
    return _track(
        "rescore",
        run_id,
        lambda: lifecycle.rescore_run(
            run_id,
            session_factory=session_factory or _session_factory(),
            runtime=runtime or get_runtime(),
            settings=settings or _settings(),
            evaluator_fn=evaluator_fn,
            rubric_id=rubric_id,
            sandbox_ids=sandbox_ids,
            model=model,
        ),
    )


def in_flight(run_id: uuid.UUID, kind: str) -> bool:
    """True while a registered task of ``kind`` for ``run_id`` is running."""
    task = _TASKS.get((run_id, kind))
    return task is not None and not task.done()


async def shutdown() -> None:
    """Cancel in-flight tasks on api shutdown (lifespan)."""
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
    """Mark a stale run failed + its non-terminal sandboxes failed/error.

    Exception: a run cut off mid-RE-SCORE keeps the lifecycle's own rule —
    done iff ≥1 Score row exists — so a restart doesn't demote a
    previously-done run to failed despite complete old scores. The
    ``rescore_requested``/``rescore_started`` checkpoints (emitted in the
    same transaction as the terminal → evaluating transition) mark a
    re-score pass; first-pass evals carry no marker and still fail,
    partial scores included.
    """
    with session_factory() as session:
        run = session.get(Run, run_id)
        rescued = False
        if run is not None and run.status not in state_machine.RUN_TERMINAL:
            to_status = "failed"
            if run.status == "evaluating":
                rescore_pass = (
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
                if rescore_pass:
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
                        rescued = True
            try:
                state_machine.transition(session, run, to_status)
            except state_machine.InvalidTransition:
                run.status = to_status
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
                event_type="checkpoint" if rescued else "error",
                payload={
                    "stage": "startup_sweep",
                    "message": (
                        "run was mid-rescore at api restart — done via existing scores"
                        if rescued
                        else "run was mid-flight at api restart — marked failed"
                    ),
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
