"""collect_signals tests (PR6) — the collector-registry contract that PR7's
exec/coverage collector plugs into.
"""

from __future__ import annotations

from pathlib import Path

from scoring import AnalysisReport, collect_signals
from scoring.collectors import COLLECTORS
from scoring.models import SignalReport


def test_collect_returns_report_with_registered_collectors(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("def f():\n    pass\n")
    report = collect_signals(tmp_path)
    assert isinstance(report, AnalysisReport)
    assert report.workspace == str(tmp_path)
    names = [s.name for s in report.signals]
    assert names == [c.name for c in COLLECTORS]
    static = report.signal("static_analysis")
    assert static is not None and static.status == "ok"
    assert static.metrics["files_analyzed"] == 1


def test_report_json_round_trip(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("def f():\n    pass\n")
    report = collect_signals(tmp_path)
    reparsed = AnalysisReport.model_validate_json(report.model_dump_json())
    assert reparsed.schema_version == report.schema_version
    assert [s.name for s in reparsed.signals] == [s.name for s in report.signals]
    assert reparsed.signal("static_analysis").metrics == report.signal(
        "static_analysis"
    ).metrics


class _BoomCollector:
    name = "boom"

    def collect(self, workspace: Path) -> SignalReport:
        raise RuntimeError("collector exploded")


class _FakeCollector:
    name = "fake"

    def collect(self, workspace: Path) -> SignalReport:
        return SignalReport(name=self.name, metrics={"score": 1})


def test_failing_collector_isolated(tmp_path: Path) -> None:
    """A raising collector degrades to status="error" — other collectors
    still run, and collect_signals never raises (PR7 contract)."""
    report = collect_signals(
        tmp_path, collectors=[_BoomCollector(), _FakeCollector()]
    )
    assert len(report.signals) == 2
    boom = report.signal("boom")
    assert boom is not None and boom.status == "error"
    assert "collector exploded" in (boom.error or "")
    fake = report.signal("fake")
    assert fake is not None and fake.status == "ok"
    assert fake.metrics == {"score": 1}


def test_collectors_override(tmp_path: Path) -> None:
    """Explicit ``collectors=`` replaces the default registry — how PR7 and
    tests can select a signal set per call."""
    report = collect_signals(tmp_path, collectors=[_FakeCollector()])
    assert [s.name for s in report.signals] == ["fake"]
