"""Artifact-preview endpoint tests (F10).

Runs against in-memory sqlite via the shared conftest app. The PreviewManager
is replaced with a fake (no docker); ARTIFACT_ROOT is monkeypatched to a tmp
dir per test — the same jail pattern as test_results.py.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from api.deps import get_session_factory
from api.routers import preview as preview_router
from api.routers import results as results_router
from db.models import Base, Event, Run, Sandbox
from sandbox.preview import Preview


@pytest.fixture()
def session_factory() -> sessionmaker[Session]:
    sf = get_session_factory()
    Base.metadata.create_all(sf.kw["bind"])
    return sf


@pytest.fixture()
def run(session_factory: sessionmaker[Session]):
    """One done run with two sandboxes (sb1 has an artifact_path, sb2 not)."""
    s = session_factory()
    try:
        r = Run(task_prompt="build 2048", model_name="qwen3:8b", config={}, status="done")
        s.add(r)
        s.flush()
        sb1 = Sandbox(run_id=r.id, harness_config={}, status="done",
                      terminal_reason="agent_done")
        sb2 = Sandbox(run_id=r.id, harness_config={}, status="done",
                      terminal_reason="agent_done")
        s.add_all([sb1, sb2])
        s.commit()
        return {"run_id": r.id, "sb1": sb1.id, "sb2": sb2.id}
    finally:
        s.close()


class FakeManager:
    """Duck-typed PreviewManager — no docker."""

    def __init__(self) -> None:
        self.previews: list[Preview] = []
        self.spawn_calls: list[dict] = []
        self.stop_calls: list[str] = []
        self.spawn_error: Exception | None = None

    def list(self, *, sandbox_id: str | None = None) -> list[Preview]:
        if sandbox_id is None:
            return list(self.previews)
        return [p for p in self.previews if p.sandbox_id == sandbox_id]

    def get_for_sandbox(self, sandbox_id: str) -> Preview | None:
        matches = self.list(sandbox_id=sandbox_id)
        return matches[0] if matches else None

    def spawn(self, **kw) -> Preview:
        self.spawn_calls.append(kw)
        if self.spawn_error is not None:
            raise self.spawn_error
        preview = Preview(
            preview_id=str(uuid.uuid4()),
            run_id=kw["run_id"],
            sandbox_id=kw["sandbox_id"],
            type=kw["plan"].type,
            container=f"heg-prev-{uuid.uuid4().hex[:12]}",
            status="running",
            port=49321,
            entry=kw["plan"].entry,
            url="http://localhost:49321/",
            created_at=datetime.now(UTC),
            expires_at=datetime.now(UTC),
        )
        self.previews.append(preview)
        return preview

    def stop(self, preview_id: str) -> bool:
        self.stop_calls.append(preview_id)
        before = len(self.previews)
        self.previews = [p for p in self.previews if p.preview_id != preview_id]
        return len(self.previews) < before

    def reap_expired(self, now: float | None = None) -> int:
        return 0


@pytest.fixture()
def fake_manager(monkeypatch: pytest.MonkeyPatch) -> FakeManager:
    mgr = FakeManager()
    monkeypatch.setattr(preview_router, "get_preview_manager", lambda: mgr)
    return mgr


@pytest.fixture()
def artifact_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point both routers' ARTIFACT_ROOT jail at a tmp dir."""

    def fake_settings() -> SimpleNamespace:
        return SimpleNamespace(ARTIFACT_ROOT=tmp_path)

    monkeypatch.setattr(preview_router, "get_settings", fake_settings)
    monkeypatch.setattr(results_router, "get_settings", fake_settings)
    return tmp_path


def _workspace(root: Path, run_id, sandbox_id, files: dict[str, str]) -> Path:
    ws = root / str(run_id) / str(sandbox_id) / "workspace"
    ws.mkdir(parents=True)
    for rel, content in files.items():
        p = ws / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    return ws


def _preview(run_id, sandbox_id, *, status: str = "running") -> Preview:
    return Preview(
        preview_id=str(uuid.uuid4()),
        run_id=str(run_id),
        sandbox_id=str(sandbox_id),
        type="static",
        container="heg-prev-abc",
        status=status,
        port=49000,
        entry="index.html",
        url="http://localhost:49000/" if status == "running" else None,
        created_at=datetime.now(UTC),
        expires_at=datetime.now(UTC),
    )


# ---------------------------------------------------------------------------
# POST .../preview
# ---------------------------------------------------------------------------


def test_start_preview_static(
    client: TestClient, run, artifact_root, fake_manager: FakeManager
) -> None:
    _workspace(artifact_root, run["run_id"], run["sb1"], {"index.html": "<h1>x</h1>"})
    resp = client.post(f"/api/runs/{run['run_id']}/sandboxes/{run['sb1']}/preview")
    assert resp.status_code == 201
    body = resp.json()
    assert body["type"] == "static"
    assert body["status"] == "running"
    assert body["url"] == "http://localhost:49321/"
    assert body["entry"] == "index.html"
    assert body["sandbox_id"] == str(run["sb1"])
    assert len(fake_manager.spawn_calls) == 1
    kw = fake_manager.spawn_calls[0]
    assert kw["serve_dir"].name == "workspace"
    assert kw["plan"].entry == "index.html"


def test_start_preview_nested_entry(
    client: TestClient, run, artifact_root, fake_manager: FakeManager
) -> None:
    _workspace(
        artifact_root,
        run["run_id"],
        run["sb1"],
        {"dist/index.html": "<h1>x</h1>", "dist/app.js": "x"},
    )
    resp = client.post(f"/api/runs/{run['run_id']}/sandboxes/{run['sb1']}/preview")
    assert resp.status_code == 201
    assert fake_manager.spawn_calls[0]["plan"].entry == "dist/index.html"


def test_start_preview_reuses_live(
    client: TestClient, run, artifact_root, fake_manager: FakeManager
) -> None:
    live = _preview(run["run_id"], run["sb1"])
    fake_manager.previews.append(live)
    resp = client.post(f"/api/runs/{run['run_id']}/sandboxes/{run['sb1']}/preview")
    assert resp.status_code == 201
    assert resp.json()["preview_id"] == live.preview_id
    assert fake_manager.spawn_calls == []


def test_start_preview_replaces_dead(
    client: TestClient, run, artifact_root, fake_manager: FakeManager
) -> None:
    dead = _preview(run["run_id"], run["sb1"], status="exited")
    fake_manager.previews.append(dead)
    _workspace(artifact_root, run["run_id"], run["sb1"], {"index.html": "x"})
    resp = client.post(f"/api/runs/{run['run_id']}/sandboxes/{run['sb1']}/preview")
    assert resp.status_code == 201
    assert dead.preview_id in fake_manager.stop_calls
    assert len(fake_manager.spawn_calls) == 1


def test_start_preview_no_workspace(
    client: TestClient, run, artifact_root, fake_manager: FakeManager
) -> None:
    resp = client.post(f"/api/runs/{run['run_id']}/sandboxes/{run['sb1']}/preview")
    assert resp.status_code == 404
    assert "workspace" in resp.json()["detail"].lower()


def test_start_preview_not_previewable(
    client: TestClient, run, artifact_root, fake_manager: FakeManager
) -> None:
    _workspace(artifact_root, run["run_id"], run["sb1"], {"main.py": "print(1)"})
    resp = client.post(f"/api/runs/{run['run_id']}/sandboxes/{run['sb1']}/preview")
    assert resp.status_code == 422
    assert "no previewable artifact" in resp.json()["detail"].lower()


def test_start_preview_ambiguous_html(
    client: TestClient, run, artifact_root, fake_manager: FakeManager
) -> None:
    _workspace(
        artifact_root,
        run["run_id"],
        run["sb1"],
        {"a.html": "a", "b.html": "b"},
    )
    resp = client.post(f"/api/runs/{run['run_id']}/sandboxes/{run['sb1']}/preview")
    assert resp.status_code == 422


def test_start_preview_spawn_failure(
    client: TestClient, run, artifact_root, fake_manager: FakeManager
) -> None:
    _workspace(artifact_root, run["run_id"], run["sb1"], {"index.html": "x"})
    fake_manager.spawn_error = RuntimeError("docker daemon unreachable")
    resp = client.post(f"/api/runs/{run['run_id']}/sandboxes/{run['sb1']}/preview")
    assert resp.status_code == 502
    assert "docker daemon unreachable" in resp.json()["detail"]


def test_start_preview_404s(
    client: TestClient, run, session_factory, artifact_root, fake_manager: FakeManager
) -> None:
    _workspace(artifact_root, run["run_id"], run["sb1"], {"index.html": "x"})
    # unknown run
    resp = client.post(
        f"/api/runs/{uuid.uuid4()}/sandboxes/{run['sb1']}/preview"
    )
    assert resp.status_code == 404
    # unknown sandbox
    resp = client.post(
        f"/api/runs/{run['run_id']}/sandboxes/{uuid.uuid4()}/preview"
    )
    assert resp.status_code == 404
    # sandbox belongs to a different run
    s = session_factory()
    try:
        other = Run(task_prompt="t", model_name="m", config={}, status="done")
        s.add(other)
        s.commit()
        other_id = other.id
    finally:
        s.close()
    resp = client.post(
        f"/api/runs/{other_id}/sandboxes/{run['sb1']}/preview"
    )
    assert resp.status_code == 404


def test_start_preview_jail_blocks_outside_artifact_path(
    client: TestClient, run, session_factory, artifact_root, fake_manager: FakeManager
) -> None:
    """artifact_path pointing outside ARTIFACT_ROOT must not be served."""
    outside = artifact_root.parent / "outside-ws"
    (outside / "workspace").mkdir(parents=True)
    (outside / "workspace" / "index.html").write_text("escape!")
    s = session_factory()
    try:
        sb = s.get(Sandbox, run["sb1"])
        sb.artifact_path = str(outside)
        s.commit()
    finally:
        s.close()
    resp = client.post(f"/api/runs/{run['run_id']}/sandboxes/{run['sb1']}/preview")
    assert resp.status_code == 404
    assert fake_manager.spawn_calls == []


def test_start_preview_emits_checkpoint(
    client: TestClient, run, session_factory, artifact_root, fake_manager: FakeManager
) -> None:
    _workspace(artifact_root, run["run_id"], run["sb1"], {"index.html": "x"})
    resp = client.post(f"/api/runs/{run['run_id']}/sandboxes/{run['sb1']}/preview")
    assert resp.status_code == 201
    s = session_factory()
    try:
        events = (
            s.query(Event)
            .filter(Event.run_id == run["run_id"], Event.type == "checkpoint")
            .all()
        )
        stages = [e.payload.get("stage") for e in events]
        assert "preview_started" in stages
    finally:
        s.close()


# ---------------------------------------------------------------------------
# GET .../preview
# ---------------------------------------------------------------------------


def test_get_preview_404(client: TestClient, run, fake_manager: FakeManager) -> None:
    resp = client.get(f"/api/runs/{run['run_id']}/sandboxes/{run['sb1']}/preview")
    assert resp.status_code == 404


def test_get_preview_returns_state(
    client: TestClient, run, fake_manager: FakeManager
) -> None:
    live = _preview(run["run_id"], run["sb1"])
    fake_manager.previews.append(live)
    resp = client.get(f"/api/runs/{run['run_id']}/sandboxes/{run['sb1']}/preview")
    assert resp.status_code == 200
    body = resp.json()
    assert body["preview_id"] == live.preview_id
    assert body["url"] == "http://localhost:49000/"
    assert body["type"] == "static"


def test_get_preview_reports_exited(
    client: TestClient, run, fake_manager: FakeManager
) -> None:
    dead = _preview(run["run_id"], run["sb1"], status="exited")
    fake_manager.previews.append(dead)
    resp = client.get(f"/api/runs/{run['run_id']}/sandboxes/{run['sb1']}/preview")
    assert resp.status_code == 200
    assert resp.json()["status"] == "exited"


def test_get_preview_wrong_run(
    client: TestClient, run, fake_manager: FakeManager
) -> None:
    resp = client.get(f"/api/runs/{uuid.uuid4()}/sandboxes/{run['sb1']}/preview")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# DELETE .../preview
# ---------------------------------------------------------------------------


def test_stop_preview(client: TestClient, run, fake_manager: FakeManager) -> None:
    live = _preview(run["run_id"], run["sb1"])
    fake_manager.previews.append(live)
    resp = client.delete(f"/api/runs/{run['run_id']}/sandboxes/{run['sb1']}/preview")
    assert resp.status_code == 204
    assert fake_manager.previews == []
    assert live.preview_id in fake_manager.stop_calls


def test_stop_preview_404(client: TestClient, run, fake_manager: FakeManager) -> None:
    resp = client.delete(f"/api/runs/{run['run_id']}/sandboxes/{run['sb1']}/preview")
    assert resp.status_code == 404


def test_stop_preview_emits_checkpoint(
    client: TestClient, run, session_factory, fake_manager: FakeManager
) -> None:
    fake_manager.previews.append(_preview(run["run_id"], run["sb1"]))
    resp = client.delete(f"/api/runs/{run['run_id']}/sandboxes/{run['sb1']}/preview")
    assert resp.status_code == 204
    s = session_factory()
    try:
        events = (
            s.query(Event)
            .filter(Event.run_id == run["run_id"], Event.type == "checkpoint")
            .all()
        )
        stages = [e.payload.get("stage") for e in events]
        assert "preview_stopped" in stages
    finally:
        s.close()


# ---------------------------------------------------------------------------
# results endpoint: previewable flag
# ---------------------------------------------------------------------------


def test_results_previewable_flag(
    client: TestClient, run, artifact_root
) -> None:
    """GET /api/runs/{id}/results surfaces previewable/preview_type per sandbox."""
    _workspace(artifact_root, run["run_id"], run["sb1"], {"index.html": "x"})
    resp = client.get(f"/api/runs/{run['run_id']}/results")
    assert resp.status_code == 200
    sandboxes = {s["sandbox_id"]: s for s in resp.json()["sandboxes"]}
    assert sandboxes[str(run["sb1"])]["previewable"] is True
    assert sandboxes[str(run["sb1"])]["preview_type"] == "static"
    assert sandboxes[str(run["sb2"])]["previewable"] is False
    assert sandboxes[str(run["sb2"])]["preview_type"] is None


def test_results_previewable_respects_jail(
    client: TestClient, run, session_factory, artifact_root
) -> None:
    """artifact_path escaping the jail → previewable stays False."""
    outside = artifact_root.parent / "outside2"
    (outside / "workspace").mkdir(parents=True)
    (outside / "workspace" / "index.html").write_text("x")
    s = session_factory()
    try:
        sb = s.get(Sandbox, run["sb1"])
        sb.artifact_path = str(outside)
        s.commit()
    finally:
        s.close()
    resp = client.get(f"/api/runs/{run['run_id']}/results")
    sandboxes = {s["sandbox_id"]: s for s in resp.json()["sandboxes"]}
    assert sandboxes[str(run["sb1"])]["previewable"] is False
