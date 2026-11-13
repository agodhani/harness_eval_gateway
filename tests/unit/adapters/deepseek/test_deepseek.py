"""DeepSeek Harness (dsh) adapter unit tests — overlay, argv, NDJSON."""

from __future__ import annotations

from adapters.common.runner import ShimConfig
from adapters.deepseek import adapter, config
from adapters.deepseek.__main__ import _argv
from adapters.deepseek.translate import translate_obj


def _cfg(tmp_path, **env):
    e = {
        "GATEWAY_BASE_URL": "http://gateway:8080",
        "GATEWAY_CRED": "heg.test",
        "MODEL_NAME": "qwen2.5-coder:3b",
        "TASK_PROMPT": "build a thing",
        "HOME": str(tmp_path),
        "DSH_HOME": str(tmp_path / ".dsh"),
    }
    e.update(env)
    return ShimConfig.from_env(e)


class TestConfig:
    def test_overlay_written(self, tmp_path):
        cfg = _cfg(tmp_path)
        path = config.write_config(cfg)
        text = path.read_text()
        assert path == tmp_path / ".dsh" / "profiles" / "headless" / "cordis.patch.yml"
        # Loader patch-list shape: {id, config} rows replacing targeted config.
        assert "- id: llm-deepseek" in text
        assert 'baseURL: "http://gateway:8080"' in text
        assert 'apiKeyEnv: "DEEPSEEK_API_KEY"' in text
        assert "- id: agent-default-model" in text
        assert 'provider: deepseek-official' in text
        assert 'model: "qwen2.5-coder:3b"' in text
        # Output cap clamps dsh's 256k default so requests pass the budget gate.
        assert "maxTokens: 16384" in text

    def test_overlay_max_tokens_env(self, tmp_path):
        cfg = _cfg(tmp_path, DSH_MAX_TOKENS="4096")
        text = config.write_config(cfg).read_text()
        assert "maxTokens: 4096" in text

    def test_dsh_home_default(self, tmp_path):
        cfg = _cfg(tmp_path, DSH_HOME="")
        cfg.env.pop("DSH_HOME", None)
        assert config.dsh_home(cfg) == tmp_path / ".dsh"


class TestEnvLaunch:
    def test_deepseek_env(self):
        env = adapter.env("http://gateway:8080", "heg.cred", "t", "m")
        assert env["DEEPSEEK_API_KEY"] == "heg.cred"
        assert env["DEEPSEEK_BASE_URL"] == "http://gateway:8080"
        assert env["DSH_HOME"] == "/home/agent/.dsh"

    def test_launch(self):
        spec = adapter.launch({"task_prompt": "t"})
        assert spec["image"] == adapter.IMAGE


class TestArgv:
    def test_headless_flags(self, tmp_path):
        argv = _argv(_cfg(tmp_path))
        assert argv[:3] == ["dsh", "--profile", "headless"]
        assert "--json" in argv
        assert "--permission-mode" in argv
        assert "build a thing" in argv[-1]


class TestTranslate:
    def test_session_and_status(self):
        ev = translate_obj({"type": "session", "sessionId": "s1"})
        assert ev[0]["type"] == "checkpoint"
        assert ev[0]["payload"]["session_id"] == "s1"
        ev = translate_obj({"type": "status", "status": "running"})
        assert ev[0]["payload"]["status"] == "running"

    def test_tool_call_variants(self):
        ev = translate_obj(
            {"type": "tool_call", "name": "bash", "arguments": {"command": "ls"}}
        )
        assert ev[0]["type"] == "shell_command"
        ev = translate_obj(
            {"type": "tool_call", "name": "read_file", "input": {"path": "/w/a"}}
        )
        assert ev[0]["type"] == "file_op"
        assert ev[0]["payload"]["op"] == "read"
        ev = translate_obj({"type": "tool_call", "name": "plan", "args": {"x": 1}})
        assert ev[0]["type"] == "tool_call"

    def test_tool_result(self):
        ev = translate_obj(
            {"type": "tool_result", "error": {"code": "ENOENT", "message": "no file"}}
        )
        assert ev[0]["type"] == "error"
        ev = translate_obj({"type": "tool_result", "result": "ok"})
        assert ev[0]["type"] == "checkpoint"

    def test_text_and_thinking(self):
        ev = translate_obj({"type": "text", "text": "hello"})
        assert ev[0]["type"] == "checkpoint"
        assert translate_obj({"type": "thinking", "text": "hmm"}) == []

    def test_final_completed(self):
        ev = translate_obj(
            {
                "type": "final",
                "sessionId": "s1",
                "reason": {"kind": "completed"},
                "answer": "done",
            }
        )
        assert ev[-1]["type"] == "terminal_state"
        assert ev[-1]["payload"]["terminal_state"] == "harness_complete"
        assert ev[-1]["payload"]["result"] == "done"

    def test_final_aborted_is_error(self):
        ev = translate_obj(
            {"type": "final", "reason": {"kind": "aborted"}, "answer": ""}
        )
        assert ev[0]["type"] == "error"
        assert ev[-1]["payload"]["terminal_state"] == "error"

    def test_error_record(self):
        ev = translate_obj({"type": "error", "message": "runner died"})
        assert ev[0]["type"] == "error"
        assert "runner died" in ev[0]["payload"]["message"]
