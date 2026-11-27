"""Monitor loop: verdict mapping, deadline, budget, done-grace, finalize."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
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
# empty-workspace flagging
# ---------------------------------------------------------------------------


def _checkpoint_payloads(session_factory, sandbox_id) -> list[dict]:
    with session_factory() as s:
        return [
            e.payload
            for e in s.execute(
                select(Event)
                .where(
                    Event.sandbox_id == sandbox_id,
                    Event.type == "checkpoint",
                )
                .order_by(Event.id)
            ).scalars()
        ]


async def test_harness_complete_empty_workspace_flagged(
    session_factory, settings, fake_runtime
) -> None:
    """A clean exit on a dot-only workspace keeps `harness_complete` but is
    flagged: `empty_workspace` on the finalized payload + a warning
    checkpoint — it's not a normal completion."""
    run = make_run(session_factory, status="running", config=run_config())
    sb = make_sandbox(session_factory, run.id, status="running")
    fake_runtime.status_map[str(sb.id)] = {"status": "exited", "exit_code": 0}
    fake_runtime.logs_map[str(sb.id)] = "cli exited 0\n"
    fake_runtime.empty_workspace_ids.add(str(sb.id))

    reason = await _watch(session_factory, settings, fake_runtime, run.id, sb.id)
    assert reason == "harness_complete"
    assert _get_sandbox(session_factory, sb.id).status == "done"

    payloads = _checkpoint_payloads(session_factory, sb.id)
    finalized = next(p for p in payloads if p.get("stage") == "sandbox_finalized")
    assert finalized["terminal_reason"] == "harness_complete"
    assert finalized["empty_workspace"] is True
    warning = next(p for p in payloads if p.get("stage") == "empty_workspace")
    assert warning["severity"] == "warning"
    assert warning["terminal_reason"] == "harness_complete"


async def test_nonempty_workspace_not_flagged(
    session_factory, settings, fake_runtime
) -> None:
    """Normal completion with files: no flag, no warning."""
    run = make_run(session_factory, status="running", config=run_config())
    sb = make_sandbox(session_factory, run.id, status="running")
    fake_runtime.logs_map[str(sb.id)] = RESULT_LINE("agent_done") + "\n"

    reason = await _watch(session_factory, settings, fake_runtime, run.id, sb.id)
    assert reason == "agent_done"

    payloads = _checkpoint_payloads(session_factory, sb.id)
    finalized = next(p for p in payloads if p.get("stage") == "sandbox_finalized")
    assert "empty_workspace" not in finalized
    assert not any(p.get("stage") == "empty_workspace" for p in payloads)


def test_workflow_only_workspace_is_not_empty(tmp_path):
    workflow = tmp_path / ".github" / "workflows" / "ci.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text("name: CI\n")
    assert not monitor._workspace_is_empty(tmp_path)


@pytest.mark.parametrize("seed_contents", ["hi\n", "different\n"])
async def test_seeded_workspace_warning_requires_no_changes(
    session_factory, settings, fake_runtime, seed_contents: str
) -> None:
    run = make_run(session_factory, status="running", config=run_config())
    sb = make_sandbox(
        session_factory, run.id, harness_config={"adapter": "miniloop", "seed_sha": "seed"}
    )
    seed = fake_runtime.state_dir.parent / "seeds" / str(run.id) / "content"
    seed.mkdir(parents=True)
    (seed / "hello.txt").write_text(seed_contents)
    fake_runtime.logs_map[str(sb.id)] = RESULT_LINE("agent_done") + "\n"

    assert await _watch(session_factory, settings, fake_runtime, run.id, sb.id) == "agent_done"

    payloads = _checkpoint_payloads(session_factory, sb.id)
    assert any(p.get("stage") == "empty_workspace" for p in payloads) == (seed_contents == "hi\n")


async def test_empty_workspace_error_reason_no_warning(
    session_factory, settings, fake_runtime
) -> None:
    """The flag stamps for any reason (an errored lane producing nothing is
    still informative), but the warning checkpoint is reserved for done-ish
    exits — `error` already says it failed."""
    run = make_run(session_factory, status="running", config=run_config())
    sb = make_sandbox(session_factory, run.id, status="running")
    fake_runtime.status_map[str(sb.id)] = {"status": "exited", "exit_code": 2}
    fake_runtime.logs_map[str(sb.id)] = RESULT_LINE("error") + "\n"
    fake_runtime.empty_workspace_ids.add(str(sb.id))

    reason = await _watch(session_factory, settings, fake_runtime, run.id, sb.id)
    assert reason == "error"

    payloads = _checkpoint_payloads(session_factory, sb.id)
    finalized = next(p for p in payloads if p.get("stage") == "sandbox_finalized")
    assert finalized["empty_workspace"] is True
    assert not any(p.get("stage") == "empty_workspace" for p in payloads)


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
    """The gateway's own budget-gate event — the PRIMARY sandbox_tokens
    enforcement path — terminates the cell AND forwards which limit fired
    into the terminate checkpoint (same contract as the poll path)."""
    run = make_run(session_factory, status="running", config=run_config())
    sb = make_sandbox(session_factory, run.id, status="running")
    _add_event(
        session_factory, run.id, sb.id, "error",
        {
            "stage": "budget",
            "reason": "budget_exceeded",
            "http_status": 429,
            "limit": "sandbox_tokens",
            "used_tokens": 1234,
            "budget": 1000,
        },
    )
    fake_runtime.status_map[str(sb.id)] = {"status": "running", "exit_code": None}

    reason = await _watch(session_factory, settings, fake_runtime, run.id, sb.id)
    assert reason == "budget_exceeded"
    row = _get_sandbox(session_factory, sb.id)
    assert row.status == "failed"
    assert row.terminal_reason == "budget_exceeded"
    term = next(
        p for p in _checkpoint_payloads(session_factory, sb.id)
        if p.get("stage") == "terminate"
    )
    assert term["reason"] == "budget_exceeded"
    assert term["limit"] == "sandbox_tokens"
    assert term["used_tokens"] == 1234
    assert term["budget"] == 1000


async def test_budget_error_event_without_limit_still_terminates(
    session_factory, settings, fake_runtime
) -> None:
    """A pre-PR28 style gate event (no limit/usage fields) still terminates
    budget_exceeded — the checkpoint just omits the optional detail keys."""
    run = make_run(session_factory, status="running", config=run_config())
    sb = make_sandbox(session_factory, run.id, status="running")
    _add_event(
        session_factory, run.id, sb.id, "error",
        {"stage": "budget", "reason": "budget_exceeded", "http_status": 429},
    )
    fake_runtime.status_map[str(sb.id)] = {"status": "running", "exit_code": None}

    reason = await _watch(session_factory, settings, fake_runtime, run.id, sb.id)
    assert reason == "budget_exceeded"
    term = next(
        p for p in _checkpoint_payloads(session_factory, sb.id)
        if p.get("stage") == "terminate"
    )
    assert term["reason"] == "budget_exceeded"
    assert "limit" not in term


async def test_budget_error_verdict_reports_earliest_gate_trip(
    session_factory, settings, fake_runtime
) -> None:
    """When a sandbox accumulates budget events under two limits (the cell
    trips sandbox_tokens, keeps retrying, then the shared run ceiling
    fires), the terminate checkpoint deterministically reports the
    EARLIEST trip — the cap that first throttled the cell."""
    run = make_run(session_factory, status="running", config=run_config())
    sb = make_sandbox(session_factory, run.id, status="running")
    _add_event(
        session_factory, run.id, sb.id, "error",
        {
            "stage": "budget",
            "reason": "budget_exceeded",
            "http_status": 429,
            "limit": "sandbox_tokens",
            "used_tokens": 1234,
            "budget": 1000,
        },
    )
    _add_event(
        session_factory, run.id, sb.id, "error",
        {
            "stage": "budget",
            "reason": "budget_exceeded",
            "http_status": 429,
            "limit": "run_tokens",
            "used_tokens": 999999,
            "budget": 500000,
        },
    )
    fake_runtime.status_map[str(sb.id)] = {"status": "running", "exit_code": None}

    reason = await _watch(session_factory, settings, fake_runtime, run.id, sb.id)
    assert reason == "budget_exceeded"
    term = next(
        p for p in _checkpoint_payloads(session_factory, sb.id)
        if p.get("stage") == "terminate"
    )
    assert term["limit"] == "sandbox_tokens"
    assert term["used_tokens"] == 1234
    assert term["budget"] == 1000


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


# ---------------------------------------------------------------------------
# PR28 — per-sandbox token + cost caps
# ---------------------------------------------------------------------------


async def test_sandbox_token_budget_terminates(
    session_factory, settings, fake_runtime
) -> None:
    """The cell's OWN total crossing sandbox_token_budget terminates it —
    siblings' tokens don't count toward the cap."""
    run = make_run(session_factory, status="running", config=run_config())
    sb = make_sandbox(session_factory, run.id, status="running")
    other = make_sandbox(session_factory, run.id, status="running")
    # sibling burns a pile — must NOT count toward sb's cap
    _add_event(
        session_factory, run.id, other.id, "llm_response",
        {"usage": {"total_tokens": 9000}},
    )
    _add_event(
        session_factory, run.id, sb.id, "llm_response",
        {"usage": {"total_tokens": 500}},
    )
    fake_runtime.status_map[str(sb.id)] = {"status": "running", "exit_code": None}

    reason = await _watch(
        session_factory, settings, fake_runtime, run.id, sb.id,
        budgets={"wall_clock_seconds": 3600, "sandbox_token_budget": 500},
    )
    assert reason == "budget_exceeded"
    assert _get_sandbox(session_factory, sb.id).terminal_reason == "budget_exceeded"
    term = next(
        p for p in _checkpoint_payloads(session_factory, sb.id)
        if p.get("stage") == "terminate"
    )
    assert term["reason"] == "budget_exceeded"
    assert term["limit"] == "sandbox_tokens"
    assert term["used_tokens"] == 500
    assert term["budget"] == 500


async def test_sandbox_token_budget_under_cap_keeps_running(
    session_factory, settings, fake_runtime
) -> None:
    """Under the cap the poll does nothing — the container's own exit decides."""
    run = make_run(session_factory, status="running", config=run_config())
    sb = make_sandbox(session_factory, run.id, status="running")
    _add_event(
        session_factory, run.id, sb.id, "llm_response",
        {"usage": {"total_tokens": 400}},
    )
    states = iter(
        [{"status": "running", "exit_code": None}, {"status": "exited", "exit_code": 0}]
    )
    fake_runtime.status_map[str(sb.id)] = lambda: next(states)
    fake_runtime.logs_map[str(sb.id)] = RESULT_LINE("agent_done") + "\n"

    reason = await _watch(
        session_factory, settings, fake_runtime, run.id, sb.id,
        budgets={"wall_clock_seconds": 3600, "sandbox_token_budget": 500},
    )
    assert reason == "agent_done"


async def test_cost_budget_terminates_priced_model(
    session_factory, settings, fake_runtime
) -> None:
    """max_cost_usd prices the sandbox's own traffic each poll; gpt-4o at
    $2.5/$10 per Mtok → 1M in + 100k out = $3.50 trips a $3.00 cap."""
    run = make_run(
        session_factory, status="running", config=run_config(), model_name="gpt-4o"
    )
    sb = make_sandbox(session_factory, run.id, status="running")
    _add_event(
        session_factory, run.id, sb.id, "llm_response",
        {
            "usage": {
                "prompt_tokens": 1_000_000,
                "completion_tokens": 100_000,
                "total_tokens": 1_100_000,
            }
        },
    )
    fake_runtime.status_map[str(sb.id)] = {"status": "running", "exit_code": None}

    reason = await _watch(
        session_factory, settings, fake_runtime, run.id, sb.id,
        budgets={"wall_clock_seconds": 3600, "max_cost_usd": 3.00},
    )
    assert reason == "budget_exceeded"
    term = next(
        p for p in _checkpoint_payloads(session_factory, sb.id)
        if p.get("stage") == "terminate"
    )
    assert term["limit"] == "cost"
    assert term["estimated_cost_usd"] == pytest.approx(3.5)
    assert term["budget_usd"] == 3.0


async def test_cost_budget_prices_via_model_prices_row(
    session_factory, settings, fake_runtime
) -> None:
    """The cap uses the LAYERED table — a model_prices glob can price a model
    the static table never heard of."""
    from db.models import ModelPriceRow

    with session_factory() as s:
        s.add(
            ModelPriceRow(
                model_glob="pr28-mon-*",
                input_per_mtok_usd=100.0,
                output_per_mtok_usd=100.0,
                source="user",
            )
        )
        s.commit()
    run = make_run(
        session_factory, status="running", config=run_config(), model_name="pr28-mon-7b"
    )
    sb = make_sandbox(session_factory, run.id, status="running")
    # 100k in @ $100/Mtok → $10 — way over a $5 cap.
    _add_event(
        session_factory, run.id, sb.id, "llm_response",
        {"usage": {"prompt_tokens": 100_000, "completion_tokens": 0, "total_tokens": 100_000}},
    )
    fake_runtime.status_map[str(sb.id)] = {"status": "running", "exit_code": None}

    reason = await _watch(
        session_factory, settings, fake_runtime, run.id, sb.id,
        budgets={"wall_clock_seconds": 3600, "max_cost_usd": 5.0},
    )
    assert reason == "budget_exceeded"
    term = next(
        p for p in _checkpoint_payloads(session_factory, sb.id)
        if p.get("stage") == "terminate"
    )
    assert term["limit"] == "cost"


async def test_cost_budget_unpriced_model_cannot_trip(
    session_factory, settings, fake_runtime
) -> None:
    """PR21 honesty: an unlisted model has no honest rate — the cap can't
    trip it no matter how many tokens it burns (never guessed)."""
    run = make_run(
        session_factory, status="running", config=run_config(), model_name="test-model"
    )
    sb = make_sandbox(session_factory, run.id, status="running")
    _add_event(
        session_factory, run.id, sb.id, "llm_response",
        {"usage": {"prompt_tokens": 10**9, "completion_tokens": 0, "total_tokens": 10**9}},
    )
    states = iter(
        [{"status": "running", "exit_code": None}, {"status": "exited", "exit_code": 0}]
    )
    fake_runtime.status_map[str(sb.id)] = lambda: next(states)
    fake_runtime.logs_map[str(sb.id)] = RESULT_LINE("agent_done") + "\n"

    reason = await _watch(
        session_factory, settings, fake_runtime, run.id, sb.id,
        budgets={"wall_clock_seconds": 3600, "max_cost_usd": 0.000001},
    )
    assert reason == "agent_done"


# ---------------------------------------------------------------------------
# F30 — budget extensions + headroom warnings + auto_extend
# ---------------------------------------------------------------------------


def _seed_extension(session_factory, run_id, sandbox_id, **payload):
    """A control-plane extend_budget grant — same rows POST .../extend_budget
    and auto_extend write."""
    _add_event(
        session_factory,
        run_id,
        sandbox_id,
        "checkpoint",
        {"stage": "budget_extended", "via": "manual", **payload},
    )


async def test_extension_defers_run_token_cap(
    session_factory, settings, fake_runtime
) -> None:
    """600 used > 500 cap would terminate budget_exceeded — a +200 grant
    widens the live cap (700) and the cell finishes normally."""
    run = make_run(session_factory, status="running", config=run_config())
    sb = make_sandbox(session_factory, run.id, status="running")
    _add_event(
        session_factory, run.id, sb.id, "llm_response",
        {"usage": {"total_tokens": 600}},
    )
    _seed_extension(session_factory, run.id, sb.id, added_tokens=200)
    states = iter(
        [{"status": "running", "exit_code": None}, {"status": "exited", "exit_code": 0}]
    )
    fake_runtime.status_map[str(sb.id)] = lambda: next(states)
    fake_runtime.logs_map[str(sb.id)] = RESULT_LINE("agent_done") + "\n"

    reason = await _watch(
        session_factory, settings, fake_runtime, run.id, sb.id,
        budgets={"wall_clock_seconds": 3600, "token_budget": 500},
    )
    assert reason == "agent_done"
    terminates = [
        p for p in _checkpoint_payloads(session_factory, sb.id)
        if p.get("stage") == "terminate"
    ]
    assert terminates == []


async def test_monitor_emits_budget_warning_once(
    session_factory, settings, fake_runtime
) -> None:
    """The gateway emits budget_warning on the request path; the monitor's
    poll is the backstop for cells that stop calling — one row per limit."""
    run = make_run(session_factory, status="running", config=run_config())
    sb = make_sandbox(session_factory, run.id, status="running")
    _add_event(
        session_factory, run.id, sb.id, "llm_response",
        {"usage": {"total_tokens": 450}},  # 90% of 500
    )
    # stay running for a few polls so dedup is exercised, then exit
    calls = {"n": 0}

    def _status():
        calls["n"] += 1
        if calls["n"] <= 3:
            return {"status": "running", "exit_code": None}
        return {"status": "exited", "exit_code": 0}

    fake_runtime.status_map[str(sb.id)] = _status
    fake_runtime.logs_map[str(sb.id)] = RESULT_LINE("agent_done") + "\n"

    reason = await _watch(
        session_factory, settings, fake_runtime, run.id, sb.id,
        budgets={"wall_clock_seconds": 3600, "token_budget": 500},
    )
    assert reason == "agent_done"
    warnings = [
        p for p in _checkpoint_payloads(session_factory, sb.id)
        if p.get("stage") == "budget_warning"
    ]
    assert len(warnings) == 1  # deduped across polls
    w = warnings[0]
    assert w["limit"] == "run_tokens"
    assert w["used_tokens"] == 450
    assert w["budget"] == 500.0
    assert w["remaining_tokens"] == 50
    assert w["pct"] == 0.9


async def test_auto_extend_grants_once_on_warning(
    session_factory, settings, fake_runtime
) -> None:
    """budgets.auto_extend → the first warn-condition poll emits a
    budget_extended via="auto" (+50% of the warned cap) — once per cell."""
    run = make_run(session_factory, status="running", config=run_config())
    sb = make_sandbox(session_factory, run.id, status="running")
    _add_event(
        session_factory, run.id, sb.id, "llm_response",
        {"usage": {"total_tokens": 450}},  # 90% of 500 → warning path
    )
    calls = {"n": 0}

    def _status():
        calls["n"] += 1
        if calls["n"] <= 3:
            return {"status": "running", "exit_code": None}
        return {"status": "exited", "exit_code": 0}

    fake_runtime.status_map[str(sb.id)] = _status
    fake_runtime.logs_map[str(sb.id)] = RESULT_LINE("agent_done") + "\n"

    reason = await _watch(
        session_factory, settings, fake_runtime, run.id, sb.id,
        budgets={
            "wall_clock_seconds": 3600,
            "token_budget": 500,
            "auto_extend": True,
        },
    )
    assert reason == "agent_done"
    grants = [
        p for p in _checkpoint_payloads(session_factory, sb.id)
        if p.get("stage") == "budget_extended"
    ]
    assert len(grants) == 1  # once only, across every poll
    assert grants[0]["via"] == "auto"
    assert grants[0]["added_tokens"] == 250  # +50% of the 500 cap


async def test_no_auto_extend_without_flag(
    session_factory, settings, fake_runtime
) -> None:
    """Same warn condition with auto_extend unset → warning only, no grant."""
    run = make_run(session_factory, status="running", config=run_config())
    sb = make_sandbox(session_factory, run.id, status="running")
    _add_event(
        session_factory, run.id, sb.id, "llm_response",
        {"usage": {"total_tokens": 450}},
    )
    states = iter(
        [{"status": "running", "exit_code": None}, {"status": "exited", "exit_code": 0}]
    )
    fake_runtime.status_map[str(sb.id)] = lambda: next(states)
    fake_runtime.logs_map[str(sb.id)] = RESULT_LINE("agent_done") + "\n"

    reason = await _watch(
        session_factory, settings, fake_runtime, run.id, sb.id,
        budgets={"wall_clock_seconds": 3600, "token_budget": 500},
    )
    assert reason == "agent_done"
    grants = [
        p for p in _checkpoint_payloads(session_factory, sb.id)
        if p.get("stage") == "budget_extended"
    ]
    assert grants == []


async def test_auto_extend_skips_when_manually_extended(
    session_factory, settings, fake_runtime
) -> None:
    """Any prior grant (manual or auto) satisfies the once-only rule."""
    run = make_run(session_factory, status="running", config=run_config())
    sb = make_sandbox(session_factory, run.id, status="running")
    _add_event(
        session_factory, run.id, sb.id, "llm_response",
        {"usage": {"total_tokens": 900}},
    )
    _seed_extension(session_factory, run.id, sb.id, added_tokens=600)
    calls = {"n": 0}

    def _status():
        calls["n"] += 1
        if calls["n"] <= 2:
            return {"status": "running", "exit_code": None}
        return {"status": "exited", "exit_code": 0}

    fake_runtime.status_map[str(sb.id)] = _status
    fake_runtime.logs_map[str(sb.id)] = RESULT_LINE("agent_done") + "\n"

    reason = await _watch(
        session_factory, settings, fake_runtime, run.id, sb.id,
        budgets={
            "wall_clock_seconds": 3600,
            "token_budget": 500,
            "auto_extend": True,
        },
    )
    assert reason == "agent_done"
    grants = [
        p for p in _checkpoint_payloads(session_factory, sb.id)
        if p.get("stage") == "budget_extended"
    ]
    assert len(grants) == 1  # only the seeded manual grant — no auto pile-on
    assert grants[0]["via"] == "manual"
