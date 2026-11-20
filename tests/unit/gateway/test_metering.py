"""gateway/metering.py — usage summation, budget selection, usage parsing."""

from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from db.models import Event, Run, Sandbox
from gateway import metering
from gateway.config import get_settings
from gateway.creds import CredClaims


def _session() -> Session:
    from gateway.deps import get_session_factory

    return get_session_factory()()


def _ensure_run(session: Session, run_id: uuid.UUID) -> None:
    """Insert the parent Run row if absent.

    ``events.run_id`` is a real FK — Postgres enforces it (and the api ingest
    endpoint validates the same invariant), so seeded events need a run row.
    The flush is explicit: there is no ORM relationship Run→Event, so the
    unit of work would not otherwise order the run insert before events.
    """
    if session.get(Run, run_id) is None:
        session.add(Run(id=run_id, task_prompt="test", model_name="test-model"))
        session.flush()


def _add_event(session: Session, run_id: uuid.UUID, type_: str, payload: dict) -> None:
    _ensure_run(session, run_id)
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


# ---------------------------------------------------------------------------
# PR28 — per-sandbox usage totals + cred claim
# ---------------------------------------------------------------------------


def _add_sandbox_event(
    session: Session,
    run_id: uuid.UUID,
    sandbox_id: uuid.UUID,
    type_: str,
    payload: dict,
) -> None:
    _ensure_run(session, run_id)
    # events.sandbox_id is a real FK — the parent Sandbox row must exist.
    if session.get(Sandbox, sandbox_id) is None:
        session.add(
            Sandbox(
                id=sandbox_id,
                run_id=run_id,
                harness_config={"adapter": "miniloop"},
                status="running",
            )
        )
        session.flush()
    session.add(
        Event(
            run_id=run_id,
            sandbox_id=sandbox_id,
            source="gateway",
            type=type_,
            payload=payload,
        )
    )


def test_llm_usage_totals_sums_all_token_fields() -> None:
    session = _session()
    run_id = uuid.uuid4()
    usage = {
        "prompt_tokens": 100,
        "completion_tokens": 40,
        "cache_read_input_tokens": 30,
        "total_tokens": 140,
    }
    _add_event(session, run_id, "llm_response", {"usage": usage})
    _add_event(
        session,
        run_id,
        "llm_response",
        {"usage": {"prompt_tokens": 50, "completion_tokens": 10, "total_tokens": 60}},
    )
    session.commit()
    try:
        totals = metering.llm_usage_totals(session, run_id)
        assert totals == {
            "prompt_tokens": 150,
            "completion_tokens": 50,
            "cache_read_tokens": 30,
            "total_tokens": 200,
        }
    finally:
        session.close()


def test_llm_usage_totals_scoped_to_one_sandbox() -> None:
    """The per-cell gate meters only the cred's own sandbox — a sibling's
    usage can't starve it."""
    session = _session()
    run_id = uuid.uuid4()
    sb_a, sb_b = uuid.uuid4(), uuid.uuid4()
    _add_sandbox_event(
        session, run_id, sb_a, "llm_response",
        {"usage": {"prompt_tokens": 60, "completion_tokens": 20, "total_tokens": 80}},
    )
    _add_sandbox_event(
        session, run_id, sb_b, "llm_response",
        {"usage": {"prompt_tokens": 900, "completion_tokens": 100, "total_tokens": 1000}},
    )
    session.commit()
    try:
        a = metering.llm_usage_totals(session, run_id, sandbox_id=sb_a)
        assert a["total_tokens"] == 80
        assert metering.tokens_used(session, run_id, sandbox_id=sb_a) == 80
        # run scope still sees both cells
        assert metering.tokens_used(session, run_id) == 1080
    finally:
        session.close()


def test_sandbox_token_budget_reads_claim() -> None:
    claims = CredClaims(
        run_id=str(uuid.uuid4()),
        sandbox_id=str(uuid.uuid4()),
        iat=0,
        exp=1,
        sandbox_max_tokens=12_000,
    )
    assert metering.sandbox_token_budget(claims) == 12_000
    # service/eval creds mint no claim → exempt from the per-cell gate
    service = CredClaims(run_id=str(uuid.uuid4()), sandbox_id=None, iat=0, exp=1)
    assert metering.sandbox_token_budget(service) is None
