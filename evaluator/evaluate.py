"""F7 — evaluate one sandbox artifact with a sandboxed LLM judge.

Contract (docs/orchestrator-plan.md §5 — the orchestrator calls this and
nothing else)::

    scores = await evaluate_sandbox(
        run_id=..., sandbox_id=..., rubric_id=..., rubric={...},
        artifact_dir=..., workspace_dir=..., transcript_path=...,
        terminal_reason=..., model=..., gateway_base_url=...,
        gateway_cred=..., session_factory=..., timeout_seconds=1800.0,
    )

What it does:

1. Stages a throwaway eval workspace at ``<eval_root>/<run>/<sandbox>/
   eval_workspace/`` — default ``eval_root`` is ``<artifact_root>/../eval``
   (i.e. ``data/eval``), a sibling of the artifact store so it lives on the
   same host-visible mount. Contents: a copy of the artifact ``workspace/``
   (the candidate repo) plus ``rubric.json`` (frozen rubric),
   ``EVAL_PROMPT.md`` (judge brief), ``ANALYSIS.md``/``analysis.json``
   (measured signals, PR6), and ``EVAL_TRANSCRIPT.log`` when a transcript
   exists.
2. Spawns a miniloop sandbox (``heg-sandbox:latest`` on the egress-locked
   ``heg-sandbox`` network) with the run-scoped gateway cred, the evaluator
   model, and a judge TASK_PROMPT. The agent reads the rubric + files and
   writes ``scores.json`` to the workspace root (= the staged dir on host).

   PR6: before staging, the candidate workspace is measured by the
   ``scoring`` signal collectors (parse-only static analysis — real
   cyclomatic complexity, size, language mix). The report is staged as
   ``ANALYSIS.md``/``analysis.json`` (judge evidence, mostly for the
   ``cyclomatic_complexity`` dimension), persisted as
   ``<artifact>/analysis.json`` for the results API, and summarized in an
   ``eval_analysis`` checkpoint event. Analysis never blocks evaluation —
   failures degrade to a missing report.
3. Waits for container exit (or ``timeout_seconds`` → docker stop), removes
   the container, parses ``scores.json``, validates it, and inserts
   ``Score`` rows (one per rubric dimension + an ``overall`` pseudo-row when
   present) plus ``source="evaluator"`` events — all in one transaction.

Return / failure semantics (plan §6): returns the ``Score`` rows it
managed to persist. A missing/invalid ``scores.json`` yields an ``error``
event + ``[]`` — the orchestrator decides run-level failure by counting
rows. Infrastructure failures (bad params, missing workspace, spawn errors)
raise. The eval sandbox is always removed; the staged dir is kept for
debugging.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import stat
import time
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from db.models import Event, Rubric, Score
from evaluator.prompts import build_eval_prompt
from evaluator.sandbox_spec import (
    DEFAULT_EVAL_MAX_ITERS,
    SANDBOX_GATEWAY_URL,
    build_eval_env,
    build_eval_spec,
    eval_sandbox_id,
)
from evaluator.scores import (
    OVERALL_DIMENSION,
    SCORES_FILENAME,
    ScoresFileError,
    load_scores_file,
)
from sandbox.runtime import DEFAULT_IMAGE, SandboxRuntime
from scoring import AnalysisReport, collect_signals
from scoring.render import (
    ANALYSIS_JSON_FILENAME,
    ANALYSIS_MD_FILENAME,
    render_analysis_markdown,
)

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_EVAL_ROOT = _REPO_ROOT / "data" / "eval"

#: Container states worth continuing to poll for (docker .State.Status).
_ACTIVE_STATES = {"created", "running", "restarting", "paused"}


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def _as_uuid(value: Any, field: str) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError(f"evaluate_sandbox: {field} is not a UUID: {value!r}") from exc


def _lookup_rubric(
    session_factory: sessionmaker[Session], run_uuid: uuid.UUID
) -> tuple[uuid.UUID | None, int | None]:
    """Latest rubric (id, version) for the run — approved preferred, then
    highest version."""
    try:
        with session_factory() as s:
            rows = s.execute(
                select(Rubric.id, Rubric.status, Rubric.version).where(
                    Rubric.run_id == run_uuid
                )
            ).all()
    except Exception:
        logger.warning("evaluator: rubric lookup failed for run %s", run_uuid, exc_info=True)
        return None, None
    if not rows:
        return None, None
    approved = [r for r in rows if r.status == "approved"]
    pool = approved or list(rows)
    latest = max(pool, key=lambda r: r.version)
    return latest.id, latest.version


def _rubric_version_of(
    session_factory: sessionmaker[Session], rubric_uuid: uuid.UUID
) -> int | None:
    """Version of one rubric row — fills ``scores.rubric_version`` when the
    caller passed ``rubric_id`` without a version."""
    try:
        with session_factory() as s:
            return s.execute(
                select(Rubric.version).where(Rubric.id == rubric_uuid)
            ).scalar_one_or_none()
    except Exception:
        logger.warning(
            "evaluator: rubric version lookup failed for %s", rubric_uuid, exc_info=True
        )
        return None


def _emit_event(
    session_factory: sessionmaker[Session],
    *,
    run_id: uuid.UUID,
    sandbox_id: uuid.UUID,
    event_type: str,
    payload: dict[str, Any],
) -> None:
    """Insert one ``source="evaluator"`` event. NEVER raises — telemetry must
    not break evaluation."""
    try:
        with session_factory() as s:
            s.add(
                Event(
                    run_id=run_id,
                    sandbox_id=sandbox_id,
                    source="evaluator",
                    type=event_type,
                    payload=payload,
                )
            )
            s.commit()
    except Exception:
        logger.warning(
            "evaluator: failed to emit %s event", event_type, exc_info=True
        )


def _collect_analysis(workspace: Path) -> AnalysisReport | None:
    """Run the scoring signal collectors; never raises.

    A catastrophic failure (shouldn't happen — ``collect_signals`` isolates
    per-collector errors) degrades to ``None``: evaluation continues with no
    staged analysis, and the ``eval_analysis`` event records it.
    """
    try:
        return collect_signals(workspace)
    except Exception:
        logger.warning("evaluator: signal collection failed for %s", workspace, exc_info=True)
        return None


def _analysis_event_payload(report: AnalysisReport | None) -> dict[str, Any]:
    """Compact summary for the ``eval_analysis`` checkpoint event."""
    if report is None:
        return {"phase": "eval_analysis", "status": "unavailable"}
    static = report.signal("static_analysis")
    payload: dict[str, Any] = {
        "phase": "eval_analysis",
        "status": "ok",
        "signals": [
            {"name": s.name, "status": s.status, **({"error": s.error} if s.error else {})}
            for s in report.signals
        ],
    }
    if static is not None and static.status == "ok":
        cc = static.metrics.get("cyclomatic_complexity") or {}
        payload["summary"] = {
            "files_analyzed": static.metrics.get("files_analyzed", 0),
            "total_nloc": static.metrics.get("total_nloc", 0),
            "function_count": static.metrics.get("function_count", 0),
            "cc_mean": cc.get("mean"),
            "cc_max": cc.get("max"),
        }
    return payload


def _persist_analysis(artifact_dir: Path, report: AnalysisReport | None) -> Path | None:
    """Write ``<artifact>/analysis.json`` for the results API + downloads.

    Best-effort — persistence failure is logged, never raised (the eval
    workspace copy still gives the judge the data).
    """
    if report is None:
        return None
    path = Path(artifact_dir) / ANALYSIS_JSON_FILENAME
    try:
        path.write_text(report.model_dump_json(indent=2) + "\n", encoding="utf-8")
    except OSError:
        logger.warning("evaluator: could not write %s", path, exc_info=True)
        return None
    return path


def _staging_ignore(dirpath: str, names: list[str]) -> set[str]:
    """copytree ``ignore`` filter for the candidate workspace.

    ``docker cp`` preserves special nodes, so only regular files, dirs, and
    symlinks may stage — ``copy2`` on a FIFO would block the worker thread
    forever and a device node could copy unbounded data.
    """
    ignored = set()
    for name in names:
        try:
            mode = os.lstat(os.path.join(dirpath, name)).st_mode
        except OSError:
            ignored.add(name)
            continue
        if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode) or stat.S_ISLNK(mode)):
            ignored.add(name)
    return ignored


def _stage_eval_workspace(
    *,
    eval_root: Path,
    run_uuid: uuid.UUID,
    sandbox_uuid: uuid.UUID,
    workspace_dir: Path,
    transcript_path: Path | None,
    rubric: dict[str, Any],
    terminal_reason: str | None,
    analysis: AnalysisReport | None = None,
) -> Path:
    """Build ``<eval_root>/<run>/<sandbox>/eval_workspace/`` and return it.

    Layout::

        eval_workspace/           # = /workspace in the eval container
          <candidate repo files>  # copied from artifact workspace/
          rubric.json             # frozen rubric (overwrites any candidate file)
          EVAL_PROMPT.md          # judge brief
          EVAL_TRANSCRIPT.log     # candidate transcript, when available
          ANALYSIS.md             # measured signals summary (PR6), when collected
          analysis.json           # machine-readable signals report (PR6)
          scores.json             # <- written later by the judge agent
    """
    stage_dir = eval_root / str(run_uuid) / str(sandbox_uuid)
    eval_ws = stage_dir / "eval_workspace"
    if eval_ws.exists():
        shutil.rmtree(eval_ws)
    eval_ws.mkdir(parents=True)

    # Copy the candidate workspace CONTENTS (dirs_exist_ok into fresh dir).
    shutil.copytree(
        workspace_dir,
        eval_ws,
        dirs_exist_ok=True,
        symlinks=True,
        ignore=_staging_ignore,
    )

    # Candidate-authored files under reserved names must not survive staging:
    # scores.json would be mistaken for judge output, and the copy preserved
    # symlinks — a symlinked rubric.json/EVAL_*.md would redirect the writes
    # below through the link to an arbitrary host path. ANALYSIS.md /
    # analysis.json are evaluator-generated (PR6): a candidate-authored file
    # under those names could inject fake "measured" evidence into the brief.
    for reserved in (
        SCORES_FILENAME,
        "rubric.json",
        "EVAL_PROMPT.md",
        "EVAL_TRANSCRIPT.log",
        ANALYSIS_MD_FILENAME,
        ANALYSIS_JSON_FILENAME,
    ):
        p = eval_ws / reserved
        if p.is_dir() and not p.is_symlink():
            shutil.rmtree(p)
        else:
            p.unlink(missing_ok=True)

    (eval_ws / "rubric.json").write_text(
        json.dumps(rubric, indent=2, ensure_ascii=False, default=str) + "\n",
        encoding="utf-8",
    )
    (eval_ws / "EVAL_PROMPT.md").write_text(
        build_eval_prompt(rubric, terminal_reason=terminal_reason),
        encoding="utf-8",
    )
    if transcript_path is not None and Path(transcript_path).is_file():
        shutil.copy2(transcript_path, eval_ws / "EVAL_TRANSCRIPT.log")
    if analysis is not None:
        (eval_ws / ANALYSIS_MD_FILENAME).write_text(
            render_analysis_markdown(analysis), encoding="utf-8"
        )
        (eval_ws / ANALYSIS_JSON_FILENAME).write_text(
            analysis.model_dump_json(indent=2) + "\n", encoding="utf-8"
        )
    return eval_ws


async def _wait_for_exit(
    rt: SandboxRuntime,
    eval_sid: str,
    *,
    deadline: float,
    poll_interval: float,
) -> tuple[dict[str, Any], bool]:
    """Poll ``rt.status`` until the container leaves an active state.

    Returns ``(last_status_dict, timed_out)``. On deadline, issues
    ``rt.stop(remove=False)`` so a stuck judge can't leak a container; the
    caller removes it and still tries to read scores.json (partial output
    beats nothing).
    """
    last: dict[str, Any] = {"status": "unknown"}
    while True:
        last = await asyncio.to_thread(rt.status, eval_sid)
        state = str(last.get("status") or "unknown")
        if state not in _ACTIVE_STATES:
            return last, False
        if time.monotonic() >= deadline:
            await asyncio.to_thread(rt.stop, eval_sid, remove=False)
            # One final status read for the event payload.
            try:
                last = await asyncio.to_thread(rt.status, eval_sid)
            except Exception:
                logger.debug("evaluator: post-stop status read failed", exc_info=True)
            return last, True
        await asyncio.sleep(poll_interval)


# ---------------------------------------------------------------------------
# contract
# ---------------------------------------------------------------------------


async def evaluate_sandbox(
    *,
    run_id: uuid.UUID | str,
    sandbox_id: uuid.UUID | str,
    rubric_id: uuid.UUID | str | None = None,
    rubric_version: int | None = None,
    rubric: dict[str, Any] | None = None,
    artifact_dir: Path | str | None = None,
    workspace_dir: Path | str | None = None,
    transcript_path: Path | str | None = None,
    terminal_reason: str | None = None,
    model: str | None = None,
    gateway_base_url: str = SANDBOX_GATEWAY_URL,
    gateway_cred: str | None = None,
    session_factory: sessionmaker[Session] | None = None,
    timeout_seconds: float = 1800.0,
    runtime: SandboxRuntime | None = None,
    max_iters: int = DEFAULT_EVAL_MAX_ITERS,
    eval_root: Path | str | None = None,
    poll_interval_seconds: float = 2.0,
    image: str = DEFAULT_IMAGE,
) -> list[Score]:
    """Score one sandbox's artifact with a sandboxed judge agent.

    Plan §5 params are keyword-only and match the orchestrator contract;
    ``runtime``/``max_iters``/``eval_root``/``poll_interval_seconds``/
    ``image`` are DI/test knobs. ``rubric_version`` (PR5) stamps the
    Score rows' provenance — omitted, it is resolved from ``rubric_id``
    (or the run's latest rubric when no id is passed).

    Returns the persisted ``Score`` ORM rows (detached). Raises ``ValueError``
    on missing required inputs, ``FileNotFoundError`` when the artifact
    workspace is absent, and propagates sandbox spawn/runtime errors.
    """
    # ---- resolve + validate ------------------------------------------------
    run_uuid = _as_uuid(run_id, "run_id")
    sandbox_uuid = _as_uuid(sandbox_id, "sandbox_id")

    model_name = (model or "").strip()
    cred_token = gateway_cred
    if not model_name:
        raise ValueError("evaluate_sandbox: 'model' is required")
    if not cred_token:
        raise ValueError("evaluate_sandbox: 'gateway_cred' is required")
    if session_factory is None:
        raise ValueError("evaluate_sandbox: 'session_factory' is required")
    if not isinstance(rubric, dict) or not rubric:
        raise ValueError("evaluate_sandbox: 'rubric' must be a non-empty dict")

    ws_dir = workspace_dir
    art_dir = Path(artifact_dir) if artifact_dir is not None else None
    if ws_dir is None and art_dir is not None:
        ws_dir = art_dir / "workspace"
    if ws_dir is None:
        raise ValueError(
            "evaluate_sandbox: 'workspace_dir' (or 'artifact_dir') is required"
        )
    ws_path = Path(ws_dir)
    art_path = art_dir if art_dir is not None else ws_path.parent
    if not ws_path.is_dir():
        raise FileNotFoundError(
            f"evaluate_sandbox: artifact workspace not found: {ws_path}"
        )
    transcript = (
        Path(transcript_path)
        if transcript_path is not None
        else (art_path / "transcript.log" if art_path else None)
    )

    looked_up_version: int | None = None
    if rubric_id is not None:
        rid_uuid = _as_uuid(rubric_id, "rubric_id")
        # Provenance denormalized onto Score rows (PR5): explicit kwarg
        # wins; otherwise read the row's version.
        rver = (
            rubric_version
            if rubric_version is not None
            else _rubric_version_of(session_factory, rid_uuid)
        )
    else:
        rid_uuid, looked_up_version = _lookup_rubric(session_factory, run_uuid)
        rver = rubric_version if rubric_version is not None else looked_up_version
    rt = runtime if runtime is not None else SandboxRuntime()
    if eval_root is not None:
        eval_root_path = Path(eval_root)
    else:
        # Default: sibling of the artifact root — <data>/eval. Deriving it
        # from the runtime's artifact_root keeps the staged workspace on the
        # same host-visible mount as artifacts/sandbox state (compose mounts
        # ./data -> /data; the eval workspace is bind-mounted into the eval
        # container, so it MUST live under a host path).
        art_root = getattr(rt, "artifact_root", None)
        eval_root_path = (
            Path(art_root).parent / "eval" if art_root else DEFAULT_EVAL_ROOT
        )
    eval_sid = eval_sandbox_id(sandbox_uuid)
    deadline = time.monotonic() + float(timeout_seconds)

    # ---- objective signals (PR6) --------------------------------------------
    # Parse-only measurement of the candidate workspace — runs BEFORE staging
    # so ANALYSIS.md/analysis.json land in the eval workspace as judge
    # evidence. Also persisted to the artifact dir for the results API.
    analysis_report = await asyncio.to_thread(_collect_analysis, ws_path)
    analysis_path = await asyncio.to_thread(
        _persist_analysis, art_path, analysis_report
    )
    _emit_event(
        session_factory,
        run_id=run_uuid,
        sandbox_id=sandbox_uuid,
        event_type="checkpoint",
        payload={
            **_analysis_event_payload(analysis_report),
            "eval_sandbox_id": eval_sid,
            **(
                {"analysis_path": str(analysis_path)}
                if analysis_path is not None
                else {}
            ),
        },
    )

    # ---- stage + spawn ------------------------------------------------------
    eval_ws = await asyncio.to_thread(
        _stage_eval_workspace,
        eval_root=eval_root_path,
        run_uuid=run_uuid,
        sandbox_uuid=sandbox_uuid,
        workspace_dir=ws_path,
        transcript_path=transcript,
        rubric=rubric,
        terminal_reason=terminal_reason,
        analysis=analysis_report,
    )

    _emit_event(
        session_factory,
        run_id=run_uuid,
        sandbox_id=sandbox_uuid,
        event_type="checkpoint",
        payload={
            "phase": "eval_started",
            "eval_sandbox_id": eval_sid,
            "model": model_name,
            "eval_workspace": str(eval_ws),
            "terminal_reason": terminal_reason,
            "max_iters": max_iters,
            "rubric_id": str(rid_uuid) if rid_uuid is not None else None,
            "rubric_version": rver,
        },
    )

    env = build_eval_env(
        gateway_base_url=gateway_base_url,
        cred=cred_token,
        model=model_name,
        max_iters=max_iters,
    )
    spec = build_eval_spec(
        sandbox_id=eval_sid,
        run_id=str(run_uuid),
        eval_workspace=eval_ws,
        env=env,
        image=image,
    )
    try:
        await asyncio.to_thread(rt.spawn, spec)
    except Exception as exc:
        _emit_event(
            session_factory,
            run_id=run_uuid,
            sandbox_id=sandbox_uuid,
            event_type="error",
            payload={
                "phase": "eval_spawn_failed",
                "eval_sandbox_id": eval_sid,
                "message": f"{type(exc).__name__}: {exc}",
            },
        )
        # spawn may have raised after docker created the container — reap it.
        try:
            await asyncio.to_thread(rt.remove, eval_sid)
        except Exception:
            logger.debug("evaluator: no container to remove for %s", eval_sid, exc_info=True)
        raise

    last_status: dict[str, Any] = {"status": "unknown"}
    timed_out = False
    try:
        last_status, timed_out = await _wait_for_exit(
            rt,
            eval_sid,
            deadline=deadline,
            poll_interval=poll_interval_seconds,
        )
    finally:
        try:
            await asyncio.to_thread(rt.remove, eval_sid)
        except Exception:
            logger.warning("evaluator: failed to remove %s", eval_sid, exc_info=True)

    exit_code = last_status.get("exit_code")
    container_state = str(last_status.get("status") or "unknown")

    # ---- collect scores -----------------------------------------------------
    scores_path = eval_ws / SCORES_FILENAME
    try:
        parsed = await asyncio.to_thread(load_scores_file, scores_path)
    except ScoresFileError as exc:
        reason = "scores_missing" if not scores_path.exists() else "scores_invalid"
        _emit_event(
            session_factory,
            run_id=run_uuid,
            sandbox_id=sandbox_uuid,
            event_type="error",
            payload={
                "phase": "eval_scores_failed",
                "reason": reason,
                "detail": str(exc),
                "eval_sandbox_id": eval_sid,
                "container_status": container_state,
                "exit_code": exit_code,
                "timed_out": timed_out,
            },
        )
        return []

    rows = [
        Score(
            sandbox_id=sandbox_uuid,
            rubric_id=rid_uuid,
            rubric_version=rver,
            dimension=entry.dimension,
            score=entry.score,
            rationale=entry.rationale,
        )
        for entry in parsed.entries
    ]
    if parsed.overall is not None:
        rows.append(
            Score(
                sandbox_id=sandbox_uuid,
                rubric_id=rid_uuid,
                rubric_version=rver,
                dimension=OVERALL_DIMENSION,
                score=parsed.overall,
                rationale=parsed.summary,
            )
        )

    with session_factory() as s:
        # Durable insertion order: created_at is only second-precision on
        # sqlite, so the results merge breaks ties on seq — stamp this
        # pass strictly after everything committed so far.
        next_seq = (s.scalar(select(func.max(Score.seq))) or 0) + 1
        for row in rows:
            row.seq = next_seq
            next_seq += 1
        s.add_all(rows)
        s.add(
            Event(
                run_id=run_uuid,
                sandbox_id=sandbox_uuid,
                source="evaluator",
                type="checkpoint",
                payload={
                    "phase": "eval_scored",
                    "eval_sandbox_id": eval_sid,
                    "container_status": container_state,
                    "exit_code": exit_code,
                    "timed_out": timed_out,
                    "dimensions": [e.dimension for e in parsed.entries],
                    "overall": parsed.overall,
                    "summary": parsed.summary[:500],
                    "warnings": parsed.warnings,
                    "scores_path": str(scores_path),
                    "rubric_id": str(rid_uuid) if rid_uuid is not None else None,
                    "rubric_version": rver,
                },
            )
        )
        s.commit()
        # Force-load attrs before detaching so callers can read them even
        # with expire_on_commit=True session factories.
        for row in rows:
            _ = (row.id, row.sandbox_id, row.rubric_id, row.dimension, row.score, row.rationale)
        s.expunge_all()

    logger.info(
        "evaluator: sandbox %s scored %d dimensions (overall=%s)",
        sandbox_uuid,
        len(parsed.entries),
        parsed.overall,
    )
    return rows
