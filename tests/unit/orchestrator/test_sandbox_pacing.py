"""F30 — local-run sandbox pacing.

``SANDBOX_MAX_CONCURRENT_LOCAL`` caps CONCURRENTLY-ACTIVE containers when
every participant model routes to a local provider (ollama): extra matrix
cells park as ``status="queued"`` BEFORE ``docker run`` and promote FIFO as
siblings go terminal — ``provision.queue_sandbox`` parks,
``pacing.claim_queued_sandbox`` claims, ``provision.spawn_queued`` launches,
and ``lifecycle._watch_cells_paced`` drives the promotion chain.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

from sqlalchemy import select

from db.models import Event, Sandbox
from orchestrator import pacing, provision
from orchestrator.lifecycle import run_lifecycle

from .conftest import FakeRuntime, make_run, make_sandbox, run_config

RESULT_DONE = "MINILOOP_RESULT " + json.dumps(
    {"terminal_state": "agent_done", "iters": 2}
) + "\n"


def _hc(name: str | None = None, **extra: Any) -> dict[str, Any]:
    return {"adapter": "miniloop", "skill_packs": [], "name": name, **extra}


def _local(settings, cap: int, *, provider: str = "ollama", routes: str = ""):
    settings.SANDBOX_MAX_CONCURRENT_LOCAL = cap
    settings.UPSTREAM_PROVIDER = provider
    settings.PROVIDER_ROUTES = routes
    return settings


def _sandbox_statuses(session_factory, run_id: uuid.UUID) -> dict[uuid.UUID, str]:
    with session_factory() as s:
        return {
            sb.id: sb.status
            for sb in s.execute(
                select(Sandbox).where(Sandbox.run_id == run_id)
            ).scalars()
        }


def _checkpoints(session_factory, sandbox_id, stage: str) -> list[dict]:
    with session_factory() as s:
        return [
            p
            for p in s.execute(
                select(Event.payload).where(
                    Event.sandbox_id == sandbox_id, Event.type == "checkpoint"
                )
            ).scalars()
            if isinstance(p, dict) and p.get("stage") == stage
        ]


class _HeldRuntime(FakeRuntime):
    """Keeps spawned containers alive until their id is released — the test
    parks cells deterministically mid-flight to observe queued rows."""

    def __init__(self, *args: Any, hold_first: int = 0, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.held: set[str] = set()
        self._hold_first = hold_first

    def spawn(self, spec: Any):
        out = super().spawn(spec)
        # Auto-hold the first N spawned cells — the monitor then sees them
        # "running" forever until the test releases them (no poll-count
        # races).
        if len(self.spawned) <= self._hold_first:
            self.held.add(str(spec.sandbox_id))
        return out

    def status(self, sandbox_id: str) -> dict[str, Any]:
        if sandbox_id in self.held:
            return {"status": "running", "exit_code": None}
        return super().status(sandbox_id)

    def release_all(self) -> None:
        self.held.clear()


async def _wait_for(cond, timeout: float = 10.0) -> None:
    deadline = asyncio.get_event_loop().time() + timeout
    while True:
        if cond():
            return
        if asyncio.get_event_loop().time() > deadline:
            raise AssertionError("condition never became true")
        await asyncio.sleep(0.01)


# ---------------------------------------------------------------------------
# cap policy
# ---------------------------------------------------------------------------


def test_local_cap_disabled_without_setting(settings, session_factory) -> None:
    # The fixture carries no SANDBOX_MAX_CONCURRENT_LOCAL — getattr default
    # 0 = pacing off.
    run = make_run(session_factory, config=run_config())
    assert pacing.local_sandbox_cap(settings, run) == 0


def test_local_cap_applies_when_all_models_local(settings, session_factory) -> None:
    _local(settings, 2)
    run = make_run(session_factory, config=run_config())
    assert pacing.local_sandbox_cap(settings, run) == 2


def test_local_cap_zero_when_any_model_remote(settings, session_factory) -> None:
    """One remote participant = mixed contention — no cap."""
    _local(settings, 2, routes='{"gpt-*": "openai"}')
    cfg = run_config(harness_configs=[_hc(model="gpt-4o")])
    cfg["models"] = ["test-model"]
    run = make_run(session_factory, config=cfg, model_name="test-model")
    assert pacing.local_sandbox_cap(settings, run) == 0


def test_local_cap_zero_when_fully_remote(settings, session_factory) -> None:
    _local(settings, 2, provider="openai")
    run = make_run(session_factory, config=run_config())
    assert pacing.local_sandbox_cap(settings, run) == 0


# ---------------------------------------------------------------------------
# queue + claim primitives
# ---------------------------------------------------------------------------


def test_queue_sandbox_writes_row_and_event(session_factory) -> None:
    run = make_run(session_factory)
    sid = provision.queue_sandbox(session_factory, run.id, _hc("a"), cap=2)
    with session_factory() as s:
        sb = s.get(Sandbox, sid)
        assert sb.status == "queued"
        assert sb.started_at is None  # no container exists yet
        assert sb.name is not None
    events = _checkpoints(session_factory, sid, "sandbox_queued")
    assert len(events) == 1
    assert events[0]["ahead"] == 0
    assert events[0]["cap"] == 2


def test_claim_promotes_oldest_fifo(session_factory) -> None:
    run = make_run(session_factory)
    a = provision.queue_sandbox(session_factory, run.id, _hc("a"), cap=2)
    b = provision.queue_sandbox(session_factory, run.id, _hc("b"), cap=2)

    sid = pacing.claim_queued_sandbox(session_factory, run.id, cap=2)
    assert sid == a
    with session_factory() as s:
        assert s.get(Sandbox, a).status == "provisioning"
        assert s.get(Sandbox, b).status == "queued"

    # The claim occupies a slot — cap=2 has one left, so b still claims.
    sid2 = pacing.claim_queued_sandbox(session_factory, run.id, cap=2)
    assert sid2 == b
    # Queue drained + cap full → None.
    assert pacing.claim_queued_sandbox(session_factory, run.id, cap=2) is None

    dequeued = _checkpoints(session_factory, a, "sandbox_dequeued")
    assert len(dequeued) == 1
    assert "queued_seconds" in dequeued[0]


def test_claim_blocked_while_cap_full(session_factory) -> None:
    run = make_run(session_factory)
    make_sandbox(session_factory, run.id, status="running")
    q = provision.queue_sandbox(session_factory, run.id, _hc("q"), cap=1)
    assert pacing.claim_queued_sandbox(session_factory, run.id, cap=1) is None
    with session_factory() as s:
        assert s.get(Sandbox, q).status == "queued"  # untouched


def test_claim_counts_terminating_as_active(session_factory) -> None:
    """A tearing-down sibling still holds its slot — the container is alive."""
    run = make_run(session_factory)
    make_sandbox(session_factory, run.id, status="terminating")
    provision.queue_sandbox(session_factory, run.id, _hc("q"), cap=1)
    assert pacing.claim_queued_sandbox(session_factory, run.id, cap=1) is None


# ---------------------------------------------------------------------------
# lifecycle end-to-end
# ---------------------------------------------------------------------------


def _paced_cfg(n: int) -> dict[str, Any]:
    # system_prompt_extra lands in each spec's env — a per-cell marker the
    # test can read back off fake.spawned to assert FIFO claim order.
    return run_config(
        harness_configs=[
            _hc(name=f"cfg-{i}", system_prompt_extra=f"extra-{i}")
            for i in range(n)
        ]
    )


async def test_lifecycle_paces_and_promotes_fifo(
    session_factory, settings, artifact_root
) -> None:
    """cap=1 over 3 local cells: first spawns, two park queued, and each
    promotion follows a sibling going terminal — spawn order stays FIFO."""
    _local(settings, 1)
    run = make_run(
        session_factory, status="provisioning", config=_paced_cfg(3)
    )
    fake = _HeldRuntime(
        artifact_root=artifact_root, default_logs=RESULT_DONE, hold_first=1
    )

    task = asyncio.create_task(
        run_lifecycle(
            run.id,
            session_factory=session_factory,
            runtime=fake,
            settings=settings,
            evaluator_fn=None,
        )
    )
    try:
        await _wait_for(
            lambda: sorted(_sandbox_statuses(session_factory, run.id).values())
            == ["queued", "queued", "running"]
        )
        assert len(fake.spawned) == 1
        queued = [
            sid
            for sid, st in _sandbox_statuses(session_factory, run.id).items()
            if st == "queued"
        ]
        assert len(queued) == 2
        for sid in queued:
            assert _checkpoints(session_factory, sid, "sandbox_queued")

        fake.release_all()
        await asyncio.wait_for(task, timeout=15)
    finally:
        if not task.done():
            fake.release_all()
            task.cancel()

    statuses = _sandbox_statuses(session_factory, run.id)
    assert set(statuses.values()) == {"done"}
    assert len(fake.spawned) == 3
    # FIFO: claims follow the config order the cells queued in.
    assert [sp.env.get("SYSTEM_PROMPT_EXTRA") for sp in fake.spawned] == [
        "extra-0",
        "extra-1",
        "extra-2",
    ]
    # Each promoted cell left a dequeued trail.
    assert sum(
        len(_checkpoints(session_factory, sid, "sandbox_dequeued"))
        for sid in statuses
    ) == 2


async def test_lifecycle_uncapped_spawns_all_immediately(
    session_factory, settings, artifact_root
) -> None:
    """No SANDBOX_MAX_CONCURRENT_LOCAL → no queued rows, every cell spawns
    up front (pre-F30 behavior preserved)."""
    settings.SANDBOX_MAX_CONCURRENT_LOCAL = 0
    run = make_run(
        session_factory, status="provisioning", config=_paced_cfg(3)
    )
    fake = FakeRuntime(artifact_root=artifact_root, default_logs=RESULT_DONE)
    await asyncio.wait_for(
        run_lifecycle(
            run.id,
            session_factory=session_factory,
            runtime=fake,
            settings=settings,
            evaluator_fn=None,
        ),
        timeout=15,
    )
    statuses = _sandbox_statuses(session_factory, run.id)
    assert set(statuses.values()) == {"done"}
    assert len(fake.spawned) == 3
    assert all(
        _checkpoints(session_factory, sid, "sandbox_queued") == []
        for sid in statuses
    )


async def test_lifecycle_remote_run_uncapped(
    session_factory, settings, artifact_root
) -> None:
    """Cap set but a remote provider → no sandbox pacing."""
    _local(settings, 1, provider="openai")
    run = make_run(
        session_factory, status="provisioning", config=_paced_cfg(2)
    )
    fake = FakeRuntime(artifact_root=artifact_root, default_logs=RESULT_DONE)
    await asyncio.wait_for(
        run_lifecycle(
            run.id,
            session_factory=session_factory,
            runtime=fake,
            settings=settings,
            evaluator_fn=None,
        ),
        timeout=15,
    )
    assert len(fake.spawned) == 2
    assert set(_sandbox_statuses(session_factory, run.id).values()) == {"done"}


async def test_lifecycle_failed_spawn_does_not_eat_slot(
    session_factory, settings, artifact_root
) -> None:
    """cap=1: when the first cell's spawn FAILS, the next config spawns
    rather than queueing — a dead spawn holds no pacing slot."""
    _local(settings, 1)
    run = make_run(
        session_factory, status="provisioning", config=_paced_cfg(2)
    )
    fake = FakeRuntime(
        artifact_root=artifact_root,
        default_logs=RESULT_DONE,
        fail_first_spawn=True,
    )
    await asyncio.wait_for(
        run_lifecycle(
            run.id,
            session_factory=session_factory,
            runtime=fake,
            settings=settings,
            evaluator_fn=None,
        ),
        timeout=15,
    )
    statuses = _sandbox_statuses(session_factory, run.id)
    assert sorted(statuses.values()) == ["done", "failed"]
    # The surviving cell spawned on the second attempt — nothing parked.
    assert len(fake.spawned) == 1
    assert all(
        _checkpoints(session_factory, sid, "sandbox_queued") == []
        for sid in statuses
    )
