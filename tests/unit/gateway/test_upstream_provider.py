"""Upstream provider selection + routing — config, deps, and client wiring.

Covers the four providers (ollama | openrouter | openai | anthropic),
PROVIDER_ROUTES model-glob resolution, the ``x-heg-provider`` request-header
override (service creds only — ignored on sandbox-scoped creds), and
protocol-header forwarding to the anthropic upstream.

Hermetic: settings are constructed directly (env file reads disabled), the
upstream is an httpx.MockTransport injected via dependency override, and
ingest emission is monkeypatched — no live upstream/api.
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Self

import httpx
import pytest
from fastapi import HTTPException, Request
from fastapi.testclient import TestClient
from pydantic import ValidationError

from gateway import deps, main, metering
from gateway.config import GatewaySettings
from gateway.creds import DEV_CRED_TOKEN, CredClaims, mint_cred
from gateway.deps import get_upstream_client

_UPSTREAM_OK = {
    "id": "chatcmpl-test",
    "object": "chat.completion",
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "hello"},
            "finish_reason": "stop",
        }
    ],
    "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
}

# Claims for direct ``deps.get_provider`` calls: sandbox-shaped (like every
# participant/evaluator cred — and ``heg-dev``) vs run-scoped service creds.
_SANDBOX_CLAIMS = CredClaims(
    run_id="run", sandbox_id="sandbox", iat=0, exp=2**31 - 1
)
_SERVICE_CLAIMS = CredClaims(run_id="run", sandbox_id=None, iat=0, exp=2**31 - 1)


def _service_cred() -> str:
    """Mint a run-scoped SERVICE cred (``sandbox_id=None``) keyed to whatever
    settings the gateway's auth currently resolves."""
    return mint_cred(
        run_id=uuid.uuid4(),
        sandbox_id=None,
        secret=deps.get_settings().GATEWAY_CRED_SECRET,
        ttl_seconds=3600,
    )


def _sandbox_cred() -> str:
    """Mint a sandbox-scoped participant cred."""
    return mint_cred(
        run_id=uuid.uuid4(),
        sandbox_id=uuid.uuid4(),
        secret=deps.get_settings().GATEWAY_CRED_SECRET,
        ttl_seconds=3600,
    )


def _settings(**kwargs) -> GatewaySettings:
    kwargs.setdefault("_env_file", None)
    return GatewaySettings(**kwargs)


def _request(headers: dict[str, str] | None = None) -> Request:
    """Minimal starlette Request carrying the given headers."""
    return Request(
        {
            "type": "http",
            "method": "POST",
            "headers": [
                (k.lower().encode(), v.encode())
                for k, v in (headers or {}).items()
            ],
        }
    )


class TestSettings:
    def test_default_provider_is_ollama(self) -> None:
        s = _settings()
        assert s.UPSTREAM_PROVIDER == "ollama"
        assert s.upstream_base_url == "http://host.docker.internal:11434"
        assert s.upstream_headers == {}
        assert s.inject_ollama_defaults is True

    def test_openrouter_base_url_and_auth_header(self) -> None:
        s = _settings(UPSTREAM_PROVIDER="openrouter", OPENROUTER_API_KEY="sk-or-k")
        # Client base is the API root — request paths carry "/v1/..." and
        # httpx concatenates paths verbatim ("/api/" + "v1/x" = "/api/v1/x").
        assert s.upstream_base_url == "https://openrouter.ai/api/"
        assert s.upstream_headers == {"Authorization": "Bearer sk-or-k"}
        assert s.inject_ollama_defaults is False

    def test_openrouter_base_url_trailing_slash_stripped(self) -> None:
        s = _settings(
            UPSTREAM_PROVIDER="openrouter",
            OPENROUTER_API_KEY="k",
            OPENROUTER_BASE_URL="https://openrouter.ai/api/v1/",
        )
        assert s.upstream_base_url == "https://openrouter.ai/api/"

    def test_openrouter_requires_api_key(self) -> None:
        with pytest.raises(ValidationError, match="OPENROUTER_API_KEY"):
            _settings(UPSTREAM_PROVIDER="openrouter")

    def test_openai_base_url_and_auth_header(self) -> None:
        s = _settings(UPSTREAM_PROVIDER="openai", OPENAI_API_KEY="sk-oai")
        assert s.upstream_base_url == "https://api.openai.com/"
        assert s.upstream_headers == {"Authorization": "Bearer sk-oai"}
        assert s.inject_ollama_defaults is False

    def test_openai_base_url_without_v1_suffix(self) -> None:
        s = _settings(
            UPSTREAM_PROVIDER="openai",
            OPENAI_API_KEY="k",
            OPENAI_BASE_URL="https://api.openai.com",
        )
        # No trailing /v1 to strip — base still lands on the API root.
        assert s.upstream_base_url == "https://api.openai.com/"

    def test_openai_requires_api_key(self) -> None:
        with pytest.raises(ValidationError, match="OPENAI_API_KEY"):
            _settings(UPSTREAM_PROVIDER="openai")

    def test_anthropic_base_url_and_headers(self) -> None:
        s = _settings(UPSTREAM_PROVIDER="anthropic", ANTHROPIC_API_KEY="sk-ant")
        assert s.upstream_base_url == "https://api.anthropic.com/"
        # Anthropic authenticates via x-api-key and requires an explicit
        # protocol version header — not a Bearer token.
        assert s.upstream_headers == {
            "x-api-key": "sk-ant",
            "anthropic-version": "2023-06-01",
        }
        assert s.inject_ollama_defaults is False

    def test_anthropic_version_header_overridable(self) -> None:
        s = _settings(
            UPSTREAM_PROVIDER="anthropic",
            ANTHROPIC_API_KEY="k",
            ANTHROPIC_VERSION="2024-01-01",
        )
        assert s.upstream_headers["anthropic-version"] == "2024-01-01"

    def test_anthropic_requires_api_key(self) -> None:
        with pytest.raises(ValidationError, match="ANTHROPIC_API_KEY"):
            _settings(UPSTREAM_PROVIDER="anthropic")


class TestProviderRoutes:
    """PROVIDER_ROUTES — JSON map of model globs → provider, resolved per
    request model; unmatched models use the process-wide UPSTREAM_PROVIDER."""

    def test_blank_routes_is_empty(self) -> None:
        assert _settings().provider_routes() == {}
        assert _settings(PROVIDER_ROUTES="").provider_routes() == {}
        assert _settings(PROVIDER_ROUTES="  ").provider_routes() == {}

    def test_unrouted_model_falls_back_to_default(self) -> None:
        s = _settings()
        assert s.provider_for_model("anything") == "ollama"
        assert s.provider_for_model(None) == "ollama"

    def test_glob_match_resolves_provider(self) -> None:
        s = _settings(
            PROVIDER_ROUTES='{"claude-*": "anthropic", "gpt-*": "openai"}',
            ANTHROPIC_API_KEY="a",
            OPENAI_API_KEY="o",
        )
        assert s.provider_for_model("claude-sonnet-4-5") == "anthropic"
        # case-insensitive matching
        assert s.provider_for_model("Claude-Haiku-4") == "anthropic"
        assert s.provider_for_model("gpt-5-codex") == "openai"
        assert s.provider_for_model("qwen3:8b") == "ollama"

    def test_first_matching_glob_wins(self) -> None:
        s = _settings(
            PROVIDER_ROUTES='{"gpt-4*": "openai", "gpt-*": "anthropic"}',
            OPENAI_API_KEY="o",
            ANTHROPIC_API_KEY="a",
        )
        assert s.provider_for_model("gpt-4o") == "openai"
        assert s.provider_for_model("gpt-5") == "anthropic"

    def test_routed_provider_requires_key(self) -> None:
        with pytest.raises(ValidationError, match="ANTHROPIC_API_KEY"):
            _settings(PROVIDER_ROUTES='{"claude-*": "anthropic"}')

    def test_route_to_ollama_needs_no_key(self) -> None:
        s = _settings(PROVIDER_ROUTES='{"qwen-*": "ollama"}')
        assert s.provider_for_model("qwen3:8b") == "ollama"

    def test_invalid_json_rejected(self) -> None:
        with pytest.raises(ValidationError, match="PROVIDER_ROUTES"):
            _settings(PROVIDER_ROUTES="not-json")

    def test_non_object_routes_rejected(self) -> None:
        with pytest.raises(ValidationError, match="JSON object"):
            _settings(PROVIDER_ROUTES='["claude-*"]')

    def test_unknown_route_provider_rejected(self) -> None:
        with pytest.raises(ValidationError, match="unknown provider"):
            _settings(PROVIDER_ROUTES='{"x-*": "bogus"}')

    def test_routes_parsed_once_cached(self) -> None:
        """provider_for_model resolves per request — the JSON parse must be
        cached, not re-run per call (identical dict object returned)."""
        s = _settings(
            PROVIDER_ROUTES='{"claude-*": "anthropic"}', ANTHROPIC_API_KEY="a"
        )
        assert s.provider_routes() is s.provider_routes()


class TestGetProvider:
    """``deps.get_provider`` — one resolution shared by the upstream client,
    body rewriting, and event attribution: ``x-heg-provider`` header →
    PROVIDER_ROUTES model globs → UPSTREAM_PROVIDER."""

    def test_model_routes_through_provider_routes(self, monkeypatch) -> None:
        monkeypatch.setattr(
            deps,
            "get_settings",
            lambda: _settings(
                PROVIDER_ROUTES='{"claude-*": "anthropic"}',
                ANTHROPIC_API_KEY="a",
            ),
        )
        assert (
            deps.get_provider(
                _request(), _SERVICE_CLAIMS, {"model": "claude-sonnet-4-5"}
            )
            == "anthropic"
        )
        # Unmatched model + absent body fall back to the default.
        assert (
            deps.get_provider(_request(), _SERVICE_CLAIMS, {"model": "qwen3:8b"})
            == "ollama"
        )
        assert deps.get_provider(_request(), _SERVICE_CLAIMS) == "ollama"

    def test_header_overrides_routes_and_default(self, monkeypatch) -> None:
        """Service creds (``sandbox_id=None``) get the header override."""
        monkeypatch.setattr(
            deps,
            "get_settings",
            lambda: _settings(
                PROVIDER_ROUTES='{"claude-*": "anthropic"}',
                ANTHROPIC_API_KEY="a",
                OPENAI_API_KEY="o",
            ),
        )
        req = _request({"x-heg-provider": "openai"})
        assert (
            deps.get_provider(
                req, _SERVICE_CLAIMS, {"model": "claude-sonnet-4-5"}
            )
            == "openai"
        )

    def test_header_value_is_case_and_space_tolerant(self, monkeypatch) -> None:
        monkeypatch.setattr(
            deps, "get_settings", lambda: _settings(OPENROUTER_API_KEY="k")
        )
        req = _request({"x-heg-provider": " OpenRouter "})
        assert deps.get_provider(req, _SERVICE_CLAIMS) == "openrouter"

    def test_sandbox_cred_ignores_header(self, monkeypatch) -> None:
        """Sandbox-scoped creds get NO override — agent-controlled code must
        not retarget its traffic onto paid upstreams. The header is ignored
        and normal PROVIDER_ROUTES/default resolution runs."""
        monkeypatch.setattr(
            deps,
            "get_settings",
            lambda: _settings(
                PROVIDER_ROUTES='{"claude-*": "anthropic"}',
                ANTHROPIC_API_KEY="a",
            ),
        )
        req = _request({"x-heg-provider": "openai"})
        assert (
            deps.get_provider(
                req, _SANDBOX_CLAIMS, {"model": "claude-sonnet-4-5"}
            )
            == "anthropic"
        )
        assert (
            deps.get_provider(req, _SANDBOX_CLAIMS, {"model": "qwen3:8b"})
            == "ollama"
        )

    def test_sandbox_cred_bogus_header_ignored_not_400(self) -> None:
        """Ignoring is total for sandbox creds — no loud 400 signal either
        (silent-ignore is the safe, consistent answer for agent callers)."""
        req = _request({"x-heg-provider": "bogus"})
        assert (
            deps.get_provider(req, _SANDBOX_CLAIMS, {"model": "m"}) == "ollama"
        )

    def test_unknown_header_value_400s(self) -> None:
        req = _request({"x-heg-provider": "bogus"})
        with pytest.raises(HTTPException) as excinfo:
            deps.get_provider(req, _SERVICE_CLAIMS, {"model": "m"})
        assert excinfo.value.status_code == 400
        assert "bogus" in str(excinfo.value.detail)

    def test_unkeyed_header_override_400s(self, monkeypatch) -> None:
        """A valid provider NAME with no configured key must also 400 for
        service creds — resolving it would only 401 upstream."""
        monkeypatch.setattr(deps, "get_settings", lambda: _settings())
        req = _request({"x-heg-provider": "anthropic"})
        with pytest.raises(HTTPException) as excinfo:
            deps.get_provider(req, _SERVICE_CLAIMS, {"model": "m"})
        assert excinfo.value.status_code == 400
        assert "anthropic" in str(excinfo.value.detail)
        # Once keyed, the same override is honored.
        monkeypatch.setattr(
            deps, "get_settings", lambda: _settings(ANTHROPIC_API_KEY="k")
        )
        assert deps.get_provider(req, _SERVICE_CLAIMS, {"model": "m"}) == "anthropic"


class TestUpstreamClient:
    async def test_ollama_client_has_no_auth(self, monkeypatch) -> None:
        monkeypatch.setattr(deps, "get_settings", lambda: _settings())
        agen = deps.get_upstream_client(provider="ollama")
        client = await agen.__anext__()
        try:
            assert str(client.base_url) == "http://host.docker.internal:11434"
            assert "authorization" not in client.headers
        finally:
            await agen.aclose()

    async def test_openrouter_client_sends_bearer(self, monkeypatch) -> None:
        monkeypatch.setattr(
            deps,
            "get_settings",
            lambda: _settings(
                UPSTREAM_PROVIDER="openrouter", OPENROUTER_API_KEY="sk-or-k"
            ),
        )
        agen = deps.get_upstream_client(provider="openrouter")
        client = await agen.__anext__()
        try:
            assert client.headers["authorization"] == "Bearer sk-or-k"
            # The merged request URL is what actually matters: the proxy
            # POSTs "/v1/chat/completions" and must land on OpenRouter's
            # "/api/v1/chat/completions" — not "/api/v1/v1/chat/completions".
            req = client.build_request("POST", "/v1/chat/completions")
            assert str(req.url) == "https://openrouter.ai/api/v1/chat/completions"
        finally:
            await agen.aclose()

    async def test_openai_client_sends_bearer(self, monkeypatch) -> None:
        monkeypatch.setattr(
            deps,
            "get_settings",
            lambda: _settings(UPSTREAM_PROVIDER="openai", OPENAI_API_KEY="sk-oai"),
        )
        agen = deps.get_upstream_client(provider="openai")
        client = await agen.__anext__()
        try:
            assert client.headers["authorization"] == "Bearer sk-oai"
            req = client.build_request("POST", "/v1/chat/completions")
            assert str(req.url) == "https://api.openai.com/v1/chat/completions"
            req = client.build_request("POST", "/v1/responses")
            assert str(req.url) == "https://api.openai.com/v1/responses"
        finally:
            await agen.aclose()

    async def test_anthropic_client_sends_x_api_key(self, monkeypatch) -> None:
        monkeypatch.setattr(
            deps,
            "get_settings",
            lambda: _settings(
                UPSTREAM_PROVIDER="anthropic", ANTHROPIC_API_KEY="sk-ant"
            ),
        )
        agen = deps.get_upstream_client(provider="anthropic")
        client = await agen.__anext__()
        try:
            assert client.headers["x-api-key"] == "sk-ant"
            assert client.headers["anthropic-version"] == "2023-06-01"
            assert "authorization" not in client.headers
            req = client.build_request("POST", "/v1/messages")
            assert str(req.url) == "https://api.anthropic.com/v1/messages"
        finally:
            await agen.aclose()

    async def test_routed_client_uses_route_provider(self, monkeypatch) -> None:
        """A provider resolved via PROVIDER_ROUTES wires the routed client."""
        monkeypatch.setattr(
            deps,
            "get_settings",
            lambda: _settings(
                PROVIDER_ROUTES='{"claude-*": "anthropic"}',
                ANTHROPIC_API_KEY="sk-ant",
            ),
        )
        provider = deps.get_provider(
            _request(), _SANDBOX_CLAIMS, {"model": "claude-sonnet-4-5"}
        )
        agen = deps.get_upstream_client(provider=provider)
        client = await agen.__anext__()
        try:
            assert str(client.base_url) == "https://api.anthropic.com/"
            assert client.headers["x-api-key"] == "sk-ant"
        finally:
            await agen.aclose()


class TestRealDepRouting:
    """A POST through the REAL dep chain — no dependency override, no
    hand-invoked agen.

    Guards the param-name coupling flagged in ``deps.get_provider``: the
    dep's ``body`` only populates because every endpoint also names its
    own body param ``body`` (FastAPI dedupes same-named body params across
    endpoint + dependencies). Renaming either side silently breaks routing
    — the dep would see ``body=None`` and fall back to the default provider
    (or the request 422s). Asserting the ROUTED provider's client proves
    the dep received the body."""

    @staticmethod
    def _fake_client(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
        """Swap httpx.AsyncClient (inside the REAL dep) for a capturing
        stand-in so nothing touches the network."""
        seen: dict[str, Any] = {}

        class _Client:
            """Stand-in for httpx.AsyncClient — captures ctor kwargs + posts."""

            def __init__(self, **kwargs: Any) -> None:
                seen["init"] = kwargs

            async def __aenter__(self) -> Self:
                return self

            async def __aexit__(self, *exc: object) -> None:
                return None

            async def post(
                self, path: str, json: Any = None, headers: Any = None
            ) -> httpx.Response:
                seen["path"], seen["json"], seen["req_headers"] = (
                    path,
                    json,
                    headers,
                )
                return httpx.Response(200, json=_UPSTREAM_OK)

        monkeypatch.setattr(
            deps.httpx, "AsyncClient", lambda **kw: _Client(**kw)
        )
        return seen

    @staticmethod
    def _no_ingest(monkeypatch: pytest.MonkeyPatch) -> None:
        async def fake_post(events: list[dict[str, Any]]) -> tuple[int, Any]:
            return 200, {"inserted": len(events)}

        monkeypatch.setattr(metering, "post_ingest_events", fake_post)

    def test_post_routes_through_real_dep(
        self, gw_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        settings = _settings(
            PROVIDER_ROUTES='{"claude-*": "anthropic"}',
            ANTHROPIC_API_KEY="sk-ant",
        )
        # deps and main each bind get_settings at import time — patch both.
        monkeypatch.setattr(deps, "get_settings", lambda: settings)
        monkeypatch.setattr(main, "get_settings", lambda: settings)
        seen = self._fake_client(monkeypatch)
        self._no_ingest(monkeypatch)

        resp = gw_client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {DEV_CRED_TOKEN}"},
            json={
                "model": "claude-sonnet-4-5",
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        assert resp.status_code == 200

        # The REAL dep saw body["model"] and built the routed anthropic
        # client — body=None would have resolved the default (ollama).
        assert seen["init"]["base_url"] == "https://api.anthropic.com/"
        assert seen["init"]["headers"] == {
            "x-api-key": "sk-ant",
            "anthropic-version": "2023-06-01",
        }
        assert seen["path"] == "/v1/chat/completions"
        assert seen["json"]["model"] == "claude-sonnet-4-5"
        assert seen["json"]["stream"] is False
        # routed provider is not ollama → no ollama-only defaults injected
        assert "num_ctx" not in seen["json"]

    def test_header_override_routes_through_real_dep(
        self, gw_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``x-heg-provider`` on a SERVICE cred forces the provider for the
        upstream client and body rewriting — a model that would otherwise
        route elsewhere (here: default ollama) still lands on the
        overridden provider."""
        settings = _settings(OPENAI_API_KEY="sk-oai")
        monkeypatch.setattr(deps, "get_settings", lambda: settings)
        monkeypatch.setattr(main, "get_settings", lambda: settings)
        seen = self._fake_client(monkeypatch)
        self._no_ingest(monkeypatch)

        resp = gw_client.post(
            "/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {_service_cred()}",
                "x-heg-provider": "openai",
            },
            json={
                "model": "qwen3:8b",
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        assert resp.status_code == 200

        # Override beat the model's default routing (qwen3 would be ollama).
        assert seen["init"]["base_url"] == "https://api.openai.com/"
        assert seen["init"]["headers"] == {"Authorization": "Bearer sk-oai"}
        # ...and the same resolution drove body rewriting: no ollama
        # defaults injected for a non-ollama provider.
        assert "num_ctx" not in seen["json"]

    def test_sandbox_cred_header_override_ignored(
        self, gw_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A sandbox-scoped cred's ``x-heg-provider`` is ignored end-to-end:
        the upstream client is built for the normally-resolved provider
        (ollama default here), never the named paid provider."""
        settings = _settings(ANTHROPIC_API_KEY="sk-ant")
        monkeypatch.setattr(deps, "get_settings", lambda: settings)
        monkeypatch.setattr(main, "get_settings", lambda: settings)
        seen = self._fake_client(monkeypatch)
        self._no_ingest(monkeypatch)

        resp = gw_client.post(
            "/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {_sandbox_cred()}",
                "x-heg-provider": "anthropic",
            },
            json={
                "model": "qwen3:8b",
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        assert resp.status_code == 200

        # Header ignored → default ollama client (no anthropic key sent),
        # with its ollama-only defaults injected as usual.
        assert seen["init"]["base_url"] == "http://host.docker.internal:11434"
        assert seen["init"]["headers"] == {}
        assert seen["json"]["num_ctx"] == 32_768

    def test_unknown_header_override_400s(
        self, gw_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A bogus ``x-heg-provider`` on a service cred must never silently
        fall back to model/default routing — the request 400s."""
        self._no_ingest(monkeypatch)
        resp = gw_client.post(
            "/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {_service_cred()}",
                "x-heg-provider": "bogus",
            },
            json={
                "model": "m",
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        assert resp.status_code == 400

    def test_unkeyed_header_override_400s(
        self, gw_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A service-cred override naming a provider with no configured API
        key 400s — resolving it would only 401 upstream."""
        settings = _settings()  # ollama default; no hosted keys
        monkeypatch.setattr(deps, "get_settings", lambda: settings)
        monkeypatch.setattr(main, "get_settings", lambda: settings)
        self._no_ingest(monkeypatch)
        resp = gw_client.post(
            "/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {_service_cred()}",
                "x-heg-provider": "anthropic",
            },
            json={
                "model": "m",
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        assert resp.status_code == 400


class TestOllamaDefaultInjection:
    """num_ctx/keep_alive are Ollama-only body params — the proxy must not
    inject them for hosted providers that may reject unknown fields."""

    @pytest.fixture()
    def forwarded(self, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
        """Capture the JSON body the proxy forwards upstream."""
        seen: dict[str, Any] = {}

        def dispatch(request: httpx.Request) -> httpx.Response:
            seen["json"] = json.loads(request.content)
            return httpx.Response(200, json=_UPSTREAM_OK)

        async def _dep():
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(dispatch),
                base_url="http://upstream.test",
            ) as client:
                yield client

        main.app.dependency_overrides[get_upstream_client] = _dep
        yield seen
        main.app.dependency_overrides.pop(get_upstream_client, None)

    @pytest.fixture(autouse=True)
    def _no_ingest(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def fake_post(events: list[dict[str, Any]]) -> tuple[int, Any]:
            return 200, {"inserted": len(events)}

        monkeypatch.setattr(metering, "post_ingest_events", fake_post)

    def _post(self, client: TestClient) -> None:
        resp = client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {DEV_CRED_TOKEN}"},
            json={"model": "m", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert resp.status_code == 200

    def test_ollama_injects_num_ctx_and_keep_alive(
        self, gw_client: TestClient, forwarded: dict[str, Any], monkeypatch
    ) -> None:
        monkeypatch.setattr(deps, "get_settings", lambda: _settings())
        self._post(gw_client)
        assert forwarded["json"]["num_ctx"] == 32_768
        assert forwarded["json"]["keep_alive"] == "30m"
        assert forwarded["json"]["stream"] is False

    def test_openrouter_strips_ollama_only_fields(
        self, gw_client: TestClient, forwarded: dict[str, Any], monkeypatch
    ) -> None:
        monkeypatch.setattr(
            deps,
            "get_settings",
            lambda: _settings(
                UPSTREAM_PROVIDER="openrouter", OPENROUTER_API_KEY="k"
            ),
        )
        resp = gw_client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {DEV_CRED_TOKEN}"},
            json={
                "model": "m",
                "messages": [{"role": "user", "content": "hi"}],
                "num_ctx": 32_768,
                "keep_alive": "30m",
                "think": False,
                "options": {"num_ctx": 32_768},
            },
        )
        assert resp.status_code == 200
        for key in ("num_ctx", "keep_alive", "think", "options"):
            assert key not in forwarded["json"]
        assert forwarded["json"]["stream"] is False

    def test_openrouter_streaming_also_skips_defaults(
        self, gw_client: TestClient, forwarded: dict[str, Any], monkeypatch
    ) -> None:
        monkeypatch.setattr(
            deps,
            "get_settings",
            lambda: _settings(
                UPSTREAM_PROVIDER="openrouter", OPENROUTER_API_KEY="k"
            ),
        )
        resp = gw_client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {DEV_CRED_TOKEN}"},
            json={
                "model": "m",
                "stream": True,
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        assert resp.status_code == 200
        assert "num_ctx" not in forwarded["json"]
        assert "keep_alive" not in forwarded["json"]
        assert forwarded["json"]["stream"] is True
        assert forwarded["json"]["stream_options"]["include_usage"] is True

    def test_anthropic_strips_ollama_only_fields_on_messages(
        self, gw_client: TestClient, forwarded: dict[str, Any], monkeypatch
    ) -> None:
        """The /v1/messages pass-through strips for hosted providers too —
        a stray ollama param must not reach api.anthropic.com."""
        monkeypatch.setattr(
            deps,
            "get_settings",
            lambda: _settings(
                UPSTREAM_PROVIDER="anthropic", ANTHROPIC_API_KEY="k"
            ),
        )
        resp = gw_client.post(
            "/v1/messages",
            headers={"Authorization": f"Bearer {DEV_CRED_TOKEN}"},
            json={
                "model": "claude-x",
                "max_tokens": 8,
                "messages": [{"role": "user", "content": "hi"}],
                "num_ctx": 32_768,
                "options": {},
            },
        )
        assert resp.status_code == 200
        assert "num_ctx" not in forwarded["json"]
        assert "options" not in forwarded["json"]
        # Anthropic-native fields pass through untouched.
        assert forwarded["json"]["max_tokens"] == 8


class TestProviderAttribution:
    """Emitted llm_* events record the resolved upstream provider — so a
    routed (or switched) upstream is attributable in the event stream."""

    @pytest.fixture()
    def posts(self, monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []

        async def fake_post(evs: list[dict[str, Any]]) -> tuple[int, Any]:
            events.extend(evs)
            return 200, {"inserted": len(evs)}

        monkeypatch.setattr(metering, "post_ingest_events", fake_post)
        return events

    @pytest.fixture()
    def _upstream(self):
        def dispatch(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_UPSTREAM_OK)

        async def _dep():
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(dispatch),
                base_url="http://upstream.test",
            ) as client:
                yield client

        main.app.dependency_overrides[get_upstream_client] = _dep
        yield
        main.app.dependency_overrides.pop(get_upstream_client, None)

    def _payloads(
        self, posts: list[dict[str, Any]], event_type: str
    ) -> list[dict[str, Any]]:
        return [e["payload"] for e in posts if e["type"] == event_type]

    def test_default_provider_recorded(
        self, gw_client: TestClient, posts: list, _upstream
    ) -> None:
        gw_client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {DEV_CRED_TOKEN}"},
            json={"model": "m", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert self._payloads(posts, "llm_request")[0]["provider"] == "ollama"
        assert self._payloads(posts, "llm_response")[0]["provider"] == "ollama"

    def test_routed_provider_recorded(
        self, gw_client: TestClient, posts: list, _upstream, monkeypatch
    ) -> None:
        monkeypatch.setattr(
            deps,
            "get_settings",
            lambda: _settings(
                PROVIDER_ROUTES='{"m": "openai"}',
                OPENAI_API_KEY="k",
            ),
        )
        gw_client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {DEV_CRED_TOKEN}"},
            json={"model": "m", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert self._payloads(posts, "llm_request")[0]["provider"] == "openai"
        assert self._payloads(posts, "llm_response")[0]["provider"] == "openai"


class TestAnthropicHeaderForwarding:
    """Caller ``anthropic-beta``/``anthropic-version`` headers must reach the
    anthropic upstream — Claude Code gates beta features (interleaved
    thinking, fine-grained tool streaming, 1M context) on them. Forwarded
    only when the resolved provider is ``anthropic``."""

    @pytest.fixture()
    def forwarded(self, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
        """Capture the request the proxy sends upstream."""
        seen: dict[str, Any] = {}

        def dispatch(request: httpx.Request) -> httpx.Response:
            seen["headers"] = request.headers
            seen["json"] = json.loads(request.content)
            return httpx.Response(200, json=_UPSTREAM_OK)

        async def _dep():
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(dispatch),
                base_url="http://upstream.test",
            ) as client:
                yield client

        main.app.dependency_overrides[get_upstream_client] = _dep
        yield seen
        main.app.dependency_overrides.pop(get_upstream_client, None)

    @pytest.fixture(autouse=True)
    def _no_ingest(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def fake_post(events: list[dict[str, Any]]) -> tuple[int, Any]:
            return 200, {"inserted": len(events)}

        monkeypatch.setattr(metering, "post_ingest_events", fake_post)

    def test_protocol_headers_forwarded_to_anthropic(
        self,
        gw_client: TestClient,
        forwarded: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # The override needs a service cred + a keyed anthropic provider.
        settings = _settings(ANTHROPIC_API_KEY="sk-ant")
        monkeypatch.setattr(deps, "get_settings", lambda: settings)
        monkeypatch.setattr(main, "get_settings", lambda: settings)
        resp = gw_client.post(
            "/v1/messages",
            headers={
                "Authorization": f"Bearer {_service_cred()}",
                "x-heg-provider": "anthropic",
                "anthropic-beta": "context-1m-2025-08-07,interleaved-thinking-2025-05-14",
                "anthropic-version": "2024-06-01",
            },
            json={
                "model": "claude-x",
                "max_tokens": 8,
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        assert resp.status_code == 200
        assert (
            forwarded["headers"]["anthropic-beta"]
            == "context-1m-2025-08-07,interleaved-thinking-2025-05-14"
        )
        assert forwarded["headers"]["anthropic-version"] == "2024-06-01"
        # The routing-override header is gateway-internal — never forwarded.
        assert "x-heg-provider" not in forwarded["headers"]

    def test_protocol_headers_not_forwarded_to_ollama(
        self, gw_client: TestClient, forwarded: dict[str, Any]
    ) -> None:
        resp = gw_client.post(
            "/v1/messages",
            headers={
                "Authorization": f"Bearer {DEV_CRED_TOKEN}",
                "anthropic-beta": "context-1m-2025-08-07",
            },
            json={
                "model": "m",
                "max_tokens": 8,
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        assert resp.status_code == 200
        assert "anthropic-beta" not in forwarded["headers"]

    def test_sandbox_cred_provider_header_ignored(
        self, gw_client: TestClient, forwarded: dict[str, Any]
    ) -> None:
        """A sandbox cred's ``x-heg-provider: anthropic`` is ignored — the
        request resolves ollama, so anthropic protocol headers must NOT
        reach the upstream either."""
        resp = gw_client.post(
            "/v1/messages",
            headers={
                "Authorization": f"Bearer {_sandbox_cred()}",
                "x-heg-provider": "anthropic",
                "anthropic-beta": "context-1m-2025-08-07",
            },
            json={
                "model": "m",
                "max_tokens": 8,
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        assert resp.status_code == 200
        assert "anthropic-beta" not in forwarded["headers"]
