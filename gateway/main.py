"""F3 — AI gateway: OpenAI-compatible proxy with metering, budgets, creds.

Endpoints
---------
``GET  /health``                  — liveness.
``POST /v1/chat/completions``     — Bearer ``heg.*`` cred → budget gate →
                                    forward to Ollama upstream (non-streaming)
                                    → emit ``llm_request``/``llm_response``/
                                    ``error`` events → return upstream
                                    body+status verbatim.
``POST /internal/events``         — sandbox-facing event ingest (cred-stamped
                                    run/sandbox ids) → forwards to the
                                    control-plane ``/api/events/ingest``.
``POST /internal/done``           — sandbox ``done`` CLI target; emits a
                                    ``done_signal`` event for the cred's
                                    run/sandbox.

Auth: ``Authorization: Bearer <cred>`` where cred is ``heg-dev`` (static dev
credential) or ``heg.<b64url-payload>.<b64url-hmac>`` (see gateway/creds.py).
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
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from api.schemas import EventSource, EventType
from gateway import metering
from gateway.config import get_settings
from gateway.creds import CredClaims, CredError, verify_cred
from gateway.deps import get_db, get_upstream_client

router = APIRouter()

DbSession = Annotated[Session, Depends(get_db)]


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


def get_cred(request: Request) -> CredClaims:
    """Strict credential auth — used by the proxy and /internal/done."""
    settings = get_settings()
    try:
        return verify_cred(
            _bearer_token(request), secret=settings.GATEWAY_CRED_SECRET
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


@router.post("/v1/chat/completions")
async def chat_completions(
    body: Annotated[dict[str, Any], Body()],
    claims: Annotated[CredClaims, Depends(get_cred)],
    session: DbSession,
    upstream: Annotated[httpx.AsyncClient, Depends(get_upstream_client)],
) -> Response:
    settings = get_settings()

    # --- budget gate (before any upstream traffic) ---
    run_uuid = metering.uuid_or_none(claims.run_id)
    if run_uuid is not None:
        used = await asyncio.to_thread(metering.tokens_used, session, run_uuid)
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

    # --- prepare upstream request: force non-streaming, inject defaults ---
    out = dict(body)
    out["stream"] = False
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
