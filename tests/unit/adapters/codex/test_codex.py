"""Codex adapter unit tests — config.toml, argv, JSONL translation."""

from __future__ import annotations

import json

from adapters.codex import adapter, config
from adapters.codex.__main__ import ARGV, main
from adapters.codex.translate import translate_events, translate_obj
from adapters.common.runner import ShimConfig


def _cfg(tmp_path, **env):
    e = {
        "GATEWAY_BASE_URL": "http://gateway:8080",
        "GATEWAY_CRED": "heg.test",
        "MODEL_NAME": "qwen2.5-coder:3b",
        "TASK_PROMPT": "build a thing",
        "HOME": str(tmp_path),
        "CODEX_HOME": str(tmp_path / ".codex"),
    }
    e.update(env)
    return ShimConfig.from_env(e)


class TestConfig:
    def test_writes_config_toml(self, tmp_path):
        cfg = _cfg(tmp_path)
        path = config.write_config(cfg)
        text = path.read_text()
        assert path == tmp_path / ".codex" / "config.toml"
        assert 'wire_api = "responses"' in text
        assert 'model = "qwen2.5-coder:3b"' in text
        assert 'base_url = "http://gateway:8080/v1"' in text
        assert 'env_key = "HEG_API_KEY"' in text
        assert 'model_provider = "heg"' in text
        assert "disable_response_storage = true" in text

    def test_escapes_quotes(self, tmp_path):
        cfg = _cfg(tmp_path, MODEL_NAME='evil"model')
        text = config.write_config(cfg).read_text()
        assert 'model = "evil\\"model"' in text


class TestEnvLaunch:
    def test_env_extras(self):
        env = adapter.env(
            "http://gateway:8080", "heg.cred", "task", "model-x"
        )
        assert env["HEG_API_KEY"] == "heg.cred"
        assert env["CODEX_HOME"] == "/home/agent/.codex"

    def test_launch_image(self):
        spec = adapter.launch({"task_prompt": "t", "model": "m"})
        assert spec["image"] == adapter.IMAGE
        assert spec["cmd"] == ["python3", "-m", "adapters.codex"]


class TestTranslate:
    def test_thread_and_turn(self):
        ev = translate_obj({"type": "thread.started", "thread_id": "t1"})
        assert ev[0]["type"] == "checkpoint"
        assert ev[0]["payload"]["thread_id"] == "t1"
        assert translate_obj({"type": "turn.started"})[0]["type"] == "checkpoint"

    def test_command_execution(self):
        ev = translate_obj(
            {
                "type": "item.completed",
                "item": {
                    "type": "command_execution",
                    "command": "bash -lc ls",
                    "aggregated_output": "file1",
                    "exit_code": 0,
                    "status": "completed",
                },
            }
        )
        assert ev[0]["type"] == "shell_command"
        assert ev[0]["payload"]["command"] == "bash -lc ls"
        assert ev[0]["payload"]["exit_code"] == 0

    def test_file_change(self):
        ev = translate_obj(
            {
                "type": "item.completed",
                "item": {
                    "type": "file_change",
                    "changes": [{"path": "a.py"}, {"path": "b.py"}],
                    "status": "completed",
                },
            }
        )
        assert ev[0]["type"] == "file_op"
        assert ev[0]["payload"]["paths"] == ["a.py", "b.py"]

    def test_mcp_tool_call(self):
        ev = translate_obj(
            {
                "type": "item.completed",
                "item": {
                    "type": "mcp_tool_call",
                    "tool": "search",
                    "server": "mcp",
                    "arguments": {"q": "x"},
                },
            }
        )
        assert ev[0]["type"] == "tool_call"
        assert ev[0]["payload"]["name"] == "search"

    def test_agent_message_and_turn_completed(self):
        ev = translate_obj(
            {
                "type": "item.completed",
                "item": {"type": "agent_message", "text": "done!"},
            }
        )
        assert ev[0]["type"] == "checkpoint"
        assert ev[0]["payload"]["stage"] == "agent_message"
        ev = translate_obj(
            {
                "type": "turn.completed",
                "usage": {"input_tokens": 5, "output_tokens": 3},
            }
        )
        assert ev[0]["payload"]["usage"]["output_tokens"] == 3

    def test_turn_failed(self):
        ev = translate_obj(
            {
                "type": "turn.failed",
                "error": {"message": "context blew up"},
            }
        )
        assert ev[0]["type"] == "error"
        assert "context blew up" in ev[0]["payload"]["message"]

    def test_unknown_records_skipped(self):
        assert translate_obj({"type": "item.started", "item": {}}) == []
        assert translate_obj({"type": "item.completed", "item": {"type": "reasoning"}}) == []
        assert translate_obj({"type": "unrelated"}) == []

    def test_stream_with_marker(self):
        lines = [
            json.dumps({"type": "turn.started"}),
            "garbage",
            'HEG_RESULT {"terminal_state": "harness_complete"}',
        ]
        events = adapter.translate_events(lines)
        types = [e["type"] for e in events]
        assert "checkpoint" in types
        assert "terminal_state" in types
        # translate module itself ignores the marker (adapter folds it).
        assert all(
            e["type"] == "checkpoint" for e in translate_events(lines[:1])
        )


class TestShim:
    def test_main_missing_binary_reports_error(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GATEWAY_BASE_URL", "http://gateway:8080")
        monkeypatch.setenv("GATEWAY_CRED", "heg.x")
        monkeypatch.setenv("MODEL_NAME", "m")
        monkeypatch.setenv("TASK_PROMPT", "t")
        monkeypatch.setenv("CODEX_HOME", str(tmp_path / ".codex"))
        monkeypatch.setenv("HOME", str(tmp_path))
        rc = main()
        assert rc == 2  # codex binary absent → spawn error, not a crash

    def test_argv_shape(self):
        assert ARGV[:3] == ["codex", "exec", "--json"]
        assert "--dangerously-bypass-approvals-and-sandbox" in ARGV
