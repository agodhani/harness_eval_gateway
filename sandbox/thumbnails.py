"""Artifact thumbnails (PR4) — headless screenshots of produced artifacts.

Product flow: results page "📷 snapshot" → ``POST .../thumbnail`` → this
module spawns the artifact's preview stack, shoots it with a throwaway
headless-chromium container over the in-network URL (the container polls
until the stack serves), then writes ``<artifact_dir>/thumbnail.png`` +
``thumbnail.json``. The results
page shows the PNG; clicking it spawns the full interactive preview.
Generation is explicit-only (never on results GET — that endpoint is polled
every few seconds) and failures degrade to "no thumbnail" — the router
maps :class:`ThumbnailError` to 502 and nothing else breaks.

Design notes:

- **Reuse of the preview stack.** The artifact is served exactly as an
  interactive preview would serve it — :meth:`PreviewManager.spawn` with
  ``purpose=PURPOSE_THUMB`` and a short TTL. Same isolation/topology (so a
  python-server/desktop artifact gets its runner+socat pair, a static site
  its hardened ``http.server``), same label registry/reaper (a leaked
  capture stack is TTL-bounded like any preview). The ``heg.preview_purpose``
  label keeps capture stacks invisible to the interactive endpoints.
- **Capture topology.** The capture container (``heg-thumb-*``, image
  ``heg-preview-thumb``) joins ``heg-preview`` — the same bridge the
  runners/proxies sit on — and resolves them by container name:

  - ``static`` → ``http://<runner>:8000/<entry path>``
  - ``python-server`` → ``http://<runner>-proxy:8000/``
  - ``desktop`` → ``http://<runner>-proxy:6080/vnc.html?autoconnect=…``

  The serving-readiness poll lives inside the capture container for the
  same reason: the preview's published port is bound to the HOST's
  loopback, which an api running in a container itself (compose) can
  never reach — the container's in-network view of ``CAPTURE_URL`` is
  what chromium loads anyway.

  Desktop has no alternative: Xvnc binds loopback-only inside the runner,
  so the websockify endpoint is the only window onto the framebuffer.
  Chromium + ``--virtual-time-budget`` proved useless for it — virtual
  time burns through the budget while real framebuffer updates are still
  seconds away, so every shot landed on the "Connecting…" spinner or a
  blank canvas. Instead ``THUMB_VNC_WS`` routes the capture container to
  a tiny stdlib VNC-over-WebSocket client (``thumbnail-vnc.py``) that
  polls the framebuffer until real content appears (then waits
  ``THUMB_VNC_SETTLE_SECONDS`` for draw margin) and writes the pixels
  straight to PNG — adaptive to slow apps, and the shot IS the screen.
  Going through the preview's own serve path (rather than e.g.
  ``file://`` for static) keeps the capture faithful to what a user sees
  — same server semantics, same relative fetches.
- **Threat posture.** Rendering candidate HTML/JS in headless chromium is
  the same trust boundary as the user's browser opening the preview URL:
  the *executed* artifact code still lives on the zero-egress
  ``heg-desktop`` net — the browser only ever sees what the preview serves
  over HTTP. The capture container itself is hardened like a static runner
  (nobody, read-only rootfs, cap-drop ALL, no-new-privileges) plus a
  bounded tmpfs ``/tmp`` for chromium's profile.
- **Output + failure contract.** Chromium writes ``/out/capture.png`` into
  a world-writable scratch dir inside the artifact dir; the api validates
  the PNG magic before atomically ``os.replace``-ing it into place and
  writing ``thumbnail.json`` (``{version, captured_at, type, entry,
  generator, width, height, bytes}`` — staleness is decided by explicit
  re-capture, not mtimes).
  Any step failing raises :class:`ThumbnailError`; the stack and scratch
  are always cleaned up. Sync like :class:`PreviewManager` — the router
  wraps calls in ``asyncio.to_thread``.
"""

from __future__ import annotations

import json
import logging
import math
import os
import shutil
import struct
import subprocess
import threading
import time
import urllib.parse
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sandbox.preview import (
    _PROXIED_PORTS,
    CONTAINER_PORT,
    DEAD_STATUSES,
    DESKTOP_CONTAINER_PORT,
    PURPOSE_THUMB,
    Preview,
    PreviewManager,
    PreviewPlan,
    _entry_url_path,
)

logger = logging.getLogger(__name__)

THUMB_CONTAINER_PREFIX = "heg-thumb-"
DEFAULT_THUMB_IMAGE = "heg-preview-thumb:latest"
THUMB_DOCKERFILE = "sandbox/images/thumbnail-capture.Dockerfile"
THUMB_FILENAME = "thumbnail.png"
THUMB_META_FILENAME = "thumbnail.json"
THUMB_META_VERSION = 1
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

#: Marker label on the capture container itself (distinct from the
#: ``heg.preview*`` registry — the capture container is not a preview).
LABEL_THUMB = "heg.thumb"

_OUT_DIR = "/out"  # scratch bind-mount inside the capture container
_OUT_FILENAME = "capture.png"  # what the capture writes under /out

# Exit codes of thumbnail-vnc.py (the desktop capture path — see the
# thumbnail-capture.sh THUMB_VNC_WS branch).
_VNC_EXIT_TIMEOUT = 4  # framebuffer stayed blank past the wait deadline


class ThumbnailError(RuntimeError):
    """Any capture-step failure — the router maps it to 502."""


@dataclass(frozen=True)
class ThumbResult:
    """Outcome of a successful capture."""

    path: Path  # <artifact_dir>/thumbnail.png
    meta_path: Path  # <artifact_dir>/thumbnail.json
    captured_at: datetime
    type: str
    entry: str | None


def _pre_capture_settle(preview_type: str, settle_seconds: float) -> float:
    """Pre-capture delay per type. Served pages get a short warmup beat
    (chromium's virtual-time budget does the rest); desktop needs none —
    its capture is adaptive (waits on framebuffer content in-container)
    so a fixed api-side sleep would be pure latency."""
    return 0.0 if preview_type == "desktop" else settle_seconds


class Thumbnailer:
    """Screenshot a snapshot workspace via its own preview stack.

    Composes a :class:`PreviewManager` — spawning, labels, networks, and
    host-path translation all come from it. ``sleep``/``clock``/``probe``
    are injectable for hermetic tests.
    """

    def __init__(
        self,
        *,
        previews: PreviewManager,
        image: str | None = None,
        ready_seconds: float = 30.0,
        capture_seconds: float = 45.0,
        preview_ttl_seconds: float = 300.0,
        # Minimum render age for the desktop framebuffer capture — after
        # first content the VNC client still lets the app draw until this
        # much wall time has passed (env THUMB_VNC_SETTLE_SECONDS in the
        # capture container). Not a fixed pre-sleep: slow apps wait for
        # real content first, fast apps aren't held longer than this.
        desktop_settle_seconds: float = 8.0,
        settle_seconds: float = 0.5,
        width: int = 1280,
        height: int = 800,
        budget_ms: int = 10_000,
        desktop_budget_ms: int = 20_000,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._previews = previews
        self.image = image or os.environ.get("PREVIEW_THUMB_IMAGE") or DEFAULT_THUMB_IMAGE
        self.ready_seconds = ready_seconds
        self.capture_seconds = capture_seconds
        self.preview_ttl_seconds = preview_ttl_seconds
        self.desktop_settle_seconds = desktop_settle_seconds
        self.settle_seconds = settle_seconds
        self.width = width
        self.height = height
        self.budget_ms = budget_ms
        self.desktop_budget_ms = desktop_budget_ms
        self._sleep = sleep
        self._clock = clock
        self._store_lock = threading.Lock()

    # -- public API ----------------------------------------------------------

    def capture(
        self,
        *,
        run_id: str,
        sandbox_id: str,
        workspace: Path,
        artifact_dir: Path,
        plan: PreviewPlan,
    ) -> ThumbResult:
        """Serve ``workspace`` and screenshot it → ``thumbnail.png`` + meta.

        The preview stack is spawned with ``purpose=PURPOSE_THUMB`` and
        always stopped before returning. Raises :class:`ThumbnailError` on
        any failure (no half-written outputs are left in place).
        """
        if (
            self._previews._docker(
                ["image", "inspect", self.image], check=False
            ).returncode
            != 0
        ):
            raise ThumbnailError(
                f"thumbnail capture image {self.image!r} not found — "
                f"build it: docker build -f {THUMB_DOCKERFILE} "
                f"-t {self.image} ."
            )
        try:
            preview = self._previews.spawn(
                run_id=run_id,
                sandbox_id=sandbox_id,
                serve_dir=workspace,
                plan=plan,
                ttl_seconds=self.preview_ttl_seconds,
                purpose=PURPOSE_THUMB,
            )
        except ValueError:
            raise  # bad-plan errors keep their 422 mapping at the router
        except Exception as exc:
            # spawn's early-crash window raises RuntimeError carrying the
            # app's exit code + log tail — re-raise as ThumbnailError so
            # the 502 detail IS the app's reason (un-prefixed).
            raise ThumbnailError(str(exc)) from exc
        scratch: Path | None = None
        try:
            live = self._wait_ready(preview.preview_id, plan)
            settle = _pre_capture_settle(plan.type, self.settle_seconds)
            if settle > 0:
                self._sleep(settle)
            scratch = self._scratch_dir(artifact_dir)
            self._run_capture(
                live, scratch, run_id=run_id, sandbox_id=sandbox_id, plan=plan
            )
            return self._finalize(scratch, artifact_dir, plan)
        finally:
            try:
                self._previews.stop(preview.preview_id)
            except Exception:
                logger.warning(
                    "thumbnail: preview stack %s failed to stop",
                    preview.preview_id,
                    exc_info=True,
                )
            if scratch is not None:
                shutil.rmtree(scratch, ignore_errors=True)

    # -- steps ----------------------------------------------------------------

    def _wait_ready(self, preview_id: str, plan: PreviewPlan) -> Preview:
        """Confirm the stack exists and hasn't died on spawn.

        Serving readiness is probed inside the capture container — it
        shares the preview network, so its view of ``CAPTURE_URL`` is
        exactly what chromium loads. The api can't do the probe itself:
        the published port binds the HOST's loopback, unreachable when
        the api runs in a container (compose). A runner that already
        exited IS reported early (waiting won't help).
        """
        deadline = self._clock() + self.ready_seconds
        while self._clock() < deadline:
            pv = self._previews.get(preview_id)
            if pv is not None:
                if pv.status in DEAD_STATUSES:
                    tail = self._previews.logs(pv.container, tail=10)
                    raise ThumbnailError(
                        f"{plan.type} {pv.detail or 'preview app died'} "
                        "before serving — the artifact likely crashed on "
                        f"startup{f' — app logs: {tail}' if tail else ''}"
                    )
                return pv
            self._sleep(0.25)
        raise ThumbnailError(
            f"{plan.type} preview stack did not come up within "
            f"{self.ready_seconds:.0f}s"
        )

    @staticmethod
    def _capture_url(preview: Preview, plan: PreviewPlan) -> str:
        """In-network URL the headless browser loads — by container name on
        ``heg-preview`` (runner directly for static, the socat sidecar for
        executed-code types; the runner's own port is unreachable there).

        ``plan`` is the authoritative spawn input; the container name is
        the only piece taken from the inspected preview.
        """
        if plan.type == "static":
            path = urllib.parse.quote(_entry_url_path(plan.entry))
            return f"http://{preview.container}:{CONTAINER_PORT}{path}"
        proxied_port = _PROXIED_PORTS.get(plan.type)
        if proxied_port is None:
            raise ThumbnailError(f"no capture route for preview type {plan.type!r}")
        path = (
            "/vnc.html?autoconnect=true&resize=scale"
            if plan.type == "desktop"
            else "/"
        )
        return f"http://{preview.container}-proxy:{proxied_port}{path}"

    def _scratch_dir(self, artifact_dir: Path) -> Path:
        """World-writable scratch dir inside the artifact dir — bind-mounted
        at ``/out`` so the nobody-user chromium can drop the PNG."""
        scratch = artifact_dir / f".thumb-{uuid.uuid4().hex[:8]}"
        try:
            scratch.mkdir(mode=0o777)
            scratch.chmod(0o777)
        except OSError as exc:
            shutil.rmtree(scratch, ignore_errors=True)
            raise ThumbnailError(
                f"cannot create capture scratch dir under {artifact_dir}: {exc}"
            ) from exc
        return scratch

    def _run_capture(
        self,
        preview: Preview,
        scratch: Path,
        *,
        run_id: str,
        sandbox_id: str,
        plan: PreviewPlan,
    ) -> None:
        """One-shot ``docker run --rm`` of the headless-chromium image.

        Foreground (blocking) — the api already runs us in a thread; the
        subprocess timeout is the outer bound (covering the entrypoint's
        in-container ready wait plus the chromium run), the entrypoint's
        own ``timeout`` wrap is the inner one. On timeout the CLI is
        killed but the container may still run → ``rm -f`` by name.

        A watchdog thread polls the preview stack while the capture runs:
        a runner that exits mid-capture means the artifact crashed, so the
        capture container is removed immediately and the error carries the
        app's exit code + log tail — instead of burning the full ready
        budget on a URL that can never serve.
        """
        url = self._capture_url(preview, plan)
        name = f"{THUMB_CONTAINER_PREFIX}{uuid.uuid4().hex[:12]}"
        budget = (
            self.desktop_budget_ms if plan.type == "desktop" else self.budget_ms
        )
        rt = self._previews._rt  # same runtime => same host-path translation
        args = [
            "run",
            "--rm",
            "--name",
            name,
            "--network",
            self._previews.network,  # heg-preview — peers = preview stack only
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--user",
            "65534:65534",
            # chromium needs a writable HOME/profile; bounded tmpfs keeps the
            # rootfs read-only.
            "--tmpfs",
            "/tmp:rw,nosuid,nodev,size=128m",
            "-e",
            f"CAPTURE_URL={url}",
            "-e",
            f"THUMB_WIDTH={self.width}",
            "-e",
            f"THUMB_HEIGHT={self.height}",
            "-e",
            f"THUMB_BUDGET_MS={budget}",
            "-e",
            f"THUMB_READY_SECONDS={math.ceil(self.ready_seconds)}",
            "-e",
            f"THUMB_TIMEOUT_SECONDS={math.ceil(self.capture_seconds)}",
            "-e",
            "HOME=/tmp",
            *(
                [
                    # Desktop: skip the chromium/noVNC path entirely — a
                    # stdlib VNC client waits on real framebuffer content
                    # then writes the pixels straight to PNG (adaptive to
                    # slow-to-render apps; no virtual-time-budget cliff).
                    "-e",
                    (
                        f"THUMB_VNC_WS=ws://{preview.container}-proxy:"
                        f"{DESKTOP_CONTAINER_PORT}/websockify"
                    ),
                    "-e",
                    f"THUMB_VNC_WAIT_SECONDS={math.ceil(self.ready_seconds)}",
                    "-e",
                    f"THUMB_VNC_SETTLE_SECONDS={self.desktop_settle_seconds:g}",
                ]
                if plan.type == "desktop"
                else []
            ),
            "-v",
            f"{rt.to_host_path(scratch)}:{_OUT_DIR}",
            "--label",
            f"{LABEL_THUMB}=1",
            # run/sandbox labels ride the shared keys — run cleanup sweeps
            # capture containers exactly like preview/sandbox containers.
            "--label",
            f"heg.run_id={run_id}",
            "--label",
            f"heg.sandbox_id={sandbox_id}",
            self.image,
        ]
        stop_watch = threading.Event()

        def _watch() -> None:
            """Kill the capture container the moment the runner dies — the
            app crashing mid-capture makes the target URL unservable, so
            waiting out the ready budget would be pure latency."""
            while not stop_watch.is_set():
                pv = self._previews.get(preview.preview_id)
                if pv is not None and pv.status in DEAD_STATUSES:
                    self._previews._docker(["rm", "-f", name], check=False)
                    return
                stop_watch.wait(0.5)

        watcher = threading.Thread(target=_watch, daemon=True)
        watcher.start()
        try:
            try:
                proc = self._previews._docker(
                    args,
                    check=False,
                    timeout=self.ready_seconds + self.capture_seconds + 30,
                )
            except subprocess.TimeoutExpired as exc:
                self._previews._docker(["rm", "-f", name], check=False)
                raise ThumbnailError(
                    "capture exceeded the "
                    f"{self.ready_seconds + self.capture_seconds + 30:.0f}s bound "
                    "(ready wait + chromium run)"
                ) from exc
        finally:
            stop_watch.set()
            watcher.join(timeout=5)
        if proc.returncode != 0:
            # A dead runner explains the failure better than the capture's
            # own exit noise — report the app's exit code + log tail.
            pv = self._previews.get(preview.preview_id)
            if pv is not None and pv.status in DEAD_STATUSES:
                tail = self._previews.logs(pv.container, tail=10)
                raise ThumbnailError(
                    f"{plan.type} {pv.detail or 'preview app died'} during "
                    "capture — the artifact crashed before the screenshot "
                    f"could be taken{f' — app logs: {tail}' if tail else ''}"
                )
            tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-5:]
            if plan.type == "desktop" and proc.returncode in (
                _VNC_EXIT_TIMEOUT,
                124,
            ):
                msg = (
                    "timed out waiting for the app to render into the "
                    f"framebuffer ({self.ready_seconds:.0f}s) — the runner "
                    "is alive but produced no visible content"
                )
                if tail:
                    msg += f" ({' | '.join(tail)})"
                raise ThumbnailError(msg)
            raise ThumbnailError(
                "capture container failed "
                f"(exit {proc.returncode}): {' | '.join(tail) or 'no output'}"
            )

    def _finalize(
        self, scratch: Path, artifact_dir: Path, plan: PreviewPlan
    ) -> ThumbResult:
        """Validate the shot → ``thumbnail.png`` + ``thumbnail.json``.

        A non-PNG file is rejected — a poisoned artifact dir gets no
        thumbnail rather than a garbage one. The PNG is moved into place
        atomically (same filesystem); the meta sidecar is written by the
        api (never the container). Recorded dimensions come from the PNG's
        own IHDR — the desktop framebuffer shot is Xvnc's geometry, not
        the chromium viewport.
        """
        shot = scratch / _OUT_FILENAME
        try:
            with shot.open("rb") as fh:
                head = fh.read(24)
            size = shot.stat().st_size
        except OSError as exc:
            raise ThumbnailError(f"capture produced no screenshot: {exc}") from exc
        if head[: len(PNG_MAGIC)] != PNG_MAGIC:
            raise ThumbnailError(
                "capture output is not a PNG — refusing to store it"
            )
        # PNG IHDR: magic(8) len(4) "IHDR"(4) width(4) height(4).
        width, height = self.width, self.height
        try:
            width, height = struct.unpack(">II", head[16:24])
        except struct.error:
            pass
        dest = artifact_dir / THUMB_FILENAME
        captured = datetime.now(UTC)
        meta: dict[str, Any] = {
            "version": THUMB_META_VERSION,
            "captured_at": captured.isoformat(),
            "type": plan.type,
            "entry": plan.entry,
            "generator": (
                "vnc-framebuffer" if plan.type == "desktop" else "chromium-headless"
            ),
            "width": width,
            "height": height,
            "bytes": size,
        }
        meta_path = artifact_dir / THUMB_META_FILENAME
        with self._store_lock:
            try:
                os.replace(shot, dest)
            except OSError as exc:
                raise ThumbnailError(
                    f"cannot store thumbnail under {artifact_dir}: {exc}"
                ) from exc
            try:
                os.chmod(dest, 0o644)
            except OSError:
                # The file is nobody-owned (container uid) — a non-owner api
                # can't chmod it; the container umask already yields a
                # readable file.
                pass
            try:
                meta_path.unlink(missing_ok=True)
                meta_path.write_text(
                    json.dumps(meta, indent=2) + "\n", encoding="utf-8"
                )
            except OSError:
                # The PNG is the artifact; the sidecar is best-effort
                # metadata.
                logger.warning(
                    "thumbnail: meta write failed under %s", artifact_dir,
                    exc_info=True,
                )
        logger.info(
            "thumbnail captured: %s (%s %s, %d bytes)",
            dest, plan.type, plan.entry, size,
        )
        return ThumbResult(
            path=dest,
            meta_path=artifact_dir / THUMB_META_FILENAME,
            captured_at=captured,
            type=plan.type,
            entry=plan.entry,
        )
