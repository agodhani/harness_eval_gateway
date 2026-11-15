"""gateway/main.py — proxy forwarding, metering, budgets, /internal/* auth.

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

from db.models import Event, Run
from gateway import metering
from gateway.config import get_settings
from gateway.creds import DEV_CRED_TOKEN, mint_cred
from gateway.deps import (
    get_ollama_client,
    get_session_factory,
    get_upstream_client,
)
from gateway.main import app

SECRET = "dev-secret-change-me"  # gateway.config default in tests
INGEST_TOKEN = "dev-ingest-token"  # gateway.config default in tests


def _cred(**kwargs: Any) -> str:
    kwargs.setdefault("run_id", uuid.uuid4())
    kwargs.setdefault("sandbox_id", uuid.uuid4())
    kwargs.setdefault("secret", SECRET)
    kwargs.setdefault("ttl_seconds", 3600)
    return mint_cred(**kwargs)


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

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
            base_url="http://upstream.test",
        ) as client:
            yield client

    app.dependency_overrides[get_upstream_client] = _dep
    # /v1/models/pull uses the ollama-pinned dep, not the routed one —
    # same mock transport so pull traffic lands in ``seen`` too.
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


@pytest.fixture()
def ingest_posts(monkeypatch: pytest.MonkeyPatch) -> list[list[dict[str, Any]]]:
    """Capture post_ingest_events calls; always returns 200."""
    posts: list[list[dict[str, Any]]] = []

    async def fake_post(events: list[dict[str, Any]]) -> tuple[int, Any]:
        posts.append(events)
        return 200, {"accepted": len(events)}

    monkeypatch.setattr(metering, "post_ingest_events", fake_post)
    return posts


def _seed_usage(run_id: uuid.UUID, total_tokens: int) -> None:
    session = get_session_factory()()
    try:
        # events.run_id is a real FK — Postgres enforces it (and the api
        # ingest endpoint validates the same invariant), so the parent run
        # row must exist before usage rows can be seeded. The flush is
        # explicit: no ORM relationship orders the run insert before events.
        session.add(Run(id=run_id, task_prompt="test", model_name="test-model"))
        session.flush()
        session.add(
            Event(
                run_id=run_id,
                source="gateway",
                type="llm_response",
                payload={"usage": {"total_tokens": total_tokens}},
            )
        )
        session.commit()
    finally:
        session.close()


_CHAT_BODY = {
    "model": "test-model",
    "messages": [{"role": "user", "content": "hi"}],
}


# ---------------------------------------------------------------------------
# health + auth
# ---------------------------------------------------------------------------


def test_health(gw_client: TestClient) -> None:
    resp = gw_client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_chat_requires_bearer(gw_client: TestClient) -> None:
    assert gw_client.post("/v1/chat/completions", json=_CHAT_BODY).status_code == 401
    assert (
        gw_client.post(
            "/v1/chat/completions", json=_CHAT_BODY, headers={"Authorization": "Basic x"}
        ).status_code
        == 401
    )


def test_chat_rejects_bad_cred(gw_client: TestClient) -> None:
    resp = gw_client.post(
        "/v1/chat/completions",
        json=_CHAT_BODY,
        headers=_auth("heg.definitely.not.valid"),
    )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# /v1/chat/completions — proxy + metering
# ---------------------------------------------------------------------------


def test_chat_completion_proxied_and_metered(
    gw_client: TestClient, upstream: Any, emitted: list
) -> None:
    run_id, sandbox_id = uuid.uuid4(), uuid.uuid4()
    resp = gw_client.post(
        "/v1/chat/completions",
        json={
            **_CHAT_BODY,
            "tools": [{"type": "function", "function": {"name": "t"}}],
            "max_tokens": 16,
        },
        headers=_auth(_cred(run_id=run_id, sandbox_id=sandbox_id)),
    )
    assert resp.status_code == 200
    # upstream body returned verbatim
    assert resp.json()["choices"][0]["message"]["content"] == "hello"
    assert resp.json()["usage"]["total_tokens"] == 5

    # exactly one upstream call, to the OpenAI-compatible path
    assert len(upstream["seen"]) == 1
    sent = upstream["seen"][0]
    assert sent["url"].endswith("/v1/chat/completions")
    assert sent["json"]["stream"] is False
    assert sent["json"]["num_ctx"] == 32_768
    assert sent["json"]["keep_alive"] == "30m"
    assert sent["json"]["model"] == "test-model"
    assert sent["json"]["max_tokens"] == 16

    types = [c["type"] for c in emitted]
    assert types == ["llm_request", "llm_response"]
    req, resp_evt = emitted
    assert req["source"] == "gateway"
    assert req["payload"]["message_count"] == 1
    assert req["payload"]["tool_count"] == 1
    assert req["payload"]["model"] == "test-model"
    assert resp_evt["payload"]["usage"]["total_tokens"] == 5
    assert resp_evt["payload"]["http_status"] == 200
    assert resp_evt["payload"]["latency_ms"] >= 0
    assert resp_evt["run_id"] == str(run_id)
    assert resp_evt["sandbox_id"] == str(sandbox_id)


def test_chat_defaults_non_streaming(
    gw_client: TestClient, upstream: Any, emitted: list
) -> None:
    """Callers that don't ask for SSE get the buffered non-streamed path."""
    resp = gw_client.post(
        "/v1/chat/completions",
        json={**_CHAT_BODY, "stream": False},
        headers=_auth(_cred()),
    )
    assert resp.status_code == 200
    assert upstream["seen"][0]["json"]["stream"] is False


_CHAT_SSE = (
    'data: {"id":"chatcmpl-s","object":"chat.completion.chunk","choices":'
    '[{"index":0,"delta":{"role":"assistant","content":"he"}}]}\n\n'
    'data: {"id":"chatcmpl-s","object":"chat.completion.chunk","choices":'
    '[{"index":0,"delta":{"content":"llo"},"finish_reason":"stop"}]}\n\n'
    'data: {"id":"chatcmpl-s","object":"chat.completion.chunk","choices":[],'
    '"usage":{"prompt_tokens":7,"completion_tokens":3,"total_tokens":10}}\n\n'
    "data: [DONE]\n\n"
)


def test_chat_streaming_passthrough_and_metered(
    gw_client: TestClient, upstream: Any, emitted: list
) -> None:
    """``stream: true`` callers (pi et al.) get a metered SSE pass-through."""
    upstream["handler"] = lambda _req: httpx.Response(
        200,
        text=_CHAT_SSE,
        headers={"content-type": "text/event-stream"},
    )
    resp = gw_client.post(
        "/v1/chat/completions",
        json={**_CHAT_BODY, "stream": True},
        headers=_auth(_cred()),
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert resp.text == _CHAT_SSE

    sent = upstream["seen"][0]
    assert sent["url"].endswith("/v1/chat/completions")
    # Stream flag preserved + usage chunk requested for metering.
    assert sent["json"]["stream"] is True
    assert sent["json"]["stream_options"]["include_usage"] is True
    assert sent["json"]["num_ctx"] == 32_768

    types = [c["type"] for c in emitted]
    assert types == ["llm_request", "llm_response"]
    usage = emitted[1]["payload"]["usage"]
    assert usage == {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}


def test_chat_streaming_preserves_caller_stream_options(
    gw_client: TestClient, upstream: Any, emitted: list
) -> None:
    upstream["handler"] = lambda _req: httpx.Response(
        200, text="data: [DONE]\n\n", headers={"content-type": "text/event-stream"}
    )
    gw_client.post(
        "/v1/chat/completions",
        json={
            **_CHAT_BODY,
            "stream": True,
            "stream_options": {"include_usage": True, "extra": 1},
        },
        headers=_auth(_cred()),
    )
    sent = upstream["seen"][0]["json"]
    assert sent["stream_options"] == {"include_usage": True, "extra": 1}


def test_chat_does_not_override_client_ollama_params(
    gw_client: TestClient, upstream: Any, emitted: list
) -> None:
    gw_client.post(
        "/v1/chat/completions",
        json={**_CHAT_BODY, "num_ctx": 8192, "keep_alive": "5m"},
        headers=_auth(_cred()),
    )
    sent = upstream["seen"][0]["json"]
    assert sent["num_ctx"] == 8192
    assert sent["keep_alive"] == "5m"


def test_chat_dev_cred(gw_client: TestClient, upstream: Any, emitted: list) -> None:
    resp = gw_client.post(
        "/v1/chat/completions", json=_CHAT_BODY, headers=_auth(DEV_CRED_TOKEN)
    )
    assert resp.status_code == 200
    # dev cred maps to the fixed dev UUIDs
    assert emitted[-1]["run_id"] == "de000000-0000-4000-8000-000000000001"


def test_chat_upstream_error_passthrough(
    gw_client: TestClient, upstream: Any, emitted: list
) -> None:
    upstream["handler"] = lambda _req: httpx.Response(
        500, json={"error": {"message": "ollama exploded"}}
    )
    resp = gw_client.post(
        "/v1/chat/completions", json=_CHAT_BODY, headers=_auth(_cred())
    )
    assert resp.status_code == 500
    assert resp.json()["error"]["message"] == "ollama exploded"
    types = [c["type"] for c in emitted]
    assert "llm_response" in types  # metered with http_status=500
    assert "error" in types
    err = next(c for c in emitted if c["type"] == "error")
    assert err["payload"]["stage"] == "upstream"
    assert err["payload"]["http_status"] == 500


def test_chat_upstream_exception_returns_502(
    gw_client: TestClient, upstream: Any, emitted: list
) -> None:
    def boom(_req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    upstream["handler"] = boom
    resp = gw_client.post(
        "/v1/chat/completions", json=_CHAT_BODY, headers=_auth(_cred())
    )
    assert resp.status_code == 502
    assert resp.json()["error"]["type"] == "upstream_error"
    types = [c["type"] for c in emitted]
    assert "error" in types
    assert "llm_response" not in types  # no response received -> not metered


# ---------------------------------------------------------------------------
# budgets
# ---------------------------------------------------------------------------


def test_budget_exceeded_by_cred_max_tokens(
    gw_client: TestClient, upstream: Any, emitted: list
) -> None:
    run_id = uuid.uuid4()
    _seed_usage(run_id, 100)
    cred = _cred(run_id=run_id, max_tokens=50)

    resp = gw_client.post(
        "/v1/chat/completions",
        json={**_CHAT_BODY, "max_tokens": 10},
        headers=_auth(cred),
    )
    assert resp.status_code == 429
    assert resp.json()["error"]["type"] == "budget_exceeded"
    # upstream never called; llm_request never emitted
    assert upstream["seen"] == []
    assert [c["type"] for c in emitted] == ["error"]
    err = emitted[0]
    assert err["payload"]["reason"] == "budget_exceeded"
    assert err["payload"]["used_tokens"] == 100
    assert err["payload"]["budget"] == 50


def test_budget_allows_under_limit(
    gw_client: TestClient, upstream: Any, emitted: list
) -> None:
    run_id = uuid.uuid4()
    _seed_usage(run_id, 100)
    cred = _cred(run_id=run_id, max_tokens=150)

    resp = gw_client.post(
        "/v1/chat/completions",
        json={**_CHAT_BODY, "max_tokens": 10},  # 100 + 10 <= 150
        headers=_auth(cred),
    )
    assert resp.status_code == 200
    assert len(upstream["seen"]) == 1


def test_budget_uses_env_default(
    gw_client: TestClient, upstream: Any, emitted: list, monkeypatch
) -> None:
    monkeypatch.setattr(get_settings(), "DEFAULT_RUN_TOKEN_BUDGET", 60)
    run_id = uuid.uuid4()
    _seed_usage(run_id, 100)  # already over the 60 budget

    resp = gw_client.post(
        "/v1/chat/completions",
        json=_CHAT_BODY,  # no max_tokens estimate needed — already over
        headers=_auth(_cred(run_id=run_id)),
    )
    assert resp.status_code == 429
    assert upstream["seen"] == []


# ---------------------------------------------------------------------------
# /internal/events
# ---------------------------------------------------------------------------


def test_internal_events_stamps_ids_from_cred(
    gw_client: TestClient, ingest_posts: list
) -> None:
    run_id, sandbox_id = uuid.uuid4(), uuid.uuid4()
    # NOTE: no run_id/sandbox_id in the event — the cred supplies them.
    resp = gw_client.post(
        "/internal/events",
        json={
            "events": [
                {
                    "source": "harness",
                    "type": "tool_call",
                    "seq": 7,
                    "payload": {"tool": "shell"},
                }
            ]
        },
        headers=_auth(_cred(run_id=run_id, sandbox_id=sandbox_id)),
    )
    assert resp.status_code == 200
    assert resp.json() == {"accepted": 1}
    assert len(ingest_posts) == 1
    ev = ingest_posts[0][0]
    assert ev["run_id"] == str(run_id)
    assert ev["sandbox_id"] == str(sandbox_id)
    assert ev["source"] == "harness"
    assert ev["type"] == "tool_call"
    assert ev["seq"] == 7
    assert ev["payload"] == {"tool": "shell"}


def test_internal_events_single_object_and_spoof_override(
    gw_client: TestClient, ingest_posts: list
) -> None:
    run_id, sandbox_id = uuid.uuid4(), uuid.uuid4()
    other_run = uuid.uuid4()
    resp = gw_client.post(
        "/internal/events",
        json={
            # bare single event (miniloop emitter shape) trying to spoof run_id
            "run_id": str(other_run),
            "sandbox_id": str(uuid.uuid4()),
            "source": "harness",
            "type": "file_op",
            "payload": {"path": "/workspace/x"},
        },
        headers=_auth(_cred(run_id=run_id, sandbox_id=sandbox_id)),
    )
    assert resp.status_code == 200
    ev = ingest_posts[0][0]
    # cred wins over any client-supplied ids
    assert ev["run_id"] == str(run_id)
    assert ev["sandbox_id"] == str(sandbox_id)


def test_internal_events_ingest_token_passthrough(
    gw_client: TestClient, ingest_posts: list
) -> None:
    run_id, sandbox_id = uuid.uuid4(), uuid.uuid4()
    resp = gw_client.post(
        "/internal/events",
        json={
            "events": [
                {
                    "run_id": str(run_id),
                    "sandbox_id": str(sandbox_id),
                    "source": "evaluator",
                    "type": "checkpoint",
                    "payload": {},
                }
            ]
        },
        headers=_auth(INGEST_TOKEN),
    )
    assert resp.status_code == 200
    ev = ingest_posts[0][0]
    # pass-through: ids preserved verbatim
    assert ev["run_id"] == str(run_id)
    assert ev["sandbox_id"] == str(sandbox_id)


def test_internal_events_requires_auth(gw_client: TestClient) -> None:
    assert (
        gw_client.post(
            "/internal/events",
            json={"events": [{"source": "harness", "type": "tool_call"}]},
        ).status_code
        == 401
    )


# ---------------------------------------------------------------------------
# /internal/done
# ---------------------------------------------------------------------------


def test_internal_done_emits_done_signal(
    gw_client: TestClient, ingest_posts: list
) -> None:
    run_id, sandbox_id = uuid.uuid4(), uuid.uuid4()
    resp = gw_client.post(
        "/internal/done",
        json={"summary": "built the thing", "status": "done"},
        headers=_auth(_cred(run_id=run_id, sandbox_id=sandbox_id)),
    )
    assert resp.status_code == 200
    ev = ingest_posts[0][0]
    assert ev["type"] == "done_signal"
    assert ev["source"] == "sandbox"
    assert ev["run_id"] == str(run_id)
    assert ev["sandbox_id"] == str(sandbox_id)
    assert ev["payload"] == {"summary": "built the thing", "status": "done"}


def test_internal_done_status_default_and_auth(
    gw_client: TestClient, ingest_posts: list
) -> None:
    resp = gw_client.post(
        "/internal/done", json={"summary": "x"}, headers=_auth(_cred())
    )
    assert resp.status_code == 200
    assert ingest_posts[0][0]["payload"]["status"] == "done"

    assert (
        gw_client.post("/internal/done", json={"summary": "x"}).status_code == 401
    )


# ---------------------------------------------------------------------------
# Phase 2.5 — protocol pass-throughs
# ---------------------------------------------------------------------------

_MESSAGES_BODY = {
    "model": "test-model",
    "max_tokens": 64,
    "messages": [{"role": "user", "content": "hi"}],
}

_ANTHROPIC_OK = {
    "id": "msg_test",
    "type": "message",
    "role": "assistant",
    "content": [{"type": "text", "text": "hello"}],
    "usage": {"input_tokens": 12, "output_tokens": 4},
}

_ANTHROPIC_SSE = (
    'event: message_start\ndata: {"type":"message_start","message":'
    '{"usage":{"input_tokens":9,"output_tokens":1}}}\n\n'
    'event: content_block_delta\ndata: {"type":"content_block_delta",'
    '"delta":{"type":"text_delta","text":"hi"}}\n\n'
    'event: message_delta\ndata: {"type":"message_delta",'
    '"usage":{"output_tokens":7}}\n\n'
)

_RESPONSES_OK = {
    "id": "resp_test",
    "object": "response",
    "status": "completed",
    "output": [],
    "usage": {"input_tokens": 20, "output_tokens": 6, "total_tokens": 26},
}


def test_messages_requires_bearer(gw_client: TestClient) -> None:
    assert gw_client.post("/v1/messages", json=_MESSAGES_BODY).status_code == 401
    assert gw_client.post("/v1/responses", json={}).status_code == 401
    assert gw_client.get("/v1/models").status_code == 401


def test_messages_accepts_x_api_key(
    gw_client: TestClient, upstream: Any, emitted: list
) -> None:
    """Anthropic SDK convention: ``x-api-key`` carries the cred (dsh)."""
    upstream["handler"] = lambda _req: httpx.Response(200, json=_ANTHROPIC_OK)
    resp = gw_client.post(
        "/v1/messages",
        json=_MESSAGES_BODY,
        headers={"x-api-key": _cred()},
    )
    assert resp.status_code == 200
    assert [c["type"] for c in emitted] == ["llm_request", "llm_response"]


def test_messages_nonstream_passthrough_and_metered(
    gw_client: TestClient, upstream: Any, emitted: list
) -> None:
    upstream["handler"] = lambda _req: httpx.Response(200, json=_ANTHROPIC_OK)
    run_id, sandbox_id = uuid.uuid4(), uuid.uuid4()
    resp = gw_client.post(
        "/v1/messages",
        json=_MESSAGES_BODY,
        headers=_auth(_cred(run_id=run_id, sandbox_id=sandbox_id)),
    )
    assert resp.status_code == 200
    assert resp.json()["usage"]["output_tokens"] == 4

    sent = upstream["seen"][0]
    assert sent["url"].endswith("/v1/messages")
    # Verbatim forward: no stream flag injected when the caller didn't set one.
    assert "stream" not in sent["json"]
    assert "num_ctx" not in sent["json"]

    types = [c["type"] for c in emitted]
    assert types == ["llm_request", "llm_response"]
    usage = emitted[1]["payload"]["usage"]
    assert usage == {"prompt_tokens": 12, "completion_tokens": 4, "total_tokens": 16}


def test_messages_streaming_passthrough_and_metered(
    gw_client: TestClient, upstream: Any, emitted: list
) -> None:
    upstream["handler"] = lambda _req: httpx.Response(
        200,
        text=_ANTHROPIC_SSE,
        headers={"content-type": "text/event-stream"},
    )
    resp = gw_client.post(
        "/v1/messages",
        json={**_MESSAGES_BODY, "stream": True},
        headers=_auth(_cred()),
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    # Body passed through verbatim.
    assert resp.text == _ANTHROPIC_SSE
    # The request kept its stream flag upstream.
    assert upstream["seen"][0]["json"]["stream"] is True
    # Usage teed from the SSE stream: message_start input + final delta output.
    types = [c["type"] for c in emitted]
    assert types == ["llm_request", "llm_response"]
    usage = emitted[1]["payload"]["usage"]
    assert usage == {"prompt_tokens": 9, "completion_tokens": 7, "total_tokens": 16}


def test_messages_budget_gate(
    gw_client: TestClient, upstream: Any, emitted: list
) -> None:
    run_id = uuid.uuid4()
    _seed_usage(run_id, 100)
    cred = _cred(run_id=run_id, max_tokens=50)
    resp = gw_client.post(
        "/v1/messages",
        json=_MESSAGES_BODY,
        headers=_auth(cred),
    )
    assert resp.status_code == 429
    assert upstream["seen"] == []
    assert [c["type"] for c in emitted] == ["error"]


def test_count_tokens_unmetered(
    gw_client: TestClient, upstream: Any, emitted: list
) -> None:
    upstream["handler"] = lambda _req: httpx.Response(
        200, json={"input_tokens": 42}
    )
    resp = gw_client.post(
        "/v1/messages/count_tokens",
        json=_MESSAGES_BODY,
        headers=_auth(_cred()),
    )
    assert resp.status_code == 200
    assert resp.json() == {"input_tokens": 42}
    assert upstream["seen"][0]["url"].endswith("/v1/messages/count_tokens")
    # Not a generation call — no llm_request/llm_response emitted.
    assert emitted == []


def test_responses_passthrough_and_metered(
    gw_client: TestClient, upstream: Any, emitted: list
) -> None:
    upstream["handler"] = lambda _req: httpx.Response(200, json=_RESPONSES_OK)
    resp = gw_client.post(
        "/v1/responses",
        json={"model": "test-model", "input": "hi", "max_output_tokens": 32},
        headers=_auth(_cred()),
    )
    assert resp.status_code == 200
    assert resp.json()["usage"]["total_tokens"] == 26
    assert upstream["seen"][0]["url"].endswith("/v1/responses")
    types = [c["type"] for c in emitted]
    assert types == ["llm_request", "llm_response"]
    usage = emitted[1]["payload"]["usage"]
    assert usage == {"prompt_tokens": 20, "completion_tokens": 6, "total_tokens": 26}


def test_responses_stream_usage_tee(
    gw_client: TestClient, upstream: Any, emitted: list
) -> None:
    sse = (
        'event: response.output_text.delta\ndata: {"type":'
        '"response.output_text.delta","delta":"hi"}\n\n'
        'event: response.completed\ndata: {"type":"response.completed",'
        '"response":{"usage":{"input_tokens":8,"output_tokens":3}}}\n\n'
    )
    upstream["handler"] = lambda _req: httpx.Response(
        200, text=sse, headers={"content-type": "text/event-stream"}
    )
    resp = gw_client.post(
        "/v1/responses",
        json={"model": "m", "input": "hi", "stream": True},
        headers=_auth(_cred()),
    )
    assert resp.status_code == 200
    assert resp.text == sse
    usage = emitted[1]["payload"]["usage"]
    assert usage == {"prompt_tokens": 8, "completion_tokens": 3, "total_tokens": 11}


def test_responses_budget_gate(
    gw_client: TestClient, upstream: Any, emitted: list
) -> None:
    run_id = uuid.uuid4()
    _seed_usage(run_id, 100)
    cred = _cred(run_id=run_id, max_tokens=50)
    resp = gw_client.post(
        "/v1/responses",
        json={"model": "m", "input": "hi", "max_output_tokens": 10},
        headers=_auth(cred),
    )
    assert resp.status_code == 429
    assert upstream["seen"] == []


def test_list_models_passthrough(
    gw_client: TestClient, upstream: Any, emitted: list
) -> None:
    upstream["handler"] = lambda _req: httpx.Response(
        200, json={"object": "list", "data": [{"id": "m1"}]}
    )
    resp = gw_client.get("/v1/models", headers=_auth(_cred()))
    assert resp.status_code == 200
    assert resp.json()["data"][0]["id"] == "m1"
    assert upstream["seen"][0]["url"].endswith("/v1/models")
    assert emitted == []  # probes are not metered
    # PR1: the api's pull_supported flag reads the active provider here.
    assert resp.headers["x-heg-provider"] == "ollama"


# ---------------------------------------------------------------------------
# PR1 — POST /v1/models/pull (ollama-only model install, streamed passthrough)
#
# Pull is control-plane only: creds carrying a sandbox_id get 403 — the api
# mints run-scoped service creds (sandbox_id=None) for /api/models/*, so
# tests mint the same shape via _cred(sandbox_id=None).
# ---------------------------------------------------------------------------

_PULL_NDJSON = b'{"status":"pulling manifest"}\n{"status":"success"}\n'


def _service_cred(**kwargs: Any) -> str:
    """Run-scoped service cred (sandbox_id=None) — what the api mints for
    /v1/models and /v1/models/pull."""
    return _cred(sandbox_id=None, **kwargs)


def test_pull_models_streams_passthrough(
    gw_client: TestClient, upstream: Any, emitted: list
) -> None:
    upstream["handler"] = lambda _req: httpx.Response(
        200,
        content=_PULL_NDJSON,
        headers={"content-type": "application/x-ndjson"},
    )
    resp = gw_client.post(
        "/v1/models/pull",
        json={"name": "qwen3:8b"},
        headers=_auth(_service_cred()),
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/x-ndjson")
    assert resp.content == _PULL_NDJSON

    sent = upstream["seen"][0]
    assert sent["url"].endswith("/api/pull")
    assert sent["json"] == {"name": "qwen3:8b", "stream": True}
    assert emitted == []  # not a generation call — unmetered


def test_pull_models_requires_bearer(gw_client: TestClient) -> None:
    assert gw_client.post("/v1/models/pull", json={"name": "m"}).status_code == 401


def test_pull_models_sandbox_cred_forbidden(
    gw_client: TestClient, upstream: Any
) -> None:
    """Sandbox participant creds (sandbox_id set) may not pull — a pull is a
    host-capability escalation (disk/bandwidth, arbitrary registries)."""
    resp = gw_client.post(
        "/v1/models/pull", json={"name": "m"}, headers=_auth(_cred())
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["type"] == "forbidden"
    assert upstream["seen"] == []


def test_pull_models_dev_cred_forbidden(
    gw_client: TestClient, upstream: Any
) -> None:
    """``heg-dev`` maps to a dev sandbox id — refused like any
    sandbox-shaped cred (the token is a public repo constant)."""
    resp = gw_client.post(
        "/v1/models/pull", json={"name": "m"}, headers=_auth(DEV_CRED_TOKEN)
    )
    assert resp.status_code == 403
    assert upstream["seen"] == []


def test_pull_models_strips_body_to_allowlist(
    gw_client: TestClient, upstream: Any
) -> None:
    """Only ollama's pull selectors (name/model) forward; ``stream`` is
    forced true and ``insecure``/unknown fields are dropped — caller-set
    ``insecure`` would enable plaintext-HTTP registry pulls."""
    upstream["handler"] = lambda _req: httpx.Response(
        200, content=_PULL_NDJSON, headers={"content-type": "application/x-ndjson"}
    )
    resp = gw_client.post(
        "/v1/models/pull",
        json={
            "name": "qwen3:8b",
            "stream": False,
            "insecure": True,
            "extra": "dropped",
        },
        headers=_auth(_service_cred()),
    )
    assert resp.status_code == 200
    assert upstream["seen"][0]["json"] == {"name": "qwen3:8b", "stream": True}

    # ``model`` is the newer-server alias for ``name`` — also allowlisted.
    resp = gw_client.post(
        "/v1/models/pull", json={"model": "qwen3:8b"}, headers=_auth(_service_cred())
    )
    assert resp.status_code == 200
    assert upstream["seen"][1]["json"] == {"model": "qwen3:8b", "stream": True}


def test_pull_models_upstream_error_status_forwarded(
    gw_client: TestClient, upstream: Any
) -> None:
    upstream["handler"] = lambda _req: httpx.Response(
        404, json={"error": "model not found"}
    )
    resp = gw_client.post(
        "/v1/models/pull",
        json={"name": "nope:1b"},
        headers=_auth(_service_cred()),
    )
    assert resp.status_code == 404


def test_pull_models_openrouter_provider_501(
    gw_client: TestClient, upstream: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from gateway import main as gw_main
    from gateway.config import GatewaySettings

    monkeypatch.setattr(
        gw_main,
        "get_settings",
        lambda: GatewaySettings(
            _env_file=None,
            UPSTREAM_PROVIDER="openrouter",
            OPENROUTER_API_KEY="sk-or-k",
        ),
    )
    resp = gw_client.post(
        "/v1/models/pull", json={"name": "m"}, headers=_auth(_service_cred())
    )
    assert resp.status_code == 501
    assert "ollama" in resp.json()["error"]["message"]
    assert upstream["seen"] == []  # nothing forwarded upstream


def test_passthrough_upstream_error_emits(
    gw_client: TestClient, upstream: Any, emitted: list
) -> None:
    upstream["handler"] = lambda _req: httpx.Response(
        500, json={"error": {"type": "overloaded_error", "message": "x"}}
    )
    resp = gw_client.post(
        "/v1/messages", json=_MESSAGES_BODY, headers=_auth(_cred())
    )
    assert resp.status_code == 500
    types = [c["type"] for c in emitted]
    assert "llm_response" in types
    assert "error" in types
    err = next(c for c in emitted if c["type"] == "error")
    assert err["payload"]["http_status"] == 500


def test_ingest_forward_failure_returns_502(
    gw_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def unreachable(events: list[dict[str, Any]]) -> tuple[int, Any]:
        return 502, {"error": {"message": "ingest unreachable", "type": "ingest_error"}}

    monkeypatch.setattr(metering, "post_ingest_events", unreachable)
    resp = gw_client.post(
        "/internal/done", json={"summary": "x"}, headers=_auth(_cred())
    )
    assert resp.status_code == 502
