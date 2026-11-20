"""Results read-API tests (F7/F8): run results, sandbox scores, downloads.

Runs against in-memory sqlite via the shared conftest app (same pattern as
test_events.py). Artifact downloads jail to ARTIFACT_ROOT — monkeypatched to
a tmp dir per test.

PR5 additions cover the eval-iteration endpoints: rubric version listing +
creation, the re-score trigger (with a stubbed ``lifecycle.rescore_run`` —
the REAL ``runner.start_rescore_task`` wiring still runs), and the
``?rubric_version=`` results filter.
"""

from __future__ import annotations

import io
import json
import os
import time
import uuid
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from api.deps import get_session_factory
from api.routers import results as results_router
from db.models import Artifact, Base, Event, Rubric, Run, Sandbox, Score
from orchestrator import lifecycle as lifecycle_mod
from orchestrator import runner


@pytest.fixture()
def session_factory() -> sessionmaker[Session]:
    sf = get_session_factory()
    Base.metadata.create_all(sf.kw["bind"])
    return sf


@pytest.fixture()
def scored_run(session_factory: sessionmaker[Session]):
    """Run (config with evaluator_model) + 2 sandboxes + rubric + scores."""
    s = session_factory()
    try:
        run = Run(
            task_prompt="build 2048",
            model_name="qwen3:8b",
            config={
                "evaluator_model": "qwen3:8b",
                "same_model_eval": True,
                "budgets": {"max_iters": 50},
            },
            status="done",
        )
        s.add(run)
        s.flush()
        sb1 = Sandbox(
            run_id=run.id,
            name="with-superpowers",
            harness_config={"adapter": "miniloop", "skill_packs": ["superpowers"]},
            status="done",
            terminal_reason="agent_done",
            artifact_path="/data/artifacts/x/y",
        )
        sb2 = Sandbox(
            run_id=run.id,
            harness_config={"adapter": "miniloop", "skill_packs": []},
            status="failed",
            terminal_reason="timeout",
        )
        s.add_all([sb1, sb2])
        s.flush()
        rubric = Rubric(
            run_id=run.id, version=1, status="approved",
            content={"dimensions": [{"name": "functional_requirements"}]},
        )
        s.add(rubric)
        s.flush()

        def score(sb, dim, val, rationale=""):
            return Score(
                sandbox_id=sb.id, rubric_id=rubric.id,
                rubric_version=1,
                dimension=dim, score=val, rationale=rationale,
            )

        s.add_all([
            score(sb1, "functional_requirements", 8.0, "game loop works"),
            score(sb1, "code_quality", 6.0, "readable"),
            score(sb1, "overall", 7.0, "solid attempt"),
            # sb2 intentionally has NO scores (eval failed/skipped)
        ])
        s.commit()
        return {
            "run_id": run.id, "sb1": sb1.id, "sb2": sb2.id,
            "rubric_id": rubric.id,
        }
    finally:
        s.close()


# ---------------------------------------------------------------------------
# GET /api/runs/{run_id}/results
# ---------------------------------------------------------------------------


def test_run_results_shape(client: TestClient, scored_run) -> None:
    resp = client.get(f"/api/runs/{scored_run['run_id']}/results")
    assert resp.status_code == 200
    body = resp.json()

    assert body["run_id"] == str(scored_run["run_id"])
    assert body["status"] == "done"
    assert body["model_name"] == "qwen3:8b"
    # PR14: effective participant set + per-sandbox model resolution
    assert body["models"] == ["qwen3:8b"]
    assert body["evaluator_model"] == "qwen3:8b"
    assert body["same_model_eval"] is True
    assert len(body["sandboxes"]) == 2

    sb1 = next(s for s in body["sandboxes"] if s["sandbox_id"] == str(scored_run["sb1"]))
    assert sb1["status"] == "done"
    assert sb1["terminal_reason"] == "agent_done"
    assert sb1["name"] == "with-superpowers"  # PR1: persisted sandbox name
    assert sb1["model"] == "qwen3:8b"  # PR14: no config model → run default
    assert sb1["harness_config"]["skill_packs"] == ["superpowers"]
    assert sb1["rubric_id"] == str(scored_run["rubric_id"])
    assert sb1["dimensions"]["functional_requirements"] == {
        "score": 8.0, "rationale": "game loop works"
    }
    assert sb1["dimensions"]["code_quality"]["score"] == 6.0
    # overall pseudo-dimension surfaced separately, not inside dimensions
    assert "overall" not in sb1["dimensions"]
    assert sb1["overall"] == {"score": 7.0, "rationale": "solid attempt"}

    sb2 = next(s for s in body["sandboxes"] if s["sandbox_id"] == str(scored_run["sb2"]))
    assert sb2["dimensions"] == {}
    assert sb2["overall"] is None
    assert sb2["terminal_reason"] == "timeout"
    assert sb2["name"] is None  # unnamed sandbox serializes null
    # no eval-failure events seeded -> eval_error absent (not "judge failed")
    assert sb2["eval_error"] is None


def _error_event(session_factory, run_id, sandbox_id, payload, source="evaluator"):
    s = session_factory()
    try:
        s.add(
            Event(
                run_id=run_id,
                sandbox_id=sandbox_id,
                source=source,
                type="error",
                payload=payload,
            )
        )
        s.commit()
    finally:
        s.close()


def _checkpoint_event(
    session_factory, run_id, sandbox_id, payload, source="orchestrator"
):
    """Monitor-style checkpoint (``sandbox_finalized`` / warning markers)."""
    s = session_factory()
    try:
        s.add(
            Event(
                run_id=run_id,
                sandbox_id=sandbox_id,
                source=source,
                type="checkpoint",
                payload=payload,
            )
        )
        s.commit()
    finally:
        s.close()


def test_run_results_surfaces_eval_failure(
    client: TestClient, scored_run, session_factory
) -> None:
    """A scoreless sandbox whose judge failed reports eval_error — the UI
    distinguishes 'judge failed' from 'no rubric scores'."""
    _error_event(
        session_factory,
        scored_run["run_id"],
        scored_run["sb2"],
        {
            "phase": "eval_scores_failed",
            "reason": "scores_invalid",
            "detail": "scores.json is not valid JSON",
        },
    )

    resp = client.get(f"/api/runs/{scored_run['run_id']}/results")
    assert resp.status_code == 200
    body = resp.json()
    sb2 = next(s for s in body["sandboxes"] if s["sandbox_id"] == str(scored_run["sb2"]))
    assert sb2["eval_error"] is not None
    assert "scores_invalid" in sb2["eval_error"]
    assert "not valid JSON" in sb2["eval_error"]
    # scored sandbox unaffected
    sb1 = next(s for s in body["sandboxes"] if s["sandbox_id"] == str(scored_run["sb1"]))
    assert sb1["eval_error"] is None


def test_run_results_eval_error_cleared_when_scored(
    client: TestClient, scored_run, session_factory
) -> None:
    """A failed attempt that a later retry recovered must not leave
    eval_error on a sandbox WITH scores."""
    _error_event(
        session_factory,
        scored_run["run_id"],
        scored_run["sb1"],
        {"phase": "eval_scores_failed", "reason": "scores_missing"},
    )
    resp = client.get(f"/api/runs/{scored_run['run_id']}/results")
    assert resp.status_code == 200
    sb1 = next(
        s for s in resp.json()["sandboxes"] if s["sandbox_id"] == str(scored_run["sb1"])
    )
    assert sb1["eval_error"] is None


def test_run_results_eval_orchestrator_failure(
    client: TestClient, scored_run, session_factory
) -> None:
    """Orchestrator-emitted stage=evaluation errors (evaluate_sandbox
    exceptions, final eval_failed marker) also surface."""
    _error_event(
        session_factory,
        scored_run["run_id"],
        scored_run["sb2"],
        {"stage": "evaluation", "message": "RuntimeError: spawn exploded"},
        source="orchestrator",
    )
    resp = client.get(f"/api/runs/{scored_run['run_id']}/results")
    assert resp.status_code == 200
    sb2 = next(
        s for s in resp.json()["sandboxes"] if s["sandbox_id"] == str(scored_run["sb2"])
    )
    assert sb2["eval_error"] is not None
    assert "spawn exploded" in sb2["eval_error"]


def test_run_results_non_eval_errors_ignored(
    client: TestClient, scored_run, session_factory
) -> None:
    """Budget/gateway/harness error events are NOT eval failures."""
    _error_event(
        session_factory,
        scored_run["run_id"],
        scored_run["sb2"],
        {"stage": "budget", "reason": "budget_exceeded", "http_status": 429},
        source="gateway",
    )
    _error_event(
        session_factory,
        scored_run["run_id"],
        scored_run["sb2"],
        {"stage": "spawn", "message": "docker gone"},
        source="orchestrator",
    )
    resp = client.get(f"/api/runs/{scored_run['run_id']}/results")
    assert resp.status_code == 200
    sb2 = next(
        s for s in resp.json()["sandboxes"] if s["sandbox_id"] == str(scored_run["sb2"])
    )
    assert sb2["eval_error"] is None


def test_run_results_empty_workspace_from_finalized_payload(
    client: TestClient, scored_run, session_factory
) -> None:
    """``sandbox_finalized``'s ``empty_workspace`` payload key flags the
    results row — read from the stored event, not the artifact FS."""
    _checkpoint_event(
        session_factory,
        scored_run["run_id"],
        scored_run["sb1"],
        {
            "stage": "sandbox_finalized",
            "terminal_reason": "agent_done",
            "sandbox_status": "done",
            "empty_workspace": True,
        },
    )
    # A finalized checkpoint WITHOUT the key is a normal finalize, not a flag.
    _checkpoint_event(
        session_factory,
        scored_run["run_id"],
        scored_run["sb2"],
        {"stage": "sandbox_finalized", "terminal_reason": "timeout"},
    )
    resp = client.get(f"/api/runs/{scored_run['run_id']}/results")
    assert resp.status_code == 200
    body = resp.json()
    sb1 = next(s for s in body["sandboxes"] if s["sandbox_id"] == str(scored_run["sb1"]))
    sb2 = next(s for s in body["sandboxes"] if s["sandbox_id"] == str(scored_run["sb2"]))
    assert sb1["empty_workspace"] is True
    assert sb2["empty_workspace"] is False


def test_run_results_empty_workspace_from_warning_checkpoint(
    client: TestClient, scored_run, session_factory
) -> None:
    """The dedicated ``stage="empty_workspace"`` warning also flags the
    row (the monitor emits it for done-ish exits; the finalized payload
    key may be absent on older events)."""
    _checkpoint_event(
        session_factory,
        scored_run["run_id"],
        scored_run["sb2"],
        {
            "stage": "empty_workspace",
            "severity": "warning",
            "terminal_reason": "harness_complete",
        },
    )
    resp = client.get(f"/api/runs/{scored_run['run_id']}/results")
    assert resp.status_code == 200
    body = resp.json()
    sb1 = next(s for s in body["sandboxes"] if s["sandbox_id"] == str(scored_run["sb1"]))
    sb2 = next(s for s in body["sandboxes"] if s["sandbox_id"] == str(scored_run["sb2"]))
    assert sb2["empty_workspace"] is True
    assert sb1["empty_workspace"] is False


def test_run_results_empty_workspace_defaults_false(
    client: TestClient, scored_run
) -> None:
    """No markers → every row reports false (the field is always present)."""
    resp = client.get(f"/api/runs/{scored_run['run_id']}/results")
    assert resp.status_code == 200
    assert all(
        sb["empty_workspace"] is False for sb in resp.json()["sandboxes"]
    )


def test_run_results_includes_artifacts(
    client: TestClient, scored_run, session_factory
) -> None:
    """Artifact rows surface as {id, kind, path} so the UI can build
    /api/artifacts/{id}/download links."""
    s = session_factory()
    try:
        art = Artifact(
            sandbox_id=scored_run["sb1"],
            kind="workspace",
            path="/data/artifacts/x/y/workspace",
            size_bytes=123,
        )
        s.add(art)
        s.commit()
        art_id = art.id
    finally:
        s.close()

    resp = client.get(f"/api/runs/{scored_run['run_id']}/results")
    assert resp.status_code == 200
    body = resp.json()
    sb1 = next(s for s in body["sandboxes"] if s["sandbox_id"] == str(scored_run["sb1"]))
    assert sb1["artifacts"] == [
        {"id": str(art_id), "kind": "workspace", "path": "/data/artifacts/x/y/workspace"}
    ]
    sb2 = next(s for s in body["sandboxes"] if s["sandbox_id"] == str(scored_run["sb2"]))
    assert sb2["artifacts"] == []


def test_run_results_404(client: TestClient) -> None:
    resp = client.get(f"/api/runs/{uuid.uuid4()}/results")
    assert resp.status_code == 404


def test_run_results_prompt_cell_output(
    client: TestClient, session_factory, artifact_root
) -> None:
    """PR25: a prompt cell's artifact dir (output.txt + meta — no
    workspace) surfaces the ``output`` artifact for download, reports
    ``previewable=False``, and never flags ``empty_workspace``."""
    art_dir = artifact_root / "runp" / "sbp"
    art_dir.mkdir(parents=True)
    (art_dir / "output.txt").write_text("the whole answer")
    (art_dir / "meta.json").write_text('{"adapter": "prompt"}')
    (art_dir / "transcript.log").write_text("call transcript")

    s = session_factory()
    try:
        run = Run(
            task_prompt="q",
            model_name="m",
            config={},
            status="done",
        )
        s.add(run)
        s.flush()
        sb = Sandbox(
            run_id=run.id,
            harness_config={"adapter": "prompt"},
            status="done",
            terminal_reason="harness_complete",
            artifact_path=str(art_dir),
        )
        s.add(sb)
        s.flush()
        for kind, name in (
            ("output", "output.txt"),
            ("transcript", "transcript.log"),
            ("meta", "meta.json"),
        ):
            s.add(
                Artifact(
                    sandbox_id=sb.id,
                    kind=kind,
                    path=str(art_dir / name),
                    size_bytes=(art_dir / name).stat().st_size,
                )
            )
        s.commit()
        run_id, sb_id = run.id, sb.id
    finally:
        s.close()

    resp = client.get(f"/api/runs/{run_id}/results")
    assert resp.status_code == 200
    row = next(
        x for x in resp.json()["sandboxes"] if x["sandbox_id"] == str(sb_id)
    )
    assert {a["kind"] for a in row["artifacts"]} == {
        "output",
        "transcript",
        "meta",
    }
    assert row["previewable"] is False
    assert row["preview_candidates"] == []
    assert row["empty_workspace"] is False

    # the output artifact downloads as the plain file
    out_id = next(a["id"] for a in row["artifacts"] if a["kind"] == "output")
    dl = client.get(f"/api/artifacts/{out_id}/download")
    assert dl.status_code == 200
    assert dl.content == b"the whole answer"


def test_run_results_multi_model(client: TestClient, session_factory) -> None:
    """PR14: per-sandbox model + run-level models list on a model matrix."""
    s = session_factory()
    try:
        run = Run(
            task_prompt="x",
            model_name="qwen3:8b",
            config={"models": ["qwen3:8b", "gpt-4o"]},
            status="done",
        )
        s.add(run)
        s.flush()
        s.add_all(
            [
                Sandbox(
                    run_id=run.id,
                    name="baseline · qwen3:8b",
                    harness_config={"adapter": "miniloop", "model": "qwen3:8b"},
                    status="done",
                ),
                Sandbox(
                    run_id=run.id,
                    name="baseline · gpt-4o",
                    harness_config={"adapter": "miniloop", "model": "gpt-4o"},
                    status="done",
                ),
            ]
        )
        s.commit()
        run_id = run.id
    finally:
        s.close()

    body = client.get(f"/api/runs/{run_id}/results").json()
    assert body["models"] == ["qwen3:8b", "gpt-4o"]
    by_model = {sb["model"]: sb for sb in body["sandboxes"]}
    assert set(by_model) == {"qwen3:8b", "gpt-4o"}
    assert by_model["gpt-4o"]["name"] == "baseline · gpt-4o"
    assert by_model["gpt-4o"]["harness_config"]["model"] == "gpt-4o"


# ---------------------------------------------------------------------------
# PR6 — SandboxResult.analysis from <artifact>/analysis.json
# ---------------------------------------------------------------------------


ANALYSIS_JSON = {
    "schema_version": 1,
    "generated_at": "2026-11-13T00:00:00+00:00",
    "workspace": "/x/y/workspace",
    "signals": [
        {
            "name": "static_analysis",
            "status": "ok",
            "metrics": {
                "files_analyzed": 2,
                "function_count": 5,
                "cyclomatic_complexity": {"mean": 3.4, "max": 9.0},
            },
            "files": [
                {
                    "path": "a.py",
                    "language": "python",
                    "lines": 40,
                    "nloc": 30,
                    "function_count": 3,
                    "avg_cyclomatic_complexity": 4.0,
                    "max_cyclomatic_complexity": 9.0,
                    "max_nesting_depth": 2,
                }
            ],
            "functions": [
                {
                    "name": "f_branchy",
                    "file": "a.py",
                    "line": 5,
                    "cyclomatic_complexity": 9.0,
                    "nloc": 12,
                    "params": 2,
                    "max_nesting_depth": 2,
                }
            ],
            "warnings": ["1 source file(s) over 512 KiB skipped"],
            "error": None,
        }
    ],
}


def _set_artifact_path(session_factory, sandbox_id, path: str) -> None:
    s = session_factory()
    try:
        sb = s.get(Sandbox, sandbox_id)
        sb.artifact_path = path
        s.commit()
    finally:
        s.close()


def test_run_results_includes_analysis(
    client: TestClient, scored_run, session_factory, artifact_root
) -> None:
    """A valid <artifact>/analysis.json is re-validated + embedded."""
    art_dir = artifact_root / "x" / "y"
    art_dir.mkdir(parents=True)
    (art_dir / "analysis.json").write_text(json.dumps(ANALYSIS_JSON))
    _set_artifact_path(session_factory, scored_run["sb1"], str(art_dir))

    resp = client.get(f"/api/runs/{scored_run['run_id']}/results")
    assert resp.status_code == 200
    body = resp.json()
    sb1 = next(s for s in body["sandboxes"] if s["sandbox_id"] == str(scored_run["sb1"]))
    assert sb1["analysis"]["schema_version"] == 1
    sig = sb1["analysis"]["signals"][0]
    assert sig["name"] == "static_analysis"
    assert sig["metrics"]["cyclomatic_complexity"]["max"] == 9.0
    assert sig["functions"][0]["name"] == "f_branchy"
    # sb2 has no artifact_path -> analysis absent
    sb2 = next(s for s in body["sandboxes"] if s["sandbox_id"] == str(scored_run["sb2"]))
    assert sb2["analysis"] is None


def test_run_results_exec_summary(
    client: TestClient, scored_run, session_factory, artifact_root
) -> None:
    """PR7: the flat `exec` field is derived from the analysis exec signal."""
    payload = json.loads(json.dumps(ANALYSIS_JSON))
    payload["signals"].append(
        {
            "name": "exec",
            "status": "ok",
            "metrics": {
                "build_ok": True,
                "tests_detected": True,
                "tests_ok": True,
                "tests_collected": 5,
                "tests_passed": 5,
                "tests_failed": 0,
                "tests_errors": 0,
                "tests_skipped": 1,
                "coverage_percent": 82.5,
                "timed_out": False,
                "duration_seconds": 9.4,
            },
            "warnings": [],
            "error": None,
        }
    )
    art_dir = artifact_root / "x" / "y"
    art_dir.mkdir(parents=True)
    (art_dir / "analysis.json").write_text(json.dumps(payload))
    _set_artifact_path(session_factory, scored_run["sb1"], str(art_dir))

    resp = client.get(f"/api/runs/{scored_run['run_id']}/results")
    assert resp.status_code == 200
    sb1 = next(
        s for s in resp.json()["sandboxes"] if s["sandbox_id"] == str(scored_run["sb1"])
    )
    ex = sb1["exec"]
    assert ex["status"] == "ok"
    assert ex["build_ok"] is True
    assert ex["tests_detected"] is True
    assert ex["tests_passed"] == 5
    assert ex["tests_skipped"] == 1
    assert ex["coverage_percent"] == 82.5
    assert ex["timed_out"] is False
    # no artifact -> no analysis -> no exec summary
    sb2 = next(
        s for s in resp.json()["sandboxes"] if s["sandbox_id"] == str(scored_run["sb2"])
    )
    assert sb2["exec"] is None


def test_run_results_exec_summary_skipped(
    client: TestClient, scored_run, session_factory, artifact_root
) -> None:
    """A skipped exec signal surfaces its status + reason, not zeros."""
    payload = json.loads(json.dumps(ANALYSIS_JSON))
    payload["signals"].append(
        {
            "name": "exec",
            "status": "skipped",
            "metrics": {},
            "warnings": ["exec scoring disabled (SCORING_EXEC=0)"],
            "error": None,
        }
    )
    art_dir = artifact_root / "x" / "y"
    art_dir.mkdir(parents=True)
    (art_dir / "analysis.json").write_text(json.dumps(payload))
    _set_artifact_path(session_factory, scored_run["sb1"], str(art_dir))

    resp = client.get(f"/api/runs/{scored_run['run_id']}/results")
    assert resp.status_code == 200
    sb1 = next(
        s for s in resp.json()["sandboxes"] if s["sandbox_id"] == str(scored_run["sb1"])
    )
    assert sb1["exec"]["status"] == "skipped"
    assert "SCORING_EXEC" in (sb1["exec"]["detail"] or "")


def test_run_results_generated_tests_summary(
    client: TestClient, scored_run, session_factory, artifact_root
) -> None:
    """PR8: the flat `generated_tests` field is derived from the signal."""
    payload = json.loads(json.dumps(ANALYSIS_JSON))
    payload["signals"].append(
        {
            "name": "generated_tests",
            "status": "ok",
            "metrics": {
                "model": "eval-model",
                "files_written": 2,
                "files_rejected": 1,
                "tests_detected": True,
                "tests_collected": 10,
                "tests_passed": 7,
                "tests_failed": 2,
                "tests_errors": 1,
                "pass_rate": 0.7,
                "coverage_percent": 66.0,
                "timed_out": False,
            },
            "warnings": [],
            "error": None,
        }
    )
    art_dir = artifact_root / "x" / "y"
    art_dir.mkdir(parents=True)
    (art_dir / "analysis.json").write_text(json.dumps(payload))
    _set_artifact_path(session_factory, scored_run["sb1"], str(art_dir))

    resp = client.get(f"/api/runs/{scored_run['run_id']}/results")
    assert resp.status_code == 200
    sb1 = next(
        s for s in resp.json()["sandboxes"] if s["sandbox_id"] == str(scored_run["sb1"])
    )
    gt = sb1["generated_tests"]
    assert gt["status"] == "ok"
    assert gt["model"] == "eval-model"
    assert gt["files_written"] == 2
    assert gt["tests_passed"] == 7
    assert gt["tests_failed"] == 2
    assert gt["pass_rate"] == 0.7
    assert gt["coverage_percent"] == 66.0
    sb2 = next(
        s for s in resp.json()["sandboxes"] if s["sandbox_id"] == str(scored_run["sb2"])
    )
    assert sb2["generated_tests"] is None


def test_run_results_generated_tests_skipped(
    client: TestClient, scored_run, session_factory, artifact_root
) -> None:
    """A skipped generated_tests signal surfaces its status + reason."""
    payload = json.loads(json.dumps(ANALYSIS_JSON))
    payload["signals"].append(
        {
            "name": "generated_tests",
            "status": "skipped",
            "metrics": {},
            "warnings": ["test-suite generation disabled (SCORING_TESTGEN=0)"],
            "error": None,
        }
    )
    art_dir = artifact_root / "x" / "y"
    art_dir.mkdir(parents=True)
    (art_dir / "analysis.json").write_text(json.dumps(payload))
    _set_artifact_path(session_factory, scored_run["sb1"], str(art_dir))

    resp = client.get(f"/api/runs/{scored_run['run_id']}/results")
    assert resp.status_code == 200
    sb1 = next(
        s for s in resp.json()["sandboxes"] if s["sandbox_id"] == str(scored_run["sb1"])
    )
    assert sb1["generated_tests"]["status"] == "skipped"
    assert "SCORING_TESTGEN" in (sb1["generated_tests"]["detail"] or "")


def test_run_results_invalid_analysis_is_null(
    client: TestClient, scored_run, session_factory, artifact_root
) -> None:
    """Malformed/foreign analysis.json degrades to analysis=null — never a
    500 or a pass-through of unvalidated candidate content."""
    art_dir = artifact_root / "x" / "y"
    art_dir.mkdir(parents=True)
    (art_dir / "analysis.json").write_text('{"completely": "wrong shape"')
    _set_artifact_path(session_factory, scored_run["sb1"], str(art_dir))

    resp = client.get(f"/api/runs/{scored_run['run_id']}/results")
    assert resp.status_code == 200
    sb1 = next(
        s for s in resp.json()["sandboxes"] if s["sandbox_id"] == str(scored_run["sb1"])
    )
    assert sb1["analysis"] is None


# ---------------------------------------------------------------------------
# GET /api/sandboxes/{sandbox_id}/scores
# ---------------------------------------------------------------------------


def test_sandbox_scores(client: TestClient, scored_run) -> None:
    resp = client.get(f"/api/sandboxes/{scored_run['sb1']}/scores")
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 3
    dims = {r["dimension"]: r for r in rows}
    assert dims["functional_requirements"]["score"] == 8.0
    assert dims["overall"]["score"] == 7.0
    assert all(r["sandbox_id"] == str(scored_run["sb1"]) for r in rows)
    assert all(r["rubric_id"] == str(scored_run["rubric_id"]) for r in rows)


def test_sandbox_scores_empty_and_404(client: TestClient, scored_run) -> None:
    resp = client.get(f"/api/sandboxes/{scored_run['sb2']}/scores")
    assert resp.status_code == 200
    assert resp.json() == []

    resp = client.get(f"/api/sandboxes/{uuid.uuid4()}/scores")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# GET /api/artifacts/{artifact_id}/download
# ---------------------------------------------------------------------------


@pytest.fixture()
def artifact_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Point the results router's artifact jail at a tmp dir."""
    monkeypatch.setattr(
        results_router,
        "get_settings",
        lambda: SimpleNamespace(
            ARTIFACT_ROOT=tmp_path, PREVIEW_SCAN_TTL_SECONDS=0
        ),
    )
    results_router.reset_scan_cache()
    return tmp_path


def _artifact(session_factory, sandbox_id, path: str, kind: str = "workspace") -> uuid.UUID:
    s = session_factory()
    try:
        art = Artifact(sandbox_id=sandbox_id, kind=kind, path=path, size_bytes=0)
        s.add(art)
        s.commit()
        return art.id
    finally:
        s.close()


def test_download_file(client: TestClient, scored_run, session_factory, artifact_root) -> None:
    f = artifact_root / "run1" / "sb1" / "hello.txt"
    f.parent.mkdir(parents=True)
    f.write_text("hello world")
    art_id = _artifact(session_factory, scored_run["sb1"], str(f), kind="transcript")

    resp = client.get(f"/api/artifacts/{art_id}/download")
    assert resp.status_code == 200
    assert resp.content == b"hello world"
    assert "hello.txt" in resp.headers.get("content-disposition", "")


def test_download_relative_path(client: TestClient, scored_run, session_factory, artifact_root) -> None:
    f = artifact_root / "rel" / "notes.md"
    f.parent.mkdir(parents=True)
    f.write_text("# notes")
    # stored relative to artifact root
    art_id = _artifact(session_factory, scored_run["sb1"], "rel/notes.md")
    resp = client.get(f"/api/artifacts/{art_id}/download")
    assert resp.status_code == 200
    assert resp.content == b"# notes"


def test_download_directory_zipped(client: TestClient, scored_run, session_factory, artifact_root) -> None:
    d = artifact_root / "ws" / "proj"
    (d / "src").mkdir(parents=True)
    (d / "src" / "app.py").write_text("print(1)")
    (d / "README.md").write_text("readme")
    art_id = _artifact(session_factory, scored_run["sb1"], str(d), kind="workspace")

    resp = client.get(f"/api/artifacts/{art_id}/download")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/zip"
    zf = zipfile.ZipFile(io.BytesIO(resp.content))
    names = set(zf.namelist())
    assert "src/app.py" in names
    assert "README.md" in names
    assert zf.read("README.md") == b"readme"


def test_download_traversal_blocked(client: TestClient, scored_run, session_factory, artifact_root) -> None:
    outside = artifact_root.parent / "secret.txt"
    outside.write_text("secret")
    try:
        # relative escape attempt
        art_id = _artifact(session_factory, scored_run["sb1"], "../secret.txt")
        resp = client.get(f"/api/artifacts/{art_id}/download")
        assert resp.status_code == 404

        # absolute path outside the jail
        art_id2 = _artifact(session_factory, scored_run["sb1"], str(outside))
        resp2 = client.get(f"/api/artifacts/{art_id2}/download")
        assert resp2.status_code == 404
    finally:
        outside.unlink()


def test_download_missing_and_unknown(client: TestClient, scored_run, session_factory, artifact_root) -> None:
    art_id = _artifact(session_factory, scored_run["sb1"], str(artifact_root / "nope.txt"))
    assert client.get(f"/api/artifacts/{art_id}/download").status_code == 404
    assert client.get(f"/api/artifacts/{uuid.uuid4()}/download").status_code == 404


def test_download_directory_zip_filters_noise(
    client: TestClient, scored_run, session_factory, artifact_root
) -> None:
    """Workspace ZIPs drop VCS internals, python caches, vendored deps and
    virtualenvs at ANY depth — download-side only; the raw files on disk
    (and every other artifact kind) are untouched."""
    d = artifact_root / "ws" / "proj"
    for rel in (
        "src/app.py",
        "README.md",
        ".env.local",
        ".git/config",
        ".git/objects/aa/bbccddee",
        "__pycache__/app.cpython-312.pyc",
        "src/__pycache__/util.cpython-312.pyc",
        "node_modules/pkg/index.js",
        ".venv/lib/python3.12/site.py",
        "src/legacy.pyc",
    ):
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x")

    art_id = _artifact(session_factory, scored_run["sb1"], str(d), kind="workspace")
    resp = client.get(f"/api/artifacts/{art_id}/download")
    assert resp.status_code == 200
    names = set(zipfile.ZipFile(io.BytesIO(resp.content)).namelist())
    assert names == {"src/app.py", "README.md", ".env.local"}

    # the raw artifact tree is untouched by the download-side filter
    assert (d / ".git" / "config").is_file()
    assert (d / "node_modules" / "pkg" / "index.js").is_file()


# ---------------------------------------------------------------------------
# PR5 — rubric versions + re-score
# ---------------------------------------------------------------------------

DIMS = [
    "functional_requirements",
    "acceptance_criteria",
    "code_quality",
    "cyclomatic_complexity",
]


def _rubric_content(**overrides: Any) -> dict[str, Any]:
    content: dict[str, Any] = {
        "summary": "scores the artifact",
        "dimensions": [{"name": d, "title": d} for d in DIMS],
        "criteria": [],
    }
    content.update(overrides)
    return content


@pytest.fixture()
def rescore_stub(session_factory, tmp_path: Path, monkeypatch):
    """Stub ``lifecycle.rescore_run`` (no docker) while keeping the REAL
    ``runner.start_rescore_task``/``_track``/``in_flight`` wiring."""
    calls: list[dict] = []
    ns = SimpleNamespace(
        ARTIFACT_ROOT=tmp_path / "artifacts",
        HOST_DATA_ROOT="",
    )

    async def fake_rescore(run_id, **kwargs):
        calls.append({"run_id": run_id, **kwargs})

    monkeypatch.setattr(lifecycle_mod, "rescore_run", fake_rescore)
    monkeypatch.setattr(runner, "_session_factory", lambda: session_factory)
    monkeypatch.setattr(runner, "_settings", lambda: ns)
    monkeypatch.setattr(
        runner, "get_runtime", lambda: SimpleNamespace(artifact_root=ns.ARTIFACT_ROOT)
    )
    return calls


def _wait_for(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


# ---- GET /api/runs/{id}/rubrics -------------------------------------------


def test_list_rubric_versions(client: TestClient, scored_run, session_factory) -> None:
    resp = client.get(f"/api/runs/{scored_run['run_id']}/rubrics")
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 1
    assert rows[0]["version"] == 1
    assert rows[0]["status"] == "approved"
    assert rows[0]["id"] == str(scored_run["rubric_id"])
    assert rows[0]["parent_id"] is None
    assert rows[0]["score_count"] == 3  # all scores carry this rubric_id

    assert client.get(f"/api/runs/{uuid.uuid4()}/rubrics").status_code == 404


# ---- POST /api/runs/{id}/rubrics ------------------------------------------


def test_create_rubric_version(client: TestClient, scored_run, session_factory) -> None:
    resp = client.post(
        f"/api/runs/{scored_run['run_id']}/rubrics",
        json={"content": _rubric_content(summary="tightened v2")},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["version"] == 2
    assert body["status"] == "approved"
    assert body["parent_id"] == str(scored_run["rubric_id"])
    assert body["content"]["summary"] == "tightened v2"

    s = session_factory()
    try:
        v1 = s.get(Rubric, scored_run["rubric_id"])
        assert v1.status == "superseded"
    finally:
        s.close()

    # run detail now serves v2 as the latest rubric
    detail = client.get(f"/api/runs/{scored_run['run_id']}").json()
    assert detail["rubric"]["version"] == 2

    # a second save → v3 parented on v2
    resp = client.post(
        f"/api/runs/{scored_run['run_id']}/rubrics",
        json={"content": _rubric_content(summary="v3")},
    )
    assert resp.status_code == 201
    assert resp.json()["version"] == 3
    versions = client.get(f"/api/runs/{scored_run['run_id']}/rubrics").json()
    assert [v["version"] for v in versions] == [3, 2, 1]
    statuses = {v["version"]: v["status"] for v in versions}
    assert statuses == {3: "approved", 2: "superseded", 1: "superseded"}


def test_create_rubric_version_requires_terminal_run(
    client: TestClient, scored_run, session_factory
) -> None:
    s = session_factory()
    try:
        run = s.get(Run, scored_run["run_id"])
        run.status = "running"
        s.commit()
    finally:
        s.close()
    resp = client.post(
        f"/api/runs/{scored_run['run_id']}/rubrics",
        json={"content": _rubric_content()},
    )
    assert resp.status_code == 409


def test_create_rubric_version_validates(
    client: TestClient, scored_run
) -> None:
    # canonical dimensions enforced at the boundary
    bad = _rubric_content()
    bad["dimensions"] = bad["dimensions"][:3]
    assert (
        client.post(
            f"/api/runs/{scored_run['run_id']}/rubrics", json={"content": bad}
        ).status_code
        == 422
    )
    # unknown run / unknown base
    assert (
        client.post(
            f"/api/runs/{uuid.uuid4()}/rubrics",
            json={"content": _rubric_content()},
        ).status_code
        == 404
    )
    assert (
        client.post(
            f"/api/runs/{scored_run['run_id']}/rubrics",
            json={"content": _rubric_content(), "base_rubric_id": str(uuid.uuid4())},
        ).status_code
        == 404
    )


# ---- POST /api/runs/{id}/rescore ------------------------------------------


def test_rescore_triggers_task_and_transitions(
    client: TestClient, scored_run, rescore_stub, session_factory
) -> None:
    resp = client.post(f"/api/runs/{scored_run['run_id']}/rescore", json={})
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["status"] == "evaluating"
    assert body["rubric_id"] == str(scored_run["rubric_id"])
    assert body["rubric_version"] == 1
    assert body["model"] == "qwen3:8b"
    assert body["sandboxes"] == 1  # only sb1 has an artifact_path

    # run flipped to evaluating + finished_at cleared
    s = session_factory()
    try:
        run = s.get(Run, scored_run["run_id"])
        assert run.status == "evaluating"
        assert run.finished_at is None
    finally:
        s.close()

    # the real task registry routed rescore_run with our kwargs
    assert _wait_for(lambda: len(rescore_stub) == 1)
    kw = rescore_stub[0]
    assert kw["run_id"] == scored_run["run_id"]
    assert kw["rubric_id"] == scored_run["rubric_id"]


def test_rescore_explicit_rubric_version_and_subset(
    client: TestClient, scored_run, rescore_stub
) -> None:
    # create a v2 rubric first
    v2 = client.post(
        f"/api/runs/{scored_run['run_id']}/rubrics",
        json={"content": _rubric_content(summary="v2")},
    ).json()
    resp = client.post(
        f"/api/runs/{scored_run['run_id']}/rescore",
        json={"rubric_id": v2["id"], "sandbox_ids": [str(scored_run["sb1"])]},
    )
    assert resp.status_code == 202, resp.text
    assert resp.json()["rubric_version"] == 2

    assert _wait_for(lambda: len(rescore_stub) == 1)
    kw = rescore_stub[0]
    assert str(kw["rubric_id"]) == v2["id"]
    assert [str(s) for s in kw["sandbox_ids"]] == [str(scored_run["sb1"])]


def test_rescore_guards(client: TestClient, scored_run, session_factory, rescore_stub) -> None:
    run_id = scored_run["run_id"]
    # unknown run / rubric
    assert client.post(f"/api/runs/{uuid.uuid4()}/rescore", json={}).status_code == 404
    assert (
        client.post(f"/api/runs/{run_id}/rescore", json={"rubric_id": str(uuid.uuid4())}).status_code
        == 404
    )
    # sandbox not in run → 422
    assert (
        client.post(
            f"/api/runs/{run_id}/rescore",
            json={"sandbox_ids": [str(uuid.uuid4())]},
        ).status_code
        == 422
    )
    # sandbox without artifact → 422
    assert (
        client.post(
            f"/api/runs/{run_id}/rescore",
            json={"sandbox_ids": [str(scored_run["sb2"])]},
        ).status_code
        == 422
    )
    # active run → 409
    s = session_factory()
    try:
        run = s.get(Run, run_id)
        run.status = "running"
        s.commit()
    finally:
        s.close()
    assert client.post(f"/api/runs/{run_id}/rescore", json={}).status_code == 409
    s = session_factory()
    try:
        run = s.get(Run, run_id)
        run.status = "done"
        s.commit()
    finally:
        s.close()
    # run with zero artifact-bearing sandboxes → 409
    s = session_factory()
    try:
        bare = Run(task_prompt="x", model_name="m", config={}, status="done")
        s.add(bare)
        s.commit()
        bare_id = bare.id
        s.add(Rubric(run_id=bare_id, version=1, status="approved", content={}))
        s.commit()
    finally:
        s.close()
    assert client.post(f"/api/runs/{bare_id}/rescore", json={}).status_code == 409


def test_rescore_cancelled_run_409(
    client: TestClient, scored_run, session_factory
) -> None:
    """Cancelled is terminal for delete/sweep, but nothing ever ran —
    a rescore 409s with the cancelled-specific detail, not the generic
    terminal-run guard or a bogus rubric/artifact failure."""
    s = session_factory()
    try:
        run = s.get(Run, scored_run["run_id"])
        run.status = "cancelled"
        s.commit()
    finally:
        s.close()
    resp = client.post(f"/api/runs/{scored_run['run_id']}/rescore", json={})
    assert resp.status_code == 409
    assert "cancelled" in resp.json()["detail"]


def test_rescore_in_flight_conflict(client: TestClient, scored_run) -> None:
    """A second trigger while a rescore task is in flight → 409.

    The shared TestClient is not context-managed, so each request runs on a
    fresh event-loop portal — a real spawned task can't stay 'in flight'
    across requests here. Seed the registry with a never-done task instead
    (the registry/dedup behaviour itself is covered in
    tests/unit/orchestrator/test_rescore.py)."""

    class _PendingTask:
        def done(self) -> bool:
            return False

    key = (scored_run["run_id"], "rescore")
    runner._TASKS[key] = _PendingTask()  # type: ignore[assignment]
    try:
        resp = client.post(f"/api/runs/{scored_run['run_id']}/rescore", json={})
        assert resp.status_code == 409
        assert "already in progress" in resp.json()["detail"]
    finally:
        runner._TASKS.pop(key, None)


def test_create_rubric_version_with_rescore(
    client: TestClient, scored_run, rescore_stub, session_factory
) -> None:
    resp = client.post(
        f"/api/runs/{scored_run['run_id']}/rubrics",
        json={"content": _rubric_content(summary="v2"), "rescore": True},
    )
    assert resp.status_code == 201, resp.text
    v2 = resp.json()

    s = session_factory()
    try:
        run = s.get(Run, scored_run["run_id"])
        assert run.status == "evaluating"
    finally:
        s.close()

    assert _wait_for(lambda: len(rescore_stub) == 1)
    assert str(rescore_stub[0]["rubric_id"]) == v2["id"]


# ---- results ?rubric_version= filter ---------------------------------------


def test_results_rubric_version_filter(
    client: TestClient, scored_run, session_factory
) -> None:
    """v1 + v2 scores coexist; the filter selects one version's provenance,
    the default view keeps newest-wins."""
    s = session_factory()
    try:
        v2 = Rubric(
            run_id=scored_run["run_id"], version=2, status="approved",
            parent_id=scored_run["rubric_id"],
            content={"dimensions": [{"name": "functional_requirements"}]},
        )
        # creating v2 directly: v1 must be superseded for realism
        v1 = s.get(Rubric, scored_run["rubric_id"])
        v1.status = "superseded"
        s.add(v2)
        s.flush()
        s.add_all([
            Score(
                sandbox_id=scored_run["sb1"], rubric_id=v2.id, rubric_version=2,
                dimension="functional_requirements", score=9.5,
                rationale="re-judged under v2",
            ),
            Score(
                sandbox_id=scored_run["sb1"], rubric_id=v2.id, rubric_version=2,
                dimension="overall", score=9.0, rationale="better",
            ),
        ])
        s.commit()
        v2_id = v2.id
    finally:
        s.close()

    # default view: newest-wins → v2 scores surface
    body = client.get(f"/api/runs/{scored_run['run_id']}/results").json()
    sb1 = next(x for x in body["sandboxes"] if x["sandbox_id"] == str(scored_run["sb1"]))
    assert sb1["dimensions"]["functional_requirements"]["score"] == 9.5
    assert sb1["overall"]["score"] == 9.0
    assert sb1["rubric_version"] == 2
    assert body["selected_rubric_version"] is None
    versions = {v["version"]: v for v in body["rubric_versions"]}
    assert versions[1]["score_count"] == 3
    assert versions[2]["score_count"] == 2
    assert versions[1]["status"] == "superseded"

    # ?rubric_version=1 → only v1 provenance
    body = client.get(
        f"/api/runs/{scored_run['run_id']}/results", params={"rubric_version": 1}
    ).json()
    sb1 = next(x for x in body["sandboxes"] if x["sandbox_id"] == str(scored_run["sb1"]))
    assert sb1["dimensions"]["functional_requirements"]["score"] == 8.0
    assert sb1["overall"]["score"] == 7.0
    assert sb1["rubric_version"] == 1
    assert body["selected_rubric_version"] == 1

    # version with no scores → empty dimensions but no error
    s = session_factory()
    try:
        s.add(
            Rubric(
                run_id=scored_run["run_id"], version=3, status="approved",
                content={}, parent_id=v2_id,
            )
        )
        s.commit()
    finally:
        s.close()
    body = client.get(
        f"/api/runs/{scored_run['run_id']}/results", params={"rubric_version": 3}
    ).json()
    sb1 = next(x for x in body["sandboxes"] if x["sandbox_id"] == str(scored_run["sb1"]))
    assert sb1["dimensions"] == {}
    assert sb1["rubric_version"] is None


def test_sandbox_scores_expose_rubric_version(
    client: TestClient, scored_run
) -> None:
    rows = client.get(f"/api/sandboxes/{scored_run['sb1']}/scores").json()
    assert all(r["rubric_version"] == 1 for r in rows)


def test_results_same_second_passes_newest_inserted_wins(
    client: TestClient, scored_run, session_factory
) -> None:
    """created_at ties (sqlite timestamps are second-precision): the
    LAST-INSERTED pass wins per dimension via seq — even when the newer
    pass re-judged against an OLDER rubric version. A version-based
    tiebreak would show the stale v2 score here."""
    ts = datetime(2099, 1, 1, tzinfo=UTC)  # identical instant, both passes
    s = session_factory()
    try:
        v2 = Rubric(
            run_id=scored_run["run_id"], version=2, status="approved",
            parent_id=scored_run["rubric_id"],
            content={"dimensions": [{"name": "functional_requirements"}]},
        )
        s.add(v2)
        s.flush()
        s.add_all([
            # Earlier pass, judged under v2.
            Score(
                sandbox_id=scored_run["sb1"], rubric_id=v2.id,
                rubric_version=2, seq=10, created_at=ts,
                dimension="functional_requirements", score=9.5,
                rationale="v2 pass",
            ),
            # Later pass, re-pinned to v1 — must win despite lower version.
            Score(
                sandbox_id=scored_run["sb1"], rubric_id=scored_run["rubric_id"],
                rubric_version=1, seq=20, created_at=ts,
                dimension="functional_requirements", score=3.0,
                rationale="v1 rescore pass",
            ),
        ])
        s.commit()
    finally:
        s.close()

    body = client.get(f"/api/runs/{scored_run['run_id']}/results").json()
    sb1 = next(
        x for x in body["sandboxes"]
        if x["sandbox_id"] == str(scored_run["sb1"])
    )
    assert sb1["dimensions"]["functional_requirements"] == {
        "score": 3.0, "rationale": "v1 rescore pass"
    }
    assert sb1["rubric_version"] == 1


# ---------------------------------------------------------------------------
# PR34 — artifact file browser + side-by-side diff
# ---------------------------------------------------------------------------


@pytest.fixture()
def browsed_run(session_factory, artifact_root):
    """Run + two sandbox workspaces + one prompt cell, all as REAL artifact
    dirs at the conventional ``<root>/<run>/<sandbox>`` layout (no
    ``artifact_path`` needed — the browse root resolves conventionally)."""
    s = session_factory()
    try:
        run = Run(
            task_prompt="build 2048", model_name="m", config={}, status="done"
        )
        s.add(run)
        s.flush()

        def mk(name: str, cfg: dict[str, Any]) -> Sandbox:
            sb = Sandbox(
                run_id=run.id,
                name=name,
                harness_config=cfg,
                status="done",
                terminal_reason="agent_done",
            )
            s.add(sb)
            s.flush()
            return sb

        sb1 = mk("one", {"adapter": "miniloop"})
        sb2 = mk("two", {"adapter": "miniloop"})
        sbp = mk("prompt", {"adapter": "prompt"})
        s.commit()
        ids = (run.id, sb1.id, sb2.id, sbp.id)
    finally:
        s.close()
    run_id, sb1_id, sb2_id, sbp_id = ids

    def art_dir(sb_id: uuid.UUID) -> Path:
        return artifact_root / str(run_id) / str(sb_id)

    ws1 = art_dir(sb1_id) / "workspace"
    (ws1 / "src").mkdir(parents=True)
    (ws1 / "index.html").write_text(
        '<h1>2048</h1>\n<script src="game.js"></script>\n'
    )
    (ws1 / "src" / "app.py").write_text("print('a')\n")
    (ws1 / "README.md").write_text("readme one\n")
    (ws1 / ".env.local").write_text("KEY=1\n")

    ws2 = art_dir(sb2_id) / "workspace"
    ws2.mkdir(parents=True)
    (ws2 / "index.html").write_text(
        '<h1>2048!</h1>\n<script src="game.js"></script>\n'
    )
    (ws2 / "solo.txt").write_text("only in b\n")

    pdir = art_dir(sbp_id)
    pdir.mkdir(parents=True)
    (pdir / "output.txt").write_text("the whole answer\n")
    (pdir / "meta.json").write_text('{"adapter": "prompt"}\n')

    return {
        "run": run_id,
        "sb1": sb1_id,
        "sb2": sb2_id,
        "sbp": sbp_id,
        "root": artifact_root,
        "ws1": ws1,
        "ws2": ws2,
        "pdir": pdir,
    }


def test_files_list_workspace(client: TestClient, browsed_run) -> None:
    """Listing a real artifact tree returns dirs + files with sizes."""
    resp = client.get(f"/api/sandboxes/{browsed_run['sb1']}/files")
    assert resp.status_code == 200
    body = resp.json()
    assert body["sandbox_id"] == str(browsed_run["sb1"])
    assert body["root"] == "workspace"
    assert body["truncated"] is False
    by_path = {e["path"]: e for e in body["entries"]}
    assert set(by_path) == {"index.html", "README.md", ".env.local", "src", "src/app.py"}
    assert by_path["src"] == {"path": "src", "kind": "dir", "size": None}
    assert by_path["index.html"]["kind"] == "file"
    assert by_path["index.html"]["size"] == len(
        b'<h1>2048</h1>\n<script src="game.js"></script>\n'
    )
    # sorted, deterministic order
    assert [e["path"] for e in body["entries"]] == sorted(by_path)


def test_files_list_prompt_cell(client: TestClient, browsed_run) -> None:
    """Prompt cells have no workspace/ — the artifact root is browsed so
    output.txt surfaces alongside meta/transcript."""
    body = client.get(f"/api/sandboxes/{browsed_run['sbp']}/files").json()
    assert body["root"] == "artifact"
    names = {e["path"] for e in body["entries"]}
    assert {"output.txt", "meta.json"} <= names


def test_files_list_404(
    client: TestClient, browsed_run, session_factory
) -> None:
    assert client.get(f"/api/sandboxes/{uuid.uuid4()}/files").status_code == 404
    # a sandbox row with no artifact dir on disk
    s = session_factory()
    try:
        sb = Sandbox(
            run_id=browsed_run["run"], harness_config={}, status="done"
        )
        s.add(sb)
        s.commit()
        sid = sb.id
    finally:
        s.close()
    assert client.get(f"/api/sandboxes/{sid}/files").status_code == 404


def test_files_list_excludes_noise(client: TestClient, browsed_run) -> None:
    """Same exclusions as the workspace ZIP: VCS internals, caches,
    vendored deps, venvs — at any depth. Hidden FILES still list."""
    ws1 = browsed_run["ws1"]
    (ws1 / ".git").mkdir()
    (ws1 / ".git" / "config").write_text("x")
    (ws1 / "__pycache__").mkdir()
    (ws1 / "__pycache__" / "a.cpython-312.pyc").write_text("x")
    (ws1 / "node_modules").mkdir()
    (ws1 / "node_modules" / "pkg.js").write_text("x")
    (ws1 / "app.pyc").write_text("x")

    body = client.get(f"/api/sandboxes/{browsed_run['sb1']}/files").json()
    names = {e["path"] for e in body["entries"]}
    assert ".git" not in names
    assert ".git/config" not in names
    assert "__pycache__" not in names
    assert "node_modules" not in names
    assert "app.pyc" not in names
    assert ".env.local" in names  # hidden files DO list (zip parity)


def test_files_list_bounded(
    client: TestClient, browsed_run, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Depth + entry caps truncate the listing instead of walking forever."""
    monkeypatch.setattr(results_router, "_BROWSE_MAX_DEPTH", 1)
    body = client.get(f"/api/sandboxes/{browsed_run['sb1']}/files").json()
    names = {e["path"] for e in body["entries"]}
    assert "src" in names and "src/app.py" not in names
    assert body["truncated"] is True

    monkeypatch.setattr(results_router, "_BROWSE_MAX_DEPTH", 8)
    monkeypatch.setattr(results_router, "_BROWSE_MAX_ENTRIES", 3)
    body = client.get(f"/api/sandboxes/{browsed_run['sb1']}/files").json()
    assert len(body["entries"]) <= 3
    assert body["truncated"] is True


def test_files_read_text_and_download(client: TestClient, browsed_run) -> None:
    text = '<h1>2048</h1>\n<script src="game.js"></script>\n'
    resp = client.get(f"/api/sandboxes/{browsed_run['sb1']}/files/index.html")
    assert resp.status_code == 200
    body = resp.json()
    assert body["path"] == "index.html"
    assert body["binary"] is False
    assert body["truncated"] is False
    assert body["text"] == text
    assert body["size"] == len(text.encode())

    dl = client.get(
        f"/api/sandboxes/{browsed_run['sb1']}/files/src/app.py",
        params={"download": "1"},
    )
    assert dl.status_code == 200
    assert dl.content == b"print('a')\n"
    assert "app.py" in dl.headers.get("content-disposition", "")


def test_files_read_caps_size(
    client: TestClient, browsed_run, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file longer than the text cap returns its head + truncated."""
    monkeypatch.setattr(results_router, "_FILE_TEXT_CAP_BYTES", 8)
    body = client.get(
        f"/api/sandboxes/{browsed_run['sb1']}/files/index.html"
    ).json()
    assert body["truncated"] is True
    assert body["text"] == "<h1>2048"
    assert body["size"] > 8


def test_files_read_binary_and_prompt_output(
    client: TestClient, browsed_run
) -> None:
    """Binary content → binary flag + null text; prompt-cell output.txt is
    readable through the same endpoint at the artifact root."""
    png = browsed_run["ws1"] / "icon.png"
    png.write_bytes(b"\x89PNG\x00\x0d\x0a\x1a\x0a")
    body = client.get(
        f"/api/sandboxes/{browsed_run['sb1']}/files/icon.png"
    ).json()
    assert body["binary"] is True
    assert body["text"] is None

    body = client.get(
        f"/api/sandboxes/{browsed_run['sbp']}/files/output.txt"
    ).json()
    assert body["binary"] is False
    assert body["text"] == "the whole answer\n"


def test_files_traversal_404(client: TestClient, browsed_run) -> None:
    """The browse root is jailed exactly like the download path."""
    outside = browsed_run["root"].parent / "secret.txt"
    outside.write_text("secret")
    try:
        sb1 = browsed_run["sb1"]
        # %2e%2e survives client-side URL normalization — escapes the jail.
        assert (
            client.get(
                f"/api/sandboxes/{sb1}/files/"
                "%2e%2e%2f%2e%2e%2f%2e%2e%2f%2e%2e%2fsecret.txt"
            ).status_code
            == 404
        )
        # escape into the SIBLING sandbox's workspace: inside ARTIFACT_ROOT
        # but outside this sandbox's browse root → still 404.
        sb2 = browsed_run["sb2"]
        assert (
            client.get(
                f"/api/sandboxes/{sb1}/files/"
                f"..%2f..%2f{sb2}%2fworkspace%2findex.html"
            ).status_code
            == 404
        )
    finally:
        outside.unlink()
    sb1 = browsed_run["sb1"]
    # absolute path → treated as in-root relative → not found → 404
    assert (
        client.get(f"/api/sandboxes/{sb1}/files/%2fetc%2fpasswd").status_code
        == 404
    )
    # a directory is not a file
    assert client.get(f"/api/sandboxes/{sb1}/files/src").status_code == 404
    # missing file
    assert client.get(f"/api/sandboxes/{sb1}/files/nope.txt").status_code == 404


def test_files_diff(client: TestClient, browsed_run) -> None:
    """?diff=<other> renders a server-side unified diff for text files."""
    resp = client.get(
        f"/api/sandboxes/{browsed_run['sb1']}/files/index.html",
        params={"diff": str(browsed_run["sb2"])},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["identical"] is False
    assert body["a_missing"] is False and body["b_missing"] is False
    assert body["binary"] is False
    diff = body["diff"]
    assert diff.startswith("--- a/index.html\n+++ b/index.html\n@@")
    assert '-<h1>2048</h1>' in diff
    assert '+<h1>2048!</h1>' in diff
    assert ' <script src="game.js"></script>' in diff  # context line


def test_files_diff_identical(client: TestClient, browsed_run) -> None:
    """Byte-equal files → identical flag, empty diff body."""
    (browsed_run["ws2"] / "README.md").write_text("readme one\n")
    body = client.get(
        f"/api/sandboxes/{browsed_run['sb1']}/files/README.md",
        params={"diff": str(browsed_run["sb2"])},
    ).json()
    assert body["identical"] is True
    assert body["diff"] == ""


def test_files_diff_missing_side(client: TestClient, browsed_run) -> None:
    """A path on only one side diffs against empty — full +/- body."""
    # solo.txt lives only in sb2: diffing FROM sb1 shows all-added.
    body = client.get(
        f"/api/sandboxes/{browsed_run['sb1']}/files/solo.txt",
        params={"diff": str(browsed_run["sb2"])},
    ).json()
    assert body["a_missing"] is True and body["b_missing"] is False
    assert "+only in b" in body["diff"]

    # reverse direction → all-removed.
    body = client.get(
        f"/api/sandboxes/{browsed_run['sb2']}/files/solo.txt",
        params={"diff": str(browsed_run["sb1"])},
    ).json()
    assert body["a_missing"] is False and body["b_missing"] is True
    assert "-only in b" in body["diff"]

    # a file on NEITHER side → 404.
    assert (
        client.get(
            f"/api/sandboxes/{browsed_run['sb1']}/files/nope.py",
            params={"diff": str(browsed_run["sb2"])},
        ).status_code
        == 404
    )


def test_files_diff_cross_root(client: TestClient, browsed_run) -> None:
    """Prompt-cell artifact root diffs against a workspace browse root —
    output.txt exists on one side only."""
    body = client.get(
        f"/api/sandboxes/{browsed_run['sbp']}/files/output.txt",
        params={"diff": str(browsed_run["sb1"])},
    ).json()
    assert body["a_missing"] is False and body["b_missing"] is True
    assert "-the whole answer" in body["diff"]


def test_files_diff_unknown_and_binary(client: TestClient, browsed_run) -> None:
    sb1, sb2 = browsed_run["sb1"], browsed_run["sb2"]
    # unknown other sandbox → 404
    assert (
        client.get(
            f"/api/sandboxes/{sb1}/files/index.html",
            params={"diff": str(uuid.uuid4())},
        ).status_code
        == 404
    )
    # a binary side → binary flag, empty diff (never garbage)
    (browsed_run["ws1"] / "icon.png").write_bytes(b"\x89PNG\x00\x0d")
    body = client.get(
        f"/api/sandboxes/{sb1}/files/icon.png", params={"diff": str(sb2)}
    ).json()
    assert body["binary"] is True
    assert body["diff"] == ""


def test_files_nul_path_404(client: TestClient, browsed_run) -> None:
    """A percent-encoded NUL in the browsed path makes resolve() raise
    ValueError — it must 404 like traversal, on the read AND diff paths,
    never escape as a 500."""
    sb1, sb2 = browsed_run["sb1"], browsed_run["sb2"]
    assert (
        client.get(f"/api/sandboxes/{sb1}/files/%00x").status_code == 404
    )
    assert (
        client.get(
            f"/api/sandboxes/{sb1}/files/%00x",
            params={"diff": str(sb2)},
        ).status_code
        == 404
    )


def test_files_unreadable_404(client: TestClient, browsed_run) -> None:
    """A candidate file with chmod-0 permissions resolves fine (stat works)
    but can't be opened — read, download, and diff all 404 rather than
    500ing or diffing the present file as absent."""
    sb1, sb2 = browsed_run["sb1"], browsed_run["sb2"]
    locked = browsed_run["ws1"] / "locked.txt"
    locked.write_text("secret\n")
    locked.chmod(0)
    try:
        if os.access(locked, os.R_OK):
            pytest.skip("privileged run — chmod 0 file still readable")
        assert (
            client.get(f"/api/sandboxes/{sb1}/files/locked.txt").status_code
            == 404
        )
        assert (
            client.get(
                f"/api/sandboxes/{sb1}/files/locked.txt",
                params={"download": "1"},
            ).status_code
            == 404
        )
        assert (
            client.get(
                f"/api/sandboxes/{sb1}/files/locked.txt",
                params={"diff": str(sb2)},
            ).status_code
            == 404
        )
    finally:
        locked.chmod(0o644)


def test_files_list_entry_cap_flags_only_on_cut(
    client: TestClient, browsed_run, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The entry cap flags ``truncated`` only when a browsable entry is
    actually cut — a tree with exactly cap entries isn't truncated."""
    total = 5  # index.html, README.md, .env.local, src, src/app.py
    monkeypatch.setattr(results_router, "_BROWSE_MAX_ENTRIES", total)
    body = client.get(f"/api/sandboxes/{browsed_run['sb1']}/files").json()
    assert len(body["entries"]) == total
    assert body["truncated"] is False
    monkeypatch.setattr(results_router, "_BROWSE_MAX_ENTRIES", total - 1)
    body = client.get(f"/api/sandboxes/{browsed_run['sb1']}/files").json()
    assert len(body["entries"]) == total - 1
    assert body["truncated"] is True


def test_files_list_depth_cutoff_counts_only_browsable(
    client: TestClient, browsed_run, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The depth cutoff flags ``truncated`` only when a BROWSABLE entry is
    actually cut — a capped dir holding only pruned names isn't a cut."""
    deep = browsed_run["ws1"] / "src" / "deep"
    deep.mkdir()
    (deep / "noise.pyc").write_text("x")
    monkeypatch.setattr(results_router, "_BROWSE_MAX_DEPTH", 2)
    body = client.get(f"/api/sandboxes/{browsed_run['sb1']}/files").json()
    assert body["truncated"] is False
    (deep / "real.txt").write_text("x")
    body = client.get(f"/api/sandboxes/{browsed_run['sb1']}/files").json()
    assert body["truncated"] is True


def test_files_diff_identical_requires_full_equality(
    client: TestClient, browsed_run, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two files sharing the capped head but differing afterwards are NOT
    identical — the flag requires whole-file byte equality."""
    monkeypatch.setattr(results_router, "_DIFF_SIDE_CAP_BYTES", 8)
    (browsed_run["ws1"] / "big.txt").write_text("AAAAAAAAtail-a\n")
    (browsed_run["ws2"] / "big.txt").write_text("AAAAAAAAtail-b\n")
    body = client.get(
        f"/api/sandboxes/{browsed_run['sb1']}/files/big.txt",
        params={"diff": str(browsed_run["sb2"])},
    ).json()
    assert body["identical"] is False
    assert body["truncated"] is True


def test_files_list_unreadable_dir_flags_truncated(
    client: TestClient, browsed_run
) -> None:
    """A chmod-0 directory can't be scandir'd — the walk's onerror flags
    ``truncated`` so the subtree is reported as cut, never silently absent."""
    locked = browsed_run["ws1"] / "src" / "locked"
    locked.mkdir()
    (locked / "hidden.txt").write_text("x")
    locked.chmod(0)
    try:
        if os.access(locked, os.R_OK | os.X_OK):
            pytest.skip("privileged run — chmod 0 dir still listable")
        body = client.get(f"/api/sandboxes/{browsed_run['sb1']}/files").json()
        names = {e["path"] for e in body["entries"]}
        assert "src/locked/hidden.txt" not in names
        assert body["truncated"] is True
    finally:
        locked.chmod(0o755)
