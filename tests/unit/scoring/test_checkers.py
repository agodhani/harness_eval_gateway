"""PR26 — deterministic suite checkers: file_exists / grep / output_grep /
py_test pass+fail on fixture artifacts.

Hermetic: fixture artifact dirs under tmp_path (``workspace/`` +
``output.txt`` written directly); the py_test checker's ExecRunner is a
scriptable stand-in — no docker, no gateway (``SCORING_EXEC`` stays pinned
off and is irrelevant here — checkers don't read that flag; they only run
when a suite task declares them).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from scoring import checkers


@pytest.fixture()
def artifact(tmp_path: Path) -> Path:
    """A container-cell artifact dir: ``workspace/`` with a couple files."""
    art = tmp_path / "art"
    ws = art / "workspace"
    ws.mkdir(parents=True)
    (ws / "index.html").write_text(
        "<!DOCTYPE html>\n<html>\n<body>hi</body>\n</html>\n"
    )
    (ws / "main.py").write_text("def f():\n    return 42\n")
    return art


@pytest.fixture()
def prompt_artifact(tmp_path: Path) -> Path:
    """A prompt-cell artifact dir (PR25 shape): ``output.txt`` at the
    artifact ROOT, no ``workspace/``."""
    art = tmp_path / "prompt-art"
    art.mkdir(parents=True)
    (art / "output.txt").write_text("Working…\nTOTAL: 42\n")
    return art


@pytest.fixture()
def suite_dir(tmp_path: Path) -> Path:
    """A vendored suite dir holding a ``tests/`` py_test target."""
    d = tmp_path / "suite"
    (d / "tests").mkdir(parents=True)
    (d / "tests" / "test_x.py").write_text("def test_x():\n    assert True\n")
    return d


# ---------------------------------------------------------------------------
# file_exists
# ---------------------------------------------------------------------------


def test_file_exists_pass_and_fail(artifact: Path) -> None:
    outcomes = checkers.run_checks(
        [
            {"check": "file_exists", "path": "index.html"},
            {"check": "file_exists", "path": "missing.js"},
        ],
        artifact_dir=artifact,
    )
    assert [o.status for o in outcomes] == ["pass", "fail"]
    assert outcomes[0].passed and not outcomes[1].passed
    assert "index.html" in outcomes[0].detail
    assert "missing.js" in outcomes[1].detail


def test_file_exists_resolves_workspace_first(artifact: Path) -> None:
    # Same relative path exists only at the artifact root → still found.
    (artifact / "root-only.txt").write_text("x")
    (out,) = checkers.run_checks(
        [{"check": "file_exists", "path": "root-only.txt"}],
        artifact_dir=artifact,
    )
    assert out.status == "pass"
    assert "root-only.txt" in out.detail


def test_file_exists_rejects_traversal(artifact: Path) -> None:
    (out,) = checkers.run_checks(
        [{"check": "file_exists", "path": "../outside.txt"}],
        artifact_dir=artifact,
    )
    assert out.status == "error"
    assert "invalid path" in out.detail


def test_file_exists_symlink_escape_rejected(artifact: Path, tmp_path: Path) -> None:
    outside = tmp_path / "secret.txt"
    outside.write_text("shh")
    link = artifact / "workspace" / "link.txt"
    link.symlink_to(outside)
    (out,) = checkers.run_checks(
        [{"check": "file_exists", "path": "link.txt"}],
        artifact_dir=artifact,
    )
    # The planted symlink resolves outside the jail → treated as absent.
    assert out.status == "fail"


# ---------------------------------------------------------------------------
# grep
# ---------------------------------------------------------------------------


def test_grep_pass_fail_and_missing_file(artifact: Path) -> None:
    outcomes = checkers.run_checks(
        [
            {"check": "grep", "path": "index.html", "pattern": "<html"},
            {"check": "grep", "path": "index.html", "pattern": "wordle"},
            {"check": "grep", "path": "nope.css", "pattern": ".*"},
        ],
        artifact_dir=artifact,
    )
    assert [o.status for o in outcomes] == ["pass", "fail", "fail"]
    assert "line 2" in outcomes[0].detail
    assert "no match" in outcomes[1].detail
    assert "not found" in outcomes[2].detail


def test_grep_invalid_pattern_is_error(artifact: Path) -> None:
    (out,) = checkers.run_checks(
        [{"check": "grep", "path": "index.html", "pattern": "([unclosed"}],
        artifact_dir=artifact,
    )
    assert out.status == "error"


def test_grep_on_directory_target_fails(artifact: Path) -> None:
    (artifact / "workspace" / "subdir").mkdir()
    (out,) = checkers.run_checks(
        [{"check": "grep", "path": "subdir", "pattern": "x"}],
        artifact_dir=artifact,
    )
    assert out.status == "fail"
    assert "not a readable file" in out.detail


# ---------------------------------------------------------------------------
# output_grep — the prompt-cell checker
# ---------------------------------------------------------------------------


def test_output_grep_pass_and_fail_on_prompt_output(prompt_artifact: Path) -> None:
    outcomes = checkers.run_checks(
        [
            {"check": "output_grep", "pattern": r"TOTAL:\s*42"},
            {"check": "output_grep", "pattern": r"TOTAL:\s*43"},
        ],
        artifact_dir=prompt_artifact,
    )
    assert [o.status for o in outcomes] == ["pass", "fail"]
    assert "line 2" in outcomes[0].detail
    assert "no match" in outcomes[1].detail


def test_output_grep_missing_output_fails(artifact: Path) -> None:
    (out,) = checkers.run_checks(
        [{"check": "output_grep", "pattern": ".*"}],
        artifact_dir=artifact,  # container artifact — no output.txt
    )
    assert out.status == "fail"
    assert "output.txt not found" in out.detail


def test_output_grep_workspace_fallback(artifact: Path) -> None:
    """A container cell that wrote output.txt into its workspace also
    satisfies the check (root copy wins when both exist)."""
    (artifact / "workspace" / "output.txt").write_text("RESULT: ok\n")
    (out,) = checkers.run_checks(
        [{"check": "output_grep", "pattern": "RESULT: ok"}],
        artifact_dir=artifact,
    )
    assert out.status == "pass"


# ---------------------------------------------------------------------------
# py_test — via the locked exec runner (fake)
# ---------------------------------------------------------------------------


@dataclass
class FakeRunner:
    """ExecRunner stand-in — returns scripted outcomes, records calls."""

    outcome: Any
    calls: list = field(default_factory=list)

    def execute(self, workspace, *, test_target=None, timeout_seconds=None):
        self.calls.append(
            {"workspace": Path(workspace), "test_target": Path(test_target) if test_target else None}
        )
        return self.outcome


def _exec_outcome(*, tests: dict | None, unavailable: bool = False, error: str | None = None):
    from sandbox.exec import ExecOutcome

    out = ExecOutcome(image="heg-exec:latest")
    out.unavailable = unavailable
    out.error = error
    if tests is not None:
        out.result = {"phases": {"tests": tests}}
    return out


def test_py_test_pass(artifact: Path, suite_dir: Path) -> None:
    runner = FakeRunner(
        _exec_outcome(
            tests={
                "detected": True,
                "ok": True,
                "collected": 3,
                "passed": 3,
                "failed": 0,
                "errors": 0,
                "skipped": 0,
                "duration_seconds": 1.5,
            }
        )
    )
    (out,) = checkers.run_checks(
        [{"check": "py_test", "path": "tests"}],
        artifact_dir=artifact,
        suite_dir=suite_dir,
        runner=runner,
    )
    assert out.status == "pass"
    assert "3/3" in out.detail
    assert out.test_summary and out.test_summary["collected"] == 3
    call = runner.calls[0]
    # The vendored suite dir is the read-only test_target; the candidate
    # workspace is what gets executed.
    assert call["workspace"] == (artifact / "workspace").resolve()
    assert call["test_target"] == (suite_dir / "tests").resolve()


def test_py_test_fail_on_red_suite(artifact: Path, suite_dir: Path) -> None:
    runner = FakeRunner(
        _exec_outcome(
            tests={
                "detected": True,
                "ok": False,
                "collected": 3,
                "passed": 2,
                "failed": 1,
                "errors": 0,
                "skipped": 0,
                "failed_tests": ["tests/test_x.py::test_bad"],
            }
        )
    )
    (out,) = checkers.run_checks(
        [{"check": "py_test", "path": "tests"}],
        artifact_dir=artifact,
        suite_dir=suite_dir,
        runner=runner,
    )
    assert out.status == "fail"
    assert "1 of 3" in out.detail
    assert "test_bad" in out.detail


@pytest.mark.parametrize("phases", [{}, {"tests": None}])
def test_py_test_no_test_phase_is_measured_failure(
    artifact: Path, suite_dir: Path, phases: dict
) -> None:
    outcome = _exec_outcome(tests=None)
    outcome.result = {"phases": phases}
    (out,) = checkers.run_checks(
        [{"check": "py_test", "path": "tests"}],
        artifact_dir=artifact,
        suite_dir=suite_dir,
        runner=FakeRunner(outcome),
    )
    assert out.status == "fail"
    assert out.detail == "no tests collected from the suite dir"
    assert out.test_summary and out.test_summary["collected"] == 0


def test_py_test_unavailable_exec_is_skipped(artifact: Path, suite_dir: Path) -> None:
    """No docker / no heg-exec image → honest 'skipped', never fail/error."""
    runner = FakeRunner(
        _exec_outcome(
            tests=None,
            unavailable=True,
            error="exec image 'heg-exec:latest' not found",
        )
    )
    (out,) = checkers.run_checks(
        [{"check": "py_test", "path": "tests"}],
        artifact_dir=artifact,
        suite_dir=suite_dir,
        runner=runner,
    )
    assert out.status == "skipped"
    assert "heg-exec" in out.detail


def test_py_test_exec_infra_failure_is_error(artifact: Path, suite_dir: Path) -> None:
    runner = FakeRunner(
        _exec_outcome(tests=None, error="container died without a result")
    )
    (out,) = checkers.run_checks(
        [{"check": "py_test", "path": "tests"}],
        artifact_dir=artifact,
        suite_dir=suite_dir,
        runner=runner,
    )
    assert out.status == "error"


def test_py_test_no_workspace_is_skipped(suite_dir: Path, tmp_path: Path) -> None:
    """Prompt-cell artifact (no workspace/) — py_test can't run → skipped,
    and the runner is never invoked."""
    art = tmp_path / "art"
    art.mkdir()
    (art / "output.txt").write_text("x")
    runner = FakeRunner(_exec_outcome(tests=None))
    (out,) = checkers.run_checks(
        [{"check": "py_test", "path": "tests"}],
        artifact_dir=art,
        suite_dir=suite_dir,
        runner=runner,
    )
    assert out.status == "skipped"
    assert runner.calls == []


def test_py_test_missing_suite_dir_is_error(artifact: Path, suite_dir: Path) -> None:
    (out,) = checkers.run_checks(
        [{"check": "py_test", "path": "tests/missing"}],
        artifact_dir=artifact,
        suite_dir=suite_dir,
    )
    assert out.status == "error"
    assert "not found" in out.detail


def test_py_test_traversal_outside_suite_is_error(
    artifact: Path, suite_dir: Path
) -> None:
    (out,) = checkers.run_checks(
        [{"check": "py_test", "path": "../elsewhere"}],
        artifact_dir=artifact,
        suite_dir=suite_dir,
    )
    assert out.status == "error"
    assert "invalid path" in out.detail


# ---------------------------------------------------------------------------
# run_checks edge cases
# ---------------------------------------------------------------------------


def test_unknown_check_type_is_error(artifact: Path) -> None:
    (out,) = checkers.run_checks(
        [{"check": "shell_escape", "cmd": "rm -rf /"}],
        artifact_dir=artifact,
    )
    assert out.status == "error"
    assert "unknown check type" in out.detail


def test_check_types_match_suite_store() -> None:
    """The suite yaml validator and the checker engine agree on names."""
    from api.suites import CHECK_TYPES as SUITE_CHECK_TYPES

    assert set(SUITE_CHECK_TYPES) == set(checkers.CHECK_TYPES)


# ---------------------------------------------------------------------------
# exact / contains / numeric_tol — output checkers (prompt cells)
# ---------------------------------------------------------------------------


def test_exact_pass_and_fail(prompt_artifact: Path) -> None:
    outcomes = checkers.run_checks(
        [
            {"check": "exact", "equals": "Working…\nTOTAL: 42"},
            {"check": "exact", "equals": "TOTAL: 42"},
        ],
        artifact_dir=prompt_artifact,
    )
    assert [o.status for o in outcomes] == ["pass", "fail"]
    assert "expected" in outcomes[1].detail


def test_exact_missing_output_fails(artifact: Path) -> None:
    (out,) = checkers.run_checks(
        [{"check": "exact", "equals": "x"}],
        artifact_dir=artifact,
    )
    assert out.status == "fail"
    assert "not found" in out.detail


def test_contains_pass_and_fail(prompt_artifact: Path) -> None:
    outcomes = checkers.run_checks(
        [
            {"check": "contains", "substring": "TOTAL"},
            {"check": "contains", "substring": "TOTAL: 43"},
        ],
        artifact_dir=prompt_artifact,
    )
    assert [o.status for o in outcomes] == ["pass", "fail"]
    assert "not found" in outcomes[1].detail


def test_numeric_tol_prompt_output(prompt_artifact: Path) -> None:
    outcomes = checkers.run_checks(
        [
            {"check": "numeric_tol", "value": 42, "tolerance": 0},
            {"check": "numeric_tol", "value": 41.5, "tolerance": 1},
            {"check": "numeric_tol", "value": 50, "tolerance": 1},
        ],
        artifact_dir=prompt_artifact,
    )
    assert [o.status for o in outcomes] == ["pass", "pass", "fail"]
    assert "42" in outcomes[2].detail


def test_numeric_tol_no_number_fails(tmp_path: Path) -> None:
    art = tmp_path / "art"
    art.mkdir()
    (art / "output.txt").write_text("no digits here")
    (out,) = checkers.run_checks(
        [{"check": "numeric_tol", "value": 1, "tolerance": 0.5}],
        artifact_dir=art,
    )
    assert out.status == "fail"
    assert "no number" in out.detail


def test_numeric_tol_bad_spec_is_error(prompt_artifact: Path) -> None:
    outcomes = checkers.run_checks(
        [
            {"check": "numeric_tol", "value": "NaN-input"},
            {"check": "numeric_tol", "value": 5, "tolerance": -1},
        ],
        artifact_dir=prompt_artifact,
    )
    assert [o.status for o in outcomes] == ["error", "error"]


# ---------------------------------------------------------------------------
# json_schema — subset validator on output.txt
# ---------------------------------------------------------------------------


@pytest.fixture()
def json_artifact(tmp_path: Path) -> Path:
    art = tmp_path / "json-art"
    art.mkdir()
    (art / "output.txt").write_text('{"answer": 42, "unit": "items"}\n')
    return art


def test_json_schema_pass(json_artifact: Path) -> None:
    (out,) = checkers.run_checks(
        [
            {
                "check": "json_schema",
                "schema": {
                    "type": "object",
                    "required": ["answer", "unit"],
                    "properties": {
                        "answer": {"type": "integer", "minimum": 0},
                        "unit": {"type": "string"},
                    },
                    "additionalProperties": False,
                },
            }
        ],
        artifact_dir=json_artifact,
    )
    assert out.status == "pass"


def test_json_schema_violations(json_artifact: Path) -> None:
    outcomes = checkers.run_checks(
        [
            # missing required key
            {
                "check": "json_schema",
                "schema": {"type": "object", "required": ["missing"]},
            },
            # wrong type
            {
                "check": "json_schema",
                "schema": {"type": "object", "properties": {"answer": {"type": "string"}}},
            },
            # extra key under additionalProperties:false
            {
                "check": "json_schema",
                "schema": {
                    "type": "object",
                    "properties": {"answer": {"type": "integer"}},
                    "additionalProperties": False,
                },
            },
            # const mismatch
            {
                "check": "json_schema",
                "schema": {"type": "object", "properties": {"unit": {"const": "kg"}}},
            },
            # numeric bound
            {
                "check": "json_schema",
                "schema": {"type": "object", "properties": {"answer": {"maximum": 10}}},
            },
        ],
        artifact_dir=json_artifact,
    )
    assert [o.status for o in outcomes] == ["fail"] * 5
    assert "required" in outcomes[0].detail
    assert "type" in outcomes[1].detail
    assert "unexpected keys" in outcomes[2].detail
    assert "const" in outcomes[3].detail
    assert "maximum" in outcomes[4].detail


def test_json_schema_nested_and_enum(json_artifact: Path) -> None:
    (json_artifact / "output.txt").write_text(
        '{"items": [{"sku": "a", "qty": 2}, {"sku": "b", "qty": -1}]}'
    )
    outcomes = checkers.run_checks(
        [
            {
                "check": "json_schema",
                "schema": {
                    "type": "object",
                    "properties": {
                        "items": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "required": ["sku"],
                                "properties": {
                                    "qty": {"type": "integer", "minimum": 0}
                                },
                            },
                        }
                    },
                },
            },
            {
                "check": "json_schema",
                "schema": {
                    "type": "object",
                    "properties": {
                        "items": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {"sku": {"enum": ["a", "c"]}},
                            },
                        }
                    },
                },
            },
        ],
        artifact_dir=json_artifact,
    )
    # First fails on the negative qty at [1].qty; second on sku "b" ∉ enum.
    assert [o.status for o in outcomes] == ["fail", "fail"]
    assert "qty" in outcomes[0].detail
    assert "enum" in outcomes[1].detail


def test_json_schema_non_json_output_fails(artifact: Path) -> None:
    (artifact / "output.txt").write_text("not json at all")
    (out,) = checkers.run_checks(
        [{"check": "json_schema", "schema": {"type": "object"}}],
        artifact_dir=artifact,
    )
    assert out.status == "fail"
    assert "not valid JSON" in out.detail


def test_json_schema_unsupported_keys_is_error(json_artifact: Path) -> None:
    (out,) = checkers.run_checks(
        [{"check": "json_schema", "schema": {"type": "object", "$ref": "#/x"}}],
        artifact_dir=json_artifact,
    )
    assert out.status == "error"
    assert "unsupported schema keys" in out.detail


# ---------------------------------------------------------------------------
# summarize / suite_checks_doc / signal_metrics — the shared roll-up
# ---------------------------------------------------------------------------


def test_summarize_pass_rate_excludes_skipped() -> None:
    """pass_rate is passed/measured — skipped/error aren't outcomes."""
    outs = [
        checkers.CheckOutcome("file_exists", {}, "pass"),
        checkers.CheckOutcome("file_exists", {}, "fail"),
        checkers.CheckOutcome("py_test", {}, "skipped"),
        checkers.CheckOutcome("exact", {}, "error"),
    ]
    roll = checkers.summarize(outs)
    assert roll["total"] == 4
    assert roll["measured"] == 2
    assert roll["pass_rate"] == 0.5
    assert roll["skipped"] == 1 and roll["errors"] == 1


def test_summarize_nothing_measured_gives_null_rate() -> None:
    outs = [checkers.CheckOutcome("py_test", {}, "skipped")]
    assert checkers.summarize(outs)["pass_rate"] is None


def test_suite_checks_doc_shape(tmp_path: Path) -> None:
    outs = [
        checkers.CheckOutcome("exact", {"equals": "x"}, "pass", "ok"),
        checkers.CheckOutcome("contains", {"substring": "y"}, "fail", "no y"),
    ]
    doc = checkers.suite_checks_doc(outs, suite_ref="demo", suite_index=2)
    assert doc["suite_ref"] == "demo"
    assert doc["suite_index"] == 2
    assert doc["pass_rate"] == 0.5
    assert doc["checks"][0]["check"] == "exact"
    metrics = checkers.signal_metrics(doc)
    assert metrics["passed"] == 1
    assert metrics["checks"][1]["status"] == "fail"
