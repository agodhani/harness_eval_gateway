"""State machine: allowed/illegal transitions + checkpoint events."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from db.models import Event, Sandbox
from orchestrator import state_machine
from orchestrator.state_machine import InvalidTransition, transition

from .conftest import make_run, make_sandbox, run_config


def _checkpoint_events(session_factory, run_id) -> list[Event]:
    with session_factory() as s:
        return list(
            s.execute(
                select(Event).where(Event.run_id == run_id, Event.type == "checkpoint")
            )
            .scalars()
            .all()
        )


# ---------------------------------------------------------------------------
# run transitions
# ---------------------------------------------------------------------------


def test_run_happy_path_transitions(session_factory) -> None:
    run = make_run(session_factory, status="submitted")
    for to_status in ("rubric_review", "provisioning", "running", "evaluating", "done"):
        with session_factory() as s:
            r = s.get(type(run), run.id)
            transition(s, r, to_status)
            s.commit()
    with session_factory() as s:
        assert s.get(type(run), run.id).status == "done"


def test_run_failed_reachable_from_every_pre_terminal(session_factory) -> None:
    for start in (
        "submitted",
        "rubric_review",
        "queued",
        "provisioning",
        "running",
        "evaluating",
    ):
        run = make_run(session_factory, status=start)
        with session_factory() as s:
            r = s.get(type(run), run.id)
            transition(s, r, "failed")
            s.commit()
        with session_factory() as s:
            assert s.get(type(run), run.id).status == "failed"


def test_run_illegal_transition_raises(session_factory) -> None:
    run = make_run(session_factory, status="submitted")
    with session_factory() as s:
        r = s.get(type(run), run.id)
        with pytest.raises(InvalidTransition):
            transition(s, r, "running")  # skips rubric_review/provisioning
        with pytest.raises(InvalidTransition):
            transition(s, r, "done")
        s.rollback()


def test_run_queued_path_transitions(session_factory) -> None:
    """PR15 pacing detour: rubric_review -> queued -> provisioning."""
    run = make_run(session_factory, status="rubric_review")
    with session_factory() as s:
        r = s.get(type(run), run.id)
        transition(s, r, "queued")
        s.commit()
    with session_factory() as s:
        r = s.get(type(run), run.id)
        transition(s, r, "provisioning")
        s.commit()
    assert s.get(type(run), run.id).status == "provisioning"


def test_run_queued_cannot_skip_to_running(session_factory) -> None:
    run = make_run(session_factory, status="queued")
    with session_factory() as s:
        r = s.get(type(run), run.id)
        with pytest.raises(InvalidTransition):
            transition(s, r, "running")
        with pytest.raises(InvalidTransition):
            transition(s, r, "rubric_review")  # no re-entry into review
        s.rollback()


def test_run_terminal_states_are_final(session_factory) -> None:
    for terminal in ("done", "failed"):
        run = make_run(session_factory, status="evaluating")
        with session_factory() as s:
            r = s.get(type(run), run.id)
            transition(s, r, terminal)
            s.commit()
        with session_factory() as s:
            r2 = s.get(type(run), run.id)
            with pytest.raises(InvalidTransition):
                transition(s, r2, "running")
            s.rollback()


def test_transition_same_status_is_noop(session_factory) -> None:
    run = make_run(session_factory, status="running")
    before = len(_checkpoint_events(session_factory, run.id))
    with session_factory() as s:
        r = s.get(type(run), run.id)
        transition(s, r, "running")
        s.commit()
    assert len(_checkpoint_events(session_factory, run.id)) == before


def test_transition_emits_checkpoint_event(session_factory) -> None:
    run = make_run(session_factory, status="submitted")
    with session_factory() as s:
        r = s.get(type(run), run.id)
        transition(s, r, "rubric_review")
        s.commit()
    events = _checkpoint_events(session_factory, run.id)
    assert len(events) == 1
    ev = events[0]
    assert ev.source == "orchestrator"
    assert ev.sandbox_id is None
    assert ev.payload["from"] == "submitted"
    assert ev.payload["to"] == "rubric_review"
    assert ev.payload["entity"] == "run"


# ---------------------------------------------------------------------------
# sandbox transitions
# ---------------------------------------------------------------------------


def test_sandbox_happy_path(session_factory) -> None:
    run = make_run(session_factory, status="running", config=run_config())
    sb = make_sandbox(session_factory, run.id, status="provisioning")
    for to_status in ("running", "terminating", "done"):
        with session_factory() as s:
            row = s.get(Sandbox, sb.id)
            transition(s, row, to_status)
            s.commit()
    with session_factory() as s:
        assert s.get(Sandbox, sb.id).status == "done"


def test_sandbox_failed_reachable(session_factory) -> None:
    run = make_run(session_factory, status="running", config=run_config())
    for start, allowed in (
        ("provisioning", "failed"),
        ("running", "failed"),
        ("terminating", "failed"),
    ):
        sb = make_sandbox(session_factory, run.id, status=start)
        with session_factory() as s:
            row = s.get(Sandbox, sb.id)
            transition(s, row, allowed)
            s.commit()
        with session_factory() as s:
            assert s.get(Sandbox, sb.id).status == "failed"


def test_sandbox_illegal_and_terminal(session_factory) -> None:
    run = make_run(session_factory, status="running", config=run_config())
    sb = make_sandbox(session_factory, run.id, status="done")
    with session_factory() as s:
        row = s.get(Sandbox, sb.id)
        with pytest.raises(InvalidTransition):
            transition(s, row, "running")
        s.rollback()

    sb2 = make_sandbox(session_factory, run.id, status="provisioning")
    with session_factory() as s:
        row = s.get(Sandbox, sb2.id)
        with pytest.raises(InvalidTransition):
            transition(s, row, "done")  # must pass through running/terminating
        s.rollback()


def test_sandbox_transition_event_has_sandbox_id(session_factory) -> None:
    run = make_run(session_factory, status="running", config=run_config())
    sb = make_sandbox(session_factory, run.id, status="provisioning")
    with session_factory() as s:
        row = s.get(Sandbox, sb.id)
        transition(s, row, "running")
        s.commit()
    events = _checkpoint_events(session_factory, run.id)
    assert events[-1].sandbox_id == sb.id
    assert events[-1].payload["entity"] == "sandbox"


def test_can_transition_helper(session_factory) -> None:
    run = make_run(session_factory, status="submitted")
    with session_factory() as s:
        r = s.get(type(run), run.id)
        assert state_machine.can_transition(r, "rubric_review")
        assert not state_machine.can_transition(r, "running")
