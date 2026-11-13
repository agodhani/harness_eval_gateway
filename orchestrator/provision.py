"""Sandbox provisioning (F2) — skillpack staging, env building, spawn.

:func:`spawn_one` is the per-harness-config unit of work awaited by
``lifecycle.run_lifecycle``. DB access runs directly on the event-loop
thread (tiny queries — see db/session.py); only the blocking file copies
and the ``docker run`` spawn go through ``asyncio.to_thread``.

Sandbox-side network constants: sandboxes live on the ``heg-sandbox``
``--internal`` network where the gateway is reachable ONLY as
``http://gateway:8080`` — that URL (not ``settings.GATEWAY_URL``, which is the
api-side address) goes into sandbox env.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from db.models import Run, Sandbox
from gateway.creds import mint_cred
from sandbox.runtime import SandboxRuntime
from sandbox.spec import SandboxSpec
from skillpack_import.store import imported_root

from . import state_machine
from .adapters import adapter_image, get_adapter
from .events import emit

log = logging.getLogger(__name__)

# URLs as seen from INSIDE a sandbox container (internal docker network).
GATEWAY_INTERNAL_URL = "http://gateway:8080"
GATEWAY_INGEST_URL = f"{GATEWAY_INTERNAL_URL}/internal/events"

DEFAULT_MAX_ITERS = 50
DEFAULT_TOKEN_BUDGET = 200_000
DEFAULT_WALL_CLOCK_SECONDS = 3600
# Cred outlives the wall-clock cap so a sandbox finishing near the deadline
# can still emit its final events.
CRED_TTL_GRACE_SECONDS = 3600


# ---------------------------------------------------------------------------
# skill packs
# ---------------------------------------------------------------------------


def known_skillpacks(*roots: str | Path) -> list[str]:
    """Directory names under the given pack roots — the valid ``skill_packs``
    values at submit time. Multiple roots (vendored ``skillpacks/`` plus the
    imported ``data/skillpacks/``) merge into one namespace; non-directories
    (README.md etc.) are excluded."""
    names: set[str] = set()
    for r in roots:
        root = Path(r)
        if not root.is_dir():
            continue
        names.update(p.name for p in root.iterdir() if p.is_dir())
    return sorted(names)


def stage_skillpacks(
    pack_names: list[str],
    dest_dir: str | Path,
    *,
    source_root: str | Path,
    extra_roots: tuple[str | Path, ...] = (),
) -> Path:
    """Copy the selected pack dirs into ``dest_dir`` (real copies — symlinks
    don't survive bind mounts). Always returns ``dest_dir`` (created even for
    an empty selection so the sandbox sees an empty packs root, NOT the full
    default mount).

    Each pack name is resolved against ``source_root`` first, then
    ``extra_roots`` in order — imported packs live in the data tree
    (``data/skillpacks/``), vendored ones in the repo tree. Pack names are
    unique across roots (imports reject vendored-name collisions), so the
    search order only matters as a defensive default.
    """
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    roots = [Path(source_root), *(Path(r) for r in extra_roots)]
    for name in pack_names:
        src: Path | None = None
        for root in roots:
            cand = root / name
            if not cand.is_dir():
                continue
            # Guard against anything weird slipping past submit validation.
            if cand.resolve().parent != root.resolve():
                raise ValueError(f"skill pack {name!r} escapes {root}")
            src = cand
            break
        if src is None:
            raise ValueError(
                f"unknown skill pack {name!r} under {[str(r) for r in roots]}"
            )
        shutil.copytree(src, dest / name, dirs_exist_ok=True)
    return dest


# ---------------------------------------------------------------------------
# env + spec construction
# ---------------------------------------------------------------------------


def build_env(
    adapter: Any,
    *,
    cred: str,
    task_prompt: str,
    model_name: str,
    max_iters: int,
    system_prompt_extra: str | None = None,
) -> dict[str, str]:
    """Sandbox env for one harness config via the adapter contract."""
    return adapter.env(
        gateway_url=GATEWAY_INTERNAL_URL,
        cred=cred,
        task_prompt=task_prompt,
        model=model_name,
        events_ingest_url=GATEWAY_INGEST_URL,
        max_iters=max_iters,
        system_prompt_extra=system_prompt_extra,
    )


def budgets_for(run: Run) -> dict[str, Any]:
    budgets = (run.config or {}).get("budgets") or {}
    return {
        "max_iters": int(budgets.get("max_iters", DEFAULT_MAX_ITERS)),
        "token_budget": int(budgets.get("token_budget", DEFAULT_TOKEN_BUDGET)),
        "wall_clock_seconds": int(
            budgets.get("wall_clock_seconds", DEFAULT_WALL_CLOCK_SECONDS)
        ),
    }


def mint_sandbox_cred(
    *,
    run_id: uuid.UUID,
    sandbox_id: uuid.UUID,
    secret: str,
    budgets: dict[str, Any],
) -> str:
    """Participant cred: sandbox-scoped, token-capped, TTL > wall clock."""
    return mint_cred(
        run_id=run_id,
        sandbox_id=sandbox_id,
        secret=secret,
        ttl_seconds=int(budgets["wall_clock_seconds"]) + CRED_TTL_GRACE_SECONDS,
        max_tokens=int(budgets["token_budget"]),
    )


def build_spec(
    *,
    sandbox_id: uuid.UUID,
    run: Run,
    harness_config: dict[str, Any],
    runtime: SandboxRuntime,
    image: str,
    env: dict[str, str],
) -> SandboxSpec:
    """Assemble the SandboxSpec for one config (paths are spawning-process
    paths; the runtime translates them for ``docker run -v``)."""
    sid = str(sandbox_id)
    adapter = get_adapter(str(harness_config.get("adapter") or "miniloop"))
    task = {
        "task_prompt": run.task_prompt,
        "model": run.model_name,
        "gateway_url": GATEWAY_INTERNAL_URL,
        "cred": env.get("GATEWAY_CRED", ""),
        "events_ingest_url": GATEWAY_INGEST_URL,
        "max_iters": env.get("MAX_ITERS"),
        "system_prompt_extra": harness_config.get("system_prompt_extra"),
    }
    launch = adapter.launch(task)
    return SandboxSpec(
        sandbox_id=sid,
        run_id=str(run.id),
        image=image,
        env=env,
        workspace_host_path=runtime.state_dir / sid / "workspace",
        skillpacks_host_path=runtime.state_dir / sid / "skillpacks",
        cmd=launch.get("cmd"),
    )


# ---------------------------------------------------------------------------
# spawn_one — the per-config provisioning unit
# ---------------------------------------------------------------------------


def _load_run(session_factory: sessionmaker[Session], run_id: uuid.UUID) -> Run | None:
    with session_factory() as session:
        return session.get(Run, run_id)


def _insert_sandbox_row(
    session_factory: sessionmaker[Session], run_id: uuid.UUID, hc: dict[str, Any]
) -> uuid.UUID:
    """Insert the Sandbox row and COMMIT before spawn — event ingest must be
    able to reference the row as soon as the container starts emitting."""
    sid = uuid.uuid4()
    with session_factory() as session:
        session.add(
            Sandbox(id=sid, run_id=run_id, harness_config=dict(hc), status="provisioning")
        )
        session.commit()
    return sid


def _mark_spawn_failed(
    session_factory: sessionmaker[Session],
    *,
    run_id: uuid.UUID,
    sandbox_id: uuid.UUID,
    message: str,
) -> None:
    with session_factory() as session:
        sb = session.get(Sandbox, sandbox_id)
        if sb is not None and sb.status not in ("done", "failed"):
            try:
                state_machine.transition(session, sb, "failed")
            except state_machine.InvalidTransition:
                sb.status = "failed"
            sb.terminal_reason = "error"
            sb.finished_at = datetime.now(UTC)
        emit(
            session,
            run_id=run_id,
            sandbox_id=sandbox_id,
            event_type="error",
            payload={"stage": "spawn", "message": message},
        )
        session.commit()


def _mark_spawned(
    session_factory: sessionmaker[Session],
    *,
    sandbox_id: uuid.UUID,
    container_id: str,
) -> None:
    with session_factory() as session:
        sb = session.get(Sandbox, sandbox_id)
        if sb is None:
            return
        sb.docker_container_id = container_id
        sb.started_at = datetime.now(UTC)
        try:
            state_machine.transition(session, sb, "running")
        except state_machine.InvalidTransition:
            sb.status = "running"
        session.commit()


async def spawn_one(
    *,
    session_factory: sessionmaker[Session],
    runtime: SandboxRuntime,
    run_id: uuid.UUID,
    harness_config: dict[str, Any],
    settings: Any,
) -> uuid.UUID | None:
    """Provision + spawn ONE sandbox for ``harness_config``.

    Returns the sandbox UUID on success, ``None`` on failure (the Sandbox row
    is still created, marked ``failed``/``error`` — spawn failures are
    per-sandbox and never abort sibling configs).

    DB helpers run on the event-loop thread; the file staging and the
    blocking ``docker run`` call are the only ``to_thread`` hops.
    """
    run = _load_run(session_factory, run_id)
    if run is None:
        log.error("spawn_one: run %s not found", run_id)
        return None
    budgets = budgets_for(run)

    sid = _insert_sandbox_row(session_factory, run_id, harness_config)
    try:
        adapter = get_adapter(str(harness_config.get("adapter") or "miniloop"))

        # Stage ONLY the selected packs (real copies under the state dir —
        # bind-mountable and per-sandbox isolated). The staged dir IS the
        # spec's skillpacks_host_path (build_spec derives it from state_dir).
        packs = list(harness_config.get("skill_packs") or [])
        await asyncio.to_thread(
            stage_skillpacks,
            packs,
            runtime.state_dir / str(sid) / "skillpacks",
            source_root=settings.SKILLPACKS_DIR,
            # User-imported packs live under the data tree (F11) — second
            # search root so they're stageable exactly like vendored ones.
            extra_roots=(imported_root(settings),),
        )

        cred = mint_sandbox_cred(
            run_id=run_id,
            sandbox_id=sid,
            secret=settings.GATEWAY_CRED_SECRET,
            budgets=budgets,
        )
        env = build_env(
            adapter,
            cred=cred,
            task_prompt=run.task_prompt,
            model_name=run.model_name,
            max_iters=budgets["max_iters"],
            system_prompt_extra=harness_config.get("system_prompt_extra"),
        )
        # Adapter-declared image wins (Phase 2.5: each CLI harness ships its
        # own image); miniloop and adapters without an ``image`` key keep the
        # configured SANDBOX_IMAGE so evaluator/dev overrides still apply.
        spec = build_spec(
            sandbox_id=sid,
            run=run,
            harness_config=harness_config,
            runtime=runtime,
            image=adapter_image(adapter, settings.SANDBOX_IMAGE),
            env=env,
        )

        result = await asyncio.to_thread(runtime.spawn, spec)
        _mark_spawned(
            session_factory, sandbox_id=sid, container_id=result.container_id
        )
        log.info("spawn_one: sandbox %s -> %s", sid, result.container_name)
        return sid
    except Exception as exc:
        log.exception("spawn_one: sandbox %s failed", sid)
        # A container may already be running (spawn returned but the
        # _mark_spawned write failed, or docker created then errored) — no
        # monitor will ever watch a failed row, so reap it best-effort.
        try:
            await asyncio.to_thread(runtime.remove, str(sid))
        except Exception:
            log.debug("spawn_one: no container to remove for %s", sid, exc_info=True)
        _mark_spawn_failed(
            session_factory,
            run_id=run_id,
            sandbox_id=sid,
            message=f"{type(exc).__name__}: {exc}",
        )
        return None
