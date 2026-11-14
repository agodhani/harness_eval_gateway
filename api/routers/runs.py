"""F1/F2 — run submission, rubric review/approval, run detail (``/api/runs*``).

Thin handlers only: validate → DB → register background tasks via
``orchestrator.runner``. All orchestration (rubric drafting, provisioning,
monitoring, evaluation) lives in ``orchestrator/``.

Handlers are ``async def`` because they call ``asyncio.create_task`` (via
``runner.start_*``) — sync endpoints run in a worker thread with no loop.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from api.config import get_settings
from api.deps import get_db
from api.schemas import (
    RubricOut,
    RunCreate,
    RunOut,
    RunSummary,
    SandboxOut,
)
from db.models import Rubric, Run, Sandbox
from orchestrator import adapters as orch_adapters
from orchestrator import continuation, pacing, provision, runner, state_machine
from orchestrator import events as orch_events
from orchestrator.rubric import RubricContent
from skillpack_import.store import imported_root

router = APIRouter(prefix="/api/runs", tags=["runs"])

DbSession = Annotated[Session, Depends(get_db)]


class RubricUpdate(BaseModel):
    """PUT /api/runs/{id}/rubric body — full replacement of draft content."""

    content: RubricContent


class ContinuationCreate(BaseModel):
    """POST /api/runs/{id}/continuations body — "add next prompt" (PR16)."""

    sandbox_id: uuid.UUID
    prompt: str = Field(min_length=1, max_length=100_000)


# ---------------------------------------------------------------------------
# response assembly
# ---------------------------------------------------------------------------


def _latest_rubric(session: Session, run_id: uuid.UUID) -> Rubric | None:
    return session.execute(
        select(Rubric)
        .where(Rubric.run_id == run_id)
        .order_by(Rubric.version.desc())
        .limit(1)
    ).scalar_one_or_none()


def _run_sandboxes(session: Session, run_id: uuid.UUID) -> list[Sandbox]:
    return list(
        session.execute(
            select(Sandbox)
            .where(Sandbox.run_id == run_id)
            .order_by(Sandbox.started_at.asc().nulls_last(), Sandbox.id.asc())
        )
        .scalars()
        .all()
    )


def _same_model_eval(run: Run) -> bool:
    cfg = run.config or {}
    evaluator = cfg.get("evaluator_model") or run.model_name
    return evaluator == run.model_name


def _run_out(session: Session, run: Run) -> RunOut:
    rubric = _latest_rubric(session, run.id)
    return RunOut(
        id=run.id,
        status=run.status,
        task_prompt=run.task_prompt,
        model_name=run.model_name,
        config=run.config or {},
        same_model_eval=_same_model_eval(run),
        created_at=run.created_at,
        updated_at=run.updated_at,
        started_at=run.started_at,
        finished_at=run.finished_at,
        sandboxes=[SandboxOut.model_validate(s) for s in _run_sandboxes(session, run.id)],
        rubric=RubricOut.model_validate(rubric) if rubric else None,
    )


def _get_run_or_404(session: Session, run_id: uuid.UUID) -> Run:
    run = session.get(Run, run_id)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="run not found")
    return run


# ---------------------------------------------------------------------------
# submit-time validation
# ---------------------------------------------------------------------------


def _validate_harness_configs(body: RunCreate) -> None:
    settings = get_settings()
    known_adapters = set(orch_adapters.adapter_names())
    # Pack names merge the vendored + imported roots into one namespace
    # (imported packs under data/skillpacks — F11).
    known_packs = set(
        provision.known_skillpacks(
            settings.SKILLPACKS_DIR, imported_root(settings)
        )
    )
    problems: list[str] = []
    for i, hc in enumerate(body.harness_configs):
        if hc.adapter not in known_adapters:
            problems.append(
                f"harness_configs[{i}].adapter {hc.adapter!r} not in {sorted(known_adapters)}"
            )
        for pack in hc.skill_packs:
            if pack not in known_packs:
                problems.append(
                    f"harness_configs[{i}].skill_packs entry {pack!r} not in "
                    f"{sorted(known_packs)}"
                )
    if problems:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="; ".join(problems),
        )


# ---------------------------------------------------------------------------
# endpoints
# ---------------------------------------------------------------------------


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_run(body: RunCreate, session: DbSession) -> RunOut:
    """Submit a run: persist the spec, kick off rubric drafting in background."""
    _validate_harness_configs(body)

    evaluator_model = body.evaluator_model or body.model_name
    config = {
        "harness_configs": [hc.model_dump() for hc in body.harness_configs],
        "evaluator_model": evaluator_model,
        "budgets": body.budgets.model_dump(),
        "same_model_eval": evaluator_model == body.model_name,
    }
    run = Run(
        status="submitted",
        task_prompt=body.task_prompt,
        model_name=body.model_name,
        config=config,
    )
    session.add(run)
    session.commit()

    runner.start_rubric_task(run.id)
    return _run_out(session, run)


@router.get("")
async def list_runs(
    session: DbSession,
    run_status: Annotated[str | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[RunSummary]:
    """Runs newest-first; ``?status=`` filters, ``limit``/``offset`` paginate."""
    stmt = select(Run).order_by(Run.created_at.desc()).limit(limit).offset(offset)
    if run_status:
        stmt = stmt.where(Run.status == run_status)
    runs = session.execute(stmt).scalars().all()

    run_ids = [r.id for r in runs]
    counts: dict[uuid.UUID, int] = {}
    if run_ids:
        counts = {
            run_id: count
            for run_id, count in session.execute(
                select(Sandbox.run_id, func.count(Sandbox.id))
                .where(Sandbox.run_id.in_(run_ids))
                .group_by(Sandbox.run_id)
            ).all()
        }
    return [
        RunSummary(
            id=r.id,
            status=r.status,
            task_prompt=r.task_prompt[:200],
            model_name=r.model_name,
            created_at=r.created_at,
            finished_at=r.finished_at,
            sandbox_count=counts.get(r.id, 0),
        )
        for r in runs
    ]


@router.get("/{run_id}")
async def get_run(run_id: uuid.UUID, session: DbSession) -> RunOut:
    return _run_out(session, _get_run_or_404(session, run_id))


@router.get("/{run_id}/rubric")
async def get_rubric(run_id: uuid.UUID, session: DbSession) -> RubricOut:
    """Latest rubric version for the run (draft or approved)."""
    _get_run_or_404(session, run_id)
    rubric = _latest_rubric(session, run_id)
    if rubric is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="no rubric for run"
        )
    return RubricOut.model_validate(rubric)


def _editable_rubric(session: Session, run_id: uuid.UUID) -> tuple[Run, Rubric]:
    """Shared guard for PUT rubric / approve: run in rubric_review AND the
    latest rubric still a draft. Raises 404/409 otherwise."""
    run = _get_run_or_404(session, run_id)
    rubric = _latest_rubric(session, run_id)
    if rubric is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="no rubric for run"
        )
    if run.status != "rubric_review" or rubric.status != "draft":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"rubric not editable: run.status={run.status!r}, "
                f"rubric.status={rubric.status!r}"
            ),
        )
    return run, rubric


@router.put("/{run_id}/rubric")
async def update_rubric(
    run_id: uuid.UUID, body: RubricUpdate, session: DbSession
) -> RubricOut:
    """Replace the draft rubric's content (version stays 1)."""
    _run, rubric = _editable_rubric(session, run_id)
    rubric.content = body.content.model_dump(mode="json")
    session.commit()
    return RubricOut.model_validate(rubric)


@router.post("/{run_id}/approve")
async def approve_run(run_id: uuid.UUID, session: DbSession) -> RunOut:
    """Freeze the rubric and kick off provisioning/lifecycle.

    PR15 pacing: when another run holds the execution slot (or is already
    queued), the approved run parks in ``queued`` instead — promotion to
    ``provisioning`` happens FIFO when the active run finishes
    (``runner._lifecycle_then_promote``) or on the next startup sweep.
    ``started_at`` = approval/queue-entry time, as before.
    """
    run, rubric = _editable_rubric(session, run_id)

    rubric.status = "approved"
    run.started_at = datetime.now(UTC)
    orch_events.emit(
        session,
        run_id=run.id,
        event_type="checkpoint",
        payload={
            "stage": "rubric_approved",
            "rubric_id": str(rubric.id),
            "version": rubric.version,
        },
    )
    placed = pacing.place_run(session, run)
    session.commit()

    if placed == "provisioning":
        runner.start_lifecycle_task(run.id)
    return _run_out(session, run)


# ---------------------------------------------------------------------------
# PR16 — "add next prompt" (multi-turn continuations)
# ---------------------------------------------------------------------------


def _continuation_active_only(session: Session, run: Run) -> bool:
    """True when the run has in-flight sandboxes AND every one of them is
    itself a continuation phase — i.e. the run's active state is
    continuation-driven (not its original lifecycle). An empty active set
    means the run is in its normal lifecycle tail (e.g. evaluating) and
    must NOT admit follow-ups."""
    active = session.execute(
        select(Sandbox).where(
            Sandbox.run_id == run.id,
            Sandbox.status.in_(continuation.ACTIVE_SANDBOX_STATES),
        )
    ).scalars().all()
    return bool(active) and all(
        (s.harness_config or {}).get("continuations") for s in active
    )


@router.post("/{run_id}/continuations", status_code=status.HTTP_201_CREATED)
async def create_continuation(
    run_id: uuid.UUID, body: ContinuationCreate, session: DbSession
) -> RunOut:
    """Continue a finished sandbox with a follow-up prompt on its workspace.

    The sandbox respawns a fresh container on the SAME state-dir workspace
    (resume-in-place at the filesystem level): the run re-holds the single
    execution slot for the phase, the event stream gains a
    ``continuation_started`` → ``continuation_completed`` phase, and the
    workspace is re-snapshotted under ``continuations/<n>/``.

    Slot rules mirror approve-time pacing: a finished run claims the slot
    atomically here (409 while another run holds it — continuations don't
    queue). While THIS run is already continuation-active, a follow-up for
    another finished sibling sandbox joins in parallel.
    """
    run = _get_run_or_404(session, run_id)
    sb = session.get(Sandbox, body.sandbox_id)
    if sb is None or sb.run_id != run.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="sandbox not found"
        )
    if sb.status not in continuation.CONTINUABLE_SANDBOX_STATES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"sandbox not finished: status={sb.status!r}",
        )

    if run.status in state_machine.RUN_TERMINAL:
        # Claim the slot the same way pacing claims a queued run — the
        # committed status flip IS the claim.
        ahead = session.scalar(
            select(func.count(Run.id)).where(
                Run.id != run.id,
                Run.status.in_(pacing.BLOCKING_RUN_STATES),
            )
        )
        if ahead:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="another run holds the execution slot",
            )
        state_machine.transition(session, run, "provisioning")
        run.finished_at = None
    elif run.status in pacing.ACTIVE_RUN_STATES and _continuation_active_only(
        session, run
    ):
        pass  # this run already holds the slot via in-flight continuations
    else:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"run is not continuable: status={run.status!r}",
        )

    phase = continuation.next_phase(sb.harness_config)
    hc = dict(sb.harness_config or {})
    hc["continuations"] = [
        *(hc.get("continuations") or []),
        {
            "phase": phase,
            "prompt": body.prompt,
            "started_at": datetime.now(UTC).isoformat(),
        },
    ]
    sb.harness_config = hc
    sb.finished_at = None
    sb.terminal_reason = None
    state_machine.transition(session, sb, "provisioning")
    orch_events.emit(
        session,
        run_id=run.id,
        sandbox_id=sb.id,
        event_type="checkpoint",
        payload={
            "stage": "continuation_started",
            "phase": phase,
            "prompt": body.prompt,
        },
    )
    session.commit()

    runner.start_continuation_task(run.id, sb.id)
    return _run_out(session, run)
