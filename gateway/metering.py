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


def tokens_used(session: Session, run_id: uuid.UUID) -> int:
    """Sum ``payload.usage.total_tokens`` over a run's ``llm_response`` events.

    Uses dialect-aware JSON extraction (``->>``/``json_extract`` via
    SQLAlchemy's JSON comparator) so it works on Postgres and sqlite alike.
    """
    token_expr = cast(Event.payload["usage"]["total_tokens"].as_string(), BigInteger)
    stmt = select(func.coalesce(func.sum(token_expr), 0)).where(
        Event.run_id == run_id, Event.type == "llm_response"
    )
    return int(session.execute(stmt).scalar_one())


def token_budget(claims: CredClaims, settings: GatewaySettings) -> int:
    """Effective budget: cred ``max_tokens`` claim wins over the env default."""
    if claims.max_tokens is not None:
        return claims.max_tokens
    return settings.DEFAULT_RUN_TOKEN_BUDGET


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
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
    }


def _to_int(v: Any) -> int:
    return v if isinstance(v, int) and not isinstance(v, bool) else 0


def _usage_dict(prompt: int, completion: int) -> dict[str, int]:
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
    }


def usage_from_anthropic(response_json: Any) -> dict[str, int]:
    """Normalize an Anthropic ``/v1/messages`` usage object to token counts."""
    usage = response_json.get("usage") if isinstance(response_json, dict) else None
    if not isinstance(usage, dict):
        usage = {}
    # Input-side tokens: fresh input plus cache-read/context tokens.
    prompt = _to_int(usage.get("input_tokens")) + _to_int(
        usage.get("cache_read_input_tokens")
    )
    return _usage_dict(prompt, _to_int(usage.get("output_tokens")))


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
    return _usage_dict(
        _to_int(usage.get("input_tokens")), _to_int(usage.get("output_tokens"))
    )


def usage_from_sse(body: bytes, *, api: str) -> dict[str, int]:
    """Extract final usage from a captured SSE stream body.

    ``api`` is ``"anthropic"`` (``/v1/messages``: ``message_start`` carries
    input-side tokens, ``message_delta`` reports cumulative output),
    ``"responses"`` (``response.completed``/``response.incomplete`` carry the
    final ``response.usage``), or ``"openai"`` (chat-completions chunks: the
    terminal ``chat.completion.chunk`` carries ``usage`` when the request set
    ``stream_options.include_usage``). Non-SSE bodies fall back to the JSON
    extractors.
    """
    import json as _json

    text = body.decode("utf-8", errors="replace")
    anthropic_in = 0
    anthropic_out = 0
    responses_usage: dict[str, int] = {}
    openai_usage: dict[str, int] = {}
    saw_event = False
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        data = line[len("data:") :].strip()
        if not data or data == "[DONE]":
            continue
        try:
            obj = _json.loads(data)
        except _json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        etype = obj.get("type")
        if api == "anthropic":
            if etype == "message_start":
                saw_event = True
                u = (obj.get("message") or {}).get("usage") or {}
                anthropic_in += _to_int(u.get("input_tokens")) + _to_int(
                    u.get("cache_read_input_tokens")
                )
                anthropic_out += _to_int(u.get("output_tokens"))
            elif etype == "message_delta":
                saw_event = True
                u = obj.get("usage") or {}
                anthropic_out = max(anthropic_out, _to_int(u.get("output_tokens")))
        elif api == "responses":
            if etype in ("response.completed", "response.incomplete"):
                saw_event = True
                responses_usage = usage_from_responses_api(obj)
        elif api == "openai" and obj.get("object") == "chat.completion.chunk":
            saw_event = True
            # Usage arrives on the final chunk (include_usage) — keep the
            # last populated one; content chunks carry usage=null.
            if isinstance(obj.get("usage"), dict):
                openai_usage = usage_from_response(obj)
    if api == "anthropic":
        if saw_event:
            return _usage_dict(anthropic_in, anthropic_out)
        try:
            return usage_from_anthropic(_json.loads(text))
        except _json.JSONDecodeError:
            return _usage_dict(0, 0)
    if api == "openai":
        if saw_event:
            return openai_usage
        try:
            return usage_from_response(_json.loads(text))
        except _json.JSONDecodeError:
            return _usage_dict(0, 0)
    if saw_event:
        return responses_usage
    try:
        return usage_from_responses_api(_json.loads(text))
    except _json.JSONDecodeError:
        return _usage_dict(0, 0)


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
    claims: CredClaims, body: dict[str, Any], *, provider: str | None = None
) -> None:
    messages = body.get("messages")
    tools = body.get("tools")
    await emit_event(
        claims,
        source="gateway",
        event_type="llm_request",
        payload={
            "model": body.get("model"),
            "provider": provider or _resolve_provider(body.get("model")),
            "message_count": len(messages) if isinstance(messages, list) else 0,
            "tool_count": len(tools) if isinstance(tools, list) else 0,
            "run_id": claims.run_id,
            "sandbox_id": claims.sandbox_id,
        },
    )


async def emit_llm_response(
    claims: CredClaims,
    *,
    model: Any,
    usage: dict[str, int],
    latency_ms: int,
    http_status: int,
    provider: str | None = None,
) -> None:
    await emit_event(
        claims,
        source="gateway",
        event_type="llm_response",
        payload={
            "model": model,
            "provider": provider or _resolve_provider(model),
            "usage": usage,
            "latency_ms": latency_ms,
            "http_status": http_status,
            "run_id": claims.run_id,
            "sandbox_id": claims.sandbox_id,
        },
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
