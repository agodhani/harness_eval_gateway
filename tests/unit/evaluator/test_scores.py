"""scores.json parsing/validation tests (F7)."""

from __future__ import annotations

import json

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
    assert parsed.overall is None
    assert any("overall" in w for w in parsed.warnings)


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
    # trailing object was truncated -> overall lost, entries salvaged
    assert parsed.overall is None


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
