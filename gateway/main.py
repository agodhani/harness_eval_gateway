"""F3 — AI gateway: OpenAI-compatible proxy with metering, budgets, creds.

Endpoints
---------
``GET  /health``                  — liveness.
``POST /v1/chat/completions``     — Bearer ``heg.*`` cred → budget gate →
                                    forward to Ollama upstream; ``stream:
                                    true`` callers get a metered SSE
                                    pass-through, others a non-streamed
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
                                    upstreams.
``POST /internal/events``         — sandbox-facing event ingest (cred-stamped
                                    run/sandbox ids) → forwards to the
                                    control-plane ``/api/events/ingest``.
``POST /internal/done``           — sandbox ``done`` CLI target; emits a
                                    ``done_signal`` event for the cred's
                                    run/sandbox.

Auth: ``Authorization: Bearer <cred>`` (or Anthropic-style ``x-api-key``)
where cred is ``heg-dev`` (static dev credential) or
``heg.<b64url-payload>.<b64url-hmac>`` (see gateway/creds.py).
``/internal/events`` additionally accepts ``Bearer <INGEST_TOKEN>`` as a
pass-through credential for producers that already carry run/sandbox ids —
this matches the ingest token sandboxes may be configured with.
"""

from __future__ import annotations

import asyncio
import secrets
import time
import uuid
from datetime import datetime
from typing import Annotated, Any

import httpx
from fastapi import APIRouter, Body, Depends, FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

from api.schemas import EventSource, EventType
from gateway import metering
from gateway.config import get_settings
from gateway.creds import CredClaims, CredError, verify_cred
from gateway.deps import get_session_factory, get_upstream_client

router = APIRouter()


# ---------------------------------------------------------------------------
# auth
# ---------------------------------------------------------------------------


def _bearer_token(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    scheme, _, token = auth.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing Bearer credential",
        )
    return token.strip()


def _credential_token(request: Request) -> str:
    """Bearer token, falling back to Anthropic-style ``x-api-key``.

    Anthropic-SDK clients (dsh's DeepSeek Messages adapter) send the
    credential as ``x-api-key`` rather than ``Authorization: Bearer``.
    """
    auth = request.headers.get("authorization", "")
    scheme, _, token = auth.partition(" ")
    if scheme.lower() == "bearer" and token.strip():
        return token.strip()
    api_key = request.headers.get("x-api-key", "").strip()
    if api_key:
        return api_key
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="missing Bearer credential",
    )


def get_cred(request: Request) -> CredClaims:
    """Strict credential auth — used by the proxy and /internal/done."""
    settings = get_settings()
    try:
        return verify_cred(
            _credential_token(request), secret=settings.GATEWAY_CRED_SECRET
        )
    except CredError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"invalid credential: {exc}",
        ) from exc


def get_events_auth(request: Request) -> tuple[CredClaims | None, bool]:
    """Auth for /internal/events.

    Returns ``(claims, via_cred)``. A valid ``heg.*``/``heg-dev`` credential
    stamps run/sandbox ids onto every event; the static ``INGEST_TOKEN``
    bearer is accepted as pass-through for producers that carry their own
    run_id/sandbox_id (the api ingest validates them).
    """
    settings = get_settings()
    token = _bearer_token(request)
    try:
        return (
            verify_cred(token, secret=settings.GATEWAY_CRED_SECRET),
            True,
        )
    except CredError:
        pass
    if secrets.compare_digest(token, settings.INGEST_TOKEN):
        return None, False
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="invalid credential",
    )


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


# Body params only Ollama understands — harnesses tuned for Ollama (miniloop's
# `think`/`num_ctx`) emit them; hosted providers may 400 on unknown fields.
_OLLAMA_ONLY_KEYS = ("num_ctx", "keep_alive", "think", "options")


def _strip_ollama_only(body: dict[str, Any]) -> dict[str, Any]:
    if get_settings().inject_ollama_defaults:
        return body
    return {k: v for k, v in body.items() if k not in _OLLAMA_ONLY_KEYS}


@router.post("/v1/chat/completions")
async def chat_completions(
    body: Annotated[dict[str, Any], Body()],
    claims: Annotated[CredClaims, Depends(get_cred)],
    upstream: Annotated[httpx.AsyncClient, Depends(get_upstream_client)],
) -> Response:
    # --- streaming callers (pi and other OpenAI-compatible CLIs always set
    # stream:true and parse SSE — a forced non-streamed JSON body reads as an
    # empty stream to them): pass through with a metering tee. The shared
    # passthrough applies the budget gate + llm_request emit, so this branch
    # must run before the non-streamed gate below.
    if body.get("stream"):
        out = _strip_ollama_only(dict(body))
        # Ask the upstream for a terminal usage chunk so streamed calls stay
        # metered (no-op for upstreams that ignore it).
        stream_options = dict(out.get("stream_options") or {})
        stream_options.setdefault("include_usage", True)
        out["stream_options"] = stream_options
        if get_settings().inject_ollama_defaults:
            out.setdefault("num_ctx", 32_768)
            out.setdefault("keep_alive", "30m")
        return await _passthrough(
            path="/v1/chat/completions",
            api="openai",
            body=out,
            claims=claims,
            upstream=upstream,
        )

    # --- budget gate (before any upstream traffic) ---
    gated = await _budget_gate(claims, body)
    if gated is not None:
        return gated

    # --- prepare upstream request: force non-streaming, inject defaults ---
    out = _strip_ollama_only(dict(body))
    out["stream"] = False
    if get_settings().inject_ollama_defaults:
        out.setdefault("num_ctx", 32_768)
        out.setdefault("keep_alive", "30m")

    await metering.emit_llm_request(claims, out)

    started = time.monotonic()
    try:
        upstream_resp = await upstream.post("/v1/chat/completions", json=out)
    except httpx.HTTPError as exc:
        latency_ms = int((time.monotonic() - started) * 1000)
        await metering.emit_error(
            claims,
            stage="upstream",
            message=f"{type(exc).__name__}: {exc}",
            latency_ms=latency_ms,
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
    latency_ms = int((time.monotonic() - started) * 1000)

    # --- meter the outcome; always return upstream verbatim ---
    try:
        response_json = upstream_resp.json()
    except Exception:  # noqa: BLE001 - non-JSON upstream body
        response_json = None
    usage = metering.usage_from_response(response_json)
    await metering.emit_llm_response(
        claims,
        model=out.get("model"),
        usage=usage,
        latency_ms=latency_ms,
        http_status=upstream_resp.status_code,
    )
    if not 200 <= upstream_resp.status_code < 300:
        await metering.emit_error(
            claims,
            stage="upstream",
            message=upstream_resp.text[:500],
            http_status=upstream_resp.status_code,
            latency_ms=latency_ms,
        )

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


async def _budget_gate(claims: CredClaims, body: dict[str, Any]) -> JSONResponse | None:
    """Shared run-token-budget gate; returns a 429 response or None (pass)."""
    settings = get_settings()
    run_uuid = metering.uuid_or_none(claims.run_id)
    if run_uuid is None:
        return None
    used = await asyncio.to_thread(_tokens_used, run_uuid)
    estimate = metering.max_tokens_estimate(body)
    limit = metering.token_budget(claims, settings)
    if used + estimate <= limit:
        return None
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
            }
        },
    )


async def _passthrough(
    *,
    path: str,
    api: str,
    body: dict[str, Any],
    claims: CredClaims,
    upstream: httpx.AsyncClient,
    meter: bool = True,
) -> Response:
    """Forward ``body`` to ``upstream`` ``path`` verbatim; meter the outcome.

    Unlike ``/v1/chat/completions`` (which forces non-streaming), the caller's
    ``stream`` flag is preserved — Anthropic/Responses clients (Claude Code,
    dsh, Codex) stream by default, and a rewritten content-type would break
    their SDK parsers. Streamed bodies are tee'd: chunks pass through as they
    arrive and the captured bytes yield the final usage for the
    ``llm_response`` event once the stream ends.
    """
    if meter:
        gated = await _budget_gate(claims, body)
        if gated is not None:
            return gated
        await metering.emit_llm_request(claims, body)

    started = time.monotonic()
    try:
        req = upstream.build_request("POST", path, json=body)
        upstream_resp = await upstream.send(req, stream=True)
    except httpx.HTTPError as exc:
        latency_ms = int((time.monotonic() - started) * 1000)
        if meter:
            await metering.emit_error(
                claims,
                stage="upstream",
                message=f"{type(exc).__name__}: {exc}",
                latency_ms=latency_ms,
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

    async def _aiter():
        chunks: list[bytes] = []
        try:
            async for chunk in upstream_resp.aiter_bytes():
                if meter:
                    chunks.append(chunk)
                yield chunk
        finally:
            latency_ms = int((time.monotonic() - started) * 1000)
            try:
                if meter:
                    captured = b"".join(chunks)
                    usage = metering.usage_from_sse(captured, api=api)
                    await metering.emit_llm_response(
                        claims,
                        model=body.get("model"),
                        usage=usage,
                        latency_ms=latency_ms,
                        http_status=upstream_resp.status_code,
                    )
                    if not 200 <= upstream_resp.status_code < 300:
                        await metering.emit_error(
                            claims,
                            stage="upstream",
                            message=captured.decode("utf-8", errors="replace")[:500],
                            http_status=upstream_resp.status_code,
                            latency_ms=latency_ms,
                        )
            finally:
                await upstream_resp.aclose()

    return StreamingResponse(
        _aiter(),
        status_code=upstream_resp.status_code,
        headers={
            "content-type": upstream_resp.headers.get(
                "content-type", "application/json"
            )
        },
    )


@router.post("/v1/messages")
async def anthropic_messages(
    body: Annotated[dict[str, Any], Body()],
    claims: Annotated[CredClaims, Depends(get_cred)],
    upstream: Annotated[httpx.AsyncClient, Depends(get_upstream_client)],
) -> Response:
    """Anthropic Messages API → Ollama's native /v1/messages (Claude, dsh)."""
    return await _passthrough(
        path="/v1/messages",
        api="anthropic",
        body=body,
        claims=claims,
        upstream=upstream,
    )


@router.post("/v1/messages/count_tokens")
async def anthropic_count_tokens(
    body: Annotated[dict[str, Any], Body()],
    claims: Annotated[CredClaims, Depends(get_cred)],
    upstream: Annotated[httpx.AsyncClient, Depends(get_upstream_client)],
) -> Response:
    """Token-count preflight — forwarded un-metered (not a generation call)."""
    return await _passthrough(
        path="/v1/messages/count_tokens",
        api="anthropic",
        body=body,
        claims=claims,
        upstream=upstream,
        meter=False,
    )


@router.post("/v1/responses")
async def openai_responses(
    body: Annotated[dict[str, Any], Body()],
    claims: Annotated[CredClaims, Depends(get_cred)],
    upstream: Annotated[httpx.AsyncClient, Depends(get_upstream_client)],
) -> Response:
    """OpenAI Responses API → Ollama's native /v1/responses (Codex CLI)."""
    return await _passthrough(
        path="/v1/responses",
        api="responses",
        body=body,
        claims=claims,
        upstream=upstream,
    )


@router.get("/v1/models")
async def list_models(
    claims: Annotated[CredClaims, Depends(get_cred)],
    upstream: Annotated[httpx.AsyncClient, Depends(get_upstream_client)],
) -> Response:
    """Model list — harness CLIs probe it; not metered.

    The ``x-heg-provider`` response header names the active upstream so the
    control plane can gate provider-only features (e.g. ollama pull).
    """
    try:
        upstream_resp = await upstream.get("/v1/models")
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
            "x-heg-provider": get_settings().UPSTREAM_PROVIDER,
        },
    )


@router.post("/v1/models/pull")
async def pull_model(
    body: Annotated[dict[str, Any], Body()],
    claims: Annotated[CredClaims, Depends(get_cred)],
    upstream: Annotated[httpx.AsyncClient, Depends(get_upstream_client)],
) -> Response:
    """``ollama pull`` — NDJSON progress streamed verbatim; not metered.

    Ollama-only: hosted providers have no pull concept → 501 so the caller
    can surface "install unsupported" instead of a confusing upstream error.
    """
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
    return await _passthrough(
        path="/api/pull",
        api="openai",  # unused — meter=False
        body={**body, "stream": True},
        claims=claims,
        upstream=upstream,
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
