"""F30 — POST /api/sandboxes/{id}/extend_budget.

The grant is an append-only ``budget_extended`` checkpoint: the gateway
gate + monitor poll sum these rows into the cell's live ceilings, so a
manual bump takes effect without restarting the cell.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from api.deps import get_db
from api.main import app
from db.models import Base, Event, Run, Sandbox
from db.session import make_engine, make_session_factory


@pytest.fixture()
def session_factory(tmp_path: Path):
    engine = make_engine(f"sqlite:///{tmp_path}/sbx.db")
    Base.metadata.create_all(engine)
    sf = make_session_factory(engine)
    yield sf
    engine.dispose()


@pytest.fixture()
def api(session_factory):
    def override_get_db():
        session = session_factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = override_get_db
    client = TestClient(app)
    yield SimpleNamespace(client=client, session_factory=session_factory)
    app.dependency_overrides.pop(get_db, None)


def _run(session_factory, *, budgets: dict | None = None) -> Run:
    with session_factory() as s:
        run = Run(
            status="running",
            task_prompt="do the thing",
            model_name="test-model",
            config={
                "harness_configs": [{"adapter": "miniloop"}],
                "budgets": budgets
                or {"max_iters": 10, "token_budget": 50_000, "wall_clock_seconds": 600},
            },
        )
        s.add(run)
        s.commit()
        return run


def _sandbox(session_factory, run_id: uuid.UUID, *, status: str = "running") -> Sandbox:
    with session_factory() as s:
        sb = Sandbox(
            run_id=run_id,
            harness_config={"adapter": "miniloop"},
            status=status,
        )
        s.add(sb)
        s.commit()
        return sb


def _usage(session_factory, run_id: uuid.UUID, sandbox_id: uuid.UUID, tokens: int) -> None:
    with session_factory() as s:
        s.add(
            Event(
                run_id=run_id,
                sandbox_id=sandbox_id,
                source="gateway",
                type="llm_response",
                payload={"usage": {"total_tokens": tokens}},
            )
        )
        s.commit()


def _extend_events(session_factory, sandbox_id: uuid.UUID) -> list[dict]:
    with session_factory() as s:
        return [
            p
            for p in s.execute(
                select(Event.payload).where(
                    Event.sandbox_id == sandbox_id, Event.type == "checkpoint"
                )
            ).scalars()
            if isinstance(p, dict) and p.get("stage") == "budget_extended"
        ]


def test_extend_404_unknown_sandbox(api) -> None:
    resp = api.client.post(
        f"/api/sandboxes/{uuid.uuid4()}/extend_budget", json={"tokens": 5000}
    )
    assert resp.status_code == 404


def test_extend_409_terminal_sandbox(api) -> None:
    run = _run(api.session_factory)
    sb = _sandbox(api.session_factory, run.id, status="failed")
    resp = api.client.post(
        f"/api/sandboxes/{sb.id}/extend_budget", json={"tokens": 5000}
    )
    assert resp.status_code == 409
    assert "resubmit" in resp.json()["detail"]


def test_extend_422_neither_field(api) -> None:
    run = _run(api.session_factory)
    sb = _sandbox(api.session_factory, run.id)
    resp = api.client.post(f"/api/sandboxes/{sb.id}/extend_budget", json={})
    assert resp.status_code == 422


def test_extend_422_token_floor(api) -> None:
    run = _run(api.session_factory)
    sb = _sandbox(api.session_factory, run.id)
    resp = api.client.post(
        f"/api/sandboxes/{sb.id}/extend_budget", json={"tokens": 500}
    )
    assert resp.status_code == 422


def test_extend_tokens_widens_run_scope(api) -> None:
    """No per-cell cap → the grant widens the shared run ceiling."""
    run = _run(api.session_factory, budgets={"token_budget": 50_000})
    sb = _sandbox(api.session_factory, run.id)
    _usage(api.session_factory, run.id, sb.id, 45_000)

    resp = api.client.post(
        f"/api/sandboxes/{sb.id}/extend_budget", json={"tokens": 25_000}
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["sandbox_id"] == str(sb.id)
    assert data["added_tokens"] == 25_000
    assert data["total_added_tokens"] == 25_000
    assert data["extensions"] == 1
    budget = data["budget"]
    assert budget["scope"] == "run"
    assert budget["limit_tokens"] == 75_000
    assert budget["used_tokens"] == 45_000
    assert budget["extended_tokens"] == 25_000

    events = _extend_events(api.session_factory, sb.id)
    assert len(events) == 1
    assert events[0]["via"] == "manual"
    assert events[0]["added_tokens"] == 25_000
    assert events[0]["total_added_tokens"] == 25_000


def test_extend_tokens_sandbox_scope(api) -> None:
    """A per-cell cap set → used_tokens is the cell's OWN total."""
    run = _run(
        api.session_factory,
        budgets={"token_budget": 500_000, "sandbox_token_budget": 10_000},
    )
    sb = _sandbox(api.session_factory, run.id)
    other = _sandbox(api.session_factory, run.id)
    _usage(api.session_factory, run.id, sb.id, 9_000)
    _usage(api.session_factory, run.id, other.id, 400_000)  # sibling's burn

    resp = api.client.post(
        f"/api/sandboxes/{sb.id}/extend_budget", json={"tokens": 5_000}
    )
    assert resp.status_code == 200, resp.text
    budget = resp.json()["budget"]
    assert budget["scope"] == "sandbox"
    assert budget["used_tokens"] == 9_000  # own usage, not the run's 409k
    assert budget["limit_tokens"] == 15_000


def test_extend_accumulates(api) -> None:
    run = _run(api.session_factory)
    sb = _sandbox(api.session_factory, run.id)
    r1 = api.client.post(
        f"/api/sandboxes/{sb.id}/extend_budget", json={"tokens": 5_000}
    )
    assert r1.status_code == 200
    r2 = api.client.post(
        f"/api/sandboxes/{sb.id}/extend_budget", json={"tokens": 5_000}
    )
    assert r2.status_code == 200
    data = r2.json()
    assert data["added_tokens"] == 5_000
    assert data["total_added_tokens"] == 10_000
    assert data["extensions"] == 2
    assert len(_extend_events(api.session_factory, sb.id)) == 2


def test_extend_cost_grant(api) -> None:
    run = _run(api.session_factory, budgets={"max_cost_usd": 2.0})
    sb = _sandbox(api.session_factory, run.id)
    resp = api.client.post(
        f"/api/sandboxes/{sb.id}/extend_budget", json={"cost_usd": 1.5}
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["added_cost_usd"] == 1.5
    assert data["total_added_cost_usd"] == 1.5
    assert data["budget"]["limit_cost_usd"] == 3.5
    assert data["budget"]["extended_cost_usd"] == 1.5


def test_extend_warning_flag_reflects_prior_warning(api) -> None:
    """A budget_warning already emitted → the response flags it so the UI
    keeps its "near cap" badge after the bump."""
    run = _run(api.session_factory, budgets={"token_budget": 50_000})
    sb = _sandbox(api.session_factory, run.id)
    _usage(api.session_factory, run.id, sb.id, 45_000)
    with api.session_factory() as s:
        s.add(
            Event(
                run_id=run.id,
                sandbox_id=sb.id,
                source="gateway",
                type="checkpoint",
                payload={
                    "stage": "budget_warning",
                    "limit": "run_tokens",
                    "used_tokens": 45_000,
                    "budget": 50_000,
                },
            )
        )
        s.commit()
    resp = api.client.post(
        f"/api/sandboxes/{sb.id}/extend_budget", json={"tokens": 100_000}
    )
    assert resp.status_code == 200
    # prior warning recorded even though spend is now far below the cap
    assert resp.json()["budget"]["warning"] is True


def test_queued_sandbox_extends(api) -> None:
    """A pacing-queued cell can be granted headroom before it even spawns —
    the extension rides the cred gate when the cell starts calling."""
    run = _run(api.session_factory)
    sb = _sandbox(api.session_factory, run.id, status="queued")
    resp = api.client.post(
        f"/api/sandboxes/{sb.id}/extend_budget", json={"tokens": 5_000}
    )
    assert resp.status_code == 200


def test_run_detail_carries_budget_summary_and_pacing_cap(api) -> None:
    """GET /api/runs/{id} — every cell gets a live ``budget`` block and the
    run reports the local-provider cap (test env is all-ollama → cap 2)."""
    run = _run(
        api.session_factory,
        budgets={"token_budget": 50_000, "sandbox_token_budget": 10_000},
    )
    sb = _sandbox(api.session_factory, run.id, status="queued")
    _usage(api.session_factory, run.id, sb.id, 8_500)

    resp = api.client.get(f"/api/runs/{run.id}")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["sandbox_pacing_cap"] == 2
    (sb_out,) = data["sandboxes"]
    budget = sb_out["budget"]
    assert budget["scope"] == "sandbox"
    assert budget["used_tokens"] == 8_500
    assert budget["limit_tokens"] == 10_000
    assert budget["pct"] == 0.85
    # past the ~80% warn line even before any budget_warning event exists
    assert budget["warning"] is True
