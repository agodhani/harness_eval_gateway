"""PR18 — opt-in upstream response cache for deterministic replay.

Dev/CI aid: when ``GATEWAY_CACHE_ENABLED`` is set, the proxy serves repeat
requests from a local sqlite store instead of re-calling the upstream, so a
run can be replayed cheaply (and offline) once the cache is warm.

Keying
------
``sha256`` over canonical JSON ``{"path", "provider", "body"}`` of the
*outgoing* (post-transform) request, salted with
``GATEWAY_CACHE_NAMESPACE``. The outgoing body already carries ``model``,
``stream``, injected defaults (``num_ctx``/``keep_alive``) and
``stream_options``, so live and replayed requests key identically while
``stream: true`` vs ``stream: false`` and per-provider routes can never
collide — a ``PROVIDER_ROUTES`` change routes to a different provider and
misses. Bump the namespace to invalidate every entry without deleting the
file.

Storage
-------
Stdlib ``sqlite3``, single table ``responses(key, status, content_type,
body, created_at, last_access, hit_count)``, bounded by
``GATEWAY_CACHE_MAX_ENTRIES`` with LRU eviction (``last_access``). A file
path persists across restarts; ``:memory:`` works for tests. Only complete
2xx responses are stored — errors and client-aborted streams always reach
the real upstream.

Metering semantics
------------------
Hits still emit ``llm_request``/``llm_response`` (payload ``cached: true``)
with usage re-extracted from the stored body, and the budget gate still runs
before lookup — a replayed run accrues tokens and 429s exactly as the live
run did. ``/v1/messages/count_tokens`` and ``GET /v1/models`` (unmetered
probes) bypass the cache entirely.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS responses (
    key          TEXT PRIMARY KEY,
    status       INTEGER NOT NULL,
    content_type TEXT NOT NULL,
    body         BLOB NOT NULL,
    created_at   REAL NOT NULL,
    last_access  REAL NOT NULL,
    hit_count    INTEGER NOT NULL DEFAULT 0
)
"""


@dataclass(frozen=True)
class CachedResponse:
    """A stored upstream response, replayable verbatim."""

    status: int
    content_type: str
    body: bytes


class ResponseCache:
    """sqlite-backed request→response store. All methods are thread-safe.

    One connection guarded by a lock (``check_same_thread=False``) — proxy
    calls reach it via ``asyncio.to_thread``. Failures are the caller's to
    swallow: the proxy must treat the cache as best-effort, like emission.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        max_entries: int = 10_000,
        namespace: str = "v1",
    ) -> None:
        self.namespace = namespace
        self._max_entries = max(1, max_entries)
        self._lock = threading.Lock()
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        with self._lock:
            self._conn.execute(_SCHEMA)
            self._conn.commit()

    def key_for(self, *, path: str, provider: str, body: dict[str, Any]) -> str:
        """Stable cache key for the outgoing request ``body`` at ``path``.

        ``provider`` is keyed explicitly (not just via ``body.model``) so
        re-routed requests miss rather than replay a different upstream's
        bytes.
        """
        canonical = json.dumps(
            {"path": path, "provider": provider, "body": body},
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        digest = hashlib.sha256(canonical.encode()).hexdigest()
        return f"{self.namespace}:{digest}"

    def get(self, key: str) -> CachedResponse | None:
        """Fetch a stored response; updates LRU timestamps on hit."""
        with self._lock:
            row = self._conn.execute(
                "SELECT status, content_type, body FROM responses WHERE key = ?",
                (key,),
            ).fetchone()
            if row is None:
                return None
            self._conn.execute(
                "UPDATE responses SET last_access = ?, hit_count = hit_count + 1"
                " WHERE key = ?",
                (time.time(), key),
            )
            self._conn.commit()
        return CachedResponse(status=row[0], content_type=row[1], body=row[2])

    def store(
        self,
        key: str,
        *,
        status: int,
        content_type: str,
        body: bytes,
    ) -> None:
        """Insert/replace an entry, then evict least-recently-used over the cap."""
        now = time.time()
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO responses"
                " (key, status, content_type, body, created_at, last_access,"
                "  hit_count)"
                " VALUES (?, ?, ?, ?, ?, ?, 0)",
                (key, status, content_type, body, now, now),
            )
            self._conn.execute(
                "DELETE FROM responses WHERE key NOT IN"
                " (SELECT key FROM responses ORDER BY last_access DESC LIMIT ?)",
                (self._max_entries,),
            )
            self._conn.commit()

    def clear(self) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM responses")
            self._conn.commit()

    def __len__(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) FROM responses").fetchone()
        return int(row[0])

    def close(self) -> None:
        with self._lock:
            self._conn.close()
