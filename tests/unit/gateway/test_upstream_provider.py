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
