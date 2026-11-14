#!/usr/bin/env python3
"""heg-preview-server runner — launch a candidate web app + TCP relay.

Runs as the ENTRYPOINT of the ``heg-preview-server`` container, which lives
alone on the ``--internal`` preview net: zero egress, and the only reachable
peer is this preview's own socat sidecar. The sidecar targets this
container's ``RELAY_PORT`` (8000) — so the runner's job is to make the
candidate app reachable on ``0.0.0.0:8000`` no matter what the artifact
actually does.

Candidate apps bind anything: flask dev server defaults to
``127.0.0.1:5000``, uvicorn to ``127.0.0.1:8000``, agent code hardcodes
``:3000``/``:8080``… So after spawning the app we watch
``/proc/net/tcp{,6}`` for its first new LISTEN socket and then:

- the app already listens on a **non-loopback :8000** → the sidecar reaches
  it directly — nothing to relay;
- otherwise we run a tiny asyncio TCP relay ``:8000 → <detected>:<port>``.
  Binding ``0.0.0.0:8000`` fails when the app holds ``127.0.0.1:8000`` —
  then the relay binds the container's primary (eth0) IP on :8000 instead,
  which is exactly what the sidecar's ``TCP:<name>:8000`` resolves to.

Contract (set by ``PreviewManager._spawn_server``):

- ``APP_ENTRY`` — required; the app entry point relative to the staged
  workspace copy (``/work``). ``manage.py`` (django) gets
  ``runserver 0.0.0.0:8000 --noreload`` rather than a bare ``python
  manage.py``.
- ``PORT``/``HOST`` — passed through to the app as hints; apps honoring
  them bind ``:8000`` and skip the relay entirely.
- The workspace is bind-mounted ``:ro`` at ``/srv/artifact-ro`` and staged
  to writable ``/work`` (apps write state next to the entry).

Lifecycle: the runner exits when the app exits (its code propagates), so
``docker inspect`` shows ``exited`` when the artifact crashes — same
visible-failure semantics as the desktop runner. While the app lives but
hasn't listened yet the runner keeps waiting (a heartbeat log every ~15s);
the preview TTL reaper is the outer bound.
"""

from __future__ import annotations

import asyncio
import ipaddress
import os
import shutil
import sys
import time
from pathlib import Path

ARTIFACT_RO = Path("/srv/artifact-ro")
WORK = Path("/work")

RELAY_PORT = int(os.environ.get("RUNNER_BIND_PORT", "8000"))
APP_PORT = RELAY_PORT  # canonical port the socat sidecar targets
_CHUNK = 64 * 1024


def _log(msg: str) -> None:
    print(f"[heg-preview-server] {msg}", flush=True)


def _decode_addr(addr_hex: str, v6: bool) -> str:
    """Decode a ``/proc/net/tcp{,6}`` local_address field to an IP string.

    IPv4 is stored little-endian; IPv6 as four little-endian 32-bit words.
    """
    raw = bytes.fromhex(addr_hex)
    if not v6:
        return str(ipaddress.IPv4Address(raw[::-1]))
    fixed = b"".join(raw[i : i + 4][::-1] for i in range(0, 16, 4))
    return str(ipaddress.IPv6Address(fixed))


def _is_wildcard(addr: str) -> bool:
    return addr in ("0.0.0.0", "::")


def _listeners() -> set[tuple[str, int]]:
    """``{(host, port)}`` for every LISTEN socket in ``/proc/net/tcp{,6}``.

    Hosts stay RAW (wildcards kept as ``0.0.0.0``/``::``) — collapsing a
    wildcard :8000 bind to loopback would misroute it through the relay
    (see ``_run``: a non-loopback :8000 listener is reachable by the
    sidecar directly and needs no relay at all).
    """
    out: set[tuple[str, int]] = set()
    for path, v6 in (("/proc/net/tcp", False), ("/proc/net/tcp6", True)):
        try:
            lines = Path(path).read_text().splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            parts = line.split()
            # st 0A == TCP_LISTEN; skip anything else + malformed rows.
            if len(parts) < 4 or parts[3] != "0A":
                continue
            addr_hex, _, port_hex = parts[1].rpartition(":")
            try:
                out.add((_decode_addr(addr_hex, v6), int(port_hex, 16)))
            except ValueError:
                continue
    return out


def _is_loopback(host: str) -> bool:
    return host.startswith("127.") or host == "::1"


def _pick(listeners: set[tuple[str, int]]) -> tuple[str, int]:
    """Choose the app's port among new listeners — the honored-``PORT``
    (canonical) one first, then lowest port; host sorts as a tiebreak so
    the choice is deterministic."""
    return min(listeners, key=lambda hp: (hp[1] != APP_PORT, hp[1], hp[0]))


def _primary_ip() -> str | None:
    """The container's eth0 IP — the address the sidecar's DNS name for us
    resolves to. Needed when the app squats on 127.0.0.1:8000 and the
    relay therefore can't wildcard-bind :8000."""
    import socket

    try:
        return socket.gethostbyname(socket.gethostname())
    except OSError:
        return None


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while chunk := await reader.read(_CHUNK):
            writer.write(chunk)
            await writer.drain()
    except (ConnectionResetError, BrokenPipeError, asyncio.IncompleteReadError):
        pass
    finally:
        try:
            writer.close()
        except OSError:
            pass


async def _connect(host: str, port: int) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Connect to the app, falling back across loopback families.

    Wildcard binds (``0.0.0.0``/``::``) accept loopback traffic, so connect
    via ``127.0.0.1``/``::1`` rather than the unspecified address; the
    ``::1`` fallback covers v6only binds that refuse v4-mapped traffic.
    """
    candidates = (
        ["127.0.0.1", "::1"]
        if _is_wildcard(host)
        else [host, "127.0.0.1", "::1"]
    )
    last: OSError | None = None
    for cand in dict.fromkeys(candidates):
        try:
            return await asyncio.open_connection(cand, port)
        except OSError as exc:
            last = exc
    assert last is not None
    raise last


async def _handle(
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
    target_host: str,
    target_port: int,
) -> None:
    try:
        app_reader, app_writer = await _connect(target_host, target_port)
    except OSError:
        try:
            client_writer.close()
        except OSError:
            pass
        return
    await asyncio.gather(
        _pipe(client_reader, app_writer),
        _pipe(app_reader, client_writer),
        return_exceptions=True,
    )


def _stage_workspace() -> Path:
    """Copy the :ro-mounted workspace into writable /work (apps save state)."""
    try:
        shutil.copytree(ARTIFACT_RO, WORK, dirs_exist_ok=True, symlinks=True)
    except OSError as exc:
        _log(f"warning: workspace staging failed ({exc}) — running from {ARTIFACT_RO}")
        return ARTIFACT_RO
    return WORK


def _app_cmd(workdir_root: Path, entry: str) -> tuple[list[str], str]:
    """Command + cwd for the app. ``manage.py`` gets django's runserver
    contract (binds 0.0.0.0:8000 directly, --noreload keeps one process)."""
    entry_path = workdir_root / entry
    if Path(entry).name == "manage.py":
        return (
            [sys.executable, str(entry_path),
             "runserver", f"0.0.0.0:{APP_PORT}", "--noreload"],
            str(entry_path.parent),
        )
    return [sys.executable, str(entry_path)], str(entry_path.parent)


async def _run() -> int:
    entry = os.environ.get("APP_ENTRY", "").strip()
    if not entry:
        _log("APP_ENTRY not set — nothing to run")
        return 2

    root = _stage_workspace()
    cmd, cwd = _app_cmd(root, entry)
    # sys.path[0] is the entry's dir — put the workspace root on PYTHONPATH
    # too so nested entries can import root-level modules.
    pythonpath = str(root)
    if os.environ.get("PYTHONPATH"):
        pythonpath += os.pathsep + os.environ["PYTHONPATH"]
    env = {
        **os.environ,
        "PORT": str(APP_PORT),
        "HOST": "0.0.0.0",
        "PYTHONUNBUFFERED": "1",
        "PYTHONPATH": pythonpath,
    }
    baseline = _listeners()  # pre-spawn snapshot — the app's binds show as new
    _log(f"launching {entry}: {' '.join(cmd)} (cwd={cwd})")
    proc = await asyncio.create_subprocess_exec(*cmd, cwd=cwd, env=env)

    # Wait for the app's first LISTEN socket while it stays alive.
    target: tuple[str, int] | None = None
    next_heartbeat = time.monotonic() + 15.0
    while proc.returncode is None:
        new = _listeners() - baseline
        if new:
            target = _pick(new)
            break
        if time.monotonic() >= next_heartbeat:
            _log(f"still waiting for {entry} to open a listening port…")
            next_heartbeat = time.monotonic() + 15.0
        await asyncio.sleep(0.25)

    if target is None:
        _log(f"{entry} exited ({proc.returncode}) before listening on a port")
        return proc.returncode or 1

    host, port = target
    server: asyncio.AbstractServer | None = None
    if port == APP_PORT and not _is_loopback(host):
        _log(f"{entry} serving directly on {host}:{port} — no relay needed")
    else:
        bind_host = "0.0.0.0"
        if any(p == APP_PORT for _, p in _listeners()):
            # App squats on :8000 loopback — wildcard bind would fail; bind
            # the eth0 IP the sidecar resolves our name to instead.
            bind_host = _primary_ip() or "0.0.0.0"
        try:
            server = await asyncio.start_server(
                lambda r, w: _handle(r, w, host, port), bind_host, APP_PORT
            )
        except OSError as exc:
            _log(f"relay failed to bind {bind_host}:{APP_PORT}: {exc}")
            proc.terminate()
            return 1
        _log(f"relay {bind_host}:{APP_PORT} -> {host}:{port}")

    # Runner life == app life: a crashed app -> exited preview (visible),
    # same as the desktop runner's `exec python`.
    while proc.returncode is None:
        await asyncio.sleep(0.5)
    if server is not None:
        server.close()
        await server.wait_closed()
    _log(f"{entry} exited ({proc.returncode})")
    return proc.returncode or 0


def main() -> int:
    try:
        return asyncio.run(_run())
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
