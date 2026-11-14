"""Shared FastAPI dependencies for the gateway app.

``get_db`` — SQLAlchemy session for budget lookups (events table).
``get_upstream_client`` — httpx.AsyncClient pointed at the upstream resolved
for the request's model (the process-wide ``UPSTREAM_PROVIDER``, overridden
by ``PROVIDER_ROUTES`` globs); tests override this dependency with a
MockTransport.
``get_response_cache`` — the opt-in replay cache (PR18); ``None`` unless
``GATEWAY_CACHE_ENABLED``. Tests override it to inject a tmp-path store.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from typing import Annotated, Any

import httpx
from fastapi import Body
from sqlalchemy.orm import Session, sessionmaker

from db.session import make_engine, make_session_factory
from gateway.cache import ResponseCache
from gateway.config import get_settings

_engine = None
_session_factory: sessionmaker[Session] | None = None
_response_cache: ResponseCache | None = None


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


def get_response_cache() -> ResponseCache | None:
    """The process-wide replay cache, or ``None`` when disabled.

    Lazily built from settings on first use (like ``get_session_factory``);
    ``GATEWAY_CACHE_PATH``/``_MAX_ENTRIES``/``_NAMESPACE`` configure it.
    """
    global _response_cache
    settings = get_settings()
    if not settings.GATEWAY_CACHE_ENABLED:
        return None
    if _response_cache is None:
        _response_cache = ResponseCache(
            settings.GATEWAY_CACHE_PATH,
            max_entries=settings.GATEWAY_CACHE_MAX_ENTRIES,
            namespace=settings.GATEWAY_CACHE_NAMESPACE,
        )
    return _response_cache


def reset_response_cache() -> None:
    """Drop the singleton (tests toggling settings between cases)."""
    global _response_cache
    if _response_cache is not None:
        _response_cache.close()
        _response_cache = None


async def get_upstream_client(
    body: Annotated[dict[str, Any] | None, Body()] = None,
) -> AsyncIterator[httpx.AsyncClient]:
    """Per-request upstream client.

    ``body`` carries the caller's ``model`` for PROVIDER_ROUTES per-model
    routing; ``None`` (GETs like ``/v1/models``, or direct calls) resolves
    the process-wide ``UPSTREAM_PROVIDER``.
    """
    settings = get_settings()
    provider = settings.provider_for_model((body or {}).get("model"))
    async with httpx.AsyncClient(
        base_url=settings.upstream_base_url_for(provider),
        headers=settings.upstream_headers_for(provider),
        timeout=settings.UPSTREAM_TIMEOUT,
    ) as client:
        yield client
