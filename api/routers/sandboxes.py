"""F30 — sandbox mutations (``/api/sandboxes/*``).

Sandbox reads live in ``api/routers/results.py``; this router holds the
control-plane write surface — starting with ``extend_budget``, the manual
half of budget resilience. The grant is an append-only ``budget_extended``
checkpoint event: the gateway gate sums these rows into the cred's live
ceilings per request and the monitor's poll into its post-hoc caps, so a
bump takes effect on the next call/poll with no restart.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, model_validator
from sqlalchemy.orm import Session

from api.deps import get_db
from api.schemas import SandboxBudgetOut
from db.models import Sandbox
from gateway import metering
from orchestrator import provision, state_machine
from orchestrator.events import emit

router = APIRouter(prefix="/api/sandboxes", tags=["sandboxes"])

DbSession = Annotated[Session, Depends(get_db)]


class ExtendBudgetIn(BaseModel):
    """``POST .../extend_budget`` body — at least one grant must be set."""

    tokens: int | None = Field(default=None, ge=1_000)
    cost_usd: float | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def _at_least_one(self) -> ExtendBudgetIn:
        if self.tokens is None and self.cost_usd is None:
            raise ValueError("set tokens and/or cost_usd")
        return self


class ExtendBudgetOut(BaseModel):
    sandbox_id: uuid.UUID
    status: str
    added_tokens: int
    added_cost_usd: float
    total_added_tokens: int
    total_added_cost_usd: float
    extensions: int  # grant rows after this one (auto + manual)
    budget: SandboxBudgetOut


@router.post("/{sandbox_id}/extend_budget")
def extend_budget(
    sandbox_id: uuid.UUID, body: ExtendBudgetIn, session: DbSession
) -> ExtendBudgetOut:
    """Bump a live cell's allowance — token grants widen BOTH ceilings the
    cred passes through (run + per-sandbox claims), cost grants widen the
    monitor's ``max_cost_usd`` check."""
    sb = session.get(Sandbox, sandbox_id)
    if sb is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="sandbox not found"
        )
    if sb.status in state_machine.SANDBOX_TERMINAL:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"sandbox is terminal ({sb.status}"
                f"{'/' + sb.terminal_reason if sb.terminal_reason else ''}) — "
                "resubmit with a larger budget instead"
            ),
        )

    prior = metering.budget_state(session, sb.run_id, sandbox_id)
    total_added_tokens = int(prior["added_tokens"]) + int(body.tokens or 0)
    total_added_cost = float(prior["added_cost_usd"]) + float(
        body.cost_usd or 0
    )
    emit(
        session,
        run_id=sb.run_id,
        sandbox_id=sandbox_id,
        event_type="checkpoint",
        payload={
            "stage": "budget_extended",
            "via": "manual",
            "added_tokens": body.tokens,
            "added_cost_usd": body.cost_usd,
            "total_added_tokens": total_added_tokens,
            "total_added_cost_usd": total_added_cost,
        },
    )
    session.commit()

    # Echo the post-grant effective caps so the UI can refresh its badge.
    run = sb.run
    budgets = provision.budgets_for(run)
    token_budget = budgets.get("token_budget")
    sb_cap = budgets.get("sandbox_token_budget")
    max_cost = budgets.get("max_cost_usd")
    usage = metering.usage_by_sandbox(session, sb.run_id)
    run_total = sum(usage.values())
    if sb_cap:
        used = int(usage.get(sandbox_id, 0))
        limit = int(sb_cap) + total_added_tokens
        scope = "sandbox"
    else:
        used = run_total
        limit = int(token_budget or 0) + total_added_tokens
        scope = "run"
    pct = used / limit if limit else 0.0
    return ExtendBudgetOut(
        sandbox_id=sandbox_id,
        status=sb.status,
        added_tokens=int(body.tokens or 0),
        added_cost_usd=float(body.cost_usd or 0),
        total_added_tokens=total_added_tokens,
        total_added_cost_usd=total_added_cost,
        extensions=int(prior["extensions"]) + 1,
        budget=SandboxBudgetOut(
            used_tokens=used,
            limit_tokens=limit,
            scope=scope,
            pct=round(pct, 3),
            warning=bool(prior["warned"])
            or pct >= metering.BUDGET_WARN_FRACTION,
            extended_tokens=total_added_tokens,
            extended_cost_usd=total_added_cost,
            limit_cost_usd=(float(max_cost) + total_added_cost)
            if max_cost
            else None,
        ),
    )
