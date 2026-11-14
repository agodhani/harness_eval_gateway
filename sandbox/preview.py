"""Artifact preview — serve a snapshot workspace from a locked-down container (F12).

Product flow: results page "Preview" button → ``POST .../preview`` → this
module spawns a tiny static-file container bound to an ephemeral localhost
port → UI opens the URL in a new tab. ``DELETE`` stops it; a TTL reaper
auto-stops containers after ``PREVIEW_TTL_SECONDS`` (default 15 min).

Design notes:

- **Preview types.** ``scan_preview`` returns every runnable entry as
  typed :class:`PreviewPlan` candidates (default-first; ``detect_preview``
  returns just the default). ``"static"`` serves ``python -m http.server``;
  ``"desktop"`` executes the artifact (GUI — pygame/pyglet/arcade/tkinter —
  see ``_desktop_candidates``) on a virtual display streamed via noVNC;
  ``"python-server"`` executes a web app (flask/fastapi/django/… — see
  ``_server_candidates``) whose listener the runner bridges onto :8000.
  The POST body's ``type=`` overrides the pick:
  ``plan_for_type`` yields the first candidate of that type (a
  both-matching workspace defaults to static), and
  ``plan_for_entry(..., preview_type=)`` forces the plan type for an
  explicit entry — ``"static"`` serves any jailed file (the entry is only
  the URL opened — view a ``.py``'s source instead of executing it) while
  ``"desktop"``/``"python-server"`` still require a ``.py`` entry.
- **Probe caching.** The results GET runs ``resolve_workspace_dir`` +
  ``scan_preview`` per sandbox per request; :class:`ScanCache` memoizes
  that probe for ``PREVIEW_SCAN_TTL_SECONDS`` — workspaces only change on
  (re-)snapshot, so a short TTL bounds staleness while UI polling stays
  cheap.
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
- **Executed-code isolation.** Desktop AND python-server previews
  *execute* candidate code, so the posture is stricter than static: the
  runner sits on a dedicated ``--internal`` network ``heg-desktop`` (zero
  egress, zero published ports; peers = its own proxy plus other
  ephemeral preview runners — none can egress) with
  ``--cpus``/``--memory``/``--pids-limit`` bounds. A
  pinned ``alpine/socat`` sidecar — a dumb TCP pipe, nothing for the artifact
  to exploit — straddles ``heg-desktop`` + ``heg-preview`` and publishes
  the runner's port on localhost (:6080 noVNC for desktop, :8000 for
  python-server). The api-side alternative was rejected: it would
  require the api to join the runner's network (giving candidate code a
  path to the control plane) and hand-roll websocket proxying.
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
from collections import OrderedDict
from collections.abc import Callable
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
DESKTOP_NETWORK = "heg-desktop"  # shared internal net for ALL executed-code previews
DEFAULT_DESKTOP_IMAGE = "heg-preview-desktop:latest"
DEFAULT_PROXY_IMAGE = "alpine/socat:1.8.1.3"
DESKTOP_CONTAINER_PORT = 6080  # websockify (noVNC web + ws) inside the runner
DESKTOP_ARTIFACT_ROOT = "/srv/artifact-ro"  # :ro mount; entrypoint copies to /work
DEFAULT_DESKTOP_CPUS = "1"
DEFAULT_DESKTOP_MEMORY = "512m"
DEFAULT_DESKTOP_PIDS = 256

# --- python-server previews (PR3) — executed web apps, same topology -------
# Same runner+sidecar topology as desktop previews (the app *executes*), but
# the app serves plain HTTP — the runner's relay script makes it reachable
# on :8000 whatever port the artifact actually binds (apps hardcode :5000,
# :3000, ... — see server-preview-runner.py).
DEFAULT_SERVER_IMAGE = "heg-preview-server:latest"
SERVER_CONTAINER_PORT = 8000  # canonical app port inside the server runner

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

#: Types with an implemented spawner — ``PreviewManager.spawn`` dispatches on
#: these. ``static`` serves files; ``desktop`` (GUI apps) and
#: ``python-server`` (web apps) execute the artifact on the internal net.
PREVIEW_TYPES = frozenset({"static", "desktop", "python-server"})

#: Executed-code types whose published port lives on the proxy sidecar —
#: ``_row_to_preview`` resolves these via ``_proxy_port`` instead of the
#: runner's own bindings.
_PROXIED_PORTS = {
    "desktop": DESKTOP_CONTAINER_PORT,
    "python-server": SERVER_CONTAINER_PORT,
}

# Desktop detection: every unambiguous .py entry (root or nested ≤3 levels)
# with explicit GUI evidence — a line-anchored pygame/pyglet/arcade/tkinter
# import in the file itself, or entry-shaped + a non-comment requirements.txt
# line naming a GUI stack (see _py_candidates).
_DESKTOP_ENTRY_NAMES = ("main.py", "app.py", "game.py", "run.py")
_DESKTOP_MAX_DEPTH = 3
# Dirs never worth scanning for an artifact entry point. ``templates`` is
# flask/django's render dir — a ``templates/index.html`` is the server
# app's fragment, not a static site entry; skipping it lets the workspace
# default to the python-server candidate.
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
        "templates",
    }
)
# pyglet/arcade install under their own names; tkinter is stdlib — a
# requirements file can't name it (it's not pip-installed), so only import
# evidence detects tk apps. `pip install tk` is a different (useless)
# package — don't count bare `tk` requirements lines as evidence.
_GUI_REQ_RE = re.compile(r"pygame(?:-ce)?|pyglet|arcade", re.IGNORECASE)
_GUI_IMPORT_RE = re.compile(
    r"^\s*(?:import|from)\s+(?:pygame|pyglet|arcade|tkinter)\b", re.MULTILINE
)
_READ_CAP = 512 * 1024  # only the head of a file is scanned for evidence

# python-server detection: same shape as desktop (see _py_candidates) — a
# line-anchored web-framework import in the file, or entry-shaped AND a
# scanned requirements.txt names a server framework on a non-comment line.
# `manage.py` (django) qualifies via its django import.
# Kept in sync with the baked deps in server-preview.Dockerfile (plus
# http.server — stdlib, runs in any python image; `from http.server` is the
# import form, matched by the dotted pattern below).
_SERVER_ENTRY_NAMES = (
    "main.py",
    "app.py",
    "server.py",
    "manage.py",
    "wsgi.py",
    "asgi.py",
    "run.py",
    "api.py",
)
_SERVER_REQ_RE = re.compile(
    r"flask|fastapi|uvicorn|starlette|aiohttp|bottle|django|gunicorn|tornado|waitress|hypercorn",
    re.IGNORECASE,
)
_SERVER_IMPORT_RE = re.compile(
    r"^\s*(?:import|from)\s+"
    r"(?:flask|fastapi|uvicorn|starlette|aiohttp|bottle|django|gunicorn|"
    r"tornado|waitress|hypercorn|http\.server)\b",
    re.MULTILINE,
)


@dataclass(frozen=True)
class PreviewPlan:
    """How one snapshot workspace can be previewed.

    ``type == "static"`` → serve the whole workspace dir over HTTP; ``entry``
    is the docroot-relative entry point (``"index.html"``, ``"game.html"``,
    ``"dist/index.html"``). ``type == "desktop"`` → execute ``entry`` (a
    ``.py``) on a virtual display streamed via noVNC. ``type ==
    "python-server"`` → execute ``entry`` (a ``.py`` web app) and publish
    whatever port it listens on. ``PreviewOut`` carries ``type``/``url``/
    ``entry`` — new types extend this rather than the API shape.
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
    """``*/index.html`` files one level down (hidden/symlinked dirs skipped).

    ``templates/`` is skipped too — that's the flask/django render dir, so
    a ``templates/index.html`` is a server app's fragment, not a static
    site entry (skipping it lets such workspaces default to the
    python-server candidate). Note ``dist/`` IS allowed — a built static
    site's entry point lives there; it's only skipped for ``*.py``.
    """
    nested: list[Path] = []
    try:
        entries = sorted(workspace.iterdir(), key=lambda p: p.name)
    except OSError:
        return []
    for d in entries:
        if (
            d.name.startswith(".")
            or d.name == "templates"
            or d.is_symlink()
            or not d.is_dir()
        ):
            continue
        for cand in _html_files(d):
            if cand.name.lower() in _INDEX_NAMES:
                nested.append(cand)
                break
    return nested


def _static_candidates(workspace: Path) -> list[PreviewPlan]:
    """All static entries, ordered for default-first presentation.

    Order: root ``index.html``/``.htm`` first (case-insensitive), then other
    root ``*.html`` files sorted, then ``*/index.html`` one level down
    sorted — hidden dirs (``.git``) skipped, symlinks never followed.
    """
    root_html = _html_files(workspace)
    plans: list[PreviewPlan] = [
        PreviewPlan(type="static", entry=p.name)
        for p in root_html
        if p.name.lower() in _INDEX_NAMES
    ]
    plans += [
        PreviewPlan(type="static", entry=p.name)
        for p in root_html
        if p.name.lower() not in _INDEX_NAMES
    ]
    plans += [
        PreviewPlan(type="static", entry=str(p.relative_to(workspace)))
        for p in _nested_indexes(workspace)
    ]
    return plans


def _read_head(path: Path, cap: int = _READ_CAP) -> str:
    """First ``cap`` bytes of a file, decoded leniently ("" on error)."""
    try:
        with path.open("rb") as fh:
            return fh.read(cap).decode("utf-8", errors="ignore")
    except OSError:
        return ""


def _req_matches(path: Path, pattern: re.Pattern[str]) -> bool:
    """``requirements.txt`` matches ``pattern`` on a REAL line — comments
    (first non-whitespace char ``#``) and blanks are stripped first."""
    lines = [
        line
        for line in _read_head(path).splitlines()
        if not line.lstrip().startswith("#")
    ]
    return pattern.search("\n".join(lines)) is not None


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


def _py_candidates(
    workspace: Path,
    *,
    import_re: re.Pattern[str],
    req_re: re.Pattern[str],
    entry_names: tuple[str, ...],
    ptype: str,
) -> list[PreviewPlan]:
    """Shared executed-code ``*.py`` candidate walk for desktop/server.

    Every ``*.py`` with evidence is a candidate — no named-entry shape
    required (the dropdown is the disambiguation mechanism):

    - the file **itself imports** a matching package (``import_re`` is
      line-anchored — comments can't match), OR
    - it is **entry-shaped** (named in ``entry_names``, or a dir containing
      exactly one ``*.py``) AND any ``requirements.txt`` within the scanned
      dirs matches ``req_re`` on a real non-comment line.

    Scanned dirs = root + ≤3 levels (``_DESKTOP_SKIP_DIRS``/hidden/symlinked
    dirs skipped). Ordered for default-first presentation: shallower first,
    named entries (in ``entry_names`` order) before others, then path — so
    ``main.py`` beats ``lib/helpers.py``.
    """
    scan_dirs = _scan_dirs(workspace)
    req_evidence = False
    for d in scan_dirs:
        req = d / "requirements.txt"
        if req.is_file() and not req.is_symlink() and _req_matches(req, req_re):
            req_evidence = True
            break

    candidates: list[tuple[int, int, str]] = []  # (depth, named_rank, rel)
    for d in scan_dirs:
        py = _py_files(d)
        entry_shaped = {d / name for name in entry_names if (d / name) in py}
        if len(py) == 1:
            entry_shaped.add(py[0])
        for cand in py:
            rel = str(cand.relative_to(workspace))
            own_evidence = import_re.search(_read_head(cand)) is not None
            if not (own_evidence or (req_evidence and cand in entry_shaped)):
                continue
            try:
                rank = entry_names.index(cand.name)
            except ValueError:
                rank = len(entry_names)
            candidates.append((len(Path(rel).parts) - 1, rank, rel))

    candidates.sort()
    return [PreviewPlan(type=ptype, entry=rel) for _, _, rel in candidates]


def _desktop_candidates(workspace: Path) -> list[PreviewPlan]:
    """Runnable GUI entries — the dropdown's ``desktop`` options.

    Evidence: the file imports pygame/pyglet/arcade/tkinter, OR it's
    entry-shaped (``main.py``/``app.py``/``game.py``/``run.py``, or a dir's
    only ``*.py``) AND a scanned ``requirements.txt`` names
    pygame(-ce)/pyglet/arcade (tkinter is stdlib — no pip evidence exists;
    bare ``tk`` in requirements is the unrelated Tcl-binding package, not
    evidence). All stacks are baked into ``heg-preview-desktop``.
    """
    return _py_candidates(
        workspace,
        import_re=_GUI_IMPORT_RE,
        req_re=_GUI_REQ_RE,
        entry_names=_DESKTOP_ENTRY_NAMES,
        ptype="desktop",
    )


def _server_candidates(workspace: Path) -> list[PreviewPlan]:
    """Runnable web-app entries — the dropdown's ``python-server`` options.

    Evidence: the file imports a web framework (flask/fastapi/uvicorn/
    django/aiohttp/bottle/tornado/…, or stdlib ``http.server``), OR it's
    entry-shaped (``app.py``/``main.py``/``server.py``/``manage.py``/…, or a
    dir's only ``*.py``) AND a scanned ``requirements.txt`` names a server
    framework. ``manage.py`` qualifies via its ``from django…`` import and
    gets the runserver contract inside the runner.
    """
    return _py_candidates(
        workspace,
        import_re=_SERVER_IMPORT_RE,
        req_re=_SERVER_REQ_RE,
        entry_names=_SERVER_ENTRY_NAMES,
        ptype="python-server",
    )


@dataclass(frozen=True)
class PreviewScan:
    """Full scan of a snapshot workspace.

    ``plan`` — the default pick (``candidates[0]``, ``None`` when nothing is
    previewable). ``candidates`` — every detected runnable entry as typed
    :class:`PreviewPlan` records, ordered default-first: static entries
    (root index → root html → nested index), then desktop ``.py`` entries,
    then python-server ``.py`` entries (each group: shallower → named →
    path). The UI renders this as the dropdown's
    options; the user picks, never types paths.
    """

    plan: PreviewPlan | None
    candidates: tuple[PreviewPlan, ...]


def scan_preview(workspace: Path) -> PreviewScan:
    """Static candidates first, then desktop, then python-server —
    ``candidates[0]`` is the default pick, so a workspace matching both
    defaults to static while the dropdown still offers the executed-code
    entries. A file matching BOTH executed detectors (say, imports flask
    AND pygame) appears once per type — the dropdown disambiguates."""
    if not workspace.is_dir():
        return PreviewScan(None, ())
    candidates = (
        *_static_candidates(workspace),
        *_desktop_candidates(workspace),
        *_server_candidates(workspace),
    )
    return PreviewScan(candidates[0] if candidates else None, candidates)


def detect_preview(workspace: Path) -> PreviewPlan | None:
    """Default pick, or ``None`` (unsupported workspace).

    Prefer :func:`scan_preview` when the full candidate list is wanted —
    this returns just the dropdown's default selection.
    """
    return scan_preview(workspace).plan


def plan_for_entry(
    workspace: Path, raw_entry: str, preview_type: str | None = None
) -> PreviewPlan:
    """Resolve an explicit ``entry=`` override into a :class:`PreviewPlan`.

    The entry is jailed to ``workspace``: must be a relative path (no ``..``,
    not absolute), resolve inside it, exist as a regular non-symlinked file,
    and — without ``preview_type`` — have a recognized suffix:
    ``.html``/``.htm`` → ``static``, ``manage.py`` → ``python-server``
    (unambiguous django — only the server runner publishes the app port),
    other ``.py`` → ``desktop``. Anything else
    raises ``ValueError`` (router maps to 422).

    ``preview_type`` overrides the suffix-derived type: ``"static"`` serves
    ANY jailed file — ``http.server`` already serves the whole workspace, so
    the entry is only the URL the browser opens (view a ``.py``'s source
    instead of executing it); ``"desktop"`` and ``"python-server"`` still
    require a ``.py`` entry (the runner executes it). Unknown types raise
    ``ValueError``.

    Note: an explicit ``.py`` entry bypasses framework evidence — the user's
    pick IS the disambiguation; the executed artifact runs under the same
    isolation regardless. Suffix alone can't tell a GUI app from a web
    server — a bare ``.py`` defaults to ``desktop``; pass
    ``"python-server"`` explicitly to run it as a web app.
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
    if preview_type is not None:
        if preview_type not in PREVIEW_TYPES:
            raise ValueError(
                f"unsupported preview type {preview_type!r} — want one of "
                f"{', '.join(sorted(PREVIEW_TYPES))}"
            )
        if preview_type in ("desktop", "python-server"):
            if suffix != ".py":
                raise ValueError(
                    f"{preview_type} previews execute a .py entry — {rel!r} is "
                    f"{suffix or 'extensionless'}"
                )
            return PreviewPlan(type=preview_type, entry=rel)
        return PreviewPlan(type="static", entry=rel)
    if suffix in _HTML_SUFFIXES:
        return PreviewPlan(type="static", entry=rel)
    if suffix == ".py":
        # manage.py is unambiguously django — a bare pick means "run the
        # project", which only the server runner can reach (its app port is
        # published; the desktop runner's 6080 is noVNC, not the app).
        if cand.name == "manage.py":
            return PreviewPlan(type="python-server", entry=rel)
        return PreviewPlan(type="desktop", entry=rel)
    raise ValueError(
        f"unsupported entry type {cand.suffix!r} — want .html/.htm or .py"
    )


def plan_for_type(workspace: Path, preview_type: str) -> PreviewPlan:
    """First detected candidate of ``preview_type`` — a type-filtered default.

    ``{"type": "desktop"}`` on a workspace matching both detectors picks its
    first desktop candidate even though the scan's default is static.
    Unknown types and types with no detected candidate raise ``ValueError``
    (the detail lists what WAS detected so the 422 is actionable).
    """
    if preview_type not in PREVIEW_TYPES:
        raise ValueError(
            f"unsupported preview type {preview_type!r} — want one of "
            f"{', '.join(sorted(PREVIEW_TYPES))}"
        )
    scan = scan_preview(workspace)
    for cand in scan.candidates:
        if cand.type == preview_type:
            return cand
    detail = f"no {preview_type!r} preview candidate in this workspace"
    detected = ", ".join(f"{c.entry} ({c.type})" for c in scan.candidates)
    if detected:
        detail += f" — detected: {detected}"
    raise ValueError(detail)


class ScanCache:
    """Bounded TTL cache for the results-page preview probe.

    ``resolve_workspace_dir`` + ``scan_preview`` hit the filesystem per
    sandbox per results GET. The workspace only changes on (re-)snapshot,
    so entries are keyed on ``(jail, run_id, sandbox_id, artifact_path)`` —
    an ``artifact_path`` change is a different entry — and expire after
    ``ttl_seconds`` (0 disables caching). ``None`` results (no workspace /
    nothing previewable) are cached too; a missing artifact is the common
    case and just as expensive to re-probe.
    """

    def __init__(
        self,
        ttl_seconds: float = 30.0,
        max_entries: int = 512,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        self._clock = clock
        self._entries: OrderedDict[
            tuple[str, str, str, str | None], tuple[float, PreviewScan | None]
        ] = OrderedDict()

    def scan(
        self,
        *,
        jail: Path,
        run_id: str,
        sandbox_id: str,
        artifact_path: str | None,
        ttl_seconds: float | None = None,
    ) -> PreviewScan | None:
        """Cached ``resolve_workspace_dir`` + ``scan_preview`` probe.

        ``ttl_seconds`` overrides the instance TTL per call. Concurrent
        results GETs may race a store — the worst case is a duplicate probe.
        """
        ttl = self.ttl_seconds if ttl_seconds is None else ttl_seconds
        if ttl <= 0:
            ws = resolve_workspace_dir(
                jail=jail,
                run_id=run_id,
                sandbox_id=sandbox_id,
                artifact_path=artifact_path,
            )
            return scan_preview(ws) if ws is not None else None
        key = (str(jail), run_id, sandbox_id, artifact_path)
        now = self._clock()
        hit = self._entries.get(key)
        if hit is not None and hit[0] > now:
            self._entries.move_to_end(key)
            return hit[1]
        ws = resolve_workspace_dir(
            jail=jail,
            run_id=run_id,
            sandbox_id=sandbox_id,
            artifact_path=artifact_path,
        )
        scan = scan_preview(ws) if ws is not None else None
        self._entries[key] = (now + ttl, scan)
        self._entries.move_to_end(key)
        while len(self._entries) > self.max_entries:
            self._entries.popitem(last=False)
        return scan

    def clear(self) -> None:
        self._entries.clear()


def _preview_url(
    port: int | None, entry: str | None, preview_type: str = "static"
) -> str | None:
    """Browser URL for a preview — entry dir/file for static, noVNC for
    desktop, the app's root for python-server."""
    if port is None:
        return None
    if preview_type == "desktop":
        return f"http://localhost:{port}/vnc.html?autoconnect=true&resize=scale"
    if preview_type == "python-server":
        # The relay/sidecar forwards to whatever the app serves — root is
        # the entry contract (the app owns its routes).
        return f"http://localhost:{port}/"
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
        server_image: str | None = None,
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
        # Executed-code resource bounds — shared by desktop + server runners.
        self.desktop_cpus = desktop_cpus
        self.desktop_memory = desktop_memory
        self.desktop_pids = desktop_pids
        self.server_image = server_image or DEFAULT_SERVER_IMAGE

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

    def _ensure_internal_network(self) -> None:
        """Create the INTERNAL runner network if missing (idempotent).

        ``heg-desktop`` is the isolation boundary for ALL executed
        artifacts (desktop + python-server runners share it): ``--internal``
        means no outbound route and no published ports — a runner can reach
        only the other executed-code containers on this net (its own socat
        proxy plus any concurrently-running preview runners; all are
        ephemeral candidate code, and none can egress). An existing network
        that is NOT internal is a security misconfiguration (the runners
        would have egress) — logged loudly.
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
        - ``desktop`` / ``python-server``: a runner on the internal
          ``heg-desktop`` net *executing* the artifact (GUI under
          Xvnc/noVNC, or a web app made reachable on :8000 by the runner's
          relay), plus a socat proxy sidecar straddling ``heg-desktop`` +
          ``heg-preview`` that publishes a localhost port.

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
        elif plan.type == "python-server":
            self._spawn_server(
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
        """GUI runner (Xvnc + noVNC :6080) — pygame/pyglet/arcade/tkinter."""
        self._spawn_isolated(
            name=name,
            serve_dir=serve_dir,
            plan=plan,
            labels=labels,
            runner_image=self.desktop_image,
            image_dockerfile="sandbox/images/desktop-preview.Dockerfile",
            container_port=DESKTOP_CONTAINER_PORT,
            env={"APP_ENTRY": plan.entry},
        )

    def _spawn_server(
        self,
        *,
        name: str,
        serve_dir: Path,
        plan: PreviewPlan,
        labels: list[str],
    ) -> None:
        """Web-app runner (heg-preview-server) — the app is made reachable
        on :8000 by the baked-in relay, whatever it actually binds."""
        self._spawn_isolated(
            name=name,
            serve_dir=serve_dir,
            plan=plan,
            labels=labels,
            runner_image=self.server_image,
            image_dockerfile="sandbox/images/server-preview.Dockerfile",
            container_port=SERVER_CONTAINER_PORT,
            env={
                "APP_ENTRY": plan.entry,
                "PORT": str(SERVER_CONTAINER_PORT),
                "HOST": "0.0.0.0",
            },
        )

    def _spawn_isolated(
        self,
        *,
        name: str,
        serve_dir: Path,
        plan: PreviewPlan,
        labels: list[str],
        runner_image: str,
        image_dockerfile: str,
        container_port: int,
        env: dict[str, str],
    ) -> None:
        """Runner on the INTERNAL net + socat proxy sidecar publishing the port.

        Shared topology for ALL executed-code previews (desktop, server —
        and future runner types). The runner *executes* candidate code, so
        unlike a static preview it gets zero egress: ``--internal``
        ``heg-desktop`` — peers are only other ephemeral preview containers
        (its own proxy plus concurrent runners; none can egress). ``socat``
        is a dumb TCP pipe — no HTTP parsing, nothing for the artifact to
        exploit — and is the only container with a published port (bound to
        localhost on ``heg-preview``). CPU/memory/pids limits bound what
        the candidate code can burn.
        """
        self._ensure_internal_network()
        if (
            self._docker(
                ["image", "inspect", runner_image], check=False
            ).returncode
            != 0
        ):
            raise RuntimeError(
                f"{plan.type} preview image {runner_image!r} not found — "
                f"build it: docker build -f {image_dockerfile} "
                f"-t {runner_image} ."
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
            *(a for k, v in env.items() for a in ("-e", f"{k}={v}")),
            "-e",
            "HOME=/tmp",
            "-v",
            f"{self._rt.to_host_path(serve_dir)}:{DESKTOP_ARTIFACT_ROOT}:ro",
            *labels,
            runner_image,
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
            f"{self.bind_host}::{container_port}",
            *self._labels_as_args(labels, role=ROLE_PROXY),
            self.proxy_image,
            "TCP-LISTEN:" + str(container_port) + ",fork,reuseaddr",
            f"TCP:{name}:{container_port}",
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
        proxied = _PROXIED_PORTS.get(preview_type)
        if proxied is not None:
            # The published port lives on the proxy sidecar, not the runner.
            port = self._proxy_port(preview_id, proxied)
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

    def _proxy_port(self, preview_id: str, container_port: int) -> int | None:
        """Published host port of a proxied preview's sidecar (desktop :6080,
        server :8000 — the port the sidecar listens on inside its net)."""
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
            ).get(f"{container_port}/tcp") or []
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
