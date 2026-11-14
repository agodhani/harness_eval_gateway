"""heg exec runner — the process that runs INSIDE the locked exec container (PR7).

Baked into the ``heg-exec`` image (``exec.Dockerfile``) and launched by
:meth:`sandbox.exec.ExecRunner.execute` as the container's PID 1. Contract:

- ``/srv/src`` — the candidate workspace, bind-mounted **read-only**. Copied
  to ``/work`` (a container-local dir owned by the nobody user) so builds,
  ``__pycache__``, pytest cache, and coverage data can write freely without
  ever touching the artifact.
- ``/out`` — a host-visible scratch dir (bind-mounted rw). The result payload
  is written to ``/out/exec_result.json`` AND printed on stdout as a
  ``HEG_EXEC_RESULT <json>`` sentinel line. ExecRunner trusts the LAST
  sentinel (the runner's final act) and uses the file as the fallback
  channel for when stdout can't be recovered.
- The container has ``--network none``, dropped capabilities, and resource
  caps — this script needs no privileges and must not assume DNS/TCP exist.

Phases (Python workspaces only for MVP — the collector gates detection):

1. ``copy`` — byte-capped copy of /srv/src into /work; dependency/vendor
   dirs pruned (they aren't the candidate's work and can be huge).
2. ``build`` — ``python -m compileall`` over /work: "does the submission
   byte-compile" is the cheap build-success signal for interpreted code.
3. ``tests`` — ``coverage run -m pytest`` when test files are detected
   (``test_*.py`` / ``*_test.py``); junit XML parsed for pass/fail counts.
4. ``coverage`` — ``coverage json`` totals (percent covered, statements).

Every phase is individually time-boxed (env knobs below) so a hanging test
suite can't burn the outer docker timeout. The process always exits 0 after
emitting a result payload — a missing payload on the host side means the
container itself died (OOM, docker kill), which the ExecRunner reports as an
infra error, not a candidate failure.

Env (injected by ExecRunner, all optional):
    EXEC_BUILD_TIMEOUT_SECONDS   compileall cap            (default 60)
    EXEC_TEST_TIMEOUT_SECONDS    pytest+coverage cap       (default 180)
    EXEC_MAX_WORKSPACE_BYTES     copy cap                  (default 300 MiB)
    COVERAGE_FILE                coverage data path        (default /tmp/.coverage)
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

SRC = Path("/srv/src")
WORK = Path("/work")
OUT = Path("/out")
RESULT_FILENAME = "exec_result.json"
RESULT_SENTINEL = "HEG_EXEC_RESULT "

BUILD_TIMEOUT = float(os.environ.get("EXEC_BUILD_TIMEOUT_SECONDS", "60"))
TEST_TIMEOUT = float(os.environ.get("EXEC_TEST_TIMEOUT_SECONDS", "180"))
MAX_COPY_BYTES = int(
    os.environ.get("EXEC_MAX_WORKSPACE_BYTES", str(300 * 1024 * 1024))
)
COVERAGE_DATA = os.environ.get("COVERAGE_FILE", "/tmp/.coverage")
JUNIT_PATH = Path("/tmp/junit.xml")
COVERAGE_JSON_PATH = Path("/tmp/coverage.json")

#: Captured per-phase output kept in the result payload (host truncates too).
TAIL_CHARS = 4000
#: Files a candidate would plausibly produce that are never worth exec'ing —
#: mirrors scoring.static.SKIP_DIRS (this file is standalone in the image and
#: cannot import the repo package).
SKIP_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".tox",
        ".venv",
        "venv",
        "env",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".next",
        ".nuxt",
        ".turbo",
        ".idea",
        ".vscode",
        "node_modules",
        "bower_components",
        "vendor",
        "target",
        "Pods",
    }
)

_TEST_FILE_RE = re.compile(r"^(test_.*|.*_test)\.py$")


def _tail(text: str | bytes | None) -> str:
    if isinstance(text, bytes):
        text = text.decode("utf-8", errors="replace")
    return (text or "")[-TAIL_CHARS:]


def _run(cmd: list[str], timeout: float, *, cwd: Path = WORK) -> dict[str, Any]:
    """Run ``cmd`` with a timeout; return a compact phase-result dict."""
    started = time.monotonic()
    try:
        proc = subprocess.run(
            [str(c) for c in cmd],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "command": " ".join(str(c) for c in cmd),
            "ok": False,
            "timed_out": True,
            "exit_code": None,
            "duration_seconds": round(time.monotonic() - started, 2),
            "stdout_tail": _tail(exc.stdout),
            "stderr_tail": _tail(exc.stderr),
        }
    except OSError as exc:  # missing binary — infra, not candidate failure
        return {
            "command": " ".join(str(c) for c in cmd),
            "ok": False,
            "timed_out": False,
            "exit_code": None,
            "duration_seconds": round(time.monotonic() - started, 2),
            "stdout_tail": "",
            "stderr_tail": _tail(f"{type(exc).__name__}: {exc}"),
        }
    return {
        "command": " ".join(str(c) for c in cmd),
        "ok": proc.returncode == 0,
        "timed_out": False,
        "exit_code": proc.returncode,
        "duration_seconds": round(time.monotonic() - started, 2),
        "stdout_tail": _tail(proc.stdout),
        "stderr_tail": _tail(proc.stderr),
    }


def _copy_workspace(warnings: list[str]) -> dict[str, Any]:
    """Copy /srv/src → /work with a byte cap and dep-dir pruning.

    Regular files and symlinks only — a FIFO/device would block or copy
    unbounded data. Symlinks are recreated (never followed): a link escaping
    /work just dangles inside the throwaway container.
    """
    copied_bytes = 0
    copied_files = 0
    truncated = False
    for dirpath, dirnames, filenames in os.walk(SRC):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        rel_dir = Path(dirpath).relative_to(SRC)
        for name in sorted(filenames):
            src = Path(dirpath) / name
            dst = WORK / rel_dir / name
            try:
                st = src.lstat()
            except OSError:
                continue
            try:
                dst.parent.mkdir(parents=True, exist_ok=True)
                if stat.S_ISLNK(st.st_mode):
                    if dst.is_symlink() or dst.exists():
                        dst.unlink()
                    os.symlink(os.readlink(src), dst)
                    continue
                if not stat.S_ISREG(st.st_mode):
                    warnings.append(f"skipped non-regular file: {src.relative_to(SRC)}")
                    continue
                copied_bytes += st.st_size
                if copied_bytes > MAX_COPY_BYTES:
                    truncated = True
                    warnings.append(
                        f"workspace copy truncated at {MAX_COPY_BYTES} bytes"
                    )
                    return {
                        "ok": True,
                        "bytes": copied_bytes,
                        "files": copied_files,
                        "truncated": True,
                    }
                shutil.copy2(src, dst)
                copied_files += 1
            except OSError as exc:
                warnings.append(f"copy failed for {src.relative_to(SRC)}: {exc}")
    return {
        "ok": True,
        "bytes": copied_bytes,
        "files": copied_files,
        "truncated": truncated,
    }


def _python_files() -> list[Path]:
    out: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(WORK):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            if name.endswith(".py"):
                out.append(Path(dirpath) / name)
    return out


def _test_files() -> list[Path]:
    return [p for p in _python_files() if _TEST_FILE_RE.match(p.name)]


def _parse_junit(path: Path) -> dict[str, int] | None:
    """Aggregate pytest junit XML → tests/failures/errors/skipped counts."""
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError):
        return None
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    if not suites:
        return None
    agg = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}
    for suite in suites:
        for key in agg:
            try:
                agg[key] += int(float(suite.get(key, 0) or 0))
            except (TypeError, ValueError):
                continue
    return agg


def _read_coverage_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return None
    totals = data.get("totals") or {}
    return {
        "ok": True,
        "percent": totals.get("percent_covered"),
        "statements": totals.get("num_statements"),
        "covered": totals.get("covered_lines"),
        "missing": totals.get("missing_lines"),
    }


def main() -> int:
    warnings: list[str] = []
    result: dict[str, Any] = {
        "version": 1,
        "language": "python",
        "phases": {},
        "warnings": warnings,
    }
    phases: dict[str, Any] = result["phases"]

    phases["copy"] = _copy_workspace(warnings)

    # Put the copied tree on sys.path for child processes: pytest's own
    # rootdir insertion only covers each test file's basedir, so a
    # ``tests/test_x.py`` couldn't ``import`` a root-level module otherwise.
    # (No pip install — the exec net is dead; PYTHONPATH is the honest
    # substitute for flat layouts. Packaged/installed projects are a
    # later-lane concern.)
    os.environ["PYTHONPATH"] = os.pathsep.join(
        p for p in (str(WORK), os.environ.get("PYTHONPATH", "")) if p
    )

    py_files = _python_files()
    phases["scan"] = {"python_files": len(py_files)}
    if not py_files:
        warnings.append("no python sources found after copy — nothing to run")
        _emit(result)
        return 0

    # -- build: byte-compile everything -------------------------------------
    build = _run(
        [sys.executable, "-m", "compileall", "-q", "-f", str(WORK)],
        BUILD_TIMEOUT,
    )
    phases["build"] = build

    # -- tests: coverage-wrapped pytest when test files exist ---------------
    test_files = _test_files()
    if not test_files:
        phases["tests"] = None
    else:
        run = _run(
            [
                sys.executable,
                "-m",
                "coverage",
                "run",
                f"--source={WORK}",
                f"--data-file={COVERAGE_DATA}",
                "-m",
                "pytest",
                "-q",
                "--tb=line",
                "-p",
                "no:cacheprovider",
                f"--junitxml={JUNIT_PATH}",
            ],
            TEST_TIMEOUT,
        )
        junit = _parse_junit(JUNIT_PATH)
        tests: dict[str, Any] = {
            "detected": True,
            "test_files": len(test_files),
            "command": run["command"],
            "exit_code": run["exit_code"],
            "timed_out": run["timed_out"],
            "duration_seconds": run["duration_seconds"],
            "stderr_tail": run["stderr_tail"],
            "stdout_tail": run["stdout_tail"],
        }
        if junit is None:
            warnings.append("pytest produced no parseable junit report")
            tests.update(
                {
                    "collected": 0,
                    "passed": 0,
                    "failed": 0,
                    "errors": 0,
                    "skipped": 0,
                    # pytest exit 5 = no tests collected; anything else
                    # nonzero still means "did not pass".
                    "ok": False,
                }
            )
        else:
            collected = junit["tests"]
            bad = junit["failures"] + junit["errors"]
            tests.update(
                {
                    "collected": collected,
                    "passed": max(collected - bad - junit["skipped"], 0),
                    "failed": junit["failures"],
                    "errors": junit["errors"],
                    "skipped": junit["skipped"],
                    # exit 0 = green; exit 5 = files matched but nothing
                    # collected — count that as detected-but-none, not fail.
                    "ok": run["exit_code"] == 0,
                }
            )
            if run["exit_code"] == 5:
                warnings.append(
                    "test files detected but pytest collected nothing"
                )
        phases["tests"] = tests

        # -- coverage totals (only meaningful when tests actually ran) ------
        if tests.get("collected"):
            cov_run = _run(
                [
                    sys.executable,
                    "-m",
                    "coverage",
                    "json",
                    f"--data-file={COVERAGE_DATA}",
                    "-o",
                    str(COVERAGE_JSON_PATH),
                ],
                60,
            )
            cov = _read_coverage_json(COVERAGE_JSON_PATH)
            if cov is not None:
                cov["duration_seconds"] = cov_run["duration_seconds"]
                phases["coverage"] = cov
            else:
                warnings.append(
                    "coverage data unavailable "
                    f"(coverage json rc={cov_run['exit_code']})"
                )
                phases["coverage"] = None
        else:
            phases["coverage"] = None

    _emit(result)
    return 0


def _emit(result: dict[str, Any]) -> None:
    """Publish the result on both channels: /out file, then stdout sentinel.

    The file write goes last-after-everything so a candidate can't pre-place
    a fake result (the /out dir starts empty and we overwrite atomically).
    The stdout sentinel — emitted last, after the file — is the channel
    ExecRunner trusts (LAST line wins, so candidate stdout can't spoof a
    later one); the file is the fallback when stdout can't be recovered.
    """
    payload = json.dumps(result, ensure_ascii=False)
    try:
        OUT.mkdir(parents=True, exist_ok=True)
        tmp = OUT / f".{RESULT_FILENAME}.tmp"
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, OUT / RESULT_FILENAME)
    except OSError:
        pass
    print(RESULT_SENTINEL + payload, flush=True)


if __name__ == "__main__":
    sys.exit(main())
