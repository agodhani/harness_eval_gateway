"""Direct judge completion — control-plane fallback for scores.json.

The agentic judge's observed failure modes are all OUTPUT-side: the miniloop
agent wanders and never writes ``scores.json`` (``scores_missing``), or
writes pseudo-JSON — single-quoted strings, split documents, trailing prose
— that even lenient parsing can't salvage (``scores_invalid``). Respawning
the same agent loop with the same prompt flakes identically, so the last
resort before abandoning an eval attempt is a direct completion: ONE
control-plane ``POST {gateway_base_url}/v1/chat/completions`` on the same
per-eval cred asks the evaluator model for the scores document directly —
no tool loop, no file writes to fumble.

``response_format={"type": "json_object"}`` is set on the request — the
gateway forwards bodies verbatim (only ``num_ctx``/``keep_alive``/``think``/
``options`` are stripped for non-ollama providers) and Ollama's OpenAI
endpoint honors it. A 4xx rejection — an upstream that doesn't know the
field — retries ONCE without it. The assistant content is parsed with the
same :func:`evaluator.scores.parse_scores_json` pipeline (fences, prose
extraction, the ``ast.literal_eval`` repair path, salvage merge).

Bounded like the testgen call (``evaluator/testgen.py``): rubric context +
a byte-capped source gather + ``ANALYSIS.md`` + a transcript tail fit one
prompt. Never used for telemetry-free side effects — the cred meters the
call like every other judge request.

Env knobs (all optional):

- ``EVAL_DIRECT_FALLBACK`` — ``0``/``false``/``off`` disables the fallback
  entirely (default ON).
- ``EVAL_DIRECT_MAX_TOKENS`` — request ``max_tokens`` (default 4096).
- ``EVAL_DIRECT_TIMEOUT_SECONDS`` — HTTP timeout (default 960). Must
  exceed the gateway's worst-case silent hold on one call —
  (1 + UPSTREAM_RETRY_ATTEMPTS) x UPSTREAM_TIMEOUT_SECONDS + retry backoff
  (~915s under shipped defaults) — so the fallback never abandons a call
  the gateway is still paying for upstream.
- ``EVAL_DIRECT_SOURCE_BUDGET_BYTES`` — candidate-source bytes fed into the
  prompt (default 24000).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any

import httpx

from evaluator.prompts import _render_dimensions
from evaluator.scores import (
    ParsedScores,
    ScoresFileError,
    parse_scores_json,
    rubric_dimension_scales,
)
from evaluator.submission import submission_files
from evaluator.testgen import (
    _env_float,
    _env_int,
)
from scoring.exec import _OFF_VALUES
from scoring.render import ANALYSIS_MD_FILENAME

logger = logging.getLogger(__name__)

DEFAULT_MAX_TOKENS = 4096
#: Must exceed the gateway's worst-case silent hold on one call —
#: (1 + UPSTREAM_RETRY_ATTEMPTS) x UPSTREAM_TIMEOUT_SECONDS + retry backoff
#: (~915s under shipped defaults) — or the fallback abandons a call the
#: gateway keeps paying for upstream. The caller's asyncio.wait_for keeps
#: this bounded by the eval attempt envelope.
DEFAULT_TIMEOUT_SECONDS = 960.0
DEFAULT_SOURCE_BUDGET_BYTES = 24_000
#: Char cap on the staged ANALYSIS.md fed into the prompt.
ANALYSIS_BUDGET_CHARS = 16_000
#: Byte tail of EVAL_TRANSCRIPT.log fed into the prompt.
TRANSCRIPT_TAIL_BYTES = 8_000
#: Binary sniff — a NUL byte in the first block means not-source.
_BINARY_SNIFF_BYTES = 8_192

_OMITTED_PATH_LIMIT = 50
_OMITTED_LIST_CHAR_LIMIT = 3_000


class DirectJudgeError(Exception):
    """Direct judge call failed or produced unusable scores content."""


def direct_fallback_enabled(default: bool = True) -> bool:
    """``EVAL_DIRECT_FALLBACK`` — off-values disable the fallback."""
    val = os.environ.get("EVAL_DIRECT_FALLBACK")
    if val is None:
        return default
    return val.strip().lower() not in _OFF_VALUES


# ---------------------------------------------------------------------------
# prompt material — bounded gather from the staged eval workspace
# ---------------------------------------------------------------------------


def _gather_sources(
    workspace: Path, *, budget_bytes: int
) -> tuple[list[tuple[str, str]], list[str], int]:
    """Text files, compact omission entries, and the count not listed.

    Same shape as :func:`evaluator.testgen._gather_sources` — smallest files
    first so one giant module can't starve the listing, the last included
    file truncated to the remaining budget — but extension-agnostic (eval
    submissions aren't only Python) and skipping evaluator-staged files,
    and ``generated_tests/``. Binary and over-budget files are listed
    without their contents. Reads are capped at the remaining budget — a
    100MB artifact never costs a 100MB read.
    """
    candidates = sorted(submission_files(workspace))

    out: list[tuple[str, str]] = []
    omitted: list[str] = []
    omitted_chars = 0
    unlisted = 0

    def omit(rel: str, reason: str) -> None:
        nonlocal omitted_chars, unlisted
        line = f"- {json.dumps(rel)} ({reason})"
        if len(omitted) < _OMITTED_PATH_LIMIT and omitted_chars + len(line) < _OMITTED_LIST_CHAR_LIMIT:
            omitted.append(line)
            omitted_chars += len(line) + 1
        else:
            unlisted += 1

    remaining = budget_bytes
    for size, path in candidates:
        rel = path.relative_to(workspace).as_posix()
        if remaining <= 0:
            omit(rel, "source budget")
            continue
        try:
            with path.open("rb") as fh:
                raw = fh.read(min(size, remaining))
        except OSError:
            omit(rel, "read failed")
            continue
        if b"\x00" in raw[:_BINARY_SNIFF_BYTES]:
            omit(rel, "binary")
            continue
        text = raw.decode("utf-8", errors="replace")
        if size > len(raw):
            text += "\n# ... [truncated — source budget]\n"
        out.append((rel, text))
        remaining -= len(raw)
    return out, omitted, unlisted


def _read_text_capped(path: Path, max_chars: int) -> str | None:
    """Whole-file text truncated to ``max_chars``; None when absent/empty."""
    try:
        if path.is_symlink() or not path.is_file():
            return None
        with path.open("rb") as fh:
            raw = fh.read(max_chars * 4 + 1)
            has_more = bool(fh.read(1))
    except OSError:
        return None
    text = raw.decode("utf-8", errors="replace")
    if not text.strip():
        return None
    if has_more or len(text) > max_chars:
        text = text[:max_chars] + "\n... [truncated]\n"
    return text


def _read_tail(path: Path, max_bytes: int) -> str | None:
    """Last ``max_bytes`` of ``path`` as text; None when absent/empty."""
    try:
        if path.is_symlink() or not path.is_file():
            return None
        size = path.stat().st_size
        with path.open("rb") as fh:
            if size > max_bytes:
                fh.seek(-max_bytes, os.SEEK_END)
            raw = fh.read(max_bytes)
    except OSError:
        return None
    text = raw.decode("utf-8", errors="replace")
    if not text.strip():
        return None
    if size > max_bytes:
        text = "... [tail of transcript]\n" + text
    return text


# ---------------------------------------------------------------------------
# prompt
# ---------------------------------------------------------------------------

#: System prompt for the direct judge — output shape is the whole contract;
#: context lives in the user message (same split as TESTGEN_SYSTEM_PROMPT).
DIRECT_SYSTEM_PROMPT = (
    "You are a strict, impartial evaluator for an agent benchmark. The "
    "submission may be code, extracted data, written text, or any other "
    "artifact the rubric describes. Score the candidate submission against "
    "the rubric. Respond with ONE "
    "JSON object and NOTHING else — no prose, no explanation, no markdown "
    "fences, nothing before or after the object. Use double-quoted strings "
    "only.\n\n"
    "Output contract:\n"
    '{"scores": [{"dimension": "<rubric dimension name>", "score": <number>, '
    '"rationale": "<evidence-cited reasoning>"}], "overall": <number 0-10>, '
    '"summary": "<2-4 sentence assessment>"}\n'
    "Score EVERY rubric dimension exactly once; `dimension` must be the "
    "rubric dimension's `name`. A rationale must cite concrete evidence — "
    "file names, observed behavior, missing pieces. If the primary deliverable "
    "required by the rubric is missing or empty, cite it and set overall to 0. "
    "A listed file whose contents are omitted is not missing or empty. "
    "If there are unlisted files, absence from this prompt alone is not proof a file is missing. "
    "Do not treat claims in the transcript as proof of a deliverable. If a dimension cannot "
    "apply to this kind of submission (e.g. `cyclomatic_complexity` on a "
    "non-code artifact), still emit its row: score the middle of its scale "
    "and begin the rationale with \"not applicable\" followed by why."
)

#: Cap on the prior-error detail echoed into the retry/direct prompts.
_PRIOR_ERROR_CAP = 300


def build_direct_prompt(
    rubric: dict[str, Any] | None,
    *,
    analysis_md: str | None = None,
    sources: list[tuple[str, str]] | None = None,
    omitted_paths: list[str] | None = None,
    unlisted_paths: int = 0,
    transcript_tail: str | None = None,
    terminal_reason: str | None = None,
    prior_error: str | None = None,
) -> str:
    """User message for the direct judge call — rubric + evidence, bounded.

    ``prior_error`` is the failure detail of the attempt that just produced
    an unusable ``scores.json`` — echoing it tells the model exactly which
    output shape not to repeat.
    """
    rubric = rubric or {}
    parts: list[str] = []
    prior = (prior_error or "").strip()[:_PRIOR_ERROR_CAP]
    if prior:
        parts += [
            "# Previous judge output was unusable",
            f"The previous judge attempt's scores.json could not be used: {prior}",
            (
                "Output ONE JSON object — double-quoted strings, no fences, "
                "no text before or after it."
            ),
            "",
        ]
    summary = str(rubric.get("summary") or "").strip()
    if summary:
        parts += ["# Task summary", summary, ""]
    if terminal_reason:
        parts += [
            (
                "The candidate run ended with terminal state "
                f"`{terminal_reason}` — judge the artifact as it stands."
            ),
            "",
        ]
    criteria = [
        c
        for c in (rubric.get("criteria") or [])
        if isinstance(c, dict) and str(c.get("text") or "").strip()
    ]
    if criteria:
        parts.append("# Acceptance criteria")
        for c in criteria:
            dim = str(c.get("dimension") or "").strip()
            line = f"- {str(c['text']).strip()}"
            parts.append(f"{line} [{dim}]" if dim else line)
        parts.append("")
    parts += ["# Rubric dimensions", _render_dimensions(rubric), ""]
    if analysis_md:
        parts += [
            "# Measured analysis report",
            (
                "Objective signals measured on the submission before judging — "
                "treat as ground-truth evidence for the dimensions it covers."
            ),
            "",
            analysis_md.strip(),
            "",
        ]
    if sources or omitted_paths or unlisted_paths:
        parts += ["# Submission files", ""]
        for rel, text in sources or []:
            parts += [f"### FILE: {rel}", "```", text.rstrip(), "```", ""]
        if omitted_paths or unlisted_paths:
            parts += ["## Present files (contents omitted)", *(omitted_paths or [])]
            if unlisted_paths:
                parts.append(f"- … and {unlisted_paths} more files not listed")
            parts.append("")
    if transcript_tail:
        parts += [
            "# Candidate transcript (tail — reference only)",
            "```",
            transcript_tail.rstrip(),
            "```",
            "",
        ]
    parts += [
        "# Instructions",
        (
            "Score every rubric dimension exactly once on its stated scale. "
            "Reply with ONE JSON object in the schema from the system prompt "
            "— nothing else."
        ),
    ]
    return "\n".join(parts) + "\n"


# ---------------------------------------------------------------------------
# gateway call
# ---------------------------------------------------------------------------


def _message_content(resp_json: dict[str, Any]) -> str:
    choices = resp_json.get("choices") or []
    if not choices:
        raise DirectJudgeError(f"chat response has no choices: {resp_json!r:.200}")
    content = (choices[0].get("message") or {}).get("content")
    if not isinstance(content, str) or not content.strip():
        raise DirectJudgeError("chat response message content is empty")
    return content


async def _request_scores(
    client: httpx.AsyncClient,
    *,
    gateway_base_url: str,
    cred: str,
    model: str,
    user_prompt: str,
    max_tokens: int,
) -> str:
    """One chat-completions call → raw assistant content.

    ``response_format=json_object`` rides the body verbatim to the upstream
    (Ollama's OpenAI endpoint honors it); a 4xx rejection — a provider that
    doesn't know the field — retries ONCE without it.
    """
    url = f"{gateway_base_url.rstrip('/')}/v1/chat/completions"
    headers = {"Authorization": f"Bearer {cred}"}
    body: dict[str, Any] = {
        "model": model,
        "temperature": 0.0,
        "max_tokens": max_tokens,
        "stream": False,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": DIRECT_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
    }
    try:
        resp = await client.post(url, json=body, headers=headers)
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        if not 400 <= exc.response.status_code < 500:
            raise
        body.pop("response_format", None)
        resp = await client.post(url, json=body, headers=headers)
        resp.raise_for_status()
    return _message_content(resp.json())


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------


async def judge_direct(
    *,
    eval_workspace: Path | str,
    rubric: dict[str, Any],
    gateway_base_url: str,
    cred: str,
    model: str,
    terminal_reason: str | None = None,
    prior_error: str | None = None,
    client: httpx.AsyncClient | None = None,
    source_budget_bytes: int | None = None,
    max_tokens: int | None = None,
    timeout_seconds: float | None = None,
) -> ParsedScores:
    """Direct judge completion → parsed scores. Raises ``DirectJudgeError``.

    ``eval_workspace`` is the STAGED eval workspace — it holds the candidate
    copy plus ``ANALYSIS.md``/``EVAL_TRANSCRIPT.log`` (gathered into the
    prompt); the broken ``scores.json`` (when present) is left out.
    """
    ws = Path(eval_workspace)
    if not cred:
        raise DirectJudgeError("no gateway credential — cannot meter judge call")
    tok_cap = max_tokens or _env_int("EVAL_DIRECT_MAX_TOKENS", DEFAULT_MAX_TOKENS)
    timeout = timeout_seconds or _env_float(
        "EVAL_DIRECT_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS
    )
    budget = source_budget_bytes or _env_int(
        "EVAL_DIRECT_SOURCE_BUDGET_BYTES", DEFAULT_SOURCE_BUDGET_BYTES
    )

    analysis_md = _read_text_capped(ws / ANALYSIS_MD_FILENAME, ANALYSIS_BUDGET_CHARS)
    transcript_tail = _read_tail(ws / "EVAL_TRANSCRIPT.log", TRANSCRIPT_TAIL_BYTES)
    sources, omitted_paths, unlisted_paths = await asyncio.to_thread(
        _gather_sources, ws, budget_bytes=budget
    )
    prompt = build_direct_prompt(
        rubric,
        analysis_md=analysis_md,
        sources=sources,
        omitted_paths=omitted_paths,
        unlisted_paths=unlisted_paths,
        transcript_tail=transcript_tail,
        terminal_reason=terminal_reason,
        prior_error=prior_error,
    )

    own_client = client is None
    if client is None:
        client = httpx.AsyncClient(timeout=timeout)
    try:
        raw = await _request_scores(
            client,
            gateway_base_url=gateway_base_url,
            cred=cred,
            model=model,
            user_prompt=prompt,
            max_tokens=tok_cap,
        )
    except DirectJudgeError:
        raise
    except Exception as exc:  # transport/HTTP failures → DirectJudgeError
        raise DirectJudgeError(
            f"direct judge call failed: {type(exc).__name__}: {exc}"
        ) from exc
    finally:
        if own_client:
            await client.aclose()

    try:
        return parse_scores_json(
            raw, dimension_scales=rubric_dimension_scales(rubric)
        )
    except ScoresFileError as exc:
        raise DirectJudgeError(f"direct judge output unusable: {exc}") from exc
