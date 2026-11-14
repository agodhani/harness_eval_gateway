"""Claude Code first-run state — seeds ``~/.claude.json``.

Headless ``claude -p`` blocks on interactive onboarding (theme pick,
trust dialog, API-key acknowledgement) unless the CLI's state file shows
onboarding already done. The shim writes a minimal ``~/.claude.json``
before spawning — merged over any existing file so a reused HOME keeps
its history.

Stdlib-only (runs inside the sandbox).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from adapters.common.runner import ShimConfig

#: State keys claude checks before starting a headless run.
_ONBOARDING_STATE: dict[str, Any] = {
    "hasCompletedOnboarding": True,
    "hasCompletedProjectOnboarding": True,
    "hasTrustDialogAccepted": True,
    # Never show the "run without project trust" / disclaimer dialogs.
    "bypassPermissionsModeAccepted": True,
    "theme": "dark",
    "autoUpdates": False,
}

#: Command hooks registered in ~/.claude/settings.json (PR17). The receiver
#: is the shared ``python3 -m adapters.common.hooks claude_code`` module —
#: claude pipes one JSON payload per firing on stdin. ``matcher: ""`` is the
#: documented match-all. Only events adding fidelity beyond the stream-json
#: translation are registered: ``PostToolUse`` yields ``tool_result``
#: outcomes (the stream's tool_result records are deliberately skipped), and
#: the lifecycle/notification events land as checkpoints. ``PreToolUse`` and
#: ``UserPromptSubmit`` would duplicate records the stream already produces
#: (and their hook stdout feeds back into the agent's context) — not wired.
HOOK_COMMAND = "python3 -m adapters.common.hooks claude_code"
HOOK_EVENTS = (
    "PostToolUse",
    "Notification",
    "Stop",
    "SubagentStop",
    "PreCompact",
    "SessionStart",
    "SessionEnd",
)


def _hooks() -> dict[str, Any]:
    return {
        event: [
            {"matcher": "", "hooks": [{"type": "command", "command": HOOK_COMMAND}]}
        ]
        for event in HOOK_EVENTS
    }


#: ~/.claude/settings.json — bypass permission prompts in print mode.
_SETTINGS: dict[str, Any] = {
    "permissions": {"defaultMode": "bypassPermissions"},
    "hooks": _hooks(),
}


def claude_json_path(cfg: ShimConfig) -> Path:
    return Path(cfg.home) / ".claude.json"


def claude_settings_path(cfg: ShimConfig) -> Path:
    return Path(cfg.home) / ".claude" / "settings.json"


def _merge_json(path: Path, updates: dict[str, Any]) -> None:
    existing: dict[str, Any] = {}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                existing = loaded
        except (OSError, json.JSONDecodeError):
            existing = {}
    existing.update(updates)
    path.write_text(json.dumps(existing, indent=2), encoding="utf-8")


def write_config(cfg: ShimConfig) -> list[Path]:
    """Seed ``~/.claude.json`` + ``~/.claude/settings.json``; returns paths."""
    json_path = claude_json_path(cfg)
    settings_path = claude_settings_path(cfg)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    _merge_json(json_path, _ONBOARDING_STATE)
    _merge_json(settings_path, _SETTINGS)
    return [json_path, settings_path]
