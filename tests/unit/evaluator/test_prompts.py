"""EVAL_PROMPT rendering + task-prompt pointer tests (F7)."""

from __future__ import annotations

from evaluator.prompts import EVAL_TASK_PROMPT, build_eval_prompt

RUBRIC = {
    "summary": "Build a 2048 game.",
    "dimensions": [
        {
            "name": "functional_requirements",
            "title": "Functional requirements",
            "description": "Game logic works: moves, merges, score.",
            "min_score": 0,
            "max_score": 10,
            "weight": 1.0,
        },
        {
            "name": "code_quality",
            "title": "Code quality",
            "description": "Readable, modular code.",
            "min_score": 0,
            "max_score": 10,
            "weight": 1.0,
        },
    ],
    "criteria": [{"id": "c1", "text": "arrow keys move tiles", "dimension": "functional_requirements"}],
}


def test_prompt_contains_dimensions_and_schema() -> None:
    prompt = build_eval_prompt(RUBRIC)
    assert "functional_requirements" in prompt
    assert "code_quality" in prompt
    assert "scores.json" in prompt
    assert '"scores"' in prompt
    assert '"overall"' in prompt
    assert '"summary"' in prompt
    assert "rationale" in prompt
    assert "rubric.json" in prompt
    assert "EVAL_TRANSCRIPT.log" in prompt
    # strict-evaluator rules present
    assert "Do NOT modify" in prompt or "do NOT modify" in prompt.lower() or "Never modify" in prompt
    assert "call `done`" in prompt or "call done" in prompt.lower()


def test_prompt_includes_terminal_reason_when_given() -> None:
    prompt = build_eval_prompt(RUBRIC, terminal_reason="timeout")
    assert "timeout" in prompt
    assert "terminal state" in prompt


def test_prompt_omits_terminal_reason_block_when_absent() -> None:
    prompt = build_eval_prompt(RUBRIC)
    assert "terminal state" not in prompt


def test_prompt_handles_empty_dimensions() -> None:
    prompt = build_eval_prompt({"dimensions": []})
    assert "scores.json" in prompt  # still a usable brief


def test_task_prompt_points_at_files() -> None:
    assert "EVAL_PROMPT.md" in EVAL_TASK_PROMPT
    assert "scores.json" in EVAL_TASK_PROMPT
    assert "rubric.json" in EVAL_TASK_PROMPT
    assert "done" in EVAL_TASK_PROMPT


def test_prompt_prior_error_renders_retry_feedback() -> None:
    prompt = build_eval_prompt(RUBRIC, prior_error="scores_invalid: single-quoted strings")
    assert "Previous attempt failed" in prompt
    assert "single-quoted strings" in prompt
    assert "ONE JSON object" in prompt
    assert "double-quoted" in prompt


def test_prompt_prior_error_omitted_when_absent() -> None:
    prompt = build_eval_prompt(RUBRIC)
    assert "Previous attempt failed" not in prompt
    prompt = build_eval_prompt(RUBRIC, prior_error="   ")
    assert "Previous attempt failed" not in prompt


def test_prompt_prior_error_capped() -> None:
    prompt = build_eval_prompt(RUBRIC, prior_error="x" * 1000)
    assert "x" * 400 not in prompt


# ---------------------------------------------------------------------------
# PR23 — dimension-aware judge brief
# ---------------------------------------------------------------------------

NON_CODE_RUBRIC = {
    "summary": "Score the support reply.",
    "dimensions": [
        {
            "name": "accuracy",
            "title": "Accuracy",
            "description": "Correct answer to the ticket.",
            "min_score": 0,
            "max_score": 10,
        },
        {
            "name": "tone",
            "title": "Tone",
            "description": "Professional register.",
            "min_score": 0,
            "max_score": 10,
        },
        {
            "name": "compliance",
            "title": "Compliance",
            "description": "Policy obeyed.",
            "min_score": 0,
            "max_score": 10,
        },
    ],
}


def test_prompt_non_code_rubric_gets_na_guidance() -> None:
    """PR23: a rubric with no code dimensions reads as a general eval — the
    measured-signals rule degrades to 'context only' and the brief tells the
    judge how to mark non-applicable dimensions."""
    prompt = build_eval_prompt(NON_CODE_RUBRIC)
    assert "not applicable" in prompt
    assert "coding-agent" not in prompt
    # no code dims -> no cyclomatic-specific measured instruction
    assert "score `cyclomatic_complexity` from its measured values" not in prompt
    # the generic form instead
    assert "context only" in prompt
    # every dimension still listed for scoring
    for name in ("accuracy", "tone", "compliance"):
        assert name in prompt


def test_prompt_cyclomatic_dim_keeps_measured_instruction() -> None:
    """A rubric carrying cyclomatic_complexity keeps the measured-CC rule —
    the not-applicable rule covers OTHER non-code dims in the same brief."""
    rubric = dict(NON_CODE_RUBRIC)
    rubric["dimensions"] = [
        *NON_CODE_RUBRIC["dimensions"],
        {"name": "cyclomatic_complexity", "min_score": 0, "max_score": 10},
    ]
    prompt = build_eval_prompt(rubric)
    assert "score `cyclomatic_complexity` from its measured values" in prompt
    assert "not applicable" in prompt


def test_prompt_req_dims_named_in_exec_evidence() -> None:
    """functional/acceptance dims are named in the exec + generated-suite
    evidence lines; a rubric without them gets the generic phrasing."""
    prompt = build_eval_prompt(RUBRIC)  # has functional_requirements
    assert "`functional_requirements`" in prompt
    generic = build_eval_prompt(NON_CODE_RUBRIC)
    assert "whichever rubric dimensions they inform" in generic
