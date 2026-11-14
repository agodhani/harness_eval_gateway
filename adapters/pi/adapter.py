"""Harness adapter contract for the pi coding agent (``pi --mode json``).

The sandbox runs the Python shim (``python3 -m adapters.pi``) which writes
``~/.pi/agent/models.json`` (provider ``heg`` → gateway
``/v1/chat/completions``, apiKey ``$GATEWAY_CRED``) and
``~/.pi/agent/extensions/heg-events.ts`` (hook forwarder →
``adapters.common.hooks``), spawns
``pi --mode json --provider heg --model <model>``, and translates its JSONL
event stream via ``translate.py``.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from adapters.common import contract

DOCKERFILE_REPO_PATH = "sandbox/images/pi.Dockerfile"
IMAGE = "heg-sandbox-pi:latest"
MODULE = "adapters.pi"
PI_AGENT_DIR = "/home/agent/.pi/agent"

MANIFEST = {
    "name": "pi",
    "display_name": "Pi (pi-mono)",
    "description": (
        "pi --mode json in a Node sandbox; model traffic is OpenAI chat "
        "completions via a heg provider in ~/.pi/agent/models.json"
    ),
    "llm_protocol": "openai-chat-completions",
    "cli": "pi",
    "cli_version": "0.73.1",
    "install": "npm i -g @mariozechner/pi-coding-agent",
    "image": IMAGE,
    "requires": ["gateway:/v1/chat/completions"],
    "hooks": (
        "generated extension (extensions/heg-events.ts) forwards "
        "tool_execution_end → tool_result via adapters.common.hooks"
    ),
}


def image_spec() -> dict[str, Any]:
    return contract.cli_image_spec(
        name="pi", image=IMAGE, dockerfile=DOCKERFILE_REPO_PATH, module=MODULE
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
    # The cred reaches pi through models.json's "$GATEWAY_CRED" interpolation.
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
        extra={"PI_CODING_AGENT_DIR": PI_AGENT_DIR},
    )


def launch(task: dict[str, Any]) -> dict[str, Any]:
    return contract.cli_launch(
        module=MODULE,
        image=IMAGE,
        task=task,
        env_extra=lambda _t: {"PI_CODING_AGENT_DIR": PI_AGENT_DIR},
    )


def translate_events(stdout_lines: Iterable[str]) -> list[dict[str, Any]]:
    """Parse pi ``--mode json`` output + shim markers into event dicts."""
    from adapters.pi.translate import translate_obj

    return contract.cli_translate_events(stdout_lines, translate_obj)


def parse_terminal_state(logs: str) -> str | None:
    return contract.cli_parse_terminal_state(logs)


def completion_signal() -> dict[str, Any]:
    return contract.cli_completion_signal()
