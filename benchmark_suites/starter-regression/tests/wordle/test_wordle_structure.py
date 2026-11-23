"""Vendored py_test suite for the `wordle-container` task.

Executed inside the locked heg-exec container: the candidate workspace is
copied to /work and pytest runs with that as its cwd, so workspace-relative
paths (Path("index.html")) are the contract — never absolute host paths.
"""

from pathlib import Path

INDEX = Path("index.html")


def test_index_html_exists() -> None:
    assert INDEX.is_file(), "index.html missing from the workspace"


def test_index_is_html_document() -> None:
    text = INDEX.read_text(encoding="utf-8", errors="replace").lower()
    assert "<html" in text or "<!doctype" in text


def test_index_has_inline_script() -> None:
    text = INDEX.read_text(encoding="utf-8", errors="replace").lower()
    assert "<script" in text, "a self-contained page needs inline JS"
