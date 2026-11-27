"""Rubric generation: JSON parsing, canonical-dimension validation, drafting."""

from __future__ import annotations

import asyncio
import json
import uuid
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import select

from db.models import Event, Rubric, Run
from gateway.creds import verify_cred
from orchestrator import rubric as rubric_mod
from orchestrator.rubric import (
    CANONICAL_DIMENSIONS,
    RubricContent,
    RubricParseError,
    parse_rubric_json,
)

from .conftest import make_run

SETTINGS = SimpleNamespace(
    GATEWAY_URL="http://gateway.test:8080",
    GATEWAY_CRED_SECRET="unit-test-secret",
    RUBRIC_MODEL="",
    EVAL_TOKEN_BUDGET=200_000,
    RUBRIC_MAX_TOKENS=8192,
)


def _valid_rubric_payload() -> dict:
    return {
        "summary": "Test rubric",
        "dimensions": [
            {
                "name": name,
                "title": name.replace("_", " ").title(),
                "description": f"desc {name}",
                "weight": 1.0,
                "min_score": 0.0,
                "max_score": 10.0,
            }
            for name in CANONICAL_DIMENSIONS
        ],
        "criteria": [
            {"id": "c1", "text": "creates hello.txt", "dimension": "functional_requirements"}
        ],
        "generated_by": {},
    }


def _chat_response(content: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        },
    )


def _mock_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# ---------------------------------------------------------------------------
# parse_rubric_json
# ---------------------------------------------------------------------------


def test_parse_bare_json() -> None:
    content = parse_rubric_json(json.dumps(_valid_rubric_payload()))
    assert {d.name for d in content.dimensions} == set(CANONICAL_DIMENSIONS)
    assert content.criteria[0].id == "c1"


def test_parse_fenced_json() -> None:
    text = "```json\n" + json.dumps(_valid_rubric_payload()) + "\n```"
    assert parse_rubric_json(text).summary == "Test rubric"


def test_parse_prose_wrapped() -> None:
    text = "Here is the rubric you asked for:\n" + json.dumps(
        _valid_rubric_payload()
    ) + "\nHope that helps!"
    assert parse_rubric_json(text).dimensions


def test_parse_no_json_raises() -> None:
    with pytest.raises(RubricParseError):
        parse_rubric_json("sorry, I cannot help with that")


def test_parse_invalid_schema_raises_validation_error() -> None:
    payload = _valid_rubric_payload()
    payload["dimensions"] = []  # below the 1-dimension minimum (PR23)
    with pytest.raises(ValidationError):
        parse_rubric_json(json.dumps(payload))


def test_dimension_name_normalization() -> None:
    """LLM variants normalize onto canonical names before validation."""
    payload = _valid_rubric_payload()
    names = ["Functional Requirements", "acceptance criteria", "Code Quality", "Cyclomatic Complexity"]
    for dim, name in zip(payload["dimensions"], names):
        dim["name"] = name
    content = parse_rubric_json(json.dumps(payload))
    assert [d.name for d in content.dimensions] == list(CANONICAL_DIMENSIONS)


def test_dimension_aliases() -> None:
    """The alias map is a soft normalizer — known re-phrasings fold onto the
    canonical spellings (still true now that arbitrary names are allowed)."""
    payload = _valid_rubric_payload()
    aliases = ["functional", "acceptance", "implementation_quality", "complexity"]
    for dim, name in zip(payload["dimensions"], aliases):
        dim["name"] = name
    content = parse_rubric_json(json.dumps(payload))
    assert {d.name for d in content.dimensions} == set(CANONICAL_DIMENSIONS)


def test_extra_dimension_accepted() -> None:
    """PR23: arbitrary dimensions are allowed — a fifth, non-canonical dim
    survives normalization instead of failing validation."""
    payload = _valid_rubric_payload()
    payload["dimensions"].append(
        {"name": "documentation", "description": "extra", "weight": 1.0}
    )
    content = parse_rubric_json(json.dumps(payload))
    assert "documentation" in {d.name for d in content.dimensions}


def test_arbitrary_dimensions_parse() -> None:
    """PR23 verify path — a rubric with NO canonical dimension (accuracy/
    tone/compliance) parses, normalizes names, and keeps its scales."""
    content = parse_rubric_json(
        json.dumps(
            {
                "summary": "support rubric",
                "dimensions": [
                    {
                        "name": "Accuracy!!",
                        "title": "Accuracy",
                        "min_score": 0.0,
                        "max_score": 5.0,
                    },
                    {"name": "tone", "max_score": 10.0},
                    {"name": "policy compliance"},
                ],
            }
        )
    )
    assert [d.name for d in content.dimensions] == [
        "accuracy",
        "tone",
        "policy_compliance",
    ]
    assert content.dimensions[0].max_score == 5.0  # per-dim scale kept
    assert content.dimensions[1].min_score == 0.0


def test_dimension_count_bounds() -> None:
    """1–12 dimensions (PR23): 1 parses, 13 fails."""
    one = _valid_rubric_payload()
    one["dimensions"] = one["dimensions"][:1]
    assert len(parse_rubric_json(json.dumps(one)).dimensions) == 1

    payload = _valid_rubric_payload()
    payload["dimensions"] = [
        {"name": f"dim_{i:02d}"} for i in range(13)
    ]
    with pytest.raises(ValidationError):
        parse_rubric_json(json.dumps(payload))


def test_duplicate_dimension_rejected() -> None:
    payload = _valid_rubric_payload()
    # Two dims normalize to the same name -> a structural rejection.
    payload["dimensions"][1]["name"] = "functional_requirements"
    with pytest.raises(ValidationError, match="duplicate dimensions"):
        parse_rubric_json(json.dumps(payload))


def test_reserved_dimension_names_rejected() -> None:
    """A dim named like a reserved score field would masquerade as the
    ``overall`` headline row, the ``suite_pass`` checker pseudo-row, or a
    flat-map key (``scores``/``summary``/``total``) the eval parser skips —
    normalization rejects every casing."""
    for name in ("overall", "Overall", "suite_pass", "total"):
        payload = _valid_rubric_payload()
        payload["dimensions"][0]["name"] = name
        with pytest.raises(ValidationError, match="reserved score field"):
            parse_rubric_json(json.dumps(payload))


def test_criterion_dimension_normalized() -> None:
    """Criteria bind to dimensions by name — the reference soft-normalizes
    the same way dimension names do (``Accuracy`` → ``accuracy``)."""
    payload = _valid_rubric_payload()
    payload["criteria"] = [{"id": "c1", "text": "x", "dimension": "Functional"}]
    content = parse_rubric_json(json.dumps(payload))
    assert content.criteria[0].dimension == "functional_requirements"


# ---------------------------------------------------------------------------
# lenient normalization — small coercions before validation
# ---------------------------------------------------------------------------


def test_parse_coerces_generated_by_string() -> None:
    """Live-observed gemma4 output: ``generated_by`` emitted as the bare
    STRING ``"rubric_generator"`` — coerced to ``{"source": ...}`` instead
    of failing validation and killing the run."""
    payload = _valid_rubric_payload()
    payload["generated_by"] = "rubric_generator"
    content = parse_rubric_json(json.dumps(payload))
    assert content.generated_by == {"source": "rubric_generator"}


def test_parse_drops_null_generated_by() -> None:
    """``generated_by: null`` falls back to the optional field default."""
    payload = _valid_rubric_payload()
    payload["generated_by"] = None
    assert parse_rubric_json(json.dumps(payload)).generated_by == {}


def test_parse_keeps_generated_by_dict() -> None:
    payload = _valid_rubric_payload()
    payload["generated_by"] = {"model": "gemma4:latest"}
    assert parse_rubric_json(json.dumps(payload)).generated_by == {
        "model": "gemma4:latest"
    }


def test_parse_coerces_numeric_strings() -> None:
    """``weight``/``min_score``/``max_score`` as numeric strings coerce —
    quoted numbers are a common small-model emission."""
    payload = _valid_rubric_payload()
    payload["dimensions"][0]["weight"] = "2.5"
    payload["dimensions"][0]["min_score"] = "1"
    payload["dimensions"][0]["max_score"] = "9"
    payload["criteria"][0]["weight"] = "3"
    content = parse_rubric_json(json.dumps(payload))
    dim = content.dimensions[0]
    assert (dim.weight, dim.min_score, dim.max_score) == (2.5, 1.0, 9.0)
    assert content.criteria[0].weight == 3.0


def test_parse_non_numeric_strings_still_rejected() -> None:
    """Normalization is small coercions only — a genuinely non-numeric
    ``weight`` still fails validation."""
    payload = _valid_rubric_payload()
    payload["dimensions"][0]["weight"] = "heavy"
    with pytest.raises(ValidationError):
        parse_rubric_json(json.dumps(payload))


def test_parse_empty_dimensions_still_rejected() -> None:
    """Structural problems are never normalized away — an empty dimensions
    list can't invent one (and a partial list is now VALID, PR23)."""
    payload = _valid_rubric_payload()
    payload["dimensions"] = payload["dimensions"][:2]
    assert len(parse_rubric_json(json.dumps(payload)).dimensions) == 2
    payload["dimensions"] = []
    with pytest.raises(ValidationError):
        parse_rubric_json(json.dumps(payload))


# ---------------------------------------------------------------------------
# draft_rubric (mocked gateway)
# ---------------------------------------------------------------------------


async def test_draft_rubric_success(session_factory) -> None:
    run = make_run(session_factory, status="submitted", task_prompt="build 2048")
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization", "")
        body = json.loads(request.content)
        seen["body"] = body
        return _chat_response(json.dumps(_valid_rubric_payload()))

    client = _mock_client(handler)
    await rubric_mod.draft_rubric(run.id, session_factory=session_factory, settings=SETTINGS, client=client)

    assert seen["url"] == "http://gateway.test:8080/v1/chat/completions"
    assert seen["auth"].startswith("Bearer heg.")
    # run-level cred: no sandbox_id; max_tokens = spent (0) + EVAL_TOKEN_BUDGET
    cred = seen["auth"].removeprefix("Bearer ")
    claims = verify_cred(cred, secret="unit-test-secret")
    assert claims.run_id == str(run.id)
    assert claims.sandbox_id is None
    assert claims.max_tokens == SETTINGS.EVAL_TOKEN_BUDGET
    assert seen["body"]["model"] == "test-model"  # RUBRIC_MODEL unset -> run model
    assert seen["body"]["stream"] is False
    assert seen["body"]["max_tokens"] == 8192  # RUBRIC_MAX_TOKENS (not 2048)

    with session_factory() as s:
        r = s.get(Run, run.id)
        assert r.status == "rubric_review"
        rubric = s.execute(select(Rubric).where(Rubric.run_id == run.id)).scalar_one()
        assert rubric.status == "draft"
        assert rubric.version == 1
        assert {d["name"] for d in rubric.content["dimensions"]} == set(
            CANONICAL_DIMENSIONS
        )
        assert rubric.content["generated_by"]["model"] == "test-model"
        types = [
            e.type
            for e in s.execute(select(Event).where(Event.run_id == run.id)).scalars()
        ]
        assert "checkpoint" in types


async def test_draft_rubric_uses_rubric_model_override(session_factory) -> None:
    run = make_run(session_factory, status="submitted")
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["model"] = json.loads(request.content)["model"]
        return _chat_response(json.dumps(_valid_rubric_payload()))

    settings = SimpleNamespace(**{**vars(SETTINGS), "RUBRIC_MODEL": "cheap-model"})
    client = _mock_client(handler)
    await rubric_mod.draft_rubric(run.id, session_factory=session_factory, settings=settings, client=client)
    assert seen["model"] == "cheap-model"


async def test_draft_rubric_max_tokens_is_configurable(session_factory) -> None:
    """RUBRIC_MAX_TOKENS flows into the chat call — thinking models need the
    headroom (2048 truncated rubric JSON mid-<think> on qwen3-class)."""
    run = make_run(session_factory, status="submitted")
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["max_tokens"] = json.loads(request.content)["max_tokens"]
        return _chat_response(json.dumps(_valid_rubric_payload()))

    settings = SimpleNamespace(**{**vars(SETTINGS), "RUBRIC_MAX_TOKENS": 16384})
    client = _mock_client(handler)
    await rubric_mod.draft_rubric(
        run.id, session_factory=session_factory, settings=settings, client=client
    )
    assert seen["max_tokens"] == 16384


async def test_draft_rubric_retries_once_on_bad_json(session_factory) -> None:
    run = make_run(session_factory, status="submitted")
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        if len(calls) == 1:
            return _chat_response("not json at all")
        return _chat_response(json.dumps(_valid_rubric_payload()))

    client = _mock_client(handler)
    await rubric_mod.draft_rubric(run.id, session_factory=session_factory, settings=SETTINGS, client=client)
    assert len(calls) == 2
    # second call carries the validation feedback (judge prior_error style)
    assert "failed validation" in calls[1]["messages"][-1]["content"]
    with session_factory() as s:
        assert s.get(Run, run.id).status == "rubric_review"


async def test_draft_rubric_retry_feeds_validation_error(session_factory) -> None:
    """The retry prompt carries the actual error — the model sees exactly
    which contract its output broke."""
    run = make_run(session_factory, status="submitted")
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        if len(calls) == 1:
            bad = _valid_rubric_payload()
            bad["dimensions"] = []  # below the 1-dim minimum (PR23)
            return _chat_response(json.dumps(bad))
        return _chat_response(json.dumps(_valid_rubric_payload()))

    client = _mock_client(handler)
    await rubric_mod.draft_rubric(run.id, session_factory=session_factory, settings=SETTINGS, client=client)
    assert len(calls) == 2
    retry_user = calls[1]["messages"][-1]["content"]
    assert "failed validation" in retry_user
    assert "dimensions" in retry_user


async def test_draft_rubric_retries_transient_http_error(session_factory, monkeypatch) -> None:
    """A 429 upstream response earns a same-prompt retry instead of failing
    the run outright (live-observed flake)."""
    monkeypatch.setattr(rubric_mod, "_RETRY_BACKOFF_SECONDS", 0.0)
    run = make_run(session_factory, status="submitted")
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        if len(calls) == 1:
            return httpx.Response(429, json={"error": "rate limited"})
        return _chat_response(json.dumps(_valid_rubric_payload()))

    client = _mock_client(handler)
    await rubric_mod.draft_rubric(run.id, session_factory=session_factory, settings=SETTINGS, client=client)
    assert len(calls) == 2
    with session_factory() as s:
        assert s.get(Run, run.id).status == "rubric_review"


async def test_draft_rubric_retries_5xx_then_succeeds(session_factory, monkeypatch) -> None:
    monkeypatch.setattr(rubric_mod, "_RETRY_BACKOFF_SECONDS", 0.0)
    run = make_run(session_factory, status="submitted")
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        if len(calls) == 1:
            return httpx.Response(503, json={"error": "upstream unavailable"})
        return _chat_response(json.dumps(_valid_rubric_payload()))

    client = _mock_client(handler)
    await rubric_mod.draft_rubric(run.id, session_factory=session_factory, settings=SETTINGS, client=client)
    assert len(calls) == 2
    with session_factory() as s:
        assert s.get(Run, run.id).status == "rubric_review"


async def test_draft_rubric_client_error_not_retried(session_factory) -> None:
    """A deterministic 4xx (not 429) fails fast — replaying an identically
    bad request only burns budget."""
    run = make_run(session_factory, status="submitted")
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        return httpx.Response(400, json={"error": "bad request"})

    client = _mock_client(handler)
    await rubric_mod.draft_rubric(run.id, session_factory=session_factory, settings=SETTINGS, client=client)
    assert len(calls) == 1
    with session_factory() as s:
        assert s.get(Run, run.id).status == "failed"


async def test_draft_rubric_retry_budget_env(session_factory, monkeypatch) -> None:
    """RUBRIC_GEN_RETRIES bounds total attempts: 0 => a single call."""
    monkeypatch.setenv("RUBRIC_GEN_RETRIES", "0")
    run = make_run(session_factory, status="submitted")
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        return _chat_response("not json at all")

    client = _mock_client(handler)
    await rubric_mod.draft_rubric(run.id, session_factory=session_factory, settings=SETTINGS, client=client)
    assert len(calls) == 1
    with session_factory() as s:
        assert s.get(Run, run.id).status == "failed"


async def test_draft_rubric_tolerates_generated_by_string(session_factory) -> None:
    """The live failure end-to-end: generated_by="rubric_generator" used to
    ValidationError the run — lenient normalization saves it."""
    run = make_run(session_factory, status="submitted")
    payload = _valid_rubric_payload()
    payload["generated_by"] = "rubric_generator"
    client = _mock_client(lambda r: _chat_response(json.dumps(payload)))
    await rubric_mod.draft_rubric(run.id, session_factory=session_factory, settings=SETTINGS, client=client)
    with session_factory() as s:
        assert s.get(Run, run.id).status == "rubric_review"
        rubric = s.execute(select(Rubric).where(Rubric.run_id == run.id)).scalar_one()
        assert rubric.content["generated_by"]["source"] == "rubric_generator"
        assert rubric.content["generated_by"]["model"] == "test-model"


async def test_draft_rubric_failure_marks_run_failed(session_factory, monkeypatch) -> None:
    monkeypatch.setattr(rubric_mod, "_RETRY_BACKOFF_SECONDS", 0.0)
    run = make_run(session_factory, status="submitted")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    client = _mock_client(handler)
    await rubric_mod.draft_rubric(run.id, session_factory=session_factory, settings=SETTINGS, client=client)
    with session_factory() as s:
        r = s.get(Run, run.id)
        assert r.status == "failed"
        assert r.finished_at is not None
        errors = [
            e
            for e in s.execute(select(Event).where(Event.run_id == run.id)).scalars()
            if e.type == "error"
        ]
        assert errors and errors[0].source == "orchestrator"
        assert errors[0].payload["stage"] == "rubric_generation"


async def test_draft_rubric_failure_saves_capped_gateway_body(
    session_factory, monkeypatch
) -> None:
    monkeypatch.setattr(rubric_mod, "_RETRY_BACKOFF_SECONDS", 0.0)
    run = make_run(session_factory, status="submitted")
    body = "model 'nope:1b' not found " + "x" * 2000

    client = _mock_client(lambda r: httpx.Response(404, text=body))
    await rubric_mod.draft_rubric(run.id, session_factory=session_factory, settings=SETTINGS, client=client)
    with session_factory() as s:
        errors = [
            e
            for e in s.execute(select(Event).where(Event.run_id == run.id)).scalars()
            if e.type == "error"
        ]
        message = errors[0].payload["message"]
        assert "gateway returned 404: model 'nope:1b' not found" in message
        assert len(message) < 600


async def test_draft_rubric_double_failure_marks_failed(session_factory) -> None:
    run = make_run(session_factory, status="submitted")

    def handler(request: httpx.Request) -> httpx.Response:
        return _chat_response("still not json")

    client = _mock_client(handler)
    await rubric_mod.draft_rubric(run.id, session_factory=session_factory, settings=SETTINGS, client=client)
    with session_factory() as s:
        assert s.get(Run, run.id).status == "failed"
        error = s.execute(
            select(Event).where(Event.run_id == run.id, Event.type == "error")
        ).scalar_one()
        assert error.payload["stage"] == "rubric_generation"
        assert "Rubric draft was invalid" in error.payload["message"]
        assert "pin the participant model" in error.payload["message"]


async def test_invalid_draft_recommends_changing_configured_rubric_model(session_factory) -> None:
    run = make_run(session_factory, status="submitted")
    settings = SimpleNamespace(**{**vars(SETTINGS), "RUBRIC_MODEL": "cheap-model"})

    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["model"] == settings.RUBRIC_MODEL
        return _chat_response("still not json")

    async with _mock_client(handler) as client:
        await rubric_mod.draft_rubric(
            run.id, session_factory=session_factory, settings=settings, client=client
        )
    with session_factory() as s:
        assert s.get(Run, run.id).status == "failed"
        error = s.execute(
            select(Event).where(Event.run_id == run.id, Event.type == "error")
        ).scalar_one()
        assert error.payload["message"] == (
            "Rubric draft was invalid. Set RUBRIC_MODEL to a stronger model."
        )


async def test_draft_rubric_missing_run_is_noop(session_factory) -> None:
    client = _mock_client(lambda r: _chat_response("{}"))
    await rubric_mod.draft_rubric(uuid.uuid4(), session_factory=session_factory, settings=SETTINGS, client=client)


# ---------------------------------------------------------------------------
# draft_rubric — generation pacing (RUBRIC_GEN_CONCURRENCY semaphore)
# ---------------------------------------------------------------------------


def _content() -> RubricContent:
    """A valid parsed rubric — the mocked ``_request_rubric`` return."""
    return parse_rubric_json(json.dumps(_valid_rubric_payload()))


def _stages(session_factory, run_id) -> list[str | None]:
    with session_factory() as s:
        return [
            e.payload.get("stage")
            for e in s.execute(
                select(Event).where(Event.run_id == run_id)
            ).scalars()
        ]


async def test_draft_rubric_generations_serialize(session_factory, monkeypatch) -> None:
    """Two concurrent drafts never overlap at the provider: with the default
    concurrency (1) the second waits on the semaphore, emits
    ``rubric_queued``, and stays ``submitted`` — pre-queue, no slot taken.
    FIFO wake order makes the sequence deterministic."""
    run_a = make_run(session_factory, status="submitted", task_prompt="task-a")
    run_b = make_run(session_factory, status="submitted", task_prompt="task-b")
    calls: list[tuple[str, str]] = []
    a_inside = asyncio.Event()
    release_a = asyncio.Event()

    async def fake_request(client, *, task_prompt, **_kw):
        calls.append(("enter", task_prompt))
        if task_prompt == "task-a":
            a_inside.set()
            await release_a.wait()
        calls.append(("exit", task_prompt))
        return _content()

    monkeypatch.setattr(rubric_mod, "_request_rubric", fake_request)

    t_a = asyncio.create_task(
        rubric_mod.draft_rubric(
            run_a.id, session_factory=session_factory, settings=SETTINGS
        )
    )
    await asyncio.wait_for(a_inside.wait(), 5)

    t_b = asyncio.create_task(
        rubric_mod.draft_rubric(
            run_b.id, session_factory=session_factory, settings=SETTINGS
        )
    )
    # B's path to the wait is synchronous (load → locked-check → emit →
    # acquire) — two yields put it parked behind A's permit.
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert calls == [("enter", "task-a")]  # B queued, not inside
    assert "rubric_queued" in _stages(session_factory, run_b.id)
    assert "rubric_queued" not in _stages(session_factory, run_a.id)
    with session_factory() as s:
        assert s.get(Run, run_b.id).status == "submitted"

    release_a.set()
    await asyncio.wait_for(asyncio.gather(t_a, t_b), 5)

    assert calls == [
        ("enter", "task-a"),
        ("exit", "task-a"),
        ("enter", "task-b"),
        ("exit", "task-b"),
    ]
    with session_factory() as s:
        assert s.get(Run, run_a.id).status == "rubric_review"
        assert s.get(Run, run_b.id).status == "rubric_review"
    assert not rubric_mod._rubric_gen_semaphore().locked()


async def test_draft_rubric_semaphore_released_on_success(
    session_factory, monkeypatch
) -> None:
    """A completed draft frees the gate — back-to-back drafts never queue."""
    run_a = make_run(session_factory, status="submitted", task_prompt="task-a")
    run_b = make_run(session_factory, status="submitted", task_prompt="task-b")

    async def fake_request(client, **_kw):
        return _content()

    monkeypatch.setattr(rubric_mod, "_request_rubric", fake_request)
    await rubric_mod.draft_rubric(
        run_a.id, session_factory=session_factory, settings=SETTINGS
    )
    assert not rubric_mod._rubric_gen_semaphore().locked()
    # Would hang forever if the permit leaked.
    await asyncio.wait_for(
        rubric_mod.draft_rubric(
            run_b.id, session_factory=session_factory, settings=SETTINGS
        ),
        timeout=5,
    )
    with session_factory() as s:
        assert s.get(Run, run_a.id).status == "rubric_review"
        assert s.get(Run, run_b.id).status == "rubric_review"
    assert "rubric_queued" not in _stages(session_factory, run_b.id)


async def test_draft_rubric_semaphore_released_on_failure(
    session_factory, monkeypatch
) -> None:
    """A failed draft frees the gate too — the next run isn't stranded
    behind a crashed generation."""
    run_a = make_run(session_factory, status="submitted", task_prompt="task-a")
    run_b = make_run(session_factory, status="submitted", task_prompt="task-b")

    async def fake_request(client, *, task_prompt, **_kw):
        if task_prompt == "task-a":
            raise RuntimeError("provider exploded")
        return _content()

    monkeypatch.setattr(rubric_mod, "_request_rubric", fake_request)
    await rubric_mod.draft_rubric(
        run_a.id, session_factory=session_factory, settings=SETTINGS
    )
    with session_factory() as s:
        assert s.get(Run, run_a.id).status == "failed"
    assert not rubric_mod._rubric_gen_semaphore().locked()
    await asyncio.wait_for(
        rubric_mod.draft_rubric(
            run_b.id, session_factory=session_factory, settings=SETTINGS
        ),
        timeout=5,
    )
    with session_factory() as s:
        assert s.get(Run, run_b.id).status == "rubric_review"


async def test_draft_rubric_concurrency_env_override(
    session_factory, monkeypatch
) -> None:
    """``RUBRIC_GEN_CONCURRENCY=2`` widens the gate: two drafts overlap at
    the provider instead of serializing (env read at use, not import)."""
    monkeypatch.setenv("RUBRIC_GEN_CONCURRENCY", "2")
    run_a = make_run(session_factory, status="submitted", task_prompt="task-a")
    run_b = make_run(session_factory, status="submitted", task_prompt="task-b")
    inside: list[str] = []
    both_inside = asyncio.Event()

    async def fake_request(client, *, task_prompt, **_kw):
        inside.append(task_prompt)
        if len(inside) == 2:
            both_inside.set()
        await both_inside.wait()  # deadlocks here if still serialized
        return _content()

    monkeypatch.setattr(rubric_mod, "_request_rubric", fake_request)
    await asyncio.wait_for(
        asyncio.gather(
            rubric_mod.draft_rubric(
                run_a.id, session_factory=session_factory, settings=SETTINGS
            ),
            rubric_mod.draft_rubric(
                run_b.id, session_factory=session_factory, settings=SETTINGS
            ),
        ),
        timeout=10,
    )
    assert sorted(inside) == ["task-a", "task-b"]
    with session_factory() as s:
        assert s.get(Run, run_a.id).status == "rubric_review"
        assert s.get(Run, run_b.id).status == "rubric_review"


# ---------------------------------------------------------------------------
# draft_rubric — generation call timeout (RUBRIC_GEN_TIMEOUT_SECONDS)
# ---------------------------------------------------------------------------


async def test_draft_rubric_client_timeout_env(session_factory, monkeypatch) -> None:
    """The internally-built draft client takes its per-call timeout from
    ``RUBRIC_GEN_TIMEOUT_SECONDS`` — it must out-wait the gateway's
    worst-case call hold so a congested draft never abandons upstream work
    its retry would then overlap."""
    real_cls = httpx.AsyncClient
    seen: list[dict] = []

    def spy(*args, **kwargs):
        seen.append(kwargs)
        return real_cls(
            transport=httpx.MockTransport(
                lambda r: _chat_response(json.dumps(_valid_rubric_payload()))
            )
        )

    monkeypatch.setattr(httpx, "AsyncClient", spy)

    monkeypatch.delenv("RUBRIC_GEN_TIMEOUT_SECONDS", raising=False)
    run = make_run(session_factory, status="submitted")
    await rubric_mod.draft_rubric(
        run.id, session_factory=session_factory, settings=SETTINGS
    )
    assert seen[-1]["timeout"] == 960.0

    monkeypatch.setenv("RUBRIC_GEN_TIMEOUT_SECONDS", "1200")
    run2 = make_run(session_factory, status="submitted")
    await rubric_mod.draft_rubric(
        run2.id, session_factory=session_factory, settings=SETTINGS
    )
    assert seen[-1]["timeout"] == 1200.0


def test_draft_rubric_timeout_outwaits_gateway_worst_case() -> None:
    """The default must exceed the gateway's worst-case silent hold on one
    call: (1 + UPSTREAM_RETRY_ATTEMPTS) attempts each burning the whole
    UPSTREAM_TIMEOUT_SECONDS, plus the summed exponential retry backoff —
    derived from GatewaySettings' declared defaults so a gateway-side bump
    that breaks the guarantee fails this test."""
    from gateway.config import GatewaySettings

    fields = GatewaySettings.model_fields
    attempts = 1 + int(fields["UPSTREAM_RETRY_ATTEMPTS"].default)
    upstream_timeout = float(fields["UPSTREAM_TIMEOUT_SECONDS"].default)
    backoff_base = float(fields["UPSTREAM_RETRY_BACKOFF_SECONDS"].default)
    worst_case = attempts * upstream_timeout + sum(
        backoff_base * (2**i) for i in range(attempts - 1)
    )
    assert rubric_mod.DEFAULT_GEN_TIMEOUT_SECONDS > worst_case


# ---------------------------------------------------------------------------
# PR23 — template basis + domain hint plumbing
# ---------------------------------------------------------------------------


async def test_draft_rubric_passes_domain_hint(session_factory) -> None:
    """``config.rubric_hint`` lands in the user message as the task domain
    hint — non-coding tasks get fitting dimensions instead of code ones."""
    run = make_run(
        session_factory,
        status="submitted",
        task_prompt="Summarize the attached earnings report",
        config={"rubric_hint": "financial summarisation"},
    )
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return _chat_response(json.dumps(_valid_rubric_payload()))

    client = _mock_client(handler)
    await rubric_mod.draft_rubric(
        run.id, session_factory=session_factory, settings=SETTINGS, client=client
    )
    user_msg = seen["body"]["messages"][1]["content"]
    assert "financial summarisation" in user_msg
    # hinted prompt: dims are chosen for the domain, not locked to canonical
    system_msg = seen["body"]["messages"][0]["content"]
    assert "EXACTLY these four" not in system_msg
    assert "domain" in system_msg


async def test_draft_rubric_template_basis(session_factory) -> None:
    """``config.rubric_template`` resolves a vendored template whose content
    becomes the generation basis embedded in the user message."""
    run = make_run(
        session_factory,
        status="submitted",
        config={"rubric_template": "extraction-accuracy"},
    )
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return _chat_response(json.dumps(_valid_rubric_payload()))

    client = _mock_client(handler)
    await rubric_mod.draft_rubric(
        run.id, session_factory=session_factory, settings=SETTINGS, client=client
    )
    messages = seen["body"]["messages"]
    user_msg = messages[1]["content"]
    assert "extraction_accuracy" in user_msg  # template dims are the basis
    assert "field_coverage" in user_msg
    assert "Rubric template" in user_msg
    assert user_msg.index("Rubric template") < user_msg.index("field_coverage")
    system_msg = messages[0]["content"]
    assert "rubric template" in system_msg.lower()


async def test_draft_rubric_inline_basis(session_factory) -> None:
    """``config.rubric_basis`` (a preset's inline suggested_rubric fragment)
    wins over a template name and feeds the prompt directly."""
    basis = {
        "dimensions": [
            {"name": "accuracy", "min_score": 0.0, "max_score": 10.0},
            {"name": "tone", "min_score": 0.0, "max_score": 10.0},
        ]
    }
    run = make_run(
        session_factory,
        status="submitted",
        config={
            "rubric_basis": basis,
            "rubric_template": "code-default",  # ignored — inline wins
        },
    )
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return _chat_response(json.dumps(_valid_rubric_payload()))

    client = _mock_client(handler)
    await rubric_mod.draft_rubric(
        run.id, session_factory=session_factory, settings=SETTINGS, client=client
    )
    user_msg = seen["body"]["messages"][1]["content"]
    assert '"accuracy"' in user_msg or "accuracy" in user_msg
    assert '"tone"' in user_msg or "tone" in user_msg
    assert "cyclomatic_complexity" not in user_msg  # template name lost


async def test_draft_rubric_unknown_template_degrades(session_factory) -> None:
    """A stale template name (deleted between submit and draft) must not
    fail the run — drafting degrades to freeform."""
    run = make_run(
        session_factory,
        status="submitted",
        config={"rubric_template": "no-such-template"},
    )
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        return _chat_response(json.dumps(_valid_rubric_payload()))

    client = _mock_client(handler)
    await rubric_mod.draft_rubric(
        run.id, session_factory=session_factory, settings=SETTINGS, client=client
    )
    assert len(calls) == 1
    with session_factory() as s:
        assert s.get(Run, run.id).status == "rubric_review"


def test_system_prompt_variants() -> None:
    """No basis/hint → the historical exactly-four contract; hint →
    domain-driven dims; basis → template-refine wording."""
    default = rubric_mod._system_prompt(rubric_basis=None, domain_hint=None)
    assert "EXACTLY these four" in default
    hinted = rubric_mod._system_prompt(
        rubric_basis=None, domain_hint="customer support"
    )
    assert "EXACTLY these four" not in hinted
    assert "domain" in hinted
    templated = rubric_mod._system_prompt(
        rubric_basis={"dimensions": []}, domain_hint=None
    )
    assert "rubric template" in templated.lower()
