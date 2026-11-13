"""Harness adapter contract for OpenAI Codex CLI (``codex exec --json``).

The sandbox runs the Python shim (``python3 -m adapters.codex``) which writes
``$CODEX_HOME/config.toml`` (custom provider ``heg`` → gateway
``/v1/responses``), spawns ``codex exec --json``, and translates its JSONL
item stream into normalized events via ``translate.py``.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from typing import Any

from adapters.common import contract

DOCKERFILE_REPO_PATH = "sandbox/images/codex.Dockerfile"
IMAGE = "heg-sandbox-codex:latest"
MODULE = "adapters.codex"
CODEX_HOME = "/home/agent/.codex"

MANIFEST = {
    "name": "codex",
    "display_name": "OpenAI Codex CLI",
    "description": (
        "codex exec --json in a Node sandbox; model traffic is the OpenAI "
        "Responses API proxied by the AI gateway to Ollama"
    ),
    "llm_protocol": "openai-responses",
    "cli": "codex",
    "cli_version": "0.169.0",
    "install": "npm i -g @openai/codex",
    "image": IMAGE,
    "requires": ["gateway:/v1/responses"],
}


def image_spec() -> dict[str, Any]:
    return contract.cli_image_spec(
        name="codex", image=IMAGE, dockerfile=DOCKERFILE_REPO_PATH, module=MODULE
    )


def env(
    gateway_url: str,
    cred: str,
    task_prompt: str,
    model: str,
    workspace: str = "/workspace",
    *,
    events_ingest_url: str | None = None,
    ingest_token: str | None = None,
    max_iters: int | None = None,
    skillpacks_root: str = "/opt/skillpacks",
    system_prompt_extra: str | None = None,
) -> dict[str, str]:
    """Codex env: the per-sandbox cred is read via ``env_key`` in config.toml."""
    return contract.cli_env(
        gateway_url=gateway_url,
        cred=cred,
        task_prompt=task_prompt,
        model=model,
        workspace=workspace,
        events_ingest_url=events_ingest_url,
        ingest_token=ingest_token,
        max_iters=max_iters,
        skillpacks_root=skillpacks_root,
        system_prompt_extra=system_prompt_extra,
        extra={"HEG_API_KEY": cred, "CODEX_HOME": CODEX_HOME},
    )


def launch(task: dict[str, Any]) -> dict[str, Any]:
    def _extra(t: dict[str, Any]) -> dict[str, str]:
        cred = t.get("cred") or os.environ.get("GATEWAY_CRED", "")
        return {"HEG_API_KEY": cred, "CODEX_HOME": CODEX_HOME}

    return contract.cli_launch(module=MODULE, image=IMAGE, task=task, env_extra=_extra)


def translate_events(stdout_lines: Iterable[str]) -> list[dict[str, Any]]:
    """Parse ``codex exec --json`` output + shim markers into event dicts."""
    from adapters.codex.translate import translate_obj

    return contract.cli_translate_events(stdout_lines, translate_obj)


def parse_terminal_state(logs: str) -> str | None:
    return contract.cli_parse_terminal_state(logs)


def completion_signal() -> dict[str, Any]:
    return contract.cli_completion_signal()
