"""Artifact preview — serve a snapshot workspace from a locked-down container (F10).

Product flow: results page "Preview" button → ``POST .../preview`` → this
module spawns a tiny static-file container bound to an ephemeral localhost
port → UI opens the URL in a new tab. ``DELETE`` stops it; a TTL reaper
auto-stops containers after ``PREVIEW_TTL_SECONDS`` (default 15 min).

Design notes:

- **Preview types.** ``detect_preview`` returns a :class:`PreviewPlan` whose
  ``type`` selects the serving strategy. Only ``"static"`` is implemented
  (``python -m http.server``); the plan/label/API shape deliberately leaves
  room for ``python-server`` / ``desktop`` (pygame via noVNC — post-checkpoint).
- **Network isolation.** Preview containers join a dedicated bridge network
  ``heg-preview`` — *not* ``heg-sandbox`` (which has the gateway attached).
  Docker cannot publish ports on ``--internal`` networks (the binding is
  silently dropped — verified: ``docker inspect`` shows ``{"8000/tcp":[]}``),
  so "internal net + reachable port" is impossible; the chosen posture is a
  dedicated bridge with the port bound to ``127.0.0.1`` (host-local only) and
  ``enable_ip_masquerade=false`` (blocks outbound NAT on Linux; inert on
  Docker Desktop). The hardening flags carry the weight: nobody user,
  read-only rootfs, cap-drop ALL, no-new-privileges, and a pinned image
  running ``http.server`` — no candidate code executes inside the container.
- **Jail.** ``resolve_workspace_dir`` reuses the download-jail pattern from
  ``api/routers/results.py``: the served directory must resolve inside
  ``ARTIFACT_ROOT`` — ``..`` escapes and symlinked ``workspace`` dirs that
  resolve outside are rejected. The artifact dir is bind-mounted read-only.
- **Registry = docker labels.** Every preview container carries
  ``heg.preview=1`` + id/run/sandbox/type/entry/expiry labels, so a restart
  of the api loses nothing: ``list()`` rebuilds state from ``docker inspect``
  and the reaper still enforces expiry written on the container itself.
- Sync like :class:`sandbox.runtime.SandboxRuntime` (docker CLI is sync);
  the api wraps calls in ``asyncio.to_thread``.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sandbox.runtime import SandboxRuntime

logger = logging.getLogger(__name__)

PREVIEW_NETWORK = "heg-preview"
CONTAINER_PREFIX = "heg-prev-"
# Same base image as the api + sandbox images — typically already pulled.
DEFAULT_IMAGE = "python:3.12-slim"
DEFAULT_BIND_HOST = "127.0.0.1"
DEFAULT_TTL_SECONDS = 900.0
CONTAINER_PORT = 8000
SERVE_ROOT = "/srv/artifact"

# Labels — ``heg.preview`` marks manager ownership; the rest rebuild Preview
# state after an api restart (and let the reaper enforce TTL out-of-process).
LABEL_MANAGED = "heg.preview"
LABEL_PREVIEW_ID = "heg.preview_id"
LABEL_RUN_ID = "heg.run_id"  # shared with sandboxes -> run cleanup sweeps these too
LABEL_SANDBOX_ID = "heg.sandbox_id"
LABEL_TYPE = "heg.preview_type"
LABEL_ENTRY = "heg.preview_entry"
LABEL_EXPIRES_AT = "heg.preview_expires_at"  # epoch seconds

#: Container statuses where the preview is (or will shortly be) serving.
LIVE_STATUSES = frozenset({"created", "running", "restarting", "paused"})

_HTML_SUFFIXES = frozenset({".html", ".htm"})
_INDEX_NAMES = frozenset({"index.html", "index.htm"})


@dataclass(frozen=True)
class PreviewPlan:
    """How one snapshot workspace can be previewed.

    ``type == "static"`` → serve the whole workspace dir over HTTP; ``entry``
    is the docroot-relative entry point (``"index.html"``, ``"game.html"``,
    ``"dist/index.html"``). Future types (``python-server``, ``desktop``)
    extend this rather than the API shape — ``PreviewOut`` already carries
    ``type``/``url``/``entry``.
    """

    type: str
    entry: str


@dataclass(frozen=True)
class Preview:
    """One preview container, rebuilt from docker inspect."""

    preview_id: str
    run_id: str
    sandbox_id: str
    type: str
    container: str
    status: str  # docker State.Status: created|running|exited|dead|...
    port: int | None
    entry: str | None
    url: str | None
    created_at: datetime | None
    expires_at: datetime | None

    @property
    def live(self) -> bool:
        return self.status in LIVE_STATUSES


# ---------------------------------------------------------------------------
# detection + jail resolution (pure filesystem — no docker)
# ---------------------------------------------------------------------------


def _within(child: Path, root: Path) -> bool:
    """True when ``child`` resolves to ``root`` or inside it (symlink-safe)."""
    try:
        resolved = child.resolve()
    except OSError:
        return False
    return resolved == root or root in resolved.parents


def resolve_workspace_dir(
    *,
    jail: Path,
    run_id: str,
    sandbox_id: str,
    artifact_path: str | None,
) -> Path | None:
    """Return ``<artifact_dir>/workspace`` for a sandbox, jailed under ARTIFACT_ROOT.

    ``Sandbox.artifact_path`` is tried first (it's where the snapshot actually
    landed), then the conventional ``<jail>/<run_id>/<sandbox_id>`` layout —
    both must resolve inside ``jail``. The ``workspace`` entry itself is also
    resolved: a candidate-planted symlink escaping the jail yields ``None``
    rather than a host path bind-mounted into a container.
    """
    try:
        jail = jail.resolve()
    except OSError:
        return None
    candidates: list[Path] = []
    if artifact_path:
        candidates.append(Path(artifact_path))
    candidates.append(jail / str(run_id) / str(sandbox_id))

    for cand in candidates:
        try:
            resolved = cand.resolve()
        except OSError:
            continue
        if not _within(resolved, jail):
            continue
        try:
            ws = (resolved / "workspace").resolve()
        except OSError:
            continue
        if _within(ws, jail) and ws != jail and ws.is_dir():
            return ws
    return None


def _html_files(dirpath: Path) -> list[Path]:
    """Regular .html/.htm files directly inside ``dirpath``, name-sorted."""
    try:
        entries = sorted(dirpath.iterdir(), key=lambda p: p.name)
    except OSError:
        return []
    return [
        p
        for p in entries
        if p.is_file() and not p.is_symlink() and p.suffix.lower() in _HTML_SUFFIXES
    ]


def detect_preview(workspace: Path) -> PreviewPlan | None:
    """Find an unambiguous static entry point in a snapshot workspace.

    Order (first match wins):
    1. ``index.html``/``index.htm`` at the workspace root (case-insensitive).
    2. A single other ``*.html``/``*.htm`` file at the root (``game.html``).
    3. A unique ``*/index.html`` one level down (``dist/``, ``game/``) —
       hidden dirs (``.git``) are skipped.

    Anything ambiguous (multiple root html files, multiple nested indexes)
    returns ``None`` → the API reports the sandbox as not previewable rather
    than guessing. Symlinked entries/dirs are skipped outright: a candidate
    could plant a symlink escaping the jail, and the jail check has already
    happened by this point.
    """
    if not workspace.is_dir():
        return None

    root_html = _html_files(workspace)
    for p in root_html:
        if p.name.lower() in _INDEX_NAMES:
            return PreviewPlan(type="static", entry=p.name)
    if len(root_html) == 1:
        return PreviewPlan(type="static", entry=root_html[0].name)

    nested: list[Path] = []
    try:
        entries = sorted(workspace.iterdir(), key=lambda p: p.name)
    except OSError:
        return None
    for d in entries:
        if d.name.startswith(".") or d.is_symlink() or not d.is_dir():
            continue
        for cand in _html_files(d):
            if cand.name.lower() in _INDEX_NAMES:
                nested.append(cand)
                break
    if len(nested) == 1:
        return PreviewPlan(type="static", entry=str(nested[0].relative_to(workspace)))
    return None


def _preview_url(port: int | None, entry: str | None) -> str | None:
    """Browser URL for a preview — entry dir for indexes, file path otherwise."""
    if port is None:
        return None
    if not entry:
        return f"http://localhost:{port}/"
    base = entry.rsplit("/", 1)[-1]
    if base.lower() in _INDEX_NAMES:
        parent = entry[: -len(base)].lstrip("/")
        return f"http://localhost:{port}/{parent}"
    return f"http://localhost:{port}/{entry}"


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        ts = datetime.fromisoformat(value)
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=UTC)


# ---------------------------------------------------------------------------
# PreviewManager — docker lifecycle for preview containers
# ---------------------------------------------------------------------------


class PreviewManager:
    """Docker-backed lifecycle for artifact-preview containers.

    Composes a :class:`SandboxRuntime` for the shared docker-CLI plumbing and
    ``to_host_path`` bind-source translation (compose-mode api). Owns its own
    network (``heg-preview``) and naming (``heg-prev-<id>``) so preview
    containers never collide with agent sandboxes (``heg-<sandbox_id>``).
    """

    def __init__(
        self,
        *,
        runtime: SandboxRuntime | None = None,
        image: str | None = None,
        network: str = PREVIEW_NETWORK,
        bind_host: str = DEFAULT_BIND_HOST,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
    ) -> None:
        self._rt = runtime or SandboxRuntime()
        self.image = (
            image or os.environ.get("PREVIEW_IMAGE") or DEFAULT_IMAGE
        )
        self.network = network
        self.bind_host = bind_host
        self.ttl_seconds = ttl_seconds

    # -- docker plumbing (delegates to the composed runtime) -----------------

    def _docker(self, args: list[str], *, check: bool = True, timeout: float | None = None):
        return self._rt._docker(args, check=check, timeout=timeout)

    def ensure_network(self) -> None:
        """Create the dedicated preview bridge if missing (idempotent).

        NOT ``--internal``: published ports are silently dropped on internal
        networks, which would make the preview unreachable. ``heg-preview``
        only ever carries preview containers (no gateway, no services); the
        masquerade option is a best-effort egress restriction (effective on
        Linux, inert on Docker Desktop). An existing network found to BE
        internal is a hard misconfiguration — previews would spawn but never
        be reachable — so it logs a loud warning.
        """
        proc = self._docker(["network", "inspect", self.network], check=False)
        if proc.returncode != 0:
            created = self._docker(
                [
                    "network",
                    "create",
                    "--driver",
                    "bridge",
                    "-o",
                    "com.docker.network.bridge.enable_ip_masquerade=false",
                    self.network,
                ],
                check=False,
            )
            if created.returncode != 0:
                # Older daemon without the option — retry a plain bridge.
                self._docker(
                    ["network", "create", "--driver", "bridge", self.network]
                )
            logger.info("created preview docker network %r", self.network)
            return
        try:
            info = json.loads(proc.stdout)[0]
        except (json.JSONDecodeError, IndexError):
            info = {}
        if info.get("Internal", False):
            logger.warning(
                "docker network %r exists and IS internal — published ports "
                "are dropped on internal networks, so previews will spawn "
                "but never be reachable!",
                self.network,
            )

    # -- spawn ---------------------------------------------------------------

    def spawn(
        self,
        *,
        run_id: str,
        sandbox_id: str,
        serve_dir: Path,
        plan: PreviewPlan,
        ttl_seconds: float | None = None,
    ) -> Preview:
        """``docker run -d`` one hardened preview container; returns the Preview.

        Only ``plan.type == "static"`` is implemented: ``python -m http.server``
        over the read-only bind-mounted workspace. The container is hardened
        for candidate-authored content: nobody user, read-only rootfs, all
        capabilities dropped, no-new-privileges, dedicated bridge network with
        the port bound to ``self.bind_host`` (localhost).
        """
        if plan.type != "static":
            raise ValueError(
                f"unsupported preview type {plan.type!r} (only 'static' is implemented)"
            )
        self.ensure_network()

        preview_id = str(uuid.uuid4())
        name = f"{CONTAINER_PREFIX}{uuid.UUID(preview_id).hex[:12]}"
        exists = self._docker(
            ["ps", "-aq", "--filter", f"name=^/{name}$"], check=False
        ).stdout.split()
        if exists:
            raise RuntimeError(f"preview container name {name!r} already exists")

        ttl = ttl_seconds if ttl_seconds is not None else self.ttl_seconds
        expires_epoch = time.time() + ttl
        args = [
            "run",
            "-d",
            "--name",
            name,
            "--network",
            self.network,
            "--restart",
            "no",
            # Hardening: the served content is candidate-authored.
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--user",
            "65534:65534",  # nobody — needs no writes (http.server is read-only)
            "-p",
            f"{self.bind_host}::{CONTAINER_PORT}",  # ephemeral localhost port
            "-v",
            f"{self._rt.to_host_path(serve_dir)}:{SERVE_ROOT}:ro",
            "--label",
            f"{LABEL_MANAGED}=1",
            "--label",
            f"{LABEL_PREVIEW_ID}={preview_id}",
            "--label",
            f"{LABEL_RUN_ID}={run_id}",
            "--label",
            f"{LABEL_SANDBOX_ID}={sandbox_id}",
            "--label",
            f"{LABEL_TYPE}={plan.type}",
            "--label",
            f"{LABEL_ENTRY}={plan.entry}",
            "--label",
            f"{LABEL_EXPIRES_AT}={expires_epoch:.0f}",
            self.image,
            "python",
            "-m",
            "http.server",
            str(CONTAINER_PORT),
            "--directory",
            SERVE_ROOT,
            "--bind",
            "0.0.0.0",
        ]
        proc = self._docker(args, timeout=180)
        container_id = proc.stdout.strip()
        preview = self._inspect_ids([container_id])
        if preview:
            return preview[0]
        # Inspect raced the daemon — return what we know; GET re-inspects.
        return Preview(
            preview_id=preview_id,
            run_id=run_id,
            sandbox_id=sandbox_id,
            type=plan.type,
            container=name,
            status="created",
            port=None,
            entry=plan.entry,
            url=None,
            created_at=datetime.now(UTC),
            expires_at=datetime.fromtimestamp(expires_epoch, UTC),
        )

    # -- inspect / registry --------------------------------------------------

    def _row_to_preview(self, row: dict[str, Any]) -> Preview | None:
        labels = (row.get("Config") or {}).get("Labels") or {}
        preview_id = labels.get(LABEL_PREVIEW_ID)
        if not preview_id:
            return None  # not one of ours (missing label) — skip
        try:
            uuid.UUID(preview_id)  # API responses parse it — reject label forgeries
        except ValueError:
            return None
        state = row.get("State") or {}
        status = str(state.get("Status") or "unknown")

        port: int | None = None
        bindings = (
            (row.get("NetworkSettings") or {}).get("Ports") or {}
        ).get(f"{CONTAINER_PORT}/tcp") or []
        if bindings and bindings[0].get("HostPort"):
            try:
                port = int(bindings[0]["HostPort"])
            except (TypeError, ValueError):
                port = None

        created = _parse_ts(row.get("Created"))
        expires: datetime | None = None
        raw_exp = labels.get(LABEL_EXPIRES_AT)
        if raw_exp:
            try:
                expires = datetime.fromtimestamp(float(raw_exp), UTC)
            except (TypeError, ValueError, OSError):
                expires = None
        if expires is None:
            # Missing/corrupt expiry label — fall back to created+TTL so the
            # reaper still bounds orphans rather than leaking them forever.
            base = created or datetime.now(UTC)
            expires = datetime.fromtimestamp(base.timestamp() + self.ttl_seconds, UTC)

        entry = labels.get(LABEL_ENTRY) or None
        return Preview(
            preview_id=preview_id,
            run_id=labels.get(LABEL_RUN_ID) or "",
            sandbox_id=labels.get(LABEL_SANDBOX_ID) or "",
            type=labels.get(LABEL_TYPE) or "static",
            container=str(row.get("Name") or "").lstrip("/"),
            status=status,
            port=port,
            entry=entry,
            url=_preview_url(port, entry),
            created_at=created,
            expires_at=expires,
        )

    def _inspect_ids(self, container_ids: list[str]) -> list[Preview]:
        if not container_ids:
            return []
        proc = self._docker(["inspect", *container_ids], check=False)
        if proc.returncode != 0:
            return []
        try:
            rows = json.loads(proc.stdout)
        except json.JSONDecodeError:
            return []
        previews = [p for p in (self._row_to_preview(r) for r in rows) if p is not None]
        previews.sort(
            key=lambda p: p.created_at or datetime.min.replace(tzinfo=UTC),
            reverse=True,
        )
        return previews

    def list(self, *, sandbox_id: str | None = None) -> list[Preview]:
        """All preview containers (any status), newest first.

        Label-scoped — rebuilds the registry from the daemon each call, so
        previews spawned by a previous api life remain discoverable/stoppable.
        """
        args = ["ps", "-aq", "--filter", f"label={LABEL_MANAGED}=1"]
        if sandbox_id:
            args += ["--filter", f"label={LABEL_SANDBOX_ID}={sandbox_id}"]
        proc = self._docker(args, check=False)
        ids = proc.stdout.split()
        return self._inspect_ids(ids)

    def get(self, preview_id: str) -> Preview | None:
        proc = self._docker(
            ["ps", "-aq", "--filter", f"label={LABEL_PREVIEW_ID}={preview_id}"],
            check=False,
        )
        ids = proc.stdout.split()
        previews = self._inspect_ids(ids[:1])
        return previews[0] if previews else None

    def get_for_sandbox(self, sandbox_id: str) -> Preview | None:
        """Newest preview for a sandbox (any status), or None."""
        previews = self.list(sandbox_id=sandbox_id)
        return previews[0] if previews else None

    # -- stop / reap ---------------------------------------------------------

    def stop(self, preview_id: str) -> bool:
        """``docker rm -f`` the preview container; False when already gone."""
        proc = self._docker(
            ["ps", "-aq", "--filter", f"label={LABEL_PREVIEW_ID}={preview_id}"],
            check=False,
        )
        ids = proc.stdout.split()
        if not ids:
            return False
        self._docker(["rm", "-f", ids[0]], check=False)
        logger.info("stopped preview %s (container %.12s)", preview_id, ids[0])
        return True

    def reap_expired(self, now: float | None = None) -> int:
        """Force-remove every managed container past its expiry label."""
        cutoff = time.time() if now is None else now
        removed = 0
        for preview in self.list():
            if (
                preview.expires_at is not None
                and preview.expires_at.timestamp() <= cutoff
                and self.stop(preview.preview_id)
            ):
                removed += 1
        if removed:
            logger.info("reaped %d expired preview container(s)", removed)
        return removed
