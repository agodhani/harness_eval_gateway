"""Rubric generation: JSON parsing, canonical-dimension validation, drafting."""

from __future__ import annotations

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
    RubricParseError,
    parse_rubric_json,
)

from .conftest import make_run

SETTINGS = SimpleNamespace(
    GATEWAY_URL="http://gateway.test:8080",
    GATEWAY_CRED_SECRET="unit-test-secret",
    RUBRIC_MODEL="",
    EVAL_TOKEN_BUDGET=200_000,
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
    payload["dimensions"] = payload["dimensions"][:3]  # missing one canonical
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
    payload = _valid_rubric_payload()
    aliases = ["functional", "acceptance", "implementation_quality", "complexity"]
    for dim, name in zip(payload["dimensions"], aliases):
        dim["name"] = name
    content = parse_rubric_json(json.dumps(payload))
    assert {d.name for d in content.dimensions} == set(CANONICAL_DIMENSIONS)


def test_extra_dimension_rejected() -> None:
    payload = _valid_rubric_payload()
    payload["dimensions"].append(
        {"name": "documentation", "description": "extra", "weight": 1.0}
    )
    with pytest.raises(ValidationError, match="unexpected dimensions"):
        parse_rubric_json(json.dumps(payload))


def test_duplicate_dimension_rejected() -> None:
    payload = _valid_rubric_payload()
    # Two dims normalize to the same canonical name -> dup + missing canonical.
    payload["dimensions"][1]["name"] = "functional_requirements"
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
    # second call carries the nudge
    assert "ONLY the JSON" in calls[1]["messages"][-1]["content"]
    with session_factory() as s:
        assert s.get(Run, run.id).status == "rubric_review"


async def test_draft_rubric_failure_marks_run_failed(session_factory) -> None:
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


async def test_draft_rubric_double_failure_marks_failed(session_factory) -> None:
    run = make_run(session_factory, status="submitted")

    def handler(request: httpx.Request) -> httpx.Response:
        return _chat_response("still not json")

    client = _mock_client(handler)
    await rubric_mod.draft_rubric(run.id, session_factory=session_factory, settings=SETTINGS, client=client)
    with session_factory() as s:
        assert s.get(Run, run.id).status == "failed"


async def test_draft_rubric_missing_run_is_noop(session_factory) -> None:
    client = _mock_client(lambda r: _chat_response("{}"))
    await rubric_mod.draft_rubric(uuid.uuid4(), session_factory=session_factory, settings=SETTINGS, client=client)
