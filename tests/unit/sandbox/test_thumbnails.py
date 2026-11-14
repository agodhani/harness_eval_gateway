"""PR4 thumbnail-capture unit tests — Thumbnailer with a stubbed docker CLI.

The full flow (spawn thumb-purpose preview → ready probe → chromium capture
container → PNG validation → ``thumbnail.png`` + ``thumbnail.json`` under the
artifact dir) runs hermetically: ``FakeRuntime``/``_inspect_row`` from the
preview tests emulate the preview stack; a subclass emulates the capture
container by writing bytes into the ``-v <scratch>:/out`` dir it sees in the
``docker run`` args. ``urllib.request.urlopen`` is monkeypatched for the
host-side ready probe and ``sleep`` is injected — no docker, no network, no
real waiting. The real docker path is covered by
``tests/integration/test_thumbnail.py`` (``@pytest.mark.docker``).
"""

from __future__ import annotations

import json
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from sandbox.preview import (
    CONTAINER_PORT,
    DESKTOP_CONTAINER_PORT,
    DESKTOP_NETWORK,
    PREVIEW_NETWORK,
    SERVER_CONTAINER_PORT,
    Preview,
    PreviewManager,
    PreviewPlan,
)
from sandbox.thumbnails import (
    PNG_MAGIC,
    THUMB_FILENAME,
    THUMB_META_FILENAME,
    Thumbnailer,
    ThumbnailError,
)
from tests.unit.sandbox.test_preview import FakeRuntime, _inspect_row, _labels

PNG_BYTES = PNG_MAGIC + b"\x00" * 64


class ThumbFakeRuntime(FakeRuntime):
    """FakeRuntime + a fake capture container.

    The ``docker run`` for a ``heg-thumb-*`` container "runs chromium":
    writes ``capture.png`` into the ``-v <scratch>:/out`` dir (unless
    ``write=False``), exits ``thumb_rc`` (or raises TimeoutExpired).
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.png_bytes: bytes = PNG_BYTES
        self.write_shot: bool = True
        self.thumb_rc: int = 0
        self.thumb_timeout: bool = False

    def _docker(
        self,
        args: list[str],
        *,
        check: bool = True,
        timeout: float | None = None,
    ) -> subprocess.CompletedProcess[str]:
        if (
            args[0] == "run"
            and "--name" in args
            and args[args.index("--name") + 1].startswith("heg-thumb-")
        ):
            self.calls.append(args)
            if self.thumb_timeout:
                raise subprocess.TimeoutExpired(["docker", *args], 10)
            if self.thumb_rc == 0 and self.write_shot:
                out_dir = Path(args[args.index("-v") + 1].split(":")[0])
                (out_dir / "capture.png").write_bytes(self.png_bytes)
            out = "thumb-cid\n" if self.thumb_rc == 0 else ""
            err = "" if self.thumb_rc == 0 else "chromium exploded"
            return subprocess.CompletedProcess(args, self.thumb_rc, out, err)
        return super()._docker(args, check=check, timeout=timeout)


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def sleeps() -> list[float]:
    return []


def _no_sleep(record: list[float]):
    def _sleep(seconds: float) -> None:
        record.append(seconds)

    return _sleep


@pytest.fixture()
def urlopen_calls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Stub the host-side ready probe — every URL 'serves' immediately."""
    calls: list[str] = []

    class _Resp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake(url, timeout=None):
        calls.append(url)
        return _Resp()

    monkeypatch.setattr(urllib.request, "urlopen", fake)
    return calls


def _static_manager(fake: ThumbFakeRuntime) -> PreviewManager:
    """Manager whose get() resolves a running static preview on :49152."""
    fake.ps_ids = ["cid123"]
    fake.inspect_rows = [_inspect_row(labels=_labels())]
    return PreviewManager(runtime=fake)  # type: ignore[arg-type]


def _isolated_manager(
    fake: ThumbFakeRuntime,
    *,
    preview_type: str,
    container_port: int,
    runner_image: str,
    **mgr_kw,
) -> PreviewManager:
    """Manager resolving an executed-code preview: runner on the internal
    net + socat proxy publishing ``container_port`` on 127.0.0.1:49160."""
    proxy_row = _inspect_row(container_id="px1", name="/heg-prev-abc-proxy")
    proxy_row["NetworkSettings"]["Ports"] = {
        f"{container_port}/tcp": [{"HostIp": "127.0.0.1", "HostPort": "49160"}]
    }
    fake.ps_ids = ["cid123"]
    fake.proxy_ps_ids = ["px1"]
    fake.rows_by_id = {
        "cid123": _inspect_row(
            container_id="cid123",
            name="/heg-prev-abc",
            labels=_labels(**{"heg.preview_type": preview_type}),
            port=None,
        ),
        "px1": proxy_row,
    }
    fake.images.add(runner_image)
    return PreviewManager(runtime=fake, **mgr_kw)  # type: ignore[arg-type]


def _desktop_manager(fake: ThumbFakeRuntime) -> PreviewManager:
    return _isolated_manager(
        fake,
        preview_type="desktop",
        container_port=DESKTOP_CONTAINER_PORT,
        runner_image="desk:img",
        desktop_image="desk:img",
    )


def _server_manager(fake: ThumbFakeRuntime) -> PreviewManager:
    return _isolated_manager(
        fake,
        preview_type="python-server",
        container_port=SERVER_CONTAINER_PORT,
        runner_image="srv:img",
        server_image="srv:img",
    )


def _ws(tmp_path: Path, files: dict[str, str] | None = None) -> tuple[Path, Path]:
    """(workspace, artifact_dir) under a fake artifact store."""
    ws = tmp_path / "run1" / "sb1" / "workspace"
    ws.mkdir(parents=True)
    for rel, content in (files or {}).items():
        p = ws / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    return ws, ws.parent


def _thumbnailer(
    mgr: PreviewManager, sleeps: list[float], **kw
) -> Thumbnailer:
    return Thumbnailer(previews=mgr, sleep=_no_sleep(sleeps), **kw)


def _thumb_run(fake: ThumbFakeRuntime) -> list[str]:
    """The ``docker run`` args of the capture container."""
    runs = [
        c
        for c in fake.calls
        if c[0] == "run"
        and "--name" in c
        and c[c.index("--name") + 1].startswith("heg-thumb-")
    ]
    assert len(runs) == 1, f"expected one capture run, got {runs!r}"
    return runs[0]


def _env(run: list[str], name: str) -> str:
    for i, a in enumerate(run):
        if a == "-e" and run[i + 1].startswith(f"{name}="):
            return run[i + 1].split("=", 1)[1]
    raise AssertionError(f"{name} not in run env: {run!r}")


# ---------------------------------------------------------------------------
# capture — static happy path
# ---------------------------------------------------------------------------


def test_capture_static_full_flow(
    tmp_path: Path, urlopen_calls: list[str], sleeps: list[float]
) -> None:
    ws, artifact_dir = _ws(tmp_path, {"index.html": "<h1>x</h1>"})
    fake = ThumbFakeRuntime()
    mgr = _static_manager(fake)
    result = _thumbnailer(mgr, sleeps).capture(
        run_id="run1",
        sandbox_id="sb1",
        workspace=ws,
        artifact_dir=artifact_dir,
        plan=PreviewPlan(type="static", entry="index.html"),
    )

    assert result.path == artifact_dir / THUMB_FILENAME
    assert result.path.read_bytes() == PNG_BYTES
    meta = json.loads((artifact_dir / THUMB_META_FILENAME).read_text())
    assert meta["type"] == "static"
    assert meta["entry"] == "index.html"
    assert meta["captured_at"]
    assert result.type == "static"

    # The preview stack was spawned for capture, not interaction.
    spawn_run = next(c for c in fake.calls if c[0] == "run")
    label_args = [spawn_run[i + 1] for i, v in enumerate(spawn_run) if v == "--label"]
    assert "heg.preview_purpose=thumb" in label_args

    # Ready probe hit the published localhost port.
    assert urlopen_calls == ["http://127.0.0.1:49152/"]

    # Capture container: in-network URL, hardened, on heg-preview.
    run = _thumb_run(fake)
    assert run[run.index("--network") + 1] == PREVIEW_NETWORK
    assert _env(run, "CAPTURE_URL") == f"http://heg-prev-abc:{CONTAINER_PORT}/"
    assert _env(run, "THUMB_BUDGET_MS") == "10000"
    assert run[run.index("--user") + 1] == "65534:65534"
    assert "--rm" in run and "--read-only" in run and "--cap-drop" in run
    labels = [run[i + 1] for i, v in enumerate(run) if v == "--label"]
    assert "heg.thumb=1" in labels
    assert "heg.sandbox_id=sb1" in labels
    mount = run[run.index("-v") + 1]
    assert mount.endswith(":/out") and ".thumb-" in mount

    # Everything was cleaned up: preview stack stopped, scratch removed.
    rm = next(c for c in fake.calls if c[0] == "rm")
    assert "cid123" in rm
    assert not list(artifact_dir.glob(".thumb-*"))
    assert sleeps == [0.5]  # non-desktop settle


def test_capture_static_entry_path(
    tmp_path: Path, urlopen_calls: list[str], sleeps: list[float]
) -> None:
    """Nested index entry → capture URL is the entry's dir on the runner."""
    ws, artifact_dir = _ws(tmp_path, {"dist/index.html": "x"})
    fake = ThumbFakeRuntime()
    mgr = _static_manager(fake)
    _thumbnailer(mgr, sleeps).capture(
        run_id="run1",
        sandbox_id="sb1",
        workspace=ws,
        artifact_dir=artifact_dir,
        plan=PreviewPlan(type="static", entry="dist/index.html"),
    )
    assert _env(_thumb_run(fake), "CAPTURE_URL") == (
        f"http://heg-prev-abc:{CONTAINER_PORT}/dist/"
    )


def test_capture_static_nonindex_entry_path(
    tmp_path: Path, urlopen_calls: list[str], sleeps: list[float]
) -> None:
    ws, artifact_dir = _ws(tmp_path, {"game.html": "x"})
    fake = ThumbFakeRuntime()
    mgr = _static_manager(fake)
    _thumbnailer(mgr, sleeps).capture(
        run_id="run1",
        sandbox_id="sb1",
        workspace=ws,
        artifact_dir=artifact_dir,
        plan=PreviewPlan(type="static", entry="game.html"),
    )
    assert _env(_thumb_run(fake), "CAPTURE_URL") == (
        f"http://heg-prev-abc:{CONTAINER_PORT}/game.html"
    )


# ---------------------------------------------------------------------------
# capture — executed-code types (proxied URL + settle + budgets)
# ---------------------------------------------------------------------------


def test_capture_desktop_proxied_url(
    tmp_path: Path, urlopen_calls: list[str], sleeps: list[float]
) -> None:
    """Desktop: shot goes through the socat sidecar's noVNC page; the app
    gets the longer settle + virtual-time budget to draw into the VNC
    framebuffer."""
    ws, artifact_dir = _ws(tmp_path, {"main.py": "import pygame\n"})
    fake = ThumbFakeRuntime()
    mgr = _desktop_manager(fake)
    result = _thumbnailer(mgr, sleeps).capture(
        run_id="run1",
        sandbox_id="sb1",
        workspace=ws,
        artifact_dir=artifact_dir,
        plan=PreviewPlan(type="desktop", entry="main.py"),
    )
    assert result.type == "desktop"

    # Ready probe traversed socat → websockify on the published port.
    assert urlopen_calls == ["http://127.0.0.1:49160/vnc.html"]

    run = _thumb_run(fake)
    assert _env(run, "CAPTURE_URL") == (
        f"http://heg-prev-abc-proxy:{DESKTOP_CONTAINER_PORT}"
        "/vnc.html?autoconnect=true&resize=scale"
    )
    assert _env(run, "THUMB_BUDGET_MS") == "20000"
    assert sleeps == [8.0]  # desktop settle

    # Runner spawned on the internal net (executed-code isolation kept).
    runner = next(c for c in fake.calls if c[0] == "run")
    assert runner[runner.index("--network") + 1] == DESKTOP_NETWORK


def test_capture_python_server_proxied_url(
    tmp_path: Path, urlopen_calls: list[str], sleeps: list[float]
) -> None:
    """python-server: app root via the sidecar on :8000."""
    ws, artifact_dir = _ws(tmp_path, {"app.py": "from flask import Flask\n"})
    fake = ThumbFakeRuntime()
    mgr = _server_manager(fake)
    result = _thumbnailer(mgr, sleeps).capture(
        run_id="run1",
        sandbox_id="sb1",
        workspace=ws,
        artifact_dir=artifact_dir,
        plan=PreviewPlan(type="python-server", entry="app.py"),
    )
    assert result.type == "python-server"
    assert urlopen_calls == ["http://127.0.0.1:49160/"]
    run = _thumb_run(fake)
    assert _env(run, "CAPTURE_URL") == (
        f"http://heg-prev-abc-proxy:{SERVER_CONTAINER_PORT}/"
    )


# ---------------------------------------------------------------------------
# capture — failure contract (always: stack stopped, no half outputs)
# ---------------------------------------------------------------------------


def test_capture_rejects_non_png(
    tmp_path: Path, urlopen_calls: list[str], sleeps: list[float]
) -> None:
    ws, artifact_dir = _ws(tmp_path, {"index.html": "x"})
    fake = ThumbFakeRuntime()
    fake.png_bytes = b"definitely not a png"
    mgr = _static_manager(fake)
    with pytest.raises(ThumbnailError, match="not a PNG"):
        _thumbnailer(mgr, sleeps).capture(
            run_id="run1",
            sandbox_id="sb1",
            workspace=ws,
            artifact_dir=artifact_dir,
            plan=PreviewPlan(type="static", entry="index.html"),
        )
    assert not (artifact_dir / THUMB_FILENAME).exists()
    assert not list(artifact_dir.glob(".thumb-*"))
    assert any(c[0] == "rm" and "cid123" in c for c in fake.calls)


def test_capture_missing_screenshot(
    tmp_path: Path, urlopen_calls: list[str], sleeps: list[float]
) -> None:
    ws, artifact_dir = _ws(tmp_path, {"index.html": "x"})
    fake = ThumbFakeRuntime()
    fake.write_shot = False  # chromium "succeeds" but leaves nothing
    mgr = _static_manager(fake)
    with pytest.raises(ThumbnailError, match="no screenshot"):
        _thumbnailer(mgr, sleeps).capture(
            run_id="run1",
            sandbox_id="sb1",
            workspace=ws,
            artifact_dir=artifact_dir,
            plan=PreviewPlan(type="static", entry="index.html"),
        )
    assert not (artifact_dir / THUMB_FILENAME).exists()


def test_capture_container_failure(
    tmp_path: Path, urlopen_calls: list[str], sleeps: list[float]
) -> None:
    ws, artifact_dir = _ws(tmp_path, {"index.html": "x"})
    fake = ThumbFakeRuntime()
    fake.thumb_rc = 3
    mgr = _static_manager(fake)
    with pytest.raises(ThumbnailError, match="capture container failed"):
        _thumbnailer(mgr, sleeps).capture(
            run_id="run1",
            sandbox_id="sb1",
            workspace=ws,
            artifact_dir=artifact_dir,
            plan=PreviewPlan(type="static", entry="index.html"),
        )
    assert not (artifact_dir / THUMB_FILENAME).exists()
    assert any(c[0] == "rm" and "cid123" in c for c in fake.calls)


def test_capture_timeout_removes_container(
    tmp_path: Path, urlopen_calls: list[str], sleeps: list[float]
) -> None:
    ws, artifact_dir = _ws(tmp_path, {"index.html": "x"})
    fake = ThumbFakeRuntime()
    fake.thumb_timeout = True
    mgr = _static_manager(fake)
    with pytest.raises(ThumbnailError, match="exceeded"):
        _thumbnailer(mgr, sleeps).capture(
            run_id="run1",
            sandbox_id="sb1",
            workspace=ws,
            artifact_dir=artifact_dir,
            plan=PreviewPlan(type="static", entry="index.html"),
        )
    # The timed-out capture container is force-removed by name.
    assert any(
        c[:2] == ["rm", "-f"] and any(n.startswith("heg-thumb-") for n in c)
        for c in fake.calls
    )


def test_wait_ready_dead_preview(
    tmp_path: Path, urlopen_calls: list[str], sleeps: list[float]
) -> None:
    """A runner that exits before serving fails fast — no capture run."""
    ws, artifact_dir = _ws(tmp_path, {"index.html": "x"})
    fake = ThumbFakeRuntime(
        inspect_rows=[_inspect_row(labels=_labels(), status="exited")]
    )
    fake.ps_ids = ["cid123"]
    mgr = PreviewManager(runtime=fake)  # type: ignore[arg-type]
    with pytest.raises(ThumbnailError, match="exited before serving"):
        _thumbnailer(mgr, sleeps).capture(
            run_id="run1",
            sandbox_id="sb1",
            workspace=ws,
            artifact_dir=artifact_dir,
            plan=PreviewPlan(type="static", entry="index.html"),
        )
    assert not any(
        c[0] == "run" and "--name" in c and "heg-thumb-" in c[c.index("--name") + 1]
        for c in fake.calls
    )


def test_wait_ready_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sleeps: list[float]
) -> None:
    """Never-serving stack → ThumbnailError after ready_seconds."""
    ws, artifact_dir = _ws(tmp_path, {"index.html": "x"})
    fake = ThumbFakeRuntime()

    def refuse(url, timeout=None):
        raise urllib.error.URLError("conn refused")

    monkeypatch.setattr(urllib.request, "urlopen", refuse)
    fake.ps_ids = ["cid123"]
    fake.inspect_rows = [_inspect_row(labels=_labels())]
    mgr = PreviewManager(runtime=fake)  # type: ignore[arg-type]
    with pytest.raises(ThumbnailError, match="did not serve"):
        _thumbnailer(mgr, sleeps, ready_seconds=0.5).capture(
            run_id="run1",
            sandbox_id="sb1",
            workspace=ws,
            artifact_dir=artifact_dir,
            plan=PreviewPlan(type="static", entry="index.html"),
        )
    assert not (artifact_dir / THUMB_FILENAME).exists()


def test_wait_ready_http_error_still_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sleeps: list[float]
) -> None:
    """An HTTP error page still proves the chain serves (the screenshot
    shows whatever the app returns)."""
    ws, artifact_dir = _ws(tmp_path, {"index.html": "x"})
    fake = ThumbFakeRuntime()

    def http500(url, timeout=None):
        raise urllib.error.HTTPError(url, 500, "err", None, None)

    monkeypatch.setattr(urllib.request, "urlopen", http500)
    mgr = _static_manager(fake)
    result = _thumbnailer(mgr, sleeps).capture(
        run_id="run1",
        sandbox_id="sb1",
        workspace=ws,
        artifact_dir=artifact_dir,
        plan=PreviewPlan(type="static", entry="index.html"),
    )
    assert result.path.exists()


# ---------------------------------------------------------------------------
# _capture_url — pure per-type URL mapping
# ---------------------------------------------------------------------------


def _pv(ptype: str, entry: str | None = None) -> Preview:
    return Preview(
        preview_id="p",
        run_id="r",
        sandbox_id="s",
        type=ptype,
        container="heg-prev-xyz",
        status="running",
        port=49000,
        entry=entry,
        url=None,
        created_at=None,
        expires_at=None,
    )


@pytest.mark.parametrize(
    "ptype,entry,want",
    [
        ("static", "index.html", f"http://heg-prev-xyz:{CONTAINER_PORT}/"),
        ("static", "a b.html", f"http://heg-prev-xyz:{CONTAINER_PORT}/a%20b.html"),
        (
            "desktop",
            "main.py",
            (
                f"http://heg-prev-xyz-proxy:{DESKTOP_CONTAINER_PORT}"
                "/vnc.html?autoconnect=true&resize=scale"
            ),
        ),
        (
            "python-server",
            "app.py",
            f"http://heg-prev-xyz-proxy:{SERVER_CONTAINER_PORT}/",
        ),
    ],
)
def test_capture_url(ptype: str, entry: str | None, want: str) -> None:
    plan = PreviewPlan(type=ptype, entry=entry or "")
    assert Thumbnailer._capture_url(_pv(ptype, entry), plan) == want


def test_capture_url_unknown_type() -> None:
    with pytest.raises(ThumbnailError, match="no capture route"):
        Thumbnailer._capture_url(
            _pv("quantum", "x"), PreviewPlan(type="quantum", entry="x")
        )
