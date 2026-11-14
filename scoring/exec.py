"""Exec signal collector (PR7) — measured build/test/coverage, sandboxed.

Runs the candidate workspace inside the locked ``heg-exec`` container
(:mod:`sandbox.exec`) and reports build success, test pass/fail counts, and
coverage as :class:`~scoring.models.SignalReport` metrics — the
``cyclomatic_complexity`` analogue for functional/acceptance dimensions.

Unlike ``static_analysis`` this collector DOES execute candidate code — the
``SignalCollector`` contract allows it when the collector's own contract
sandboxes the exec (``--network none``, nobody user, cap-drop, resource
bounds, read-only artifact mount). The workspace is never modified.

Config knobs (env / api settings, overridable via constructor for tests/PR8):

- ``SCORING_EXEC`` — ``0``/``false``/``off`` disables the collector
  (``status="skipped"``); default on. Declared in ``api.config.Settings``,
  documented in ``.env.example``, and passed through by compose — the env
  var wins when set, the api setting is the fallback (host-run dev where
  only ``.env`` is loaded). ``tests/conftest.py`` pins it off so unit
  tests never spawn containers.
- ``EXEC_IMAGE`` — exec container image (default ``heg-exec:latest``);
  same env → settings → default resolution in ``sandbox.exec``.

Status semantics: ``skipped`` = declined (not a python workspace, disabled,
no docker daemon, image not built); ``error`` = container ran/died without
a usable result (timeout kill, spawn failure); ``ok`` = a result payload was
measured — failing builds/tests still yield ``ok`` (a red suite IS the
measurement).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from scoring.models import SignalReport
from scoring.static import SKIP_DIRS

if TYPE_CHECKING:
    from sandbox.exec import ExecOutcome, ExecRunner

logger = logging.getLogger(__name__)

COLLECTOR_NAME = "exec"

_OFF_VALUES = frozenset({"0", "false", "no", "off", "disabled"})


def _exec_enabled(default: bool = True) -> bool:
    """SCORING_EXEC resolution: env var wins, api settings is the fallback.

    ``api.config`` is consulted lazily and failure-tolerated so the scoring
    package stays usable standalone (no hard api dependency at import).
    """
    val = os.environ.get("SCORING_EXEC")
    if val is not None:
        return val.strip().lower() not in _OFF_VALUES
    try:
        from api.config import get_settings

        return bool(get_settings().SCORING_EXEC)
    except Exception:  # noqa: BLE001 — standalone scoring without api config
        return default


def _has_python_sources(workspace: Path) -> bool:
    """Any ``*.py`` outside dependency/vendor dirs — cheap early-exit walk."""
    for dirpath, dirnames, filenames in os.walk(workspace):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        if any(name.endswith(".py") for name in filenames):
            return True
    return False


def _flat_metrics(outcome: ExecOutcome) -> dict[str, Any]:
    """Flatten the runner payload into SignalReport metrics.

    Flat scalars (``build_ok``/``tests_ok``/``coverage_percent``) are the
    stable public shape — the API ``ExecSummary`` and any future analytics
    read them without knowing the runner's phase layout. The raw
    ``phases`` tree is embedded for detail (log tails for the judge).
    """
    result = outcome.result or {}
    phases = result.get("phases") or {}
    build = phases.get("build") or {}
    tests = phases.get("tests") or None
    coverage = phases.get("coverage") or None

    metrics: dict[str, Any] = {
        "language": result.get("language", "python"),
        "image": outcome.image,
        "container": outcome.container,
        "duration_seconds": outcome.duration_seconds,
        "timed_out": outcome.timed_out,
        "build_ok": build.get("ok"),
        "tests_detected": bool(tests and tests.get("detected")),
        "tests_ok": tests.get("ok") if tests else None,
        "coverage_percent": coverage.get("percent") if coverage else None,
        "limits": {
            "network": "none",
            "user": "nobody",
        },
    }
    if build:
        metrics["build_exit_code"] = build.get("exit_code")
        metrics["build_timed_out"] = build.get("timed_out", False)
        if build.get("stderr_tail"):
            metrics["build_log_tail"] = build["stderr_tail"]
    if tests:
        metrics.update(
            {
                "tests_collected": tests.get("collected", 0),
                "tests_passed": tests.get("passed", 0),
                "tests_failed": tests.get("failed", 0),
                "tests_errors": tests.get("errors", 0),
                "tests_skipped": tests.get("skipped", 0),
                "test_files": tests.get("test_files", 0),
                "test_duration_seconds": tests.get("duration_seconds"),
            }
        )
        for tail_key in ("stderr_tail", "stdout_tail"):
            if tests.get(tail_key):
                metrics["test_log_tail"] = tests[tail_key]
                break
    if coverage:
        metrics.update(
            {
                "coverage_statements": coverage.get("statements"),
                "coverage_covered": coverage.get("covered"),
                "coverage_missing": coverage.get("missing"),
            }
        )
    metrics["phases"] = phases
    return metrics


class ExecSandboxCollector:
    """Measured exec signals: build success, test pass, coverage.

    ``runner`` is injected for tests/PR8 — anything with
    ``execute(workspace) -> ExecOutcome`` works. The default runner is built
    lazily inside ``collect`` so constructing the registry stays docker-free.
    """

    name = COLLECTOR_NAME

    def __init__(
        self,
        *,
        runner: ExecRunner | None = None,
        enabled: bool | None = None,
    ) -> None:
        self._runner = runner
        self._enabled = enabled

    def _build_runner(self) -> ExecRunner:
        from sandbox.exec import ExecRunner

        return ExecRunner()

    def collect(self, workspace: Path) -> SignalReport:
        ws = Path(workspace)
        report = SignalReport(name=self.name, status="ok")

        enabled = (
            self._enabled if self._enabled is not None else _exec_enabled()
        )
        if not enabled:
            report.status = "skipped"
            report.warnings.append("exec scoring disabled (SCORING_EXEC=0)")
            return report
        if not ws.is_dir():
            report.status = "skipped"
            report.warnings.append(f"workspace is not a directory: {ws}")
            return report
        if not _has_python_sources(ws):
            report.status = "skipped"
            report.warnings.append(
                "no python sources found — exec scoring supports python only"
            )
            return report

        runner = self._runner or self._build_runner()
        outcome = runner.execute(ws)

        report.warnings.extend(outcome.warnings)
        if outcome.unavailable:
            # Can't measure (no daemon/image) — decline, don't fail.
            report.status = "skipped"
            if outcome.error:
                report.warnings.append(outcome.error)
            return report
        if not outcome.ok:
            report.status = "error"
            report.error = outcome.error or "exec produced no result payload"
            if outcome.logs_tail:
                report.warnings.append(f"container logs: {outcome.logs_tail}")
            report.metrics["timed_out"] = outcome.timed_out
            report.metrics["duration_seconds"] = outcome.duration_seconds
            return report

        result = outcome.result or {}
        report.metrics = _flat_metrics(outcome)
        report.warnings.extend(str(w) for w in result.get("warnings") or [])
        return report
