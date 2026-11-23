"""gateway PR30 — per-call speed metrics on ``llm_response`` payloads.

``ttft_ms`` (time to first response byte/chunk), ``total_ms``, and the
derived ``tokens_per_sec`` are recorded on both metered paths — the
buffered ``/v1/chat/completions`` read and the streamed metering tee.

Behavioral oracles (independent of the implementation):
- an upstream that pauses between body chunks must yield ``ttft_ms``
  strictly less than ``total_ms`` on the emitted event — a code path that
  never measures the first byte separately could not produce that order;
- ``tokens_per_sec`` is ``completion_tokens / (total_ms / 1000)`` — a
  per-MINUTE rate, a prompt-token rate, or a first-byte-only rate would
  all compute a different number;
- cache replays carry ``tokens_per_sec`` null — the replay times the
  store, not the model, so an honest rate doesn't exist.

Hermetic: httpx.MockTransport upstream + monkeypatched event emission.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from gateway import metering
from gateway.creds import CredClaims, mint_cred
from gateway.deps import get_ollama_client, get_upstream_client
from gateway.main import app

SECRET = "dev-secret-change-me"  # gateway.config default in tests

_CHAT_BODY = {
    "model": "test-model",
    "messages": [{"role": "user", "content": "hi"}],
}

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

# Two SSE frames: one content chunk, then the terminal usage chunk —
# split so a pause between them separates first-byte time from total.
_SSE_HEAD = (
    'data: {"id":"chatcmpl-s","object":"chat.completion.chunk","choices":'
    '[{"index":0,"delta":{"role":"assistant","content":"he"}}]}\n\n'
)
_SSE_TAIL = (
    'data: {"id":"chatcmpl-s","object":"chat.completion.chunk","choices":'
    '[{"index":0,"delta":{"content":"llo"},"finish_reason":"stop"}]}\n\n'
    'data: {"id":"chatcmpl-s","object":"chat.completion.chunk","choices":[],'
    '"usage":{"prompt_tokens":7,"completion_tokens":3,"total_tokens":10}}\n\n'
    "data: [DONE]\n\n"
)


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
        return httpx.Response(200, json=_UPSTREAM_OK)

    state["handler"] = default_handler

    def dispatch(request: httpx.Request) -> httpx.Response:
        state["seen"].append({"url": str(request.url)})
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
        calls.append({"source": source, "type": event_type, "payload": payload})

    monkeypatch.setattr(metering, "emit_event", fake_emit_event)
    return calls


class _DelayedStream(httpx.AsyncByteStream):
    """Byte stream that pauses BEFORE each chunk after the first — the
    first byte arrives fast while the body completes slowly, which is
    exactly what ttft < total must observe."""

    def __init__(self, chunks: list[bytes], delay_s: float) -> None:
        self._chunks = chunks
        self._delay = delay_s

    async def __aiter__(self):
        for i, chunk in enumerate(self._chunks):
            if i:
                await asyncio.sleep(self._delay)
            yield chunk

    async def aclose(self) -> None:
        return None


def _llm_response_payload(emitted: list[dict[str, Any]]) -> dict[str, Any]:
    return next(c for c in emitted if c["type"] == "llm_response")["payload"]


# ---------------------------------------------------------------------------
# buffered path — /v1/chat/completions without stream
# ---------------------------------------------------------------------------


def test_buffered_response_carries_speed_fields(
    gw_client: TestClient, upstream: Any, emitted: list
) -> None:
    """A delayed-second-chunk body yields ttft < total: the first byte is
    measured off the wire, not approximated as the whole read."""
    body = json.dumps(_UPSTREAM_OK).encode()
    mid = len(body) // 2
    upstream["handler"] = lambda _req: httpx.Response(
        200,
        stream=_DelayedStream([body[:mid], body[mid:]], delay_s=0.05),
        headers={"content-type": "application/json"},
    )
    resp = gw_client.post(
        "/v1/chat/completions",
        json=_CHAT_BODY,
        headers=_auth(_cred()),
    )
    assert resp.status_code == 200
    # Body reassembled intact through the timed read — client sees the
    # same JSON as before.
    assert resp.json()["choices"][0]["message"]["content"] == "hello"

    payload = _llm_response_payload(emitted)
    # total_ms mirrors the established latency_ms field.
    assert payload["total_ms"] == payload["latency_ms"]
    # ~50ms pause before chunk 2 → total covers it; the first byte didn't.
    assert payload["total_ms"] >= 40
    assert 0 <= payload["ttft_ms"] < payload["total_ms"]
    # derived oracle: 2 completion tokens over total wall seconds.
    expected = round(2 / (payload["total_ms"] / 1000), 1)
    assert payload["tokens_per_sec"] == pytest.approx(expected)
    assert payload["tokens_per_sec"] <= 50  # 2 tok in >=40ms — any faster
    # reading would mean the delay wasn't counted in the total.


def test_buffered_instant_response_fields_present(
    gw_client: TestClient, upstream: Any, emitted: list
) -> None:
    """A no-delay buffered body still emits the speed fields — ttft is
    measured, tok/s derives whenever a completion count exists."""
    resp = gw_client.post(
        "/v1/chat/completions",
        json=_CHAT_BODY,
        headers=_auth(_cred()),
    )
    assert resp.status_code == 200
    payload = _llm_response_payload(emitted)
    assert payload["ttft_ms"] is not None
    assert 0 <= payload["ttft_ms"] <= payload["total_ms"]
    # completion=2 over the measured (possibly ~1ms) latency.
    if payload["total_ms"] > 0:
        assert payload["tokens_per_sec"] == pytest.approx(
            round(2 / (payload["total_ms"] / 1000), 1)
        )


# ---------------------------------------------------------------------------
# streamed path — metered SSE tee
# ---------------------------------------------------------------------------


def test_streamed_response_ttft_precedes_total(
    gw_client: TestClient, upstream: Any, emitted: list
) -> None:
    """A pause between SSE frames yields ttft < total: first-chunk time is
    recorded at the first upstream chunk, not at stream end."""
    upstream["handler"] = lambda _req: httpx.Response(
        200,
        stream=_DelayedStream(
            [_SSE_HEAD.encode(), _SSE_TAIL.encode()], delay_s=0.05
        ),
        headers={"content-type": "text/event-stream"},
    )
    resp = gw_client.post(
        "/v1/chat/completions",
        json={**_CHAT_BODY, "stream": True},
        headers=_auth(_cred()),
    )
    assert resp.status_code == 200
    assert "chat.completion.chunk" in resp.text

    payload = _llm_response_payload(emitted)
    assert payload["total_ms"] == payload["latency_ms"]
    assert payload["total_ms"] >= 40
    assert 0 <= payload["ttft_ms"] < payload["total_ms"]
    # Provider usage chunk honored → completion=3 drives the rate.
    assert payload["usage"]["completion_tokens"] == 3
    expected = round(3 / (payload["total_ms"] / 1000), 1)
    assert payload["tokens_per_sec"] == pytest.approx(expected)


def test_streamed_response_without_delay_has_real_ttft(
    gw_client: TestClient, upstream: Any, emitted: list
) -> None:
    """Instant upstream still records a bounded, non-negative ttft — the
    field is always present on metered llm_response payloads."""
    upstream["handler"] = lambda _req: httpx.Response(
        200,
        text=_SSE_HEAD + _SSE_TAIL,
        headers={"content-type": "text/event-stream"},
    )
    resp = gw_client.post(
        "/v1/chat/completions",
        json={**_CHAT_BODY, "stream": True},
        headers=_auth(_cred()),
    )
    assert resp.status_code == 200
    payload = _llm_response_payload(emitted)
    assert payload["ttft_ms"] is not None
    assert 0 <= payload["ttft_ms"] <= payload["total_ms"]


# ---------------------------------------------------------------------------
# emit_llm_response — derivation + fallbacks (worked-oracle unit tests)
# ---------------------------------------------------------------------------


def _claims() -> CredClaims:
    import time as _time

    return CredClaims(
        run_id=str(uuid.uuid4()),
        sandbox_id=str(uuid.uuid4()),
        iat=int(_time.time()),
        exp=int(_time.time()) + 3600,
    )


def _emit(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Capture emit_event payloads — same seam the proxy tests use."""
    calls: list[dict[str, Any]] = []

    async def fake(claims, *, source, event_type, payload) -> None:
        calls.append(payload)

    monkeypatch.setattr(metering, "emit_event", fake)
    return calls


def test_emit_llm_response_derives_speed_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ttft rides through; total mirrors latency; tok/s = out/(total_s).

    Oracle by hand: 100 completion tokens in 2000ms → 50.0 tok/s.
    """
    calls = _emit(monkeypatch)
    asyncio.run(
        metering.emit_llm_response(
            _claims(),
            model="m",
            usage={
                "prompt_tokens": 10,
                "completion_tokens": 100,
                "total_tokens": 110,
            },
            latency_ms=2000,
            ttft_ms=250,
            http_status=200,
        )
    )
    (p,) = calls
    assert p["total_ms"] == 2000
    assert p["latency_ms"] == 2000
    assert p["ttft_ms"] == 250
    assert p["tokens_per_sec"] == 50.0


def test_emit_llm_response_ttft_defaults_to_total(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No measured first byte (pre-start disconnect, cache replay) →
    ttft reports the total: no byte arrived inside the call's span."""
    calls = _emit(monkeypatch)
    asyncio.run(
        metering.emit_llm_response(
            _claims(),
            model="m",
            usage={"prompt_tokens": 1, "completion_tokens": 4, "total_tokens": 5},
            latency_ms=80,
            http_status=200,
        )
    )
    (p,) = calls
    assert p["ttft_ms"] == 80
    assert p["total_ms"] == 80


def test_emit_llm_response_cached_nulls_tokens_per_sec(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cache replay times the store, not the model — the speed fields
    stay present but the rate is honestly null."""
    calls = _emit(monkeypatch)
    asyncio.run(
        metering.emit_llm_response(
            _claims(),
            model="m",
            usage={"prompt_tokens": 1, "completion_tokens": 500, "total_tokens": 501},
            latency_ms=2,
            http_status=200,
            cached=True,
        )
    )
    (p,) = calls
    assert p["cached"] is True
    assert p["tokens_per_sec"] is None
    # ttft/total still real: they measure the replay's serve time.
    assert p["ttft_ms"] == 2
    assert p["total_ms"] == 2


def test_emit_llm_response_no_rate_without_measurable_inputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No completion count or a zero elapsed → null rate, not div/0."""
    calls = _emit(monkeypatch)
    asyncio.run(
        metering.emit_llm_response(
            _claims(),
            model="m",
            # estimated usage leaves prompt null; completion 0 → rate 0.0
            usage={"prompt_tokens": None, "completion_tokens": 0, "total_tokens": 0},
            latency_ms=120,
            http_status=200,
        )
    )
    assert calls[0]["tokens_per_sec"] == 0.0

    calls.clear()
    asyncio.run(
        metering.emit_llm_response(
            _claims(),
            model="m",
            usage={"prompt_tokens": 1, "completion_tokens": 9, "total_tokens": 10},
            latency_ms=0,
            http_status=200,
        )
    )
    assert calls[0]["tokens_per_sec"] is None

    calls.clear()
    asyncio.run(
        metering.emit_llm_response(
            _claims(),
            model="m",
            usage={"prompt_tokens": 1, "completion_tokens": None, "total_tokens": 1},
            latency_ms=500,
            http_status=200,
        )
    )
    assert calls[0]["tokens_per_sec"] is None
