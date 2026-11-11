"""Tool implementations for miniloop.

Each tool is a plain function ``(ctx, args) -> str`` returning the string that
becomes the ``role:"tool"`` message content. Results are compact,
machine-readable JSON text. Tools never raise — failures are returned as
``{"error": ...}`` payloads so a misbehaving tool can't kill the agent loop.

Safety rules:
- All file ops are confined to ``ctx.workspace`` (resolve + prefix check;
  ``..`` escapes and symlink escapes are rejected).
- ``shell`` runs ``bash -c`` with ``cwd=workspace`` and a hard timeout cap.
"""

from __future__ import annotations

import json
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .events import EventEmitter
from .skills import SkillCatalog

MAX_SHELL_TIMEOUT = 120.0
DEFAULT_SHELL_TIMEOUT = 30.0
# Output budget for shell results: ~8KB total (6KB head + 2KB tail on stdout,
# a smaller slice on stderr), marked with "truncated": true when clipped.
STDOUT_HEAD = 6144
STDOUT_TAIL = 2048
STDERR_HEAD = 1536
STDERR_TAIL = 512
READ_DEFAULT_LIMIT = 200
READ_MAX_LIMIT = 500
SKILL_CONTENT_MAX = 16 * 1024  # ~16KB


@dataclass
class ToolContext:
    """Everything a tool needs at dispatch time."""

    workspace: Path
    emitter: EventEmitter
    skills: SkillCatalog


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _ok(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _err(message: str, **extra: Any) -> str:
    payload: dict[str, Any] = {"error": message}
    payload.update(extra)
    return _ok(payload)


def _resolve_in_workspace(workspace: Path, path_str: str) -> Path:
    """Resolve ``path_str`` inside ``workspace``; raise ValueError on escape."""
    ws = workspace.resolve()
    p = Path(path_str).expanduser()
    if not p.is_absolute():
        p = ws / p
    resolved = p.resolve()
    if resolved != ws and ws not in resolved.parents:
        raise ValueError(f"path escapes workspace: {path_str!r}")
    return resolved


def _head_tail(text: str, head: int, tail: int) -> tuple[str, bool]:
    """Keep head+tail of text when it exceeds head+tail bytes/chars."""
    if len(text) <= head + tail:
        return text, False
    return text[:head] + f"\n...[truncated {len(text) - head - tail} chars]...\n" + text[-tail:], True


# ---------------------------------------------------------------------------
# tools
# ---------------------------------------------------------------------------


def tool_shell(ctx: ToolContext, args: dict[str, Any]) -> str:
    command = str(args.get("command", ""))
    if not command.strip():
        return _err("shell: 'command' is required")
    try:
        timeout = float(args.get("timeout_seconds", DEFAULT_SHELL_TIMEOUT))
    except (TypeError, ValueError):
        timeout = DEFAULT_SHELL_TIMEOUT
    timeout = max(1.0, min(timeout, MAX_SHELL_TIMEOUT))

    start = time.monotonic()
    timed_out = False
    try:
        proc = subprocess.run(  # bash -c by design; this IS the shell tool
            ["bash", "-c", command],
            cwd=str(ctx.workspace),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        exit_code = proc.returncode
        stdout, stderr = proc.stdout or "", proc.stderr or ""
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        exit_code = -1
        stdout = exc.stdout if isinstance(exc.stdout, str) else (exc.stdout or b"").decode("utf-8", "replace")
        stderr = exc.stderr if isinstance(exc.stderr, str) else (exc.stderr or b"").decode("utf-8", "replace")
        stderr = (stderr or "") + f"\n[miniloop] command timed out after {timeout:.0f}s"
    except Exception as exc:  # noqa: BLE001
        duration_ms = int((time.monotonic() - start) * 1000)
        ctx.emitter.emit(
            "shell_command",
            {"command": command, "exit_code": -1, "duration_ms": duration_ms, "out_bytes": 0},
        )
        return _err(f"shell failed to start: {exc}")

    duration_ms = int((time.monotonic() - start) * 1000)
    out_bytes = len(stdout.encode("utf-8", "replace")) + len(stderr.encode("utf-8", "replace"))
    ctx.emitter.emit(
        "shell_command",
        {
            "command": command,
            "exit_code": exit_code,
            "duration_ms": duration_ms,
            "out_bytes": out_bytes,
            "timed_out": timed_out,
        },
    )

    stdout_t, t1 = _head_tail(stdout, STDOUT_HEAD, STDOUT_TAIL)
    stderr_t, t2 = _head_tail(stderr, STDERR_HEAD, STDERR_TAIL)
    return _ok(
        {
            "exit_code": exit_code,
            "timed_out": timed_out,
            "duration_ms": duration_ms,
            "stdout": stdout_t,
            "stderr": stderr_t,
            "truncated": t1 or t2,
        }
    )


def tool_read_file(ctx: ToolContext, args: dict[str, Any]) -> str:
    path_str = str(args.get("path", ""))
    if not path_str:
        return _err("read_file: 'path' is required")
    try:
        offset = max(1, int(args.get("offset", 1)))
        limit = max(1, min(int(args.get("limit", READ_DEFAULT_LIMIT)), READ_MAX_LIMIT))
    except (TypeError, ValueError):
        offset, limit = 1, READ_DEFAULT_LIMIT

    try:
        path = _resolve_in_workspace(ctx.workspace, path_str)
    except ValueError as exc:
        return _err(str(exc))
    if not path.is_file():
        return _err(f"read_file: not a file: {path_str}")

    lines: list[str] = []
    total = 0
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for i, line in enumerate(fh, start=1):
                total = i
                if offset <= i < offset + limit:
                    lines.append(line.rstrip("\n"))
    except OSError as exc:
        return _err(f"read_file failed: {exc}")

    size = path.stat().st_size
    ctx.emitter.emit(
        "file_op",
        {"op": "read", "path": str(path), "bytes": size, "offset": offset, "limit": limit},
    )
    return _ok(
        {
            "path": str(path),
            "offset": offset,
            "limit": limit,
            "total_lines": total,
            "content": "\n".join(lines),
            "eof": offset + len(lines) - 1 >= total,
        }
    )


def tool_write_file(ctx: ToolContext, args: dict[str, Any]) -> str:
    path_str = str(args.get("path", ""))
    if not path_str:
        return _err("write_file: 'path' is required")
    content = args.get("content")
    if not isinstance(content, str):
        return _err("write_file: 'content' must be a string")
    try:
        path = _resolve_in_workspace(ctx.workspace, path_str)
    except ValueError as exc:
        return _err(str(exc))
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        data = content.encode("utf-8")
        path.write_bytes(data)
    except OSError as exc:
        return _err(f"write_file failed: {exc}")

    ctx.emitter.emit("file_op", {"op": "write", "path": str(path), "bytes": len(data)})
    return _ok({"path": str(path), "bytes_written": len(data)})


def tool_use_skill(ctx: ToolContext, args: dict[str, Any]) -> str:
    name = str(args.get("name", "")).strip()
    if not name:
        return _err("use_skill: 'name' is required")
    entry = ctx.skills.resolve(name)
    if entry is None:
        available = [e.catalog_name for e in ctx.skills.entries][:50]
        return _err(
            f"use_skill: unknown skill {name!r}",
            available_skills=available,
        )
    try:
        content = entry.path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return _err(f"use_skill: cannot read {entry.path}: {exc}")

    truncated = len(content) > SKILL_CONTENT_MAX
    if truncated:
        content = content[:SKILL_CONTENT_MAX] + "\n...[skill truncated]..."

    # Note sibling files (references/, scripts/, assets/) so the agent knows
    # what else exists in the skill directory.
    siblings = _list_skill_dir(entry)
    if siblings:
        content += "\n\n## Skill directory contents\n" + "\n".join(siblings)

    ctx.emitter.emit(
        "skill_invocation",
        {"name": entry.catalog_name, "path": entry.rel_path, "bytes": len(content)},
    )
    return _ok(
        {
            "skill": entry.catalog_name,
            "pack": entry.pack,
            "path": entry.rel_path,
            "truncated": truncated,
            "instructions": content,
        }
    )


def _list_skill_dir(entry: Any, max_entries: int = 60) -> list[str]:
    """Relative paths of files in the skill dir, excluding SKILL.md itself."""
    try:
        files = sorted(
            p for p in entry.skill_dir.rglob("*") if p.is_file() and p.name != "SKILL.md"
        )
    except OSError:
        return []
    rel = [str(p.relative_to(entry.skill_dir)) for p in files[:max_entries]]
    if len(files) > max_entries:
        rel.append(f"... and {len(files) - max_entries} more")
    return rel


def tool_done(ctx: ToolContext, args: dict[str, Any]) -> str:
    status = str(args.get("status", "success"))
    if status not in ("success", "blocked"):
        status = "success"
    summary = str(args.get("summary", ""))[:2000]
    ctx.emitter.emit("done_signal", {"status": status, "summary": summary})
    return _ok({"ok": True, "status": status})


TOOL_IMPLS: dict[str, Callable[[ToolContext, dict[str, Any]], str]] = {
    "shell": tool_shell,
    "read_file": tool_read_file,
    "write_file": tool_write_file,
    "use_skill": tool_use_skill,
    "done": tool_done,
}
