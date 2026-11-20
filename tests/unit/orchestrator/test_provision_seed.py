"""PR24 — spawn_one seeds every sandbox workspace from run.config.task_inputs.

Hermetic: file-backed sqlite + FakeRuntime (its spawn honors
``spec.seed_host_path`` exactly like the real SandboxRuntime — copies the
tree into the cell workspace and returns the copy's content hash). The
seed itself is staged as a real upload archive so the full chain runs:
``prepare_run_seed`` → ``seed_host_path`` → per-cell copy →
``harness_config.seed_sha``.
"""

from __future__ import annotations

import io
import json
import tarfile
import uuid
from pathlib import Path

from sqlalchemy import select

from db.models import Event, Sandbox
from orchestrator.provision import spawn_one
from sandbox import seeds

from .conftest import make_run, run_config

UPLOAD_ID = "12345678-1234-5678-9abc-def012345678"


def _seed_archive(tmp_path: Path) -> Path:
    """A real seed archive on disk (what POST /api/uploads stages)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for name, data in {
            "proj/app/main.py": b"print('run me')",
            "proj/docs/spec.md": b"# spec",
            "proj/README.md": b"task input repo",
        }.items():
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            tf.addfile(ti, io.BytesIO(data))
    p = tmp_path / "bundle.tar"
    p.write_bytes(buf.getvalue())
    return p


def _stage_upload(settings, archive: Path) -> None:
    """Stage the archive under the uploads root the run will resolve."""
    uploads = seeds.uploads_root(settings)
    d = uploads / UPLOAD_ID
    d.mkdir(parents=True)
    (d / f"{seeds.UPLOAD_ARCHIVE_NAME}.tar").write_bytes(archive.read_bytes())
    (d / seeds.UPLOAD_META_NAME).write_text(
        json.dumps({"filename": "bundle.tar", "size": 42, "sha256": "abc"})
    )


def _db_sandbox(session_factory, sid: uuid.UUID) -> Sandbox:
    with session_factory() as s:
        return s.get(Sandbox, sid)


def _db_sandbox_from_name(session_factory, run_id: uuid.UUID, name: str) -> Sandbox:
    with session_factory() as s:
        return s.execute(
            select(Sandbox).where(Sandbox.run_id == run_id, Sandbox.name == name)
        ).scalar_one()


async def test_spawn_two_cells_share_identical_seed_sha(
    session_factory, settings, fake_runtime, tmp_path
) -> None:
    """The card's verify: every cell's workspace starts from identical
    bytes, proven by the same ``harness_config.seed_sha`` on each row."""
    _stage_upload(settings, _seed_archive(tmp_path))

    run = make_run(
        session_factory,
        status="provisioning",
        config=run_config(
            harness_configs=[
                {"adapter": "miniloop", "skill_packs": [], "name": "cfg-a"},
                {"adapter": "miniloop", "skill_packs": [], "name": "cfg-b"},
            ]
        )
        | {"task_inputs": {"archive_id": UPLOAD_ID}},
    )
    sids = [
        await spawn_one(
            session_factory=session_factory,
            runtime=fake_runtime,
            run_id=run.id,
            harness_config=hc,
            settings=settings,
        )
        for hc in run.config["harness_configs"]
    ]
    assert all(sids)

    rows = [_db_sandbox(session_factory, sid) for sid in sids]
    shas = [r.harness_config.get("seed_sha") for r in rows]
    assert shas[0] is not None
    assert shas[0] == shas[1]  # identical bytes per cell
    for row in rows:
        assert row.harness_config["seed"]["type"] == "archive"
        assert row.harness_config["seed"]["upload_id"] == UPLOAD_ID

    # each cell's workspace independently holds the seed bytes (common root
    # dir flattened — the workspace mirrors the repo root)
    for spec in fake_runtime.spawned:
        assert (spec.workspace_host_path / "app" / "main.py").read_text() == "print('run me')"
        assert (spec.workspace_host_path / "docs" / "spec.md").is_file()
        assert (spec.workspace_host_path / "README.md").is_file()
        assert not (spec.workspace_host_path / "proj").exists()

    # a `seed` checkpoint event lands per sandbox (timeline provenance)
    with session_factory() as s:
        cps = [
            e
            for e in s.execute(
                select(Event).where(Event.run_id == run.id, Event.type == "checkpoint")
            ).scalars()
            if e.payload.get("stage") == "seed"
        ]
        assert len(cps) == 2
        assert {str(e.sandbox_id) for e in cps} == {str(sid) for sid in sids}
        for e in cps:
            assert e.payload["seed_sha"] == shas[0]
            assert e.payload["type"] == "archive"

    # the materialization itself lives once under <data>/seeds/<run_id>/
    seed_dir = fake_runtime.state_dir.parent / "seeds" / str(run.id) / "content"
    assert (seed_dir / "app" / "main.py").is_file()


async def test_spawn_unseeded_run_has_no_seed_keys(
    session_factory, settings, fake_runtime
) -> None:
    run = make_run(session_factory, status="provisioning", config=run_config())
    sid = await spawn_one(
        session_factory=session_factory,
        runtime=fake_runtime,
        run_id=run.id,
        harness_config={"adapter": "miniloop", "skill_packs": []},
        settings=settings,
    )
    assert sid is not None
    sb = _db_sandbox(session_factory, sid)
    assert "seed" not in sb.harness_config
    assert "seed_sha" not in sb.harness_config
    assert fake_runtime.spawned[0].seed_host_path is None
    with session_factory() as s:
        cps = [
            e
            for e in s.execute(
                select(Event).where(Event.run_id == run.id, Event.type == "checkpoint")
            ).scalars()
            if e.payload.get("stage") == "seed"
        ]
        assert cps == []


async def test_spawn_unresolvable_seed_fails_cell_not_run(
    session_factory, settings, fake_runtime
) -> None:
    """A dangling archive_id fails THAT sandbox (error terminal, sibling
    unaffected) — same isolation as any spawn failure."""
    run = make_run(
        session_factory,
        status="provisioning",
        config=run_config(
            harness_configs=[
                {"adapter": "miniloop", "skill_packs": [], "name": "doomed"},
                {"adapter": "miniloop", "skill_packs": [], "name": "fine"},
            ]
        )
        | {"task_inputs": {"archive_id": "00000000-0000-0000-0000-000000000000"}},
    )
    hcs = run.config["harness_configs"]
    sid1 = await spawn_one(
        session_factory=session_factory, runtime=fake_runtime, run_id=run.id,
        harness_config=hcs[0], settings=settings,
    )
    assert sid1 is None
    failed = _db_sandbox_from_name(session_factory, run.id, "doomed")
    assert failed.status == "failed"
    assert failed.terminal_reason == "error"


async def test_spawn_git_seed_records_resolved_sha(
    session_factory, settings, fake_runtime, monkeypatch, tmp_path
) -> None:
    """repo_url seeds go through the git runner; the provenance records the
    resolved sha so the pin is auditable (subprocess is faked)."""

    def fake_git(argv: list[str]) -> str:
        if argv[2] == "checkout":
            Path(argv[1], "repo.py").write_text("cloned = True")
        if argv[-1] == "HEAD":
            return "f00dcafe\n"
        return ""

    monkeypatch.setattr(seeds, "git_runner", lambda timeout=None: fake_git)

    run = make_run(
        session_factory,
        status="provisioning",
        config=run_config()
        | {
            "task_inputs": {
                "repo_url": "https://github.com/acme/widget",
                "ref": "abc123",
            }
        },
    )
    sid = await spawn_one(
        session_factory=session_factory,
        runtime=fake_runtime,
        run_id=run.id,
        harness_config={"adapter": "miniloop", "skill_packs": []},
        settings=settings,
    )
    assert sid is not None
    sb = _db_sandbox(session_factory, sid)
    seed = sb.harness_config["seed"]
    assert seed["type"] == "repo"
    assert seed["url"] == "https://github.com/acme/widget"
    assert seed["ref"] == "abc123"
    assert seed["resolved_sha"] == "f00dcafe"
    assert sb.harness_config["seed_sha"]
    spec = fake_runtime.spawned[0]
    assert (spec.workspace_host_path / "repo.py").read_text() == "cloned = True"
