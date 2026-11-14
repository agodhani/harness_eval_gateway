"""Shared FastAPI dependencies for the gateway app.

``get_db`` — SQLAlchemy session for budget lookups (events table).
``get_provider`` — the request's resolved upstream provider
(``x-heg-provider`` header → ``PROVIDER_ROUTES`` model globs →
``UPSTREAM_PROVIDER``); one resolution shared by the upstream client,
body rewriting, and event attribution via FastAPI's per-request dep cache.
``get_upstream_client`` — httpx.AsyncClient pointed at that resolved
upstream; tests override this dependency with a MockTransport.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from typing import Annotated, Any

import httpx
from fastapi import Body, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session, sessionmaker

from db.session import make_engine, make_session_factory
from gateway.config import ProviderName, get_settings, require_provider

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


def get_provider(
    request: Request,
    body: Annotated[dict[str, Any] | None, Body()] = None,
) -> ProviderName:
    """The request's resolved upstream provider.

    An ``x-heg-provider`` header forces a provider outright — an unknown
    value 400s rather than silently falling back to model routing.
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
        try:
            return require_provider(override)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
            ) from exc
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
