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
