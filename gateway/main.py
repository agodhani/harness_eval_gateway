"""F3 — AI gateway: OpenAI-compatible proxy with metering, budgets, creds.

Endpoints
---------
``GET  /health``                  — liveness.
``POST /v1/chat/completions``     — Bearer ``heg.*`` cred → budget gate →
                                    forward to the resolved upstream;
                                    ``stream: true`` callers get a metered
                                    SSE pass-through, others a non-streamed
                                    buffered response → emit ``llm_request``/
                                    ``llm_response``/``error`` events →
                                    return upstream body+status verbatim.
``POST /v1/messages``             — Anthropic Messages pass-through (Claude
                                    Code, dsh); same cred/budget/metering
                                    pipeline as chat completions, streaming
                                    preserved with a metering tee.
``POST /v1/messages/count_tokens``— Anthropic token-count preflight (plain
                                    pass-through, no llm events — it is not a
                                    generation call).
``POST /v1/responses``            — OpenAI Responses pass-through (Codex CLI).
``GET  /v1/models``               — model list pass-through (CLIs probe it);
                                    response carries ``x-heg-provider`` so
                                    callers know which upstream answered.
``POST /v1/models/pull``          — ``ollama pull`` pass-through (PR1 model
                                    install); NDJSON progress streamed
                                    verbatim, un-metered; 501 on non-ollama
                                    upstreams. Control-plane service creds
                                    only (sandbox creds → 403); the pull
                                    body is allowlisted to ``name``/``model``
                                    and the upstream client is pinned to
                                    Ollama — see the endpoint docstring.
``POST /internal/events``         — sandbox-facing event ingest (cred-stamped
                                    run/sandbox ids) → forwards to the
                                    control-plane ``/api/events/ingest``.
``POST /internal/done``           — sandbox ``done`` CLI target; emits a
                                    ``done_signal`` event for the cred's
                                    run/sandbox.

Response cache (PR18): when ``GATEWAY_CACHE_ENABLED``, metered generation
calls are replayed from a local sqlite store keyed on
path+provider+outgoing-body — hits skip the upstream entirely, still emit
``llm_request``/``llm_response`` (payload ``cached: true``), and still count
against the run budget, so a replayed run meters identically to the live one.
Hits carry an ``x-heg-cache: hit`` response header. See gateway/cache.py.

Upstream providers: ``UPSTREAM_PROVIDER`` (``ollama`` default, ``openrouter``,
``openai``, ``anthropic``) is the process-wide default; ``PROVIDER_ROUTES``
maps model globs → provider for per-request overrides resolved from the
request body's ``model``; an ``x-heg-provider`` request header forces a
provider outright for SERVICE creds only (``sandbox_id=None`` — unknown or
unkeyed values 400; the header is ignored on sandbox-scoped creds so agent
code can't retarget onto paid upstreams). Caller ``anthropic-beta``/
``anthropic-version`` headers are forwarded when the resolved provider is
``anthropic``. Endpoint coverage differs per provider — a path the
resolved upstream doesn't implement returns its native 404 verbatim (e.g.
``/v1/messages`` on ``openai``, ``/v1/chat/completions`` on ``anthropic``).

Auth: ``Authorization: Bearer <cred>`` (or Anthropic-style ``x-api-key``)
where cred is ``heg-dev`` (static dev credential) or
``heg.<b64url-payload>.<b64url-hmac>`` (see gateway/creds.py).
``/internal/events`` additionally accepts ``Bearer <INGEST_TOKEN>`` as a
pass-through credential for producers that already carry run/sandbox ids —
this matches the ingest token sandboxes may be configured with.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime
from typing import Annotated, Any

import anyio
import httpx
from fastapi import APIRouter, Body, Depends, FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field
from starlette.types import Send

from api.schemas import EventSource, EventType
from gateway import metering
from gateway.cache import CachedResponse, ResponseCache
from gateway.config import ProviderName, get_settings
from gateway.creds import CredClaims
from gateway.deps import (
    get_cred,
    get_events_auth,
    get_ollama_client,
    get_provider,
    get_response_cache,
    get_session_factory,
    get_upstream_client,
)

log = logging.getLogger("gateway.main")

router = APIRouter()


# ---------------------------------------------------------------------------
# schemas local to the gateway
# ---------------------------------------------------------------------------


class SandboxEvent(BaseModel):
    """Event accepted on /internal/events.

    run_id/sandbox_id are optional — a valid cred stamps/overrides them, and
    the api ingest is the final validator for pass-through callers.
    """

    run_id: uuid.UUID | None = None
    sandbox_id: uuid.UUID | None = None
    source: EventSource
    type: EventType
    seq: int | None = None
    ts: datetime | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class SandboxIngestRequest(BaseModel):
    events: list[SandboxEvent] = Field(min_length=1)


class DoneRequest(BaseModel):
    summary: str
    status: str = "done"


# ---------------------------------------------------------------------------
# endpoints
# ---------------------------------------------------------------------------


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


def _tokens_used(run_uuid: uuid.UUID) -> int:
    """Run token total for the budget gate on a FRESH session.

    Called via ``asyncio.to_thread`` — it must not reuse a request-scoped
    session, which would share one DBAPI connection across threads.
    """
    with get_session_factory()() as session:
        return metering.tokens_used(session, run_uuid)


def _sandbox_tokens_used(run_uuid: uuid.UUID, sandbox_uuid: uuid.UUID) -> int:
    """One sandbox's token total (PR28 per-cell gate) — same fresh-session
    rule as ``_tokens_used``."""
    with get_session_factory()() as session:
        return metering.tokens_used(session, run_uuid, sandbox_id=sandbox_uuid)


# Body params only Ollama understands — harnesses tuned for Ollama (miniloop's
# `think`/`num_ctx`) emit them; hosted providers may 400 on unknown fields.
_OLLAMA_ONLY_KEYS = ("num_ctx", "keep_alive", "think", "options")


def _strip_ollama_only(
    body: dict[str, Any], provider: ProviderName
) -> dict[str, Any]:
    """Drop Ollama-only body params unless the resolved provider is ollama."""
    if provider == "ollama":
        return body
    return {k: v for k, v in body.items() if k not in _OLLAMA_ONLY_KEYS}


# Anthropic protocol headers a caller may send that must reach the anthropic
# upstream — `anthropic-beta` gates beta features (interleaved thinking,
# fine-grained tool streaming, 1M context); `anthropic-version` names the
# protocol revision the client speaks. Per-request headers override the
# client's configured ANTHROPIC_VERSION default.
_ANTHROPIC_FORWARD_HEADERS = ("anthropic-beta", "anthropic-version")


def _forward_protocol_headers(
    request_headers: Mapping[str, str], provider: ProviderName
) -> dict[str, str] | None:
    """Caller protocol headers to forward for ``provider`` — anthropic only."""
    if provider != "anthropic":
        return None
    fwd = {
        name: request_headers[name]
        for name in _ANTHROPIC_FORWARD_HEADERS
        if name in request_headers
    }
    return fwd or None


# ---------------------------------------------------------------------------
# Transient upstream retry (bughunt): a saturated-but-alive upstream (Ollama
# swapping a model, a proxy hiccup) surfaces connect/read timeouts or bare
# 502/503/504s — failing the request straight to the harness kills the lane
# on a transient. ``_upstream_post`` retries transport errors and those
# statuses up to ``UPSTREAM_RETRY_ATTEMPTS`` extra times with exponential
# backoff; 4xx/429s and failures once the response started streaming to the
# client are NEVER retried. Each retry emits an ``upstream_retry``-staged
# error event (kept distinct from ``stage="upstream"`` so the monitor's
# error threshold still bounds the OUTER failure count).
# ---------------------------------------------------------------------------

_RETRYABLE_UPSTREAM_STATUSES = frozenset({502, 503, 504})
_RETRYABLE_UPSTREAM_ERRORS = (
    httpx.TimeoutException,  # connect/read/write/pool timeouts
    httpx.NetworkError,  # refused, reset, EOF (ConnectError et al.)
    httpx.RemoteProtocolError,  # server hung up without a response
)

# Bound on bytes teed off a streamed response for metering (usage + output
# text + error frames). The client stream itself is unbounded — past this
# cap metering degrades to the estimate path and the body is never cached.
_STREAM_CAPTURE_LIMIT = 2 * 1024 * 1024


async def _emit_upstream_retry(
    claims: CredClaims,
    *,
    meter: bool,
    provider: ProviderName,
    attempt: int,
    max_attempts: int,
    delay: float,
    message: str,
    http_status: int | None = None,
) -> None:
    if not meter:
        return
    await metering.emit_error(
        claims,
        stage="upstream_retry",
        message=message,
        http_status=http_status,
        attempt=attempt,
        max_attempts=max_attempts,
        retry_delay_seconds=delay,
        provider=provider,
    )


async def _emit_client_disconnected(
    claims: CredClaims,
    *,
    meter: bool,
    provider: ProviderName,
    latency_ms: int,
    message: str,
) -> None:
    """Best-effort terminal event for a call the client abandoned.

    Raised from inside the cancelled request task, so this runs under a
    SHIELDED cancel scope — every checkpoint would otherwise re-raise
    ``Cancelled`` and the event would be dropped, leaving the streamed
    call unaccounted (live-observed: deepseek giving up mid-request left
    zero ``llm_response`` events).
    """
    if not meter:
        return
    with anyio.CancelScope(shield=True):
        await metering.emit_error(
            claims,
            stage="client_disconnected",
            message=message,
            latency_ms=latency_ms,
            provider=provider,
        )


async def _upstream_post(
    *,
    upstream: httpx.AsyncClient,
    path: str,
    body: dict[str, Any],
    claims: CredClaims,
    provider: ProviderName,
    request_headers: Mapping[str, str] | None,
    stream: bool,
    started: float,
    meter: bool = True,
) -> httpx.Response | JSONResponse:
    """POST ``body`` upstream, retrying transient failures before giving up.

    Returns the upstream ``httpx.Response`` once a response is available
    (headers-received when ``stream=True``) — INCLUDING final-failure
    statuses, which callers meter and forward verbatim — or a 502
    ``JSONResponse`` when every attempt died in transport (the historical
    upstream-error path). The request body is identical across attempts.
    """
    settings = get_settings()
    max_tries = 1 + max(0, settings.UPSTREAM_RETRY_ATTEMPTS)
    backoff_base = settings.UPSTREAM_RETRY_BACKOFF_SECONDS
    headers = _forward_protocol_headers(request_headers or {}, provider)
    attempt = 0
    while True:
        attempt += 1
        try:
            if stream:
                req = upstream.build_request(
                    "POST", path, json=body, headers=headers
                )
                resp = await upstream.send(req, stream=True)
            else:
                resp = await upstream.post(path, json=body, headers=headers)
        except httpx.HTTPError as exc:
            if isinstance(exc, _RETRYABLE_UPSTREAM_ERRORS) and attempt < max_tries:
                delay = backoff_base * (2 ** (attempt - 1))
                await _emit_upstream_retry(
                    claims,
                    meter=meter,
                    provider=provider,
                    attempt=attempt,
                    max_attempts=max_tries,
                    delay=delay,
                    message=f"{type(exc).__name__}: {exc}",
                )
                await asyncio.sleep(delay)
                continue
            # Terminal transport failure — the historical upstream error.
            latency_ms = int((time.monotonic() - started) * 1000)
            if meter:
                await metering.emit_error(
                    claims,
                    stage="upstream",
                    message=f"{type(exc).__name__}: {exc}",
                    latency_ms=latency_ms,
                    provider=provider,
                )
            return JSONResponse(
                status_code=status.HTTP_502_BAD_GATEWAY,
                content={
                    "error": {
                        "message": f"upstream request failed: {exc}",
                        "type": "upstream_error",
                    }
                },
            )
        if resp.status_code in _RETRYABLE_UPSTREAM_STATUSES and attempt < max_tries:
            # Drain the failed attempt before retrying — frees the
            # connection and captures the upstream's own error text.
            detail = ""
            try:
                detail = (await resp.aread()).decode("utf-8", errors="replace")[
                    :500
                ]
            except Exception as exc:  # noqa: BLE001 — drain is best-effort
                log.debug("upstream retry: error body drain failed: %s", exc)
            try:
                await resp.aclose()
            except Exception as exc:  # noqa: BLE001 — response is dead anyway
                log.debug("upstream retry: response close failed: %s", exc)
            delay = backoff_base * (2 ** (attempt - 1))
            await _emit_upstream_retry(
                claims,
                meter=meter,
                provider=provider,
                attempt=attempt,
                max_attempts=max_tries,
                delay=delay,
                message=detail or f"HTTP {resp.status_code}",
                http_status=resp.status_code,
            )
            await asyncio.sleep(delay)
            continue
        # 2xx, 4xx/429 (never retried), or an exhausted 502/503/504 — the
        # caller's normal metering/error path handles all of these.
        return resp


# ---------------------------------------------------------------------------
# PR18 — response cache helpers (opt-in via GATEWAY_CACHE_*; see
# gateway/cache.py). Lookups/stores are best-effort like event emission —
# a broken cache must degrade to a miss, never a proxy 5xx.
# ---------------------------------------------------------------------------


async def _cache_get(
    cache: ResponseCache | None, key: str | None
) -> CachedResponse | None:
    if cache is None or key is None:
        return None
    try:
        return await asyncio.to_thread(cache.get, key)
    except Exception:  # best-effort; degrade to miss
        log.warning("cache lookup failed; treating as miss", exc_info=True)
        return None


async def _cache_store(
    cache: ResponseCache | None,
    key: str | None,
    *,
    status: int,
    content_type: str,
    body: bytes,
) -> None:
    if cache is None or key is None:
        return
    try:
        await asyncio.to_thread(
            cache.store, key, status=status, content_type=content_type, body=body
        )
    except Exception:  # best-effort; dropping the entry is ok
        log.warning("cache store failed", exc_info=True)


def _cache_hit_headers(content_type: str) -> dict[str, str]:
    return {"content-type": content_type, "x-heg-cache": "hit"}


@router.post("/v1/chat/completions")
async def chat_completions(
    request: Request,
    body: Annotated[dict[str, Any], Body()],
    claims: Annotated[CredClaims, Depends(get_cred)],
    upstream: Annotated[httpx.AsyncClient, Depends(get_upstream_client)],
    provider: Annotated[ProviderName, Depends(get_provider)],
    cache: Annotated[ResponseCache | None, Depends(get_response_cache)],
) -> Response:
    # --- streaming callers (pi and other OpenAI-compatible CLIs always set
    # stream:true and parse SSE — a forced non-streamed JSON body reads as an
    # empty stream to them): pass through with a metering tee. The shared
    # passthrough applies the budget gate + llm_request emit, so this branch
    # must run before the non-streamed gate below.
    if body.get("stream"):
        out = _strip_ollama_only(dict(body), provider)
        # Ask the upstream for a terminal usage chunk so streamed calls stay
        # metered (no-op for upstreams that ignore it — the tee then falls
        # back to a len/4 usage estimate flagged ``usage_estimated``). The
        # resulting usage-only ``chat.completion.chunk`` frame passes
        # through to the client verbatim: it's the same frame OpenAI emits
        # when callers request ``include_usage`` themselves, so OpenAI-
        # compatible SDKs parse it as a valid empty-choices chunk.
        stream_options = dict(out.get("stream_options") or {})
        stream_options.setdefault("include_usage", True)
        out["stream_options"] = stream_options
        if provider == "ollama":
            out.setdefault("num_ctx", 32_768)
            out.setdefault("keep_alive", "30m")
        return await _passthrough(
            path="/v1/chat/completions",
            api="openai",
            body=out,
            claims=claims,
            upstream=upstream,
            provider=provider,
            request_headers=request.headers,
            cache=cache,
        )

    # --- budget gate (before any upstream traffic) ---
    gated = await _budget_gate(claims, body, provider)
    if gated is not None:
        return gated

    # --- prepare upstream request: force non-streaming, inject defaults ---
    out = _strip_ollama_only(dict(body), provider)
    out["stream"] = False
    if provider == "ollama":
        out.setdefault("num_ctx", 32_768)
        out.setdefault("keep_alive", "30m")

    # --- replay cache (PR18): lookup AFTER the budget gate so a replayed
    # run 429s at the same point as the live one. Hits emit llm_request +
    # llm_response with payload.cached=true and the stored body meters the
    # recorded usage, keeping the run's token accrual deterministic.
    cache_key = (
        cache.key_for(path="/v1/chat/completions", provider=provider, body=out)
        if cache is not None
        else None
    )
    started = time.monotonic()
    hit = await _cache_get(cache, cache_key)
    latency_ms = int((time.monotonic() - started) * 1000)

    await metering.emit_llm_request(
        claims, out, provider=provider, cached=hit is not None
    )
    if hit is not None:
        try:
            cached_json = json.loads(hit.body)
        except ValueError:
            cached_json = None
        await metering.emit_llm_response(
            claims,
            model=out.get("model"),
            usage=metering.usage_from_response(cached_json),
            latency_ms=latency_ms,
            http_status=hit.status,
            provider=provider,
            cached=True,
        )
        return Response(
            content=hit.body,
            status_code=hit.status,
            headers=_cache_hit_headers(hit.content_type),
        )

    started = time.monotonic()
    try:
        result = await _upstream_post(
            upstream=upstream,
            path="/v1/chat/completions",
            body=out,
            claims=claims,
            provider=provider,
            request_headers=request.headers,
            stream=False,
            started=started,
        )
    except asyncio.CancelledError:
        await _emit_client_disconnected(
            claims,
            meter=True,
            provider=provider,
            latency_ms=int((time.monotonic() - started) * 1000),
            message="client disconnected while awaiting upstream response",
        )
        raise
    if isinstance(result, JSONResponse):
        return result
    upstream_resp = result
    latency_ms = int((time.monotonic() - started) * 1000)

    # --- meter the outcome; always return upstream verbatim ---
    try:
        response_json = upstream_resp.json()
    except Exception:  # noqa: BLE001 - non-JSON upstream body
        response_json = None
    usage = metering.usage_from_response(response_json)
    response_emitted = False
    try:
        await metering.emit_llm_response(
            claims,
            model=out.get("model"),
            usage=usage,
            latency_ms=latency_ms,
            http_status=upstream_resp.status_code,
            provider=provider,
        )
        response_emitted = True
        if not 200 <= upstream_resp.status_code < 300:
            await metering.emit_error(
                claims,
                stage="upstream",
                message=upstream_resp.text[:500],
                http_status=upstream_resp.status_code,
                latency_ms=latency_ms,
                provider=provider,
            )
        else:
            await _cache_store(
                cache,
                cache_key,
                status=upstream_resp.status_code,
                content_type=upstream_resp.headers.get(
                    "content-type", "application/json"
                ),
                body=upstream_resp.content,
            )
    except asyncio.CancelledError:
        with anyio.CancelScope(shield=True):
            if not response_emitted:
                await metering.emit_llm_response(
                    claims,
                    model=out.get("model"),
                    usage=usage,
                    latency_ms=latency_ms,
                    http_status=upstream_resp.status_code,
                    provider=provider,
                )
            await metering.emit_error(
                claims,
                stage="client_disconnected",
                message="client disconnected while metering upstream response",
                http_status=upstream_resp.status_code,
                latency_ms=latency_ms,
                provider=provider,
                usage=usage,
            )
        raise

    return Response(
        content=upstream_resp.content,
        status_code=upstream_resp.status_code,
        headers={
            "content-type": upstream_resp.headers.get(
                "content-type", "application/json"
            )
        },
    )


# ---------------------------------------------------------------------------
# Phase 2.5 — protocol pass-throughs (Anthropic messages / OpenAI responses)
# ---------------------------------------------------------------------------


async def _budget_gate(
    claims: CredClaims, body: dict[str, Any], provider: ProviderName
) -> JSONResponse | None:
    """Shared token-budget gate; returns a 429 response or None (pass).

    Two limits, both checked before any upstream traffic:

    - RUN ceiling — the cred's ``max_tokens`` (or env default) against the
      whole run's metered tokens; every cred shares it.
    - PER-SANDBOX ceiling (PR28) — the cred's ``sandbox_max_tokens`` claim
      against ONLY this sandbox's metered tokens, so a runaway cell starves
      itself instead of eating the run's budget out from under its
      siblings. Only participant creds carry the claim; service/eval creds
      (``sandbox_id=None`` or no claim) skip the gate.
    """
    settings = get_settings()
    run_uuid = metering.uuid_or_none(claims.run_id)
    if run_uuid is None:
        return None
    used = await asyncio.to_thread(_tokens_used, run_uuid)
    estimate = metering.max_tokens_estimate(body)
    limit = metering.token_budget(claims, settings)
    if used + estimate > limit:
        await metering.emit_error(
            claims,
            stage="budget",
            reason="budget_exceeded",
            message=(
                f"run token budget exceeded: used={used} "
                f"+ estimated={estimate} > limit={limit}"
            ),
            http_status=status.HTTP_429_TOO_MANY_REQUESTS,
            used_tokens=used,
            budget=limit,
            limit="run_tokens",
            provider=provider,
        )
        return JSONResponse(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            content={
                "error": {
                    "message": (
                        "run token budget exceeded "
                        f"(used={used}, estimate={estimate}, limit={limit})"
                    ),
                    "type": "budget_exceeded",
                    "used_tokens": used,
                    "budget": limit,
                    "limit": "run_tokens",
                }
            },
        )

    sandbox_limit = metering.sandbox_token_budget(claims)
    sandbox_uuid = metering.uuid_or_none(claims.sandbox_id)
    if sandbox_limit is None or sandbox_uuid is None:
        return None
    sb_used = await asyncio.to_thread(_sandbox_tokens_used, run_uuid, sandbox_uuid)
    if sb_used + estimate <= sandbox_limit:
        return None
    await metering.emit_error(
        claims,
        stage="budget",
        reason="budget_exceeded",
        message=(
            f"sandbox token budget exceeded: used={sb_used} "
            f"+ estimated={estimate} > limit={sandbox_limit}"
        ),
        http_status=status.HTTP_429_TOO_MANY_REQUESTS,
        used_tokens=sb_used,
        budget=sandbox_limit,
        limit="sandbox_tokens",
        provider=provider,
    )
    return JSONResponse(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        content={
            "error": {
                "message": (
                    "sandbox token budget exceeded "
                    f"(used={sb_used}, estimate={estimate}, limit={sandbox_limit})"
                ),
                "type": "budget_exceeded",
                "used_tokens": sb_used,
                "budget": sandbox_limit,
                "limit": "sandbox_tokens",
            }
        },
    )


class _GuardedStreamingResponse(StreamingResponse):
    """StreamingResponse that always runs ``on_finalize`` when the send
    loop exits — normal completion, send failure, or task cancellation.

    The metering generator's own ``finally`` covers every exit AFTER its
    first iteration, but a client already gone when ``http.response.start``
    is sent (OSError under ASGI spec>=2.4, stream-task cancellation under
    <2.4) prevents the generator from ever starting — and an unstarted
    async generator's ``finally`` does not run, so terminal metering and
    the upstream close would silently never happen.
    """

    def __init__(
        self,
        content: Any,
        *,
        on_finalize: Callable[[], Awaitable[None]],
        **kwargs: Any,
    ) -> None:
        super().__init__(content, **kwargs)
        self._on_finalize = on_finalize

    async def stream_response(self, send: Send) -> None:
        try:
            await super().stream_response(send)
        finally:
            await self._on_finalize()


async def _passthrough(
    *,
    path: str,
    api: str,
    body: dict[str, Any],
    claims: CredClaims,
    upstream: httpx.AsyncClient,
    provider: ProviderName,
    request_headers: Mapping[str, str] | None = None,
    meter: bool = True,
    cache: ResponseCache | None = None,
) -> Response:
    """Forward ``body`` to ``upstream`` ``path`` verbatim; meter the outcome.

    Unlike ``/v1/chat/completions`` (which forces non-streaming), the caller's
    ``stream`` flag is preserved — Anthropic/Responses clients (Claude Code,
    dsh, Codex) stream by default, and a rewritten content-type would break
    their SDK parsers. Streamed bodies are tee'd: chunks pass through as they
    arrive and the captured bytes (bounded by ``_STREAM_CAPTURE_LIMIT``)
    yield the final usage for the ``llm_response`` event once the stream
    ends — provider usage when present, else a ``len/4`` estimate flagged
    ``usage_estimated``, plus an ``error`` event for streamed
    ``{"error": ...}`` frames.

    Transient upstream failures (timeouts, refused/reset, 502/503/504) are
    retried via ``_upstream_post`` before any bytes reach the client —
    mid-stream failures are terminal.

    ``provider`` is the request's resolved upstream provider (recorded on the
    emitted events); ``request_headers`` are the inbound headers — protocol
    headers the resolved provider honors (``anthropic-beta``/
    ``anthropic-version`` for ``anthropic``) are forwarded.

    ``cache`` (PR18) replays metered calls from the store on a key hit —
    the replayed body is tee'd the same way, so usage extraction and the
    emitted events are identical to a live call (payload ``cached: true``).
    Only fully-captured 2xx responses are stored; ``meter=False`` callers
    (e.g. count_tokens preflight) bypass the cache entirely.

    Client abandonment never leaves the call unaccounted: a disconnect
    while the upstream request is in flight emits a ``client_disconnected``
    -staged ``error`` event, and a disconnect mid-stream emits the partial
    ``llm_response`` (usage captured so far or ``usage_estimated``) plus
    the same error — all under a shielded cancel scope, since Starlette
    delivers the disconnect by cancelling this generator. A disconnect
    before the stream's first byte never even starts the generator, so
    ``_GuardedStreamingResponse`` guarantees that same teardown from the
    send loop's own ``finally``.
    """
    body = _strip_ollama_only(body, provider)
    if meter:
        gated = await _budget_gate(claims, body, provider)
        if gated is not None:
            return gated
        cache_key = (
            cache.key_for(path=path, provider=provider, body=body)
            if cache is not None
            else None
        )
        started = time.monotonic()
        hit = await _cache_get(cache, cache_key)
        lookup_ms = int((time.monotonic() - started) * 1000)
        await metering.emit_llm_request(
            claims, body, provider=provider, cached=hit is not None
        )
        if hit is not None:
            # Replay the stored bytes as a stream — content_type drives the
            # client's parser (SSE vs JSON), so the replay is byte-faithful.
            parsed = metering.metered_stream_usage(hit.body, api=api)
            await metering.emit_llm_response(
                claims,
                model=body.get("model"),
                usage=parsed["usage"],
                latency_ms=lookup_ms,
                http_status=hit.status,
                provider=provider,
                cached=True,
                usage_estimated=parsed["estimated"],
            )

            async def _replay():
                yield hit.body

            return StreamingResponse(
                _replay(),
                status_code=hit.status,
                headers=_cache_hit_headers(hit.content_type),
            )
    else:
        cache_key = None

    started = time.monotonic()
    try:
        result = await _upstream_post(
            upstream=upstream,
            path=path,
            body=body,
            claims=claims,
            provider=provider,
            request_headers=request_headers,
            stream=True,
            started=started,
            meter=meter,
        )
    except asyncio.CancelledError:
        await _emit_client_disconnected(
            claims,
            meter=meter,
            provider=provider,
            latency_ms=int((time.monotonic() - started) * 1000),
            message="client disconnected while awaiting upstream response",
        )
        raise
    if isinstance(result, JSONResponse):
        return result
    upstream_resp = result

    # Generator-lifecycle state shared with the response-level teardown:
    # the send loop can die before the generator's first __anext__ (client
    # already gone at http.response.start), which skips its finally.
    stream_state = {"started": False, "emitted": False}

    async def _aiter():
        stream_state["started"] = True
        # Bounded capture for metering: the client stream is unbounded, but
        # usage/output text only needs the head — past the cap the emitted
        # usage simply degrades to the estimate path.
        captured = bytearray()
        truncated = False
        complete = False
        abandoned = False
        stream_error: Exception | None = None
        try:
            async for chunk in upstream_resp.aiter_bytes():
                if meter:
                    if len(captured) < _STREAM_CAPTURE_LIMIT:
                        captured += chunk[
                            : _STREAM_CAPTURE_LIMIT - len(captured)
                        ]
                    else:
                        truncated = True
                try:
                    yield chunk
                except (GeneratorExit, asyncio.CancelledError, OSError):
                    # An exception at the yield means the DOWNSTREAM side
                    # is dead — send failed or the task was cancelled on
                    # disconnect (Starlette cancels the stream task).
                    # Upstream-read failures raise from the async-for.
                    abandoned = True
                    raise
            complete = True
        except Exception as exc:
            # Mid-stream transport failure: the body already reached the
            # client so retrying is impossible — terminal by definition.
            stream_error = exc
            raise
        except BaseException:
            # GeneratorExit/CancelledError raised on the upstream-read
            # await — the generator was closed or the task cancelled
            # mid-stream; the client is gone either way.
            abandoned = True
            raise
        finally:
            stream_state["emitted"] = True
            latency_ms = int((time.monotonic() - started) * 1000)
            # Shield the terminal events: after a client disconnect every
            # checkpoint re-raises Cancelled, which would silently drop
            # llm_response AND the error event — an unaccounted streamed
            # call whose upstream tokens were still spent.
            with anyio.CancelScope(shield=True):
                try:
                    if meter:
                        captured_b = bytes(captured)
                        parsed = metering.metered_stream_usage(
                            captured_b, api=api
                        )
                        await metering.emit_llm_response(
                            claims,
                            model=body.get("model"),
                            usage=parsed["usage"],
                            latency_ms=latency_ms,
                            http_status=upstream_resp.status_code,
                            provider=provider,
                            usage_estimated=parsed["estimated"],
                        )
                        if abandoned:
                            # Mid-stream disconnect: the llm_response
                            # above accounts the partial stream (real
                            # usage if a usage frame arrived, else the
                            # len/4 estimate); this marks the lane dead.
                            await metering.emit_error(
                                claims,
                                stage="client_disconnected",
                                message=(
                                    "client disconnected mid-stream "
                                    f"({len(captured_b)} bytes streamed)"
                                ),
                                http_status=upstream_resp.status_code,
                                latency_ms=latency_ms,
                                provider=provider,
                                usage=parsed["usage"],
                                usage_estimated=parsed["estimated"],
                            )
                        elif stream_error is not None:
                            await metering.emit_error(
                                claims,
                                stage="upstream",
                                message=(
                                    f"{type(stream_error).__name__}: "
                                    f"{stream_error}"
                                ),
                                http_status=upstream_resp.status_code,
                                latency_ms=latency_ms,
                                provider=provider,
                            )
                        elif not 200 <= upstream_resp.status_code < 300:
                            await metering.emit_error(
                                claims,
                                stage="upstream",
                                message=captured_b.decode(
                                    "utf-8", errors="replace"
                                )[:500],
                                http_status=upstream_resp.status_code,
                                latency_ms=latency_ms,
                                provider=provider,
                            )
                        else:
                            # Provider-reported mid-stream failures
                            # (OpenRouter et al. stream {"error": ...}
                            # frames under a 200).
                            for err in parsed["errors"]:
                                await metering.emit_error(
                                    claims,
                                    stage="upstream",
                                    message=err,
                                    http_status=upstream_resp.status_code,
                                    latency_ms=latency_ms,
                                    provider=provider,
                                )
                            if complete and not truncated:
                                # Cache only fully-captured 2xx bodies —
                                # partial streams (client abort, transport
                                # error, capture cap) would replay
                                # truncated output.
                                await _cache_store(
                                    cache,
                                    cache_key,
                                    status=upstream_resp.status_code,
                                    content_type=upstream_resp.headers.get(
                                        "content-type", "application/json"
                                    ),
                                    body=captured_b,
                                )
                except Exception:
                    # Metering must never break the proxy — and a raise
                    # here would reproduce the silent-terminal-event gap
                    # this whole block exists to prevent.
                    log.warning(
                        "terminal stream metering failed", exc_info=True
                    )
                finally:
                    try:
                        await upstream_resp.aclose()
                    except Exception:
                        log.warning(
                            "upstream response close failed", exc_info=True
                        )

    agen = _aiter()

    async def _stream_finalized() -> None:
        # The send loop is done — deterministically close the generator so
        # a mid-stream send failure (generator left suspended at a yield)
        # still runs its own terminal-emission finally.
        if stream_state["emitted"]:
            return
        with anyio.CancelScope(shield=True):
            try:
                await agen.aclose()
            except Exception:
                log.warning("metered stream aclose failed", exc_info=True)
            if stream_state["emitted"]:
                return
            # The generator never ran: emit the terminal pair and release
            # the upstream connection from here or the call meters nothing.
            try:
                if meter:
                    latency_ms = int((time.monotonic() - started) * 1000)
                    parsed = metering.metered_stream_usage(b"", api=api)
                    await metering.emit_llm_response(
                        claims,
                        model=body.get("model"),
                        usage=parsed["usage"],
                        latency_ms=latency_ms,
                        http_status=upstream_resp.status_code,
                        provider=provider,
                        usage_estimated=parsed["estimated"],
                    )
                    await metering.emit_error(
                        claims,
                        stage="client_disconnected",
                        message=(
                            "client disconnected before response start"
                            if not stream_state["started"]
                            else "client disconnected mid-stream"
                        ),
                        http_status=upstream_resp.status_code,
                        latency_ms=latency_ms,
                        provider=provider,
                        usage=parsed["usage"],
                        usage_estimated=parsed["estimated"],
                    )
            except Exception:
                log.warning("terminal stream metering failed", exc_info=True)
            finally:
                try:
                    await upstream_resp.aclose()
                except Exception:
                    log.warning("upstream response close failed", exc_info=True)

    return _GuardedStreamingResponse(
        agen,
        status_code=upstream_resp.status_code,
        headers={
            "content-type": upstream_resp.headers.get(
                "content-type", "application/json"
            )
        },
        on_finalize=_stream_finalized,
    )


@router.post("/v1/messages")
async def anthropic_messages(
    request: Request,
    body: Annotated[dict[str, Any], Body()],
    claims: Annotated[CredClaims, Depends(get_cred)],
    upstream: Annotated[httpx.AsyncClient, Depends(get_upstream_client)],
    provider: Annotated[ProviderName, Depends(get_provider)],
    cache: Annotated[ResponseCache | None, Depends(get_response_cache)],
) -> Response:
    """Anthropic Messages API → the resolved upstream's /v1/messages."""
    return await _passthrough(
        path="/v1/messages",
        api="anthropic",
        body=body,
        claims=claims,
        upstream=upstream,
        provider=provider,
        request_headers=request.headers,
        cache=cache,
    )


@router.post("/v1/messages/count_tokens")
async def anthropic_count_tokens(
    request: Request,
    body: Annotated[dict[str, Any], Body()],
    claims: Annotated[CredClaims, Depends(get_cred)],
    upstream: Annotated[httpx.AsyncClient, Depends(get_upstream_client)],
    provider: Annotated[ProviderName, Depends(get_provider)],
) -> Response:
    """Token-count preflight — forwarded un-metered (not a generation call)."""
    return await _passthrough(
        path="/v1/messages/count_tokens",
        api="anthropic",
        body=body,
        claims=claims,
        upstream=upstream,
        provider=provider,
        request_headers=request.headers,
        meter=False,
    )


@router.post("/v1/responses")
async def openai_responses(
    request: Request,
    body: Annotated[dict[str, Any], Body()],
    claims: Annotated[CredClaims, Depends(get_cred)],
    upstream: Annotated[httpx.AsyncClient, Depends(get_upstream_client)],
    provider: Annotated[ProviderName, Depends(get_provider)],
    cache: Annotated[ResponseCache | None, Depends(get_response_cache)],
) -> Response:
    """OpenAI Responses API → the resolved upstream's /v1/responses."""
    return await _passthrough(
        path="/v1/responses",
        api="responses",
        body=body,
        claims=claims,
        upstream=upstream,
        provider=provider,
        request_headers=request.headers,
        cache=cache,
    )


@router.get("/v1/models")
async def list_models(
    request: Request,
    claims: Annotated[CredClaims, Depends(get_cred)],
    upstream: Annotated[httpx.AsyncClient, Depends(get_upstream_client)],
    provider: Annotated[ProviderName, Depends(get_provider)],
) -> Response:
    """Model list — harness CLIs probe it; not metered.

    The ``x-heg-provider`` response header names the active upstream so the
    control plane can gate provider-only features (e.g. ollama pull).
    """
    try:
        upstream_resp = await upstream.get(
            "/v1/models",
            headers=_forward_protocol_headers(request.headers, provider),
        )
    except httpx.HTTPError as exc:
        return JSONResponse(
            status_code=status.HTTP_502_BAD_GATEWAY,
            content={"error": {"message": f"upstream request failed: {exc}"}},
        )
    return Response(
        content=upstream_resp.content,
        status_code=upstream_resp.status_code,
        headers={
            "content-type": upstream_resp.headers.get(
                "content-type", "application/json"
            ),
            "x-heg-provider": provider,
        },
    )


# Ollama's ``/api/pull`` accepts only a model selector (``name``, or
# ``model`` on newer servers) plus ``insecure``/``stream``. The pull body
# is allowlisted to the selector keys — caller-supplied ``insecure``
# (plaintext-HTTP pulls from arbitrary registries), ``stream`` (forced on
# below so progress reaches the caller), and anything else is dropped.
_PULL_ALLOWED_KEYS = ("name", "model")


@router.post("/v1/models/pull")
async def pull_model(
    body: Annotated[dict[str, Any], Body()],
    claims: Annotated[CredClaims, Depends(get_cred)],
    upstream: Annotated[httpx.AsyncClient, Depends(get_ollama_client)],
) -> Response:
    """``ollama pull`` — NDJSON progress streamed verbatim; not metered.

    Control-plane only: a pull installs onto the host's Ollama (disk +
    bandwidth, arbitrary model names/registries), so creds carrying a
    ``sandbox_id`` — every sandbox participant and evaluator cred — are
    refused with 403. Only run-scoped service creds (``sandbox_id=None``,
    minted by the api's ``/api/models/*`` routes and rubric drafting) may
    pull; ``heg-dev`` maps to a dev sandbox id and is refused like any
    sandbox-shaped cred.

    Ollama-only and pinned: hosted providers have no pull concept → 501 so
    the caller can surface "install unsupported" instead of a confusing
    upstream error, and the upstream client comes from
    ``get_ollama_client`` rather than the request-scoped dep so a smuggled
    ``model`` field can never retarget the pull under PROVIDER_ROUTES-style
    per-model routing.
    """
    if claims.sandbox_id is not None:
        return JSONResponse(
            status_code=status.HTTP_403_FORBIDDEN,
            content={
                "error": {
                    "message": (
                        "model install is a control-plane operation — "
                        "sandbox-scoped credentials may not pull"
                    ),
                    "type": "forbidden",
                }
            },
        )
    if get_settings().UPSTREAM_PROVIDER != "ollama":
        return JSONResponse(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            content={
                "error": {
                    "message": (
                        "model install is only supported for the ollama "
                        f"upstream (active: {get_settings().UPSTREAM_PROVIDER})"
                    ),
                    "type": "unsupported",
                }
            },
        )
    pull = {k: body[k] for k in _PULL_ALLOWED_KEYS if k in body}
    pull["stream"] = True
    return await _passthrough(
        path="/api/pull",
        api="openai",  # unused — meter=False
        body=pull,
        claims=claims,
        upstream=upstream,
        provider="ollama",
        meter=False,
    )


@router.post("/internal/events")
async def internal_events(
    body: SandboxIngestRequest | SandboxEvent,
    auth: Annotated[tuple[CredClaims | None, bool], Depends(get_events_auth)],
) -> JSONResponse:
    """Forward sandbox/harness events to the control-plane ingest endpoint.

    With a valid cred, run_id (and sandbox_id when the cred carries one) are
    stamped from the credential — sandboxes cannot spoof other runs.
    """
    claims, via_cred = auth
    events_in = body.events if isinstance(body, SandboxIngestRequest) else [body]

    stamped: list[dict[str, Any]] = []
    for ev in events_in:
        d = ev.model_dump(mode="json")
        if via_cred and claims is not None:
            d["run_id"] = claims.run_id
            if claims.sandbox_id is not None:
                d["sandbox_id"] = claims.sandbox_id
        stamped.append(d)

    status_code, payload = await metering.post_ingest_events(stamped)
    return JSONResponse(status_code=status_code, content=payload)


@router.post("/internal/done")
async def internal_done(
    body: DoneRequest,
    claims: Annotated[CredClaims, Depends(get_cred)],
) -> JSONResponse:
    """``done`` CLI target — emits a done_signal event for the cred's run."""
    event: dict[str, Any] = {
        "run_id": claims.run_id,
        "source": "sandbox",
        "type": "done_signal",
        "payload": {"summary": body.summary, "status": body.status},
    }
    if claims.sandbox_id is not None:
        event["sandbox_id"] = claims.sandbox_id
    status_code, payload = await metering.post_ingest_events([event])
    return JSONResponse(status_code=status_code, content=payload)


# ---------------------------------------------------------------------------
# app factory
# ---------------------------------------------------------------------------


def create_app() -> FastAPI:
    get_settings()  # fail fast on bad env config
    app = FastAPI(title="harness_eval_gateway (AI gateway)", version="0.1.0")

    # Sandboxes and local tooling may call the gateway directly — allow all.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(router)
    return app


app = create_app()
