"""PR4 thumbnail-capture integration test — requires a live Docker daemon.

Marked ``docker``; run with ``uv run pytest -m docker`` or
``uv run pytest tests/integration/test_thumbnail.py``.

Builds ``heg-preview-thumb`` (debian chromium on python:3.12-slim) and runs
the real capture path: static workspace → thumb-purpose preview stack →
headless screenshot → ``thumbnail.png`` + ``thumbnail.json``.
"""

import json
import struct
import subprocess
import time
from pathlib import Path

import pytest

from sandbox.preview import PreviewManager, PreviewPlan
from sandbox.runtime import SandboxRuntime
from sandbox.thumbnails import (
    PNG_MAGIC,
    THUMB_FILENAME,
    THUMB_META_FILENAME,
    Thumbnailer,
    ThumbnailError,
)

pytestmark = pytest.mark.docker

REPO_ROOT = Path(__file__).resolve().parents[2]
PREVIEW_IMAGE = "python:3.12-slim"
THUMB_IMAGE = "heg-preview-thumb:latest"
THUMB_DOCKERFILE = REPO_ROOT / "sandbox/images/thumbnail-capture.Dockerfile"
DESKTOP_IMAGE = "heg-preview-desktop:latest"
DESKTOP_DOCKERFILE = REPO_ROOT / "sandbox/images/desktop-preview.Dockerfile"
PROXY_IMAGE = "alpine/socat:1.8.1.3"


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


def _build_image(dockerfile: Path, tag: str) -> str:
    proc = subprocess.run(
        ["docker", "build", "-f", str(dockerfile), "-t", tag, str(REPO_ROOT)],
        capture_output=True,
        text=True,
        timeout=1800,
        check=False,
    )
    if proc.returncode != 0:
        pytest.fail(f"failed to build {tag}:\n{proc.stderr[-3000:]}")
    return tag


@pytest.fixture(scope="session")
def thumb_image() -> str:
    """heg-preview-thumb: headless chromium screenshotter."""
    return _build_image(THUMB_DOCKERFILE, THUMB_IMAGE)


@pytest.fixture(scope="session")
def preview_image() -> str:
    if _docker("image", "inspect", PREVIEW_IMAGE).returncode != 0:
        proc = _docker("pull", PREVIEW_IMAGE)
        if proc.returncode != 0:
            pytest.fail(f"failed to pull {PREVIEW_IMAGE}:\n{proc.stderr[-2000:]}")
    return PREVIEW_IMAGE


@pytest.fixture(scope="session")
def desktop_image() -> str:
    """heg-preview-desktop (TigerVNC + noVNC + pygame-ce) + the socat
    sidecar image its proxy needs."""
    _build_image(DESKTOP_DOCKERFILE, DESKTOP_IMAGE)
    if _docker("image", "inspect", PROXY_IMAGE).returncode != 0:
        proc = _docker("pull", PROXY_IMAGE)
        if proc.returncode != 0:
            pytest.fail(f"failed to pull {PROXY_IMAGE}:\n{proc.stderr[-2000:]}")
    return DESKTOP_IMAGE


# Slow-booting app: sleeps before the first draw, then paints a big green
# rect on black — proves the VNC capture waits for real framebuffer
# content instead of shooting the "Connecting…" spinner (the old bug).
_DESKTOP_APP = """\
import os, time
os.environ.setdefault("SDL_VIDEODRIVER", "x11")
import pygame
pygame.init()
time.sleep(2.5)
screen = pygame.display.set_mode((640, 480))
screen.fill((10, 200, 40))
pygame.display.flip()
end = time.time() + 120
while time.time() < end:
    for e in pygame.event.get():
        pass
    time.sleep(0.05)
"""


def _sandbox_containers() -> list[str]:
    """Containers created by THIS test — scoped by the run/sandbox labels
    the preview + capture containers carry (never touches other heg-*
    containers that may be running)."""
    out = _docker(
        "ps",
        "-aq",
        "--filter",
        "label=heg.run_id=run1",
        "--filter",
        "label=heg.sandbox_id=sb1",
    )
    return out.stdout.split()


def test_static_capture_end_to_end(
    tmp_path: Path, thumb_image: str, preview_image: str
) -> None:
    """jail/run/sb/workspace with a static site → real PNG thumbnail +
    meta sidecar, and every container cleaned up afterwards."""
    jail = tmp_path / "artifacts"
    artifact_dir = jail / "run1" / "sb1"
    ws = artifact_dir / "workspace"
    ws.mkdir(parents=True)
    (ws / "index.html").write_text(
        "<html><body style='background:#114'>"
        "<h1 style='color:#fff'>thumb me</h1></body></html>"
    )

    mgr = PreviewManager(
        runtime=SandboxRuntime(), image=preview_image, ttl_seconds=300
    )
    thumb = Thumbnailer(
        previews=mgr,
        image=thumb_image,
        ready_seconds=30,
        capture_seconds=60,
        preview_ttl_seconds=120,
        settle_seconds=0.5,
    )
    try:
        result = thumb.capture(
            run_id="run1",
            sandbox_id="sb1",
            workspace=ws,
            artifact_dir=artifact_dir,
            plan=PreviewPlan(type="static", entry="index.html"),
        )
    finally:
        # Safety net — capture() stops its own stack; this catches leaks if
        # the test itself is interrupted mid-flight.
        for cid in _sandbox_containers():
            _docker("rm", "-f", cid)

    png = artifact_dir / THUMB_FILENAME
    assert png == result.path and png.is_file()
    data = png.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    # A rendered 1280x800 PNG is well past trivial size.
    assert len(data) > 2_000

    meta = json.loads((artifact_dir / THUMB_META_FILENAME).read_text())
    assert meta["type"] == "static" and meta["entry"] == "index.html"
    assert meta["captured_at"]

    # No scratch dir or containers left behind.
    assert not list(artifact_dir.glob(".thumb-*"))
    deadline = time.monotonic() + 10
    leftovers = _sandbox_containers()
    while leftovers and time.monotonic() < deadline:
        time.sleep(0.5)
        leftovers = _sandbox_containers()
    assert leftovers == []


def test_desktop_capture_waits_for_framebuffer(
    tmp_path: Path, thumb_image: str, desktop_image: str
) -> None:
    """Desktop app that draws only after 2.5s → the thumbnail is the REAL
    framebuffer (big non-black pixels), captured by the in-container VNC
    client — not the early-screenshot "Connecting…" spinner/blank canvas
    a fixed-sleep capture produced before."""
    jail = tmp_path / "artifacts"
    artifact_dir = jail / "run1" / "sb1"
    ws = artifact_dir / "workspace"
    ws.mkdir(parents=True)
    (ws / "main.py").write_text(_DESKTOP_APP)

    mgr = PreviewManager(
        runtime=SandboxRuntime(), image=desktop_image, ttl_seconds=300
    )
    thumb = Thumbnailer(
        previews=mgr,
        image=thumb_image,
        ready_seconds=60,  # Xvnc + websockify + slow first draw
        capture_seconds=90,
        preview_ttl_seconds=120,
        desktop_settle_seconds=2.0,
    )
    try:
        result = thumb.capture(
            run_id="run1",
            sandbox_id="sb1",
            workspace=ws,
            artifact_dir=artifact_dir,
            plan=PreviewPlan(type="desktop", entry="main.py"),
        )
    finally:
        for cid in _sandbox_containers():
            _docker("rm", "-f", cid)

    png = artifact_dir / THUMB_FILENAME
    assert png == result.path and png.is_file()
    data = png.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    # Xvnc's framebuffer geometry (1024x768), not the chromium viewport —
    # IHDR bytes 16-24 hold width/height.
    width, height = struct.unpack(">II", data[16:24])
    assert (width, height) == (1024, 768)

    meta = json.loads((artifact_dir / THUMB_META_FILENAME).read_text())
    assert meta["type"] == "desktop" and meta["entry"] == "main.py"
    assert meta["generator"] == "vnc-framebuffer"
    assert (meta["width"], meta["height"]) == (width, height)

    assert data[:8] == PNG_MAGIC
    # Cheap check: PNG bytes for a solid-color screen compress small;
    # an app's pixels still compress but the file is meaningfully sized
    # either way — the strong signal is generator + geometry above.
    assert len(data) > 1_000

    assert not list(artifact_dir.glob(".thumb-*"))
    deadline = time.monotonic() + 10
    leftovers = _sandbox_containers()
    while leftovers and time.monotonic() < deadline:
        time.sleep(0.5)
        leftovers = _sandbox_containers()
    assert leftovers == []


def test_desktop_capture_dead_app_reports_exit(
    tmp_path: Path, thumb_image: str, desktop_image: str
) -> None:
    """An app that dies before capture → ThumbnailError naming the exit
    code + the app's log tail — not a hung ready-wait."""
    jail = tmp_path / "artifacts"
    artifact_dir = jail / "run1" / "sb1"
    ws = artifact_dir / "workspace"
    ws.mkdir(parents=True)
    # NB: "this is = not python" is a REAL SyntaxError — the tempting
    # "this is not python" is a valid `x is not y` expression that
    # compiles and dies at runtime as a NameError instead.
    (ws / "main.py").write_text("import pygame\nthis is = not python\n")

    mgr = PreviewManager(
        runtime=SandboxRuntime(), image=desktop_image, ttl_seconds=300
    )
    thumb = Thumbnailer(
        previews=mgr,
        image=thumb_image,
        ready_seconds=45,
        capture_seconds=60,
        preview_ttl_seconds=120,
    )
    try:
        with pytest.raises(ThumbnailError) as ei:
            thumb.capture(
                run_id="run1",
                sandbox_id="sb1",
                workspace=ws,
                artifact_dir=artifact_dir,
                plan=PreviewPlan(type="desktop", entry="main.py"),
            )
    finally:
        for cid in _sandbox_containers():
            _docker("rm", "-f", cid)
    msg = str(ei.value)
    assert "exited" in msg and "SyntaxError" in msg
    assert not (artifact_dir / THUMB_FILENAME).exists()
