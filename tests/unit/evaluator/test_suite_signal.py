"""PR26 — the ``suite`` signal: orchestrator-written ``suite_checks.json``
folds into analysis.json + ANALYSIS.md so the judge sees the deterministic
pass rate and the results API can read it back.
"""

from __future__ import annotations

import json
from pathlib import Path

from evaluator.evaluate import _load_suite_signal
from scoring import AnalysisReport, checkers
from scoring.render import render_analysis_markdown


def _write_doc(art: Path, *, checks: list[dict], passed: int, failed: int) -> None:
    doc = {
        "suite_ref": "starter-regression",
        "suite_index": 1,
        "checks": checks,
        "total": len(checks),
        "passed": passed,
        "failed": failed,
        "skipped": 0,
        "errors": 0,
        "measured": passed + failed,
        "pass_rate": passed / (passed + failed) if (passed + failed) else None,
    }
    (art / checkers.SUITE_CHECKS_FILENAME).write_text(json.dumps(doc))


def test_load_suite_signal_reads_artifact_doc(tmp_path: Path) -> None:
    _write_doc(
        tmp_path,
        checks=[
            {"check": "exact", "spec": {"equals": "x"}, "status": "pass", "detail": "ok"},
            {"check": "contains", "spec": {"substring": "y"}, "status": "fail", "detail": "no y"},
        ],
        passed=1,
        failed=1,
    )
    sig = _load_suite_signal(tmp_path)
    assert sig is not None and sig.name == "suite"
    assert sig.metrics["suite_ref"] == "starter-regression"
    assert sig.metrics["suite_index"] == 1
    assert sig.metrics["pass_rate"] == 0.5
    assert len(sig.metrics["checks"]) == 2


def test_load_suite_signal_absent_and_broken(tmp_path: Path) -> None:
    # No file → None (non-suite artifact).
    assert _load_suite_signal(tmp_path) is None
    # Unparseable file → None, never raises.
    (tmp_path / checkers.SUITE_CHECKS_FILENAME).write_text("{oops")
    assert _load_suite_signal(tmp_path) is None
    # Empty checks list → None (a stub file isn't evidence).
    (tmp_path / checkers.SUITE_CHECKS_FILENAME).write_text('{"checks": []}')
    assert _load_suite_signal(tmp_path) is None


def test_analysis_json_carries_suite_signal(tmp_path: Path) -> None:
    """The signal rides the AnalysisReport envelope — analysis.json gets
    the machine-readable pass rate + per-check rows."""
    _write_doc(
        tmp_path,
        checks=[
            {"check": "output_grep", "spec": {"pattern": "x"}, "status": "pass", "detail": "m"},
        ],
        passed=1,
        failed=0,
    )
    sig = _load_suite_signal(tmp_path)
    report = AnalysisReport(workspace=str(tmp_path))
    report.signals.append(sig)
    raw = json.loads(report.model_dump_json())
    suite_sig = next(s for s in raw["signals"] if s["name"] == "suite")
    assert suite_sig["metrics"]["pass_rate"] == 1.0
    assert suite_sig["metrics"]["checks"][0]["check"] == "output_grep"


def test_rendered_analysis_md_has_suite_section(tmp_path: Path) -> None:
    _write_doc(
        tmp_path,
        checks=[
            {"check": "exact", "spec": {}, "status": "fail", "detail": "output differs"},
            {"check": "py_test", "spec": {}, "status": "pass", "detail": "3/3 tests passed"},
        ],
        passed=1,
        failed=1,
    )
    sig = _load_suite_signal(tmp_path)
    report = AnalysisReport(workspace=str(tmp_path))
    report.signals.append(sig)
    md = render_analysis_markdown(report)
    assert "Suite checks" in md
    assert "starter-regression" in md
    assert "50%" in md
    assert "FAIL" in md and "PASS" in md
    assert "output differs" in md
