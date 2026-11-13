"""F10 artifact-preview integration tests — require a live Docker daemon.

Marked ``docker``; run with ``uv run pytest -m docker`` or
``uv run pytest tests/integration/test_preview.py``.

Exercises the real spawn path: a ``python:3.12-slim`` container running
``python -m http.server`` on the internal ``heg-preview`` network, serving a
fake snapshot workspace over an ephemeral localhost port.
"""

import json
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from sandbox.preview import (
    PREVIEW_NETWORK,
    PreviewManager,
    PreviewPlan,
    detect_preview,
    resolve_workspace_dir,
)
from sandbox.runtime import SandboxRuntime

pytestmark = pytest.mark.docker

PREVIEW_IMAGE = "python:3.12-slim"


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
def preview_image() -> str:
    """Ensure python:3.12-slim is available locally (pull if needed)."""
    if _docker("image", "inspect", PREVIEW_IMAGE).returncode != 0:
        proc = _docker("pull", PREVIEW_IMAGE)
        if proc.returncode != 0:
            pytest.fail(f"failed to pull {PREVIEW_IMAGE}:\n{proc.stderr[-2000:]}")
    return PREVIEW_IMAGE


@pytest.fixture()
def manager(preview_image: str) -> PreviewManager:
    return PreviewManager(
        runtime=SandboxRuntime(), image=preview_image, ttl_seconds=300
    )


@pytest.fixture()
def artifact_ws(tmp_path: Path):
    """A fake artifact store: jail/run/sandbox/workspace with a static site."""
    jail = tmp_path / "artifacts"
    ws = jail / "run1" / "sb1" / "workspace"
    ws.mkdir(parents=True)
    (ws / "index.html").write_text("<h1>artifact preview</h1>")
    (ws / "style.css").write_text("h1{color:red}")
    return {"jail": jail, "ws": ws, "run_id": "run1", "sandbox_id": "sb1"}


def _get(url: str, timeout: float = 5) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")


def _wait_http(url: str, seconds: float = 15) -> tuple[int, str]:
    deadline = time.monotonic() + seconds
    last: tuple[int, str] = (0, "")
    while time.monotonic() < deadline:
        try:
            last = _get(url)
            if last[0] != 0:
                return last
        except OSError:
            pass
        time.sleep(0.3)
    return last


def test_static_preview_serves_workspace(
    manager: PreviewManager, artifact_ws
) -> None:
    """Full lifecycle: detect → spawn → HTTP GET → labels → stop."""
    ws = resolve_workspace_dir(
        jail=artifact_ws["jail"],
        run_id=artifact_ws["run_id"],
        sandbox_id=artifact_ws["sandbox_id"],
        artifact_path=None,
    )
    assert ws is not None
    plan = detect_preview(ws)
    assert plan is not None and plan.type == "static"

    preview = manager.spawn(
        run_id=artifact_ws["run_id"],
        sandbox_id=artifact_ws["sandbox_id"],
        serve_dir=ws,
        plan=plan,
    )
    try:
        assert preview.port is not None
        assert preview.url is not None
        code, body = _wait_http(preview.url)
        assert code == 200, f"preview returned {code}: {body[:300]}"
        assert "artifact preview" in body
        # other workspace files are served too
        code2, css = _wait_http(f"http://localhost:{preview.port}/style.css")
        assert code2 == 200 and "color" in css

        assert preview.sandbox_id == "sb1"
        assert preview.expires_at is not None
        # rediscovered via label-scoped listing (post-restart path)
        again = manager.get_for_sandbox("sb1")
        assert again is not None and again.preview_id == preview.preview_id
    finally:
        assert manager.stop(preview.preview_id)
    assert manager.get(preview.preview_id) is None


def test_preview_port_is_localhost_only(
    manager: PreviewManager, artifact_ws
) -> None:
    """The published port must bind 127.0.0.1, never a LAN-visible address.

    (Docker can't publish ports on --internal networks — verified: the binding
    is silently dropped — so heg-preview is a dedicated bridge. Outbound
    masquerade is disabled as best-effort egress control; on Docker Desktop
    the container may still technically egress, which is why the container
    runs nobody + read-only + cap-drop and serves a pinned http.server with
    no artifact code executing inside.)
    """
    ws = artifact_ws["ws"]
    preview = manager.spawn(
        run_id="r",
        sandbox_id="s",
        serve_dir=ws,
        plan=PreviewPlan(type="static", entry="index.html"),
    )
    try:
        info = json.loads(_docker("inspect", preview.container).stdout)[0]
        bindings = info["NetworkSettings"]["Ports"]["8000/tcp"]
        assert bindings, "port binding missing — network must not be internal"
        assert {b["HostIp"] for b in bindings} == {"127.0.0.1"}

        net = json.loads(_docker("network", "inspect", PREVIEW_NETWORK).stdout)[0]
        assert net["Internal"] is False
        assert (
            net["Options"].get("com.docker.network.bridge.enable_ip_masquerade")
            == "false"
        )
        # container's only network is the dedicated preview bridge
        assert set(info["NetworkSettings"]["Networks"]) == {PREVIEW_NETWORK}
    finally:
        manager.stop(preview.preview_id)


def test_reap_expired_removes_container(
    manager: PreviewManager, artifact_ws
) -> None:
    """A preview already past its TTL is force-removed by the reaper."""
    preview = manager.spawn(
        run_id="r",
        sandbox_id="s",
        serve_dir=artifact_ws["ws"],
        plan=PreviewPlan(type="static", entry="index.html"),
        ttl_seconds=-1,  # already expired
    )
    try:
        assert manager.reap_expired() == 1
        assert manager.get(preview.preview_id) is None
    finally:
        manager.stop(preview.preview_id)  # belt: tolerate already-removed
        shutil.rmtree(artifact_ws["jail"], ignore_errors=True)
