"""Rubric generation for runs (F2).

Flow (docs/orchestrator-plan.md §2): ``POST /api/runs`` registers a background
task → :func:`draft_rubric` mints a run-level gateway cred (``sandbox_id=None``,
no ``max_tokens`` so the run budget isn't consumed by drafting), calls
``{GATEWAY_URL}/v1/chat/completions``, parses + validates the JSON rubric, and
in ONE transaction inserts a ``draft`` Rubric row, moves the run to
``rubric_review``, and emits orchestrator checkpoint events. Any failure marks
the run ``failed`` — resubmit is the recovery (no retry endpoint in MVP).

The rubric's ``dimensions`` must cover exactly the four canonical names the
evaluator (F7) scores against; LLM variants are normalized by name-match.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.orm import Session, sessionmaker

from db.models import Rubric, Run
from gateway.creds import mint_cred

from . import state_machine
from .events import emit

if TYPE_CHECKING:  # avoid a runtime api.* import cycle; settings is duck-typed
    from api.config import Settings

log = logging.getLogger(__name__)

# Canonical rubric dimensions — the F7/evaluator contract (AGENTS.md MVP set).
CANONICAL_DIMENSIONS = (
    "functional_requirements",
    "acceptance_criteria",
    "code_quality",
    "cyclomatic_complexity",
)

# LLM-friendly aliases normalized onto the canonical names before validation.
_DIMENSION_ALIASES = {
    "functional": "functional_requirements",
    "functional_req": "functional_requirements",
    "functional_reqs": "functional_requirements",
    "requirements": "functional_requirements",
    "acceptance": "acceptance_criteria",
    "acceptance_tests": "acceptance_criteria",
    "code": "code_quality",
    "implementation": "code_quality",
    "implementation_quality": "code_quality",
    "code_implementation": "code_quality",
    "code_implementation_quality": "code_quality",
    "quality": "code_quality",
    "complexity": "cyclomatic_complexity",
    "cyclomatic": "cyclomatic_complexity",
}

_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


def _normalize_dimension_name(name: str) -> str:
    """``"Code Implementation Quality!!"`` → ``code_implementation_quality``."""
    return _NON_ALNUM_RE.sub("_", name.strip().lower()).strip("_")


def _canonical_dimension(name: str) -> str:
    norm = _normalize_dimension_name(name)
    if norm in CANONICAL_DIMENSIONS:
        return norm
    return _DIMENSION_ALIASES.get(norm, norm)


# ---------------------------------------------------------------------------
# pydantic content models (the frozen rubric JSON shape)
# ---------------------------------------------------------------------------


class RubricDimension(BaseModel):
    name: str
    title: str = ""
    description: str = ""
    weight: float = 1.0
    min_score: float = 0.0
    max_score: float = 10.0


class RubricCriterion(BaseModel):
    """One task-specific checkable requirement decomposed from the prompt."""

    id: str = ""
    text: str
    dimension: str
    weight: float = 1.0


class RubricContent(BaseModel):
    """Validated rubric JSON — what gets frozen into ``rubrics.content``.

    ``extra="allow"`` keeps LLM-added top-level fields; ``dimensions`` is
    strict: after name normalization it must be EXACTLY the four canonical
    dimensions (F7 scores per dimension name).
    """

    model_config = ConfigDict(extra="allow")

    summary: str = ""
    dimensions: list[RubricDimension] = Field(min_length=1)
    criteria: list[RubricCriterion] = Field(default_factory=list)
    generated_by: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _enforce_canonical_dimensions(self) -> RubricContent:
        seen: list[str] = []
        for dim in self.dimensions:
            canonical = _canonical_dimension(dim.name)
            dim.name = canonical
            seen.append(canonical)
        names = set(seen)
        missing = [d for d in CANONICAL_DIMENSIONS if d not in names]
        extra = sorted(names - set(CANONICAL_DIMENSIONS))
        problems = []
        if missing:
            problems.append(f"missing dimensions: {missing}")
        if extra:
            problems.append(f"unexpected dimensions: {extra}")
        if len(seen) != len(names):
            problems.append("duplicate dimensions after normalization")
        if problems:
            raise ValueError("; ".join(problems))
        return self


class RubricParseError(ValueError):
    """Raised when LLM output can't be massaged into RubricContent."""


# ---------------------------------------------------------------------------
# prompt + parsing
# ---------------------------------------------------------------------------

_DIMENSION_DOC = (
    "- `functional_requirements`: does the artifact implement what the task "
    "prompt asked for?\n"
    "- `acceptance_criteria`: do the prompt's stated acceptance checks pass?\n"
    "- `code_quality`: is the implementation clean, readable, and idiomatic?\n"
    "- `cyclomatic_complexity`: is the control flow reasonably simple?\n"
)

SYSTEM_PROMPT = (
    "You design evaluation rubrics for coding-agent benchmarks. Given a task "
    "prompt, output ONLY a JSON rubric — no prose, no markdown fences.\n\n"
    "Required top-level shape:\n"
    '{"summary": "<one-paragraph rubric summary>", '
    '"dimensions": [{"name", "title", "description", "weight", '
    '"min_score", "max_score"}], '
    '"criteria": [{"id", "text", "dimension", "weight"}], '
    '"generated_by": {}}\n\n'
    "The `dimensions` array must contain EXACTLY these four entries, one per "
    "canonical name (use the names verbatim):\n"
    + _DIMENSION_DOC
    + "\nEach dimension scores on a 0-10 scale (min_score=0.0, max_score=10.0, "
    "weight=1.0).\n"
    "`criteria` decomposes the task prompt's concrete requirements into "
    "checkable items; each criterion's `dimension` must be one of the four "
    "canonical names and `id` a short slug (e.g. \"creates-hello-txt\")."
)

_USER_TEMPLATE = "Task prompt:\n{task_prompt}\n\nReturn JSON only."
_RETRY_NUDGE = (
    "Your previous reply was not valid rubric JSON. Respond with ONLY the "
    "JSON object described in your instructions — no prose, no code fences."
)


def _extract_json(text: str) -> str:
    """Best-effort JSON extraction: strip fences/prose, keep first {..last }."""
    stripped = text.strip()
    if stripped.startswith("```"):
        # ```json\n{...}\n``` — drop the fence lines.
        lines = stripped.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise RubricParseError("no JSON object found in model output")
    return stripped[start : end + 1]


def parse_rubric_json(text: str) -> RubricContent:
    """Parse + validate rubric JSON from model output.

    Tolerates ``` fences and leading/trailing prose. Raises
    :class:`RubricParseError` (no JSON) or pydantic ``ValidationError``
    (schema/canonical-dimension violations) — callers retry once on either.
    """
    data = json.loads(_extract_json(text))
    if not isinstance(data, dict):
        raise RubricParseError("rubric JSON is not an object")
    return RubricContent.model_validate(data)


# ---------------------------------------------------------------------------
# LLM call
# ---------------------------------------------------------------------------


def _chat_url(gateway_url: str) -> str:
    return f"{gateway_url.rstrip('/')}/v1/chat/completions"


async def _request_rubric(
    client: httpx.AsyncClient,
    *,
    gateway_url: str,
    cred: str,
    model: str,
    task_prompt: str,
) -> RubricContent:
    """One chat-completions call → RubricContent; one JSON-only retry."""
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": _USER_TEMPLATE.format(task_prompt=task_prompt)},
    ]
    body_base = {
        "model": model,
        "temperature": 0.2,
        "max_tokens": 2048,
        "stream": False,
    }
    headers = {"Authorization": f"Bearer {cred}"}

    last_error: Exception | None = None
    for attempt in range(2):
        resp = await client.post(
            _chat_url(gateway_url),
            json={**body_base, "messages": messages},
            headers=headers,
        )
        resp.raise_for_status()
        try:
            content = _message_content(resp.json())
            return parse_rubric_json(content)
        except ValueError as exc:
            # RubricParseError, JSONDecodeError, and pydantic ValidationError
            # are all ValueErrors — one JSON-only retry covers them.
            last_error = exc
            log.warning("rubric parse attempt %d failed: %s", attempt + 1, exc)
            if attempt == 0:
                messages = [
                    *messages,
                    {"role": "assistant", "content": "(invalid rubric JSON)"},
                    {"role": "user", "content": _RETRY_NUDGE},
                ]
    assert last_error is not None
    raise last_error


def _message_content(resp_json: dict[str, Any]) -> str:
    choices = resp_json.get("choices") or []
    if not choices:
        raise RubricParseError(f"chat response has no choices: {resp_json!r:.200}")
    msg = choices[0].get("message") or {}
    content = msg.get("content")
    if not isinstance(content, str) or not content.strip():
        raise RubricParseError("chat response message content is empty")
    return content


# ---------------------------------------------------------------------------
# orchestration entry point
# ---------------------------------------------------------------------------


def _load_run(session_factory: sessionmaker[Session], run_id: uuid.UUID) -> Run | None:
    with session_factory() as session:
        return session.get(Run, run_id)


def _persist_draft(
    session_factory: sessionmaker[Session],
    run_id: uuid.UUID,
    content: RubricContent,
    *,
    model: str,
) -> None:
    """Single transaction: rubric row + run→rubric_review + events."""
    with session_factory() as session:
        run = session.get(Run, run_id)
        if run is None:
            log.warning("draft_rubric: run %s vanished", run_id)
            return
        if run.status != "submitted":
            # e.g. startup sweep already failed it — don't resurrect.
            log.info(
                "draft_rubric: run %s is %r, not submitted — skipping persist",
                run_id,
                run.status,
            )
            return
        content.generated_by = {
            **(content.generated_by or {}),
            "model": model,
            "generated_at": datetime.now(UTC).isoformat(),
        }
        rubric = Rubric(
            run_id=run.id,
            version=1,
            status="draft",
            content=content.model_dump(mode="json"),
        )
        session.add(rubric)
        state_machine.transition(session, run, "rubric_review")
        emit(
            session,
            run_id=run.id,
            event_type="checkpoint",
            payload={
                "stage": "rubric_drafted",
                "rubric_version": 1,
                "dimensions": [d.name for d in content.dimensions],
                "criteria": len(content.criteria),
            },
        )
        session.commit()


def _mark_failed(
    session_factory: sessionmaker[Session],
    run_id: uuid.UUID,
    message: str,
) -> None:
    with session_factory() as session:
        run = session.get(Run, run_id)
        if run is None:
            return
        try:
            state_machine.transition(session, run, "failed")
        except state_machine.InvalidTransition:
            run.status = "failed"  # already-terminal states can't transition
        run.finished_at = datetime.now(UTC)
        emit(
            session,
            run_id=run.id,
            event_type="error",
            payload={"stage": "rubric_generation", "message": message},
        )
        session.commit()


async def draft_rubric(
    run_id: uuid.UUID,
    *,
    session_factory: sessionmaker[Session],
    settings: Settings,
    client: httpx.AsyncClient | None = None,
) -> None:
    """Generate + persist a draft rubric for ``run_id`` (background task).

    Never raises — failures mark the run ``failed`` with an orchestrator
    ``error`` event so the API surface stays consistent.
    """
    own_client = client is None
    if client is None:
        client = httpx.AsyncClient(timeout=180.0)
    try:
        run = _load_run(session_factory, run_id)
        if run is None:
            log.warning("draft_rubric: run %s not found", run_id)
            return

        # Run-level cred: no sandbox_id, NO max_tokens (would 429 once the
        # participants burn the shared run budget). TTL covers the call.
        cred = mint_cred(
            run_id=run.id,
            sandbox_id=None,
            secret=settings.GATEWAY_CRED_SECRET,
            ttl_seconds=3600,
        )
        model = settings.RUBRIC_MODEL or run.model_name
        content = await _request_rubric(
            client,
            gateway_url=settings.GATEWAY_URL,
            cred=cred,
            model=model,
            task_prompt=run.task_prompt,
        )
        _persist_draft(session_factory, run_id, content, model=model)
        log.info("draft_rubric: run %s -> rubric_review", run_id)
    except Exception as exc:
        log.exception("draft_rubric: run %s failed", run_id)
        message = f"{type(exc).__name__}: {exc}"
        try:
            _mark_failed(session_factory, run_id, message)
        except Exception:
            log.exception("draft_rubric: could not mark run %s failed", run_id)
    finally:
        if own_client:
            await client.aclose()
