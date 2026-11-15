"""Harness adapter contract for Claude Code CLI.

The sandbox runs the Python shim (``python3 -m adapters.claude_code``) which
seeds ``~/.claude.json`` (first-run onboarding completed), spawns
``claude -p --output-format stream-json --verbose
--dangerously-skip-permissions``, and translates the stream into normalized
events via ``translate.py``.

Auth: ``ANTHROPIC_AUTH_TOKEN`` is sent as a Bearer header — the sandbox's
per-run ``heg.*`` credential goes straight through.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from typing import Any

from adapters.common import contract

DOCKERFILE_REPO_PATH = "sandbox/images/claude_code.Dockerfile"
IMAGE = "heg-sandbox-claude:latest"
MODULE = "adapters.claude_code"

MANIFEST = {
    "name": "claude_code",
    "display_name": "Claude Code CLI",
    "description": (
        "claude -p --output-format stream-json in a Node sandbox; model "
        "traffic is the Anthropic Messages API proxied by the AI gateway"
    ),
    "llm_protocol": "anthropic-messages",
    "cli": "claude",
    "cli_version": "2.1.325",
    "install": "npm i -g @anthropic-ai/claude-code",
    "image": IMAGE,
    "requires": ["gateway:/v1/messages"],
}


def _extra(cred: str, gateway_url: str, model: str) -> dict[str, str]:
    return {
        "ANTHROPIC_BASE_URL": gateway_url,
        "ANTHROPIC_AUTH_TOKEN": cred,
        "ANTHROPIC_MODEL": model,
        # Aux/background calls (titles, compaction helpers) use this slot —
        # same model keeps the comparison axis clean.
        "ANTHROPIC_SMALL_FAST_MODEL": model,
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": model,
        # Headless hardening: no telemetry, update checks, or OAuth pings —
        # the sandbox network only carries the gateway anyway.
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "DISABLE_TELEMETRY": "1",
        "DISABLE_AUTOUPDATER": "1",
        "DISABLE_ERROR_REPORTING": "1",
        "DISABLE_BUG_COMMAND": "1",
        "CLAUDE_CODE_ENTRYPOINT": "cli",
    }


def image_spec() -> dict[str, Any]:
    return contract.cli_image_spec(
        name="claude_code", image=IMAGE, dockerfile=DOCKERFILE_REPO_PATH, module=MODULE
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
        extra=_extra(cred, gateway_url, model),
    )


def launch(task: dict[str, Any]) -> dict[str, Any]:
    def _launch_extra(t: dict[str, Any]) -> dict[str, str]:
        cred = t.get("cred") or os.environ.get("GATEWAY_CRED", "")
        url = t.get("gateway_url") or os.environ.get("GATEWAY_BASE_URL", "")
        model = t.get("model") or os.environ.get("MODEL_NAME", "")
        return _extra(cred, url, model)

    return contract.cli_launch(
        module=MODULE, image=IMAGE, task=task, env_extra=_launch_extra
    )


def translate_events(stdout_lines: Iterable[str]) -> list[dict[str, Any]]:
    """Parse claude stream-json output + shim markers into event dicts."""
    from adapters.claude_code.translate import translate_obj

    return contract.cli_translate_events(stdout_lines, translate_obj)


def parse_terminal_state(logs: str) -> str | None:
    return contract.cli_parse_terminal_state(logs)


def completion_signal() -> dict[str, Any]:
    return contract.cli_completion_signal()
