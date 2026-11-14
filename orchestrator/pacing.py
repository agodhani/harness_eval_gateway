"""Run pacing (PR15) — submission queue + memory-aware spawn scheduling.

Two mechanisms:

1. **Submission queue.** One run holds the execution slot at a time; an
   approval landing while another run is active — or already queued — parks
   in ``queued`` and promotes FIFO when the slot frees. The queue IS the
   ``runs.status`` column (no in-memory state), so it survives api restarts;
   ``runner.startup_sweep`` re-kicks promotion on boot.

   - :func:`place_run` — approve-time placement (``"queued"`` |
     ``"provisioning"``), called inside the approve handler's session.
   - :func:`claim_next_queued` — atomic check+promote of the oldest queued
     run in one committed session; the caller starts its lifecycle task on
     a non-None return.

   Queue order is ``started_at`` (approval time — the approve handler sets
   it before commit) falling back to ``created_at``. Every check and the
   claiming transition run synchronously inside a single session on the
   one api event loop, so two placements/claims can't interleave.

2. **Memory-aware spawning.** ``spawn_one`` awaits
   :func:`wait_for_headroom` just before ``docker run`` — at 8 harness
   configs, launching every sandbox at once can OOM the host/VM that also
   hosts the model. Pressure is probed by :func:`read_host_memory`
   (``/proc/meminfo`` → ``os.sysconf`` → ``docker info`` + ``docker
   stats``); an unreadable probe disables the gate (logged, proceed). The
   wait is capped by ``SPAWN_MEM_WAIT_SECONDS`` — an expired wait fails
   THAT sandbox like any spawn failure, so sustained pressure can't hang a
   run forever.

Pacing ``checkpoint`` events make the queue visible on the run timeline:
``run_queued`` / ``run_dequeued`` at run scope and ``spawn_mem_wait`` /
``spawn_mem_release`` / ``spawn_mem_timeout`` at sandbox scope.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import subprocess
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from db.models import Run

from . import state_machine
from .events import emit

log = logging.getLogger(__name__)

#: Run statuses that hold the single execution slot.
ACTIVE_RUN_STATES: tuple[str, ...] = ("provisioning", "running", "evaluating")
#: Statuses that block a new approval from starting immediately.
BLOCKING_RUN_STATES: tuple[str, ...] = (*ACTIVE_RUN_STATES, "queued")

#: Cadence for re-emitting ``spawn_mem_wait`` while a spawn stays parked —
#: often enough that the timeline shows the wait is live, sparse enough to
#: not flood the append-only events table.
_MEM_WAIT_HEARTBEAT_SECONDS = 60.0

_PROC_MEMINFO = Path("/proc/meminfo")


def _now() -> datetime:
    return datetime.now(UTC)


def _aware(dt: datetime | None) -> datetime | None:
    """SQLite returns naive datetimes; treat them as UTC."""
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


# ---------------------------------------------------------------------------
# run queue
# ---------------------------------------------------------------------------


def place_run(session: Session, run: Run) -> str:
    """Approve-time placement: park ``run`` behind blocking work or start it.

    Blocking = any other run holding the execution slot (``provisioning`` /
    ``running`` / ``evaluating``) or already parked (``queued``) — checking
    queued runs too preserves FIFO when an approval lands between a run
    finishing and the promotion claim. Emits ``run_queued`` when parked.
    Caller owns the commit. Returns the placed status.
    """
    ahead = session.scalar(
        select(func.count(Run.id)).where(
            Run.id != run.id, Run.status.in_(BLOCKING_RUN_STATES)
        )
    )
    if ahead:
        state_machine.transition(session, run, "queued")
        emit(
            session,
            run_id=run.id,
            event_type="checkpoint",
            payload={
                "stage": "run_queued",
                "ahead": int(ahead),
                "reason": "another run holds the execution slot",
            },
        )
        return "queued"
    state_machine.transition(session, run, "provisioning")
    return "provisioning"


def claim_next_queued(session_factory: sessionmaker[Session]) -> uuid.UUID | None:
    """Promote the oldest queued run when the execution slot is free.

    The check and the ``queued → provisioning`` transition happen inside one
    committed session — the status flip IS the claim, so a second caller can
    never promote the same run. Returns the promoted run's id (caller starts
    its lifecycle task), ``None`` when the slot is taken or the queue empty.
    """
    with session_factory() as session:
        active = session.scalar(
            select(func.count(Run.id)).where(Run.status.in_(ACTIVE_RUN_STATES))
        )
        if active:
            return None
        nxt = session.execute(
            select(Run)
            .where(Run.status == "queued")
            .order_by(Run.started_at.asc().nulls_last(), Run.created_at.asc())
            .limit(1)
        ).scalar_one_or_none()
        if nxt is None:
            return None
        nxt_id = nxt.id
        queued_seconds = None
        started = _aware(nxt.started_at)
        if started is not None:
            queued_seconds = max(0.0, (_now() - started).total_seconds())
        state_machine.transition(session, nxt, "provisioning")
        emit(
            session,
            run_id=nxt_id,
            event_type="checkpoint",
            payload={"stage": "run_dequeued", "queued_seconds": queued_seconds},
        )
        session.commit()
        return nxt_id


# ---------------------------------------------------------------------------
# host memory probes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MemReading:
    """One memory-pressure sample; ``source`` records which probe produced it."""

    available_bytes: int
    total_bytes: int
    source: str  # proc_meminfo | sysconf | docker_stats


def _proc_meminfo(path: Path = _PROC_MEMINFO) -> MemReading | None:
    """Linux host/VM memory via procfs — also right inside containers, where
    /proc reflects the host kernel (or the Docker Desktop VM, which is the
    scope sandboxes share)."""
    try:
        fields: dict[str, int] = {}
        for line in path.read_text().splitlines():
            key, _, rest = line.partition(":")
            parts = rest.split()
            if parts and parts[0].isdigit():
                fields[key.strip()] = int(parts[0]) * 1024  # kB -> bytes
    except OSError:
        return None
    total = fields.get("MemTotal", 0)
    if total <= 0:
        return None
    return MemReading(fields.get("MemAvailable", 0), total, "proc_meminfo")


def _sysconf() -> MemReading | None:
    """POSIX sysconf fallback (SC_AVPHYS_PAGES is absent on macOS -> None)."""
    try:
        page_size = int(os.sysconf("SC_PAGE_SIZE"))
        total = page_size * int(os.sysconf("SC_PHYS_PAGES"))
        available = page_size * int(os.sysconf("SC_AVPHYS_PAGES"))
    except (ValueError, OSError, AttributeError):
        return None
    if total <= 0:
        return None
    return MemReading(available, total, "sysconf")


_SIZE_UNITS = {
    "B": 1,
    "KB": 1000,
    "MB": 1000**2,
    "GB": 1000**3,
    "TB": 1000**4,
    "KIB": 1024,
    "MIB": 1024**2,
    "GIB": 1024**3,
    "TIB": 1024**4,
    "PIB": 1024**5,
}
_SIZE_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([A-Za-z]+)?\s*$")


def _parse_docker_size(text: str) -> int | None:
    """Parse a docker human size (``"1.5GiB"``, ``"300MiB"``, ``"512B"``)."""
    m = _SIZE_RE.match(text)
    if not m:
        return None
    mult = _SIZE_UNITS.get((m.group(2) or "B").upper())
    if mult is None:
        return None
    return int(float(m.group(1)) * mult)


def _docker_cli(args: list[str], *, timeout: float) -> str | None:
    try:
        proc = subprocess.run(
            ["docker", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout if proc.returncode == 0 else None


def _docker_stats() -> MemReading | None:
    """Memory as the docker daemon sees it: ``docker info`` MemTotal minus
    summed ``docker stats`` container usage.

    Misses non-container host usage (it under-reports pressure, never over),
    so it is the last-resort probe — its real niche is a host-run api on
    Docker Desktop, where /proc and SC_AVPHYS_PAGES don't exist but the VM
    total is exactly the pool sandboxes compete for.
    """
    total_out = _docker_cli(["info", "--format", "{{.MemTotal}}"], timeout=10.0)
    if total_out is None:
        return None
    try:
        total = int(float(total_out.strip()))
    except ValueError:
        return None
    if total <= 0:
        return None
    stats_out = _docker_cli(
        ["stats", "--no-stream", "--format", "{{.MemUsage}}"], timeout=30.0
    )
    if stats_out is None:
        # Usage unreadable -> no reading at all; reporting "all free" would
        # be the dangerous lie under pressure.
        return None
    used = 0
    for line in stats_out.splitlines():
        usage = _parse_docker_size(line.split("/", maxsplit=1)[0])
        if usage:
            used += usage
    return MemReading(max(total - used, 0), total, "docker_stats")


def read_host_memory() -> MemReading | None:
    """First successful probe in the chain; ``None`` when none can read."""
    for probe in (_proc_meminfo, _sysconf, _docker_stats):
        try:
            reading = probe()
        except Exception:
            log.debug("mem probe %s failed", probe.__name__, exc_info=True)
            continue
        if reading is not None:
            return reading
    return None


# ---------------------------------------------------------------------------
# spawn memory gate
# ---------------------------------------------------------------------------


def _gate_event(
    session_factory: sessionmaker[Session],
    *,
    run_id: uuid.UUID,
    sandbox_id: uuid.UUID,
    stage: str,
    **fields: Any,
) -> None:
    """Commit one pacing checkpoint; event failures must never block a spawn."""
    try:
        with session_factory() as session:
            emit(
                session,
                run_id=run_id,
                sandbox_id=sandbox_id,
                event_type="checkpoint",
                payload={"stage": stage, **fields},
            )
            session.commit()
    except Exception:
        log.exception("pacing: could not emit %s event", stage)


def _mb(nbytes: int | None) -> float | None:
    return None if nbytes is None else round(nbytes / (1024 * 1024), 1)


async def wait_for_headroom(
    *,
    session_factory: sessionmaker[Session],
    run_id: uuid.UUID,
    sandbox_id: uuid.UUID,
    settings: Any,
    mem_reader: Any | None = None,
) -> bool:
    """Wait until host memory headroom >= ``SPAWN_MIN_FREE_MEM_MB``.

    ``mem_reader`` is injectable for tests (defaults to
    :func:`read_host_memory`). Returns True to proceed with the spawn —
    including when the gate is disabled (min free <= 0) or the host memory
    is unreadable — and False when ``SPAWN_MEM_WAIT_SECONDS`` elapsed under
    continuous pressure.
    """
    min_free = int(getattr(settings, "SPAWN_MIN_FREE_MEM_MB", 0) or 0) * 1024 * 1024
    if min_free <= 0:
        return True
    reader = mem_reader if mem_reader is not None else read_host_memory
    poll = max(float(getattr(settings, "SPAWN_MEM_POLL_SECONDS", 5.0)), 0.05)
    wait_cap = float(getattr(settings, "SPAWN_MEM_WAIT_SECONDS", 3600.0))

    started = time.monotonic()
    last_emit = started
    waiting = False
    while True:
        try:
            reading = await asyncio.to_thread(reader)
        except Exception:
            log.exception("pacing: mem probe raised; proceeding ungated")
            reading = None
        now = time.monotonic()
        elapsed = now - started

        if reading is None or reading.available_bytes >= min_free:
            if waiting:
                _gate_event(
                    session_factory,
                    run_id=run_id,
                    sandbox_id=sandbox_id,
                    stage="spawn_mem_release",
                    waited_seconds=round(elapsed, 1),
                    available_mb=_mb(
                        None if reading is None else reading.available_bytes
                    ),
                )
            return True

        if elapsed >= wait_cap:
            _gate_event(
                session_factory,
                run_id=run_id,
                sandbox_id=sandbox_id,
                stage="spawn_mem_timeout",
                waited_seconds=round(elapsed, 1),
                required_mb=_mb(min_free),
                available_mb=_mb(reading.available_bytes),
            )
            return False

        if not waiting or now - last_emit >= _MEM_WAIT_HEARTBEAT_SECONDS:
            waiting = True
            last_emit = now
            _gate_event(
                session_factory,
                run_id=run_id,
                sandbox_id=sandbox_id,
                stage="spawn_mem_wait",
                waited_seconds=round(elapsed, 1),
                required_mb=_mb(min_free),
                available_mb=_mb(reading.available_bytes),
                source=reading.source,
            )
        else:
            log.info(
                "pacing: spawn %s parked on memory (%.0fMiB free < %.0fMiB)",
                sandbox_id,
                reading.available_bytes / (1024 * 1024),
                min_free / (1024 * 1024),
            )
        await asyncio.sleep(poll)
