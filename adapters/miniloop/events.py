"""Structured event emission for miniloop.

Every harness event is emitted on two channels:

1. **stdout JSONL** — always on. One JSON object per line:
   ``{"ts": <iso8601>, "seq": <int>, "source": "harness", "type": <str>,
   "payload": {...}}``. This is the primary channel the sandbox runtime tails.
2. **HTTP ingest** — when ``EVENTS_INGEST_URL`` is configured, each event is
   POSTed as the same JSON object. Best-effort only: failures are counted and
   swallowed — telemetry must never crash the agent loop.

Gateway-owned event types (``llm_request`` / ``llm_response``) are NOT emitted
here; the gateway captures those passively from the request/response stream.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from typing import Any, TextIO

import httpx

SOURCE = "harness"


class EventEmitter:
    """Emits harness events to stdout (JSONL) and optionally to an ingest URL."""

    def __init__(
        self,
        ingest_url: str | None = None,
        token: str | None = None,
        *,
        stdout: TextIO | None = None,
        client: httpx.Client | None = None,
        ingest_timeout: float = 5.0,
    ) -> None:
        self.ingest_url = ingest_url.rstrip("/") if ingest_url else None
        self._token = token
        self._stdout = stdout if stdout is not None else sys.stdout
        self._seq = 0
        self.ingest_failures = 0
        self.events_emitted = 0
        # Lazy client so tests/emitter without ingest never open sockets.
        self._client = client
        self._ingest_timeout = ingest_timeout

    # -- public API ---------------------------------------------------------

    def emit(self, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Emit one event. Returns the event dict that was written."""
        self._seq += 1
        event = {
            "ts": datetime.now(UTC).isoformat(),
            "seq": self._seq,
            "source": SOURCE,
            "type": event_type,
            "payload": payload,
        }
        self.events_emitted += 1
        line = json.dumps(event, ensure_ascii=False, default=str)
        print(line, file=self._stdout, flush=True)
        if self.ingest_url:
            self._post(event)
        return event

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    # -- internals ----------------------------------------------------------

    def _post(self, event: dict[str, Any]) -> None:
        try:
            headers = {}
            if self._token:
                headers["Authorization"] = f"Bearer {self._token}"
            if self._client is None:
                self._client = httpx.Client(timeout=self._ingest_timeout)
            self._client.post(self.ingest_url, json=event, headers=headers)
        except Exception:  # noqa: BLE001 — telemetry must never crash the loop
            self.ingest_failures += 1
