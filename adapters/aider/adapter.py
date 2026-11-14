"""Harness adapter contract for Aider (``aider --message``).

The sandbox runs the Python shim (``python3 -m adapters.aider``) which
writes ``$HOME/.aider.conf.yml`` (openai provider → gateway
``/v1/chat/completions``, headless flags, ``weak-model`` pinned to the run
model), then spawns ``aider --yes-always --message "<task>"`` and
translates its plain-text status lines via ``translate.py``.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from typing import Any

from adapters.common import contract

DOCKERFILE_REPO_PATH = "sandbox/images/aider.Dockerfile"
IMAGE = "heg-sandbox-aider:latest"
MODULE = "adapters.aider"

MANIFEST = {
    "name": "aider",
    "display_name": "Aider",
    "description": (
        "aider --yes-always --message in a Python sandbox; model traffic is "
        "OpenAI chat completions via litellm's openai provider "
        "(OPENAI_API_BASE → the AI gateway). Plain-text output is "
        "translated heuristically — aider has no JSONL mode."
    ),
    "llm_protocol": "openai-chat-completions",
    "cli": "aider",
    "cli_version": "0.86.2",
    "install": "pip install aider-chat",
    "image": IMAGE,
    "requires": ["gateway:/v1/chat/completions"],
}


def _extra(cred: str, gateway_url: str) -> dict[str, str]:
    base = f"{gateway_url}/v1"
    return {
        "OPENAI_API_KEY": cred,
        # litellm honors both names — OPENAI_BASE_URL became canonical in
        # litellm 1.68, which briefly broke openai-api-base handling. Send
        # both so every aider code path lands at the gateway's /v1 root.
        "OPENAI_API_BASE": base,
        "OPENAI_BASE_URL": base,
        # Belt-and-suspenders for paths outside conf-file/argv control.
        "AIDER_ANALYTICS_DISABLE": "true",
        "AIDER_CHECK_UPDATE": "false",
        # Git identity so aider's auto-commits work in the repo it creates
        # under /workspace (raw `git commit` needs an author/committer).
        "GIT_AUTHOR_NAME": "aider",
        "GIT_AUTHOR_EMAIL": "aider@heg.local",
        "GIT_COMMITTER_NAME": "aider",
        "GIT_COMMITTER_EMAIL": "aider@heg.local",
    }


def image_spec() -> dict[str, Any]:
    return contract.cli_image_spec(
        name="aider", image=IMAGE, dockerfile=DOCKERFILE_REPO_PATH, module=MODULE
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
    """Aider env: the per-sandbox cred reaches litellm as ``OPENAI_API_KEY``."""
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
    """Parse aider's plain-text output + shim markers into event dicts."""
    from adapters.aider.translate import translate_obj, translate_text

    return contract.cli_translate_events(stdout_lines, translate_obj, translate_text)


def parse_terminal_state(logs: str) -> str | None:
    return contract.cli_parse_terminal_state(logs)


def completion_signal() -> dict[str, Any]:
    return contract.cli_completion_signal()
