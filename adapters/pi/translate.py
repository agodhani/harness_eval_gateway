"""pi ``--mode json`` record → normalized event dicts.

Pi's JSON mode emits session records (packages/coding-agent/docs/json.md)::

    {"type":"session","version":3,"id":"...","cwd":"..."}
    {"type":"agent_start"} / {"type":"turn_start"}
    {"type":"tool_execution_start","toolCallId":"call_1","toolName":"bash",
        "args":{"command":"ls"}}
    {"type":"tool_execution_end","toolCallId":"call_1","toolName":"bash",
        "result":{...},"isError":false,"durationMs":12}
    {"type":"message_end","message":{"role":"assistant","content":[...]}}
    {"type":"compaction_end","reason":"threshold","result":{"tokensBefore":N,
        "estimatedTokensAfter":M}}
    {"type":"agent_settled","aborted":false}

Runs inside the sandbox — stdlib only.
"""

from __future__ import annotations

from typing import Any

from adapters.common.runner import MAX_SNIPPET

#: pi built-in tool names that run a shell.
_SHELL_TOOLS = {"bash", "powershell"}
#: pi built-in tool names that operate on files.
_FILE_TOOLS = {
    "read": "read",
    "write": "write",
    "edit": "edit",
    "grep": "grep",
    "find": "find",
    "ls": "ls",
}


def _truncate(value: Any, limit: int = MAX_SNIPPET) -> str | None:
    if value is None:
        return None
    text = value if isinstance(value, str) else str(value)
    return text if len(text) <= limit else text[:limit] + "…[truncated]"


def _checkpoint(stage: str, **fields: Any) -> dict[str, Any]:
    return {"type": "checkpoint", "payload": {"stage": stage, **fields}}


def _assistant_text(message: dict[str, Any]) -> str | None:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            b.get("text")
            for b in content
            if isinstance(b, dict) and b.get("type") == "text" and b.get("text")
        ]
        if parts:
            return "\n".join(str(p) for p in parts)
    return None


def _tool_start(obj: dict[str, Any]) -> list[dict[str, Any]]:
    name = obj.get("toolName")
    args = obj.get("args") if isinstance(obj.get("args"), dict) else {}
    if not isinstance(name, str) or not name:
        return []
    if name in _SHELL_TOOLS:
        return [
            {
                "type": "shell_command",
                "payload": {
                    "command": _truncate(args.get("command"), 2000),
                    "tool": name,
                    "tool_call_id": obj.get("toolCallId"),
                },
            }
        ]
    if name in _FILE_TOOLS:
        return [
            {
                "type": "file_op",
                "payload": {
                    "op": _FILE_TOOLS[name],
                    "tool": name,
                    "path": args.get("path")
                    or args.get("pattern")
                    or args.get("file_path"),
                    "tool_call_id": obj.get("toolCallId"),
                },
            }
        ]
    return [
        {
            "type": "tool_call",
            "payload": {
                "name": name,
                "args": args if args else obj.get("args"),
                "tool_call_id": obj.get("toolCallId"),
            },
        }
    ]


def translate_obj(obj: dict[str, Any]) -> list[dict[str, Any]]:
    """One pi JSON-mode record → normalized event dicts."""
    rec_type = obj.get("type")

    if rec_type == "session":
        return [
            _checkpoint(
                "session", session_id=obj.get("id"), cwd=obj.get("cwd")
            )
        ]
    if rec_type in ("agent_start", "turn_start"):
        return [_checkpoint(rec_type)]
    if rec_type == "turn_end":
        return [_checkpoint("turn_end")]
    if rec_type == "agent_end":
        return [_checkpoint("agent_end", will_retry=obj.get("willRetry"))]
    if rec_type == "tool_execution_start":
        return _tool_start(obj)
    if rec_type == "tool_execution_update":
        return []  # partial results — the end record carries the outcome
    if rec_type == "tool_execution_end":
        if obj.get("isError"):
            return [
                {
                    "type": "error",
                    "payload": {
                        "stage": "tool_execution",
                        "tool": obj.get("toolName"),
                        "tool_call_id": obj.get("toolCallId"),
                        "message": _truncate(obj.get("result"), 2000),
                    },
                }
            ]
        return [
            _checkpoint(
                "tool_execution_end",
                tool=obj.get("toolName"),
                tool_call_id=obj.get("toolCallId"),
                duration_ms=obj.get("durationMs"),
            )
        ]
    if rec_type == "message_end":
        message = obj.get("message")
        if isinstance(message, dict) and message.get("role") == "assistant":
            text = _assistant_text(message)
            if text:
                return [
                    _checkpoint("assistant_message", text=_truncate(text, 2000))
                ]
        return []
    if rec_type == "message_update":
        return []  # streaming deltas — too fine-grained for the timeline
    if rec_type == "compaction_end":
        result = obj.get("result") if isinstance(obj.get("result"), dict) else {}
        return [
            {
                "type": "context_compacted",
                "payload": {
                    "reason": obj.get("reason"),
                    "aborted": obj.get("aborted"),
                    "tokens_before": result.get("tokensBefore"),
                    "tokens_after": result.get("estimatedTokensAfter"),
                },
            }
        ]
    if rec_type == "compaction_start":
        return [_checkpoint("compaction_start", reason=obj.get("reason"))]
    if rec_type in (
        "auto_retry_start",
        "auto_retry_end",
        "summarization_retry_scheduled",
        "summarization_retry_attempt_start",
        "summarization_retry_finished",
        "queue_update",
        "entry_appended",
        "session_info_changed",
        "thinking_level_changed",
    ):
        return [_checkpoint(rec_type)]
    if rec_type == "agent_settled":
        aborted = bool(obj.get("aborted"))
        return [
            {
                "type": "terminal_state",
                "payload": {
                    "terminal_state": "error" if aborted else "harness_complete",
                    "aborted": aborted,
                },
            }
        ]
    if rec_type in ("extension_error", "error"):
        return [
            {
                "type": "error",
                "payload": {
                    "stage": "pi",
                    "message": _truncate(obj.get("error") or obj.get("message"), 2000),
                },
            }
        ]
    return []


def translate_events(lines: Any) -> list[dict[str, Any]]:
    """Convenience wrapper used by tests — adapter.py routes through
    ``contract.cli_translate_events`` which also folds HEG_RESULT markers."""
    from adapters.common.runner import translate_lines

    return translate_lines(lines, translate_obj)


# ---------------------------------------------------------------------------
# Extension hook (PR17) — stdin JSON via adapters.common.hooks
# ---------------------------------------------------------------------------


def translate_hook(obj: dict[str, Any]) -> list[dict[str, Any]]:
    """One pi extension-forwarded payload → normalized events.

    Envelope (``config._EXTENSION_TS``):
    ``{"hook_event_name": <pi event name>, "event": <event object>}``.
    ``tool_execution_end`` → ``tool_result`` — the result payload the ``--mode
    json`` translation drops into a thin checkpoint to avoid double-counting
    the matching ``tool_execution_start`` event.
    """
    if obj.get("hook_event_name") != "tool_execution_end":
        return []
    event = obj.get("event")
    if not isinstance(event, dict):
        return []
    payload: dict[str, Any] = {
        "name": event.get("toolName") or event.get("name") or event.get("tool"),
        "tool_call_id": (
            event.get("toolCallId") or event.get("tool_call_id") or event.get("id")
        ),
        "is_error": bool(event.get("isError") or event.get("is_error")),
        "duration_ms": event.get("durationMs") or event.get("duration_ms"),
    }
    if event.get("result") is not None:
        payload["output"] = _truncate(event.get("result"))
    return [
        {
            "type": "tool_result",
            "payload": {k: v for k, v in payload.items() if v is not None},
        }
    ]
