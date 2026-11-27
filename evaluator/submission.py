from __future__ import annotations

import os
import stat
from collections.abc import Iterator
from pathlib import Path

from evaluator.scores import SCORES_FILENAME
from evaluator.testgen import GENERATED_TESTS_DIRNAME
from scoring.render import ANALYSIS_JSON_FILENAME, ANALYSIS_MD_FILENAME

EVALUATOR_FILES = frozenset({
    SCORES_FILENAME,
    "rubric.json",
    "EVAL_PROMPT.md",
    "EVAL_TRANSCRIPT.log",
    ANALYSIS_MD_FILENAME,
    ANALYSIS_JSON_FILENAME,
    GENERATED_TESTS_DIRNAME,
})
_VCS_INTERNALS = {".git", ".hg", ".svn"}
_AIDER_FILES = {
    ".aider.conf.yml",
    ".aider.chat.history.md",
    ".aider.input.history",
    ".aider.llm.history.txt",
}


def _is_aider_ignore(path: Path, size: int) -> bool:
    if size > 1024:
        return False
    with path.open("rb") as fh:
        raw = fh.read(size + 1)
    entries = {line.strip() for line in raw.splitlines()
               if line.strip() and not line.lstrip().startswith(b"#")}
    return entries == {b".aider*"}


def submission_files(workspace: Path) -> Iterator[tuple[int, Path]]:
    for root, dirs, files in os.walk(workspace):
        directory = Path(root)
        root_level = directory == workspace
        reserved = _VCS_INTERNALS | EVALUATOR_FILES if root_level else _VCS_INTERNALS
        dirs[:] = sorted(
            name for name in dirs
            if name not in reserved and not (root_level and name.startswith(".aider.tags.cache"))
        )
        for name in sorted(files):
            if name in reserved or (root_level and name in _AIDER_FILES):
                continue
            path = directory / name
            try:
                info = path.lstat()
                if not stat.S_ISREG(info.st_mode) or not info.st_size:
                    continue
                if root_level and name == ".gitignore" and _is_aider_ignore(path, info.st_size):
                    continue
            except OSError:
                continue
            yield info.st_size, path


def has_submission(workspace: Path) -> bool:
    return next(submission_files(workspace), None) is not None
