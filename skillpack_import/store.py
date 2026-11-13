"""Pack storage for user-imported skill packs.

Imported packs live under ``data/skillpacks/<pack>/`` — the same data tree
as artifacts + sandbox state (see module docstring in ``__init__`` for the
storage rationale). Layout mirrors the vendored convention::

    data/skillpacks/<pack>/
      PACK.json                 # provenance: source url, repo, ref, sha, imports
      skills/<skill-dir>/
        SKILL.md                # required — frontmatter + instructions
        references/ scripts/ assets/ ...   # sibling files, verbatim

Pack names are restricted to ``[A-Za-z0-9][A-Za-z0-9_-]{0,63}`` — URL- and
filesystem-safe, no dots so ``..`` is impossible by construction (the
resolved-path containment check is defense-in-depth on top). Skill dir
names follow the same rule; the source dir's basename is sanitized and
deduplicated with ``-2``, ``-3``… suffixes on collision.
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from adapters.miniloop.skills import SkillCatalog, parse_frontmatter

_DIR_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_NON_NAME_CHARS = re.compile(r"[^A-Za-z0-9_-]+")


# ---------------------------------------------------------------------------
# errors
# ---------------------------------------------------------------------------


class PackStoreError(Exception):
    """Typed store failure; ``kind`` maps to an HTTP status in the router.

    Kinds: ``invalid_name`` (pack/skill name fails the safety regex),
    ``name_conflict`` (pack name already taken — see ``NameConflictError``),
    ``unknown_selection`` (requested skill dirs missing / no SKILL.md),
    ``not_found`` (delete of an absent pack), ``vendored_protected``
    (delete/merge attempted on a vendored pack), ``empty`` (nothing to
    write).
    """

    def __init__(self, kind: str, detail: str) -> None:
        super().__init__(detail)
        self.kind = kind
        self.detail = detail


class NameConflictError(PackStoreError):
    """``name_conflict`` carrying the structured payload the UI needs for
    its confirm-and-rename flow: the taken ``name``, where it was found
    (``source``: ``"vendored"`` | ``"imported"``), and the suggested next
    free name (``name-2``, ``name-3``… — ``None`` when unknown, e.g. the
    defense-in-depth check inside ``write_pack`` where the imported-name
    set isn't in scope).
    """

    def __init__(
        self,
        name: str,
        *,
        source: str,
        suggested_name: str | None = None,
    ) -> None:
        super().__init__(
            "name_conflict",
            f"a pack named {name!r} already exists ({source})",
        )
        self.name = name
        self.source = source
        self.suggested_name = suggested_name


# ---------------------------------------------------------------------------
# roots + names
# ---------------------------------------------------------------------------


def imported_root(settings: Any) -> Path:
    """Root dir for user-imported packs.

    Explicit ``IMPORTED_SKILLPACKS_DIR`` wins; otherwise derived as
    ``ARTIFACT_ROOT.parent / "skillpacks"`` — i.e. ``./data/skillpacks`` in
    host dev, ``/data/skillpacks`` in compose (bind-mounted, gitignored).
    Works with the SimpleNamespace settings fakes in tests (ARTIFACT_ROOT is
    always present).
    """
    explicit = getattr(settings, "IMPORTED_SKILLPACKS_DIR", None)
    if explicit:
        return Path(explicit)
    return Path(settings.ARTIFACT_ROOT).parent / "skillpacks"


def sanitize_dir_name(name: str, *, what: str = "pack name") -> str:
    """Validate a pack/skill-dir name against the safety regex."""
    cleaned = (name or "").strip()
    if not _DIR_NAME_RE.match(cleaned):
        raise PackStoreError(
            "invalid_name",
            f"{what} {name!r} must match {_DIR_NAME_RE.pattern} "
            "(start with a letter/digit; letters, digits, _ and - only)",
        )
    return cleaned


def _sanitize_skill_dir(source_dir: str, fallback: str) -> str:
    """Map a repo-relative skill dir to a safe local dir name."""
    base = source_dir.rstrip("/").rsplit("/", 1)[-1] or fallback
    cleaned = _NON_NAME_CHARS.sub("-", base).strip("-_")
    if not cleaned:
        cleaned = _NON_NAME_CHARS.sub("-", fallback).strip("-_") or "skill"
    if not _DIR_NAME_RE.match(cleaned):
        cleaned = f"skill-{cleaned}"[:64]
    return cleaned


def _dedupe(existing: set[str], want: str) -> str:
    if want not in existing:
        return want
    i = 2
    while f"{want}-{i}" in existing:
        i += 1
    return f"{want}-{i}"


_TRAILING_NUM_RE = re.compile(r"^(.*?)-(\d+)$")


def suggest_pack_name(taken: set[str], base: str) -> str:
    """First free pack name: ``base`` itself, else ``base-2``, ``base-3``…

    A base already ending in ``-<digits>`` continues from that number
    (``x-9`` taken → ``x-10``). The stem is truncated so results stay
    within the 64-char name budget.
    """
    if base not in taken:
        return base
    m = _TRAILING_NUM_RE.match(base)
    stem, i = (m.group(1), int(m.group(2)) + 1) if m else (base, 2)
    for _ in range(10_000):
        cand = f"{stem[: max(1, 64 - len(str(i)) - 1)]}-{i}"
        if cand not in taken:
            return cand
        i += 1
    raise PackStoreError("name_conflict", f"no free pack name derived from {base!r}")


def existing_pack_names(root: str | Path) -> set[str]:
    """Directory names under a pack root (empty for a missing root)."""
    p = Path(root)
    return {d.name for d in p.iterdir() if d.is_dir()} if p.is_dir() else set()


def pack_name_for_skill(source_dir: str) -> str:
    """Pack name derived from a skill's repo dir — split mode names each
    pack after its skill (``skills/frontend-design`` → ``frontend-design``)."""
    return _sanitize_skill_dir(source_dir, fallback="skill")


# ---------------------------------------------------------------------------
# listing
# ---------------------------------------------------------------------------


@dataclass
class PackSummary:
    """One selectable pack — vendored or imported."""

    name: str
    source: str           # "vendored" | "imported"
    skill_count: int
    skills: list[str] = field(default_factory=list)   # frontmatter names
    origin_url: str | None = None
    imported_at: str | None = None


def _manifest(pack_dir: Path) -> dict[str, Any]:
    try:
        return json.loads((pack_dir / "PACK.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def list_packs(vendored_root: str | Path, imp_root: str | Path) -> list[PackSummary]:
    """All selectable packs — vendored first, then imported (both sorted).

    Skill names/counts come from the same ``SkillCatalog`` the miniloop
    loader uses, so listing matches what sandboxes will actually see.
    On a name collision (shouldn't happen — imports reject vendored names)
    the vendored entry wins.
    """
    out: list[PackSummary] = []
    seen: set[str] = set()
    for root, source in ((Path(vendored_root), "vendored"), (Path(imp_root), "imported")):
        catalog = SkillCatalog.load(root)
        by_pack: dict[str, list[str]] = {}
        for entry in catalog.entries:
            by_pack.setdefault(entry.pack, []).append(entry.name)
        if not root.is_dir():
            continue
        for pack_dir in sorted(p for p in root.iterdir() if p.is_dir()):
            if pack_dir.name in seen:
                continue
            seen.add(pack_dir.name)
            manifest = _manifest(pack_dir)
            names = sorted(by_pack.get(pack_dir.name, []))
            out.append(
                PackSummary(
                    name=pack_dir.name,
                    source=source,
                    skill_count=len(names),
                    skills=names,
                    origin_url=manifest.get("upstream_url"),
                    imported_at=manifest.get("imported_at"),
                )
            )
    return out


# ---------------------------------------------------------------------------
# import write
# ---------------------------------------------------------------------------


@dataclass
class SkillPayload:
    """A selected skill's files, ready to persist."""

    source_dir: str                    # repo-relative dir (selection token)
    files: dict[str, bytes]            # relpath-within-dir -> content


@dataclass
class ImportedSkillResult:
    source_dir: str
    name: str                          # frontmatter name (dir fallback)
    dir: str                           # final dir under <pack>/skills/
    file_count: int
    renamed: bool                      # dir name was deduplicated


@dataclass
class ImportedPackResult:
    pack: str
    source: str = "imported"
    skill_count: int = 0               # total skills in the pack post-write
    imported: list[ImportedSkillResult] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)


def _existing_skill_names(skills_root: Path) -> set[str]:
    """Frontmatter names of skills already in the pack (for dup warnings)."""
    names: set[str] = set()
    if not skills_root.is_dir():
        return names
    for skill_md in skills_root.rglob("SKILL.md"):
        try:
            meta, _ = parse_frontmatter(skill_md.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        names.add(str(meta.get("name") or "").strip() or skill_md.parent.name)
    return names


def _write_files(pack_dir: Path, skill_dir: str, files: dict[str, bytes]) -> int:
    """Write one skill's files under ``<pack>/skills/<skill_dir>/``.

    Returns files written. Paths are pre-sanitized by the extractor
    (``..``/absolute/empty rejected); the resolve-check here is
    defense-in-depth.
    """
    base = (pack_dir / "skills" / skill_dir).resolve()
    if base.parent.parent != pack_dir.resolve():
        raise PackStoreError("invalid_name", f"skill dir {skill_dir!r} escapes pack")
    written = 0
    for rel, content in files.items():
        dest = base / rel
        if not dest.resolve().is_relative_to(base):
            raise PackStoreError(
                "invalid_name", f"file {rel!r} escapes skill dir {skill_dir!r}"
            )
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(content)
        written += 1
    return written


def _update_manifest(pack_dir: Path, entry: dict[str, Any]) -> None:
    """Create/merge ``PACK.json`` — records each import event."""
    manifest = _manifest(pack_dir)
    manifest.setdefault("name", pack_dir.name)
    manifest["source"] = "github-import"
    manifest["upstream_url"] = entry["upstream_url"]
    manifest["repo"] = entry["repo"]
    manifest["ref"] = entry["ref"]
    manifest["commit_sha"] = entry.get("commit_sha")
    manifest["imported_at"] = entry["imported_at"]
    manifest["skill_count"] = sum(
        1 for _ in (pack_dir / "skills").rglob("SKILL.md")
    ) if (pack_dir / "skills").is_dir() else 0
    imports = manifest.setdefault("imports", [])
    imports.append(entry)
    (pack_dir / "PACK.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )


def write_pack(
    imp_root: str | Path,
    pack_name: str,
    payloads: list[SkillPayload],
    *,
    provenance: dict[str, Any],
    vendored_names: set[str] | None = None,
) -> ImportedPackResult:
    """Persist selected skills under ``imp_root/<pack>/skills/<dir>/``.

    Merging: if ``<pack>`` already exists under the imported root the skills
    are APPENDED (that's the "import single skills into an existing pack"
    flow); a name that collides with a *vendored* pack is rejected outright
    so imported packs can never shadow vendored ones — keeping
    ``harness_configs[].skill_packs`` values unambiguous across roots.
    """
    pack_name = sanitize_dir_name(pack_name)
    if vendored_names and pack_name in vendored_names:
        raise NameConflictError(pack_name, source="vendored")
    if not payloads:
        raise PackStoreError("empty", "no skills selected")

    root = Path(imp_root)
    pack_dir = (root / pack_name).resolve()
    if pack_dir.parent != root.resolve():
        raise PackStoreError("invalid_name", f"pack name {pack_name!r} escapes {root}")
    fresh = not pack_dir.exists()
    pack_dir.mkdir(parents=True, exist_ok=True)

    skills_root = pack_dir / "skills"
    existing = {p.name for p in skills_root.iterdir() if p.is_dir()} if skills_root.is_dir() else set()
    seen_names = _existing_skill_names(skills_root)

    result = ImportedPackResult(pack=pack_name)
    try:
        for payload in payloads:
            text = payload.files.get("SKILL.md")
            if text is None:
                raise PackStoreError(
                    "unknown_selection",
                    f"{payload.source_dir!r} has no SKILL.md — not a skill directory",
                )
            meta, _body = parse_frontmatter(text.decode("utf-8", errors="replace"))
            want = _sanitize_skill_dir(payload.source_dir, fallback=pack_name)
            final = _dedupe(existing, want)
            count = _write_files(pack_dir, final, payload.files)
            name = str(meta.get("name") or "").strip() or final
            if name in seen_names:
                result.issues.append(f"duplicate skill name {name!r} in pack")
            seen_names.add(name)
            if not meta.get("description"):
                result.issues.append(f"skill {name!r}: no description in frontmatter")
            result.imported.append(
                ImportedSkillResult(
                    source_dir=payload.source_dir,
                    name=name,
                    dir=final,
                    file_count=count,
                    renamed=(final != want),
                )
            )
            existing.add(final)
    except Exception:
        # All-or-nothing: drop the dirs this call wrote; remove the pack dir
        # entirely when the import created it (no partial packs on failure).
        for item in result.imported:
            shutil.rmtree(skills_root / item.dir, ignore_errors=True)
        if fresh:
            shutil.rmtree(pack_dir, ignore_errors=True)
        raise

    _update_manifest(
        pack_dir,
        {
            "at": datetime.now(UTC).isoformat(),
            "upstream_url": provenance.get("url", ""),
            "repo": provenance.get("repo", ""),
            "ref": provenance.get("ref", ""),
            "commit_sha": provenance.get("commit_sha"),
            "imported_at": datetime.now(UTC).isoformat(),
            "skills": [i.dir for i in result.imported],
        },
    )
    # Recount after write — the pack may have been merged into.
    result.skill_count = sum(1 for _ in skills_root.rglob("SKILL.md")) if skills_root.is_dir() else len(result.imported)
    return result


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------


def delete_pack(imp_root: str | Path, pack_name: str) -> None:
    """Remove an imported pack dir. Vendored packs can't reach this (the
    router checks the vendored root first), so any name here is user data."""
    pack_name = sanitize_dir_name(pack_name)
    root = Path(imp_root)
    pack_dir = (root / pack_name).resolve()
    if pack_dir.parent != root.resolve():
        raise PackStoreError("invalid_name", f"pack name {pack_name!r} escapes {root}")
    if not pack_dir.is_dir():
        raise PackStoreError("not_found", f"no imported pack named {pack_name!r}")
    shutil.rmtree(pack_dir)
