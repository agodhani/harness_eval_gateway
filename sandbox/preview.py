"""Artifact preview — serve a snapshot workspace from a locked-down container (F10).

Product flow: results page "Preview" button → ``POST .../preview`` → this
module spawns a tiny static-file container bound to an ephemeral localhost
port → UI opens the URL in a new tab. ``DELETE`` stops it; a TTL reaper
auto-stops containers after ``PREVIEW_TTL_SECONDS`` (default 15 min).

Design notes:

- **Preview types.** ``detect_preview`` returns a :class:`PreviewPlan` whose
  ``type`` selects the serving strategy. ``"static"`` serves
  ``python -m http.server``; ``"desktop"`` executes the artifact (pygame —
  see ``_detect_desktop`` for the conservative heuristic) on a virtual
  display streamed via noVNC. The plan/label/API shape leaves room for
  ``python-server`` etc.
- **Static network posture.** Static preview containers join a dedicated
  bridge network ``heg-preview`` — *not* ``heg-sandbox`` (which has the
  gateway attached). Docker cannot publish ports on ``--internal`` networks
  (the binding is silently dropped — verified: ``docker inspect`` shows
  ``{"8000/tcp":[]}``), so "internal net + reachable port" is impossible;
  the chosen posture is a dedicated bridge with the port bound to
  ``127.0.0.1`` (host-local only) and ``enable_ip_masquerade=false`` (blocks
  outbound NAT on Linux; inert on Docker Desktop). The hardening flags carry
  the weight: nobody user, read-only rootfs, cap-drop ALL, no-new-privileges,
  and a pinned image running ``http.server`` — no candidate code executes.
- **Desktop isolation.** Desktop previews *execute* candidate code, so the
  posture is stricter: the runner sits alone on a dedicated ``--internal``
  network ``heg-desktop`` (zero egress, zero published ports, only peer =
  its own proxy) with ``--cpus``/``--memory``/``--pids-limit`` bounds. A
  pinned ``alpine/socat`` sidecar — a dumb TCP pipe, nothing for the artifact
  to exploit — straddles ``heg-desktop`` + ``heg-preview`` and publishes the
  noVNC port on localhost. The api-side alternative was rejected: it would
  require the api to join the runner's network (giving candidate code a path
  to the control plane) and hand-roll websocket proxying.
- **Jail.** ``resolve_workspace_dir`` reuses the download-jail pattern from
  ``api/routers/results.py``: the served directory must resolve inside
  ``ARTIFACT_ROOT`` — ``..`` escapes and symlinked ``workspace`` dirs that
  resolve outside are rejected. The artifact dir is bind-mounted read-only.
- **Registry = docker labels.** Every preview container carries
  ``heg.preview=1`` + id/run/sandbox/type/entry/expiry/role labels, so a
  restart of the api loses nothing: ``list()`` rebuilds state from
  ``docker inspect`` and the reaper still enforces expiry written on the
  container itself. Runner and proxy share ``heg.preview_id``; the
  ``heg.preview_role`` label distinguishes them.
- Sync like :class:`sandbox.runtime.SandboxRuntime` (docker CLI is sync);
  the api wraps calls in ``asyncio.to_thread``.
"""

from __future__ import annotations

import json
import logging
import os
import re
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

# --- desktop previews (pygame etc. — executes candidate code) ---------------
# The RUNNER container goes on a dedicated --internal network: zero egress,
# zero reachable peers except its own proxy sidecar (the only way in). The
# proxy straddles heg-desktop + heg-preview (the bridge that can publish a
# localhost port) and is a dumb TCP pipe — alpine/socat, no HTTP parsing.
DESKTOP_NETWORK = "heg-desktop"
DEFAULT_DESKTOP_IMAGE = "heg-preview-desktop:latest"
DEFAULT_PROXY_IMAGE = "alpine/socat:1.8.1.3"
DESKTOP_CONTAINER_PORT = 6080  # websockify (noVNC web + ws) inside the runner
DESKTOP_ARTIFACT_ROOT = "/srv/artifact-ro"  # :ro mount; entrypoint copies to /work
DEFAULT_DESKTOP_CPUS = "1"
DEFAULT_DESKTOP_MEMORY = "512m"
DEFAULT_DESKTOP_PIDS = 256

# Labels — ``heg.preview`` marks manager ownership; the rest rebuild Preview
# state after an api restart (and let the reaper enforce TTL out-of-process).
LABEL_MANAGED = "heg.preview"
LABEL_PREVIEW_ID = "heg.preview_id"
LABEL_RUN_ID = "heg.run_id"  # shared with sandboxes -> run cleanup sweeps these too
LABEL_SANDBOX_ID = "heg.sandbox_id"
LABEL_TYPE = "heg.preview_type"
LABEL_ENTRY = "heg.preview_entry"
LABEL_EXPIRES_AT = "heg.preview_expires_at"  # epoch seconds
# runner = the container doing the work (http.server / the app); proxy = the
# desktop sidecar that publishes the port. Containers without the label
# (pre-role previews) count as runners.
LABEL_ROLE = "heg.preview_role"
ROLE_RUNNER = "runner"
ROLE_PROXY = "proxy"

#: Container statuses where the preview is (or will shortly be) serving.
LIVE_STATUSES = frozenset({"created", "running", "restarting", "paused"})

_HTML_SUFFIXES = frozenset({".html", ".htm"})
_INDEX_NAMES = frozenset({"index.html", "index.htm"})

# Desktop detection: an unambiguous .py entry (root or nested ≤3 levels) AND
# explicit pygame evidence — a non-comment requirements.txt line mentioning
# pygame/pygame-ce, or `import pygame`/`from pygame` in the candidate file.
_DESKTOP_ENTRY_NAMES = ("main.py", "app.py", "game.py", "run.py")
_DESKTOP_MAX_DEPTH = 3
# Dirs never worth scanning for an artifact entry point.
_DESKTOP_SKIP_DIRS = frozenset(
    {
        "node_modules",
        "__pycache__",
        "tests",
        "test",
        "venv",
        "env",
        "dist",
        "build",
        "docs",
        "doc",
        "site-packages",
        "migrations",
        "fixtures",
    }
)
_PYGAME_REQ_RE = re.compile(r"pygame(?:-ce)?", re.IGNORECASE)
_PYGAME_IMPORT_RE = re.compile(r"^\s*(?:import|from)\s+pygame\b", re.MULTILINE)
_READ_CAP = 512 * 1024  # only the head of a file is scanned for evidence


@dataclass(frozen=True)
class PreviewPlan:
    """How one snapshot workspace can be previewed.

    ``type == "static"`` → serve the whole workspace dir over HTTP; ``entry``
    is the docroot-relative entry point (``"index.html"``, ``"game.html"``,
    ``"dist/index.html"``). ``type == "desktop"`` → execute ``entry`` (a
    root-level ``.py``) on a virtual display streamed via noVNC. Future
    types (``python-server``) extend this rather than the API shape —
    ``PreviewOut`` already carries ``type``/``url``/``entry``.
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


def _nested_indexes(workspace: Path) -> list[Path]:
    """``*/index.html`` files one level down (hidden/symlinked dirs skipped)."""
    nested: list[Path] = []
    try:
        entries = sorted(workspace.iterdir(), key=lambda p: p.name)
    except OSError:
        return []
    for d in entries:
        if d.name.startswith(".") or d.is_symlink() or not d.is_dir():
            continue
        for cand in _html_files(d):
            if cand.name.lower() in _INDEX_NAMES:
                nested.append(cand)
                break
    return nested


def _detect_static(workspace: Path) -> PreviewPlan | None:
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
    root_html = _html_files(workspace)
    for p in root_html:
        if p.name.lower() in _INDEX_NAMES:
            return PreviewPlan(type="static", entry=p.name)
    if len(root_html) == 1:
        return PreviewPlan(type="static", entry=root_html[0].name)

    nested = _nested_indexes(workspace)
    if len(nested) == 1:
        return PreviewPlan(type="static", entry=str(nested[0].relative_to(workspace)))
    return None


def _static_candidates(workspace: Path) -> list[str]:
    """Ambiguous static entries — only called when ``_detect_static`` found
    no unique pick: multiple root ``*.html`` (no index) or multiple nested
    ``*/index.html``. Returned as workspace-relative paths for the
    entry-chooser / ``entry=`` override."""
    root_html = _html_files(workspace)
    if len(root_html) > 1:
        return [p.name for p in root_html]
    nested = _nested_indexes(workspace)
    if len(nested) > 1:
        return [str(p.relative_to(workspace)) for p in nested]
    return []


def _read_head(path: Path, cap: int = _READ_CAP) -> str:
    """First ``cap`` bytes of a file, decoded leniently ("" on error)."""
    try:
        with path.open("rb") as fh:
            return fh.read(cap).decode("utf-8", errors="ignore")
    except OSError:
        return ""


def _req_mentions_pygame(path: Path) -> bool:
    """``requirements.txt`` names pygame on a REAL line — comments (first
    non-whitespace char ``#``) and blanks are stripped before matching."""
    lines = [
        line
        for line in _read_head(path).splitlines()
        if not line.lstrip().startswith("#")
    ]
    return _PYGAME_REQ_RE.search("\n".join(lines)) is not None


def _scan_dirs(workspace: Path) -> list[Path]:
    """Workspace root + dirs up to ``_DESKTOP_MAX_DEPTH`` levels down (BFS).

    Hidden dirs, symlinks, and ``_DESKTOP_SKIP_DIRS`` (``.git`` is hidden,
    ``node_modules``/``__pycache__``/``tests``/``venv``/… are named) are
    skipped — they're never the artifact's entry point.
    """
    dirs = [workspace]
    frontier = [workspace]
    for _depth in range(_DESKTOP_MAX_DEPTH):
        nxt: list[Path] = []
        for d in frontier:
            try:
                entries = sorted(d.iterdir(), key=lambda p: p.name)
            except OSError:
                continue
            for e in entries:
                if (
                    e.is_dir()
                    and not e.is_symlink()
                    and not e.name.startswith(".")
                    and e.name not in _DESKTOP_SKIP_DIRS
                ):
                    dirs.append(e)
                    nxt.append(e)
        frontier = nxt
    return dirs


def _py_files(dirpath: Path) -> list[Path]:
    """Regular ``*.py`` files directly inside ``dirpath``, name-sorted."""
    try:
        entries = sorted(dirpath.iterdir(), key=lambda p: p.name)
    except OSError:
        return []
    return [
        p
        for p in entries
        if p.is_file() and not p.is_symlink() and p.suffix == ".py"
    ]


def _detect_desktop(workspace: Path) -> tuple[PreviewPlan | None, list[str]]:
    """Detect runnable GUI artifacts — pygame today (``type="desktop"``).

    Conservative on purpose — detection decides what gets *executed*:

    - **Candidate entries** in each scanned dir (root + ≤3 levels, skipping
      hidden/``_DESKTOP_SKIP_DIRS``/symlinked dirs): named entries
      ``main.py``/``app.py``/``game.py``/``run.py``, else a dir containing
      exactly one ``*.py``.
    - **Evidence per candidate**: the candidate file itself imports pygame
      (line-anchored — comments/docstrings at line start excepted), OR any
      ``requirements.txt`` within the scanned dirs names pygame on a real
      (non-comment) line.
    - **Ambiguity**: exactly one eligible candidate → auto-pick it.
      Multiple → ``(None, candidates)`` — the caller picks via ``entry=``;
      we never silently choose what to execute.
    """
    scan_dirs = _scan_dirs(workspace)
    req_evidence = False
    for d in scan_dirs:
        req = d / "requirements.txt"
        if req.is_file() and not req.is_symlink() and _req_mentions_pygame(req):
            req_evidence = True
            break

    candidates: list[str] = []
    for d in scan_dirs:
        py = _py_files(d)
        entries = [d / name for name in _DESKTOP_ENTRY_NAMES if (d / name) in py]
        if not entries and len(py) == 1:
            entries = py
        for cand in entries:
            if req_evidence or (
                _PYGAME_IMPORT_RE.search(_read_head(cand)) is not None
            ):
                candidates.append(str(cand.relative_to(workspace)))

    candidates = sorted(set(candidates))
    if len(candidates) == 1:
        return PreviewPlan(type="desktop", entry=candidates[0]), []
    return None, candidates


@dataclass(frozen=True)
class PreviewScan:
    """Full scan of a snapshot workspace.

    ``plan`` — the auto-picked preview (``None`` when ambiguous/unsupported).
    ``candidates`` — workspace-relative entry paths the user could pick via
    the ``entry=`` override; populated only when ``plan`` is ``None`` and at
    least two plausible entries exist (``.html`` files for static, evidence-
    backed ``.py`` entries for desktop). Empty candidates + no plan = the
    artifact simply isn't previewable today.
    """

    plan: PreviewPlan | None
    candidates: tuple[str, ...]


def scan_preview(workspace: Path) -> PreviewScan:
    """First match wins: ``static`` → ``desktop`` → ambiguous/none.

    A workspace matching both previews as static — the safe/cheap default.
    """
    if not workspace.is_dir():
        return PreviewScan(None, ())
    plan = _detect_static(workspace)
    if plan is not None:
        return PreviewScan(plan, ())
    plan, desk_candidates = _detect_desktop(workspace)
    if plan is not None:
        return PreviewScan(plan, ())
    return PreviewScan(
        None, tuple(sorted(desk_candidates + _static_candidates(workspace)))
    )


def detect_preview(workspace: Path) -> PreviewPlan | None:
    """Auto-picked plan, or ``None`` (ambiguous or unsupported workspace).

    Prefer :func:`scan_preview` when candidates are wanted for a chooser UI.
    """
    return scan_preview(workspace).plan


def plan_for_entry(workspace: Path, raw_entry: str) -> PreviewPlan:
    """Resolve an explicit ``entry=`` override into a :class:`PreviewPlan`.

    The entry is jailed to ``workspace``: must be a relative path (no ``..``,
    not absolute), resolve inside it, exist as a regular non-symlinked file,
    and have a recognized suffix — ``.html``/``.htm`` → ``static``, ``.py``
    → ``desktop``. Anything else raises ``ValueError`` (router maps to 422).

    Note: an explicit ``.py`` entry bypasses pygame evidence — the user's
    pick IS the disambiguation; the executed artifact runs under the same
    isolation regardless.
    """
    raw = raw_entry.strip()
    rel_path = Path(raw)
    if (
        not raw
        or rel_path.is_absolute()
        or ".." in rel_path.parts
        or any(not p or p == "." for p in rel_path.parts)
    ):
        raise ValueError(f"invalid entry path {raw_entry!r}")
    rel = str(rel_path)
    cand = workspace / rel
    if cand.is_symlink():
        raise ValueError(f"entry {raw_entry!r} is a symlink — not previewable")
    try:
        ws = workspace.resolve()
        resolved = cand.resolve()
    except OSError as exc:
        raise ValueError(f"entry {raw_entry!r} could not be resolved") from exc
    if resolved != ws and ws not in resolved.parents:
        raise ValueError(f"entry {raw_entry!r} escapes the workspace")
    if not cand.is_file():
        raise ValueError(f"entry {raw_entry!r} not found in the workspace")
    suffix = cand.suffix.lower()
    if suffix in _HTML_SUFFIXES:
        return PreviewPlan(type="static", entry=rel)
    if suffix == ".py":
        return PreviewPlan(type="desktop", entry=rel)
    raise ValueError(
        f"unsupported entry type {cand.suffix!r} — want .html/.htm or .py"
    )


def _preview_url(
    port: int | None, entry: str | None, preview_type: str = "static"
) -> str | None:
    """Browser URL for a preview — entry dir/file for static, noVNC for desktop."""
    if port is None:
        return None
    if preview_type == "desktop":
        return f"http://localhost:{port}/vnc.html?autoconnect=true&resize=scale"
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
        desktop_image: str | None = None,
        proxy_image: str | None = None,
        desktop_network: str = DESKTOP_NETWORK,
        desktop_cpus: str = DEFAULT_DESKTOP_CPUS,
        desktop_memory: str = DEFAULT_DESKTOP_MEMORY,
        desktop_pids: int = DEFAULT_DESKTOP_PIDS,
    ) -> None:
        self._rt = runtime or SandboxRuntime()
        self.image = (
            image or os.environ.get("PREVIEW_IMAGE") or DEFAULT_IMAGE
        )
        self.network = network
        self.bind_host = bind_host
        self.ttl_seconds = ttl_seconds
        self.desktop_image = desktop_image or DEFAULT_DESKTOP_IMAGE
        self.proxy_image = proxy_image or DEFAULT_PROXY_IMAGE
        self.desktop_network = desktop_network
        self.desktop_cpus = desktop_cpus
        self.desktop_memory = desktop_memory
        self.desktop_pids = desktop_pids

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

    def _ensure_desktop_network(self) -> None:
        """Create the INTERNAL desktop-runner network if missing (idempotent).

        ``heg-desktop`` is the isolation boundary for executed artifacts:
        ``--internal`` means no outbound route and no published ports — the
        runner can reach only its own socat proxy sidecar. An existing
        network that is NOT internal is a security misconfiguration (the
        runner would have egress) — logged loudly.
        """
        proc = self._docker(
            ["network", "inspect", self.desktop_network], check=False
        )
        if proc.returncode != 0:
            self._docker(
                [
                    "network",
                    "create",
                    "--internal",
                    "--driver",
                    "bridge",
                    self.desktop_network,
                ]
            )
            logger.info("created internal docker network %r", self.desktop_network)
            return
        try:
            info = json.loads(proc.stdout)[0]
        except (json.JSONDecodeError, IndexError):
            info = {}
        if not info.get("Internal", False):
            logger.warning(
                "docker network %r exists but is NOT internal — desktop "
                "preview runners will have external egress!",
                self.desktop_network,
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
        """Spawn the container(s) for ``plan``; returns the live Preview.

        - ``static``: one hardened ``python -m http.server`` container.
        - ``desktop``: a runner on the internal ``heg-desktop`` net executing
          the artifact under Xvnc/noVNC, plus a socat proxy sidecar straddling
          ``heg-desktop`` + ``heg-preview`` that publishes a localhost port.

        Unknown types raise ``ValueError`` — the router maps it to 422.
        """
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
        labels = self._labels(
            preview_id=preview_id,
            run_id=run_id,
            sandbox_id=sandbox_id,
            plan=plan,
            expires_epoch=expires_epoch,
        )

        if plan.type == "static":
            self._spawn_static(
                name=name, serve_dir=serve_dir, labels=labels
            )
        elif plan.type == "desktop":
            self._spawn_desktop(
                name=name,
                serve_dir=serve_dir,
                plan=plan,
                labels=labels,
            )
        else:
            raise ValueError(f"unsupported preview type {plan.type!r}")

        preview = self.get(preview_id)
        if preview is not None:
            return preview
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

    def _labels(
        self,
        *,
        preview_id: str,
        run_id: str,
        sandbox_id: str,
        plan: PreviewPlan,
        expires_epoch: float,
        role: str = ROLE_RUNNER,
    ) -> list[str]:
        """Flat ``[--label, k=v, ...]`` args — the restart-safe registry."""
        pairs = [
            (LABEL_MANAGED, "1"),
            (LABEL_PREVIEW_ID, preview_id),
            (LABEL_RUN_ID, run_id),
            (LABEL_SANDBOX_ID, sandbox_id),
            (LABEL_TYPE, plan.type),
            (LABEL_ENTRY, plan.entry or ""),
            (LABEL_EXPIRES_AT, f"{expires_epoch:.0f}"),
            (LABEL_ROLE, role),
        ]
        args: list[str] = []
        for k, v in pairs:
            args += ["--label", f"{k}={v}"]
        return args

    def _spawn_static(
        self, *, name: str, serve_dir: Path, labels: list[str]
    ) -> None:
        """``python -m http.server`` over the :ro bind-mounted workspace.

        Hardened for candidate-authored content: nobody user, read-only
        rootfs, all capabilities dropped, no-new-privileges, dedicated bridge
        network with the port bound to ``self.bind_host`` (localhost).
        """
        args = [
            "run",
            "-d",
            "--name",
            name,
            "--network",
            self.network,
            "--restart",
            "no",
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
            *labels,
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
        self._docker(args, timeout=180)

    def _spawn_desktop(
        self,
        *,
        name: str,
        serve_dir: Path,
        plan: PreviewPlan,
        labels: list[str],
    ) -> None:
        """Runner on the INTERNAL net + socat proxy sidecar publishing the port.

        The runner *executes* candidate code, so unlike a static preview it
        gets zero egress: ``--internal`` ``heg-desktop`` with its only peer
        being its own proxy. ``socat`` is a dumb TCP pipe — no HTTP parsing,
        nothing for the artifact to exploit — and is the only container with
        a published port (bound to localhost on ``heg-preview``). CPU/memory/
        pids limits bound what the candidate code can burn.
        """
        self._ensure_desktop_network()
        if (
            self._docker(
                ["image", "inspect", self.desktop_image], check=False
            ).returncode
            != 0
        ):
            raise RuntimeError(
                f"desktop preview image {self.desktop_image!r} not found — "
                f"build it: docker build -f "
                f"sandbox/images/desktop-preview.Dockerfile "
                f"-t {self.desktop_image} ."
            )

        runner_args = [
            "run",
            "-d",
            "--name",
            name,
            "--network",
            self.desktop_network,  # internal: no egress, no published ports
            "--restart",
            "no",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--user",
            "65534:65534",
            "--cpus",
            self.desktop_cpus,
            "--memory",
            self.desktop_memory,
            "--pids-limit",
            str(self.desktop_pids),
            "-e",
            f"APP_ENTRY={plan.entry}",
            "-e",
            "HOME=/tmp",
            "-v",
            f"{self._rt.to_host_path(serve_dir)}:{DESKTOP_ARTIFACT_ROOT}:ro",
            *labels,
            self.desktop_image,
        ]
        proxy_name = f"{name}-proxy"
        # The proxy's FIRST network must be the publishable bridge — docker
        # only honors -p on the gateway network the container starts on.
        proxy_args = [
            "run",
            "-d",
            "--name",
            proxy_name,
            "--network",
            self.network,
            "--restart",
            "no",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--user",
            "65534:65534",
            "-p",
            f"{self.bind_host}::{DESKTOP_CONTAINER_PORT}",
            *self._labels_as_args(labels, role=ROLE_PROXY),
            self.proxy_image,
            "TCP-LISTEN:" + str(DESKTOP_CONTAINER_PORT) + ",fork,reuseaddr",
            f"TCP:{name}:{DESKTOP_CONTAINER_PORT}",
        ]
        try:
            self._docker(runner_args, timeout=180)
            self._docker(proxy_args, timeout=180)
            self._docker(
                ["network", "connect", self.desktop_network, proxy_name],
                timeout=30,
            )
        except Exception:
            # Don't leak a half-spawned pair.
            self._docker(["rm", "-f", name, proxy_name], check=False)
            raise

    @staticmethod
    def _labels_as_args(labels: list[str], *, role: str) -> list[str]:
        """Clone label args swapping in a different ``heg.preview_role``."""
        out: list[str] = []
        for i, arg in enumerate(labels):
            if (
                arg == "--label"
                and i + 1 < len(labels)
                and labels[i + 1].startswith(f"{LABEL_ROLE}=")
            ):
                out += [arg, f"{LABEL_ROLE}={role}"]
                continue
            if arg.startswith(f"{LABEL_ROLE}="):
                continue
            out.append(arg)
        return out

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
        if labels.get(LABEL_ROLE) == ROLE_PROXY:
            return None  # proxy sidecars aren't previews — the runner is
        state = row.get("State") or {}
        status = str(state.get("Status") or "unknown")

        preview_type = labels.get(LABEL_TYPE) or "static"
        port: int | None = None
        if preview_type == "desktop":
            # The published port lives on the proxy sidecar, not the runner.
            port = self._proxy_port(preview_id)
        else:
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
            type=preview_type,
            container=str(row.get("Name") or "").lstrip("/"),
            status=status,
            port=port,
            entry=entry,
            url=_preview_url(port, entry, preview_type),
            created_at=created,
            expires_at=expires,
        )

    def _proxy_port(self, preview_id: str) -> int | None:
        """Published host port of a desktop preview's proxy sidecar."""
        proc = self._docker(
            [
                "ps",
                "-aq",
                "--filter",
                f"label={LABEL_PREVIEW_ID}={preview_id}",
                "--filter",
                f"label={LABEL_ROLE}={ROLE_PROXY}",
            ],
            check=False,
        )
        ids = proc.stdout.split()
        if not ids:
            return None
        info = self._docker(["inspect", ids[0]], check=False)
        if info.returncode != 0:
            return None
        try:
            row = json.loads(info.stdout)[0]
            bindings = (
                (row.get("NetworkSettings") or {}).get("Ports") or {}
            ).get(f"{DESKTOP_CONTAINER_PORT}/tcp") or []
            if bindings and bindings[0].get("HostPort"):
                return int(bindings[0]["HostPort"])
        except (json.JSONDecodeError, IndexError, TypeError, ValueError):
            return None
        return None

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
        # ids may include proxy sidecars (same preview_id label) —
        # _row_to_preview skips them, so inspect the whole set.
        previews = self._inspect_ids(ids)
        return previews[0] if previews else None

    def get_for_sandbox(self, sandbox_id: str) -> Preview | None:
        """Newest preview for a sandbox (any status), or None."""
        previews = self.list(sandbox_id=sandbox_id)
        return previews[0] if previews else None

    # -- stop / reap ---------------------------------------------------------

    def stop(self, preview_id: str) -> bool:
        """``docker rm -f`` ALL containers of a preview (runner + sidecars).

        A desktop preview is two containers sharing ``heg.preview_id``; the
        label filter finds both, so orphaned proxies can't outlive runners.
        """
        proc = self._docker(
            ["ps", "-aq", "--filter", f"label={LABEL_PREVIEW_ID}={preview_id}"],
            check=False,
        )
        ids = proc.stdout.split()
        if not ids:
            return False
        self._docker(["rm", "-f", *ids], check=False)
        logger.info("stopped preview %s (%d container(s))", preview_id, len(ids))
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
