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
from db.models import Artifact, Run, Sandbox, Score
from evaluator.scores import OVERALL_DIMENSION

router = APIRouter(tags=["results"])

DbSession = Annotated[Session, Depends(get_db)]


# ---------------------------------------------------------------------------
# response models (local to this router)
# ---------------------------------------------------------------------------


class DimensionScore(BaseModel):
    score: float
    rationale: str = ""


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

    results: list[SandboxResult] = []
    for sb in sandboxes:
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
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in sorted(dirpath.rglob("*")):
            if p.is_file() and not p.is_symlink():
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
