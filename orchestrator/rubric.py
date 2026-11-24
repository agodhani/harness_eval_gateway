"""Rubric generation for runs (F2).

Flow (docs/orchestrator-plan.md §2): ``POST /api/runs`` registers a background
task → :func:`draft_rubric` mints a run-level gateway cred (``sandbox_id=None``,
``max_tokens`` = tokens already spent + ``EVAL_TOKEN_BUDGET`` so drafting keeps
its own allowance), calls
``{GATEWAY_URL}/v1/chat/completions``, parses + validates the JSON rubric, and
in ONE transaction inserts a ``draft`` Rubric row, moves the run to
``rubric_review``, and emits orchestrator checkpoint events. Failure marks the
run ``failed`` — resubmit is the recovery (no retry endpoint in MVP).

Generation is paced process-wide: ``_rubric_gen_semaphore``
(``RUBRIC_GEN_CONCURRENCY``, default 1) serializes the provider call across
concurrent submits — the PR15 run queue paces EXECUTION, but drafting fires
at submit time ahead of it. A run that finds the gate taken emits a
``stage="rubric_queued"`` checkpoint and stays ``submitted`` until its turn
(pre-queue: it never consumes an execution slot).

The generation call is fault-tolerant at two levels (live-observed flake
modes: a 429/5xx upstream, thinking-model output truncation, ``generated_by``
emitted as a bare string): a bounded retry loop (``RUBRIC_GEN_RETRIES``
extra attempts, default 2) covers transient HTTP/timeout failures with the
same prompt AND parse/validation failures with the validation error fed
back into the prompt (judge ``prior_error`` pattern); and small lenient
coercions before validation (``generated_by`` non-dict → ``{"source": ...}``,
numeric strings in dimension/criterion number fields). Structural problems
— duplicate dimensions after normalization — still hard-fail.

PR23 (F26) unlocked the dimension set: ``dimensions`` accepts 1–12 arbitrary
names (snake_case normalized, the canonical alias map stays as a soft
normalizer, deduplicated after normalization). The four canonical names
remain the DEFAULT the generation prompt requests for freeform coding tasks;
a submitted ``rubric_hint``/preset category steers non-code tasks toward
fitting dimensions, and a ``rubric_template`` (vendored ``rubric_templates/
*.json`` or a user ``rubric_templates`` row) supplies the rubric basis the
generator refines instead of drafting freeform.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from db.models import Rubric, Run
from gateway.creds import mint_cred
from gateway.metering import tokens_used
from scoring.checkers import SUITE_PASS_DIMENSION

from . import state_machine
from .events import emit

if TYPE_CHECKING:  # avoid a runtime api.* import cycle; settings is duck-typed
    from api.config import Settings

log = logging.getLogger(__name__)

# Canonical rubric dimensions — the DEFAULT set the generation prompt asks
# for on freeform coding tasks (the ``code-default`` rubric template). Since
# PR23 (F26) a rubric may carry 1–12 arbitrary dimensions; these names stay
# special only as the code-task default, the alias map's targets, and the
# results UI's sort anchor (canonical-first ordering in web/lib/api.ts).
CANONICAL_DIMENSIONS = (
    "functional_requirements",
    "acceptance_criteria",
    "code_quality",
    "cyclomatic_complexity",
)

#: ``RubricContent.dimensions`` count bounds (PR23).
MIN_DIMENSIONS = 1
MAX_DIMENSIONS = 12
#: ``RubricDimension.name`` bound (normalized form) — the name keys
#: ``scores.dimension`` (String(128)) and the eval parser truncates longer
#: judge-emitted names, so a longer rubric name would silently lose its score.
MAX_DIMENSION_NAME = 128

#: Normalized names a dimension can never take: ``overall`` and
#: ``suite_pass`` are reserved pseudo-rows in ``scores.dimension`` (the
#: results payload routes them to the headline / deterministic-checker
#: slots), and ``scores``/``summary``/``total`` are the eval parser's
#: flat-map exclusions — a real dimension under one of these names loses
#: its score or masquerades as another field.
RESERVED_DIMENSION_NAMES = frozenset(
    {"overall", "scores", "summary", "total", SUITE_PASS_DIMENSION}
)

# LLM-friendly aliases normalized onto the canonical names before validation.
# SOFT normalization only — arbitrary dimension names pass through untouched;
# the aliases just fold common LLM re-phrasings onto the canonical spellings.
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
    """One scored axis — ``name`` is the stable key scores are stored under.

    PR23: arbitrary names are allowed (1–12 dimensions per rubric); the name
    normalizes to snake_case and folds through the canonical alias map only
    when it matches a known alias — a soft normalizer, never a validator.
    """

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


def _normalized_dimension_name(value: str) -> str:
    """Soft-normalize a dimension/criterion reference.

    Folds whitespace/punctuation/case variants to snake_case and maps known
    aliases onto the canonical spellings; anything else passes through.
    Raises ``ValueError`` when nothing usable remains — a name made purely
    of punctuation would key scores under ``""``.
    """
    normalized = _canonical_dimension(value)
    if not normalized:
        raise ValueError(f"dimension name {value!r} normalizes to empty")
    if len(normalized) > MAX_DIMENSION_NAME:
        raise ValueError(
            f"dimension name {value!r} normalizes to {len(normalized)} chars "
            f"(max {MAX_DIMENSION_NAME})"
        )
    return normalized


class RubricContent(BaseModel):
    """Validated rubric JSON — what gets frozen into ``rubrics.content``.

    ``extra="allow"`` keeps LLM-added top-level fields. ``dimensions``
    accepts 1–12 entries (PR23): each name soft-normalizes (snake_case +
    canonical aliases) and the normalized set must be unique — duplicates
    after normalization are the one structural rejection.
    """

    model_config = ConfigDict(extra="allow")

    summary: str = ""
    dimensions: list[RubricDimension] = Field(
        min_length=MIN_DIMENSIONS, max_length=MAX_DIMENSIONS
    )
    criteria: list[RubricCriterion] = Field(default_factory=list)
    generated_by: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _normalize_dimensions(self) -> RubricContent:
        seen: list[str] = []
        for dim in self.dimensions:
            canonical = _normalized_dimension_name(dim.name)
            if canonical in RESERVED_DIMENSION_NAMES:
                raise ValueError(
                    f"dimension name {canonical!r} is a reserved score field"
                )
            dim.name = canonical
            seen.append(canonical)
        names = set(seen)
        if len(seen) != len(names):
            dupes = sorted({n for n in seen if seen.count(n) > 1})
            raise ValueError(f"duplicate dimensions after normalization: {dupes}")
        for criterion in self.criteria:
            # Criteria reference dimensions by name — soft-normalize the same
            # way so "Accuracy" binds a dim normalized to ``accuracy``. An
            # unusable reference (punctuation only) is evidence text, not
            # structure — keep it verbatim rather than rejecting the rubric.
            if _canonical_dimension(criterion.dimension):
                criterion.dimension = _normalized_dimension_name(
                    criterion.dimension
                )
        return self


class RubricParseError(ValueError):
    """Raised when LLM output can't be massaged into RubricContent."""


#: Extra attempts beyond the first generation call (``RUBRIC_GEN_RETRIES``
#: env override) — covers transient upstream failures (429/5xx, timeouts)
#: and parse/validation failures alike. Total attempts = 1 + retries.
DEFAULT_GEN_RETRIES = 2
#: Concurrent rubric generations allowed process-wide
#: (``RUBRIC_GEN_CONCURRENCY`` env override) — 1 = serialized. Drafting is
#: the pipeline's one pre-queue provider call: parallel submits used to
#: thrash a single-model upstream (live-observed model-swap + ~2h read
#: timeouts, every run failed), so the generation phase paces itself
#: instead of relying on the PR15 execution queue it precedes.
DEFAULT_GEN_CONCURRENCY = 1
#: Per-call HTTP timeout for the draft generation request
#: (``RUBRIC_GEN_TIMEOUT_SECONDS`` env override). Must EXCEED the gateway's
#: worst-case silent hold on one call — (1 + UPSTREAM_RETRY_ATTEMPTS) x
#: UPSTREAM_TIMEOUT_SECONDS + total retry backoff (~915s under the shipped
#: defaults) — or a congested draft abandons the call while the gateway is
#: still paying for it upstream, and the retry then overlaps the orphaned
#: job (the bounded version of the pileup the semaphore exists to prevent).
DEFAULT_GEN_TIMEOUT_SECONDS = 960.0
#: Base seconds of the linear backoff between transient-failure retries.
_RETRY_BACKOFF_SECONDS = 1.0
#: Cap on the validation-error detail echoed back into the retry prompt —
#: the same bound the judge's ``prior_error`` feedback uses.
_RETRY_ERROR_CAP = 300


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


#: Number fields a small model may emit as quoted strings — coerced, not
#: rejected (the "small coercions only" half of lenient normalization).
_NUMERIC_FIELDS = ("weight", "min_score", "max_score")


def _normalize_rubric_dict(data: dict[str, Any]) -> dict[str, Any]:
    """Lenient pre-validation coercions on the parsed rubric dict.

    Only shape-preserving fixes for observed small-model quirks — the
    structural contract (1–12 dimensions, unique after normalization) is
    still enforced by :class:`RubricContent` validation:

    - ``generated_by`` emitted as a scalar (live: the string
      ``"rubric_generator"``) wraps as ``{"source": <value>}``; a null
      drops to the field default (it's optional provenance metadata).
    - ``dimensions[]``/``criteria[]`` ``weight``/``min_score``/
      ``max_score`` arriving as numeric strings coerce to float; genuinely
      non-numeric values pass through untouched for validation to reject.
    """
    if "generated_by" in data:
        generated_by = data["generated_by"]
        if generated_by is None:
            del data["generated_by"]
        elif not isinstance(generated_by, dict):
            data["generated_by"] = {"source": str(generated_by)}
    for key in ("dimensions", "criteria"):
        items = data.get(key)
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            for field in _NUMERIC_FIELDS:
                value = item.get(field)
                if isinstance(value, str):
                    try:
                        item[field] = float(value.strip())
                    except ValueError:
                        pass  # leave it — schema validation reports it
    return data


def create_rubric_version(
    session: Session,
    run: Run,
    content: RubricContent,
    *,
    base: Rubric | None = None,
) -> Rubric:
    """Append a post-run rubric version (PR5).

    New row: ``version`` = max+1, ``status="approved"`` (post-run edits are
    frozen on save — no second review gate), ``parent_id`` = ``base`` or the
    latest existing row. Previously ``approved`` rows become ``superseded``
    so exactly one live rubric exists per run; ``_latest_approved_rubric``
    keeps working unchanged. Emits a ``rubric_version_created`` checkpoint.
    The caller owns the commit.
    """
    rows = list(
        session.execute(
            select(Rubric)
            .where(Rubric.run_id == run.id)
            .order_by(Rubric.version.desc())
        )
        .scalars()
        .all()
    )
    if base is None:
        base = rows[0] if rows else None
    version = (rows[0].version + 1) if rows else 1
    superseded: list[int] = []
    for row in rows:
        if row.status == "approved":
            row.status = "superseded"
            superseded.append(row.version)
    rubric = Rubric(
        run_id=run.id,
        version=version,
        parent_id=base.id if base is not None else None,
        status="approved",
        content=content.model_dump(mode="json"),
    )
    session.add(rubric)
    session.flush()  # populate rubric.id for the event payload + response
    emit(
        session,
        run_id=run.id,
        event_type="checkpoint",
        payload={
            "stage": "rubric_version_created",
            "rubric_id": str(rubric.id),
            "version": version,
            "parent_version": base.version if base is not None else None,
            "superseded": superseded,
        },
    )
    return rubric


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

_SYSTEM_BASE = (
    "You design evaluation rubrics for agent benchmarks — the task may be a "
    "coding task or any other AI-workload task (extraction, summarisation, "
    "support replies, ...). Given a task prompt, output ONLY a JSON rubric — "
    "no prose, no markdown fences.\n\n"
    "Required top-level shape:\n"
    '{"summary": "<one-paragraph rubric summary>", '
    '"dimensions": [{"name", "title", "description", "weight", '
    '"min_score", "max_score"}], '
    '"criteria": [{"id", "text", "dimension", "weight"}], '
    '"generated_by": {}}\n\n'
)

_SYSTEM_DEFAULT = (
    "The `dimensions` array must contain EXACTLY these four entries, one per "
    "canonical name (use the names verbatim):\n"
    + _DIMENSION_DOC
    + "\nEach dimension scores on a 0-10 scale (min_score=0.0, max_score=10.0, "
    "weight=1.0).\n"
    "`criteria` decomposes the task prompt's concrete requirements into "
    "checkable items; each criterion's `dimension` must be one of the rubric's "
    "dimension names and `id` a short slug (e.g. \"creates-hello-txt\")."
)

_SYSTEM_HINTED = (
    "The `dimensions` array names the scored axes — between "
    f"{MIN_DIMENSIONS} and {MAX_DIMENSIONS} entries, snake_case `name` keys, "
    "each with its own min_score/max_score scale and weight. Choose names "
    "that fit the task's domain (see the task domain hint below): for "
    "software-engineering submissions the canonical default is\n"
    + _DIMENSION_DOC
    + "\nFor other domains pick fitting dimensions instead (e.g. `accuracy`, "
    "`completeness`, `tone`, `format_compliance`) — do not force code "
    "dimensions onto a non-code task.\n"
    "`criteria` decomposes the task prompt's concrete requirements into "
    "checkable items; each criterion's `dimension` must be one of the rubric's "
    "dimension names and `id` a short slug (e.g. \"extracts-total\")."
)

_SYSTEM_TEMPLATE = (
    "A rubric template is provided as the generation basis. Keep its "
    "dimension `name` keys and overall scale conventions, and adapt titles, "
    "descriptions, weights, scales and criteria to THIS task — the template "
    "is the starting point, not the verdict. Add or drop a dimension only "
    "when the task clearly demands it (never below 1 or above "
    f"{MAX_DIMENSIONS} dimensions, snake_case `name` keys).\n"
    "`criteria` decomposes the task prompt's concrete requirements into "
    "checkable items; each criterion's `dimension` must be one of the rubric's "
    "dimension names and `id` a short slug (e.g. \"extracts-total\")."
)


def _system_prompt(*, rubric_basis: dict[str, Any] | None, domain_hint: str | None) -> str:
    """System prompt for the draft call — the dimension contract varies with
    the generation basis (PR23):

    - a template basis → refine it, keep its dimension names;
    - a domain hint → pick 1–12 fitting names (canonical four offered as the
      code-task default only);
    - neither → the historical MVP contract: exactly the four canonical
      dimensions.
    """
    if rubric_basis is not None:
        return _SYSTEM_BASE + _SYSTEM_TEMPLATE
    if domain_hint:
        return _SYSTEM_BASE + _SYSTEM_HINTED
    return _SYSTEM_BASE + _SYSTEM_DEFAULT


# Back-compat alias — the historical prompt (no basis/hint) is what tests
# and external references assert on.
SYSTEM_PROMPT = _SYSTEM_BASE + _SYSTEM_DEFAULT


def _user_prompt(
    task_prompt: str,
    *,
    rubric_basis: dict[str, Any] | None = None,
    domain_hint: str | None = None,
) -> str:
    """User-turn content: the task plus optional domain hint / template."""
    parts = [f"Task prompt:\n{task_prompt}"]
    if domain_hint:
        parts.append(f"Task domain hint: {domain_hint}")
    if rubric_basis is not None:
        basis_json = json.dumps(rubric_basis, ensure_ascii=False, default=str)
        parts.append(f"Rubric template to use as the basis:\n{basis_json}")
    parts.append("Return JSON only.")
    return "\n\n".join(parts)


def _retry_feedback(error: Exception) -> str:
    """User-turn content for a parse/validation retry — the error echoes
    back so the model sees exactly which contract its output broke (the
    same ``prior_error`` feedback the eval judge's retry carries)."""
    detail = f"{type(error).__name__}: {error}"[:_RETRY_ERROR_CAP]
    return (
        f"The previous output failed validation: {detail}\n"
        "Emit only valid JSON matching the schema in your instructions — "
        "no prose, no code fences."
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

    Tolerates ``` fences and leading/trailing prose, plus the small
    coercions in :func:`_normalize_rubric_dict`. Raises
    :class:`RubricParseError` (no JSON) or pydantic ``ValidationError``
    (schema violations — incl. the 1–12 dimension bounds and
    duplicate-after-normalization) — the caller retries on either with the
    failure fed back into the prompt.
    """
    data = json.loads(_extract_json(text))
    if not isinstance(data, dict):
        raise RubricParseError("rubric JSON is not an object")
    return RubricContent.model_validate(_normalize_rubric_dict(data))


# ---------------------------------------------------------------------------
# LLM call
# ---------------------------------------------------------------------------


def _chat_url(gateway_url: str) -> str:
    return f"{gateway_url.rstrip('/')}/v1/chat/completions"


def _is_transient_call_error(exc: Exception) -> bool:
    """Upstream failures worth a same-prompt retry.

    429 + 5xx are capacity/availability failures the upstream may clear;
    ``httpx.TransportError`` covers timeouts (``TimeoutException``) and
    connect/read errors. Other 4xx are deterministic rejections — a retry
    would replay an identically-bad request.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        return status == 429 or status >= 500
    return isinstance(exc, httpx.TransportError)


async def _request_rubric(
    client: httpx.AsyncClient,
    *,
    gateway_url: str,
    cred: str,
    model: str,
    task_prompt: str,
    max_tokens: int,
    retries: int | None = None,
    rubric_basis: dict[str, Any] | None = None,
    domain_hint: str | None = None,
) -> RubricContent:
    """Chat-completions → RubricContent with a bounded retry.

    ``retries`` = extra attempts beyond the first (``None`` → env
    ``RUBRIC_GEN_RETRIES``, default 2 → up to 3 calls total).

    PR23: ``rubric_basis`` (a rubric template's content, from
    ``config.rubric_template``/``config.rubric_basis``) switches generation
    from freeform drafting to refining that template; ``domain_hint`` (a
    submitted ``rubric_hint`` or the resolved preset's category) steers the
    dimension set for non-coding tasks.

    - Parse/validation failures (``ValueError`` — RubricParseError,
      JSONDecodeError, pydantic ValidationError) retry with the failure
      detail fed back into the prompt, judge ``prior_error`` style.
    - Transient call failures (429/5xx, timeouts, transport errors) retry
      the SAME messages after a short linear backoff — a live 429 failed
      the whole run when drafting had no retry budget.
    - Anything else propagates immediately (``draft_rubric`` marks the run
      failed with the final error).
    """
    attempts = 1 + max(
        0,
        retries
        if retries is not None
        else _env_int("RUBRIC_GEN_RETRIES", DEFAULT_GEN_RETRIES),
    )
    messages: list[dict[str, Any]] = [
        {
            "role": "system",
            "content": _system_prompt(
                rubric_basis=rubric_basis, domain_hint=domain_hint
            ),
        },
        {
            "role": "user",
            "content": _user_prompt(
                task_prompt, rubric_basis=rubric_basis, domain_hint=domain_hint
            ),
        },
    ]
    body_base = {
        "model": model,
        "temperature": 0.2,
        # Thinking models spend output tokens on <think> blocks before the
        # JSON — a small cap truncates the document into unparseable JSON.
        "max_tokens": max_tokens,
        "stream": False,
    }
    headers = {"Authorization": f"Bearer {cred}"}

    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            resp = await client.post(
                _chat_url(gateway_url),
                json={**body_base, "messages": messages},
                headers=headers,
            )
            resp.raise_for_status()
            content = _message_content(resp.json())
            return parse_rubric_json(content)
        except ValueError as exc:
            # RubricParseError, JSONDecodeError, and pydantic ValidationError
            # are all ValueErrors — the retry carries the failure detail
            # back so the model can correct the specific violation.
            last_error = exc
            log.warning(
                "rubric generation attempt %d/%d failed validation: %s",
                attempt + 1,
                attempts,
                exc,
            )
            if attempt + 1 < attempts:
                messages = [
                    *messages,
                    {"role": "assistant", "content": "(invalid rubric output)"},
                    {"role": "user", "content": _retry_feedback(exc)},
                ]
                continue
        except Exception as exc:
            if not _is_transient_call_error(exc):
                raise
            last_error = exc
            log.warning(
                "rubric generation attempt %d/%d failed transiently: %s",
                attempt + 1,
                attempts,
                exc,
            )
            if attempt + 1 < attempts:
                await asyncio.sleep(
                    min(_RETRY_BACKOFF_SECONDS * (attempt + 1), 4.0)
                )
                continue
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


def _default_templates_dir() -> str:
    return str(Path(__file__).resolve().parents[1] / "rubric_templates")


def _rubric_context(
    session_factory: sessionmaker[Session],
    run: Run,
    settings: Any,
) -> tuple[dict[str, Any] | None, str | None]:
    """Resolve the generation basis + domain hint from ``run.config`` (PR23).

    ``config.rubric_template`` names a template in the merged store
    (vendored ``rubric_templates/*.json`` + user ``rubric_templates`` rows);
    ``config.rubric_basis`` is an inline rubric fragment (a preset's
    ``suggested_rubric`` carried on the run) — the inline form wins when
    both exist. ``config.rubric_hint`` is the free-text domain hint the
    prompt echoes. Resolution failures degrade to freeform drafting: a
    stored template may have been deleted between submit and draft, which
    must not fail the run.
    """
    config = run.config or {}
    hint = str(config.get("rubric_hint") or "").strip() or None
    basis: dict[str, Any] | None = None
    inline = config.get("rubric_basis")
    if isinstance(inline, dict) and inline.get("dimensions"):
        basis = inline
    elif isinstance(config.get("rubric_template"), str):
        name = str(config["rubric_template"]).strip()
        if name:
            try:
                # Lazy import — api.* must not land in orchestrator's
                # import graph (the router imports this module).
                from api import rubric_templates as template_store

                templates_dir = str(
                    getattr(settings, "RUBRIC_TEMPLATES_DIR", "")
                    or _default_templates_dir()
                )
                with session_factory() as session:
                    template = template_store.resolve(session, templates_dir, name)
                if template is not None:
                    basis = template.content
            except Exception:  # noqa: BLE001 — drafting must not fail here
                log.warning(
                    "draft_rubric: could not resolve rubric_template %r for "
                    "run %s — drafting freeform",
                    name,
                    run.id,
                )
    return basis, hint


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


# ---------------------------------------------------------------------------
# generation pacing — one provider call at a time, process-wide
# ---------------------------------------------------------------------------

_gen_sem: asyncio.Semaphore | None = None
_gen_sem_permits: int | None = None


def _rubric_gen_semaphore() -> asyncio.Semaphore:
    """Process-wide cap on concurrent rubric generations.

    Sized ``RUBRIC_GEN_CONCURRENCY`` (default :data:`DEFAULT_GEN_CONCURRENCY`
    = 1 → serialized). Module-level: one api process owns every
    ``draft_rubric`` task (submit path AND the startup-sweep respawn), so a
    plain ``asyncio.Semaphore`` paces them all. Built lazily — the env is
    read at first use, not import — and rebuilt when the env value changes
    so tests/late overrides get a correctly-sized gate (asyncio semaphores
    can't be resized in place; parked waiters stay on the old object).
    """
    global _gen_sem, _gen_sem_permits
    permits = max(1, _env_int("RUBRIC_GEN_CONCURRENCY", DEFAULT_GEN_CONCURRENCY))
    if _gen_sem is None or permits != _gen_sem_permits:
        _gen_sem = asyncio.Semaphore(permits)
        _gen_sem_permits = permits
    return _gen_sem


def _emit_queued(
    session_factory: sessionmaker[Session], run_id: uuid.UUID
) -> None:
    """``rubric_queued`` checkpoint — the run is parked on the generation
    semaphore, still ``submitted`` (pre-queue; no execution slot taken).

    Purely explanatory (the timeline shows WHY the run is waiting): a
    failure here — e.g. the run was deleted mid-queue — mustn't abort the
    draft, which still proceeds to wait on the semaphore.
    """
    try:
        with session_factory() as session:
            emit(
                session,
                run_id=run_id,
                event_type="checkpoint",
                payload={
                    "stage": "rubric_queued",
                    "detail": (
                        "rubric generation is serialized "
                        "(RUBRIC_GEN_CONCURRENCY) — waiting behind an "
                        "in-flight draft"
                    ),
                },
            )
            session.commit()
    except Exception:  # noqa: BLE001 — observability only, never fatal
        log.warning(
            "draft_rubric: could not emit rubric_queued for run %s", run_id
        )


async def draft_rubric(
    run_id: uuid.UUID,
    *,
    session_factory: sessionmaker[Session],
    settings: Settings,
    client: httpx.AsyncClient | None = None,
) -> None:
    """Generate + persist a draft rubric for ``run_id`` (background task).

    The generation phase (cred mint → gateway call → retries) is serialized
    process-wide by :func:`_rubric_gen_semaphore` — submits arrive together
    but a single-model provider can only serve one draft at a time. A run
    that finds the semaphore taken emits a ``rubric_queued`` checkpoint and
    stays ``submitted`` while it waits; the run-queue slot is untouched
    (this precedes it).

    Never raises — failures mark the run ``failed`` with an orchestrator
    ``error`` event so the API surface stays consistent.
    """
    own_client = client is None
    if client is None:
        client = httpx.AsyncClient(
            timeout=_env_float(
                "RUBRIC_GEN_TIMEOUT_SECONDS", DEFAULT_GEN_TIMEOUT_SECONDS
            )
        )
    try:
        run = _load_run(session_factory, run_id)
        if run is None:
            log.warning("draft_rubric: run %s not found", run_id)
            return

        sem = _rubric_gen_semaphore()
        if sem.locked():
            # Would block → say so before parking. A free semaphore skips
            # the event entirely; retries INSIDE the gate don't re-emit.
            _emit_queued(session_factory, run_id)
        async with sem:
            # Run-level cred: no sandbox_id; max_tokens = tokens already
            # spent (~0 at draft time) + EVAL_TOKEN_BUDGET — an explicit
            # allowance so drafting can't 429 on the shared default budget.
            # TTL covers the call.
            with session_factory() as session:
                used = tokens_used(session, run.id)
            cred = mint_cred(
                run_id=run.id,
                sandbox_id=None,
                secret=settings.GATEWAY_CRED_SECRET,
                ttl_seconds=int(
                    max(
                        3600,
                        _env_float(
                            "RUBRIC_GEN_TIMEOUT_SECONDS",
                            DEFAULT_GEN_TIMEOUT_SECONDS,
                        )
                        * (1 + _env_int("RUBRIC_GEN_RETRIES", DEFAULT_GEN_RETRIES))
                        + 300,
                    )
                ),
                max_tokens=used + int(settings.EVAL_TOKEN_BUDGET),
            )
            model = settings.RUBRIC_MODEL or run.model_name
            # PR23: template basis + domain hint from the run config (the
            # submit path persists rubric_template/rubric_basis/rubric_hint
            # and the preset fallbacks it resolved).
            rubric_basis, domain_hint = _rubric_context(
                session_factory, run, settings
            )
            content = await _request_rubric(
                client,
                gateway_url=settings.GATEWAY_URL,
                cred=cred,
                model=model,
                task_prompt=run.task_prompt,
                max_tokens=int(settings.RUBRIC_MAX_TOKENS),
                rubric_basis=rubric_basis,
                domain_hint=domain_hint,
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
