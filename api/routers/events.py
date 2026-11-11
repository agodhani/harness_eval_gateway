"""F6 — event ingestion, query, and SSE streaming.

- ``POST /api/events/ingest`` — authenticated batch (or single-event) insert.
  One transaction per request; run/sandbox existence is validated up front so
  FK violations surface as clean 404/422s instead of IntegrityErrors.
- ``GET  /api/runs/{run_id}/events`` — filtered, id-ordered query
  (``after_id`` cursor pagination, ``limit`` capped at 1000).
- ``GET  /api/runs/{run_id}/events/stream`` — SSE stream polling the DB every
  ~1s for rows with ``id > last_seen``. MVP polling is fine (AGENTS.md); it
  replays existing events first, then tails new ones.
"""

from __future__ import annotations

import secrets
import time
from collections.abc import Iterator
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from api.config import get_settings
from api.deps import get_db, get_session_factory
from api.schemas import (
    EventOut,
    EventSource,
    EventType,
    IngestEvent,
    IngestRequest,
)
from db.models import Event, Run, Sandbox

router = APIRouter(tags=["events"])

DbSession = Annotated[Session, Depends(get_db)]

INGEST_TOKEN_HEADER = "X-Ingest-Token"


def require_ingest_token(
    x_ingest_token: Annotated[str | None, Header(alias=INGEST_TOKEN_HEADER)] = None,
) -> None:
    """Shared-secret auth for machine producers (gateway, orchestrator)."""
    settings = get_settings()
    if x_ingest_token is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"missing {INGEST_TOKEN_HEADER} header",
        )
    if not secrets.compare_digest(x_ingest_token, settings.INGEST_TOKEN):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="invalid ingest token",
        )


def _validate_events(session: Session, events: list[IngestEvent]) -> None:
    """Check run/sandbox existence + sandbox↔run consistency.

    Raises 404 for unknown ids, 422 when a sandbox belongs to a different run.
    """
    run_ids = {e.run_id for e in events}
    found_runs = set(
        session.execute(select(Run.id).where(Run.id.in_(run_ids))).scalars().all()
    )
    missing_runs = run_ids - found_runs
    if missing_runs:
        sample = ", ".join(sorted(str(r) for r in missing_runs)[:5])
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"unknown run_id(s): {sample}",
        )

    sandbox_ids = {e.sandbox_id for e in events if e.sandbox_id is not None}
    if not sandbox_ids:
        return
    sandbox_rows = session.execute(
        select(Sandbox.id, Sandbox.run_id).where(Sandbox.id.in_(sandbox_ids))
    ).all()
    sandbox_run = {sid: rid for sid, rid in sandbox_rows}
    missing_sandboxes = sandbox_ids - set(sandbox_run)
    if missing_sandboxes:
        sample = ", ".join(sorted(str(s) for s in missing_sandboxes)[:5])
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"unknown sandbox_id(s): {sample}",
        )
    for e in events:
        if e.sandbox_id is not None and sandbox_run[e.sandbox_id] != e.run_id:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=(
                    f"sandbox_id {e.sandbox_id} does not belong to run_id {e.run_id}"
                ),
            )


def _to_row(e: IngestEvent) -> Event:
    """Build an Event row; omit unset optionals so server defaults apply."""
    kwargs: dict = {
        "run_id": e.run_id,
        "sandbox_id": e.sandbox_id,
        "seq": e.seq,
        "source": e.source.value,
        "type": e.type.value,
        "payload": e.payload,
    }
    if e.ts is not None:
        kwargs["ts"] = e.ts
    return Event(**kwargs)


@router.post("/api/events/ingest")
def ingest_events(
    body: IngestRequest | IngestEvent,
    session: DbSession,
    _auth: Annotated[None, Depends(require_ingest_token)],
) -> dict[str, int]:
    """Insert a batch (``{"events": [...]}``) or a single bare event object.

    All-or-nothing: validation failures reject the whole batch; the insert
    itself is a single transaction.
    """
    events_in = body.events if isinstance(body, IngestRequest) else [body]
    _validate_events(session, events_in)

    session.add_all(_to_row(e) for e in events_in)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        # FK race (row deleted between check and insert) or other constraint.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"event insert failed: {exc.orig}",
        ) from exc
    return {"accepted": len(events_in)}


@router.get("/api/runs/{run_id}/events")
def list_events(
    run_id: UUID,
    session: DbSession,
    source: Annotated[EventSource | None, Query()] = None,
    event_type: Annotated[EventType | None, Query(alias="type")] = None,
    sandbox_id: Annotated[UUID | None, Query()] = None,
    after_id: Annotated[int | None, Query(ge=0)] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
) -> list[EventOut]:
    """Events for a run, ordered by id ASC.

    ``after_id`` is a cursor (returns rows with ``id > after_id``) — feed the
    last seen id to page forward. ``limit`` defaults to 200, max 1000.
    """
    stmt = (
        select(Event)
        .where(Event.run_id == run_id)
        .order_by(Event.id)
        .limit(limit)
    )
    if source is not None:
        stmt = stmt.where(Event.source == source.value)
    if event_type is not None:
        stmt = stmt.where(Event.type == event_type.value)
    if sandbox_id is not None:
        stmt = stmt.where(Event.sandbox_id == sandbox_id)
    if after_id is not None:
        stmt = stmt.where(Event.id > after_id)

    rows = session.execute(stmt).scalars().all()
    return [EventOut.model_validate(r) for r in rows]


def _event_sse(ev: Event) -> str:
    return f"id: {ev.id}\ndata: {EventOut.model_validate(ev).model_dump_json()}\n\n"


def stream_run_events(
    run_id: UUID,
    *,
    poll_interval: float = 1.0,
    max_idle_polls: int | None = None,
    page_size: int = 500,
) -> Iterator[str]:
    """SSE body generator: replay backlog, then poll for new events.

    Yields ``id:``/``data:`` pairs plus ``: keepalive`` comments while idle.
    ``max_idle_polls`` exists for tests — the endpoint leaves it unset (stream
    until the client disconnects).
    """
    factory = get_session_factory()
    last_id = 0
    idle_polls = 0
    while True:
        with factory() as session:
            rows = (
                session.execute(
                    select(Event)
                    .where(Event.run_id == run_id, Event.id > last_id)
                    .order_by(Event.id)
                    .limit(page_size)
                )
                .scalars()
                .all()
            )
        if rows:
            idle_polls = 0
            for ev in rows:
                last_id = ev.id
                yield _event_sse(ev)
        else:
            idle_polls += 1
            yield ": keepalive\n\n"
            if max_idle_polls is not None and idle_polls >= max_idle_polls:
                return
        time.sleep(poll_interval)


@router.get("/api/runs/{run_id}/events/stream")
def stream_events(run_id: UUID) -> StreamingResponse:
    return StreamingResponse(
        stream_run_events(run_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )
