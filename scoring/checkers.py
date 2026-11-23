"""PR26 — deterministic suite checkers: objective pass/fail on artifacts.

A benchmark-suite task (``benchmark_suites/<slug>/suite.yaml``) declares a
``check:`` block of deterministic checks; the orchestrator measures them
on each sandbox's artifact dir AFTER snapshot, before judging (see
``orchestrator.lifecycle._evaluate_all``), and persists one
``checker_results`` row per check. They complement the LLM judge — a red
check is honest evidence the judge can't smooth over; a green one is
deterministic corroboration.

Checker types (the ``check`` key in a normalized spec dict):

- ``file_exists`` — ``{"path": rel}``: some file/dir exists under the
  artifact's ``workspace/`` (container cells), else directly under the
  artifact dir.
- ``grep`` — ``{"path": rel, "pattern": regex}``: the pattern matches
  somewhere in that file's text (same workspace-first resolution).
- ``output_grep`` — ``{"pattern": regex}``: the pattern matches the
  prompt-cell ``output.txt`` (PR25 convention: written at the ARTIFACT
  root, not inside workspace/; workspace ``output.txt`` is the fallback).
- ``py_test`` — ``{"path": rel}``: run the pytest suite vendored under
  ``benchmark_suites/<slug>/<path>`` against the candidate workspace,
  inside the locked ``heg-exec`` container (``sandbox.exec.ExecRunner``
  ``test_target=`` — same ``--network none`` / demoted-uid / read-only
  posture as scoring exec + PR8 generated tests).

Status semantics mirror the exec collector's: ``pass``/``fail`` are real
measurements (a missing file or failing test suite is a fail, never an
error); ``skipped`` = declined (no workspace for py_test, or no docker /
exec image to run it in); ``error`` = the check itself couldn't run
(misconfigured spec, exec container died without a result).

Candidate-safety: every artifact path resolves inside the artifact dir
(``..`` escapes and planted symlinks can't reach host files), file reads
are byte-capped, and only ``py_test`` executes candidate code — inside
the zero-egress exec container, never on the host.

Synchronous (docker-wait blocks like ``ExecRunner``); the orchestrator
wraps calls in ``asyncio.to_thread``.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sandbox.exec import ExecRunner

logger = logging.getLogger(__name__)

#: Checker type names — must stay in sync with api.suites.CHECK_TYPES.
CHECK_TYPES = frozenset({"file_exists", "grep", "py_test", "output_grep"})

#: Byte cap on grep/output_grep reads — a multi-GB artifact file can't be
#: slurped into api memory; the pattern still sees the head of the file.
_MAX_READ_BYTES = 8 * 1024 * 1024

_MAX_DETAIL_LEN = 500


@dataclass
class CheckOutcome:
    """Result of one check — mirrors what ``checker_results`` persists."""

    check_type: str
    spec: dict[str, Any]
    # pass | fail | skipped | error
    status: str
    detail: str = ""
    # py_test measurement summary (collected/passed/failed/...) — feeds the
    # suite_check test_execution event; None for other checker types.
    test_summary: dict[str, Any] | None = None

    @property
    def passed(self) -> bool:
        return self.status == "pass"


def _outcome(
    check_type: str,
    spec: dict[str, Any],
    status: str,
    detail: str = "",
    test_summary: dict[str, Any] | None = None,
) -> CheckOutcome:
    return CheckOutcome(
        check_type=check_type,
        spec=spec,
        status=status,
        detail=detail[:_MAX_DETAIL_LEN],
        test_summary=test_summary,
    )


# ---------------------------------------------------------------------------
# path resolution
# ---------------------------------------------------------------------------


def _resolve_jailed(root: Path, rel: str) -> Path | None:
    """``root/rel`` resolved; None when it escapes ``root`` or is malformed.

    ``resolve()`` follows symlinks — a candidate-planted
    ``workspace/x -> /etc/passwd`` resolves outside the jail and is
    rejected rather than read. ``..`` components and absolute paths land
    outside the jail by construction once resolved.
    """
    p = PurePosixPath(str(rel).strip().replace("\\", "/"))
    if not str(rel).strip() or p.is_absolute() or ".." in p.parts or "\x00" in rel:
        return None
    try:
        base = root.resolve()
        target = (root / str(p)).resolve()
    except OSError:
        return None
    if target == base or base in target.parents:
        return target
    return None


def _artifact_file(artifact_dir: Path, rel: str) -> tuple[Path | None, str]:
    """Resolve a check path inside the artifact — ``workspace/`` first
    (container cells), then the artifact root directly (covers prompt-cell
    layout and flat artifacts). Returns ``(path, error_detail)`` — exactly
    one of them is set."""
    for cand in (artifact_dir / "workspace" / rel, artifact_dir / rel):
        resolved = _resolve_jailed(artifact_dir, cand.relative_to(artifact_dir))
        if resolved is not None and resolved.exists():
            return resolved, ""
    return None, f"not found: {rel}"


def _read_text_bounded(path: Path) -> str | None:
    """Head-bounded text read; None on unreadable/non-file targets."""
    try:
        if not path.is_file():
            return None
        with path.open("rb") as fh:
            raw = fh.read(_MAX_READ_BYTES + 1)
    except OSError:
        return None
    return raw[:_MAX_READ_BYTES].decode("utf-8", errors="replace")


def _match_line(text: str, match_start: int) -> int:
    """1-based line number where a regex match begins."""
    return text.count("\n", 0, match_start) + 1


# ---------------------------------------------------------------------------
# individual checkers
# ---------------------------------------------------------------------------


def _check_file_exists(
    spec: dict[str, Any], artifact_dir: Path, **_kw: Any
) -> CheckOutcome:
    rel = str(spec.get("path") or "")
    if not rel:
        return _outcome("file_exists", spec, "error", "missing 'path' in spec")
    found, why = _artifact_file(artifact_dir, rel)
    if found is not None:
        kind = "dir" if found.is_dir() else "file"
        return _outcome(
            "file_exists", spec, "pass", f"{kind} exists: {found.relative_to(artifact_dir)}"
        )
    if _resolve_jailed(artifact_dir, rel) is None and rel:
        # Path couldn't even be jailed — spec bug, not an artifact fail.
        return _outcome("file_exists", spec, "error", f"invalid path: {rel}")
    return _outcome("file_exists", spec, "fail", why)


def _check_grep(
    spec: dict[str, Any], artifact_dir: Path, **_kw: Any
) -> CheckOutcome:
    rel = str(spec.get("path") or "")
    pattern = str(spec.get("pattern") or "")
    if not rel or not pattern:
        return _outcome(
            "grep", spec, "error", "missing 'path'/'pattern' in spec"
        )
    try:
        rx = re.compile(pattern)
    except re.error as exc:
        return _outcome("grep", spec, "error", f"invalid pattern {pattern!r}: {exc}")
    found, why = _artifact_file(artifact_dir, rel)
    if found is None:
        if _resolve_jailed(artifact_dir, rel) is None:
            return _outcome("grep", spec, "error", f"invalid path: {rel}")
        return _outcome("grep", spec, "fail", why)
    text = _read_text_bounded(found)
    if text is None:
        return _outcome("grep", spec, "fail", f"not a readable file: {rel}")
    m = rx.search(text)
    if m:
        return _outcome(
            "grep",
            spec,
            "pass",
            f"matched at line {_match_line(text, m.start())} of {rel}",
        )
    return _outcome("grep", spec, "fail", f"no match for {pattern!r} in {rel}")


def _check_output_grep(
    spec: dict[str, Any], artifact_dir: Path, **_kw: Any
) -> CheckOutcome:
    pattern = str(spec.get("pattern") or "")
    if not pattern:
        return _outcome(
            "output_grep", spec, "error", "missing 'pattern' in spec"
        )
    try:
        rx = re.compile(pattern)
    except re.error as exc:
        return _outcome(
            "output_grep", spec, "error", f"invalid pattern {pattern!r}: {exc}"
        )
    # PR25 prompt-cell convention: output.txt at the ARTIFACT ROOT (prompt
    # cells write it there — no workspace/ exists). The workspace copy is
    # the fallback so container cells that deliberately wrote output.txt
    # still satisfy the check.
    for cand in (artifact_dir / "output.txt", artifact_dir / "workspace" / "output.txt"):
        resolved = _resolve_jailed(
            artifact_dir, cand.relative_to(artifact_dir)
        )
        if resolved is None or not resolved.exists():
            continue
        text = _read_text_bounded(resolved)
        if text is None:
            return _outcome(
                "output_grep", spec, "fail", "output.txt is not a readable file"
            )
        m = rx.search(text)
        if m:
            where = resolved.relative_to(artifact_dir)
            return _outcome(
                "output_grep",
                spec,
                "pass",
                f"matched at line {_match_line(text, m.start())} of {where}",
            )
        return _outcome(
            "output_grep", spec, "fail", f"no match for {pattern!r} in output.txt"
        )
    return _outcome("output_grep", spec, "fail", "output.txt not found")


def _check_py_test(
    spec: dict[str, Any],
    artifact_dir: Path,
    *,
    suite_dir: Path | None = None,
    runner: ExecRunner | None = None,
    **_kw: Any,
) -> CheckOutcome:
    rel = str(spec.get("path") or "")
    if not rel:
        return _outcome("py_test", spec, "error", "missing 'path' in spec")
    if suite_dir is None:
        return _outcome(
            "py_test", spec, "error", "no suite dir supplied for py_test"
        )
    target = _resolve_jailed(Path(suite_dir), rel)
    if target is None:
        return _outcome("py_test", spec, "error", f"invalid path: {rel}")
    if not target.is_dir():
        return _outcome(
            "py_test",
            spec,
            "error",
            f"suite test dir not found: benchmark_suites/<suite>/{rel}",
        )
    workspace = _resolve_jailed(artifact_dir, "workspace")
    if workspace is None or not workspace.is_dir():
        return _outcome(
            "py_test",
            spec,
            "skipped",
            "no artifact workspace — prompt cells produce only output.txt",
        )
    if runner is None:
        from sandbox.exec import ExecRunner

        runner = ExecRunner()
    outcome = runner.execute(workspace, test_target=target)
    if outcome.unavailable:
        return _outcome(
            "py_test",
            spec,
            "skipped",
            outcome.error or "exec environment unavailable",
        )
    if not outcome.ok:
        return _outcome(
            "py_test",
            spec,
            "error",
            outcome.error or "exec produced no result payload",
        )
    tests = (outcome.result or {}).get("phases", {}).get("tests")
    summary = {
        "collected": int(tests.get("collected") or 0),
        "passed": int(tests.get("passed") or 0),
        "failed": int(tests.get("failed") or 0),
        "errors": int(tests.get("errors") or 0),
        "skipped": int(tests.get("skipped") or 0),
        "duration_seconds": tests.get("duration_seconds"),
        "ok": bool(tests.get("ok")) if tests else False,
    }
    if not tests or not tests.get("detected"):
        return _outcome(
            "py_test",
            spec,
            "fail",
            "no tests collected from the suite dir",
            test_summary=summary,
        )
    collected = summary["collected"]
    passed_n = summary["passed"]
    failed_n = summary["failed"] + summary["errors"]
    if tests.get("ok") and collected > 0:
        detail = f"{passed_n}/{collected} tests passed"
        if summary["skipped"]:
            detail += f" ({summary['skipped']} skipped)"
        return _outcome("py_test", spec, "pass", detail, test_summary=summary)
    failed_tests = tests.get("failed_tests") or []
    tail = f" ({', '.join(str(t) for t in failed_tests[:3])})" if failed_tests else ""
    return _outcome(
        "py_test",
        spec,
        "fail",
        f"{failed_n} of {collected} tests failed{tail}",
        test_summary=summary,
    )


_CHECKS: dict[str, Any] = {
    "file_exists": _check_file_exists,
    "grep": _check_grep,
    "output_grep": _check_output_grep,
    "py_test": _check_py_test,
}


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------


def run_checks(
    entries: Iterable[dict[str, Any]],
    *,
    artifact_dir: Path | str,
    suite_dir: Path | str | None = None,
    runner: ExecRunner | None = None,
) -> list[CheckOutcome]:
    """Run every declared check against ``artifact_dir``; one outcome each.

    ``entries`` are the normalized flat dicts produced by
    ``api.suites.SuiteTask.checks()`` — ``{"check": <type>, ...spec}``.
    ``suite_dir`` is the vendored ``benchmark_suites/<slug>/`` dir —
    required for ``py_test`` (its ``path`` resolves inside it). A single
    shared ``ExecRunner`` (DI/test knob) serves all py_test checks.
    """
    artifact_dir = Path(artifact_dir)
    suite_path = Path(suite_dir) if suite_dir is not None else None
    outcomes: list[CheckOutcome] = []
    for entry in entries:
        if not isinstance(entry, dict):
            outcomes.append(
                _outcome("unknown", {}, "error", f"check entry is not a mapping: {entry!r}")
            )
            continue
        check_type = str(entry.get("check") or "")
        spec = {k: v for k, v in entry.items() if k != "check"}
        fn = _CHECKS.get(check_type)
        if fn is None:
            outcomes.append(
                _outcome(
                    check_type or "unknown",
                    spec,
                    "error",
                    f"unknown check type {check_type!r} (expected one of {sorted(CHECK_TYPES)})",
                )
            )
            continue
        try:
            outcomes.append(
                fn(spec, artifact_dir, suite_dir=suite_path, runner=runner)
            )
        except Exception as exc:  # noqa: BLE001 — a broken check is reported
            logger.warning("check %s crashed: %s", check_type, exc)
            outcomes.append(
                _outcome(check_type, spec, "error", f"check crashed: {type(exc).__name__}: {exc}")
            )
    return outcomes
