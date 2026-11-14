"""Pacing (PR15): run queue placement/promotion + spawn memory gate.

Queue tests exercise ``pacing.place_run`` / ``pacing.claim_next_queued``
directly and the ``runner`` wiring end-to-end with a stubbed lifecycle.
Memory-gate tests inject scripted ``mem_reader`` sequences — no real
/proc, sysconf, or docker calls.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import select

from db.models import Event, Run, Sandbox
from orchestrator import lifecycle as lifecycle_mod
from orchestrator import pacing, runner
from orchestrator.pacing import (
    MemReading,
    claim_next_queued,
    place_run,
    wait_for_headroom,
)
from orchestrator.provision import spawn_one

from .conftest import make_run, make_sandbox, run_config

MIB = 1024 * 1024


def _stage_events(session_factory, run_id, stage: str) -> list[Event]:
    with session_factory() as s:
        return [
            e
            for e in s.execute(
                select(Event).where(Event.run_id == run_id, Event.type == "checkpoint")
            ).scalars()
            if e.payload.get("stage") == stage
        ]


def _status(session_factory, run_id: uuid.UUID) -> str:
    with session_factory() as s:
        return s.get(Run, run_id).status


def _set_status(session_factory, run_id: uuid.UUID, status: str, **fields) -> None:
    with session_factory() as s:
        run = s.get(Run, run_id)
        run.status = status
        for k, v in fields.items():
            setattr(run, k, v)
        s.commit()


# ---------------------------------------------------------------------------
# place_run — approve-time placement
# ---------------------------------------------------------------------------


def test_place_run_provisions_when_slot_free(session_factory) -> None:
    run = make_run(session_factory, status="rubric_review")
    with session_factory() as s:
        placed = place_run(s, s.get(Run, run.id))
        s.commit()
    assert placed == "provisioning"
    assert _status(session_factory, run.id) == "provisioning"
    assert _stage_events(session_factory, run.id, "run_queued") == []


def test_place_run_queues_behind_active_run(session_factory) -> None:
    for active in ("provisioning", "running", "evaluating"):
        make_run(session_factory, status=active)
        parked = make_run(session_factory, status="rubric_review")
        with session_factory() as s:
            placed = place_run(s, s.get(Run, parked.id))
            s.commit()
        assert placed == "queued"
        events = _stage_events(session_factory, parked.id, "run_queued")
        assert len(events) == 1
        assert events[0].payload["ahead"] >= 1


def test_place_run_queues_behind_queued_runs_fifo(session_factory) -> None:
    """An approval landing behind BOTH an active run and a queued run must
    not jump the queue — the queued count is included in `ahead`."""
    make_run(session_factory, status="running")
    make_run(session_factory, status="queued")
    parked = make_run(session_factory, status="rubric_review")
    with session_factory() as s:
        placed = place_run(s, s.get(Run, parked.id))
        s.commit()
    assert placed == "queued"
    events = _stage_events(session_factory, parked.id, "run_queued")
    assert events[0].payload["ahead"] == 2


def test_place_run_ignores_terminal_and_prereview_runs(session_factory) -> None:
    """done/failed/submitted/rubric_review siblings don't hold the slot."""
    for status in ("done", "failed", "submitted", "rubric_review"):
        make_run(session_factory, status=status)
    run = make_run(session_factory, status="rubric_review")
    with session_factory() as s:
        placed = place_run(s, s.get(Run, run.id))
        s.commit()
    assert placed == "provisioning"


# ---------------------------------------------------------------------------
# claim_next_queued — promotion gate
# ---------------------------------------------------------------------------


def test_claim_promotes_oldest_queued_fifo(session_factory) -> None:
    earlier = make_run(session_factory, status="queued")
    _set_status(
        session_factory, earlier.id, "queued",
        started_at=datetime.now(UTC) - timedelta(minutes=5),
    )
    later = make_run(session_factory, status="queued")
    _set_status(
        session_factory, later.id, "queued",
        started_at=datetime.now(UTC) - timedelta(minutes=1),
    )

    first = claim_next_queued(session_factory)
    assert first == earlier.id
    assert _status(session_factory, earlier.id) == "provisioning"
    # claimed run now holds the slot -> second claim is a no-op
    assert claim_next_queued(session_factory) is None
    assert _status(session_factory, later.id) == "queued"

    ev = _stage_events(session_factory, earlier.id, "run_dequeued")
    assert len(ev) == 1
    assert ev[0].payload["queued_seconds"] >= 299  # ~5min park


def test_claim_noop_when_active_or_empty(session_factory) -> None:
    queued = make_run(session_factory, status="queued")
    make_run(session_factory, status="running")
    assert claim_next_queued(session_factory) is None
    assert _status(session_factory, queued.id) == "queued"


# ---------------------------------------------------------------------------
# runner wiring — promote on lifecycle exit + sweep kick
# ---------------------------------------------------------------------------


def _done_marking_lifecycle(calls: list[uuid.UUID]):
    """Stub run_lifecycle: record the call, mark the run done."""

    async def fake(run_id: uuid.UUID, *, session_factory, **_kwargs):
        calls.append(run_id)
        with session_factory() as s:
            run = s.get(Run, run_id)
            if run is not None:
                run.status = "done"
                s.commit()

    return fake


async def _wait_calls(calls: list, n: int) -> None:
    for _ in range(500):
        if len(calls) >= n:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"expected {n} lifecycle calls, got {calls}")


async def test_lifecycle_exit_promotes_queued_run(
    session_factory, settings, fake_runtime, monkeypatch
) -> None:
    """A finishing run hands the slot to the oldest queued run — chain runs
    entirely through runner.start_lifecycle_task (no manual dequeue)."""
    calls: list[uuid.UUID] = []
    monkeypatch.setattr(
        lifecycle_mod, "run_lifecycle", _done_marking_lifecycle(calls)
    )

    active = make_run(session_factory, status="provisioning")
    queued = make_run(session_factory, status="queued")
    _set_status(session_factory, queued.id, "queued", started_at=datetime.now(UTC))

    task = runner.start_lifecycle_task(
        active.id,
        session_factory=session_factory,
        settings=settings,
        runtime=fake_runtime,
    )
    await task
    await _wait_calls(calls, 2)

    assert calls == [active.id, queued.id]
    assert _status(session_factory, queued.id) == "done"
    assert _stage_events(session_factory, queued.id, "run_dequeued")


async def test_promote_skips_while_shutting_down(
    session_factory, settings, fake_runtime, monkeypatch
) -> None:
    calls: list[uuid.UUID] = []
    monkeypatch.setattr(
        lifecycle_mod, "run_lifecycle", _done_marking_lifecycle(calls)
    )
    monkeypatch.setattr(runner, "_accepting", False)

    queued = make_run(session_factory, status="queued")
    await runner.promote_next_queued(
        session_factory=session_factory, settings=settings, runtime=fake_runtime
    )
    await asyncio.sleep(0.05)
    assert calls == []
    assert _status(session_factory, queued.id) == "queued"


async def test_startup_sweep_promotes_queued_after_failing_stale(
    session_factory, settings, fake_runtime, monkeypatch
) -> None:
    """Restart reconciliation: stale active -> failed frees the slot for a
    run that was parked behind it (or behind pre-restart work)."""
    calls: list[uuid.UUID] = []
    monkeypatch.setattr(
        lifecycle_mod, "run_lifecycle", _done_marking_lifecycle(calls)
    )

    stale = make_run(session_factory, status="running")
    parked = make_run(session_factory, status="queued")
    _set_status(session_factory, parked.id, "queued", started_at=datetime.now(UTC))

    await runner.startup_sweep(
        session_factory=session_factory, settings=settings, runtime=fake_runtime
    )
    await _wait_calls(calls, 1)

    assert calls == [parked.id]
    assert _status(session_factory, stale.id) == "failed"
    assert _status(session_factory, parked.id) == "done"


async def test_promote_retries_transient_claim_failure(
    session_factory, settings, fake_runtime, monkeypatch
) -> None:
    """A claim that raises once must not strand the queue — promotion
    retries and still launches the oldest queued run."""
    calls: list[uuid.UUID] = []
    monkeypatch.setattr(
        lifecycle_mod, "run_lifecycle", _done_marking_lifecycle(calls)
    )
    real_claim = pacing.claim_next_queued
    attempts = {"n": 0}

    def flaky_claim(sf):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("transient db failure")
        return real_claim(sf)

    monkeypatch.setattr(pacing, "claim_next_queued", flaky_claim)
    monkeypatch.setattr(runner, "_CLAIM_RETRY_DELAY_SECONDS", 0.01)

    queued = make_run(session_factory, status="queued")
    _set_status(session_factory, queued.id, "queued", started_at=datetime.now(UTC))

    await runner.promote_next_queued(
        session_factory=session_factory, settings=settings, runtime=fake_runtime
    )
    await _wait_calls(calls, 1)

    # >= 2: the first attempt raised, a retry claimed the run (the promoted
    # run's own lifecycle exit may add another no-op claim attempt).
    assert attempts["n"] >= 2
    assert calls == [queued.id]
    assert _status(session_factory, queued.id) == "done"


async def test_promote_gives_up_after_bounded_claim_retries(
    session_factory, settings, fake_runtime, monkeypatch
) -> None:
    """A persistent claim failure returns after the bounded retries — the
    queued run stays parked rather than crashing the promote path."""
    calls: list[uuid.UUID] = []
    monkeypatch.setattr(
        lifecycle_mod, "run_lifecycle", _done_marking_lifecycle(calls)
    )
    attempts = {"n": 0}

    def always_fail(sf):
        attempts["n"] += 1
        raise RuntimeError("db down")

    monkeypatch.setattr(pacing, "claim_next_queued", always_fail)
    monkeypatch.setattr(runner, "_CLAIM_RETRY_DELAY_SECONDS", 0.01)

    queued = make_run(session_factory, status="queued")
    await runner.promote_next_queued(
        session_factory=session_factory, settings=settings, runtime=fake_runtime
    )
    await asyncio.sleep(0.05)

    assert attempts["n"] == runner._CLAIM_ATTEMPTS
    assert calls == []
    assert _status(session_factory, queued.id) == "queued"


async def test_startup_sweep_rearms_accepting_after_shutdown(
    session_factory, settings, fake_runtime, monkeypatch
) -> None:
    """A same-process shutdown() -> startup_sweep() cycle (e.g. a lifespan
    re-entry) must leave queue promotion live, not stuck off."""
    calls: list[uuid.UUID] = []
    monkeypatch.setattr(
        lifecycle_mod, "run_lifecycle", _done_marking_lifecycle(calls)
    )

    await runner.shutdown()  # real shutdown flips the module flag
    try:
        queued = make_run(session_factory, status="queued")
        await runner.startup_sweep(
            session_factory=session_factory, settings=settings, runtime=fake_runtime
        )
        await _wait_calls(calls, 1)
        assert calls == [queued.id]
        assert _status(session_factory, queued.id) == "done"
    finally:
        runner._accepting = True  # don't leak the shutdown state


# ---------------------------------------------------------------------------
# wait_for_headroom — the memory gate
# ---------------------------------------------------------------------------


def _readings(*values: int | None):
    """Scripted mem_reader: yields each available_bytes value, then repeats
    the last one forever."""

    seq = list(values)

    def reader() -> MemReading | None:
        v = seq.pop(0) if len(seq) > 1 else seq[0]
        if v is None:
            return None
        return MemReading(available_bytes=v, total_bytes=64 * 1024 * MIB, source="fake")

    return reader


async def test_wait_disabled_skips_reader(session_factory, settings) -> None:
    settings.SPAWN_MIN_FREE_MEM_MB = 0
    consulted = []

    def reader():
        consulted.append(1)
        return MemReading(0, 1, "fake")

    run = make_run(session_factory, status="provisioning")
    ok = await wait_for_headroom(
        session_factory=session_factory,
        run_id=run.id,
        sandbox_id=uuid.uuid4(),
        settings=settings,
        mem_reader=reader,
    )
    assert ok is True
    assert consulted == []


async def test_wait_proceeds_immediately_with_headroom(
    session_factory, settings
) -> None:
    settings.SPAWN_MIN_FREE_MEM_MB = 1  # 1 MiB floor
    run = make_run(session_factory, status="provisioning")
    ok = await wait_for_headroom(
        session_factory=session_factory,
        run_id=run.id,
        sandbox_id=uuid.uuid4(),
        settings=settings,
        mem_reader=_readings(512 * MIB),
    )
    assert ok is True
    assert _stage_events(session_factory, run.id, "spawn_mem_wait") == []


async def test_wait_parks_then_releases(
    session_factory, settings, fake_runtime
) -> None:
    """Under pressure the spawn waits; release emits wait + release events."""
    settings.SPAWN_MIN_FREE_MEM_MB = 256
    settings.SPAWN_MEM_POLL_SECONDS = 0.01
    run = make_run(session_factory, status="provisioning")
    sid = make_sandbox(session_factory, run.id, status="provisioning").id

    # two pressured readings (100MiB < 256MiB), then headroom
    reader = _readings(100 * MIB, 100 * MIB, 600 * MIB)
    ok = await wait_for_headroom(
        session_factory=session_factory,
        run_id=run.id,
        sandbox_id=sid,
        settings=settings,
        mem_reader=reader,
    )
    assert ok is True

    waits = [
        e
        for e in _stage_events(session_factory, run.id, "spawn_mem_wait")
        if e.sandbox_id == sid
    ]
    assert len(waits) == 1
    assert waits[0].payload["required_mb"] == 256.0
    assert waits[0].payload["available_mb"] == 100.0
    releases = _stage_events(session_factory, run.id, "spawn_mem_release")
    assert len(releases) == 1
    assert releases[0].payload["waited_seconds"] >= 0


async def test_wait_timeout_returns_false(session_factory, settings) -> None:
    """Sustained pressure past SPAWN_MEM_WAIT_SECONDS -> caller fails the
    sandbox (bounded — a wedged host can't hang the run forever)."""
    settings.SPAWN_MIN_FREE_MEM_MB = 256
    settings.SPAWN_MEM_POLL_SECONDS = 0.01
    settings.SPAWN_MEM_WAIT_SECONDS = 0.05
    run = make_run(session_factory, status="provisioning")
    sid = make_sandbox(session_factory, run.id, status="provisioning").id

    ok = await wait_for_headroom(
        session_factory=session_factory,
        run_id=run.id,
        sandbox_id=sid,
        settings=settings,
        mem_reader=_readings(100 * MIB),
    )
    assert ok is False
    timeouts = _stage_events(session_factory, run.id, "spawn_mem_timeout")
    assert len(timeouts) == 1


async def test_wait_unreadable_probe_proceeds(session_factory, settings) -> None:
    """No usable memory reading -> gate is inert (never blocks a spawn on
    an unmeasurable host)."""
    settings.SPAWN_MIN_FREE_MEM_MB = 256
    run = make_run(session_factory, status="provisioning")
    ok = await wait_for_headroom(
        session_factory=session_factory,
        run_id=run.id,
        sandbox_id=uuid.uuid4(),
        settings=settings,
        mem_reader=_readings(None),
    )
    assert ok is True


# ---------------------------------------------------------------------------
# memory probe readers
# ---------------------------------------------------------------------------


def test_proc_meminfo_parses_kb_fields(tmp_path: Path) -> None:
    meminfo = tmp_path / "meminfo"
    meminfo.write_text(
        "MemTotal:       16384000 kB\n"
        "MemFree:         1024000 kB\n"
        "MemAvailable:    8192000 kB\n"
        "Buffers:          512000 kB\n"
    )
    reading = pacing._proc_meminfo(meminfo)
    assert reading is not None
    assert reading.total_bytes == 16384000 * 1024
    assert reading.available_bytes == 8192000 * 1024  # MemAvailable, not MemFree
    assert reading.source == "proc_meminfo"


def test_proc_meminfo_missing_or_malformed(tmp_path: Path) -> None:
    assert pacing._proc_meminfo(tmp_path / "nonexistent") is None
    bad = tmp_path / "meminfo"
    bad.write_text("not a meminfo file\n")
    assert pacing._proc_meminfo(bad) is None


def test_parse_docker_size_units() -> None:
    assert pacing._parse_docker_size("1.5GiB") == int(1.5 * 1024**3)
    assert pacing._parse_docker_size("300MiB") == 300 * 1024**2
    assert pacing._parse_docker_size("512B") == 512
    assert pacing._parse_docker_size("12kB") == 12_000
    assert pacing._parse_docker_size("  2GiB  ") == 2 * 1024**3
    assert pacing._parse_docker_size("garbage") is None
    assert pacing._parse_docker_size("12XB") is None


def test_read_host_memory_chain_order_and_fallback(monkeypatch) -> None:
    calls: list[str] = []

    def _mk(name, result):
        def probe():
            calls.append(name)
            return result

        return probe

    # all probes fail -> None
    monkeypatch.setattr(pacing, "_proc_meminfo", _mk("proc", None))
    monkeypatch.setattr(pacing, "_sysconf", _mk("sysconf", None))
    monkeypatch.setattr(pacing, "_docker_stats", _mk("docker", None))
    assert pacing.read_host_memory() is None
    assert calls == ["proc", "sysconf", "docker"]

    # sysconf succeeds -> chain stops there
    calls.clear()
    reading = MemReading(1, 2, "sysconf")
    monkeypatch.setattr(pacing, "_sysconf", _mk("sysconf", reading))
    assert pacing.read_host_memory() == reading
    assert calls == ["proc", "sysconf"]


# ---------------------------------------------------------------------------
# spawn_one integration — gate in the provisioning path
# ---------------------------------------------------------------------------


async def test_spawn_one_waits_for_memory_then_spawns(
    session_factory, settings, fake_runtime, monkeypatch
) -> None:
    settings.SPAWN_MIN_FREE_MEM_MB = 256
    settings.SPAWN_MEM_POLL_SECONDS = 0.01
    monkeypatch.setattr(
        pacing, "read_host_memory", _readings(50 * MIB, 600 * MIB)
    )

    run = make_run(session_factory, status="provisioning", config=run_config())
    sid = await spawn_one(
        session_factory=session_factory,
        runtime=fake_runtime,
        run_id=run.id,
        harness_config=run.config["harness_configs"][0],
        settings=settings,
    )
    assert sid is not None
    assert len(fake_runtime.spawned) == 1
    with session_factory() as s:
        sb = s.get(Sandbox, sid)
        assert sb.status == "running"
    assert _stage_events(session_factory, run.id, "spawn_mem_wait")
    assert _stage_events(session_factory, run.id, "spawn_mem_release")


async def test_spawn_one_mem_timeout_fails_sandbox(
    session_factory, settings, fake_runtime, monkeypatch
) -> None:
    settings.SPAWN_MIN_FREE_MEM_MB = 256
    settings.SPAWN_MEM_POLL_SECONDS = 0.01
    settings.SPAWN_MEM_WAIT_SECONDS = 0.05
    monkeypatch.setattr(pacing, "read_host_memory", _readings(50 * MIB))

    run = make_run(session_factory, status="provisioning", config=run_config())
    sid = await spawn_one(
        session_factory=session_factory,
        runtime=fake_runtime,
        run_id=run.id,
        harness_config=run.config["harness_configs"][0],
        settings=settings,
    )
    assert sid is None
    assert fake_runtime.spawned == []
    with session_factory() as s:
        sb = s.execute(
            select(Sandbox).where(Sandbox.run_id == run.id)
        ).scalar_one()
        assert sb.status == "failed"
        assert sb.terminal_reason == "error"
        errs = [
            e
            for e in s.execute(
                select(Event).where(Event.run_id == run.id, Event.type == "error")
            ).scalars()
        ]
        assert errs and "memory" in errs[0].payload["message"]


async def test_spawn_one_ungated_probe_failure_still_spawns(
    session_factory, settings, fake_runtime, monkeypatch
) -> None:
    """Probe returning None (can't measure) never blocks the spawn."""
    settings.SPAWN_MIN_FREE_MEM_MB = 256
    monkeypatch.setattr(pacing, "read_host_memory", _readings(None))

    run = make_run(session_factory, status="provisioning", config=run_config())
    sid = await spawn_one(
        session_factory=session_factory,
        runtime=fake_runtime,
        run_id=run.id,
        harness_config=run.config["harness_configs"][0],
        settings=settings,
    )
    assert sid is not None
    assert len(fake_runtime.spawned) == 1
