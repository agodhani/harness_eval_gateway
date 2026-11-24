"""scores.json contract — parsing and validation for evaluator output (F7).

The eval sandbox's miniloop agent is instructed (see ``prompts.py``) to write
``/workspace/scores.json`` — which lands on the host at
``<eval_workspace>/scores.json`` because the workspace is a bind mount.

Canonical schema::

    {
      "scores": [
        {"dimension": "functional_requirements", "score": 7.5,
         "rationale": "evidence-cited reasoning ..."},
        ...
      ],
      "overall": 7.0,          # optional holistic 0-10
      "summary": "..."         # optional 2-4 sentence assessment
    }

Parsing is deliberately tolerant — a 3-8B judge model will occasionally wrap
the JSON in markdown fences, add prose, or emit single-quoted Python
literals. ``parse_scores_json`` strips fences, falls back to
``ast.literal_eval`` for pseudo-JSON dicts,
extracts the outermost object, validates each entry individually, and reports
per-entry problems in ``ParsedScores.warnings`` rather than failing the whole
file. Only a structurally unusable file (not JSON, no ``scores`` list, zero
valid entries) raises :class:`ScoresFileError`.
"""

from __future__ import annotations

import ast
import json
import math
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: Filename the eval agent must write at the workspace root.
SCORES_FILENAME = "scores.json"

#: Pseudo-dimension used for the holistic ``overall`` score. Stored as a
#: regular Score row so it is queryable; the results API surfaces it as
#: ``overall`` rather than as a rubric dimension.
OVERALL_DIMENSION = "overall"

#: Hard column limit on ``scores.dimension`` (db String(128)).
MAX_DIMENSION_LEN = 128
#: Sanity cap on rationale text (db Text is unbounded; keep payloads sane).
MAX_RATIONALE_LEN = 10_000
#: Byte cap on the scores document itself — the file is written by the
#: judge sandbox, so the host-side read stays bounded (a real scores.json
#: is a few KB; 1 MiB is far past legitimate).
SCORES_FILE_MAX_BYTES = 1_048_576

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


class ScoresFileError(Exception):
    """scores.json is missing, unparseable, or contains no usable entries."""


@dataclass
class ScoreEntry:
    """One validated per-dimension score."""

    dimension: str
    score: float
    rationale: str = ""


@dataclass
class ParsedScores:
    """Result of parsing scores.json.

    ``entries`` are the valid per-dimension scores (invalid entries are
    dropped and described in ``warnings``). ``overall``/``summary`` come from
    the top-level fields when present and valid; when ``overall`` is missing
    or unusable but entries exist, it is synthesized as the mean of the
    dimension scores (flagged in ``warnings``) so the results surface never
    shows a silent null.
    """

    entries: list[ScoreEntry] = field(default_factory=list)
    overall: float | None = None
    summary: str = ""
    warnings: list[str] = field(default_factory=list)


def _literal_eval(text: str) -> Any | None:
    """``ast.literal_eval`` repair path — dict/list results only.

    Small judges emit Python-literal pseudo-JSON (single-quoted strings —
    the observed ``scores_invalid`` mode) that ``json.loads`` rejects but
    ``literal_eval`` parses exactly. Scalars (``True``, ``5``, a bare
    string) are rejected: they can never be a scores document.
    """
    try:
        obj = ast.literal_eval(text)
    except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
        return None
    return obj if isinstance(obj, (dict, list)) else None


def _loads_lenient(text: str) -> Any | None:
    """Best-effort JSON load; returns None instead of raising.

    Tries the whole document, then a ``` fence strip, then the outermost
    ``{...}`` block — covering the common small-model failure modes. When
    strict JSON fails on every candidate the same candidates go through
    :func:`_literal_eval` (single-quoted Python literals).
    """
    candidate = text.strip()
    if candidate.startswith("```"):
        candidate = _FENCE_RE.sub("", candidate).strip()
    candidates = [candidate]
    start = candidate.find("{")
    end = candidate.rfind("}")
    if start != -1 and end > start:
        candidates.append(candidate[start : end + 1])
    for attempt in candidates:
        try:
            return json.loads(attempt)
        except json.JSONDecodeError:
            pass
    for attempt in candidates:
        obj = _literal_eval(attempt)
        if obj is not None:
            return obj
    return None


def _extract_json_values(text: str) -> list[Any]:
    """Pull every top-level JSON value out of ``text`` via raw_decode.

    Salvages the observed small-model failure mode: the agent writes a bare
    ``[{...}, {...}]`` scores array followed by a separate (possibly
    truncated) ``{"overall": ..., "summary": ...}`` object — two JSON values
    in one file. Concatenated/prose-wrapped values are all recovered.
    """
    decoder = json.JSONDecoder()
    values: list[Any] = []
    idx = 0
    while idx < len(text):
        match = re.search(r"[\{\[]", text[idx:])
        if match is None:
            break
        start = idx + match.start()
        try:
            obj, end = decoder.raw_decode(text[start:])
        except json.JSONDecodeError:
            idx = start + 1
            continue
        values.append(obj)
        idx = start + end
    return values


def _merge_json_values(values: list[Any]) -> dict[str, Any] | None:
    """Recombine salvaged JSON values into a canonical scores document.

    Priority: a dict with a ``scores`` list > a bare list of entries > loose
    dicts that look like score entries. ``overall``/``summary`` are picked up
    from any sibling dict that carries them.
    """
    scores_payload: list[Any] | None = None
    overall: Any = None
    summary: Any = None
    loose_entries: list[dict[str, Any]] = []

    for v in values:
        if isinstance(v, dict):
            if isinstance(v.get("scores"), list) and scores_payload is None:
                scores_payload = v["scores"]
            if "overall" in v and overall is None:
                overall = v.get("overall")
            if "summary" in v and summary is None:
                summary = v.get("summary")
            if "dimension" in v or "name" in v:
                loose_entries.append(v)
        elif isinstance(v, list) and scores_payload is None:
            scores_payload = v

    if scores_payload is None and loose_entries:
        scores_payload = loose_entries
    if scores_payload is None:
        return None

    merged: dict[str, Any] = {"scores": scores_payload}
    if overall is not None:
        merged["overall"] = overall
    if summary is not None:
        merged["summary"] = summary
    return merged


def _as_float(value: Any) -> float | None:
    """Coerce to a finite float; rejects bools, strings, NaN/inf."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    f = float(value)
    return f if math.isfinite(f) else None


def _clean_dimension(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    dim = value.strip()
    if not dim:
        return None
    return dim[:MAX_DIMENSION_LEN]


_DIM_NAME_FOLD_RE = re.compile(r"[^a-z0-9]+")


def _fold_dimension_name(name: str) -> str:
    """snake_case fold matching the rubric's dimension-name normalization —
    judge-emitted names fold the same way for scale lookup."""
    return _DIM_NAME_FOLD_RE.sub("_", name.strip().lower()).strip("_")


def rubric_dimension_scales(
    rubric: dict[str, Any] | None,
) -> dict[str, tuple[float, float, float]]:
    """``{dimension: (min_score, max_score, weight)}`` from a frozen rubric.

    Feeds the synthesized-overall fallback: a judge score is normalized
    onto the 0-10 headline scale via its dimension's declared range.
    Dimensions with missing/non-numeric scale fields take the 0-10 /
    weight-1 defaults the generation schema declares.
    """
    out: dict[str, tuple[float, float, float]] = {}
    if not isinstance(rubric, dict):
        return out
    dims = rubric.get("dimensions")
    if not isinstance(dims, list):
        return out
    for dim in dims:
        if not isinstance(dim, dict):
            continue
        name = dim.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        lo = _as_float(dim.get("min_score"))
        hi = _as_float(dim.get("max_score"))
        weight = _as_float(dim.get("weight"))
        out[_fold_dimension_name(name)] = (
            lo if lo is not None else 0.0,
            hi if hi is not None else 10.0,
            weight if weight is not None else 1.0,
        )
    return out


def _synthesized_overall(
    entries: list[ScoreEntry],
    scales: dict[str, tuple[float, float, float]] | None,
) -> float:
    """Fallback ``overall`` — always on the 0-10 headline scale.

    With rubric scales each entry contributes its normalized position on
    the declared dimension range, weighted; an entry whose dimension has
    no usable scale (unknown name, degenerate min >= max) counts at its
    raw score — the historical plain mean when no scales are passed. An
    off-scale judge score is clamped to the dimension range so it can't
    push the headline outside 0-10.
    """
    mean = round(min(10.0, max(0.0, sum(e.score for e in entries) / len(entries))), 1)
    if not scales:
        return mean
    weighted = 0.0
    total_weight = 0.0
    for entry in entries:
        spec = scales.get(_fold_dimension_name(entry.dimension))
        lo, hi, weight = spec if spec is not None else (0.0, 10.0, 1.0)
        if not math.isfinite(weight) or weight < 0:
            weight = 1.0
        if math.isfinite(lo) and math.isfinite(hi) and hi > lo:
            pos = (entry.score - lo) / (hi - lo)
        else:
            pos = entry.score / 10.0
        weighted += weight * min(1.0, max(0.0, pos))
        total_weight += weight
    if total_weight <= 0:
        return mean
    return round(10.0 * weighted / total_weight, 1)


#: Keys excluded when a flat ``{dimension: score}`` dict is normalized —
#: ``overall``/``summary`` are schema fields, ``total`` is a sum on a
#: different scale than the per-dimension 0-10 scores.
_FLAT_MAP_SKIP_KEYS = frozenset({"scores", "overall", "summary", "total"})


def _flat_scores_entries(obj: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalize a flat ``{dimension: number}`` mapping into entry dicts.

    Small judge models sometimes emit ``{"functional_requirements": 8.0,
    "total": 34.0}`` instead of the ``scores`` list — the per-dimension
    values are valid scores, just unwrapped.
    """
    return [
        {"dimension": k, "score": v}
        for k, v in obj.items()
        if k not in _FLAT_MAP_SKIP_KEYS and _as_float(v) is not None
    ]


def parse_scores_json(
    text: str,
    *,
    dimension_scales: dict[str, tuple[float, float, float]] | None = None,
) -> ParsedScores:
    """Parse scores.json content into :class:`ParsedScores`.

    Raises :class:`ScoresFileError` when the document is not a JSON object,
    lacks a ``scores`` list, or yields zero valid entries. A missing or
    non-numeric ``overall`` is synthesized from the valid entries (with a
    warning) — a null headline score is never emitted for a usable
    document. ``dimension_scales`` (see :func:`rubric_dimension_scales`)
    gives each dimension's declared scale so the synthesized ``overall``
    stays a 0-10 score on custom-scale rubrics.
    """
    obj = _loads_lenient(text)
    if obj is None:
        # Salvage mode: multiple/truncated JSON values in one file.
        obj = _merge_json_values(_extract_json_values(text))
    if obj is None:
        raise ScoresFileError("scores.json is not valid JSON")
    if isinstance(obj, list):
        # A bare top-level array of entries is also acceptable.
        obj = {"scores": obj}
    if not isinstance(obj, dict):
        raise ScoresFileError("scores.json top level is not an object")

    raw_scores = obj.get("scores")
    if not isinstance(raw_scores, list):
        raw_scores = _flat_scores_entries(obj)
    if not raw_scores:
        raise ScoresFileError("scores.json has no 'scores' list")

    parsed = ParsedScores()
    seen: set[str] = set()
    for i, item in enumerate(raw_scores):
        if not isinstance(item, dict):
            parsed.warnings.append(f"scores[{i}] is not an object — dropped")
            continue
        dim = _clean_dimension(item.get("dimension") or item.get("name"))
        if dim is None:
            parsed.warnings.append(
                f"scores[{i}] has no usable 'dimension' — dropped"
            )
            continue
        score = _as_float(item.get("score"))
        if score is None:
            parsed.warnings.append(
                f"scores[{i}] ({dim!r}) has non-numeric 'score' — dropped"
            )
            continue
        rationale = item.get("rationale")
        if not isinstance(rationale, str):
            rationale = ""
        rationale = rationale.strip()[:MAX_RATIONALE_LEN]
        if dim in seen:
            parsed.warnings.append(
                f"duplicate dimension {dim!r} — keeping last entry"
            )
        seen.add(dim)
        # Replace any earlier entry with the same dimension (last wins).
        parsed.entries = [e for e in parsed.entries if e.dimension != dim]
        parsed.entries.append(
            ScoreEntry(dimension=dim, score=score, rationale=rationale)
        )

    if not parsed.entries:
        raise ScoresFileError("scores.json contains no valid score entries")

    overall = _as_float(obj.get("overall"))
    if overall is not None:
        parsed.overall = overall
    elif "overall" in obj:
        parsed.warnings.append("'overall' is not numeric — ignored")
    if parsed.overall is None:
        # A judge that scored every dimension but omitted `overall` must not
        # leave the field null — the results matrix reads it as the headline
        # score. Synthesize the dimension mean and flag it as derived so the
        # eval_scored event's warnings show where the number came from.
        parsed.overall = _synthesized_overall(parsed.entries, dimension_scales)
        parsed.warnings.append(
            "overall missing — computed as mean of dimension scores"
        )

    summary = obj.get("summary")
    if isinstance(summary, str):
        parsed.summary = summary.strip()[:MAX_RATIONALE_LEN]
    return parsed


def load_scores_file(
    path: Path,
    *,
    dimension_scales: dict[str, tuple[float, float, float]] | None = None,
) -> ParsedScores:
    """Read + parse ``path``; raises :class:`ScoresFileError` on any failure.

    ``path`` must be a regular file — the eval workspace is writable by the
    judge sandbox, so symlinks/FIFOs/devices are rejected before opening
    (``open()`` on a FIFO would block forever waiting for a writer).
    """
    try:
        p = Path(path)
        if not stat.S_ISREG(p.lstat().st_mode):
            raise ScoresFileError(f"{path} is not a regular file")
        with p.open("rb") as fh:
            raw = fh.read(SCORES_FILE_MAX_BYTES + 1)
    except OSError as exc:
        raise ScoresFileError(f"cannot read {path}: {exc}") from exc
    if len(raw) > SCORES_FILE_MAX_BYTES:
        raise ScoresFileError(f"{path} exceeds {SCORES_FILE_MAX_BYTES} bytes")
    return parse_scores_json(
        raw.decode("utf-8", errors="replace"), dimension_scales=dimension_scales
    )
