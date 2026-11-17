"""gateway upstream retry — Bug B: transient upstream failures (connect/
read timeouts, refused/reset, bare 502/503/504) are retried INSIDE the
gateway before an error reaches the client, with ``upstream_retry``-staged
error events per attempt. 4xx/429s and mid-stream failures never retry.

Hermetic: the upstream is an httpx.MockTransport injected via dependency
override; ingest emission is monkeypatched; retry backoff is pinned to 0 —
no live Ollama/Postgres/api and no real sleeps.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from gateway import metering
from gateway.config import GatewaySettings, get_settings
from gateway.creds import mint_cred
from gateway.deps import get_ollama_client, get_upstream_client
from gateway.main import app

SECRET = "dev-secret-change-me"  # gateway.config default in tests


def _cred(**kwargs: Any) -> str:
    kwargs.setdefault("run_id", uuid.uuid4())
    kwargs.setdefault("sandbox_id", uuid.uuid4())
    kwargs.setdefault("secret", SECRET)
    kwargs.setdefault("ttl_seconds", 3600)
    return mint_cred(**kwargs)


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(autouse=True)
def _no_retry_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin backoff to 0 — retry *counting* is under test, not real sleeps."""
    monkeypatch.setattr(get_settings(), "UPSTREAM_RETRY_BACKOFF_SECONDS", 0)


@pytest.fixture()
def upstream() -> Any:
    """Mock upstream; ``state['handler']`` is replaceable per test."""
    state: dict[str, Any] = {"seen": [], "handler": None}

    def default_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_UPSTREAM_OK)

    state["handler"] = default_handler

    def dispatch(request: httpx.Request) -> httpx.Response:
        try:
            body = json.loads(request.content)
        except ValueError:
            body = None
        state["seen"].append({"url": str(request.url), "json": body})
        return state["handler"](request)

    async def _dep():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(dispatch),
            base_url="http://upstream.test",
        ) as client:
            yield client

    app.dependency_overrides[get_upstream_client] = _dep
    app.dependency_overrides[get_ollama_client] = _dep
    yield state
    app.dependency_overrides.pop(get_upstream_client, None)
    app.dependency_overrides.pop(get_ollama_client, None)


@pytest.fixture()
def emitted(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Capture emit_event calls (claims + source/type/payload)."""
    calls: list[dict[str, Any]] = []

    async def fake_emit_event(
        claims, *, source: str, event_type: str, payload: dict[str, Any]
    ) -> None:
        calls.append(
            {
                "run_id": claims.run_id,
                "sandbox_id": claims.sandbox_id,
                "source": source,
                "type": event_type,
                "payload": payload,
            }
        )

    monkeypatch.setattr(metering, "emit_event", fake_emit_event)
    return calls


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

_CHAT_BODY = {
    "model": "test-model",
    "messages": [{"role": "user", "content": "hi"}],
}

_CHAT_SSE = (
    'data: {"id":"chatcmpl-s","object":"chat.completion.chunk","choices":'
    '[{"index":0,"delta":{"content":"hi"},"finish_reason":"stop"}]}\n\n'
    'data: {"id":"chatcmpl-s","object":"chat.completion.chunk","choices":[],'
    '"usage":{"prompt_tokens":2,"completion_tokens":1,"total_tokens":3}}\n\n'
    "data: [DONE]\n\n"
)


def _flaky(
    steps: list[Any],
) -> Callable[[httpx.Request], httpx.Response]:
    """Handler running ``steps`` in order; a step that is an exception
    instance is raised, a Response is returned."""
    calls = iter(steps)

    def handler(_request: httpx.Request) -> httpx.Response:
        step = next(calls)
        if isinstance(step, Exception):
            raise step
        return step

    return handler


def _retry_events(emitted: list) -> list[dict[str, Any]]:
    return [
        c
        for c in emitted
        if c["type"] == "error" and c["payload"].get("stage") == "upstream_retry"
    ]


def _upstream_errors(emitted: list) -> list[dict[str, Any]]:
    return [
        c
        for c in emitted
        if c["type"] == "error" and c["payload"].get("stage") == "upstream"
    ]


# ---------------------------------------------------------------------------
# settings
# ---------------------------------------------------------------------------


def test_retry_settings_defaults() -> None:
    s = GatewaySettings(_env_file=None)
    assert s.UPSTREAM_RETRY_ATTEMPTS == 2
    assert s.UPSTREAM_RETRY_BACKOFF_SECONDS == 5.0


# ---------------------------------------------------------------------------
# transient transport failures are retried
# ---------------------------------------------------------------------------


def test_read_timeout_retried_then_succeeds(
    gw_client: TestClient, upstream: Any, emitted: list
) -> None:
    """A saturated-but-alive upstream (ReadTimeout once) must not kill the
    lane — the second attempt succeeds."""
    upstream["handler"] = _flaky(
        [
            httpx.ReadTimeout("read timed out"),
            httpx.Response(200, json=_UPSTREAM_OK),
        ]
    )
    resp = gw_client.post(
        "/v1/chat/completions", json=_CHAT_BODY, headers=_auth(_cred())
    )
    assert resp.status_code == 200
    assert resp.json()["usage"]["total_tokens"] == 5
    assert len(upstream["seen"]) == 2

    retries = _retry_events(emitted)
    assert len(retries) == 1
    assert retries[0]["payload"]["attempt"] == 1
    assert retries[0]["payload"]["max_attempts"] == 3
    assert "ReadTimeout" in retries[0]["payload"]["message"]
    # ...and the successful response is metered normally afterwards.
    assert emitted[-1]["type"] == "llm_response"


def test_connect_error_exhausts_retries_then_502(
    gw_client: TestClient, upstream: Any, emitted: list
) -> None:
    """Every attempt failing → the historical 502 upstream-error path,
    with one retry event per extra attempt (default: 2 extra → 3 tries)."""
    upstream["handler"] = _flaky(
        [httpx.ConnectError("connection refused")] * 4
    )
    resp = gw_client.post(
        "/v1/chat/completions", json=_CHAT_BODY, headers=_auth(_cred())
    )
    assert resp.status_code == 502
    assert resp.json()["error"]["type"] == "upstream_error"
    assert len(upstream["seen"]) == 3  # 1 + UPSTREAM_RETRY_ATTEMPTS(2)

    retries = _retry_events(emitted)
    assert len(retries) == 2
    assert [r["payload"]["attempt"] for r in retries] == [1, 2]
    # Final failure takes the normal upstream-error path.
    errors = _upstream_errors(emitted)
    assert len(errors) == 1
    assert "ConnectError" in errors[0]["payload"]["message"]
    # No response ever arrived → not metered.
    assert not any(c["type"] == "llm_response" for c in emitted)


@pytest.mark.parametrize("status_code", [502, 503, 504])
def test_retryable_statuses_retried_then_succeed(
    gw_client: TestClient, upstream: Any, emitted: list, status_code: int
) -> None:
    """Bare gateway-ish 5xx statuses are transient: retried, with the
    upstream's own error body captured on the retry event."""
    upstream["handler"] = _flaky(
        [
            httpx.Response(status_code, json={"error": "busy"}),
            httpx.Response(200, json=_UPSTREAM_OK),
        ]
    )
    resp = gw_client.post(
        "/v1/chat/completions", json=_CHAT_BODY, headers=_auth(_cred())
    )
    assert resp.status_code == 200
    assert len(upstream["seen"]) == 2

    retries = _retry_events(emitted)
    assert len(retries) == 1
    assert retries[0]["payload"]["http_status"] == status_code
    assert "busy" in retries[0]["payload"]["message"]


def test_retryable_status_exhausted_surfaces_upstream_response(
    gw_client: TestClient, upstream: Any, emitted: list
) -> None:
    """Exhausted 503 → the LAST upstream response forwards verbatim (status
    + body), metered like any other upstream failure."""
    upstream["handler"] = _flaky(
        [httpx.Response(503, json={"error": "still busy"})] * 4
    )
    resp = gw_client.post(
        "/v1/chat/completions", json=_CHAT_BODY, headers=_auth(_cred())
    )
    assert resp.status_code == 503
    assert resp.json()["error"] == "still busy"
    assert len(upstream["seen"]) == 3
    assert len(_retry_events(emitted)) == 2
    # Response arrived → metered + upstream error event, as always.
    assert any(c["type"] == "llm_response" for c in emitted)
    assert _upstream_errors(emitted)[0]["payload"]["http_status"] == 503


# ---------------------------------------------------------------------------
# never retried: 4xx, 429, other 5xx
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status_code", [400, 404, 422, 429, 500])
def test_client_and_other_statuses_never_retried(
    gw_client: TestClient, upstream: Any, emitted: list, status_code: int
) -> None:
    """4xx model/client errors, 429 rate limits, and non-gateway 5xx surface
    immediately — no retry, no upstream_retry events."""
    upstream["handler"] = _flaky(
        [httpx.Response(status_code, json={"error": "nope"})] * 2
    )
    resp = gw_client.post(
        "/v1/chat/completions", json=_CHAT_BODY, headers=_auth(_cred())
    )
    assert resp.status_code == status_code
    assert len(upstream["seen"]) == 1
    assert _retry_events(emitted) == []


def test_streamed_request_retries_then_streams(
    gw_client: TestClient, upstream: Any, emitted: list
) -> None:
    """stream:true callers get the same protection — a 503 at open is
    retried BEFORE any bytes reach the client."""
    upstream["handler"] = _flaky(
        [
            httpx.Response(503, json={"error": "loading model"}),
            httpx.Response(
                200,
                text=_CHAT_SSE,
                headers={"content-type": "text/event-stream"},
            ),
        ]
    )
    resp = gw_client.post(
        "/v1/chat/completions",
        json={**_CHAT_BODY, "stream": True},
        headers=_auth(_cred()),
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert resp.text == _CHAT_SSE
    assert len(upstream["seen"]) == 2
    assert len(_retry_events(emitted)) == 1
    assert emitted[-1]["type"] == "llm_response"


def test_streamed_timeout_retried(
    gw_client: TestClient, upstream: Any, emitted: list
) -> None:
    upstream["handler"] = _flaky(
        [
            httpx.ReadTimeout("sat"),
            httpx.Response(
                200,
                text=_CHAT_SSE,
                headers={"content-type": "text/event-stream"},
            ),
        ]
    )
    resp = gw_client.post(
        "/v1/chat/completions",
        json={**_CHAT_BODY, "stream": True},
        headers=_auth(_cred()),
    )
    assert resp.status_code == 200
    assert len(upstream["seen"]) == 2


def test_request_body_identical_across_attempts(
    gw_client: TestClient, upstream: Any, emitted: list
) -> None:
    """Retries resend the SAME outgoing body — including injected defaults —
    so upstream behavior is deterministic across attempts."""
    upstream["handler"] = _flaky(
        [
            httpx.ConnectError("refused"),
            httpx.Response(503, text="busy"),
            httpx.Response(200, json=_UPSTREAM_OK),
        ]
    )
    resp = gw_client.post(
        "/v1/chat/completions",
        json={**_CHAT_BODY, "max_tokens": 32},
        headers=_auth(_cred()),
    )
    assert resp.status_code == 200
    assert len(upstream["seen"]) == 3
    bodies = [s["json"] for s in upstream["seen"]]
    assert bodies[0] == bodies[1] == bodies[2]
    assert bodies[0]["model"] == "test-model"
    assert bodies[0]["max_tokens"] == 32


def test_retry_attempts_env_override(
    gw_client: TestClient,
    upstream: Any,
    emitted: list,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``UPSTREAM_RETRY_ATTEMPTS`` bounds the extra tries — 0 disables."""
    monkeypatch.setattr(get_settings(), "UPSTREAM_RETRY_ATTEMPTS", 0)
    upstream["handler"] = _flaky([httpx.ConnectError("x")] * 3)
    resp = gw_client.post(
        "/v1/chat/completions", json=_CHAT_BODY, headers=_auth(_cred())
    )
    assert resp.status_code == 502
    assert len(upstream["seen"]) == 1  # no retries at all
    assert _retry_events(emitted) == []

    upstream["seen"].clear()
    monkeypatch.setattr(get_settings(), "UPSTREAM_RETRY_ATTEMPTS", 1)
    resp = gw_client.post(
        "/v1/chat/completions", json=_CHAT_BODY, headers=_auth(_cred())
    )
    assert resp.status_code == 502
    assert len(upstream["seen"]) == 2  # one extra attempt
    assert len(_retry_events(emitted)) == 1
    assert _retry_events(emitted)[0]["payload"]["max_attempts"] == 2
