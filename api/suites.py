"""PR26 — vendored benchmark suite store.

A benchmark suite is a named batch of evaluation tasks committed under
``benchmark_suites/<slug>/`` — the suite-dir analogue of ``task_presets/<slug>.md``
(PR22). ``suite.yaml`` declares the suite id/version plus its ``tasks``
list; each task is either a ``preset_ref`` (resolved at submit like a
normal run submit — the preset body becomes the run's task_prompt and
``runs.preset_*`` snapshots the provenance) or an inline ``prompt``.

Each task may also declare:

- ``adapter`` — restrict the task to harness configs selecting that
  adapter (e.g. ``miniloop`` for a container cell, ``prompt`` for the
  PR25 prompt-only cell). Omit → the task runs on every submitted config.
- ``check`` — deterministic checkers the post-artifact step measures on
  the sandbox's artifact dir (``scoring.checkers``): ``file_exists``,
  ``grep``, ``py_test`` (locked ``heg-exec`` container), plus the
  ``output.txt`` output checkers ``output_grep``/``exact``/``contains``/
  ``json_schema``/``numeric_tol`` (prompt cells). They complement the LLM
  judge — never mutate scores or events (the ``suite_pass`` score row
  they produce is a measurement, not a judge override).

Unlike presets there is no user/DB side — suites are vendored only.

Path safety mirrors ``api/presets.py``: slugs validate against
``SLUG_RE`` BEFORE any filesystem touch, and every suite-relative path
(check targets, ``py_test`` dirs) must be a clean relative POSIX path —
no ``..``, no absolute roots — resolved + jailed under the suite dir.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path, PurePosixPath
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from api.presets import SLUG_RE

log = logging.getLogger(__name__)

SUITE_FILENAME = "suite.yaml"

#: Checker type names accepted under ``tasks[].check`` — the keys of the
#: normalized check spec dicts scoring.checkers consumes.
CHECK_TYPES = (
    "file_exists",
    "grep",
    "py_test",
    "output_grep",
    "exact",
    "contains",
    "json_schema",
    "numeric_tol",
)

#: JSON Schema keys the checkers' subset validator understands — parse
#: rejects unknown keys so a vendored schema bug fails the contract test
#: instead of silently passing at measure time (mirrors
#: scoring.checkers.JSON_SCHEMA_KEYS).
_JSON_SCHEMA_KEYS = frozenset(
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

_MAX_TASKS = 64
_MAX_PROMPT_LEN = 100_000


class SuiteError(Exception):
    """Typed store failure; ``kind`` maps to an HTTP status in the router.

    Kinds: ``invalid_slug``, ``invalid_body`` (suite.yaml shape problems —
    vendored files are committed, so these are repo bugs the contract test
    catches), ``not_found``.
    """

    def __init__(self, kind: str, detail: str) -> None:
        super().__init__(detail)
        self.kind = kind
        self.detail = detail


# ---------------------------------------------------------------------------
# path hygiene (artifact- and suite-relative paths share the same rules)
# ---------------------------------------------------------------------------


def _clean_relpath(value: str, *, what: str) -> str:
    """Validate ``value`` as a clean relative path; return it normalized.

    Rejects empties, absolute roots, drive-letter forms, NULs and any
    ``..`` component — the same contract every check/jail resolution
    downstream relies on. ``what`` names the field in error text.
    """
    text = (value or "").strip()
    p = PurePosixPath(text.replace("\\", "/")) if text else None
    if (
        not text
        or "\x00" in text
        or p is None
        or p.is_absolute()
        or any(part in ("..", "") for part in p.parts)
        or ":" in p.parts[0]  # windows drive form
    ):
        raise ValueError(
            f"{what} must be a relative path without '..' or absolute roots "
            f"(got {value!r})"
        )
    return str(p)


class GrepCheck(BaseModel):
    """``check.grep`` — ``pattern`` must match inside ``path``."""

    model_config = ConfigDict(extra="forbid")

    path: str
    pattern: str

    @field_validator("path")
    @classmethod
    def _path_ok(cls, v: str) -> str:
        return _clean_relpath(v, what="check.grep.path")

    @field_validator("pattern")
    @classmethod
    def _pattern_ok(cls, v: str) -> str:
        _compile_pattern(v, what="check.grep.pattern")
        return v


def _compile_pattern(pattern: str, *, what: str) -> None:
    try:
        re.compile(pattern)
    except re.error as exc:
        raise ValueError(f"{what} is not a valid regex ({pattern!r}): {exc}") from exc


def _validate_schema_node(node: Any, *, where: str) -> None:
    """Eagerly reject unsupported JSON-Schema keys inside a ``json_schema``
    check — recurses through properties/items/additionalProperties. The
    measure-time validator is a documented subset; a vendored suite using
    an unknown key is a repo bug, caught here (contract test) rather than
    surfacing as a checker ``error`` on the first run."""
    if not isinstance(node, dict):
        raise TypeError(f"{where} must be a schema mapping")
    bad = sorted(set(node) - _JSON_SCHEMA_KEYS)
    if bad:
        raise ValueError(
            f"{where}: unsupported schema keys {bad} — supported: {sorted(_JSON_SCHEMA_KEYS)}"
        )
    for key, sub in (node.get("properties") or {}).items():
        _validate_schema_node(sub, where=f"{where}.properties.{key}")
    if isinstance(node.get("items"), dict):
        _validate_schema_node(node["items"], where=f"{where}.items")
    if isinstance(node.get("additionalProperties"), dict):
        _validate_schema_node(
            node["additionalProperties"], where=f"{where}.additionalProperties"
        )


class NumericTolCheck(BaseModel):
    """``check.numeric_tol`` — first number in ``output.txt`` within
    ``tolerance`` of ``value``."""

    model_config = ConfigDict(extra="forbid")

    value: float
    tolerance: float = Field(default=0.0, ge=0.0)


class CheckSpec(BaseModel):
    """The ``check`` block on a suite task — one entry per checker type.

    Scalar forms: ``file_exists: <path>``, ``py_test: <suite-rel dir>``,
    ``output_grep: <pattern>``, ``exact: <expected>``,
    ``contains: <substring>``. Mapping forms: ``grep: {path, pattern}``,
    ``json_schema: {schema: {...}}`` (or the bare schema mapping —
    normalized to ``{"schema": ...}``), ``numeric_tol: {value,
    tolerance}``.
    """

    model_config = ConfigDict(extra="forbid")

    file_exists: str | None = None
    grep: GrepCheck | None = None
    py_test: str | None = None
    output_grep: str | None = None
    exact: str | None = None
    contains: str | None = None
    json_schema: dict[str, Any] | None = None
    numeric_tol: NumericTolCheck | None = None

    @field_validator("file_exists")
    @classmethod
    def _file_exists_ok(cls, v: str | None) -> str | None:
        return _clean_relpath(v, what="check.file_exists") if v is not None else None

    @field_validator("py_test")
    @classmethod
    def _py_test_ok(cls, v: str | None) -> str | None:
        return _clean_relpath(v, what="check.py_test") if v is not None else None

    @field_validator("output_grep")
    @classmethod
    def _output_grep_ok(cls, v: str | None) -> str | None:
        if v is not None:
            _compile_pattern(v, what="check.output_grep")
        return v

    @field_validator("json_schema")
    @classmethod
    def _json_schema_ok(cls, v: dict[str, Any] | None) -> dict[str, Any] | None:
        if v is None:
            return None
        # Bare schema mapping is sugar for {"schema": {...}} — either way
        # the checker sees spec["schema"].
        schema = v.get("schema") if set(v) == {"schema"} else v
        try:
            _validate_schema_node(schema, where="check.json_schema")
        except (TypeError, ValueError) as exc:
            # TypeError (non-mapping schema node) must still become a
            # ValidationError — pydantic only converts ValueError.
            raise ValueError(str(exc)) from exc
        return {"schema": schema}

    def entries(self) -> list[dict[str, Any]]:
        """Normalized flat check list — canonical type order, each dict
        ``{"check": <type>, ...spec}``; serializes straight into
        ``checker_results.spec`` and feeds ``scoring.checkers.run_checks``."""
        out: list[dict[str, Any]] = []
        if self.file_exists is not None:
            out.append({"check": "file_exists", "path": self.file_exists})
        if self.grep is not None:
            out.append(
                {"check": "grep", "path": self.grep.path, "pattern": self.grep.pattern}
            )
        if self.py_test is not None:
            out.append({"check": "py_test", "path": self.py_test})
        if self.output_grep is not None:
            out.append({"check": "output_grep", "pattern": self.output_grep})
        if self.exact is not None:
            out.append({"check": "exact", "equals": self.exact})
        if self.contains is not None:
            out.append({"check": "contains", "substring": self.contains})
        if self.json_schema is not None:
            out.append({"check": "json_schema", "schema": self.json_schema["schema"]})
        if self.numeric_tol is not None:
            out.append(
                {
                    "check": "numeric_tol",
                    "value": self.numeric_tol.value,
                    "tolerance": self.numeric_tol.tolerance,
                }
            )
        return out


class SuiteTask(BaseModel):
    """One suite task — ``preset_ref`` XOR ``prompt``; optional adapter pin
    + deterministic ``check`` specs."""

    model_config = ConfigDict(extra="forbid")

    id: str | None = Field(default=None, max_length=128)
    preset_ref: str | None = None
    prompt: str | None = Field(default=None, max_length=_MAX_PROMPT_LEN)
    adapter: str | None = Field(default=None, max_length=64)
    check: CheckSpec | None = None

    @field_validator("id", "adapter", mode="before")
    @classmethod
    def _normalize_optional(cls, v: object) -> object:
        if isinstance(v, str):
            v = " ".join(v.split())
            return v or None
        return v

    @model_validator(mode="after")
    def _task_ok(self) -> SuiteTask:
        has_ref = bool(self.preset_ref)
        has_prompt = bool(self.prompt and self.prompt.strip())
        if has_ref == has_prompt:
            raise ValueError(
                "each suite task needs exactly one of 'preset_ref' or 'prompt'"
            )
        if has_ref:
            ref = (self.preset_ref or "").strip().lower()
            if not SLUG_RE.match(ref):
                raise ValueError(
                    f"preset_ref {self.preset_ref!r} fails the slug pattern "
                    f"{SLUG_RE.pattern}"
                )
            self.preset_ref = ref
        if self.prompt is not None:
            self.prompt = self.prompt.strip()
        return self

    def task_id(self, index: int) -> str:
        """Stable per-task label: declared id → preset slug → ``task-<index>``."""
        return self.id or self.preset_ref or f"task-{index}"

    def checks(self) -> list[dict[str, Any]]:
        return self.check.entries() if self.check is not None else []


class SuiteDoc(BaseModel):
    """The parsed ``suite.yaml`` root."""

    model_config = ConfigDict(extra="forbid")

    id: str
    version: int = Field(ge=1)
    title: str
    description: str | None = None
    tasks: list[SuiteTask] = Field(min_length=1, max_length=_MAX_TASKS)

    @field_validator("id", "title")
    @classmethod
    def _nonempty(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("must be non-empty")
        return v


class Suite(BaseModel):
    """A resolved suite — the parsed doc plus its vendored location."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    slug: str
    version: int
    title: str
    description: str
    tasks: list[SuiteTask]
    #: Vendored suite directory — ``py_test`` check paths resolve under it.
    #: A Path (not a str) so checkers never re-derive the location.
    suite_dir: Path

    def task(self, index: int) -> SuiteTask | None:
        if 0 <= index < len(self.tasks):
            return self.tasks[index]
        return None


# ---------------------------------------------------------------------------
# parsing + lookup
# ---------------------------------------------------------------------------


def parse_suite_doc(data: Any, *, filename: str, slug: str) -> SuiteDoc:
    """Validate the yaml.safe_load output into a ``SuiteDoc``.

    ``id`` must equal ``slug`` (the directory name — same anti-drift rule
    as preset slug == filename stem).
    """
    if not isinstance(data, dict):
        raise SuiteError("invalid_body", f"{filename}: top level must be a mapping")
    try:
        doc = SuiteDoc.model_validate(data)
    except ValidationError as exc:
        raise SuiteError(
            "invalid_body", f"{filename}: invalid suite.yaml: {exc}"
        ) from exc
    if doc.id != slug:
        raise SuiteError(
            "invalid_body",
            f"{filename}: id {doc.id!r} must equal the suite directory name {slug!r}",
        )
    if not SLUG_RE.match(doc.id):
        raise SuiteError(
            "invalid_body", f"{filename}: id {doc.id!r} fails the slug pattern"
        )
    return doc


def parse_suite_yaml(text: str, *, filename: str, slug: str) -> SuiteDoc:
    """yaml.safe_load + doc validation. ``safe_load`` only — suite files
    are committed inputs, but arbitrary object construction is never on."""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise SuiteError("invalid_body", f"{filename}: invalid YAML: {exc}") from exc
    return parse_suite_doc(data, filename=filename, slug=slug)


def load_suite(suite_dir: str | Path, slug: str) -> Suite:
    """Parse ``<suite_dir>/suite.yaml`` into a ``Suite`` (dir already chosen)."""
    suite_dir = Path(suite_dir)
    path = suite_dir / SUITE_FILENAME
    doc = parse_suite_yaml(
        path.read_text(encoding="utf-8", errors="replace"),
        filename=f"{slug}/{SUITE_FILENAME}",
        slug=slug,
    )
    return Suite(
        slug=doc.id,
        version=doc.version,
        title=doc.title,
        description=(doc.description or "").strip(),
        tasks=doc.tasks,
        suite_dir=suite_dir,
    )


def list_suites(root: str | Path) -> list[Suite]:
    """Every ``<root>/<slug>/suite.yaml`` under the vendored root, slug-sorted.

    A dir whose yaml fails to parse is skipped with a warning — one bad
    suite must not take down the list (the contract test asserts every
    committed suite parses, so this is belt-and-suspenders for hand edits).
    """
    root = Path(root)
    out: list[Suite] = []
    if not root.is_dir():
        return out
    for child in sorted(root.iterdir()):
        if not child.is_dir() or not SLUG_RE.match(child.name):
            continue
        if not (child / SUITE_FILENAME).is_file():
            continue
        try:
            out.append(load_suite(child, child.name))
        except SuiteError as exc:
            log.warning("skipping unparseable suite %s: %s", child, exc.detail)
    return sorted(out, key=lambda s: s.slug)


def get_suite(root: str | Path, slug: str) -> Suite | None:
    """One suite by slug — direct dir lookup, no scan.

    The slug must match ``SLUG_RE`` BEFORE touching the filesystem —
    callers hand us unvalidated strings (URL params); a traversal-looking
    slug must never reach ``Path(root) / slug``. An unparseable suite.yaml
    logs + returns None (→ 404) so a hand-edited file can't 500 the API.
    """
    cleaned = (slug or "").strip().lower()
    if not SLUG_RE.match(cleaned):
        return None
    suite_dir = Path(root) / cleaned
    if not (suite_dir / SUITE_FILENAME).is_file():
        return None
    try:
        return load_suite(suite_dir, cleaned)
    except SuiteError:
        log.warning("vendored suite %s fails to parse", suite_dir, exc_info=True)
        return None


def require_suite(root: str | Path, slug: str) -> Suite:
    """``get_suite`` that raises — routers map kinds to HTTP statuses."""
    cleaned = (slug or "").strip().lower()
    if not SLUG_RE.match(cleaned):
        raise SuiteError(
            "invalid_slug",
            f"suite slug {slug!r} must match {SLUG_RE.pattern} "
            "(lowercase letters, digits and -; can't start or end with -)",
        )
    suite = get_suite(root, cleaned)
    if suite is None:
        raise SuiteError("not_found", f"suite {cleaned!r} not found")
    return suite
