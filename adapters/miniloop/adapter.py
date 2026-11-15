"""Harness adapter contract for miniloop — plain functions, no framework.

Implements the contract from AGENTS.md::

    image_spec()          -> dict describing the sandbox image
    env(...)              -> env vars wiring the harness to the gateway
    launch(task)          -> would-be subprocess spec (container launch is
                             the sandbox runtime's job)
    translate_events(...) -> normalized event dicts from agent stdout
    completion_signal()   -> how terminal state is detected

The agent itself is the ``python -m miniloop`` program in this package; the
sandbox runtime (Lane C) is responsible for actually spawning the container,
mounting ``skillpacks/`` read-only at ``/opt/skillpacks``, and tailing stdout.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]

# Where the sandbox image definition will live (owned by the sandbox lane).
DOCKERFILE_REPO_PATH = "sandbox/images/miniloop.Dockerfile"

# Terminal states miniloop can emit on its MINILOOP_RESULT line.
TERMINAL_STATES = ("agent_done", "harness_complete", "stuck_loop", "error")

#: Registry manifest for GET /api/harnesses (Phase 2.5). Miniloop keeps the
#: configured SANDBOX_IMAGE rather than declaring its own tag, so ``image``
#: stays absent here — adapter_image() falls back for it.
MANIFEST = {
    "name": "miniloop",
    "display_name": "Miniloop (built-in)",
    "description": (
        "Custom minimal tool-call loop (shell, file read/write, done) running "
        "on the configured SANDBOX_IMAGE; reference harness + evaluator image"
    ),
    "llm_protocol": "openai-chat-completions",
    "cli": "python -m miniloop",
    "requires": ["gateway:/v1/chat/completions"],
}


def image_spec() -> dict[str, Any]:
    """Describe the sandbox image for this harness.

    Returns::

        {
          "name": "miniloop",
          "dockerfile": "sandbox/images/miniloop.Dockerfile",  # repo-relative
          "context": "<repo root>",                              # build context
          "workdir": "/workspace",
          "skillpacks_mount": {"host": "./skillpacks", "container": "/opt/skillpacks", "mode": "ro"},
        }
    """
    return {
        "name": "miniloop",
        "dockerfile": DOCKERFILE_REPO_PATH,
        "dockerfile_path": str(REPO_ROOT / DOCKERFILE_REPO_PATH),
        "context": str(REPO_ROOT),
        "workdir": "/workspace",
        "skillpacks_mount": {
            "host": str(REPO_ROOT / "skillpacks"),
            "container": "/opt/skillpacks",
            "mode": "ro",
        },
        "entrypoint": ["python", "-m", "miniloop"],
    }


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
    max_tokens: int | None = None,
    skillpacks_root: str = "/opt/skillpacks",
    system_prompt_extra: str | None = None,
) -> dict[str, str]:
    """Environment variables the miniloop agent reads (see agent.AgentConfig)."""
    out = {
        "GATEWAY_BASE_URL": gateway_url,
        "GATEWAY_CRED": cred,
        "MODEL_NAME": model,
        "TASK_PROMPT": task_prompt,
        "WORKSPACE": workspace,
        "SKILLPACKS_ROOT": skillpacks_root,
        # Matches ENV PYTHONPATH in sandbox/images/miniloop.Dockerfile, which
        # COPYs the repo's adapters/ tree to /opt/miniloop/adapters/ — setting
        # it here too keeps `python -m miniloop` working if the image ENV is
        # ever bypassed. Covers both `miniloop` and `adapters.miniloop` forms.
        "PYTHONPATH": "/opt/miniloop:/opt/miniloop/adapters",
    }
    if events_ingest_url:
        out["EVENTS_INGEST_URL"] = events_ingest_url
    if ingest_token:
        out["INGEST_TOKEN"] = ingest_token
    if max_iters is not None:
        out["MAX_ITERS"] = str(max_iters)
    if max_tokens is not None:
        # Per-request output cap — thinking models burn completion tokens on
        # <think> blocks, so judges/rubric callers raise it past the 4096
        # default to keep the JSON payload from truncating.
        out["MAX_TOKENS"] = str(max_tokens)
    if system_prompt_extra:
        out["SYSTEM_PROMPT_EXTRA"] = system_prompt_extra
    return out


def launch(task: dict[str, Any]) -> dict[str, Any]:
    """Return the would-be subprocess spec for one sandbox.

    ``task`` keys (all optional except prompt/model):
      - ``task_prompt`` or ``prompt``: the task text
      - ``model``: model name to request via the gateway
      - ``workspace``: container working dir (default ``/workspace``)
      - ``gateway_url``, ``cred``: default to env GATEWAY_BASE_URL/GATEWAY_CRED
      - ``events_ingest_url``, ``ingest_token``, ``max_iters``, ``max_tokens``,
        ``skillpacks_root``
      - ``system_prompt_extra``: appended to the agent's system prompt

    Returns::

        {"cmd": ["python", "-m", "miniloop"], "env": {...}, "cwd": workspace}

    Actual container creation is the sandbox runtime's responsibility.
    """
    import os

    prompt = task.get("task_prompt") or task.get("prompt") or ""
    model = task.get("model") or os.environ.get("MODEL_NAME", "")
    gateway_url = task.get("gateway_url") or os.environ.get("GATEWAY_BASE_URL", "")
    cred = task.get("cred") or os.environ.get("GATEWAY_CRED", "")
    workspace = task.get("workspace", "/workspace")

    env_vars = env(
        gateway_url=gateway_url,
        cred=cred,
        task_prompt=prompt,
        model=model,
        workspace=workspace,
        events_ingest_url=task.get("events_ingest_url"),
        ingest_token=task.get("ingest_token"),
        max_iters=task.get("max_iters"),
        max_tokens=task.get("max_tokens"),
        skillpacks_root=task.get("skillpacks_root", "/opt/skillpacks"),
        system_prompt_extra=task.get("system_prompt_extra"),
    )
    return {
        "cmd": ["python", "-m", "miniloop"],
        "env": env_vars,
        "cwd": workspace,
        "image": image_spec()["name"],
    }


def translate_events(stdout_lines: Iterable[str]) -> list[dict[str, Any]]:
    """Parse miniloop stdout into normalized event dicts.

    Each stdout line is either:
      - a harness event JSONL object ``{ts, seq, source, type, payload}`` →
        yielded as ``{"type", "payload", "ts", "source", "seq"}``
      - the final ``MINILOOP_RESULT {...}`` line → yielded as
        ``{"type": "miniloop_result", "payload": {...}}``
      - anything else (warnings, noise) → skipped
    """
    events: list[dict[str, Any]] = []
    for raw in stdout_lines:
        line = raw.strip()
        if not line:
            continue
        if line.startswith("MINILOOP_RESULT "):
            try:
                payload = json.loads(line[len("MINILOOP_RESULT ") :])
            except json.JSONDecodeError:
                payload = {"raw": line}
            events.append({"type": "miniloop_result", "payload": payload})
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict) or "type" not in obj:
            continue
        events.append(
            {
                "type": obj.get("type"),
                "payload": obj.get("payload"),
                "ts": obj.get("ts"),
                "seq": obj.get("seq"),
                "source": obj.get("source", "harness"),
            }
        )
    return events


def parse_terminal_state(logs: str) -> str | None:
    """Extract the agent's terminal state from captured container logs.

    Scans for ``MINILOOP_RESULT {json}`` marker lines (reusing
    :func:`translate_events`) and returns the ``terminal_state`` of the LAST
    one found — ``agent_done`` | ``harness_complete`` | ``stuck_loop`` |
    ``error`` — or ``None`` when no well-formed result line exists (crash
    before the agent could report, truncated logs, non-miniloop image).
    """
    terminal_state: str | None = None
    for event in translate_events(logs.splitlines()):
        if event.get("type") != "miniloop_result":
            continue
        payload = event.get("payload")
        if isinstance(payload, dict):
            state = payload.get("terminal_state")
            if isinstance(state, str) and state:
                terminal_state = state
    return terminal_state


def completion_signal() -> dict[str, Any]:
    """How the runtime detects that a miniloop sandbox reached a terminal state.

    Primary signal: the ``MINILOOP_RESULT {json}`` line on stdout (always the
    last line). The ``done`` tool produces a ``done_signal`` harness event
    earlier, and a clean exit (code 0) corroborates ``agent_done`` /
    ``harness_complete`` / ``stuck_loop``; exit code 2 means ``error``.
    """
    return {
        "mechanism": "stdout_marker",
        "result_marker": "MINILOOP_RESULT ",
        "done_event_type": "done_signal",
        "terminal_states": list(TERMINAL_STATES),
        "exit_codes": {
            "agent_done": 0,
            "harness_complete": 0,
            "stuck_loop": 0,
            "error": 2,
        },
        "notes": (
            "Parse the last MINILOOP_RESULT line for terminal_state; "
            "timeout/budget_exceeded are enforced by the orchestrator/gateway, "
            "not by miniloop."
        ),
    }
