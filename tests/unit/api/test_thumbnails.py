"""PR4 artifact-thumbnail endpoint tests.

Runs against in-memory sqlite via the shared conftest app. The Thumbnailer
is replaced with a fake (no docker/chromium); ARTIFACT_ROOT is monkeypatched
to a tmp dir per test — the same jail pattern as test_preview.py. The
PreviewManager fake is reused from test_preview for the purpose-filtering
case (a live thumb-purpose stack must not satisfy start_preview's reuse).
"""

from __future__ import annotations

import json
import uuid
from dataclasses import replace
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
from sandbox.preview import PreviewPlan
from sandbox.thumbnails import (
    PNG_MAGIC,
    THUMB_FILENAME,
    THUMB_META_FILENAME,
    ThumbnailError,
    ThumbResult,
)
from tests.unit.api.test_preview import FakeManager, _preview, _workspace

PNG_BYTES = PNG_MAGIC + b"\x00" * 64


@pytest.fixture()
def session_factory() -> sessionmaker[Session]:
    sf = get_session_factory()
    Base.metadata.create_all(sf.kw["bind"])
    return sf


@pytest.fixture()
def run(session_factory: sessionmaker[Session]):
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


@pytest.fixture()
def artifact_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point both routers' ARTIFACT_ROOT jail at a tmp dir."""

    def fake_settings() -> SimpleNamespace:
        return SimpleNamespace(
            ARTIFACT_ROOT=tmp_path, PREVIEW_SCAN_TTL_SECONDS=60
        )

    monkeypatch.setattr(preview_router, "get_settings", fake_settings)
    monkeypatch.setattr(results_router, "get_settings", fake_settings)
    results_router.reset_scan_cache()
    return tmp_path


class FakeThumbnailer:
    """Duck-typed Thumbnailer — writes the PNG+meta itself, no docker."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.error: Exception | None = None

    def capture(self, **kw) -> ThumbResult:
        self.calls.append(kw)
        if self.error is not None:
            raise self.error
        artifact_dir: Path = kw["artifact_dir"]
        plan: PreviewPlan = kw["plan"]
        (artifact_dir / THUMB_FILENAME).write_bytes(PNG_BYTES)
        captured = datetime.now(UTC)
        meta = {
            "version": 1,
            "captured_at": captured.isoformat(),
            "type": plan.type,
            "entry": plan.entry,
        }
        (artifact_dir / THUMB_META_FILENAME).write_text(json.dumps(meta))
        return ThumbResult(
            path=artifact_dir / THUMB_FILENAME,
            meta_path=artifact_dir / THUMB_META_FILENAME,
            captured_at=captured,
            type=plan.type,
            entry=plan.entry,
        )


@pytest.fixture()
def fake_thumbnailer(monkeypatch: pytest.MonkeyPatch) -> FakeThumbnailer:
    thumb = FakeThumbnailer()
    monkeypatch.setattr(preview_router, "get_thumbnailer", lambda: thumb)
    return thumb


@pytest.fixture()
def fake_manager(monkeypatch: pytest.MonkeyPatch) -> FakeManager:
    mgr = FakeManager()
    monkeypatch.setattr(preview_router, "get_preview_manager", lambda: mgr)
    return mgr


def _thumb_path(run_id, sandbox_id) -> str:
    return f"/api/runs/{run_id}/sandboxes/{sandbox_id}/thumbnail"


# ---------------------------------------------------------------------------
# POST .../thumbnail
# ---------------------------------------------------------------------------


def test_capture_thumbnail_static(
    client: TestClient, run, artifact_root, fake_thumbnailer: FakeThumbnailer
) -> None:
    ws = _workspace(artifact_root, run["run_id"], run["sb1"], {"index.html": "<h1>x</h1>"})
    resp = client.post(_thumb_path(run["run_id"], run["sb1"]))
    assert resp.status_code == 201
    body = resp.json()
    assert body["url"] == _thumb_path(run["run_id"], run["sb1"])
    assert body["type"] == "static"
    assert body["entry"] == "index.html"
    assert body["captured_at"]

    kw = fake_thumbnailer.calls[0]
    assert kw["workspace"] == ws
    assert kw["artifact_dir"] == ws.parent
    assert kw["plan"] == PreviewPlan(type="static", entry="index.html")
    assert (ws.parent / THUMB_FILENAME).read_bytes() == PNG_BYTES
    meta = json.loads((ws.parent / THUMB_META_FILENAME).read_text())
    assert meta["type"] == "static"


def test_capture_thumbnail_entry_override(
    client: TestClient, run, artifact_root, fake_thumbnailer: FakeThumbnailer
) -> None:
    """{entry, type} picks the captured candidate like POST .../preview."""
    _workspace(
        artifact_root, run["run_id"], run["sb1"], {"a.html": "a", "b.html": "b"}
    )
    resp = client.post(_thumb_path(run["run_id"], run["sb1"]), json={"entry": "b.html"})
    assert resp.status_code == 201
    assert resp.json()["entry"] == "b.html"
    assert fake_thumbnailer.calls[0]["plan"].entry == "b.html"


def test_capture_thumbnail_type_override(
    client: TestClient, run, artifact_root, fake_thumbnailer: FakeThumbnailer
) -> None:
    """{"type": "desktop"} on a both-matching workspace shoots the game."""
    _workspace(
        artifact_root,
        run["run_id"],
        run["sb1"],
        {"index.html": "x", "main.py": "import pygame\n"},
    )
    resp = client.post(_thumb_path(run["run_id"], run["sb1"]), json={"type": "desktop"})
    assert resp.status_code == 201
    assert resp.json()["type"] == "desktop"
    assert fake_thumbnailer.calls[0]["plan"].type == "desktop"


def test_capture_thumbnail_no_workspace(
    client: TestClient, run, artifact_root, fake_thumbnailer: FakeThumbnailer
) -> None:
    resp = client.post(_thumb_path(run["run_id"], run["sb1"]))
    assert resp.status_code == 404
    assert "workspace" in resp.json()["detail"].lower()
    assert fake_thumbnailer.calls == []


def test_capture_thumbnail_not_previewable(
    client: TestClient, run, artifact_root, fake_thumbnailer: FakeThumbnailer
) -> None:
    _workspace(artifact_root, run["run_id"], run["sb1"], {"main.py": "print(1)"})
    resp = client.post(_thumb_path(run["run_id"], run["sb1"]))
    assert resp.status_code == 422
    assert fake_thumbnailer.calls == []


def test_capture_thumbnail_capture_failure(
    client: TestClient, run, artifact_root, fake_thumbnailer: FakeThumbnailer
) -> None:
    _workspace(artifact_root, run["run_id"], run["sb1"], {"index.html": "x"})
    fake_thumbnailer.error = ThumbnailError("chromium exploded")
    resp = client.post(_thumb_path(run["run_id"], run["sb1"]))
    assert resp.status_code == 502
    assert "chromium exploded" in resp.json()["detail"]


def test_capture_thumbnail_404s(
    client: TestClient, run, artifact_root, fake_thumbnailer: FakeThumbnailer
) -> None:
    _workspace(artifact_root, run["run_id"], run["sb1"], {"index.html": "x"})
    resp = client.post(f"/api/runs/{uuid.uuid4()}/sandboxes/{run['sb1']}/thumbnail")
    assert resp.status_code == 404
    resp = client.post(f"/api/runs/{run['run_id']}/sandboxes/{uuid.uuid4()}/thumbnail")
    assert resp.status_code == 404
    assert fake_thumbnailer.calls == []


def test_capture_thumbnail_emits_checkpoint(
    client: TestClient, run, session_factory, artifact_root, fake_thumbnailer
) -> None:
    _workspace(artifact_root, run["run_id"], run["sb1"], {"index.html": "x"})
    resp = client.post(_thumb_path(run["run_id"], run["sb1"]))
    assert resp.status_code == 201
    s = session_factory()
    try:
        events = (
            s.query(Event)
            .filter(Event.run_id == run["run_id"], Event.type == "checkpoint")
            .all()
        )
        stages = [e.payload.get("stage") for e in events]
        assert "thumbnail_captured" in stages
    finally:
        s.close()


# ---------------------------------------------------------------------------
# GET .../thumbnail
# ---------------------------------------------------------------------------


def test_get_thumbnail_serves_png(
    client: TestClient, run, artifact_root
) -> None:
    ws = _workspace(artifact_root, run["run_id"], run["sb1"], {"index.html": "x"})
    (ws.parent / THUMB_FILENAME).write_bytes(PNG_BYTES)
    resp = client.get(_thumb_path(run["run_id"], run["sb1"]))
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("image/png")
    assert resp.content == PNG_BYTES


def test_get_thumbnail_404s(client: TestClient, run, artifact_root) -> None:
    # no artifact dir at all
    resp = client.get(_thumb_path(run["run_id"], run["sb1"]))
    assert resp.status_code == 404
    # workspace exists but no capture yet
    _workspace(artifact_root, run["run_id"], run["sb1"], {"index.html": "x"})
    resp = client.get(_thumb_path(run["run_id"], run["sb1"]))
    assert resp.status_code == 404
    # wrong run
    resp = client.get(f"/api/runs/{uuid.uuid4()}/sandboxes/{run['sb1']}/thumbnail")
    assert resp.status_code == 404


def test_get_thumbnail_jail_blocks_symlink(
    client: TestClient, run, artifact_root, tmp_path_factory
) -> None:
    """A planted thumbnail.png symlink escaping ARTIFACT_ROOT → 404."""
    ws = _workspace(artifact_root, run["run_id"], run["sb1"], {"index.html": "x"})
    outside = tmp_path_factory.mktemp("outside") / "outside.png"
    outside.write_bytes(PNG_BYTES)
    (ws.parent / THUMB_FILENAME).symlink_to(outside)
    resp = client.get(_thumb_path(run["run_id"], run["sb1"]))
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# DELETE .../thumbnail
# ---------------------------------------------------------------------------


def test_delete_thumbnail(
    client: TestClient, run, artifact_root
) -> None:
    ws = _workspace(artifact_root, run["run_id"], run["sb1"], {"index.html": "x"})
    png = ws.parent / THUMB_FILENAME
    meta = ws.parent / THUMB_META_FILENAME
    png.write_bytes(PNG_BYTES)
    meta.write_text("{}")
    resp = client.delete(_thumb_path(run["run_id"], run["sb1"]))
    assert resp.status_code == 204
    assert not png.exists() and not meta.exists()
    # second delete → 404
    resp = client.delete(_thumb_path(run["run_id"], run["sb1"]))
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# results GET — thumbnail ref embedded
# ---------------------------------------------------------------------------


def test_results_embeds_thumbnail(
    client: TestClient, run, artifact_root
) -> None:
    ws = _workspace(artifact_root, run["run_id"], run["sb1"], {"index.html": "x"})
    (ws.parent / THUMB_FILENAME).write_bytes(PNG_BYTES)
    (ws.parent / THUMB_META_FILENAME).write_text(
        json.dumps(
            {
                "version": 1,
                "captured_at": "2026-02-01T10:00:00+00:00",
                "type": "static",
                "entry": "index.html",
            }
        )
    )
    resp = client.get(f"/api/runs/{run['run_id']}/results")
    assert resp.status_code == 200
    sandboxes = {s["sandbox_id"]: s for s in resp.json()["sandboxes"]}
    thumb = sandboxes[str(run["sb1"])]["thumbnail"]
    assert thumb is not None
    assert thumb["url"] == _thumb_path(run["run_id"], run["sb1"])
    assert thumb["type"] == "static"
    assert thumb["entry"] == "index.html"
    assert thumb["captured_at"].startswith("2026-02-01")
    # absent thumbnail → null, not an error
    assert sandboxes[str(run["sb2"])]["thumbnail"] is None


def test_results_thumbnail_without_meta(
    client: TestClient, run, artifact_root
) -> None:
    """A bare thumbnail.png (no sidecar) still surfaces — meta is decor."""
    ws = _workspace(artifact_root, run["run_id"], run["sb1"], {"index.html": "x"})
    (ws.parent / THUMB_FILENAME).write_bytes(PNG_BYTES)
    resp = client.get(f"/api/runs/{run['run_id']}/results")
    thumb = {
        s["sandbox_id"]: s for s in resp.json()["sandboxes"]
    }[str(run["sb1"])]["thumbnail"]
    assert thumb is not None
    assert thumb["url"].endswith("/thumbnail")
    assert thumb["type"] is None and thumb["captured_at"] is None


# ---------------------------------------------------------------------------
# purpose filtering — thumb stacks are invisible to interactive previews
# ---------------------------------------------------------------------------


def test_start_preview_ignores_thumb_purpose(
    client: TestClient, run, artifact_root, fake_manager: FakeManager
) -> None:
    """A live purpose=thumb stack does NOT satisfy the reuse check — the
    interactive POST replaces it (stop + fresh spawn)."""
    thumb = replace(_preview(run["run_id"], run["sb1"]), purpose="thumb")
    fake_manager.previews.append(thumb)
    _workspace(artifact_root, run["run_id"], run["sb1"], {"index.html": "x"})
    resp = client.post(f"/api/runs/{run['run_id']}/sandboxes/{run['sb1']}/preview")
    assert resp.status_code == 201
    assert resp.json()["preview_id"] != thumb.preview_id
    assert thumb.preview_id in fake_manager.stop_calls
    assert len(fake_manager.spawn_calls) == 1


def test_get_preview_ignores_thumb_purpose(
    client: TestClient, run, artifact_root, fake_manager: FakeManager
) -> None:
    """GET .../preview must not surface a mid-flight capture stack —
    the manager's purpose filter hides it (404 = "no preview")."""
    thumb = replace(_preview(run["run_id"], run["sb1"]), purpose="thumb")
    fake_manager.previews.append(thumb)
    resp = client.get(f"/api/runs/{run['run_id']}/sandboxes/{run['sb1']}/preview")
    assert resp.status_code == 404
