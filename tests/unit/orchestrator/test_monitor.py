"""Monitor loop: verdict mapping, deadline, budget, done-grace, finalize."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from db.models import Artifact, Event, Sandbox
from orchestrator import monitor
from orchestrator.monitor import watch_sandbox

from .conftest import make_run, make_sandbox, run_config

BUDGETS = {"wall_clock_seconds": 3600, "token_budget": 200_000, "max_iters": 10}

RESULT_LINE = lambda state: (
    "MINILOOP_RESULT " + json.dumps({"terminal_state": state, "iters": 3})
)


def _watch(session_factory, settings, fake_runtime, run_id, sandbox_id, budgets=None):
    return watch_sandbox(
        session_factory=session_factory,
        runtime=fake_runtime,
        settings=settings,
        run_id=run_id,
        sandbox_id=sandbox_id,
        budgets=budgets or BUDGETS,
    )


def _get_sandbox(session_factory, sid):
    with session_factory() as s:
        return s.get(Sandbox, sid)


def _add_event(session_factory, run_id, sandbox_id, etype, payload, ts=None):
    # ts is omitted (not None) when unset so the server_default func.now()
    # fires — the column is NOT NULL and an explicit NULL would violate it.
    kwargs = {} if ts is None else {"ts": ts}
    with session_factory() as s:
        s.add(
            Event(
                run_id=run_id,
                sandbox_id=sandbox_id,
                source="gateway" if etype in ("error", "llm_response") else "harness",
                type=etype,
                payload=payload,
                **kwargs,
            )
        )
        s.commit()


# ---------------------------------------------------------------------------
# exited-container verdicts
# ---------------------------------------------------------------------------


async def test_exited_agent_done_result(session_factory, settings, fake_runtime) -> None:
    run = make_run(session_factory, status="running", config=run_config())
    sb = make_sandbox(session_factory, run.id, status="running")
    fake_runtime.logs_map[str(sb.id)] = RESULT_LINE("agent_done") + "\n"

    reason = await _watch(session_factory, settings, fake_runtime, run.id, sb.id)

    assert reason == "agent_done"
    row = _get_sandbox(session_factory, sb.id)
    assert row.status == "done"
    assert row.terminal_reason == "agent_done"
    assert row.finished_at is not None
    assert row.artifact_path is not None
    # artifacts snapshotted + container removed
    assert fake_runtime.snapshots == [(str(sb.id), str(run.id))]
    assert str(sb.id) in fake_runtime.removed
    with session_factory() as s:
        kinds = {
            a.kind
            for a in s.execute(
                select(Artifact).where(Artifact.sandbox_id == sb.id)
            ).scalars()
        }
        assert {"workspace", "transcript", "meta"} <= kinds


async def test_exited_harness_complete_no_result(session_factory, settings, fake_runtime) -> None:
    run = make_run(session_factory, status="running", config=run_config())
    sb = make_sandbox(session_factory, run.id, status="running")
    fake_runtime.status_map[str(sb.id)] = {"status": "exited", "exit_code": 0}
    fake_runtime.logs_map[str(sb.id)] = "some noise\n"

    reason = await _watch(session_factory, settings, fake_runtime, run.id, sb.id)
    assert reason == "harness_complete"
    assert _get_sandbox(session_factory, sb.id).status == "done"


async def test_exited_exit0_with_done_signal_is_agent_done(
    session_factory, settings, fake_runtime
) -> None:
    run = make_run(session_factory, status="running", config=run_config())
    sb = make_sandbox(session_factory, run.id, status="running")
    _add_event(
        session_factory, run.id, sb.id, "done_signal",
        {"summary": "ok", "status": "success"},
    )
    fake_runtime.status_map[str(sb.id)] = {"status": "exited", "exit_code": 0}
    fake_runtime.logs_map[str(sb.id)] = "no result line"

    reason = await _watch(session_factory, settings, fake_runtime, run.id, sb.id)
    assert reason == "agent_done"


async def test_exited_nonzero_exit_is_error(session_factory, settings, fake_runtime) -> None:
    run = make_run(session_factory, status="running", config=run_config())
    sb = make_sandbox(session_factory, run.id, status="running")
    fake_runtime.status_map[str(sb.id)] = {"status": "exited", "exit_code": 2}
    fake_runtime.logs_map[str(sb.id)] = RESULT_LINE("error") + "\n"

    reason = await _watch(session_factory, settings, fake_runtime, run.id, sb.id)
    assert reason == "error"
    row = _get_sandbox(session_factory, sb.id)
    assert row.status == "failed"
    assert row.terminal_reason == "error"


async def test_exited_stuck_loop_is_done(session_factory, settings, fake_runtime) -> None:
    run = make_run(session_factory, status="running", config=run_config())
    sb = make_sandbox(session_factory, run.id, status="running")
    fake_runtime.status_map[str(sb.id)] = {"status": "exited", "exit_code": 0}
    fake_runtime.logs_map[str(sb.id)] = RESULT_LINE("stuck_loop") + "\n"

    reason = await _watch(session_factory, settings, fake_runtime, run.id, sb.id)
    assert reason == "stuck_loop"
    assert _get_sandbox(session_factory, sb.id).status == "done"


async def test_missing_container_is_error(session_factory, settings, fake_runtime) -> None:
    run = make_run(session_factory, status="running", config=run_config())
    sb = make_sandbox(session_factory, run.id, status="running")
    fake_runtime.status_map[str(sb.id)] = {"status": "missing", "exit_code": None}
    fake_runtime.snapshot_raises = True  # container gone -> snapshot fails too

    reason = await _watch(session_factory, settings, fake_runtime, run.id, sb.id)
    assert reason == "error"
    row = _get_sandbox(session_factory, sb.id)
    assert row.status == "failed"
    assert row.artifact_path is None


# ---------------------------------------------------------------------------
# policy terminations
# ---------------------------------------------------------------------------


async def test_wall_clock_deadline_timeout(session_factory, settings, fake_runtime) -> None:
    run = make_run(session_factory, status="running", config=run_config())
    past = datetime.now(UTC) - timedelta(seconds=120)
    sb = make_sandbox(session_factory, run.id, status="running", started_at=past)
    fake_runtime.status_map[str(sb.id)] = {"status": "running", "exit_code": None}

    reason = await _watch(
        session_factory, settings, fake_runtime, run.id, sb.id,
        budgets={"wall_clock_seconds": 60, "token_budget": None},
    )
    assert reason == "timeout"
    row = _get_sandbox(session_factory, sb.id)
    assert row.status == "failed"
    assert row.terminal_reason == "timeout"
    # terminate path: stop(remove=False) then finalize -> snapshot + rm
    assert str(sb.id) in fake_runtime.stopped
    assert fake_runtime.snapshots == [(str(sb.id), str(run.id))]
    assert str(sb.id) in fake_runtime.removed


async def test_budget_error_event_terminates(session_factory, settings, fake_runtime) -> None:
    run = make_run(session_factory, status="running", config=run_config())
    sb = make_sandbox(session_factory, run.id, status="running")
    _add_event(
        session_factory, run.id, sb.id, "error",
        {"stage": "budget", "reason": "budget_exceeded", "http_status": 429},
    )
    fake_runtime.status_map[str(sb.id)] = {"status": "running", "exit_code": None}

    reason = await _watch(session_factory, settings, fake_runtime, run.id, sb.id)
    assert reason == "budget_exceeded"
    row = _get_sandbox(session_factory, sb.id)
    assert row.status == "failed"
    assert row.terminal_reason == "budget_exceeded"


async def test_upstream_429_is_not_budget_exceeded(
    session_factory, settings, fake_runtime
) -> None:
    """An upstream provider 429 (stage=upstream) is NOT the gateway's budget
    gate — the sandbox exits with its own verdict (error), not
    budget_exceeded."""
    run = make_run(session_factory, status="running", config=run_config())
    sb = make_sandbox(session_factory, run.id, status="running")
    _add_event(
        session_factory, run.id, sb.id, "error",
        {
            "stage": "upstream",
            "http_status": 429,
            "message": "429 rate limit exceeded (daily quota)",
        },
    )
    # Agent dies on the failed call -> container exits -> verdict=error.
    fake_runtime.status_map[str(sb.id)] = {"status": "exited", "exit_code": 2}
    fake_runtime.logs_map[str(sb.id)] = RESULT_LINE("error") + "\n"

    reason = await _watch(session_factory, settings, fake_runtime, run.id, sb.id)
    assert reason == "error"
    row = _get_sandbox(session_factory, sb.id)
    assert row.status == "failed"
    assert row.terminal_reason == "error"


async def test_upstream_error_threshold_terminates_error(
    session_factory, settings, fake_runtime
) -> None:
    """>= UPSTREAM_ERROR_THRESHOLD upstream failures while still running ->
    terminate error (provider dead for this run) — not budget_exceeded."""
    run = make_run(session_factory, status="running", config=run_config())
    sb = make_sandbox(session_factory, run.id, status="running")
    for i in range(monitor.UPSTREAM_ERROR_THRESHOLD):
        _add_event(
            session_factory, run.id, sb.id, "error",
            {"stage": "upstream", "http_status": 500, "message": f"boom {i}"},
        )
    fake_runtime.status_map[str(sb.id)] = {"status": "running", "exit_code": None}

    reason = await _watch(session_factory, settings, fake_runtime, run.id, sb.id)
    assert reason == "error"
    row = _get_sandbox(session_factory, sb.id)
    assert row.status == "failed"
    assert row.terminal_reason == "error"
    assert str(sb.id) in fake_runtime.stopped


async def test_upstream_below_threshold_does_not_terminate(
    session_factory, settings, fake_runtime
) -> None:
    """A lone transient upstream failure lets the harness keep running —
    the container's own exit decides the verdict."""
    run = make_run(session_factory, status="running", config=run_config())
    sb = make_sandbox(session_factory, run.id, status="running")
    for _ in range(monitor.UPSTREAM_ERROR_THRESHOLD - 1):
        _add_event(
            session_factory, run.id, sb.id, "error",
            {"stage": "upstream", "http_status": 429, "message": "transient"},
        )
    states = iter(
        [
            {"status": "running", "exit_code": None},
            {"status": "exited", "exit_code": 0},
        ]
    )
    fake_runtime.status_map[str(sb.id)] = lambda: next(states)
    fake_runtime.logs_map[str(sb.id)] = RESULT_LINE("agent_done") + "\n"

    reason = await _watch(session_factory, settings, fake_runtime, run.id, sb.id)
    assert reason == "agent_done"
    assert _get_sandbox(session_factory, sb.id).status == "done"


async def test_run_token_budget_terminates(session_factory, settings, fake_runtime) -> None:
    run = make_run(session_factory, status="running", config=run_config())
    sb = make_sandbox(session_factory, run.id, status="running")
    _add_event(
        session_factory, run.id, sb.id, "llm_response",
        {"usage": {"total_tokens": 500}},
    )
    fake_runtime.status_map[str(sb.id)] = {"status": "running", "exit_code": None}

    reason = await _watch(
        session_factory, settings, fake_runtime, run.id, sb.id,
        budgets={"wall_clock_seconds": 3600, "token_budget": 500},
    )
    assert reason == "budget_exceeded"


async def test_done_signal_grace_terminates_agent_done(
    session_factory, settings, fake_runtime
) -> None:
    """Container still running but done_signal is older than the grace window."""
    run = make_run(session_factory, status="running", config=run_config())
    sb = make_sandbox(session_factory, run.id, status="running")
    old_ts = datetime.now(UTC) - timedelta(seconds=monitor.DONE_GRACE_SECONDS + 10)
    _add_event(
        session_factory, run.id, sb.id, "done_signal",
        {"summary": "done", "status": "success"}, ts=old_ts,
    )
    fake_runtime.status_map[str(sb.id)] = {"status": "running", "exit_code": None}

    reason = await _watch(session_factory, settings, fake_runtime, run.id, sb.id)
    assert reason == "agent_done"
    row = _get_sandbox(session_factory, sb.id)
    assert row.status == "done"
    assert str(sb.id) in fake_runtime.stopped


async def test_fresh_done_signal_does_not_terminate(
    session_factory, settings, fake_runtime
) -> None:
    """done_signal within the grace window -> keep monitoring; then container
    exits -> normal finalize."""
    run = make_run(session_factory, status="running", config=run_config())
    sb = make_sandbox(session_factory, run.id, status="running")
    _add_event(
        session_factory, run.id, sb.id, "done_signal",
        {"summary": "done", "status": "success"},
        ts=datetime.now(UTC),
    )
    # First poll: running (fresh done) -> continue; second poll: exited.
    states = iter(
        [
            {"status": "running", "exit_code": None},
            {"status": "exited", "exit_code": 0},
        ]
    )
    fake_runtime.status_map[str(sb.id)] = lambda: next(states)
    fake_runtime.logs_map[str(sb.id)] = RESULT_LINE("agent_done") + "\n"

    reason = await _watch(session_factory, settings, fake_runtime, run.id, sb.id)
    assert reason == "agent_done"


async def test_already_terminal_sandbox_returns_reason(
    session_factory, settings, fake_runtime
) -> None:
    run = make_run(session_factory, status="running", config=run_config())
    sb = make_sandbox(
        session_factory, run.id, status="done", terminal_reason="agent_done"
    )
    reason = await _watch(session_factory, settings, fake_runtime, run.id, sb.id)
    assert reason == "agent_done"
    assert fake_runtime.stopped == []
