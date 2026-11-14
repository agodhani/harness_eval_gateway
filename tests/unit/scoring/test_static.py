"""StaticAnalysisCollector tests (PR6) — workspaces with known complexity.

Lizard measures cyclomatic complexity across languages; these fixtures pin
the exact CC values so regressions in the collector plumbing (walk, skip
rules, aggregation) are caught.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from scoring.static import (
    COLLECTOR_NAME,
    MAX_FILE_BYTES,
    StaticAnalysisCollector,
)

# Python: f_simple cc=1, f_branchy cc=7 (if + for + if + elif + `and`).
PY_SOURCE = """def f_simple():
    pass


def f_branchy(a, b):
    if a:
        for i in range(10):
            if i % 2:
                print(i)
            elif b:
                break
    elif a is None and b:
        return 1
    return 0
"""

# JS: loop cc=3 (for + if/else), g cc=2 (ternary).
JS_SOURCE = """function loop(a) {
  for (let i = 0; i < 10; i++) {
    if (a) { f(); } else { g(); }
  }
}
const g = (x) => x ? 1 : 2;
"""


@pytest.fixture()
def py_workspace(tmp_path: Path) -> Path:
    (tmp_path / "a.py").write_text(PY_SOURCE)
    return tmp_path


def test_python_file_known_complexity(py_workspace: Path) -> None:
    report = StaticAnalysisCollector().collect(py_workspace)
    assert report.name == COLLECTOR_NAME
    assert report.status == "ok"
    m = report.metrics
    assert m["files_scanned"] == 1
    assert m["files_analyzed"] == 1
    assert m["function_count"] == 2

    cc = m["cyclomatic_complexity"]
    assert cc["max"] == 7.0
    assert cc["mean"] == pytest.approx((1 + 7) / 2, abs=0.01)
    assert cc["over_10"] == 0
    assert cc["over_25"] == 0

    by_name = {f.name: f for f in report.functions}
    assert by_name["f_branchy"].cyclomatic_complexity == 7.0
    assert by_name["f_branchy"].file == "a.py"
    assert by_name["f_branchy"].line == 5
    assert by_name["f_branchy"].max_nesting_depth == 3
    assert by_name["f_branchy"].params == 2
    assert by_name["f_simple"].cyclomatic_complexity == 1.0

    assert m["worst_function"]["name"] == "f_branchy"
    assert m["worst_function"]["cyclomatic_complexity"] == 7.0
    assert m["languages"] == {"python": {"files": 1, "nloc": 12, "functions": 2}}

    fm = report.files[0]
    assert fm.path == "a.py"
    assert fm.language == "python"
    assert fm.function_count == 2
    assert fm.max_cyclomatic_complexity == 7.0


def test_multilanguage_workspace(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text(PY_SOURCE)
    (tmp_path / "game.js").write_text(JS_SOURCE)
    (tmp_path / "README.md").write_text("# not source\n")

    report = StaticAnalysisCollector().collect(tmp_path)
    m = report.metrics
    assert m["files_analyzed"] == 2
    assert m["function_count"] == 4
    assert set(m["languages"]) == {"python", "javascript"}
    assert m["languages"]["javascript"]["functions"] == 2

    # functions sorted worst-first by CC
    ccs = [f.cyclomatic_complexity for f in report.functions]
    assert ccs == sorted(ccs, reverse=True)
    assert report.functions[0].name == "f_branchy"


def test_nested_dirs_analyzed_relative_paths(tmp_path: Path) -> None:
    pkg = tmp_path / "src" / "pkg"
    pkg.mkdir(parents=True)
    (pkg / "mod.py").write_text("def f():\n    return 1\n")
    report = StaticAnalysisCollector().collect(tmp_path)
    assert report.metrics["files_analyzed"] == 1
    assert report.files[0].path == "src/pkg/mod.py"


def test_dependency_dirs_skipped(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("def f():\n    pass\n")
    for d in ("node_modules", ".git", "__pycache__", "venv"):
        sub = tmp_path / d / "pkg"
        sub.mkdir(parents=True)
        (sub / "x.py").write_text("def g():\n    while True:\n        pass\n")
    report = StaticAnalysisCollector().collect(tmp_path)
    assert report.metrics["files_analyzed"] == 1
    assert report.files[0].path == "app.py"


def test_non_source_files_ignored(tmp_path: Path) -> None:
    (tmp_path / "data.json").write_text("{}")
    (tmp_path / "page.html").write_text("<html></html>")
    report = StaticAnalysisCollector().collect(tmp_path)
    assert report.metrics["files_analyzed"] == 0
    assert report.metrics["files_scanned"] == 0
    assert any("no analyzable source" in w for w in report.warnings)


def test_oversized_file_skipped_with_warning(tmp_path: Path) -> None:
    big = tmp_path / "big.py"
    # ~10 bytes per line, comfortably over MAX_FILE_BYTES.
    big.write_text("x = 1\n" + "# padding\n" * (MAX_FILE_BYTES // 8))
    (tmp_path / "small.py").write_text("def f():\n    pass\n")
    report = StaticAnalysisCollector().collect(tmp_path)
    assert report.metrics["files_analyzed"] == 1
    assert any("skipped" in w for w in report.warnings)


def test_symlinked_source_not_followed(tmp_path: Path) -> None:
    if not hasattr(os, "symlink"):
        pytest.skip("POSIX only")
    outside = tmp_path.parent / "outside-secret.py"
    outside.write_text("def leaked():\n    pass\n")
    try:
        (tmp_path / "link.py").symlink_to(outside)
        (tmp_path / "real.py").write_text("def f():\n    pass\n")
        report = StaticAnalysisCollector().collect(tmp_path)
        assert report.metrics["files_analyzed"] == 1
        assert report.files[0].path == "real.py"
    finally:
        outside.unlink(missing_ok=True)


def test_no_functions_warns(tmp_path: Path) -> None:
    (tmp_path / "top.py").write_text("X = 1\nY = 2\n")
    report = StaticAnalysisCollector().collect(tmp_path)
    assert report.metrics["files_analyzed"] == 1
    assert report.metrics["function_count"] == 0
    assert report.metrics["worst_function"] is None
    assert any("no functions" in w for w in report.warnings)


def test_missing_workspace_skipped(tmp_path: Path) -> None:
    report = StaticAnalysisCollector().collect(tmp_path / "nope")
    assert report.status == "skipped"
    assert report.metrics == {}


def test_unparseable_source_does_not_raise(tmp_path: Path) -> None:
    (tmp_path / "broken.py").write_text("def f(:\n    syntax error here\n")
    (tmp_path / "ok.py").write_text("def g():\n    return 1\n")
    report = StaticAnalysisCollector().collect(tmp_path)
    assert report.status == "ok"
    # lizard tolerates broken files (soft failure); the good file is measured.
    assert report.metrics["files_analyzed"] >= 1
