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
- ``exact`` — ``{"equals": str}``: ``output.txt`` (whitespace-trimmed)
  equals the expected text exactly.
- ``contains`` — ``{"substring": str}``: literal substring appears in
  ``output.txt`` — the no-regex sibling of ``output_grep``.
- ``json_schema`` — ``{"schema": dict}``: ``output.txt`` parses as JSON
  and validates against a documented subset of JSON Schema (``type``,
  ``required``, ``properties``, ``items``, ``enum``, ``const``,
  ``minimum``/``maximum``, ``exclusiveMinimum``/``exclusiveMaximum``,
  ``minLength``/``maxLength``, ``pattern``, ``additionalProperties``).
  No external validator dep — suite authors get a strict, enumerable
  contract; unsupported keys are a spec ``error``, not a silent skip.
- ``numeric_tol`` — ``{"value": number, "tolerance": number}``: the first
  number found in ``output.txt`` is within ``tolerance`` of ``value``.
- ``py_test`` — ``{"path": rel}``: run the pytest suite vendored under
  ``benchmark_suites/<slug>/<path>`` against the candidate workspace,
  inside the locked ``heg-exec`` container (``sandbox.exec.ExecRunner``
  ``test_target=`` — same ``--network none`` / demoted-uid / read-only
  posture as scoring exec + PR8 generated tests).

Roadmap-card mapping: the card names checkers ``exact | regex | contains
| json_schema | numeric_tol | file_exists | exec_exit_zero`` — ``regex``
ships as ``output_grep`` (output) + ``grep`` (any artifact file), and the
exec check ships as ``py_test`` (a pytest run is the concrete
exit-nonzero signal ExecRunner supports); the names above are the
implementation spellings.

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

import json
import logging
import math
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sandbox.exec import ExecRunner

logger = logging.getLogger(__name__)

#: Checker type names — must stay in sync with api.suites.CHECK_TYPES.
CHECK_TYPES = frozenset(
    {
        "file_exists",
        "grep",
        "py_test",
        "output_grep",
        "exact",
        "contains",
        "json_schema",
        "numeric_tol",
    }
)

#: ``scores.dimension`` for the deterministic suite pass rate (0–10 scale,
#: matching every other dimension). Persisted alongside checker_results so
#: the suite verdict surfaces anywhere scores are read — rubric-free
#: (``rubric_id`` NULL), the row exists even when no judge ever ran.
SUITE_PASS_DIMENSION = "suite_pass"

#: Normalized names a dimension can never take: ``overall`` and
#: ``suite_pass`` are reserved pseudo-rows in ``scores.dimension`` (the
#: results payload routes them to the headline / deterministic-checker
#: slots), and ``scores``/``summary``/``total`` are the eval parser's
#: document fields — a real dimension under one of these names loses its
#: score or masquerades as another field. Single-sourced for both guards:
#: ``orchestrator.rubric`` rejects them at authoring and
#: ``evaluator.scores`` drops judge entries naming them.
RESERVED_DIMENSION_NAMES = frozenset(
    {"overall", "scores", "summary", "total", SUITE_PASS_DIMENSION}
)

#: LLM re-phrasings folded onto the canonical dimension names — a SOFT
#: normalizer (arbitrary names pass through untouched), shared by the
#: authoring fold (``orchestrator.rubric``) and the eval parser's
#: declared-dimension match (``evaluator.scores``): a judge emitting an
#: alias spelling lands on the declared name. Frozen rubrics can never
#: declare an alias word — the authoring fold maps them onto canonical
#: first — so eval-side folding can't steal a real dimension's name.
DIMENSION_ALIASES = {
    "functional": "functional_requirements",
    "functional_req": "functional_requirements",
    "functional_reqs": "functional_requirements",
    "requirements": "functional_requirements",
    "acceptance": "acceptance_criteria",
    "acceptance_tests": "acceptance_criteria",
    "code": "code_quality",
    "implementation": "code_quality",
    "implementation_quality": "code_quality",
    "code_implementation": "code_quality",
    "code_implementation_quality": "code_quality",
    "quality": "code_quality",
    "complexity": "cyclomatic_complexity",
    "cyclomatic": "cyclomatic_complexity",
}

_DIM_NAME_FOLD_RE = re.compile(r"[^a-z0-9]+")


def fold_dimension_name(name: str) -> str:
    """Shared dimension-name fold: snake_case normalization +
    :data:`DIMENSION_ALIASES` resolution.

    ``"Functional Requirements!!"`` → ``functional_requirements``,
    ``"complexity"`` → ``cyclomatic_complexity``; unknown names keep their
    own folded spelling. Rubric authoring and judge-score parsing fold
    names identically so a judge's phrasing lands on the declared
    dimension.
    """
    norm = _DIM_NAME_FOLD_RE.sub("_", name.strip().lower()).strip("_")
    return DIMENSION_ALIASES.get(norm, norm)


#: Artifact-side measurement file: ``_run_suite_checks`` writes the raw
#: outcomes here so the evaluator can fold them into ``analysis.json`` /
#: ``ANALYSIS.md`` (the ``suite`` signal) without re-measuring.
SUITE_CHECKS_FILENAME = "suite_checks.json"

#: Byte cap on grep/output reads — a multi-GB artifact file can't be
#: slurped into api memory; the pattern still sees the head of the file.
_MAX_READ_BYTES = 8 * 1024 * 1024

_MAX_DETAIL_LEN = 500

#: First signed number anywhere in the output (numeric_tol's measurement).
_NUM_RE = re.compile(r"[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?")

#: JSON Schema keys the subset validator understands — anything else is a
#: spec error at parse AND at measure time (strict beats silently ignored).
JSON_SCHEMA_KEYS = frozenset(
    {
        "type",
        "required",
        "properties",
        "items",
        "enum",
        "const",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "minLength",
        "maxLength",
        "pattern",
        "additionalProperties",
    }
)


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
    rel_str = str(rel).strip()
    p = PurePosixPath(rel_str.replace("\\", "/"))
    if not rel_str or p.is_absolute() or ".." in p.parts or "\x00" in rel_str:
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
    text = _output_text(artifact_dir)
    if text is None:
        return _outcome("output_grep", spec, "fail", "output.txt not found")
    m = rx.search(text)
    if m:
        return _outcome(
            "output_grep",
            spec,
            "pass",
            f"matched at line {_match_line(text, m.start())} of output.txt",
        )
    return _outcome(
        "output_grep", spec, "fail", f"no match for {pattern!r} in output.txt"
    )


def _output_text(artifact_dir: Path) -> str | None:
    """The prompt-cell output — ``output.txt`` at the ARTIFACT ROOT first
    (PR25 convention: prompt cells write it there, no workspace/ exists),
    the workspace copy as fallback so container cells that deliberately
    wrote output.txt still satisfy output checks. None when absent or
    unreadable."""
    for cand in (artifact_dir / "output.txt", artifact_dir / "workspace" / "output.txt"):
        resolved = _resolve_jailed(artifact_dir, cand.relative_to(artifact_dir))
        if resolved is None or not resolved.exists():
            continue
        return _read_text_bounded(resolved)
    return None


def _check_exact(
    spec: dict[str, Any], artifact_dir: Path, **_kw: Any
) -> CheckOutcome:
    expected = spec.get("equals")
    if not isinstance(expected, str):
        return _outcome("exact", spec, "error", "missing 'equals' in spec")
    text = _output_text(artifact_dir)
    if text is None:
        return _outcome("exact", spec, "fail", "output.txt not found")
    if text.strip() == expected.strip():
        return _outcome("exact", spec, "pass", "output.txt matches expected text")
    return _outcome(
        "exact",
        spec,
        "fail",
        f"output differs: got {text.strip()[:120]!r}, expected {expected.strip()[:120]!r}",
    )


def _check_contains(
    spec: dict[str, Any], artifact_dir: Path, **_kw: Any
) -> CheckOutcome:
    needle = spec.get("substring")
    if not isinstance(needle, str) or not needle:
        return _outcome("contains", spec, "error", "missing 'substring' in spec")
    text = _output_text(artifact_dir)
    if text is None:
        return _outcome("contains", spec, "fail", "output.txt not found")
    if needle in text:
        return _outcome(
            "contains",
            spec,
            "pass",
            f"found {needle[:80]!r} in output.txt",
        )
    return _outcome(
        "contains", spec, "fail", f"{needle[:80]!r} not found in output.txt"
    )


def _check_numeric_tol(
    spec: dict[str, Any], artifact_dir: Path, **_kw: Any
) -> CheckOutcome:
    try:
        value = float(spec.get("value"))
        tolerance = float(spec.get("tolerance", 0))
    except (TypeError, ValueError):
        return _outcome(
            "numeric_tol", spec, "error", "spec needs numeric value/tolerance"
        )
    if not (math.isfinite(value) and math.isfinite(tolerance)) or tolerance < 0:
        return _outcome(
            "numeric_tol", spec, "error", "value/tolerance must be finite, tolerance >= 0"
        )
    text = _output_text(artifact_dir)
    if text is None:
        return _outcome("numeric_tol", spec, "fail", "output.txt not found")
    m = _NUM_RE.search(text)
    if m is None:
        return _outcome(
            "numeric_tol", spec, "fail", "no number found in output.txt"
        )
    got = float(m.group(0))
    if abs(got - value) <= tolerance:
        return _outcome(
            "numeric_tol",
            spec,
            "pass",
            f"found {got:g} within ±{tolerance:g} of {value:g}",
        )
    return _outcome(
        "numeric_tol",
        spec,
        "fail",
        f"found {got:g}, outside ±{tolerance:g} of {value:g}",
    )


def _check_json_schema(
    spec: dict[str, Any], artifact_dir: Path, **_kw: Any
) -> CheckOutcome:
    schema = spec.get("schema")
    if not isinstance(schema, dict):
        return _outcome(
            "json_schema", spec, "error", "missing 'schema' mapping in spec"
        )
    unsupported = sorted(set(schema) - JSON_SCHEMA_KEYS)
    if unsupported:
        return _outcome(
            "json_schema",
            spec,
            "error",
            f"unsupported schema keys {unsupported} — supported: {sorted(JSON_SCHEMA_KEYS)}",
        )
    text = _output_text(artifact_dir)
    if text is None:
        return _outcome("json_schema", spec, "fail", "output.txt not found")
    try:
        instance = json.loads(text)
    except json.JSONDecodeError as exc:
        return _outcome(
            "json_schema", spec, "fail", f"output.txt is not valid JSON: {exc}"
        )
    problem = _schema_violation(instance, schema)
    if problem is None:
        return _outcome("json_schema", spec, "pass", "output validates against schema")
    return _outcome("json_schema", spec, "fail", problem)


def _schema_violation(instance: Any, schema: dict[str, Any], path: str = "$") -> str | None:
    """Subset JSON-Schema validator — returns a human-readable violation or
    None (valid). Supported keys are ``JSON_SCHEMA_KEYS``; nested schemas in
    ``properties``/``items``/``additionalProperties`` validate recursively.
    Suite files are repo-vendored, so strictness on unknown keys is a spec
    bug the parse layer already rejects — this assumes a clean schema."""
    expected = schema.get("type")
    if expected is not None and not _type_matches(instance, expected):
        return f"{path}: expected type {expected!r}, got {_json_type(instance)}"
    if "const" in schema and instance != schema["const"]:
        return f"{path}: expected const {schema['const']!r}, got {instance!r}"
    if "enum" in schema and instance not in schema["enum"]:
        return f"{path}: {instance!r} not in enum {schema['enum']!r}"
    if isinstance(instance, (int, float)) and not isinstance(instance, bool):
        for key, op in (
            ("minimum", lambda v, b: v >= b),
            ("maximum", lambda v, b: v <= b),
            ("exclusiveMinimum", lambda v, b: v > b),
            ("exclusiveMaximum", lambda v, b: v < b),
        ):
            bound = schema.get(key)
            if bound is not None and not op(instance, bound):
                return f"{path}: {instance!r} violates {key}={bound!r}"
    if isinstance(instance, str):
        if "minLength" in schema and len(instance) < schema["minLength"]:
            return f"{path}: string shorter than minLength={schema['minLength']}"
        if "maxLength" in schema and len(instance) > schema["maxLength"]:
            return f"{path}: string longer than maxLength={schema['maxLength']}"
        if "pattern" in schema and re.search(schema["pattern"], instance) is None:
            return f"{path}: {instance[:80]!r} fails pattern {schema['pattern']!r}"
    if isinstance(instance, dict):
        for req in schema.get("required") or []:
            if req not in instance:
                return f"{path}: missing required key {req!r}"
        props = schema.get("properties") or {}
        for key, sub in props.items():
            if key in instance and isinstance(sub, dict):
                problem = _schema_violation(instance[key], sub, f"{path}.{key}")
                if problem:
                    return problem
        additional = schema.get("additionalProperties", True)
        extras = [k for k in instance if k not in props]
        if additional is False and extras:
            return f"{path}: unexpected keys {extras!r}"
        if isinstance(additional, dict):
            for key in extras:
                problem = _schema_violation(instance[key], additional, f"{path}.{key}")
                if problem:
                    return problem
    if isinstance(instance, list):
        items = schema.get("items")
        if isinstance(items, dict):
            for i, elt in enumerate(instance):
                problem = _schema_violation(elt, items, f"{path}[{i}]")
                if problem:
                    return problem
    return None


def _json_type(instance: Any) -> str:
    if instance is None:
        return "null"
    if isinstance(instance, bool):
        return "boolean"
    if isinstance(instance, int):
        return "integer"
    if isinstance(instance, float):
        return "number"
    if isinstance(instance, str):
        return "string"
    if isinstance(instance, list):
        return "array"
    return "object"


def _type_matches(instance: Any, expected: Any) -> bool:
    if isinstance(expected, list):
        return any(_type_matches(instance, e) for e in expected)
    if expected == "number":
        return isinstance(instance, (int, float)) and not isinstance(instance, bool)
    if expected == "integer":
        return isinstance(instance, int) and not isinstance(instance, bool)
    return _json_type(instance) == expected


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
    result = outcome.result or {}
    phases = result.get("phases") or {}
    tests = phases.get("tests") or {}
    version = result.get("version")
    if not tests:
        detail = "exec image did not report a pytest phase for the supplied test target"
        if not isinstance(version, int) or version < 3:
            detail = f"exec image {outcome.image!r} did not report a pytest phase; rebuild the exec image"
        return _outcome(
            "py_test",
            spec,
            "error",
            detail,
        )
    summary = {
        "collected": int(tests.get("collected") or 0),
        "passed": int(tests.get("passed") or 0),
        "failed": int(tests.get("failed") or 0),
        "errors": int(tests.get("errors") or 0),
        "skipped": int(tests.get("skipped") or 0),
        "duration_seconds": tests.get("duration_seconds"),
        "ok": bool(tests.get("ok")),
    }
    if not tests.get("detected"):
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
    "exact": _check_exact,
    "contains": _check_contains,
    "json_schema": _check_json_schema,
    "numeric_tol": _check_numeric_tol,
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


# ---------------------------------------------------------------------------
# roll-ups — shared by the lifecycle (score row / artifact file) and the
# evaluator's ``suite`` signal, so all surfaces read the same numbers
# ---------------------------------------------------------------------------


def summarize(outcomes: Iterable[CheckOutcome]) -> dict[str, Any]:
    """Counts + pass rate over one set of outcomes.

    ``pass_rate`` is ``passed / measured`` where measured = pass + fail —
    ``skipped`` (couldn't measure) and ``error`` (check itself broke) are
    excluded so a red check and an unmeasurable check aren't conflated.
    """
    counts = {"pass": 0, "fail": 0, "skipped": 0, "error": 0}
    total = 0
    for o in outcomes:
        counts[o.status] = counts.get(o.status, 0) + 1
        total += 1
    measured = counts["pass"] + counts["fail"]
    return {
        "total": total,
        "passed": counts["pass"],
        "failed": counts["fail"],
        "skipped": counts["skipped"],
        "errors": counts["error"],
        "measured": measured,
        "pass_rate": (counts["pass"] / measured) if measured else None,
    }


def suite_checks_doc(
    outcomes: list[CheckOutcome], *, suite_ref: str, suite_index: int
) -> dict[str, Any]:
    """The ``suite_checks.json`` artifact document — provenance + per-check
    results + the roll-up. The evaluator folds this into ``analysis.json``
    as the ``suite`` signal without re-measuring."""
    return {
        "suite_ref": suite_ref,
        "suite_index": suite_index,
        "checks": [
            {"check": o.check_type, "spec": o.spec, "status": o.status, "detail": o.detail}
            for o in outcomes
        ],
        **summarize(outcomes),
    }


def signal_metrics(doc: dict[str, Any]) -> dict[str, Any]:
    """``suite`` signal metrics for ``analysis.json`` — the same numbers the
    artifact document carries, flattened into the SignalReport envelope."""
    return {
        "suite_ref": doc.get("suite_ref"),
        "suite_index": doc.get("suite_index"),
        "total": doc.get("total", 0),
        "passed": doc.get("passed", 0),
        "failed": doc.get("failed", 0),
        "skipped": doc.get("skipped", 0),
        "errors": doc.get("errors", 0),
        "measured": doc.get("measured", 0),
        "pass_rate": doc.get("pass_rate"),
        "checks": doc.get("checks") or [],
    }
