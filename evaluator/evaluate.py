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

   PR7: the collector registry also runs ``exec`` — the workspace's build/
   tests/coverage are executed inside a locked ``heg-exec`` container (no
   network, resource-capped, artifact mounted read-only) and land in the
   same ``analysis.json`` envelope + ANALYSIS.md exec section. When tests
   ran, a ``test_execution`` event records pass/fail counts on the sandbox
   timeline. Disable via ``SCORING_EXEC=0``.

   PR8: a third signal, ``generated_tests``, is appended after collection —
   an LLM call (via the gateway, metered on the eval cred) writes a pytest
   suite for the submission, persisted at ``<artifact>/generated_tests/``
   and executed against the untouched workspace in the same locked
   container (``ExecRunner`` ``test_target=``). The pass rate lands in the
   same envelope + a ``generated_tests/`` copy staged for the judge.
   Disable via ``SCORING_TESTGEN=0``.
3. Waits for container exit (or ``timeout_seconds`` → docker stop), removes
   the container, parses ``scores.json``, validates it, and inserts
   ``Score`` rows (one per rubric dimension + an ``overall`` pseudo-row when
   present) plus ``source="evaluator"`` events — all in one transaction.

   When scores.json is missing or unparseable, a control-plane fallback
   (``evaluator/judge_direct.py`` — one direct chat-completions call on the
   same eval cred, ``response_format=json_object``) gets a bounded rubric +
   evidence prompt before the attempt is abandoned; on success the parsed
   doc is written back over scores.json and the ``eval_scored`` event
   carries ``mode="direct_fallback"`` (vs ``"agentic"``), on failure the
   ``eval_scores_failed`` event gains a ``fallback_detail`` field. Disable
   via ``EVAL_DIRECT_FALLBACK=0``.

Return / failure semantics (plan §6): returns the ``Score`` rows it
managed to persist. A missing/invalid ``scores.json`` yields an ``error``
event + ``[]`` — the orchestrator decides run-level failure by counting
rows. Infrastructure failures and empty submissions raise. The eval sandbox
is always removed; the staged dir is kept for
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

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from db.models import Event, Rubric, Score
from evaluator.judge_direct import (
    _read_text_capped,
    direct_fallback_enabled,
    judge_direct,
)
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
    ParsedScores,
    ScoresFileError,
    load_scores_file,
    rubric_dimension_scales,
)
from evaluator.submission import EVALUATOR_FILES, has_submission
from evaluator.testgen import GENERATED_TESTS_DIRNAME, generate_tests_signal
from sandbox.runtime import DEFAULT_IMAGE, SandboxRuntime
from scoring import AnalysisReport, SignalReport, checkers, collect_signals
from scoring.exec import exec_summary_dict
from scoring.render import (
    ANALYSIS_JSON_FILENAME,
    ANALYSIS_MD_FILENAME,
    render_analysis_markdown,
)

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_EVAL_ROOT = _REPO_ROOT / "data" / "eval"

#: PR25: bound on the prompt-cell output staged as the judge's submission —
#: far past any sane reply, still bounded (untrusted model text).
PROMPT_OUTPUT_MAX_CHARS = 200_000

#: EVAL_PROMPT.md bullet replacing the default "candidate submission" line
#: for prompt-only cells — the judge should read output.txt as the whole
#: submission rather than hunt for a code workspace.
PROMPT_SUBMISSION_NOTE = (
    "**The candidate submission** — `output.txt`, the whole reply of a "
    "single prompt-only model call (a no-sandbox cell: no agent loop, no "
    "tools, no code workspace). Judge its content against the rubric. "
    "**Do NOT modify, fix, or add to it.**"
)

#: Container states worth continuing to poll for (docker .State.Status).
_ACTIVE_STATES = {"created", "running", "restarting", "paused"}

#: Seconds of the attempt deadline reserved after the direct-fallback call
#: — a call bounded AT the deadline could be cancelled while its scores
#: were being persisted.
_FALLBACK_EXIT_RESERVE_SECONDS = 5.0

class EmptySubmissionError(FileNotFoundError):
    """The snapshot contains no submission that can be judged."""


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


async def _collect_generated_tests(
    *,
    ws_path: Path,
    art_path: Path,
    rubric: dict[str, Any],
    model: str,
    gateway_base_url: str,
    cred: str,
    attempt_deadline: float | None = None,
) -> SignalReport | None:
    """PR8: generate + execute an LLM-written pytest suite; never raises.

    A catastrophic failure (LLM call stack, exec plumbing) degrades to
    ``None`` — evaluation continues with the signal absent, mirroring
    ``_collect_analysis``. Routine degradation (disabled, unrunnable output,
    no docker) is a ``skipped``/``error`` report, NOT ``None``.
    """
    try:
        return await generate_tests_signal(
            workspace=ws_path,
            gen_dir=art_path / GENERATED_TESTS_DIRNAME,
            gateway_base_url=gateway_base_url,
            cred=cred,
            model=model,
            rubric=rubric,
            attempt_deadline=attempt_deadline,
        )
    except Exception:
        logger.warning(
            "evaluator: test-suite generation failed for %s", ws_path, exc_info=True
        )
        return None


def _load_suite_signal(artifact_dir: Path) -> SignalReport | None:
    """PR26: fold the orchestrator's ``suite_checks.json`` measurement into
    the analysis report as the ``suite`` signal — the deterministic pass
    rate lands in ``analysis.json``/``ANALYSIS.md`` as judge evidence
    alongside the other objective signals. Returns None when no suite ran
    checks on this artifact (file absent or unreadable — never raises)."""
    try:
        path = Path(artifact_dir) / checkers.SUITE_CHECKS_FILENAME
        if not path.is_file():
            return None
        doc = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        if not isinstance(doc, dict) or not doc.get("checks"):
            return None
        return SignalReport(name="suite", metrics=checkers.signal_metrics(doc))
    except Exception:  # evidence must not break the eval
        logger.warning(
            "evaluator: could not read %s/%s",
            artifact_dir,
            checkers.SUITE_CHECKS_FILENAME,
            exc_info=True,
        )
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
    exec_sig = report.signal("exec")
    if exec_sig is not None and exec_sig.status == "ok":
        payload["exec"] = exec_summary_dict(exec_sig.metrics)
    gen_sig = report.signal("generated_tests")
    if gen_sig is not None and gen_sig.status == "ok":
        payload["generated_tests"] = {
            "model": gen_sig.metrics.get("model"),
            "files_written": gen_sig.metrics.get("files_written", 0),
            "tests_collected": gen_sig.metrics.get("tests_collected", 0),
            "tests_passed": gen_sig.metrics.get("tests_passed", 0),
            "tests_failed": gen_sig.metrics.get("tests_failed", 0),
            "pass_rate": gen_sig.metrics.get("pass_rate"),
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


def _stage_prompt_submission(
    eval_root: Path,
    run_uuid: uuid.UUID,
    sandbox_uuid: uuid.UUID,
    output_path: Path,
) -> Path:
    """PR25: synthesize the dir the pipeline treats as the submission.

    A prompt cell has no snapshot workspace — the submission is the single
    ``output.txt`` the cell wrote. A bounded copy lands under
    ``<eval_root>/<run>/<sandbox>/prompt_submission/`` (daemon-visible like
    the eval workspace itself); collectors/staging/judging then run
    unchanged — analysis reports an empty code profile, and the judge
    reads output.txt as the submission.
    """
    text = _read_text_capped(output_path, PROMPT_OUTPUT_MAX_CHARS)
    if text is None:
        raise EmptySubmissionError("prompt output is missing, unreadable, or blank")
    sub_dir = eval_root / str(run_uuid) / str(sandbox_uuid) / "prompt_submission"
    if sub_dir.exists():
        shutil.rmtree(sub_dir)
    sub_dir.mkdir(parents=True)
    (sub_dir / "output.txt").write_text(text, encoding="utf-8")
    return sub_dir


def _stage_eval_workspace(
    *,
    eval_root: Path,
    run_uuid: uuid.UUID,
    sandbox_uuid: uuid.UUID,
    workspace_dir: Path,
    transcript_path: Path | None,
    rubric: dict[str, Any],
    terminal_reason: str | None,
    prior_error: str | None = None,
    analysis: AnalysisReport | None = None,
    generated_tests_dir: Path | None = None,
    submission_note: str | None = None,
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
          generated_tests/        # LLM-written suite (PR8), when generated
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
    # generated_tests/ (PR8) is likewise evaluator-authored — a candidate dir
    # under that name could plant fake "LLM-generated" tests for the judge.
    for reserved in EVALUATOR_FILES:
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
        build_eval_prompt(
            rubric,
            terminal_reason=terminal_reason,
            prior_error=prior_error,
            submission_note=submission_note,
        ),
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
    if generated_tests_dir is not None and Path(generated_tests_dir).is_dir():
        shutil.copytree(generated_tests_dir, eval_ws / GENERATED_TESTS_DIRNAME)
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


async def _direct_judge_fallback(
    *,
    eval_ws: Path,
    submission_workspace: Path,
    seed_dir: Path | None,
    rubric: dict[str, Any],
    model: str,
    gateway_base_url: str,
    cred: str,
    terminal_reason: str | None,
    prior_error: str | None,
    client: httpx.AsyncClient | None,
    time_budget_seconds: float,
) -> tuple[ParsedScores | None, str | None]:
    """Control-plane last resort: ONE direct chat call for the scores doc.

    The agentic judge's failures are output-side (no scores.json, or
    unparseable pseudo-JSON) — a bounded prompt carrying the rubric, the
    staged ANALYSIS.md, a capped source gather, and the transcript tail can
    still land scores a wandering tool loop couldn't write.

    ``time_budget_seconds`` bounds the call to what remains of the
    attempt's deadline — an overlong fallback cancelled by the caller's
    outer ``wait_for`` surfaces as a deterministic failure and burns the
    configured retry budget; expiry instead degrades to a detail.

    Returns ``(parsed, None)`` on success — the canonical doc is written
    back over ``eval_ws/scores.json`` so artifact/debug parity holds — or
    ``(None, detail)`` when disabled or failed. NEVER raises; callers fold
    a failure into the normal ``eval_scores_failed`` path.
    """
    if not direct_fallback_enabled():
        return None, "direct fallback disabled (EVAL_DIRECT_FALLBACK=0)"
    if time_budget_seconds <= 0:
        return None, "attempt deadline exhausted — no budget for direct fallback"
    try:
        parsed = await asyncio.wait_for(
            judge_direct(
                eval_workspace=eval_ws,
                submission_workspace=submission_workspace if seed_dir is not None else None,
                seed_dir=seed_dir,
                rubric=rubric,
                gateway_base_url=gateway_base_url,
                cred=cred,
                model=model,
                terminal_reason=terminal_reason,
                prior_error=prior_error,
                client=client,
            ),
            timeout=time_budget_seconds,
        )
    except TimeoutError:
        logger.warning("evaluator: direct judge fallback hit the attempt deadline")
        return None, (
            f"direct fallback exceeded the attempt deadline "
            f"({time_budget_seconds:.1f}s left)"
        )
    except Exception as exc:  # noqa: BLE001 — fallback must not escape eval
        logger.warning("evaluator: direct judge fallback failed: %s", exc)
        return None, f"{type(exc).__name__}: {exc}"
    doc: dict[str, Any] = {
        "scores": [
            {"dimension": e.dimension, "score": e.score, "rationale": e.rationale}
            for e in parsed.entries
        ]
    }
    if parsed.overall is not None:
        doc["overall"] = parsed.overall
    if parsed.summary:
        doc["summary"] = parsed.summary
    scores_out = eval_ws / SCORES_FILENAME
    try:
        # The judge could leave a symlink at scores.json — unlink so the
        # write can't be redirected through it to an arbitrary host path.
        scores_out.unlink(missing_ok=True)
        scores_out.write_text(
            json.dumps(doc, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    except OSError:
        logger.warning(
            "evaluator: could not write fallback scores.json to %s", eval_ws,
            exc_info=True,
        )
    return parsed, None


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
    seed_dir: Path | None = None,
    prompt_output_path: Path | str | None = None,
    transcript_path: Path | str | None = None,
    terminal_reason: str | None = None,
    prior_error: str | None = None,
    model: str | None = None,
    gateway_base_url: str = SANDBOX_GATEWAY_URL,
    gateway_cred: str | None = None,
    session_factory: sessionmaker[Session] | None = None,
    timeout_seconds: float = 1800.0,
    deadline_margin_seconds: float = 60.0,
    runtime: SandboxRuntime | None = None,
    max_iters: int = DEFAULT_EVAL_MAX_ITERS,
    judge_max_tokens: int | None = None,
    eval_root: Path | str | None = None,
    poll_interval_seconds: float = 2.0,
    image: str = DEFAULT_IMAGE,
    direct_client: httpx.AsyncClient | None = None,
) -> list[Score]:
    """Score one sandbox's artifact with a sandboxed judge agent.

    Plan §5 params are keyword-only and match the orchestrator contract;
    ``runtime``/``max_iters``/``eval_root``/``poll_interval_seconds``/
    ``image`` are DI/test knobs. ``rubric_version`` (PR5) stamps the
    Score rows' provenance — omitted, it is resolved from ``rubric_id``
    (or the run's latest rubric when no id is passed).

    Returns the persisted ``Score`` ORM rows (detached). Raises ``ValueError``
    on missing required inputs, ``EmptySubmissionError`` when no submission
    is available to judge, and propagates sandbox spawn/runtime errors.

    ``judge_max_tokens`` overrides the judge's per-request output cap
    (miniloop ``MAX_TOKENS`` env); ``None`` keeps the 4096 agent default.

    ``prior_error`` (error-feedback retry) carries the previous attempt's
    ``eval_scores_failed`` detail into EVAL_PROMPT.md so a re-spawned judge
    sees WHY its output was rejected instead of repeating the same failure
    mode; the orchestrator supplies it on retries. ``direct_client`` is a
    DI/test knob for the direct-judge fallback (``evaluator.judge_direct``)
    — when the agentic judge leaves a missing/unusable scores.json, one
    control-plane chat call on the same eval cred asks for the scores
    document directly; disable via ``EVAL_DIRECT_FALLBACK=0``.

    ``deadline_margin_seconds`` is the slack the caller grants beyond
    ``timeout_seconds`` for the whole attempt (the orchestrator's outer
    ``wait_for`` margin); the direct fallback is bounded to what remains of
    it so a slow fallback can't outlive the attempt and forfeit the
    configured score retries.

    ``prompt_output_path`` (PR25) marks an output-only submission: the
    orchestrator passes the prompt cell's ``output.txt`` instead of a
    snapshot workspace. A bounded copy is staged into a synthesized
    submission dir and the usual pipeline (analysis, judge staging, direct
    fallback) treats it as the submission — ``workspace_dir`` is ignored
    and never required. Missing, unreadable or blank output is not judged.
    """
    # ---- resolve + validate ------------------------------------------------
    attempt_started = time.monotonic()
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

    prompt_output = (
        Path(prompt_output_path) if prompt_output_path is not None else None
    )
    art_dir = Path(artifact_dir) if artifact_dir is not None else None
    ws_path: Path | None
    if prompt_output is not None:
        # Output-only submission — the synthesized dir staged below stands
        # in for the snapshot workspace; artifact_dir still anchors outputs
        # (analysis.json etc.), defaulting to output.txt's parent.
        ws_path = None
        art_path = art_dir if art_dir is not None else prompt_output.parent
    else:
        ws_dir = workspace_dir
        if ws_dir is None and art_dir is not None:
            ws_dir = art_dir / "workspace"
        if ws_dir is None:
            raise ValueError(
                "evaluate_sandbox: 'workspace_dir' (or 'artifact_dir') is required"
            )
        ws_path = Path(ws_dir)
        art_path = art_dir if art_dir is not None else ws_path.parent
        if not await asyncio.to_thread(has_submission, ws_path, seed_dir):
            raise EmptySubmissionError("workspace has no non-empty submission files added or changed")
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

    if prompt_output is not None:
        # PR25: stand the staged output.txt copy in for the workspace — a
        # real dir under eval_root so analysis/staging/judge paths are the
        # same shape a container artifact produces.
        ws_path = await asyncio.to_thread(
            _stage_prompt_submission,
            eval_root_path,
            run_uuid,
            sandbox_uuid,
            prompt_output,
        )

    # ---- objective signals (PR6) --------------------------------------------
    # Parse-only measurement of the candidate workspace — runs BEFORE staging
    # so ANALYSIS.md/analysis.json land in the eval workspace as judge
    # evidence. Also persisted to the artifact dir for the results API.
    analysis_report = await asyncio.to_thread(_collect_analysis, ws_path)

    # PR8: LLM-generated pytest suite — written to <artifact>/generated_tests/
    # (never the candidate workspace), executed against the untouched ws in
    # the locked heg-exec container, folded into the same AnalysisReport
    # envelope BEFORE persisting/staging so the judge sees the pass rate.
    # Generation traffic uses the eval cred → metered/budgeted like the
    # judge's own calls. Degrades to None on catastrophic failure.
    gen_sig = await _collect_generated_tests(
        ws_path=ws_path,
        art_path=art_path,
        rubric=rubric,
        model=model_name,
        gateway_base_url=gateway_base_url,
        cred=cred_token,
        # Same bound the orchestrator's outer wait_for enforces on the whole
        # attempt — keeps the raised-cap retry inside the envelope so a slow
        # retry degrades to skipped instead of a hard cancel.
        attempt_deadline=(
            attempt_started
            + float(timeout_seconds)
            + float(deadline_margin_seconds)
        ),
    )
    if gen_sig is not None:
        # A declined (``skipped``) signal rides along inside an existing
        # report but must not resurrect an analysis that never measured —
        # a fully absent report stays absent (see
        # test_analysis_failure_does_not_block_eval).
        if analysis_report is not None:
            analysis_report.signals.append(gen_sig)
        elif gen_sig.status != "skipped":
            analysis_report = AnalysisReport(workspace=str(ws_path))
            analysis_report.signals.append(gen_sig)
    # The suite only goes to the judge when THIS attempt wrote files — a
    # stale dir from an earlier attempt is provenance, not fresh evidence.
    gen_stage_dir = (
        art_path / GENERATED_TESTS_DIRNAME
        if gen_sig is not None and gen_sig.metrics.get("files_written")
        else None
    )

    # PR26: the suite's deterministic checks ran pre-judge (orchestrator)
    # and left suite_checks.json on the artifact — fold it in as the
    # ``suite`` signal so the judge sees the pass rate and analysis.json
    # carries the machine-readable form. Unlike a skipped gen_tests signal
    # this IS fresh evidence, so it may open a report when the collectors
    # failed (mirrors the gen_sig non-skipped path).
    suite_sig = _load_suite_signal(art_path)
    if suite_sig is not None:
        if analysis_report is not None:
            analysis_report.signals.append(suite_sig)
        else:
            analysis_report = AnalysisReport(workspace=str(ws_path))
            analysis_report.signals.append(suite_sig)

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

    # PR7: when the exec signal actually ran candidate tests, record the
    # outcome as a test_execution event on the sandbox timeline — same
    # event type harness-side test runs use, sourced from the evaluator.
    exec_sig = analysis_report.signal("exec") if analysis_report else None
    if (
        exec_sig is not None
        and exec_sig.status == "ok"
        and exec_sig.metrics.get("tests_detected")
    ):
        _emit_event(
            session_factory,
            run_id=run_uuid,
            sandbox_id=sandbox_uuid,
            event_type="test_execution",
            payload={
                "phase": "eval_exec",
                "eval_sandbox_id": eval_sid,
                "image": exec_sig.metrics.get("image"),
                **exec_summary_dict(exec_sig.metrics),
            },
        )

    # PR8: mirror for the generated suite — pass rate on the timeline next
    # to the candidate's own test results.
    if (
        gen_sig is not None
        and gen_sig.status == "ok"
        and gen_sig.metrics.get("tests_detected")
    ):
        _emit_event(
            session_factory,
            run_id=run_uuid,
            sandbox_id=sandbox_uuid,
            event_type="test_execution",
            payload={
                "phase": "eval_testgen",
                "eval_sandbox_id": eval_sid,
                "image": gen_sig.metrics.get("image"),
                "generator_model": gen_sig.metrics.get("model"),
                "generated_files": gen_sig.metrics.get("files_written", 0),
                "pass_rate": gen_sig.metrics.get("pass_rate"),
                **exec_summary_dict(gen_sig.metrics),
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
        prior_error=prior_error,
        analysis=analysis_report,
        generated_tests_dir=gen_stage_dir,
        submission_note=(
            PROMPT_SUBMISSION_NOTE if prompt_output is not None else None
        ),
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
        max_tokens=judge_max_tokens,
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
    deadline = time.monotonic() + float(timeout_seconds)
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
    mode = "agentic"
    try:
        parsed = await asyncio.to_thread(
            load_scores_file,
            scores_path,
            dimension_scales=rubric_dimension_scales(rubric),
        )
    except ScoresFileError as exc:
        reason = "scores_missing" if not scores_path.exists() else "scores_invalid"
        # Direct-judge fallback: the tool loop failed at OUTPUT, not
        # reasoning — one bounded control-plane call may still land scores.
        # The prompt sees both the orchestrator-supplied retry feedback and
        # this attempt's parse failure.
        prior_parts = [p for p in (prior_error, f"{reason}: {exc}") if p]
        parsed, fallback_detail = await _direct_judge_fallback(
            eval_ws=eval_ws,
            submission_workspace=ws_path,
            seed_dir=seed_dir,
            rubric=rubric,
            model=model_name,
            gateway_base_url=gateway_base_url,
            cred=cred_token,
            terminal_reason=terminal_reason,
            prior_error="\n".join(prior_parts) or None,
            client=direct_client,
            time_budget_seconds=(
                attempt_started
                + float(timeout_seconds)
                + float(deadline_margin_seconds)
                - _FALLBACK_EXIT_RESERVE_SECONDS
                - time.monotonic()
            ),
        )
        if parsed is None:
            _emit_event(
                session_factory,
                run_id=run_uuid,
                sandbox_id=sandbox_uuid,
                event_type="error",
                payload={
                    "phase": "eval_scores_failed",
                    "judge_model": model_name,
                    "reason": reason,
                    "detail": str(exc),
                    "fallback_detail": fallback_detail,
                    "eval_sandbox_id": eval_sid,
                    "container_status": container_state,
                    "exit_code": exit_code,
                    "timed_out": timed_out,
                },
            )
            return []
        mode = "direct_fallback"

    # PR29 — every row stamps the judging model so a multi-judge panel's
    # scores stay attributable per judge in the results merge (source
    # defaults to "llm" — judge-authored).
    rows = [
        Score(
            sandbox_id=sandbox_uuid,
            rubric_id=rid_uuid,
            rubric_version=rver,
            dimension=entry.dimension,
            score=entry.score,
            rationale=entry.rationale,
            judge_model=model_name,
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
                judge_model=model_name,
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
                    "judge_model": model_name,
                    "mode": mode,
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
