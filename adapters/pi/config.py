"""Pi ``models.json`` writer — declares the ``heg`` provider.

``~/.pi/agent/models.json`` tells pi where compatible endpoints live
(packages/coding-agent/docs/models.md). ``apiKey: "$GATEWAY_CRED"`` is pi's
env interpolation — the per-sandbox cred never lands in a file, and pi reads
it at request time from the process env the shim inherits.

Stdlib-only (runs inside the sandbox).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from adapters.common.runner import ShimConfig


def pi_agent_dir(cfg: ShimConfig) -> Path:
    override = cfg.env.get("PI_CODING_AGENT_DIR")
    return Path(override) if override else Path(cfg.home) / ".pi" / "agent"


def models_json(cfg: ShimConfig) -> dict[str, Any]:
    return {
        "providers": {
            "heg": {
                "baseUrl": f"{cfg.gateway_url}/v1",
                "api": "openai-completions",
                "apiKey": "$GATEWAY_CRED",
                "models": [{"id": cfg.model}],
            }
        }
    }


def write_config(cfg: ShimConfig) -> Path:
    """Write ``~/.pi/agent/models.json``; returns the path written."""
    agent_dir = pi_agent_dir(cfg)
    agent_dir.mkdir(parents=True, exist_ok=True)
    path = agent_dir / "models.json"
    path.write_text(json.dumps(models_json(cfg), indent=2), encoding="utf-8")
    return path
