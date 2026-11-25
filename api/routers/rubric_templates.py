"""PR23 — rubric template library (``/api/rubric-templates*``).

Endpoints:

- ``GET /api/rubric-templates`` — merged template list: vendored
  ``rubric_templates/*.json`` files plus user ``rubric_templates`` rows,
  each carrying ``source`` (``vendored`` | ``user``). Powers the submit
  form's template select.
- ``GET /api/rubric-templates/{name}`` — one template, same shape.

Templates are the rubric-generation basis — selecting one at submit
(``RunCreate.rubric_template``) makes ``draft_rubric`` refine its
dimensions/criteria for the task instead of drafting freeform. Unlike
presets there is no in-app write path yet: user rows are created out of
band (the table + namespace contract already match presets', so a future
"save as template" flow only needs its router handlers).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from api import rubric_templates as template_store
from api.config import get_settings
from api.deps import get_db
from api.schemas import RubricTemplateOut

router = APIRouter(prefix="/api/rubric-templates", tags=["rubric-templates"])

DbSession = Annotated[Session, Depends(get_db)]


def _template_out(template: template_store.Template) -> RubricTemplateOut:
    return RubricTemplateOut(
        name=template.name,
        description=template.description,
        content=template.content,
        source=template.source,
        created_at=template.created_at,
    )


@router.get("")
async def list_templates(session: DbSession) -> list[RubricTemplateOut]:
    """Merged template library — vendored files first, then user rows."""
    settings = get_settings()
    templates = template_store.list_all(session, settings.RUBRIC_TEMPLATES_DIR)
    return [_template_out(t) for t in templates]


@router.get("/{name}")
async def get_template(name: str, session: DbSession) -> RubricTemplateOut:
    settings = get_settings()
    template = template_store.resolve(session, settings.RUBRIC_TEMPLATES_DIR, name)
    if template is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"rubric template {name!r} not found",
        )
    return _template_out(template)
