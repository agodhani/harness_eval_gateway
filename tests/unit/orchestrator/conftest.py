"""Shared fixtures for orchestrator unit tests.

Hermetic: file-backed sqlite (tmp_path) + FakeRuntime — NO real docker, NO
real gateway. ``settings`` is a SimpleNamespace carrying every attribute the
orchestrator modules read (GATEWAY_URL, GATEWAY_CRED_SECRET, RUBRIC_MODEL,
SANDBOX_IMAGE, MONITOR_POLL_SECONDS, EVAL_TIMEOUT_SECONDS, EVAL_TOKEN_BUDGET,
HOST_DATA_ROOT, SKILLPACKS_DIR, ARTIFACT_ROOT).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from db.models import Base, Run, Sandbox
from db.session import make_engine, make_session_factory
from sandbox.runtime import SandboxRuntime, SnapshotResult, SpawnResult


@pytest.fixture()
def session_factory(tmp_path: Path):
    """Isolated sqlite DB per test (file-backed: monitor threads are safe)."""
    engine = make_engine(f"sqlite:///{tmp_path}/orch.db")
    Base.metadata.create_all(engine)
    sf = make_session_factory(engine)
    yield sf
    engine.dispose()


@pytest.fixture()
def skillpacks_dir(tmp_path: Path) -> Path:
    """Fake vendored packs: two dirs + a non-pack file."""
    root = tmp_path / "skillpacks"
    for pack in ("pack-a", "pack-b"):
        d = root / pack / "skills" / "demo"
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(
            f"---\nname: demo-{pack}\ndescription: demo skill {pack}\n---\nbody\n"
        )
    (root / "README.md").write_text("not a pack")
    return root


@pytest.fixture()
def settings(tmp_path: Path, skillpacks_dir: Path) -> SimpleNamespace:
    return SimpleNamespace(
        DATABASE_URL="sqlite:///:memory:",
        GATEWAY_URL="http://gateway.test:8080",
        GATEWAY_CRED_SECRET="unit-test-secret",
        RUBRIC_MODEL="",
        SANDBOX_IMAGE="heg-test:latest",
        MONITOR_POLL_SECONDS=0.01,
        EVAL_TIMEOUT_SECONDS=5.0,
        EVAL_TOKEN_BUDGET=200_000,
        RUBRIC_MAX_TOKENS=8192,
        EVAL_JUDGE_MAX_TOKENS=8192,
        HOST_DATA_ROOT="",
        SKILLPACKS_DIR=skillpacks_dir,
        ARTIFACT_ROOT=tmp_path / "artifacts",
    )


@pytest.fixture()
def artifact_root(tmp_path: Path) -> Path:
    return tmp_path / "artifacts"


@pytest.fixture()
def runtime(artifact_root: Path) -> SandboxRuntime:
    """Real SandboxRuntime (paths only — docker methods are never called)."""
    return SandboxRuntime(artifact_root=artifact_root)


@pytest.fixture()
def fake_runtime(artifact_root: Path) -> FakeRuntime:
    return FakeRuntime(artifact_root=artifact_root)


def make_run(
    session_factory,
    *,
    status: str = "provisioning",
    task_prompt: str = "create hello.txt",
    model_name: str = "test-model",
    config: dict[str, Any] | None = None,
) -> Run:
    with session_factory() as s:
        run = Run(
            status=status,
            task_prompt=task_prompt,
            model_name=model_name,
            config=config or {},
        )
        s.add(run)
        s.commit()
        return run


def run_config(
    *,
    harness_configs: list[dict[str, Any]] | None = None,
    budgets: dict[str, Any] | None = None,
    evaluator_model: str | None = "eval-model",
) -> dict[str, Any]:
    return {
        "harness_configs": harness_configs
        or [{"adapter": "miniloop", "skill_packs": [], "system_prompt_extra": None}],
        "evaluator_model": evaluator_model,
        "budgets": budgets
        or {"max_iters": 10, "token_budget": 100_000, "wall_clock_seconds": 3600},
        "same_model_eval": False,
    }


def make_sandbox(
    session_factory,
    run_id: uuid.UUID,
    *,
    status: str = "running",
    harness_config: dict[str, Any] | None = None,
    started_at: datetime | None = None,
    terminal_reason: str | None = None,
    artifact_path: str | None = None,
) -> Sandbox:
    with session_factory() as s:
        sb = Sandbox(
            run_id=run_id,
            harness_config=harness_config or {"adapter": "miniloop"},
            status=status,
            started_at=started_at or datetime.now(UTC),
            terminal_reason=terminal_reason,
            artifact_path=artifact_path,
        )
        s.add(sb)
        s.commit()
        return sb


class FakeRuntime:
    """Scriptable SandboxRuntime stand-in — no docker.

    - ``spawn`` records specs; raises RuntimeError for sandbox_ids in
      ``spawn_fail_ids`` (or when ``fail_first_spawn`` consumes the first call).
    - ``status`` returns ``status_map[sid]`` — a dict or a zero-arg callable
      returning one (for sequences); default ``exited``/``exit_code``.
    - ``logs`` returns ``logs_map.get(sid, default_logs)``.
    - ``snapshot`` materializes a real artifact dir under artifact_root so
      artifact-row sizing works.
    """

    def __init__(
        self,
        *,
        artifact_root: Path,
        state_dir: Path | None = None,
        spawn_fail_ids: set[str] | None = None,
        fail_first_spawn: bool = False,
        status_map: dict[str, Any] | None = None,
        exit_codes: dict[str, int] | None = None,
        logs_map: dict[str, str] | None = None,
        default_logs: str = "",
        snapshot_raises: bool = False,
    ) -> None:
        self.artifact_root = Path(artifact_root)
        self.state_dir = Path(state_dir) if state_dir else self.artifact_root.parent / "sandboxes"
        self.spawn_fail_ids = spawn_fail_ids or set()
        self.fail_first_spawn = fail_first_spawn
        self._spawn_count = 0
        self.status_map = status_map or {}
        self.exit_codes = exit_codes or {}
        self.logs_map = logs_map or {}
        self.default_logs = default_logs
        self.snapshot_raises = snapshot_raises
        self.spawned: list[Any] = []
        self.stopped: list[str] = []
        self.removed: list[str] = []
        self.snapshots: list[tuple[str, str]] = []

    # -- network / lifecycle -------------------------------------------------

    def ensure_network(self) -> dict[str, Any]:
        return {"network": "fake", "created": False, "gateway_attached": True}

    def spawn(self, spec: Any) -> SpawnResult:
        self._spawn_count += 1
        if self.fail_first_spawn and self._spawn_count == 1:
            raise RuntimeError("simulated spawn failure")
        if spec.sandbox_id in self.spawn_fail_ids:
            raise RuntimeError(f"simulated spawn failure for {spec.sandbox_id}")
        self.spawned.append(spec)
        return SpawnResult(
            sandbox_id=spec.sandbox_id,
            run_id=spec.run_id,
            container_id=f"cid-{spec.sandbox_id}",
            container_name=f"heg-{spec.sandbox_id}",
            image=spec.image,
        )

    def status(self, sandbox_id: str) -> dict[str, Any]:
        entry = self.status_map.get(sandbox_id, {"status": "exited", "exit_code": 0})
        if callable(entry):
            entry = entry()
        out = dict(entry)
        out.setdefault("sandbox_id", sandbox_id)
        out.setdefault("exit_code", self.exit_codes.get(sandbox_id, out.get("exit_code", 0)))
        out.setdefault("running", out.get("status") == "running")
        return out

    def logs(self, sandbox_id: str, *, tail: int = 200) -> str:
        return self.logs_map.get(sandbox_id, self.default_logs)

    def stop(self, sandbox_id: str, *, remove: bool = True, timeout: int = 10) -> None:
        self.stopped.append(sandbox_id)

    def remove(self, sandbox_id: str) -> None:
        self.removed.append(sandbox_id)

    def remove_run_containers(self, run_id: str) -> int:
        return 0

    # -- artifacts ------------------------------------------------------------

    def snapshot(self, sandbox_id: str, run_id: str) -> SnapshotResult:
        if self.snapshot_raises:
            raise RuntimeError("simulated snapshot failure")
        artifact_dir = self.artifact_root / run_id / sandbox_id
        workspace_dir = artifact_dir / "workspace"
        transcript_path = artifact_dir / "transcript.log"
        meta_path = artifact_dir / "meta.json"
        workspace_dir.mkdir(parents=True, exist_ok=True)
        (workspace_dir / "hello.txt").write_text("hi\n")
        transcript_path.write_text(self.logs_map.get(sandbox_id, self.default_logs))
        meta_path.write_text(f'{{"sandbox_id": "{sandbox_id}"}}\n')
        self.snapshots.append((sandbox_id, run_id))
        return SnapshotResult(
            sandbox_id=sandbox_id,
            run_id=run_id,
            artifact_dir=artifact_dir,
            workspace_dir=workspace_dir,
            transcript_path=transcript_path,
            meta_path=meta_path,
        )

    # NOTE: artifact_root / state_dir are plain attributes — the orchestrator
    # only reads them (lifecycle eval_root derivation, provision path building).
