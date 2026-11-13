"""``codex exec --json`` record → normalized event dicts.

Codex emits one JSON object per line (experimental JSON event stream)::

    {"type":"thread.started","thread_id":"..."}
    {"type":"turn.started"}
    {"type":"item.completed","item":{"type":"command_execution",
        "command":"bash -lc ls","aggregated_output":"...","exit_code":0,
        "status":"completed"}}
    {"type":"item.completed","item":{"type":"file_change","changes":[...]}}
    {"type":"item.completed","item":{"type":"agent_message","text":"..."}}
    {"type":"turn.completed","usage":{"input_tokens":...,
        "cached_input_tokens":...,"output_tokens":...}}
    {"type":"turn.failed","error":{"message":"..."}}

Runs inside the sandbox — stdlib only.
"""

from __future__ import annotations

from typing import Any

from adapters.common.runner import MAX_SNIPPET

_MAX_CHANGES = 40


def _truncate(value: Any, limit: int = MAX_SNIPPET) -> str:
    text = value if isinstance(value, str) else str(value)
    return text if len(text) <= limit else text[:limit] + "…[truncated]"


def _checkpoint(stage: str, **fields: Any) -> dict[str, Any]:
    return {"type": "checkpoint", "payload": {"stage": stage, **fields}}


def _item_completed(item: dict[str, Any]) -> list[dict[str, Any]]:
    item_type = item.get("type")
    if item_type == "command_execution":
        return [
            {
                "type": "shell_command",
                "payload": {
                    "command": _truncate(item.get("command"), 2000),
                    "exit_code": item.get("exit_code"),
                    "status": item.get("status"),
                    "output": _truncate(item.get("aggregated_output")),
                },
            }
        ]
    if item_type == "file_change":
        changes = item.get("changes")
        payload: dict[str, Any] = {"status": item.get("status")}
        if isinstance(changes, list):
            payload["changes"] = changes[:_MAX_CHANGES]
            paths = [
                str(c.get("path"))
                for c in changes[:_MAX_CHANGES]
                if isinstance(c, dict) and c.get("path")
            ]
            if paths:
                payload["paths"] = paths
        return [{"type": "file_op", "payload": payload}]
    if item_type == "mcp_tool_call":
        return [
            {
                "type": "tool_call",
                "payload": {
                    "name": item.get("tool") or item.get("server"),
                    "arguments": item.get("arguments"),
                    "status": item.get("status"),
                },
            }
        ]
    if item_type == "agent_message":
        return [
            _checkpoint("agent_message", text=_truncate(item.get("text")))
        ]
    if item_type == "web_search":
        return [
            {
                "type": "tool_call",
                "payload": {"name": "web_search", "query": item.get("query")},
            }
        ]
    if item_type == "todo_list":
        return [_checkpoint("todo_list", items=item.get("items"))]
    if item_type == "error":
        return [
            {
                "type": "error",
                "payload": {"stage": "codex_item", "message": _truncate(item.get("message"))},
            }
        ]
    # reasoning, collab_*, plan steps, and anything new upstream — ignored.
    return []


def translate_obj(obj: dict[str, Any]) -> list[dict[str, Any]]:
    """One codex JSONL record → normalized event dicts."""
    rec_type = obj.get("type")
    if rec_type == "thread.started":
        return [
            _checkpoint("thread_started", thread_id=obj.get("thread_id"))
        ]
    if rec_type == "turn.started":
        return [_checkpoint("turn_started")]
    if rec_type == "item.completed":
        item = obj.get("item")
        return _item_completed(item) if isinstance(item, dict) else []
    if rec_type == "item.started":
        # The completed record carries the same info plus results — emit on
        # completion only so analytics counts stay deterministic.
        return []
    if rec_type == "turn.completed":
        return [_checkpoint("turn_completed", usage=obj.get("usage"))]
    if rec_type == "turn.failed":
        error = obj.get("error") or {}
        return [
            {
                "type": "error",
                "payload": {
                    "stage": "turn_failed",
                    "message": _truncate(
                        error.get("message") if isinstance(error, dict) else error
                    ),
                },
            }
        ]
    if rec_type in ("error", "turn.timeout"):
        return [
            {
                "type": "error",
                "payload": {
                    "stage": "codex",
                    "message": _truncate(obj.get("message") or obj.get("error")),
                },
            }
        ]
    return []


def translate_events(lines: Any) -> list[dict[str, Any]]:
    """Convenience wrapper used by tests — adapter.py routes through
    ``contract.cli_translate_events`` which also folds HEG_RESULT markers."""
    from adapters.common.runner import translate_lines

    return translate_lines(lines, translate_obj)
