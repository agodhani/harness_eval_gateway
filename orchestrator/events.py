"""Orchestrator event emission — direct DB inserts, no HTTP hop.

Orchestrator tasks run inside the api process with direct DB access, so events
are written as ``Event`` rows (``source="orchestrator"``) instead of going
through ``/api/events/ingest``. ``emit()`` only ``add`` + ``flush`` — the
caller owns the transaction boundary (commit).
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.orm import Session

from db.models import Event

SOURCE = "orchestrator"


def emit(
    session: Session,
    *,
    run_id: uuid.UUID,
    sandbox_id: uuid.UUID | None = None,
    event_type: str,
    payload: dict[str, Any] | None = None,
    source: str = SOURCE,
) -> Event:
    """Insert one orchestrator event row (flushed, uncommitted)."""
    event = Event(
        run_id=run_id,
        sandbox_id=sandbox_id,
        source=source,
        type=event_type,
        payload=payload or {},
    )
    session.add(event)
    session.flush()
    return event
