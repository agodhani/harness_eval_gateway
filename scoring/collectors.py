"""Signal-collector registry — the extension point for objective scoring.

A *signal collector* measures an artifact workspace without judging it and
returns a :class:`~scoring.models.SignalReport`. ``collect_signals`` runs
every registered collector and bundles the reports into an
:class:`~scoring.models.AnalysisReport` — the ``analysis.json`` contract.

PR7 landed the exec-derived signals (build success, test pass, coverage) as
``ExecSandboxCollector`` — appended to :data:`COLLECTORS`. The report
envelope, ``analysis.json``, the ``eval_analysis`` event, and the results
payload all stay unchanged; the exec signal self-skips when disabled
(``SCORING_EXEC=0``), when the workspace has no python sources, or when no
docker daemon/exec image is available. Collectors are isolated: one raising
yields a ``status="error"`` report instead of breaking the bundle.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Protocol, runtime_checkable

from scoring.exec import ExecSandboxCollector
from scoring.models import AnalysisReport, SignalReport
from scoring.static import StaticAnalysisCollector

logger = logging.getLogger(__name__)


@runtime_checkable
class SignalCollector(Protocol):
    """One objective-signal source over a candidate workspace."""

    #: Registry/SignalReport id — stable, unique, snake_case
    #: (``"static_analysis"``; PR7: e.g. ``"exec"``).
    name: str

    def collect(self, workspace: Path) -> SignalReport:
        """Measure ``workspace`` and return a SignalReport.

        Must be side-effect free w.r.t. the workspace (read-only) and must
        not execute candidate code unless the collector's own contract
        sandboxes it. Implementations record per-file problems in
        ``warnings`` and return ``status="error"`` on unrecoverable
        failure rather than raising — ``collect_signals`` also guards.
        """
        ...


#: Default collector set, in report order. Callers needing a different set
#: pass ``collectors=`` explicitly (unit tests do — the exec collector would
#: otherwise spawn containers).
COLLECTORS: tuple[SignalCollector, ...] = (
    StaticAnalysisCollector(),
    ExecSandboxCollector(),
)


def collect_signals(
    workspace: Path | str,
    *,
    collectors: tuple[SignalCollector, ...] | list[SignalCollector] | None = None,
) -> AnalysisReport:
    """Run every collector over ``workspace`` → :class:`AnalysisReport`.

    Never raises for collector failures — each exception becomes an
    ``error``-status :class:`SignalReport` so a broken collector can't
    block the others (or evaluation).
    """
    ws = Path(workspace)
    report = AnalysisReport(workspace=str(ws))
    for collector in collectors if collectors is not None else COLLECTORS:
        name = getattr(collector, "name", type(collector).__name__)
        try:
            report.signals.append(collector.collect(ws))
        except Exception as exc:  # collector isolation — never break the bundle
            logger.warning("scoring: collector %s raised", name, exc_info=True)
            report.signals.append(
                SignalReport(
                    name=str(name),
                    status="error",
                    error=f"{type(exc).__name__}: {exc}",
                )
            )
    return report
