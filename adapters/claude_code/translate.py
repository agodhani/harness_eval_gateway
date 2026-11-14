"""claude ``--output-format stream-json`` record → normalized event dicts.

Claude Code's stream-json emits one JSON object per line::

    {"type":"system","subtype":"init","session_id":...,"tools":[...]}
    {"type":"assistant","message":{"content":[
        {"type":"text","text":"..."},
        {"type":"tool_use","id":"...","name":"Bash","input":{"command":"ls"}}
    ]}}
    {"type":"user","message":{"content":[
        {"type":"tool_result","tool_use_id":"...","is_error":false,...}
    ]}}
    {"type":"result","subtype":"success","is_error":false,"result":"...",
     "num_turns":5,"duration_ms":1234,"total_cost_usd":0.0,"usage":{...}}

Runs inside the sandbox — stdlib only.
"""

from __future__ import annotations

from typing import Any

from adapters.common.runner import MAX_SNIPPET

#: Claude Code tool names that run a shell — mapped to ``shell_command``.
_SHELL_TOOLS = {"Bash", "KillShell", "BashOutput"}
#: Tool names that operate on files — mapped to ``file_op``.
_FILE_TOOLS = {
    "Read": "read",
    "Write": "write",
    "Edit": "edit",
    "MultiEdit": "edit",
    "NotebookEdit": "edit",
    "Glob": "glob",
    "Grep": "grep",
    "LS": "ls",
}

_TEXT_LIMIT = MAX_SNIPPET


def _truncate(value: Any, limit: int = _TEXT_LIMIT) -> str:
    text = value if isinstance(value, str) else str(value)
    return text if len(text) <= limit else text[:limit] + "…[truncated]"


def _checkpoint(stage: str, **fields: Any) -> dict[str, Any]:
    return {"type": "checkpoint", "payload": {"stage": stage, **fields}}


def _tool_use_event(block: dict[str, Any]) -> dict[str, Any] | None:
    name = block.get("name")
    tool_input = block.get("input") if isinstance(block.get("input"), dict) else {}
    if not isinstance(name, str) or not name:
        return None
    if name in _SHELL_TOOLS:
        return {
            "type": "shell_command",
            "payload": {
                "command": _truncate(tool_input.get("command"), 2000),
                "tool": name,
            },
        }
    if name in _FILE_TOOLS:
        return {
            "type": "file_op",
            "payload": {
                "op": _FILE_TOOLS[name],
                "tool": name,
                "path": tool_input.get("file_path")
                or tool_input.get("notebook_path")
                or tool_input.get("pattern")
                or tool_input.get("path"),
            },
        }
    return {
        "type": "tool_call",
        "payload": {
            "name": name,
            "input": tool_input if tool_input else block.get("input"),
        },
    }


def _assistant_events(obj: dict[str, Any]) -> list[dict[str, Any]]:
    message = obj.get("message")
    if not isinstance(message, dict):
        return []
    content = message.get("content")
    if not isinstance(content, list):
        return []
    events: list[dict[str, Any]] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "tool_use":
            ev = _tool_use_event(block)
            if ev is not None:
                events.append(ev)
        elif btype == "text":
            text = block.get("text")
            if isinstance(text, str) and text.strip():
                events.append(
                    _checkpoint("assistant_message", text=_truncate(text, 2000))
                )
    return events


def _result_events(obj: dict[str, Any]) -> list[dict[str, Any]]:
    is_error = bool(obj.get("is_error"))
    state = "error" if is_error else "harness_complete"
    payload: dict[str, Any] = {
        "terminal_state": state,
        "subtype": obj.get("subtype"),
        "num_turns": obj.get("num_turns"),
        "duration_ms": obj.get("duration_ms"),
    }
    if obj.get("usage") is not None:
        payload["usage"] = obj.get("usage")
    if obj.get("total_cost_usd") is not None:
        payload["total_cost_usd"] = obj.get("total_cost_usd")
    result_text = obj.get("result")
    if isinstance(result_text, str) and result_text.strip():
        payload["result"] = _truncate(result_text, 2000)
    if is_error:
        events = [
            {
                "type": "error",
                "payload": {
                    "stage": "claude_result",
                    "message": _truncate(result_text or obj.get("subtype"), 2000),
                },
            }
        ]
    else:
        events = []
    events.append({"type": "terminal_state", "payload": payload})
    return events


def translate_obj(obj: dict[str, Any]) -> list[dict[str, Any]]:
    """One claude stream-json record → normalized event dicts."""
    rec_type = obj.get("type")
    if rec_type == "system":
        subtype = obj.get("subtype")
        if subtype == "init":
            tools = obj.get("tools")
            return [
                _checkpoint(
                    "session_init",
                    session_id=obj.get("session_id"),
                    model=obj.get("model"),
                    tool_count=len(tools) if isinstance(tools, list) else None,
                )
            ]
        if subtype == "compact_boundary":
            return [
                {
                    "type": "context_compacted",
                    "payload": {
                        "trigger": obj.get("compact_metadata", {}).get("trigger")
                        if isinstance(obj.get("compact_metadata"), dict)
                        else None
                    },
                }
            ]
        return []
    if rec_type == "assistant":
        return _assistant_events(obj)
    if rec_type == "user":
        # tool_result records carry outputs, not calls — the matching
        # tool_use already produced an event; results would double-count.
        return []
    if rec_type == "result":
        return _result_events(obj)
    return []


def translate_events(lines: Any) -> list[dict[str, Any]]:
    """Convenience wrapper used by tests — adapter.py routes through
    ``contract.cli_translate_events`` which also folds HEG_RESULT markers."""
    from adapters.common.runner import translate_lines

    return translate_lines(lines, translate_obj)


# ---------------------------------------------------------------------------
# settings.json hooks (PR17) — stdin JSON payloads via adapters.common.hooks
# ---------------------------------------------------------------------------

#: Dict ``tool_response`` keys probed for an exit-code-ish field, in order.
_RESPONSE_EXIT_KEYS = ("returnCode", "exitCode", "exit_code", "code")
#: Dict ``tool_response`` keys probed for an error flag, in order.
_RESPONSE_ERROR_KEYS = ("is_error", "isError")


def _post_tool_use(obj: dict[str, Any]) -> dict[str, Any]:
    """PostToolUse payload → ``tool_result`` event.

    The stream translation drops ``user``/``tool_result`` records to avoid
    double-counting the ``tool_use`` events — the hook is the ONLY place tool
    outcomes (response body, exit code, duration) reach the timeline.
    ``tool_response`` shape varies by tool: a dict (e.g. Bash's
    ``{stdout, stderr, returnCode, interrupted}``) or a plain string.
    """
    response = obj.get("tool_response")
    payload: dict[str, Any] = {
        "name": obj.get("tool_name"),
        "tool_use_id": obj.get("tool_use_id"),
        "session_id": obj.get("session_id"),
        "duration_ms": obj.get("duration_ms"),
        "output": _truncate(response),
    }
    is_error: Any = obj.get("is_error")
    if isinstance(response, dict):
        for key in _RESPONSE_EXIT_KEYS:
            if response.get(key) is not None:
                payload["exit_code"] = response[key]
                break
        for key in _RESPONSE_ERROR_KEYS:
            if response.get(key) is not None:
                is_error = response[key]
                break
    if is_error is not None:
        payload["is_error"] = bool(is_error)
    return {
        "type": "tool_result",
        "payload": {k: v for k, v in payload.items() if v is not None},
    }


def translate_hook(obj: dict[str, Any]) -> list[dict[str, Any]]:
    """One claude ``settings.json`` hook stdin payload → normalized events.

    Only events registered in ``config.HOOK_EVENTS`` produce output — the
    receiver tolerates (and drops) anything else so a stale hook config can't
    feed noise into the event stream.
    """
    hook = obj.get("hook_event_name")
    session_id = obj.get("session_id")
    if hook == "PostToolUse":
        return [_post_tool_use(obj)]
    if hook == "SessionStart":
        return [
            _checkpoint(
                "session_start",
                session_id=session_id,
                source=obj.get("source"),
                model=obj.get("model"),
            )
        ]
    if hook == "SessionEnd":
        return [
            _checkpoint(
                "session_end", session_id=session_id, reason=obj.get("reason")
            )
        ]
    if hook == "Stop":
        return [
            _checkpoint(
                "stop",
                session_id=session_id,
                stop_hook_active=obj.get("stop_hook_active"),
            )
        ]
    if hook == "SubagentStop":
        return [
            _checkpoint(
                "subagent_stop",
                session_id=session_id,
                agent_id=obj.get("agent_id"),
                agent_type=obj.get("agent_type"),
                stop_hook_active=obj.get("stop_hook_active"),
            )
        ]
    if hook == "Notification":
        return [
            _checkpoint(
                "notification",
                session_id=session_id,
                title=obj.get("title"),
                message=_truncate(obj.get("message"), 2000),
            )
        ]
    if hook == "PreCompact":
        return [
            _checkpoint(
                "pre_compact",
                session_id=session_id,
                trigger=obj.get("trigger"),
            )
        ]
    # PreToolUse / UserPromptSubmit are deliberately unregistered —
    # translated stream records already cover them (see config.py).
    return []
