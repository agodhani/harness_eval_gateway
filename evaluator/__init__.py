"""F7 — evaluator: post-run LLM judge scoring artifacts against the rubric.

Public contract for the orchestrator (docs/orchestrator-plan.md §5)::

    from evaluator import evaluate_sandbox
    scores = await evaluate_sandbox(...)

See ``evaluator.evaluate`` for the full docstring; ``evaluator.scores``
holds the scores.json schema/parser, ``evaluator.prompts`` the judge brief,
``evaluator.sandbox_spec`` the sandbox env/spec construction,
``evaluator.judge_direct`` the control-plane direct-completion fallback for
unusable judge output, and ``evaluator.testgen`` the PR8 generated-test-suite
signal.
"""

from evaluator.evaluate import evaluate_sandbox
from evaluator.scores import OVERALL_DIMENSION, ParsedScores, ScoreEntry

__all__ = ["OVERALL_DIMENSION", "ParsedScores", "ScoreEntry", "evaluate_sandbox"]
