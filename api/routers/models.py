"""PR1 — model catalog endpoints: submit-form picker + ``ollama pull``.

``GET /api/models`` proxies the gateway's ``GET /v1/models`` (the only path
to the upstream) and normalizes the OpenAI-shaped ``data[]`` list into
``ModelsOut`` — the submit form's live model suggestions. The gateway tags
the response ``x-heg-provider`` so the UI only offers install when the
upstream can actually pull (``provider == "ollama"``).

``POST /api/models/pull`` streams ``ollama pull`` progress: api → gateway
``POST /v1/models/pull`` → upstream ``/api/pull``, NDJSON lines passed
through verbatim so the UI renders live progress. Non-ollama providers 501
at the gateway; the error detail is forwarded to the caller.

Auth to the gateway uses a short-lived minted cred (run-scoped placeholder —
neither endpoint is metered, so no real run_id is needed).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from typing import Annotated, Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from api.config import get_settings
from gateway.creds import mint_cred

router = APIRouter(tags=["models"])

# Placeholder run scope for the minted api→gateway cred — /v1/models and
# /v1/models/pull are not metered, so claims only need to verify.
_MODELS_RUN_ID = uuid.UUID(int=0)

# Pull downloads can run for a long time between progress lines; bound the
# per-read wait at 30 min (connect/write stay tight).
_PULL_TIMEOUT = httpx.Timeout(10.0, read=1800.0)


async def get_gateway_client() -> AsyncIterator[httpx.AsyncClient]:
    """Injectable gateway client (mirrors gateway.get_upstream_client)."""
    async with httpx.AsyncClient(
        base_url=get_settings().GATEWAY_URL, timeout=15.0
    ) as client:
        yield client


GatewayClient = Annotated[httpx.AsyncClient, Depends(get_gateway_client)]


class ModelInfo(BaseModel):
    """One upstream model entry (OpenAI ``/v1/models`` shape)."""

    model_config = ConfigDict(extra="allow")

    id: str


class ModelsOut(BaseModel):
    """GET /api/models — normalized model catalog for the submit form."""

    models: list[ModelInfo] = Field(default_factory=list)
    # Gateway's UPSTREAM_PROVIDER (``x-heg-provider`` header); None on older
    # gateways → pull_supported stays False.
    provider: str | None = None
    pull_supported: bool = False


class ModelPullIn(BaseModel):
    """POST /api/models/pull body — model name/tag to install via ollama."""

    name: str = Field(min_length=1, max_length=255)


def _service_cred(secret: str) -> str:
    """Short-lived cred for api→gateway meta calls (unmetered endpoints)."""
    return mint_cred(
        run_id=_MODELS_RUN_ID,
        sandbox_id=None,
        secret=secret,
        ttl_seconds=120,
    )


async def _error_message(resp: httpx.Response) -> str:
    """Best-effort message extraction for forwarded gateway/upstream errors."""
    try:
        raw = await resp.aread()
    except httpx.HTTPError:
        raw = b""
    try:
        body: Any = json.loads(raw)
    except ValueError:
        body = None
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            return error["message"]
        detail = body.get("detail")
        if isinstance(detail, str) and detail:
            return detail
        return json.dumps(body)[:300]
    return raw.decode("utf-8", errors="replace")[:300] or f"HTTP {resp.status_code}"


@router.get("/api/models", response_model=ModelsOut)
async def list_models(client: GatewayClient) -> ModelsOut:
    """Live model catalog — gateway ``/v1/models`` normalized for the UI."""
    settings = get_settings()
    try:
        resp = await client.get(
            "/v1/models",
            headers={
                "Authorization": f"Bearer {_service_cred(settings.GATEWAY_CRED_SECRET)}"
            },
        )
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"cannot reach gateway at {settings.GATEWAY_URL}: {exc}",
        ) from exc
    if resp.status_code != status.HTTP_200_OK:
        raise HTTPException(
            status_code=resp.status_code, detail=await _error_message(resp)
        )
    try:
        data = resp.json()
    except ValueError:
        data = {}
    raw = data.get("data") if isinstance(data, dict) else None
    models = [
        ModelInfo.model_validate(m)
        for m in (raw if isinstance(raw, list) else [])
        if isinstance(m, dict) and isinstance(m.get("id"), str) and m["id"]
    ]
    provider = resp.headers.get("x-heg-provider")
    return ModelsOut(
        models=models, provider=provider, pull_supported=provider == "ollama"
    )


@router.post("/api/models/pull")
async def pull_model(body: ModelPullIn, client: GatewayClient) -> StreamingResponse:
    """Stream an ``ollama pull`` through the gateway (NDJSON progress lines)."""
    settings = get_settings()
    req = client.build_request(
        "POST",
        "/v1/models/pull",
        json={"name": body.name, "stream": True},
        headers={
            "Authorization": f"Bearer {_service_cred(settings.GATEWAY_CRED_SECRET)}",
            "Accept": "application/x-ndjson",
        },
        timeout=_PULL_TIMEOUT,
    )
    try:
        resp = await client.send(req, stream=True)
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"cannot reach gateway at {settings.GATEWAY_URL}: {exc}",
        ) from exc
    if resp.status_code != status.HTTP_200_OK:
        try:
            detail = await _error_message(resp)
        finally:
            await resp.aclose()
        raise HTTPException(status_code=resp.status_code, detail=detail)

    async def _aiter() -> AsyncIterator[bytes]:
        try:
            async for chunk in resp.aiter_bytes():
                yield chunk
        finally:
            await resp.aclose()

    return StreamingResponse(_aiter(), media_type="application/x-ndjson")
