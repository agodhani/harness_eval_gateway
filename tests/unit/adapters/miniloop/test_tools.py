"""Hermetic tests for miniloop tools — no network, no Ollama."""

import io
import json
from pathlib import Path

import pytest

from adapters.miniloop.events import EventEmitter
from adapters.miniloop.skills import SkillCatalog
from adapters.miniloop.tools import (
    MAX_SHELL_TIMEOUT,
    ToolContext,
    _resolve_in_workspace,
    tool_done,
    tool_read_file,
    tool_shell,
    tool_use_skill,
    tool_write_file,
)


@pytest.fixture()
def ctx(tmp_path: Path) -> tuple[ToolContext, io.StringIO]:
    out = io.StringIO()
    emitter = EventEmitter(stdout=out)
    context = ToolContext(
        workspace=tmp_path, emitter=emitter, skills=SkillCatalog()
    )
    return context, out


def _result_json(raw: str) -> dict:
    return json.loads(raw)


def _events(out: io.StringIO) -> list[dict]:
    return [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# shell
# ---------------------------------------------------------------------------


def test_shell_basic(ctx) -> None:
    context, _ = ctx
    raw = tool_shell(context, {"command": "echo hello && exit 0"})
    result = _result_json(raw)
    assert result["exit_code"] == 0
    assert "hello" in result["stdout"]
    assert result["timed_out"] is False


def test_shell_nonzero_exit(ctx) -> None:
    context, _ = ctx
    result = _result_json(tool_shell(context, {"command": "exit 3"}))
    assert result["exit_code"] == 3


def test_shell_stderr_captured(ctx) -> None:
    context, _ = ctx
    result = _result_json(tool_shell(context, {"command": "echo oops >&2"}))
    assert "oops" in result["stderr"]


def test_shell_timeout(ctx) -> None:
    context, _ = ctx
    result = _result_json(
        tool_shell(context, {"command": "sleep 30", "timeout_seconds": 1})
    )
    assert result["timed_out"] is True
    assert result["exit_code"] == -1


def test_shell_timeout_capped(ctx) -> None:
    context, _ = ctx
    # absurd timeout is clamped to MAX_SHELL_TIMEOUT without erroring
    result = _result_json(
        tool_shell(context, {"command": "true", "timeout_seconds": 99999})
    )
    assert result["exit_code"] == 0
    assert MAX_SHELL_TIMEOUT == 120.0


def test_shell_output_truncated(ctx) -> None:
    context, _ = ctx
    result = _result_json(
        tool_shell(context, {"command": "python3 -c \"print('x' * 50000)\""})
    )
    assert result["truncated"] is True
    # ~8KB budget: head 6KB + tail 2KB + marker — far below 50KB
    assert len(result["stdout"]) < 12000
    assert "truncated" in result["stdout"]


def test_shell_requires_command(ctx) -> None:
    context, _ = ctx
    result = _result_json(tool_shell(context, {}))
    assert "error" in result


def test_shell_emits_event(ctx) -> None:
    context, out = ctx
    tool_shell(context, {"command": "echo hi"})
    events = _events(out)
    shell_events = [e for e in events if e["type"] == "shell_command"]
    assert len(shell_events) == 1
    payload = shell_events[0]["payload"]
    assert payload["command"] == "echo hi"
    assert payload["exit_code"] == 0
    assert "duration_ms" in payload
    assert "out_bytes" in payload
    assert shell_events[0]["source"] == "harness"


# ---------------------------------------------------------------------------
# read_file / write_file
# ---------------------------------------------------------------------------


def test_write_then_read(ctx) -> None:
    context, _ = ctx
    raw = tool_write_file(context, {"path": "sub/dir/file.txt", "content": "line1\nline2\n"})
    result = _result_json(raw)
    assert result["bytes_written"] == len("line1\nline2\n")
    assert (context.workspace / "sub/dir/file.txt").read_text() == "line1\nline2\n"

    raw = tool_read_file(context, {"path": "sub/dir/file.txt"})
    result = _result_json(raw)
    assert result["content"] == "line1\nline2"
    assert result["total_lines"] == 2
    assert result["eof"] is True


def test_read_file_offset_limit(ctx) -> None:
    context, _ = ctx
    tool_write_file(context, {"path": "nums.txt", "content": "\n".join(str(i) for i in range(10))})
    result = _result_json(tool_read_file(context, {"path": "nums.txt", "offset": 3, "limit": 2}))
    assert result["content"] == "2\n3"
    assert result["total_lines"] == 10
    assert result["eof"] is False


def test_read_file_limit_capped(ctx) -> None:
    context, _ = ctx
    tool_write_file(context, {"path": "big.txt", "content": "\n".join(["x"] * 600)})
    result = _result_json(tool_read_file(context, {"path": "big.txt", "limit": 9999}))
    # limit clamps at 500
    assert result["content"].count("\n") == 499


def test_read_file_missing(ctx) -> None:
    context, _ = ctx
    result = _result_json(tool_read_file(context, {"path": "nope.txt"}))
    assert "error" in result


def test_write_file_emits_file_op(ctx) -> None:
    context, out = ctx
    tool_write_file(context, {"path": "a.txt", "content": "abc"})
    tool_read_file(context, {"path": "a.txt"})
    ops = [e["payload"] for e in _events(out) if e["type"] == "file_op"]
    assert ops[0]["op"] == "write" and ops[0]["bytes"] == 3
    assert ops[1]["op"] == "read"


# ---------------------------------------------------------------------------
# path safety
# ---------------------------------------------------------------------------


def test_resolve_rejects_dotdot(ctx) -> None:
    context, _ = ctx
    for bad in ("../escape.txt", "sub/../../escape.txt", ".."):
        result = _result_json(tool_write_file(context, {"path": bad, "content": "x"}))
        assert "escapes workspace" in result["error"]
        result = _result_json(tool_read_file(context, {"path": bad}))
        assert "escapes workspace" in result["error"]


def test_resolve_rejects_absolute_outside(ctx) -> None:
    context, _ = ctx
    result = _result_json(tool_write_file(context, {"path": "/etc/evil.txt", "content": "x"}))
    assert "escapes workspace" in result["error"]


def test_resolve_allows_absolute_inside(ctx) -> None:
    context, _ = ctx
    inside = context.workspace / "abs.txt"
    result = _result_json(
        tool_write_file(context, {"path": str(inside), "content": "ok"})
    )
    assert "bytes_written" in result


def test_resolve_rejects_symlink_escape(ctx) -> None:
    context, _ = ctx
    outside = context.workspace.parent / "outside_secret.txt"
    outside.write_text("secret")
    link = context.workspace / "link.txt"
    link.symlink_to(outside)
    result = _result_json(tool_read_file(context, {"path": "link.txt"}))
    assert "escapes workspace" in result["error"]


def test_resolve_workspace_itself(ctx) -> None:
    context, _ = ctx
    resolved = _resolve_in_workspace(context.workspace, ".")
    assert resolved == context.workspace.resolve()


# ---------------------------------------------------------------------------
# done
# ---------------------------------------------------------------------------


def test_done_emits_signal(ctx) -> None:
    context, out = ctx
    raw = tool_done(context, {"summary": "finished", "status": "success"})
    assert _result_json(raw)["ok"] is True
    events = _events(out)
    done_events = [e for e in events if e["type"] == "done_signal"]
    assert len(done_events) == 1
    assert done_events[0]["payload"]["status"] == "success"
    assert done_events[0]["payload"]["summary"] == "finished"


def test_done_bad_status_clamped(ctx) -> None:
    context, out = ctx
    tool_done(context, {"summary": "x", "status": "bogus"})
    done_events = [e for e in _events(out) if e["type"] == "done_signal"]
    assert done_events[0]["payload"]["status"] == "success"


# ---------------------------------------------------------------------------
# use_skill
# ---------------------------------------------------------------------------


def _make_skill(root: Path, pack: str, name: str, body: str = "Do the thing.") -> None:
    skill_dir = root / pack / "skills" / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: desc for {name}\n---\n\n# {name}\n\n{body}\n"
    )


def test_use_skill_loads_instructions(ctx, tmp_path: Path) -> None:
    _make_skill(tmp_path / "packs", "demo", "cool-skill", "SECRET_BODY_TEXT")
    context, out = ctx
    context.skills = SkillCatalog.load(tmp_path / "packs")
    raw = tool_use_skill(context, {"name": "cool-skill"})
    result = _result_json(raw)
    assert "SECRET_BODY_TEXT" in result["instructions"]
    assert result["skill"] == "demo:cool-skill"
    assert result["pack"] == "demo"
    invocations = [e for e in _events(out) if e["type"] == "skill_invocation"]
    assert invocations[0]["payload"]["name"] == "demo:cool-skill"


def test_use_skill_pack_prefix(ctx, tmp_path: Path) -> None:
    _make_skill(tmp_path / "packs", "demo", "cool-skill")
    context, _ = ctx
    context.skills = SkillCatalog.load(tmp_path / "packs")
    result = _result_json(tool_use_skill(context, {"name": "demo:cool-skill"}))
    assert result["skill"] == "demo:cool-skill"


def test_use_skill_unknown_lists_available(ctx, tmp_path: Path) -> None:
    _make_skill(tmp_path / "packs", "demo", "cool-skill")
    context, _ = ctx
    context.skills = SkillCatalog.load(tmp_path / "packs")
    result = _result_json(tool_use_skill(context, {"name": "nope"}))
    assert "error" in result
    assert "demo:cool-skill" in result["available_skills"]


def test_use_skill_truncates_large(ctx, tmp_path: Path) -> None:
    skill_dir = tmp_path / "packs" / "demo" / "skills" / "huge"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: huge\ndescription: big\n---\n\n" + "x" * 40000
    )
    context, _ = ctx
    context.skills = SkillCatalog.load(tmp_path / "packs")
    result = _result_json(tool_use_skill(context, {"name": "huge"}))
    assert result["truncated"] is True
    assert len(result["instructions"]) < 17000
