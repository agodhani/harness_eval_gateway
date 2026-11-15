"""Shared FastAPI dependencies for the gateway app.

``get_db`` — SQLAlchemy session for budget lookups (events table).
``get_cred`` / ``get_events_auth`` — ``heg.*``/``heg-dev`` credential auth
(``/internal/events`` additionally accepts the static ``INGEST_TOKEN``).
``get_provider`` — the request's resolved upstream provider
(``x-heg-provider`` header → ``PROVIDER_ROUTES`` model globs →
``UPSTREAM_PROVIDER``); one resolution shared by the upstream client,
body rewriting, and event attribution via FastAPI's per-request dep cache.
``get_upstream_client`` — httpx.AsyncClient pointed at that resolved
upstream; tests override this dependency with a MockTransport.
"""

from __future__ import annotations

import logging
import secrets
from collections.abc import AsyncIterator, Iterator
from typing import Annotated, Any

import httpx
from fastapi import Body, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session, sessionmaker

from db.session import make_engine, make_session_factory
from gateway.config import ProviderName, get_settings, require_provider
from gateway.creds import CredClaims, CredError, verify_cred

log = logging.getLogger(__name__)

_engine = None
_session_factory: sessionmaker[Session] | None = None


def get_session_factory() -> sessionmaker[Session]:
    """Lazily build engine + session factory from gateway settings."""
    global _engine, _session_factory
    if _session_factory is None:
        _engine = make_engine(get_settings().DATABASE_URL)
        _session_factory = make_session_factory(_engine)
    return _session_factory


def get_db() -> Iterator[Session]:
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.close()


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
# upstream provider resolution
# ---------------------------------------------------------------------------


def get_provider(
    request: Request,
    claims: Annotated[CredClaims, Depends(get_cred)],
    body: Annotated[dict[str, Any] | None, Body()] = None,
) -> ProviderName:
    """The request's resolved upstream provider.

    An ``x-heg-provider`` header forces a provider outright — but only for
    SERVICE creds (``claims.sandbox_id is None``: run-level control-plane
    creds like rubric drafting / minted operator creds). Sandbox-scoped
    creds (``sandbox_id`` set — every participant and evaluator cred,
    ``heg-dev`` included) get NO override: the header is ignored silently
    so agent-controlled code inside a sandbox can't retarget its model
    traffic onto paid upstreams keyed by the operator.

    For honored overrides an unknown value 400s rather than silently
    falling back — and so does a valid provider with no configured API
    key (it would otherwise resolve then 401 upstream).

    Otherwise ``PROVIDER_ROUTES`` matches ``body["model"]`` (``None`` body —
    GETs like ``/v1/models``, or direct calls — resolves the process-wide
    ``UPSTREAM_PROVIDER``).

    WARNING — param-name coupling: FastAPI dedupes request-body params by
    NAME across the endpoint and its dependencies, so this ``body`` is only
    populated because every endpoint also names its own body param ``body``.
    Renaming either side silently breaks the merge (all proxied POSTs would
    422 — or the dep sees ``body=None`` and routes to the default provider).
    ``TestRealDepRouting.test_post_routes_through_real_dep`` exercises the
    REAL dep chain end-to-end to guard this.
    """
    override = request.headers.get("x-heg-provider")
    if override is not None:
        if claims.sandbox_id is None:
            try:
                provider = require_provider(override)
            except ValueError as exc:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
                ) from exc
            if not get_settings().provider_keyed(provider):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=(
                        f"x-heg-provider {provider!r} is not configured on "
                        "this gateway (no API key set)"
                    ),
                )
            return provider
        log.debug(
            "x-heg-provider %r ignored: cred is sandbox-scoped "
            "(run=%s sandbox=%s)",
            override,
            claims.run_id,
            claims.sandbox_id,
        )
    return get_settings().provider_for_model((body or {}).get("model"))


async def get_upstream_client(
    provider: Annotated[ProviderName, Depends(get_provider)],
) -> AsyncIterator[httpx.AsyncClient]:
    """Per-request upstream client for the resolved provider."""
    settings = get_settings()
    async with httpx.AsyncClient(
        base_url=settings.upstream_base_url_for(provider),
        headers=settings.upstream_headers_for(provider),
        timeout=settings.UPSTREAM_TIMEOUT,
    ) as client:
        yield client
