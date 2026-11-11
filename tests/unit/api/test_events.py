"""F6 — event ingestion / query / SSE tests.

Runs against in-memory sqlite (tests/conftest.py sets DATABASE_URL before
api.main is imported). The schema is created lazily per test via
``Base.metadata.create_all`` on the shared StaticPool engine.
"""

from __future__ import annotations

import json
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from api.deps import get_session_factory
from api.routers import events as events_router
from db.models import Base, Run, Sandbox

INGEST_TOKEN = "dev-ingest-token"  # api.config.Settings default
AUTH = {"X-Ingest-Token": INGEST_TOKEN}


@pytest.fixture()
def session_factory() -> sessionmaker[Session]:
    sf = get_session_factory()
    Base.metadata.create_all(sf.kw["bind"])
    return sf


@pytest.fixture()
def run_and_sandbox(session_factory: sessionmaker[Session]) -> tuple[uuid.UUID, uuid.UUID]:
    """A persisted Run + Sandbox; returns (run_id, sandbox_id)."""
    s = session_factory()
    try:
        run = Run(task_prompt="do a thing", model_name="test-model", config={})
        s.add(run)
        s.flush()  # populate run.id (python-side uuid4 default)
        sandbox = Sandbox(run_id=run.id, harness_config={}, status="running")
        s.add(sandbox)
        s.commit()
        return run.id, sandbox.id
    finally:
        s.close()


def _event(run_id, **overrides) -> dict:
    base = {
        "run_id": str(run_id),
        "source": "harness",
        "type": "tool_call",
        "payload": {"tool": "shell"},
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# POST /api/events/ingest
# ---------------------------------------------------------------------------


def test_ingest_batch_then_query_roundtrip(
    client: TestClient, run_and_sandbox
) -> None:
    run_id, sandbox_id = run_and_sandbox
    ts = "2025-01-02T03:04:05+00:00"
    body = {
        "events": [
            _event(run_id, sandbox_id=str(sandbox_id), seq=1, ts=ts),
            _event(
                run_id,
                source="gateway",
                type="llm_request",
                seq=2,
                payload={"model": "qwen3:8b"},
            ),
        ]
    }
    resp = client.post("/api/events/ingest", json=body, headers=AUTH)
    assert resp.status_code == 200
    assert resp.json() == {"accepted": 2}

    got = client.get(f"/api/runs/{run_id}/events")
    assert got.status_code == 200
    events = got.json()
    assert len(events) == 2
    # ordered by id ASC, server-assigned id/ts
    assert events[0]["id"] < events[1]["id"]
    assert events[0]["seq"] == 1
    assert events[0]["sandbox_id"] == str(sandbox_id)
    assert events[0]["source"] == "harness"
    assert events[0]["type"] == "tool_call"
    assert events[0]["payload"] == {"tool": "shell"}
    # producer-supplied ts is passed through
    assert events[0]["ts"].startswith("2025-01-02T03:04:05")
    # server-assigned ts when omitted
    assert events[1]["ts"] is not None


def test_ingest_single_event_object(client: TestClient, run_and_sandbox) -> None:
    run_id, _ = run_and_sandbox
    resp = client.post(
        "/api/events/ingest",
        json=_event(run_id, type="done_signal", payload={"summary": "ok"}),
        headers=AUTH,
    )
    assert resp.status_code == 200
    assert resp.json() == {"accepted": 1}
    events = client.get(f"/api/runs/{run_id}/events").json()
    assert len(events) == 1
    assert events[0]["type"] == "done_signal"


def test_ingest_auth_required(client: TestClient, run_and_sandbox) -> None:
    run_id, _ = run_and_sandbox
    body = {"events": [_event(run_id)]}

    missing = client.post("/api/events/ingest", json=body)
    assert missing.status_code == 401

    wrong = client.post(
        "/api/events/ingest", json=body, headers={"X-Ingest-Token": "nope"}
    )
    assert wrong.status_code == 403


def test_ingest_unknown_run_404(client: TestClient) -> None:
    resp = client.post(
        "/api/events/ingest",
        json={"events": [_event(uuid.uuid4())]},
        headers=AUTH,
    )
    assert resp.status_code == 404


def test_ingest_unknown_sandbox_404(client: TestClient, run_and_sandbox) -> None:
    run_id, _ = run_and_sandbox
    resp = client.post(
        "/api/events/ingest",
        json={"events": [_event(run_id, sandbox_id=str(uuid.uuid4()))]},
        headers=AUTH,
    )
    assert resp.status_code == 404


def test_ingest_sandbox_from_other_run_422(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    """A sandbox_id belonging to a different run is rejected."""
    s = session_factory()
    try:
        run_a = Run(task_prompt="a", model_name="m", config={})
        run_b = Run(task_prompt="b", model_name="m", config={})
        s.add_all([run_a, run_b])
        s.flush()
        sandbox_b = Sandbox(run_id=run_b.id, harness_config={}, status="running")
        s.add(sandbox_b)
        s.commit()
        run_a_id, sandbox_b_id = run_a.id, sandbox_b.id
    finally:
        s.close()

    resp = client.post(
        "/api/events/ingest",
        json={"events": [_event(run_a_id, sandbox_id=str(sandbox_b_id))]},
        headers=AUTH,
    )
    assert resp.status_code == 422


def test_ingest_invalid_source_or_type_422(client: TestClient, run_and_sandbox) -> None:
    run_id, _ = run_and_sandbox
    resp = client.post(
        "/api/events/ingest",
        json={"events": [_event(run_id, source="bogus")]},
        headers=AUTH,
    )
    assert resp.status_code == 422
    resp = client.post(
        "/api/events/ingest",
        json={"events": [_event(run_id, type="bogus")]},
        headers=AUTH,
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# GET /api/runs/{run_id}/events — filters + pagination
# ---------------------------------------------------------------------------


def _seed_events(client: TestClient, run_id, sandbox_id) -> list[dict]:
    body = {
        "events": [
            _event(run_id, sandbox_id=str(sandbox_id), source="harness",
                   type="tool_call", seq=1),
            _event(run_id, source="gateway", type="llm_request", seq=2),
            _event(run_id, sandbox_id=str(sandbox_id), source="harness",
                   type="shell_command", seq=3),
        ]
    }
    resp = client.post("/api/events/ingest", json=body, headers=AUTH)
    assert resp.json() == {"accepted": 3}
    return client.get(f"/api/runs/{run_id}/events").json()


def test_query_filters(client: TestClient, run_and_sandbox) -> None:
    run_id, sandbox_id = run_and_sandbox
    events = _seed_events(client, run_id, sandbox_id)
    assert len(events) == 3
    e1, e2, e3 = (e["id"] for e in events)

    def q(**params):
        return client.get(f"/api/runs/{run_id}/events", params=params).json()

    assert [e["id"] for e in q(source="harness")] == [e1, e3]
    assert [e["id"] for e in q(type="tool_call")] == [e1]
    assert [e["id"] for e in q(sandbox_id=str(sandbox_id))] == [e1, e3]
    assert [e["id"] for e in q(source="harness", type="shell_command")] == [e3]
    # run-level events only (no sandbox_id)
    assert [e["id"] for e in q(source="gateway")] == [e2]


def test_query_after_id_and_limit(client: TestClient, run_and_sandbox) -> None:
    run_id, sandbox_id = run_and_sandbox
    events = _seed_events(client, run_id, sandbox_id)
    e1, e2, e3 = (e["id"] for e in events)

    page1 = client.get(f"/api/runs/{run_id}/events", params={"limit": 2}).json()
    assert [e["id"] for e in page1] == [e1, e2]

    page2 = client.get(
        f"/api/runs/{run_id}/events", params={"after_id": page1[-1]["id"]}
    ).json()
    assert [e["id"] for e in page2] == [e3]

    assert client.get(
        f"/api/runs/{run_id}/events", params={"after_id": e3}
    ).json() == []


def test_query_limit_validation(client: TestClient, run_and_sandbox) -> None:
    run_id, _ = run_and_sandbox
    assert client.get(f"/api/runs/{run_id}/events", params={"limit": 0}).status_code == 422
    assert client.get(f"/api/runs/{run_id}/events", params={"limit": 1001}).status_code == 422


# ---------------------------------------------------------------------------
# GET /api/runs/{run_id}/events/stream — SSE
# ---------------------------------------------------------------------------


def test_sse_generator_emits_events(
    client: TestClient, run_and_sandbox
) -> None:
    run_id, sandbox_id = run_and_sandbox
    _seed_events(client, run_id, sandbox_id)

    chunks = list(
        events_router.stream_run_events(run_id, poll_interval=0.01, max_idle_polls=1)
    )
    text = "".join(chunks)
    assert "data: " in text
    data_lines = [ln[6:] for ln in text.splitlines() if ln.startswith("data: ")]
    assert len(data_lines) == 3
    parsed = [json.loads(line) for line in data_lines]
    assert [e["type"] for e in parsed] == ["tool_call", "llm_request", "shell_command"]
    assert all(e["run_id"] == str(run_id) for e in parsed)
    # keepalive comment emitted while idle before shutdown
    assert ": keepalive" in text


def test_sse_endpoint_content_type_and_data(
    client: TestClient, run_and_sandbox
) -> None:
    run_id, _ = run_and_sandbox
    client.post(
        "/api/events/ingest",
        json={"events": [_event(run_id, type="checkpoint")]},
        headers=AUTH,
    )
    with client.stream("GET", f"/api/runs/{run_id}/events/stream") as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        seen_data = False
        lines = resp.iter_lines()
        for _ in range(10):
            line = next(lines)
            if line.startswith("data: "):
                payload = json.loads(line[6:])
                assert payload["type"] == "checkpoint"
                seen_data = True
                break
        assert seen_data


def test_sse_endpoint_object(run_and_sandbox) -> None:
    """The route returns a StreamingResponse with SSE media type."""
    run_id, _ = run_and_sandbox
    resp = events_router.stream_events(run_id)
    assert resp.media_type == "text/event-stream"
