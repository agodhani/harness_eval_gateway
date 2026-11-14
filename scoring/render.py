"""Render an :class:`AnalysisReport` as ``ANALYSIS.md`` — the judge-facing
markdown brief staged into the eval workspace next to ``rubric.json``.

Compact tables a small judge model can parse; the full machine-readable
detail stays in ``analysis.json``. Sections are emitted per signal
collector — currently the static-analysis roll-up; PR7's exec signals add
their own block here via the ``metrics`` table.
"""

from __future__ import annotations

from scoring.models import AnalysisReport, SignalReport

#: Filename staged into the eval workspace (and written alongside
#: ``analysis.json`` in the artifact dir).
ANALYSIS_MD_FILENAME = "ANALYSIS.md"
#: Machine-readable report filename.
ANALYSIS_JSON_FILENAME = "analysis.json"


def _fmt_num(value: object) -> str:
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


def _render_static(sig: SignalReport) -> list[str]:
    m = sig.metrics
    cc = m.get("cyclomatic_complexity") or {}
    fn_nloc = m.get("function_nloc") or {}
    files_line = (
        f"- Files analyzed: **{m.get('files_analyzed', 0)}**"
        f" (scanned {m.get('files_scanned', 0)},"
        f" skipped {m.get('files_skipped', 0)})"
    )
    nloc_line = (
        f"- Total nloc (non-comment lines): **{m.get('total_nloc', 0)}**"
        f" across {m.get('total_lines', 0)} lines"
    )
    fn_line = (
        f"- Functions: **{m.get('function_count', 0)}** —"
        f" avg CC **{cc.get('mean', 0)}**, p90 **{cc.get('p90', 0)}**,"
        f" max **{cc.get('max', 0)}**"
    )
    cc_line = (
        f"- Functions with CC > 10: **{cc.get('over_10', 0)}**;"
        f" CC > 25: **{cc.get('over_25', 0)}**"
    )
    depth_line = (
        f"- Max nesting depth: **{m.get('max_nesting_depth', 0)}**;"
        f" largest function: **{fn_nloc.get('max', 0)}** nloc"
        f" (avg {fn_nloc.get('mean', 0)})"
    )
    lines = [
        "## Cyclomatic complexity (measured)",
        "",
        files_line,
        nloc_line,
        fn_line,
        cc_line,
        depth_line,
        "",
    ]
    langs = m.get("languages") or {}
    if langs:
        lines += [
            "### By language",
            "",
            "| language | files | nloc | functions |",
            "|---|---|---|---|",
        ]
        for lang, row in sorted(langs.items()):
            lines.append(
                f"| {lang} | {row.get('files', 0)} | {row.get('nloc', 0)}"
                f" | {row.get('functions', 0)} |"
            )
        lines.append("")
    if sig.functions:
        lines += [
            "### Most complex functions (worst by CC)",
            "",
            "| function | file:line | CC | nloc | nesting | params |",
            "|---|---|---|---|---|---|",
        ]
        for f in sig.functions:
            lines.append(
                f"| `{f.name}` | {f.file}:{f.line}"
                f" | {_fmt_num(f.cyclomatic_complexity)} | {f.nloc}"
                f" | {f.max_nesting_depth} | {f.params} |"
            )
        lines.append("")
    return lines


def _render_generic(sig: SignalReport) -> list[str]:
    """Fallback block for collectors without a custom section (PR7 exec)."""
    lines = [f"## Signal: {sig.name} ({sig.status})", ""]
    scalars = {k: v for k, v in sig.metrics.items() if isinstance(v, (int, float, str, bool))}
    if scalars:
        lines += ["| metric | value |", "|---|---|"]
        lines += [f"| {k} | {_fmt_num(v)} |" for k, v in scalars.items()]
        lines.append("")
    else:
        lines.append("(no scalar metrics)")
        lines.append("")
    return lines


def render_analysis_markdown(report: AnalysisReport) -> str:
    """``ANALYSIS.md`` body for one sandbox workspace."""
    intro = (
        "Measured by the evaluator before judging — **parse-only static"
        " analysis, no code was executed**. Use these values (especially"
        " cyclomatic complexity) as evidence; `analysis.json` holds the"
        " machine-readable form."
    )
    lines = [
        "# Objective workspace analysis",
        "",
        intro,
        "",
    ]
    for sig in report.signals:
        if sig.status == "error":
            lines += [
                f"## Signal: {sig.name} (error)",
                "",
                f"Collector failed: {sig.error or 'unknown'}",
                "",
            ]
            continue
        if sig.name == "static_analysis":
            lines += _render_static(sig)
        else:
            lines += _render_generic(sig)
        for w in sig.warnings[:10]:
            lines.append(f"- warning: {w}")
        if sig.warnings:
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"
