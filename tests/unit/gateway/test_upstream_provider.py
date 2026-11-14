"""Upstream provider selection (ollama | openrouter) — config + client wiring.

Hermetic: settings are constructed directly (env file reads disabled), the
upstream is an httpx.MockTransport injected via dependency override, and
ingest emission is monkeypatched — no live upstream/api.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from gateway import deps, main, metering
from gateway.config import GatewaySettings
from gateway.creds import DEV_CRED_TOKEN
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


def _settings(**kwargs) -> GatewaySettings:
    kwargs.setdefault("_env_file", None)
    return GatewaySettings(**kwargs)


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


class TestUpstreamClient:
    async def test_ollama_client_has_no_auth(self, monkeypatch) -> None:
        monkeypatch.setattr(deps, "get_settings", lambda: _settings())
        agen = deps.get_upstream_client()
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
        agen = deps.get_upstream_client()
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
        agen = deps.get_upstream_client()
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
        agen = deps.get_upstream_client()
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
        """The request body's model picks the provider per PROVIDER_ROUTES."""
        monkeypatch.setattr(
            deps,
            "get_settings",
            lambda: _settings(
                PROVIDER_ROUTES='{"claude-*": "anthropic"}',
                ANTHROPIC_API_KEY="sk-ant",
            ),
        )
        agen = deps.get_upstream_client({"model": "claude-sonnet-4-5"})
        client = await agen.__anext__()
        try:
            assert str(client.base_url) == "https://api.anthropic.com/"
            assert client.headers["x-api-key"] == "sk-ant"
        finally:
            await agen.aclose()
        # Unmatched model + absent body fall back to the default (ollama).
        agen = deps.get_upstream_client({"model": "qwen3:8b"})
        client = await agen.__anext__()
        try:
            assert str(client.base_url) == "http://host.docker.internal:11434"
        finally:
            await agen.aclose()


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
        monkeypatch.setattr(main, "get_settings", lambda: _settings())
        self._post(gw_client)
        assert forwarded["json"]["num_ctx"] == 32_768
        assert forwarded["json"]["keep_alive"] == "30m"
        assert forwarded["json"]["stream"] is False

    def test_openrouter_strips_ollama_only_fields(
        self, gw_client: TestClient, forwarded: dict[str, Any], monkeypatch
    ) -> None:
        monkeypatch.setattr(
            main,
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
            main,
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
            main,
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
            main,
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
