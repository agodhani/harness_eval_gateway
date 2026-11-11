"""gateway/metering.py — usage summation, budget selection, usage parsing."""

from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from db.models import Event
from gateway import metering
from gateway.config import get_settings
from gateway.creds import CredClaims


def _session() -> Session:
    from gateway.deps import get_session_factory

    return get_session_factory()()


def _add_event(session: Session, run_id: uuid.UUID, type_: str, payload: dict) -> None:
    session.add(
        Event(
            run_id=run_id,
            source="gateway",
            type=type_,
            payload=payload,
        )
    )


def test_tokens_used_sums_llm_response_usage() -> None:
    session = _session()
    run_id = uuid.uuid4()
    _add_event(
        session,
        run_id,
        "llm_response",
        {"usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}},
    )
    _add_event(
        session,
        run_id,
        "llm_response",
        {"usage": {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7}},
    )
    # Non-llm_response events are ignored even if payload looks like usage.
    _add_event(session, run_id, "tool_call", {"usage": {"total_tokens": 999}})
    # llm_response without usage contributes 0.
    _add_event(session, run_id, "llm_response", {"model": "x"})
    # Other runs don't leak in.
    _add_event(
        session,
        uuid.uuid4(),
        "llm_response",
        {"usage": {"total_tokens": 500}},
    )
    session.commit()
    try:
        assert metering.tokens_used(session, run_id) == 22
    finally:
        session.close()


def test_tokens_used_empty_run_is_zero() -> None:
    session = _session()
    try:
        assert metering.tokens_used(session, uuid.uuid4()) == 0
    finally:
        session.close()


def test_token_budget_prefers_cred_max_tokens() -> None:
    settings = get_settings()
    claims = CredClaims(
        run_id=str(uuid.uuid4()), sandbox_id=None, iat=0, exp=1, max_tokens=42
    )
    assert metering.token_budget(claims, settings) == 42


def test_token_budget_falls_back_to_env_default() -> None:
    settings = get_settings()
    claims = CredClaims(run_id=str(uuid.uuid4()), sandbox_id=None, iat=0, exp=1)
    assert metering.token_budget(claims, settings) == settings.DEFAULT_RUN_TOKEN_BUDGET


def test_max_tokens_estimate() -> None:
    assert metering.max_tokens_estimate({"max_tokens": 128}) == 128
    assert metering.max_tokens_estimate({"max_tokens": 0}) == 0
    assert metering.max_tokens_estimate({}) == 0
    assert metering.max_tokens_estimate({"max_tokens": "lots"}) == 0
    assert metering.max_tokens_estimate({"max_tokens": True}) == 0


def test_usage_from_response() -> None:
    assert metering.usage_from_response(
        {"usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}}
    ) == {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}
    # total_tokens falls back to prompt+completion
    assert metering.usage_from_response(
        {"usage": {"prompt_tokens": 4, "completion_tokens": 6}}
    )["total_tokens"] == 10
    # missing/garbage -> zeros
    assert metering.usage_from_response({})["total_tokens"] == 0
    assert metering.usage_from_response(None)["total_tokens"] == 0
    assert metering.usage_from_response({"usage": "nope"})["total_tokens"] == 0
