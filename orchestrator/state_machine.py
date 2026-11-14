"""Run + sandbox state machines (F2).

Allowed transitions mirror the lifecycle in AGENTS.md / docs/orchestrator-plan.md:

    run:     submitted → rubric_review → [queued →] provisioning → running
             → evaluating → done | failed
    sandbox: provisioning → running → terminating → done | failed

``queued`` (PR15 pacing) parks an approved run while another run holds the
single execution slot — see ``orchestrator/pacing.py``. ``queued → failed``
exists so sweeps / a future cancel can retire parked runs.

``done``/``failed → provisioning`` on BOTH machines is the continuation
re-entry (PR16 "add next prompt"): a finished sandbox respawns a fresh
container on its still-live workspace dir for a follow-up phase, and the run
re-holds the execution slot while it does — see
``orchestrator/continuation.py``.

``transition()`` is the ONLY sanctioned way to change ``status`` — it validates
against the table, mutates the row, and emits an orchestrator ``checkpoint``
event (callers commit the session). Illegal transitions raise
:class:`InvalidTransition`.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from db.models import Run, Sandbox

from .events import emit

RUN_TRANSITIONS: dict[str, frozenset[str]] = {
    "submitted": frozenset({"rubric_review", "failed"}),
    "rubric_review": frozenset({"provisioning", "queued", "failed"}),
    "queued": frozenset({"provisioning", "failed"}),
    "provisioning": frozenset({"running", "failed"}),
    "running": frozenset({"evaluating", "failed"}),
    "evaluating": frozenset({"done", "failed"}),
    # terminal → provisioning: PR16 continuation re-entry only.
    "done": frozenset({"provisioning"}),
    "failed": frozenset({"provisioning"}),
}

SANDBOX_TRANSITIONS: dict[str, frozenset[str]] = {
    "provisioning": frozenset({"running", "terminating", "failed"}),
    "running": frozenset({"terminating", "done", "failed"}),
    "terminating": frozenset({"done", "failed"}),
    # terminal → provisioning: PR16 continuation re-entry only.
    "done": frozenset({"provisioning"}),
    "failed": frozenset({"provisioning"}),
}

RUN_TERMINAL = frozenset({"done", "failed"})
SANDBOX_TERMINAL = frozenset({"done", "failed"})

# Sandbox terminal_reason values that still count as a *successful* outcome
# for artifact/evaluation purposes vs. failure verdicts.
DONE_REASONS = frozenset({"agent_done", "harness_complete", "stuck_loop"})
FAILED_REASONS = frozenset({"timeout", "budget_exceeded", "error"})


class InvalidTransition(Exception):
    """Raised when a status change is not in the allowed-transition table."""


def _table_for(entity: Run | Sandbox) -> dict[str, frozenset[str]]:
    if isinstance(entity, Run):
        return RUN_TRANSITIONS
    if isinstance(entity, Sandbox):
        return SANDBOX_TRANSITIONS
    raise TypeError(f"transition() expects Run or Sandbox, got {type(entity)!r}")


def can_transition(entity: Run | Sandbox, to_status: str) -> bool:
    """True when ``entity.status -> to_status`` is allowed."""
    return to_status in _table_for(entity).get(entity.status, frozenset())


def transition(session: Session, entity: Run | Sandbox, to_status: str) -> Run | Sandbox:
    """Move ``entity`` to ``to_status`` if allowed; emits a checkpoint event.

    Same-status calls are a no-op (idempotent for retried orchestrator steps).
    Does NOT commit — the caller owns the transaction.
    """
    table = _table_for(entity)
    old = entity.status
    if to_status == old:
        return entity
    allowed = table.get(old, frozenset())
    if to_status not in allowed:
        raise InvalidTransition(
            f"{type(entity).__name__} {entity.id}: illegal transition "
            f"{old!r} -> {to_status!r} (allowed: {sorted(allowed)})"
        )
    entity.status = to_status

    if isinstance(entity, Sandbox):
        run_id, sandbox_id = entity.run_id, entity.id
    else:
        run_id, sandbox_id = entity.id, None
    emit(
        session,
        run_id=run_id,
        sandbox_id=sandbox_id,
        event_type="checkpoint",
        payload={
            "stage": "state_transition",
            "entity": type(entity).__name__.lower(),
            "from": old,
            "to": to_status,
        },
    )
    return entity
