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

PR34 adds the artifact file browser + side-by-side diff:

- ``GET /api/sandboxes/{sandbox_id}/files`` — jailed, depth/entry-bounded
  listing of the artifact ``workspace/`` (prompt cells and other
  workspace-less artifacts browse the artifact root itself, where
  ``output.txt``/transcript/meta live).
- ``GET /api/sandboxes/{sandbox_id}/files/{path}`` — size-capped text read
  as JSON (``binary`` flag on undecodable content), ``?download=1`` for a
  FileResponse download, or ``?diff=<other_sandbox_id>`` for a server-side
  unified diff of that path across two sandboxes' browse roots. Path
  resolution is jailed to the browse root — traversal attempts 404 exactly
  like the artifact-download path.

PR35 adds run export + share links (F28):

- ``GET /api/runs/{run_id}/export?fmt=json|csv`` — the full run bundle as a
  download: submitted spec, every rubric version (content included), the
  score matrix (:class:`RunResults`, same payload the results page reads),
  the analytics rollup (:class:`RunAnalytics`), and events-lite — summary
  event rows with payloads projected to scalar fields, NOT full bodies.
  ``fmt=csv`` instead flattens to one row per sandbox × score dimension
  (see :func:`_export_csv` for the column doctrine).
- ``POST /api/runs/{run_id}/share`` / ``DELETE .../share`` — mint / revoke
  the run's ``share_token`` (unguessable; idempotent mint).
- ``GET /api/share/{token}`` — the read-only share bundle the
  ``/share/{token}`` page renders: spec + score matrix + events-lite.
  No auth beyond the token itself (the app ships unauthenticated — this
  is the PR19 extension point, not a security boundary).

Pydantic models live here (not api/schemas.py) per Phase-2 ownership split.
"""

from __future__ import annotations

import csv
import difflib
import io
import json
import math
import os
import random
import secrets
import time
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal, NamedTuple
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import FileResponse, Response
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    field_validator,
)
from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from api.config import get_settings
from api.deps import get_db
from api.routers.analytics import get_run_analytics
from api.schemas import (
    AnalysisReport,
    CheckerResultOut,
    RubricOut,
    RunAnalytics,
)
from db.models import (
    Artifact,
    CheckerResult,
    Event,
    PairwiseVote,
    Rubric,
    Run,
    Sandbox,
    Score,
)
from evaluator.scores import OVERALL_DIMENSION, rubric_dimension_scales
from orchestrator import events as orch_events
from orchestrator import prompt_cell, runner, state_machine
from orchestrator.matrix import (
    judge_models,
    run_models,
    sandbox_model,
    trial_index,
)
from orchestrator.rubric import RubricContent, create_rubric_version
from sandbox.preview import (
    ScanCache,
    resolve_artifact_dir,
    resolve_workspace_dir,
)
from sandbox.thumbnails import THUMB_FILENAME, THUMB_META_FILENAME
from scoring.checkers import SUITE_PASS_DIMENSION, fold_dimension_name
from scoring.exec import exec_summary_dict
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


class JudgeScore(BaseModel):
    """One judge's newest verdict on a dimension (PR29 panel).

    ``judge_model`` is the model stamped on the score row — ``None`` for
    checker rows (``suite_pass``) and pre-panel rows whose author resolves
    to the run's primary judge at merge time (the resolved name is
    reported, so ``None`` here truly means "not model-authored").
    """

    judge_model: str | None = None
    score: float
    rationale: str = ""
    source: str = "llm"  # llm | checker


class DimensionScore(BaseModel):
    score: float
    rationale: str = ""
    # PR29 — where the displayed number came from: "llm" = per-judge
    # newest-score MEAN across the panel; "human" = a POST
    # /api/sandboxes/{id}/scores override row, which wins the pair
    # outright; "checker" = deterministic suite_pass measurement.
    source: str = "llm"
    # Free-text context on human overrides (why the score moved).
    note: str | None = None
    # Per-judge newest verdicts feeding the mean (empty only for a human
    # cell that no judge row exists beside) — the UI renders these in the
    # cell tooltip + the "judges said X" line under a human override.
    judges: list[JudgeScore] = Field(default_factory=list)
    # max − min across the contributing judges; ``None`` below 2 judges.
    # The UI badges disagreement above its threshold (>2 pts).
    spread: float | None = None
    judge_count: int = 0


class ExecSummary(BaseModel):
    """Flat exec-signal headline (PR7) — the measured build/test/coverage
    outcome, derived from ``analysis.json``'s ``exec`` signal so UI
    consumers don't re-derive it from nested metrics. ``status`` mirrors
    the SignalReport status: ``ok`` = measured (build/tests may still be
    red), ``skipped``/``error`` = not measured."""

    status: str = "ok"
    build_ok: bool | None = None
    tests_detected: bool = False
    tests_ok: bool | None = None
    tests_collected: int = 0
    tests_passed: int = 0
    tests_failed: int = 0
    tests_errors: int = 0
    tests_skipped: int = 0
    coverage_percent: float | None = None
    timed_out: bool = False
    duration_seconds: float | None = None
    detail: str | None = None  # skip reason / error message


class GeneratedTestsSummary(BaseModel):
    """Flat generated-tests headline (PR8) — the evaluator-authored pytest
    suite's outcome, derived from ``analysis.json``'s ``generated_tests``
    signal. ``status`` mirrors the SignalReport status: ``ok`` = measured
    (the suite may still be red), ``skipped``/``error`` = not measured."""

    status: str = "ok"
    model: str | None = None
    files_written: int = 0
    files_rejected: int = 0
    tests_detected: bool = False
    tests_collected: int = 0
    tests_passed: int = 0
    tests_failed: int = 0
    tests_errors: int = 0
    pass_rate: float | None = None
    coverage_percent: float | None = None
    timed_out: bool = False
    detail: str | None = None  # skip reason / error message


class SuiteSummary(BaseModel):
    """PR26 flat suite headline — the deterministic checker roll-up over
    the sandbox's merged ``checker_results`` rows (newest-wins per check).
    DB-sourced, so it lands even when no judge ever ran — the "per-case
    pass/fail + mean without judge involvement" contract. ``pass_rate``
    excludes skipped/error checks (not measured outcomes); null when
    nothing measured. ``suite_pass`` mirrors the Score dimension ×10."""

    total: int = 0
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    errors: int = 0
    pass_rate: float | None = None
    suite_pass: float | None = None  # the suite_pass Score value (0–10)


class ArtifactRef(BaseModel):
    """Artifact row exposed for the download endpoint."""

    id: UUID
    kind: str
    path: str


class PreviewCandidate(BaseModel):
    """One detected runnable entry — {entry: workspace-relative path, type}."""

    entry: str
    type: str


class ThumbnailRef(BaseModel):
    """PR4 — a captured artifact screenshot; ``url`` is the PNG's GET path."""

    url: str
    captured_at: datetime | None = None
    type: str | None = None
    entry: str | None = None


class SandboxResult(BaseModel):
    """One sandbox's evaluation outcome inside the run matrix."""

    sandbox_id: UUID
    harness_config: dict[str, Any] = Field(default_factory=dict)
    # Config label from the sandbox row (PR1 named harness configs); NULL
    # on sandboxes that predate the column.
    name: str | None = None
    # PR14: the participant model this sandbox ran — the harness_config
    # ``model`` cell on multi-model runs, else the run's ``model_name``.
    model: str = ""
    # PR27: which repeat of its config × model cell this sandbox is
    # (``harness_config.trial_index``; None on untrialed runs).
    trial_index: int | None = None
    status: str
    terminal_reason: str | None = None
    # Monitor flagged a done-ish exit whose snapshot workspace had no real
    # files — the ``empty_workspace`` badge on the results row. Sourced from
    # the stored checkpoint payloads (``sandbox_finalized`` key /
    # ``stage="empty_workspace"`` warning — same markers analytics keys
    # on), never an FS probe.
    empty_workspace: bool = False
    artifact_path: str | None = None
    rubric_id: UUID | None = None
    # PR5: version of the rubric the displayed scores were judged against
    # (newest contributing score row).
    rubric_version: int | None = None
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
    # PR4: a captured artifact screenshot (``<artifact>/thumbnail.png``)
    # when present — the UI renders it small; click spawns a full preview.
    # None when never captured. Generation is explicit-only (POST
    # .../thumbnail) — this GET never spawns anything.
    thumbnail: ThumbnailRef | None = None
    # PR6: objective signals measured on the workspace before judging
    # (``<artifact>/analysis.json``). None when the evaluator ran without
    # signal collection or the report is unreadable.
    analysis: AnalysisReport | None = None
    # PR7: flat view of the ``exec`` signal (build/test/coverage measured
    # in the locked heg-exec container) — None when no analysis exists.
    exec: ExecSummary | None = None
    # PR8: flat view of the ``generated_tests`` signal (LLM-written suite
    # executed against the submission) — None when no analysis exists.
    generated_tests: GeneratedTestsSummary | None = None
    # PR26: deterministic checker outcomes for suite-task runs — pass /
    # fail / skipped / error beside the judge scores. Newest-wins per
    # (check_type, spec), mirroring the score merge; empty for non-suite
    # runs and suites without ``check:`` blocks.
    checker_results: list[CheckerResultOut] = Field(default_factory=list)
    # PR26: flat roll-up of those rows — counts + pass rate (+ the
    # ``suite_pass`` score value). None for non-suite runs.
    suite: SuiteSummary | None = None


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
    # PR14: the run's full participant-model set — [model_name] + extras +
    # per-config pins — so the UI can render multi-model runs without
    # re-deriving the matrix.
    models: list[str] = Field(default_factory=list)
    evaluator_model: str | None = None
    # PR29 — the full judge panel ([evaluator_model] on single-judge
    # runs — always ≥1 for a well-formed run); evaluator_model = panel[0].
    evaluator_models: list[str] = Field(default_factory=list)
    same_model_eval: bool | None = None
    # PR24: the task-input seed spec echoed from run.config (a repo_url+ref
    # or an upload archive_id), or None for unseeded runs. The per-cell
    # proof rides each SandboxResult.harness_config — ``seed_sha`` is the
    # identical hash across the matrix; ``seed`` its provenance.
    task_inputs: dict[str, Any] | None = None
    # PR27: repeats-per-cell submitted on this run (1 = single-shot) — the
    # results matrix groups trial cells under one config × model header.
    trials: int = 1
    # PR5: the ?rubric_version= filter echoed back (None = newest-wins view).
    selected_rubric_version: int | None = None
    # Every rubric version of the run + score counts — drives the UI's
    # version selector without a second request.
    rubric_versions: list[RubricVersionOut] = Field(default_factory=list)
    # PR29 — blind A/B roll-up over pairwise_votes: per-sandbox win rate +
    # judge-vs-human agreement. None until the run has votes.
    votes: VotesSummary | None = None
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
    # PR29 provenance — judge model for llm rows (NULL pre-panel); the
    # row's author class (llm | human | checker); human override context.
    judge_model: str | None = None
    source: str = "llm"
    note: str | None = None
    created_at: datetime


class HumanScoreIn(BaseModel):
    """POST /api/sandboxes/{id}/scores body — a human override verdict on
    one dimension (PR29).

    The row lands with ``source="human"`` + ``seq`` above the sandbox's
    current max, so it wins the newest-wins merge for its (sandbox,
    dimension) pair regardless of what judge rows exist. Rubric
    provenance stamps the run's latest approved rubric — "the judging
    context the human was correcting" — while the merge keeps the row
    visible in every ``?rubric_version=`` view (the correction is
    version-agnostic).
    """

    dimension: Annotated[str, StringConstraints(strip_whitespace=True)] = Field(
        min_length=1, max_length=128
    )
    score: float
    rationale: str = Field(default="", max_length=10_000)
    note: Annotated[str, StringConstraints(strip_whitespace=True)] | None = Field(
        default=None, max_length=2000
    )

    @field_validator("score")
    @classmethod
    def _finite_score(cls, v: float) -> float:
        # Scale is rubric-defined (0–10 MVP, arbitrary in PR23 rubrics) —
        # only require a finite real number.
        if not math.isfinite(v):
            raise ValueError("score must be finite")
        return v


class VoteStats(BaseModel):
    """One sandbox's blind-A/B tally (PR29). ``win_rate`` counts decided
    votes only (wins / (wins+losses) — ties excluded); ``None`` when the
    sandbox has no decided votes yet."""

    wins: int = 0
    losses: int = 0
    ties: int = 0
    win_rate: float | None = None


class VotesSummary(BaseModel):
    """Blind A/B roll-up for the results page (PR29).

    ``total`` = merged vote count (newest vote per (unordered pair,
    dimension) wins — a re-vote on the same pair/dim replaces rather than
    double-counts). ``by_sandbox`` keys on sandbox id. ``compared`` /
    ``agreed`` / ``agreement_rate`` measure judge-vs-human agreement: of
    the votes where BOTH sides carry a judge mean on the voted dimension,
    how often the judge's ranking matches the human's pick.
    """

    total: int = 0
    by_sandbox: dict[str, VoteStats] = Field(default_factory=dict)
    compared: int = 0
    agreed: int = 0
    agreement_rate: float | None = None


class PairCreateIn(BaseModel):
    """POST /api/runs/{id}/pair body — optionally pin which two sandboxes
    face off (both must be artifact-bearing members of the run). Omitted
    → the server picks two at random."""

    sandbox_ids: list[UUID] | None = Field(default=None, min_length=2, max_length=2)


class PairSideOut(BaseModel):
    """One anonymized side of a pair session. ``root`` mirrors the file
    browser's browse-root kind ("workspace" | "artifact") — under
    anonymization the artifact root exposes ONLY ``output.txt`` (prompt
    submissions), never meta.json/transcript/analysis.
    """

    label: str  # "A" | "B"
    root: str
    entries: list[FileEntry] = Field(default_factory=list)
    truncated: bool = False


class PairOut(BaseModel):
    """201 response to POST /api/runs/{id}/pair — an anonymized A/B view.

    Deliberately carries NO sandbox ids/config/model names: the pair_id
    is the only handle the reviewer (and the vote endpoint) needs.
    """

    pair_id: str
    run_id: UUID
    # Rubric dimension names + "overall" — the reviewer's vote menu.
    dimensions: list[str] = Field(default_factory=list)
    sides: list[PairSideOut] = Field(default_factory=list)


class PairFileOut(BaseModel):
    """Anonymized read of one pair-side file — ``side`` is the label from
    the pair session, never the sandbox id."""

    pair_id: str
    side: str
    path: str
    size: int
    truncated: bool = False
    binary: bool = False
    text: str | None = None


class PairVoteIn(BaseModel):
    """POST /api/runs/{id}/pair/{pair_id}/vote body — the reviewer picks
    a side per dimension. ``winner`` is the A/B label (or "tie"); the
    stored row resolves it to left/right sandbox ids server-side."""

    dimension: Annotated[str, StringConstraints(strip_whitespace=True)] = Field(
        min_length=1, max_length=128
    )
    winner: Annotated[str, StringConstraints(strip_whitespace=True)] = Field(
        min_length=1, max_length=8
    )
    note: Annotated[str, StringConstraints(strip_whitespace=True)] | None = Field(
        default=None, max_length=2000
    )


class PairwiseVoteOut(BaseModel):
    """One recorded blind-A/B vote. Returning the REAL sandbox ids is
    deliberate — the reveal happens post-vote, and the results summary
    needs them for per-sandbox win rates."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    run_id: UUID
    left_sandbox: UUID
    right_sandbox: UUID
    dimension: str
    winner: str  # left | right | tie — left := the sandbox behind "A"
    note: str
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


def _load_thumbnail(jail: Path, sandbox: Sandbox) -> ThumbnailRef | None:
    """Read ``<artifact>/thumbnail.{png,json}`` (PR4), jailed — cheap FS
    probe per sandbox; the PNG's existence gates the ref and the JSON
    sidecar supplies captured_at/type/entry (best-effort)."""
    artifact_dir = resolve_artifact_dir(
        jail=jail,
        run_id=str(sandbox.run_id),
        sandbox_id=str(sandbox.id),
        artifact_path=sandbox.artifact_path,
    )
    if artifact_dir is None:
        return None
    try:
        png = (artifact_dir / THUMB_FILENAME).resolve()
    except OSError:
        return None
    try:
        if not (png == jail or jail in png.parents) or not png.is_file():
            return None
    except OSError:
        return None
    meta: dict[str, Any] = {}
    try:
        meta_path = (artifact_dir / THUMB_META_FILENAME).resolve()
        if (
            (meta_path == jail or jail in meta_path.parents)
            and meta_path.is_file()
        ):
            raw = json.loads(
                meta_path.read_text(encoding="utf-8", errors="replace")
            )
            if isinstance(raw, dict):
                meta = raw
    except Exception:  # noqa: BLE001 — meta is best-effort decoration
        meta = {}
    captured_at: datetime | None = None
    raw_ts = meta.get("captured_at")
    if isinstance(raw_ts, str):
        try:
            captured_at = datetime.fromisoformat(raw_ts)
        except ValueError:
            captured_at = None
    return ThumbnailRef(
        url=f"/api/runs/{sandbox.run_id}/sandboxes/{sandbox.id}/thumbnail",
        captured_at=captured_at,
        type=meta.get("type") if isinstance(meta.get("type"), str) else None,
        entry=meta.get("entry") if isinstance(meta.get("entry"), str) else None,
    )


def _exec_summary(analysis: AnalysisReport | None) -> ExecSummary | None:
    """Flatten ``analysis``'s exec signal into a :class:`ExecSummary`.

    None when there's no analysis or no exec signal at all; skipped/error
    signals surface their status + a short detail (skip reason or error).
    Malformed metric values degrade to the bare status — analysis.json is
    persisted candidate-adjacent data, so summary coercion never 500s.
    """
    if analysis is None:
        return None
    sig = analysis.signal("exec")
    if sig is None:
        return None
    detail = sig.error or (sig.warnings[0] if sig.warnings else None)
    try:
        return ExecSummary.model_validate(
            {"status": sig.status, "detail": detail}
            | exec_summary_dict(sig.metrics)
        )
    except ValidationError:
        return ExecSummary(
            status=sig.status, detail=detail or "malformed exec metrics"
        )


def _generated_tests_summary(
    analysis: AnalysisReport | None,
) -> GeneratedTestsSummary | None:
    """Flatten ``analysis``'s ``generated_tests`` signal (PR8) — same
    contract as :func:`_exec_summary`."""
    if analysis is None:
        return None
    sig = analysis.signal("generated_tests")
    if sig is None:
        return None
    m = sig.metrics
    detail = sig.error or (sig.warnings[0] if sig.warnings else None)
    try:
        return GeneratedTestsSummary(
            status=sig.status,
            model=m.get("model"),
            files_written=int(m.get("files_written") or 0),
            files_rejected=int(m.get("files_rejected") or 0),
            tests_detected=bool(m.get("tests_detected")),
            tests_collected=int(m.get("tests_collected") or 0),
            tests_passed=int(m.get("tests_passed") or 0),
            tests_failed=int(m.get("tests_failed") or 0),
            tests_errors=int(m.get("tests_errors") or 0),
            pass_rate=m.get("pass_rate"),
            coverage_percent=m.get("coverage_percent"),
            timed_out=bool(m.get("timed_out")),
            detail=detail,
        )
    except (ValidationError, TypeError, ValueError):
        return GeneratedTestsSummary(
            status=sig.status,
            detail=detail or "malformed generated_tests metrics",
        )


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


def _empty_workspace_marker(event_type: str, payload: dict[str, Any]) -> bool:
    """Monitor's file-less-workspace markers (orchestrator/monitor.py) —
    ``empty_workspace`` on the ``sandbox_finalized`` checkpoint payload, or
    the dedicated ``stage="empty_workspace"`` warning checkpoint. Same
    detection the analytics row uses; the results flag reads the stored
    events, never the artifact FS."""
    return bool(payload.get("empty_workspace")) or (
        event_type == "checkpoint"
        and payload.get("stage") == "empty_workspace"
    )


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


def _score_bucket(sc: Score, primary_judge: str | None) -> tuple[str, str | None]:
    """Merge-bucket identity of one score row (PR29).

    The newest-wins merge now keys on (source, judge) instead of the
    dimension alone — a judge panel writes one row per judge per
    dimension and the cell merges to their MEAN, so each judge's newest
    row must survive:

    - ``human`` overrides share ONE bucket regardless of author — the
      newest human post wins the pair outright.
    - ``checker`` rows (deterministic ``suite_pass``) are their own
      bucket — no model authored them.
    - ``llm`` rows bucket by ``judge_model``; a NULL judge (pre-PR29 or
      unstamped rows) resolves to the run's primary judge — the only
      model that could have written them — so they merge into the
      primary's bucket rather than a phantom "unknown" judge.
    """
    if sc.source == "human":
        return ("human", None)
    if sc.source == "checker":
        return ("checker", None)
    return ("llm", sc.judge_model or primary_judge)


def _dimension_entry(
    buckets: dict[tuple[str, str | None], Score],
    *,
    newest: Score,
) -> DimensionScore:
    """Fold one dimension's per-bucket newest rows into the cell value.

    ``newest`` is the dimension's latest row overall — its rationale is
    the headline rationale for non-human merges (the most recent
    contributing verdict, matching pre-PR29 last-wins display semantics).
    A human bucket wins the pair outright (its score/rationale/note
    display) but the judges' verdicts stay attached for the "what judges
    said" tooltip + vote-agreement stat.
    """
    human = buckets.get(("human", None))
    winners = sorted(
        ((k, w) for k, w in buckets.items() if k[0] != "human"),
        key=lambda kw: (kw[0][1] or "", kw[0][0]),
    )
    judges = [
        JudgeScore(
            # The bucket key already carries the resolved judge name —
            # NULL judge_model on an llm row folded to the primary judge
            # at bucket time; only checker rows truly report None.
            judge_model=(k[1] if k[0] == "llm" else None),
            score=w.score,
            rationale=w.rationale or "",
            source=w.source or "llm",
        )
        for k, w in winners
    ]
    spread = (
        round(
            max(w.score for _, w in winners)
            - min(w.score for _, w in winners),
            2,
        )
        if len(winners) >= 2
        else None
    )
    if human is not None:
        return DimensionScore(
            score=human.score,
            rationale=human.rationale or "",
            source="human",
            note=human.note,
            judges=judges,
            spread=spread,
            judge_count=len(winners),
        )
    mean = sum(w.score for _, w in winners) / len(winners)
    return DimensionScore(
        score=round(mean, 2),
        rationale=newest.rationale or "",
        source=(
            "checker"
            if all(w.source == "checker" for _, w in winners)
            else "llm"
        ),
        judges=judges,
        spread=spread,
        judge_count=len(winners),
    )


class _MergedScores(NamedTuple):
    """One sandbox's merged score state for the results matrix."""

    dimensions: dict[str, DimensionScore]
    overall: DimensionScore | None
    suite_pass: float | None
    # dim → mean across LLM judges (human overrides excluded — the
    # judge-vs-human agreement stat needs the JUDGE number even on
    # overridden cells).
    judge_means: dict[str, float]


def _merge_dimensions(
    rows: list[tuple[str, Score]], *, primary_judge: str | None
) -> _MergedScores:
    """Newest-wins-per-bucket merge of one sandbox's score rows.

    ``rows`` carry the display dimension and arrive ordered newest-last
    (created_at, seq, rubric_version, id). Per dimension, the last row per
    (source, judge) bucket survives; the cell then resolves as human >
    judge-mean (see ``_dimension_entry``).
    """
    dims: dict[str, dict[tuple[str, str | None], Score]] = {}
    newest_of: dict[str, Score] = {}
    for dim, sc in rows:
        dims.setdefault(dim, {})[_score_bucket(sc, primary_judge)] = sc
        # Every row overwrites its own bucket, so the dimension's last
        # row is always one of the surviving winners.
        newest_of[dim] = sc
    dimensions: dict[str, DimensionScore] = {}
    overall: DimensionScore | None = None
    suite_pass: float | None = None
    judge_means: dict[str, float] = {}
    for dim, buckets in dims.items():
        entry = _dimension_entry(buckets, newest=newest_of[dim])
        llm_scores = [j.score for j in entry.judges if j.source == "llm"]
        if llm_scores:
            judge_means[dim] = sum(llm_scores) / len(llm_scores)
        if dim == OVERALL_DIMENSION:
            overall = entry
        else:
            dimensions[dim] = entry
            if dim == SUITE_PASS_DIMENSION:
                suite_pass = entry.score
    return _MergedScores(
        dimensions=dimensions,
        overall=overall,
        suite_pass=suite_pass,
        judge_means=judge_means,
    )


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
    rubric_dimensions = {
        r.version: set(rubric_dimension_scales(r.content)) for r in rubrics
    }
    declared_dimensions = set().union(*rubric_dimensions.values())

    def score_version(score: Score) -> int | None:
        if score.rubric_version is not None:
            return score.rubric_version
        return version_of.get(score.rubric_id) if score.rubric_id is not None else None

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
        # Merge order is last-wins per dimension. created_at is only
        # second-precision on sqlite, so ties break on seq — the durable
        # insertion order stamped per eval pass (a re-score pinned to an
        # OLDER rubric version still wins via its higher seq). NULL seq =
        # unstamped rows, sorted first; rubric_version is the final
        # semantic tiebreak for them.
        .order_by(
            Score.created_at.asc(),
            Score.seq.asc().nulls_first(),
            Score.rubric_version.asc().nulls_first(),
            Score.id,
        )
    )
    if rubric_version is not None:
        # Strict provenance filter — also catches rows whose rubric_id maps
        # to this version but whose denormalized rubric_version was never
        # stamped (pre-backfill/manual inserts). PR29: human overrides are
        # version-AGNOSTIC corrections — they stay visible in every
        # pinned-version view (the human's adjustment to a cell stands
        # regardless of which rubric version produced the judge scores).
        matching_ids = [rid for rid, ver in version_of.items() if ver == rubric_version]
        score_stmt = score_stmt.where(
            or_(
                Score.rubric_version == rubric_version,
                Score.rubric_id.in_(matching_ids),
                Score.source == "human",
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

    # PR26 checker results — newest-wins per (check_type, spec): the rows
    # are append-only (a rescore re-measures and inserts), and the autoinc
    # id is the durable insertion order, so the last row per key wins.
    checker_rows = (
        session.execute(
            select(CheckerResult)
            .where(CheckerResult.sandbox_id.in_(sandbox_ids))
            .order_by(CheckerResult.id)
        )
        .scalars()
        .all()
        if sandbox_ids
        else []
    )
    checker_by_sandbox: dict[UUID, dict[tuple[str, str], CheckerResult]] = {}
    for cr in checker_rows:
        key = (cr.check_type, json.dumps(cr.spec or {}, sort_keys=True))
        checker_by_sandbox.setdefault(cr.sandbox_id, {})[key] = cr

    # Flat suite roll-up per sandbox — the card's "per-case pass/fail +
    # mean without judge involvement": computed from the DB rows, so it
    # exists even when analysis.json/the judge never did.
    suite_by_sandbox: dict[UUID, dict[str, Any]] = {}
    for sid, merged in checker_by_sandbox.items():
        rows = list(merged.values())
        passed = sum(1 for r in rows if r.status == "pass")
        failed = sum(1 for r in rows if r.status == "fail")
        measured = passed + failed
        suite_by_sandbox[sid] = {
            "total": len(rows),
            "passed": passed,
            "failed": failed,
            "skipped": sum(1 for r in rows if r.status == "skipped"),
            "errors": sum(1 for r in rows if r.status == "error"),
            "pass_rate": (passed / measured) if measured else None,
        }

    # Eval-failure events — latest label per sandbox. Only surfaced on
    # sandboxes with NO Score rows: a retry that eventually produced scores
    # supersedes its earlier failed attempts.
    eval_failures: dict[UUID, str] = {}
    # Sandboxes the monitor finalized on a file-less workspace — feeds the
    # ``empty_workspace`` row flag. Same stored-event markers the analytics
    # row keys on (finalized payload + warning checkpoint); no FS re-probe.
    empty_ws: set[UUID] = set()
    if sandbox_ids:
        ev_rows = session.execute(
            select(Event.sandbox_id, Event.type, Event.payload)
            .where(
                Event.run_id == run_id,
                Event.sandbox_id.in_(sandbox_ids),
                Event.type.in_(("error", "checkpoint")),
            )
            .order_by(Event.id)
        ).all()
        for sid, ev_type, payload in ev_rows:
            if sid is None or not isinstance(payload, dict):
                continue
            if ev_type == "error":
                label = _eval_error_label(payload)
                if label is not None:
                    eval_failures[sid] = label
            if _empty_workspace_marker(ev_type, payload):
                empty_ws.add(sid)

    jail = _artifact_jail()
    scan_ttl = get_settings().PREVIEW_SCAN_TTL_SECONDS
    cfg = run.config or {}
    # PR29 — the run's judge panel; panel[0] is the primary judge NULL
    # judge_model rows resolve into (pre-panel rows were authored by it).
    panel = judge_models(cfg, run.model_name)
    primary_judge = panel[0] if panel else None
    judge_means_by_sandbox: dict[UUID, dict[str, float]] = {}
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
        analysis = _load_analysis(jail, sb)
        rubric_id: UUID | None = None
        sb_rubric_version: int | None = None
        sb_scores = by_sandbox.get(sb.id, [])
        for sc in sb_scores:
            if sc.rubric_id is not None:
                rubric_id = sc.rubric_id  # rows ordered asc -> newest wins
            row_version = score_version(sc)
            if row_version is not None:
                sb_rubric_version = row_version
        visible_scores: list[tuple[str, Score]] = []
        for sc in sb_scores:
            dim = sc.dimension
            if dim not in {OVERALL_DIMENSION, SUITE_PASS_DIMENSION}:
                folded = fold_dimension_name(dim)
                if sc.source == "llm":
                    original_dimensions = rubric_dimensions.get(score_version(sc) or 0)
                    if original_dimensions:
                        if folded not in original_dimensions:
                            continue
                        dim = folded
                elif sc.source == "human" and folded in declared_dimensions:
                    dim = folded
            visible_scores.append((dim, sc))
        # PR29 — per-(source,judge) buckets: human wins its pair; llm
        # buckets merge to the panel mean + spread.
        merged = _merge_dimensions(visible_scores, primary_judge=primary_judge)
        judge_means_by_sandbox[sb.id] = merged.judge_means
        suite_pass_score = merged.suite_pass
        suite_block = suite_by_sandbox.get(sb.id)
        results.append(
            SandboxResult(
                sandbox_id=sb.id,
                harness_config=sb.harness_config or {},
                name=sb.name,
                model=sandbox_model(sb.harness_config or {}, run.model_name),
                trial_index=trial_index(sb.harness_config),
                status=sb.status,
                terminal_reason=sb.terminal_reason,
                empty_workspace=sb.id in empty_ws,
                artifact_path=sb.artifact_path,
                rubric_id=rubric_id,
                rubric_version=sb_rubric_version,
                dimensions=merged.dimensions,
                overall=merged.overall,
                eval_error=(
                    # A suite_pass row is a checker measurement, not a
                    # judge verdict — it must not mask "judge failed".
                    None
                    if any(
                        sc.dimension != SUITE_PASS_DIMENSION
                        for sc in visible_scores
                    )
                    else eval_failures.get(sb.id)
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
                thumbnail=_load_thumbnail(jail, sb),
                analysis=analysis,
                exec=_exec_summary(analysis),
                generated_tests=_generated_tests_summary(analysis),
                checker_results=[
                    CheckerResultOut(
                        check=cr.check_type,
                        spec=cr.spec or {},
                        status=cr.status,
                        passed=bool(cr.passed),
                        detail=cr.detail or "",
                        created_at=cr.created_at,
                    )
                    for cr in checker_by_sandbox.get(sb.id, {}).values()
                ],
                suite=(
                    SuiteSummary(**suite_block, suite_pass=suite_pass_score)
                    if suite_block is not None
                    else None
                ),
            )
        )

    counts = _score_counts_by_rubric(session, run_id)
    return RunResults(
        run_id=run.id,
        status=run.status,
        model_name=run.model_name,
        trials=getattr(run, "trials", None) or cfg.get("trials") or 1,
        models=run_models(cfg, run.model_name),
        evaluator_model=cfg.get("evaluator_model"),
        evaluator_models=panel,
        same_model_eval=cfg.get("same_model_eval"),
        task_inputs=cfg.get("task_inputs") or None,
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
        votes=_votes_summary(session, run_id, judge_means_by_sandbox, declared_dimensions),
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
            .order_by(
                Score.created_at.asc(),
                Score.seq.asc().nulls_first(),
                Score.id,
            )
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
    if run.status == "cancelled":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="run is 'cancelled' — nothing was ever executed to re-score",
        )
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
            "judge_models": (
                [model] if model else judge_models(run.config, run.model_name)
            ),
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


#: Names never shipped in workspace ZIPs — VCS internals, python caches,
#: vendored deps, virtualenvs. Pruned at ANY depth (a vendored repo's own
#: ``.git`` is as unwanted as the root one). Download-side only: raw
#: artifact paths/files are untouched.
_ZIP_EXCLUDED_DIRS = frozenset({".git", "__pycache__", "node_modules", ".venv"})


def _zip_dir(dirpath: Path) -> bytes:
    # os.walk doesn't descend into symlinked dirs (followlinks=False) — the
    # snapshotted workspace is candidate-authored, so rglob could escape the
    # jail through a planted symlink. File symlinks are skipped outright.
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(dirpath):
            # Prune excluded dirs in-place so walk never descends into them.
            dirs[:] = sorted(d for d in dirs if d not in _ZIP_EXCLUDED_DIRS)
            for name in sorted(files):
                # A stray `.git` FILE (worktree/submodule pointer) + *.pyc
                # bytecode are noise as much as the pruned dirs.
                if name in _ZIP_EXCLUDED_DIRS or name.endswith(".pyc"):
                    continue
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


# ---------------------------------------------------------------------------
# PR34 — artifact file browser + side-by-side diff
# ---------------------------------------------------------------------------

#: Browse bounds — the artifact workspace is candidate-authored, so every
#: probe is bounded: listing walks at most ``_BROWSE_MAX_DEPTH`` levels and
#: ``_BROWSE_MAX_ENTRIES`` rows (``truncated`` flags the cut), a text read
#: caps at ``_FILE_TEXT_CAP_BYTES``, and a diff caps each side's input at
#: ``_DIFF_SIDE_CAP_BYTES`` + the rendered output at ``_DIFF_MAX_LINES``.
_BROWSE_MAX_DEPTH = 8
_BROWSE_MAX_ENTRIES = 5000
_FILE_TEXT_CAP_BYTES = 256 * 1024
_DIFF_SIDE_CAP_BYTES = 1024 * 1024
_DIFF_MAX_LINES = 10_000


class FileEntry(BaseModel):
    """One browsable path inside the artifact root — ``path`` is POSIX-style
    relative; ``size`` is None for dirs."""

    path: str
    kind: str  # "file" | "dir"
    size: int | None = None


class FileListOut(BaseModel):
    """Bounded listing for one sandbox's artifact browse root."""

    sandbox_id: UUID
    # "workspace" — the snapshot workspace dir; "artifact" — the artifact
    # dir itself (prompt cells + workspace-less artifacts: output.txt,
    # transcript.log, meta.json, analysis.json, generated_tests/…).
    root: str
    entries: list[FileEntry] = Field(default_factory=list)
    truncated: bool = False


class FileContentOut(BaseModel):
    """Size-capped text read of one artifact file — ``text`` is None when
    the content is binary (NUL bytes in the read head); the UI offers the
    ``?download=1`` variant instead."""

    sandbox_id: UUID
    path: str
    size: int
    truncated: bool = False
    binary: bool = False
    text: str | None = None


class FileDiffOut(BaseModel):
    """Server-side unified diff of one path across two sandboxes' browse
    roots (``sandbox_id`` = ``a``/left, ``other_sandbox_id`` = ``b``/right).

    A path absent on one side diffs against empty (all +/-) — a path
    existing on NEITHER side 404s before this is built. ``binary`` means a
    side carried NUL bytes; ``diff`` stays empty then (the UI offers
    downloads). ``truncated`` covers an over-cap input side OR an output
    longer than ``_DIFF_MAX_LINES``.
    """

    sandbox_id: UUID
    other_sandbox_id: UUID
    path: str
    a_missing: bool = False
    b_missing: bool = False
    binary: bool = False
    identical: bool = False
    truncated: bool = False
    diff: str = ""


def _browse_root(jail: Path, sandbox: Sandbox) -> tuple[Path, str] | None:
    """The browsable root for a sandbox's artifact, jailed under ARTIFACT_ROOT.

    ``workspace/`` wins when present (the snapshot the agent produced); a
    workspace-less artifact (prompt cells — ``output.txt`` at the artifact
    root — or pre-snapshot failures) browses the artifact dir itself so the
    submission + transcript/meta stay inspectable. Returns ``(root, kind)``
    or None when no artifact dir resolves inside the jail.
    """
    ws = resolve_workspace_dir(
        jail=jail,
        run_id=str(sandbox.run_id),
        sandbox_id=str(sandbox.id),
        artifact_path=sandbox.artifact_path,
    )
    if ws is not None:
        return ws, "workspace"
    art = resolve_artifact_dir(
        jail=jail,
        run_id=str(sandbox.run_id),
        sandbox_id=str(sandbox.id),
        artifact_path=sandbox.artifact_path,
    )
    if art is not None:
        return art, "artifact"
    return None


def _resolve_in_root(root: Path, rel: str) -> tuple[Path, str] | None:
    """Jailed join — ``rel`` must resolve to a regular file inside ``root``.

    Returns ``(absolute, posix_relative)``. ``..`` segments escaping the
    root, absolute paths that escape, nonexistent paths, and dirs all
    return None → the endpoint 404s exactly like the artifact-download jail.
    A ``..`` that stays inside the root (``src/../index.html``) resolves
    fine — it never escapes the jail.
    """
    rel = rel.strip("/")
    if not rel:
        return None
    try:
        resolved = (root / rel).resolve()
    except (OSError, ValueError):
        return None
    if resolved != root and root not in resolved.parents:
        return None
    try:
        if not resolved.is_file():
            return None
    except (OSError, ValueError):
        return None
    return resolved, resolved.relative_to(root).as_posix()


def _browsable_file(dirpath: str, name: str) -> int | None:
    """Size of a listable artifact file — ``None`` when the listing
    predicate rejects it (noise name, symlink, non-regular file, or a
    stat that fails). Shared by the emit loop and the depth cutoff so
    ``truncated`` flags only when a browsable entry is actually cut."""
    if name in _ZIP_EXCLUDED_DIRS or name.endswith(".pyc"):
        return None
    p = Path(dirpath) / name
    if p.is_symlink() or not p.is_file():
        return None
    try:
        return p.stat().st_size
    except OSError:
        return None


def _list_root_entries(root: Path) -> tuple[list[FileEntry], bool]:
    """Depth/entry-bounded walk of ``root`` → ``(entries, truncated)``.

    Same exclusions as the workspace ZIP (``.git``/``__pycache__``/
    ``node_modules``/``.venv`` dirs at any depth, ``*.pyc`` files) — the
    browser shows the artifact, not its noise. Symlinks are never followed
    or listed (candidate content could point outside the jail). A scandir
    failure (a chmod-0 candidate dir survives ``docker cp`` tar
    extraction) flags ``truncated`` — an unlistable subtree must not
    vanish silently.
    """
    entries: list[FileEntry] = []
    truncated = False

    def _walk_failed(_err: OSError) -> None:
        nonlocal truncated
        truncated = True

    for dirpath, dirnames, filenames in os.walk(root, onerror=_walk_failed):
        rel_dir = Path(dirpath).relative_to(root)
        depth = len(rel_dir.parts)
        if depth > 0:
            if len(entries) >= _BROWSE_MAX_ENTRIES:
                truncated = True
                break
            entries.append(
                FileEntry(path=rel_dir.as_posix(), kind="dir", size=None)
            )
        # Prune noise dirs at any depth + symlinked dirs (never descended,
        # but keep them out of the walk for clarity).
        dirnames[:] = sorted(
            d
            for d in dirnames
            if d not in _ZIP_EXCLUDED_DIRS
            and not (Path(dirpath) / d).is_symlink()
        )
        if depth >= _BROWSE_MAX_DEPTH:
            if dirnames or any(
                _browsable_file(dirpath, name) is not None
                for name in filenames
            ):
                truncated = True
            dirnames[:] = []
            continue
        for name in sorted(filenames):
            size = _browsable_file(dirpath, name)
            if size is None:
                continue
            if len(entries) >= _BROWSE_MAX_ENTRIES:
                truncated = True
                break
            rel = name if depth == 0 else (rel_dir / name).as_posix()
            entries.append(FileEntry(path=rel, kind="file", size=size))
        if truncated:
            break
    entries.sort(key=lambda e: e.path)
    return entries[:_BROWSE_MAX_ENTRIES], truncated


def _read_text_capped(path: Path) -> tuple[str | None, bool, int]:
    """``(text, truncated, size)`` — ``text`` is None when the read head
    carries NUL bytes (binary). Reads at most ``_FILE_TEXT_CAP_BYTES + 1``;
    a mid-codepoint cut decodes with ``errors="replace"`` so a large text
    file is never mislabeled binary. A file that can't be statted/read
    404s like a missing one — a chmod-0 candidate file must not escape as
    a 500."""
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            data = fh.read(_FILE_TEXT_CAP_BYTES + 1)
    except OSError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="file not found"
        )
    truncated = len(data) > _FILE_TEXT_CAP_BYTES
    body = data[:_FILE_TEXT_CAP_BYTES]
    if b"\x00" in body:
        return None, truncated, size
    return body.decode("utf-8", errors="replace"), truncated, size


def _diff_side(
    file_path: Path | None,
) -> tuple[list[str] | None, bool, bool, bytes | None]:
    """One side of a file diff → ``(lines, binary, truncated_input, body)``.

    ``file_path`` None (absent on that side) → empty side — the diff
    renders the other side as all +/-. A NUL-carrying side flips
    ``binary`` so the caller returns no diff at all instead of garbage.
    An unreadable side propagates OSError — the caller 404s rather than
    diffing a present file as absent. ``body`` is the raw capped bytes so
    ``identical`` can require true byte equality, not just equal decoded
    heads.
    """
    if file_path is None:
        return None, False, False, None
    with file_path.open("rb") as fh:
        data = fh.read(_DIFF_SIDE_CAP_BYTES + 1)
    truncated = len(data) > _DIFF_SIDE_CAP_BYTES
    body = data[:_DIFF_SIDE_CAP_BYTES]
    if b"\x00" in body:
        return None, True, truncated, body
    return body.decode("utf-8", errors="replace").splitlines(), False, truncated, body


def _file_diff(
    session: Session,
    sandbox: Sandbox,
    root: Path,
    other_id: UUID,
    rel: str,
) -> FileDiffOut:
    """``?diff=<other>`` — unified diff of ``rel`` across both browse roots."""
    other = session.get(Sandbox, other_id)
    if other is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="other sandbox not found",
        )
    other_resolved = _browse_root(_artifact_jail(), other)
    other_root = other_resolved[0] if other_resolved is not None else None

    a_found = _resolve_in_root(root, rel)
    b_found = _resolve_in_root(other_root, rel) if other_root is not None else None
    if a_found is None and b_found is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="file not found on either sandbox",
        )
    resolved = a_found or b_found
    assert resolved is not None  # the 404 above guarantees one side
    rel_posix = resolved[1]

    try:
        a_lines, a_binary, a_trunc, a_body = _diff_side(
            a_found[0] if a_found else None
        )
        b_lines, b_binary, b_trunc, b_body = _diff_side(
            b_found[0] if b_found else None
        )
    except OSError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="file unreadable on one side",
        )

    common = {
        "sandbox_id": sandbox.id,
        "other_sandbox_id": other_id,
        "path": rel_posix,
        "a_missing": a_found is None,
        "b_missing": b_found is None,
    }
    if a_binary or b_binary:
        return FileDiffOut(
            **common, binary=True, truncated=a_trunc or b_trunc
        )

    identical = (
        a_body is not None
        and b_body is not None
        and not a_trunc
        and not b_trunc
        and a_body == b_body
    )
    diff_text = ""
    out_truncated = a_trunc or b_trunc
    if not identical:
        lines = list(
            difflib.unified_diff(
                a_lines or [],
                b_lines or [],
                fromfile=f"a/{rel_posix}",
                tofile=f"b/{rel_posix}",
                n=3,
                lineterm="",
            )
        )
        if len(lines) > _DIFF_MAX_LINES:
            lines = lines[:_DIFF_MAX_LINES]
            out_truncated = True
        diff_text = "\n".join(lines)
    return FileDiffOut(
        **common,
        identical=identical,
        truncated=out_truncated,
        diff=diff_text,
    )


@router.get("/api/sandboxes/{sandbox_id}/files", response_model=FileListOut)
def list_sandbox_files(sandbox_id: UUID, session: DbSession) -> FileListOut:
    """PR34 — jailed, depth/entry-bounded listing of the sandbox artifact.

    Root = ``workspace/`` when the snapshot exists, else the artifact dir
    (prompt cells: ``output.txt`` + transcript + meta live there).
    """
    sandbox = session.get(Sandbox, sandbox_id)
    if sandbox is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="sandbox not found"
        )
    resolved = _browse_root(_artifact_jail(), sandbox)
    if resolved is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="sandbox has no browsable artifact",
        )
    root, kind = resolved
    entries, truncated = _list_root_entries(root)
    return FileListOut(
        sandbox_id=sandbox_id, root=kind, entries=entries, truncated=truncated
    )


@router.get("/api/sandboxes/{sandbox_id}/files/{path:path}", response_model=None)
def get_sandbox_file(
    sandbox_id: UUID,
    path: str,
    session: DbSession,
    download: bool = False,
    diff: UUID | None = None,
) -> Response | FileContentOut | FileDiffOut:
    """PR34 — read one browsed file, or diff it across sandboxes.

    Default → :class:`FileContentOut` (size-capped text, ``binary`` flag on
    undecodable content). ``?download=1`` → FileResponse attachment.
    ``?diff=<other_sandbox_id>`` → :class:`FileDiffOut` unified diff of the
    same path in both browse roots. Traversal → 404, same jail doctrine as
    the artifact download.
    """
    sandbox = session.get(Sandbox, sandbox_id)
    if sandbox is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="sandbox not found"
        )
    resolved = _browse_root(_artifact_jail(), sandbox)
    if resolved is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="sandbox has no browsable artifact",
        )
    root, _kind = resolved

    if diff is not None:
        return _file_diff(session, sandbox, root, diff, path)

    found = _resolve_in_root(root, path)
    if found is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="file not found"
        )
    file_path, rel_posix = found

    if download:
        # FileResponse stats/opens at send time — probe readability now so
        # an unreadable file 404s instead of erroring mid-response.
        try:
            with file_path.open("rb"):
                pass
        except OSError:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="file not found"
            )
        return FileResponse(file_path, filename=file_path.name)

    text, truncated, size = _read_text_capped(file_path)
    return FileContentOut(
        sandbox_id=sandbox_id,
        path=rel_posix,
        size=size,
        truncated=truncated,
        binary=text is None,
        text=text,
    )


# ---------------------------------------------------------------------------
# PR35 (F28) — run export + share links
# ---------------------------------------------------------------------------

#: Max events embedded in an export bundle / share view. A run's stream is
#: almost always far smaller (hundreds–low thousands); the bound keeps a
#: pathological run's JSON bounded and is reported via
#: ``events_total``/``events_truncated`` so consumers can tell.
_EXPORT_EVENT_CAP = 50_000

#: Scalar projection caps for events-lite rows: strings truncate at this
#: many chars; nested objects are dropped EXCEPT ``usage`` (the small
#: metered token block analytics + the timeline summary read — keeping it
#: verbatim costs nothing and preserves the per-call accounting).
_LITE_STRING_CAP = 300
_LITE_KEEP_NESTED = frozenset({"usage"})


class EventLiteOut(BaseModel):
    """Events-lite row — an :class:`db.models.Event` minus its heavy payload.

    ``payload`` is the stored payload projected to its top-level SCALAR
    fields (str/int/float/bool/None; strings capped at
    ``_LITE_STRING_CAP``), plus the small ``usage`` token dict verbatim.
    Message bodies, tool args, diffs, and other nested content are dropped
    — the export stays a summary index, not a transcript. The field set
    matches :class:`api.schemas.EventOut` so timeline consumers
    (``summarizeEvent``) read it unchanged.
    """

    id: int
    run_id: UUID
    sandbox_id: UUID | None
    ts: datetime
    seq: int | None
    source: str
    type: str
    payload: dict[str, Any] = Field(default_factory=dict)


class RunSpecOut(BaseModel):
    """The run's submitted spec + lifecycle stamps — the spec block shared
    by the JSON export (``RunExport.spec``) and the share view
    (``ShareOut.run``). ``config`` is the verbatim submitted config
    (harness_configs, models, budgets, task_inputs, ...)."""

    id: UUID
    status: str
    task_prompt: str
    model_name: str
    # The full participant-model set ([model_name] + config extras + any
    # per-config pins) — same expansion the results payload reports.
    models: list[str] = Field(default_factory=list)
    evaluator_model: str | None = None
    same_model_eval: bool | None = None
    trials: int = 1
    config: dict[str, Any] = Field(default_factory=dict)
    preset_ref: str | None = None
    preset_revision: int | None = None
    suite_ref: str | None = None
    suite_index: int | None = None
    suite_run_id: UUID | None = None
    created_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None


class RunExport(BaseModel):
    """``GET /api/runs/{id}/export?fmt=json`` — the full run bundle.

    Self-contained snapshot: the submitted spec, every rubric version
    (content included — score provenance is meaningless without it), the
    score matrix exactly as ``GET .../results`` serves it, the analytics
    rollup exactly as ``GET .../analytics`` serves it, and the events-lite
    index. ``format``/``format_version`` tag the schema for re-import
    validation (``RunExport.model_validate`` round-trips the payload).
    """

    format: Literal["heg-run-export"] = "heg-run-export"
    format_version: int = 1
    exported_at: datetime
    spec: RunSpecOut
    rubrics: list[RubricOut] = Field(default_factory=list)
    results: RunResults
    analytics: RunAnalytics
    events_total: int = 0
    events_truncated: bool = False
    events: list[EventLiteOut] = Field(default_factory=list)


class ShareLinkOut(BaseModel):
    """``POST /api/runs/{id}/share`` — the minted (or existing) link."""

    run_id: UUID
    token: str
    # API-relative path the web app serves the read-only view at.
    url: str


class ShareOut(BaseModel):
    """``GET /api/share/{token}`` — the read-only share bundle.

    Spec + score matrix + the bounded events-lite index — everything the
    ``/share/{token}`` page renders. Control-plane filesystem references
    (``artifact_path``, ``artifacts``, ``thumbnail``, the
    ``previewable``/``preview_type``/``preview_candidates`` probe fields,
    the analysis ``workspace`` path, the ``_SHARE_FS_KEYS`` payload/metric
    keys) are stripped — the share view exposes neither artifact download
    nor browse.
    """

    run: RunSpecOut
    results: RunResults
    events: list[EventLiteOut] = Field(default_factory=list)
    events_total: int = 0
    events_truncated: bool = False


def _event_lite_payload(
    payload: dict[str, Any], *, drop: frozenset[str] = frozenset()
) -> dict[str, Any]:
    """Project a stored event payload to a bounded summary row.

    Top-level scalar fields pass through (strings capped); ``usage``
    keeps its scalar members; every other nested dict/list is dropped.
    ``drop`` omits named keys outright (the share bundle's FS-ref keys —
    the authenticated export projects verbatim).
    """
    out: dict[str, Any] = {}
    for key, value in payload.items():
        if key in drop:
            continue
        if value is None or isinstance(value, bool | int | float):
            out[key] = value
        elif isinstance(value, str):
            out[key] = (
                value
                if len(value) <= _LITE_STRING_CAP
                else value[:_LITE_STRING_CAP] + "…"
            )
        elif key in _LITE_KEEP_NESTED and isinstance(value, dict):
            out[key] = {
                k: v
                for k, v in value.items()
                if v is None or isinstance(v, bool | int | float | str)
            }
    return out


def _event_lite_rows(
    session: Session,
    run_id: UUID,
    *,
    cap: int,
    drop: frozenset[str] = frozenset(),
) -> tuple[list[EventLiteOut], int]:
    """Events-lite rows for a run (oldest → newest) + the run's true total.

    Only the first ``cap`` rows are returned; ``total`` counts the whole
    stream so callers can flag ``events_truncated``.
    """
    total = session.execute(
        select(func.count(Event.id)).where(Event.run_id == run_id)
    ).scalar_one()
    rows = (
        session.execute(
            select(Event)
            .where(Event.run_id == run_id)
            .order_by(Event.id)
            .limit(cap)
        )
        .scalars()
        .all()
    )
    return (
        [
            EventLiteOut(
                id=ev.id,
                run_id=ev.run_id,
                sandbox_id=ev.sandbox_id,
                ts=ev.ts,
                seq=ev.seq,
                source=ev.source,
                type=ev.type,
                payload=_event_lite_payload(ev.payload or {}, drop=drop),
            )
            for ev in rows
        ],
        int(total),
    )


def _run_spec_out(run: Run) -> RunSpecOut:
    cfg = run.config or {}
    return RunSpecOut(
        id=run.id,
        status=run.status,
        task_prompt=run.task_prompt,
        model_name=run.model_name,
        models=run_models(cfg, run.model_name),
        evaluator_model=cfg.get("evaluator_model"),
        same_model_eval=cfg.get("same_model_eval"),
        trials=getattr(run, "trials", None) or cfg.get("trials") or 1,
        config=cfg,
        preset_ref=run.preset_ref,
        preset_revision=run.preset_revision,
        suite_ref=run.suite_ref,
        suite_index=run.suite_index,
        suite_run_id=run.suite_run_id,
        created_at=run.created_at,
        started_at=run.started_at,
        finished_at=run.finished_at,
    )


#: CSV flattening doctrine (the card's "one row per sandbox×dimension"):
#: every score row of the matrix becomes a sheet row carrying the score +
#: rationale; the judge's ``overall`` pseudo-dimension is emitted as a
#: regular dimension row (last per sandbox, mirroring the matrix's top
#: row); a sandbox with NO scores still contributes one row with empty
#: dimension/score so the sheet covers the whole matrix. Per-sandbox
#: fields (config label, status, participant-phase analytics) repeat per
#: row — sheets pivot on them freely; there's no separate analytics sheet.
_EXPORT_CSV_FIELDS = [
    "run_id",
    "sandbox_id",
    "sandbox_name",
    "adapter",
    "model",
    "cell",
    "trial_index",
    "status",
    "terminal_reason",
    "empty_workspace",
    "eval_error",
    "rubric_version",
    "dimension",
    "score",
    "rationale",
    "llm_turns",
    "tokens_input",
    "tokens_output",
    "tokens_cache_read",
    "tokens_total",
    "wall_clock_seconds",
    "estimated_cost_usd",
]


_CSV_FORMULA_CHARS = ("=", "+", "-", "@", "\t", "\r")


def _csv_cell(value: Any) -> Any:
    # String cells that Excel would parse as a formula get the standard
    # quote prefix — the BOM targets Excel, and names/rationales are
    # submitter- and judge-influenced text.
    if isinstance(value, str) and value[:1] in _CSV_FORMULA_CHARS:
        return "'" + value
    return value


def _export_csv(
    run_id: UUID, results: RunResults, analytics: RunAnalytics
) -> str:
    """Render the score matrix + per-sandbox analytics as CSV.

    The ``\\ufeff`` BOM prefix keeps Excel's auto-detect on UTF-8 —
    rationales can carry non-ASCII text.
    """
    metrics = {sa.sandbox_id: sa for sa in analytics.sandboxes}
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(_EXPORT_CSV_FIELDS)
    for sb in results.sandboxes:
        sa = metrics.get(sb.sandbox_id)
        hc = sb.harness_config or {}
        base: dict[str, Any] = {
            "run_id": str(run_id),
            "sandbox_id": str(sb.sandbox_id),
            "sandbox_name": sb.name or "",
            "adapter": hc.get("adapter") or hc.get("harness") or "",
            "model": sb.model,
            # The analytics cell key (config × model group) — lets a sheet
            # pivot trials of one cell; empty only when analytics lacks
            # the sandbox (shouldn't happen — both walk the same rows).
            "cell": sa.cell if sa is not None else "",
            "trial_index": sb.trial_index if sb.trial_index is not None else "",
            "status": sb.status,
            "terminal_reason": sb.terminal_reason or "",
            "empty_workspace": "1" if sb.empty_workspace else "0",
            "eval_error": sb.eval_error or "",
            "rubric_version": (
                sb.rubric_version if sb.rubric_version is not None else ""
            ),
            "llm_turns": sa.llm_turns if sa is not None else "",
            "tokens_input": sa.tokens.input if sa is not None else "",
            "tokens_output": sa.tokens.output if sa is not None else "",
            "tokens_cache_read": sa.tokens.cache_read if sa is not None else "",
            "tokens_total": sa.tokens.total if sa is not None else "",
            "wall_clock_seconds": (
                sa.wall_clock_seconds if sa is not None else ""
            ),
            "estimated_cost_usd": (
                sa.estimated_cost_usd if sa is not None else ""
            ),
            # Score columns — per-dimension rows overwrite these; the
            # score-less fallback row ships them empty.
            "dimension": "",
            "score": "",
            "rationale": "",
        }
        # dimension order: alphabetical, then overall last — deterministic
        # across runs so exports of sibling runs align row-for-row.
        dim_rows = [
            (dim, entry)
            for dim, entry in sorted(sb.dimensions.items())
        ]
        if sb.overall is not None:
            dim_rows.append(("overall", sb.overall))
        if not dim_rows:
            writer.writerow([_csv_cell(base[f]) for f in _EXPORT_CSV_FIELDS])
            continue
        for dim, entry in dim_rows:
            row = dict(base)
            row.update(
                {
                    "dimension": dim,
                    "score": entry.score,
                    "rationale": entry.rationale,
                }
            )
            writer.writerow([_csv_cell(row[f]) for f in _EXPORT_CSV_FIELDS])
    return "\ufeff" + buf.getvalue()


def _run_or_404(session: Session, run_id: UUID) -> Run:
    run = session.get(Run, run_id)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="run not found")
    return run


@router.get("/api/runs/{run_id}/export")
def export_run(
    run_id: UUID,
    session: DbSession,
    fmt: Literal["json", "csv"] = "json",
) -> Response:
    """PR35 — the full run bundle as a download.

    ``fmt=json`` (default) → :class:`RunExport` (spec, rubrics, score
    matrix, analytics, events-lite). ``fmt=csv`` → the flattened sheet:
    one row per sandbox × score dimension (see :func:`_export_csv`).
    """
    run = _run_or_404(session, run_id)
    results = get_run_results(run_id, session)
    analytics = get_run_analytics(run_id, session)

    if fmt == "csv":
        body = _export_csv(run_id, results, analytics)
        return Response(
            content=body.encode("utf-8"),
            media_type="text/csv",
            headers={
                "Content-Disposition": (
                    f'attachment; filename="heg-run-{run_id}.csv"'
                )
            },
        )

    events, events_total = _event_lite_rows(
        session, run_id, cap=_EXPORT_EVENT_CAP
    )
    bundle = RunExport(
        exported_at=datetime.now(UTC),
        spec=_run_spec_out(run),
        rubrics=[
            RubricOut.model_validate(r) for r in _rubric_rows(session, run_id)
        ],
        results=results,
        analytics=analytics,
        events_total=events_total,
        events_truncated=events_total > len(events),
        events=events,
    )
    return Response(
        content=bundle.model_dump_json(),
        media_type="application/json",
        headers={
            "Content-Disposition": (
                f'attachment; filename="heg-run-{run_id}.json"'
            )
        },
    )


# ---------------------------------------------------------------------------
# share links (PR35 stretch — the read-only /share/{token} surface)
# ---------------------------------------------------------------------------

#: Stored payload/metric keys that carry control-plane filesystem paths —
#: dropped from the share bundle's events and analysis signal metrics
#: (``sb.artifact_path``/``artifacts``/``thumbnail``/the preview probe
#: fields/analysis ``workspace`` are scrubbed field-by-field in
#: ``get_shared_run``; the authenticated export keeps everything
#: verbatim).
_SHARE_FS_KEYS = frozenset(
    {
        "artifact_path",
        "eval_workspace",
        "analysis_path",
        "scores_path",
        "tests_dir",
    }
)


@router.post("/api/runs/{run_id}/share", response_model=ShareLinkOut)
def create_share_link(run_id: UUID, session: DbSession) -> ShareLinkOut:
    """Mint (or return) the run's share token — idempotent.

    ``secrets.token_urlsafe(16)`` = 128 bits of entropy — the token is the
    only credential the read-only share surface checks. The mint claims the
    slot with a conditional UPDATE — stays idempotent under concurrent
    POSTs on every dialect (``FOR UPDATE`` locks are a no-op on SQLite;
    a SHARED-lock upgrade stall rolls back and re-reads the winner's
    token).
    """
    run = session.get(Run, run_id)
    if run is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="run not found"
        )
    token = run.share_token
    if token is None:
        for _ in range(2):
            candidate = secrets.token_urlsafe(16)
            try:
                claimed = session.execute(
                    update(Run)
                    .where(Run.id == run_id, Run.share_token.is_(None))
                    .values(share_token=candidate)
                ).rowcount
                session.commit()
            except OperationalError:
                session.rollback()
                claimed = 0
            if claimed:
                token = candidate
                break
            token = session.execute(
                select(Run.share_token).where(Run.id == run_id)
            ).scalar_one_or_none()
            if token is not None:
                break
        if token is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="share link contention",
            )
    session.commit()
    return ShareLinkOut(
        run_id=run_id,
        token=token,
        url=f"/share/{token}",
    )


@router.delete("/api/runs/{run_id}/share", status_code=status.HTTP_204_NO_CONTENT)
def delete_share_link(run_id: UUID, session: DbSession) -> None:
    """Revoke the share link — the token stops resolving immediately."""
    run = _run_or_404(session, run_id)
    if run.share_token is not None:
        try:
            run.share_token = None
            session.commit()
        except OperationalError:
            # Same SQLite SHARED-lock upgrade stall as the mint — drop the
            # read lock and apply the write in a fresh transaction.
            session.rollback()
            session.execute(
                update(Run).where(Run.id == run_id).values(share_token=None)
            )
            session.commit()


@router.get("/api/share/{token}", response_model=ShareOut)
def get_shared_run(token: str, session: DbSession) -> ShareOut:
    """The read-only bundle behind a share link — spec, score matrix, and
    the bounded events-lite index.

    Unknown/revoked tokens 404 identically — a revoked link's prior
    existence is not disclosed.
    """
    run = session.execute(
        select(Run).where(Run.share_token == token)
    ).scalar_one_or_none()
    if run is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="share link not found"
        )
    events, events_total = _event_lite_rows(
        session, run.id, cap=_EXPORT_EVENT_CAP, drop=_SHARE_FS_KEYS
    )
    results = get_run_results(run.id, session)
    for sb in results.sandboxes:
        sb.artifact_path = None
        sb.artifacts = []
        sb.thumbnail = None
        sb.previewable = False
        sb.preview_type = None
        sb.preview_candidates = []
        if sb.analysis is not None:
            sb.analysis.workspace = ""
            for sig in sb.analysis.signals:
                for key in _SHARE_FS_KEYS:
                    sig.metrics.pop(key, None)
    return ShareOut(
        run=_run_spec_out(run),
        results=results,
        events=events,
        events_total=events_total,
        events_truncated=events_total > len(events),
    )

# ---------------------------------------------------------------------------
# PR29 — human score override + blind A/B pairwise review
# ---------------------------------------------------------------------------


def _votes_summary(
    session: Session,
    run_id: UUID,
    judge_means_by_sandbox: dict[UUID, dict[str, float]],
    declared_dimensions: set[str],
) -> VotesSummary | None:
    """Pairwise-vote roll-up for the results page.

    Votes merge newest-wins per (unordered sandbox pair, dimension) — a
    re-vote on the same matchup replaces the earlier pick — then
    aggregate per sandbox (wins/losses/ties; win_rate counts decided
    votes) plus the judge-vs-human agreement rate over votes where BOTH
    sides carry a judge mean on the voted dimension (``agreement_rate``
    is None until at least one vote is comparable).
    """
    rows = (
        session.execute(
            select(PairwiseVote)
            .where(PairwiseVote.run_id == run_id)
            .order_by(PairwiseVote.id)
        )
        .scalars()
        .all()
    )
    if not rows:
        return None
    latest: dict[tuple[tuple[UUID, UUID], str], PairwiseVote] = {}
    for v in rows:
        folded = fold_dimension_name(v.dimension)
        key = (
            tuple(sorted((v.left_sandbox, v.right_sandbox), key=str)),
            folded if folded in declared_dimensions else v.dimension,
        )
        latest[key] = v

    stats: dict[UUID, VoteStats] = {}
    compared = 0
    agreed = 0
    for (_, dimension), v in latest.items():
        if v.winner == "tie":
            stats.setdefault(v.left_sandbox, VoteStats()).ties += 1
            stats.setdefault(v.right_sandbox, VoteStats()).ties += 1
        elif v.winner == "left":
            stats.setdefault(v.left_sandbox, VoteStats()).wins += 1
            stats.setdefault(v.right_sandbox, VoteStats()).losses += 1
        elif v.winner == "right":
            stats.setdefault(v.right_sandbox, VoteStats()).wins += 1
            stats.setdefault(v.left_sandbox, VoteStats()).losses += 1
        left_mean = judge_means_by_sandbox.get(v.left_sandbox, {}).get(dimension)
        right_mean = judge_means_by_sandbox.get(v.right_sandbox, {}).get(dimension)
        if left_mean is None or right_mean is None:
            continue
        compared += 1
        judge_winner = (
            "left"
            if left_mean > right_mean
            else "right"
            if right_mean > left_mean
            else "tie"
        )
        if judge_winner == v.winner:
            agreed += 1
    for st in stats.values():
        decided = st.wins + st.losses
        st.win_rate = st.wins / decided if decided else None
    return VotesSummary(
        total=len(latest),
        by_sandbox={str(sid): st for sid, st in stats.items()},
        compared=compared,
        agreed=agreed,
        agreement_rate=(agreed / compared) if compared else None,
    )


@router.post(
    "/api/sandboxes/{sandbox_id}/scores",
    status_code=status.HTTP_201_CREATED,
    response_model=ScoreOut,
)
def post_human_score(
    sandbox_id: UUID, body: HumanScoreIn, session: DbSession
) -> ScoreOut:
    """PR29 — human override: a ``source="human"`` score row.

    The row stamps ``seq`` above the sandbox's current max (the durable
    newest-wins tiebreak) and carries the run's latest approved rubric as
    provenance; the results merge then lets it WIN its (dimension,
    sandbox) pair outright — judges' verdicts stay attached underneath.
    A ``human_score`` checkpoint lands in the same transaction for
    auditability.
    """
    sandbox = session.get(Sandbox, sandbox_id)
    if sandbox is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="sandbox not found"
        )
    rubric = _latest_approved_rubric(session, sandbox.run_id)
    next_seq = (
        session.scalar(
            select(func.max(Score.seq)).where(Score.sandbox_id == sandbox_id)
        )
        or 0
    ) + 1
    row = Score(
        sandbox_id=sandbox_id,
        rubric_id=rubric.id if rubric is not None else None,
        rubric_version=rubric.version if rubric is not None else None,
        seq=next_seq,
        dimension=body.dimension,
        score=body.score,
        rationale=body.rationale or "",
        source="human",
        judge_model=None,
        note=body.note,
    )
    session.add(row)
    orch_events.emit(
        session,
        run_id=sandbox.run_id,
        sandbox_id=sandbox_id,
        event_type="checkpoint",
        payload={
            "stage": "human_score",
            "dimension": body.dimension,
            "score": body.score,
            "rubric_version": rubric.version if rubric is not None else None,
        },
    )
    session.commit()
    session.refresh(row)
    return ScoreOut.model_validate(row)


#: Live blind-A/B pair sessions (pair_id → record). In-memory is
#: deliberate: the anonymized mapping is presentation state, not data —
#: a restart just expires open sessions (the reviewer requests a fresh
#: pair); recorded votes persist in ``pairwise_votes``. TTL-bounded and
#: size-capped so sessions can't accumulate.
_PAIR_TTL_SECONDS = 2 * 60 * 60
_PAIR_MAX_SESSIONS = 256


class _PairSession(NamedTuple):
    run_id: UUID
    # The sandbox behind label "A" — recorded as ``left_sandbox`` on the
    # vote rows. Right = label "B". The anonymization is presentation
    # only; votes resolve to real ids here, not in the payload.
    left: UUID
    right: UUID
    created: float


_PAIR_SESSIONS: dict[str, _PairSession] = {}


def reset_pair_sessions() -> None:
    """Test hook — drop every live pair session."""
    _PAIR_SESSIONS.clear()


def _pair_get(pair_id: str) -> _PairSession | None:
    sess = _PAIR_SESSIONS.get(pair_id)
    if sess is None:
        return None
    if time.monotonic() - sess.created > _PAIR_TTL_SECONDS:
        _PAIR_SESSIONS.pop(pair_id, None)
        return None
    return sess


def _pair_put(record: _PairSession) -> str:
    """Register a pair session → opaque pair_id. Expired sessions are
    reaped first; the cap then evicts the oldest live sessions."""
    now = time.monotonic()
    for pid, sess in list(_PAIR_SESSIONS.items()):
        if now - sess.created > _PAIR_TTL_SECONDS:
            _PAIR_SESSIONS.pop(pid, None)
    while len(_PAIR_SESSIONS) >= _PAIR_MAX_SESSIONS:
        oldest = min(_PAIR_SESSIONS.items(), key=lambda kv: kv[1].created)[0]
        _PAIR_SESSIONS.pop(oldest, None)
    pair_id = uuid4().hex
    _PAIR_SESSIONS[pair_id] = record
    return pair_id


def _pair_side_root(jail: Path, sandbox: Sandbox) -> tuple[Path, str] | None:
    """The anonymized browse root for one pair side — same jail rules as
    the file browser. ``workspace/`` when the snapshot exists; else the
    artifact dir, where only ``output.txt`` is ever exposed (prompt-cell
    submissions — meta.json/transcript/analysis never surface)."""
    ws = resolve_workspace_dir(
        jail=jail,
        run_id=str(sandbox.run_id),
        sandbox_id=str(sandbox.id),
        artifact_path=sandbox.artifact_path,
    )
    if ws is not None:
        return ws, "workspace"
    art = resolve_artifact_dir(
        jail=jail,
        run_id=str(sandbox.run_id),
        sandbox_id=str(sandbox.id),
        artifact_path=sandbox.artifact_path,
    )
    if art is not None:
        return art, "artifact"
    return None


def _pair_side(jail: Path, sandbox: Sandbox, label: str) -> PairSideOut:
    """One side's anonymized file listing.

    Anonymization contract (PR29): the reviewer sees the CANDIDATE'S own
    files — the workspace tree, or exactly ``output.txt`` for
    workspace-less prompt submissions. Harness/evaluator byproducts
    (meta.json, transcript.log, analysis.json, suite_checks.json,
    generated_tests/, score dumps) never appear — they carry
    sandbox/model provenance that would un-blind the pair.
    """
    resolved = _pair_side_root(jail, sandbox)
    if resolved is None:
        return PairSideOut(label=label, root="artifact", entries=[])
    root, kind = resolved
    if kind == "artifact":
        out_file = root / prompt_cell.OUTPUT_FILENAME
        entries: list[FileEntry] = []
        try:
            if out_file.is_file() and not out_file.is_symlink():
                entries.append(
                    FileEntry(
                        path=prompt_cell.OUTPUT_FILENAME,
                        kind="file",
                        size=out_file.stat().st_size,
                    )
                )
        except OSError:
            pass
        return PairSideOut(label=label, root="artifact", entries=entries)
    entries, truncated = _list_root_entries(root)
    return PairSideOut(
        label=label, root=kind, entries=entries, truncated=truncated
    )


def _pair_dimensions(session: Session, run_id: UUID) -> list[str]:
    """The vote menu for a pair — the approved rubric's dimension names +
    ``overall`` (the reviewer may still post other names — the vote row
    accepts any non-empty dimension)."""
    rubric = _latest_approved_rubric(session, run_id)
    names: list[str] = []
    if rubric is not None:
        for d in (rubric.content or {}).get("dimensions") or []:
            if isinstance(d, dict) and d.get("name"):
                names.append(str(d["name"]))
    names.append(OVERALL_DIMENSION)
    return names


@router.post(
    "/api/runs/{run_id}/pair",
    status_code=status.HTTP_201_CREATED,
    response_model=PairOut,
)
def create_pair(
    run_id: UUID,
    session: DbSession,
    body: PairCreateIn | None = None,
) -> PairOut:
    """PR29 — open a blind A/B pair session.

    Picks two artifact-bearing sandboxes (``sandbox_ids`` may pin a
    specific matchup for testing/focused review), assigns them labels
    A/B in randomized order, and returns each side's anonymized file
    listing. The response deliberately carries NO sandbox ids, model or
    config names — ``pair_id`` is the only handle the reviewer (and the
    vote endpoint) needs.
    """
    run = session.get(Run, run_id)
    if run is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="run not found"
        )
    candidates = (
        session.execute(
            select(Sandbox)
            .where(
                Sandbox.run_id == run_id,
                Sandbox.artifact_path.is_not(None),
            )
            .order_by(Sandbox.started_at.asc().nulls_last(), Sandbox.id)
        )
        .scalars()
        .all()
    )
    by_id = {sb.id: sb for sb in candidates}
    if body is not None and body.sandbox_ids:
        picked: list[Sandbox] = []
        for sid in body.sandbox_ids:
            if sid not in by_id:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail=(
                        f"sandbox {sid} is not an artifact-bearing member "
                        "of this run"
                    ),
                )
            picked.append(by_id[sid])
        if picked[0].id == picked[1].id:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="pair requires two distinct sandboxes",
            )
    else:
        if len(candidates) < 2:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="run has fewer than two artifact-bearing sandboxes",
            )
        picked = list(random.sample(list(candidates), 2))

    left, right = picked[0], picked[1]
    if random.random() < 0.5:  # A/B order is blinded, not request order
        left, right = right, left
    pair_id = _pair_put(
        _PairSession(
            run_id=run_id,
            left=left.id,
            right=right.id,
            created=time.monotonic(),
        )
    )
    jail = _artifact_jail()
    return PairOut(
        pair_id=pair_id,
        run_id=run_id,
        dimensions=_pair_dimensions(session, run_id),
        sides=[
            _pair_side(jail, left, "A"),
            _pair_side(jail, right, "B"),
        ],
    )


def _pair_side_sandbox(
    pair: _PairSession, side: str, session: Session
) -> Sandbox:
    label = side.upper()
    if label == "A":
        sid = pair.left
    elif label == "B":
        sid = pair.right
    else:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="unknown pair side"
        )
    sb = session.get(Sandbox, sid)
    if sb is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="pair artifact gone"
        )
    return sb


@router.get(
    "/api/runs/{run_id}/pair/{pair_id}/files/{side}/{path:path}",
    response_model=PairFileOut,
)
def get_pair_file(
    run_id: UUID,
    pair_id: str,
    side: str,
    path: str,
    session: DbSession,
) -> PairFileOut:
    """Read one file inside a pair side — anonymized (the side label
    resolves to its sandbox server-side; nothing identifying returns).

    For artifact-root sides (prompt cells) only ``output.txt`` resolves —
    the rest of the artifact dir stays hidden, same contract as the
    pair's file listing.
    """
    pair = _pair_get(pair_id)
    if pair is None or pair.run_id != run_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="pair session not found or expired — POST a new pair",
        )
    sandbox = _pair_side_sandbox(pair, side, session)
    resolved = _pair_side_root(_artifact_jail(), sandbox)
    if resolved is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="pair side has no browsable artifact",
        )
    root, kind = resolved
    if kind == "artifact" and path.strip("/") != prompt_cell.OUTPUT_FILENAME:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="file not found"
        )
    found = _resolve_in_root(root, path)
    if found is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="file not found"
        )
    file_path, rel_posix = found
    text, truncated, size = _read_text_capped(file_path)
    return PairFileOut(
        pair_id=pair_id,
        side=side.upper(),
        path=rel_posix,
        size=size,
        truncated=truncated,
        binary=text is None,
        text=text,
    )


@router.post(
    "/api/runs/{run_id}/pair/{pair_id}/vote",
    status_code=status.HTTP_201_CREATED,
    response_model=PairwiseVoteOut,
)
def post_pair_vote(
    run_id: UUID,
    pair_id: str,
    body: PairVoteIn,
    session: DbSession,
) -> PairwiseVoteOut:
    """Record the reviewer's verdict — ``winner`` is an A/B label or
    ``tie``; the stored row resolves the pair session's real sandbox ids
    (``left_sandbox`` := the sandbox behind "A")."""
    pair = _pair_get(pair_id)
    if pair is None or pair.run_id != run_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="pair session not found or expired — POST a new pair",
        )
    winner = body.winner.lower()
    if winner not in ("a", "b", "tie"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="winner must be 'A', 'B' or 'tie'",
        )
    stored = {"a": "left", "b": "right", "tie": "tie"}[winner]
    row = PairwiseVote(
        run_id=run_id,
        left_sandbox=pair.left,
        right_sandbox=pair.right,
        dimension=body.dimension,
        winner=stored,
        note=body.note or "",
    )
    session.add(row)
    orch_events.emit(
        session,
        run_id=run_id,
        event_type="checkpoint",
        payload={
            "stage": "pairwise_vote",
            "dimension": body.dimension,
            "winner": stored,
        },
    )
    session.commit()
    session.refresh(row)
    return PairwiseVoteOut.model_validate(row)


@router.get(
    "/api/runs/{run_id}/votes",
    response_model=list[PairwiseVoteOut],
)
def list_pair_votes(run_id: UUID, session: DbSession) -> list[PairwiseVoteOut]:
    """Every recorded pairwise vote for the run, newest first — the raw
    rows behind ``RunResults.votes`` (the merge/summary view)."""
    if session.get(Run, run_id) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="run not found"
        )
    rows = (
        session.execute(
            select(PairwiseVote)
            .where(PairwiseVote.run_id == run_id)
            .order_by(PairwiseVote.id.desc())
        )
        .scalars()
        .all()
    )
    return [PairwiseVoteOut.model_validate(v) for v in rows]
