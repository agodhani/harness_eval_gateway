"""Sandbox monitoring + termination (F2).

``watch_sandbox`` is the per-sandbox poll loop spawned by
``lifecycle.run_lifecycle`` (one ``asyncio`` task per running sandbox,
``MONITOR_POLL_SECONDS`` cadence). Each iteration checks, in order:

1. **Deadline** — ``started_at + wall_clock_seconds`` → terminate ``timeout``.
2. **Budget/provider** — gateway ``error`` events for this sandbox:
   ``reason=="budget_exceeded"`` (the gateway's own run/sandbox budget 429 —
   its signature is ``stage="budget"`` + ``reason``, NOT the bare status) →
   ``budget_exceeded``. ``stage=="upstream"`` events are provider-side
   failures (rate-limit 429s, 5xx, unreachable) — ≥
   ``UPSTREAM_ERROR_THRESHOLD`` of them terminate ``error`` instead. Then the
   poll-side caps (PR28): run-wide ``metering.tokens_used >= token_budget``,
   this-sandbox tokens ``>= sandbox_token_budget``, and this-sandbox
   ``estimated_cost_usd >= max_cost_usd`` — each terminates
   ``budget_exceeded`` with ``payload.limit`` = ``run_tokens`` /
   ``sandbox_tokens`` / ``cost``. The cost cap prices the sandbox's own
   participant model through the layered price table and only bites when a
   rate honestly resolves — unpriced cells can't trip it (PR21).
3. **Container state** — ``running`` → ``done_signal`` older than
   ``DONE_GRACE_SECONDS`` → terminate ``agent_done`` (safety net);
   ``exited``/``dead`` → parse ``MINILOOP_RESULT`` from logs, exit-code
   fallback; ``missing`` → ``error``.

Every path ends in :func:`finalize_sandbox`: snapshot artifacts FIRST, then
DB rows, then ``docker rm``. The function never raises — unexpected errors
finalize the sandbox as ``error`` so ``asyncio.gather`` always completes.

Empty-workspace flagging: when the snapshot workspace has no real files
(nothing, or only dot-entries like ``.git``), finalize stamps
``empty_workspace: true`` onto the ``sandbox_finalized`` checkpoint payload,
and — for the done-ish reasons (``agent_done``/``harness_complete``/
``stuck_loop``, where a clean exit producing nothing is anomalous) — emits a
``stage="empty_workspace"`` warning checkpoint. Analytics surfaces the flag
per sandbox (``empty_workspace`` on the results row).
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from api import pricing
from db.models import Artifact, Event, Run, Sandbox
from evaluator.submission import has_submission
from gateway import metering
from gateway.metering import (
    budget_state as metering_budget_state,
)
from gateway.metering import (
    llm_usage_totals,
    tokens_used,
)
from sandbox.runtime import SandboxRuntime

from . import state_machine
from .adapters import get_adapter, parse_terminal_state
from .events import emit
from .matrix import sandbox_model

log = logging.getLogger(__name__)

DONE_GRACE_SECONDS = 30.0
#: Gateway ``stage="upstream"`` error events (provider 429s/5xx/outages)
#: tolerated before the monitor gives up on the run's provider — the harness
#: may retry a transient blip, but this many failed calls means the upstream
#: is effectively dead for this run (e.g. daily-quota exhaustion).
UPSTREAM_ERROR_THRESHOLD = 3
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


def _gateway_error_verdict(
    session_factory: sessionmaker[Session],
    run_id: uuid.UUID,
    sandbox_id: uuid.UUID,
) -> tuple[str, dict[str, Any] | None] | None:
    """``(terminal_reason, detail)`` implied by this sandbox's gateway
    ``error`` events.

    Two distinct 429 sources used to be conflated here (any error event with
    ``http_status==429`` → ``budget_exceeded``), which mislabeled upstream
    provider rate-limits as budget exhaustion:

    - ``stage="budget"`` / ``reason="budget_exceeded"`` — the gateway's OWN
      run/sandbox token-budget gate → ``budget_exceeded``. The gate's error
      payload already records which line it fired at — ``limit`` ∈
      ``run_tokens``/``sandbox_tokens`` plus ``used_tokens``/``budget`` —
      so this is the PRIMARY enforcement path for the sandbox claim; the
      poll below is the backstop. ``detail`` forwards those fields so the
      terminate checkpoint carries ``payload.limit`` exactly like the
      poll-path checkpoints do.
    - ``stage="upstream"`` — the provider failed the call (rate-limit 429,
      5xx, unreachable). ≥ ``UPSTREAM_ERROR_THRESHOLD`` → ``error``; a lone
      transient blip doesn't kill a healthy sandbox.
    """
    with session_factory() as session:
        payloads = session.execute(
            select(Event.payload)
            .where(
                Event.run_id == run_id,
                Event.sandbox_id == sandbox_id,
                Event.type == "error",
            )
            .order_by(Event.id)
        ).scalars().all()
    upstream_errors = 0
    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        if (
            payload.get("reason") == "budget_exceeded"
            or payload.get("stage") == "budget"
        ):
            detail = {
                key: payload[key]
                for key in ("limit", "used_tokens", "budget")
                if payload.get(key) is not None
            }
            return "budget_exceeded", detail or None
        if payload.get("stage") == "upstream":
            upstream_errors += 1
    if upstream_errors >= UPSTREAM_ERROR_THRESHOLD:
        return "error", None
    return None


def _run_tokens_used(
    session_factory: sessionmaker[Session], run_id: uuid.UUID
) -> int:
    with session_factory() as session:
        return tokens_used(session, run_id)


def _budget_state(
    session_factory: sessionmaker[Session],
    run_id: uuid.UUID,
    sandbox_id: uuid.UUID,
) -> dict[str, Any]:
    """F30 extension store + warning dedup for one cell — read fresh each
    poll so ``POST .../extend_budget`` widens the live caps mid-watch."""
    with session_factory() as session:
        return metering_budget_state(session, run_id, sandbox_id)


def _maybe_budget_warning(
    session_factory: sessionmaker[Session],
    *,
    run_id: uuid.UUID,
    sandbox_id: uuid.UUID,
    limit: str,
    used_tokens: int | None,
    estimated_cost_usd: float | None,
    budget: float,
    warned: set[str],
) -> bool:
    """Emit ``budget_warning`` once per (sandbox, limit kind) when spend
    crosses ~80% of the effective cap — the gateway's own request-path
    warning is the same checkpoint, so either writer dedups the other.

    Returns True iff the warn CONDITION holds this poll (the caller needs
    it for auto_extend even when the event was already emitted)."""
    if used_tokens is None and estimated_cost_usd is None:
        return False
    used = used_tokens if used_tokens is not None else estimated_cost_usd
    assert used is not None  # for the type checker; guarded above
    if float(used) < metering.BUDGET_WARN_FRACTION * budget:
        return False
    if limit in warned:
        return True  # condition holds; the row was already written
    payload: dict[str, Any] = {
        "stage": "budget_warning",
        "limit": limit,
        "budget": budget,
        "pct": round(float(used) / budget, 3),
    }
    if used_tokens is not None:
        payload["used_tokens"] = used_tokens
        payload["remaining_tokens"] = int(budget) - used_tokens
    else:
        payload["estimated_cost_usd"] = round(float(estimated_cost_usd or 0), 6)
        payload["remaining_cost_usd"] = round(
            budget - float(estimated_cost_usd or 0), 6
        )
    with session_factory() as session:
        emit(
            session,
            run_id=run_id,
            sandbox_id=sandbox_id,
            event_type="checkpoint",
            payload=payload,
        )
        session.commit()
    warned.add(limit)
    return True


def _maybe_auto_extend(
    session_factory: sessionmaker[Session],
    *,
    run_id: uuid.UUID,
    sandbox_id: uuid.UUID,
    budgets: dict[str, Any],
    warn_conditions: dict[str, bool],
    extensions: int,
) -> None:
    """Stretch goal: ``budgets.auto_extend`` bumps the cred once by +50% of
    the first limit whose warn condition holds — an append-only
    ``budget_extended`` checkpoint, so the same store a manual
    extend_budget writes. ``extensions`` is the count of prior grant rows;
    any extension (manual or auto) satisfies the once-only rule."""
    if not budgets.get("auto_extend") or extensions:
        return
    payload: dict[str, Any] = {"stage": "budget_extended", "via": "auto"}
    if warn_conditions.get("run_tokens") and budgets.get("token_budget"):
        payload["added_tokens"] = max(
            1, int(0.5 * int(budgets["token_budget"]))
        )
    elif warn_conditions.get("sandbox_tokens") and budgets.get(
        "sandbox_token_budget"
    ):
        payload["added_tokens"] = max(
            1, int(0.5 * int(budgets["sandbox_token_budget"]))
        )
    if warn_conditions.get("cost") and budgets.get("max_cost_usd"):
        payload["added_cost_usd"] = 0.5 * float(budgets["max_cost_usd"])
    if "added_tokens" not in payload and "added_cost_usd" not in payload:
        return
    payload["reason"] = "auto_extend on budget_warning (+50%, once)"
    with session_factory() as session:
        emit(
            session,
            run_id=run_id,
            sandbox_id=sandbox_id,
            event_type="checkpoint",
            payload=payload,
        )
        session.commit()
    log.info("auto_extend granted for sandbox %s: %s", sandbox_id, payload)


def _sandbox_tokens_used(
    session_factory: sessionmaker[Session],
    run_id: uuid.UUID,
    sandbox_id: uuid.UUID,
) -> int:
    with session_factory() as session:
        return tokens_used(session, run_id, sandbox_id=sandbox_id)


def _sandbox_estimated_cost_usd(
    session_factory: sessionmaker[Session],
    run: Run,
    sb: Sandbox,
) -> float | None:
    """Estimated USD spend for the sandbox's participant traffic (PR28).

    Priced at the sandbox's own model through the layered price table —
    returns ``None`` when the model resolves no rate (unpriced, PR21): a
    cost cap then simply can't trip the cell instead of guessing a rate.
    Poll-cadence traffic only, so judge-phase usage is irrelevant (the watch
    exits before the evaluator runs).
    """
    model = sandbox_model(sb.harness_config or {}, run.model_name)
    with session_factory() as session:
        book = pricing.load_book(session)
        usage = llm_usage_totals(session, run.id, sandbox_id=sb.id)
    price = book.price_for(book.key_for(model))
    if price is None:
        return None
    return pricing.estimate_cost_usd(
        price,
        input_tokens=usage["prompt_tokens"],
        output_tokens=usage["completion_tokens"],
        cache_read_tokens=usage["cache_read_tokens"],
    )


def _mark_terminating(
    session_factory: sessionmaker[Session],
    *,
    run_id: uuid.UUID,
    sandbox_id: uuid.UUID,
    reason: str,
    detail: dict[str, Any] | None = None,
) -> None:
    with session_factory() as session:
        sb = session.get(Sandbox, sandbox_id)
        if sb is not None and sb.status == "running":
            try:
                state_machine.transition(session, sb, "terminating")
            except state_machine.InvalidTransition:
                sb.status = "terminating"
            payload: dict[str, Any] = {"stage": "terminate", "reason": reason}
            if detail:
                # Budget context (PR28): which limit fired + the numbers
                # that tripped it — ``limit`` ∈ run_tokens|sandbox_tokens|cost.
                payload.update(detail)
            emit(
                session,
                run_id=run_id,
                sandbox_id=sandbox_id,
                event_type="checkpoint",
                payload=payload,
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
    # os.walk doesn't descend into symlinked dirs (followlinks=False) — the
    # snapshot tree is candidate-authored, so rglob could escape the artifact
    # dir through a planted symlink or spin on a cycle.
    if path.is_symlink():
        return 0
    if path.is_file():
        try:
            return path.stat().st_size
        except OSError:
            return 0
    total = 0
    if path.is_dir():
        for root, _dirs, files in os.walk(path):
            for name in files:
                f = Path(root) / name
                if f.is_symlink() or not f.is_file():
                    continue
                try:
                    total += f.stat().st_size
                except OSError:
                    continue
    return total


def _snapshot_sizes(snapshot: Any) -> dict[str, int]:
    """kind -> size for each artifact candidate. Runs on a worker thread —
    rglob'ing a large workspace must not block the event loop."""
    return {
        kind: _dir_size(Path(path))
        for kind, path in (
            ("workspace", snapshot.workspace_dir),
            ("transcript", snapshot.transcript_path),
            ("meta", snapshot.meta_path),
        )
    }


def _workspace_is_empty(path: Path, seed_dir: Path | None = None) -> bool:
    return not has_submission(path, seed_dir)


def _finalize_db(
    session_factory: sessionmaker[Session],
    *,
    run_id: uuid.UUID,
    sandbox_id: uuid.UUID,
    snapshot: Any,
    sizes: dict[str, int],
    reason: str,
    empty_workspace: bool = False,
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
                        size_bytes=sizes.get(kind, 0),
                    )
                )
        to_status = "done" if reason in state_machine.DONE_REASONS else "failed"
        try:
            state_machine.transition(session, sb, to_status)
        except state_machine.InvalidTransition:
            sb.status = to_status
        sb.terminal_reason = reason
        sb.finished_at = _now()
        payload: dict[str, Any] = {
            "stage": "sandbox_finalized",
            "terminal_reason": reason,
            "sandbox_status": sb.status,
            "artifact_path": sb.artifact_path,
        }
        if empty_workspace:
            # Distinguishes "finished cleanly" from "finished cleanly but
            # produced nothing" — the terminal_reason itself is unchanged.
            payload["empty_workspace"] = True
        emit(
            session,
            run_id=run_id,
            sandbox_id=sandbox_id,
            event_type="checkpoint",
            payload=payload,
        )
        if empty_workspace and reason in state_machine.DONE_REASONS:
            # A done-ish exit on an empty workspace is anomalous (e.g. a
            # CLI exiting 0 without ever touching files) — warn so the
            # timeline shows it isn't a normal completion.
            emit(
                session,
                run_id=run_id,
                sandbox_id=sandbox_id,
                event_type="checkpoint",
                payload={
                    "stage": "empty_workspace",
                    "severity": "warning",
                    "terminal_reason": reason,
                    "detail": (
                        f"{reason} but the snapshotted workspace has no "
                        "non-empty submission files added or changed"
                    ),
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

    sizes: dict[str, int] = {}
    empty_workspace = False
    if snapshot is not None:
        try:
            sizes = await asyncio.to_thread(_snapshot_sizes, snapshot)
        except Exception:
            log.warning("finalize: artifact sizing failed for %s", sid, exc_info=True)
        try:
            sb = _load_sandbox(session_factory, sandbox_id)
            seed_dir = None
            if sb is not None and (sb.harness_config or {}).get("seed_sha"):
                seed_dir = Path(runtime.state_dir).parent / "seeds" / str(run_id) / "content"
            empty_workspace = await asyncio.to_thread(
                _workspace_is_empty, Path(snapshot.workspace_dir), seed_dir
            )
        except Exception:
            log.warning(
                "finalize: workspace emptiness probe failed for %s",
                sid,
                exc_info=True,
            )

    # 2) DB rows (artifacts + terminal fields + checkpoint event).
    try:
        _finalize_db(
            session_factory,
            run_id=run_id,
            sandbox_id=sandbox_id,
            snapshot=snapshot,
            sizes=sizes,
            reason=reason,
            empty_workspace=empty_workspace,
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
    detail: dict[str, Any] | None = None,
) -> str:
    """Policy-driven stop (timeout/budget/done-grace): mark terminating, stop
    the container (keep it for snapshot), then finalize."""
    _mark_terminating(
        session_factory,
        run_id=run_id,
        sandbox_id=sandbox_id,
        reason=reason,
        detail=detail,
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
    # PR28 per-cell caps — absent ⇒ no per-sandbox checks (pre-PR28 runs).
    sandbox_token_budget = budgets.get("sandbox_token_budget")
    max_cost_usd = budgets.get("max_cost_usd")
    # The run row carries model_name for the per-sandbox cost estimate;
    # loaded once — model_name is immutable after submit.
    run = None
    if max_cost_usd:
        with session_factory() as session:
            run = session.get(Run, run_id)
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

            # 2) gateway error events (budget gate vs upstream failures),
            #    then run-wide token total
            verdict = _gateway_error_verdict(session_factory, run_id, sandbox_id)
            if verdict is not None:
                reason, detail = verdict
                return await _terminate(
                    session_factory=session_factory,
                    runtime=runtime,
                    run_id=run_id,
                    sandbox_id=sandbox_id,
                    reason=reason,
                    detail=detail,
                )
            # F30: extension store + warning dedup, fresh each poll — a
            # control-plane extend_budget (or this poll's auto_extend grant)
            # widens the effective caps below without restarting the cell.
            bstate = _budget_state(session_factory, run_id, sandbox_id)
            ext_tokens = int(bstate["added_tokens"])
            ext_cost = float(bstate["added_cost_usd"])
            warned: set[str] = bstate["warned"]
            warn_conditions: dict[str, bool] = {}

            if token_budget:
                used = _run_tokens_used(session_factory, run_id)
                run_limit = int(token_budget) + ext_tokens
                if used >= run_limit:
                    return await _terminate(
                        session_factory=session_factory,
                        runtime=runtime,
                        run_id=run_id,
                        sandbox_id=sandbox_id,
                        reason="budget_exceeded",
                        detail={
                            "limit": "run_tokens",
                            "used_tokens": used,
                            "budget": run_limit,
                        },
                    )
                warn_conditions["run_tokens"] = _maybe_budget_warning(
                    session_factory,
                    run_id=run_id,
                    sandbox_id=sandbox_id,
                    limit="run_tokens",
                    used_tokens=used,
                    estimated_cost_usd=None,
                    budget=float(run_limit),
                    warned=warned,
                )
            # PR28 per-sandbox token cap — the gateway claim enforces it
            # per request; this poll is the backstop that terminates the
            # cell when only its OWN total crosses the line.
            if sandbox_token_budget:
                sb_used = _sandbox_tokens_used(session_factory, run_id, sandbox_id)
                sb_limit = int(sandbox_token_budget) + ext_tokens
                if sb_used >= sb_limit:
                    return await _terminate(
                        session_factory=session_factory,
                        runtime=runtime,
                        run_id=run_id,
                        sandbox_id=sandbox_id,
                        reason="budget_exceeded",
                        detail={
                            "limit": "sandbox_tokens",
                            "used_tokens": sb_used,
                            "budget": sb_limit,
                        },
                    )
                warn_conditions["sandbox_tokens"] = _maybe_budget_warning(
                    session_factory,
                    run_id=run_id,
                    sandbox_id=sandbox_id,
                    limit="sandbox_tokens",
                    used_tokens=sb_used,
                    estimated_cost_usd=None,
                    budget=float(sb_limit),
                    warned=warned,
                )
            # PR28 per-sandbox cost cap — layered-price estimate each poll;
            # unpriced cells get None and can't trip the cap (PR21 honesty).
            if max_cost_usd and run is not None:
                cost_limit = float(max_cost_usd) + ext_cost
                est = _sandbox_estimated_cost_usd(session_factory, run, sb)
                if est is not None and est >= cost_limit:
                    return await _terminate(
                        session_factory=session_factory,
                        runtime=runtime,
                        run_id=run_id,
                        sandbox_id=sandbox_id,
                        reason="budget_exceeded",
                        detail={
                            "limit": "cost",
                            "estimated_cost_usd": round(est, 6),
                            "budget_usd": cost_limit,
                        },
                    )
                warn_conditions["cost"] = _maybe_budget_warning(
                    session_factory,
                    run_id=run_id,
                    sandbox_id=sandbox_id,
                    limit="cost",
                    used_tokens=None,
                    estimated_cost_usd=est,
                    budget=cost_limit,
                    warned=warned,
                )
            _maybe_auto_extend(
                session_factory,
                run_id=run_id,
                sandbox_id=sandbox_id,
                budgets=budgets,
                warn_conditions=warn_conditions,
                extensions=int(bstate["extensions"]),
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
