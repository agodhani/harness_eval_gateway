"""PR24 — POST /api/uploads (task-input archives) + task_inputs on runs.

Hermetic: uploads stage under a tmp UPLOADS_DIR (never the repo data/);
run-create tests stub the orchestrator entry points exactly like
test_runs.py (no gateway/docker). The archive builders are real zip/tar
bytes — member-safety behavior is exercised through the API surface.
"""

from __future__ import annotations

import io
import os
import tarfile
import uuid
import zipfile
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
from api.routers import uploads as uploads_router
from db.models import Base, Rubric, Run
from db.session import make_engine, make_session_factory
from orchestrator import lifecycle as lifecycle_mod
from orchestrator import rubric as rubric_mod
from orchestrator import runner, state_machine
from sandbox import seeds

DIMS = ["functional_requirements"]


def _rubric_content() -> dict[str, Any]:
    return {
        "summary": "scores",
        "dimensions": [
            {
                "name": d,
                "title": d,
                "description": "d",
                "weight": 1.0,
                "min_score": 0.0,
                "max_score": 10.0,
            }
            for d in DIMS
        ],
        "criteria": [],
        "generated_by": {},
    }


def _zip_bytes(members: dict[str, bytes | str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in members.items():
            data = content.encode() if isinstance(content, str) else content
            zf.writestr(name, data)
    return buf.getvalue()


def _targz_bytes(members: dict[str, bytes | str], *, root: str) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        top = tarfile.TarInfo(root)
        top.type = tarfile.DIRTYPE
        tf.addfile(top)
        for name, content in members.items():
            data = content.encode() if isinstance(content, str) else content
            ti = tarfile.TarInfo(f"{root}/{name}")
            ti.size = len(data)
            tf.addfile(ti, io.BytesIO(data))
    return buf.getvalue()


@pytest.fixture()
def session_factory(tmp_path: Path):
    engine = make_engine(f"sqlite:///{tmp_path}/uploads.db")
    Base.metadata.create_all(engine)
    sf = make_session_factory(engine)
    yield sf
    engine.dispose()


@pytest.fixture()
def api(session_factory, tmp_path: Path, monkeypatch):
    """TestClient + tmp data roots; orchestrator entry points stubbed."""
    ns = SimpleNamespace(
        DATABASE_URL="sqlite:///:memory:",
        GATEWAY_URL="http://gateway.test:8080",
        GATEWAY_CRED_SECRET="unit-test-secret",
        RUBRIC_MODEL="",
        SANDBOX_IMAGE="heg-test:latest",
        MONITOR_POLL_SECONDS=0.01,
        EVAL_TIMEOUT_SECONDS=5.0,
        HOST_DATA_ROOT="",
        SKILLPACKS_DIR=tmp_path / "skillpacks",
        ARTIFACT_ROOT=tmp_path / "data" / "artifacts",
        UPLOADS_DIR=tmp_path / "data" / "uploads",
        UPLOAD_MAX_BYTES=1024 * 1024,  # 1 MiB — small enough to trip 413 cheaply
        # Seed member caps small enough to trip 422s with in-test archives.
        SEED_MAX_BYTES=2048,
        SEED_MAX_FILES=64,
    )
    (tmp_path / "skillpacks").mkdir()

    async def fake_draft_rubric(run_id, **_kwargs):
        with session_factory() as s:
            run = s.get(Run, run_id)
            if run is None or run.status != "submitted":
                return
            state_machine.transition(s, run, "rubric_review")
            s.add(Rubric(run_id=run.id, version=1, status="draft", content=_rubric_content()))
            s.commit()

    async def fake_run_lifecycle(run_id, **_kwargs):
        return None

    # every module that resolves get_settings() from its own globals
    # (results stays on real settings — it needs no tmp roots for the
    # task_inputs echo and reads real attrs like PREVIEW_SCAN_TTL_SECONDS).
    monkeypatch.setattr(runs_router, "get_settings", lambda: ns)
    monkeypatch.setattr(uploads_router, "get_settings", lambda: ns)
    monkeypatch.setattr(runner, "_session_factory", lambda: session_factory)
    monkeypatch.setattr(runner, "_settings", lambda: ns)
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
    yield SimpleNamespace(client=client, session_factory=session_factory, settings=ns)
    app.dependency_overrides.pop(get_db, None)


@pytest.fixture(autouse=True)
def _modelcheck_offline(monkeypatch):
    async def _offline(settings, provider):
        return None

    monkeypatch.setattr(modelcheck, "_fetch_catalog", _offline)
    modelcheck.reset_catalog_cache()
    yield
    modelcheck.reset_catalog_cache()


def _run_body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "task_prompt": "fix the off-by-one in parser.py",
        "model_name": "qwen3:8b",
        "harness_configs": [{"adapter": "miniloop", "skill_packs": []}],
        "evaluator_model": "eval-model",
        "budgets": {"max_iters": 10, "token_budget": 50_000, "wall_clock_seconds": 600},
    }
    body.update(overrides)
    return body


def _post_zip(client: TestClient, members: dict[str, bytes | str], filename="seed.zip"):
    return client.post(
        "/api/uploads",
        files={"file": (filename, _zip_bytes(members), "application/zip")},
    )


# ---------------------------------------------------------------------------
# POST /api/uploads
# ---------------------------------------------------------------------------


def test_upload_zip_stages_and_get_returns_meta(api) -> None:
    resp = _post_zip(api.client, {"app.py": "print(1)", "docs/readme.md": "hi"})
    assert resp.status_code == 201, resp.text
    out = resp.json()
    upload_id = out["id"]
    uuid.UUID(upload_id)  # well-formed id
    assert out["filename"] == "seed.zip"
    assert out["kind"] == "zip"
    assert out["size"] > 0
    assert len(out["sha256"]) == 64

    # staged under <UPLOADS_DIR>/<id>/ — jail-contained
    root = Path(api.settings.UPLOADS_DIR)
    assert (root / upload_id / "archive.zip").is_file()
    meta = seeds.read_upload_meta(root, upload_id)
    assert meta and meta["filename"] == "seed.zip"

    got = api.client.get(f"/api/uploads/{upload_id}")
    assert got.status_code == 200
    assert got.json()["sha256"] == out["sha256"]
    assert got.json()["kind"] == "zip"


def test_upload_targz_detects_tar_kind(api) -> None:
    resp = api.client.post(
        "/api/uploads",
        files={
            "file": (
                "repo.tar.gz",
                _targz_bytes({"a.py": "1"}, root="proj"),
                "application/gzip",
            )
        },
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["kind"] == "tar"


def test_upload_rejects_bad_suffix_and_missing_name(api) -> None:
    resp = api.client.post(
        "/api/uploads",
        files={"file": ("notes.txt", b"plain text", "text/plain")},
    )
    assert resp.status_code == 422
    assert "unsupported archive type" in resp.json()["detail"]


def test_upload_rejects_traversal_members_and_keeps_nothing(api) -> None:
    resp = _post_zip(api.client, {"../evil.py": "x", "ok.py": "y"})
    assert resp.status_code == 422, resp.text
    assert "unsafe archive" in resp.json()["detail"]
    root = Path(api.settings.UPLOADS_DIR)
    staged = list(root.iterdir()) if root.is_dir() else []
    assert staged == []  # rejected uploads leave no staged dir


def test_upload_rejects_absolute_and_special_members(api) -> None:
    resp = _post_zip(api.client, {"/etc/passwd": "x"})
    assert resp.status_code == 422

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        data = b"x"
        ti = tarfile.TarInfo("real.py")
        ti.size = len(data)
        tf.addfile(ti, io.BytesIO(data))
        link = tarfile.TarInfo("link")
        link.type = tarfile.SYMTYPE
        link.linkname = "real.py"
        tf.addfile(link)
    resp = api.client.post(
        "/api/uploads",
        files={"file": ("s.tar", buf.getvalue(), "application/x-tar")},
    )
    assert resp.status_code == 422


def test_upload_rejects_members_over_seed_caps(api) -> None:
    """A ≤UPLOAD_MAX_BYTES archive whose DECLARED member sizes exceed the
    seed caps 422s at upload — the same caps extraction enforces, so a
    staged upload can never fail at provisioning."""
    big = _zip_bytes({"blob.bin": b"x" * 4096})
    assert len(big) < api.settings.UPLOAD_MAX_BYTES  # passes the raw cap
    resp = api.client.post(
        "/api/uploads",
        files={"file": ("big.zip", big, "application/zip")},
    )
    assert resp.status_code == 422
    assert "size cap" in resp.json()["detail"]
    root = Path(api.settings.UPLOADS_DIR)
    staged = list(root.iterdir()) if root.is_dir() else []
    assert staged == []  # nothing kept


def test_upload_over_cap_is_413(api) -> None:
    """The cap counts RAW upload bytes (pre-extraction) — incompressible
    random content keeps the multipart file itself over the 1 MiB test cap."""
    big = _zip_bytes({"blob.bin": os.urandom(1024 * 1024 + 64)})
    assert len(big) > api.settings.UPLOAD_MAX_BYTES
    resp = api.client.post(
        "/api/uploads",
        files={"file": ("big.zip", big, "application/zip")},
    )
    assert resp.status_code == 413
    root = Path(api.settings.UPLOADS_DIR)
    staged = list(root.iterdir()) if root.is_dir() else []
    assert staged == []  # nothing kept


def test_upload_404_for_unknown_id(api) -> None:
    resp = api.client.get(f"/api/uploads/{uuid.uuid4()}")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# task_inputs on POST /api/runs
# ---------------------------------------------------------------------------


def _uploaded_id(api, members=None) -> str:
    resp = _post_zip(api.client, members or {"app.py": "print(1)"})
    assert resp.status_code == 201
    return resp.json()["id"]


def test_create_run_with_archive_task_inputs(api) -> None:
    upload_id = _uploaded_id(api)
    resp = api.client.post(
        "/api/runs",
        json=_run_body(task_inputs={"archive_id": upload_id}),
    )
    assert resp.status_code == 201, resp.text
    cfg = resp.json()["config"]
    assert cfg["task_inputs"] == {"archive_id": upload_id}


def test_create_run_with_repo_task_inputs(api) -> None:
    resp = api.client.post(
        "/api/runs",
        json=_run_body(
            task_inputs={
                "repo_url": "https://github.com/acme/widget",
                "ref": "v1.0",
            }
        ),
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["config"]["task_inputs"] == {
        "repo_url": "https://github.com/acme/widget",
        "ref": "v1.0",
    }


def test_create_run_dangling_archive_id_422(api) -> None:
    resp = api.client.post(
        "/api/runs",
        json=_run_body(task_inputs={"archive_id": str(uuid.uuid4())}),
    )
    assert resp.status_code == 422
    assert "does not match a staged upload" in resp.json()["detail"]


@pytest.mark.parametrize(
    "task_inputs",
    [
        {"repo_url": "https://github.com/a/b", "archive_id": str(uuid.uuid4())},  # both
        {"ref": "v1"},  # neither source
        {"repo_url": "git://github.com/a/b"},  # non-https scheme
        {"repo_url": "ssh://git@github.com/a/b"},
        {"repo_url": "https://user:pw@github.com/a/b"},  # embedded credentials
        {"repo_url": "https://github.com/a/b", "ref": "--tags"},  # option-shaped ref
        {"repo_url": "https://github.com/a/b", "ref": "has space"},
        {"archive_id": "not-a-uuid"},
        {"archive_id": str(uuid.uuid4()), "ref": "v1"},  # ref w/o repo
    ],
)
def test_create_run_task_inputs_shape_422(api, task_inputs) -> None:
    resp = api.client.post("/api/runs", json=_run_body(task_inputs=task_inputs))
    assert resp.status_code == 422, resp.text


def test_create_run_no_task_inputs_keeps_legacy_config(api) -> None:
    resp = api.client.post("/api/runs", json=_run_body())
    assert resp.status_code == 201
    assert "task_inputs" not in resp.json()["config"]


def test_run_detail_echoes_task_inputs(api) -> None:
    upload_id = _uploaded_id(api)
    run = api.client.post(
        "/api/runs",
        json=_run_body(task_inputs={"archive_id": upload_id}),
    ).json()
    got = api.client.get(f"/api/runs/{run['id']}")
    assert got.status_code == 200
    assert got.json()["config"]["task_inputs"]["archive_id"] == upload_id


def test_results_echoes_task_inputs(api, session_factory) -> None:
    """Results read-API surfaces the seed spec (per-cell proof rides each
    sandbox's harness_config.seed_sha — covered by provision tests)."""
    run = api.client.post(
        "/api/runs",
        json=_run_body(
            task_inputs={
                "repo_url": "https://github.com/acme/widget",
                "ref": "abc123",
            }
        ),
    ).json()
    # Drive the run to a terminal-ish state directly (lifecycle is stubbed):
    # results only needs the run row + config.
    with session_factory() as s:
        r = s.execute(select(Run).where(Run.id == uuid.UUID(run["id"]))).scalar_one()
        r.status = "done"
        s.commit()
    resp = api.client.get(f"/api/runs/{run['id']}/results")
    assert resp.status_code == 200, resp.text
    assert resp.json()["task_inputs"] == {
        "repo_url": "https://github.com/acme/widget",
        "ref": "abc123",
    }
