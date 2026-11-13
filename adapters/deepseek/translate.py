"""``dsh --profile headless --json`` record → normalized event dicts.

Per packages/bundle/headless/README.md, the NDJSON stream opens with
``session`` and closes with ``final``, carrying ``status``, ``text``,
``thinking``, ``tool_call``, ``tool_result``, and ``error`` events::

    {"type":"session","sessionId":"session-..."}
    {"type":"tool_call","name":"bash","arguments":{...}}       # shape may vary
    {"type":"tool_result","error":{"name":"...","code":"..."}} # error optional
    {"type":"text","text":"..."}
    {"type":"final","sessionId":"...","reason":{"kind":"completed"},"answer":"..."}
    {"type":"error","message":"..."}   # no `final` follows a runner error

Field names beyond ``type`` are best-effort — the translator tolerates
``id``/``sessionId``, ``input``/``arguments``/``args``, and
``reason.kind``/``reason`` string variants.

Runs inside the sandbox — stdlib only.
"""

from __future__ import annotations

from typing import Any

from adapters.common.runner import MAX_SNIPPET

#: dsh tool names (best-effort) that run a shell / operate on files.
_SHELL_NAMES = {"bash", "shell", "sh", "run", "exec", "command"}
_FILE_NAMES = {
    "read": "read",
    "read_file": "read",
    "write": "write",
    "write_file": "write",
    "edit": "edit",
    "edit_file": "edit",
    "glob": "glob",
    "grep": "grep",
    "find": "find",
    "ls": "ls",
    "list": "ls",
    "list_dir": "ls",
}


def _truncate(value: Any, limit: int = MAX_SNIPPET) -> str:
    text = value if isinstance(value, str) else str(value)
    return text if len(text) <= limit else text[:limit] + "…[truncated]"


def _checkpoint(stage: str, **fields: Any) -> dict[str, Any]:
    return {"type": "checkpoint", "payload": {"stage": stage, **fields}}


def _args_of(obj: dict[str, Any]) -> dict[str, Any]:
    for key in ("arguments", "args", "input", "params"):
        v = obj.get(key)
        if isinstance(v, dict):
            return v
    return {}


def _tool_call(obj: dict[str, Any]) -> list[dict[str, Any]]:
    name = obj.get("name") or obj.get("tool") or obj.get("toolName")
    args = _args_of(obj)
    if not isinstance(name, str) or not name:
        return [_checkpoint("tool_call", raw_type=obj.get("type"))]
    lname = name.lower()
    if lname in _SHELL_NAMES:
        return [
            {
                "type": "shell_command",
                "payload": {
                    "command": _truncate(
                        args.get("command") or args.get("cmd") or args.get("script"),
                        2000,
                    ),
                    "tool": name,
                },
            }
        ]
    if lname in _FILE_NAMES:
        return [
            {
                "type": "file_op",
                "payload": {
                    "op": _FILE_NAMES[lname],
                    "tool": name,
                    "path": args.get("path")
                    or args.get("file_path")
                    or args.get("pattern"),
                },
            }
        ]
    return [
        {
            "type": "tool_call",
            "payload": {"name": name, "args": args if args else None},
        }
    ]


def _final_events(obj: dict[str, Any]) -> list[dict[str, Any]]:
    reason = obj.get("reason")
    kind = reason.get("kind") if isinstance(reason, dict) else reason
    completed = kind in (None, "completed")  # null reason ⇒ last turn had no terminal event
    state = "harness_complete" if completed and kind == "completed" else "error"
    payload: dict[str, Any] = {
        "terminal_state": state,
        "reason": kind,
        "session_id": obj.get("sessionId") or obj.get("id"),
    }
    answer = obj.get("answer") or obj.get("text")
    if isinstance(answer, str) and answer.strip():
        payload["result"] = _truncate(answer, 2000)
    events: list[dict[str, Any]] = []
    if not completed:
        events.append(
            {
                "type": "error",
                "payload": {
                    "stage": "dsh_final",
                    "message": f"final reason {kind!r} != 'completed'",
                },
            }
        )
    events.append({"type": "terminal_state", "payload": payload})
    return events


def translate_obj(obj: dict[str, Any]) -> list[dict[str, Any]]:
    """One dsh headless --json record → normalized event dicts."""
    rec_type = obj.get("type")

    if rec_type == "session":
        return [
            _checkpoint(
                "session", session_id=obj.get("sessionId") or obj.get("id")
            )
        ]
    if rec_type == "status":
        return [_checkpoint("status", status=obj.get("status") or obj.get("state"))]
    if rec_type == "text":
        text = obj.get("text") or obj.get("content")
        return [
            _checkpoint("assistant_message", text=_truncate(text, 2000))
        ]
    if rec_type == "thinking":
        return []  # reasoning deltas — too noisy for the timeline
    if rec_type == "tool_call":
        return _tool_call(obj)
    if rec_type == "tool_result":
        error = obj.get("error")
        if isinstance(error, dict) and error:
            return [
                {
                    "type": "error",
                    "payload": {
                        "stage": "tool_result",
                        "message": _truncate(
                            error.get("message") or error.get("code") or error.get("name"),
                            2000,
                        ),
                    },
                }
            ]
        return [_checkpoint("tool_result")]
    if rec_type == "final":
        return _final_events(obj)
    if rec_type == "error":
        return [
            {
                "type": "error",
                "payload": {
                    "stage": "dsh",
                    "message": _truncate(obj.get("message") or obj.get("error"), 2000),
                },
            }
        ]
    return []


def translate_events(lines: Any) -> list[dict[str, Any]]:
    """Convenience wrapper used by tests — adapter.py routes through
    ``contract.cli_translate_events`` which also folds HEG_RESULT markers."""
    from adapters.common.runner import translate_lines

    return translate_lines(lines, translate_obj)
