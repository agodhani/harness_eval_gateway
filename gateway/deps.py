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
``get_ollama_client`` — same, pinned to Ollama regardless of provider
routing (``/v1/models/pull`` only).
``get_response_cache`` — the opt-in replay cache (PR18); ``None`` unless
``GATEWAY_CACHE_ENABLED``. Tests override it to inject a tmp-path store.
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
from gateway.cache import ResponseCache
from gateway.config import ProviderName, get_settings, require_provider
from gateway.creds import CredClaims, CredError, verify_cred

log = logging.getLogger(__name__)

_engine = None
_session_factory: sessionmaker[Session] | None = None
_response_cache: ResponseCache | None = None
_cache_failed = False


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
# response cache (PR18)
# ---------------------------------------------------------------------------


def get_response_cache() -> ResponseCache | None:
    """The process-wide replay cache, or ``None`` when disabled.

    Lazily built from settings on first use (like ``get_session_factory``);
    ``GATEWAY_CACHE_PATH``/``_MAX_ENTRIES``/``_NAMESPACE`` configure it.
    """
    global _response_cache, _cache_failed
    settings = get_settings()
    if not settings.GATEWAY_CACHE_ENABLED:
        return None
    if _response_cache is None and not _cache_failed:
        try:
            _response_cache = ResponseCache(
                settings.GATEWAY_CACHE_PATH,
                max_entries=settings.GATEWAY_CACHE_MAX_ENTRIES,
                namespace=settings.GATEWAY_CACHE_NAMESPACE,
            )
        except Exception:
            _cache_failed = True
            log.warning(
                "response cache init failed; replay cache disabled",
                exc_info=True,
            )
    return _response_cache


def reset_response_cache() -> None:
    """Drop the singleton (tests toggling settings between cases)."""
    global _response_cache, _cache_failed
    _cache_failed = False
    if _response_cache is not None:
        _response_cache.close()
        _response_cache = None


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
        timeout=settings.UPSTREAM_TIMEOUT_SECONDS,
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
        timeout=settings.UPSTREAM_TIMEOUT_SECONDS,
    ) as client:
        yield client
