"""scores.json parsing/validation tests (F7)."""

from __future__ import annotations

import json
import os

import pytest

from evaluator.scores import (
    OVERALL_DIMENSION,
    ScoresFileError,
    load_scores_file,
    parse_scores_json,
)

GOOD = {
    "scores": [
        {
            "dimension": "functional_requirements",
            "score": 8,
            "rationale": "hello.txt exists and prints greeting",
        },
        {
            "dimension": "code_quality",
            "score": 6.5,
            "rationale": "single file, no tests",
        },
    ],
    "overall": 7.2,
    "summary": "Minimal but working submission.",
}


def test_parse_good_payload() -> None:
    parsed = parse_scores_json(json.dumps(GOOD))
    assert len(parsed.entries) == 2
    assert parsed.entries[0].dimension == "functional_requirements"
    assert parsed.entries[0].score == 8.0
    assert parsed.entries[1].score == 6.5
    assert parsed.overall == 7.2
    assert parsed.summary == "Minimal but working submission."
    assert parsed.warnings == []


def test_parse_fenced_json() -> None:
    text = "```json\n" + json.dumps(GOOD) + "\n```"
    parsed = parse_scores_json(text)
    assert len(parsed.entries) == 2
    assert parsed.overall == 7.2


def test_parse_prose_wrapped_json() -> None:
    text = "Here are my scores:\n" + json.dumps(GOOD) + "\nHope this helps!"
    parsed = parse_scores_json(text)
    assert len(parsed.entries) == 2


def test_parse_name_alias_for_dimension() -> None:
    payload = {"scores": [{"name": "code_quality", "score": 5, "rationale": "ok"}]}
    parsed = parse_scores_json(json.dumps(payload))
    assert parsed.entries[0].dimension == "code_quality"


def test_parse_missing_scores_key_raises() -> None:
    with pytest.raises(ScoresFileError):
        parse_scores_json(json.dumps({"overall": 5}))


def test_parse_scores_not_list_raises() -> None:
    with pytest.raises(ScoresFileError):
        parse_scores_json(json.dumps({"scores": "nope"}))


def test_parse_empty_scores_raises() -> None:
    with pytest.raises(ScoresFileError):
        parse_scores_json(json.dumps({"scores": []}))


def test_parse_non_json_raises() -> None:
    with pytest.raises(ScoresFileError):
        parse_scores_json("the submission was fine, scores: great")


def test_parse_top_level_array_of_entries() -> None:
    # A bare array of score entries is accepted (small models emit this).
    parsed = parse_scores_json(
        json.dumps([{"dimension": "x", "score": 1, "rationale": "r"}])
    )
    assert parsed.entries[0].dimension == "x"


def test_parse_flat_dimension_map() -> None:
    # Observed small-model output: a flat {dimension: score} dict with a
    # sum "total" key instead of the canonical scores list.
    payload = {
        "functional_requirements": 8.0,
        "acceptance_criteria": 8.0,
        "code_quality": 9.0,
        "cyclomatic_complexity": 9.0,
        "total": 34.0,
    }
    parsed = parse_scores_json(json.dumps(payload))
    dims = {e.dimension: e.score for e in parsed.entries}
    assert dims == {
        "functional_requirements": 8.0,
        "acceptance_criteria": 8.0,
        "code_quality": 9.0,
        "cyclomatic_complexity": 9.0,
    }
    # "total" is a sum on a different scale — not a per-dimension score.
    assert "total" not in dims


def test_parse_drops_invalid_entries_with_warnings() -> None:
    payload = {
        "scores": [
            {"dimension": "functional_requirements", "score": 9, "rationale": "good"},
            "not-an-object",
            {"score": 5},  # no dimension
            {"dimension": "code_quality", "score": "high"},  # non-numeric
            {"dimension": "", "score": 3},  # empty dimension
            {"dimension": "acceptance_criteria", "score": 7},  # no rationale ok
        ],
        "overall": 8,
    }
    parsed = parse_scores_json(json.dumps(payload))
    dims = [e.dimension for e in parsed.entries]
    assert dims == ["functional_requirements", "acceptance_criteria"]
    assert parsed.entries[1].rationale == ""
    assert len(parsed.warnings) == 4


def test_parse_duplicate_dimension_last_wins() -> None:
    payload = {
        "scores": [
            {"dimension": "code_quality", "score": 3, "rationale": "first"},
            {"dimension": "code_quality", "score": 9, "rationale": "second"},
        ]
    }
    parsed = parse_scores_json(json.dumps(payload))
    assert len(parsed.entries) == 1
    assert parsed.entries[0].score == 9.0
    assert parsed.entries[0].rationale == "second"
    assert any("duplicate" in w for w in parsed.warnings)


def test_parse_overall_non_numeric_ignored() -> None:
    payload = {"scores": [{"dimension": "d", "score": 5}], "overall": "great"}
    parsed = parse_scores_json(json.dumps(payload))
    # non-numeric overall is ignored, then synthesized from the entries
    assert parsed.overall == 5.0
    assert any("not numeric" in w for w in parsed.warnings)
    assert any("computed from dimension scores" in w for w in parsed.warnings)


def test_parse_overall_synthesized_when_missing() -> None:
    """Dimension scores without an `overall` must not leave the field null —
    the results matrix treats it as the headline score."""
    payload = {
        "scores": [
            {"dimension": "functional_requirements", "score": 8},
            {"dimension": "code_quality", "score": 4},
        ]
    }
    parsed = parse_scores_json(json.dumps(payload))
    assert parsed.overall == 6.0
    assert parsed.warnings == [
        (
            "overall missing — computed from dimension scores "
            "(scale-normalized to 0-10)"
        )
    ]


def test_parse_genuine_overall_not_flagged() -> None:
    """A real `overall` is used verbatim — no synthesis warning."""
    parsed = parse_scores_json(json.dumps(GOOD))
    assert parsed.overall == 7.2
    assert not any("computed from dimension scores" in w for w in parsed.warnings)


def test_parse_overall_synthesis_rounds_to_one_decimal() -> None:
    payload = {
        "scores": [
            {"dimension": "a", "score": 8},
            {"dimension": "b", "score": 7},
            {"dimension": "c", "score": 9},
        ]
    }
    parsed = parse_scores_json(json.dumps(payload))
    assert parsed.overall == 8.0  # 24/3 — mean rounded to 1 decimal
    payload["scores"].append({"dimension": "d", "score": 7})
    parsed = parse_scores_json(json.dumps(payload))
    assert parsed.overall == 7.8  # 31/4 = 7.75 -> 7.8


def test_parse_bool_score_rejected() -> None:
    payload = {"scores": [{"dimension": "d", "score": True}]}
    with pytest.raises(ScoresFileError):
        parse_scores_json(json.dumps(payload))


def test_parse_nan_score_rejected() -> None:
    # json.loads accepts NaN by default; parser must reject non-finite.
    text = '{"scores": [{"dimension": "d", "score": NaN}]}'
    with pytest.raises(ScoresFileError):
        parse_scores_json(text)


def test_load_scores_file_missing(tmp_path) -> None:
    with pytest.raises(ScoresFileError):
        load_scores_file(tmp_path / "scores.json")


def test_load_scores_file_roundtrip(tmp_path) -> None:
    p = tmp_path / "scores.json"
    p.write_text(json.dumps(GOOD))
    parsed = load_scores_file(p)
    assert parsed.overall == 7.2
    assert OVERALL_DIMENSION == "overall"


def test_load_scores_file_rejects_symlink(tmp_path) -> None:
    real = tmp_path / "real.json"
    real.write_text(json.dumps(GOOD))
    (tmp_path / "scores.json").symlink_to(real)
    with pytest.raises(ScoresFileError):
        load_scores_file(tmp_path / "scores.json")


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="POSIX only")
def test_load_scores_file_rejects_fifo(tmp_path) -> None:
    # open() on a FIFO blocks until a writer appears; load must reject it
    # without opening.
    fifo = tmp_path / "scores.json"
    os.mkfifo(fifo)
    with pytest.raises(ScoresFileError):
        load_scores_file(fifo)


# ---------------------------------------------------------------------------
# salvage mode — observed live failure: split/truncated JSON values
# ---------------------------------------------------------------------------


def test_parse_salvages_bare_array_plus_truncated_object() -> None:
    """Live-observed qwen3:8b output: a bare scores array followed by a
    separate, truncated {"overall"...} object — entries must still land."""
    text = (
        '[{"dimension": "functional_requirements", "score": 10,'
        ' "rationale": "hello.txt present"},'
        ' {"dimension": "code_quality", "score": 10,'
        ' "rationale": "no junk files"}]\n\n'
        '{"overall": 10, "summary": "meets requirements"\n'
    )
    parsed = parse_scores_json(text)
    assert [e.dimension for e in parsed.entries] == [
        "functional_requirements",
        "code_quality",
    ]
    # trailing object was truncated -> genuine overall lost; the mean of
    # the salvaged entries is synthesized instead (flagged in warnings)
    assert parsed.overall == 10.0
    assert any("computed from dimension scores" in w for w in parsed.warnings)


def test_parse_salvages_bare_array_plus_complete_object() -> None:
    text = (
        json.dumps([{"dimension": "d1", "score": 8, "rationale": "x"}])
        + '\n\n{"overall": 7.5, "summary": "decent"}'
    )
    parsed = parse_scores_json(text)
    assert len(parsed.entries) == 1
    assert parsed.overall == 7.5
    assert parsed.summary == "decent"


def test_parse_salvages_top_level_array() -> None:
    text = json.dumps([{"dimension": "d1", "score": 8}, {"dimension": "d2", "score": 4}])
    parsed = parse_scores_json(text)
    assert [e.dimension for e in parsed.entries] == ["d1", "d2"]


def test_parse_salvages_loose_entry_objects() -> None:
    text = '{"dimension": "d1", "score": 8}{"dimension": "d2", "score": 4}'
    parsed = parse_scores_json(text)
    assert [e.dimension for e in parsed.entries] == ["d1", "d2"]


def test_parse_still_raises_on_total_garbage() -> None:
    with pytest.raises(ScoresFileError):
        parse_scores_json("no json here at all... just prose {unclosed")


# ---------------------------------------------------------------------------
# literal_eval repair — observed live failure: single-quoted pseudo-JSON
# ---------------------------------------------------------------------------


def test_parse_single_quoted_python_literal() -> None:
    """Observed qwen3:8b scores_invalid output: single-quoted dict — strict
    JSON rejects it, ast.literal_eval parses it exactly."""
    text = (
        "{'scores': ["
        "{'dimension': 'functional_requirements', 'score': 8,"
        " 'rationale': 'hello.txt present'},"
        " {'dimension': 'code_quality', 'score': 6,"
        " 'rationale': 'Handles valid moves only'}],"
        " 'overall': 7.0, 'summary': 'Minimal but working.'}"
    )
    parsed = parse_scores_json(text)
    assert [e.dimension for e in parsed.entries] == [
        "functional_requirements",
        "code_quality",
    ]
    assert parsed.entries[1].rationale == "Handles valid moves only"
    assert parsed.overall == 7.0
    assert parsed.summary == "Minimal but working."


def test_parse_mixed_quotes_python_literal() -> None:
    text = (
        "{'scores': [{'dimension': \"d1\", 'score': 8, 'rationale': 'x'}],"
        " 'overall': 7}"
    )
    parsed = parse_scores_json(text)
    assert parsed.entries[0].dimension == "d1"
    assert parsed.entries[0].rationale == "x"
    assert parsed.overall == 7.0


def test_parse_fenced_single_quoted_literal() -> None:
    text = "```json\n{'scores': [{'dimension': 'd', 'score': 4}]}\n```"
    parsed = parse_scores_json(text)
    assert parsed.entries[0].dimension == "d"


def test_parse_python_literals_true_none_inside() -> None:
    """True/None literals are valid inside a literal_eval'd document."""
    text = (
        "{'scores': [{'dimension': 'd', 'score': 5, 'rationale': 'ok',"
        " 'extra': None}], 'verified': True, 'overall': 6}"
    )
    parsed = parse_scores_json(text)
    assert parsed.entries[0].score == 5.0
    assert parsed.overall == 6.0


def test_parse_literal_eval_rejects_garbage() -> None:
    # Syntactically broken pseudo-JSON — literal_eval must fail safely.
    with pytest.raises(ScoresFileError):
        parse_scores_json("{'scores': [{'dimension': 'd', 'score': }]}")
    with pytest.raises(ScoresFileError):
        parse_scores_json("{'scores': [{'dimension': 'd', 'score': 5}")


def test_parse_literal_eval_rejects_scalars() -> None:
    """A bare literal that parses (string/number) is not a scores doc —
    the dict/list guard rejects it before validation."""
    with pytest.raises(ScoresFileError):
        parse_scores_json("'just a quoted string'")
    with pytest.raises(ScoresFileError):
        parse_scores_json("(1, 2, 3)")


# ---------------------------------------------------------------------------
# reserved score-namespace + declared-dimension folding (PR23)
# ---------------------------------------------------------------------------


def test_parse_drops_reserved_dimension_names() -> None:
    """A judge entry under a reserved score field must never persist —
    ``suite_pass``/``overall`` are pipeline-written pseudo-rows and
    ``scores``/``summary``/``total`` are document fields."""
    payload = {
        "scores": [
            {"dimension": "suite_pass", "score": 10},
            {"dimension": "Overall", "score": 9},
            {"dimension": "total", "score": 40},
            {"dimension": "code_quality", "score": 7},
        ]
    }
    parsed = parse_scores_json(json.dumps(payload))
    assert [e.dimension for e in parsed.entries] == ["code_quality"]
    assert sum("reserved score field" in w for w in parsed.warnings) == 3


def test_parse_flat_map_drops_suite_pass() -> None:
    """The flat-map form reaches the same reserved check — a ``suite_pass``
    key is not a per-dimension score."""
    parsed = parse_scores_json(json.dumps({"suite_pass": 10, "code_quality": 7}))
    assert {e.dimension: e.score for e in parsed.entries} == {"code_quality": 7.0}


def test_parse_all_reserved_entries_raises() -> None:
    with pytest.raises(ScoresFileError):
        parse_scores_json(
            json.dumps({"scores": [{"dimension": "suite_pass", "score": 10}]})
        )


def test_parse_folds_judge_spelling_onto_declared_dimension() -> None:
    """A judge's 'Accuracy' for a rubric declaring ``accuracy`` persists
    under the declared (normalized) spelling, not verbatim."""
    payload = {"scores": [{"dimension": "Accuracy", "score": 8}]}
    scales = {"accuracy": (0.0, 10.0, 1.0)}
    parsed = parse_scores_json(json.dumps(payload), dimension_scales=scales)
    assert [e.dimension for e in parsed.entries] == ["accuracy"]


def test_parse_folds_alias_spelling_onto_declared_dimension() -> None:
    """A judge's alias spelling (``functional``, ``complexity``) for a
    rubric declaring the canonical name persists under the declared name —
    the same fold the authoring side applies, so the declared dimension
    reads scored instead of a phantom column appearing."""
    payload = {
        "scores": [
            {"dimension": "functional", "score": 8},
            {"dimension": "complexity", "score": 6},
        ]
    }
    scales = {
        "functional_requirements": (0.0, 10.0, 1.0),
        "cyclomatic_complexity": (0.0, 10.0, 1.0),
    }
    parsed = parse_scores_json(json.dumps(payload), dimension_scales=scales)
    assert {e.dimension: e.score for e in parsed.entries} == {
        "functional_requirements": 8.0,
        "cyclomatic_complexity": 6.0,
    }


def test_parse_folded_duplicate_last_wins() -> None:
    """Case variants of one declared dimension dedup after folding."""
    payload = {
        "scores": [
            {"dimension": "Accuracy", "score": 3},
            {"dimension": "accuracy", "score": 9},
        ]
    }
    scales = {"accuracy": (0.0, 10.0, 1.0)}
    parsed = parse_scores_json(json.dumps(payload), dimension_scales=scales)
    assert [(e.dimension, e.score) for e in parsed.entries] == [("accuracy", 9.0)]
    assert any("duplicate" in w for w in parsed.warnings)


def test_parse_unknown_spelling_passes_through() -> None:
    """A folded name matching no declared dimension keeps verbatim spelling."""
    payload = {"scores": [{"dimension": "Some Custom Axis", "score": 6}]}
    scales = {"accuracy": (0.0, 10.0, 1.0)}
    parsed = parse_scores_json(json.dumps(payload), dimension_scales=scales)
    assert [e.dimension for e in parsed.entries] == ["Some Custom Axis"]
