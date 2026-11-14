"""F7/F8 — results read API: scores, per-run result matrix, artifact download.

- ``GET /api/runs/{run_id}/results`` — every sandbox of the run with its
  scores grouped as ``dimension -> {score, rationale}`` plus ``overall``
  when the judge emitted one. Sandboxes without scores appear with empty
  dimensions so the UI matrix can show "pending"/"failed" uniformly.
  ``?rubric_version=N`` restricts the matrix to one rubric version's
  provenance (PR5); default is newest-score-wins per sandbox.
- ``GET /api/sandboxes/{sandbox_id}/scores`` — raw Score rows for one
  sandbox (includes the ``overall`` pseudo-dimension row when present).
- ``GET /api/artifacts/{artifact_id}/download`` — FileResponse for artifact
  files; directories are zipped on the fly. Paths are jailed to
  ``ARTIFACT_ROOT`` — traversal attempts return 404.

PR5 (re-score + rubric edits) adds:

- ``GET /api/runs/{run_id}/rubrics`` — all rubric versions + score counts.
- ``POST /api/runs/{run_id}/rubrics`` — post-run edit → new approved
  version (previous approved superseded); ``rescore=true`` also queues a
  re-score against the new version.
- ``POST /api/runs/{run_id}/rescore`` — re-run the evaluator on stored
  artifacts (terminal runs only), optionally pinned to a rubric version, a
  sandbox subset, or a different judge model.

Pydantic models live here (not api/schemas.py) per Phase-2 ownership split.
"""

from __future__ import annotations

import io
import os
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from api.config import get_settings
from api.deps import get_db
from api.schemas import RubricOut
from db.models import Artifact, Rubric, Run, Sandbox, Score
from evaluator.scores import OVERALL_DIMENSION
from orchestrator import events as orch_events
from orchestrator import runner, state_machine
from orchestrator.rubric import RubricContent, create_rubric_version
from sandbox.preview import resolve_workspace_dir, scan_preview

router = APIRouter(tags=["results"])

DbSession = Annotated[Session, Depends(get_db)]


# ---------------------------------------------------------------------------
# response models (local to this router)
# ---------------------------------------------------------------------------


class DimensionScore(BaseModel):
    score: float
    rationale: str = ""


class ArtifactRef(BaseModel):
    """Artifact row exposed for the download endpoint."""

    id: UUID
    kind: str
    path: str


class PreviewCandidate(BaseModel):
    """One detected runnable entry — {entry: workspace-relative path, type}."""

    entry: str
    type: str


class SandboxResult(BaseModel):
    """One sandbox's evaluation outcome inside the run matrix."""

    sandbox_id: UUID
    harness_config: dict[str, Any] = Field(default_factory=dict)
    status: str
    terminal_reason: str | None = None
    artifact_path: str | None = None
    rubric_id: UUID | None = None
    # PR5: version of the rubric the displayed scores were judged against
    # (newest contributing score row).
    rubric_version: int | None = None
    dimensions: dict[str, DimensionScore] = Field(default_factory=dict)
    overall: DimensionScore | None = None
    artifacts: list[ArtifactRef] = Field(default_factory=list)
    # F12: whether the snapshot workspace has a previewable entry —
    # drives the results-page Preview button. preview_candidates lists
    # EVERY detected runnable entry, default-first (previewable/preview_type
    # describe candidates[0]); the UI renders a dropdown + POST {entry}.
    # Empty + previewable=false = unsupported.
    previewable: bool = False
    preview_type: str | None = None
    preview_candidates: list[PreviewCandidate] = Field(default_factory=list)


class RubricVersionOut(BaseModel):
    """One rubric version for the results-page version selector.

    ``score_count`` = Score rows carrying this rubric's provenance.
    """

    id: UUID
    version: int
    status: str  # draft | approved | superseded
    parent_id: UUID | None = None
    created_at: datetime
    score_count: int = 0


class RubricVersionCreate(BaseModel):
    """POST /api/runs/{id}/rubrics body — post-run rubric edit.

    The new content becomes version N+1 and is frozen immediately
    (``approved``); the previous approved version is superseded.
    ``base_rubric_id`` pins which row the edit derives from (default: the
    latest version). ``rescore`` additionally queues a re-score against the
    new version in the same request.
    """

    content: RubricContent
    base_rubric_id: UUID | None = None
    rescore: bool = False


class RescoreIn(BaseModel):
    """POST /api/runs/{id}/rescore body — everything optional."""

    # Pin a specific rubric version (any status — re-judging against a
    # superseded version is a legitimate comparison). Default: latest
    # approved.
    rubric_id: UUID | None = None
    # Restrict re-judging to these sandboxes (min 1 when provided).
    sandbox_ids: list[UUID] | None = Field(default=None, min_length=1)
    # Judge-model override for this pass only (default: run's
    # evaluator_model).
    model: str | None = Field(default=None, max_length=255)


class RescoreOut(BaseModel):
    """202 response — re-score accepted and queued."""

    run_id: UUID
    status: str  # "evaluating"
    rubric_id: UUID
    rubric_version: int
    model: str
    sandboxes: int  # artifact-bearing sandboxes queued for judging


class RunResults(BaseModel):
    run_id: UUID
    status: str
    model_name: str
    evaluator_model: str | None = None
    same_model_eval: bool | None = None
    # PR5: the ?rubric_version= filter echoed back (None = newest-wins view).
    selected_rubric_version: int | None = None
    # Every rubric version of the run + score counts — drives the UI's
    # version selector without a second request.
    rubric_versions: list[RubricVersionOut] = Field(default_factory=list)
    sandboxes: list[SandboxResult] = Field(default_factory=list)


class ScoreOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    sandbox_id: UUID
    rubric_id: UUID | None
    # PR5 provenance — denormalized rubrics.version at write time.
    rubric_version: int | None = None
    dimension: str
    score: float
    rationale: str
    created_at: datetime


# ---------------------------------------------------------------------------
# rubric versions + results
# ---------------------------------------------------------------------------


def _rubric_rows(session: Session, run_id: UUID) -> list[Rubric]:
    """Every rubric version for a run, newest first."""
    return list(
        session.execute(
            select(Rubric)
            .where(Rubric.run_id == run_id)
            .order_by(Rubric.version.desc())
        )
        .scalars()
        .all()
    )


def _score_counts_by_rubric(session: Session, run_id: UUID) -> dict[UUID, int]:
    """Score-row counts keyed by rubric row id (= one rubric version)."""
    return dict(
        session.execute(
            select(Score.rubric_id, func.count(Score.id))
            .join(Sandbox, Score.sandbox_id == Sandbox.id)
            .where(Sandbox.run_id == run_id, Score.rubric_id.is_not(None))
            .group_by(Score.rubric_id)
        ).all()
    )


def _latest_approved_rubric(session: Session, run_id: UUID) -> Rubric | None:
    return session.execute(
        select(Rubric)
        .where(Rubric.run_id == run_id, Rubric.status == "approved")
        .order_by(Rubric.version.desc())
        .limit(1)
    ).scalar_one_or_none()


@router.get("/api/runs/{run_id}/rubrics", response_model=list[RubricVersionOut])
def list_rubric_versions(run_id: UUID, session: DbSession) -> list[RubricVersionOut]:
    """All rubric versions (newest first) + per-version score counts."""
    if session.get(Run, run_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="run not found")
    counts = _score_counts_by_rubric(session, run_id)
    return [
        RubricVersionOut(
            id=r.id,
            version=r.version,
            status=r.status,
            parent_id=r.parent_id,
            created_at=r.created_at,
            score_count=counts.get(r.id, 0),
        )
        for r in _rubric_rows(session, run_id)
    ]


@router.get("/api/rubrics/{rubric_id}", response_model=RubricOut)
def get_rubric_version(rubric_id: UUID, session: DbSession) -> RubricOut:
    """One rubric version incl. content — backs the post-run edit flow, where
    the UI prefills the editor from whichever version the user picked."""
    rubric = session.get(Rubric, rubric_id)
    if rubric is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="rubric not found"
        )
    return RubricOut.model_validate(rubric)


@router.get("/api/runs/{run_id}/results", response_model=RunResults)
def get_run_results(
    run_id: UUID,
    session: DbSession,
    rubric_version: Annotated[int | None, Query(ge=1)] = None,
) -> RunResults:
    run = session.get(Run, run_id)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="run not found")

    # rubric_id -> version map for provenance display + filter fallbacks.
    rubrics = _rubric_rows(session, run_id)
    version_of: dict[UUID, int] = {r.id: r.version for r in rubrics}

    sandboxes = (
        session.execute(
            select(Sandbox)
            .where(Sandbox.run_id == run_id)
            .order_by(Sandbox.started_at.asc().nulls_last(), Sandbox.id)
        )
        .scalars()
        .all()
    )
    sandbox_ids = [sb.id for sb in sandboxes]
    score_stmt = (
        select(Score)
        .where(Score.sandbox_id.in_(sandbox_ids))
        # Merge order is last-wins per dimension. created_at ties are broken
        # by rubric_version (sqlite's CURRENT_TIMESTAMP is second-precision,
        # so two eval passes can land in the same second — the higher
        # version is the later judgment). A re-score pinned to an older
        # version still wins via its strictly-newer timestamp.
        .order_by(
            Score.created_at.asc(),
            Score.rubric_version.asc().nulls_first(),
            Score.id,
        )
    )
    if rubric_version is not None:
        # Strict provenance filter — also catches rows whose rubric_id maps
        # to this version but whose denormalized rubric_version was never
        # stamped (pre-backfill/manual inserts).
        matching_ids = [rid for rid, ver in version_of.items() if ver == rubric_version]
        score_stmt = score_stmt.where(
            or_(
                Score.rubric_version == rubric_version,
                Score.rubric_id.in_(matching_ids),
            )
        )
    scores = (
        session.execute(score_stmt).scalars().all() if sandbox_ids else []
    )

    by_sandbox: dict[UUID, list[Score]] = {sid: [] for sid in sandbox_ids}
    for sc in scores:
        by_sandbox.setdefault(sc.sandbox_id, []).append(sc)

    artifacts = (
        session.execute(
            select(Artifact)
            .where(Artifact.sandbox_id.in_(sandbox_ids))
            .order_by(Artifact.created_at.asc(), Artifact.id)
        )
        .scalars()
        .all()
        if sandbox_ids
        else []
    )
    artifacts_by_sandbox: dict[UUID, list[Artifact]] = {
        sid: [] for sid in sandbox_ids
    }
    for art in artifacts:
        artifacts_by_sandbox.setdefault(art.sandbox_id, []).append(art)

    jail = _artifact_jail()
    results: list[SandboxResult] = []
    for sb in sandboxes:
        # F12: cheap FS probe so the UI only offers Preview when the snapshot
        # workspace actually has a detectable entry point.
        ws = resolve_workspace_dir(
            jail=jail,
            run_id=str(run_id),
            sandbox_id=str(sb.id),
            artifact_path=sb.artifact_path,
        )
        scan = scan_preview(ws) if ws is not None else None
        plan = scan.plan if scan is not None else None
        dimensions: dict[str, DimensionScore] = {}
        overall: DimensionScore | None = None
        rubric_id: UUID | None = None
        sb_rubric_version: int | None = None
        for sc in by_sandbox.get(sb.id, []):
            if sc.rubric_id is not None:
                rubric_id = sc.rubric_id  # rows ordered asc -> newest wins
            row_version = sc.rubric_version or (
                version_of.get(sc.rubric_id) if sc.rubric_id is not None else None
            )
            if row_version is not None:
                sb_rubric_version = row_version
            entry = DimensionScore(score=sc.score, rationale=sc.rationale or "")
            if sc.dimension == OVERALL_DIMENSION:
                overall = entry
            else:
                dimensions[sc.dimension] = entry
        results.append(
            SandboxResult(
                sandbox_id=sb.id,
                harness_config=sb.harness_config or {},
                status=sb.status,
                terminal_reason=sb.terminal_reason,
                artifact_path=sb.artifact_path,
                rubric_id=rubric_id,
                rubric_version=sb_rubric_version,
                dimensions=dimensions,
                overall=overall,
                artifacts=[
                    ArtifactRef(id=a.id, kind=a.kind, path=a.path)
                    for a in artifacts_by_sandbox.get(sb.id, [])
                ],
                previewable=plan is not None,
                preview_type=plan.type if plan is not None else None,
                preview_candidates=(
                    [
                        PreviewCandidate(entry=c.entry, type=c.type)
                        for c in scan.candidates
                    ]
                    if scan is not None
                    else []
                ),
            )
        )

    cfg = run.config or {}
    counts = _score_counts_by_rubric(session, run_id)
    return RunResults(
        run_id=run.id,
        status=run.status,
        model_name=run.model_name,
        evaluator_model=cfg.get("evaluator_model"),
        same_model_eval=cfg.get("same_model_eval"),
        selected_rubric_version=rubric_version,
        rubric_versions=[
            RubricVersionOut(
                id=r.id,
                version=r.version,
                status=r.status,
                parent_id=r.parent_id,
                created_at=r.created_at,
                score_count=counts.get(r.id, 0),
            )
            for r in rubrics
        ],
        sandboxes=results,
    )


# ---------------------------------------------------------------------------
# GET /api/sandboxes/{sandbox_id}/scores
# ---------------------------------------------------------------------------


@router.get("/api/sandboxes/{sandbox_id}/scores", response_model=list[ScoreOut])
def get_sandbox_scores(sandbox_id: UUID, session: DbSession) -> list[ScoreOut]:
    sandbox = session.get(Sandbox, sandbox_id)
    if sandbox is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="sandbox not found"
        )
    version_of = {
        r.id: r.version for r in _rubric_rows(session, sandbox.run_id)
    }
    rows = (
        session.execute(
            select(Score)
            .where(Score.sandbox_id == sandbox_id)
            .order_by(Score.created_at.asc(), Score.id)
        )
        .scalars()
        .all()
    )
    out: list[ScoreOut] = []
    for r in rows:
        item = ScoreOut.model_validate(r)
        if item.rubric_version is None and r.rubric_id is not None:
            item.rubric_version = version_of.get(r.rubric_id)
        out.append(item)
    return out


# ---------------------------------------------------------------------------
# POST /api/runs/{run_id}/rubrics + /rescore — eval iteration (PR5)
# ---------------------------------------------------------------------------


def _prepare_rescore(
    session: Session,
    run: Run,
    *,
    rubric_id: UUID | None,
    sandbox_ids: list[UUID] | None,
    model: str | None,
) -> tuple[Rubric, list[Sandbox]]:
    """Shared guards + transition for re-score triggers.

    Resolves the rubric (explicit id or latest approved) and the
    artifact-bearing sandboxes to judge, then mutates the run to
    ``evaluating`` (finished_at cleared) and emits a ``rescore_requested``
    checkpoint — the caller commits, then registers the background task.
    """
    if run.status not in state_machine.RUN_TERMINAL:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"run is {run.status!r} — re-score only applies to terminal runs",
        )
    if runner.in_flight(run.id, "rescore"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="a re-score is already in progress for this run",
        )

    if rubric_id is not None:
        rubric = session.get(Rubric, rubric_id)
        if rubric is None or rubric.run_id != run.id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="rubric not found for run",
            )
    else:
        rubric = _latest_approved_rubric(session, run.id)
        if rubric is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="no approved rubric for this run",
            )

    sb_stmt = select(Sandbox).where(Sandbox.run_id == run.id)
    if sandbox_ids is not None:
        sb_stmt = sb_stmt.where(Sandbox.id.in_(sandbox_ids))
        resolved = list(session.execute(sb_stmt).scalars().all())
        found = {sb.id for sb in resolved}
        missing = [str(s) for s in sandbox_ids if s not in found]
        if missing:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"sandbox ids not in run: {missing}",
            )
        no_artifact = [str(sb.id) for sb in resolved if not sb.artifact_path]
        if no_artifact:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"sandboxes without artifacts cannot be re-scored: {no_artifact}",
            )
        sandboxes = resolved
    else:
        sandboxes = list(
            session.execute(
                sb_stmt.where(Sandbox.artifact_path.is_not(None))
            ).scalars().all()
        )
        if not sandboxes:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="no artifact-bearing sandboxes to re-score",
            )

    try:
        state_machine.transition(session, run, "evaluating")
    except state_machine.InvalidTransition:
        # Defensive — RUN_TERMINAL → evaluating is in the table.
        run.status = "evaluating"
    run.finished_at = None
    orch_events.emit(
        session,
        run_id=run.id,
        event_type="checkpoint",
        payload={
            "stage": "rescore_requested",
            "rubric_id": str(rubric.id),
            "rubric_version": rubric.version,
            "model": model or (run.config or {}).get("evaluator_model") or run.model_name,
            "sandbox_ids": (
                [str(s) for s in sandbox_ids] if sandbox_ids is not None else "all"
            ),
        },
    )
    return rubric, sandboxes


@router.post("/api/runs/{run_id}/rubrics", status_code=status.HTTP_201_CREATED)
async def create_rubric_version_endpoint(
    run_id: UUID, body: RubricVersionCreate, session: DbSession
) -> RubricOut:
    """Post-run rubric edit → new approved version (+ optional re-score)."""
    run = session.get(Run, run_id)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="run not found")
    if run.status not in state_machine.RUN_TERMINAL:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"run is {run.status!r} — rubric edits only apply to terminal runs",
        )
    base: Rubric | None = None
    if body.base_rubric_id is not None:
        base = session.get(Rubric, body.base_rubric_id)
        if base is None or base.run_id != run_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="base rubric not found for run",
            )

    # Insert the new version first so the re-score pins IT (the flushed row
    # is visible to _prepare_rescore's session.get). All-or-nothing: a
    # rescore guard failure (no artifacts, eval in flight) raises before
    # commit and the rubric row is rolled back with it.
    rubric = create_rubric_version(session, run, body.content, base=base)
    rescore_params: tuple[Rubric, list[Sandbox]] | None = None
    if body.rescore:
        rescore_params = _prepare_rescore(
            session, run, rubric_id=rubric.id, sandbox_ids=None, model=None
        )
    session.commit()
    if rescore_params is not None:
        runner.start_rescore_task(run.id, rubric_id=rubric.id)
    return RubricOut.model_validate(rubric)


@router.post(
    "/api/runs/{run_id}/rescore",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=RescoreOut,
)
async def rescore_run_endpoint(
    run_id: UUID, session: DbSession, body: RescoreIn | None = None
) -> RescoreOut:
    """Re-run the evaluator on stored artifacts (terminal runs only)."""
    run = session.get(Run, run_id)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="run not found")
    body = body or RescoreIn()
    model = body.model.strip() or None if body.model else None
    rubric, sandboxes = _prepare_rescore(
        session,
        run,
        rubric_id=body.rubric_id,
        sandbox_ids=body.sandbox_ids,
        model=model,
    )
    session.commit()
    runner.start_rescore_task(
        run.id,
        rubric_id=rubric.id,
        sandbox_ids=body.sandbox_ids,
        model=model,
    )
    return RescoreOut(
        run_id=run.id,
        status="evaluating",
        rubric_id=rubric.id,
        rubric_version=rubric.version,
        model=model
        or (run.config or {}).get("evaluator_model")
        or run.model_name,
        sandboxes=len(sandboxes),
    )


# ---------------------------------------------------------------------------
# GET /api/artifacts/{artifact_id}/download
# ---------------------------------------------------------------------------


def _artifact_jail() -> Path:
    """Resolved root all artifact downloads must stay under."""
    return Path(get_settings().ARTIFACT_ROOT).resolve()


def _resolve_artifact_path(artifact: Artifact, jail: Path) -> Path | None:
    """Resolve ``artifact.path`` to an existing file/dir inside ``jail``.

    ``Artifact.path`` may be absolute or relative — relative paths are tried
    against the artifact root, the process cwd, and the sandbox's artifact
    dir (covers ``"workspace"``-style relative entries). Anything resolving
    outside ``jail`` (incl. ``..`` escapes and symlink tricks via resolve())
    returns None → 404, so the endpoint can't be used to read arbitrary files.
    """
    raw = Path(artifact.path)
    sandbox_dir = None
    if artifact.sandbox is not None and artifact.sandbox.artifact_path:
        sandbox_dir = Path(artifact.sandbox.artifact_path)

    candidates: list[Path] = []
    if raw.is_absolute():
        candidates.append(raw)
    else:
        candidates.append(jail / raw)
        candidates.append(Path.cwd() / raw)
        if sandbox_dir is not None:
            candidates.append(sandbox_dir / raw)
            candidates.append(sandbox_dir / raw.name)

    for cand in candidates:
        try:
            resolved = cand.resolve()
        except OSError:
            continue
        if (resolved == jail or jail in resolved.parents) and resolved.exists():
            return resolved
    return None


def _zip_dir(dirpath: Path) -> bytes:
    # os.walk doesn't descend into symlinked dirs (followlinks=False) — the
    # snapshotted workspace is candidate-authored, so rglob could escape the
    # jail through a planted symlink. File symlinks are skipped outright.
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, _dirs, files in os.walk(dirpath):
            for name in sorted(files):
                p = Path(root) / name
                if p.is_symlink() or not p.is_file():
                    continue
                zf.write(p, p.relative_to(dirpath))
    return buf.getvalue()


@router.get("/api/artifacts/{artifact_id}/download")
def download_artifact(artifact_id: UUID, session: DbSession) -> Response:
    artifact = session.get(Artifact, artifact_id)
    if artifact is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="artifact not found"
        )

    jail = _artifact_jail()
    path = _resolve_artifact_path(artifact, jail)
    if path is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="artifact file not found"
        )

    if path.is_dir():
        data = _zip_dir(path)
        filename = f"{artifact.kind}-{artifact.id}.zip"
        return Response(
            content=data,
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    return FileResponse(path, filename=path.name)
