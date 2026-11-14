"""Static-analysis signal collector (PR6) — parse-only, never executes code.

Walks a candidate workspace and feeds every lizard-recognizable source file
through `lizard <https://github.com/terryyin/lizard>`_ — a pure-Python
multi-language analyzer (C/C++, Java, JS/TS, Python, Go, Rust, Ruby, …).
Per file it reports nloc (non-comment lines), cyclomatic complexity (CCN),
token/parameter counts, and — via lizard's bundled ``lizardnd`` extension —
max nesting depth. No code is imported or executed: analysis of hostile or
broken candidate code can at worst mis-measure, never run.

Guards keep adversarial workspaces cheap: dependency dirs are pruned,
symlinks skipped (a planted ``x.py -> /etc/…`` would otherwise leak host
files into the judge's brief), and file size/count are capped.
"""

from __future__ import annotations

import logging
import os
import statistics
from pathlib import Path
from typing import Any

from scoring.models import FileMetric, FunctionMetric, SignalReport

logger = logging.getLogger(__name__)

COLLECTOR_NAME = "static_analysis"

#: Directories pruned from the walk — dependency/build trees whose code is
#: not the candidate's own work. ``dist``/``build`` stay: built output IS the
#: artifact for many tasks (the size cap drops huge minified bundles).
SKIP_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".tox",
        ".venv",
        "venv",
        "env",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".next",
        ".nuxt",
        ".turbo",
        ".idea",
        ".vscode",
        "node_modules",
        "bower_components",
        "vendor",
        "target",  # rust/java build output
        "Pods",
    }
)

#: Per-file size cap — minified bundles / generated blobs above this are
#: skipped (recorded in warnings) instead of grinding the analyzer.
MAX_FILE_BYTES = 512 * 1024
#: Hard cap on source files analyzed per workspace.
MAX_FILES = 400
#: Per-file cap stored in the report (keeps analysis.json bounded).
MAX_REPORT_FILES = 200
#: Worst-N functions kept in ``functions`` for evidence citation.
TOP_FUNCTIONS = 8
#: Conventional CC thresholds used for the metrics summary + warnings.
CC_WARN = 10
CC_HIGH = 25


def _language_for(path: Path) -> str | None:
    """Canonical language name for ``path``, or None when unsupported."""
    import lizard

    reader = lizard.get_reader_for(str(path))
    if reader is None:
        return None
    names = getattr(reader, "language_names", None) or []
    return str(names[0]) if names else path.suffix.lstrip(".") or "unknown"


def _iter_source_files(
    workspace: Path,
) -> tuple[list[Path], list[str], int]:
    """Collect analyzable files under ``workspace``.

    Returns ``(candidates, warnings, files_scanned)`` — candidates are
    recognized source files within caps; oversized files are skipped with a
    warning. The walk is best-effort: unreadable dirs are skipped silently.
    """
    candidates: list[Path] = []
    warnings: list[str] = []
    scanned = 0
    skipped_big = 0
    capped = False

    for dirpath, dirnames, filenames in os.walk(workspace):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in sorted(filenames):
            p = Path(dirpath) / name
            try:
                if p.is_symlink() or not p.is_file():
                    continue
                if _language_for(p) is None:
                    continue
                scanned += 1
                if p.stat().st_size > MAX_FILE_BYTES:
                    skipped_big += 1
                    continue
            except OSError:
                continue
            if len(candidates) >= MAX_FILES:
                capped = True
                continue
            candidates.append(p)

    if skipped_big:
        warnings.append(
            f"{skipped_big} source file(s) over {MAX_FILE_BYTES // 1024} KiB skipped"
        )
    if capped:
        warnings.append(f"file cap hit — analyzed first {MAX_FILES} source files")
    return candidates, warnings, scanned


def _analyze_files(paths: list[Path]) -> dict[str, Any]:
    """Run lizard over ``paths`` (with the nesting-depth extension)."""
    import lizard
    from lizard_ext.lizardnd import LizardExtension as NestingDepth

    exts = lizard.get_extensions([NestingDepth()])
    infos = list(lizard.analyze_files([str(p) for p in paths], exts=exts))
    return {info.filename: info for info in infos}


def _percentile(values: list[float], pct: float) -> float:
    """Nearest-rank percentile (statistics.quantiles needs ≥2 points)."""
    if not values:
        return 0.0
    if len(values) == 1:
        return float(values[0])
    ordered = sorted(values)
    idx = max(0, min(len(ordered) - 1, round(pct / 100 * (len(ordered) - 1))))
    return float(ordered[idx])


class StaticAnalysisCollector:
    """Measured complexity + size signals via lizard. ``name`` is the
    registry/SignalReport id other collectors must not collide with."""

    name = COLLECTOR_NAME

    def collect(self, workspace: Path) -> SignalReport:
        workspace = Path(workspace)
        report = SignalReport(name=self.name, status="ok")
        if not workspace.is_dir():
            report.status = "skipped"
            report.warnings.append(f"workspace is not a directory: {workspace}")
            return report

        candidates, warnings, scanned = _iter_source_files(workspace)
        report.warnings.extend(warnings)

        if not candidates:
            report.metrics["files_scanned"] = scanned
            report.metrics["files_analyzed"] = 0
            if scanned == 0:
                report.warnings.append("no analyzable source files found")
            return report

        try:
            infos = _analyze_files(candidates)
        except Exception as exc:  # noqa: BLE001 — lizard is tolerant; infra failure is not
            report.status = "error"
            report.error = f"{type(exc).__name__}: {exc}"
            report.metrics["files_scanned"] = scanned
            report.metrics["files_analyzed"] = 0
            return report

        files: list[FileMetric] = []
        functions: list[FunctionMetric] = []
        languages: dict[str, dict[str, int]] = {}
        total_lines = 0
        total_nloc = 0
        total_tokens = 0

        for path in candidates:
            key = str(path)
            info = infos.get(key) or infos.get(key.lstrip("./"))
            if info is None:
                # lizard returns what it can; missing entry = analyze failure.
                report.warnings.append(f"{path.name}: no analysis result")
                continue
            rel = str(path.relative_to(workspace))
            language = _language_for(path) or "unknown"
            try:
                lines = len(path.read_bytes().splitlines())
            except OSError:
                lines = 0

            fns = list(info.function_list)
            ccs = [float(f.cyclomatic_complexity) for f in fns]
            nd = max((int(getattr(f, "max_nesting_depth", 0) or 0) for f in fns), default=0)
            files.append(
                FileMetric(
                    path=rel,
                    language=language,
                    lines=lines,
                    nloc=int(info.nloc),
                    function_count=len(fns),
                    avg_cyclomatic_complexity=(
                        round(statistics.fmean(ccs), 2) if ccs else 0.0
                    ),
                    max_cyclomatic_complexity=max(ccs, default=0.0),
                    max_nesting_depth=nd,
                )
            )
            for f in fns:
                functions.append(
                    FunctionMetric(
                        name=str(f.name),
                        file=rel,
                        line=int(f.start_line),
                        cyclomatic_complexity=float(f.cyclomatic_complexity),
                        nloc=int(f.nloc),
                        params=int(f.parameter_count),
                        max_nesting_depth=int(
                            getattr(f, "max_nesting_depth", 0) or 0
                        ),
                    )
                )
            lang = languages.setdefault(language, {"files": 0, "nloc": 0, "functions": 0})
            lang["files"] += 1
            lang["nloc"] += int(info.nloc)
            lang["functions"] += len(fns)
            total_lines += lines
            total_nloc += int(info.nloc)
            total_tokens += int(getattr(info, "token_count", 0) or 0)

        all_cc = [f.cyclomatic_complexity for f in functions]
        all_fn_nloc = [f.nloc for f in functions]
        functions.sort(key=lambda f: (-f.cyclomatic_complexity, -f.nloc, f.file, f.line))
        report.functions = functions[:TOP_FUNCTIONS]
        report.files = sorted(files, key=lambda f: -f.nloc)[:MAX_REPORT_FILES]

        over_warn = sum(1 for c in all_cc if c > CC_WARN)
        over_high = sum(1 for c in all_cc if c > CC_HIGH)
        report.metrics = {
            "files_scanned": scanned,
            "files_analyzed": len(files),
            "files_skipped": scanned - len(files),
            "total_lines": total_lines,
            "total_nloc": total_nloc,
            "total_tokens": total_tokens,
            "function_count": len(functions),
            "cyclomatic_complexity": {
                "mean": round(statistics.fmean(all_cc), 2) if all_cc else 0.0,
                "max": max(all_cc, default=0.0),
                "p90": _percentile(all_cc, 90),
                "over_10": over_warn,
                "over_25": over_high,
            },
            "function_nloc": {
                "mean": round(statistics.fmean(all_fn_nloc), 1) if all_fn_nloc else 0.0,
                "max": max(all_fn_nloc, default=0),
            },
            "max_nesting_depth": max(
                (f.max_nesting_depth for f in files), default=0
            ),
            "languages": languages,
            "worst_function": (
                {
                    "name": report.functions[0].name,
                    "file": report.functions[0].file,
                    "line": report.functions[0].line,
                    "cyclomatic_complexity": report.functions[0].cyclomatic_complexity,
                }
                if report.functions
                else None
            ),
        }
        if len(functions) == 0:
            report.warnings.append(
                "no functions found — files may be data/config or unsupported syntax"
            )
        return report
