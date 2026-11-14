"""F12 artifact-preview integration tests — require a live Docker daemon.

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


# ---------------------------------------------------------------------------
# desktop previews (executes candidate code — internal net + socat sidecar)
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[2]
DESKTOP_IMAGE = "heg-preview-desktop:latest"
SERVER_IMAGE = "heg-preview-server:latest"
PROXY_IMAGE = "alpine/socat:1.8.1.3"
DESKTOP_DOCKERFILE = REPO_ROOT / "sandbox/images/desktop-preview.Dockerfile"
SERVER_DOCKERFILE = REPO_ROOT / "sandbox/images/server-preview.Dockerfile"

_PYGAME_APP = """\
import os, time
os.environ.setdefault("SDL_VIDEODRIVER", "x11")
import pygame
pygame.init()
screen = pygame.display.set_mode((320, 240))
screen.fill((20, 120, 60))
print("PYGAME_OK", flush=True)
end = time.time() + 45
while time.time() < end:
    for e in pygame.event.get():
        pass
    pygame.display.flip()
    time.sleep(0.05)
"""


def _build_image(dockerfile: Path, tag: str) -> str:
    """``docker build`` unconditionally — the layer cache makes repeat
    builds cheap, and it guarantees the image matches the CURRENT
    Dockerfile (a stale local tag would silently skip new baked deps).

    First build is heavy (apt + pip); if your daemon lacks network for the
    build, `docker build` manually first:
      docker build -f sandbox/images/<name>-preview.Dockerfile \\
          -t <tag> .
    """
    proc = subprocess.run(
        [
            "docker",
            "build",
            "-f",
            str(dockerfile),
            "-t",
            tag,
            str(REPO_ROOT),
        ],
        capture_output=True,
        text=True,
        timeout=1800,
        check=False,
    )
    if proc.returncode != 0:
        pytest.fail(f"failed to build {tag}:\n{proc.stderr[-3000:]}")
    return tag


@pytest.fixture(scope="session")
def desktop_image() -> str:
    """heg-preview-desktop: TigerVNC + noVNC + pygame-ce/pyglet/arcade/tkinter."""
    _build_image(DESKTOP_DOCKERFILE, DESKTOP_IMAGE)
    if _docker("image", "inspect", PROXY_IMAGE).returncode != 0:
        proc = _docker("pull", PROXY_IMAGE)
        if proc.returncode != 0:
            pytest.fail(f"failed to pull {PROXY_IMAGE}:\n{proc.stderr[-2000:]}")
    return DESKTOP_IMAGE


@pytest.fixture(scope="session")
def server_image() -> str:
    """heg-preview-server: baked web frameworks (flask/fastapi/django/...)."""
    _build_image(SERVER_DOCKERFILE, SERVER_IMAGE)
    if _docker("image", "inspect", PROXY_IMAGE).returncode != 0:
        proc = _docker("pull", PROXY_IMAGE)
        if proc.returncode != 0:
            pytest.fail(f"failed to pull {PROXY_IMAGE}:\n{proc.stderr[-2000:]}")
    return SERVER_IMAGE


def _wait_log_marker(container: str, marker: str, seconds: float = 30) -> str:
    deadline = time.monotonic() + seconds
    logs = ""
    while time.monotonic() < deadline:
        proc = _docker("logs", container)
        logs = proc.stdout + proc.stderr  # app tracebacks land on stderr
        if marker in logs:
            return logs
        info = json.loads(_docker("inspect", container).stdout)[0]
        if info["State"]["Status"] == "exited":
            return logs
        time.sleep(0.5)
    return logs


def test_desktop_preview_pygame(
    manager: PreviewManager, desktop_image: str, tmp_path: Path
) -> None:
    """Runner on --internal net executes a pygame app; socat sidecar serves noVNC."""
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "main.py").write_text(_PYGAME_APP)

    plan = detect_preview(ws)
    assert plan is not None and plan.type == "desktop" and plan.entry == "main.py"

    preview = manager.spawn(
        run_id="r",
        sandbox_id="s",
        serve_dir=ws,
        plan=plan,
        ttl_seconds=300,
    )
    try:
        assert preview.type == "desktop" and preview.port is not None
        assert preview.url is not None and "/vnc.html" in preview.url

        # Runner: internal net only, no published ports, resource bounds.
        info = json.loads(_docker("inspect", preview.container).stdout)[0]
        nets = set(info["NetworkSettings"]["Networks"])
        assert nets == {"heg-desktop"}
        assert not (info["NetworkSettings"]["Ports"] or {}), (
            "runner must not publish ports — the proxy does"
        )
        host = info["HostConfig"]
        assert host["NanoCpus"] == 1_000_000_000  # --cpus 1
        assert host["Memory"] == 512 * 1024 * 1024
        assert host["PidsLimit"] == 256
        desk = json.loads(_docker("network", "inspect", "heg-desktop").stdout)[0]
        assert desk["Internal"] is True

        # Proxy: straddles both nets, port bound to localhost only.
        proxy = json.loads(
            _docker("inspect", f"{preview.container}-proxy").stdout
        )[0]
        assert set(proxy["NetworkSettings"]["Networks"]) == {
            PREVIEW_NETWORK,
            "heg-desktop",
        }
        bindings = proxy["NetworkSettings"]["Ports"]["6080/tcp"]
        assert {b["HostIp"] for b in bindings} == {"127.0.0.1"}

        # The app actually ran — candidate code executed under Xvnc.
        logs = _wait_log_marker(preview.container, "PYGAME_OK")
        assert "PYGAME_OK" in logs

        # noVNC web UI reachable through the sidecar.
        code, body = _wait_http(preview.url)
        assert code == 200, f"noVNC page returned {code}: {body[:200]}"
    finally:
        assert manager.stop(preview.preview_id)
    # BOTH containers gone — no orphaned proxy.
    assert manager.get(preview.preview_id) is None
    assert (
        _docker("inspect", f"{preview.container}-proxy").returncode != 0
    )


# ---------------------------------------------------------------------------
# PR3 — more desktop GUI stacks (pyglet / arcade / tkinter, baked deps)
# ---------------------------------------------------------------------------


def test_desktop_image_gui_deps_import(desktop_image: str) -> None:
    """All PR3 GUI stacks import inside the baked image (no egress needed —
    deps were installed at build time)."""
    proc = subprocess.run(
        [
            "docker", "run", "--rm", "--entrypoint", "python",
            desktop_image, "-c",
            "import pyglet, arcade, tkinter; print('GUI_DEPS_OK')",
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert "GUI_DEPS_OK" in proc.stdout


_TK_APP = """\
import time
import tkinter as tk
root = tk.Tk()
root.geometry("320x240")
root.title("tk preview")
print("TK_OK", flush=True)
root.update()
time.sleep(8)
"""


def test_desktop_preview_tkinter(
    manager: PreviewManager, desktop_image: str, tmp_path: Path
) -> None:
    """A tkinter app executes under Xvnc — proves the baked `tk` libs +
    DISPLAY plumbing work end-to-end (not just importable)."""
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "main.py").write_text(_TK_APP)

    plan = detect_preview(ws)
    assert plan is not None and plan.type == "desktop" and plan.entry == "main.py"

    preview = manager.spawn(
        run_id="r", sandbox_id="s", serve_dir=ws, plan=plan, ttl_seconds=300
    )
    try:
        logs = _wait_log_marker(preview.container, "TK_OK")
        assert "TK_OK" in logs, f"tkinter app did not start:\n{logs[-2000:]}"
    finally:
        manager.stop(preview.preview_id)


_PYGLET_APP = """\
import time
import pyglet
win = pyglet.window.Window(width=320, height=240, caption="pyglet preview")
print("PYGLET_OK", flush=True)
time.sleep(8)
"""


def test_desktop_preview_pyglet(
    manager: PreviewManager, desktop_image: str, tmp_path: Path
) -> None:
    """A pyglet window opens under Xvnc — exercises the baked mesa/GL stack
    (libgl/libglx/dri) that pyglet/arcade need for real rendering."""
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "main.py").write_text(_PYGLET_APP)

    plan = detect_preview(ws)
    assert plan is not None and plan.type == "desktop" and plan.entry == "main.py"

    preview = manager.spawn(
        run_id="r", sandbox_id="s", serve_dir=ws, plan=plan, ttl_seconds=300
    )
    try:
        logs = _wait_log_marker(preview.container, "PYGLET_OK")
        assert "PYGLET_OK" in logs, f"pyglet app did not start:\n{logs[-2000:]}"
    finally:
        manager.stop(preview.preview_id)


_ARCADE_APP = """\
import time
import arcade
win = arcade.Window(320, 240, "arcade preview")
print("ARCADE_OK", flush=True)
time.sleep(8)
"""


def test_desktop_preview_arcade(
    manager: PreviewManager, desktop_image: str, tmp_path: Path
) -> None:
    """An arcade window opens under Xvnc — arcade rides on pyglet, so this
    also smoke-tests the heaviest baked dep chain (arcade+pymunk)."""
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "main.py").write_text(_ARCADE_APP)

    plan = detect_preview(ws)
    assert plan is not None and plan.type == "desktop" and plan.entry == "main.py"

    preview = manager.spawn(
        run_id="r", sandbox_id="s", serve_dir=ws, plan=plan, ttl_seconds=300
    )
    try:
        logs = _wait_log_marker(preview.container, "ARCADE_OK")
        assert "ARCADE_OK" in logs, f"arcade app did not start:\n{logs[-2000:]}"
    finally:
        manager.stop(preview.preview_id)


# ---------------------------------------------------------------------------
# PR3 — python-server previews (executed web apps, same isolated topology)
# ---------------------------------------------------------------------------

_FLASK_APP = """\
from flask import Flask
app = Flask(__name__)

@app.route("/")
def index():
    return "flask-ok"

if __name__ == "__main__":
    # deliberately loopback + non-canonical port — the runner's relay must
    # bridge it to the sidecar's :8000 target
    app.run(host="127.0.0.1", port=5000)
"""


def test_python_server_preview_flask(
    manager: PreviewManager, server_image: str, tmp_path: Path
) -> None:
    """Flask app on 127.0.0.1:5000 → runner detects the listener, relays
    :8000 → :5000, socat sidecar publishes it on localhost."""
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "app.py").write_text(_FLASK_APP)

    plan = detect_preview(ws)
    assert plan is not None
    assert plan.type == "python-server" and plan.entry == "app.py"

    preview = manager.spawn(
        run_id="r", sandbox_id="s", serve_dir=ws, plan=plan, ttl_seconds=300
    )
    try:
        assert preview.type == "python-server" and preview.port is not None
        assert preview.url == f"http://localhost:{preview.port}/"

        # Runner: internal net only, no published ports, resource bounds.
        info = json.loads(_docker("inspect", preview.container).stdout)[0]
        assert set(info["NetworkSettings"]["Networks"]) == {"heg-desktop"}
        assert not (info["NetworkSettings"]["Ports"] or {})
        host = info["HostConfig"]
        assert host["NanoCpus"] == 1_000_000_000
        assert host["Memory"] == 512 * 1024 * 1024
        assert host["PidsLimit"] == 256

        # Proxy straddles both nets, publishes localhost:8000 only.
        proxy = json.loads(
            _docker("inspect", f"{preview.container}-proxy").stdout
        )[0]
        assert set(proxy["NetworkSettings"]["Networks"]) == {
            PREVIEW_NETWORK,
            "heg-desktop",
        }
        bindings = proxy["NetworkSettings"]["Ports"]["8000/tcp"]
        assert {b["HostIp"] for b in bindings} == {"127.0.0.1"}

        # The relay came up, then the app actually served through it.
        logs = _wait_log_marker(preview.container, "relay")
        assert "-> 127.0.0.1:5000" in logs
        code, body = _wait_http(preview.url, seconds=30)
        assert code == 200, f"app returned {code}: {body[:300]}"
        assert "flask-ok" in body
    finally:
        assert manager.stop(preview.preview_id)
    assert manager.get(preview.preview_id) is None
    assert (
        _docker("inspect", f"{preview.container}-proxy").returncode != 0
    )


_STDLIB_SERVER_APP = """\
from http.server import BaseHTTPRequestHandler, HTTPServer

class H(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"stdlib-ok")
    def log_message(self, *a):
        pass

# honors the runner's PORT/HOST env — binds 0.0.0.0:8000 directly, so the
# runner takes the no-relay path
import os
HTTPServer((os.environ.get("HOST", "0.0.0.0"),
            int(os.environ.get("PORT", "8000"))), H).serve_forever()
"""


def test_python_server_preview_stdlib_direct(
    manager: PreviewManager, server_image: str, tmp_path: Path
) -> None:
    """A stdlib app honoring PORT/HOST binds 0.0.0.0:8000 itself — the
    runner detects the non-loopback :8000 listener and skips the relay."""
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "serve.py").write_text(_STDLIB_SERVER_APP)

    plan = detect_preview(ws)
    assert plan is not None and plan.type == "python-server"

    preview = manager.spawn(
        run_id="r", sandbox_id="s", serve_dir=ws, plan=plan, ttl_seconds=300
    )
    try:
        logs = _wait_log_marker(preview.container, "no relay needed")
        assert "no relay needed" in logs
        code, body = _wait_http(preview.url, seconds=30)
        assert code == 200 and "stdlib-ok" in body
    finally:
        assert manager.stop(preview.preview_id)
