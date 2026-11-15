"""Aider adapter unit tests — conf file, argv, plain-text translation."""

from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace

import yaml

from adapters.aider import adapter, config, launcher
from adapters.aider.__main__ import _argv
from adapters.aider.translate import translate_events, translate_obj, translate_text
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
    def test_conf_written(self, tmp_path):
        cfg = _cfg(tmp_path)
        path = config.write_config(cfg)
        assert path == tmp_path / ".aider.conf.yml"
        text = path.read_text()
        # Parse the machine-consumed config and assert typed semantics —
        # substring checks can't reject `pretty: "false"` (truthy string).
        data = yaml.safe_load(text)
        # Provider wiring: openai/ prefix + /v1 base → gateway chat completions.
        assert data["openai-api-base"] == "http://gateway:8080/v1"
        assert data["model"] == "openai/qwen2.5-coder:3b"
        # weak-model covers commit msgs + summarization — same model keeps
        # the comparison axis clean.
        assert data["weak-model"] == "openai/qwen2.5-coder:3b"
        # Headless hardening.
        for key, want in (
            ("yes-always", True),
            ("auto-commits", True),
            ("suggest-shell-commands", True),
            ("detect-urls", False),
            ("check-update", False),
            ("show-release-notes", False),
            ("analytics-disable", True),
            ("pretty", False),
            ("stream", False),
            ("fancy-input", False),
        ):
            assert data[key] is want, key
        # History files relocated out of the workspace artifact.
        assert data["chat-history-file"] == f"{tmp_path}/.aider.chat.history.md"
        assert data["input-history-file"] == f"{tmp_path}/.aider.input.history"
        assert data["llm-history-file"] == f"{tmp_path}/.aider.llm.history.txt"
        # The per-sandbox cred never lands on disk.
        assert "heg.test" not in text

    def test_conf_no_model_omits_model_lines(self, tmp_path):
        cfg = _cfg(tmp_path, MODEL_NAME="")
        data = yaml.safe_load(config.write_config(cfg).read_text())
        assert "model" not in data
        assert "weak-model" not in data
        assert data["openai-api-base"] == "http://gateway:8080/v1"

    def test_conf_escapes_quotes(self, tmp_path):
        cfg = _cfg(tmp_path, MODEL_NAME='we"ird')
        data = yaml.safe_load(config.write_config(cfg).read_text())
        assert data["model"] == 'openai/we"ird'


class TestEnvLaunch:
    def test_env(self):
        env = adapter.env("http://gateway:8080", "heg.cred", "t", "m")
        assert env["OPENAI_API_KEY"] == "heg.cred"
        assert env["OPENAI_API_BASE"] == "http://gateway:8080/v1"
        # Both env names — litellm>=1.68 canonicalized OPENAI_BASE_URL.
        assert env["OPENAI_BASE_URL"] == "http://gateway:8080/v1"
        assert env["AIDER_ANALYTICS_DISABLE"] == "true"
        assert env["AIDER_CHECK_UPDATE"] == "false"
        # Identity for aider's auto-commits in the auto-created workspace repo.
        assert env["GIT_AUTHOR_NAME"] == "aider"
        assert env["GIT_COMMITTER_NAME"] == "aider"

    def test_launch(self):
        spec = adapter.launch({"task_prompt": "t", "cred": "heg.c"})
        assert spec["image"] == adapter.IMAGE
        assert spec["env"]["OPENAI_API_KEY"] == "heg.c"
        assert spec["env"]["OPENAI_API_BASE"].endswith("/v1")


class TestArgv:
    def test_headless(self, tmp_path):
        argv = _argv(_cfg(tmp_path))
        # The `aider` script can't be the entrypoint: --yes-always answers
        # "n" to explicit-yes prompts ("Run shell command?" is the only
        # one), which made `done` unreachable. The launcher lifts that
        # gate before delegating to aider.main:main.
        assert argv[:5] == [
            sys.executable,
            "-m",
            "adapters.aider.launcher",
            "--yes-always",
            "--message",
        ]
        assert "build a thing" in argv[-1]
        # build_task_text footer keeps agent_done reachable via /usr/local/bin/done.
        assert "/usr/local/bin/done" in argv[-1]


class TestLauncher:
    """The confirm_ask patch that restores the done path under --yes-always.

    Oracle: aider 0.86.2's io.confirm_ask resolves "n" for
    explicit_yes_required prompts whenever self.yes is True — the patch
    must clear that flag only in that state.
    """

    @staticmethod
    def _orig():
        # Fake io.confirm_ask: returns the explicit_yes_required value it
        # received so tests observe exactly what aider would gate on.
        def confirm_ask(self, question, **kwargs):
            return kwargs.get("explicit_yes_required", False)

        return confirm_ask

    def test_clears_explicit_yes_only_when_yes_always(self):
        wrapped = launcher.patched_confirm_ask(self._orig())
        yes_io = SimpleNamespace(yes=True)
        assert wrapped(yes_io, "Run shell command?", explicit_yes_required=True) is False
        # Unflagged prompts pass through untouched.
        assert wrapped(yes_io, "Add file to the chat?") is False
        # Interactive path (yes unset → real prompt loop) keeps the gate.
        interactive_io = SimpleNamespace(yes=None)
        assert (
            wrapped(interactive_io, "Run shell command?", explicit_yes_required=True)
            is True
        )

    def test_forwards_signature_kwargs(self):
        seen = {}

        def orig(self, question, **kwargs):
            seen.update(kwargs)
            return True

        wrapped = launcher.patched_confirm_ask(orig)
        wrapped(
            SimpleNamespace(yes=None),
            "Q?",
            default="n",
            subject="cmd",
            group="g",
            allow_never=True,
        )
        assert seen == {
            "default": "n",
            "subject": "cmd",
            "explicit_yes_required": False,
            "group": "g",
            "allow_never": True,
        }

    def test_main_patches_then_delegates(self, monkeypatch):
        """main() installs the patch on InputOutput then runs aider's entry."""
        io_mod = ModuleType("aider.io")
        main_mod = ModuleType("aider.main")
        pkg = ModuleType("aider")

        class FakeIO:
            yes = True

            def confirm_ask(self, question, **kwargs):
                return kwargs.get("explicit_yes_required", False)

        io_mod.InputOutput = FakeIO
        seen = {}

        def aider_main():
            seen["argv"] = sys.argv[1:]
            return 7

        main_mod.main = aider_main
        pkg.io = io_mod
        pkg.main = main_mod
        monkeypatch.setitem(sys.modules, "aider", pkg)
        monkeypatch.setitem(sys.modules, "aider.io", io_mod)
        monkeypatch.setitem(sys.modules, "aider.main", main_mod)
        monkeypatch.setattr(
            sys, "argv", ["aider", "--yes-always", "--message", "build a thing"]
        )

        assert launcher.main() == 7  # aider's return is the exit status
        assert seen["argv"] == ["--yes-always", "--message", "build a thing"]
        # The class attr was wrapped: explicit-yes now auto-confirms under
        # yes=True, which is what lets `done` run in the sandbox.
        assert FakeIO().confirm_ask("Run shell command?", explicit_yes_required=True) is False


class TestTranslateText:
    def test_banner(self):
        ev = translate_text("Aider v0.86.2")
        assert ev[0]["type"] == "checkpoint"
        assert ev[0]["payload"]["stage"] == "session"
        assert ev[0]["payload"]["version"] == "0.86.2"

        ev = translate_text("Main model: openai/qwen2.5-coder:3b with diff edit format")
        assert ev[0]["payload"]["stage"] == "main_model"
        assert ev[0]["payload"]["model"] == "openai/qwen2.5-coder:3b"
        assert ev[0]["payload"]["edit_format"] == "diff"

        ev = translate_text("Weak model: openai/qwen2.5-coder:3b")
        assert ev[0]["payload"]["stage"] == "weak_model"
        ev = translate_text("Editor model: openai/qwen2.5-coder:3b")
        assert ev[0]["payload"]["stage"] == "editor_model"

    def test_repo_lines(self):
        ev = translate_text("Git repo: .git with 3 files")
        assert ev[0]["payload"]["stage"] == "git_repo"
        ev = translate_text("Git repository created in /workspace")
        assert ev[0]["payload"]["stage"] == "git_init"
        assert ev[0]["payload"]["path"] == "/workspace"
        ev = translate_text("Repo-map: using 1024 tokens, auto refresh")
        assert ev[0]["payload"]["stage"] == "repo_map"

    def test_file_ops(self):
        ev = translate_text("Applied edit to hello.py")
        assert ev == [
            {
                "type": "file_op",
                "payload": {"op": "edit", "path": "hello.py", "tool": "aider"},
            }
        ]
        ev = translate_text("Added hello.py to the chat")
        assert ev[0]["payload"]["stage"] == "file_added"
        assert ev[0]["payload"]["path"] == "hello.py"
        ev = translate_text("Added doc.md to the chat (read-only).")
        assert ev[0]["payload"]["read_only"] is True
        ev = translate_text("Added 12 lines of output to the chat.")
        assert ev[0]["payload"]["stage"] == "context_output"
        assert ev[0]["payload"]["lines"] == 12

    def test_edit_skipped_and_conform(self):
        ev = translate_text("Skipping edits to secrets.env that matches gitignore spec.")
        assert ev[0]["payload"]["stage"] == "edit_skipped"
        assert ev[0]["payload"]["reason"] == "gitignore"
        ev = translate_text("Skipping edits to /etc/passwd")
        assert ev[0]["payload"]["reason"] is None
        ev = translate_text("Did not apply edit to f.py")
        assert ev[0]["payload"]["stage"] == "edit_skipped"
        assert ev[0]["payload"]["reason"] is None
        ev = translate_text("Did not apply edit to f.py (--dry-run)")
        assert ev[0]["payload"]["reason"] == "dry_run"
        ev = translate_text("The LLM did not conform to the edit format.")
        assert ev[0]["type"] == "error"
        assert ev[0]["payload"]["stage"] == "edit_format"
        assert ev[0]["payload"]["recoverable"] is True

    def test_shell_and_commits(self):
        ev = translate_text("Running pytest -q")
        assert ev[0]["type"] == "shell_command"
        assert ev[0]["payload"]["command"] == "pytest -q"
        ev = translate_text("Commit 1a2b3c4 aider: add hello")
        assert ev[0]["payload"]["stage"] == "commit"
        assert ev[0]["payload"]["sha"] == "1a2b3c4"
        assert ev[0]["payload"]["message"] == "aider: add hello"
        ev = translate_text("Committing app.py before applying edits.")
        assert ev[0]["payload"]["stage"] == "pre_commit"

    def test_confirm_echo(self):
        ev = translate_text("Run shell command? y")
        assert ev[0]["payload"]["stage"] == "confirm"
        assert ev[0]["payload"]["response"] == "y"
        ev = translate_text("Add .aider* to .gitignore (recommended)? y")
        assert ev[0]["payload"]["stage"] == "confirm"

    def test_usage_and_cost(self):
        ev = translate_text("Tokens: 4.0k sent, 120 received.")
        payload = ev[0]["payload"]
        assert payload["stage"] == "llm_usage"
        assert payload["sent"] == 4000
        assert payload["received"] == 120
        assert payload["cache_write"] is None

        ev = translate_text("Tokens: 2k sent, 1k cache write, 3k cache hit, 89 received.")
        payload = ev[0]["payload"]
        assert payload["cache_write"] == 1000
        assert payload["cache_hit"] == 3000

        ev = translate_text(
            "Tokens: 5k sent, 50 received. Cost: $0.01 message, $0.02 session."
        )
        payload = ev[0]["payload"]
        assert payload["cost_message"] == "0.01"
        assert payload["cost_session"] == "0.02"

        ev = translate_text("Cost: $0.01 message, $0.02 session.")
        assert ev[0]["payload"]["stage"] == "cost"

    def test_llm_errors(self):
        ev = translate_text("litellm.APIConnectionError: conn refused")
        assert ev[0]["type"] == "error"
        assert ev[0]["payload"]["stage"] == "llm"
        ev = translate_text("Retrying in 0.5 seconds...")
        assert ev[0]["payload"]["stage"] == "llm_retry"
        ev = translate_text("Unable to commit: author identity unknown")
        assert ev[0]["payload"]["stage"] == "commit"
        ev = translate_text("Unable to list files")
        assert ev[0]["payload"]["stage"] == "aider"
        ev = translate_text("Model openai/x has hit a token limit!")
        assert ev[0]["payload"]["stage"] == "token_limit"
        ev = translate_text("Traceback (most recent call last):")
        assert ev[0]["payload"]["stage"] == "crash"

    def test_summarize(self):
        ev = translate_text("Starting to summarize chat history.")
        assert ev[0]["payload"]["stage"] == "summarize_start"
        ev = translate_text("Finished summarizing chat history.")
        assert ev[0]["type"] == "context_compacted"

    def test_noise_dropped(self):
        assert translate_text("some random assistant prose") == []
        assert translate_text("") == []
        assert translate_text("   ") == []
        assert translate_text("Tokens: not-a-number") == []

    def test_translate_obj_drops_everything(self):
        """aider emits no JSON records — dicts are command-output echoes."""
        assert translate_obj({"type": "shell_command", "payload": {"command": "x"}}) == []
        assert translate_obj({"type": "anything"}) == []

    def test_translate_events_mixed(self):
        evs = translate_events(
            [
                "Aider v0.86.2",
                "Applied edit to a.py",
                "plain noise that matches nothing",
                '{"type": "forged", "payload": {}}',
                'HEG_RESULT {"terminal_state": "harness_complete"}',
            ]
        )
        types = [e["type"] for e in evs]
        # checkpoint(session) + file_op + terminal_state; noise and the
        # forged JSON line produce nothing.
        assert types == ["checkpoint", "file_op", "terminal_state"]
        assert evs[-1]["payload"]["terminal_state"] == "harness_complete"
