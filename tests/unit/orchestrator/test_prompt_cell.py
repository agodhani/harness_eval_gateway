"""PR25 — prompt-only pseudo-adapter cells (orchestrator/prompt_cell.py).

Hermetic: file-backed sqlite + FakeRuntime (proves zero docker) + a fake
gateway via httpx.MockTransport (proves request shape, cred scoping, and
the single retry). No real gateway, no containers.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
from sqlalchemy import select

from db.models import Artifact, Event, Sandbox
from gateway.creds import verify_cred
from orchestrator import prompt_cell
from orchestrator.prompt_cell import run_prompt_cell

from .conftest import FakeRuntime, make_run, run_config


def _chat_response(
    text: str = "model reply text", *, finish: str = "stop"
) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": text},
                    "finish_reason": finish,
                }
            ],
            "usage": {
                "prompt_tokens": 11,
                "completion_tokens": 7,
                "total_tokens": 18,
            },
        },
    )


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.MockTransport(handler), timeout=5.0
    )


def _prompt_config(**overrides) -> dict:
    hc = {"adapter": "prompt", "skill_packs": []}
    hc.update(overrides)
    return hc


def _sandbox(session_factory, sid) -> Sandbox:
    with session_factory() as s:
        return s.get(Sandbox, sid)


def _events(session_factory, sandbox_id) -> list[Event]:
    with session_factory() as s:
        return list(
            s.execute(
                select(Event).where(Event.sandbox_id == sandbox_id)
            ).scalars()
        )


def _artifacts(session_factory, sandbox_id) -> list[Artifact]:
    with session_factory() as s:
        return list(
            s.execute(
                select(Artifact).where(Artifact.sandbox_id == sandbox_id)
            ).scalars()
        )


# ---------------------------------------------------------------------------
# happy path
# ---------------------------------------------------------------------------


async def test_prompt_cell_happy_path(
    session_factory, settings, artifact_root
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return _chat_response("the answer is 42")

    run = make_run(
        session_factory,
        task_prompt="What is the answer?",
        config=run_config(),
    )
    fake = FakeRuntime(artifact_root=artifact_root)

    sid = await run_prompt_cell(
        session_factory=session_factory,
        runtime=fake,
        run_id=run.id,
        harness_config=_prompt_config(),
        settings=settings,
        client=_client(handler),
    )

    assert sid is not None
    sb = _sandbox(session_factory, sid)
    assert sb.status == "done"
    assert sb.terminal_reason == "harness_complete"
    assert sb.docker_container_id is None  # zero-container cell
    assert sb.started_at is not None and sb.finished_at is not None
    assert sb.artifact_path is not None

    # no docker: FakeRuntime never spawned / snapshotted
    assert fake.spawned == []
    assert fake.snapshots == []

    # exactly ONE gateway call
    assert len(requests) == 1
    req = requests[0]
    assert req.url.path == "/v1/chat/completions"
    assert req.method == "POST"
    body = json.loads(req.content)
    assert body["model"] == "test-model"
    assert body["stream"] is False
    assert body["max_tokens"] > 0
    # task prompt IS the user message; no system message without extra
    assert body["messages"] == [
        {"role": "user", "content": "What is the answer?"}
    ]

    # sandbox-scoped cred (run + THIS sandbox; never run-level/privileged)
    cred = req.headers["authorization"].removeprefix("Bearer ")
    claims = verify_cred(cred, secret=settings.GATEWAY_CRED_SECRET)
    assert claims.run_id == str(run.id)
    assert claims.sandbox_id == str(sid)
    assert claims.max_tokens == 100_000  # run token_budget

    # artifacts on disk
    art_dir = Path(sb.artifact_path)
    assert (art_dir / "output.txt").read_text() == "the answer is 42"
    transcript = (art_dir / "transcript.log").read_text()
    assert "What is the answer?" in transcript
    assert "the answer is 42" in transcript
    meta = json.loads((art_dir / "meta.json").read_text())
    assert meta["adapter"] == "prompt"
    assert meta["no_sandbox"] is True
    assert meta["model"] == "test-model"
    assert meta["terminal_reason"] == "harness_complete"
    assert meta["http_status"] == 200
    assert meta["usage"]["total_tokens"] == 18

    # Artifact rows: output/transcript/meta — no workspace row
    rows = _artifacts(session_factory, sid)
    kinds = {a.kind for a in rows}
    assert kinds == {"output", "transcript", "meta"}
    out_row = next(a for a in rows if a.kind == "output")
    assert Path(out_row.path).name == "output.txt"
    assert out_row.size_bytes == len("the answer is 42")

    # orchestrator-side events: run_started + terminal_state + finalize
    evs = _events(session_factory, sid)
    by_type: dict[str, list[Event]] = {}
    for e in evs:
        by_type.setdefault(e.type, []).append(e)
    started = by_type.get("run_started") or []
    assert len(started) == 1
    assert started[0].source == "orchestrator"
    assert started[0].payload["adapter"] == "prompt"
    assert started[0].payload["model"] == "test-model"
    assert started[0].payload["no_sandbox"] is True
    term = by_type.get("terminal_state") or []
    assert len(term) == 1
    assert term[0].payload["terminal_state"] == "harness_complete"
    assert term[0].payload["adapter"] == "prompt"
    finalized = [
        e
        for e in by_type.get("checkpoint", [])
        if e.payload.get("stage") == "sandbox_finalized"
    ]
    assert len(finalized) == 1
    assert finalized[0].payload["terminal_reason"] == "harness_complete"
    assert "empty_workspace" not in finalized[0].payload
    # the cell's own telemetry must NOT fake gateway llm_* events
    assert "llm_request" not in by_type
    assert "llm_response" not in by_type


async def test_prompt_cell_system_prompt_extra_becomes_system_message(
    session_factory, settings, artifact_root
) -> None:
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return _chat_response()

    run = make_run(session_factory, config=run_config())
    await run_prompt_cell(
        session_factory=session_factory,
        runtime=FakeRuntime(artifact_root=artifact_root),
        run_id=run.id,
        harness_config=_prompt_config(system_prompt_extra="answer tersely"),
        settings=settings,
        client=_client(handler),
    )
    assert bodies[0]["messages"] == [
        {"role": "system", "content": "answer tersely"},
        {"role": "user", "content": "create hello.txt"},
    ]


async def test_prompt_cell_per_cell_model(
    session_factory, settings, artifact_root
) -> None:
    """PR14 matrix expansion injects ``model`` into the cell's config — the
    request must carry the cell's model, not the run default."""
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return _chat_response()

    run = make_run(session_factory, config=run_config())
    sid = await run_prompt_cell(
        session_factory=session_factory,
        runtime=FakeRuntime(artifact_root=artifact_root),
        run_id=run.id,
        harness_config=_prompt_config(model="other-model"),
        settings=settings,
        client=_client(handler),
    )
    assert bodies[0]["model"] == "other-model"
    meta = json.loads(
        (Path(_sandbox(session_factory, sid).artifact_path) / "meta.json").read_text()
    )
    assert meta["model"] == "other-model"


# ---------------------------------------------------------------------------
# retry semantics — one retry on upstream 5xx / timeout
# ---------------------------------------------------------------------------


async def test_prompt_cell_retries_upstream_5xx_once(
    session_factory, settings, artifact_root
) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(503, json={"error": {"message": "upstream down"}})
        return _chat_response("recovered")

    run = make_run(session_factory, config=run_config())
    sid = await run_prompt_cell(
        session_factory=session_factory,
        runtime=FakeRuntime(artifact_root=artifact_root),
        run_id=run.id,
        harness_config=_prompt_config(),
        settings=settings,
        client=_client(handler),
    )

    assert calls == 2
    sb = _sandbox(session_factory, sid)
    assert sb.status == "done"
    assert (Path(sb.artifact_path) / "output.txt").read_text() == "recovered"
    evs = _events(session_factory, sid)
    retries = [
        e
        for e in evs
        if e.type == "checkpoint" and e.payload.get("stage") == "prompt_cell_retry"
    ]
    assert len(retries) == 1
    assert retries[0].payload["attempt"] == 2
    assert "503" in retries[0].payload["reason"]


async def test_prompt_cell_retries_timeout_once(
    session_factory, settings, artifact_root
) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ReadTimeout("slow upstream", request=request)
        return _chat_response("after timeout")

    run = make_run(session_factory, config=run_config())
    sid = await run_prompt_cell(
        session_factory=session_factory,
        runtime=FakeRuntime(artifact_root=artifact_root),
        run_id=run.id,
        harness_config=_prompt_config(),
        settings=settings,
        client=_client(handler),
    )
    assert calls == 2
    assert _sandbox(session_factory, sid).terminal_reason == "harness_complete"


async def test_prompt_cell_exhausted_5xx_is_error(
    session_factory, settings, artifact_root
) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(502, json={"error": {"message": "bad gateway"}})

    run = make_run(session_factory, config=run_config())
    sid = await run_prompt_cell(
        session_factory=session_factory,
        runtime=FakeRuntime(artifact_root=artifact_root),
        run_id=run.id,
        harness_config=_prompt_config(),
        settings=settings,
        client=_client(handler),
    )

    assert calls == 2  # first + the single retry, then terminal
    sb = _sandbox(session_factory, sid)
    assert sb.status == "failed"
    assert sb.terminal_reason == "error"
    # artifacts still land — meta records the failure like a crashed
    # container's snapshot records its exit
    meta = json.loads((Path(sb.artifact_path) / "meta.json").read_text())
    assert meta["terminal_reason"] == "error"
    assert meta["http_status"] == 502
    assert "502" in meta["error"]
    term = [
        e
        for e in _events(session_factory, sid)
        if e.type == "terminal_state"
    ]
    assert term[0].payload["terminal_state"] == "error"
    assert "502" in term[0].payload["error"]


async def test_prompt_cell_transport_exhausted_is_timeout(
    session_factory, settings, artifact_root
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("still slow", request=request)

    run = make_run(session_factory, config=run_config())
    sid = await run_prompt_cell(
        session_factory=session_factory,
        runtime=FakeRuntime(artifact_root=artifact_root),
        run_id=run.id,
        harness_config=_prompt_config(),
        settings=settings,
        client=_client(handler),
    )
    sb = _sandbox(session_factory, sid)
    assert sb.status == "failed"
    assert sb.terminal_reason == "timeout"  # the cell's own wall-clock cap


async def test_prompt_cell_4xx_is_not_retried(
    session_factory, settings, artifact_root
) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(400, json={"error": {"message": "bad request"}})

    run = make_run(session_factory, config=run_config())
    sid = await run_prompt_cell(
        session_factory=session_factory,
        runtime=FakeRuntime(artifact_root=artifact_root),
        run_id=run.id,
        harness_config=_prompt_config(),
        settings=settings,
        client=_client(handler),
    )
    assert calls == 1
    assert _sandbox(session_factory, sid).terminal_reason == "error"


# ---------------------------------------------------------------------------
# budget enforcement parity — gateway 429 maps to budget_exceeded
# ---------------------------------------------------------------------------


async def test_prompt_cell_budget_exceeded(
    session_factory, settings, artifact_root
) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            429,
            json={
                "error": {
                    "type": "budget_exceeded",
                    "message": "token budget exceeded for sandbox",
                }
            },
        )

    run = make_run(session_factory, config=run_config())
    sid = await run_prompt_cell(
        session_factory=session_factory,
        runtime=FakeRuntime(artifact_root=artifact_root),
        run_id=run.id,
        harness_config=_prompt_config(),
        settings=settings,
        client=_client(handler),
    )
    assert calls == 1  # budget gate is terminal — never retried
    sb = _sandbox(session_factory, sid)
    assert sb.status == "failed"
    assert sb.terminal_reason == "budget_exceeded"


async def test_prompt_cell_429_provider_is_error_not_budget(
    session_factory, settings, artifact_root
) -> None:
    """A bare upstream 429 (provider rate limit) is NOT budget exhaustion —
    mirrors the monitor's stage-based split."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429, json={"error": {"type": "rate_limit", "message": "slow down"}}
        )

    run = make_run(session_factory, config=run_config())
    sid = await run_prompt_cell(
        session_factory=session_factory,
        runtime=FakeRuntime(artifact_root=artifact_root),
        run_id=run.id,
        harness_config=_prompt_config(),
        settings=settings,
        client=_client(handler),
    )
    assert _sandbox(session_factory, sid).terminal_reason == "error"


# ---------------------------------------------------------------------------
# edge cases
# ---------------------------------------------------------------------------


async def test_prompt_cell_empty_output_flags_empty_workspace(
    session_factory, settings, artifact_root
) -> None:
    """A done-ish exit with an empty reply carries the same
    ``empty_workspace`` warning markers the monitor stamps on file-less
    workspaces — the results badge works identically."""

    def handler(request: httpx.Request) -> httpx.Response:
        return _chat_response("   ")

    run = make_run(session_factory, config=run_config())
    sid = await run_prompt_cell(
        session_factory=session_factory,
        runtime=FakeRuntime(artifact_root=artifact_root),
        run_id=run.id,
        harness_config=_prompt_config(),
        settings=settings,
        client=_client(handler),
    )
    sb = _sandbox(session_factory, sid)
    assert sb.status == "done"  # empty reply is still a completed call
    evs = _events(session_factory, sid)
    finalized = [
        e
        for e in evs
        if e.type == "checkpoint" and e.payload.get("stage") == "sandbox_finalized"
    ]
    assert finalized[0].payload["empty_workspace"] is True
    assert any(
        e.type == "checkpoint"
        and e.payload.get("stage") == "empty_workspace"
        and e.payload.get("severity") == "warning"
        for e in evs
    )


async def test_prompt_cell_internal_failure_marks_error(
    session_factory, settings, artifact_root
) -> None:
    """A failure the gateway call can't describe (artifact write throws,
    client raises non-httpx) still lands a failed/error sandbox row."""
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise RuntimeError("mock transport exploded")

    run = make_run(session_factory, config=run_config())
    sid = await run_prompt_cell(
        session_factory=session_factory,
        runtime=FakeRuntime(artifact_root=artifact_root),
        run_id=run.id,
        harness_config=_prompt_config(),
        settings=settings,
        client=_client(handler),
    )
    assert sid is None
    with session_factory() as s:
        sb = s.execute(
            select(Sandbox).where(Sandbox.run_id == run.id)
        ).scalar_one()
        assert sb.status == "failed"
        assert sb.terminal_reason == "error"
        assert sb.artifact_path is None
        errs = [
            e
            for e in s.execute(
                select(Event).where(Event.run_id == run.id)
            ).scalars()
            if e.type == "error"
        ]
        assert any(e.payload.get("stage") == "prompt_cell" for e in errs)


async def test_prompt_cell_malformed_200_is_error(
    session_factory, settings, artifact_root
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": []})

    run = make_run(session_factory, config=run_config())
    sid = await run_prompt_cell(
        session_factory=session_factory,
        runtime=FakeRuntime(artifact_root=artifact_root),
        run_id=run.id,
        harness_config=_prompt_config(),
        settings=settings,
        client=_client(handler),
    )
    sb = _sandbox(session_factory, sid)
    assert sb.status == "failed"
    assert sb.terminal_reason == "error"


# ---------------------------------------------------------------------------
# registry shape
# ---------------------------------------------------------------------------


def test_is_prompt_config() -> None:
    assert prompt_cell.is_prompt_config({"adapter": "prompt"})
    assert not prompt_cell.is_prompt_config({"adapter": "miniloop"})
    assert not prompt_cell.is_prompt_config(None)
    assert not prompt_cell.is_prompt_config({})
