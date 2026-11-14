"""Pi adapter unit tests — models.json, argv, JSON-mode translation."""

from __future__ import annotations

import json

from adapters.common.runner import ShimConfig
from adapters.pi import adapter, config
from adapters.pi.__main__ import _argv
from adapters.pi.translate import translate_hook, translate_obj


def _cfg(tmp_path, **env):
    e = {
        "GATEWAY_BASE_URL": "http://gateway:8080",
        "GATEWAY_CRED": "heg.test",
        "MODEL_NAME": "qwen2.5-coder:3b",
        "TASK_PROMPT": "build a thing",
        "HOME": str(tmp_path),
        "PI_CODING_AGENT_DIR": str(tmp_path / "agent"),
    }
    e.update(env)
    return ShimConfig.from_env(e)


class TestConfig:
    def test_models_json(self, tmp_path):
        cfg = _cfg(tmp_path)
        path = config.write_config(cfg)
        data = json.loads(path.read_text())
        prov = data["providers"]["heg"]
        assert prov["baseUrl"] == "http://gateway:8080/v1"
        assert prov["api"] == "openai-completions"
        # Cred is interpolated from env at request time — never on disk.
        assert prov["apiKey"] == "$GATEWAY_CRED"
        assert prov["models"] == [{"id": "qwen2.5-coder:3b"}]

    def test_dir_override(self, tmp_path):
        cfg = _cfg(tmp_path, PI_CODING_AGENT_DIR=str(tmp_path / "x" / "y"))
        path = config.write_config(cfg)
        assert path == tmp_path / "x" / "y" / "models.json"

    def test_extension_written(self, tmp_path):
        cfg = _cfg(tmp_path)
        config.write_config(cfg)
        ext = tmp_path / "agent" / "extensions" / "heg-events.ts"
        assert ext.is_file()
        text = ext.read_text()
        # Forwards events to the shared receiver for this adapter…
        assert '"-m", "adapters.common.hooks", "pi"' in text
        assert "tool_execution_end" in text


class TestEnvLaunch:
    def test_env(self):
        env = adapter.env("http://gateway:8080", "heg.cred", "t", "m")
        assert env["PI_CODING_AGENT_DIR"] == "/home/agent/.pi/agent"

    def test_launch(self):
        spec = adapter.launch({"task_prompt": "t", "cred": "heg.c"})
        assert spec["image"] == adapter.IMAGE


class TestArgv:
    def test_headless(self, tmp_path):
        cfg = _cfg(tmp_path)
        argv = _argv(cfg)
        assert argv[:3] == ["pi", "--mode", "json"]
        assert "--no-session" in argv
        assert argv[argv.index("--provider") + 1] == "heg"
        assert argv[argv.index("--model") + 1] == "qwen2.5-coder:3b"
        assert argv[argv.index("--api-key") + 1] == "heg.test"
        assert "build a thing" in argv[-1]

    def test_system_prompt_native(self, tmp_path):
        argv = _argv(_cfg(tmp_path, SYSTEM_PROMPT_EXTRA="be terse"))
        assert argv[argv.index("--append-system-prompt") + 1] == "be terse"
        assert "be terse" not in argv[-1]


class TestTranslate:
    def test_session_and_agent(self):
        ev = translate_obj({"type": "session", "id": "s1", "cwd": "/workspace"})
        assert ev[0]["type"] == "checkpoint"
        assert ev[0]["payload"]["session_id"] == "s1"
        assert translate_obj({"type": "agent_start"})[0]["type"] == "checkpoint"

    def test_tool_execution_bash(self):
        ev = translate_obj(
            {
                "type": "tool_execution_start",
                "toolCallId": "c1",
                "toolName": "bash",
                "args": {"command": "ls"},
            }
        )
        assert ev[0]["type"] == "shell_command"
        assert ev[0]["payload"]["command"] == "ls"
        assert ev[0]["payload"]["tool_call_id"] == "c1"

    def test_tool_execution_file_and_generic(self):
        ev = translate_obj(
            {
                "type": "tool_execution_start",
                "toolCallId": "c2",
                "toolName": "read",
                "args": {"path": "/w/a.py"},
            }
        )
        assert ev[0]["type"] == "file_op"
        assert ev[0]["payload"]["op"] == "read"
        ev = translate_obj(
            {
                "type": "tool_execution_start",
                "toolCallId": "c3",
                "toolName": "web_search",
                "args": {"q": "x"},
            }
        )
        assert ev[0]["type"] == "tool_call"
        assert ev[0]["payload"]["name"] == "web_search"

    def test_tool_execution_error(self):
        ev = translate_obj(
            {
                "type": "tool_execution_end",
                "toolCallId": "c1",
                "toolName": "bash",
                "isError": True,
                "result": {"content": "exit 127"},
            }
        )
        assert ev[0]["type"] == "error"
        assert ev[0]["payload"]["tool"] == "bash"

    def test_tool_execution_ok_is_checkpoint(self):
        ev = translate_obj(
            {
                "type": "tool_execution_end",
                "toolCallId": "c1",
                "toolName": "bash",
                "isError": False,
                "durationMs": 12,
            }
        )
        assert ev[0]["type"] == "checkpoint"
        assert ev[0]["payload"]["duration_ms"] == 12

    def test_message_end_assistant(self):
        ev = translate_obj(
            {
                "type": "message_end",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "hi"}],
                },
            }
        )
        assert ev[0]["type"] == "checkpoint"
        assert ev[0]["payload"]["text"] == "hi"
        assert (
            translate_obj({"type": "message_end", "message": {"role": "user"}})
            == []
        )

    def test_compaction(self):
        ev = translate_obj(
            {
                "type": "compaction_end",
                "reason": "threshold",
                "aborted": False,
                "result": {"tokensBefore": 100, "estimatedTokensAfter": 40},
            }
        )
        assert ev[0]["type"] == "context_compacted"
        assert ev[0]["payload"]["tokens_before"] == 100

    def test_agent_settled(self):
        ev = translate_obj({"type": "agent_settled", "aborted": False})
        assert ev[0]["payload"]["terminal_state"] == "harness_complete"
        ev = translate_obj({"type": "agent_settled", "aborted": True})
        assert ev[0]["payload"]["terminal_state"] == "error"

    def test_noise_skipped(self):
        assert translate_obj({"type": "message_update"}) == []
        assert translate_obj({"type": "tool_execution_update"}) == []
        assert translate_obj({"type": "mystery"}) == []


class TestHooks:
    """Extension-forwarded payloads → normalized events (PR17)."""

    def test_tool_execution_end_result(self):
        ev = translate_hook(
            {
                "hook_event_name": "tool_execution_end",
                "event": {
                    "toolCallId": "c1",
                    "toolName": "bash",
                    "isError": False,
                    "durationMs": 42,
                    "result": {"content": "ok"},
                },
            }
        )
        assert len(ev) == 1
        assert ev[0]["type"] == "tool_result"
        p = ev[0]["payload"]
        assert p["name"] == "bash"
        assert p["tool_call_id"] == "c1"
        assert p["is_error"] is False
        assert p["duration_ms"] == 42
        assert "ok" in p["output"]

    def test_tool_execution_end_error(self):
        ev = translate_hook(
            {
                "hook_event_name": "tool_execution_end",
                "event": {"toolName": "bash", "isError": True, "result": "exit 127"},
            }
        )
        p = ev[0]["payload"]
        assert p["is_error"] is True
        assert p["output"] == "exit 127"

    def test_other_hooks_dropped(self):
        assert (
            translate_hook({"hook_event_name": "tool_execution_start", "event": {}})
            == []
        )
        assert translate_hook({"hook_event_name": "tool_execution_end"}) == []
        assert translate_hook({}) == []
