"""F1/F2 — run submission, rubric review/approval, run detail (``/api/runs*``).

Thin handlers only: validate → DB → register background tasks via
``orchestrator.runner``. All orchestration (rubric drafting, provisioning,
monitoring, evaluation) lives in ``orchestrator/``.

Handlers are ``async def`` because they call ``asyncio.create_task`` (via
``runner.start_*``) — sync endpoints run in a worker thread with no loop.
"""

from __future__ import annotations

import logging
import math
import shutil
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from api import modelcheck
from api import presets as preset_store
from api.config import get_settings
from api.deps import get_db
from api.schemas import (
    RubricOut,
    RunCreate,
    RunDeleteOut,
    RunOut,
    RunSummary,
    SandboxOut,
)
from db.models import (
    Artifact,
    CheckerResult,
    Event,
    Rubric,
    Run,
    Sandbox,
    Score,
)
from orchestrator import adapters as orch_adapters
from orchestrator import events as orch_events
from orchestrator import pacing, provision, runner, state_machine
from orchestrator.matrix import participant_models, run_models
from orchestrator.rubric import RubricContent
from skillpack_import.store import imported_root

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/runs", tags=["runs"])

DbSession = Annotated[Session, Depends(get_db)]


class RubricUpdate(BaseModel):
    """PUT /api/runs/{id}/rubric body — full replacement of draft content."""

    content: RubricContent


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
    """True when the judge model equals ANY participant model — non-independent
    judging for that cell (PR14: participants are a set, not just model_name;
    per-config ``model`` pins count too — ``run_models``)."""
    evaluator = (run.config or {}).get("evaluator_model") or run.model_name
    return evaluator in set(run_models(run.config, run.model_name))


def _queue_positions(session: Session) -> dict[uuid.UUID, int]:
    """run_id → 1-based position among ``queued`` runs, in the same order
    ``pacing.claim_next_queued`` promotes them (``started_at`` =
    approval/queue-entry time, ``created_at`` fallback)."""
    rows = session.execute(
        select(Run.id)
        .where(Run.status == "queued")
        .order_by(Run.started_at.asc().nulls_last(), Run.created_at.asc())
    ).scalars()
    return {run_id: i + 1 for i, run_id in enumerate(rows)}


def _run_out(
    session: Session, run: Run, *, warnings: list[str] | None = None
) -> RunOut:
    rubric = _latest_rubric(session, run.id)
    return RunOut(
        id=run.id,
        status=run.status,
        task_prompt=run.task_prompt,
        model_name=run.model_name,
        trials=run.trials or 1,
        config=run.config or {},
        same_model_eval=_same_model_eval(run),
        created_at=run.created_at,
        updated_at=run.updated_at,
        started_at=run.started_at,
        finished_at=run.finished_at,
        queue_position=(
            _queue_positions(session).get(run.id) if run.status == "queued" else None
        ),
        warnings=list(warnings or []),
        preset_ref=run.preset_ref,
        preset_revision=run.preset_revision,
        suite_ref=run.suite_ref,
        suite_index=run.suite_index,
        suite_run_id=run.suite_run_id,
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
    # PR25: selectable includes no-sandbox pseudo-adapters (``prompt``) —
    # they're valid matrix cells, just never spawned.
    known_adapters = set(orch_adapters.selectable_adapter_names())
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


def _persist_run(session: Session, body: RunCreate, warnings: list[str]) -> Run:
    """Build the run config, resolve the preset ref, insert the Run row.

    Flush-only — the caller commits so multi-run batches (PR26 suite
    fan-out) stay atomic. Returns the pending Run (``run.id`` is assigned
    by the flush). ``warnings`` accumulates advisory notices (PR27
    model_options drops) for the caller to echo back.
    """
    # PR27: model_options keys the config's adapter doesn't expose are
    # stripped before persisting — the sandbox env/request only ever sees
    # honored knobs — and the drop is called out as a warning (never a 422:
    # the user asked for the option where supported, not for submission to
    # fail on a harness that can't do seed/temperature).
    configs_in: list[dict[str, Any]] = []
    for i, hc in enumerate(body.harness_configs):
        entry = {
            k: v
            for k, v in hc.model_dump().items()
            if k != "model" or v is not None
        }
        provided = hc.model_options.provided() if hc.model_options else {}
        if provided:
            supported = orch_adapters.supported_model_options(hc.adapter)
            kept = {k: v for k, v in provided.items() if k in supported}
            dropped = sorted(set(provided) - set(kept))
            if dropped:
                warnings.append(
                    f"harness_configs[{i}] (adapter {hc.adapter!r}): "
                    f"model_options {dropped} not supported — dropped "
                    f"(supported: {sorted(supported) or 'none'})"
                )
            if kept:
                entry["model_options"] = kept
            else:
                entry.pop("model_options", None)
        else:
            entry.pop("model_options", None)
        configs_in.append(entry)

    evaluator_model = body.evaluator_model or body.model_name
    # PR14: persist the run's model list ([model_name] + deduped extras) so
    # lifecycle/results/analytics can expand the matrix without re-parsing
    # the raw request. Per-config ``model`` pins stay inside each config
    # dict (null pins omitted — unexpanded configs carry no ``model`` key).
    models = participant_models(body.model_name, body.models)
    config = {
        "harness_configs": configs_in,
        "models": models,
        "evaluator_model": evaluator_model,
        "budgets": body.budgets.model_dump(),
        # PR27: repeats-per-cell — duplicated on the Run column so GETs can
        # read it without parsing config; the matrix expansion reads config.
        "trials": body.trials,
    }
    # Judge == any participant model flags non-independent scoring; pins
    # count as participants even when outside the run's model list.
    config["same_model_eval"] = evaluator_model in set(
        run_models(config, body.model_name)
    )

    # PR22 preset resolution — a preset_ref asserts the task came from the
    # preset: the preset's body REPLACES the submitted task_prompt (stored
    # verbatim, same comparison contract) and the resolved slug+revision are
    # snapshotted as provenance. Unknown refs 422 like unknown adapters.
    task_prompt = body.task_prompt
    preset_ref: str | None = None
    preset_revision: int | None = None
    if body.preset_ref:
        preset = preset_store.resolve(
            session, get_settings().TASK_PRESETS_DIR, body.preset_ref
        )
        if preset is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=(
                    f"unknown preset {body.preset_ref!r} — "
                    "GET /api/presets lists valid slugs"
                ),
            )
        task_prompt = preset.task_prompt
        preset_ref = preset.slug
        preset_revision = preset.revision

    run = Run(
        status="submitted",
        task_prompt=task_prompt,
        model_name=body.model_name,
        trials=body.trials,
        config=config,
        preset_ref=preset_ref,
        preset_revision=preset_revision,
    )
    session.add(run)
    session.flush()
    return run


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_run(body: RunCreate, session: DbSession) -> RunOut:
    """Submit a run: persist the spec, kick off rubric drafting in background."""
    _validate_harness_configs(body)
    # gt=0 accepts inf — a non-finite cap would silently never trip (and a
    # field-level rejection serializes the echoed body into the 422 → 500).
    if body.budgets.max_cost_usd is not None and not math.isfinite(
        body.budgets.max_cost_usd
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="budgets.max_cost_usd must be a finite number",
        )
    # Best-effort provider-catalog check — warnings on absent models, a hard
    # 422 on provider-reported tool-capability mismatches. Catalog fetch
    # failures skip validation entirely (offline tolerance).
    warnings = await modelcheck.validate_run_models(body, settings=get_settings())

    run = _persist_run(session, body, warnings)
    session.commit()

    runner.start_rubric_task(run.id)
    return _run_out(session, run, warnings=warnings)


@router.get("")
async def list_runs(
    session: DbSession,
    run_status: Annotated[str | None, Query(alias="status")] = None,
    preset: Annotated[str | None, Query()] = None,
    suite: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[RunSummary]:
    """Runs newest-first; ``?status=``/``?preset=``/``?suite=`` filters,
    paginated."""
    stmt = select(Run).order_by(Run.created_at.desc()).limit(limit).offset(offset)
    if run_status:
        stmt = stmt.where(Run.status == run_status)
    if preset:
        stmt = stmt.where(Run.preset_ref == preset.strip().lower())
    if suite:
        stmt = stmt.where(Run.suite_ref == suite.strip().lower())
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
    # One queue-order query for the whole page — skipped when nothing's parked.
    positions = (
        _queue_positions(session)
        if any(r.status == "queued" for r in runs)
        else {}
    )
    return [
        RunSummary(
            id=r.id,
            status=r.status,
            task_prompt=r.task_prompt[:200],
            model_name=r.model_name,
            models=run_models(r.config, r.model_name),
            created_at=r.created_at,
            finished_at=r.finished_at,
            sandbox_count=counts.get(r.id, 0),
            preset_ref=r.preset_ref,
            suite_ref=r.suite_ref,
            suite_index=r.suite_index,
            suite_run_id=r.suite_run_id,
            queue_position=positions.get(r.id),
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


def _editable_rubric(
    session: Session, run_id: uuid.UUID, *, action: str = "edit"
) -> tuple[Run, Rubric]:
    """Shared guard for PUT rubric / approve: run in rubric_review AND the
    latest rubric still a draft. Raises 404/409 otherwise — the approve
    message names the real condition (the run already moved on) instead of
    the edit-path's misleading "rubric not editable"."""
    run = _get_run_or_404(session, run_id)
    rubric = _latest_rubric(session, run_id)
    if rubric is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="no rubric for run"
        )
    if run.status != "rubric_review" or rubric.status != "draft":
        if action == "approve":
            detail = (
                f"run already approved or finished: run.status={run.status!r}, "
                f"rubric.status={rubric.status!r}"
            )
        else:
            detail = (
                f"rubric not editable: run.status={run.status!r}, "
                f"rubric.status={rubric.status!r}"
            )
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)
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
    run, rubric = _editable_rubric(session, run_id, action="approve")

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
# cancel / delete
# ---------------------------------------------------------------------------


def _hard_delete_run(session: Session, run: Run) -> None:
    """Cascade-delete one terminal run's rows: scores + artifacts hang off
    sandboxes, events/rubrics/sandboxes off the run (no ORM cascade is
    declared, so FK order matters — children first). Events are append-only
    per AGENTS.md — hard delete is the explicit exception (junk-run removal).
    """
    sandbox_ids = select(Sandbox.id).where(Sandbox.run_id == run.id)
    session.execute(delete(Score).where(Score.sandbox_id.in_(sandbox_ids)))
    session.execute(delete(Artifact).where(Artifact.sandbox_id.in_(sandbox_ids)))
    session.execute(
        delete(CheckerResult).where(CheckerResult.run_id == run.id)
    )
    session.execute(delete(Event).where(Event.run_id == run.id))
    # Rubric.parent_id is a self-FK — a single DELETE of the run's rows
    # removes parent + child versions together.
    session.execute(delete(Rubric).where(Rubric.run_id == run.id))
    session.execute(delete(Sandbox).where(Sandbox.run_id == run.id))
    # Bulk delete the run row too — session.delete() would load the
    # (already-removed) collections to nullify FKs under the ORM cascade.
    session.execute(delete(Run).where(Run.id == run.id))


def _remove_artifact_dir(run_id: uuid.UUID) -> None:
    """Best-effort ``<ARTIFACT_ROOT>/<run_id>/`` removal — jailed like the
    download endpoint; a failure only leaves files behind, never 500s."""
    try:
        jail = Path(get_settings().ARTIFACT_ROOT).resolve()
        target = (jail / str(run_id)).resolve()
        if target != jail and jail in target.parents and target.is_dir():
            shutil.rmtree(target)
    except OSError:
        log.warning("could not remove artifact dir for run %s", run_id, exc_info=True)


@router.delete("/{run_id}")
async def delete_run(run_id: uuid.UUID, session: DbSession) -> RunDeleteOut:
    """Cancel a pre-spawn run, or hard-delete a terminal one.

    - ``submitted`` / ``rubric_review`` / ``queued`` → ``cancelled``: nothing
      has been spawned, so cancellation is a status flip + checkpoint; a
      pending rubric-draft task is cancelled and a freed queue position
      promotes the next parked run.
    - ``done`` / ``failed`` / ``cancelled`` → hard delete of the run and its
      rows, plus best-effort ``data/artifacts/<run_id>/`` removal.
    - ``provisioning`` / ``running`` / ``evaluating`` → 409: in-flight
      sandbox teardown is out of scope — wait for a terminal state.
    """
    run = _get_run_or_404(session, run_id)
    session.refresh(run)  # decide on the latest committed status

    if run.status in pacing.ACTIVE_RUN_STATES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"cannot cancel run in status {run.status!r} — in-flight "
                "sandboxes are not cancellable; wait for it to reach a "
                "terminal state"
            ),
        )

    if run.status in state_machine.RUN_TERMINAL:
        _hard_delete_run(session, run)
        session.commit()
        _remove_artifact_dir(run_id)
        return RunDeleteOut(status="deleted")

    # submitted | rubric_review | queued — safe: no sandboxes exist yet and
    # the lifecycle bails when the run left 'provisioning' before it starts.
    runner.cancel_task(run.id, "rubric")
    previous_status = run.status
    state_machine.transition(session, run, "cancelled")
    run.finished_at = datetime.now(UTC)
    orch_events.emit(
        session,
        run_id=run.id,
        event_type="checkpoint",
        payload={"stage": "cancelled", "previous_status": previous_status},
    )
    session.commit()
    # A cancelled `queued` run frees its queue position — promote the next
    # parked run, same as a lifecycle exit (no-op when the slot is held).
    await runner.release_run_slot()
    return RunDeleteOut(status="cancelled")
