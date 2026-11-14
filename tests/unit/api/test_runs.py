"""F1/F2 — POST /api/runs, rubric review/approve, run detail.

Hermetic: per-test file-backed sqlite (dependency-overridden ``get_db``),
fake skillpacks dir, and stubbed orchestrator background tasks — the REAL
``runner.start_*_task``/``_track`` wiring runs, but ``draft_rubric`` and
``run_lifecycle`` are replaced with in-process fakes so no gateway/docker
is touched.
"""

from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from api.deps import get_db
from api.main import app
from api.routers import runs as runs_router
from db.models import Base, Rubric, Run
from db.session import make_engine, make_session_factory
from orchestrator import lifecycle as lifecycle_mod
from orchestrator import rubric as rubric_mod
from orchestrator import runner, state_machine

DIMS = [
    "functional_requirements",
    "acceptance_criteria",
    "code_quality",
    "cyclomatic_complexity",
]


def _rubric_content(**overrides: Any) -> dict[str, Any]:
    content: dict[str, Any] = {
        "summary": "scores the artifact",
        "dimensions": [
            {
                "name": name,
                "title": name.replace("_", " ").title(),
                "description": f"desc {name}",
                "weight": 1.0,
                "min_score": 0.0,
                "max_score": 10.0,
            }
            for name in DIMS
        ],
        "criteria": [
            {"id": "c1", "text": "creates hello.txt", "dimension": "functional_requirements"}
        ],
        "generated_by": {},
    }
    content.update(overrides)
    return content


def _valid_body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "task_prompt": "create hello.txt containing hi",
        "model_name": "qwen3:8b",
        "harness_configs": [
            {"adapter": "miniloop", "skill_packs": ["pack-a"]},
            {"adapter": "miniloop", "skill_packs": [], "system_prompt_extra": "be terse"},
        ],
        "evaluator_model": "eval-model",
        "budgets": {"max_iters": 10, "token_budget": 50_000, "wall_clock_seconds": 600},
    }
    body.update(overrides)
    return body


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def session_factory(tmp_path: Path):
    engine = make_engine(f"sqlite:///{tmp_path}/runs.db")
    Base.metadata.create_all(engine)
    sf = make_session_factory(engine)
    yield sf
    engine.dispose()


@pytest.fixture()
def packs_dir(tmp_path: Path) -> Path:
    root = tmp_path / "skillpacks"
    (root / "pack-a" / "skills").mkdir(parents=True)
    (root / "pack-b").mkdir(parents=True)
    return root


@pytest.fixture()
def api(session_factory, packs_dir: Path, tmp_path: Path, monkeypatch):
    """TestClient + recording lists for the background-task fakes."""
    ns = SimpleNamespace(
        DATABASE_URL="sqlite:///:memory:",
        GATEWAY_URL="http://gateway.test:8080",
        GATEWAY_CRED_SECRET="unit-test-secret",
        RUBRIC_MODEL="",
        SANDBOX_IMAGE="heg-test:latest",
        MONITOR_POLL_SECONDS=0.01,
        EVAL_TIMEOUT_SECONDS=5.0,
        HOST_DATA_ROOT="",
        SKILLPACKS_DIR=packs_dir,
        ARTIFACT_ROOT=tmp_path / "artifacts",
    )
    rubric_calls: list[uuid.UUID] = []
    lifecycle_calls: list[uuid.UUID] = []

    async def fake_draft_rubric(run_id, **_kwargs):
        rubric_calls.append(run_id)
        # Simulate a successful LLM draft: transition + insert draft rubric.
        with session_factory() as s:
            run = s.get(Run, run_id)
            if run is None or run.status != "submitted":
                return
            state_machine.transition(s, run, "rubric_review")
            s.add(
                Rubric(
                    run_id=run.id,
                    version=1,
                    status="draft",
                    content=_rubric_content(),
                )
            )
            s.commit()

    async def fake_run_lifecycle(run_id, **_kwargs):
        lifecycle_calls.append(run_id)

    # runs.py resolves get_settings() from its own module globals.
    monkeypatch.setattr(runs_router, "get_settings", lambda: ns)
    # runner resolves _session_factory/_settings lazily per call.
    monkeypatch.setattr(runner, "_session_factory", lambda: session_factory)
    monkeypatch.setattr(runner, "_settings", lambda: ns)
    # The orchestrator entry points themselves are stubbed (no gateway/docker).
    monkeypatch.setattr(rubric_mod, "draft_rubric", fake_draft_rubric)
    monkeypatch.setattr(lifecycle_mod, "run_lifecycle", fake_run_lifecycle)

    def override_get_db():
        session = session_factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = override_get_db
    client = TestClient(app)
    yield SimpleNamespace(
        client=client,
        session_factory=session_factory,
        settings=ns,
        rubric_calls=rubric_calls,
        lifecycle_calls=lifecycle_calls,
    )
    app.dependency_overrides.pop(get_db, None)


def _wait_run(client: TestClient, run_id: uuid.UUID | str, **match) -> dict:
    """Poll GET /api/runs/{id} until all ``match`` key/values hold."""
    deadline = time.monotonic() + 5.0
    last: dict = {}
    while time.monotonic() < deadline:
        resp = client.get(f"/api/runs/{run_id}")
        assert resp.status_code == 200, resp.text
        last = resp.json()
        if all(last.get(k) == v for k, v in match.items()):
            return last
        time.sleep(0.05)
    raise AssertionError(f"run {run_id} never reached {match}; last={last}")


def _post_run(client: TestClient, body: dict | None = None) -> dict:
    resp = client.post("/api/runs", json=body or _valid_body())
    assert resp.status_code == 201, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# POST /api/runs
# ---------------------------------------------------------------------------


def test_create_run_persists_and_schedules_rubric(api) -> None:
    data = _post_run(api.client)
    run_id = data["id"]

    assert data["status"] == "submitted"
    assert data["task_prompt"] == "create hello.txt containing hi"
    assert data["model_name"] == "qwen3:8b"
    assert data["same_model_eval"] is False
    cfg = data["config"]
    assert len(cfg["harness_configs"]) == 2
    assert cfg["harness_configs"][0]["skill_packs"] == ["pack-a"]
    assert cfg["harness_configs"][1]["system_prompt_extra"] == "be terse"
    assert cfg["evaluator_model"] == "eval-model"
    assert cfg["budgets"]["token_budget"] == 50_000
    assert data["sandboxes"] == []
    assert data["rubric"] is None

    # background rubric task was registered and completes -> rubric_review
    detail = _wait_run(api.client, run_id, status="rubric_review")
    assert api.rubric_calls == [uuid.UUID(run_id)]
    assert detail["rubric"]["status"] == "draft"
    assert detail["rubric"]["version"] == 1
    assert {d["name"] for d in detail["rubric"]["content"]["dimensions"]} == set(DIMS)


def test_create_run_defaults(api) -> None:
    body = {
        "task_prompt": "do the thing",
        "model_name": "qwen3:8b",
        "harness_configs": [{}],  # all defaults: miniloop, no packs
    }
    data = _post_run(api.client, body)
    cfg = data["config"]
    assert cfg["harness_configs"][0]["adapter"] == "miniloop"
    assert cfg["harness_configs"][0]["skill_packs"] == []
    assert cfg["budgets"] == {
        "max_iters": 50,
        "token_budget": 200_000,
        "wall_clock_seconds": 3600,
    }
    # evaluator defaults to participant model -> same_model_eval flag
    assert cfg["evaluator_model"] == "qwen3:8b"
    assert data["same_model_eval"] is True


def test_create_run_multi_adapter(api) -> None:
    """Phase 2.5: every registered adapter name validates at submit."""
    body = _valid_body(
        harness_configs=[
            {"adapter": a}
            for a in ("miniloop", "claude_code", "codex", "deepseek", "pi")
        ]
    )
    data = _post_run(api.client, body)
    adapters = [hc["adapter"] for hc in data["config"]["harness_configs"]]
    assert adapters == ["miniloop", "claude_code", "codex", "deepseek", "pi"]


def test_create_run_harness_config_names(api) -> None:
    """PR1: optional per-config name survives submit -> run.config round-trip."""
    body = _valid_body(
        harness_configs=[
            {"adapter": "miniloop", "name": "baseline-loop"},
            {"adapter": "miniloop"},  # no name -> null, provision defaults it
        ]
    )
    data = _post_run(api.client, body)
    hcs = data["config"]["harness_configs"]
    assert hcs[0]["name"] == "baseline-loop"
    assert hcs[1]["name"] is None


def test_create_run_validation(api) -> None:
    client = api.client
    cases = [
        # schema bounds
        {**_valid_body(), "task_prompt": ""},
        {**_valid_body(), "harness_configs": []},
        {**_valid_body(), "harness_configs": [{}] * 9},
        {
            **_valid_body(),
            "harness_configs": [{"skill_packs": [f"p{i}" for i in range(17)]}],
        },
        {
            **_valid_body(),
            "harness_configs": [{"system_prompt_extra": "x" * 8001}],
        },
        {
            **_valid_body(),
            "harness_configs": [{"name": "x" * 65}],
        },
        {**_valid_body(), "budgets": {"max_iters": 0}},
        # submit-time semantic validation
        {
            **_valid_body(),
            "harness_configs": [{"adapter": "not-an-adapter"}],
        },
        {
            **_valid_body(),
            "harness_configs": [{"skill_packs": ["does-not-exist"]}],
        },
    ]
    for body in cases:
        resp = client.post("/api/runs", json=body)
        assert resp.status_code == 422, f"{body} -> {resp.status_code}: {resp.text}"


# ---------------------------------------------------------------------------
# GET /api/runs + /api/runs/{id}
# ---------------------------------------------------------------------------


def test_list_and_get_runs(api) -> None:
    client = api.client
    first = _post_run(client)
    second = _post_run(client, {**_valid_body(), "task_prompt": "second task"})

    # created_at has second precision — backdate the first run so the
    # newest-first ordering is deterministic.
    with api.session_factory() as s:
        run = s.get(Run, uuid.UUID(first["id"]))
        run.created_at = datetime.now(UTC) - timedelta(hours=1)
        s.commit()

    resp = client.get("/api/runs")
    assert resp.status_code == 200
    rows = resp.json()
    ids = [r["id"] for r in rows]
    # newest first
    assert ids.index(second["id"]) < ids.index(first["id"])
    mine = {r["id"]: r for r in rows if r["id"] in (first["id"], second["id"])}
    assert mine[first["id"]]["sandbox_count"] == 0
    # fake draft task may already have advanced it past 'submitted'
    assert mine[first["id"]]["status"] in ("submitted", "rubric_review")
    assert len(mine[first["id"]]["task_prompt"]) <= 200

    resp = client.get("/api/runs", params={"status": "done"})
    assert all(r["status"] == "done" for r in resp.json())

    detail = client.get(f"/api/runs/{first['id']}")
    assert detail.status_code == 200
    assert detail.json()["id"] == first["id"]

    assert client.get(f"/api/runs/{uuid.uuid4()}").status_code == 404


# ---------------------------------------------------------------------------
# rubric GET/PUT + approve
# ---------------------------------------------------------------------------


def _run_with_draft_rubric(api) -> tuple[str, uuid.UUID]:
    """POST a run and wait for the (fake) draft -> returns (run_id, rubric_id)."""
    data = _post_run(api.client)
    run_id = data["id"]
    _wait_run(api.client, run_id, status="rubric_review")
    with api.session_factory() as s:
        rubric = s.execute(
            select(Rubric).where(Rubric.run_id == uuid.UUID(run_id))
        ).scalar_one()
        return run_id, rubric.id


def test_get_and_update_rubric(api) -> None:
    client = api.client
    run_id, rubric_id = _run_with_draft_rubric(api)

    resp = client.get(f"/api/runs/{run_id}/rubric")
    assert resp.status_code == 200
    assert resp.json()["id"] == str(rubric_id)
    assert resp.json()["status"] == "draft"

    edited = _rubric_content(summary="human-edited rubric")
    resp = client.put(f"/api/runs/{run_id}/rubric", json={"content": edited})
    assert resp.status_code == 200, resp.text
    assert resp.json()["content"]["summary"] == "human-edited rubric"
    assert resp.json()["status"] == "draft"  # still editable until approve

    # canonical dimensions are enforced at the boundary
    bad = _rubric_content()
    bad["dimensions"] = bad["dimensions"][:3]
    resp = client.put(f"/api/runs/{run_id}/rubric", json={"content": bad})
    assert resp.status_code == 422


def test_approve_freezes_rubric_and_starts_lifecycle(api) -> None:
    client = api.client
    run_id, rubric_id = _run_with_draft_rubric(api)

    resp = client.post(f"/api/runs/{run_id}/approve")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "provisioning"
    assert body["rubric"]["status"] == "approved"
    assert body["started_at"] is not None

    # lifecycle background task was registered for this run
    deadline = time.monotonic() + 5.0
    while not api.lifecycle_calls and time.monotonic() < deadline:
        time.sleep(0.05)
    assert api.lifecycle_calls == [uuid.UUID(run_id)]

    # rubric is frozen: further edits + re-approve conflict
    resp = client.put(
        f"/api/runs/{run_id}/rubric", json={"content": _rubric_content()}
    )
    assert resp.status_code == 409
    resp = client.post(f"/api/runs/{run_id}/approve")
    assert resp.status_code == 409

    with api.session_factory() as s:
        rubric = s.get(Rubric, rubric_id)
        assert rubric.status == "approved"
        assert rubric.version == 1


def test_rubric_endpoints_404_and_409(api) -> None:
    client = api.client
    missing = uuid.uuid4()
    assert client.get(f"/api/runs/{missing}/rubric").status_code == 404
    # valid content body so the request reaches the handler -> 404
    assert (
        client.put(
            f"/api/runs/{missing}/rubric", json={"content": _rubric_content()}
        ).status_code
        == 404
    )
    assert client.post(f"/api/runs/{missing}/approve").status_code == 404

    # run exists but has no rubric yet -> 404 (still 'submitted')
    with api.session_factory() as s:
        run = Run(task_prompt="x", model_name="m", config={}, status="submitted")
        s.add(run)
        s.commit()
        bare_id = run.id
    assert client.get(f"/api/runs/{bare_id}/rubric").status_code == 404
    assert client.post(f"/api/runs/{bare_id}/approve").status_code == 404

    # run past rubric_review with a draft rubric row -> 409 on edit/approve
    with api.session_factory() as s:
        run = s.get(Run, bare_id)
        run.status = "running"
        s.add(Rubric(run_id=bare_id, version=1, status="draft", content=_rubric_content()))
        s.commit()
    assert client.put(
        f"/api/runs/{bare_id}/rubric", json={"content": _rubric_content()}
    ).status_code == 409
    assert client.post(f"/api/runs/{bare_id}/approve").status_code == 409
