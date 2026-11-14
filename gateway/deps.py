"""Shared FastAPI dependencies for the gateway app.

``get_db`` — SQLAlchemy session for budget lookups (events table).
``get_upstream_client`` — httpx.AsyncClient pointed at the OpenAI-compatible
upstream (Ollama); tests override this dependency with a MockTransport.
``get_ollama_client`` — same, pinned to Ollama regardless of provider
routing (``/v1/models/pull`` only).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator

import httpx
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


async def get_upstream_client() -> AsyncIterator[httpx.AsyncClient]:
    settings = get_settings()
    async with httpx.AsyncClient(
        base_url=settings.upstream_base_url,
        headers=settings.upstream_headers,
        timeout=settings.UPSTREAM_TIMEOUT,
    ) as client:
        yield client


async def get_ollama_client() -> AsyncIterator[httpx.AsyncClient]:
    """Upstream client pinned to Ollama, bypassing provider routing.

    ``/v1/models/pull`` is an Ollama host capability — it must never follow
    per-model provider routing (a smuggled ``model`` field in the pull body
    would otherwise retarget a routed dep to a provider that has no pull
    API).
    """
    settings = get_settings()
    async with httpx.AsyncClient(
        base_url=settings.OLLAMA_UPSTREAM_URL.rstrip("/"),
        timeout=settings.UPSTREAM_TIMEOUT,
    ) as client:
        yield client
