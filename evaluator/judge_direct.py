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
    SCORES_FILENAME,
    ParsedScores,
    ScoresFileError,
    parse_scores_json,
    rubric_dimension_scales,
)
from evaluator.testgen import (
    GENERATED_TESTS_DIRNAME,
    _env_float,
    _env_int,
)
from scoring.exec import _OFF_VALUES
from scoring.render import ANALYSIS_JSON_FILENAME, ANALYSIS_MD_FILENAME
from scoring.static import SKIP_DIRS

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

#: Evaluator-staged files the source gather must skip — they enter the
#: prompt through their own sections (or are judge output, not candidate
#: code). ``generated_tests/`` joins SKIP_DIRS entirely.
_SOURCE_SKIP_DIRS = SKIP_DIRS | {GENERATED_TESTS_DIRNAME}
_SOURCE_SKIP_FILES = frozenset(
    {
        "rubric.json",
        SCORES_FILENAME,
        "EVAL_PROMPT.md",
        "EVAL_TRANSCRIPT.log",
        ANALYSIS_MD_FILENAME,
        ANALYSIS_JSON_FILENAME,
    }
)


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
) -> list[tuple[str, str]]:
    """``(relative path, text)`` for text files under ``workspace``.

    Same shape as :func:`evaluator.testgen._gather_sources` — smallest files
    first so one giant module can't starve the listing, the last included
    file truncated to the remaining budget — but extension-agnostic (eval
    submissions aren't only Python) and skipping evaluator-staged files,
    ``generated_tests/``, and binary-looking content (NUL byte in the first
    block). Reads are capped at the remaining budget — a 100MB artifact
    never costs a 100MB read.
    """
    candidates: list[tuple[int, Path]] = []
    for dirpath, dirnames, filenames in os.walk(workspace):
        dirnames[:] = [d for d in dirnames if d not in _SOURCE_SKIP_DIRS]
        for name in sorted(filenames):
            if name in _SOURCE_SKIP_FILES:
                continue
            path = Path(dirpath) / name
            try:
                if path.is_symlink() or not path.is_file():
                    continue
                size = path.stat().st_size
            except OSError:
                continue
            if size:
                candidates.append((size, path))
    candidates.sort(key=lambda t: (t[0], str(t[1])))

    out: list[tuple[str, str]] = []
    remaining = budget_bytes
    for size, path in candidates:
        if remaining <= 0:
            break
        rel = path.relative_to(workspace).as_posix()
        try:
            with path.open("rb") as fh:
                raw = fh.read(min(size, remaining))
        except OSError:
            continue
        if b"\x00" in raw[:_BINARY_SNIFF_BYTES]:
            continue
        text = raw.decode("utf-8", errors="replace")
        if size > len(raw):
            text += "\n# ... [truncated — source budget]\n"
        out.append((rel, text))
        remaining -= len(raw)
    return out


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
    "file names, observed behavior, missing pieces. If a dimension cannot "
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
    if sources:
        parts += ["# Submission files", ""]
        for rel, text in sources:
            parts += [f"### FILE: {rel}", "```", text.rstrip(), "```", ""]
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


_ERROR_DRAIN_BYTES = 64 * 1024
_ERROR_DRAIN_SECONDS = 2
_ERROR_PREVIEW_BYTES = 500
_POST_DONE_DRAIN_SECONDS = 5.0


async def _raise_for_status(response: httpx.Response) -> None:
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        detail = bytearray()
        drained = 0
        try:
            async with asyncio.timeout(_ERROR_DRAIN_SECONDS):
                async for chunk in exc.response.aiter_bytes():
                    if len(detail) < _ERROR_PREVIEW_BYTES:
                        detail.extend(chunk[: _ERROR_PREVIEW_BYTES - len(detail)])
                    drained += len(chunk)
                    if drained >= _ERROR_DRAIN_BYTES:
                        break
        except (httpx.RequestError, TimeoutError):
            pass
        preview = detail.decode("utf-8", errors="replace").strip()
        if preview:
            raise httpx.HTTPStatusError(
                f"{exc}: {preview}", request=exc.request, response=exc.response
            ) from exc
        raise


async def _chat_content(
    client: httpx.AsyncClient, url: str, body: dict[str, Any], headers: dict[str, str]
) -> str:
    if not body["stream"]:
        response = await client.post(url, json=body, headers=headers)
        await _raise_for_status(response)
        return _message_content(response.json())

    async with client.stream("POST", url, json=body, headers=headers) as response:
        await _raise_for_status(response)
        parts: list[str] = []
        done = False
        try:
            async with asyncio.timeout(None) as drain:
                async for line in response.aiter_lines():
                    if done or not line.startswith("data:"):
                        continue
                    data = line.removeprefix("data:").strip()
                    if data == "[DONE]":
                        done = True
                        drain.reschedule(asyncio.get_running_loop().time() + _POST_DONE_DRAIN_SECONDS)
                        continue
                    frame = json.loads(data)
                    if not isinstance(frame, dict) or frame.get("error"):
                        raise DirectJudgeError("upstream judge stream returned an invalid frame")
                    for choice in frame.get("choices") or []:
                        if choice.get("index", 0) != 0:
                            continue
                        content = (choice.get("delta") or {}).get("content")
                        if isinstance(content, str):
                            parts.append(content)
        except (httpx.RequestError, TimeoutError):
            if not done:
                raise
        if not done:
            raise DirectJudgeError("upstream judge stream ended before [DONE]")
        content = "".join(parts)
        if not content.strip():
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
    stream: bool,
) -> str:
    """Get raw assistant content, streaming on the local Ollama path.

    ``response_format=json_object`` rides the body verbatim to the upstream
    (Ollama's OpenAI endpoint honors it); a 4xx rejection — a provider that
    doesn't know the field — retries ONCE without it. A local connection
    failure retries once before any request has reached the gateway.
    """
    url = f"{gateway_base_url.rstrip('/')}/v1/chat/completions"
    headers = {"Authorization": f"Bearer {cred}"}
    body: dict[str, Any] = {
        "model": model,
        "temperature": 0.0,
        "max_tokens": max_tokens,
        "stream": stream,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": DIRECT_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
    }
    retried_connect = False
    while True:
        try:
            return await _chat_content(client, url, body, headers)
        except (httpx.ConnectError, httpx.ConnectTimeout):
            if not stream or retried_connect:
                raise
            retried_connect = True
            await asyncio.sleep(1)
        except httpx.HTTPStatusError as exc:
            if "response_format" not in body or not 400 <= exc.response.status_code < 500:
                raise
            body.pop("response_format")


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
    stream: bool = False,
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
    sources = await asyncio.to_thread(_gather_sources, ws, budget_bytes=budget)
    prompt = build_direct_prompt(
        rubric,
        analysis_md=analysis_md,
        sources=sources,
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
            stream=stream,
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
