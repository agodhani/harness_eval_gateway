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
_AIDER_IGNORE_BYTES = 1024
_AIDER_FILES = {
    ".aider.conf.yml",
    ".aider.chat.history.md",
    ".aider.input.history",
    ".aider.llm.history.txt",
}


def _is_aider_ignore(path: Path, size: int) -> bool:
    if size > _AIDER_IGNORE_BYTES:
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


def _matches_seed(left: Path, right: Path, *, allow_aider_append: bool) -> bool:
    with left.open("rb") as actual, right.open("rb") as original:
        ends_at_line_boundary = True
        while chunk := original.read(64 * 1024):
            if actual.read(len(chunk)) != chunk:
                return False
            ends_at_line_boundary = chunk.endswith(b"\n")
        if not allow_aider_append:
            return not actual.read(1)
        tail = actual.read(_AIDER_IGNORE_BYTES + 1)
        if len(tail) > _AIDER_IGNORE_BYTES:
            return False
        if not ends_at_line_boundary and not tail.startswith((b"\n", b"\r\n")):
            return False
        return {line.strip() for line in tail.splitlines() if line.strip()} == {b".aider*"}


def submission_files_with_changes(
    workspace: Path, seed_dir: Path
) -> Iterator[tuple[bool, int, Path]]:
    if not seed_dir.is_dir():
        raise FileNotFoundError("run seed is unavailable for submission comparison")
    seed_sizes = {path.relative_to(seed_dir): size for size, path in submission_files(seed_dir)}
    for size, path in submission_files(workspace):
        rel = path.relative_to(workspace)
        seed_size = seed_sizes.get(rel)
        allow_aider_append = (
            rel == Path(".gitignore")
            and seed_size is not None
            and 0 < size - seed_size <= _AIDER_IGNORE_BYTES
        )
        if seed_size is None or (seed_size != size and not allow_aider_append):
            yield True, size, path
            continue
        seed_path = seed_dir / rel
        mode_changed = stat.S_IMODE(path.lstat().st_mode) != stat.S_IMODE(seed_path.lstat().st_mode)
        changed = mode_changed or not _matches_seed(
            path, seed_path, allow_aider_append=allow_aider_append
        )
        yield changed, size, path


def has_submission(workspace: Path, seed_dir: Path | None = None) -> bool:
    if seed_dir is None:
        return next(submission_files(workspace), None) is not None
    return any(changed for changed, _, _ in submission_files_with_changes(workspace, seed_dir))
