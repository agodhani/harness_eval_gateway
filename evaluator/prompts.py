"""Prompt assets for the evaluator sandbox (F7).

Two artifacts are staged into the eval workspace:

- ``EVAL_PROMPT.md`` — the full self-contained judge brief (written to the
  workspace root next to ``rubric.json`` so the agent can ``read_file`` it).
- ``EVAL_TASK_PROMPT`` — the ``TASK_PROMPT`` env var: a short pointer telling
  miniloop to read EVAL_PROMPT.md and produce scores.json.

The prompt is generated per evaluation so it embeds the *actual* rubric
dimensions (name/title/description/scale) — the judge still reads
``rubric.json`` for criteria detail, but the dimension list is spelled out
here to reduce the chance a small model skips a dimension.
"""

from __future__ import annotations

from typing import Any

#: Env TASK_PROMPT — deliberately short; EVAL_PROMPT.md carries the spec.
EVAL_TASK_PROMPT = (
    "You are the evaluation judge for a coding-agent benchmark. Your "
    "workspace contains a candidate submission (the files at the workspace "
    "root — do NOT modify them), plus rubric.json and EVAL_PROMPT.md. "
    "Read EVAL_PROMPT.md and follow it exactly: inspect the submission, "
    "score every rubric dimension, and write scores.json to the workspace "
    "root in the required schema. Verify scores.json by reading it back, "
    "then call done. Do not modify any candidate files."
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


def build_eval_prompt(rubric: dict[str, Any], *, terminal_reason: str | None = None) -> str:
    """Render EVAL_PROMPT.md — the judge's self-contained brief.

    ``rubric`` is the frozen ``Rubric.content`` dict. ``terminal_reason`` is
    the candidate sandbox's terminal state (e.g. ``agent_done``, ``timeout``)
    — surfaced as context so the judge knows e.g. a timeout-cut artifact is
    still judged on its merits.
    """
    summary = str(rubric.get("summary") or "").strip()
    term_line = (
        f"- The candidate run ended with terminal state: `{terminal_reason}`. "
        "Judge the artifact as it stands.\n"
        if terminal_reason
        else ""
    )
    return f"""# Evaluation task — score the candidate submission

You are a **strict, impartial evaluator** for a coding-agent benchmark. A
coding agent was given a task; its final workspace is the submission you are
scoring. Your job is to read the rubric and the submission, then produce
**scores.json** — nothing else.

## What is in this workspace (read-only inputs)

- **The candidate submission** — the files at the workspace root. This is
  what the agent produced. **Do NOT modify, fix, or add to these files.**
- **`rubric.json`** — the frozen rubric (full JSON, including task-specific
  `criteria`). Read it first.
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

- `dimension` MUST be the `name` field of a rubric dimension (e.g.
  `functional_requirements`). Score **every** dimension exactly once.
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
2. Apply the scale **consistently and strictly** — reward working,
   requirement-matching code; penalize missing features, bugs you can point
   to, and unfinished work. Do not inflate scores.
3. Judge the artifact, not the effort. A timed-out or minimal submission can
   still score well on what it does contain.
4. **Never modify candidate files.** The only file you create is
   `scores.json`.
5. After writing `scores.json`, read it back and confirm it is **one
   complete JSON object** matching the schema (opening `{{`, closing `}}`,
   all fields inside). Fix it if needed, then call `done`.

## Candidate run context

{term_line}- Evaluator model: see `MODEL_NAME` — irrelevant to scoring.
- You have limited iterations: be efficient. List files once, read what
  matters, write scores.json, verify, done.
"""
