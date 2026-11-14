"""F12 — artifact preview endpoints: serve a sandbox's snapshot in a browser.

- ``POST /api/runs/{run_id}/sandboxes/{sandbox_id}/preview`` — detect a
  previewable artifact (static html entry, a GUI app for desktop, or a
  web app for python-server) and
  spawn a locked-down serving container; returns ``{preview_id, url, type,
  status, entry, expires_at}``. Idempotent per sandbox: an already-live
  preview is returned as-is — unless an explicit ``entry``/``type``
  requests a different plan, in which case the live container is stopped
  and replaced. Optional body ``{"entry": "rel/path"}`` picks an explicit
  entry — for ambiguous scans (the 422 detail lists candidates) or
  artifacts detection missed; ``.html``/``.htm`` → static, ``.py`` →
  desktop, ``manage.py`` → python-server, jailed to the workspace.
  ``{"type": "static"|"desktop"|"python-server"}``
  overrides the pick: alone it selects the first detected candidate of
  that type (a both-matching workspace defaults to static, so
  ``{"type": "desktop"}`` reaches the game); combined with ``entry`` it
  overrides the suffix-derived type — ``{"entry": "x.py", "type":
  "static"}`` serves the file instead of executing it, while ``desktop``
  and ``python-server`` still require a ``.py`` entry.
- ``GET .../preview`` — current preview state (newest container), 404 when
  none exists.
- ``DELETE .../preview`` — stop the preview container(s) for the sandbox.

The workspace being served must resolve inside ``ARTIFACT_ROOT`` (same jail
rule as artifact downloads). Preview state lives on docker labels, so a
restart of the api loses nothing; ``start_reaper`` (wired into the FastAPI
lifespan) enforces the TTL for expired/orphaned containers.

Extensibility: ``PreviewOut.type`` mirrors :class:`sandbox.preview.PreviewPlan`.
Implemented spawners: ``static`` (http.server), ``desktop`` (GUI executed
under Xvnc, streamed via noVNC through a socat sidecar), and
``python-server`` (a web app executed by a relay runner — whatever port it
binds is made reachable on :8000, published through the same socat sidecar
topology). All executed-code types run on the ``--internal``
``heg-desktop`` net with zero egress.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from pathlib import Path
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Body, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from api.config import get_settings
from api.deps import get_db
from db.models import Run, Sandbox
from orchestrator import runner
from orchestrator.events import emit
from sandbox.preview import (
    Preview,
    PreviewManager,
    plan_for_entry,
    plan_for_type,
    resolve_workspace_dir,
    scan_preview,
)

log = logging.getLogger(__name__)

router = APIRouter(tags=["preview"])

DbSession = Annotated[Session, Depends(get_db)]


# ---------------------------------------------------------------------------
# response model + manager singleton
# ---------------------------------------------------------------------------


class PreviewStart(BaseModel):
    """Optional POST body — explicit entry point and/or preview-type override.

    ``entry`` is a workspace-relative path (``game/main.py``, ``index.html``)
    jailed to the artifact workspace: must exist, not be a symlink, and —
    without ``type`` — end in ``.html``/``.htm`` (→ static), ``.py`` (→
    desktop), or ``manage.py`` (→ python-server). It both picks one of
    several detected candidates and reaches
    artifacts auto-detection missed — an explicit ``.py`` bypasses
    framework evidence (the pick IS the disambiguation; isolation is
    identical either way).

    ``type`` overrides how the artifact is previewed. Alone (no ``entry``)
    it selects the first detected candidate of that type — a both-matching
    workspace defaults to static, so ``{"type": "desktop"}`` reaches the
    game. With ``entry`` it forces the plan type: ``"static"`` serves any
    jailed file (view a ``.py``'s source instead of executing it — the
    entry is only the URL opened), ``"desktop"`` and ``"python-server"``
    require a ``.py`` entry.
    """

    entry: str | None = None
    type: str | None = None


class PreviewOut(BaseModel):
    preview_id: UUID
    run_id: UUID
    sandbox_id: UUID
    type: str  # static | desktop | python-server
    status: str  # docker container status: created|running|exited|...
    url: str | None = None
    entry: str | None = None
    container: str
    created_at: datetime | None = None
    expires_at: datetime | None = None


def _out(preview: Preview) -> PreviewOut:
    return PreviewOut(
        preview_id=UUID(preview.preview_id),
        run_id=UUID(preview.run_id),
        sandbox_id=UUID(preview.sandbox_id),
        type=preview.type,
        status=preview.status,
        url=preview.url,
        entry=preview.entry,
        container=preview.container,
        created_at=preview.created_at,
        expires_at=preview.expires_at,
    )


_manager: PreviewManager | None = None


def get_preview_manager() -> PreviewManager:
    """Process-wide PreviewManager, wired to the orchestrator's runtime.

    Reuses ``runner.get_runtime()`` so artifact_root / HOST_DATA_ROOT
    translation matches sandbox spawning (compose-mode api).
    """
    global _manager
    if _manager is None:
        settings = get_settings()
        _manager = PreviewManager(
            runtime=runner.get_runtime(),
            image=settings.PREVIEW_IMAGE,
            network=settings.PREVIEW_NETWORK,
            bind_host=settings.PREVIEW_BIND_HOST,
            ttl_seconds=settings.PREVIEW_TTL_SECONDS,
            desktop_image=settings.PREVIEW_DESKTOP_IMAGE,
            server_image=settings.PREVIEW_SERVER_IMAGE,
            proxy_image=settings.PREVIEW_PROXY_IMAGE,
            desktop_network=settings.PREVIEW_DESKTOP_NETWORK,
            desktop_cpus=settings.PREVIEW_DESKTOP_CPUS,
            desktop_memory=settings.PREVIEW_DESKTOP_MEMORY,
            desktop_pids=settings.PREVIEW_DESKTOP_PIDS,
        )
    return _manager


def reset_preview_manager() -> None:
    """Test hook: drop the cached manager."""
    global _manager
    _manager = None


# ---------------------------------------------------------------------------
# TTL reaper (lifespan hooks)
# ---------------------------------------------------------------------------

_reaper_task: asyncio.Task[None] | None = None


async def _reaper_loop(interval: float) -> None:
    # Reap-then-sleep: the first sweep runs immediately at boot — orphans from
    # a previous api life whose TTL already elapsed shouldn't wait an interval.
    while True:
        try:
            removed = await asyncio.to_thread(get_preview_manager().reap_expired)
            if removed:
                log.info("preview reaper stopped %d expired container(s)", removed)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("preview reaper sweep failed")
        await asyncio.sleep(interval)


def start_reaper(*, interval: float | None = None) -> None:
    """Start the background TTL reaper (called from the FastAPI lifespan)."""
    global _reaper_task
    if _reaper_task is not None and not _reaper_task.done():
        return
    seconds = interval if interval is not None else get_settings().PREVIEW_REAP_SECONDS
    _reaper_task = asyncio.create_task(_reaper_loop(seconds))


async def stop_reaper() -> None:
    """Cancel the reaper on api shutdown."""
    global _reaper_task
    if _reaper_task is None:
        return
    _reaper_task.cancel()
    try:
        await _reaper_task
    except asyncio.CancelledError:
        pass
    _reaper_task = None


# ---------------------------------------------------------------------------
# endpoints
# ---------------------------------------------------------------------------


def _find_sandbox(session: Session, run_id: UUID, sandbox_id: UUID) -> Sandbox:
    if session.get(Run, run_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="run not found")
    sb = session.get(Sandbox, sandbox_id)
    if sb is None or sb.run_id != run_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="sandbox not found in run"
        )
    return sb


def _jail() -> Path:
    return Path(get_settings().ARTIFACT_ROOT).resolve()


@router.post(
    "/api/runs/{run_id}/sandboxes/{sandbox_id}/preview",
    response_model=PreviewOut,
    status_code=status.HTTP_201_CREATED,
)
async def start_preview(
    run_id: UUID,
    sandbox_id: UUID,
    session: DbSession,
    body: Annotated[PreviewStart | None, Body()] = None,
) -> PreviewOut:
    sb = _find_sandbox(session, run_id, sandbox_id)
    mgr = get_preview_manager()

    existing = await asyncio.to_thread(mgr.list, sandbox_id=str(sandbox_id))
    live = next((p for p in existing if p.live), None)
    entry = body.entry if body else None
    ptype = ((body.type or "").strip().lower() or None) if body else None
    # Idempotent: a live preview is returned as-is — unless an explicit
    # entry/type was requested, in which case the resolved plan decides
    # below (same plan → return it, different plan → stop + respawn).
    if live is not None and not entry and not ptype:
        return _out(live)

    workspace = resolve_workspace_dir(
        jail=_jail(),
        run_id=str(run_id),
        sandbox_id=str(sandbox_id),
        artifact_path=sb.artifact_path,
    )
    if workspace is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="no snapshot workspace found for this sandbox",
        )
    try:
        if entry:
            # Explicit pick: jailed to the workspace; type= may override the
            # suffix-derived plan (e.g. serve a .py instead of executing it).
            plan = plan_for_entry(workspace, entry, ptype)
        elif ptype:
            # Type-only override: first detected candidate of that type —
            # a both-matching workspace defaults to static, {"type":
            # "desktop"} reaches the game.
            plan = plan_for_type(workspace, ptype)
        else:
            # No explicit pick → the scan's default (candidates[0]; results
            # payload carries the full list for the UI's dropdown).
            plan = scan_preview(workspace).plan
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc
    if plan is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                "no previewable artifact: expected a .html/.htm entry, "
                "a GUI app (.py with pygame/pyglet/arcade/tkinter evidence), "
                "or a web app (.py with a web-framework evidence)"
            ),
        )

    if live is not None and live.type == plan.type and live.entry == plan.entry:
        return _out(live)
    # Clear stale containers — dead ones always, and a live one whose plan
    # doesn't match the explicit request.
    for preview in existing:
        await asyncio.to_thread(mgr.stop, preview.preview_id)

    try:
        preview = await asyncio.to_thread(
            mgr.spawn,
            run_id=str(run_id),
            sandbox_id=str(sandbox_id),
            serve_dir=workspace,
            plan=plan,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"preview container failed to start: {exc}",
        ) from exc

    try:
        emit(
            session,
            run_id=run_id,
            sandbox_id=sandbox_id,
            event_type="checkpoint",
            payload={
                "stage": "preview_started",
                "preview_id": preview.preview_id,
                "type": preview.type,
                "url": preview.url,
                "entry": preview.entry,
            },
        )
        session.commit()
    except Exception:
        session.rollback()
        log.warning("preview_started event emit failed", exc_info=True)

    return _out(preview)


@router.get(
    "/api/runs/{run_id}/sandboxes/{sandbox_id}/preview",
    response_model=PreviewOut,
)
async def get_preview(run_id: UUID, sandbox_id: UUID, session: DbSession) -> PreviewOut:
    _find_sandbox(session, run_id, sandbox_id)
    mgr = get_preview_manager()
    preview = await asyncio.to_thread(mgr.get_for_sandbox, str(sandbox_id))
    if preview is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="no preview for this sandbox"
        )
    return _out(preview)


@router.delete(
    "/api/runs/{run_id}/sandboxes/{sandbox_id}/preview",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def stop_preview(run_id: UUID, sandbox_id: UUID, session: DbSession) -> None:
    _find_sandbox(session, run_id, sandbox_id)
    mgr = get_preview_manager()
    existing = await asyncio.to_thread(mgr.list, sandbox_id=str(sandbox_id))
    if not existing:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="no preview for this sandbox"
        )
    for preview in existing:
        await asyncio.to_thread(mgr.stop, preview.preview_id)
    try:
        emit(
            session,
            run_id=run_id,
            sandbox_id=sandbox_id,
            event_type="checkpoint",
            payload={
                "stage": "preview_stopped",
                "preview_ids": [p.preview_id for p in existing],
            },
        )
        session.commit()
    except Exception:
        session.rollback()
        log.warning("preview_stopped event emit failed", exc_info=True)
