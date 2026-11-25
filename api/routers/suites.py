"""PR26 — benchmark suites: browse, fan-out runs, suite-run aggregation.

- ``GET /api/suites`` — vendored suites under ``benchmark_suites/`` (slug,
  version, title, task/check counts + launched-batch count).
- ``GET /api/suites/{slug}`` — suite detail: every task entry (preset_ref
  or inline prompt, adapter pin, declared checks) plus the most recent
  ``suite_runs`` batches.
- ``POST /api/suites/{slug}/run`` — batch submit: ONE run per suite task,
  sharing the request's model/config/budget spec. The fan-out is atomic —
  any task that fails validation aborts the whole POST. Child runs record
  ``suite_ref``/``suite_index``/``suite_run_id`` provenance and each goes
  through the normal rubric-draft → approve → lifecycle pipeline.
- ``GET /api/suite-runs/{id}`` — the task × cell aggregation for one
  batch: per task's run, per sandbox cell, the newest-wins judge
  ``overall`` score beside the deterministic checker roll-up
  (``checker_results``).

Suite YAML is parsed by :mod:`api.suites`; deterministic checks run
post-artifact by :mod:`scoring.checkers` inside the eval pass.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from api import modelcheck
from api import suites as suite_store
from api.config import get_settings
from api.deps import get_db
from api.routers.runs import _persist_run, _validate_harness_configs
from api.schemas import (
    CheckerResultOut,
    RunCreate,
    SuiteCellChecksOut,
    SuiteCellOut,
    SuiteCheckOut,
    SuiteDetailOut,
    SuiteOut,
    SuiteRunAggregate,
    SuiteRunChildOut,
    SuiteRunIn,
    SuiteRunOut,
    SuiteRunRef,
    SuiteRunResults,
    SuiteRunTaskOut,
    SuiteTaskOut,
)
from db.models import CheckerResult, Run, Sandbox, Score, SuiteRun
from orchestrator import runner
from orchestrator.matrix import sandbox_model

log = logging.getLogger(__name__)

router = APIRouter(tags=["suites"])

DbSession = Annotated[Session, Depends(get_db)]

#: How many recent suite_runs the suite detail page lists.
_RECENT_RUNS = 5


def _suites_root() -> Any:
    return get_settings().BENCHMARK_SUITES_DIR


def _require_suite(slug: str) -> suite_store.Suite:
    """Resolve the slug → suite, mapping store errors to HTTP statuses."""
    try:
        return suite_store.require_suite(_suites_root(), slug)
    except suite_store.SuiteError as exc:
        code = (
            status.HTTP_404_NOT_FOUND
            if exc.kind == "not_found"
            else status.HTTP_422_UNPROCESSABLE_CONTENT
        )
        raise HTTPException(status_code=code, detail=exc.detail) from exc


def _task_out(index: int, task: suite_store.SuiteTask) -> SuiteTaskOut:
    return SuiteTaskOut(
        index=index,
        id=task.task_id(index),
        preset_ref=task.preset_ref,
        prompt=task.prompt,
        adapter=task.adapter,
        checks=[SuiteCheckOut(check=c["check"], spec={k: v for k, v in c.items() if k != "check"}) for c in task.checks()],
    )


def _suite_out(session: Session, suite: suite_store.Suite) -> SuiteOut:
    batch_count = int(
        session.scalar(
            select(func.count(SuiteRun.id)).where(SuiteRun.suite_slug == suite.slug)
        )
        or 0
    )
    return SuiteOut(
        slug=suite.slug,
        version=suite.version,
        title=suite.title,
        description=suite.description,
        task_count=len(suite.tasks),
        checks_count=sum(len(t.checks()) for t in suite.tasks),
        suite_runs_count=batch_count,
    )


def _run_count_by_batch(session: Session, batch_ids: list[uuid.UUID]) -> dict[uuid.UUID, int]:
    if not batch_ids:
        return {}
    return {
        run_id: count
        for run_id, count in session.execute(
            select(Run.suite_run_id, func.count(Run.id))
            .where(Run.suite_run_id.in_(batch_ids))
            .group_by(Run.suite_run_id)
        ).all()
    }


# ---------------------------------------------------------------------------
# GET /api/suites + /api/suites/{slug}
# ---------------------------------------------------------------------------


@router.get("/api/suites", response_model=list[SuiteOut])
def list_suites(session: DbSession) -> list[SuiteOut]:
    """Every vendored suite, slug-sorted, with per-suite batch counts."""
    suites = suite_store.list_suites(_suites_root())
    return [_suite_out(session, s) for s in suites]


@router.get("/api/suites/{slug}", response_model=SuiteDetailOut)
def get_suite(slug: str, session: DbSession) -> SuiteDetailOut:
    suite = _require_suite(slug)
    base = _suite_out(session, suite)
    recent = list(
        session.execute(
            select(SuiteRun)
            .where(SuiteRun.suite_slug == suite.slug)
            .order_by(SuiteRun.created_at.desc())
            .limit(_RECENT_RUNS)
        )
        .scalars()
        .all()
    )
    counts = _run_count_by_batch(session, [sr.id for sr in recent])
    return SuiteDetailOut(
        **base.model_dump(),
        tasks=[_task_out(i, t) for i, t in enumerate(suite.tasks)],
        recent_runs=[
            SuiteRunRef(
                id=sr.id,
                suite_slug=sr.suite_slug,
                suite_version=sr.suite_version,
                created_at=sr.created_at,
                run_count=counts.get(sr.id, 0),
            )
            for sr in recent
        ],
    )


# ---------------------------------------------------------------------------
# POST /api/suites/{slug}/run — fan-out one run per task
# ---------------------------------------------------------------------------


@router.post("/api/suites/{slug}/run", status_code=status.HTTP_201_CREATED)
async def run_suite(
    slug: str, body: SuiteRunIn, session: DbSession
) -> SuiteRunOut:
    """Launch the whole suite as a batch — one run per suite task.

    Each child run is a normal ``Run`` (rubric draft → approve →
    provisioning → lifecycle) carrying ``suite_ref``/``suite_index``/
    ``suite_run_id`` provenance. Tasks that declare ``adapter`` run only
    against the submitted configs selecting it — e.g. a ``prompt``-pinned
    task lands exclusively on prompt cells once PR25's adapter is
    registered. Validation is shared: the probe RunCreate exercises the
    full submitted matrix once, then per-task preset resolution happens
    inside ``_persist_run`` — any failure aborts the batch (the session
    commits once at the end).
    """
    suite = _require_suite(slug)

    # Shared validation on the full submitted matrix — identical checks a
    # normal submit applies (adapter names, skill packs, provider catalog).
    probe = RunCreate(
        task_prompt="(suite run probe)",
        model_name=body.model_name,
        models=body.models,
        harness_configs=body.harness_configs,
        evaluator_model=body.evaluator_model,
        evaluator_models=body.evaluator_models,
        budgets=body.budgets,
        trials=body.trials,
    )
    _validate_harness_configs(probe)
    warnings = await modelcheck.validate_run_models(probe, settings=get_settings())

    suite_run = SuiteRun(suite_slug=suite.slug, suite_version=suite.version)
    session.add(suite_run)
    session.flush()  # assign suite_run.id for child FKs

    children: list[SuiteRunChildOut] = []
    for i, task in enumerate(suite.tasks):
        configs = list(body.harness_configs)
        if task.adapter:
            configs = [hc for hc in configs if hc.adapter == task.adapter]
            if not configs:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail=(
                        f"suite task {i} ({task.task_id(i)!r}) pins "
                        f"adapter {task.adapter!r} but no submitted "
                        "harness_config selects it — submit a config with "
                        f"adapter {task.adapter!r} or drop the pin"
                    ),
                )
        rc = RunCreate(
            # A preset_ref task's prompt is REPLACED by the preset body at
            # _persist_run — the placeholder is never persisted.
            task_prompt=task.prompt or "(suite preset task)",
            preset_ref=task.preset_ref,
            model_name=body.model_name,
            models=body.models,
            harness_configs=configs,
            evaluator_model=body.evaluator_model,
            evaluator_models=body.evaluator_models,
            budgets=body.budgets,
            trials=body.trials,
        )
        run = _persist_run(session, rc, warnings)
        run.suite_ref = suite.slug
        run.suite_index = i
        run.suite_run_id = suite_run.id
        # JSON columns aren't mutation-tracked — reassign the whole dict.
        # suite_task_id gives the aggregation a durable task label even if
        # the vendored suite.yaml is later re-edited.
        run.config = {**(run.config or {}), "suite_task_id": task.task_id(i)}
        children.append(
            SuiteRunChildOut(
                run_id=run.id,
                suite_index=i,
                task_id=task.task_id(i),
                preset_ref=run.preset_ref,
                status=run.status,
            )
        )
    session.commit()

    # Rubric drafting kicks off only after the batch is durable — the tasks
    # open their own sessions and must see committed rows.
    for child in children:
        runner.start_rubric_task(child.run_id)

    return SuiteRunOut(
        id=suite_run.id,
        suite_slug=suite_run.suite_slug,
        suite_version=suite_run.suite_version,
        runs=children,
        warnings=warnings,
    )


# ---------------------------------------------------------------------------
# GET /api/suite-runs/{id} — task × cell aggregation
# ---------------------------------------------------------------------------


def _cell_label(harness_config: dict[str, Any], model: str) -> str:
    """Cell column key — the sandbox's config label plus its participant
    model. Same fields the results matrix header shows; grouping by this
    aligns cells across tasks (same submitted matrix → same labels)."""
    name = str(harness_config.get("name") or "").strip()
    if name:
        base = name
    else:
        parts = [str(harness_config.get("adapter") or "?")]
        packs = [str(p) for p in harness_config.get("skill_packs") or []]
        if packs:
            parts.append("+".join(packs))
        extra = str(harness_config.get("system_prompt_extra") or "").strip()
        if extra:
            parts.append("'extra'")
        base = " · ".join(parts)
    return f"{base} · {model}" if model else base


def _latest_checks(
    rows: list[CheckerResult],
) -> dict[uuid.UUID, list[CheckerResultOut]]:
    """Newest-wins per (sandbox, check_type, spec) — id asc order means the
    last row for a key is the latest measurement."""
    by_sandbox: dict[uuid.UUID, dict[tuple[str, str], CheckerResult]] = {}
    for row in rows:
        key = (row.check_type, json.dumps(row.spec or {}, sort_keys=True))
        by_sandbox.setdefault(row.sandbox_id, {})[key] = row
    out: dict[uuid.UUID, list[CheckerResultOut]] = {}
    for sid, per_check in by_sandbox.items():
        out[sid] = [
            CheckerResultOut(
                check=r.check_type,
                spec=r.spec or {},
                status=r.status,
                passed=bool(r.passed),
                detail=r.detail or "",
                created_at=r.created_at,
            )
            for r in per_check.values()
        ]
    return out


def _checks_rollup(checks: list[CheckerResultOut]) -> SuiteCellChecksOut:
    passed = sum(1 for c in checks if c.status == "pass")
    failed = sum(1 for c in checks if c.status == "fail")
    skipped = sum(1 for c in checks if c.status == "skipped")
    errors = sum(1 for c in checks if c.status == "error")
    measured = passed + failed
    return SuiteCellChecksOut(
        total=len(checks),
        passed=passed,
        failed=failed,
        skipped=skipped,
        errors=errors,
        pass_rate=(passed / measured) if measured else None,
        checks=checks,
    )


@router.get("/api/suite-runs/{suite_run_id}", response_model=SuiteRunResults)
def get_suite_run(suite_run_id: uuid.UUID, session: DbSession) -> SuiteRunResults:
    """Aggregate one fan-out batch — per task, per sandbox cell: judge
    ``overall`` (newest-wins) beside the deterministic checker roll-up."""
    suite_run = session.get(SuiteRun, suite_run_id)
    if suite_run is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="suite run not found"
        )

    runs = list(
        session.execute(
            select(Run)
            .where(Run.suite_run_id == suite_run_id)
            .order_by(Run.suite_index.asc().nulls_last(), Run.id)
        )
        .scalars()
        .all()
    )
    run_ids = [r.id for r in runs]

    sandboxes: list[Sandbox] = []
    scores: list[Score] = []
    checker_rows: list[CheckerResult] = []
    if run_ids:
        sandboxes = list(
            session.execute(
                select(Sandbox)
                .where(Sandbox.run_id.in_(run_ids))
                .order_by(Sandbox.started_at.asc().nulls_last(), Sandbox.id)
            )
            .scalars()
            .all()
        )
        sandbox_ids = [sb.id for sb in sandboxes]
        if sandbox_ids:
            scores = list(
                session.execute(
                    select(Score)
                    .where(
                        Score.sandbox_id.in_(sandbox_ids),
                        Score.dimension == "overall",
                    )
                    .order_by(
                        Score.created_at.asc(),
                        Score.seq.asc().nulls_first(),
                        Score.id,
                    )
                )
                .scalars()
                .all()
            )
            checker_rows = list(
                session.execute(
                    select(CheckerResult)
                    .where(CheckerResult.sandbox_id.in_(sandbox_ids))
                    .order_by(CheckerResult.id)
                )
                .scalars()
                .all()
            )

    # Newest-wins judge overall per sandbox (asc order → last write wins).
    overall_by_sandbox: dict[uuid.UUID, float] = {}
    for sc in scores:
        overall_by_sandbox[sc.sandbox_id] = sc.score
    checks_by_sandbox = _latest_checks(checker_rows)

    tasks: list[SuiteRunTaskOut] = []
    for run in runs:
        cells = [
            SuiteCellOut(
                sandbox_id=sb.id,
                name=sb.name,
                label=_cell_label(
                    sb.harness_config or {},
                    sandbox_model(sb.harness_config or {}, run.model_name),
                ),
                model=sandbox_model(sb.harness_config or {}, run.model_name),
                status=sb.status,
                terminal_reason=sb.terminal_reason,
                judge_overall=overall_by_sandbox.get(sb.id),
                checks=_checks_rollup(checks_by_sandbox.get(sb.id, [])),
            )
            for sb in sandboxes
            if sb.run_id == run.id
        ]
        tasks.append(
            SuiteRunTaskOut(
                run_id=run.id,
                suite_index=run.suite_index if run.suite_index is not None else -1,
                task_id=(
                    (run.config or {}).get("suite_task_id")
                    or f"task-{run.suite_index}"
                ),
                preset_ref=run.preset_ref,
                status=run.status,
                created_at=run.created_at,
                finished_at=run.finished_at,
                cells=cells,
            )
        )

    # Batch roll-up — pooled checker pass rate + mean judge overall, the
    # "mean" beside each task's per-case pass/fail.
    all_cells = [c for t in tasks for c in t.cells]
    c_passed = sum(c.checks.passed for c in all_cells)
    c_failed = sum(c.checks.failed for c in all_cells)
    c_measured = c_passed + c_failed
    judged = [c.judge_overall for c in all_cells if c.judge_overall is not None]
    aggregate = SuiteRunAggregate(
        tasks=len(tasks),
        cells=len(all_cells),
        checks_total=sum(c.checks.total for c in all_cells),
        checks_passed=c_passed,
        checks_failed=c_failed,
        checks_skipped=sum(c.checks.skipped for c in all_cells),
        checks_errors=sum(c.checks.errors for c in all_cells),
        check_pass_rate=(c_passed / c_measured) if c_measured else None,
        cells_scored=len(judged),
        mean_judge_overall=(sum(judged) / len(judged)) if judged else None,
    )
    return SuiteRunResults(
        id=suite_run.id,
        suite_slug=suite_run.suite_slug,
        suite_version=suite_run.suite_version,
        created_at=suite_run.created_at,
        runs=tasks,
        aggregate=aggregate,
    )
