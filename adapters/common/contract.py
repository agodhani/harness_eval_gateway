"""Boilerplate for the four CLI-harness adapters' contract functions.

This module runs in the orchestrator/api process (never inside a sandbox —
the sandbox side of the same mechanism is ``adapters/common/runner.py``).
Each ``adapters/<name>/adapter.py`` delegates to these helpers so the adapter
contract (AGENTS.md: ``image_spec`` / ``env`` / ``launch`` /
``translate_events`` / ``completion_signal``) stays uniform while only the
CLI-specific bits (env var names, argv, event translation) differ.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from adapters.common.runner import (
    RESULT_MARKER,
    TERMINAL_STATES,
    parse_heg_result,
    translate_heg_marker,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Env signature shared with adapters/miniloop/adapter.py::env — provision's
#: ``build_env`` calls every adapter with the same kwargs.
def cli_env(
    *,
    gateway_url: str,
    cred: str,
    task_prompt: str,
    model: str,
    workspace: str = "/workspace",
    events_ingest_url: str | None = None,
    ingest_token: str | None = None,
    max_iters: int | None = None,
    skillpacks_root: str = "/opt/skillpacks",
    system_prompt_extra: str | None = None,
    extra: dict[str, str] | None = None,
) -> dict[str, str]:
    """Common sandbox env for CLI harnesses + per-adapter ``extra`` vars.

    ``max_iters`` is accepted for contract parity but never emitted — external
    CLIs have no iteration knob; wall-clock/token budgets still cap them.
    ``ingest_token`` is likewise unused: ingest auth is the sandbox cred.
    """
    del ingest_token, max_iters  # contract-parity params, intentionally unused
    out = {
        "GATEWAY_BASE_URL": gateway_url,
        "GATEWAY_CRED": cred,
        "MODEL_NAME": model,
        "TASK_PROMPT": task_prompt,
        "WORKSPACE": workspace,
        "SKILLPACKS_ROOT": skillpacks_root,
        "PYTHONPATH": "/opt/harness",
        "PYTHONUNBUFFERED": "1",
    }
    if events_ingest_url:
        out["EVENTS_INGEST_URL"] = events_ingest_url
    if system_prompt_extra:
        out["SYSTEM_PROMPT_EXTRA"] = system_prompt_extra
    if extra:
        out.update(extra)
    return out


def cli_image_spec(
    *, name: str, image: str, dockerfile: str, module: str
) -> dict[str, Any]:
    """``image_spec()`` for CLI adapters — same shape as miniloop's plus
    ``image`` (the docker tag the orchestrator actually spawns)."""
    return {
        "name": name,
        "image": image,
        "dockerfile": dockerfile,
        "dockerfile_path": str(REPO_ROOT / dockerfile),
        "context": str(REPO_ROOT),
        "workdir": "/workspace",
        "skillpacks_mount": {
            "host": str(REPO_ROOT / "skillpacks"),
            "container": "/opt/skillpacks",
            "mode": "ro",
        },
        "entrypoint": ["python3", "-m", module],
    }


def cli_launch(
    *,
    module: str,
    image: str,
    task: dict[str, Any],
    env_extra: Callable[[dict[str, str]], dict[str, str]] | None = None,
) -> dict[str, Any]:
    """``launch(task)`` — the would-be subprocess spec for one sandbox.

    ``task`` keys mirror miniloop's adapter; the cmd always runs the adapter's
    in-image shim (``python3 -m adapters.<name>``).
    """
    prompt = task.get("task_prompt") or task.get("prompt") or ""
    model = task.get("model") or os.environ.get("MODEL_NAME", "")
    gateway_url = task.get("gateway_url") or os.environ.get("GATEWAY_BASE_URL", "")
    cred = task.get("cred") or os.environ.get("GATEWAY_CRED", "")
    workspace = task.get("workspace", "/workspace")

    extra = env_extra(task) if env_extra is not None else None
    env_vars = cli_env(
        gateway_url=gateway_url,
        cred=cred,
        task_prompt=prompt,
        model=model,
        workspace=workspace,
        events_ingest_url=task.get("events_ingest_url"),
        max_iters=task.get("max_iters"),
        skillpacks_root=task.get("skillpacks_root", "/opt/skillpacks"),
        system_prompt_extra=task.get("system_prompt_extra"),
        extra=extra,
    )
    return {
        "cmd": ["python3", "-m", module],
        "env": env_vars,
        "cwd": workspace,
        "image": image,
    }


def cli_translate_events(
    lines: Iterable[str], translate_obj: Callable[[dict[str, Any]], list[dict[str, Any]]]
) -> list[dict[str, Any]]:
    """``translate_events(lines)`` for CLI adapters.

    Folds native JSONL records (via ``translate_obj``) and the shim's
    ``HEG_RESULT`` marker lines into normalized event dicts.
    """
    events: list[dict[str, Any]] = []
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        if line.startswith(RESULT_MARKER):
            events.extend(translate_heg_marker([line]))
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            events.extend(translate_obj(obj))
    return events


def cli_parse_terminal_state(logs: str) -> str | None:
    """``parse_terminal_state(logs)`` for CLI adapters — HEG_RESULT scan."""
    return parse_heg_result(logs)


def cli_completion_signal() -> dict[str, Any]:
    """``completion_signal()`` — identical mechanism for every shim'd CLI."""
    return {
        "mechanism": "stdout_marker",
        "result_marker": RESULT_MARKER,
        "done_event_type": "done_signal",
        "terminal_states": list(TERMINAL_STATES),
        "exit_codes": {
            "agent_done": 0,
            "harness_complete": 0,
            "stuck_loop": 0,
            "error": 2,
        },
        "notes": (
            "The adapter shim emits HEG_RESULT {json} last on stdout and "
            "POSTs a terminal_state event to the gateway; the `done` binary "
            "on PATH lets the agent self-report (agent_done). "
            "timeout/budget_exceeded are enforced by the orchestrator/gateway."
        ),
    }
