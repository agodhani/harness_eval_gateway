"""Codex ``config.toml`` writer — points codex at the AI gateway.

Written inside the sandbox by the shim before ``codex exec`` starts. Codex's
``wire_api = "chat"`` support was removed upstream (Feb 2026), so the custom
provider uses ``"responses"`` — the gateway exposes ``/v1/responses`` backed
by Ollama's native implementation by default (``PROVIDER_ROUTES`` can route
it to real OpenAI).

Stdlib-only (runs inside the sandbox).
"""

from __future__ import annotations

from pathlib import Path

from adapters.common.runner import ShimConfig

CONFIG_TOML_TEMPLATE = '''\
model = "{model}"
model_provider = "heg"

[model_providers.heg]
name = "harness_eval_gateway"
base_url = "{gateway_url}/v1"
env_key = "HEG_API_KEY"
wire_api = "responses"
'''


def _toml_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def codex_home(cfg: ShimConfig) -> Path:
    return Path(cfg.env.get("CODEX_HOME") or (Path(cfg.home) / ".codex"))


def write_config(cfg: ShimConfig) -> Path:
    """Write ``$CODEX_HOME/config.toml``; returns the path written."""
    home = codex_home(cfg)
    home.mkdir(parents=True, exist_ok=True)
    config_path = home / "config.toml"
    config_path.write_text(
        CONFIG_TOML_TEMPLATE.format(
            model=_toml_escape(cfg.model),
            gateway_url=_toml_escape(cfg.gateway_url),
        ),
        encoding="utf-8",
    )
    return config_path
