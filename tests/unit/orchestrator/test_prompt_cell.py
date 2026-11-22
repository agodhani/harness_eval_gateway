"""PR25 — prompt-only pseudo-adapter cells (orchestrator/prompt_cell.py).

Hermetic: file-backed sqlite + FakeRuntime (proves zero docker) + a fake
gateway via httpx.MockTransport (proves request shape, cred scoping, and
the single retry). No real gateway, no containers.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path

import httpx
import pytest
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
    # provider override is a SERVICE-cred privilege — a sandbox cell sends
    # only its bearer token; the provider is baked into the cred
    assert "x-heg-provider" not in req.headers
    assert set(req.headers).isdisjoint(
        h for h in req.headers if h.startswith("x-heg-")
    )

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


def _seed_llm_response(
    session_factory, settings, request: httpx.Request, usage: dict
) -> None:
    """Insert the ``llm_response`` event the real gateway commits before it
    returns this response — its ingest POST is awaited, so the row exists
    by the time the cell reads the body. Same cred → same run+sandbox."""
    cred = request.headers["authorization"].removeprefix("Bearer ")
    claims = verify_cred(cred, secret=settings.GATEWAY_CRED_SECRET)
    with session_factory() as s:
        s.add(
            Event(
                run_id=uuid.UUID(claims.run_id),
                sandbox_id=uuid.UUID(claims.sandbox_id),
                source="gateway",
                type="llm_response",
                payload={"usage": usage},
            )
        )
        s.commit()


async def test_prompt_cell_cost_cap_terminates_budget_exceeded(
    session_factory, settings, artifact_root
) -> None:
    """PR28 parity: ``max_cost_usd`` has no monitor poll for a no-sandbox
    cell, so the post-call check is its enforcement site. gpt-4o at
    $2.5/$10 per Mtok → 1000 in + 1000 out = $0.0125 trips a $0.01 cap."""

    def handler(request: httpx.Request) -> httpx.Response:
        _seed_llm_response(
            session_factory,
            settings,
            request,
            {
                "prompt_tokens": 1000,
                "completion_tokens": 1000,
                "total_tokens": 2000,
            },
        )
        return _chat_response("pricey reply")

    run = make_run(
        session_factory,
        model_name="gpt-4o",
        config=run_config(budgets={"max_cost_usd": 0.01}),
    )
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
    assert sb.terminal_reason == "budget_exceeded"
    term = next(
        e.payload
        for e in _events(session_factory, sid)
        if e.type == "checkpoint" and e.payload.get("stage") == "terminate"
    )
    assert term["reason"] == "budget_exceeded"
    assert term["limit"] == "cost"
    assert term["estimated_cost_usd"] == pytest.approx(0.0125)
    assert term["budget_usd"] == 0.01
    meta = json.loads((Path(sb.artifact_path) / "meta.json").read_text())
    assert meta["terminal_reason"] == "budget_exceeded"
    # the call's output still lands — a terminated container's snapshot
    # keeps its workspace too
    assert (Path(sb.artifact_path) / "output.txt").read_text() == "pricey reply"


async def test_prompt_cell_sandbox_token_cap_terminates_budget_exceeded(
    session_factory, settings, artifact_root
) -> None:
    """PR28 parity: ``sandbox_token_budget`` mints the cred's
    ``sandbox_max_tokens`` claim, which only gates admission — a reply
    can overshoot it, and containers get the monitor's post-hoc
    ``budget_exceeded``/``limit=sandbox_tokens`` verdict for that. A
    prompt cell has no monitor, so the post-call check is its
    enforcement site: 300 recorded tokens vs a 200 cap."""

    def handler(request: httpx.Request) -> httpx.Response:
        _seed_llm_response(
            session_factory,
            settings,
            request,
            {
                "prompt_tokens": 200,
                "completion_tokens": 100,
                "total_tokens": 300,
            },
        )
        return _chat_response("over-cap reply")

    run = make_run(
        session_factory,
        config=run_config(
            budgets={
                "max_iters": 10,
                "token_budget": 100_000,
                "wall_clock_seconds": 3600,
                "sandbox_token_budget": 200,
            }
        ),
    )
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
    assert sb.terminal_reason == "budget_exceeded"
    term = next(
        e.payload
        for e in _events(session_factory, sid)
        if e.type == "checkpoint" and e.payload.get("stage") == "terminate"
    )
    assert term["reason"] == "budget_exceeded"
    assert term["limit"] == "sandbox_tokens"
    assert term["used_tokens"] == 300
    assert term["budget"] == 200
    meta = json.loads((Path(sb.artifact_path) / "meta.json").read_text())
    assert meta["terminal_reason"] == "budget_exceeded"
    # the call's output still lands — a terminated container's snapshot
    # keeps its workspace too
    assert (Path(sb.artifact_path) / "output.txt").read_text() == "over-cap reply"


async def test_prompt_cell_cost_cap_unpriced_model_cannot_trip(
    session_factory, settings, artifact_root
) -> None:
    """Same PR21 honesty boundary as containers: an unlisted model has no
    honest rate — the cap can't trip it, the clean completion stands."""

    def handler(request: httpx.Request) -> httpx.Response:
        _seed_llm_response(
            session_factory,
            settings,
            request,
            {
                "prompt_tokens": 10**9,
                "completion_tokens": 0,
                "total_tokens": 10**9,
            },
        )
        return _chat_response("free reply")

    run = make_run(
        session_factory,
        model_name="test-model",  # not in PRICE_TABLE, no model_prices rows
        config=run_config(budgets={"max_cost_usd": 0.000001}),
    )
    sid = await run_prompt_cell(
        session_factory=session_factory,
        runtime=FakeRuntime(artifact_root=artifact_root),
        run_id=run.id,
        harness_config=_prompt_config(),
        settings=settings,
        client=_client(handler),
    )

    sb = _sandbox(session_factory, sid)
    assert sb.status == "done"
    assert sb.terminal_reason == "harness_complete"
    assert not any(
        e.type == "checkpoint" and e.payload.get("stage") == "terminate"
        for e in _events(session_factory, sid)
    )


async def test_prompt_cell_cost_cap_under_budget_completes(
    session_factory, settings, artifact_root
) -> None:
    """Priced but under the cap — the verdict stays harness_complete."""

    def handler(request: httpx.Request) -> httpx.Response:
        _seed_llm_response(
            session_factory,
            settings,
            request,
            {
                "prompt_tokens": 100,
                "completion_tokens": 100,
                "total_tokens": 200,
            },
        )
        return _chat_response("cheap reply")

    run = make_run(
        session_factory,
        model_name="gpt-4o",
        config=run_config(budgets={"max_cost_usd": 1.00}),
    )
    sid = await run_prompt_cell(
        session_factory=session_factory,
        runtime=FakeRuntime(artifact_root=artifact_root),
        run_id=run.id,
        harness_config=_prompt_config(),
        settings=settings,
        client=_client(handler),
    )
    assert _sandbox(session_factory, sid).terminal_reason == "harness_complete"


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


# ---------------------------------------------------------------------------
# config-shape edge cases + constants contract
# ---------------------------------------------------------------------------


async def test_prompt_cell_ignores_skill_packs(
    session_factory, settings, artifact_root
) -> None:
    """A stale skill_packs selection on a prompt config is inert: the cell
    stages nothing, sends the bare task prompt, and still lands a normal
    artifact (submit strips these client-side; this proves the backend
    ignores whatever survives)."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return _chat_response("pack-free reply")

    run = make_run(session_factory, config=run_config())
    sid = await run_prompt_cell(
        session_factory=session_factory,
        runtime=FakeRuntime(artifact_root=artifact_root),
        run_id=run.id,
        harness_config=_prompt_config(skill_packs=["superpowers", "spec-kit"]),
        settings=settings,
        client=_client(handler),
    )

    sb = _sandbox(session_factory, sid)
    assert sb.status == "done"
    assert len(requests) == 1
    body = json.loads(requests[0].content)
    # no pack names/content ever reach the model call
    assert body["messages"] == [{"role": "user", "content": run.task_prompt}]
    assert "superpowers" not in requests[0].content.decode()
    assert "spec-kit" not in requests[0].content.decode()
    assert (Path(sb.artifact_path) / "output.txt").read_text() == "pack-free reply"


async def test_prompt_cell_connect_error_is_error_not_timeout(
    session_factory, settings, artifact_root
) -> None:
    """Non-timeout transport failures (connection refused/reset) are
    retried once, then land as `error` — NOT `timeout` (which is reserved
    for the cell's wall-clock cap, matching monitor semantics)."""
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("gateway refused", request=request)

    run = make_run(session_factory, config=run_config())
    sid = await run_prompt_cell(
        session_factory=session_factory,
        runtime=FakeRuntime(artifact_root=artifact_root),
        run_id=run.id,
        harness_config=_prompt_config(),
        settings=settings,
        client=_client(handler),
    )

    assert calls == 2  # one retry, then terminal
    sb = _sandbox(session_factory, sid)
    assert sb.status == "failed"
    assert sb.terminal_reason == "error"
    meta = json.loads((Path(sb.artifact_path) / "meta.json").read_text())
    assert "ConnectError" in meta["error"]
    term = [
        e for e in _events(session_factory, sid) if e.type == "terminal_state"
    ]
    assert term[0].payload["terminal_state"] == "error"


# ---------------------------------------------------------------------------
# wall_clock_seconds — cell-level deadline (monitor parity)
# ---------------------------------------------------------------------------


async def test_prompt_cell_wall_clock_deadline_aborts_call(
    session_factory, settings, artifact_root
) -> None:
    """``wall_clock_seconds`` bounds a prompt cell like a container's
    monitor cap: an in-flight call past the deadline is aborted — the
    orphaned upstream call mirrors a container being killed — and the
    sandbox lands ``timeout``/``failed`` with its artifact set."""
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        await asyncio.sleep(5)  # outlives the 1s deadline; gets cancelled
        return _chat_response()

    run = make_run(
        session_factory,
        config=run_config(
            budgets={
                "max_iters": 10,
                "token_budget": 100_000,
                "wall_clock_seconds": 1,
            }
        ),
    )
    sid = await run_prompt_cell(
        session_factory=session_factory,
        runtime=FakeRuntime(artifact_root=artifact_root),
        run_id=run.id,
        harness_config=_prompt_config(),
        settings=settings,
        client=_client(handler),
    )

    assert calls == 1  # aborted mid-flight — no second attempt
    sb = _sandbox(session_factory, sid)
    assert sb.status == "failed"
    assert sb.terminal_reason == "timeout"
    meta = json.loads((Path(sb.artifact_path) / "meta.json").read_text())
    assert meta["terminal_reason"] == "timeout"
    assert "wall_clock" in meta["error"]
    term = [e for e in _events(session_factory, sid) if e.type == "terminal_state"]
    assert term[0].payload["terminal_state"] == "timeout"
    assert term[0].payload["reason"] == "wall_clock"


async def test_prompt_cell_wall_clock_expired_between_retries(
    session_factory, settings, artifact_root
) -> None:
    """A deadline that lands after a retryable failure suppresses the
    remaining retry — the 5xx is superseded by ``timeout``, not ``error``."""
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.9)  # burns most of the 1s deadline
        return httpx.Response(503, json={"error": {"message": "upstream down"}})

    run = make_run(
        session_factory,
        config=run_config(
            budgets={
                "max_iters": 10,
                "token_budget": 100_000,
                "wall_clock_seconds": 1,
            }
        ),
    )
    sid = await run_prompt_cell(
        session_factory=session_factory,
        runtime=FakeRuntime(artifact_root=artifact_root),
        run_id=run.id,
        harness_config=_prompt_config(),
        settings=settings,
        client=_client(handler),
    )

    assert calls == 1  # the retry the 5xx would normally earn never ran
    sb = _sandbox(session_factory, sid)
    assert sb.status == "failed"
    assert sb.terminal_reason == "timeout"


async def test_prompt_cell_wall_clock_already_elapsed_makes_no_call(
    session_factory, settings, artifact_root
) -> None:
    """A deadline already past at cell start issues zero requests — same
    as a container the monitor kills before its first poll."""
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return _chat_response()

    run = make_run(
        session_factory,
        config=run_config(
            budgets={
                "max_iters": 10,
                "token_budget": 100_000,
                "wall_clock_seconds": 0,
            }
        ),
    )
    sid = await run_prompt_cell(
        session_factory=session_factory,
        runtime=FakeRuntime(artifact_root=artifact_root),
        run_id=run.id,
        harness_config=_prompt_config(),
        settings=settings,
        client=_client(handler),
    )

    assert calls == 0
    sb = _sandbox(session_factory, sid)
    assert sb.status == "failed"
    assert sb.terminal_reason == "timeout"


def test_prompt_cell_timeout_outwaits_gateway_worst_case() -> None:
    """PROMPT_CELL_TIMEOUT_SECONDS must exceed the gateway's worst-case
    silent hold on one call — (1 + UPSTREAM_RETRY_ATTEMPTS) attempts each
    burning the whole UPSTREAM_TIMEOUT_SECONDS plus summed exponential
    backoff — same contract as rubric/miniloop/testgen callers. Derived
    from GatewaySettings defaults so a gateway-side bump that breaks the
    guarantee fails this test."""
    from gateway.config import GatewaySettings

    fields = GatewaySettings.model_fields
    attempts = 1 + int(fields["UPSTREAM_RETRY_ATTEMPTS"].default)
    upstream_timeout = float(fields["UPSTREAM_TIMEOUT_SECONDS"].default)
    backoff_base = float(fields["UPSTREAM_RETRY_BACKOFF_SECONDS"].default)
    worst_case = attempts * upstream_timeout + sum(
        backoff_base * (2**i) for i in range(attempts - 1)
    )
    assert prompt_cell.DEFAULT_TIMEOUT_SECONDS > worst_case
