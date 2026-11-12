"""Sandbox monitoring + termination (F2).

``watch_sandbox`` is the per-sandbox poll loop spawned by
``lifecycle.run_lifecycle`` (one ``asyncio`` task per running sandbox,
``MONITOR_POLL_SECONDS`` cadence). Each iteration checks, in order:

1. **Deadline** — ``started_at + wall_clock_seconds`` → terminate ``timeout``.
2. **Budget** — gateway ``error`` events (``reason=="budget_exceeded"`` or
   ``http_status==429``) for this sandbox, or run-wide
   ``metering.tokens_used >= token_budget`` → terminate ``budget_exceeded``.
3. **Container state** — ``running`` → ``done_signal`` older than
   ``DONE_GRACE_SECONDS`` → terminate ``agent_done`` (safety net);
   ``exited``/``dead`` → parse ``MINILOOP_RESULT`` from logs, exit-code
   fallback; ``missing`` → ``error``.

Every path ends in :func:`finalize_sandbox`: snapshot artifacts FIRST, then
DB rows, then ``docker rm``. The function never raises — unexpected errors
finalize the sandbox as ``error`` so ``asyncio.gather`` always completes.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from db.models import Artifact, Event, Sandbox
from gateway.metering import tokens_used
from sandbox.runtime import SandboxRuntime

from . import state_machine
from .adapters import get_adapter, parse_terminal_state
from .events import emit

log = logging.getLogger(__name__)

DONE_GRACE_SECONDS = 30.0
_AGENT_TERMINAL_STATES = frozenset(
    {"agent_done", "harness_complete", "stuck_loop", "error"}
)


def _aware(dt: datetime | None) -> datetime | None:
    """SQLite returns naive datetimes; treat them as UTC."""
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _now() -> datetime:
    return datetime.now(UTC)


# ---------------------------------------------------------------------------
# DB helpers (sync — called directly on the event-loop thread; these are
# tiny indexed queries, and keeping them off worker threads means the shared
# sqlite :memory:/StaticPool connection is never touched concurrently — see
# db/session.py. Only blocking docker calls go through asyncio.to_thread.)
# ---------------------------------------------------------------------------


def _load_sandbox(
    session_factory: sessionmaker[Session], sandbox_id: uuid.UUID
) -> Sandbox | None:
    with session_factory() as session:
        return session.get(Sandbox, sandbox_id)


def _sandbox_done_signal_ts(
    session_factory: sessionmaker[Session], sandbox_id: uuid.UUID
) -> datetime | None:
    """Latest done_signal event ts for a sandbox, if any."""
    with session_factory() as session:
        ts = session.execute(
            select(func.max(Event.ts)).where(
                Event.sandbox_id == sandbox_id, Event.type == "done_signal"
            )
        ).scalar_one_or_none()
        return ts


def _budget_event_seen(
    session_factory: sessionmaker[Session],
    run_id: uuid.UUID,
    sandbox_id: uuid.UUID,
) -> bool:
    """True when a gateway ``error`` event marks this sandbox budget-limited."""
    with session_factory() as session:
        payloads = session.execute(
            select(Event.payload).where(
                Event.run_id == run_id,
                Event.sandbox_id == sandbox_id,
                Event.type == "error",
            )
        ).scalars().all()
    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        if payload.get("reason") == "budget_exceeded" or payload.get("http_status") == 429:
            return True
    return False


def _run_tokens_used(
    session_factory: sessionmaker[Session], run_id: uuid.UUID
) -> int:
    with session_factory() as session:
        return tokens_used(session, run_id)


def _mark_terminating(
    session_factory: sessionmaker[Session],
    *,
    run_id: uuid.UUID,
    sandbox_id: uuid.UUID,
    reason: str,
) -> None:
    with session_factory() as session:
        sb = session.get(Sandbox, sandbox_id)
        if sb is not None and sb.status == "running":
            try:
                state_machine.transition(session, sb, "terminating")
            except state_machine.InvalidTransition:
                sb.status = "terminating"
            emit(
                session,
                run_id=run_id,
                sandbox_id=sandbox_id,
                event_type="checkpoint",
                payload={"stage": "terminate", "reason": reason},
            )
            session.commit()


# ---------------------------------------------------------------------------
# verdict + finalize
# ---------------------------------------------------------------------------


def _verdict(
    *,
    adapter: Any,
    logs: str,
    exit_code: int | None,
    done_seen: bool,
) -> str:
    """Map container outcome → terminal_reason (plan §4 table)."""
    agent_state = parse_terminal_state(adapter, logs)
    if agent_state in _AGENT_TERMINAL_STATES:
        return agent_state
    if exit_code == 0:
        return "agent_done" if done_seen else "harness_complete"
    return "error"


def _dir_size(path: Path) -> int:
    if path.is_file():
        try:
            return path.stat().st_size
        except OSError:
            return 0
    total = 0
    if path.is_dir():
        for f in path.rglob("*"):
            if f.is_file():
                try:
                    total += f.stat().st_size
                except OSError:
                    continue
    return total


def _finalize_db(
    session_factory: sessionmaker[Session],
    *,
    run_id: uuid.UUID,
    sandbox_id: uuid.UUID,
    snapshot: Any,
    reason: str,
) -> None:
    """DB half of finalize: artifact rows + sandbox terminal fields + event."""
    with session_factory() as session:
        sb = session.get(Sandbox, sandbox_id)
        if sb is None:
            return
        if snapshot is not None:
            sb.artifact_path = str(snapshot.artifact_dir)
            candidates = [
                ("workspace", snapshot.workspace_dir),
                ("transcript", snapshot.transcript_path),
                ("meta", snapshot.meta_path),
            ]
            for kind, path in candidates:
                p = Path(path)
                if not p.exists():
                    continue
                session.add(
                    Artifact(
                        sandbox_id=sandbox_id,
                        kind=kind,
                        path=str(p),
                        size_bytes=_dir_size(p),
                    )
                )
        sb.status = "done" if reason in state_machine.DONE_REASONS else "failed"
        sb.terminal_reason = reason
        sb.finished_at = _now()
        emit(
            session,
            run_id=run_id,
            sandbox_id=sandbox_id,
            event_type="checkpoint",
            payload={
                "stage": "sandbox_finalized",
                "terminal_reason": reason,
                "sandbox_status": sb.status,
                "artifact_path": sb.artifact_path,
            },
        )
        session.commit()


async def finalize_sandbox(
    *,
    session_factory: sessionmaker[Session],
    runtime: SandboxRuntime,
    run_id: uuid.UUID,
    sandbox_id: uuid.UUID,
    reason: str,
) -> str:
    """Snapshot → artifact/DB rows → ``docker rm``. Always returns ``reason``."""
    sid = str(sandbox_id)

    # 1) Snapshot FIRST — docker cp/logs need the container to exist.
    snapshot = None
    try:
        snapshot = await asyncio.to_thread(runtime.snapshot, sid, str(run_id))
    except Exception as exc:  # noqa: BLE001 — e.g. container already gone
        log.warning("finalize: snapshot failed for %s: %s", sid, exc)

    # 2) DB rows (artifacts + terminal fields + checkpoint event).
    try:
        _finalize_db(
            session_factory,
            run_id=run_id,
            sandbox_id=sandbox_id,
            snapshot=snapshot,
            reason=reason,
        )
    except Exception:
        log.exception("finalize: DB update failed for sandbox %s", sid)

    # 3) Remove the container — keep nothing running after terminal state.
    try:
        await asyncio.to_thread(runtime.remove, sid)
    except Exception:
        log.exception("finalize: docker rm failed for %s", sid)

    return reason


# ---------------------------------------------------------------------------
# watch loop
# ---------------------------------------------------------------------------


async def _terminate(
    *,
    session_factory: sessionmaker[Session],
    runtime: SandboxRuntime,
    run_id: uuid.UUID,
    sandbox_id: uuid.UUID,
    reason: str,
) -> str:
    """Policy-driven stop (timeout/budget/done-grace): mark terminating, stop
    the container (keep it for snapshot), then finalize."""
    _mark_terminating(
        session_factory,
        run_id=run_id,
        sandbox_id=sandbox_id,
        reason=reason,
    )
    try:
        await asyncio.to_thread(runtime.stop, str(sandbox_id), remove=False)
    except Exception:
        log.exception("terminate: docker stop failed for %s", sandbox_id)
    return await finalize_sandbox(
        session_factory=session_factory,
        runtime=runtime,
        run_id=run_id,
        sandbox_id=sandbox_id,
        reason=reason,
    )


async def watch_sandbox(
    *,
    session_factory: sessionmaker[Session],
    runtime: SandboxRuntime,
    settings: Any,
    run_id: uuid.UUID,
    sandbox_id: uuid.UUID,
    budgets: dict[str, Any],
) -> str | None:
    """Monitor one sandbox until terminal; returns its terminal_reason.

    Never raises — the top-level guard finalizes as ``error`` so
    ``asyncio.gather`` in the lifecycle always completes.
    """
    sid = str(sandbox_id)
    wall_clock = float(budgets.get("wall_clock_seconds", 3600))
    token_budget = budgets.get("token_budget")
    poll = float(getattr(settings, "MONITOR_POLL_SECONDS", 5.0))
    try:
        while True:
            await asyncio.sleep(poll)

            sb = _load_sandbox(session_factory, sandbox_id)
            if sb is None:
                log.warning("watch: sandbox %s row gone", sid)
                return None
            if sb.status in state_machine.SANDBOX_TERMINAL:
                return sb.terminal_reason

            now = _now()
            started = _aware(sb.started_at) or now

            # 1) wall-clock deadline
            if (now - started).total_seconds() > wall_clock:
                return await _terminate(
                    session_factory=session_factory,
                    runtime=runtime,
                    run_id=run_id,
                    sandbox_id=sandbox_id,
                    reason="timeout",
                )

            # 2) budget: gateway error events, then run-wide token total
            if _budget_event_seen(session_factory, run_id, sandbox_id):
                return await _terminate(
                    session_factory=session_factory,
                    runtime=runtime,
                    run_id=run_id,
                    sandbox_id=sandbox_id,
                    reason="budget_exceeded",
                )
            if token_budget:
                used = _run_tokens_used(session_factory, run_id)
                if used >= int(token_budget):
                    return await _terminate(
                        session_factory=session_factory,
                        runtime=runtime,
                        run_id=run_id,
                        sandbox_id=sandbox_id,
                        reason="budget_exceeded",
                    )

            # 3) container state
            status = await asyncio.to_thread(runtime.status, sid)
            state = status.get("status")

            if state == "running":
                # done_signal safety net: agent called `done` but the process
                # hasn't exited — give it DONE_GRACE_SECONDS, then stop it.
                done_ts = _sandbox_done_signal_ts(session_factory, sandbox_id)
                done_aware = _aware(done_ts)
                if (
                    done_aware is not None
                    and (now - done_aware).total_seconds() > DONE_GRACE_SECONDS
                ):
                    return await _terminate(
                        session_factory=session_factory,
                        runtime=runtime,
                        run_id=run_id,
                        sandbox_id=sandbox_id,
                        reason="agent_done",
                    )
                continue

            if state in ("exited", "dead"):
                logs = await asyncio.to_thread(runtime.logs, sid, tail=500)
                done_ts = _sandbox_done_signal_ts(session_factory, sandbox_id)
                hc = sb.harness_config or {}
                adapter = get_adapter(str(hc.get("adapter") or "miniloop"))
                reason = _verdict(
                    adapter=adapter,
                    logs=logs,
                    exit_code=status.get("exit_code"),
                    done_seen=done_ts is not None,
                )
                return await finalize_sandbox(
                    session_factory=session_factory,
                    runtime=runtime,
                    run_id=run_id,
                    sandbox_id=sandbox_id,
                    reason=reason,
                )

            if state == "missing":
                return await finalize_sandbox(
                    session_factory=session_factory,
                    runtime=runtime,
                    run_id=run_id,
                    sandbox_id=sandbox_id,
                    reason="error",
                )
            # created/restarting/paused/etc — keep polling.
    except Exception:
        log.exception("watch_sandbox: unexpected error for %s", sid)
        try:
            return await finalize_sandbox(
                session_factory=session_factory,
                runtime=runtime,
                run_id=run_id,
                sandbox_id=sandbox_id,
                reason="error",
            )
        except Exception:
            log.exception("watch_sandbox: finalize also failed for %s", sid)
            return "error"
