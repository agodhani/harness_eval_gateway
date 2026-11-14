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

from api.deps import get_db
from api.main import app
from api.routers import runs as runs_router
from db.models import Base, Event, Rubric, Run, Sandbox
from db.session import make_engine, make_session_factory
from orchestrator import continuation as continuation_mod
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


# ---------------------------------------------------------------------------
# PR16 — POST /api/runs/{id}/continuations ("add next prompt")
# ---------------------------------------------------------------------------


def _finished_run_with_sandbox(
    api,
    *,
    run_status: str = "done",
    sb_status: str = "done",
    continuations: list | None = None,
) -> tuple[uuid.UUID, uuid.UUID]:
    """Run + one sandbox row in arbitrary DB states for continuation tests."""
    with api.session_factory() as s:
        run = Run(
            status=run_status,
            task_prompt="create hello.txt",
            model_name="qwen3:8b",
            config={
                "harness_configs": [{"adapter": "miniloop"}],
                "budgets": {
                    "max_iters": 10,
                    "token_budget": 50_000,
                    "wall_clock_seconds": 600,
                },
            },
            started_at=datetime.now(UTC) - timedelta(hours=1),
            finished_at=datetime.now(UTC),
        )
        s.add(run)
        s.flush()
        sb = Sandbox(
            run_id=run.id,
            harness_config={
                "adapter": "miniloop",
                "skill_packs": [],
                **({"continuations": continuations} if continuations else {}),
            },
            status=sb_status,
            started_at=datetime.now(UTC) - timedelta(hours=1),
            finished_at=datetime.now(UTC),
            terminal_reason="agent_done",
            artifact_path="/tmp/artifacts/x",
        )
        s.add(sb)
        s.commit()
        return run.id, sb.id


def test_continuation_accepts_and_registers_task(api, monkeypatch) -> None:
    """Happy path: finished run+sandbox -> 201, run re-holds the slot, the
    sandbox records the phase and a continuation task starts."""
    calls: list[tuple[uuid.UUID, uuid.UUID]] = []

    async def fake_continuation(run_id, sandbox_id, **_kwargs):
        calls.append((run_id, sandbox_id))

    monkeypatch.setattr(continuation_mod, "run_continuation", fake_continuation)

    run_id, sb_id = _finished_run_with_sandbox(api)
    resp = api.client.post(
        f"/api/runs/{run_id}/continuations",
        json={"sandbox_id": str(sb_id), "prompt": "now add tests"},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "provisioning"
    assert body["finished_at"] is None

    with api.session_factory() as s:
        run = s.get(Run, run_id)
        assert run.status == "provisioning"
        sb = s.get(Sandbox, sb_id)
        assert sb.status == "provisioning"
        assert sb.finished_at is None
        assert sb.terminal_reason is None
        conts = sb.harness_config["continuations"]
        assert len(conts) == 1
        assert conts[0]["phase"] == 2
        assert conts[0]["prompt"] == "now add tests"
        stages = [
            e.payload.get("stage")
            for e in s.execute(select(Event).where(Event.run_id == run_id))
            .scalars()
            if e.type == "checkpoint"
        ]
    assert "continuation_started" in stages

    deadline = time.monotonic() + 5.0
    while not calls and time.monotonic() < deadline:
        time.sleep(0.05)
    assert calls == [(run_id, sb_id)]


def test_continuation_404s(api, monkeypatch) -> None:
    calls: list = []

    async def fake_continuation(*a, **k):
        calls.append(a)

    monkeypatch.setattr(continuation_mod, "run_continuation", fake_continuation)
    client = api.client

    assert client.post(
        f"/api/runs/{uuid.uuid4()}/continuations",
        json={"sandbox_id": str(uuid.uuid4()), "prompt": "x"},
    ).status_code == 404

    run_id, _sb_id = _finished_run_with_sandbox(api)
    # sandbox id that doesn't exist / belongs to another run
    assert client.post(
        f"/api/runs/{run_id}/continuations",
        json={"sandbox_id": str(uuid.uuid4()), "prompt": "x"},
    ).status_code == 404
    _other_run, other_sb = _finished_run_with_sandbox(api)
    assert client.post(
        f"/api/runs/{run_id}/continuations",
        json={"sandbox_id": str(other_sb), "prompt": "x"},
    ).status_code == 404
    assert calls == []


def test_continuation_409s(api, monkeypatch) -> None:
    calls: list = []

    async def fake_continuation(*a, **k):
        calls.append(a)

    monkeypatch.setattr(continuation_mod, "run_continuation", fake_continuation)
    client = api.client

    # sandbox not finished
    run_id, sb_id = _finished_run_with_sandbox(api, sb_status="running")
    resp = client.post(
        f"/api/runs/{run_id}/continuations",
        json={"sandbox_id": str(sb_id), "prompt": "x"},
    )
    assert resp.status_code == 409

    # run not finished and its active work is NOT continuation-driven
    run_id, sb_id = _finished_run_with_sandbox(
        api, run_status="running", sb_status="running"
    )
    resp = client.post(
        f"/api/runs/{run_id}/continuations",
        json={"sandbox_id": str(sb_id), "prompt": "x"},
    )
    assert resp.status_code == 409

    # run in rubric_review — human gate not passed
    run_id, sb_id = _finished_run_with_sandbox(api, run_status="rubric_review")
    resp = client.post(
        f"/api/runs/{run_id}/continuations",
        json={"sandbox_id": str(sb_id), "prompt": "x"},
    )
    assert resp.status_code == 409

    # queued run is parked, not finished
    run_id, sb_id = _finished_run_with_sandbox(api, run_status="queued")
    resp = client.post(
        f"/api/runs/{run_id}/continuations",
        json={"sandbox_id": str(sb_id), "prompt": "x"},
    )
    assert resp.status_code == 409

    # evaluating with NO in-flight continuation sandboxes — normal lifecycle
    # tail must not admit follow-ups (empty active set ≠ continuation-driven)
    run_id, sb_id = _finished_run_with_sandbox(api, run_status="evaluating")
    resp = client.post(
        f"/api/runs/{run_id}/continuations",
        json={"sandbox_id": str(sb_id), "prompt": "x"},
    )
    assert resp.status_code == 409

    # another run holds the execution slot -> 409 (continuations don't queue)
    run_id, sb_id = _finished_run_with_sandbox(api)
    with api.session_factory() as s:
        s.add(Run(status="provisioning", task_prompt="busy", model_name="m", config={}))
        s.commit()
    resp = client.post(
        f"/api/runs/{run_id}/continuations",
        json={"sandbox_id": str(sb_id), "prompt": "x"},
    )
    assert resp.status_code == 409
    assert calls == []


def test_continuation_parallel_sibling_admission(api, monkeypatch) -> None:
    """While THIS run's active work is itself continuations, a follow-up for
    another finished sibling joins in parallel (run already holds the slot)."""
    calls: list = []

    async def fake_continuation(*a, **k):
        calls.append(a)

    monkeypatch.setattr(continuation_mod, "run_continuation", fake_continuation)
    client = api.client

    with api.session_factory() as s:
        run = Run(
            status="running",
            task_prompt="create hello.txt",
            model_name="qwen3:8b",
            config={},
            started_at=datetime.now(UTC) - timedelta(hours=1),
        )
        s.add(run)
        s.flush()
        active = Sandbox(
            run_id=run.id,
            harness_config={
                "adapter": "miniloop",
                "continuations": [
                    {"phase": 2, "prompt": "first follow-up", "started_at": "t"}
                ],
            },
            status="running",
            started_at=datetime.now(UTC) - timedelta(minutes=30),
        )
        done = Sandbox(
            run_id=run.id,
            harness_config={"adapter": "miniloop"},
            status="done",
            started_at=datetime.now(UTC) - timedelta(hours=1),
            finished_at=datetime.now(UTC),
            terminal_reason="agent_done",
            artifact_path="/tmp/artifacts/x",
        )
        s.add_all([active, done])
        s.commit()
        run_id, done_sb = run.id, done.id

    resp = client.post(
        f"/api/runs/{run_id}/continuations",
        json={"sandbox_id": str(done_sb), "prompt": "second follow-up"},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["status"] == "running"  # slot already held — stays
    with api.session_factory() as s:
        sb = s.get(Sandbox, done_sb)
        assert sb.status == "provisioning"
        assert sb.harness_config["continuations"][-1]["phase"] == 2


def test_continuation_validation(api) -> None:
    client = api.client
    run_id, sb_id = _finished_run_with_sandbox(api)
    assert client.post(
        f"/api/runs/{run_id}/continuations",
        json={"sandbox_id": str(sb_id), "prompt": ""},
    ).status_code == 422
    assert client.post(
        f"/api/runs/{run_id}/continuations",
        json={"prompt": "x"},
    ).status_code == 422
