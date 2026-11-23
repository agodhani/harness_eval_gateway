"""Budget accounting + best-effort event emission for the gateway.

Budget: sum ``usage.total_tokens`` across prior ``llm_response`` events for
the run (events table via SQLAlchemy), add the request's ``max_tokens``
estimate, compare against the run budget (cred ``max_tokens`` claim or
``DEFAULT_RUN_TOKEN_BUDGET``).

Emission: every helper posts to the control-plane ingest endpoint
(``API_INGEST_URL`` + ``/api/events/ingest``) with ``X-Ingest-Token``.
**Emission must never break the proxy** — ``emit_event`` swallows all
exceptions and only logs warnings.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

import httpx
from sqlalchemy import BigInteger, cast, func, select
from sqlalchemy.orm import Session

from db.models import Event
from gateway.config import GatewaySettings, get_settings
from gateway.creds import CredClaims

log = logging.getLogger("gateway.metering")


def uuid_or_none(value: str | uuid.UUID | None) -> uuid.UUID | None:
    """Coerce to UUID; None on missing/invalid (budget/emission then skip)."""
    if value is None:
        return None
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return None


def llm_usage_totals(
    session: Session,
    run_id: uuid.UUID,
    *,
    sandbox_id: uuid.UUID | None = None,
) -> dict[str, int]:
    """Sum ``payload.usage.{prompt,completion,cache_read,total}_tokens`` over
    a run's ``llm_response`` events, optionally scoped to one sandbox.

    Uses dialect-aware JSON extraction (``->>``/``json_extract`` via
    SQLAlchemy's JSON comparator) so it works on Postgres and sqlite alike.
    ``cache_read_tokens`` sums ``cache_read_input_tokens`` (Anthropic's
    field name) — the prompt side already includes it, matching the
    analytics cost math (cache tokens bill at the cache rate when a rate
    is declared, else at the input rate).
    """

    def _col(field: str):
        return func.coalesce(
            func.sum(
                cast(Event.payload["usage"][field].as_string(), BigInteger)
            ),
            0,
        )

    stmt = select(
        _col("prompt_tokens"),
        _col("completion_tokens"),
        _col("cache_read_input_tokens"),
        _col("total_tokens"),
    ).where(Event.run_id == run_id, Event.type == "llm_response")
    if sandbox_id is not None:
        stmt = stmt.where(Event.sandbox_id == sandbox_id)
    prompt, completion, cache_read, total = session.execute(stmt).one()
    return {
        "prompt_tokens": int(prompt),
        "completion_tokens": int(completion),
        "cache_read_tokens": int(cache_read),
        "total_tokens": int(total),
    }


def tokens_used(
    session: Session, run_id: uuid.UUID, *, sandbox_id: uuid.UUID | None = None
) -> int:
    """Sum ``payload.usage.total_tokens`` over a run's ``llm_response`` events
    (one sandbox's events when ``sandbox_id`` is given — PR28 per-cell cap)."""
    return llm_usage_totals(session, run_id, sandbox_id=sandbox_id)["total_tokens"]


def token_budget(claims: CredClaims, settings: GatewaySettings) -> int:
    """Effective run budget: cred ``max_tokens`` claim wins over the env default."""
    if claims.max_tokens is not None:
        return claims.max_tokens
    return settings.DEFAULT_RUN_TOKEN_BUDGET


def sandbox_token_budget(claims: CredClaims) -> int | None:
    """Per-sandbox cap from the cred's ``sandbox_max_tokens`` claim (PR28).

    Only sandbox-scoped participant creds carry it — service/eval creds
    mint no claim and stay exempt from the per-cell gate.
    """
    return claims.sandbox_max_tokens


def max_tokens_estimate(body: dict[str, Any]) -> int:
    """Best-effort token estimate: ``max_tokens`` (chat/Anthropic) or
    ``max_output_tokens`` (OpenAI Responses API).

    Absent/invalid values estimate 0 — the gate then only rejects runs whose
    recorded usage already exceeds the budget.
    """
    for key in ("max_tokens", "max_output_tokens"):
        mt = body.get(key)
        if isinstance(mt, bool):
            continue
        if isinstance(mt, (int, float)) and mt > 0:
            return int(mt)
    return 0


def usage_from_response(response_json: Any) -> dict[str, int]:
    """Normalize an OpenAI-style ``usage`` object to token counts."""
    usage = response_json.get("usage") if isinstance(response_json, dict) else None
    if not isinstance(usage, dict):
        usage = {}

    def _int(v: Any) -> int:
        return v if isinstance(v, int) and not isinstance(v, bool) else 0

    prompt = _int(usage.get("prompt_tokens"))
    completion = _int(usage.get("completion_tokens"))
    total = _int(usage.get("total_tokens")) or prompt + completion
    out: dict[str, int] = {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
    }
    # PR28: OpenAI's ``prompt_tokens`` already includes cached_tokens — the
    # split rides separately so a declared cache-read rate can bill them.
    details = usage.get("prompt_tokens_details")
    cache = _int(details.get("cached_tokens")) if isinstance(details, dict) else 0
    if cache > 0:
        out["cache_read_input_tokens"] = cache
    return out


def _to_int(v: Any) -> int:
    return v if isinstance(v, int) and not isinstance(v, bool) else 0


def _usage_dict(
    prompt: int, completion: int, *, cache_read: int = 0
) -> dict[str, int]:
    usage = {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
    }
    # PR28: the cache-read split rides the usage payload only when the
    # provider reported it — analytics/metering read the same field to bill
    # those (already-folded-into-prompt) tokens at a declared cache rate.
    if cache_read > 0:
        usage["cache_read_input_tokens"] = cache_read
    return usage


def usage_from_anthropic(response_json: Any) -> dict[str, int]:
    """Normalize an Anthropic ``/v1/messages`` usage object to token counts."""
    usage = response_json.get("usage") if isinstance(response_json, dict) else None
    if not isinstance(usage, dict):
        usage = {}
    # Input-side tokens: fresh input plus cache-read/context tokens; the
    # cache-read share also rides separately for cache-rate billing (PR28).
    cache_read = _to_int(usage.get("cache_read_input_tokens"))
    prompt = _to_int(usage.get("input_tokens")) + cache_read
    return _usage_dict(
        prompt, _to_int(usage.get("output_tokens")), cache_read=cache_read
    )


def usage_from_responses_api(response_json: Any) -> dict[str, int]:
    """Normalize an OpenAI ``/v1/responses`` usage object to token counts."""
    if isinstance(response_json, dict):
        # Non-streamed bodies: top-level ``usage``; streamed ``response.*``
        # events nest it under ``response``.
        usage = response_json.get("usage")
        if usage is None and isinstance(response_json.get("response"), dict):
            usage = response_json["response"].get("usage")
    else:
        usage = None
    if not isinstance(usage, dict):
        usage = {}
    details = usage.get("input_tokens_details")
    return _usage_dict(
        _to_int(usage.get("input_tokens")),
        _to_int(usage.get("output_tokens")),
        cache_read=_to_int(details.get("cached_tokens"))
        if isinstance(details, dict)
        else 0,
    )


def _sse_data_frames(body: bytes) -> tuple[list[dict[str, Any]], bool]:
    """Parse ``data:`` payloads from an SSE body → (objects, saw_sse).

    ``saw_sse`` is True when at least one ``data:`` line was present — it
    distinguishes a real event stream from a plain JSON body (which then
    takes the non-SSE extraction path). Unparseable payloads are skipped.
    """
    import json as _json

    objects: list[dict[str, Any]] = []
    saw_sse = False
    for line in body.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        data = line[len("data:") :].strip()
        if not data or data == "[DONE]":
            continue
        saw_sse = True
        try:
            obj = _json.loads(data)
        except _json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            objects.append(obj)
    return objects, saw_sse


def _stream_error_message(err: Any) -> str:
    """Human string for a streamed ``error`` value (dict or bare string)."""
    if isinstance(err, dict):
        msg = err.get("message")
        return str(msg) if msg else str(err)[:500]
    return str(err)[:500]


def _scan_metered_body(
    body: bytes, api: str
) -> tuple[dict[str, int] | None, int, list[str]]:
    """Single pass over a captured response body.

    Returns ``(usage, output_chars, errors)``:

    - ``usage`` — the provider-reported token counts, or ``None`` when the
      body carries no usage (provider ignored ``include_usage``, truncated
      capture, error stream). ``None`` vs zeros is how callers decide to
      estimate instead.
    - ``output_chars`` — characters of generated output found in the body
      (streamed deltas, or final message content for non-SSE bodies); the
      token estimator divides by 4.
    - ``errors`` — ``data: {"error": ...}`` stream frames (capped at 3);
      providers like OpenRouter report failures this way mid-stream.
    """
    import json as _json

    errors: list[str] = []

    def _collect_error(obj: dict[str, Any]) -> None:
        err = obj.get("error")
        if err and len(errors) < 3:
            errors.append(_stream_error_message(err))

    objects, saw_sse = _sse_data_frames(body)
    usage: dict[str, int] | None = None
    chars = 0

    if saw_sse:
        anthropic_in = 0
        anthropic_out = 0
        anthropic_cache = 0
        anthropic_usage_seen = False
        for obj in objects:
            _collect_error(obj)
            if api == "anthropic":
                etype = obj.get("type")
                if etype == "message_start":
                    u = (obj.get("message") or {}).get("usage")
                    if isinstance(u, dict):
                        anthropic_usage_seen = True
                        frame_cache = _to_int(u.get("cache_read_input_tokens"))
                        anthropic_cache += frame_cache
                        anthropic_in += _to_int(u.get("input_tokens")) + frame_cache
                        anthropic_out += _to_int(u.get("output_tokens"))
                elif etype == "message_delta":
                    u = obj.get("usage")
                    if isinstance(u, dict):
                        anthropic_usage_seen = True
                        anthropic_out = max(
                            anthropic_out, _to_int(u.get("output_tokens"))
                        )
                elif etype == "content_block_delta":
                    delta = obj.get("delta") or {}
                    t = delta.get("text")
                    if not isinstance(t, str):
                        # tool-call args stream as input_json_delta partial_json
                        t = delta.get("partial_json")
                    if isinstance(t, str):
                        chars += len(t)
            elif api == "responses":
                etype = obj.get("type")
                if etype in ("response.completed", "response.incomplete"):
                    resp = obj.get("response")
                    if isinstance(obj.get("usage"), dict) or (
                        isinstance(resp, dict)
                        and isinstance(resp.get("usage"), dict)
                    ):
                        usage = usage_from_responses_api(obj)
                elif etype in (
                    "response.output_text.delta",
                    "response.function_call_arguments.delta",
                ):
                    delta = obj.get("delta")
                    if isinstance(delta, str):
                        chars += len(delta)
            elif api == "openai" and obj.get("object") == "chat.completion.chunk":
                # Usage arrives on the final chunk (include_usage) — keep
                # the last populated one; content chunks carry usage=null.
                if isinstance(obj.get("usage"), dict):
                    usage = usage_from_response(obj)
                for choice in obj.get("choices") or []:
                    if isinstance(choice, dict):
                        delta = choice.get("delta") or {}
                        t = delta.get("content")
                        if isinstance(t, str):
                            chars += len(t)
        if api == "anthropic" and anthropic_usage_seen:
            usage = _usage_dict(
                anthropic_in, anthropic_out, cache_read=anthropic_cache
            )
        return usage, chars, errors

    # --- non-SSE body: buffered JSON response (or garbage) ---
    try:
        obj = _json.loads(body.decode("utf-8", errors="replace"))
    except _json.JSONDecodeError:
        return None, 0, errors
    if not isinstance(obj, dict):
        return None, 0, errors
    _collect_error(obj)
    if api == "anthropic":
        if isinstance(obj.get("usage"), dict):
            usage = usage_from_anthropic(obj)
        for block in obj.get("content") or []:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                chars += len(block["text"])
    elif api == "responses":
        resp = obj.get("response")
        if isinstance(obj.get("usage"), dict) or (
            isinstance(resp, dict) and isinstance(resp.get("usage"), dict)
        ):
            usage = usage_from_responses_api(obj)
        for item in obj.get("output") or []:
            if isinstance(item, dict):
                for part in item.get("content") or []:
                    if isinstance(part, dict) and isinstance(part.get("text"), str):
                        chars += len(part["text"])
    else:
        if isinstance(obj.get("usage"), dict):
            usage = usage_from_response(obj)
        for choice in obj.get("choices") or []:
            if isinstance(choice, dict):
                msg = choice.get("message") or {}
                t = msg.get("content")
                if isinstance(t, str):
                    chars += len(t)
    return usage, chars, errors


def usage_from_sse(body: bytes, *, api: str) -> dict[str, int]:
    """Extract final usage from a captured SSE stream body.

    ``api`` is ``"anthropic"`` (``/v1/messages``: ``message_start`` carries
    input-side tokens, ``message_delta`` reports cumulative output),
    ``"responses"`` (``response.completed``/``response.incomplete`` carry the
    final ``response.usage``), or ``"openai"`` (chat-completions chunks: the
    terminal ``chat.completion.chunk`` carries ``usage`` when the request set
    ``stream_options.include_usage``). Non-SSE bodies fall back to the JSON
    extractors. Returns zeros when no usage is present — callers that need
    to distinguish "no usage reported" use ``metered_stream_usage``.
    """
    usage, _chars, _errors = _scan_metered_body(body, api)
    return usage or _usage_dict(0, 0)


def metered_stream_usage(body: bytes, *, api: str) -> dict[str, Any]:
    """Usage + stream-error frames for a captured response body.

    Returns ``{"usage": {...}, "estimated": bool, "errors": [str, ...]}``.

    Provider-reported usage wins. When the body carries none — the provider
    ignored ``stream_options.include_usage``, the capture was truncated, or
    the stream was an error/keep-alive only — ``completion_tokens`` is
    ESTIMATED from the generated output text (``chars // 4``) with
    ``prompt_tokens`` null and ``estimated=True``. The estimate still feeds
    the run token budget (``payload.usage.total_tokens``) but is flagged so
    analytics can tell it from provider numbers.
    """
    usage, chars, errors = _scan_metered_body(body, api)
    if usage is not None:
        return {"usage": usage, "estimated": False, "errors": errors}
    est = chars // 4
    return {
        "usage": {
            "prompt_tokens": None,
            "completion_tokens": est,
            "total_tokens": est,
        },
        "estimated": True,
        "errors": errors,
    }


async def post_ingest_events(events: list[dict[str, Any]]) -> tuple[int, Any]:
    """POST ``{"events": [...]}`` to the control-plane ingest endpoint.

    Returns ``(http_status, json_body)``. Transport failures return
    ``(502, {"error": ...})`` — this helper never raises, so callers (proxy,
    /internal/*) can forward the status verbatim.
    """
    settings = get_settings()
    url = f"{settings.API_INGEST_URL.rstrip('/')}/api/events/ingest"
    try:
        async with httpx.AsyncClient(timeout=settings.INGEST_TIMEOUT) as client:
            resp = await client.post(
                url,
                json={"events": events},
                headers={"X-Ingest-Token": settings.INGEST_TOKEN},
            )
    except Exception as exc:  # noqa: BLE001 - report, never raise
        log.warning("ingest POST %s failed: %s", url, exc)
        return 502, {"error": {"message": f"ingest unreachable: {exc}", "type": "ingest_error"}}
    try:
        return resp.status_code, resp.json()
    except Exception:  # noqa: BLE001 - non-JSON body; pass raw text through
        return resp.status_code, {"raw": resp.text[:2000]}


def _cred_ids(claims: CredClaims) -> tuple[str | None, str | None]:
    return claims.run_id, claims.sandbox_id


def _resolve_provider(model: Any) -> str:
    """Provider serving ``model`` for event payloads (PROVIDER_ROUTES-aware).

    Emit callers pass ``provider=`` when the request path already resolved
    it; this is the fallback for callers that didn't.
    """
    return get_settings().provider_for_model(
        model if isinstance(model, str) else None
    )


async def emit_event(
    claims: CredClaims,
    *,
    source: str,
    event_type: str,
    payload: dict[str, Any],
) -> None:
    """Best-effort single-event emission. NEVER raises.

    Non-UUID run ids (can't exist in the events table) are skipped with a
    debug log; ingest rejections (>=400) are warnings.
    """
    try:
        run_id, sandbox_id = _cred_ids(claims)
        rid = uuid_or_none(run_id)
        if rid is None:
            log.debug("skipping %s event: run_id %r is not a UUID", event_type, run_id)
            return
        sid = uuid_or_none(sandbox_id)
        event: dict[str, Any] = {
            "run_id": str(rid),
            "source": source,
            "type": event_type,
            "payload": payload,
        }
        if sid is not None:
            event["sandbox_id"] = str(sid)
        status_code, _body = await post_ingest_events([event])
        if status_code >= 400:
            log.warning(
                "ingest rejected %s event (HTTP %s): %s",
                event_type,
                status_code,
                _body,
            )
    except Exception:
        log.warning("event emission failed for %s", event_type, exc_info=True)


async def emit_llm_request(
    claims: CredClaims,
    body: dict[str, Any],
    *,
    provider: str | None = None,
    cached: bool = False,
) -> None:
    messages = body.get("messages")
    tools = body.get("tools")
    payload: dict[str, Any] = {
        "model": body.get("model"),
        "provider": provider or _resolve_provider(body.get("model")),
        "message_count": len(messages) if isinstance(messages, list) else 0,
        "tool_count": len(tools) if isinstance(tools, list) else 0,
        "run_id": claims.run_id,
        "sandbox_id": claims.sandbox_id,
    }
    if cached:
        # PR18 — response was replayed from the gateway cache, not upstream.
        payload["cached"] = True
    await emit_event(
        claims,
        source="gateway",
        event_type="llm_request",
        payload=payload,
    )


def _tokens_per_sec(usage: dict[str, int | None], latency_ms: int) -> float | None:
    """Output-token throughput over the call's whole wall time (PR30).

    ``completion_tokens / total_seconds`` — the end-to-end speed axis for
    analytics. ``ttft_ms`` rides the payload separately so a reader can
    still derive the post-first-token decode rate when it wants it.
    ``None`` when the call recorded no completion count or no measurable
    elapsed time (an instant replay/mocked call has no honest rate).
    """
    completion = usage.get("completion_tokens") if isinstance(usage, dict) else None
    if (
        isinstance(completion, int)
        and not isinstance(completion, bool)
        and latency_ms > 0
    ):
        return round(completion / (latency_ms / 1000), 1)
    return None


async def emit_llm_response(
    claims: CredClaims,
    *,
    model: Any,
    usage: dict[str, int | None],
    latency_ms: int,
    http_status: int,
    provider: str | None = None,
    cached: bool = False,
    usage_estimated: bool = False,
    ttft_ms: int | None = None,
) -> None:
    """Meter one completed upstream call.

    Timing (PR30): ``latency_ms`` stays the established field; ``total_ms``
    carries the same end-to-end value under the canonical name, and
    ``ttft_ms`` is the time to the first response byte/chunk — for a
    streamed body the first ``aiter_bytes`` chunk, for a buffered body the
    first content byte read off the wire. ``ttft_ms`` falls back to the
    total when no first byte was ever observed (pre-start disconnect,
    empty stream, cache replay). ``tokens_per_sec`` is the derived output
    throughput — null on cache replays, which measure the store, not the
    model.
    """
    payload: dict[str, Any] = {
        "model": model,
        "provider": provider or _resolve_provider(model),
        "usage": usage,
        "latency_ms": latency_ms,
        "total_ms": latency_ms,
        "ttft_ms": ttft_ms if ttft_ms is not None else latency_ms,
        "tokens_per_sec": None if cached else _tokens_per_sec(usage, latency_ms),
        "http_status": http_status,
        "run_id": claims.run_id,
        "sandbox_id": claims.sandbox_id,
    }
    if cached:
        payload["cached"] = True
    if usage_estimated:
        # No provider usage was reported — completion_tokens is the len/4
        # output-text estimate (see metering.metered_stream_usage).
        payload["usage_estimated"] = True
    await emit_event(
        claims,
        source="gateway",
        event_type="llm_response",
        payload=payload,
    )


async def emit_error(
    claims: CredClaims,
    *,
    stage: str,
    message: str,
    http_status: int | None = None,
    reason: str | None = None,
    **extra: Any,
) -> None:
    payload: dict[str, Any] = {
        "stage": stage,
        "message": message,
        "run_id": claims.run_id,
        "sandbox_id": claims.sandbox_id,
    }
    if http_status is not None:
        payload["http_status"] = http_status
    if reason is not None:
        payload["reason"] = reason
    payload.update(extra)
    await emit_event(claims, source="gateway", event_type="error", payload=payload)
