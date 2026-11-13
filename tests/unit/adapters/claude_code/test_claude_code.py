"""Claude Code adapter unit tests — onboarding state, argv, stream-json."""

from __future__ import annotations

import json

from adapters.claude_code import adapter, config
from adapters.claude_code.__main__ import _argv
from adapters.claude_code.translate import translate_obj
from adapters.common.runner import ShimConfig


def _cfg(tmp_path, **env):
    e = {
        "GATEWAY_BASE_URL": "http://gateway:8080",
        "GATEWAY_CRED": "heg.test",
        "MODEL_NAME": "qwen2.5-coder:3b",
        "TASK_PROMPT": "build a thing",
        "HOME": str(tmp_path),
    }
    e.update(env)
    return ShimConfig.from_env(e)


class TestConfig:
    def test_writes_onboarding_state(self, tmp_path):
        cfg = _cfg(tmp_path)
        paths = config.write_config(cfg)
        claude_json = json.loads(paths[0].read_text())
        assert claude_json["hasCompletedOnboarding"] is True
        assert claude_json["hasTrustDialogAccepted"] is True
        assert claude_json["bypassPermissionsModeAccepted"] is True
        settings = json.loads(paths[1].read_text())
        assert settings["permissions"]["defaultMode"] == "bypassPermissions"

    def test_merges_existing_state(self, tmp_path):
        (tmp_path / ".claude.json").write_text('{"customKey": 1, "theme": "light"}')
        paths = config.write_config(_cfg(tmp_path))
        state = json.loads(paths[0].read_text())
        assert state["customKey"] == 1
        assert state["hasCompletedOnboarding"] is True
        assert state["theme"] == "dark"  # defaults overwrite onboarding keys


class TestEnvLaunch:
    def test_anthropic_env(self):
        env = adapter.env("http://gateway:8080", "heg.cred", "task", "model-x")
        assert env["ANTHROPIC_BASE_URL"] == "http://gateway:8080"
        assert env["ANTHROPIC_AUTH_TOKEN"] == "heg.cred"
        assert env["ANTHROPIC_MODEL"] == "model-x"
        assert env["ANTHROPIC_SMALL_FAST_MODEL"] == "model-x"
        assert env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] == "1"

    def test_launch_image(self):
        spec = adapter.launch({"task_prompt": "t"})
        assert spec["image"] == adapter.IMAGE


class TestArgv:
    def test_headless_flags(self, tmp_path):
        argv = _argv(_cfg(tmp_path))
        assert argv[:2] == ["claude", "-p"]
        assert "stream-json" in argv
        assert "--dangerously-skip-permissions" in argv
        assert argv[argv.index("--model") + 1] == "qwen2.5-coder:3b"
        # Positional task prompt is last.
        assert "build a thing" in argv[-1]
        assert "done" in argv[-1]  # sandbox footer

    def test_append_system_prompt_native(self, tmp_path):
        cfg = _cfg(tmp_path, SYSTEM_PROMPT_EXTRA="be terse")
        argv = _argv(cfg)
        idx = argv.index("--append-system-prompt")
        assert argv[idx + 1] == "be terse"
        # ...and it is NOT duplicated into the prompt.
        assert "be terse" not in argv[-1]


class TestTranslate:
    def test_system_init(self):
        ev = translate_obj(
            {
                "type": "system",
                "subtype": "init",
                "session_id": "s1",
                "tools": ["Bash", "Read"],
                "model": "m",
            }
        )
        assert ev[0]["type"] == "checkpoint"
        assert ev[0]["payload"]["tool_count"] == 2

    def test_assistant_tool_use_bash(self):
        ev = translate_obj(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "Bash",
                            "input": {"command": "ls -la"},
                        }
                    ]
                },
            }
        )
        assert ev[0]["type"] == "shell_command"
        assert ev[0]["payload"]["command"] == "ls -la"

    def test_assistant_tool_use_file_and_generic(self):
        ev = translate_obj(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "Read",
                            "input": {"file_path": "/w/a.py"},
                        },
                        {"type": "tool_use", "name": "Task", "input": {"prompt": "p"}},
                        {"type": "text", "text": "working on it"},
                    ]
                },
            }
        )
        types = [e["type"] for e in ev]
        assert types == ["file_op", "tool_call", "checkpoint"]
        assert ev[0]["payload"]["op"] == "read"
        assert ev[1]["payload"]["name"] == "Task"

    def test_tool_results_skipped(self):
        # Results would double-count; the matching tool_use already emitted.
        assert (
            translate_obj(
                {
                    "type": "user",
                    "message": {
                        "content": [
                            {"type": "tool_result", "is_error": True, "content": "boom"}
                        ]
                    },
                }
            )
            == []
        )

    def test_result_success(self):
        ev = translate_obj(
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "result": "all done",
                "num_turns": 4,
                "usage": {"input_tokens": 10, "output_tokens": 5},
            }
        )
        assert ev[-1]["type"] == "terminal_state"
        assert ev[-1]["payload"]["terminal_state"] == "harness_complete"
        assert ev[-1]["payload"]["num_turns"] == 4

    def test_result_error(self):
        ev = translate_obj(
            {
                "type": "result",
                "subtype": "error_during_execution",
                "is_error": True,
                "result": "blew up",
            }
        )
        assert ev[0]["type"] == "error"
        assert ev[-1]["payload"]["terminal_state"] == "error"

    def test_compact_boundary(self):
        ev = translate_obj(
            {
                "type": "system",
                "subtype": "compact_boundary",
                "compact_metadata": {"trigger": "auto"},
            }
        )
        assert ev[0]["type"] == "context_compacted"
        assert ev[0]["payload"]["trigger"] == "auto"

    def test_unknown_skipped(self):
        assert translate_obj({"type": "rate_limit_event"}) == []
        assert translate_obj({"type": "system", "subtype": "api_retry"}) == []
