#!/usr/bin/env python3
"""heg-thumb VNC framebuffer waiter/screenshotter — stdlib only.

The desktop preview runner keeps its Xvnc display loopback-only inside
the container; websockify (:6080, reached as ``<runner>-proxy`` on the
``heg-preview`` bridge) is the only window onto it. Chromium +
``--virtual-time-budget`` could not shoot it reliably — virtual time
burns through the budget while real framebuffer updates are still
seconds away, so thumbnails landed on noVNC's "Connecting…" spinner or
a blank canvas.

This client speaks just enough RFC6455 + RFB (RAW encoding) to read the
framebuffer directly: it polls until the screen has real content, waits
``THUMB_VNC_SETTLE_SECONDS`` of draw margin, then writes the pixels
straight to ``$OUT`` as a PNG — the shot IS the virtual display, exactly
what the noVNC tab would show. A crashed app fails fast: the runner's
entrypoint ``exec``s the artifact, so the app exiting takes Xvnc +
websockify down with it — the channel drops and we exit 3 instead of
idling out the wait budget.

Exit codes: ``0`` wrote the PNG; ``3`` the VNC channel dropped mid-wait
(the api then reports the runner's own exit code + logs); ``4`` the
framebuffer stayed blank past the wait deadline; ``2`` usage/protocol
error.

Env (injected by ``Thumbnailer._run_capture`` for ``plan.type ==
"desktop"``):

- ``THUMB_VNC_WS`` — required, ``ws://<proxy>:6080/websockify``
- ``THUMB_VNC_WAIT_SECONDS`` — hard deadline for real content (default 30)
- ``THUMB_VNC_SETTLE_SECONDS`` — minimum render age before the shot:
  first content only "counts" once this much wall time has passed, so a
  splash frame can't end the wait early (default 5)
- ``THUMB_VNC_MIN_COLORS`` — sampled distinct-color threshold for
  "has content" (default 2; a uniform root stays blank — a solid-fill
  app screen, 2-tone, must NOT)
- ``THUMB_VNC_POLL_SECONDS`` — delay between full framebuffer refresh
  requests while blank (default 1.0)
- ``OUT`` — PNG output path (default ``/out/capture.png``)
"""

from __future__ import annotations

import base64
import os
import socket
import struct
import sys
import time
import urllib.parse
import zlib

OUT = os.environ.get("OUT", "/out/capture.png")
WAIT_SECONDS = float(os.environ.get("THUMB_VNC_WAIT_SECONDS", "30"))
SETTLE_SECONDS = float(os.environ.get("THUMB_VNC_SETTLE_SECONDS", "5"))
MIN_COLORS = int(os.environ.get("THUMB_VNC_MIN_COLORS", "2"))
POLL_SECONDS = float(os.environ.get("THUMB_VNC_POLL_SECONDS", "1.0"))

EXIT_DROPPED = 3
EXIT_BLANK_TIMEOUT = 4
EXIT_ERROR = 2

# Fraction of sampled pixels that may equal the dominant color and still
# count as "blank" — a drawn mouse cursor or X root-weave dots stay under
# it; any real app screen (text, sprites, canvas) is well over.
_BLANK_DOMINANT = 0.998
_SAMPLE_STRIDE = 23  # every Nth pixel — ~34k samples at 1024x768
_CONNECT_RETRY = 1.0
_MAX_RECONNECTS = 3


def _log(msg: str) -> None:
    print(f"[heg-thumb-vnc] {msg}", file=sys.stderr, flush=True)


class _WS:
    """Minimal RFC6455 client — binary frames in/out over one socket.

    websockify serves raw VNC bytes as binary websocket messages; server
    frames arrive unmasked, client frames must be masked per spec.
    """

    def __init__(self, url: str, timeout: float) -> None:
        parts = urllib.parse.urlsplit(url)
        if parts.scheme != "ws" or parts.hostname is None:
            raise ValueError(f"unsupported ws url {url!r}")
        host = parts.hostname
        port = parts.port or 80
        path = parts.path or "/"
        if parts.query:
            path += f"?{parts.query}"
        self._sock = socket.create_connection((host, port), timeout=timeout)
        self._sock.settimeout(timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "Sec-WebSocket-Protocol: binary\r\n"
            "\r\n"
        )
        self._sock.sendall(request.encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise ConnectionError("websocket handshake: connection closed")
            head += chunk
            if len(head) > 16384:
                raise ConnectionError("websocket handshake: header overflow")
        head, _, rest = head.partition(b"\r\n\r\n")
        status_line = head.split(b"\r\n", 1)[0]
        if b" 101" not in status_line:
            raise ConnectionError(
                f"websocket upgrade refused: {status_line.decode('utf-8', 'replace')}"
            )
        self._rx = bytearray(rest)  # bytes past the HTTP headers
        self._buf = bytearray()  # decoded binary-message stream

    def _read_exact(self, n: int) -> bytes:
        while len(self._rx) < n:
            chunk = self._sock.recv(65536)
            if not chunk:
                raise ConnectionError("websocket closed")
            self._rx += chunk
        out = bytes(self._rx[:n])
        del self._rx[:n]
        return out

    def _pump(self) -> None:
        """Read frames until a complete binary message lands in _buf."""
        while True:
            b1, b2 = self._read_exact(2)
            fin = bool(b1 & 0x80)
            opcode = b1 & 0x0F
            masked = bool(b2 & 0x80)
            length = b2 & 0x7F
            if length == 126:
                (length,) = struct.unpack(">H", self._read_exact(2))
            elif length == 127:
                (length,) = struct.unpack(">Q", self._read_exact(8))
            mask = self._read_exact(4) if masked else b""
            payload = bytearray(self._read_exact(length))
            if masked:
                for i in range(length):
                    payload[i] ^= mask[i % 4]
            if opcode == 0x9:  # ping → pong
                self._send(0xA, bytes(payload))
                continue
            if opcode == 0x8:
                raise ConnectionError("websocket close frame")
            if opcode in (0x0, 0x1, 0x2):  # continuation / text / binary
                self._buf += payload
                if fin:
                    return
            # other opcodes are control/reserved — ignore

    def read(self, n: int) -> bytes:
        """n bytes of the binary stream (reassembled across frames)."""
        while len(self._buf) < n:
            self._pump()
        out = bytes(self._buf[:n])
        del self._buf[:n]
        return out

    def _send(self, opcode: int, payload: bytes) -> None:
        mask = os.urandom(4)
        header = bytes([0x80 | opcode])
        n = len(payload)
        if n < 126:
            header += bytes([0x80 | n])
        elif n < 65536:
            header += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            header += bytes([0x80 | 127]) + struct.pack(">Q", n)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self._sock.sendall(header + mask + masked)

    def send(self, payload: bytes) -> None:
        self._send(0x2, payload)

    def close(self) -> None:
        try:
            self._send(0x8, b"")
        except OSError:
            pass
        try:
            self._sock.close()
        except OSError:
            pass


class _DroppedError(ConnectionError):
    """The VNC channel went away — the runner almost certainly exited."""


class _RFB:
    """Just enough RFB (003.008, SecurityTypes None, RAW encoding) to grab
    full-screen framebuffer updates from Xvnc."""

    def __init__(self, ws: _WS) -> None:
        self.ws = ws
        version = ws.read(12)
        if not version.startswith(b"RFB ") or version[8:11] != b"008":
            raise RuntimeError(f"unsupported RFB greeting {version!r}")
        ws.send(version[:11] + b"\n")
        (nsec,) = struct.unpack(">B", ws.read(1))
        sectypes = set(ws.read(nsec)) if nsec else set()
        if 1 not in sectypes:
            raise RuntimeError(
                f"VNC 'None' security not offered (got {sorted(sectypes)})"
            )
        ws.send(b"\x01")  # choose: None
        (result,) = struct.unpack(">I", ws.read(4))
        if result != 0:
            raise RuntimeError(f"VNC security handshake refused ({result})")
        ws.send(b"\x01")  # ClientInit: shared session
        server_init = ws.read(24)
        self.width, self.height = struct.unpack(">HH", server_init[:4])
        (name_len,) = struct.unpack(">I", server_init[20:24])
        ws.read(name_len)
        # Ask for a fixed 32bpp true-colour little-endian format + RAW only
        # so decoding below has exactly one shape: B,G,R,X per pixel.
        pixel_format = struct.pack(
            ">BBBBHHHBBB3x", 32, 24, 0, 1, 255, 255, 255, 16, 8, 0
        )
        ws.send(b"\x00" + b"\x00" * 3 + pixel_format)  # SetPixelFormat
        ws.send(b"\x02" + b"\x00" + struct.pack(">Hi", 1, 0))  # SetEncodings [RAW]
        self.fb = bytearray(self.width * self.height * 3)  # RGB

    def request_update(self) -> None:
        """FramebufferUpdateRequest — full screen, non-incremental."""
        self.ws.send(
            b"\x03" + b"\x00" + struct.pack(">HHHH", 0, 0, self.width, self.height)
        )

    def read_update(self) -> None:
        """Read server messages until a FramebufferUpdate is painted."""
        while True:
            (mtype,) = struct.unpack(">B", self.ws.read(1))
            if mtype == 0:  # FramebufferUpdate
                self.ws.read(1)  # padding
                (nrects,) = struct.unpack(">H", self.ws.read(2))
                for _ in range(nrects):
                    x, y, w, h, enc = struct.unpack(">HHHHi", self.ws.read(12))
                    if enc != 0:
                        raise RuntimeError(f"non-RAW encoding {enc} received")
                    data = self.ws.read(w * h * 4)
                    self._paint(x, y, w, h, data)
                return
            if mtype == 1:  # SetColourMapEntries
                hdr = self.ws.read(5)
                (ncolors,) = struct.unpack(">H", hdr[3:5])
                self.ws.read(ncolors * 6)
            elif mtype == 2:  # Bell
                continue
            elif mtype == 3:  # ServerCutText
                self.ws.read(3)
                (length,) = struct.unpack(">I", self.ws.read(4))
                self.ws.read(length)
            else:
                raise RuntimeError(f"unknown VNC server message {mtype}")

    def _paint(self, x: int, y: int, w: int, h: int, data: bytes) -> None:
        """Composite a RAW rect (B,G,R,X per pixel) into the RGB buffer.

        Extended-slice channel copies — a 1024x768 frame is ~786k pixels;
        per-pixel Python loops cost ~1s/frame vs milliseconds here.
        """
        width = self.width
        fb = self.fb
        for row in range(h):
            src = row * w * 4
            row_px = data[src : src + w * 4]
            dst = ((y + row) * width + x) * 3
            end = dst + w * 3
            fb[dst:end:3] = row_px[2::4]  # R
            fb[dst + 1 : end : 3] = row_px[1::4]  # G
            fb[dst + 2 : end : 3] = row_px[0::4]  # B

    def has_content(self) -> bool:
        """True when the framebuffer looks rendered, not a blank root.

        Two signals, both sampled: enough DISTINCT colors (a uniform
        root/cursor stays under — a solid-fill app screen, 2-tone, must
        NOT) AND enough pixels disagreeing with the dominant color (a
        lone drawn cursor stays under).
        """
        fb = self.fb
        colors: set[bytes] = set()
        counts: dict[bytes, int] = {}
        total = 0
        for off in range(0, len(fb), _SAMPLE_STRIDE * 3):
            px = bytes(fb[off : off + 3])
            colors.add(px)
            counts[px] = counts.get(px, 0) + 1
            total += 1
            if len(colors) >= MIN_COLORS and total > 512:
                # Early exit once the color threshold is clearly met.
                dominant = max(counts.values())
                if dominant < total * _BLANK_DOMINANT:
                    return True
        if len(colors) < MIN_COLORS or total == 0:
            return False
        dominant = max(counts.values())
        return dominant < total * _BLANK_DOMINANT


def _write_png(path: str, width: int, height: int, rgb: bytearray) -> None:
    """Minimal RGB PNG writer (8-bit truecolor, filter 0)."""

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    rows = bytearray()
    stride = width * 3
    for y in range(height):
        rows.append(0)
        rows += rgb[y * stride : (y + 1) * stride]
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(bytes(rows), 6))
        + chunk(b"IEND", b"")
    )
    with open(path, "wb") as fh:
        fh.write(png)


def _connect(ws_url: str, deadline: float) -> _RFB | None:
    """One RFB connection attempt, or None when the deadline is near."""
    remaining = deadline - time.monotonic()
    if remaining <= 0.5:
        return None
    ws = _WS(ws_url, timeout=min(10.0, remaining))
    return _RFB(ws)


def main() -> int:
    ws_url = os.environ.get("THUMB_VNC_WS", "").strip()
    if not ws_url:
        _log("THUMB_VNC_WS is required")
        return EXIT_ERROR

    started = time.monotonic()
    deadline = started + WAIT_SECONDS
    rfb: _RFB | None = None
    drops = 0
    ever_connected = False

    # Phase 1 — connect + wait for real framebuffer content. Connect
    # failures are retried (websockify may lag the proxy), but once a
    # session WAS live a drop means the runner died → fail fast.
    while True:
        try:
            if rfb is None:
                rfb = _connect(ws_url, deadline)
                if rfb is None:
                    break
                ever_connected = True
                drops = 0
                _log(
                    f"vnc session up: {rfb.width}x{rfb.height} — "
                    "waiting for rendered content"
                )
            rfb.request_update()
            rfb.read_update()
        except (ConnectionError, OSError) as exc:
            drops += 1
            if rfb is not None:
                rfb.ws.close()
            rfb = None
            if ever_connected and drops >= _MAX_RECONNECTS:
                _log(f"vnc channel dropped mid-wait ({exc}) — app likely exited")
                return EXIT_DROPPED
            if time.monotonic() > deadline:
                break
            time.sleep(_CONNECT_RETRY)
            continue
        except RuntimeError as exc:
            _log(f"rfb protocol error: {exc}")
            return EXIT_ERROR

        elapsed = time.monotonic() - started
        if rfb.has_content() and elapsed >= SETTLE_SECONDS:
            break
        if time.monotonic() > deadline:
            break
        time.sleep(POLL_SECONDS)

    if rfb is None or not rfb.has_content():
        waited = time.monotonic() - started
        _log(
            f"framebuffer stayed blank for {waited:.1f}s "
            f"(connected={'yes' if ever_connected else 'no'})"
        )
        return EXIT_BLANK_TIMEOUT

    # One last fresh frame right before the shot — the last polled frame
    # could be up to POLL_SECONDS stale.
    try:
        rfb.request_update()
        rfb.read_update()
    except (ConnectionError, OSError, RuntimeError) as exc:
        _log(f"final frame grab failed ({exc}) — using last painted frame")
    _write_png(OUT, rfb.width, rfb.height, rfb.fb)
    _log(f"wrote {OUT} ({rfb.width}x{rfb.height})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
