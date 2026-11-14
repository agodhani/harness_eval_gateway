"""In-image exec runner unit tests (PR7/PR8) — pure-helper coverage.

``sandbox/images/exec_runner.py`` runs INSIDE the heg-exec container as a
standalone script (stdlib only); it is loaded here by path — the image dir
isn't a package. Full lifecycle is covered by
``tests/integration/test_exec_scoring.py`` (docker-marked); these tests pin
the PR8 additions: junit ``failed_tests`` names, test-target staging, and
test-file discovery against an arbitrary root.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
RUNNER_PATH = REPO_ROOT / "sandbox" / "images" / "exec_runner.py"

spec = importlib.util.spec_from_file_location("heg_exec_runner", RUNNER_PATH)
assert spec is not None and spec.loader is not None
er = importlib.util.module_from_spec(spec)
spec.loader.exec_module(er)

JUNIT_XML = """<?xml version="1.0" encoding="utf-8"?>
<testsuites>
  <testsuite name="pytest" errors="1" failures="1" skipped="1" tests="5" time="0.5">
    <testcase classname="test_calc" name="test_add" time="0.01"/>
    <testcase classname="test_calc" name="test_sub" time="0.02">
      <failure message="assert 1 == 2">boom</failure>
    </testcase>
    <testcase classname="gen.test_extra" name="test_broken" time="0.0">
      <error message="collection error">kaboom</error>
    </testcase>
    <testcase classname="test_calc" name="test_skip" time="0.0">
      <skipped message="nope"/>
    </testcase>
    <testcase classname="test_calc" name="test_mul" time="0.01"/>
  </testsuite>
</testsuites>
"""


def test_parse_junit_collects_failed_test_names(tmp_path: Path) -> None:
    path = tmp_path / "junit.xml"
    path.write_text(JUNIT_XML)
    agg = er._parse_junit(path)
    assert agg is not None
    assert agg["tests"] == 5
    assert agg["failures"] == 1
    assert agg["errors"] == 1
    assert agg["skipped"] == 1
    assert agg["failed_tests"] == [
        "test_calc::test_sub",
        "gen.test_extra::test_broken",
    ]


def test_parse_junit_unparseable_returns_none(tmp_path: Path) -> None:
    path = tmp_path / "junit.xml"
    path.write_text("<not xml")
    assert er._parse_junit(path) is None


def test_parse_junit_missing_returns_none(tmp_path: Path) -> None:
    assert er._parse_junit(tmp_path / "absent.xml") is None


def test_stage_test_target_copies_into_writable_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = tmp_path / "mounted"
    src.mkdir()
    (src / "test_x.py").write_text("def test_x():\n    assert True\n")
    (src / "_manifest.json").write_text("{}")
    dest = tmp_path / "gen_tests"
    monkeypatch.setattr(er, "GEN_TESTS_DIR", dest)
    warnings: list[str] = []
    out = er._stage_test_target(src, warnings)
    assert out == dest
    assert (dest / "test_x.py").is_file()
    assert warnings == []


def test_stage_test_target_missing_warns(tmp_path: Path) -> None:
    warnings: list[str] = []
    assert er._stage_test_target(tmp_path / "nope", warnings) is None
    assert "not a directory" in warnings[0]


def test_test_files_scans_arbitrary_root(tmp_path: Path) -> None:
    (tmp_path / "test_a.py").write_text("x = 1\n")
    (tmp_path / "helper.py").write_text("x = 1\n")
    sub = tmp_path / "tests"
    sub.mkdir()
    (sub / "test_b.py").write_text("x = 1\n")
    names = sorted(p.name for p in er._test_files(tmp_path))
    assert names == ["test_a.py", "test_b.py"]
