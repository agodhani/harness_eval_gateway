"""Render an :class:`AnalysisReport` as ``ANALYSIS.md`` — the judge-facing
markdown brief staged into the eval workspace next to ``rubric.json``.

Compact tables a small judge model can parse; the full machine-readable
detail stays in ``analysis.json``. Sections are emitted per signal
collector — the static-analysis roll-up, PR7's exec block (build/test/
coverage measured in the locked ``heg-exec`` container), and PR8's
``generated_tests`` block (LLM-written suite executed against the
submission — pass rate as functional evidence).
"""

from __future__ import annotations

from typing import Any

from scoring.models import AnalysisReport, SignalReport

#: Filename staged into the eval workspace (judge-facing only — only
#: ``analysis.json`` is persisted to the artifact dir).
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
    ]
    if m.get("function_count", 0):
        lines += [fn_line, cc_line, depth_line]
    else:
        lines.append("- Functions: **0** — no functions detected")
    lines.append("")
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


def _render_exec(sig: SignalReport) -> list[str]:
    """PR7 exec block — build/test/coverage measured in the locked container."""
    m = sig.metrics
    lines = [
        "## Executed checks (sandboxed — no network, resource-capped)",
        "",
    ]

    build_ok = m.get("build_ok")
    if build_ok is None:
        lines.append("- Build (byte-compile): not run")
    elif m.get("build_timed_out"):
        lines.append("- Build (byte-compile): **TIMED OUT**")
    else:
        lines.append(
            f"- Build (byte-compile): **{'ok' if build_ok else 'FAILED'}**"
            + ("" if build_ok else f" (exit {m.get('build_exit_code')})")
        )

    if not m.get("tests_detected"):
        lines.append("- Tests: none detected")
    else:
        tests_line = (
            f"- Tests: **{m.get('tests_passed', 0)} passed** /"
            f" {m.get('tests_failed', 0)} failed /"
            f" {m.get('tests_errors', 0)} errors /"
            f" {m.get('tests_skipped', 0)} skipped"
            f" (collected {m.get('tests_collected', 0)}"
            f" in {m.get('test_duration_seconds') or 0:g}s"
            f" from {m.get('test_files', 0)} test file(s))"
        )
        if m.get("timed_out"):
            tests_line += " — exec container timed out"
        lines.append(tests_line)
        lines += _failed_test_lines(m)

    cov = m.get("coverage_percent")
    if cov is None:
        lines.append("- Coverage: not measured")
    else:
        lines.append(
            f"- Coverage: **{cov}%**"
            f" ({m.get('coverage_covered', '?')}/{m.get('coverage_statements', '?')}"
            " statements)"
        )
    lines.append("")
    return lines


def _failed_test_lines(m: dict[str, Any]) -> list[str]:
    """Bullet list of failing/errored test names (junit detail, PR8)."""
    names = [str(n) for n in (m.get("failed_tests") or []) if str(n).strip()]
    if not names:
        return []
    return ["- Failing: " + ", ".join(f"`{n}`" for n in names[:10])]


def _render_generated_tests(sig: SignalReport) -> list[str]:
    """PR8 block — an LLM-written suite executed against the submission.

    Distinct framing from ``_render_exec``: these tests are EVALUATOR-
    authored, so a failure means "submission diverges from stated behavior"
    modulo generated-test bugs — the judge is told to weigh accordingly.
    """
    m = sig.metrics
    model = m.get("model") or "unknown model"
    lines = [
        "## Generated test suite (evaluator-authored, sandboxed)",
        "",
        f"- Suite: **{m.get('files_written', 0)} file(s)** generated by"
        f" `{model}`"
        + (
            f" — {m.get('files_rejected', 0)} chunk(s) rejected as unusable"
            if m.get("files_rejected")
            else ""
        ),
    ]
    if not m.get("tests_detected"):
        lines.append("- Tests: none collected")
    else:
        rate = m.get("pass_rate")
        rate_txt = f"{rate * 100:g}%" if isinstance(rate, (int, float)) else "n/a"
        line = (
            f"- Tests: **{m.get('tests_passed', 0)} passed** /"
            f" {m.get('tests_failed', 0)} failed /"
            f" {m.get('tests_errors', 0)} errors"
            f" (collected {m.get('tests_collected', 0)},"
            f" pass rate **{rate_txt}**)"
        )
        if m.get("timed_out"):
            line += " — exec container timed out"
        lines.append(line)
        lines += _failed_test_lines(m)
    cov = m.get("coverage_percent")
    if cov is not None:
        lines.append(
            f"- Coverage of submission code: **{cov}%**"
            f" ({m.get('coverage_covered', '?')}/{m.get('coverage_statements', '?')}"
            " statements)"
        )
    lines.append("")
    return lines


def _render_generic(sig: SignalReport) -> list[str]:
    """Fallback block for collectors without a custom section."""
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
    has_exec = any(
        s.name == "exec" and s.status == "ok" for s in report.signals
    )
    has_gen = any(
        s.name == "generated_tests" and s.status == "ok"
        for s in report.signals
    )
    if has_exec or has_gen:
        intro = (
            "Measured by the evaluator before judging. Static analysis is"
            " **parse-only**; the executed checks below ran tests inside a"
            " **locked sandbox container** (no network, resource-capped,"
            " artifact mounted read-only). A 'Generated test suite' section"
            " reports an LLM-written pytest suite run against the submission"
            " — evaluator-authored, not the candidate's own tests. Use these"
            " values as evidence — `analysis.json` holds the machine-readable"
            " form."
        )
    else:
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
        if sig.status != "ok":
            lines += [
                f"## Signal: {sig.name} ({sig.status})",
                "",
            ]
            if sig.error:
                lines += [f"Collector failed: {sig.error}", ""]
        elif sig.name == "static_analysis":
            lines += _render_static(sig)
        elif sig.name == "exec":
            lines += _render_exec(sig)
        elif sig.name == "generated_tests":
            lines += _render_generated_tests(sig)
        else:
            lines += _render_generic(sig)
        for w in sig.warnings[:10]:
            lines.append(f"- warning: {w}")
        if sig.warnings:
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"
