"""gateway/cache.py + PR18 replay wiring — hit/miss, keying, streaming vs
non-streaming, budget interplay, invalidation.

Hermetic: the upstream is an httpx.MockTransport injected via dependency
override, ingest emission is monkeypatched, and the cache is a tmp-path
sqlite — no live upstream/api/Postgres.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from db.models import Event, Run
from gateway import deps, main, metering
from gateway.cache import ResponseCache
from gateway.config import GatewaySettings, get_settings
from gateway.creds import mint_cred
from gateway.deps import get_response_cache, get_session_factory, get_upstream_client
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

_CHAT_SSE = (
    'data: {"id":"chatcmpl-s","object":"chat.completion.chunk","choices":'
    '[{"index":0,"delta":{"role":"assistant","content":"he"}}]}\n\n'
    'data: {"id":"chatcmpl-s","object":"chat.completion.chunk","choices":'
    '[{"index":0,"delta":{"content":"llo"},"finish_reason":"stop"}]}\n\n'
    'data: {"id":"chatcmpl-s","object":"chat.completion.chunk","choices":[],'
    '"usage":{"prompt_tokens":7,"completion_tokens":3,"total_tokens":10}}\n\n'
    "data: [DONE]\n\n"
)


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


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
    yield state
    app.dependency_overrides.pop(get_upstream_client, None)


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
def cache(tmp_path) -> Any:
    """Injected tmp-path cache — enables the replay path for the request."""
    store = ResponseCache(tmp_path / "cache.sqlite3", max_entries=50)
    app.dependency_overrides[get_response_cache] = lambda: store
    yield store
    app.dependency_overrides.pop(get_response_cache, None)
    store.close()


def _seed_usage(run_id: uuid.UUID, total_tokens: int) -> None:
    session = get_session_factory()()
    try:
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


@pytest.fixture()
def persisted_events(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Ingest stand-in that writes emitted events to the events table —
    budget accounting (``tokens_used``) then sees them, like live ingest.
    ``sandbox_id`` is dropped (nullable; needs a sandboxes row otherwise).
    """
    rows: list[dict[str, Any]] = []

    async def fake_post(events: list[dict[str, Any]]) -> tuple[int, Any]:
        session = get_session_factory()()
        try:
            for ev in events:
                rows.append(ev)
                session.add(
                    Event(
                        run_id=uuid.UUID(ev["run_id"]),
                        sandbox_id=None,
                        source=ev["source"],
                        type=ev["type"],
                        payload=ev["payload"],
                    )
                )
            session.commit()
            return 200, {"inserted": len(events)}
        finally:
            session.close()

    monkeypatch.setattr(metering, "post_ingest_events", fake_post)
    return rows


def _post_chat(
    gw_client: TestClient, cred: str, **overrides: Any
) -> httpx.Response:
    return gw_client.post(
        "/v1/chat/completions",
        json={**_CHAT_BODY, **overrides},
        headers=_auth(cred),
    )


# ---------------------------------------------------------------------------
# ResponseCache unit behavior
# ---------------------------------------------------------------------------


class TestResponseCache:
    def test_get_miss_returns_none(self, tmp_path) -> None:
        c = ResponseCache(tmp_path / "c.sqlite3")
        assert c.get("nope") is None

    def test_store_get_roundtrip(self, tmp_path) -> None:
        c = ResponseCache(tmp_path / "c.sqlite3")
        key = c.key_for(path="/v1/x", provider="ollama", body={"model": "m"})
        c.store(key, status=200, content_type="application/json", body=b"{}")
        hit = c.get(key)
        assert hit is not None
        assert hit.status == 200
        assert hit.content_type == "application/json"
        assert hit.body == b"{}"
        assert len(c) == 1

    def test_key_is_stable(self, tmp_path) -> None:
        c = ResponseCache(tmp_path / "c.sqlite3")
        body = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}
        # dict order doesn't matter — canonical JSON is sorted.
        body_reordered = {"messages": body["messages"], "model": "m"}
        k1 = c.key_for(path="/v1/x", provider="ollama", body=body)
        k2 = c.key_for(path="/v1/x", provider="ollama", body=body_reordered)
        assert k1 == k2

    def test_key_varies_by_provider_model_body_path(self, tmp_path) -> None:
        c = ResponseCache(tmp_path / "c.sqlite3")
        body = {"model": "m", "messages": []}
        base = c.key_for(path="/v1/x", provider="ollama", body=body)
        variants = [
            c.key_for(path="/v1/y", provider="ollama", body=body),
            c.key_for(path="/v1/x", provider="openai", body=body),
            c.key_for(path="/v1/x", provider="ollama", body={**body, "model": "m2"}),
            c.key_for(path="/v1/x", provider="ollama", body={"model": "m"}),
            c.key_for(path="/v1/x", provider="ollama", body={**body, "stream": True}),
        ]
        assert len({base, *variants}) == 1 + len(variants)

    def test_namespace_salts_the_key(self, tmp_path) -> None:
        body = {"model": "m"}
        v1 = ResponseCache(tmp_path / "c.sqlite3", namespace="v1")
        v2 = ResponseCache(tmp_path / "c.sqlite3", namespace="v2")
        k1 = v1.key_for(path="/v1/x", provider="ollama", body=body)
        k2 = v2.key_for(path="/v1/x", provider="ollama", body=body)
        assert k1 != k2
        v1.store(k1, status=200, content_type="application/json", body=b"1")
        # Same logical request under a bumped namespace misses entirely —
        # this is the cheap invalidation path.
        assert v2.get(k2) is None
        assert v1.get(k1) is not None

    def test_persists_across_instances(self, tmp_path) -> None:
        path = tmp_path / "c.sqlite3"
        c1 = ResponseCache(path)
        key = c1.key_for(path="/v1/x", provider="ollama", body={"model": "m"})
        c1.store(key, status=200, content_type="application/json", body=b"{}")
        c1.close()
        # A "restarted" gateway (fresh instance, same file) still hits —
        # persistence is what makes prewarmed CI replays work.
        c2 = ResponseCache(path)
        assert c2.get(key) is not None
        c2.close()

    def test_lru_eviction_respects_bound(self, tmp_path) -> None:
        c = ResponseCache(tmp_path / "c.sqlite3", max_entries=2)
        keys = [
            c.key_for(path="/v1/x", provider="ollama", body={"i": i})
            for i in range(3)
        ]
        for i, k in enumerate(keys):
            c.store(k, status=200, content_type="application/json", body=str(i).encode())
        assert len(c) == 2
        # Oldest entry evicted; the two most recent survive.
        assert c.get(keys[0]) is None
        assert c.get(keys[1]) is not None
        assert c.get(keys[2]) is not None

    def test_lru_hit_refreshes_recency(self, tmp_path) -> None:
        c = ResponseCache(tmp_path / "c.sqlite3", max_entries=2)
        k_old = c.key_for(path="/v1/x", provider="ollama", body={"i": 0})
        k_new = c.key_for(path="/v1/x", provider="ollama", body={"i": 1})
        c.store(k_old, status=200, content_type="application/json", body=b"0")
        c.store(k_new, status=200, content_type="application/json", body=b"1")
        c.get(k_old)  # touch — k_old is now more recent than k_new
        k_third = c.key_for(path="/v1/x", provider="ollama", body={"i": 2})
        c.store(k_third, status=200, content_type="application/json", body=b"2")
        assert c.get(k_new) is None  # evicted, not the just-hit k_old
        assert c.get(k_old) is not None

    def test_clear(self, tmp_path) -> None:
        c = ResponseCache(tmp_path / "c.sqlite3")
        k = c.key_for(path="/v1/x", provider="ollama", body={})
        c.store(k, status=200, content_type="application/json", body=b"{}")
        c.clear()
        assert len(c) == 0
        assert c.get(k) is None

    def test_memory_backend(self) -> None:
        c = ResponseCache(":memory:")
        k = c.key_for(path="/v1/x", provider="ollama", body={})
        c.store(k, status=200, content_type="application/json", body=b"{}")
        assert c.get(k) is not None
        c.close()


# ---------------------------------------------------------------------------
# settings / dependency wiring
# ---------------------------------------------------------------------------


class TestCacheSettings:
    def test_settings_defaults(self) -> None:
        s = GatewaySettings(_env_file=None)
        assert s.GATEWAY_CACHE_ENABLED is False
        assert s.GATEWAY_CACHE_MAX_ENTRIES > 0
        assert s.GATEWAY_CACHE_NAMESPACE

    def test_dep_none_when_disabled(self) -> None:
        deps.reset_response_cache()
        assert deps.get_response_cache() is None

    def test_dep_builds_from_settings(self, tmp_path, monkeypatch) -> None:
        deps.reset_response_cache()
        settings = get_settings()
        monkeypatch.setattr(settings, "GATEWAY_CACHE_ENABLED", True)
        monkeypatch.setattr(settings, "GATEWAY_CACHE_PATH", str(tmp_path / "c.sqlite3"))
        monkeypatch.setattr(settings, "GATEWAY_CACHE_MAX_ENTRIES", 7)
        monkeypatch.setattr(settings, "GATEWAY_CACHE_NAMESPACE", "ns")
        try:
            c = deps.get_response_cache()
            assert isinstance(c, ResponseCache)
            assert c.namespace == "ns"
            assert c._max_entries == 7
            assert deps.get_response_cache() is c  # process-wide singleton
        finally:
            deps.reset_response_cache()


# ---------------------------------------------------------------------------
# proxy integration — hit/miss, metering, budgets
# ---------------------------------------------------------------------------


def test_cache_off_by_default_requests_passthrough(
    gw_client: TestClient, upstream: Any, emitted: list
) -> None:
    """No cache dependency override -> every call reaches the upstream."""
    cred = _cred()
    assert _post_chat(gw_client, cred).status_code == 200
    assert _post_chat(gw_client, cred).status_code == 200
    assert len(upstream["seen"]) == 2
    assert all("cached" not in c["payload"] for c in emitted)


def test_miss_then_hit_replays_without_upstream(
    gw_client: TestClient, upstream: Any, emitted: list, cache: ResponseCache
) -> None:
    cred = _cred()
    first = _post_chat(gw_client, cred)
    assert first.status_code == 200
    assert "x-heg-cache" not in first.headers
    assert len(cache) == 1

    second = _post_chat(gw_client, cred)
    assert second.status_code == 200
    # Replayed verbatim + flagged, upstream untouched.
    assert second.headers["x-heg-cache"] == "hit"
    assert second.json() == first.json()
    assert len(upstream["seen"]) == 1
    assert len(cache) == 1

    types = [c["type"] for c in emitted]
    assert types == ["llm_request", "llm_response", "llm_request", "llm_response"]
    # Live pair unmarked; replay pair marked on BOTH events.
    assert "cached" not in emitted[0]["payload"]
    assert "cached" not in emitted[1]["payload"]
    assert emitted[2]["payload"]["cached"] is True
    assert emitted[3]["payload"]["cached"] is True
    # Replay meters the stored response's usage (deterministic accrual).
    assert emitted[3]["payload"]["usage"] == {
        "prompt_tokens": 3,
        "completion_tokens": 2,
        "total_tokens": 5,
    }
    assert emitted[3]["payload"]["http_status"] == 200


def test_different_bodies_miss(
    gw_client: TestClient, upstream: Any, emitted: list, cache: ResponseCache
) -> None:
    cred = _cred()
    _post_chat(gw_client, cred)
    _post_chat(gw_client, cred, messages=[{"role": "user", "content": "other"}])
    _post_chat(gw_client, cred, model="other-model")
    _post_chat(gw_client, cred, max_tokens=8)
    assert len(upstream["seen"]) == 4
    assert len(cache) == 4


def test_provider_is_part_of_the_key(
    gw_client: TestClient,
    upstream: Any,
    emitted: list,
    cache: ResponseCache,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same request routed to a different provider must not collide."""
    cred = _cred()
    _post_chat(gw_client, cred)
    assert len(upstream["seen"]) == 1

    monkeypatch.setattr(
        main,
        "get_settings",
        lambda: GatewaySettings(
            _env_file=None,
            UPSTREAM_PROVIDER="openrouter",
            OPENROUTER_API_KEY="k",
        ),
    )
    resp = _post_chat(gw_client, cred)
    assert resp.status_code == 200
    # Re-resolved provider changed the key -> miss -> second upstream call.
    assert len(upstream["seen"]) == 2
    resp_events = [c for c in emitted if c["type"] == "llm_response"]
    assert resp_events[-1]["payload"]["provider"] == "openrouter"
    assert "cached" not in resp_events[-1]["payload"]


def test_streaming_hit_replays_sse_verbatim(
    gw_client: TestClient, upstream: Any, emitted: list, cache: ResponseCache
) -> None:
    upstream["handler"] = lambda _req: httpx.Response(
        200, text=_CHAT_SSE, headers={"content-type": "text/event-stream"}
    )
    cred = _cred()
    first = _post_chat(gw_client, cred, stream=True)
    assert first.status_code == 200
    assert first.text == _CHAT_SSE
    assert len(cache) == 1

    second = _post_chat(gw_client, cred, stream=True)
    assert second.status_code == 200
    assert second.headers["content-type"].startswith("text/event-stream")
    assert second.headers["x-heg-cache"] == "hit"
    assert second.text == _CHAT_SSE
    assert len(upstream["seen"]) == 1

    # Usage re-extracted from the stored SSE body, flagged cached.
    resp_events = [c for c in emitted if c["type"] == "llm_response"]
    assert len(resp_events) == 2
    assert resp_events[1]["payload"]["cached"] is True
    assert resp_events[1]["payload"]["usage"] == {
        "prompt_tokens": 7,
        "completion_tokens": 3,
        "total_tokens": 10,
    }


def test_stream_and_buffered_variants_have_distinct_keys(
    gw_client: TestClient, upstream: Any, emitted: list, cache: ResponseCache
) -> None:
    """``stream: true`` vs ``false`` key differently — an SSE body must never
    replay to a caller expecting a single JSON object."""
    upstream["handler"] = lambda req: (
        httpx.Response(200, text=_CHAT_SSE, headers={"content-type": "text/event-stream"})
        if json.loads(req.content).get("stream")
        else httpx.Response(200, json=_UPSTREAM_OK)
    )
    cred = _cred()
    _post_chat(gw_client, cred, stream=True)
    _post_chat(gw_client, cred, stream=False)
    assert len(upstream["seen"]) == 2
    assert len(cache) == 2
    # Repeats of each hit their own entry.
    assert _post_chat(gw_client, cred, stream=True).text == _CHAT_SSE
    assert _post_chat(gw_client, cred, stream=False).json() == _UPSTREAM_OK
    assert len(upstream["seen"]) == 2


def test_non_2xx_responses_are_not_cached(
    gw_client: TestClient, upstream: Any, emitted: list, cache: ResponseCache
) -> None:
    cred = _cred()
    upstream["handler"] = lambda _req: httpx.Response(
        500, json={"error": {"message": "boom"}}
    )
    assert _post_chat(gw_client, cred).status_code == 500
    assert len(cache) == 0  # errors pass through, never stored

    upstream["handler"] = lambda _req: httpx.Response(200, json=_UPSTREAM_OK)
    assert _post_chat(gw_client, cred).status_code == 200
    assert len(upstream["seen"]) == 2  # both reached upstream


def test_budget_gate_runs_before_cache_lookup(
    gw_client: TestClient, upstream: Any, emitted: list, cache: ResponseCache
) -> None:
    """A replay can't spend past the budget — the gate still wins."""
    run_id = uuid.uuid4()
    cred = _cred(run_id=run_id, max_tokens=100)
    assert _post_chat(gw_client, cred, max_tokens=10).status_code == 200
    assert len(cache) == 1

    _seed_usage(run_id, 200)  # run now over budget
    resp = _post_chat(gw_client, cred, max_tokens=10)
    assert resp.status_code == 429
    assert resp.json()["error"]["type"] == "budget_exceeded"
    assert len(upstream["seen"]) == 1  # the cached entry was never served
    errors = [c for c in emitted if c["type"] == "error"]
    assert errors[-1]["payload"]["reason"] == "budget_exceeded"


def test_cached_usage_still_accrues_to_run_budget(
    gw_client: TestClient,
    upstream: Any,
    persisted_events: list,
    cache: ResponseCache,
) -> None:
    """Cached llm_response events persist real usage -> they count toward
    the run budget, so a replayed run 429s at exactly the same point the
    live run would (4 calls x 5 tokens against a 20 budget)."""
    run_id = uuid.uuid4()
    _seed_usage(run_id, 0)  # parent Run row for the events FK
    cred = _cred(run_id=run_id, max_tokens=20)
    # live(5) -> hit(10) -> hit(15) -> hit(20) -> gate: 20 + 4 > 20.
    for expected in (200, 200, 200, 200, 429):
        assert _post_chat(gw_client, cred, max_tokens=4).status_code == expected
    assert len(upstream["seen"]) == 1  # only the first call ever hit upstream
    cached = [e for e in persisted_events if e["payload"].get("cached")]
    assert len(cached) >= 3  # llm_request + llm_response per hit


def test_messages_endpoint_replays(
    gw_client: TestClient, upstream: Any, emitted: list, cache: ResponseCache
) -> None:
    anthropic_ok = {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "content": [{"type": "text", "text": "hello"}],
        "usage": {"input_tokens": 12, "output_tokens": 4},
    }
    upstream["handler"] = lambda _req: httpx.Response(200, json=anthropic_ok)
    cred = _cred()
    body = {"model": "m", "max_tokens": 64, "messages": [{"role": "user", "content": "hi"}]}
    assert gw_client.post("/v1/messages", json=body, headers=_auth(cred)).status_code == 200
    resp = gw_client.post("/v1/messages", json=body, headers=_auth(cred))
    assert resp.status_code == 200
    assert resp.headers["x-heg-cache"] == "hit"
    assert resp.json() == anthropic_ok
    assert len(upstream["seen"]) == 1
    resp_events = [c for c in emitted if c["type"] == "llm_response"]
    assert resp_events[-1]["payload"]["cached"] is True
    assert resp_events[-1]["payload"]["usage"]["total_tokens"] == 16


def test_responses_endpoint_replays(
    gw_client: TestClient, upstream: Any, emitted: list, cache: ResponseCache
) -> None:
    upstream["handler"] = lambda _req: httpx.Response(
        200, json={"id": "r", "usage": {"input_tokens": 2, "output_tokens": 1}}
    )
    cred = _cred()
    body = {"model": "m", "input": "hi", "max_output_tokens": 8}
    gw_client.post("/v1/responses", json=body, headers=_auth(cred))
    resp = gw_client.post("/v1/responses", json=body, headers=_auth(cred))
    assert resp.headers["x-heg-cache"] == "hit"
    assert len(upstream["seen"]) == 1


def test_count_tokens_preflight_bypasses_cache(
    gw_client: TestClient, upstream: Any, emitted: list, cache: ResponseCache
) -> None:
    """Unmetered calls (meter=False) never consult or populate the cache."""
    upstream["handler"] = lambda _req: httpx.Response(200, json={"input_tokens": 42})
    cred = _cred()
    body = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}
    for _ in range(2):
        resp = gw_client.post(
            "/v1/messages/count_tokens", json=body, headers=_auth(cred)
        )
        assert resp.status_code == 200
    assert len(upstream["seen"]) == 2
    assert len(cache) == 0


def test_cache_lookup_failure_degrades_to_miss(
    gw_client: TestClient,
    upstream: Any,
    emitted: list,
    cache: ResponseCache,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A broken cache must not 5xx the proxy — treat as miss."""

    def boom(_key: str) -> None:
        raise RuntimeError("sqlite exploded")

    monkeypatch.setattr(cache, "get", boom)
    resp = _post_chat(gw_client, _cred())
    assert resp.status_code == 200
    assert len(upstream["seen"]) == 1


def test_cache_store_failure_does_not_break_response(
    gw_client: TestClient,
    upstream: Any,
    emitted: list,
    cache: ResponseCache,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("disk full")

    monkeypatch.setattr(cache, "store", boom)
    resp = _post_chat(gw_client, _cred())
    assert resp.status_code == 200
    assert resp.json()["usage"]["total_tokens"] == 5


def test_namespace_bump_invalidates(
    gw_client: TestClient, upstream: Any, emitted: list, cache: ResponseCache
) -> None:
    cred = _cred()
    _post_chat(gw_client, cred)
    assert len(upstream["seen"]) == 1
    cache.namespace = "v2"  # GATEWAY_CACHE_NAMESPACE bump in production
    assert _post_chat(gw_client, cred).status_code == 200
    assert len(upstream["seen"]) == 2


def test_hit_latency_is_cache_scale_not_upstream(
    gw_client: TestClient, upstream: Any, emitted: list, cache: ResponseCache
) -> None:
    cred = _cred()
    _post_chat(gw_client, cred)
    _post_chat(gw_client, cred)
    latencies = [
        c["payload"]["latency_ms"] for c in emitted if c["type"] == "llm_response"
    ]
    # A hit's latency_ms measures the sqlite lookup, not an LLM call.
    assert 0 <= latencies[1] <= 100
