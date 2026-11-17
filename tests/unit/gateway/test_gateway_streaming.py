"""gateway stream metering — Bug A: streamed responses must produce exactly
one ``llm_response`` event carrying usage (provider-reported when the
upstream honors ``stream_options.include_usage``, else a ``len/4`` estimate
flagged ``usage_estimated``), plus ``error`` events for streamed
``{"error": ...}`` frames and mid-stream transport failures.

Hermetic: the upstream is an httpx.MockTransport injected via dependency
override; ingest emission is monkeypatched — no live Ollama/Postgres/api.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from gateway import metering
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


@pytest.fixture()
def upstream() -> Any:
    """Mock upstream; ``state['handler']`` is replaceable per test."""
    state: dict[str, Any] = {"seen": [], "handler": None}

    def default_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

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


_CHAT_BODY = {
    "model": "test-model",
    "messages": [{"role": "user", "content": "hi"}],
}

_MESSAGES_BODY = {
    "model": "test-model",
    "max_tokens": 64,
    "messages": [{"role": "user", "content": "hi"}],
}

# Provider honored include_usage: terminal empty-choices usage chunk.
_CHAT_SSE_USAGE = (
    'data: {"id":"chatcmpl-s","object":"chat.completion.chunk","choices":'
    '[{"index":0,"delta":{"role":"assistant","content":"he"}}]}\n\n'
    'data: {"id":"chatcmpl-s","object":"chat.completion.chunk","choices":'
    '[{"index":0,"delta":{"content":"llo"},"finish_reason":"stop"}]}\n\n'
    'data: {"id":"chatcmpl-s","object":"chat.completion.chunk","choices":[],'
    '"usage":{"prompt_tokens":7,"completion_tokens":3,"total_tokens":10}}\n\n'
    "data: [DONE]\n\n"
)

# Provider ignored include_usage: content chunks then straight to [DONE].
_CHAT_SSE_NO_USAGE = (
    'data: {"id":"chatcmpl-s","object":"chat.completion.chunk","choices":'
    '[{"index":0,"delta":{"role":"assistant","content":"hello"}}]}\n\n'
    'data: {"id":"chatcmpl-s","object":"chat.completion.chunk","choices":'
    '[{"index":0,"delta":{"content":" wor"},"finish_reason":"stop"}]}\n\n'
    "data: [DONE]\n\n"
)

# Mid-stream provider failure reported as an SSE error frame (OpenRouter
# et al. do this under a 200 status).
_CHAT_SSE_ERROR_FRAME = (
    'data: {"id":"chatcmpl-s","object":"chat.completion.chunk","choices":'
    '[{"index":0,"delta":{"content":"he"}}]}\n\n'
    'data: {"error":{"message":"model overloaded","type":"server_error"}}\n\n'
    "data: [DONE]\n\n"
)

# Anthropic stream carrying text deltas but no usage events at all.
_ANTHROPIC_SSE_NO_USAGE = (
    'event: content_block_delta\ndata: {"type":"content_block_delta",'
    '"delta":{"type":"text_delta","text":"hello world"}}\n\n'
    'event: message_stop\ndata: {"type":"message_stop"}\n\n'
)


def _sse_handler(body: str):
    return lambda _req: httpx.Response(
        200, text=body, headers={"content-type": "text/event-stream"}
    )


def test_stream_emits_one_llm_response_with_provider_usage(
    gw_client: TestClient, upstream: Any, emitted: list
) -> None:
    """stream:true → exactly ONE llm_response, emitted at stream end with
    the terminal usage chunk the injected stream_options requested."""
    upstream["handler"] = _sse_handler(_CHAT_SSE_USAGE)
    resp = gw_client.post(
        "/v1/chat/completions",
        json={**_CHAT_BODY, "stream": True},
        headers=_auth(_cred()),
    )
    assert resp.status_code == 200
    assert resp.text == _CHAT_SSE_USAGE

    types = [c["type"] for c in emitted]
    assert types == ["llm_request", "llm_response"]
    payload = emitted[1]["payload"]
    assert payload["usage"] == {
        "prompt_tokens": 7,
        "completion_tokens": 3,
        "total_tokens": 10,
    }
    assert "usage_estimated" not in payload
    assert payload["http_status"] == 200
    assert payload["latency_ms"] >= 0


def test_stream_without_usage_emits_estimated(
    gw_client: TestClient, upstream: Any, emitted: list
) -> None:
    """Provider ignored include_usage → estimate completion_tokens from the
    streamed delta text (chars//4), prompt_tokens null, flagged."""
    upstream["handler"] = _sse_handler(_CHAT_SSE_NO_USAGE)
    resp = gw_client.post(
        "/v1/chat/completions",
        json={**_CHAT_BODY, "stream": True},
        headers=_auth(_cred()),
    )
    assert resp.status_code == 200
    assert resp.text == _CHAT_SSE_NO_USAGE

    types = [c["type"] for c in emitted]
    assert types == ["llm_request", "llm_response"]
    payload = emitted[1]["payload"]
    # "hello" + " wor" = 9 chars → 9 // 4 = 2 estimated completion tokens.
    assert payload["usage"] == {
        "prompt_tokens": None,
        "completion_tokens": 2,
        "total_tokens": 2,
    }
    assert payload["usage_estimated"] is True


def test_injected_stream_options_never_leaks_to_client_stream(
    gw_client: TestClient, upstream: Any, emitted: list
) -> None:
    """The gateway-injected ``stream_options`` request key must not appear
    in the client-facing stream; the usage frame itself passes through
    (same shape OpenAI emits for caller-requested include_usage)."""
    upstream["handler"] = _sse_handler(_CHAT_SSE_USAGE)
    resp = gw_client.post(
        "/v1/chat/completions",
        json={**_CHAT_BODY, "stream": True},
        headers=_auth(_cred()),
    )
    assert resp.status_code == 200
    # Injected upstream; absent from the client stream entirely.
    assert upstream["seen"][0]["json"]["stream_options"] == {
        "include_usage": True
    }
    assert "stream_options" not in resp.text
    # Only real SSE data frames reach the client — no gateway artifacts.
    for line in resp.text.splitlines():
        if line.startswith("data:") and line != "data: [DONE]":
            frame = json.loads(line[len("data:") :])
            assert "stream_options" not in frame


def test_stream_error_frame_emits_error_event(
    gw_client: TestClient, upstream: Any, emitted: list
) -> None:
    """A ``data: {"error": ...}`` frame mid-stream surfaces as an upstream
    error event — the body is still passed through verbatim."""
    upstream["handler"] = _sse_handler(_CHAT_SSE_ERROR_FRAME)
    resp = gw_client.post(
        "/v1/chat/completions",
        json={**_CHAT_BODY, "stream": True},
        headers=_auth(_cred()),
    )
    assert resp.status_code == 200
    assert resp.text == _CHAT_SSE_ERROR_FRAME

    types = [c["type"] for c in emitted]
    assert types == ["llm_request", "llm_response", "error"]
    err = emitted[2]["payload"]
    assert err["stage"] == "upstream"
    assert "model overloaded" in err["message"]


def test_anthropic_stream_without_usage_estimates(
    gw_client: TestClient, upstream: Any, emitted: list
) -> None:
    """``/v1/messages`` stream with no usage events → estimated usage with
    null prompt_tokens (anthropic usage normally rides message_start /
    message_delta — absent here)."""
    upstream["handler"] = _sse_handler(_ANTHROPIC_SSE_NO_USAGE)
    resp = gw_client.post(
        "/v1/messages",
        json={**_MESSAGES_BODY, "stream": True},
        headers=_auth(_cred()),
    )
    assert resp.status_code == 200
    assert resp.text == _ANTHROPIC_SSE_NO_USAGE

    types = [c["type"] for c in emitted]
    assert types == ["llm_request", "llm_response"]
    payload = emitted[1]["payload"]
    # "hello world" = 11 chars → 11 // 4 = 2.
    assert payload["usage"] == {
        "prompt_tokens": None,
        "completion_tokens": 2,
        "total_tokens": 2,
    }
    assert payload["usage_estimated"] is True


def test_anthropic_stream_usage_parsed_when_present(
    gw_client: TestClient, upstream: Any, emitted: list
) -> None:
    sse = (
        'event: message_start\ndata: {"type":"message_start","message":'
        '{"usage":{"input_tokens":9,"output_tokens":1}}}\n\n'
        'event: content_block_delta\ndata: {"type":"content_block_delta",'
        '"delta":{"type":"text_delta","text":"hi"}}\n\n'
        'event: message_delta\ndata: {"type":"message_delta",'
        '"usage":{"output_tokens":7}}\n\n'
    )
    upstream["handler"] = _sse_handler(sse)
    resp = gw_client.post(
        "/v1/messages",
        json={**_MESSAGES_BODY, "stream": True},
        headers=_auth(_cred()),
    )
    assert resp.status_code == 200
    payload = next(c for c in emitted if c["type"] == "llm_response")["payload"]
    assert payload["usage"] == {
        "prompt_tokens": 9,
        "completion_tokens": 7,
        "total_tokens": 16,
    }
    assert "usage_estimated" not in payload


def test_streamed_json_body_meters_provider_usage(
    gw_client: TestClient, upstream: Any, emitted: list
) -> None:
    """Provider that replies with plain JSON to a stream:true request —
    the tee meters the JSON body's usage, not an estimate."""
    upstream["handler"] = lambda _req: httpx.Response(
        200,
        json={
            "id": "chatcmpl-x",
            "object": "chat.completion",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "hi there"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 4,
                "completion_tokens": 2,
                "total_tokens": 6,
            },
        },
    )
    resp = gw_client.post(
        "/v1/chat/completions",
        json={**_CHAT_BODY, "stream": True},
        headers=_auth(_cred()),
    )
    assert resp.status_code == 200
    payload = next(c for c in emitted if c["type"] == "llm_response")["payload"]
    assert payload["usage"] == {
        "prompt_tokens": 4,
        "completion_tokens": 2,
        "total_tokens": 6,
    }
    assert "usage_estimated" not in payload


def test_midstream_transport_failure_emits_error_no_retry(
    gw_client: TestClient, upstream: Any, emitted: list
) -> None:
    """Bytes already reached the client → mid-stream failure is terminal:
    llm_response (partial) + upstream error, no upstream_retry."""

    class _PartialStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield (
                b'data: {"id":"x","object":"chat.completion.chunk",'
                b'"choices":[{"index":0,"delta":{"content":"he"}}]}\n\n'
            )
            raise httpx.ReadError("connection reset mid-stream")

        async def aclose(self) -> None:
            return None

    upstream["handler"] = lambda _req: httpx.Response(
        200,
        stream=_PartialStream(),
        headers={"content-type": "text/event-stream"},
    )
    with pytest.raises(httpx.ReadError):  # truncated stream → client error
        gw_client.post(
            "/v1/chat/completions",
            json={**_CHAT_BODY, "stream": True},
            headers=_auth(_cred()),
        )

    # No retry once the response started streaming.
    assert len(upstream["seen"]) == 1
    assert not any(
        c["type"] == "error" and c["payload"].get("stage") == "upstream_retry"
        for c in emitted
    )
    types = [c["type"] for c in emitted]
    assert "llm_response" in types
    assert "error" in types
    err = next(c for c in emitted if c["type"] == "error")
    assert err["payload"]["stage"] == "upstream"
    assert "ReadError" in err["payload"]["message"]


def test_stream_estimated_usage_on_empty_stream(
    gw_client: TestClient, upstream: Any, emitted: list
) -> None:
    """Degenerate stream (only [DONE]) still meters — a zero estimate is
    better than no event at all."""
    upstream["handler"] = _sse_handler("data: [DONE]\n\n")
    resp = gw_client.post(
        "/v1/chat/completions",
        json={**_CHAT_BODY, "stream": True},
        headers=_auth(_cred()),
    )
    assert resp.status_code == 200
    payload = next(c for c in emitted if c["type"] == "llm_response")["payload"]
    assert payload["usage"] == {
        "prompt_tokens": None,
        "completion_tokens": 0,
        "total_tokens": 0,
    }
    assert payload["usage_estimated"] is True
