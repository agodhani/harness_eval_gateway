"""PR22 — task preset store.

Task presets are reusable task prompts the submit form offers instead of a
freeform prompt. Two sources share ONE slug namespace:

- **vendored** — ``task_presets/<slug>.md`` at the repo root: a YAML-ish
  frontmatter block (``slug`` — which must equal the filename stem —
  ``title``, ``description``, ``category``, ``tags``, optional
  ``suggested_adapter``/``suggested_models``/``suggested_rubric``/``suite``)
  followed by a markdown body that IS the task prompt. Committed + baked
  into the api image like ``skillpacks/``.
- **user** — ``task_presets`` table rows created via
  ``POST /api/presets/{slug}`` ("save as preset" from the submit form or a
  run's task prompt). Only user presets accept PATCH/DELETE; ``revision``
  bumps on every successful PATCH and is snapshotted onto runs at submit.

A run submitted from a preset keeps ``runs.preset_ref`` + the revision it
saw, while ``runs.task_prompt`` still stores the text verbatim — the preset
link is provenance, the stored prompt is the comparison contract.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from adapters.miniloop.skills import parse_frontmatter
from db.models import TaskPreset

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

log = logging.getLogger(__name__)

#: Preset slug contract — lowercase, URL/path-safe, can't be confused with a
#: traversal or a file name (no dots at all). 1–64 chars, must start AND end
#: with a letter/digit.
SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")
_SLUG_STRIP_RE = re.compile(r"[^a-z0-9]+")
_TRAILING_NUM_RE = re.compile(r"^(.*?)-(\d+)$")

_FRONTMATTER_REQUIRED = ("slug", "title", "category")


class PresetError(Exception):
    """Typed store failure; ``kind`` maps to an HTTP status in the router.

    Kinds: ``invalid_slug``, ``invalid_body`` (preset file / request shape
    problems), ``name_conflict`` (slug already taken — ``source`` +
    ``suggested_name`` set for the rename flow), ``vendored_protected``
    (PATCH/DELETE on a vendored preset), ``not_found``.
    """

    def __init__(
        self,
        kind: str,
        detail: str,
        *,
        name: str | None = None,
        source: str | None = None,
        suggested_name: str | None = None,
    ) -> None:
        super().__init__(detail)
        self.kind = kind
        self.detail = detail
        self.name = name
        self.source = source
        self.suggested_name = suggested_name


@dataclass
class Preset:
    """Normalized preset view — vendored file or user row reads identically."""

    slug: str
    title: str
    description: str
    task_prompt: str
    category: str
    tags: list[str] = field(default_factory=list)
    source: str = "user"  # "vendored" | "user"
    revision: int = 1
    suggested_adapter: str | None = None
    suggested_models: list[str] = field(default_factory=list)
    suggested_rubric: dict[str, Any] | None = None
    suite: dict[str, Any] | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


# ---------------------------------------------------------------------------
# slugs
# ---------------------------------------------------------------------------


def validate_slug(slug: str, *, what: str = "preset slug") -> str:
    """Return ``slug`` or raise ``invalid_slug`` — the shared name contract."""
    cleaned = (slug or "").strip().lower()
    if not SLUG_RE.match(cleaned):
        raise PresetError(
            "invalid_slug",
            f"{what} {slug!r} must match {SLUG_RE.pattern} "
            "(lowercase letters, digits and -; can't start or end with -)",
        )
    return cleaned


def slugify(text: str) -> str:
    """Best-effort slug base from a title/prompt — "My Task!" → ``my-task``."""
    return _SLUG_STRIP_RE.sub("-", text.strip().lower()).strip("-")[:64].strip("-")


def suggest_slug(taken: set[str], base: str) -> str:
    """First free slug: ``base`` itself, else ``base-2``, ``base-3``…"""
    if base and base not in taken:
        return base
    m = _TRAILING_NUM_RE.match(base)
    stem, i = (m.group(1), int(m.group(2)) + 1) if m else (base or "preset", 2)
    for _ in range(10_000):
        cand = f"{stem[: max(1, 63 - len(str(i)))]}-{i}"
        if cand not in taken:
            return cand
        i += 1
    raise PresetError("name_conflict", f"no free slug derived from {base!r}")


# ---------------------------------------------------------------------------
# vendored file parsing
# ---------------------------------------------------------------------------


def _scalar(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _list_value(value: Any) -> list[str]:
    """Normalize a frontmatter list: ``[a, b]`` flow style, comma-separated,
    or the block-list string the mini-parser yields ("- a\\n- b")."""
    if value is None:
        return []
    if isinstance(value, list | tuple):
        items = value
    else:
        text = str(value).strip()
        if text.startswith("[") and text.endswith("]"):
            text = text[1:-1]
        parts: list[str] = []
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("- ") or line == "-":
                parts.append(line.lstrip("-").strip())
            else:
                parts.extend(p.strip() for p in line.split(","))
        items = parts
    out: list[str] = []
    for item in items:
        s = str(item).strip().strip("'\"")
        if s and s not in out:
            out.append(s)
    return out


def _object_value(value: Any) -> dict[str, Any] | None:
    """Nested frontmatter objects — the mini-parser yields strings, so a
    vendored file expresses an object as an inline JSON literal."""
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("{"):
            try:
                parsed = json.loads(text)
            except ValueError:
                return None
            if isinstance(parsed, dict):
                return parsed
    return None


def parse_preset_md(text: str, *, filename: str) -> Preset:
    """Parse one ``<slug>.md`` preset file into a vendored ``Preset``.

    Required frontmatter keys: ``slug`` (which must equal ``filename``'s
    ``.md`` stem — catches a file renamed without updating its frontmatter),
    ``title``, ``category``. The markdown body is the verbatim task prompt.
    Raises ``PresetError(kind="invalid_body")`` on any violation — vendored
    files are committed, so a broken file is a repo bug the contract test
    catches.
    """
    meta, body = parse_frontmatter(text)
    prompt = body.strip()
    missing = [k for k in _FRONTMATTER_REQUIRED if not _scalar(meta.get(k))]
    if missing:
        raise PresetError(
            "invalid_body",
            f"{filename}: missing required frontmatter keys: {', '.join(missing)}",
        )
    slug = _scalar(meta["slug"])
    stem = filename.removesuffix(".md")
    if not SLUG_RE.match(slug):
        raise PresetError("invalid_body", f"{filename}: slug {slug!r} fails the slug pattern")
    if stem != slug:
        raise PresetError(
            "invalid_body",
            f"{filename}: slug {slug!r} must equal the filename stem",
        )
    if not prompt:
        raise PresetError("invalid_body", f"{filename}: empty task prompt body")
    return Preset(
        slug=slug,
        title=_scalar(meta["title"]),
        description=_scalar(meta.get("description")),
        task_prompt=prompt,
        category=_scalar(meta["category"]),
        tags=_list_value(meta.get("tags")),
        source="vendored",
        revision=1,
        suggested_adapter=_scalar(meta.get("suggested_adapter")) or None,
        suggested_models=_list_value(meta.get("suggested_models")),
        suggested_rubric=_object_value(meta.get("suggested_rubric")),
        suite=_object_value(meta.get("suite")),
    )


def list_vendored(root: str | Path) -> list[Preset]:
    """Every ``*.md`` under the vendored root, sorted by slug.

    A file that fails to parse is skipped with a warning — one bad file must
    not take down the merged list (the contract test asserts all committed
    files parse, so this is belt-and-suspenders for hand edits).
    """
    root = Path(root)
    out: list[Preset] = []
    if not root.is_dir():
        return out
    for path in sorted(root.glob("*.md")):
        try:
            out.append(
                parse_preset_md(
                    path.read_text(encoding="utf-8", errors="replace"),
                    filename=path.name,
                )
            )
        except PresetError as exc:
            log.warning("skipping unparseable preset file %s: %s", path, exc.detail)
    return sorted(out, key=lambda p: p.slug)


def get_vendored(root: str | Path, slug: str) -> Preset | None:
    """One vendored preset by slug — direct file lookup, no full scan.

    Slug must match the pattern BEFORE touching the filesystem — callers
    hand us unvalidated strings (RunCreate.preset_ref, URL params), and a
    dotted/traversal-looking slug must never reach ``Path(root) / slug``.
    """
    cleaned = (slug or "").strip().lower()
    if not SLUG_RE.match(cleaned):
        return None
    path = Path(root) / f"{cleaned}.md"
    if not path.is_file():
        return None
    try:
        return parse_preset_md(
            path.read_text(encoding="utf-8", errors="replace"),
            filename=path.name,
        )
    except PresetError:
        log.warning("vendored preset file %s fails to parse", path, exc_info=True)
        return None


# ---------------------------------------------------------------------------
# user (DB) rows + merged views
# ---------------------------------------------------------------------------


def user_to_preset(row: TaskPreset) -> Preset:
    return Preset(
        slug=row.slug,
        title=row.title,
        description=row.description or "",
        task_prompt=row.task_prompt,
        category=row.category or "",
        tags=[str(t) for t in (row.tags or [])],
        source=row.source or "user",
        revision=row.revision or 1,
        suggested_adapter=row.suggested_adapter,
        suggested_models=[str(m) for m in (row.suggested_models or [])],
        suggested_rubric=row.suggested_rubric,
        suite=row.suite,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def get_user(session: Session, slug: str) -> TaskPreset | None:
    return session.execute(
        select(TaskPreset).where(TaskPreset.slug == (slug or "").strip().lower())
    ).scalar_one_or_none()


def list_user(session: Session) -> list[Preset]:
    rows = session.execute(select(TaskPreset).order_by(TaskPreset.slug)).scalars().all()
    return [user_to_preset(r) for r in rows]


def list_all(session: Session, vendored_root: str | Path) -> list[Preset]:
    """Merged preset list — vendored first, then user (each slug-sorted).

    One slug namespace: POST rejects user slugs that collide with either
    source, so a vendored/user slug clash should never occur — if it does
    (hand-edited DB), the vendored entry wins the listing.
    """
    out = list_vendored(vendored_root)
    seen = {p.slug for p in out}
    out.extend(p for p in list_user(session) if p.slug not in seen)
    return out


def resolve(session: Session, vendored_root: str | Path, slug: str) -> Preset | None:
    """Resolve ``slug`` to a preset — vendored file first, then user row."""
    vendored = get_vendored(vendored_root, slug)
    if vendored is not None:
        return vendored
    row = get_user(session, slug)
    return user_to_preset(row) if row is not None else None


def taken_slugs(session: Session, vendored_root: str | Path) -> set[str]:
    """Every slug in use — vendored files plus user rows."""
    slugs = {p.slug for p in list_vendored(vendored_root)}
    slugs.update(session.execute(select(TaskPreset.slug)).scalars().all())
    return slugs
