"""Report models for objective scoring signals (PR6).

``AnalysisReport`` is the serialization contract for ``analysis.json`` — the
evaluator writes one per sandbox artifact (into the eval workspace for the
judge and into the artifact dir for the results API), and
``api/routers/results.py`` re-validates the stored JSON with these same
models for ``SandboxResult.analysis``.

The shape is deliberately collector-agnostic: PR7's exec/coverage collector
emits another :class:`SignalReport` (``name="exec"``-style) whose
``metrics`` carries build/test/coverage scalars — the report envelope and
API payload do not change.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

#: ``analysis.json`` schema version — bump on breaking shape changes.
SCHEMA_VERSION = 1

#: Collector outcome. ``ok`` = ran cleanly; ``error`` = collector raised (the
#: report still lists it so failures are visible); ``skipped`` = collector
#: declined the workspace (e.g. no applicable files).
SignalStatus = Literal["ok", "error", "skipped"]


class FunctionMetric(BaseModel):
    """One measured function/method — used for the worst-by-CC outliers."""

    name: str
    file: str  # workspace-relative path
    line: int  # 1-based start line
    cyclomatic_complexity: float
    nloc: int = 0  # non-comment lines inside the function body
    params: int = 0
    max_nesting_depth: int = 0


class FileMetric(BaseModel):
    """Per-source-file roll-up."""

    path: str  # workspace-relative
    language: str
    lines: int = 0  # raw line count
    nloc: int = 0  # non-comment lines of code (lizard)
    function_count: int = 0
    avg_cyclomatic_complexity: float = 0.0
    max_cyclomatic_complexity: float = 0.0
    max_nesting_depth: int = 0


class SignalReport(BaseModel):
    """Output of one signal collector.

    ``metrics`` holds aggregate scalars/tables (arbitrary JSON — each
    collector documents its own keys); ``files``/``functions`` are the
    typed structured rows most collectors can fill. ``warnings`` are
    non-fatal caveats (files skipped, truncated output); ``error`` is set
    when ``status="error"``.
    """

    name: str
    status: SignalStatus = "ok"
    metrics: dict[str, Any] = Field(default_factory=dict)
    files: list[FileMetric] = Field(default_factory=list)
    functions: list[FunctionMetric] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    error: str | None = None


class AnalysisReport(BaseModel):
    """All objective signals measured on one sandbox workspace."""

    schema_version: int = SCHEMA_VERSION
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    # Host path that was analyzed — debugging aid for staged artifacts.
    workspace: str = ""
    signals: list[SignalReport] = Field(default_factory=list)

    def signal(self, name: str) -> SignalReport | None:
        """First signal report with ``name``, or None."""
        return next((s for s in self.signals if s.name == name), None)
