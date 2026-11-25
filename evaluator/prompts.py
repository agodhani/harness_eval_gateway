"""Prompt assets for the evaluator sandbox (F7) + test-suite generation (PR8).

Two artifacts are staged into the eval workspace:

- ``EVAL_PROMPT.md`` — the full self-contained judge brief (written to the
  workspace root next to ``rubric.json`` so the agent can ``read_file`` it).
- ``EVAL_TASK_PROMPT`` — the ``TASK_PROMPT`` env var: a short pointer telling
  miniloop to read EVAL_PROMPT.md and produce scores.json.

The prompt is generated per evaluation so it embeds the *actual* rubric
dimensions (name/title/description/scale) — the judge still reads
``rubric.json`` for criteria detail, but the dimension list is spelled out
here to reduce the chance a small model skips a dimension.

PR8 adds ``TESTGEN_SYSTEM_PROMPT`` + ``build_testgen_prompt`` — the
control-plane generation call that writes a pytest suite for the submission
(run against it in the locked exec container; the suite is evaluator output,
not judge input).
"""

from __future__ import annotations

from typing import Any

#: Env TASK_PROMPT — deliberately short; EVAL_PROMPT.md carries the spec.
EVAL_TASK_PROMPT = (
    "You are the evaluation judge for an agent benchmark. Your workspace "
    "contains a candidate submission (the files at the workspace root — do "
    "NOT modify them), plus rubric.json and EVAL_PROMPT.md. Read "
    "EVAL_PROMPT.md and follow it exactly: inspect the submission, score "
    "every rubric dimension, and write scores.json to the workspace root "
    "in the required schema. Verify scores.json by reading it back, then "
    "call done. Do not modify any candidate files."
)


def _render_dimensions(rubric: dict[str, Any]) -> str:
    """Markdown bullet list of rubric dimensions for the prompt body."""
    dims = rubric.get("dimensions") or []
    lines: list[str] = []
    for d in dims:
        if not isinstance(d, dict):
            continue
        name = str(d.get("name") or "").strip()
        if not name:
            continue
        title = str(d.get("title") or "").strip()
        desc = str(d.get("description") or "").strip()
        lo = d.get("min_score", 0)
        hi = d.get("max_score", 10)
        weight = d.get("weight", 1)
        head = f"- `{name}`"
        if title:
            head += f" — {title}"
        head += f" (scale {lo}–{hi}"
        if weight not in (None, 1, 1.0):
            head += f", weight {weight}"
        head += ")"
        lines.append(head)
        if desc:
            lines.append(f"  {desc}")
    if not lines:
        lines.append("- (see rubric.json — no dimensions listed inline)")
    return "\n".join(lines)


def build_eval_prompt(
    rubric: dict[str, Any],
    *,
    terminal_reason: str | None = None,
    prior_error: str | None = None,
    submission_note: str | None = None,
) -> str:
    """Render EVAL_PROMPT.md — the judge's self-contained brief.

    ``rubric`` is the frozen ``Rubric.content`` dict. ``terminal_reason`` is
    the candidate sandbox's terminal state (e.g. ``agent_done``, ``timeout``)
    — surfaced as context so the judge knows e.g. a timeout-cut artifact is
    still judged on its merits. ``prior_error`` (error-feedback retry) is
    the previous attempt's ``eval_scores_failed`` detail — a judge respawned
    with the SAME prompt flakes identically, so the failure mode is spelled
    out with an explicit output-shape reminder. ``submission_note`` (PR25)
    replaces the default "candidate submission" bullet for non-workspace
    submissions — a prompt cell's staged ``output.txt`` needs different
    framing so the judge doesn't hunt for a code repo.
    """
    summary = str(rubric.get("summary") or "").strip()
    term_line = (
        f"- The candidate run ended with terminal state: `{terminal_reason}`. "
        "Judge the artifact as it stands.\n"
        if terminal_reason
        else ""
    )
    prior = (prior_error or "").strip()[:300]
    retry_block = (
        f"""## Previous attempt failed — fix the output this time

Your previous attempt's `scores.json` could not be used: {prior}
Write ONE JSON object — double-quoted strings, no markdown fences, no text
before or after it — then read it back and fix it before calling `done`.
"""
        if prior
        else ""
    )
    submission_line = (
        submission_note
        if submission_note
        else (
            "**The candidate submission** — the files at the workspace root. "
            "This is what the agent produced. **Do NOT modify, fix, or add "
            "to these files.**"
        )
    )
    # PR23: rubric dimension names drive the measured-signals guidance — a
    # cyclomatic_complexity dim gets its measured-values instruction, and
    # requirement-style dims get the exec/generated-suite evidence pointer;
    # rubrics without them get the generic "measurements are context" line
    # so a non-code rubric never reads as a code task.
    dim_names = {
        str(d.get("name") or "").strip()
        for d in (rubric.get("dimensions") or [])
        if isinstance(d, dict)
    }
    has_cc = "cyclomatic_complexity" in dim_names
    req_like = sorted(
        dim_names & {"functional_requirements", "acceptance_criteria"}
    )
    if has_cc:
        measured_line = (
            "score `cyclomatic_complexity` from its measured values — do NOT "
            "estimate complexity by skimming. Cite the numbers in the "
            "rationale (e.g. 'avg CC 4.2, worst fn `update` at 14'); if the "
            "report shows no analyzable source, note that instead of "
            "guessing. "
        )
    else:
        measured_line = (
            "use its measurements where they evidence a rubric dimension. "
            "Metrics with no matching dimension — cyclomatic complexity on a "
            "non-code rubric, for instance — are context only, never extra "
            "scoring axes. "
        )
    if req_like:
        exec_dims = " and ".join(f"`{n}`" for n in req_like)
        exec_line = (
            "Likewise, when an \"Executed checks\" section is present, treat "
            "its build/test/coverage results as ground-truth evidence for "
            f"{exec_dims} — a submission that fails to build or has failing "
            "tests is not fully working; cite the counts (e.g. 'build ok, "
            "5/5 tests pass, 82% coverage')."
        )
        gen_line = (
            "the pass rate is objective evidence for "
            f"{exec_dims} — a submission failing generated behavioral checks "
            "is likely missing required behavior. "
        )
    else:
        exec_line = (
            "Likewise, when an \"Executed checks\" section is present, treat "
            "its build/test/coverage results as ground-truth evidence for "
            "whichever rubric dimensions they inform — a submission that "
            "fails to build or has failing tests is not fully working; cite "
            "the counts (e.g. 'build ok, 5/5 tests pass, 82% coverage')."
        )
        gen_line = (
            "the pass rate is objective evidence for whichever rubric "
            "dimensions it informs — a submission failing generated "
            "behavioral checks is likely missing required behavior. "
        )
    return f"""# Evaluation task — score the candidate submission

You are a **strict, impartial evaluator** for an agent benchmark. An agent
was given a task; its final workspace is the submission you are scoring —
it may be code, extracted data, written text, or any other artifact the
rubric describes. Your job is to read the rubric and the submission, then
produce **scores.json** — nothing else.

## What is in this workspace (read-only inputs)

- {submission_line}
- **`rubric.json`** — the frozen rubric (full JSON, including task-specific
  `criteria`). Read it first.
- **`ANALYSIS.md`** *(if present)* — objective measurements collected from
  the submission BEFORE you ran: real cyclomatic complexity per function,
  file sizes, language mix (parse-only static analysis), and — when an
  "Executed checks" section is present — the submission's build/test/
  coverage results measured by running it inside a locked, network-less
  sandbox container, and — when a "Generated test suite" section is
  present — pass/fail counts for a pytest suite written by an LLM test
  generator (not by the candidate) and executed against the submission in
  the same locked container. **`analysis.json`** is the machine-readable
  form of the same report.
- **`generated_tests/`** *(if present)* — the raw generated suite whose
  results `ANALYSIS.md` summarizes. Evaluator-authored, not candidate work;
  read it to judge what was actually verified.
- **`EVAL_TRANSCRIPT.log`** *(if present)* — the candidate agent's run
  transcript. Reference only; it is NOT part of the submission.
- **`EVAL_PROMPT.md`** — this file.

## Rubric

{('Task summary: ' + summary + chr(10) + chr(10)) if summary else ''}Dimensions to score (each exactly once, 0–10 unless the scale says otherwise):

{_render_dimensions(rubric)}

## Deliverable — `/workspace/scores.json`

Write **exactly one file**: `scores.json` at the workspace root. The file
must contain **ONE single JSON object — nothing before it, nothing after
it, no markdown fences, no second JSON block**. Do NOT split the scores
array and the overall/summary into separate objects; they are fields of the
same object. Valid JSON in this schema:

```json
{{
  "scores": [
    {{"dimension": "<dimension name from rubric>", "score": <number>, "rationale": "<reasoning>"}}
  ],
  "overall": <number 0-10>,
  "summary": "<2-4 sentence overall assessment>"
}}
```

- `dimension` MUST be the `name` field of a rubric dimension — the exact
  names listed above. Score **every** dimension exactly once.
- `score` is a number on the dimension's scale.
- `rationale` MUST cite concrete evidence: file names, observed behavior,
  missing pieces. "Looks good" is not a rationale.
- `overall` is your holistic 0–10 judgment across all dimensions.
- `summary` is a short paragraph for the human reviewer.
- Do not use `"` characters inside `rationale` or `summary` strings — quote
  file or symbol names with single quotes instead (inner double quotes make
  the JSON invalid and the scores cannot be read).

## Rules

1. Read `rubric.json`, then inspect the submission (`ls`, `find`, `read_file`,
   `shell` are all available). Skim large files; focus on what the rubric asks.
   `read_file` reports `total_lines` and `eof` — when `eof` is false, continue
   with `offset` set to the next unread line. Never repeat an identical tool
   call; vary the arguments or move on.
2. Apply the scale **consistently and strictly** — reward submissions that
   satisfy the rubric's requirements; penalize missing pieces, defects you
   can point to, and unfinished work. Do not inflate scores.
3. Judge the artifact, not the effort. A timed-out or minimal submission can
   still score well on what it does contain.
4. **Mark non-applicable dimensions honestly.** If a rubric dimension cannot
   apply to this kind of submission — for example `cyclomatic_complexity`
   when the submission contains no analyzable code, or a format rule on a
   free-text artifact — still emit its score row: score it at the middle of
   its scale and begin the rationale with "not applicable" followed by why.
   Never drop, rename, or invent a dimension row.
5. **Use the measured signals.** When `ANALYSIS.md`/`analysis.json` is
   present, {measured_line}{exec_line}
6. **Use the generated-suite pass rate.** When a "Generated test suite"
   section is present, an LLM (not the candidate) wrote pytest tests for the
   task's stated behavior and they ran against the submission: {gen_line}Caveat: generated tests can
   themselves be wrong — check `generated_tests/` or the report's failing
   names when a failure looks like a test bug rather than a submission bug,
   and weigh accordingly. Cite the counts (e.g. 'generated suite 7/10
   passed, 70% coverage').
7. **Never modify candidate files.** The only file you create is
   `scores.json`.
8. After writing `scores.json`, read it back and confirm it is **one
   complete JSON object** matching the schema (opening `{{`, closing `}}`,
   all fields inside). Fix it if needed, then call `done`.

## Candidate run context

{term_line}- Evaluator model: see `MODEL_NAME` — irrelevant to scoring.
- You have limited iterations: be efficient. List files once, read what
  matters, write scores.json, verify, done.
{retry_block}"""


# ---------------------------------------------------------------------------
# PR8 — generated test suite (control-plane generation call)
# ---------------------------------------------------------------------------

#: System prompt for the suite generator — the ONLY rules are about output
#: shape and determinism; the user message carries the actual context.
TESTGEN_SYSTEM_PROMPT = (
    "You write pytest test suites that grade a Python submission for a "
    "coding-agent benchmark. Output ONLY Python test code — no prose, no "
    "explanation, no markdown fences.\n\n"
    "Output contract:\n"
    "- Emit one or more files; start EVERY file with a marker line of the "
    "form '### FILE: test_<name>.py'.\n"
    "- Every file must define at least one 'def test_*' function (plain "
    "pytest — tmp_path/monkeypatch fixtures are fine, nothing else).\n\n"
    "Test rules:\n"
    "- Import the submission's modules by the paths shown (the submission "
    "root is on sys.path — 'from app import x', never 'src.app' unless the "
    "listing shows a package).\n"
    "- Test externally observable behavior implied by the task summary and "
    "criteria, not implementation details.\n"
    "- Be deterministic: no sleeps, no network, no unseeded randomness, no "
    "writes outside tmp_path.\n"
    "- Tests must be INDEPENDENT of any tests the submission already has."
)


def build_testgen_prompt(
    rubric: dict[str, Any] | None,
    sources: list[tuple[str, str]],
) -> str:
    """User message for the generation call — rubric context + source listing.

    ``sources`` is ``[(workspace-relative path, text)]`` from
    ``evaluator.testgen._gather_sources`` (already budget-capped). The rubric
    supplies the task summary + acceptance criteria — the closest thing to a
    spec the generator can test against.
    """
    rubric = rubric or {}
    parts: list[str] = []
    summary = str(rubric.get("summary") or "").strip()
    if summary:
        parts += ["# Task summary", summary, ""]
    criteria = [
        c
        for c in (rubric.get("criteria") or [])
        if isinstance(c, dict) and str(c.get("text") or "").strip()
    ]
    if criteria:
        parts.append("# Acceptance criteria")
        for c in criteria:
            dim = str(c.get("dimension") or "").strip()
            line = f"- {str(c['text']).strip()}"
            parts.append(f"{line} [{dim}]" if dim else line)
        parts.append("")
    parts.append("# Submission source files")
    parts.append("")
    for rel, text in sources:
        parts += [f"### FILE: {rel}", "```python", text.rstrip(), "```", ""]
    parts += [
        "# Instructions",
        (
            "Write a pytest suite that checks whether this submission "
            "satisfies the task summary and acceptance criteria above. Cover "
            "the happy-path requirements first, then edge cases the criteria "
            "imply. Remember: '### FILE: test_<name>.py' marker before every "
            "file, test code only."
        ),
    ]
    return "\n".join(parts) + "\n"

