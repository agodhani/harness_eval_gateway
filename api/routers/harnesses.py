"""Phase 2.5 — ``GET /api/harnesses``: the registered harness adapters.

Serves the adapter manifests the submit form uses to populate its harness
dropdown (display name, protocol, install line, sandbox image). The list is
the orchestrator registry — adding an adapter module + image is enough to
make it selectable; no frontend constants to keep in sync.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from orchestrator import adapters as orch_adapters

router = APIRouter(tags=["harnesses"])


@router.get("/api/harnesses")
async def list_harnesses() -> list[dict[str, Any]]:
    """Adapter manifests for every registered harness (registry order)."""
    return orch_adapters.harnesses()
