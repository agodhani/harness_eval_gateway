"""Results read-API tests (F7/F8): run results, sandbox scores, downloads.

Runs against in-memory sqlite via the shared conftest app (same pattern as
test_events.py). Artifact downloads jail to ARTIFACT_ROOT — monkeypatched to
a tmp dir per test.
"""

from __future__ import annotations

import io
import json
import uuid
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from api.deps import get_session_factory
from api.routers import results as results_router
from db.models import Artifact, Base, Rubric, Run, Sandbox, Score


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


def test_run_results_exec_summary(
    client: TestClient, scored_run, session_factory, artifact_root
) -> None:
    """PR7: the flat `exec` field is derived from the analysis exec signal."""
    payload = json.loads(json.dumps(ANALYSIS_JSON))
    payload["signals"].append(
        {
            "name": "exec",
            "status": "ok",
            "metrics": {
                "build_ok": True,
                "tests_detected": True,
                "tests_ok": True,
                "tests_collected": 5,
                "tests_passed": 5,
                "tests_failed": 0,
                "tests_errors": 0,
                "tests_skipped": 1,
                "coverage_percent": 82.5,
                "timed_out": False,
                "duration_seconds": 9.4,
            },
            "warnings": [],
            "error": None,
        }
    )
    art_dir = artifact_root / "x" / "y"
    art_dir.mkdir(parents=True)
    (art_dir / "analysis.json").write_text(json.dumps(payload))
    _set_artifact_path(session_factory, scored_run["sb1"], str(art_dir))

    resp = client.get(f"/api/runs/{scored_run['run_id']}/results")
    assert resp.status_code == 200
    sb1 = next(
        s for s in resp.json()["sandboxes"] if s["sandbox_id"] == str(scored_run["sb1"])
    )
    ex = sb1["exec"]
    assert ex["status"] == "ok"
    assert ex["build_ok"] is True
    assert ex["tests_detected"] is True
    assert ex["tests_passed"] == 5
    assert ex["tests_skipped"] == 1
    assert ex["coverage_percent"] == 82.5
    assert ex["timed_out"] is False
    # no artifact -> no analysis -> no exec summary
    sb2 = next(
        s for s in resp.json()["sandboxes"] if s["sandbox_id"] == str(scored_run["sb2"])
    )
    assert sb2["exec"] is None


def test_run_results_exec_summary_skipped(
    client: TestClient, scored_run, session_factory, artifact_root
) -> None:
    """A skipped exec signal surfaces its status + reason, not zeros."""
    payload = json.loads(json.dumps(ANALYSIS_JSON))
    payload["signals"].append(
        {
            "name": "exec",
            "status": "skipped",
            "metrics": {},
            "warnings": ["exec scoring disabled (SCORING_EXEC=0)"],
            "error": None,
        }
    )
    art_dir = artifact_root / "x" / "y"
    art_dir.mkdir(parents=True)
    (art_dir / "analysis.json").write_text(json.dumps(payload))
    _set_artifact_path(session_factory, scored_run["sb1"], str(art_dir))

    resp = client.get(f"/api/runs/{scored_run['run_id']}/results")
    assert resp.status_code == 200
    sb1 = next(
        s for s in resp.json()["sandboxes"] if s["sandbox_id"] == str(scored_run["sb1"])
    )
    assert sb1["exec"]["status"] == "skipped"
    assert "SCORING_EXEC" in (sb1["exec"]["detail"] or "")


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
