"""F9 — run analytics endpoint tests (GET /api/runs/{id}/analytics).

Same sqlite + session-seeding pattern as test_results.py: Run/Sandbox/Event
rows are written straight through the session factory, then the TestClient
hits the endpoint.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from api.deps import get_session_factory
from api.pricing import PRICE_TABLE, estimate_cost_usd
from db.models import Base, Event, Run, Sandbox, Score

T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


@pytest.fixture()
def session_factory() -> sessionmaker[Session]:
    sf = get_session_factory()
    Base.metadata.create_all(sf.kw["bind"])
    return sf


def _ev(
    run_id,
    *,
    sandbox_id=None,
    source="harness",
    type_,
    ts=None,
    payload=None,
) -> Event:
    return Event(
        run_id=run_id,
        sandbox_id=sandbox_id,
        source=source,
        type=type_,
        ts=ts or T0,
        payload=payload or {},
    )


@pytest.fixture()
def analytics_run(session_factory: sessionmaker[Session]):
    """Run (model qwen3:8b → local $0 default) + 2 sandboxes + mixed events.

    sb1: 2 llm_responses (in 100+200, out 40+60, latency 500/1500),
    tool_calls shell×2 + write_file×1, 1 skill_invocation, 1 shell_command,
    1 file_op, 1 test_execution, 1 error, run_started/terminal_state markers —
    then an eval boundary + judge-side llm_response that must NOT count.
    sb2: 1 llm_response only.
    """
    s = session_factory()
    try:
        run = Run(
            task_prompt="build 2048",
            model_name="qwen3:8b",
            config={"evaluator_model": "gpt-4o"},
            status="done",
        )
        s.add(run)
        s.flush()
        sb1 = Sandbox(
            run_id=run.id,
            harness_config={"adapter": "miniloop", "skill_packs": ["superpowers"]},
            status="done",
            terminal_reason="agent_done",
        )
        sb2 = Sandbox(
            run_id=run.id,
            harness_config={"adapter": "miniloop", "skill_packs": []},
            status="failed",
            terminal_reason=None,  # filled in from terminal_state event
        )
        s.add_all([sb1, sb2])
        s.flush()

        def resp(sb, ts, prompt, completion, latency, model="qwen3:8b", source="gateway"):
            return _ev(
                run.id,
                sandbox_id=sb.id,
                source=source,
                type_="llm_response",
                ts=ts,
                payload={
                    "model": model,
                    "usage": {
                        "prompt_tokens": prompt,
                        "completion_tokens": completion,
                        "total_tokens": prompt + completion,
                    },
                    "latency_ms": latency,
                    "http_status": 200,
                },
            )

        s.add_all(
            [
                # ---- sb1 participant phase (t0 .. t0+9min) ----
                _ev(run.id, sandbox_id=sb1.id, type_="run_started", ts=T0,
                    payload={"model": "qwen3:8b"}),
                resp(sb1, T0 + timedelta(minutes=1), 100, 40, 500),
                _ev(run.id, sandbox_id=sb1.id, type_="tool_call",
                    ts=T0 + timedelta(minutes=2), payload={"name": "shell"}),
                _ev(run.id, sandbox_id=sb1.id, type_="shell_command",
                    ts=T0 + timedelta(minutes=2, seconds=1),
                    payload={"command": "ls", "exit_code": 0}),
                _ev(run.id, sandbox_id=sb1.id, type_="tool_call",
                    ts=T0 + timedelta(minutes=3), payload={"name": "shell"}),
                _ev(run.id, sandbox_id=sb1.id, type_="tool_call",
                    ts=T0 + timedelta(minutes=4), payload={"name": "write_file"}),
                _ev(run.id, sandbox_id=sb1.id, type_="file_op",
                    ts=T0 + timedelta(minutes=4, seconds=1),
                    payload={"op": "write", "path": "/w/app.py", "bytes": 10}),
                _ev(run.id, sandbox_id=sb1.id, type_="skill_invocation",
                    ts=T0 + timedelta(minutes=5),
                    payload={"name": "superpowers/brainstorming"}),
                _ev(run.id, sandbox_id=sb1.id, type_="test_execution",
                    ts=T0 + timedelta(minutes=6),
                    payload={"command": "pytest", "exit_code": 1}),
                _ev(run.id, sandbox_id=sb1.id, type_="error",
                    ts=T0 + timedelta(minutes=7),
                    payload={"message": "flaky"}),
                resp(sb1, T0 + timedelta(minutes=8), 200, 60, 1500),
                _ev(run.id, sandbox_id=sb1.id, type_="terminal_state",
                    ts=T0 + timedelta(minutes=9),
                    payload={"terminal_state": "agent_done", "iters": 9}),
                # ---- sb1 evaluator phase (judge, gpt-4o creds on sb1) ----
                _ev(run.id, sandbox_id=sb1.id, source="evaluator",
                    type_="checkpoint", ts=T0 + timedelta(minutes=20),
                    payload={"phase": "eval_started"}),
                resp(sb1, T0 + timedelta(minutes=21), 1000, 90, 800,
                     model="gpt-4o"),
                _ev(run.id, sandbox_id=sb1.id, source="evaluator",
                    type_="checkpoint", ts=T0 + timedelta(minutes=22),
                    payload={"phase": "eval_scored"}),
                # ---- sb2 participant phase ----
                _ev(run.id, sandbox_id=sb2.id, type_="run_started", ts=T0),
                resp(sb2, T0 + timedelta(minutes=1), 50, 25, 300),
                _ev(run.id, sandbox_id=sb2.id, type_="terminal_state",
                    ts=T0 + timedelta(minutes=2),
                    payload={"terminal_state": "timeout", "iters": 50}),
                # run-level event (no sandbox) — must not leak in
                _ev(run.id, source="orchestrator", type_="checkpoint",
                    ts=T0, payload={"stage": "rubric_approved"}),
            ]
        )
        s.commit()
        return {"run_id": run.id, "sb1": sb1.id, "sb2": sb2.id}
    finally:
        s.close()


def _get(client: TestClient, run_id, **params) -> dict:
    resp = client.get(f"/api/runs/{run_id}/analytics", params=params)
    assert resp.status_code == 200
    return resp.json()


def test_analytics_shape_and_defaults(client: TestClient, analytics_run) -> None:
    body = _get(client, analytics_run["run_id"])
    assert body["run_id"] == str(analytics_run["run_id"])
    assert body["model_name"] == "qwen3:8b"
    # run's model IS in the table → it's the default price model
    assert body["price_model"] == "qwen3:8b"
    assert body["price_table"]["qwen3:8b"]["local"] is True
    assert set(body["price_table"]) == set(PRICE_TABLE)
    assert len(body["sandboxes"]) == 2


def test_per_sandbox_aggregation(client: TestClient, analytics_run) -> None:
    body = _get(client, analytics_run["run_id"])
    sb1 = next(x for x in body["sandboxes"] if x["sandbox_id"] == str(analytics_run["sb1"]))

    assert sb1["status"] == "done"
    assert sb1["terminal_reason"] == "agent_done"
    assert sb1["label"] == "miniloop · superpowers"
    assert sb1["llm_turns"] == 2
    assert sb1["tokens"] == {"input": 300, "output": 100, "cache_read": 0, "total": 400}
    assert sb1["latency_ms"]["avg"] == pytest.approx(1000.0)
    assert sb1["latency_ms"]["p95"] == 1500.0
    assert sb1["latency_ms"]["max"] == 1500.0
    assert sb1["tool_calls"] == {
        "total": 3,
        "by_tool": {"shell": 2, "write_file": 1},
    }
    assert sb1["skill_calls"] == {
        "total": 1,
        "by_skill": {"superpowers/brainstorming": 1},
    }
    assert sb1["shell_commands"] == 1
    assert sb1["file_ops"] == 1
    assert sb1["test_executions"] == 1
    assert sb1["errors"] == 1
    # run_started @t0 → terminal_state @t0+9m (judge events excluded)
    assert sb1["wall_clock_seconds"] == pytest.approx(540.0)


def test_terminal_reason_falls_back_to_event(
    client: TestClient, analytics_run
) -> None:
    body = _get(client, analytics_run["run_id"])
    sb2 = next(x for x in body["sandboxes"] if x["sandbox_id"] == str(analytics_run["sb2"]))
    assert sb2["terminal_reason"] == "timeout"  # row NULL → event-derived
    assert sb2["llm_turns"] == 1
    assert sb2["tokens"]["total"] == 75
    assert sb2["wall_clock_seconds"] == pytest.approx(120.0)
    assert sb2["evaluator"] is None


def test_evaluator_phase_split_out(client: TestClient, analytics_run) -> None:
    """Judge traffic (same sandbox_id, post eval_started) is reported under
    ``evaluator``, not folded into participant metrics."""
    body = _get(client, analytics_run["run_id"])
    sb1 = next(x for x in body["sandboxes"] if x["sandbox_id"] == str(analytics_run["sb1"]))
    assert sb1["evaluator"] is not None
    assert sb1["evaluator"]["llm_turns"] == 1
    assert sb1["evaluator"]["tokens"]["input"] == 1000
    assert sb1["evaluator"]["tokens"]["output"] == 90
    # judge priced at evaluator_model gpt-4o: (1000*2.5 + 90*10)/1e6 = 0.0034
    assert sb1["evaluator"]["estimated_cost_usd"] == pytest.approx(0.0034)
    # participant metrics unaffected
    assert sb1["llm_turns"] == 2
    assert sb1["tokens"]["input"] == 300


def test_cost_estimate_price_model_param(client: TestClient, analytics_run) -> None:
    body = _get(client, analytics_run["run_id"], price_model="gpt-4o")
    assert body["price_model"] == "gpt-4o"
    sb1 = next(x for x in body["sandboxes"] if x["sandbox_id"] == str(analytics_run["sb1"]))
    expected = estimate_cost_usd(
        PRICE_TABLE["gpt-4o"], input_tokens=300, output_tokens=100
    )
    # (300*2.5 + 100*10)/1e6 = 0.00175
    assert expected == pytest.approx(0.00175)
    assert sb1["estimated_cost_usd"] == pytest.approx(expected)


def test_default_cost_is_zero_for_local_model(
    client: TestClient, analytics_run
) -> None:
    body = _get(client, analytics_run["run_id"])
    for sb in body["sandboxes"]:
        assert sb["estimated_cost_usd"] == 0.0


def test_default_price_model_falls_back_to_first_entry(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    s = session_factory()
    try:
        run = Run(task_prompt="x", model_name="mystery-model", config={}, status="running")
        s.add(run)
        s.commit()
        run_id = run.id
    finally:
        s.close()
    body = _get(client, run_id)
    assert body["price_model"] == next(iter(PRICE_TABLE))


def test_unknown_price_model_422(client: TestClient, analytics_run) -> None:
    resp = client.get(
        f"/api/runs/{analytics_run['run_id']}/analytics",
        params={"price_model": "not-a-model"},
    )
    assert resp.status_code == 422


def test_analytics_404(client: TestClient) -> None:
    assert client.get(f"/api/runs/{uuid.uuid4()}/analytics").status_code == 404


def test_zero_event_run_returns_zeros(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    s = session_factory()
    try:
        run = Run(task_prompt="empty", model_name="gpt-4o-mini", config={})
        s.add(run)
        s.flush()
        sb = Sandbox(
            run_id=run.id, harness_config={"adapter": "miniloop"}, status="provisioning"
        )
        s.add(sb)
        s.commit()
        run_id, sb_id = run.id, sb.id
    finally:
        s.close()

    body = _get(client, run_id)
    assert body["price_model"] == "gpt-4o-mini"  # run model in table
    (sb,) = body["sandboxes"]
    assert sb["sandbox_id"] == str(sb_id)
    assert sb["llm_turns"] == 0
    assert sb["tokens"] == {"input": 0, "output": 0, "cache_read": 0, "total": 0}
    assert sb["latency_ms"] == {"avg": None, "p95": None, "max": None}
    assert sb["tool_calls"] == {"total": 0, "by_tool": {}}
    assert sb["skill_calls"] == {"total": 0, "by_skill": {}}
    assert sb["shell_commands"] == 0
    assert sb["file_ops"] == 0
    assert sb["test_executions"] == 0
    assert sb["errors"] == 0
    assert sb["wall_clock_seconds"] == 0.0
    assert sb["estimated_cost_usd"] == 0.0
    assert sb["evaluator"] is None


# ---------------------------------------------------------------------------
# F13 — cross-run trends (GET /api/analytics/runs)
# ---------------------------------------------------------------------------


@pytest.fixture()
def trend_runs(session_factory: sessionmaker[Session]):
    """Two scored runs for cross-run aggregation.

    run_old (T0): qwen3:8b (local → $0 auto-price), 1 sandbox, participant
    tokens 100/50, one overall score 8 → mean 8; 200-char prompt (truncated
    to 120 in the response).
    run_new (T0+1d): gpt-4o, judge claude-sonnet-4.5, 2 sandboxes —
    participant turns 3 (in 900 / out 300), 1 error, judge turn 1
    (1000/200); overall scores 6 + 4 → mean 5 (a code_quality row must not
    pollute the mean).
    """
    s = session_factory()
    try:
        old = Run(
            task_prompt="old run " + "x" * 200,
            model_name="qwen3:8b",
            config={},
            status="done",
            created_at=T0,
        )
        new = Run(
            task_prompt="new run — build 2048",
            model_name="gpt-4o",
            config={"evaluator_model": "claude-sonnet-4.5"},
            status="evaluating",
            created_at=T0 + timedelta(days=1),
        )
        s.add_all([old, new])
        s.flush()
        sb_old = Sandbox(
            run_id=old.id, harness_config={"adapter": "miniloop"}, status="done"
        )
        sb_new1 = Sandbox(
            run_id=new.id, harness_config={"adapter": "miniloop"}, status="done"
        )
        sb_new2 = Sandbox(
            run_id=new.id, harness_config={"adapter": "codex"}, status="failed"
        )
        s.add_all([sb_old, sb_new1, sb_new2])
        s.flush()

        def resp(sb, ts, prompt, completion, source="gateway"):
            return _ev(
                new.id if sb.run_id == new.id else old.id,
                sandbox_id=sb.id,
                source=source,
                type_="llm_response",
                ts=ts,
                payload={
                    "usage": {
                        "prompt_tokens": prompt,
                        "completion_tokens": completion,
                        "total_tokens": prompt + completion,
                    },
                    "latency_ms": 500,
                },
            )

        t1 = T0 + timedelta(days=1)
        s.add_all(
            [
                # old run: participant only
                resp(sb_old, T0 + timedelta(minutes=1), 100, 50),
                # new run: sb_new1 participant ×2, then judge boundary + traffic
                resp(sb_new1, t1 + timedelta(minutes=1), 200, 100),
                resp(sb_new1, t1 + timedelta(minutes=2), 300, 50),
                _ev(new.id, sandbox_id=sb_new1.id, source="evaluator",
                    type_="checkpoint", ts=t1 + timedelta(minutes=10),
                    payload={"phase": "eval_started"}),
                resp(sb_new1, t1 + timedelta(minutes=11), 1000, 200),
                # sb_new2 participant ×1 + an error
                resp(sb_new2, t1 + timedelta(minutes=1), 400, 150),
                _ev(new.id, sandbox_id=sb_new2.id, type_="error",
                    ts=t1 + timedelta(minutes=2), payload={"message": "boom"}),
            ]
        )
        s.add_all(
            [
                Score(sandbox_id=sb_old.id, dimension="overall", score=8.0,
                      rationale="good"),
                Score(sandbox_id=sb_new1.id, dimension="overall", score=6.0,
                      rationale="ok"),
                Score(sandbox_id=sb_new2.id, dimension="overall", score=4.0,
                      rationale="weak"),
                Score(sandbox_id=sb_new1.id, dimension="code_quality",
                      score=7.0, rationale="fine"),
            ]
        )
        s.commit()
        return {"old": old.id, "new": new.id}
    finally:
        s.close()


def _trend_point(body: dict, run_id) -> dict:
    return next(p for p in body["points"] if p["run_id"] == str(run_id))


def test_trends_shape_and_newest_first(
    client: TestClient, trend_runs
) -> None:
    # limit=100 so every session run fits — points arrive newest-first.
    body = client.get("/api/analytics/runs", params={"limit": 100}).json()
    assert body["price_model"] is None  # auto mode → per-run pricing
    assert set(body["price_table"]) == set(PRICE_TABLE)
    ids = [p["run_id"] for p in body["points"]]
    assert len(ids) == len(set(ids))  # one point per run
    assert ids.index(str(trend_runs["new"])) < ids.index(str(trend_runs["old"]))


def test_trends_point_aggregation(client: TestClient, trend_runs) -> None:
    body = client.get("/api/analytics/runs", params={"limit": 100}).json()
    pt = _trend_point(body, trend_runs["new"])

    assert pt["status"] == "evaluating"
    assert pt["model_name"] == "gpt-4o"
    assert pt["evaluator_model"] == "claude-sonnet-4.5"
    assert pt["task_prompt"] == "new run — build 2048"
    assert pt["sandbox_count"] == 2
    assert pt["llm_turns"] == 3
    assert pt["tokens"] == {
        "input": 900,
        "output": 300,
        "cache_read": 0,
        "total": 1200,
    }
    assert pt["errors"] == 1
    # judge phase split out at eval_started
    assert pt["evaluator_llm_turns"] == 1
    assert pt["evaluator_tokens"]["total"] == 1200
    # auto pricing: run model gpt-4o is listed → its own rate
    assert pt["price_model"] == "gpt-4o"
    # (900*2.5 + 300*10)/1e6 = 0.00525
    assert pt["estimated_cost_usd"] == pytest.approx(
        estimate_cost_usd(PRICE_TABLE["gpt-4o"], input_tokens=900, output_tokens=300)
    )
    # judge priced at claude-sonnet-4.5: (1000*3 + 200*15)/1e6 = 0.006
    assert pt["evaluator_cost_usd"] == pytest.approx(0.006)
    # mean of overall scores 6 + 4; the code_quality row must not count
    assert pt["mean_overall_score"] == pytest.approx(5.0)


def test_trends_auto_pricing_uses_each_runs_model(
    client: TestClient, trend_runs
) -> None:
    body = client.get("/api/analytics/runs", params={"limit": 100}).json()
    old = _trend_point(body, trend_runs["old"])
    assert old["price_model"] == "qwen3:8b"  # local → $0
    assert old["estimated_cost_usd"] == 0.0
    assert old["evaluator_cost_usd"] == 0.0
    assert old["sandbox_count"] == 1
    assert old["llm_turns"] == 1
    assert old["mean_overall_score"] == pytest.approx(8.0)
    assert len(old["task_prompt"]) == 120  # truncated server-side


def test_trends_uniform_repricing(client: TestClient, trend_runs) -> None:
    body = client.get(
        "/api/analytics/runs", params={"limit": 100, "price_model": "claude-opus-4.1"}
    ).json()
    assert body["price_model"] == "claude-opus-4.1"
    for pt in body["points"]:
        assert pt["price_model"] == "claude-opus-4.1"
    old = _trend_point(body, trend_runs["old"])
    # (100*15 + 50*75)/1e6 = 0.00525 — even the local-model run reprices
    assert old["estimated_cost_usd"] == pytest.approx(0.00525)


def test_trends_unknown_price_model_422(client: TestClient, trend_runs) -> None:
    resp = client.get("/api/analytics/runs", params={"price_model": "nope"})
    assert resp.status_code == 422


def test_trends_limit_bounds(client: TestClient, trend_runs) -> None:
    body = client.get("/api/analytics/runs", params={"limit": 1}).json()
    assert len(body["points"]) == 1
    assert client.get("/api/analytics/runs", params={"limit": 0}).status_code == 422
    assert client.get("/api/analytics/runs", params={"limit": 101}).status_code == 422


def test_trends_run_without_scores_has_null_mean(
    client: TestClient, analytics_run
) -> None:
    body = client.get("/api/analytics/runs", params={"limit": 100}).json()
    pt = _trend_point(body, analytics_run["run_id"])
    assert pt["mean_overall_score"] is None
    assert pt["sandbox_count"] == 2
