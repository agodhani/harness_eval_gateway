"""PR26 lifecycle integration — deterministic suite checks measured
post-artifact, pre-judge; persisted as checker_results + events.

Hermetic: file-backed sqlite + FakeRuntime; ``settings.BENCHMARK_SUITES_DIR``
is pointed at a tmp vendored suite per test. ``py_test`` never reaches real
docker — a fake ``ExecRunner.execute`` is monkeypatched in when the test
needs a real test_execution event.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import select

from db.models import CheckerResult, Event, Rubric, Run, Sandbox, Score
from orchestrator.lifecycle import run_lifecycle
from sandbox import exec as exec_mod
from sandbox.exec import ExecRunner

from .conftest import FakeRuntime, make_run, run_config

RESULT_DONE = "MINILOOP_RESULT " + json.dumps(
    {"terminal_state": "agent_done", "iters": 2}
) + "\n"

SUITE_YAML = """\
id: lifecycle-suite
version: 1
title: Lifecycle suite
tasks:
  - id: build-the-file
    prompt: "build it"
    check:
      file_exists: hello.txt
      grep:
        path: hello.txt
        pattern: "hi"
      output_grep: "TOTAL:"
"""

SUITE_PYTEST_YAML = """\
id: pytest-suite
version: 1
title: Pytest suite
tasks:
  - id: has-tests
    prompt: "build it"
    check:
      py_test: tests/wordle
"""


@pytest.fixture()
def suites_dir(tmp_path: Path) -> Path:
    root = tmp_path / "benchmark_suites"
    (root / "lifecycle-suite").mkdir(parents=True)
    (root / "lifecycle-suite" / "suite.yaml").write_text(SUITE_YAML)
    (root / "pytest-suite").mkdir()
    (root / "pytest-suite" / "suite.yaml").write_text(SUITE_PYTEST_YAML)
    (root / "pytest-suite" / "tests" / "wordle").mkdir(parents=True)
    (root / "pytest-suite" / "tests" / "wordle" / "test_x.py").write_text(
        "def test_ok():\n    assert True\n"
    )
    return root


def _approved_rubric(session_factory, run_id) -> Rubric:
    with session_factory() as s:
        r = Rubric(
            run_id=run_id,
            version=1,
            status="approved",
            content={"summary": "s", "dimensions": [], "criteria": []},
        )
        s.add(r)
        s.commit()
        return r


def _suite_run(session_factory, *, suite_ref: str, suite_index: int = 0):
    """A run carrying suite provenance — as POST /api/suites/{slug}/run leaves it."""
    run = make_run(session_factory, status="provisioning", config=run_config())
    with session_factory() as s:
        r = s.get(Run, run.id)
        r.suite_ref = suite_ref
        r.suite_index = suite_index
        s.commit()
    return run


def _score_writing_evaluator(calls: list):
    async def fake_eval(**kwargs):
        calls.append(kwargs)
        with kwargs["session_factory"]() as s:
            s.add(
                Score(
                    sandbox_id=kwargs["sandbox_id"],
                    rubric_id=kwargs["rubric_id"],
                    dimension="overall",
                    score=8.0,
                    rationale="looks fine",
                )
            )
            s.commit()
        return []

    return fake_eval


async def test_suite_run_persists_checker_results_and_event(
    session_factory, settings, artifact_root, suites_dir
) -> None:
    """End-to-end: a suite task's checks land as checker_results rows +
    a suite_checks checkpoint event, alongside normal judge scoring."""
    settings.BENCHMARK_SUITES_DIR = suites_dir
    run = _suite_run(session_factory, suite_ref="lifecycle-suite")
    _approved_rubric(session_factory, run.id)
    calls: list = []

    await run_lifecycle(
        run.id,
        session_factory=session_factory,
        runtime=FakeRuntime(artifact_root=artifact_root, default_logs=RESULT_DONE),
        settings=settings,
        evaluator_fn=_score_writing_evaluator(calls),
    )

    with session_factory() as s:
        assert s.get(Run, run.id).status == "done"
        rows = list(s.execute(select(CheckerResult)).scalars())
        assert {r.check_type for r in rows} == {
            "file_exists",
            "grep",
            "output_grep",
        }
        by_type = {r.check_type: r for r in rows}
        # FakeRuntime writes workspace/hello.txt containing "hi"
        assert by_type["file_exists"].status == "pass"
        assert by_type["file_exists"].passed is True
        assert by_type["grep"].status == "pass"
        assert by_type["output_grep"].status == "fail"  # no output.txt
        # suite_pass score row: 2/3 measured → 10 * 2/3, rubric-free.
        suite_pass = [
            sc for sc in s.execute(select(Score)).scalars()
            if sc.dimension == "suite_pass"
        ]
        assert len(suite_pass) == 1
        assert suite_pass[0].rubric_id is None
        assert abs(suite_pass[0].score - (10 * 2 / 3)) < 0.01
        assert "2/3" in suite_pass[0].rationale
        events = list(s.execute(select(Event)).scalars())
        checks_ev = [
            e
            for e in events
            if e.type == "checkpoint" and e.payload.get("stage") == "suite_checks"
        ]
        assert len(checks_ev) == 1
        payload = checks_ev[0].payload
        assert payload["passed"] == 2
        assert payload["failed"] == 1
        assert {c["check"] for c in payload["checks"]} == {
            "file_exists",
            "grep",
            "output_grep",
        }
        # Judge still ran — checks complement, not replace.
        assert len(calls) == 1
        assert s.execute(select(Score)).scalars().all()

    # suite_checks.json landed on the artifact for the evaluator's
    # ``suite`` signal — provenance + per-check results + roll-up.
    with session_factory() as s:
        sb = s.execute(select(Sandbox)).scalar_one()
        doc_path = Path(sb.artifact_path) / "suite_checks.json"
        doc = json.loads(doc_path.read_text())
        assert doc["suite_ref"] == "lifecycle-suite"
        assert doc["suite_index"] == 0
        assert doc["passed"] == 2 and doc["failed"] == 1
        assert doc["pass_rate"] == pytest.approx(2 / 3)
        assert {c["check"] for c in doc["checks"]} == {
            "file_exists",
            "grep",
            "output_grep",
        }


async def test_non_suite_run_writes_no_checker_rows(
    session_factory, settings, artifact_root, suites_dir
) -> None:
    settings.BENCHMARK_SUITES_DIR = suites_dir
    run = make_run(session_factory, status="provisioning", config=run_config())
    _approved_rubric(session_factory, run.id)

    await run_lifecycle(
        run.id,
        session_factory=session_factory,
        runtime=FakeRuntime(artifact_root=artifact_root, default_logs=RESULT_DONE),
        settings=settings,
        evaluator_fn=_score_writing_evaluator([]),
    )

    with session_factory() as s:
        assert s.get(Run, run.id).status == "done"
        assert s.execute(select(CheckerResult)).scalars().all() == []
        stages = {
            e.payload.get("stage")
            for e in s.execute(select(Event)).scalars()
            if e.type == "checkpoint"
        }
        assert "suite_checks" not in stages


async def test_suite_run_deleted_suite_skips_checks(
    session_factory, settings, artifact_root, suites_dir
) -> None:
    """A renamed/removed suite can't be re-resolved at eval time — checks
    skip cleanly and the run still completes."""
    settings.BENCHMARK_SUITES_DIR = suites_dir
    run = _suite_run(session_factory, suite_ref="does-not-exist")
    _approved_rubric(session_factory, run.id)
    calls: list = []

    await run_lifecycle(
        run.id,
        session_factory=session_factory,
        runtime=FakeRuntime(artifact_root=artifact_root, default_logs=RESULT_DONE),
        settings=settings,
        evaluator_fn=_score_writing_evaluator(calls),
    )

    with session_factory() as s:
        assert s.get(Run, run.id).status == "done"
        assert s.execute(select(CheckerResult)).scalars().all() == []
        assert len(calls) == 1


async def test_suite_run_index_out_of_range_skips_checks(
    session_factory, settings, artifact_root, suites_dir
) -> None:
    settings.BENCHMARK_SUITES_DIR = suites_dir
    run = _suite_run(session_factory, suite_ref="lifecycle-suite", suite_index=9)
    _approved_rubric(session_factory, run.id)

    await run_lifecycle(
        run.id,
        session_factory=session_factory,
        runtime=FakeRuntime(artifact_root=artifact_root, default_logs=RESULT_DONE),
        settings=settings,
        evaluator_fn=_score_writing_evaluator([]),
    )

    with session_factory() as s:
        assert s.get(Run, run.id).status == "done"
        assert s.execute(select(CheckerResult)).scalars().all() == []


async def test_py_test_check_emits_test_execution_event(
    session_factory, settings, artifact_root, suites_dir, monkeypatch
) -> None:
    """A py_test check that runs to completion mirrors the test_execution
    event convention (phase='suite_check') like eval_exec/generated_tests."""
    settings.BENCHMARK_SUITES_DIR = suites_dir
    run = _suite_run(session_factory, suite_ref="pytest-suite")
    _approved_rubric(session_factory, run.id)

    def fake_execute(self, workspace, *, test_target=None, **_kw):
        outcome = exec_mod.ExecOutcome(image=self.image)
        outcome.result = {
            "phases": {
                "tests": {
                    "detected": True,
                    "ok": True,
                    "collected": 3,
                    "passed": 3,
                    "failed": 0,
                    "errors": 0,
                    "skipped": 0,
                    "duration_seconds": 0.2,
                }
            }
        }
        return outcome

    monkeypatch.setattr(ExecRunner, "execute", fake_execute)

    await run_lifecycle(
        run.id,
        session_factory=session_factory,
        runtime=FakeRuntime(artifact_root=artifact_root, default_logs=RESULT_DONE),
        settings=settings,
        evaluator_fn=_score_writing_evaluator([]),
    )

    with session_factory() as s:
        assert s.get(Run, run.id).status == "done"
        row = s.execute(select(CheckerResult)).scalar_one()
        assert row.check_type == "py_test"
        assert row.status == "pass"
        assert "3/3" in row.detail
        test_evs = [
            e
            for e in s.execute(select(Event)).scalars()
            if e.type == "test_execution"
        ]
        assert len(test_evs) == 1
        assert test_evs[0].payload["phase"] == "suite_check"
        assert test_evs[0].payload["ok"] is True
        assert test_evs[0].payload["collected"] == 3


async def test_py_test_without_docker_is_skipped_not_failed(
    session_factory, settings, artifact_root, suites_dir, monkeypatch
) -> None:
    """No exec image / daemon → skipped, run completes; the red verdict is
    reserved for real measurements."""
    settings.BENCHMARK_SUITES_DIR = suites_dir
    run = _suite_run(session_factory, suite_ref="pytest-suite")
    _approved_rubric(session_factory, run.id)

    def unavailable(self, workspace, *, test_target=None, **_kw):
        outcome = exec_mod.ExecOutcome(image=self.image)
        outcome.unavailable = True
        outcome.error = "docker daemon unavailable"
        return outcome

    monkeypatch.setattr(ExecRunner, "execute", unavailable)

    await run_lifecycle(
        run.id,
        session_factory=session_factory,
        runtime=FakeRuntime(artifact_root=artifact_root, default_logs=RESULT_DONE),
        settings=settings,
        evaluator_fn=_score_writing_evaluator([]),
    )

    with session_factory() as s:
        assert s.get(Run, run.id).status == "done"
        row = s.execute(select(CheckerResult)).scalar_one()
        assert row.status == "skipped"
        assert row.passed is False
        # skipped checks emit no test_execution event
        assert [
            e
            for e in s.execute(select(Event)).scalars()
            if e.type == "test_execution" and e.payload.get("phase") == "suite_check"
        ] == []
        # Nothing measured → no suite_pass score row (no verdict to report).
        assert [
            sc for sc in s.execute(select(Score)).scalars()
            if sc.dimension == "suite_pass"
        ] == []


async def test_checker_crash_isolated_from_eval(
    session_factory, settings, artifact_root, suites_dir, monkeypatch
) -> None:
    """A catastrophic checker failure degrades to an error event; the eval
    pass continues and judge scores still land."""
    settings.BENCHMARK_SUITES_DIR = suites_dir
    run = _suite_run(session_factory, suite_ref="lifecycle-suite")
    _approved_rubric(session_factory, run.id)
    calls: list = []

    import orchestrator.lifecycle as lifecycle_mod
    from scoring import checkers as checkers_mod

    def boom(*a, **k):
        raise RuntimeError("checker subsystem exploded")

    monkeypatch.setattr(checkers_mod, "run_checks", boom)
    monkeypatch.setattr(lifecycle_mod.checkers, "run_checks", boom)

    await run_lifecycle(
        run.id,
        session_factory=session_factory,
        runtime=FakeRuntime(artifact_root=artifact_root, default_logs=RESULT_DONE),
        settings=settings,
        evaluator_fn=_score_writing_evaluator(calls),
    )

    with session_factory() as s:
        # Zero Score rows → run still failed by the normal rule, but the
        # evaluator WAS called — the crash didn't abort the eval pass.
        assert len(calls) == 1
        errs = [
            e
            for e in s.execute(select(Event)).scalars()
            if e.type == "error" and e.payload.get("stage") == "suite_checks"
        ]
        assert len(errs) == 1
        assert "checker subsystem exploded" in errs[0].payload["message"]
        assert s.get(Run, run.id).status == "done"  # score row landed


async def test_per_sandbox_checker_isolation(
    session_factory, settings, artifact_root, suites_dir
) -> None:
    """Multi-cell run: each sandbox gets its own checker_results rows."""
    settings.BENCHMARK_SUITES_DIR = suites_dir
    cfg = run_config(
        harness_configs=[
            {"adapter": "miniloop", "skill_packs": [], "name": "a"},
            {"adapter": "miniloop", "skill_packs": [], "name": "b"},
        ]
    )
    run = make_run(session_factory, status="provisioning", config=cfg)
    with session_factory() as s:
        r = s.get(Run, run.id)
        r.suite_ref = "lifecycle-suite"
        r.suite_index = 0
        s.commit()
    _approved_rubric(session_factory, run.id)

    await run_lifecycle(
        run.id,
        session_factory=session_factory,
        runtime=FakeRuntime(artifact_root=artifact_root, default_logs=RESULT_DONE),
        settings=settings,
        evaluator_fn=_score_writing_evaluator([]),
    )

    with session_factory() as s:
        sbs = list(s.execute(select(Sandbox)).scalars())
        rows = list(s.execute(select(CheckerResult)).scalars())
        assert len(sbs) == 2
        for sb in sbs:
            sb_rows = [r for r in rows if r.sandbox_id == sb.id]
            assert len(sb_rows) == 3  # one per declared check
        evs = [
            e
            for e in s.execute(select(Event)).scalars()
            if e.type == "checkpoint" and e.payload.get("stage") == "suite_checks"
        ]
        assert len(evs) == 2  # one per sandbox
