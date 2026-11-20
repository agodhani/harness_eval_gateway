"""F1/F2 — POST /api/runs, rubric review/approve, run detail.

Hermetic: per-test file-backed sqlite (dependency-overridden ``get_db``),
fake skillpacks dir, and stubbed orchestrator background tasks — the REAL
``runner.start_*_task``/``_track`` wiring runs, but ``draft_rubric`` and
``run_lifecycle`` are replaced with in-process fakes so no gateway/docker
is touched.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from api import modelcheck
from api.deps import get_db
from api.main import app
from api.routers import runs as runs_router
from db.models import Artifact, Base, Event, Rubric, Run, Sandbox, Score
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


@pytest.fixture(autouse=True)
def _modelcheck_offline(monkeypatch):
    """Submit validation never touches a real gateway/ollama in unit tests —
    default the catalog fetch to "unreachable" (the offline-tolerance path);
    tests exercising catalog checks re-patch ``_fetch_catalog`` themselves.
    The ~60s module cache is reset so each test's stub is authoritative."""

    async def _offline(settings, provider):
        return None

    monkeypatch.setattr(modelcheck, "_fetch_catalog", _offline)
    modelcheck.reset_catalog_cache()
    yield
    modelcheck.reset_catalog_cache()


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
            for a in ("miniloop", "claude_code", "codex", "deepseek", "pi", "aider")
        ]
    )
    data = _post_run(api.client, body)
    adapters = [hc["adapter"] for hc in data["config"]["harness_configs"]]
    assert adapters == ["miniloop", "claude_code", "codex", "deepseek", "pi", "aider"]


def test_create_run_prompt_adapter_validates(api) -> None:
    """PR25: the no-sandbox ``prompt`` pseudo-adapter is a valid
    ``harness_configs[].adapter`` — selectable without a registry module."""
    data = _post_run(
        api.client,
        _valid_body(
            harness_configs=[
                {"adapter": "prompt", "system_prompt_extra": "be terse"},
                {"adapter": "prompt"},
                {"adapter": "miniloop"},
            ]
        ),
    )
    hcs = data["config"]["harness_configs"]
    assert [hc["adapter"] for hc in hcs] == ["prompt", "prompt", "miniloop"]
    assert hcs[0]["system_prompt_extra"] == "be terse"


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


def test_create_run_multi_model_matrix(api) -> None:
    """PR14: models[] expands the matrix — config.models persists the
    effective participant set ([model_name] + deduped extras)."""
    body = _valid_body(models=["gpt-4o", "claude-opus-4.1", "gpt-4o", "qwen3:8b"])
    data = _post_run(api.client, body)
    cfg = data["config"]
    # model_name leads; extras deduped against it and each other
    assert cfg["models"] == ["qwen3:8b", "gpt-4o", "claude-opus-4.1"]
    # harness_configs stay unexpanded at submit — expansion happens at
    # provisioning (orchestrator.matrix.expanded_configs).
    assert len(cfg["harness_configs"]) == 2
    assert all("model" not in hc for hc in cfg["harness_configs"])
    # evaluator_model "eval-model" isn't a participant → flag stays False
    assert cfg["same_model_eval"] is False
    assert data["same_model_eval"] is False


def test_create_run_models_default_single_model(api) -> None:
    """Omitted/empty models → single-model run; config.models still records
    the effective set ([model_name]) for downstream consumers."""
    for models in (None, []):
        body = _valid_body()
        if models is not None:
            body["models"] = models
        data = _post_run(api.client, body)
        assert data["config"]["models"] == ["qwen3:8b"]


def test_create_run_same_model_eval_against_any_participant(api) -> None:
    """PR14: the same-model flag fires when the judge equals ANY participant
    model — non-independent judging for that matrix cell."""
    # evaluator == an EXTRA participant model → flagged
    body = _valid_body(models=["gpt-4o"], evaluator_model="gpt-4o")
    data = _post_run(api.client, body)
    assert data["config"]["same_model_eval"] is True
    assert data["same_model_eval"] is True
    # evaluator == primary participant → flagged (pre-PR14 behavior);
    # drop the explicit evaluator_model so it defaults to model_name
    body = _valid_body(models=["gpt-4o"])
    del body["evaluator_model"]
    data = _post_run(api.client, body)
    assert data["config"]["evaluator_model"] == "qwen3:8b"
    assert data["same_model_eval"] is True
    # evaluator distinct from all participants → not flagged
    body = _valid_body(models=["gpt-4o"], evaluator_model="eval-model")
    data = _post_run(api.client, body)
    assert data["same_model_eval"] is False


def test_create_run_matrix_size_cap(api) -> None:
    """PR14: configs × models is capped — each cell is a real sandbox."""
    # 8 configs × (1 + 4 models) = 40 cells > MAX_MATRIX_CELLS (32)
    body = _valid_body(
        harness_configs=[{}] * 8,
        models=["m2", "m3", "m4", "m5"],
    )
    resp = api.client.post("/api/runs", json=body)
    assert resp.status_code == 422
    assert "matrix" in resp.text.lower() or "sandboxes" in resp.text.lower()
    # exactly at the cap → accepted
    body = _valid_body(harness_configs=[{}] * 8, models=["m2", "m3"])
    data = _post_run(api.client, body)  # 8 × 3 = 24 ≤ 32
    assert len(data["config"]["models"]) == 3


def test_create_run_models_field_bounds(api) -> None:
    client = api.client
    # >8 extra models rejected by the field bound
    resp = client.post(
        "/api/runs", json=_valid_body(models=[f"m{i}" for i in range(9)])
    )
    assert resp.status_code == 422
    # entries longer than 255 chars 422 like model_name/evaluator_model —
    # a malformed entry must not silently shrink the participant set
    resp = client.post("/api/runs", json=_valid_body(models=["x" * 300]))
    assert resp.status_code == 422


def test_create_run_per_config_model_pin(api) -> None:
    """PR14: harness_configs[i].model pins that config to one model —
    persisted in run.config, counted in the participant set."""
    body = _valid_body(
        models=["gpt-4o"],
        harness_configs=[
            {"adapter": "miniloop", "model": " claude-opus-4.1 "},
            {"adapter": "miniloop"},
        ],
        evaluator_model="claude-opus-4.1",
    )
    data = _post_run(api.client, body)
    cfg = data["config"]
    # pin survives (whitespace-normalized); the unpinned config stays bare
    assert cfg["harness_configs"][0]["model"] == "claude-opus-4.1"
    assert "model" not in cfg["harness_configs"][1]
    # config.models is the run's model list — pins join the participant
    # set via run_models instead (a pin may sit outside the list)
    assert cfg["models"] == ["qwen3:8b", "gpt-4o"]
    # judge == the pinned participant model → same-model flag fires
    assert cfg["same_model_eval"] is True
    assert data["same_model_eval"] is True
    rows = api.client.get("/api/runs").json()
    row = next(r for r in rows if r["id"] == data["id"])
    assert row["models"] == ["qwen3:8b", "gpt-4o", "claude-opus-4.1"]

    # blank pin normalizes away — the config is unpinned (no model key)
    body = _valid_body(harness_configs=[{"adapter": "miniloop", "model": "  "}])
    data = _post_run(api.client, body)
    assert "model" not in data["config"]["harness_configs"][0]


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


def test_approve_queues_behind_active_run(api, monkeypatch) -> None:
    """PR15 pacing: approvals serialize — a second run parks in `queued` and
    promotes FIFO (by approval) when the active run frees the slot.

    Note on mechanics: starlette's TestClient builds a fresh blocking
    portal per request, so background tasks spawned mid-request are
    cancelled when it completes — a test can't hold a "still running"
    lifecycle across requests. Instead the fake marks done only runs the
    test releases, A's task exits while A stays `provisioning` (promote
    correctly no-ops: slot still held), and A's end is driven by calling
    ``promote_next_queued`` on a throwaway loop. B's own task-finally
    promotes C, so the real chain still runs.
    """
    client = api.client
    calls: list[uuid.UUID] = []
    finishable: set[uuid.UUID] = set()

    async def finishable_lifecycle(run_id, **kwargs):
        calls.append(run_id)
        if run_id in finishable:
            with api.session_factory() as s:
                r = s.get(Run, run_id)
                if r is not None:
                    r.status = "done"
                    s.commit()

    monkeypatch.setattr(lifecycle_mod, "run_lifecycle", finishable_lifecycle)

    run_a, _ = _run_with_draft_rubric(api)
    resp = client.post(f"/api/runs/{run_a}/approve")
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "provisioning"

    # A's lifecycle ran but A still holds the slot (fake left it active)
    deadline = time.monotonic() + 5.0
    while not calls and time.monotonic() < deadline:
        time.sleep(0.05)
    assert calls == [uuid.UUID(run_a)]

    run_b, _ = _run_with_draft_rubric(api)
    run_c, _ = _run_with_draft_rubric(api)
    resp = client.post(f"/api/runs/{run_b}/approve")
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "queued"
    resp = client.post(f"/api/runs/{run_c}/approve")
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "queued"
    # parked: no lifecycle started for B or C while A holds the slot
    assert calls == [uuid.UUID(run_a)]

    # A finishes -> the queue drains FIFO through real promote calls.
    finishable.update({uuid.UUID(run_a), uuid.UUID(run_b), uuid.UUID(run_c)})
    with api.session_factory() as s:
        s.get(Run, uuid.UUID(run_a)).status = "done"
        s.commit()

    async def _drain():
        await runner.promote_next_queued(
            session_factory=api.session_factory,
            settings=api.settings,
            runtime=None,  # unused by the fake
        )
        for _ in range(200):
            if len(calls) >= 3:
                return
            await asyncio.sleep(0.01)

    asyncio.run(_drain())

    # FIFO by approval time: B promotes before C
    assert calls == [uuid.UUID(run_a), uuid.UUID(run_b), uuid.UUID(run_c)]

    for rid in (run_b, run_c):
        _wait_run(client, rid, status="done")
        with api.session_factory() as s:
            stages = [
                e.payload.get("stage")
                for e in s.execute(
                    select(Event).where(Event.run_id == uuid.UUID(rid))
                ).scalars()
                if e.type == "checkpoint"
            ]
        assert "run_queued" in stages
        assert "run_dequeued" in stages


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


def test_approve_terminal_run_reports_real_reason(api) -> None:
    """Approving a run already past the gate names the real condition —
    "already approved or finished" — not the edit-path's "not editable"."""
    run_id, _rubric_id = _run_with_draft_rubric(api)
    with api.session_factory() as s:
        s.get(Run, uuid.UUID(run_id)).status = "done"
        s.commit()

    resp = api.client.post(f"/api/runs/{run_id}/approve")
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert "already approved or finished" in detail
    assert "not editable" not in detail

    # PUT rubric keeps its edit-path message — same guard, other phrasing.
    resp = api.client.put(
        f"/api/runs/{run_id}/rubric", json={"content": _rubric_content()}
    )
    assert resp.status_code == 409
    assert "not editable" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# queue_position — parked runs expose their FIFO position
# ---------------------------------------------------------------------------


def _seed_run(api, **fields) -> uuid.UUID:
    """Insert a bare Run row directly (bypasses submit/rubric machinery)."""
    with api.session_factory() as s:
        run = Run(task_prompt="x", model_name="m", config={}, **fields)
        s.add(run)
        s.commit()
        return run.id


def _stages(api, run_id: uuid.UUID) -> list[str]:
    """Checkpoint stage names emitted for one run."""
    with api.session_factory() as s:
        return [
            e.payload.get("stage")
            for e in s.execute(select(Event).where(Event.run_id == run_id)).scalars()
            if e.type == "checkpoint"
        ]


def test_queue_positions_on_list_and_detail(api) -> None:
    """1-based position among queued runs, ordered by queue-entry
    (started_at) — the same order claim_next_queued promotes. Non-queued
    runs surface null."""
    q1 = _seed_run(api, status="queued")
    q2 = _seed_run(api, status="queued")
    running = _seed_run(api, status="running")
    with api.session_factory() as s:
        s.get(Run, q1).started_at = datetime.now(UTC) - timedelta(minutes=5)
        s.get(Run, q2).started_at = datetime.now(UTC) - timedelta(minutes=1)
        s.commit()

    rows = api.client.get("/api/runs", params={"status": "queued"}).json()
    positions = {uuid.UUID(r["id"]): r["queue_position"] for r in rows}
    assert positions[q1] == 1
    assert positions[q2] == 2

    detail = api.client.get(f"/api/runs/{q2}").json()
    assert detail["queue_position"] == 2
    assert api.client.get(f"/api/runs/{running}").json()["queue_position"] is None
    rows = api.client.get("/api/runs", params={"status": "running"}).json()
    assert all(r["queue_position"] is None for r in rows)


# ---------------------------------------------------------------------------
# submit validation — whitespace + model catalog (api/modelcheck.py)
# ---------------------------------------------------------------------------


def _stub_catalog(monkeypatch, catalog_or_exc) -> None:
    """One catalog for every provider the spec resolves to — an Exception
    value simulates an unreachable gateway instead."""

    async def fake_fetch(settings, provider):
        if isinstance(catalog_or_exc, Exception):
            raise catalog_or_exc
        return catalog_or_exc

    monkeypatch.setattr(modelcheck, "_fetch_catalog", fake_fetch)


def test_create_run_whitespace_only_fields_422(api) -> None:
    """Strip-before-validate: whitespace-only prompt/model 422s instead of
    persisting a spec the rubric drafter can't use."""
    resp = api.client.post("/api/runs", json=_valid_body(task_prompt="  \n\t  "))
    assert resp.status_code == 422
    resp = api.client.post("/api/runs", json=_valid_body(model_name="   "))
    assert resp.status_code == 422


def test_create_run_unknown_models_warn(api, monkeypatch) -> None:
    """Models absent from the provider catalog → advisory warnings on the
    201 response — never a 422 (the name may be pullable/renamed)."""
    _stub_catalog(
        monkeypatch,
        {"qwen3:8b": frozenset({"tools"}), "eval-model": frozenset({"tools"})},
    )
    data = _post_run(api.client, _valid_body(models=["mystery:1"]))
    warnings = data["warnings"]
    assert len(warnings) == 1
    assert "models[] 'mystery:1'" in warnings[0]
    assert "ollama" in warnings[0]


def test_create_run_no_tools_model_422(api, monkeypatch) -> None:
    """Provider reports no ``tools`` + a tool-call harness → hard 422 with
    the provider's capability report — the mid-run ollama failure, caught
    at submit."""
    _stub_catalog(
        monkeypatch,
        {
            "qwen2.5vl:7b": frozenset({"completion", "vision"}),
            "qwen3:8b": frozenset({"completion", "tools", "thinking"}),
        },
    )
    resp = api.client.post(
        "/api/runs",
        json=_valid_body(model_name="qwen2.5vl:7b", evaluator_model="qwen3:8b"),
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert "does not support tools" in detail
    assert "qwen2.5vl:7b" in detail
    assert "vision" in detail


def test_create_run_no_tools_per_config_pin_422(api, monkeypatch) -> None:
    """The same capability check applies to per-config model pins."""
    _stub_catalog(
        monkeypatch,
        {
            "qwen3:8b": frozenset({"tools"}),
            "eval-model": frozenset({"tools"}),
            "v-only": frozenset({"completion"}),
        },
    )
    body = _valid_body(harness_configs=[{"adapter": "miniloop", "model": "v-only"}])
    resp = api.client.post("/api/runs", json=body)
    assert resp.status_code == 422
    assert "harness_configs[0].model 'v-only'" in resp.json()["detail"]


def test_create_run_catalog_offline_tolerated(api, monkeypatch) -> None:
    """Catalog fetch failure skips validation entirely — submit still 201s
    offline (no warnings, no 422)."""
    _stub_catalog(monkeypatch, RuntimeError("gateway down"))
    data = _post_run(api.client)
    assert data["status"] == "submitted"
    assert data["warnings"] == []


# ---------------------------------------------------------------------------
# DELETE /api/runs/{id} — cancel pre-spawn, hard-delete terminal
# ---------------------------------------------------------------------------


def test_delete_submitted_run_cancels(api) -> None:
    """submitted → cancelled: status flip + checkpoint + finished_at; a
    pending rubric-draft task is cancelled too."""
    run_id = _seed_run(api, status="submitted")
    cancelled: list[bool] = []

    class _PendingTask:
        def done(self) -> bool:
            return False

        def cancel(self) -> None:
            cancelled.append(True)

    runner._TASKS[(run_id, "rubric")] = _PendingTask()  # type: ignore[assignment]
    try:
        resp = api.client.delete(f"/api/runs/{run_id}")
        assert resp.status_code == 200
        assert resp.json() == {"status": "cancelled"}
        with api.session_factory() as s:
            run = s.get(Run, run_id)
            assert run.status == "cancelled"
            assert run.finished_at is not None
        assert "cancelled" in _stages(api, run_id)
        assert cancelled == [True]
    finally:
        runner._TASKS.pop((run_id, "rubric"), None)


def test_delete_rubric_review_run_cancels(api) -> None:
    """rubric_review → cancelled; the draft rubric row is left behind
    (hard delete is the terminal-path semantic, not cancel's)."""
    run_id = _seed_run(api, status="rubric_review")
    with api.session_factory() as s:
        s.add(
            Rubric(
                run_id=run_id, version=1, status="draft",
                content=_rubric_content(),
            )
        )
        s.commit()

    resp = api.client.delete(f"/api/runs/{run_id}")
    assert resp.json() == {"status": "cancelled"}
    with api.session_factory() as s:
        assert s.get(Run, run_id).status == "cancelled"
        assert (
            s.execute(select(Rubric).where(Rubric.run_id == run_id)).scalar_one()
            is not None
        )


def test_delete_queued_run_releases_slot(api) -> None:
    """Cancelling a queued run frees its queue position — the next parked
    run promotes through the same claim a lifecycle exit performs."""
    first = _seed_run(api, status="queued")
    nxt = _seed_run(api, status="queued")
    with api.session_factory() as s:
        s.get(Run, first).started_at = datetime.now(UTC) - timedelta(minutes=2)
        s.get(Run, nxt).started_at = datetime.now(UTC) - timedelta(minutes=1)
        s.commit()

    resp = api.client.delete(f"/api/runs/{first}")
    assert resp.json() == {"status": "cancelled"}
    with api.session_factory() as s:
        assert s.get(Run, first).status == "cancelled"
        assert s.get(Run, nxt).status == "provisioning"
    assert "run_dequeued" in _stages(api, nxt)


def test_delete_inflight_run_409(api) -> None:
    """provisioning/running/evaluating → 409: in-flight sandbox teardown is
    out of scope, and the run is left untouched."""
    for active in ("provisioning", "running", "evaluating"):
        run_id = _seed_run(api, status=active)
        resp = api.client.delete(f"/api/runs/{run_id}")
        assert resp.status_code == 409, active
        assert "in-flight" in resp.json()["detail"]
        with api.session_factory() as s:
            assert s.get(Run, run_id).status == active


def test_delete_done_run_hard_deletes(api) -> None:
    """Terminal runs hard-delete: every dependent row (scores, artifacts,
    events, rubrics, sandboxes) plus the on-disk artifact dir."""
    run_id = _seed_run(api, status="done")
    with api.session_factory() as s:
        sb = Sandbox(
            run_id=run_id, harness_config={}, status="done",
            artifact_path="/x/y",
        )
        s.add(sb)
        s.flush()
        s.add(Score(sandbox_id=sb.id, dimension="d", score=1.0))
        s.add(Artifact(sandbox_id=sb.id, kind="workspace", path="/x/y", size_bytes=1))
        s.add(
            Event(
                run_id=run_id, sandbox_id=sb.id, source="orchestrator",
                type="checkpoint", payload={"stage": "done"},
            )
        )
        s.add(Rubric(run_id=run_id, version=1, status="approved", content={}))
        s.commit()

    art_dir = api.settings.ARTIFACT_ROOT / str(run_id)
    (art_dir / "sb").mkdir(parents=True)
    (art_dir / "sb" / "hello.txt").write_text("hi")

    resp = api.client.delete(f"/api/runs/{run_id}")
    assert resp.status_code == 200
    assert resp.json() == {"status": "deleted"}

    with api.session_factory() as s:
        assert s.get(Run, run_id) is None
        assert s.execute(select(Sandbox)).scalars().all() == []
        assert s.execute(select(Score)).scalars().all() == []
        assert s.execute(select(Artifact)).scalars().all() == []
        assert s.execute(select(Rubric)).scalars().all() == []
        assert s.execute(select(Event)).scalars().all() == []
    assert not art_dir.exists()
    assert api.client.get(f"/api/runs/{run_id}").status_code == 404


def test_delete_cancelled_run_hard_deletes(api) -> None:
    """cancelled IS terminal — a second DELETE removes the row."""
    run_id = _seed_run(api, status="cancelled")
    resp = api.client.delete(f"/api/runs/{run_id}")
    assert resp.json() == {"status": "deleted"}
    with api.session_factory() as s:
        assert s.get(Run, run_id) is None


def test_delete_unknown_run_404(api) -> None:
    assert api.client.delete(f"/api/runs/{uuid.uuid4()}").status_code == 404
