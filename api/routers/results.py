"""F7/F8 — results read API: scores, per-run result matrix, artifact download.

- ``GET /api/runs/{run_id}/results`` — every sandbox of the run with its
  scores grouped as ``dimension -> {score, rationale}`` plus ``overall``
  when the judge emitted one. Sandboxes without scores appear with empty
  dimensions so the UI matrix can show "pending"/"failed" uniformly.
- ``GET /api/sandboxes/{sandbox_id}/scores`` — raw Score rows for one
  sandbox (includes the ``overall`` pseudo-dimension row when present).
- ``GET /api/artifacts/{artifact_id}/download`` — FileResponse for artifact
  files; directories are zipped on the fly. Paths are jailed to
  ``ARTIFACT_ROOT`` — traversal attempts return 404.

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

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from api.config import get_settings
from api.deps import get_db
from api.schemas import AnalysisReport
from db.models import Artifact, Event, Run, Sandbox, Score
from evaluator.scores import OVERALL_DIMENSION
from sandbox.preview import ScanCache
from scoring.render import ANALYSIS_JSON_FILENAME

router = APIRouter(tags=["results"])

DbSession = Annotated[Session, Depends(get_db)]

# F12: the per-sandbox preview probe (workspace resolve + candidate scan) is
# an FS walk — memoized for PREVIEW_SCAN_TTL_SECONDS; workspaces only change
# on (re-)snapshot.
_scan_cache = ScanCache()


def reset_scan_cache() -> None:
    """Test hook: drop cached workspace scans."""
    _scan_cache.clear()


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
    dimensions: dict[str, DimensionScore] = Field(default_factory=dict)
    overall: DimensionScore | None = None
    # Judge/eval pipeline failure detail — set only when the eval failed and
    # the sandbox has no Score rows, so the UI can distinguish "judge failed"
    # from "no rubric scores" (pending / never evaluated). Null otherwise.
    eval_error: str | None = None
    artifacts: list[ArtifactRef] = Field(default_factory=list)
    # F12: whether the snapshot workspace has a previewable entry —
    # drives the results-page Preview button. preview_candidates lists
    # EVERY detected runnable entry, default-first (previewable/preview_type
    # describe candidates[0]); the UI renders a dropdown + POST {entry,
    # type}. Empty + previewable=false = unsupported.
    previewable: bool = False
    preview_type: str | None = None
    preview_candidates: list[PreviewCandidate] = Field(default_factory=list)
    # PR6: objective signals measured on the workspace before judging
    # (``<artifact>/analysis.json``). None when the evaluator ran without
    # signal collection or the report is unreadable.
    analysis: AnalysisReport | None = None


class RunResults(BaseModel):
    run_id: UUID
    status: str
    model_name: str
    evaluator_model: str | None = None
    same_model_eval: bool | None = None
    sandboxes: list[SandboxResult] = Field(default_factory=list)


class ScoreOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    sandbox_id: UUID
    rubric_id: UUID | None
    dimension: str
    score: float
    rationale: str
    created_at: datetime


def _load_analysis(jail: Path, sandbox: Sandbox) -> AnalysisReport | None:
    """Read ``<artifact>/analysis.json`` (PR6), jailed + schema-validated.

    Returns None when the report is absent, outside the artifact jail, or
    doesn't validate — analysis is best-effort extra detail, never a 500.
    """
    if not sandbox.artifact_path:
        return None
    try:
        path = (Path(sandbox.artifact_path) / ANALYSIS_JSON_FILENAME).resolve()
    except OSError:
        return None
    if not (path == jail or jail in path.parents):
        return None
    try:
        if not path.is_file():
            return None
        return AnalysisReport.model_validate_json(
            path.read_text(encoding="utf-8", errors="replace")
        )
    except Exception:  # noqa: BLE001 — unreadable/invalid report -> absent
        return None


# ---------------------------------------------------------------------------
# eval-failure surfacing
# ---------------------------------------------------------------------------

#: ``error``-event payload markers that mean "the eval pipeline failed this
#: sandbox": evaluator-emitted phases (spawn died, scores.json missing/
#: invalid, final eval_failed marker) plus the orchestrator's
#: ``stage="evaluation"`` catch-all (evaluate_sandbox exceptions).
_EVAL_FAIL_PHASES = frozenset(
    {"eval_scores_failed", "eval_spawn_failed", "eval_failed"}
)


def _eval_error_label(payload: dict[str, Any]) -> str | None:
    """Short eval-failure label for a sandbox ``error`` event, else None."""
    phase = payload.get("phase")
    if phase not in _EVAL_FAIL_PHASES and payload.get("stage") != "evaluation":
        return None
    code = payload.get("reason") or phase or "evaluation"
    detail = payload.get("detail") or payload.get("message")
    if isinstance(detail, str) and detail.strip():
        return f"{code}: {detail.strip()[:160]}"
    return str(code)


# ---------------------------------------------------------------------------
# GET /api/runs/{run_id}/results
# ---------------------------------------------------------------------------


@router.get("/api/runs/{run_id}/results", response_model=RunResults)
def get_run_results(run_id: UUID, session: DbSession) -> RunResults:
    run = session.get(Run, run_id)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="run not found")

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
    scores = (
        session.execute(
            select(Score)
            .where(Score.sandbox_id.in_(sandbox_ids))
            .order_by(Score.created_at.asc(), Score.id)
        )
        .scalars()
        .all()
        if sandbox_ids
        else []
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

    # Eval-failure events — latest label per sandbox. Only surfaced on
    # sandboxes with NO Score rows: a retry that eventually produced scores
    # supersedes its earlier failed attempts.
    eval_failures: dict[UUID, str] = {}
    if sandbox_ids:
        err_rows = session.execute(
            select(Event.sandbox_id, Event.payload)
            .where(
                Event.run_id == run_id,
                Event.sandbox_id.in_(sandbox_ids),
                Event.type == "error",
            )
            .order_by(Event.id)
        ).all()
        for sid, payload in err_rows:
            if sid is None or not isinstance(payload, dict):
                continue
            label = _eval_error_label(payload)
            if label is not None:
                eval_failures[sid] = label

    jail = _artifact_jail()
    scan_ttl = get_settings().PREVIEW_SCAN_TTL_SECONDS
    results: list[SandboxResult] = []
    for sb in sandboxes:
        # F12: FS probe so the UI only offers Preview when the snapshot
        # workspace actually has a detectable entry point — cached per
        # sandbox for PREVIEW_SCAN_TTL_SECONDS.
        scan = _scan_cache.scan(
            jail=jail,
            run_id=str(run_id),
            sandbox_id=str(sb.id),
            artifact_path=sb.artifact_path,
            ttl_seconds=scan_ttl,
        )
        plan = scan.plan if scan is not None else None
        dimensions: dict[str, DimensionScore] = {}
        overall: DimensionScore | None = None
        rubric_id: UUID | None = None
        for sc in by_sandbox.get(sb.id, []):
            if sc.rubric_id is not None:
                rubric_id = sc.rubric_id  # rows ordered asc -> newest wins
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
                dimensions=dimensions,
                overall=overall,
                eval_error=(
                    None if by_sandbox.get(sb.id) else eval_failures.get(sb.id)
                ),
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
                analysis=_load_analysis(jail, sb),
            )
        )

    cfg = run.config or {}
    return RunResults(
        run_id=run.id,
        status=run.status,
        model_name=run.model_name,
        evaluator_model=cfg.get("evaluator_model"),
        same_model_eval=cfg.get("same_model_eval"),
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
    rows = (
        session.execute(
            select(Score)
            .where(Score.sandbox_id == sandbox_id)
            .order_by(Score.created_at.asc(), Score.id)
        )
        .scalars()
        .all()
    )
    return [ScoreOut.model_validate(r) for r in rows]


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
