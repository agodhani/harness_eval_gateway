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
    """Best-effort token estimate for a chat request: its ``max_tokens``.

    Absent/invalid values estimate 0 — the gate then only rejects runs whose
    recorded usage already exceeds the budget.
    """
    mt = body.get("max_tokens")
    if isinstance(mt, bool):
        return 0
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


async def emit_llm_request(claims: CredClaims, body: dict[str, Any]) -> None:
    messages = body.get("messages")
    tools = body.get("tools")
    await emit_event(
        claims,
        source="gateway",
        event_type="llm_request",
        payload={
            "model": body.get("model"),
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
) -> None:
    await emit_event(
        claims,
        source="gateway",
        event_type="llm_response",
        payload={
            "model": model,
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
