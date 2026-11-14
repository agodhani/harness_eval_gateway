"""Results read-API tests (F7/F8): run results, sandbox scores, downloads.

Runs against in-memory sqlite via the shared conftest app (same pattern as
test_events.py). Artifact downloads jail to ARTIFACT_ROOT — monkeypatched to
a tmp dir per test.

PR5 additions cover the eval-iteration endpoints: rubric version listing +
creation, the re-score trigger (with a stubbed ``lifecycle.rescore_run`` —
the REAL ``runner.start_rescore_task`` wiring still runs), and the
``?rubric_version=`` results filter.
"""

from __future__ import annotations

import io
import json
import time
import uuid
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from api.deps import get_session_factory
from api.routers import results as results_router
from db.models import Artifact, Base, Rubric, Run, Sandbox, Score
from orchestrator import lifecycle as lifecycle_mod
from orchestrator import runner


@pytest.fixture()
def session_factory() -> sessionmaker[Session]:
    sf = get_session_factory()
    Base.metadata.create_all(sf.kw["bind"])
    return sf


@pytest.fixture()
def scored_run(session_factory: sessionmaker[Session]):
    """Run (config with evaluator_model) + 2 sandboxes + rubric + scores."""
    s = session_factory()
    try:
        run = Run(
            task_prompt="build 2048",
            model_name="qwen3:8b",
            config={
                "evaluator_model": "qwen3:8b",
                "same_model_eval": True,
                "budgets": {"max_iters": 50},
            },
            status="done",
        )
        s.add(run)
        s.flush()
        sb1 = Sandbox(
            run_id=run.id,
            harness_config={"adapter": "miniloop", "skill_packs": ["superpowers"]},
            status="done",
            terminal_reason="agent_done",
            artifact_path="/data/artifacts/x/y",
        )
        sb2 = Sandbox(
            run_id=run.id,
            harness_config={"adapter": "miniloop", "skill_packs": []},
            status="failed",
            terminal_reason="timeout",
        )
        s.add_all([sb1, sb2])
        s.flush()
        rubric = Rubric(
            run_id=run.id, version=1, status="approved",
            content={"dimensions": [{"name": "functional_requirements"}]},
        )
        s.add(rubric)
        s.flush()

        def score(sb, dim, val, rationale=""):
            return Score(
                sandbox_id=sb.id, rubric_id=rubric.id,
                rubric_version=1,
                dimension=dim, score=val, rationale=rationale,
            )

        s.add_all([
            score(sb1, "functional_requirements", 8.0, "game loop works"),
            score(sb1, "code_quality", 6.0, "readable"),
            score(sb1, "overall", 7.0, "solid attempt"),
            # sb2 intentionally has NO scores (eval failed/skipped)
        ])
        s.commit()
        return {
            "run_id": run.id, "sb1": sb1.id, "sb2": sb2.id,
            "rubric_id": rubric.id,
        }
    finally:
        s.close()


# ---------------------------------------------------------------------------
# GET /api/runs/{run_id}/results
# ---------------------------------------------------------------------------


def test_run_results_shape(client: TestClient, scored_run) -> None:
    resp = client.get(f"/api/runs/{scored_run['run_id']}/results")
    assert resp.status_code == 200
    body = resp.json()

    assert body["run_id"] == str(scored_run["run_id"])
    assert body["status"] == "done"
    assert body["model_name"] == "qwen3:8b"
    assert body["evaluator_model"] == "qwen3:8b"
    assert body["same_model_eval"] is True
    assert len(body["sandboxes"]) == 2

    sb1 = next(s for s in body["sandboxes"] if s["sandbox_id"] == str(scored_run["sb1"]))
    assert sb1["status"] == "done"
    assert sb1["terminal_reason"] == "agent_done"
    assert sb1["harness_config"]["skill_packs"] == ["superpowers"]
    assert sb1["rubric_id"] == str(scored_run["rubric_id"])
    assert sb1["dimensions"]["functional_requirements"] == {
        "score": 8.0, "rationale": "game loop works"
    }
    assert sb1["dimensions"]["code_quality"]["score"] == 6.0
    # overall pseudo-dimension surfaced separately, not inside dimensions
    assert "overall" not in sb1["dimensions"]
    assert sb1["overall"] == {"score": 7.0, "rationale": "solid attempt"}

    sb2 = next(s for s in body["sandboxes"] if s["sandbox_id"] == str(scored_run["sb2"]))
    assert sb2["dimensions"] == {}
    assert sb2["overall"] is None
    assert sb2["terminal_reason"] == "timeout"


def test_run_results_includes_artifacts(
    client: TestClient, scored_run, session_factory
) -> None:
    """Artifact rows surface as {id, kind, path} so the UI can build
    /api/artifacts/{id}/download links."""
    s = session_factory()
    try:
        art = Artifact(
            sandbox_id=scored_run["sb1"],
            kind="workspace",
            path="/data/artifacts/x/y/workspace",
            size_bytes=123,
        )
        s.add(art)
        s.commit()
        art_id = art.id
    finally:
        s.close()

    resp = client.get(f"/api/runs/{scored_run['run_id']}/results")
    assert resp.status_code == 200
    body = resp.json()
    sb1 = next(s for s in body["sandboxes"] if s["sandbox_id"] == str(scored_run["sb1"]))
    assert sb1["artifacts"] == [
        {"id": str(art_id), "kind": "workspace", "path": "/data/artifacts/x/y/workspace"}
    ]
    sb2 = next(s for s in body["sandboxes"] if s["sandbox_id"] == str(scored_run["sb2"]))
    assert sb2["artifacts"] == []


def test_run_results_404(client: TestClient) -> None:
    resp = client.get(f"/api/runs/{uuid.uuid4()}/results")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# PR6 — SandboxResult.analysis from <artifact>/analysis.json
# ---------------------------------------------------------------------------


ANALYSIS_JSON = {
    "schema_version": 1,
    "generated_at": "2026-11-13T00:00:00+00:00",
    "workspace": "/x/y/workspace",
    "signals": [
        {
            "name": "static_analysis",
            "status": "ok",
            "metrics": {
                "files_analyzed": 2,
                "function_count": 5,
                "cyclomatic_complexity": {"mean": 3.4, "max": 9.0},
            },
            "files": [
                {
                    "path": "a.py",
                    "language": "python",
                    "lines": 40,
                    "nloc": 30,
                    "function_count": 3,
                    "avg_cyclomatic_complexity": 4.0,
                    "max_cyclomatic_complexity": 9.0,
                    "max_nesting_depth": 2,
                }
            ],
            "functions": [
                {
                    "name": "f_branchy",
                    "file": "a.py",
                    "line": 5,
                    "cyclomatic_complexity": 9.0,
                    "nloc": 12,
                    "params": 2,
                    "max_nesting_depth": 2,
                }
            ],
            "warnings": ["1 source file(s) over 512 KiB skipped"],
            "error": None,
        }
    ],
}


def _set_artifact_path(session_factory, sandbox_id, path: str) -> None:
    s = session_factory()
    try:
        sb = s.get(Sandbox, sandbox_id)
        sb.artifact_path = path
        s.commit()
    finally:
        s.close()


def test_run_results_includes_analysis(
    client: TestClient, scored_run, session_factory, artifact_root
) -> None:
    """A valid <artifact>/analysis.json is re-validated + embedded."""
    art_dir = artifact_root / "x" / "y"
    art_dir.mkdir(parents=True)
    (art_dir / "analysis.json").write_text(json.dumps(ANALYSIS_JSON))
    _set_artifact_path(session_factory, scored_run["sb1"], str(art_dir))

    resp = client.get(f"/api/runs/{scored_run['run_id']}/results")
    assert resp.status_code == 200
    body = resp.json()
    sb1 = next(s for s in body["sandboxes"] if s["sandbox_id"] == str(scored_run["sb1"]))
    assert sb1["analysis"]["schema_version"] == 1
    sig = sb1["analysis"]["signals"][0]
    assert sig["name"] == "static_analysis"
    assert sig["metrics"]["cyclomatic_complexity"]["max"] == 9.0
    assert sig["functions"][0]["name"] == "f_branchy"
    # sb2 has no artifact_path -> analysis absent
    sb2 = next(s for s in body["sandboxes"] if s["sandbox_id"] == str(scored_run["sb2"]))
    assert sb2["analysis"] is None


def test_run_results_invalid_analysis_is_null(
    client: TestClient, scored_run, session_factory, artifact_root
) -> None:
    """Malformed/foreign analysis.json degrades to analysis=null — never a
    500 or a pass-through of unvalidated candidate content."""
    art_dir = artifact_root / "x" / "y"
    art_dir.mkdir(parents=True)
    (art_dir / "analysis.json").write_text('{"completely": "wrong shape"')
    _set_artifact_path(session_factory, scored_run["sb1"], str(art_dir))

    resp = client.get(f"/api/runs/{scored_run['run_id']}/results")
    assert resp.status_code == 200
    sb1 = next(
        s for s in resp.json()["sandboxes"] if s["sandbox_id"] == str(scored_run["sb1"])
    )
    assert sb1["analysis"] is None


# ---------------------------------------------------------------------------
# GET /api/sandboxes/{sandbox_id}/scores
# ---------------------------------------------------------------------------


def test_sandbox_scores(client: TestClient, scored_run) -> None:
    resp = client.get(f"/api/sandboxes/{scored_run['sb1']}/scores")
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 3
    dims = {r["dimension"]: r for r in rows}
    assert dims["functional_requirements"]["score"] == 8.0
    assert dims["overall"]["score"] == 7.0
    assert all(r["sandbox_id"] == str(scored_run["sb1"]) for r in rows)
    assert all(r["rubric_id"] == str(scored_run["rubric_id"]) for r in rows)


def test_sandbox_scores_empty_and_404(client: TestClient, scored_run) -> None:
    resp = client.get(f"/api/sandboxes/{scored_run['sb2']}/scores")
    assert resp.status_code == 200
    assert resp.json() == []

    resp = client.get(f"/api/sandboxes/{uuid.uuid4()}/scores")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# GET /api/artifacts/{artifact_id}/download
# ---------------------------------------------------------------------------


@pytest.fixture()
def artifact_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Point the results router's artifact jail at a tmp dir."""
    monkeypatch.setattr(
        results_router,
        "get_settings",
        lambda: SimpleNamespace(
            ARTIFACT_ROOT=tmp_path, PREVIEW_SCAN_TTL_SECONDS=0
        ),
    )
    results_router.reset_scan_cache()
    return tmp_path


def _artifact(session_factory, sandbox_id, path: str, kind: str = "workspace") -> uuid.UUID:
    s = session_factory()
    try:
        art = Artifact(sandbox_id=sandbox_id, kind=kind, path=path, size_bytes=0)
        s.add(art)
        s.commit()
        return art.id
    finally:
        s.close()


def test_download_file(client: TestClient, scored_run, session_factory, artifact_root) -> None:
    f = artifact_root / "run1" / "sb1" / "hello.txt"
    f.parent.mkdir(parents=True)
    f.write_text("hello world")
    art_id = _artifact(session_factory, scored_run["sb1"], str(f), kind="transcript")

    resp = client.get(f"/api/artifacts/{art_id}/download")
    assert resp.status_code == 200
    assert resp.content == b"hello world"
    assert "hello.txt" in resp.headers.get("content-disposition", "")


def test_download_relative_path(client: TestClient, scored_run, session_factory, artifact_root) -> None:
    f = artifact_root / "rel" / "notes.md"
    f.parent.mkdir(parents=True)
    f.write_text("# notes")
    # stored relative to artifact root
    art_id = _artifact(session_factory, scored_run["sb1"], "rel/notes.md")
    resp = client.get(f"/api/artifacts/{art_id}/download")
    assert resp.status_code == 200
    assert resp.content == b"# notes"


def test_download_directory_zipped(client: TestClient, scored_run, session_factory, artifact_root) -> None:
    d = artifact_root / "ws" / "proj"
    (d / "src").mkdir(parents=True)
    (d / "src" / "app.py").write_text("print(1)")
    (d / "README.md").write_text("readme")
    art_id = _artifact(session_factory, scored_run["sb1"], str(d), kind="workspace")

    resp = client.get(f"/api/artifacts/{art_id}/download")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/zip"
    zf = zipfile.ZipFile(io.BytesIO(resp.content))
    names = set(zf.namelist())
    assert "src/app.py" in names
    assert "README.md" in names
    assert zf.read("README.md") == b"readme"


def test_download_traversal_blocked(client: TestClient, scored_run, session_factory, artifact_root) -> None:
    outside = artifact_root.parent / "secret.txt"
    outside.write_text("secret")
    try:
        # relative escape attempt
        art_id = _artifact(session_factory, scored_run["sb1"], "../secret.txt")
        resp = client.get(f"/api/artifacts/{art_id}/download")
        assert resp.status_code == 404

        # absolute path outside the jail
        art_id2 = _artifact(session_factory, scored_run["sb1"], str(outside))
        resp2 = client.get(f"/api/artifacts/{art_id2}/download")
        assert resp2.status_code == 404
    finally:
        outside.unlink()


def test_download_missing_and_unknown(client: TestClient, scored_run, session_factory, artifact_root) -> None:
    art_id = _artifact(session_factory, scored_run["sb1"], str(artifact_root / "nope.txt"))
    assert client.get(f"/api/artifacts/{art_id}/download").status_code == 404
    assert client.get(f"/api/artifacts/{uuid.uuid4()}/download").status_code == 404


# ---------------------------------------------------------------------------
# PR5 — rubric versions + re-score
# ---------------------------------------------------------------------------

DIMS = [
    "functional_requirements",
    "acceptance_criteria",
    "code_quality",
    "cyclomatic_complexity",
]


def _rubric_content(**overrides: Any) -> dict[str, Any]:
    content: dict[str, Any] = {
        "summary": "scores the artifact",
        "dimensions": [{"name": d, "title": d} for d in DIMS],
        "criteria": [],
    }
    content.update(overrides)
    return content


@pytest.fixture()
def rescore_stub(session_factory, tmp_path: Path, monkeypatch):
    """Stub ``lifecycle.rescore_run`` (no docker) while keeping the REAL
    ``runner.start_rescore_task``/``_track``/``in_flight`` wiring."""
    calls: list[dict] = []
    ns = SimpleNamespace(
        ARTIFACT_ROOT=tmp_path / "artifacts",
        HOST_DATA_ROOT="",
    )

    async def fake_rescore(run_id, **kwargs):
        calls.append({"run_id": run_id, **kwargs})

    monkeypatch.setattr(lifecycle_mod, "rescore_run", fake_rescore)
    monkeypatch.setattr(runner, "_session_factory", lambda: session_factory)
    monkeypatch.setattr(runner, "_settings", lambda: ns)
    monkeypatch.setattr(
        runner, "get_runtime", lambda: SimpleNamespace(artifact_root=ns.ARTIFACT_ROOT)
    )
    return calls


def _wait_for(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


# ---- GET /api/runs/{id}/rubrics -------------------------------------------


def test_list_rubric_versions(client: TestClient, scored_run, session_factory) -> None:
    resp = client.get(f"/api/runs/{scored_run['run_id']}/rubrics")
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 1
    assert rows[0]["version"] == 1
    assert rows[0]["status"] == "approved"
    assert rows[0]["id"] == str(scored_run["rubric_id"])
    assert rows[0]["parent_id"] is None
    assert rows[0]["score_count"] == 3  # all scores carry this rubric_id

    assert client.get(f"/api/runs/{uuid.uuid4()}/rubrics").status_code == 404


# ---- POST /api/runs/{id}/rubrics ------------------------------------------


def test_create_rubric_version(client: TestClient, scored_run, session_factory) -> None:
    resp = client.post(
        f"/api/runs/{scored_run['run_id']}/rubrics",
        json={"content": _rubric_content(summary="tightened v2")},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["version"] == 2
    assert body["status"] == "approved"
    assert body["parent_id"] == str(scored_run["rubric_id"])
    assert body["content"]["summary"] == "tightened v2"

    s = session_factory()
    try:
        v1 = s.get(Rubric, scored_run["rubric_id"])
        assert v1.status == "superseded"
    finally:
        s.close()

    # run detail now serves v2 as the latest rubric
    detail = client.get(f"/api/runs/{scored_run['run_id']}").json()
    assert detail["rubric"]["version"] == 2

    # a second save → v3 parented on v2
    resp = client.post(
        f"/api/runs/{scored_run['run_id']}/rubrics",
        json={"content": _rubric_content(summary="v3")},
    )
    assert resp.status_code == 201
    assert resp.json()["version"] == 3
    versions = client.get(f"/api/runs/{scored_run['run_id']}/rubrics").json()
    assert [v["version"] for v in versions] == [3, 2, 1]
    statuses = {v["version"]: v["status"] for v in versions}
    assert statuses == {3: "approved", 2: "superseded", 1: "superseded"}


def test_create_rubric_version_requires_terminal_run(
    client: TestClient, scored_run, session_factory
) -> None:
    s = session_factory()
    try:
        run = s.get(Run, scored_run["run_id"])
        run.status = "running"
        s.commit()
    finally:
        s.close()
    resp = client.post(
        f"/api/runs/{scored_run['run_id']}/rubrics",
        json={"content": _rubric_content()},
    )
    assert resp.status_code == 409


def test_create_rubric_version_validates(
    client: TestClient, scored_run
) -> None:
    # canonical dimensions enforced at the boundary
    bad = _rubric_content()
    bad["dimensions"] = bad["dimensions"][:3]
    assert (
        client.post(
            f"/api/runs/{scored_run['run_id']}/rubrics", json={"content": bad}
        ).status_code
        == 422
    )
    # unknown run / unknown base
    assert (
        client.post(
            f"/api/runs/{uuid.uuid4()}/rubrics",
            json={"content": _rubric_content()},
        ).status_code
        == 404
    )
    assert (
        client.post(
            f"/api/runs/{scored_run['run_id']}/rubrics",
            json={"content": _rubric_content(), "base_rubric_id": str(uuid.uuid4())},
        ).status_code
        == 404
    )


# ---- POST /api/runs/{id}/rescore ------------------------------------------


def test_rescore_triggers_task_and_transitions(
    client: TestClient, scored_run, rescore_stub, session_factory
) -> None:
    resp = client.post(f"/api/runs/{scored_run['run_id']}/rescore", json={})
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["status"] == "evaluating"
    assert body["rubric_id"] == str(scored_run["rubric_id"])
    assert body["rubric_version"] == 1
    assert body["model"] == "qwen3:8b"
    assert body["sandboxes"] == 1  # only sb1 has an artifact_path

    # run flipped to evaluating + finished_at cleared
    s = session_factory()
    try:
        run = s.get(Run, scored_run["run_id"])
        assert run.status == "evaluating"
        assert run.finished_at is None
    finally:
        s.close()

    # the real task registry routed rescore_run with our kwargs
    assert _wait_for(lambda: len(rescore_stub) == 1)
    kw = rescore_stub[0]
    assert kw["run_id"] == scored_run["run_id"]
    assert kw["rubric_id"] == scored_run["rubric_id"]


def test_rescore_explicit_rubric_version_and_subset(
    client: TestClient, scored_run, rescore_stub
) -> None:
    # create a v2 rubric first
    v2 = client.post(
        f"/api/runs/{scored_run['run_id']}/rubrics",
        json={"content": _rubric_content(summary="v2")},
    ).json()
    resp = client.post(
        f"/api/runs/{scored_run['run_id']}/rescore",
        json={"rubric_id": v2["id"], "sandbox_ids": [str(scored_run["sb1"])]},
    )
    assert resp.status_code == 202, resp.text
    assert resp.json()["rubric_version"] == 2

    assert _wait_for(lambda: len(rescore_stub) == 1)
    kw = rescore_stub[0]
    assert str(kw["rubric_id"]) == v2["id"]
    assert [str(s) for s in kw["sandbox_ids"]] == [str(scored_run["sb1"])]


def test_rescore_guards(client: TestClient, scored_run, session_factory, rescore_stub) -> None:
    run_id = scored_run["run_id"]
    # unknown run / rubric
    assert client.post(f"/api/runs/{uuid.uuid4()}/rescore", json={}).status_code == 404
    assert (
        client.post(f"/api/runs/{run_id}/rescore", json={"rubric_id": str(uuid.uuid4())}).status_code
        == 404
    )
    # sandbox not in run → 422
    assert (
        client.post(
            f"/api/runs/{run_id}/rescore",
            json={"sandbox_ids": [str(uuid.uuid4())]},
        ).status_code
        == 422
    )
    # sandbox without artifact → 422
    assert (
        client.post(
            f"/api/runs/{run_id}/rescore",
            json={"sandbox_ids": [str(scored_run["sb2"])]},
        ).status_code
        == 422
    )
    # active run → 409
    s = session_factory()
    try:
        run = s.get(Run, run_id)
        run.status = "running"
        s.commit()
    finally:
        s.close()
    assert client.post(f"/api/runs/{run_id}/rescore", json={}).status_code == 409
    s = session_factory()
    try:
        run = s.get(Run, run_id)
        run.status = "done"
        s.commit()
    finally:
        s.close()
    # run with zero artifact-bearing sandboxes → 409
    s = session_factory()
    try:
        bare = Run(task_prompt="x", model_name="m", config={}, status="done")
        s.add(bare)
        s.commit()
        bare_id = bare.id
        s.add(Rubric(run_id=bare_id, version=1, status="approved", content={}))
        s.commit()
    finally:
        s.close()
    assert client.post(f"/api/runs/{bare_id}/rescore", json={}).status_code == 409


def test_rescore_in_flight_conflict(client: TestClient, scored_run) -> None:
    """A second trigger while a rescore task is in flight → 409.

    The shared TestClient is not context-managed, so each request runs on a
    fresh event-loop portal — a real spawned task can't stay 'in flight'
    across requests here. Seed the registry with a never-done task instead
    (the registry/dedup behaviour itself is covered in
    tests/unit/orchestrator/test_rescore.py)."""

    class _PendingTask:
        def done(self) -> bool:
            return False

    key = (scored_run["run_id"], "rescore")
    runner._TASKS[key] = _PendingTask()  # type: ignore[assignment]
    try:
        resp = client.post(f"/api/runs/{scored_run['run_id']}/rescore", json={})
        assert resp.status_code == 409
        assert "already in progress" in resp.json()["detail"]
    finally:
        runner._TASKS.pop(key, None)


def test_create_rubric_version_with_rescore(
    client: TestClient, scored_run, rescore_stub, session_factory
) -> None:
    resp = client.post(
        f"/api/runs/{scored_run['run_id']}/rubrics",
        json={"content": _rubric_content(summary="v2"), "rescore": True},
    )
    assert resp.status_code == 201, resp.text
    v2 = resp.json()

    s = session_factory()
    try:
        run = s.get(Run, scored_run["run_id"])
        assert run.status == "evaluating"
    finally:
        s.close()

    assert _wait_for(lambda: len(rescore_stub) == 1)
    assert str(rescore_stub[0]["rubric_id"]) == v2["id"]


# ---- results ?rubric_version= filter ---------------------------------------


def test_results_rubric_version_filter(
    client: TestClient, scored_run, session_factory
) -> None:
    """v1 + v2 scores coexist; the filter selects one version's provenance,
    the default view keeps newest-wins."""
    s = session_factory()
    try:
        v2 = Rubric(
            run_id=scored_run["run_id"], version=2, status="approved",
            parent_id=scored_run["rubric_id"],
            content={"dimensions": [{"name": "functional_requirements"}]},
        )
        # creating v2 directly: v1 must be superseded for realism
        v1 = s.get(Rubric, scored_run["rubric_id"])
        v1.status = "superseded"
        s.add(v2)
        s.flush()
        s.add_all([
            Score(
                sandbox_id=scored_run["sb1"], rubric_id=v2.id, rubric_version=2,
                dimension="functional_requirements", score=9.5,
                rationale="re-judged under v2",
            ),
            Score(
                sandbox_id=scored_run["sb1"], rubric_id=v2.id, rubric_version=2,
                dimension="overall", score=9.0, rationale="better",
            ),
        ])
        s.commit()
        v2_id = v2.id
    finally:
        s.close()

    # default view: newest-wins → v2 scores surface
    body = client.get(f"/api/runs/{scored_run['run_id']}/results").json()
    sb1 = next(x for x in body["sandboxes"] if x["sandbox_id"] == str(scored_run["sb1"]))
    assert sb1["dimensions"]["functional_requirements"]["score"] == 9.5
    assert sb1["overall"]["score"] == 9.0
    assert sb1["rubric_version"] == 2
    assert body["selected_rubric_version"] is None
    versions = {v["version"]: v for v in body["rubric_versions"]}
    assert versions[1]["score_count"] == 3
    assert versions[2]["score_count"] == 2
    assert versions[1]["status"] == "superseded"

    # ?rubric_version=1 → only v1 provenance
    body = client.get(
        f"/api/runs/{scored_run['run_id']}/results", params={"rubric_version": 1}
    ).json()
    sb1 = next(x for x in body["sandboxes"] if x["sandbox_id"] == str(scored_run["sb1"]))
    assert sb1["dimensions"]["functional_requirements"]["score"] == 8.0
    assert sb1["overall"]["score"] == 7.0
    assert sb1["rubric_version"] == 1
    assert body["selected_rubric_version"] == 1

    # version with no scores → empty dimensions but no error
    s = session_factory()
    try:
        s.add(
            Rubric(
                run_id=scored_run["run_id"], version=3, status="approved",
                content={}, parent_id=v2_id,
            )
        )
        s.commit()
    finally:
        s.close()
    body = client.get(
        f"/api/runs/{scored_run['run_id']}/results", params={"rubric_version": 3}
    ).json()
    sb1 = next(x for x in body["sandboxes"] if x["sandbox_id"] == str(scored_run["sb1"]))
    assert sb1["dimensions"] == {}
    assert sb1["rubric_version"] is None


def test_sandbox_scores_expose_rubric_version(
    client: TestClient, scored_run
) -> None:
    rows = client.get(f"/api/sandboxes/{scored_run['sb1']}/scores").json()
    assert all(r["rubric_version"] == 1 for r in rows)


def test_results_same_second_passes_newest_inserted_wins(
    client: TestClient, scored_run, session_factory
) -> None:
    """created_at ties (sqlite timestamps are second-precision): the
    LAST-INSERTED pass wins per dimension via seq — even when the newer
    pass re-judged against an OLDER rubric version. A version-based
    tiebreak would show the stale v2 score here."""
    ts = datetime(2099, 1, 1, tzinfo=UTC)  # identical instant, both passes
    s = session_factory()
    try:
        v2 = Rubric(
            run_id=scored_run["run_id"], version=2, status="approved",
            parent_id=scored_run["rubric_id"],
            content={"dimensions": [{"name": "functional_requirements"}]},
        )
        s.add(v2)
        s.flush()
        s.add_all([
            # Earlier pass, judged under v2.
            Score(
                sandbox_id=scored_run["sb1"], rubric_id=v2.id,
                rubric_version=2, seq=10, created_at=ts,
                dimension="functional_requirements", score=9.5,
                rationale="v2 pass",
            ),
            # Later pass, re-pinned to v1 — must win despite lower version.
            Score(
                sandbox_id=scored_run["sb1"], rubric_id=scored_run["rubric_id"],
                rubric_version=1, seq=20, created_at=ts,
                dimension="functional_requirements", score=3.0,
                rationale="v1 rescore pass",
            ),
        ])
        s.commit()
    finally:
        s.close()

    body = client.get(f"/api/runs/{scored_run['run_id']}/results").json()
    sb1 = next(
        x for x in body["sandboxes"]
        if x["sandbox_id"] == str(scored_run["sb1"])
    )
    assert sb1["dimensions"]["functional_requirements"] == {
        "score": 3.0, "rationale": "v1 rescore pass"
    }
    assert sb1["rubric_version"] == 1
