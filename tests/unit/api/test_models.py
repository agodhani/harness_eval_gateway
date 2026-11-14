"""PR1 — GET /api/models (catalog proxy) + POST /api/models/pull (streaming).

Hermetic: the gateway is an httpx.MockTransport injected via the
``get_gateway_client`` dependency override — no live gateway/Ollama. The
api mints a real ``heg.*`` cred for the upstream call; tests only assert
its shape, not its signature.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from api.main import app
from api.routers import models as models_router

_NDJSON_PULL = (
    b'{"status":"pulling manifest"}\n'
    b'{"status":"downloading","digest":"sha256:x","total":100,"completed":50}\n'
    b'{"status":"verifying sha256 digest"}\n'
    b'{"status":"success"}\n'
)


@pytest.fixture()
def gw() -> Any:
    """Mock gateway; ``state['handler']`` is replaceable per test."""
    state: dict[str, Any] = {"seen": [], "handler": None}

    def default_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"object": "list", "data": [{"id": "m1"}]})

    state["handler"] = default_handler

    def dispatch(request: httpx.Request) -> httpx.Response:
        try:
            body = json.loads(request.content)
        except ValueError:
            body = None
        state["seen"].append(
            {
                "url": str(request.url),
                "json": body,
                "authorization": request.headers.get("authorization"),
            }
        )
        return state["handler"](request)

    async def _dep():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(dispatch),
            base_url="http://gateway.test",
        ) as client:
            yield client

    app.dependency_overrides[models_router.get_gateway_client] = _dep
    yield state
    app.dependency_overrides.pop(models_router.get_gateway_client, None)


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


# ---------------------------------------------------------------------------
# GET /api/models
# ---------------------------------------------------------------------------


def test_list_models_normalizes_and_reads_provider_header(
    client: TestClient, gw: Any
) -> None:
    gw["handler"] = lambda _r: httpx.Response(
        200,
        json={
            "object": "list",
            "data": [
                {"id": "qwen3:8b", "object": "model", "owned_by": "library"},
                {"object": "model"},  # no id — dropped
                "junk",
                {"id": "qwen2.5-coder:3b"},
            ],
        },
        headers={"x-heg-provider": "ollama"},
    )
    resp = client.get("/api/models")
    assert resp.status_code == 200
    body = resp.json()
    assert [m["id"] for m in body["models"]] == ["qwen3:8b", "qwen2.5-coder:3b"]
    # extra upstream fields are preserved for future UI use
    assert body["models"][0]["owned_by"] == "library"
    assert body["provider"] == "ollama"
    assert body["pull_supported"] is True
    # went to the gateway /v1/models with a minted bearer cred
    assert gw["seen"][0]["url"].endswith("/v1/models")
    assert gw["seen"][0]["authorization"].startswith("Bearer heg.")


def test_list_models_missing_provider_header_means_no_pull(
    client: TestClient, gw: Any
) -> None:
    gw["handler"] = lambda _r: httpx.Response(200, json={"data": [{"id": "m"}]})
    body = client.get("/api/models").json()
    assert body["provider"] is None
    assert body["pull_supported"] is False


def test_list_models_openrouter_provider_disables_pull(
    client: TestClient, gw: Any
) -> None:
    gw["handler"] = lambda _r: httpx.Response(
        200,
        json={"data": [{"id": "particle-ai/particle-1:free"}]},
        headers={"x-heg-provider": "openrouter"},
    )
    body = client.get("/api/models").json()
    assert body["provider"] == "openrouter"
    assert body["pull_supported"] is False


def test_list_models_gateway_error_status_forwarded(
    client: TestClient, gw: Any
) -> None:
    gw["handler"] = lambda _r: httpx.Response(
        502, json={"error": {"message": "upstream request failed: refused"}}
    )
    resp = client.get("/api/models")
    assert resp.status_code == 502
    assert resp.json()["detail"] == "upstream request failed: refused"


def test_list_models_gateway_unreachable(client: TestClient, gw: Any) -> None:
    def boom(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    gw["handler"] = boom
    resp = client.get("/api/models")
    assert resp.status_code == 502
    assert "cannot reach gateway" in resp.json()["detail"]


def test_list_models_non_json_body(client: TestClient, gw: Any) -> None:
    gw["handler"] = lambda _r: httpx.Response(200, text="<html>proxy</html>")
    resp = client.get("/api/models")
    assert resp.status_code == 200
    assert resp.json()["models"] == []


def test_list_models_non_list_data_body(client: TestClient, gw: Any) -> None:
    gw["handler"] = lambda _r: httpx.Response(200, json={"data": 5})
    resp = client.get("/api/models")
    assert resp.status_code == 200
    assert resp.json()["models"] == []


# ---------------------------------------------------------------------------
# POST /api/models/pull
# ---------------------------------------------------------------------------


def test_pull_streams_ndjson_progress(client: TestClient, gw: Any) -> None:
    gw["handler"] = lambda _r: httpx.Response(
        200,
        content=_NDJSON_PULL,
        headers={"content-type": "application/x-ndjson"},
    )
    resp = client.post("/api/models/pull", json={"name": "qwen3:8b"})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/x-ndjson")
    lines = [json.loads(x) for x in resp.text.splitlines() if x.strip()]
    assert lines[0]["status"] == "pulling manifest"
    assert lines[-1]["status"] == "success"
    # forwarded verbatim + forced stream
    sent = gw["seen"][0]
    assert sent["url"].endswith("/v1/models/pull")
    assert sent["json"] == {"name": "qwen3:8b", "stream": True}
    assert sent["authorization"].startswith("Bearer heg.")


def test_pull_forwards_gateway_error_status(client: TestClient, gw: Any) -> None:
    gw["handler"] = lambda _r: httpx.Response(
        501,
        json={
            "error": {
                "message": "model install is only supported for the ollama "
                "upstream (active: openrouter)"
            }
        },
    )
    resp = client.post("/api/models/pull", json={"name": "m:latest"})
    assert resp.status_code == 501
    assert "ollama" in resp.json()["detail"]


def test_pull_gateway_unreachable(client: TestClient, gw: Any) -> None:
    def boom(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    gw["handler"] = boom
    resp = client.post("/api/models/pull", json={"name": "m:latest"})
    assert resp.status_code == 502
    assert "cannot reach gateway" in resp.json()["detail"]


def test_pull_validates_name(client: TestClient) -> None:
    resp = client.post("/api/models/pull", json={"name": ""})
    assert resp.status_code == 422
