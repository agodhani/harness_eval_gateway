"""Task-input seeds (PR24) — a run's shared starting workspace.

``RunCreate.task_inputs`` lets a task carry input files: a public git repo
(shallow-cloned at a pinned ref) or an archive the user staged through
``POST /api/uploads`` under ``<data>/uploads/<id>/``. The orchestrator calls
:func:`prepare_run_seed` once per run — the materialized tree lives under
``<data>/seeds/<run_id>/content/`` — and every sandbox's
``SandboxSpec.seed_host_path`` points at it, so :func:`copy_seed` (called by
``runtime.spawn``) lands identical bytes in each cell's workspace. The copy's
content hash is returned to the caller and recorded on the sandbox row as
``harness_config.seed_sha`` — the proof that all cells started identical.

Safety rules (the "seeds can't escape ``data/``" jail):

- Upload ids are uuids; resolution is jailed under ``uploads_root`` exactly
  like the artifact-download jail (``results.py``).
- Archive members must be regular files or dirs with clean relative POSIX
  names — absolute paths, ``..`` segments, drive-letter roots, backslash
  separators and NULs are rejected, as are symlinks, hardlinks, devices,
  fifos and every other non-file member. Extraction writes bytes manually
  (never ``extractall``) under per-call size/file-count caps, so a hostile
  or corrupt archive fails closed.
- After materialization (clone or extract), a sweep drops any symlink whose
  resolved target leaves the seed root — in-tree links stay (real repos
  have them); escapees would resolve against the HOST FS once the artifact
  is snapshotted, walking straight out of the artifact jail.
- Uploads and seeds never write outside ``<data>/uploads`` /
  ``<data>/seeds``.

Synchronous on purpose — the ``git`` CLI and FS work are sync; orchestrator
callers wrap in ``asyncio.to_thread`` like the rest of provisioning.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import stat
import subprocess
import tarfile
import zipfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import IO, Any

log = logging.getLogger(__name__)

#: Suffixes accepted for uploaded seed archives (kind is re-detected by magic
#: bytes at extract time — the name only gates the upload UX).
ALLOWED_UPLOAD_SUFFIXES = (
    ".zip",
    ".tar",
    ".tar.gz",
    ".tgz",
    ".tar.bz2",
    ".tbz2",
    ".tar.xz",
    ".txz",
)

#: Stored name for the archive file under ``<uploads>/<id>/`` — the original
#: filename is kept in ``meta.json`` for display.
UPLOAD_ARCHIVE_NAME = "archive"
UPLOAD_META_NAME = "meta.json"

#: Defaults (settings overridable): extraction caps — a zip-bomb can't blow
#: past these even though the upload itself was already byte-capped.
DEFAULT_SEED_MAX_BYTES = 256 * 1024 * 1024
DEFAULT_SEED_MAX_FILES = 20_000
DEFAULT_GIT_TIMEOUT_SECONDS = 300.0

_DRIVE_RE = re.compile(r"^[A-Za-z]:$")


class SeedError(RuntimeError):
    """Any seed-prep failure — surfaces as the sandbox's spawn error."""


# ---------------------------------------------------------------------------
# data-tree roots
# ---------------------------------------------------------------------------


def uploads_root(settings: Any) -> Path:
    """Root of the upload store: ``<data>/uploads`` (ARTIFACT_ROOT's sibling),
    or ``settings.UPLOADS_DIR`` when explicitly set."""
    override = getattr(settings, "UPLOADS_DIR", None)
    if override:
        return Path(override).resolve()
    return (Path(settings.ARTIFACT_ROOT).resolve().parent / "uploads").resolve()


def _upload_dir(uploads: Path, upload_id: str) -> Path | None:
    """Resolved ``<uploads>/<id>`` dir, jailed — None when it escapes or is
    not a directory (same resolve+parents rule as the artifact jail).
    ``uploads`` itself is resolved first so callers on symlinked roots
    (macOS ``/var`` → ``/private/var``) compare canonical paths."""
    try:
        root = uploads.resolve()
        d = (root / upload_id).resolve()
    except OSError:
        return None
    if d == root or root not in d.parents or not d.is_dir():
        return None
    return d


def read_upload_meta(uploads: Path, upload_id: str) -> dict[str, Any] | None:
    """The upload's ``meta.json`` dict, or None when absent/invalid."""
    d = _upload_dir(uploads, upload_id)
    if d is None:
        return None
    meta_path = d / UPLOAD_META_NAME
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return meta if isinstance(meta, dict) else None


def resolve_upload_archive(uploads: Path, upload_id: str) -> Path | None:
    """The staged archive file for ``upload_id`` (``<uploads>/<id>/archive*``),
    jailed — None when the upload doesn't exist."""
    d = _upload_dir(uploads, upload_id)
    if d is None:
        return None
    try:
        for p in sorted(d.iterdir()):
            if p.name.startswith(UPLOAD_ARCHIVE_NAME) and p.is_file():
                return p
    except OSError:
        return None
    return None


# ---------------------------------------------------------------------------
# archive member safety (shared by upload-time check and extraction)
# ---------------------------------------------------------------------------


def _member_relpath(name: str) -> PurePosixPath | None:
    """Validate one archive member name → a safe relative path.

    Returns None for the root-dir entry itself (``"."`` — harmless). Raises
    :class:`SeedError` on anything that could land bytes outside the
    destination: absolute paths, ``..`` segments, drive-letter roots, NULs,
    empty names. Backslashes are normalized to ``/`` first so a
    ``..\\evil`` member can't hide a separator from the check.
    """
    if not name or "\x00" in name:
        raise SeedError(f"archive member name is empty or contains NUL: {name!r}")
    norm = name.replace("\\", "/")
    if norm.startswith("/"):
        raise SeedError(f"archive member path is absolute: {name!r}")
    p = PurePosixPath(norm)
    parts = p.parts  # collapses "." components and repeats "/"
    if not parts or parts == (".",):
        return None
    if parts[0] == "~" or _DRIVE_RE.match(parts[0]):
        raise SeedError(f"archive member path escapes the workspace: {name!r}")
    if any(part == ".." for part in parts):
        raise SeedError(f"archive member path escapes the workspace: {name!r}")
    return PurePosixPath(*parts)


def _common_root_part(rels: list[PurePosixPath]) -> str | None:
    """The single shared first path segment of ``rels`` when it denotes a
    directory — i.e. every member has that first part and at least one has
    more parts. Archives packed as ``proj/…`` extract at the workspace root
    instead of under ``proj/``; a bare single file stays put."""
    if not rels:
        return None
    first = rels[0].parts[0]
    if all(rel.parts[0] == first for rel in rels) and any(
        len(rel.parts) > 1 for rel in rels
    ):
        return first
    return None


@dataclass(frozen=True)
class _Member:
    """One safe archive member — ``open_fn`` streams file bytes (dirs: None)."""

    relpath: PurePosixPath
    is_dir: bool
    size: int
    open_fn: Callable[[], IO[bytes]] | None = field(repr=False, default=None)


def _iter_zip_members(archive: Path) -> Iterator[_Member]:
    with zipfile.ZipFile(archive) as zf:
        infos = zf.infolist()
        rels = [_member_relpath(i.filename) for i in infos]
        prefix = _common_root_part([r for r in rels if r is not None])
        for info, rel in zip(infos, rels, strict=True):
            if rel is None:
                continue
            if prefix is not None:
                if len(rel.parts) <= 1:
                    continue  # the stripped root entry itself
                rel = PurePosixPath(*rel.parts[1:])
            if info.is_dir():
                yield _Member(relpath=rel, is_dir=True, size=0)
                continue
            # Zip symlinks: unix mode in the high 16 bits of external_attr.
            if stat.S_IFMT(info.external_attr >> 16) == stat.S_IFLNK:
                raise SeedError(
                    f"archive member is a symlink — rejected: {info.filename!r}"
                )
            # Hold a reference to the info + zf; the generator stays inside
            # `with` so the member remains readable while iterated.
            yield _Member(
                relpath=rel,
                is_dir=False,
                size=int(info.file_size),
                open_fn=lambda i=info, z=zf: z.open(i, "r"),
            )


def _iter_tar_members(archive: Path) -> Iterator[_Member]:
    with tarfile.open(archive, "r:*") as tf:
        members = tf.getmembers()
        rels = [_member_relpath(m.name) for m in members]
        prefix = _common_root_part([r for r in rels if r is not None])
        for m, rel in zip(members, rels, strict=True):
            if rel is None:
                continue
            if prefix is not None:
                if len(rel.parts) <= 1:
                    continue  # the stripped root entry itself
                rel = PurePosixPath(*rel.parts[1:])
            if m.isdir():
                yield _Member(relpath=rel, is_dir=True, size=0)
                continue
            if not m.isreg():
                # SYMTYPE/LNKTYPE/CHRTYPE/BLKTYPE/FIFOTYPE/sparse/… — links
                # and special files can never land in a workspace.
                raise SeedError(
                    f"archive member {m.name!r} is not a regular file "
                    f"(type {m.type!r}) — rejected"
                )
            yield _Member(
                relpath=rel,
                is_dir=False,
                size=int(m.size),
                open_fn=lambda mm=m, t=tf: t.extractfile(mm),
            )


def _archive_kind(path: Path) -> str:
    """Detect by content, not name: ``"zip"`` or ``"tar"``; SeedError else."""
    try:
        if zipfile.is_zipfile(path):
            return "zip"
    except OSError:
        pass
    try:
        if tarfile.is_tarfile(path):
            return "tar"
    except OSError:
        pass
    raise SeedError(f"{path.name!r} is not a readable zip or tar archive")


def _iter_members(archive: Path) -> Iterator[_Member]:
    kind = _archive_kind(archive)
    if kind == "zip":
        return _iter_zip_members(archive)
    return _iter_tar_members(archive)


def check_archive(archive: Path) -> str:
    """Upload-time safety pass — walks member names/types without writing a
    byte. Returns the detected ``"zip"``/``"tar"`` kind; SeedError on
    unreadable archives, unsafe members, or an archive with no files."""
    count = 0
    for member in _iter_members(archive):
        if not member.is_dir:
            count += 1
    if count == 0:
        raise SeedError("archive contains no extractable files")
    return _archive_kind(archive)


def _jailed_join(root: Path, rel: PurePosixPath) -> Path:
    """``root / rel`` resolved — must stay inside root (defense in depth on
    top of ``_member_relpath``)."""
    out = root.joinpath(*rel.parts).resolve()
    if out != root and root not in out.parents:
        raise SeedError(f"member path escapes the workspace: {rel}")
    return out


def extract_archive(
    archive: Path,
    dest: Path,
    *,
    max_bytes: int = DEFAULT_SEED_MAX_BYTES,
    max_files: int = DEFAULT_SEED_MAX_FILES,
) -> int:
    """Safely extract ``archive`` into ``dest``; returns files written.

    A single common top-level directory is flattened (``proj/x`` → ``x``) so
    the workspace mirrors the repo's root, matching the git-clone shape.
    Only regular files and dirs survive; caps stop decompression bombs.
    """
    root = dest.resolve()
    dest.mkdir(parents=True, exist_ok=True)
    written = 0
    total = 0
    for member in _iter_members(archive):
        out = _jailed_join(root, member.relpath)
        if member.is_dir:
            out.mkdir(parents=True, exist_ok=True)
            continue
        total += member.size
        if member.size > max_bytes or total > max_bytes:
            raise SeedError(
                f"archive exceeds the seed size cap ({max_bytes} bytes)"
            )
        written += 1
        if written > max_files:
            raise SeedError(
                f"archive exceeds the seed file cap ({max_files} files)"
            )
        out.parent.mkdir(parents=True, exist_ok=True)
        assert member.open_fn is not None  # files always carry a reader
        with member.open_fn() as src, out.open("wb") as dst:
            shutil.copyfileobj(src, dst, length=1024 * 1024)
    if written == 0:
        raise SeedError("archive contains no extractable files")
    return written


# ---------------------------------------------------------------------------
# git seeds
# ---------------------------------------------------------------------------


def _git(argv: list[str], *, timeout: float | None = None) -> str:
    """Default git runner — runs ``git`` with prompting disabled so a
    private/bad URL fails instead of hanging on an auth prompt."""
    env = {
        **os.environ,
        "GIT_TERMINAL_PROMPT": "0",
        # A GUI askpass could still pop a dialog on some platforms — the
        # `true` binary answers nothing instantly.
        "GIT_ASKPASS": "true",
        "SSH_ASKPASS": "true",
    }
    try:
        proc = subprocess.run(
            ["git", *argv],
            capture_output=True,
            text=True,
            errors="replace",
            env=env,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as e:
        raise SeedError(f"git {' '.join(argv[:2])} timed out after {timeout}s") from e
    except FileNotFoundError as e:
        raise SeedError("git executable not found — cannot seed from repo_url") from e
    if proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip()
        raise SeedError(
            f"git {' '.join(argv[:2])} failed (exit {proc.returncode}): "
            f"{detail[:400]}"
        )
    return proc.stdout


def git_runner(timeout: float | None = None) -> Callable[[list[str]], str]:
    """The production git runner — one subprocess per argv, ``timeout``
    seconds per call (None = uncapped)."""

    def run(argv: list[str]) -> str:
        return _git(argv, timeout=timeout)

    return run


#: Alias of the factory for use inside ``prepare_run_seed``, whose
#: ``git_runner`` *parameter* shadows the module-level name in that body.
_make_git_runner = git_runner


def _clone_repo(
    url: str,
    ref: str | None,
    dest: Path,
    *,
    runner: Callable[[list[str]], str],
) -> str:
    """Shallow-clone ``url`` into ``dest`` at ``ref`` (branch, tag, or commit
    sha); returns the resolved HEAD sha.

    Uses init + ``fetch --depth 1 origin <ref>`` + ``checkout FETCH_HEAD``
    rather than ``clone --branch`` — one code path covers branches, tags,
    and raw shas alike (a bare ``--branch`` can't take a sha). No ref → the
    remote's ``HEAD`` (default branch tip).
    """
    if not url.startswith("https://"):
        raise SeedError(
            f"repo_url must be an https:// URL — got {url!r} "
            "(public git only; no ssh/file:// schemes)"
        )
    dest.mkdir(parents=True, exist_ok=True)
    runner(["init", "-q", str(dest)])
    runner(["-C", str(dest), "remote", "add", "origin", url])
    runner(["-C", str(dest), "fetch", "-q", "--depth", "1", "origin", ref or "HEAD"])
    runner(["-C", str(dest), "checkout", "-q", "FETCH_HEAD"])
    return runner(["-C", str(dest), "rev-parse", "HEAD"]).strip()


# ---------------------------------------------------------------------------
# tree hashing + copying
# ---------------------------------------------------------------------------

_HASH_CHUNK = 1024 * 1024


def hash_tree(root: Path) -> str:
    """Deterministic content hash of a directory tree.

    Hashes ``relpath + file bytes`` in sorted order; symlink entries hash
    their *target text* (the link itself is the content). Empty dirs carry
    no bytes so they don't contribute — the hash answers "same files?"
    which is the identical-bytes contract between cells.
    """
    h = hashlib.sha256()
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames.sort()
        for name in sorted(filenames):
            p = Path(dirpath) / name
            try:
                rel = p.relative_to(root).as_posix()
            except ValueError:
                continue
            h.update(rel.encode("utf-8"))
            h.update(b"\0")
            try:
                if p.is_symlink():
                    h.update(b"L\0")
                    h.update(os.readlink(p).encode("utf-8", "surrogateescape"))
                    h.update(b"\0")
                    continue
                h.update(b"F\0")
                with p.open("rb") as fh:
                    while chunk := fh.read(_HASH_CHUNK):
                        h.update(chunk)
            except OSError:
                # Vanished/unreadable entry — hash its absence marker so two
                # identical failures still agree.
                h.update(b"!\0")
            h.update(b"\0")
    return h.hexdigest()


def _drop_escapee_links(root: Path) -> list[str]:
    """Unlink symlinks whose resolved target escapes ``root``.

    Returns the dropped workspace-relative names (for warnings). In-tree
    links stay: real repos carry them, and every artifact consumer (zip
    download, preview scan, workspace sizing) already skips links safely.
    A link pointing OUT resolves against the host FS once the workspace is
    snapshotted — that's the escape this sweep closes.
    """
    dropped: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for name in [*dirnames, *filenames]:
            p = Path(dirpath) / name
            try:
                if not p.is_symlink():
                    continue
            except OSError:
                continue
            try:
                target = p.resolve()
            except OSError:
                target = Path(os.path.abspath(os.readlink(p)))
            if target == root or root in target.parents:
                continue
            try:
                p.unlink()
                dropped.append(p.relative_to(root).as_posix())
            except OSError:
                log.warning("could not drop escapee symlink %s", p)
            if name in dirnames:
                dirnames.remove(name)
    return dropped


def copy_seed(seed_dir: Path, workspace_dir: Path) -> str:
    """Copy the prepared seed tree into a sandbox workspace; returns the
    copied tree's content hash — every cell gets the same bytes, proven by
    ``harness_config.seed_sha`` on its sandbox row."""
    src = Path(seed_dir)
    ws = Path(workspace_dir)
    ws.mkdir(parents=True, exist_ok=True)
    if not src.is_dir():
        raise SeedError(f"seed dir {src} does not exist")
    shutil.copytree(src, ws, dirs_exist_ok=True, symlinks=True)
    return hash_tree(ws)


# ---------------------------------------------------------------------------
# prepare_run_seed — one materialized seed per run
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PreparedSeed:
    """A materialized run seed: the content dir to copy into workspaces."""

    path: Path
    sha256: str
    #: Provenance descriptor recorded as ``harness_config.seed`` on each
    #: sandbox row — ``{"type": "repo"|"archive", ...source detail}``.
    source: dict[str, Any]
    warnings: tuple[str, ...] = ()


def _utcnow_iso() -> str:
    return datetime.now(UTC).isoformat()


def _seed_meta(seeds_dir: Path) -> dict[str, Any] | None:
    try:
        meta = json.loads((seeds_dir / "meta.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return meta if isinstance(meta, dict) else None


def prepare_run_seed(
    run_id: Any,
    task_inputs: dict[str, Any],
    *,
    seed_root: Path,
    uploads: Path,
    git_runner: Callable[[list[str]], str] | None = None,
    max_bytes: int = DEFAULT_SEED_MAX_BYTES,
    max_files: int = DEFAULT_SEED_MAX_FILES,
) -> PreparedSeed | None:
    """Materialize the run's task input once, idempotently.

    ``seed_root`` is ``<data>/seeds``; the tree lands at
    ``<seed_root>/<run_id>/content/`` next to a ``meta.json`` carrying the
    hash + provenance. A later call with the same run_id reuses the dir —
    sequential spawns only ever materialize once. Returns None when
    ``task_inputs`` is empty/absent (unseeded run).
    """
    ti = task_inputs or {}
    repo_url = str(ti.get("repo_url") or "").strip()
    ref = str(ti.get("ref") or "").strip() or None
    archive_id = str(ti.get("archive_id") or "").strip()
    if not repo_url and not archive_id:
        return None
    if repo_url and archive_id:
        raise SeedError("task_inputs takes exactly one source (repo_url OR archive_id)")
    if ref and not repo_url:
        raise SeedError("task_inputs.ref requires repo_url")

    seeds_dir = Path(seed_root) / str(run_id)
    content = seeds_dir / "content"
    # Idempotent reuse: content + meta appear together via the atomic rename
    # below — a pair means a completed materialization.
    if content.is_dir():
        meta = _seed_meta(seeds_dir)
        if isinstance(meta, dict) and isinstance(meta.get("sha256"), str):
            source = meta.get("source")
            warnings = meta.get("warnings")
            return PreparedSeed(
                path=content,
                sha256=str(meta["sha256"]),
                source=source if isinstance(source, dict) else {},
                warnings=tuple(warnings) if isinstance(warnings, list) else (),
            )
        # Meta unreadable — rematerialize rather than trust the dir.
        shutil.rmtree(seeds_dir, ignore_errors=True)

    run_key = re.sub(r"[^A-Za-z0-9_-]", "_", str(run_id))
    tmp = Path(seed_root) / f".tmp-{run_key}-{os.urandom(4).hex()}"
    tmp_content = tmp / "content"
    warnings: list[str] = []
    runner = git_runner or _make_git_runner(DEFAULT_GIT_TIMEOUT_SECONDS)
    try:
        if repo_url:
            resolved = _clone_repo(repo_url, ref, tmp_content, runner=runner)
            source: dict[str, Any] = {
                "type": "repo",
                "url": repo_url,
                "ref": ref,
                "resolved_sha": resolved or None,
            }
        else:
            archive = resolve_upload_archive(Path(uploads), archive_id)
            if archive is None:
                raise SeedError(
                    f"upload {archive_id!r} not found under {uploads} — "
                    "upload the archive via POST /api/uploads first"
                )
            meta = read_upload_meta(Path(uploads), archive_id) or {}
            extract_archive(
                archive, tmp_content, max_bytes=max_bytes, max_files=max_files
            )
            source = {
                "type": "archive",
                "upload_id": archive_id,
                "filename": meta.get("filename"),
                "upload_sha256": meta.get("sha256"),
            }
        dropped = _drop_escapee_links(tmp_content)
        if dropped:
            warnings.append(
                "dropped out-of-tree symlink(s): " + ", ".join(dropped[:8])
            )
        sha = hash_tree(tmp_content)
        (tmp / "meta.json").write_text(
            json.dumps(
                {
                    "sha256": sha,
                    "source": source,
                    "warnings": warnings,
                    "prepared_at": _utcnow_iso(),
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        tmp.parent.mkdir(parents=True, exist_ok=True)
        # seeds_dir only ever appears via this atomic rename (a stray dir
        # without its content/meta pair is rematerialization bait).
        if seeds_dir.exists():
            shutil.rmtree(seeds_dir, ignore_errors=True)
        os.rename(tmp, seeds_dir)
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    finally:
        # Best-effort GC of stale staging dirs for THIS run (crash leftovers).
        for stale in Path(seed_root).glob(f".tmp-{run_key}-*"):
            if stale.is_dir():
                shutil.rmtree(stale, ignore_errors=True)
    log.info(
        "prepared run seed for %s from %s (sha=%s)",
        run_id,
        source.get("url") or source.get("upload_id"),
        sha[:12],
    )
    return PreparedSeed(
        path=seeds_dir / "content", sha256=sha, source=source, warnings=tuple(warnings)
    )
