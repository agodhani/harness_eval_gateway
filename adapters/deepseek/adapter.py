"""Harness adapter contract for DeepSeek Harness (``dsh --profile headless``).

The sandbox runs the Python shim (``python3 -m adapters.deepseek``) which
writes a ``cordis.patch.yml`` overlay for the headless profile (deepseek
provider ``baseURL`` → gateway, default model = the run's model), then spawns
``dsh --profile headless --json`` and translates its event stream via
``translate.py``.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from typing import Any

from adapters.common import contract

DOCKERFILE_REPO_PATH = "sandbox/images/deepseek.Dockerfile"
IMAGE = "heg-sandbox-deepseek:latest"
MODULE = "adapters.deepseek"
DSH_HOME = "/home/agent/.dsh"

MANIFEST = {
    "name": "deepseek",
    "display_name": "DeepSeek Harness (dsh)",
    "description": (
        "dsh --profile headless --json in a Node sandbox; model traffic goes "
        "through DEEPSEEK_BASE_URL → the AI gateway (Anthropic-compatible "
        "messages API)"
    ),
    "llm_protocol": "anthropic-messages",
    "cli": "dsh",
    "cli_version": "0.2.2-rc.1",
    "install": "npm i -g @deepseek-ai/dsh",
    "image": IMAGE,
    "requires": ["gateway:/v1/messages"],
}


def _extra(cred: str, gateway_url: str) -> dict[str, str]:
    return {
        "DEEPSEEK_API_KEY": cred,
        "DEEPSEEK_BASE_URL": gateway_url,
        "DSH_HOME": DSH_HOME,
    }


def image_spec() -> dict[str, Any]:
    return contract.cli_image_spec(
        name="deepseek", image=IMAGE, dockerfile=DOCKERFILE_REPO_PATH, module=MODULE
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
        extra=_extra(cred, gateway_url),
    )


def launch(task: dict[str, Any]) -> dict[str, Any]:
    def _launch_extra(t: dict[str, Any]) -> dict[str, str]:
        cred = t.get("cred") or os.environ.get("GATEWAY_CRED", "")
        url = t.get("gateway_url") or os.environ.get("GATEWAY_BASE_URL", "")
        return _extra(cred, url)

    return contract.cli_launch(
        module=MODULE, image=IMAGE, task=task, env_extra=_launch_extra
    )


def translate_events(stdout_lines: Iterable[str]) -> list[dict[str, Any]]:
    """Parse ``dsh --profile headless --json`` output + markers into events."""
    from adapters.deepseek.translate import translate_obj

    return contract.cli_translate_events(stdout_lines, translate_obj)


def parse_terminal_state(logs: str) -> str | None:
    return contract.cli_parse_terminal_state(logs)


def completion_signal() -> dict[str, Any]:
    return contract.cli_completion_signal()
