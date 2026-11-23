"""PR23 — rubric template store (``rubric_templates/*.json`` + user rows).

A rubric template is a named rubric-content draft a submit selects via
``RunCreate.rubric_template`` (or a preset's ``suggested_rubric``): the
orchestrator's rubric generator then REFINES the template's dimensions and
criteria for the submitted task instead of drafting freeform — reusable
domain rubrics for non-coding evals.

Two sources share one name namespace, mirroring the task-preset library
(F24):

- **vendored** — committed ``rubric_templates/<name>.json`` files
  (``code-default``, ``extraction-accuracy``, ``summarisation-fidelity``,
  ``support-tone-compliance``). Immutable at runtime.
- **user** — ``rubric_templates`` table rows (``source="user"``), created
  out of band; ``name`` shares the vendored namespace, ``content`` is a
  RubricContent-shaped dict.

Resolution is vendored-first (same rule as presets): a name found in both
reads as the vendored definition. ``content`` validates through
``orchestrator.rubric.RubricContent`` — the same contract the generated
rubric must satisfy — so a template that can't produce a valid rubric
fails at parse, not at draft time.

Name safety mirrors presets too: ``SLUG_RE`` is checked BEFORE any
filesystem touch so caller-supplied names (``RunCreate.rubric_template``)
can never become traversal paths.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from api.presets import SLUG_RE
from db.models import RubricTemplate
from orchestrator.rubric import RubricContent

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

log = logging.getLogger(__name__)

#: Template names reuse the preset slug contract — lowercase, path-safe,
#: URL-safe (also the submit-form select value).
NAME_RE = SLUG_RE

_REQUIRED_KEYS = ("name", "description", "content")


class TemplateError(Exception):
    """Typed store failure; ``kind`` maps to an HTTP status in the router.

    Kinds: ``invalid_name``, ``invalid_body`` (vendored file shape — a
    committed file that fails is a repo bug the contract test catches),
    ``not_found``.
    """

    def __init__(self, kind: str, detail: str) -> None:
        super().__init__(detail)
        self.kind = kind
        self.detail = detail


@dataclass
class Template:
    """Normalized template view — vendored file or user row reads the same."""

    name: str
    description: str
    content: dict[str, Any]
    source: str = "user"  # "vendored" | "user"
    created_at: datetime | None = None


def validate_name(name: str) -> str:
    """Return the normalized name or raise ``invalid_name``."""
    cleaned = (name or "").strip().lower()
    if not NAME_RE.match(cleaned):
        raise TemplateError(
            "invalid_name",
            f"rubric template name {name!r} must match {NAME_RE.pattern} "
            "(lowercase letters, digits and -; can't start or end with -)",
        )
    return cleaned


def parse_template_json(text: str, *, filename: str) -> Template:
    """Parse one vendored ``<name>.json`` template file.

    Shape: ``{"name", "description", "content": <RubricContent dict>}``.
    ``name`` must match the filename stem (a renamed file can't silently
    change identity) and ``content`` must satisfy ``RubricContent`` — the
    same validator every rubric passes through, so a template that can't
    produce a valid rubric is a committed-file bug, not a draft-time
    surprise. Raises ``TemplateError`` on any violation.
    """
    try:
        doc = json.loads(text)
    except json.JSONDecodeError as exc:
        raise TemplateError("invalid_body", f"{filename}: not valid JSON: {exc}") from exc
    if not isinstance(doc, dict):
        raise TemplateError("invalid_body", f"{filename}: top-level JSON must be an object")
    missing = [k for k in _REQUIRED_KEYS if k not in doc]
    if missing:
        raise TemplateError(
            "invalid_body",
            f"{filename}: missing required keys: {', '.join(missing)}",
        )
    name = validate_name(str(doc["name"]))
    stem = filename.removesuffix(".json")
    if stem != name:
        raise TemplateError(
            "invalid_body",
            f"{filename}: name {name!r} must equal the filename stem",
        )
    content = doc["content"]
    if not isinstance(content, dict):
        raise TemplateError("invalid_body", f"{filename}: content must be an object")
    try:
        # model_dump normalizes (dimension names snake_cased, defaults
        # filled) so the template's basis is already the frozen shape.
        content = RubricContent.model_validate(content).model_dump(mode="json")
    except ValueError as exc:
        raise TemplateError(
            "invalid_body", f"{filename}: content fails rubric validation: {exc}"
        ) from exc
    return Template(
        name=name,
        description=str(doc["description"] or "").strip(),
        content=content,
        source="vendored",
    )


# ---------------------------------------------------------------------------
# vendored files
# ---------------------------------------------------------------------------


def list_vendored(root: str | Path) -> list[Template]:
    """Every ``*.json`` under the vendored root, name-sorted.

    An unparseable file is skipped with a warning — one bad file must not
    take down the library (the contract test asserts every committed file
    parses, so this is belt-and-suspenders for hand edits).
    """
    root = Path(root)
    out: list[Template] = []
    if not root.is_dir():
        return out
    for path in sorted(root.glob("*.json")):
        try:
            out.append(
                parse_template_json(
                    path.read_text(encoding="utf-8", errors="replace"),
                    filename=path.name,
                )
            )
        except TemplateError as exc:
            log.warning("skipping unparseable rubric template %s: %s", path, exc.detail)
    return sorted(out, key=lambda t: t.name)


def get_vendored(root: str | Path, name: str) -> Template | None:
    """One vendored template by name — direct file lookup, no full scan.

    The name validates against ``NAME_RE`` BEFORE touching the filesystem:
    callers hand us unvalidated strings (``RunCreate.rubric_template``) and
    a dotted/traversal-looking name must never reach ``Path(root) / name``.
    """
    try:
        cleaned = validate_name(name)
    except TemplateError:
        return None
    path = Path(root) / f"{cleaned}.json"
    if not path.is_file():
        return None
    try:
        return parse_template_json(
            path.read_text(encoding="utf-8", errors="replace"),
            filename=path.name,
        )
    except TemplateError:
        log.warning("vendored rubric template %s fails to parse", path, exc_info=True)
        return None


# ---------------------------------------------------------------------------
# user (DB) rows + merged views
# ---------------------------------------------------------------------------


def user_to_template(row: RubricTemplate) -> Template:
    return Template(
        name=row.name,
        description=row.description or "",
        content=dict(row.content or {}),
        source=row.source or "user",
        created_at=row.created_at,
    )


def get_user(session: Session, name: str) -> RubricTemplate | None:
    cleaned = (name or "").strip().lower()
    if not cleaned:
        return None
    return session.execute(
        select(RubricTemplate).where(RubricTemplate.name == cleaned)
    ).scalar_one_or_none()


def list_user(session: Session) -> list[Template]:
    rows = (
        session.execute(select(RubricTemplate).order_by(RubricTemplate.name))
        .scalars()
        .all()
    )
    return [user_to_template(r) for r in rows]


def list_all(session: Session, vendored_root: str | Path) -> list[Template]:
    """Merged template list — vendored first, then user (each name-sorted).

    One name namespace: a vendored/user name clash should never occur (a
    vendored file added over an existing user name is the reachable case)
    — when it does, the vendored entry wins the listing, same rule as
    presets.
    """
    out = list_vendored(vendored_root)
    seen = {t.name for t in out}
    out.extend(t for t in list_user(session) if t.name not in seen)
    return out


def resolve(session: Session, vendored_root: str | Path, name: str) -> Template | None:
    """Resolve ``name`` to a template — vendored file first, then user row."""
    vendored = get_vendored(vendored_root, name)
    if vendored is not None:
        return vendored
    row = get_user(session, name)
    return user_to_template(row) if row is not None else None


def taken_names(session: Session, vendored_root: str | Path) -> set[str]:
    """Every name in use — vendored files plus user rows."""
    names = {t.name for t in list_vendored(vendored_root)}
    names.update(session.execute(select(RubricTemplate.name)).scalars().all())
    return names
