"""Hermetic tests for miniloop event emission — stdout JSONL + HTTP ingest."""

import io
import json

import httpx

from adapters.miniloop.events import EventEmitter


def _lines(out: io.StringIO) -> list[dict]:
    return [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]


def test_emit_stdout_jsonl_shape() -> None:
    out = io.StringIO()
    emitter = EventEmitter(stdout=out)
    emitter.emit("tool_call", {"name": "shell", "args": {"command": "ls"}})
    emitter.emit("done_signal", {"status": "success", "summary": "done"})

    events = _lines(out)
    assert len(events) == 2
    first = events[0]
    assert first["source"] == "harness"
    assert first["type"] == "tool_call"
    assert first["payload"]["name"] == "shell"
    assert first["seq"] == 1
    assert events[1]["seq"] == 2
    # ts is ISO-8601 parseable
    from datetime import datetime

    datetime.fromisoformat(first["ts"])


def test_emit_never_raises_on_ingest_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    out = io.StringIO()
    emitter = EventEmitter(
        ingest_url="http://ingest.local/events", client=client, stdout=out
    )
    event = emitter.emit("checkpoint", {"iter": 1})
    assert event["type"] == "checkpoint"
    assert emitter.ingest_failures == 1
    # stdout still got the event
    assert len(_lines(out)) == 1
    emitter.close()


def test_emit_posts_to_ingest_with_auth() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"ok": True})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    out = io.StringIO()
    emitter = EventEmitter(
        ingest_url="http://ingest.local/internal/events",
        token="sekret",
        client=client,
        stdout=out,
    )
    emitter.emit("file_op", {"op": "write", "path": "/w/a.txt", "bytes": 3})
    emitter.close()

    assert len(requests) == 1
    req = requests[0]
    assert req.url.path == "/internal/events"
    assert req.headers["authorization"] == "Bearer sekret"
    body = json.loads(req.content)
    assert body["type"] == "file_op"
    assert body["payload"]["bytes"] == 3
    assert emitter.ingest_failures == 0


def test_no_ingest_url_skips_http() -> None:
    out = io.StringIO()
    emitter = EventEmitter(stdout=out)
    emitter.emit("checkpoint", {"iter": 1})
    assert emitter._client is None
    assert len(_lines(out)) == 1
