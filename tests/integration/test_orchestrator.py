"""F2 orchestrator integration test — real Docker sandbox, real miniloop.

Marked ``docker``; skips without a daemon or the ``heg-sandbox:latest``
image. Drives ONE sandbox through the real provisioning + monitor path
(``provision.spawn_one`` → ``monitor.watch_sandbox``) with a tiny iter cap
and wall-clock bound, then asserts the terminal mechanics: sandbox row
terminal, artifact snapshot on disk + Artifact rows, container removed.

The gateway/upstream model may or may not be reachable in a given
environment — ANY terminal_reason is acceptable (agent_done /
harness_complete / error / timeout); what matters is that the pipeline
reaches a terminal state cleanly. A live gateway makes the happy path
(agent_done/harness_complete) likely; without one the agent errors out,
which still exercises spawn → monitor → snapshot → finalize.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from db.models import Artifact, Base, Run, Sandbox
from db.session import make_engine, make_session_factory
from orchestrator import monitor, provision
from sandbox.runtime import SandboxRuntime

pytestmark = pytest.mark.docker

REPO_ROOT = Path(__file__).resolve().parents[2]
SANDBOX_IMAGE = "heg-sandbox:latest"
TERMINAL_REASONS = {
    "agent_done",
    "harness_complete",
    "timeout",
    "budget_exceeded",
    "stuck_loop",
    "error",
}


def _docker(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args], capture_output=True, text=True, check=False
    )


@pytest.fixture(autouse=True)
def _require_docker_and_image() -> None:
    if _docker("info").returncode != 0:
        pytest.skip("docker daemon unavailable")
    if _docker("image", "inspect", SANDBOX_IMAGE).returncode != 0:
        pytest.skip(f"{SANDBOX_IMAGE} image not built")


@pytest.fixture()
def env(tmp_path: Path):
    engine = make_engine(f"sqlite:///{tmp_path}/orch-integration.db")
    Base.metadata.create_all(engine)
    sf = make_session_factory(engine)
    settings = SimpleNamespace(
        DATABASE_URL="sqlite:///:memory:",
        GATEWAY_URL="http://gateway.test:8080",
        GATEWAY_CRED_SECRET=os.environ.get(
            "GATEWAY_CRED_SECRET", "dev-secret-change-me"
        ),
        RUBRIC_MODEL="",
        SANDBOX_IMAGE=SANDBOX_IMAGE,
        MONITOR_POLL_SECONDS=2.0,
        EVAL_TIMEOUT_SECONDS=5.0,
        EVAL_TOKEN_BUDGET=200_000,
        HOST_DATA_ROOT="",
        SKILLPACKS_DIR=REPO_ROOT / "skillpacks",
        ARTIFACT_ROOT=tmp_path / "artifacts",
    )
    runtime = SandboxRuntime(
        artifact_root=tmp_path / "artifacts",
        state_dir=tmp_path / "sandboxes",
        host_data_root=None,
    )
    yield SimpleNamespace(
        session_factory=sf, settings=settings, runtime=runtime, tmp_path=tmp_path
    )
    engine.dispose()


async def test_spawn_monitor_finalize_real_sandbox(env) -> None:
    sf, settings, runtime = env.session_factory, env.settings, env.runtime

    with sf() as s:
        run = Run(
            status="provisioning",
            task_prompt="Create hello.txt containing the text hi, then stop.",
            model_name=os.environ.get("ORCH_TEST_MODEL", "qwen3:8b"),
            config={
                "harness_configs": [
                    {"adapter": "miniloop", "skill_packs": [], "system_prompt_extra": None}
                ],
                "evaluator_model": "eval-model",
                "budgets": {
                    "max_iters": 2,
                    "token_budget": 200_000,
                    "wall_clock_seconds": 240,
                },
            },
        )
        s.add(run)
        s.commit()
        run_id = run.id

    try:
        sid = await provision.spawn_one(
            session_factory=sf,
            runtime=runtime,
            run_id=run_id,
            harness_config={"adapter": "miniloop", "skill_packs": []},
            settings=settings,
        )
        assert sid is not None, "spawn_one failed — check docker + image"

        # container is really up on the internal network
        st = runtime.status(str(sid))
        assert st["status"] == "running"
        assert f"heg-{sid}" in _docker("ps", "--format", "{{.Names}}").stdout

        reason = await monitor.watch_sandbox(
            session_factory=sf,
            runtime=runtime,
            settings=settings,
            run_id=run_id,
            sandbox_id=sid,
            budgets={
                "max_iters": 2,
                "token_budget": 200_000,
                "wall_clock_seconds": 240,
            },
        )
        assert reason in TERMINAL_REASONS

        with sf() as s:
            sb = s.get(Sandbox, sid)
            assert sb.status in ("done", "failed")
            assert sb.terminal_reason == reason
            assert sb.finished_at is not None
            assert sb.artifact_path is not None
            artifact_dir = Path(sb.artifact_path)
            assert (artifact_dir / "workspace").is_dir()
            assert (artifact_dir / "transcript.log").is_file()
            assert (artifact_dir / "meta.json").is_file()
            kinds = {
                a.kind
                for a in s.execute(
                    select(Artifact).where(Artifact.sandbox_id == sid)
                ).scalars()
            }
            assert {"workspace", "transcript", "meta"} <= kinds

        # container removed after finalize
        assert runtime.status(str(sid))["status"] == "missing"
    finally:
        # best-effort orphan cleanup (label-scoped to this run only)
        runtime.remove_run_containers(str(run_id))
        shutil.rmtree(env.tmp_path / "artifacts", ignore_errors=True)
        shutil.rmtree(env.tmp_path / "sandboxes", ignore_errors=True)
