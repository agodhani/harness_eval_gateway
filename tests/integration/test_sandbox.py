"""F4 sandbox runtime integration tests — require a live Docker daemon.

Marked ``docker``; run with ``uv run pytest -m docker`` or
``uv run pytest tests/integration/test_sandbox.py``.

Uses the ``heg-testapp`` stub image (sandbox/images/testapp/) — NOT
miniloop — so these tests exercise pure container mechanics: internal
network + egress lockdown, env injection, exec, snapshot artifacts.
"""

import contextlib
import json
import logging
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

from sandbox.runtime import SandboxRuntime
from sandbox.spec import SandboxSpec

pytestmark = pytest.mark.docker

REPO_ROOT = Path(__file__).resolve().parents[2]
TESTAPP_IMAGE = "heg-testapp:latest"
TESTAPP_DOCKERFILE = REPO_ROOT / "sandbox" / "images" / "testapp.Dockerfile"
TESTAPP_CONTEXT = REPO_ROOT / "sandbox" / "images" / "testapp"
NETWORK_NAME = "heg-sandbox"
GATEWAY_CONTAINER = "harness_eval_gateway-gateway-1"


def _docker(*args: str, check: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args], capture_output=True, text=True, check=check
    )


@pytest.fixture(autouse=True)
def _require_docker() -> None:
    proc = subprocess.run(
        ["docker", "info"], capture_output=True, timeout=30, check=False
    )
    if proc.returncode != 0:
        pytest.skip("docker daemon unavailable")


@pytest.fixture(scope="session")
def testapp_image() -> str:
    """Build heg-testapp:latest once per session."""
    proc = _docker(
        "build",
        "-f",
        str(TESTAPP_DOCKERFILE),
        "-t",
        TESTAPP_IMAGE,
        str(TESTAPP_CONTEXT),
    )
    if proc.returncode != 0:
        pytest.fail(
            f"failed to build {TESTAPP_IMAGE}:\n{proc.stderr[-2000:]}"
        )
    return TESTAPP_IMAGE


@pytest.fixture()
def runtime() -> SandboxRuntime:
    return SandboxRuntime()


@contextlib.contextmanager
def _sandbox(
    runtime: SandboxRuntime,
    image: str,
    *,
    env: dict[str, str] | None = None,
    cmd: list[str] | None = None,
    run_id: str | None = None,
) -> Iterator[SandboxSpec]:
    """Spawn a uniquely-named test sandbox; always force-remove it after."""
    sandbox_id = f"s{uuid.uuid4().hex[:10]}"
    rid = run_id or f"r{uuid.uuid4().hex[:10]}"
    ws = Path(tempfile.mkdtemp(prefix="hegws-")).resolve()
    spec = SandboxSpec(
        sandbox_id=sandbox_id,
        run_id=rid,
        image=image,
        env=env or {},
        workspace_host_path=ws,
        cmd=cmd,
    )
    try:
        runtime.spawn(spec)
        yield spec
    finally:
        _docker("rm", "-f", f"heg-{sandbox_id}")
        shutil.rmtree(ws, ignore_errors=True)
        (runtime.state_dir / f"{sandbox_id}.json").unlink(missing_ok=True)


# ----------------------------------------------------------------------
# network
# ----------------------------------------------------------------------


def test_ensure_network_creates_internal_network(runtime: SandboxRuntime) -> None:
    runtime.ensure_network()
    runtime.ensure_network()  # idempotent

    proc = _docker("network", "inspect", NETWORK_NAME, check=True)
    info = json.loads(proc.stdout)[0]
    assert info["Internal"] is True
    assert info["Driver"] == "bridge"


def test_ensure_network_tolerates_missing_gateway(
    runtime: SandboxRuntime, caplog: pytest.LogCaptureFixture
) -> None:
    """Gateway connect must warn-but-not-fail while the gateway container is absent."""
    gateway = os.environ.get("GATEWAY_CONTAINER", GATEWAY_CONTAINER)
    gateway_present = _docker("inspect", gateway).returncode == 0

    with caplog.at_level(logging.WARNING, logger="sandbox.runtime"):
        result = runtime.ensure_network()

    if gateway_present:
        assert result["gateway_attached"] is True
    else:
        assert result["gateway_attached"] is False
        assert any(
            "gateway" in record.message.lower() for record in caplog.records
        )


# ----------------------------------------------------------------------
# spawn / exec / env
# ----------------------------------------------------------------------


def test_spawn_exec_env_and_state_file(
    runtime: SandboxRuntime, testapp_image: str
) -> None:
    with _sandbox(
        runtime,
        testapp_image,
        env={"TEST_VAR": "hi", "GATEWAY_CRED": "sekrit-test-value"},
    ) as spec:
        st = runtime.status(spec.sandbox_id)
        assert st["status"] == "running"
        assert st["running"] is True
        assert st["container"] == f"heg-{spec.sandbox_id}"

        # env injection reached the container
        proc = runtime.exec(
            spec.sandbox_id,
            ["python", "-c", "import os; print(os.environ.get('TEST_VAR', ''))"],
        )
        assert proc.returncode == 0
        assert proc.stdout.strip() == "hi"

        # python works inside the sandbox
        ver = runtime.exec(spec.sandbox_id, ["python", "--version"])
        assert ver.returncode == 0
        assert "Python 3.12" in ver.stdout

        # runtime-injected defaults are present
        gw = runtime.exec(
            spec.sandbox_id,
            ["python", "-c", "import os; print(os.environ.get('GATEWAY_BASE_URL', ''))"],
        )
        assert gw.stdout.strip() == "http://gateway:8080"

        # state file written for debugging, credentials redacted
        state_path = runtime.state_dir / f"{spec.sandbox_id}.json"
        assert state_path.exists()
        state = json.loads(state_path.read_text())
        assert state["sandbox_id"] == spec.sandbox_id
        assert state["run_id"] == spec.run_id
        assert state["container"] == f"heg-{spec.sandbox_id}"
        assert state["env"]["TEST_VAR"] == "hi"
        assert state["env"]["GATEWAY_CRED"] == "***"


# ----------------------------------------------------------------------
# egress lockdown — the key security property
# ----------------------------------------------------------------------


def test_egress_lockdown_blocks_external_http(
    runtime: SandboxRuntime, testapp_image: str
) -> None:
    """Sandbox is on an --internal network: external HTTPS MUST fail."""
    with _sandbox(runtime, testapp_image) as spec:
        proc = runtime.exec(
            spec.sandbox_id,
            [
                "python",
                "-c",
                (
                    "import urllib.request; "
                    "urllib.request.urlopen('https://example.com', timeout=5)"
                ),
            ],
            check=False,
            timeout=60,
        )
        assert proc.returncode != 0, (
            "egress lockdown FAILED: sandbox reached https://example.com "
            f"(stdout={proc.stdout!r} stderr={proc.stderr!r})"
        )
        # confirm it was a network-level failure, not some other error
        assert "Error" in proc.stderr or "error" in proc.stderr


# ----------------------------------------------------------------------
# gateway reachability (soft — gateway container may not exist yet)
# ----------------------------------------------------------------------


def test_gateway_alias_reachable_when_present(
    runtime: SandboxRuntime, testapp_image: str
) -> None:
    gateway = os.environ.get("GATEWAY_CONTAINER", GATEWAY_CONTAINER)
    if gateway not in runtime.attached_containers():
        pytest.skip("gateway container not attached to heg-sandbox yet")

    with _sandbox(runtime, testapp_image) as spec:
        code = (
            "import urllib.request, urllib.error\n"
            "try:\n"
            "    r = urllib.request.urlopen('http://gateway:8080/health', timeout=5)\n"
            "    print(r.status)\n"
            "except urllib.error.HTTPError as e:\n"
            "    print(e.code)\n"
        )
        proc = runtime.exec(
            spec.sandbox_id, ["python", "-c", code], check=False, timeout=30
        )
        if proc.returncode != 0:
            if "Connection refused" in proc.stderr:
                pytest.skip("gateway attached but not serving :8080 yet")
            pytest.fail(
                f"gateway unreachable from sandbox despite network attach:\n"
                f"{proc.stderr}"
            )
        # Any HTTP status means DNS alias + TCP path to gateway:8080 works.
        assert proc.stdout.strip().isdigit()


# ----------------------------------------------------------------------
# snapshot
# ----------------------------------------------------------------------


def test_snapshot_captures_workspace_logs_and_meta(
    runtime: SandboxRuntime, testapp_image: str
) -> None:
    run_id = f"r{uuid.uuid4().hex[:10]}"
    try:
        with _sandbox(runtime, testapp_image, run_id=run_id) as spec:
            name = f"heg-{spec.sandbox_id}"

            # write a probe file into the (bind-mounted) workspace
            runtime.exec(
                spec.sandbox_id,
                ["sh", "-c", "echo hi > /workspace/probe.txt"],
            )

            # wait until the stub app's JSONL events have hit docker logs
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                logs = _docker("logs", name).stdout
                if "test_start" in logs:
                    break
                time.sleep(0.2)
            else:
                pytest.fail("testapp never emitted its startup events")

            # stop but keep the container so docker cp/logs still work
            runtime.stop(spec.sandbox_id, remove=False)
            assert runtime.status(spec.sandbox_id)["status"] == "exited"

            result = runtime.snapshot(spec.sandbox_id, run_id)

            assert result.artifact_dir == (
                runtime.artifact_root / run_id / spec.sandbox_id
            )
            probe = result.workspace_dir / "probe.txt"
            assert probe.read_text().strip() == "hi"

            transcript = result.transcript_path.read_text()
            assert "test_start" in transcript
            assert "agent_done" in transcript

            meta = json.loads(result.meta_path.read_text())
            assert meta["sandbox_id"] == spec.sandbox_id
            assert meta["run_id"] == run_id
            assert meta["container"] == name
            # snapshot records whatever exit code the daemon reports —
            # observed 137 (SIGKILL) on Docker Desktop even for SIGTERM stops.
            assert meta["exit_code"] is not None
            assert meta["finished_at"]
    finally:
        shutil.rmtree(runtime.artifact_root / run_id, ignore_errors=True)


def test_spawn_rejects_duplicate_container_name(
    runtime: SandboxRuntime, testapp_image: str
) -> None:
    with _sandbox(runtime, testapp_image) as spec:
        dup = SandboxSpec(
            sandbox_id=spec.sandbox_id,  # same id -> same container name
            run_id="r-dup",
            image=testapp_image,
            env={},
            workspace_host_path=Path(tempfile.mkdtemp(prefix="hegws-dup-")),
        )
        try:
            with pytest.raises(RuntimeError, match="already exists"):
                runtime.spawn(dup)
        finally:
            shutil.rmtree(dup.workspace_host_path, ignore_errors=True)
