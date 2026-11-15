"""Exec-signal collector tests (PR7) — fake runner, no docker.

``ExecRunner`` itself is covered in ``tests/unit/sandbox/test_exec.py``;
these tests pin the collector contract: detection/skip rules, status
semantics (unavailable → skipped, no-result → error, red suite → ok), and
the flat metric shape the API ``ExecSummary`` reads.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import api.config
from sandbox.exec import ExecOutcome
from scoring.exec import ExecSandboxCollector, _exec_enabled
from scoring.models import SignalReport


class _FakeRunner:
    """Returns a canned ExecOutcome and records the workspace it got."""

    def __init__(self, outcome: ExecOutcome) -> None:
        self.outcome = outcome
        self.seen: Path | None = None

    def execute(self, workspace: Path, **_kwargs: object) -> ExecOutcome:
        self.seen = workspace
        return self.outcome


def _result_payload(
    *,
    build_ok: bool = True,
    tests: dict | None = None,
    coverage: dict | None = None,
    warnings: list[str] | None = None,
) -> dict:
    return {
        "version": 1,
        "language": "python",
        "phases": {
            "copy": {"ok": True, "bytes": 100, "files": 3, "truncated": False},
            "scan": {"python_files": 2},
            "build": {
                "ok": build_ok,
                "exit_code": 0 if build_ok else 1,
                "timed_out": False,
                "duration_seconds": 0.4,
                "stderr_tail": "" if build_ok else "Sorry: SyntaxError...",
            },
            "tests": tests,
            "coverage": coverage,
        },
        "warnings": warnings or [],
    }


def _ok_outcome(payload: dict | None = None, **kw: object) -> ExecOutcome:
    return ExecOutcome(
        image="heg-exec:latest",
        container="hegexec-abc123",
        result=_result_payload() if payload is None else payload,
        exit_code=0,
        duration_seconds=3.5,
        **kw,  # type: ignore[arg-type]
    )


@pytest.fixture()
def python_ws(tmp_path: Path) -> Path:
    (tmp_path / "app.py").write_text("def add(a, b):\n    return a + b\n")
    (tmp_path / "test_app.py").write_text(
        "from app import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n"
    )
    return tmp_path


def _collect(ws: Path, outcome: ExecOutcome) -> tuple[SignalReport, _FakeRunner]:
    runner = _FakeRunner(outcome)
    collector = ExecSandboxCollector(runner=runner, enabled=True)
    return collector.collect(ws), runner


# ----------------------------------------------------------------------
# skip rules
# ----------------------------------------------------------------------


def test_disabled_collector_skips(python_ws: Path) -> None:
    collector = ExecSandboxCollector(enabled=False)
    report = collector.collect(python_ws)
    assert report.status == "skipped"
    assert "disabled" in report.warnings[0]


def test_env_flag_disables(
    python_ws: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SCORING_EXEC", "off")
    collector = ExecSandboxCollector()  # enabled=None → env decides
    assert collector.collect(python_ws).status == "skipped"


def test_exec_enabled_env_beats_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An explicit SCORING_EXEC env var wins over api settings; with env
    unset the api config value decides (host-run dev where only .env loads)."""
    fake = SimpleNamespace(SCORING_EXEC=False)
    monkeypatch.setattr(api.config, "get_settings", lambda: fake)

    monkeypatch.setenv("SCORING_EXEC", "1")
    assert _exec_enabled() is True  # env wins over settings=False

    monkeypatch.setenv("SCORING_EXEC", "0")
    assert _exec_enabled() is False

    monkeypatch.delenv("SCORING_EXEC", raising=False)
    assert _exec_enabled() is False  # falls through to settings

    fake.SCORING_EXEC = True
    assert _exec_enabled() is True


def test_missing_workspace_skips(tmp_path: Path) -> None:
    collector = ExecSandboxCollector(enabled=True)
    report = collector.collect(tmp_path / "nope")
    assert report.status == "skipped"


def test_non_python_workspace_skips(tmp_path: Path) -> None:
    (tmp_path / "index.html").write_text("<html></html>")
    (tmp_path / "style.css").write_text("body {}")
    collector = ExecSandboxCollector(enabled=True)
    report = collector.collect(tmp_path)
    assert report.status == "skipped"
    assert "no python sources" in report.warnings[0]


def test_python_only_in_venv_still_skips(tmp_path: Path) -> None:
    """Dependency dirs are pruned from detection — vendored .py isn't the
    candidate's own work."""
    vendored = tmp_path / ".venv" / "lib"
    vendored.mkdir(parents=True)
    (vendored / "pkg.py").write_text("x = 1\n")
    collector = ExecSandboxCollector(enabled=True)
    assert collector.collect(tmp_path).status == "skipped"


def test_unavailable_runner_skips(python_ws: Path) -> None:
    outcome = ExecOutcome(
        image="heg-exec:latest",
        error="exec image 'heg-exec:latest' not found",
        unavailable=True,
    )
    report, _ = _collect(python_ws, outcome)
    assert report.status == "skipped"
    assert "not found" in report.warnings[-1]


# ----------------------------------------------------------------------
# measured outcomes
# ----------------------------------------------------------------------


def test_passing_suite_reports_metrics(python_ws: Path) -> None:
    payload = _result_payload(
        tests={
            "detected": True,
            "test_files": 1,
            "collected": 3,
            "passed": 3,
            "failed": 0,
            "errors": 0,
            "skipped": 0,
            "ok": True,
            "exit_code": 0,
            "timed_out": False,
            "duration_seconds": 1.1,
        },
        coverage={"ok": True, "percent": 87.5, "statements": 8, "covered": 7, "missing": 1},
        warnings=["inner warning"],
    )
    report, runner = _collect(python_ws, _ok_outcome(payload))
    assert runner.seen == python_ws
    assert report.name == "exec"
    assert report.status == "ok"
    m = report.metrics
    assert m["build_ok"] is True
    assert m["tests_detected"] is True
    assert m["tests_ok"] is True
    assert m["tests_passed"] == 3
    assert m["tests_collected"] == 3
    assert m["coverage_percent"] == 87.5
    assert m["coverage_statements"] == 8
    assert m["language"] == "python"
    assert "inner warning" in report.warnings
    assert "phases" in m  # raw runner payload embedded for detail


def test_failing_tests_still_ok_status(python_ws: Path) -> None:
    """A red suite IS the measurement — the signal ran fine."""
    payload = _result_payload(
        tests={
            "detected": True,
            "test_files": 1,
            "collected": 4,
            "passed": 2,
            "failed": 2,
            "errors": 0,
            "skipped": 0,
            "ok": False,
            "exit_code": 1,
            "timed_out": False,
            "duration_seconds": 2.0,
            "stderr_tail": "FAILED test_app.py::test_x",
        }
    )
    report, _ = _collect(python_ws, _ok_outcome(payload))
    assert report.status == "ok"
    assert report.metrics["tests_ok"] is False
    assert report.metrics["tests_failed"] == 2
    assert report.metrics["coverage_percent"] is None
    assert "FAILED" in report.metrics["test_log_tail"]


def test_no_tests_detected(python_ws: Path) -> None:
    payload = _result_payload(tests=None)
    report, _ = _collect(python_ws, _ok_outcome(payload))
    assert report.status == "ok"
    assert report.metrics["tests_detected"] is False
    assert report.metrics["tests_ok"] is None


# ----------------------------------------------------------------------
# infra failures
# ----------------------------------------------------------------------


def test_no_result_payload_is_error(python_ws: Path) -> None:
    outcome = ExecOutcome(
        image="heg-exec:latest",
        container="hegexec-dead",
        error="exec container produced no result payload (exit 137)",
        exit_code=137,
        duration_seconds=5.0,
        logs_tail="boom",
    )
    report, _ = _collect(python_ws, outcome)
    assert report.status == "error"
    assert "no result payload" in (report.error or "")


def test_timeout_kill_is_error(python_ws: Path) -> None:
    outcome = ExecOutcome(
        image="heg-exec:latest",
        container="hegexec-slow",
        error="exec container exceeded 300s — killed",
        timed_out=True,
        duration_seconds=300.0,
    )
    report, _ = _collect(python_ws, outcome)
    assert report.status == "error"
    assert report.metrics["timed_out"] is True


def test_collector_default_registered() -> None:
    """PR7 contract: exec collector is in the default registry."""
    from scoring.collectors import COLLECTORS

    names = [c.name for c in COLLECTORS]
    assert names == ["static_analysis", "exec"]
