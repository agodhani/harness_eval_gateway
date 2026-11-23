"""PR26 — benchmark suites: vendored store, browse endpoints, fan-out runs,
suite-run aggregation.

Hermetic: per-test file-backed sqlite (dependency-overridden ``get_db``),
tmp ``benchmark_suites`` + ``task_presets`` dirs swapped in via
monkeypatched ``get_settings`` on the suites + runs routers, and stubbed
modelcheck/orchestrator entry points so POST /api/suites/{slug}/run
exercises real fan-out + preset resolution without touching gateway/docker.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from api import suites as suite_store
from api.deps import get_db
from api.main import app
from api.routers import runs as runs_router
from api.routers import suites as suites_router
from db.models import Base, CheckerResult, Event, Run, Sandbox, Score, SuiteRun
from db.session import make_engine, make_session_factory
from orchestrator import runner

REPO_ROOT = Path(__file__).resolve().parents[3]
VENDORED_SUITES_DIR = REPO_ROOT / "benchmark_suites"

ALPHA_MD = """\
---
slug: alpha-preset
title: Alpha preset
description: the alpha task
category: testing
---

Build the alpha thing.
"""

DEMO_YAML = """\
id: demo-suite
version: 2
title: Demo suite
description: preset + inline tasks
tasks:
  - id: alpha
    preset_ref: alpha-preset
    check:
      file_exists: index.html
      grep:
        path: index.html
        pattern: '<html'
  - id: beta-inline
    prompt: "Say hi."
    check:
      output_grep: "hi"
"""

PINNED_YAML = """\
id: pinned-suite
version: 1
title: Pinned
tasks:
  - id: any
    prompt: "runs everywhere"
  - id: codex-only
    prompt: "codex cells only"
    adapter: codex
"""

PROMPT_YAML = """\
id: prompt-suite
version: 1
title: Prompt
tasks:
  - id: prompt-task
    prompt: "prompt cell only"
    adapter: prompt
"""

MINI_YAML = """\
id: mini-suite
version: 1
title: Mini
tasks:
  - prompt: "ping"
"""

BROKEN_YAML = """\
id: [not, a, string
"""


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def session_factory(tmp_path: Path):
    engine = make_engine(f"sqlite:///{tmp_path}/suites.db")
    Base.metadata.create_all(engine)
    sf = make_session_factory(engine)
    yield sf
    engine.dispose()


@pytest.fixture()
def presets_dir(tmp_path: Path) -> Path:
    root = tmp_path / "task_presets"
    root.mkdir()
    (root / "alpha-preset.md").write_text(ALPHA_MD)
    return root


@pytest.fixture()
def suites_dir(tmp_path: Path) -> Path:
    root = tmp_path / "benchmark_suites"
    (root / "demo-suite").mkdir(parents=True)
    (root / "demo-suite" / "suite.yaml").write_text(DEMO_YAML)
    (root / "pinned-suite").mkdir()
    (root / "pinned-suite" / "suite.yaml").write_text(PINNED_YAML)
    (root / "prompt-suite").mkdir()
    (root / "prompt-suite" / "suite.yaml").write_text(PROMPT_YAML)
    (root / "mini-suite").mkdir()
    (root / "mini-suite" / "suite.yaml").write_text(MINI_YAML)
    # A broken suite must not poison the list.
    (root / "broken").mkdir()
    (root / "broken" / "suite.yaml").write_text(BROKEN_YAML)
    return root


@pytest.fixture()
def api(session_factory, presets_dir: Path, suites_dir: Path, tmp_path: Path, monkeypatch):
    ns = SimpleNamespace(
        DATABASE_URL="sqlite:///:memory:",
        SKILLPACKS_DIR=tmp_path / "skillpacks",
        IMPORTED_SKILLPACKS_DIR=tmp_path / "imported_skillpacks",
        TASK_PRESETS_DIR=presets_dir,
        BENCHMARK_SUITES_DIR=suites_dir,
        ARTIFACT_ROOT=tmp_path / "artifacts",
    )
    monkeypatch.setattr(suites_router, "get_settings", lambda: ns)
    monkeypatch.setattr(runs_router, "get_settings", lambda: ns)

    async def _no_catalog(body, settings=None):
        return []

    monkeypatch.setattr(runs_router.modelcheck, "validate_run_models", _no_catalog)
    monkeypatch.setattr(runner, "start_rubric_task", lambda *a, **k: None)

    def override_get_db():
        session = session_factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = override_get_db
    yield SimpleNamespace(
        client=TestClient(app),
        session_factory=session_factory,
        presets_dir=presets_dir,
        suites_dir=suites_dir,
    )
    app.dependency_overrides.pop(get_db, None)


def _run_body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model_name": "m1",
        "harness_configs": [{"adapter": "miniloop"}],
    }
    body.update(overrides)
    return body


# ---------------------------------------------------------------------------
# store
# ---------------------------------------------------------------------------


def test_store_list_and_get(suites_dir: Path) -> None:
    suites = suite_store.list_suites(suites_dir)
    assert [s.slug for s in suites] == [
        "demo-suite",
        "mini-suite",
        "pinned-suite",
        "prompt-suite",
    ]
    demo = suite_store.get_suite(suites_dir, "demo-suite")
    assert demo is not None and demo.version == 2
    assert demo.title == "Demo suite"
    assert [t.task_id(i) for i, t in enumerate(demo.tasks)] == [
        "alpha",
        "beta-inline",
    ]
    alpha = demo.tasks[0]
    assert alpha.preset_ref == "alpha-preset"
    assert alpha.checks() == [
        {"check": "file_exists", "path": "index.html"},
        {"check": "grep", "path": "index.html", "pattern": "<html"},
    ]
    assert demo.tasks[1].prompt == "Say hi."
    pinned = suite_store.get_suite(suites_dir, "pinned-suite")
    assert pinned.tasks[1].adapter == "codex"


def test_store_traversal_and_missing(suites_dir: Path) -> None:
    assert suite_store.get_suite(suites_dir, "../demo-suite") is None
    assert suite_store.get_suite(suites_dir, "..") is None
    assert suite_store.get_suite(suites_dir, "nonexistent") is None
    assert suite_store.get_suite(suites_dir, "broken") is None
    with pytest.raises(suite_store.SuiteError) as exc:
        suite_store.require_suite(suites_dir, "../x")
    assert exc.value.kind == "invalid_slug"
    with pytest.raises(suite_store.SuiteError) as exc:
        suite_store.require_suite(suites_dir, "missing")
    assert exc.value.kind == "not_found"


def test_store_task_validation(tmp_path: Path) -> None:
    """preset_ref XOR prompt enforced; bad check shapes rejected."""
    d = tmp_path / "bad-xor"
    d.mkdir()
    (d / "suite.yaml").write_text(
        "id: bad-xor\nversion: 1\ntitle: x\ntasks:\n"
        "  - preset_ref: alpha-preset\n    prompt: 'also inline'\n"
    )
    with pytest.raises(suite_store.SuiteError) as exc:
        suite_store.load_suite(d, "bad-xor")
    assert exc.value.kind == "invalid_body"

    d2 = tmp_path / "bad-check"
    d2.mkdir()
    (d2 / "suite.yaml").write_text(
        "id: bad-check\nversion: 1\ntitle: x\ntasks:\n"
        "  - prompt: x\n    check:\n        bogus_type: yes\n"
    )
    with pytest.raises(suite_store.SuiteError) as exc:
        suite_store.load_suite(d2, "bad-check")
    assert exc.value.kind == "invalid_body"


def test_store_output_checkers_parse(tmp_path: Path) -> None:
    """exact/contains/json_schema/numeric_tol normalize into flat specs;
    json_schema rejects keys outside the supported subset."""
    d = tmp_path / "rich-checks"
    d.mkdir()
    (d / "suite.yaml").write_text(
        "id: rich-checks\nversion: 1\ntitle: x\ntasks:\n"
        "  - prompt: say things\n"
        "    check:\n"
        "      exact: 'TOTAL: 42'\n"
        "      contains: TOTAL\n"
        "      numeric_tol: {value: 42, tolerance: 0.5}\n"
        "      json_schema:\n"
        "        type: object\n"
        "        required: [answer]\n"
        "        properties:\n"
        "          answer: {type: integer}\n"
        "        additionalProperties: false\n"
    )
    suite = suite_store.load_suite(d, "rich-checks")
    checks = suite.tasks[0].checks()
    assert checks == [
        {"check": "exact", "equals": "TOTAL: 42"},
        {"check": "contains", "substring": "TOTAL"},
        {"check": "json_schema", "schema": {
            "type": "object",
            "required": ["answer"],
            "properties": {"answer": {"type": "integer"}},
            "additionalProperties": False,
        }},
        {"check": "numeric_tol", "value": 42, "tolerance": 0.5},
    ]

    d2 = tmp_path / "bad-schema"
    d2.mkdir()
    (d2 / "suite.yaml").write_text(
        "id: bad-schema\nversion: 1\ntitle: x\ntasks:\n"
        "  - prompt: x\n"
        "    check:\n"
        "      json_schema: {$ref: '#/definitions/thing'}\n"
    )
    with pytest.raises(suite_store.SuiteError) as exc:
        suite_store.load_suite(d2, "bad-schema")
    assert exc.value.kind == "invalid_body"

    d3 = tmp_path / "bad-tol"
    d3.mkdir()
    (d3 / "suite.yaml").write_text(
        "id: bad-tol\nversion: 1\ntitle: x\ntasks:\n"
        "  - prompt: x\n"
        "    check:\n"
        "      numeric_tol: {value: 5, tolerance: -0.1}\n"
    )
    with pytest.raises(suite_store.SuiteError) as exc:
        suite_store.load_suite(d3, "bad-tol")
    assert exc.value.kind == "invalid_body"


def test_committed_suites_parse() -> None:
    """Contract: every vendored suite under benchmark_suites/ parses and
    demo-covers a container cell AND a prompt cell."""
    suites = suite_store.list_suites(VENDORED_SUITES_DIR)
    assert suites, "no vendored suites found"
    starter = suite_store.get_suite(VENDORED_SUITES_DIR, "starter-regression")
    assert starter is not None
    adapters = {t.adapter for t in starter.tasks}
    assert "miniloop" in adapters  # container cell
    assert "prompt" in adapters  # prompt cell
    # Every checker type is exercised somewhere in the example suite.
    kinds = {c["check"] for t in starter.tasks for c in t.checks()}
    assert kinds == set(suite_store.CHECK_TYPES)


# ---------------------------------------------------------------------------
# GET endpoints
# ---------------------------------------------------------------------------


def test_list_suites_endpoint(api) -> None:
    res = api.client.get("/api/suites")
    assert res.status_code == 200
    rows = {s["slug"]: s for s in res.json()}
    demo = rows["demo-suite"]
    assert demo["version"] == 2
    assert demo["task_count"] == 2
    assert demo["checks_count"] == 3
    assert demo["suite_runs_count"] == 0
    assert "broken" not in rows


def test_get_suite_detail(api) -> None:
    res = api.client.get("/api/suites/demo-suite")
    assert res.status_code == 200
    data = res.json()
    assert [t["id"] for t in data["tasks"]] == ["alpha", "beta-inline"]
    assert data["tasks"][0]["checks"] == [
        {"check": "file_exists", "spec": {"path": "index.html"}},
        {"check": "grep", "spec": {"path": "index.html", "pattern": "<html"}},
    ]
    assert data["tasks"][1]["prompt"] == "Say hi."
    assert data["recent_runs"] == []


def test_get_suite_404_and_traversal(api) -> None:
    assert api.client.get("/api/suites/nope").status_code == 404
    res = api.client.get("/api/suites/..%2Fdemo-suite")
    # Traversal-looking slugs fail the pattern before the FS is touched —
    # a 422, never a resolved path (or the router 404s the weird path).
    assert res.status_code in (404, 422)


# ---------------------------------------------------------------------------
# POST /api/suites/{slug}/run — the fan-out
# ---------------------------------------------------------------------------


def test_suite_run_fans_out_runs_with_provenance(api) -> None:
    res = api.client.post("/api/suites/demo-suite/run", json=_run_body())
    assert res.status_code == 201, res.text
    data = res.json()
    assert data["suite_slug"] == "demo-suite"
    assert data["suite_version"] == 2
    assert len(data["runs"]) == 2
    assert [r["suite_index"] for r in data["runs"]] == [0, 1]
    assert data["runs"][0]["task_id"] == "alpha"
    assert data["runs"][0]["preset_ref"] == "alpha-preset"

    with api.session_factory() as s:
        batch = s.get(SuiteRun, uuid.UUID(data["id"]))
        assert batch is not None and batch.suite_slug == "demo-suite"
        children = list(
            s.execute(
                select(Run)
                .where(Run.suite_run_id == batch.id)
                .order_by(Run.suite_index)
            ).scalars()
        )
        assert len(children) == 2
        for i, run in enumerate(children):
            assert run.suite_ref == "demo-suite"
            assert run.suite_index == i
            assert run.suite_run_id == batch.id
            assert run.status == "submitted"
        # preset_ref task resolved like a normal submit: preset body becomes
        # task_prompt, revision snapshot lands.
        assert children[0].preset_ref == "alpha-preset"
        assert children[0].preset_revision == 1
        assert children[0].task_prompt == "Build the alpha thing."
        # Inline prompts land verbatim.
        assert children[1].task_prompt == "Say hi."
        assert children[1].preset_ref is None


def test_suite_run_adapter_pin_filters_configs(api) -> None:
    """A task pinned to adapter X only fans out configs selecting X —
    ``codex`` is a real adapter so the filter is exercised without stubs."""
    res = api.client.post(
        "/api/suites/pinned-suite/run",
        json=_run_body(
            harness_configs=[
                {"adapter": "miniloop", "name": "container"},
                {"adapter": "codex", "name": "codexcell"},
            ]
        ),
    )
    assert res.status_code == 201, res.text
    with api.session_factory() as s:
        children = list(
            s.execute(
                select(Run)
                .where(Run.suite_ref == "pinned-suite")
                .order_by(Run.suite_index)
            ).scalars()
        )
        # Task 0 (unpinned): both configs. Task 1 (pinned codex): only the
        # codex config.
        assert len(children[0].config["harness_configs"]) == 2
        pinned = children[1]
        assert [hc["adapter"] for hc in pinned.config["harness_configs"]] == [
            "codex"
        ]
        assert pinned.config["suite_task_id"] == "codex-only"


def test_suite_run_adapter_pin_no_match_422(api) -> None:
    """Pinned adapter with no matching config aborts the batch (nothing
    commits — atomic fan-out)."""
    res = api.client.post("/api/suites/pinned-suite/run", json=_run_body())
    assert res.status_code == 422
    assert "codex" in res.json()["detail"]
    # The PR25 ``prompt`` pin behaves identically until that adapter lands.
    res2 = api.client.post("/api/suites/prompt-suite/run", json=_run_body())
    assert res2.status_code == 422
    assert "prompt" in res2.json()["detail"]
    with api.session_factory() as s:
        assert s.execute(select(Run).where(Run.suite_ref == "pinned-suite")).all() == []
        assert s.execute(select(Run).where(Run.suite_ref == "prompt-suite")).all() == []
        assert s.execute(select(SuiteRun)).all() == []


def test_suite_run_unknown_preset_422_and_atomic(api) -> None:
    """A suite task pointing at a deleted preset 422s the whole batch."""
    yaml_bad = """\
id: bad-ref
version: 1
title: bad ref
tasks:
  - prompt: "ok"
  - preset_ref: does-not-exist
"""
    d = api.suites_dir / "bad-ref"
    d.mkdir()
    (d / "suite.yaml").write_text(yaml_bad)
    res = api.client.post("/api/suites/bad-ref/run", json=_run_body())
    assert res.status_code == 422
    with api.session_factory() as s:
        assert s.execute(select(Run).where(Run.suite_ref == "bad-ref")).all() == []
        assert s.execute(select(SuiteRun)).all() == []


def test_suite_run_unknown_suite_404(api) -> None:
    res = api.client.post("/api/suites/nope/run", json=_run_body())
    assert res.status_code == 404


def test_suite_run_rubric_tasks_started(api, monkeypatch) -> None:
    started: list[str] = []
    monkeypatch.setattr(runner, "start_rubric_task", lambda rid: started.append(str(rid)))
    res = api.client.post("/api/suites/mini-suite/run", json=_run_body())
    assert res.status_code == 201
    assert started == [res.json()["runs"][0]["run_id"]]


# ---------------------------------------------------------------------------
# run provenance + ?suite= filter
# ---------------------------------------------------------------------------


def test_run_rows_carry_suite_provenance(api) -> None:
    res = api.client.post("/api/suites/mini-suite/run", json=_run_body())
    run_id = res.json()["runs"][0]["run_id"]
    detail = api.client.get(f"/api/runs/{run_id}")
    assert detail.status_code == 200
    body = detail.json()
    assert body["suite_ref"] == "mini-suite"
    assert body["suite_index"] == 0
    assert body["suite_run_id"] == res.json()["id"]

    # Non-suite runs stay null.
    res2 = api.client.post(
        "/api/runs",
        json={
            "task_prompt": "freeform",
            "model_name": "m1",
            "harness_configs": [{"adapter": "miniloop"}],
        },
    )
    assert res2.status_code == 201
    assert res2.json()["suite_ref"] is None
    assert res2.json()["suite_index"] is None


def test_runs_list_suite_filter(api) -> None:
    api.client.post("/api/suites/mini-suite/run", json=_run_body())
    api.client.post(
        "/api/runs",
        json={
            "task_prompt": "freeform",
            "model_name": "m1",
            "harness_configs": [{"adapter": "miniloop"}],
        },
    )
    filtered = api.client.get("/api/runs?suite=mini-suite")
    assert filtered.status_code == 200
    rows = filtered.json()
    assert len(rows) == 1
    assert rows[0]["suite_ref"] == "mini-suite"
    assert api.client.get("/api/runs?suite=nothing").json() == []


# ---------------------------------------------------------------------------
# GET /api/suite-runs/{id} — task × cell aggregation
# ---------------------------------------------------------------------------


def _seed_batch(api) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """One suite_run + one run + two sandboxes (a pass + a fail)."""
    with api.session_factory() as s:
        batch = SuiteRun(suite_slug="demo-suite", suite_version=2)
        s.add(batch)
        s.flush()
        run = Run(
            task_prompt="Build the alpha thing.",
            model_name="m1",
            config={
                "harness_configs": [{"adapter": "miniloop"}],
                "models": ["m1"],
                "suite_task_id": "alpha",
            },
            status="done",
            suite_ref="demo-suite",
            suite_index=0,
            suite_run_id=batch.id,
        )
        s.add(run)
        s.flush()
        sb_pass = Sandbox(
            run_id=run.id,
            name="cfg-a",
            harness_config={"adapter": "miniloop", "name": "cfg-a", "model": "m1"},
            status="done",
            terminal_reason="agent_done",
        )
        sb_fail = Sandbox(
            run_id=run.id,
            name="cfg-b",
            harness_config={"adapter": "miniloop", "name": "cfg-b", "model": "m1"},
            status="done",
            terminal_reason="agent_done",
        )
        s.add_all([sb_pass, sb_fail])
        s.flush()
        s.add(Score(sandbox_id=sb_pass.id, dimension="overall", score=9.0))
        s.add_all(
            [
                CheckerResult(
                    run_id=run.id,
                    sandbox_id=sb_pass.id,
                    check_type="file_exists",
                    spec={"path": "index.html"},
                    status="pass",
                    passed=True,
                    detail="file exists",
                ),
                CheckerResult(
                    run_id=run.id,
                    sandbox_id=sb_pass.id,
                    check_type="grep",
                    spec={"path": "index.html", "pattern": "<html"},
                    status="pass",
                    passed=True,
                    detail="matched",
                ),
                CheckerResult(
                    run_id=run.id,
                    sandbox_id=sb_fail.id,
                    check_type="file_exists",
                    spec={"path": "index.html"},
                    status="fail",
                    passed=False,
                    detail="not found",
                ),
                CheckerResult(
                    run_id=run.id,
                    sandbox_id=sb_fail.id,
                    check_type="py_test",
                    spec={"path": "tests"},
                    status="skipped",
                    passed=False,
                    detail="no exec image",
                ),
            ]
        )
        s.commit()
        return batch.id, run.id, sb_pass.id


def test_suite_run_aggregation(api) -> None:
    batch_id, run_id, _sb_pass = _seed_batch(api)
    res = api.client.get(f"/api/suite-runs/{batch_id}")
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["suite_slug"] == "demo-suite"
    assert data["suite_version"] == 2
    assert len(data["runs"]) == 1
    task = data["runs"][0]
    assert task["run_id"] == str(run_id)
    assert task["suite_index"] == 0
    assert task["task_id"] == "alpha"
    cells = {c["name"]: c for c in task["cells"]}
    assert cells["cfg-a"]["judge_overall"] == 9.0
    assert cells["cfg-a"]["checks"]["passed"] == 2
    assert cells["cfg-a"]["checks"]["pass_rate"] == 1.0
    assert cells["cfg-b"]["checks"]["failed"] == 1
    assert cells["cfg-b"]["checks"]["skipped"] == 1
    # pass_rate excludes skipped — 0/1 measured.
    assert cells["cfg-b"]["checks"]["pass_rate"] == 0.0
    # Cell labels carry model for the column key.
    assert cells["cfg-a"]["label"].endswith("m1")


def test_suite_run_aggregation_404(api) -> None:
    res = api.client.get(f"/api/suite-runs/{uuid.uuid4()}")
    assert res.status_code == 404


def test_results_endpoint_surfaces_checker_results(api) -> None:
    """The per-run results API carries checker outcomes beside scores."""
    _batch_id, run_id, _sb_pass = _seed_batch(api)
    res = api.client.get(f"/api/runs/{run_id}/results")
    assert res.status_code == 200, res.text
    sandboxes = {s["name"]: s for s in res.json()["sandboxes"]}
    checks = sandboxes["cfg-a"]["checker_results"]
    assert len(checks) == 2
    by_type = {c["check"]: c for c in checks}
    assert by_type["file_exists"]["status"] == "pass"
    assert by_type["file_exists"]["passed"] is True
    assert by_type["grep"]["detail"] == "matched"
    fail_checks = sandboxes["cfg-b"]["checker_results"]
    assert {c["status"] for c in fail_checks} == {"fail", "skipped"}

    # Flat `suite` summary — counts + pass rate per sandbox (skipped not
    # counted against measured).
    a_suite = sandboxes["cfg-a"]["suite"]
    assert a_suite["total"] == 2
    assert a_suite["passed"] == 2
    assert a_suite["pass_rate"] == 1.0
    b_suite = sandboxes["cfg-b"]["suite"]
    assert b_suite["skipped"] == 1
    assert b_suite["pass_rate"] == 0.0


def test_results_suite_block_without_judge(api) -> None:
    """The card's verify: per-case pass/fail + mean in results WITHOUT
    judge involvement — checker rows exist with zero Score rows."""
    with api.session_factory() as s:
        batch = SuiteRun(suite_slug="demo-suite", suite_version=2)
        s.add(batch)
        s.flush()
        run = Run(
            task_prompt="p",
            model_name="m1",
            config={"suite_task_id": "alpha"},
            status="done",
            suite_ref="demo-suite",
            suite_index=0,
            suite_run_id=batch.id,
        )
        s.add(run)
        s.flush()
        sb = Sandbox(
            run_id=run.id,
            name="cell",
            harness_config={"adapter": "miniloop"},
            status="done",
            terminal_reason="agent_done",
        )
        s.add(sb)
        s.flush()
        # No Score rows at all — judge never ran / failed entirely.
        for i, (ctype, st) in enumerate(
            [("exact", "pass"), ("contains", "pass"), ("output_grep", "fail")]
        ):
            s.add(
                CheckerResult(
                    run_id=run.id,
                    sandbox_id=sb.id,
                    check_type=ctype,
                    spec={"i": i},
                    status=st,
                    passed=st == "pass",
                    detail=f"{ctype} {st}",
                )
            )
        s.commit()
        run_id = run.id

    res = api.client.get(f"/api/runs/{run_id}/results")
    assert res.status_code == 200
    sb = res.json()["sandboxes"][0]
    assert sb["overall"] is None
    assert sb["dimensions"] == {}
    suite = sb["suite"]
    assert suite["total"] == 3
    assert suite["passed"] == 2 and suite["failed"] == 1
    assert suite["pass_rate"] == pytest.approx(2 / 3)
    assert suite["suite_pass"] is None  # no Score row was written
    assert {c["status"] for c in sb["checker_results"]} == {"pass", "fail"}


def test_suite_pass_row_does_not_mask_eval_failure(api) -> None:
    """A suite_pass score is a checker measurement — a sandbox whose ONLY
    score is suite_pass and whose judge failed still shows eval_error."""
    with api.session_factory() as s:
        run = Run(task_prompt="p", model_name="m1", config={}, status="done")
        s.add(run)
        s.flush()
        sb = Sandbox(
            run_id=run.id,
            name="cell",
            harness_config={},
            status="done",
            terminal_reason="agent_done",
        )
        s.add(sb)
        s.flush()
        s.add(
            Score(
                sandbox_id=sb.id,
                dimension="suite_pass",
                score=10.0,
                rationale="3/3 deterministic suite checks passed",
            )
        )
        s.add(
            Event(
                run_id=run.id,
                sandbox_id=sb.id,
                source="evaluator",
                type="error",
                payload={"phase": "eval_failed", "attempts": 2},
            )
        )
        s.commit()
        run_id = run.id

    res = api.client.get(f"/api/runs/{run_id}/results")
    sb_out = res.json()["sandboxes"][0]
    assert sb_out["eval_error"] is not None
    assert "eval_failed" in sb_out["eval_error"]
    # suite_pass still surfaces as a dimension score.
    assert sb_out["dimensions"]["suite_pass"]["score"] == 10.0


def test_suite_pass_row_mirrors_flat_summary(api) -> None:
    """When lifecycle persisted suite_pass, the flat summary echoes it."""
    _batch_id, run_id, sb_pass = _seed_batch(api)
    with api.session_factory() as s:
        s.add(
            Score(
                sandbox_id=sb_pass,
                dimension="suite_pass",
                score=10.0,
                rationale="2/2 deterministic suite checks passed",
            )
        )
        s.commit()
    res = api.client.get(f"/api/runs/{run_id}/results")
    sb_out = {s["name"]: s for s in res.json()["sandboxes"]}["cfg-a"]
    assert sb_out["suite"]["suite_pass"] == 10.0
    assert sb_out["dimensions"]["suite_pass"]["score"] == 10.0


def test_checker_newest_wins_on_rescore(api) -> None:
    """A second checker pass (rescore) appends rows — the API shows the
    latest verdict per check."""
    batch_id, run_id, sb_pass = _seed_batch(api)
    with api.session_factory() as s:
        s.add(
            CheckerResult(
                run_id=run_id,
                sandbox_id=sb_pass,
                check_type="file_exists",
                spec={"path": "index.html"},
                status="fail",
                passed=False,
                detail="file removed in v2",
            )
        )
        s.commit()
    res = api.client.get(f"/api/suite-runs/{batch_id}")
    cells = {c["name"]: c for c in res.json()["runs"][0]["cells"]}
    cell = cells["cfg-a"]
    assert cell["checks"]["passed"] == 1
    assert cell["checks"]["failed"] == 1
    by_type = {c["check"]: c for c in cell["checks"]["checks"]}
    assert by_type["file_exists"]["detail"] == "file removed in v2"
