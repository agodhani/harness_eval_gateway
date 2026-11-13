"""Provisioning: skillpack staging, env building, cred minting, spawn_one."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from sqlalchemy import select

from db.models import Event, Sandbox
from gateway.creds import verify_cred
from orchestrator import provision
from orchestrator.provision import (
    GATEWAY_INGEST_URL,
    GATEWAY_INTERNAL_URL,
    build_env,
    known_skillpacks,
    mint_sandbox_cred,
    spawn_one,
    stage_skillpacks,
)
from sandbox.runtime import SandboxRuntime

from .conftest import make_run, run_config

# ---------------------------------------------------------------------------
# skillpacks
# ---------------------------------------------------------------------------


def test_known_skillpacks_lists_dirs_only(skillpacks_dir: Path) -> None:
    assert known_skillpacks(skillpacks_dir) == ["pack-a", "pack-b"]
    assert known_skillpacks(skillpacks_dir / "missing") == []


def test_stage_skillpacks_copies_only_selected(
    skillpacks_dir: Path, tmp_path: Path
) -> None:
    dest = stage_skillpacks(["pack-a"], tmp_path / "staged", source_root=skillpacks_dir)
    assert (dest / "pack-a" / "skills" / "demo" / "SKILL.md").is_file()
    assert not (dest / "pack-b").exists()
    # real copy, not symlink
    assert not (dest / "pack-a").is_symlink()


def test_stage_skillpacks_empty_selection_creates_empty_dir(
    skillpacks_dir: Path, tmp_path: Path
) -> None:
    dest = stage_skillpacks([], tmp_path / "staged", source_root=skillpacks_dir)
    assert dest.is_dir()
    assert list(dest.iterdir()) == []


def test_stage_skillpacks_rejects_unknown_and_traversal(
    skillpacks_dir: Path, tmp_path: Path
) -> None:
    with pytest.raises(ValueError, match="unknown skill pack"):
        stage_skillpacks(["nope"], tmp_path / "s", source_root=skillpacks_dir)
    with pytest.raises(ValueError, match="escapes"):
        stage_skillpacks([".."], tmp_path / "s", source_root=skillpacks_dir)


# ---------------------------------------------------------------------------
# env + creds
# ---------------------------------------------------------------------------


def _adapter():
    from orchestrator.adapters import get_adapter

    return get_adapter("miniloop")


def test_build_env_contract() -> None:
    env = build_env(
        _adapter(),
        cred="heg.cred.sig",
        task_prompt="do the thing",
        model_name="qwen3:8b",
        max_iters=7,
        system_prompt_extra="be terse",
    )
    assert env["GATEWAY_BASE_URL"] == GATEWAY_INTERNAL_URL == "http://gateway:8080"
    assert env["GATEWAY_CRED"] == "heg.cred.sig"
    assert env["MODEL_NAME"] == "qwen3:8b"
    assert env["TASK_PROMPT"] == "do the thing"
    assert env["EVENTS_INGEST_URL"] == GATEWAY_INGEST_URL
    assert env["MAX_ITERS"] == "7"
    assert env["SYSTEM_PROMPT_EXTRA"] == "be terse"
    assert env["SKILLPACKS_ROOT"] == "/opt/skillpacks"
    assert "INGEST_TOKEN" not in env  # cred auth suffices for /internal/events


def test_build_env_omits_prompt_extra_when_unset() -> None:
    env = build_env(
        _adapter(), cred="c", task_prompt="t", model_name="m", max_iters=1
    )
    assert "SYSTEM_PROMPT_EXTRA" not in env


def test_mint_sandbox_cred_claims() -> None:
    run_id, sandbox_id = uuid.uuid4(), uuid.uuid4()
    budgets = {"wall_clock_seconds": 100, "token_budget": 50_000}
    cred = mint_sandbox_cred(
        run_id=run_id, sandbox_id=sandbox_id, secret="s3cr3t", budgets=budgets
    )
    claims = verify_cred(cred, secret="s3cr3t")
    assert claims.run_id == str(run_id)
    assert claims.sandbox_id == str(sandbox_id)
    assert claims.max_tokens == 50_000
    # ttl = wall_clock + grace
    assert claims.exp - claims.iat == 100 + provision.CRED_TTL_GRACE_SECONDS


def test_to_host_path_translation(runtime: SandboxRuntime) -> None:
    """host_data_root rewrites data-tree paths for docker -v sources."""
    rt = SandboxRuntime(
        artifact_root="/data/artifacts",
        state_dir="/data/sandboxes",
        host_data_root="/host/checkout/data",
    )
    assert rt.to_host_path("/data/sandboxes/x/workspace") == Path(
        "/host/checkout/data/sandboxes/x/workspace"
    )
    assert rt.to_host_path("/data/artifacts/r/s") == Path(
        "/host/checkout/data/artifacts/r/s"
    )
    # outside the data tree -> verbatim
    assert rt.to_host_path("/opt/skillpacks") == Path("/opt/skillpacks")


def test_to_host_path_identity_without_host_root(runtime: SandboxRuntime) -> None:
    p = Path("/anywhere/data/sandboxes/x")
    assert runtime.to_host_path(p) == p


# ---------------------------------------------------------------------------
# spawn_one
# ---------------------------------------------------------------------------


def _db_sandbox(session_factory, sid: uuid.UUID) -> Sandbox:
    with session_factory() as s:
        return s.get(Sandbox, sid)


async def test_spawn_one_success(session_factory, settings, fake_runtime, tmp_path) -> None:
    run = make_run(
        session_factory,
        status="provisioning",
        config=run_config(
            harness_configs=[
                {"adapter": "miniloop", "skill_packs": ["pack-a"], "system_prompt_extra": "hi"}
            ]
        ),
    )
    sid = await spawn_one(
        session_factory=session_factory,
        runtime=fake_runtime,
        run_id=run.id,
        harness_config=run.config["harness_configs"][0],
        settings=settings,
    )
    assert sid is not None

    sb = _db_sandbox(session_factory, sid)
    assert sb.status == "running"
    assert sb.docker_container_id == f"cid-{sid}"
    assert sb.started_at is not None

    # exactly one spec spawned, with the contract env + paths
    assert len(fake_runtime.spawned) == 1
    spec = fake_runtime.spawned[0]
    assert spec.sandbox_id == str(sid)
    assert spec.run_id == str(run.id)
    assert spec.image == "heg-test:latest"
    assert spec.cmd == ["python", "-m", "miniloop"]
    assert spec.env["MODEL_NAME"] == "test-model"
    assert spec.env["SYSTEM_PROMPT_EXTRA"] == "hi"
    assert spec.env["MAX_ITERS"] == "10"
    cred = spec.env["GATEWAY_CRED"]
    claims = verify_cred(cred, secret="unit-test-secret")
    assert claims.sandbox_id == str(sid)
    assert claims.max_tokens == 100_000

    # workspace + staged skillpacks live under the runtime state dir
    assert spec.workspace_host_path == fake_runtime.state_dir / str(sid) / "workspace"
    assert spec.skillpacks_host_path == fake_runtime.state_dir / str(sid) / "skillpacks"
    assert (spec.skillpacks_host_path / "pack-a" / "skills" / "demo" / "SKILL.md").is_file()
    assert not (spec.skillpacks_host_path / "pack-b").exists()


async def test_spawn_one_empty_packs_stages_empty_dir(
    session_factory, settings, fake_runtime
) -> None:
    run = make_run(session_factory, status="provisioning", config=run_config())
    hc = {"adapter": "miniloop", "skill_packs": [], "system_prompt_extra": None}
    sid = await spawn_one(
        session_factory=session_factory,
        runtime=fake_runtime,
        run_id=run.id,
        harness_config=hc,
        settings=settings,
    )
    assert sid is not None
    spec = fake_runtime.spawned[0]
    assert spec.skillpacks_host_path.is_dir()
    assert list(spec.skillpacks_host_path.iterdir()) == []
    assert "SYSTEM_PROMPT_EXTRA" not in spec.env


async def test_spawn_one_failure_isolated(session_factory, settings, fake_runtime) -> None:
    """A spawn exception marks THAT sandbox failed; siblings still spawn."""
    run = make_run(
        session_factory,
        status="provisioning",
        config=run_config(
            harness_configs=[
                {"adapter": "miniloop", "skill_packs": []},
                {"adapter": "miniloop", "skill_packs": []},
            ]
        ),
    )
    fake_runtime.fail_first_spawn = True
    hcs = run.config["harness_configs"]
    sid1 = await spawn_one(
        session_factory=session_factory, runtime=fake_runtime, run_id=run.id,
        harness_config=hcs[0], settings=settings,
    )
    sid2 = await spawn_one(
        session_factory=session_factory, runtime=fake_runtime, run_id=run.id,
        harness_config=hcs[1], settings=settings,
    )
    assert sid1 is None
    assert sid2 is not None

    # failed row recorded with terminal_reason=error + error event
    with session_factory() as s:
        failed = [
            sb for sb in s.execute(select(Sandbox).where(Sandbox.run_id == run.id)).scalars()
            if sb.status == "failed"
        ]
        assert len(failed) == 1
        assert failed[0].terminal_reason == "error"
        assert failed[0].finished_at is not None
        errs = [
            e for e in s.execute(select(Event).where(Event.run_id == run.id)).scalars()
            if e.type == "error"
        ]
        assert errs and errs[0].payload["stage"] == "spawn"


async def test_spawn_one_unknown_adapter_fails_sandbox(
    session_factory, settings, fake_runtime
) -> None:
    run = make_run(session_factory, status="provisioning", config=run_config())
    sid = await spawn_one(
        session_factory=session_factory,
        runtime=fake_runtime,
        run_id=run.id,
        harness_config={"adapter": "bogus-adapter", "skill_packs": []},
        settings=settings,
    )
    assert sid is None
    assert fake_runtime.spawned == []
    with session_factory() as s:
        row = s.execute(
            select(Sandbox).where(Sandbox.run_id == run.id)
        ).scalar_one()
        assert row.status == "failed"


async def test_spawn_one_missing_run_returns_none(session_factory, settings, fake_runtime) -> None:
    assert (
        await spawn_one(
            session_factory=session_factory,
            runtime=fake_runtime,
            run_id=uuid.uuid4(),
            harness_config={"adapter": "miniloop"},
            settings=settings,
        )
        is None
    )
