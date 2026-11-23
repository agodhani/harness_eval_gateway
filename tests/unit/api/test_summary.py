"""PR36 — dashboard summary tests (GET /api/analytics/summary).

Cross-run aggregate like the leaderboard — it can't ride the shared
in-memory fixture, so same per-test tmp_path sqlite pattern as
test_leaderboard.py.

Timestamps are relative to ``datetime.now(UTC)`` in exact 7-day offsets —
one ISO week is always exactly one bucket earlier regardless of the
time-of-day the suite runs at, so bucket assertions stay deterministic.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from api.deps import get_db
from api.main import app
from db.models import Base, Event, Run, Sandbox, Score
from db.session import make_engine, make_session_factory

NOW = datetime.now(UTC)


@pytest.fixture()
def api(tmp_path: Path):
    engine = make_engine(f"sqlite:///{tmp_path}/summary.db")
    Base.metadata.create_all(engine)
    sf = make_session_factory(engine)

    def override_get_db():
        session = sf()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = override_get_db
    yield SimpleNamespace(client=TestClient(app), session_factory=sf)
    app.dependency_overrides.pop(get_db, None)
    engine.dispose()


def _llm(run_id, *, ts, sandbox_id=None, source="gateway", model=None, inp, out):
    return Event(
        run_id=run_id,
        sandbox_id=sandbox_id,
        source=source,
        type="llm_response",
        ts=ts,
        payload={
            "model": model,
            "latency_ms": 100,
            "usage": {
                "prompt_tokens": inp,
                "completion_tokens": out,
                "total_tokens": inp + out,
            },
        },
    )


@pytest.fixture()
def seeded(api):
    """Five runs spread over two ISO weeks + metered traffic.

    run_a (done, gpt-4o, evaluator claude-sonnet-4.5, created NOW):
      sb_a1 agent_done — participant llm_response {1000,500} @gpt-4o;
        eval boundary then a judge llm_response {2000,100} (claude rate);
        run-level llm_response {1000,500} on payload model gpt-4o.
        scores: sb_a1 8.0 → rescore 9.0 (newest wins), sb_a2 6.0 → mean 7.5
      sb_a2 timeout.
    run_b (running, qwen3:8b, created NOW−7d): sb_b1 running, no terminal
      reason — one local-model llm_response {800,200} @NOW−7d.
    run_c (queued, created+started NOW−7d) → queue position 1.
    run_e (queued, created+started NOW) → queue position 2.
    run_d (rubric_review, created NOW) → awaiting_review.
    """
    s: Session = api.session_factory()
    try:
        run_a = Run(
            task_prompt="build 2048",
            model_name="gpt-4o",
            config={"evaluator_model": "claude-sonnet-4.5"},
            status="done",
            created_at=NOW,
            finished_at=NOW,
        )
        run_b = Run(
            task_prompt="build snake",
            model_name="qwen3:8b",
            config={},
            status="running",
            created_at=NOW - timedelta(days=7),
        )
        run_c = Run(
            task_prompt="queued first",
            model_name="gpt-4o",
            config={},
            status="queued",
            created_at=NOW - timedelta(days=7),
            started_at=NOW - timedelta(days=7),
        )
        run_e = Run(
            task_prompt="queued second",
            model_name="gpt-4o",
            config={},
            status="queued",
            created_at=NOW,
            started_at=NOW,
        )
        run_d = Run(
            task_prompt="awaiting rubric",
            model_name="gpt-4o",
            config={},
            status="rubric_review",
            created_at=NOW,
        )
        s.add_all([run_a, run_b, run_c, run_e, run_d])
        s.flush()

        sb_a1 = Sandbox(
            run_id=run_a.id,
            name="a1",
            harness_config={"adapter": "miniloop", "skill_packs": []},
            status="done",
            terminal_reason="agent_done",
        )
        sb_a2 = Sandbox(
            run_id=run_a.id,
            name="a2",
            harness_config={"adapter": "miniloop", "skill_packs": []},
            status="done",
            terminal_reason="timeout",
        )
        sb_b1 = Sandbox(
            run_id=run_b.id,
            name="b1",
            harness_config={"adapter": "miniloop", "skill_packs": []},
            status="running",
            terminal_reason=None,
        )
        s.add_all([sb_a1, sb_a2, sb_b1])
        s.flush()

        s.add_all(
            [
                # Participant call on sb_a1 — gpt-4o: (1000·2.5 + 500·10)/1e6.
                _llm(run_a.id, ts=NOW, sandbox_id=sb_a1.id, model="gpt-4o", inp=1000, out=500),
                # Judge boundary — everything at/after it is eval traffic.
                Event(
                    run_id=run_a.id,
                    sandbox_id=sb_a1.id,
                    source="evaluator",
                    type="checkpoint",
                    ts=NOW + timedelta(hours=1),
                    payload={"stage": "eval_started"},
                ),
                # Judge call on the SAME sandbox (eval cred shares the id)
                # → priced at claude-sonnet-4.5: (2000·3 + 100·15)/1e6.
                _llm(
                    run_a.id,
                    ts=NOW + timedelta(hours=2),
                    sandbox_id=sb_a1.id,
                    model="claude-sonnet-4.5",
                    inp=2000,
                    out=100,
                ),
                # Run-level control-plane call (rubric drafting — no
                # sandbox) → priced at the payload model (gpt-4o).
                _llm(run_a.id, ts=NOW, sandbox_id=None, model="gpt-4o", inp=1000, out=500),
                # Participant call on sb_b1 — qwen3:8b local → $0, prior week.
                _llm(
                    run_b.id,
                    ts=NOW - timedelta(days=7),
                    sandbox_id=sb_b1.id,
                    model="qwen3:8b",
                    inp=800,
                    out=200,
                ),
            ]
        )
        s.add_all(
            [
                Score(
                    sandbox_id=sb_a1.id,
                    dimension="overall",
                    score=8.0,
                    rationale="",
                    created_at=NOW,
                    seq=1,
                ),
                Score(
                    sandbox_id=sb_a1.id,
                    dimension="overall",
                    score=9.0,
                    rationale="",
                    created_at=NOW + timedelta(hours=3),
                    seq=2,
                ),
                Score(
                    sandbox_id=sb_a2.id,
                    dimension="overall",
                    score=6.0,
                    rationale="",
                    created_at=NOW,
                    seq=1,
                ),
            ]
        )
        s.commit()
        return {
            "runs": {
                "a": run_a.id,
                "b": run_b.id,
                "c": run_c.id,
                "d": run_d.id,
                "e": run_e.id,
            },
            "sandboxes": {"a1": sb_a1.id, "a2": sb_a2.id, "b1": sb_b1.id},
        }
    finally:
        s.close()


def _get(client: TestClient, **params) -> dict:
    resp = client.get("/api/analytics/summary", params=params)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _week_of(ts: datetime) -> str:
    """ISO label the endpoint assigns — mirrors _week_start/_iso_week_label."""
    day = (ts if ts.tzinfo else ts.replace(tzinfo=UTC)).date()
    monday = day - timedelta(days=day.weekday())
    iso = monday.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def test_summary_empty_scope(api) -> None:
    body = _get(api.client)
    assert body["price_model"] is None
    t = body["totals"]
    assert t["runs"] == 0 and t["sandboxes"] == 0
    assert t["llm_turns"] == 0 and t["tokens"]["total"] == 0
    assert t["estimated_cost_usd"] == 0.0  # no traffic → honest $0
    assert t["unpriced_llm_turns"] == 0
    assert len(body["weeks"]) == 12
    assert all(w["runs"] == 0 for w in body["weeks"])
    assert body["terminal_mix"] == [] and body["terminal_pending"] == 0
    assert body["queue"] == {"active": [], "queued": [], "awaiting_review": 0}
    assert body["recent_runs"] == []


def test_summary_weeks_and_totals(api, seeded) -> None:
    body = _get(api.client)
    t = body["totals"]
    assert t["runs"] == 5
    assert t["sandboxes"] == 3
    assert t["scored_sandboxes"] == 2
    assert t["llm_turns"] == 4
    # in 1000+2000+1000+800, out 500+100+500+200
    assert t["tokens"]["input"] == 4800
    assert t["tokens"]["output"] == 1300
    assert t["tokens"]["total"] == 6100
    # gpt-4o participant 0.0075 + claude judge 0.0075 + run-level 0.0075
    # + qwen local 0.0
    assert t["estimated_cost_usd"] == pytest.approx(0.0225)
    assert t["unpriced_llm_turns"] == 0

    weeks = {w["week"]: w for w in body["weeks"]}
    assert len(body["weeks"]) == 12
    this_wk = weeks[_week_of(NOW)]
    prev_wk = weeks[_week_of(NOW - timedelta(days=7))]
    # current week: runs a, d, e created; 3 metered calls (participant,
    # judge, run-level).
    assert this_wk["runs"] == 3
    assert this_wk["llm_turns"] == 3
    assert this_wk["estimated_cost_usd"] == pytest.approx(0.0225)
    assert this_wk["unpriced_llm_turns"] == 0
    # prior week: runs b, c; one $0-local call — cost is a real zero.
    assert prev_wk["runs"] == 2
    assert prev_wk["llm_turns"] == 1
    assert prev_wk["estimated_cost_usd"] == 0.0


def test_summary_terminal_and_queue(api, seeded) -> None:
    body = _get(api.client)
    mix = {s["reason"]: s["count"] for s in body["terminal_mix"]}
    assert mix == {"agent_done": 1, "timeout": 1}
    assert body["terminal_pending"] == 1  # sb_b1 still running

    q = body["queue"]
    assert [r["status"] for r in q["active"]] == ["running"]
    assert q["active"][0]["run_id"] == str(seeded["runs"]["b"])
    assert [r["queue_position"] for r in q["queued"]] == [1, 2]
    # promotion order = started_at — run_c parked a week before run_e.
    assert q["queued"][0]["run_id"] == str(seeded["runs"]["c"])
    assert q["queued"][1]["run_id"] == str(seeded["runs"]["e"])
    assert q["awaiting_review"] == 1


def test_summary_recent_runs(api, seeded) -> None:
    body = _get(api.client)
    recent = body["recent_runs"]
    assert len(recent) == 5
    by_id = {r["run_id"]: r for r in recent}
    a = by_id[str(seeded["runs"]["a"])]
    assert a["status"] == "done"
    assert a["sandbox_count"] == 2
    # newest-wins: sb_a1 9.0 (rescore), sb_a2 6.0 → mean 7.5
    assert a["mean_overall_score"] == pytest.approx(7.5)
    b = by_id[str(seeded["runs"]["b"])]
    assert b["sandbox_count"] == 1
    assert b["mean_overall_score"] is None
    for key in ("c", "d", "e"):
        r = by_id[str(seeded["runs"][key])]
        assert r["sandbox_count"] == 0
        assert r["mean_overall_score"] is None
    # newest-first ordering (created_at desc).
    stamps = [r["created_at"] for r in recent]
    assert stamps == sorted(stamps, reverse=True)


def test_summary_unpriced(api) -> None:
    """A model with no rate must not bill at a fallback — the week and the
    grand total report unpriced (null) with the call counted."""
    s: Session = api.session_factory()
    try:
        run = Run(
            task_prompt="mystery",
            model_name="mystery-9b",
            config={},
            status="done",
            created_at=NOW,
        )
        s.add(run)
        s.flush()
        sb = Sandbox(
            run_id=run.id,
            harness_config={"adapter": "miniloop", "skill_packs": []},
            status="done",
            terminal_reason="agent_done",
        )
        s.add(sb)
        s.flush()
        s.add(
            _llm(run.id, ts=NOW, sandbox_id=sb.id, model="mystery-9b", inp=1000, out=500)
        )
        s.commit()
    finally:
        s.close()

    body = _get(api.client)
    t = body["totals"]
    assert t["llm_turns"] == 1
    assert t["tokens"]["total"] == 1500  # tokens are metered regardless
    assert t["estimated_cost_usd"] is None
    assert t["unpriced_llm_turns"] == 1
    wk = {w["week"]: w for w in body["weeks"]}[_week_of(NOW)]
    assert wk["estimated_cost_usd"] is None
    assert wk["unpriced_llm_turns"] == 1

    # An explicit reprice rescues the same traffic at the picked rate.
    priced = _get(api.client, price_model="gpt-4o")
    assert priced["price_model"] == "gpt-4o"
    assert priced["totals"]["estimated_cost_usd"] == pytest.approx(0.0075)
    assert priced["totals"]["unpriced_llm_turns"] == 0


def test_summary_price_model_reprice(api, seeded) -> None:
    body = _get(api.client, price_model="gpt-4o")
    assert body["price_model"] == "gpt-4o"
    # Every call at gpt-4o (2.5/10 per Mtok): participant .0075, judge
    # (2000·2.5 + 100·10)/1e6 = .006, run-level .0075, qwen-cell .004.
    assert body["totals"]["estimated_cost_usd"] == pytest.approx(0.025)
    resp = api.client.get("/api/analytics/summary", params={"price_model": "nope-9b"})
    assert resp.status_code == 422


def test_summary_window_params(api, seeded) -> None:
    body = _get(api.client, weeks=2, recent=2)
    assert len(body["weeks"]) == 2
    assert len(body["recent_runs"]) == 2
    # A weeks=1 window only covers the current ISO week — the older
    # run's traffic still lands in the totals, just not the series.
    one = _get(api.client, weeks=1)
    assert len(one["weeks"]) == 1
    assert one["weeks"][0]["week"] == _week_of(NOW)
    assert one["totals"]["llm_turns"] == 4
    for bad in (0, 53):
        resp = api.client.get("/api/analytics/summary", params={"weeks": bad})
        assert resp.status_code == 422
