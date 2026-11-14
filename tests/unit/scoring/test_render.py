"""ANALYSIS.md renderer tests (PR6)."""

from __future__ import annotations

from pathlib import Path

from scoring import AnalysisReport, collect_signals
from scoring.models import SignalReport
from scoring.render import render_analysis_markdown


def _workspace(tmp_path: Path) -> Path:
    (tmp_path / "a.py").write_text(
        "def f_branchy(a):\n"
        "    if a:\n"
        "        for i in range(3):\n"
        "            print(i)\n"
        "    return 0\n"
    )
    return tmp_path


def test_render_contains_measured_values(tmp_path: Path) -> None:
    report = collect_signals(_workspace(tmp_path))
    md = render_analysis_markdown(report)
    assert "Cyclomatic complexity" in md
    assert "f_branchy" in md
    assert "Files analyzed: **1**" in md
    assert "parse-only" in md
    # worst-functions table row
    assert "| `f_branchy` | a.py:1" in md
    # language table
    assert "| python | 1 |" in md


def test_render_error_signal(tmp_path: Path) -> None:
    report = AnalysisReport(
        signals=[
            SignalReport(
                name="broken", status="error", error="RuntimeError: boom"
            )
        ]
    )
    md = render_analysis_markdown(report)
    assert "Signal: broken (error)" in md
    assert "RuntimeError: boom" in md


def test_render_skipped_signal_has_no_fake_metrics(tmp_path: Path) -> None:
    """A skipped collector renders its status, not placeholder zeros read
    as measured values."""
    report = collect_signals(tmp_path / "does-not-exist")
    md = render_analysis_markdown(report)
    assert "Signal: static_analysis (skipped)" in md
    assert "avg CC" not in md


def test_render_zero_functions_has_no_fake_cc(tmp_path: Path) -> None:
    """Zero measured functions must not print 'avg CC 0' as a measurement."""
    (tmp_path / "data.py").write_text("x = 1\n")
    report = collect_signals(tmp_path)
    md = render_analysis_markdown(report)
    assert "Files analyzed: **1**" in md
    assert "avg CC" not in md


def test_render_generic_signal_metrics(tmp_path: Path) -> None:
    """Collectors without a custom section get a scalar metrics table."""
    report = AnalysisReport(
        signals=[
            SignalReport(
                name="future_signal",
                metrics={"build_success": True, "tests_passed": 12},
            )
        ]
    )
    md = render_analysis_markdown(report)
    assert "Signal: future_signal (ok)" in md
    assert "| build_success | True |" in md
    assert "| tests_passed | 12 |" in md


def test_render_exec_section(tmp_path: Path) -> None:
    """PR7: the exec signal renders its own measured build/test/coverage block."""
    report = AnalysisReport(
        signals=[
            SignalReport(
                name="exec",
                metrics={
                    "language": "python",
                    "build_ok": True,
                    "tests_detected": True,
                    "tests_ok": True,
                    "tests_collected": 5,
                    "tests_passed": 5,
                    "tests_failed": 0,
                    "tests_errors": 0,
                    "tests_skipped": 1,
                    "test_files": 2,
                    "test_duration_seconds": 1.4,
                    "coverage_percent": 82.5,
                    "coverage_statements": 40,
                    "coverage_covered": 33,
                    "timed_out": False,
                },
            )
        ]
    )
    md = render_analysis_markdown(report)
    assert "Executed checks" in md
    assert "**ok**" in md
    assert "5 passed" in md
    assert "82.5%" in md
    # exec ran → intro no longer claims 'no code was executed'
    assert "no code was executed" not in md
    assert "locked sandbox" in md


def test_render_exec_failed_tests(tmp_path: Path) -> None:
    report = AnalysisReport(
        signals=[
            SignalReport(
                name="exec",
                metrics={
                    "build_ok": True,
                    "tests_detected": True,
                    "tests_ok": False,
                    "tests_collected": 4,
                    "tests_passed": 2,
                    "tests_failed": 2,
                    "tests_errors": 0,
                    "tests_skipped": 0,
                },
            )
        ]
    )
    md = render_analysis_markdown(report)
    assert "2 failed" in md
    assert "Coverage: not measured" in md


def test_render_empty_report() -> None:
    md = render_analysis_markdown(AnalysisReport())
    assert md.startswith("# Objective workspace analysis")
    assert md.endswith("\n")
