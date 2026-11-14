"""PR4 thumbnail-capture integration test — requires a live Docker daemon.

Marked ``docker``; run with ``uv run pytest -m docker`` or
``uv run pytest tests/integration/test_thumbnail.py``.

Builds ``heg-preview-thumb`` (debian chromium on python:3.12-slim) and runs
the real capture path: static workspace → thumb-purpose preview stack →
headless screenshot → ``thumbnail.png`` + ``thumbnail.json``.
"""

import json
import subprocess
import time
from pathlib import Path

import pytest

from sandbox.preview import PreviewManager, PreviewPlan
from sandbox.runtime import SandboxRuntime
from sandbox.thumbnails import THUMB_FILENAME, THUMB_META_FILENAME, Thumbnailer

pytestmark = pytest.mark.docker

REPO_ROOT = Path(__file__).resolve().parents[2]
PREVIEW_IMAGE = "python:3.12-slim"
THUMB_IMAGE = "heg-preview-thumb:latest"
THUMB_DOCKERFILE = REPO_ROOT / "sandbox/images/thumbnail-capture.Dockerfile"


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
