"""Tests for adapters/common/hooks.py — the shared native-hook receiver."""

from __future__ import annotations

import io
import json
from typing import Any, Self

import pytest

from adapters.common import hooks, runner


def _sink(buf: io.StringIO | None = None) -> tuple[runner.EventSink, io.StringIO]:
    out = buf if buf is not None else io.StringIO()
    return runner.EventSink(None, "cred", stdout=out), out


class TestParsePayload:
    def test_argv_json_wins(self) -> None:
        payload = hooks.parse_payload(
            ["claude_code", '{"a": 1}'], stdin=io.StringIO('{"b": 2}')
        )
        assert payload == {"a": 1}

    def test_argv_scanned_right_to_left(self) -> None:
        # Codex appends the payload after configured argv tokens.
        payload = hooks.parse_payload(
            ["not-json", '{"x": 1}'], stdin=io.StringIO("")
        )
        assert payload == {"x": 1}

    def test_stdin_fallback(self) -> None:
        payload = hooks.parse_payload(
            [], stdin=io.StringIO('{"hook_event_name": "Stop"}')
        )
        assert payload == {"hook_event_name": "Stop"}

    def test_non_dict_rejected(self) -> None:
        assert hooks.parse_payload(['"a string"'], stdin_text="") is None
        assert hooks.parse_payload(["[1, 2]"], stdin_text="") is None

    def test_garbage_returns_none(self) -> None:
        assert hooks.parse_payload(["noise"], stdin_text="also noise") is None
        assert hooks.parse_payload([], stdin_text="") is None


class TestReadStdin:
    def test_stringio_read(self) -> None:
        # StringIO has no fileno — the buffered-read fallback kicks in.
        assert hooks._read_stdin(io.StringIO('{"k": 1}')) == '{"k": 1}'

    def test_empty_when_nothing_ready(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class _Fd(io.StringIO):
            def fileno(self) -> int:
                return 0

        monkeypatch.setattr(hooks.select, "select", lambda *a, **kw: ([], [], []))
        assert hooks._read_stdin(_Fd()) == ""


class TestRunHook:
    def test_emits_translated_events_with_via_stamp(self) -> None:
        sink, out = _sink()
        rc = hooks.run_hook(
            "claude_code",
            {"hook_event_name": "Stop", "session_id": "s1"},
            cfg=runner.ShimConfig(gateway_url="g", cred="c", model="m", task_prompt="t"),
            sink=sink,
        )
        assert rc == 0
        lines = [json.loads(x) for x in out.getvalue().strip().splitlines()]
        assert len(lines) == 1
        assert lines[0]["type"] == "checkpoint"
        assert lines[0]["payload"]["stage"] == "stop"
        # Hook provenance is stamped for timeline/analytics discrimination.
        assert lines[0]["payload"]["via"] == "hook"
        assert lines[0]["source"] == "harness"

    def test_posts_to_ingest(self, monkeypatch: pytest.MonkeyPatch) -> None:
        posted: list[dict[str, Any]] = []

        class _Resp:
            status = 200

            def __enter__(self) -> Self:
                return self

            def __exit__(self, *a: object) -> None:
                return None

        def fake_urlopen(req: Any, timeout: float = 0) -> _Resp:
            posted.append(
                {
                    "url": req.full_url,
                    "auth": req.headers.get("Authorization"),
                    "body": json.loads(req.data),
                }
            )
            return _Resp()

        monkeypatch.setattr(runner.urllib.request, "urlopen", fake_urlopen)
        sink = runner.EventSink("http://gw/internal/events", "heg.c", stdout=io.StringIO())
        rc = hooks.run_hook(
            "codex",
            {"type": "agent-turn-complete", "last-assistant-message": "done"},
            cfg=runner.ShimConfig(gateway_url="g", cred="heg.c", model="m", task_prompt="t"),
            sink=sink,
        )
        assert rc == 0
        assert len(posted) == 1
        assert posted[0]["url"] == "http://gw/internal/events"
        assert posted[0]["auth"] == "Bearer heg.c"
        assert posted[0]["body"]["type"] == "checkpoint"
        assert posted[0]["body"]["payload"]["stage"] == "agent_turn_complete"

    def test_unknown_adapter_is_noop(self) -> None:
        sink, out = _sink()
        rc = hooks.run_hook(
            "deepseek",  # no translate_hook — stays on translated output
            {"hook_event_name": "x"},
            cfg=runner.ShimConfig(gateway_url="g", cred="c", model="m", task_prompt="t"),
            sink=sink,
        )
        assert rc == 0
        assert out.getvalue() == ""

    def test_translate_error_surfaces_error_event(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(_obj: dict[str, Any]) -> list[dict[str, Any]]:
            raise RuntimeError("kaboom")

        monkeypatch.setattr(hooks, "translate_hook_fn", lambda _a: boom)
        sink, out = _sink()
        rc = hooks.run_hook(
            "claude_code",
            {"hook_event_name": "PostToolUse"},
            cfg=runner.ShimConfig(gateway_url="g", cred="c", model="m", task_prompt="t"),
            sink=sink,
        )
        assert rc == 0  # never non-zero — telemetry must not break the agent
        ev = json.loads(out.getvalue().strip())
        assert ev["type"] == "error"
        assert ev["payload"]["stage"] == "hook"
        assert "kaboom" in ev["payload"]["message"]

    def test_empty_translation_emits_nothing(self) -> None:
        sink, out = _sink()
        rc = hooks.run_hook(
            "claude_code",
            {"hook_event_name": "PreToolUse"},  # deliberately unregistered
            cfg=runner.ShimConfig(gateway_url="g", cred="c", model="m", task_prompt="t"),
            sink=sink,
        )
        assert rc == 0
        assert out.getvalue() == ""


class TestMain:
    def test_no_args(self) -> None:
        assert hooks.main([]) == 0

    def test_unparseable_payload(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(hooks, "_read_stdin", lambda _s: "")
        assert hooks.main(["claude_code", "not json"]) == 0

    def test_argv_payload_emits(self, monkeypatch: pytest.MonkeyPatch) -> None:
        emitted = io.StringIO()
        sink = runner.EventSink(None, "c", stdout=emitted)
        # main() builds its own sink — stub run_hook's sink via EventSink is
        # not injectable through main; patch run_hook instead.
        seen: dict[str, Any] = {}

        def fake_run(adapter: str, payload: dict[str, Any], **kw: Any) -> int:
            seen["adapter"] = adapter
            seen["payload"] = payload
            return 0

        monkeypatch.setattr(hooks, "run_hook", fake_run)
        assert (
            hooks.main(["codex", '{"type": "agent-turn-complete", "turn-id": "t1"}'])
            == 0
        )
        assert seen["adapter"] == "codex"
        assert seen["payload"]["turn-id"] == "t1"
        del sink, emitted


class TestTranslatorRegistry:
    @pytest.mark.parametrize("name", ["claude_code", "codex", "pi"])
    def test_hooked_adapters_have_translate_hook(self, name: str) -> None:
        assert callable(hooks.translate_hook_fn(name))

    @pytest.mark.parametrize("name", ["miniloop", "deepseek", "nonexistent"])
    def test_unhooked_adapters_have_none(self, name: str) -> None:
        assert hooks.translate_hook_fn(name) is None
