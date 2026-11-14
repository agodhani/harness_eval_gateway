"""Shared FastAPI dependencies for the gateway app.

``get_db`` — SQLAlchemy session for budget lookups (events table).
``get_upstream_client`` — httpx.AsyncClient pointed at the upstream resolved
for the request's model (the process-wide ``UPSTREAM_PROVIDER``, overridden
by ``PROVIDER_ROUTES`` globs); tests override this dependency with a
MockTransport.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from typing import Annotated, Any

import httpx
from fastapi import Body
from sqlalchemy.orm import Session, sessionmaker

from db.session import make_engine, make_session_factory
from gateway.config import get_settings

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


async def get_upstream_client(
    body: Annotated[dict[str, Any] | None, Body()] = None,
) -> AsyncIterator[httpx.AsyncClient]:
    """Per-request upstream client.

    ``body`` carries the caller's ``model`` for PROVIDER_ROUTES per-model
    routing; ``None`` (GETs like ``/v1/models``, or direct calls) resolves
    the process-wide ``UPSTREAM_PROVIDER``.

    WARNING — param-name coupling: FastAPI dedupes request-body params by
    NAME across the endpoint and its dependencies, so this ``body`` is only
    populated because every endpoint also names its own body param ``body``.
    Renaming either side silently breaks the merge (all proxied POSTs would
    422 — or the dep sees ``body=None`` and routes to the default provider).
    ``TestRealDepRouting.test_post_routes_through_real_dep`` exercises the
    REAL dep end-to-end to guard this.
    """
    settings = get_settings()
    provider = settings.provider_for_model((body or {}).get("model"))
    async with httpx.AsyncClient(
        base_url=settings.upstream_base_url_for(provider),
        headers=settings.upstream_headers_for(provider),
        timeout=settings.UPSTREAM_TIMEOUT,
    ) as client:
        yield client
