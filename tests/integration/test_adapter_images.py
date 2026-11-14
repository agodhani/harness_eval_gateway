"""Phase 2.5 CLI-harness adapter image integration tests — need Docker.

Marked ``docker``; run with ``uv run pytest -m docker``.

Per adapter image this proves, inside the real sandbox container:

1. The pinned CLI binary is installed and runnable.
2. ``python3 -m adapters.<name>`` (the shim) starts, writes the harness's
   native config, and ends with a ``HEG_RESULT`` marker — even with no
   reachable upstream the shim must report a terminal state, not hang.

Images are built once per session from the repo Dockerfiles; each test
skips when the build fails (e.g. npm registry unreachable) rather than
failing on infra it doesn't control.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

import pytest

from adapters.common.runner import RESULT_MARKER
from sandbox.runtime import SandboxRuntime
from sandbox.spec import SandboxSpec

pytestmark = pytest.mark.docker

REPO_ROOT = Path(__file__).resolve().parents[2]

#: adapter name -> (image tag, CLI binary on PATH)
ADAPTERS = {
    "codex": ("heg-sandbox-codex:latest", "codex"),
    "claude_code": ("heg-sandbox-claude:latest", "claude"),
    "pi": ("heg-sandbox-pi:latest", "pi"),
    "deepseek": ("heg-sandbox-deepseek:latest", "dsh"),
    "aider": ("heg-sandbox-aider:latest", "aider"),
}


def _docker(*args: str, check: bool = False, timeout: int = 600) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args], capture_output=True, text=True, check=check, timeout=timeout
    )


@pytest.fixture(autouse=True)
def _require_docker() -> None:
    proc = subprocess.run(["docker", "info"], capture_output=True, timeout=30, check=False)
    if proc.returncode != 0:
        pytest.skip("docker daemon unavailable")


@pytest.fixture(scope="session")
def built_images() -> dict[str, str]:
    """Build every adapter image once; returns name -> tag (builds may skip)."""
    built: dict[str, str] = {}
    for name, (tag, _cli) in ADAPTERS.items():
        dockerfile = REPO_ROOT / "sandbox" / "images" / f"{name}.Dockerfile"
        proc = _docker(
            "build", "-f", str(dockerfile), "-t", tag, str(REPO_ROOT), timeout=900
        )
        if proc.returncode != 0:
            pytest.skip(
                f"image build failed for {name} (npm registry or docker issue):\n"
                f"{proc.stderr[-1500:]}"
            )
        built[name] = tag
    return built


@pytest.fixture()
def runtime() -> SandboxRuntime:
    return SandboxRuntime()


def _logs(name: str) -> str:
    return _docker("logs", name).stdout + _docker("logs", name).stderr


@pytest.mark.parametrize("name", sorted(ADAPTERS))
def test_cli_binary_installed(built_images: dict[str, str], name: str) -> None:
    tag, cli = ADAPTERS[name]
    if name not in built_images:
        pytest.skip(f"{name} image not built")
    proc = _docker(
        "run", "--rm", "--entrypoint", cli, tag, "--version", check=False, timeout=120
    )
    assert proc.returncode == 0, (
        f"{cli} --version failed in {tag}:\n{proc.stderr[-800:]}"
    )


@pytest.mark.parametrize("name", sorted(ADAPTERS))
def test_shim_reports_terminal_state(
    built_images: dict[str, str], runtime: SandboxRuntime, name: str
) -> None:
    """With no reachable upstream the shim still writes config + reports."""
    if name not in built_images:
        pytest.skip(f"{name} image not built")
    tag = built_images[name]
    sandbox_id = f"s{uuid.uuid4().hex[:10]}"
    ws = Path(tempfile.mkdtemp(prefix="hegws-")).resolve()
    spec = SandboxSpec(
        sandbox_id=sandbox_id,
        run_id=f"r{uuid.uuid4().hex[:10]}",
        image=tag,
        env={
            "GATEWAY_BASE_URL": "http://gateway:8080",
            "GATEWAY_CRED": "heg.test",
            "MODEL_NAME": "no-such-model",
            "TASK_PROMPT": "say hi",
        },
        workspace_host_path=ws,
    )
    try:
        runtime.spawn(spec)
        container = f"heg-{sandbox_id}"
        # The CLI will fail fast (no gateway/invalid model); bound the wait.
        deadline = time.monotonic() + 300
        state = ""
        while time.monotonic() < deadline:
            st = runtime.status(sandbox_id)
            state = st.get("status", "")
            if state in ("exited", "dead"):
                break
            time.sleep(2)
        logs = _logs(container)
        marker = [ln for ln in logs.splitlines() if ln.strip().startswith(RESULT_MARKER)]
        assert marker, f"{name}: no HEG_RESULT marker in logs:\n{logs[-3000:]}"
        payload = json.loads(marker[-1].strip()[len(RESULT_MARKER):])
        assert payload.get("terminal_state") in (
            "error",
            "harness_complete",
            "agent_done",
            "stuck_loop",
        )
    finally:
        _docker("rm", "-f", f"heg-{sandbox_id}")
        shutil.rmtree(ws, ignore_errors=True)
        (runtime.state_dir / f"{sandbox_id}.json").unlink(missing_ok=True)
