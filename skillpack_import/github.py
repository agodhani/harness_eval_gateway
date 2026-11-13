"""GitHub fetch + SKILL.md discovery for user-imported skill packs.

Public surface used by ``api/routers/skillpacks.py``:

- :func:`parse_github_url` — repo-root / ``/tree/<ref>/<path>`` /
  ``/blob/<ref>/<path>/SKILL.md`` / ``raw.githubusercontent.com`` links.
- :func:`fetch_repo` — resolve the (possibly ambiguous) ref and download the
  repo tarball from codeload in one shot. ``/tree/<branch>/<path>`` URLs are
  ambiguous when branch names contain slashes; candidate refs are probed
  longest-first (GitHub's own longest-match semantics), each probe being the
  tarball request itself — a 404 just means "try a shorter ref".
- :func:`scan_skills` — discover ``SKILL.md`` files under the resolved
  subpath and parse frontmatter (name/description) without extracting.
- :func:`extract_skill_files` — pull whole skill directories (SKILL.md +
  references/scripts/assets) for import.

I/O is injectable: everything network-facing goes through the ``Fetcher``
protocol so tests pass a fake (no HTTP). ``HttpxFetcher`` is the production
implementation (streaming, byte cap, redirects).

Caps guard the control plane against abusive archives: tarball download,
member count, per-file and per-skill sizes, total extraction bytes.
"""

from __future__ import annotations

import io
import json
import posixpath
import re
import tarfile
from dataclasses import dataclass, field
from typing import Literal, Protocol
from urllib.parse import quote, urlsplit

import httpx

from adapters.miniloop.skills import parse_frontmatter

# ---------------------------------------------------------------------------
# limits + constants
# ---------------------------------------------------------------------------

MAX_TARBALL_BYTES = 40 * 1024 * 1024  # download cap (skillpack repos are docs)
MAX_MEMBER_FILES = 20_000             # tar member count guard
MAX_SKILL_MD_BYTES = 1024 * 1024      # a single SKILL.md is never this big
MAX_SKILL_FILE_BYTES = 2 * 1024 * 1024  # per extracted file inside a skill dir
MAX_SKILL_FILES = 200                 # files per skill dir
MAX_EXTRACT_BYTES = 30 * 1024 * 1024  # total bytes written for one import
MAX_DISCOVERED_SKILLS = 300

GITHUB_HOSTS = {"github.com", "www.github.com"}
RAW_HOST = "raw.githubusercontent.com"
CODELOAD_TARBALL = "https://codeload.github.com/{owner}/{repo}/tar.gz/{ref}"

_SKIP_DIRS = {".git", "node_modules", "__pycache__"}  # mirrors miniloop loader
_DESCRIPTION_MAX = 240

_OWNER_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")

SUPPORTED_URL_HINT = (
    "paste a repo root (github.com/owner/repo), a subdirectory "
    "(…/tree/<branch>/<path>), or a SKILL.md link (…/blob/<branch>/<path>/SKILL.md)"
)


# ---------------------------------------------------------------------------
# errors
# ---------------------------------------------------------------------------


class FetchError(Exception):
    """Typed failure; ``kind`` maps to an HTTP status in the router.

    Kinds: ``invalid_url`` (malformed/unsupported link), ``not_found``
    (repo/ref/file missing or private), ``rate_limited``, ``network``
    (transport or unexpected upstream status), ``too_large`` (cap hit),
    ``bad_archive`` (unreadable tarball), ``no_skills`` (zero SKILL.md found),
    ``ref_mismatch`` (client-pinned ref inconsistent with the URL shape).
    """

    def __init__(self, kind: str, detail: str) -> None:
        super().__init__(detail)
        self.kind = kind
        self.detail = detail


# ---------------------------------------------------------------------------
# URL parsing
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GithubTarget:
    """A parsed GitHub link.

    ``segments`` holds the ambiguous ``<ref>/<path>`` tail for tree/blob
    links — which prefix is the ref is resolved at fetch time. For root
    links ``segments`` is empty and the ref is ``HEAD``.
    """

    owner: str
    repo: str
    kind: Literal["root", "tree", "blob"]
    segments: tuple[str, ...] = ()

    @property
    def repo_slug(self) -> str:
        return f"{self.owner}/{self.repo}"


def parse_github_url(url: str) -> GithubTarget:
    """Parse a GitHub repo/tree/blob/raw link into a :class:`GithubTarget`.

    Accepts: ``github.com/o/r`` (with or without scheme, optional ``.git``,
    query/fragment ignored), ``…/tree/<ref>/<path…>``, ``…/blob/<ref>/<path>``,
    and ``raw.githubusercontent.com/o/r/<ref>/<path…>``. Anything else on
    github.com (issues, pulls, settings, …) is rejected — whitelist, not
    blacklist.
    """
    raw = (url or "").strip()
    if not raw:
        raise FetchError("invalid_url", "empty GitHub URL")
    if "://" not in raw:
        raw = "https://" + raw.lstrip("/")
    try:
        parsed = urlsplit(raw)
    except ValueError as exc:
        raise FetchError("invalid_url", f"unparseable URL: {exc}") from exc
    if parsed.scheme not in ("http", "https"):
        raise FetchError("invalid_url", f"unsupported scheme {parsed.scheme!r}")
    host = parsed.netloc.lower()
    segs = tuple(s for s in parsed.path.split("/") if s)

    if host == RAW_HOST:
        # raw.githubusercontent.com/<o>/<r>/<ref>/<path…> — like a blob link.
        if len(segs) < 4:
            raise FetchError(
                "invalid_url",
                f"raw link needs /<owner>/<repo>/<ref>/<path>; {SUPPORTED_URL_HINT}",
            )
        owner, repo, segments = segs[0], segs[1], segs[2:]
        kind: Literal["root", "tree", "blob"] = "blob"
    elif host in GITHUB_HOSTS:
        if len(segs) < 2:
            raise FetchError("invalid_url", f"URL has no owner/repo; {SUPPORTED_URL_HINT}")
        owner, repo = segs[0], segs[1]
        repo = repo.removesuffix(".git")
        rest = segs[2:]
        if not rest:
            kind, segments = "root", ()
        elif rest[0] in ("tree", "blob"):
            kind = rest[0]
            segments = rest[1:]
            if not segments:
                raise FetchError(
                    "invalid_url",
                    f"/{rest[0]} link is missing the branch/path; {SUPPORTED_URL_HINT}",
                )
        else:
            raise FetchError(
                "invalid_url",
                f"unsupported GitHub page {rest[0]!r}; {SUPPORTED_URL_HINT}",
            )
    else:
        raise FetchError(
            "invalid_url",
            f"not a GitHub link (host {host!r}); {SUPPORTED_URL_HINT}",
        )

    if not _OWNER_RE.match(owner):
        raise FetchError("invalid_url", f"invalid owner name {owner!r}")
    if not _REPO_RE.match(repo) or repo in (".", ".."):
        raise FetchError("invalid_url", f"invalid repo name {repo!r}")
    if kind == "blob" and posixpath.basename(segments[-1] if segments else "") != "SKILL.md":
        raise FetchError(
            "invalid_url",
            "file links must point at a SKILL.md; " + SUPPORTED_URL_HINT,
        )
    return GithubTarget(owner=owner, repo=repo, kind=kind, segments=segments)


# ---------------------------------------------------------------------------
# fetch plumbing (injectable)
# ---------------------------------------------------------------------------


@dataclass
class FetchResponse:
    status: int
    body: bytes = b""
    headers: dict[str, str] = field(default_factory=dict)


class Fetcher(Protocol):
    """Minimal async HTTP-get shape — fakeable in tests."""

    async def get(
        self, url: str, *, headers: dict[str, str] | None = None, max_bytes: int
    ) -> FetchResponse: ...


class HttpxFetcher:
    """Production :class:`Fetcher` — streaming GET with a hard byte cap."""

    def __init__(self, timeout: float = 25.0) -> None:
        self._timeout = timeout

    async def get(
        self, url: str, *, headers: dict[str, str] | None = None, max_bytes: int
    ) -> FetchResponse:
        try:
            async with httpx.AsyncClient(
                follow_redirects=True, timeout=self._timeout
            ) as client, client.stream("GET", url, headers=headers or {}) as resp:
                status = resp.status_code
                if status != 200:
                    # Drain small error bodies so limits stay predictable,
                    # then drop them — only the status matters.
                    await resp.aread()
                    return FetchResponse(
                        status=status, headers=dict(resp.headers.items())
                    )
                chunks: list[bytes] = []
                total = 0
                async for chunk in resp.aiter_bytes(64 * 1024):
                    total += len(chunk)
                    if total > max_bytes:
                        raise FetchError(
                            "too_large",
                            f"download exceeds the {max_bytes // (1024 * 1024)} MiB cap",
                        )
                    chunks.append(chunk)
                return FetchResponse(
                    status=200,
                    body=b"".join(chunks),
                    headers=dict(resp.headers.items()),
                )
        except httpx.HTTPError as exc:
            raise FetchError("network", f"{type(exc).__name__}: {exc}") from exc


def _auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"} if token else {}


# ---------------------------------------------------------------------------
# ref resolution + tarball fetch
# ---------------------------------------------------------------------------


@dataclass
class RepoSnapshot:
    """A downloaded repo tarball plus the resolved addressing."""

    data: bytes
    repo: str                 # "owner/repo"
    ref: str                  # resolved ref actually fetched (branch/sha/HEAD)
    subpath: str              # dir (tree/root) or file path (blob); "" = root
    kind: str                 # root | tree | blob
    commit_sha: str | None = None
    canonical_url: str = ""   # normalized github.com/owner/repo[/tree/…] URL


def _subpath_after(target: GithubTarget, ref: str) -> str:
    """Remainder of the URL tail once ``ref`` consumes its segments."""
    if not target.segments:
        return ""  # root link — no path tail to consume
    ref_parts = len([p for p in ref.split("/") if p])
    if ref_parts > len(target.segments):
        raise FetchError(
            "ref_mismatch",
            f"pinned ref {ref!r} is longer than the URL path in {target.repo_slug}",
        )
    return "/".join(target.segments[ref_parts:])


def _sha_from_tarball(data: bytes) -> str | None:
    """Best-effort sha from codeload's ``<repo>-<sha>/`` root dir — only set
    when the fetched ref WAS a sha/tag-ish (branch dirs carry the ref name)."""
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as tf:
            first = tf.next()
    except (tarfile.TarError, OSError):
        return None
    if first is None:
        return None
    tail = first.name.rstrip("/").rsplit("-", 1)[-1]
    return tail if re.fullmatch(r"[0-9a-f]{7,40}", tail) else None


async def _resolve_commit_sha(
    target: GithubTarget,
    ref: str,
    fetcher: Fetcher,
    headers: dict[str, str],
) -> str | None:
    """Best-effort ref→sha resolution via api.github.com.

    ``git/ref/heads|tags/<ref>`` answers in a tiny payload and takes slash-
    containing branch names verbatim; ``commits/<ref>`` covers ``HEAD`` and
    non-branch refs (heavier body — capped at 1 MiB). Any failure — rate
    limit, network — degrades to ``None``: provenance is advisory, never
    fetch-blocking.
    """
    if re.fullmatch(r"[0-9a-f]{40}", ref):
        return ref
    paths = (
        [f"commits/{quote(ref, safe='')}"] if ref == "HEAD"
        else [
            f"git/ref/heads/{ref}",
            f"git/ref/tags/{ref}",
            f"commits/{quote(ref, safe='')}",
        ]
    )
    for path in paths:
        try:
            resp = await fetcher.get(
                f"https://api.github.com/repos/{target.repo_slug}/{path}",
                headers=headers,
                max_bytes=1024 * 1024,
            )
            if resp.status != 200:
                continue
            data = json.loads(resp.body)
        except (FetchError, json.JSONDecodeError, UnicodeDecodeError):
            continue
        if path.startswith("git/"):
            sha = (data.get("object") or {}).get("sha") if isinstance(data, dict) else None
        else:
            sha = data.get("sha") if isinstance(data, dict) else None
        if isinstance(sha, str) and re.fullmatch(r"[0-9a-f]{7,40}", sha):
            return sha
    return None


def _raise_for_status(status: int, headers: dict[str, str], repo: str) -> None:
    if status == 403 and (
        headers.get("x-ratelimit-remaining") == "0"
        or headers.get("X-RateLimit-Remaining") == "0"
    ):
        raise FetchError(
            "rate_limited",
            "GitHub rate limit hit — set GITHUB_TOKEN on the api to raise it",
        )
    raise FetchError("network", f"GitHub returned HTTP {status} for {repo}")


async def fetch_repo(
    target: GithubTarget,
    fetcher: Fetcher,
    *,
    token: str = "",
    pinned_ref: str | None = None,
    pinned_subpath: str | None = None,
) -> RepoSnapshot:
    """Resolve the ref + download the repo tarball (one HTTP call per probe).

    Candidate refs (longest-first for tree/blob URLs) are probed against
    codeload; the first 200 IS the payload, so a well-formed link costs one
    request. ``pinned_ref`` (from a prior discover call — resolved ref or
    sha) skips probing and reproducibly re-fetches the same tree; pair it
    with ``pinned_subpath`` when the pinned ref's segment count differs from
    the URL's original ref (e.g. pinning a sha for a slash-containing
    branch).
    """
    if pinned_ref:
        subpath = (
            pinned_subpath
            if pinned_subpath is not None
            else _subpath_after(target, pinned_ref)
        )
        candidates = [(pinned_ref, subpath)]
    elif target.kind == "root":
        candidates = [("HEAD", "")]
    else:
        segs = target.segments
        candidates = [
            ("/".join(segs[:i]), "/".join(segs[i:]))
            for i in range(len(segs), 0, -1)
        ]

    headers = _auth_headers(token)
    for ref, subpath in candidates:
        resp = await fetcher.get(
            CODELOAD_TARBALL.format(
                owner=target.owner, repo=target.repo, ref=ref
            ),
            headers=headers,
            max_bytes=MAX_TARBALL_BYTES,
        )
        if resp.status == 200:
            canonical = f"https://github.com/{target.repo_slug}"
            if target.kind == "tree" and subpath:
                canonical += f"/tree/{ref}/{subpath}"
            elif target.kind == "blob" and subpath:
                canonical += f"/blob/{ref}/{subpath}"
            sha = _sha_from_tarball(resp.body) or await _resolve_commit_sha(
                target, ref, fetcher, headers
            )
            return RepoSnapshot(
                data=resp.body,
                repo=target.repo_slug,
                ref=ref,
                subpath=subpath,
                kind=target.kind,
                commit_sha=sha,
                canonical_url=canonical,
            )
        if resp.status != 404:
            _raise_for_status(resp.status, resp.headers, target.repo_slug)
        # 404: either the ref is wrong (probe shorter) or repo private/missing.
    raise FetchError(
        "not_found",
        f"{target.repo_slug} not found at ref {candidates[0][0]!r} — "
        "check the link; private repos need a GITHUB_TOKEN",
    )


# ---------------------------------------------------------------------------
# tarball scanning / extraction
# ---------------------------------------------------------------------------


@dataclass
class SkillCandidate:
    """One discovered skill directory under the resolved subpath."""

    dir: str              # repo-relative skill dir — the selection token
    name: str             # frontmatter name (dir-name fallback)
    description: str
    file_count: int       # files inside the skill dir (incl. SKILL.md)
    has_frontmatter: bool


def _strip_root(name: str) -> str | None:
    """Drop codeload's ``<owner>-<repo>-<sha>/`` prefix; None = not inside it."""
    name = name.removeprefix("./")
    if "/" not in name:
        return None
    return name.split("/", 1)[1]


def _under_subpath(rel: str, subpath: str) -> bool:
    return not subpath or rel == subpath or rel.startswith(subpath + "/")


def _in_skip_dir(rel: str) -> bool:
    return any(part in _SKIP_DIRS for part in rel.split("/"))


def _first_body_line(body: str) -> str:
    for line in body.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            return line[:_DESCRIPTION_MAX]
    return ""


def _open_tar(data: bytes) -> tarfile.TarFile:
    try:
        return tarfile.open(fileobj=io.BytesIO(data), mode="r:*")
    except tarfile.TarError as exc:
        raise FetchError("bad_archive", f"unreadable repo archive: {exc}") from exc


def scan_skills(data: bytes, *, subpath: str = "") -> list[SkillCandidate]:
    """Discover skill dirs (any dir containing ``SKILL.md``) in a repo tarball.

    ``subpath`` limits the scan to a repo subtree (from a ``/tree/`` link).
    A SKILL.md at the repo (or subpath) ROOT is reported with dir ``"."`` —
    the whole subtree is the skill dir.
    """
    file_lists: dict[str, list[str]] = {}
    skill_md: dict[str, str] = {}  # skill dir -> SKILL.md text
    total_files = 0

    try:
        with _open_tar(data) as tf:
            for member in tf:
                if not member.isfile():
                    continue
                rel = _strip_root(member.name)
                if rel is None or _in_skip_dir(rel) or not _under_subpath(rel, subpath):
                    continue
                total_files += 1
                if total_files > MAX_MEMBER_FILES:
                    raise FetchError(
                        "too_large",
                        f"repo has more than {MAX_MEMBER_FILES} files under the link",
                    )
                parent = posixpath.dirname(rel)
                file_lists.setdefault(parent, []).append(rel)
                if posixpath.basename(rel) == "SKILL.md":
                    if member.size > MAX_SKILL_MD_BYTES:
                        raise FetchError(
                            "too_large", f"{rel} exceeds the SKILL.md size cap"
                        )
                    f = tf.extractfile(member)
                    raw = f.read() if f is not None else b""
                    skill_md[parent or "."] = raw.decode("utf-8", errors="replace")
    except tarfile.TarError as exc:
        raise FetchError("bad_archive", f"unreadable repo archive: {exc}") from exc

    all_files = sorted(f for files in file_lists.values() for f in files)
    candidates: list[SkillCandidate] = []
    for d in sorted(skill_md):
        meta, body = parse_frontmatter(skill_md[d])
        fallback = posixpath.basename(d) if d != "." else "root"
        name = str(meta.get("name") or "").strip() or fallback
        description = str(meta.get("description") or "").strip() or _first_body_line(body)
        count = sum(1 for f in all_files if f == d or f.startswith(d + "/")) if d != "." else len(all_files)
        candidates.append(
            SkillCandidate(
                dir=d,
                name=name,
                description=description,
                file_count=count,
                has_frontmatter=bool(meta),
            )
        )
        if len(candidates) > MAX_DISCOVERED_SKILLS:
            raise FetchError(
                "too_large",
                f"more than {MAX_DISCOVERED_SKILLS} SKILL.md files under the link",
            )
    return candidates


def extract_skill_files(
    data: bytes, wanted_dirs: set[str]
) -> dict[str, dict[str, bytes]]:
    """Pull full skill dirs for the selected ``wanted_dirs``.

    Returns ``{skill_dir: {relpath_within_dir: bytes}}`` — dirs absent from
    the tarball are simply missing from the result (the caller reports them
    as unknown selections). Non-regular members and path-traversal names are
    skipped, sizes capped.
    """
    extracted: dict[str, dict[str, bytes]] = {}
    total_bytes = 0
    try:
        with _open_tar(data) as tf:
            for member in tf:
                if not member.isfile():
                    continue
                rel = _strip_root(member.name)
                if rel is None or _in_skip_dir(rel):
                    continue
                # "." = repo-root skill dir — every file belongs to it.
                hit = next(
                    (d for d in wanted_dirs if d == "." or rel.startswith(d + "/")),
                    None,
                )
                if hit is None:
                    continue
                inner = posixpath.normpath(
                    rel if hit == "." else rel[len(hit) + 1 :]
                )
                if inner.startswith(("..", "/")) or "\x00" in inner:
                    continue  # traversal-looking name — never write it
                files = extracted.setdefault(hit, {})
                if len(files) >= MAX_SKILL_FILES:
                    raise FetchError(
                        "too_large",
                        f"skill dir {hit!r} has more than {MAX_SKILL_FILES} files",
                    )
                if member.size > MAX_SKILL_FILE_BYTES:
                    raise FetchError(
                        "too_large",
                        f"{rel} exceeds the {MAX_SKILL_FILE_BYTES // (1024 * 1024)} MiB file cap",
                    )
                total_bytes += member.size
                if total_bytes > MAX_EXTRACT_BYTES:
                    raise FetchError(
                        "too_large",
                        f"selected skills exceed the {MAX_EXTRACT_BYTES // (1024 * 1024)} MiB total cap",
                    )
                f = tf.extractfile(member)
                files[inner] = f.read() if f is not None else b""
    except tarfile.TarError as exc:
        raise FetchError("bad_archive", f"unreadable repo archive: {exc}") from exc
    return extracted
