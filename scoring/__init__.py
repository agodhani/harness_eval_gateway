"""Objective scoring signals (PR6) — measured, not LLM-estimated.

``collect_signals(workspace)`` runs every registered
:class:`~scoring.collectors.SignalCollector` over a candidate workspace and
returns an :class:`AnalysisReport` (the ``analysis.json`` contract). The
evaluator feeds the report to the judge as evidence and persists it for the
results API; PR7 adds exec/coverage collectors behind the same envelope.

``scoring.models`` is import-light (pydantic only) — safe for
``api/schemas.py``. ``collect_signals`` is lazy so importing this package
doesn't pull lizard into processes that only need the report models.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from scoring.models import (
    SCHEMA_VERSION,
    AnalysisReport,
    FileMetric,
    FunctionMetric,
    SignalReport,
    SignalStatus,
)

if TYPE_CHECKING:
    from pathlib import Path

__all__ = [
    "SCHEMA_VERSION",
    "AnalysisReport",
    "FileMetric",
    "FunctionMetric",
    "SignalCollector",
    "SignalReport",
    "SignalStatus",
    "collect_signals",
]

#: Names resolved lazily so ``import scoring`` never pulls lizard into
#: processes that only need the report models (e.g. api.schemas).
_LAZY = {
    "SignalCollector": "scoring.collectors",
}


def collect_signals(workspace: Path | str, **kwargs: object) -> AnalysisReport:
    """Lazy wrapper — see :func:`scoring.collectors.collect_signals`."""
    from scoring.collectors import collect_signals as _collect

    return _collect(workspace, **kwargs)  # type: ignore[arg-type]


def __getattr__(name: str):
    if name in _LAZY:
        import importlib

        return getattr(importlib.import_module(_LAZY[name]), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
