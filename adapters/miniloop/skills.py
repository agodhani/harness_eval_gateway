"""Skill pack discovery and loading for miniloop.

Packs are mounted read-only at ``$SKILLPACKS_ROOT`` (default
``/opt/skillpacks``) inside sandboxes. Layout convention::

    <root>/<pack>/skills/**/SKILL.md        # canonical
    <root>/<pack>/skills/<group>/<name>/SKILL.md   # e.g. mattpocock-skills
    <root>/<pack>/<name>/SKILL.md           # tolerated fallback

Each ``SKILL.md`` starts with a small YAML-ish frontmatter block::

    ---
    name: my-skill
    description: What it does and when to use it.
    ---

Only ``name`` and ``description`` are parsed (stdlib mini-parser — no pyyaml
dependency). Missing frontmatter falls back to the directory name.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_SKILLPACKS_ROOT = "/opt/skillpacks"
_DESCRIPTION_MAX = 240


@dataclass
class SkillEntry:
    """One discovered skill."""

    name: str            # frontmatter name (or dir name fallback)
    pack: str            # top-level pack directory name
    description: str
    path: Path           # absolute path to SKILL.md
    skill_dir: Path      # directory containing SKILL.md
    rel_path: str        # path relative to SKILLPACKS_ROOT, for display

    @property
    def catalog_name(self) -> str:
        return f"{self.pack}:{self.name}"


@dataclass
class SkillCatalog:
    entries: list[SkillEntry] = field(default_factory=list)
    root: Path | None = None

    # -- discovery ----------------------------------------------------------

    @classmethod
    def load(cls, root: str | Path | None = None) -> SkillCatalog:
        root_path = Path(root) if root else Path(
            os.environ.get("SKILLPACKS_ROOT", DEFAULT_SKILLPACKS_ROOT)
        )
        catalog = cls(root=root_path)
        if not root_path.is_dir():
            return catalog

        seen: set[tuple[str, str, str]] = set()
        for pack_dir in sorted(p for p in root_path.iterdir() if p.is_dir()):
            search_root = pack_dir / "skills"
            if not search_root.is_dir():
                search_root = pack_dir
            for skill_md in sorted(search_root.rglob("SKILL.md")):
                if any(part in {".git", "node_modules", "__pycache__"} for part in skill_md.parts):
                    continue
                try:
                    rel = skill_md.relative_to(root_path).as_posix()
                except ValueError:
                    rel = str(skill_md)
                key = (pack_dir.name, rel, "")
                if key in seen:
                    continue
                seen.add(key)
                entry = _entry_from_file(skill_md, root_path, pack_dir.name, rel)
                if entry is not None:
                    catalog.entries.append(entry)
        return catalog

    # -- lookup -------------------------------------------------------------

    def resolve(self, name: str) -> SkillEntry | None:
        """Resolve ``name`` — bare skill name or ``pack:name`` — to an entry."""
        name = name.strip()
        if not name:
            return None
        if ":" in name:
            pack, _, skill = name.partition(":")
            for e in self.entries:
                if e.pack == pack and e.name == skill:
                    return e
            # tolerate "pack:dir-name" when frontmatter name differs
            for e in self.entries:
                if e.pack == pack and e.skill_dir.name == skill:
                    return e
            return None
        matches = [e for e in self.entries if e.name == name]
        if not matches:
            matches = [e for e in self.entries if e.skill_dir.name == name]
        if not matches:
            return None
        # Prefer unique pack-prefixed determinism: sort and take first.
        matches.sort(key=lambda e: (e.pack, e.name))
        return matches[0]

    # -- rendering ----------------------------------------------------------

    def catalog_lines(self) -> list[str]:
        """One ``- name (pack): description — path`` line per skill."""
        lines = []
        for e in sorted(self.entries, key=lambda x: (x.pack, x.name)):
            desc = e.description.replace("\n", " ").strip() or "(no description)"
            if len(desc) > _DESCRIPTION_MAX:
                desc = desc[: _DESCRIPTION_MAX - 1] + "…"
            lines.append(f"- {e.name} ({e.pack}): {desc} — {e.rel_path}")
        return lines


def _entry_from_file(
    skill_md: Path, root: Path, pack: str, rel: str
) -> SkillEntry | None:
    try:
        text = skill_md.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    meta, _body = parse_frontmatter(text)
    name = str(meta.get("name") or skill_md.parent.name).strip() or skill_md.parent.name
    description = str(meta.get("description") or "").strip()
    if not description:
        description = _first_body_line(text)
    return SkillEntry(
        name=name,
        pack=pack,
        description=description,
        path=skill_md,
        skill_dir=skill_md.parent,
        rel_path=rel,
    )


def _first_body_line(text: str) -> str:
    _meta, body = parse_frontmatter(text)
    for line in body.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            return line[:_DESCRIPTION_MAX]
    return ""


# ---------------------------------------------------------------------------
# frontmatter mini-parser (stdlib only)
# ---------------------------------------------------------------------------

_FRONTMATTER_RE = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*\r?\n?", re.DOTALL)


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Parse a leading ``---`` YAML-ish block into ``{key: value}``.

    Handles single-line ``key: value`` pairs and simple ``key: >`` /
    ``key: |`` folded/literal blocks (indented continuation lines joined with
    spaces / newlines). Anything fancier is ignored — SKILL.md frontmatter in
    the wild only needs name/description.
    """
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    block = m.group(1)
    body = text[m.end() :]
    meta: dict[str, Any] = {}
    current_key: str | None = None
    current_lines: list[str] = []
    current_folded = False

    def _flush() -> None:
        nonlocal current_key, current_lines, current_folded
        if current_key is not None:
            joined = (
                " ".join(current_lines)
                if current_folded
                else "\n".join(current_lines)
            ).strip()
            meta[current_key] = _unquote(joined)
        current_key, current_lines, current_folded = None, [], False

    for raw in block.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        if line[:1] in (" ", "\t") and current_key is not None:
            current_lines.append(line.strip())
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        _flush()
        if value in (">", "|", ">-", "|-"):
            current_key = key
            current_folded = value.startswith(">")
            current_lines = []
        else:
            meta[key] = _unquote(value)
    _flush()
    return meta, body


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value
