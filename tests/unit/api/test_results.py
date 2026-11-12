"""Results read-API tests (F7/F8): run results, sandbox scores, downloads.

Runs against in-memory sqlite via the shared conftest app (same pattern as
test_events.py). Artifact downloads jail to ARTIFACT_ROOT — monkeypatched to
a tmp dir per test.
"""

from __future__ import annotations

import io
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


def test_run_results_404(client: TestClient) -> None:
    resp = client.get(f"/api/runs/{uuid.uuid4()}/results")
    assert resp.status_code == 404


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
        lambda: SimpleNamespace(ARTIFACT_ROOT=tmp_path),
    )
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
