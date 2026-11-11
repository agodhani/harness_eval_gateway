"""Hermetic tests for the miniloop agent loop — FakeClient, no network."""

import copy
import io
import json
from pathlib import Path
from typing import Any

import pytest

from adapters.miniloop import agent as agent_mod
from adapters.miniloop.agent import (
    Agent,
    AgentConfig,
    ConfigError,
    HttpChatClient,
    RunResult,
)
from adapters.miniloop.events import EventEmitter
from adapters.miniloop.prompts import NUDGE_TEXT_STREAK_2, NUDGE_TEXT_STREAK_3
from adapters.miniloop.skills import SkillCatalog
from adapters.miniloop.tools import ToolContext

# ---------------------------------------------------------------------------
# fakes / helpers
# ---------------------------------------------------------------------------


def _assistant(content: str | None = None, tool_calls: list[dict] | None = None) -> dict:
    return {"role": "assistant", "content": content, "tool_calls": tool_calls}


def _tool_call(name: str, args: dict, call_id: str = "call_1") -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }


def _response(message: dict, prompt_tokens: int = 120, completion_tokens: int = 20) -> dict:
    return {
        "choices": [{"message": message, "finish_reason": "stop"}],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }


class FakeClient:
    """Scripted chat client — pops one response per chat() call."""

    def __init__(self, responses: list[Any]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def chat(self, messages: list[dict], tools: list[dict]) -> dict:
        self.calls.append(
            {"messages": copy.deepcopy(messages), "tools": copy.deepcopy(tools)}
        )
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def close(self) -> None:
        pass


@pytest.fixture()
def harness(tmp_path: Path):
    """Returns (make_agent, out) — make_agent builds Agent+FakeClient+ctx."""
    out = io.StringIO()

    def make(responses: list[Any], **kwargs) -> tuple[Agent, FakeClient, io.StringIO]:
        emitter = EventEmitter(stdout=out)
        ctx = ToolContext(
            workspace=tmp_path, emitter=emitter, skills=SkillCatalog()
        )
        client = FakeClient(responses)
        agent = Agent(client=client, ctx=ctx, **kwargs)
        return agent, client, out

    return make, out


def _run(agent: Agent) -> RunResult:
    return agent.run("SYS", "TASK")


def _events(out: io.StringIO) -> list[dict]:
    return [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# termination states
# ---------------------------------------------------------------------------


def test_done_terminates_agent_done(harness, tmp_path: Path) -> None:
    make, out = harness
    agent, _, _ = make(
        [
            _response(_assistant(tool_calls=[_tool_call("write_file", {"path": "hi.txt", "content": "hi"}, "c1")])),
            _response(_assistant(tool_calls=[_tool_call("done", {"summary": "wrote file", "status": "success"}, "c2")])),
        ]
    )
    result = _run(agent)
    assert result.terminal_state == "agent_done"
    assert result.iters == 2
    assert result.done_status == "success"
    assert (tmp_path / "hi.txt").read_text() == "hi"

    types = [e["type"] for e in _events(out)]
    assert "done_signal" in types
    assert "terminal_state" in types
    term = next(e for e in _events(out) if e["type"] == "terminal_state")
    assert term["payload"]["terminal_state"] == "agent_done"


def test_identical_tool_calls_three_times_stuck(harness) -> None:
    make, _ = harness
    same = _tool_call("shell", {"command": "ls"}, "c1")
    agent, client, _ = make(
        [_response(_assistant(tool_calls=[same])) for _ in range(3)]
    )
    result = _run(agent)
    assert result.terminal_state == "stuck_loop"
    assert result.iters == 3
    assert len(client.calls) == 3


def test_different_tool_calls_not_stuck(harness) -> None:
    make, _ = harness
    calls = [
        _tool_call("shell", {"command": f"echo {i}"}, f"c{i}") for i in range(4)
    ]
    responses = [_response(_assistant(tool_calls=[c])) for c in calls]
    responses.append(_response(_assistant(tool_calls=[_tool_call("done", {"summary": "ok", "status": "success"}, "cd")])))
    agent, _, _ = make(responses, max_iters=10)
    result = _run(agent)
    assert result.terminal_state == "agent_done"


def test_text_only_streak_stuck(harness) -> None:
    make, _ = harness
    agent, client, _ = make(
        [_response(_assistant(content="thinking out loud")) for _ in range(4)]
    )
    result = _run(agent)
    assert result.terminal_state == "stuck_loop"
    assert result.iters == 4
    # nudges injected at streaks 2 and 3 → visible in requests 3 and 4
    third_req_users = [m for m in client.calls[2]["messages"] if m["role"] == "user"]
    fourth_req_users = [m for m in client.calls[3]["messages"] if m["role"] == "user"]
    assert NUDGE_TEXT_STREAK_2 in third_req_users[-1]["content"]
    assert NUDGE_TEXT_STREAK_3 in fourth_req_users[-1]["content"]


def test_text_then_tools_recovers(harness) -> None:
    make, _ = harness
    agent, _, _ = make(
        [
            _response(_assistant(content="hmm")),
            _response(_assistant(content="still thinking")),
            _response(_assistant(tool_calls=[_tool_call("done", {"summary": "ok", "status": "success"}, "cd")])),
        ],
        max_iters=10,
    )
    result = _run(agent)
    assert result.terminal_state == "agent_done"


def test_max_iters_harness_complete(harness) -> None:
    make, _ = harness
    # distinct args each iter → never identical-signature stuck
    responses = [
        _response(_assistant(tool_calls=[_tool_call("shell", {"command": f"echo {i}"}, f"c{i}")]))
        for i in range(3)
    ]
    agent, _, _ = make(responses, max_iters=3)
    result = _run(agent)
    assert result.terminal_state == "harness_complete"
    assert result.iters == 3


def test_client_exception_error_terminal(harness) -> None:
    make, out = harness
    agent, _, _ = make([RuntimeError("gateway down")])
    result = _run(agent)
    assert result.terminal_state == "error"
    assert "gateway down" in (result.error or "")
    types = [e["type"] for e in _events(out)]
    assert "error" in types


def test_usage_accumulated(harness) -> None:
    make, _ = harness
    agent, _, _ = make(
        [
            _response(_assistant(tool_calls=[_tool_call("done", {"summary": "s", "status": "success"}, "c")])),
        ]
    )
    result = _run(agent)
    assert result.usage["requests"] == 1
    assert result.usage["prompt_tokens"] == 120
    assert result.usage["completion_tokens"] == 20


# ---------------------------------------------------------------------------
# request formation + message history
# ---------------------------------------------------------------------------


def test_request_includes_tools_and_params(harness) -> None:
    make, _ = harness
    agent, client, _ = make(
        [_response(_assistant(tool_calls=[_tool_call("done", {"summary": "s", "status": "success"}, "c")]))]
    )
    _run(agent)
    call = client.calls[0]
    names = [t["function"]["name"] for t in call["tools"]]
    assert names == ["shell", "read_file", "write_file", "use_skill", "done"]
    # messages: system + task
    assert call["messages"][0]["role"] == "system"
    assert call["messages"][1] == {"role": "user", "content": "TASK"}


def test_assistant_message_appended_verbatim(harness) -> None:
    make, _ = harness
    msg = _assistant(
        content=None,
        tool_calls=[_tool_call("shell", {"command": "pwd"}, "c1")],
    )
    msg["extra_field"] = "kept"
    agent, client, _ = make(
        [
            _response(msg),
            _response(_assistant(tool_calls=[_tool_call("done", {"summary": "s", "status": "success"}, "c2")])),
        ]
    )
    _run(agent)
    second_req = client.calls[1]["messages"]
    assistant_msgs = [m for m in second_req if m["role"] == "assistant"]
    assert assistant_msgs[0].get("extra_field") == "kept"
    # tool result appended with matching id
    tool_msgs = [m for m in second_req if m["role"] == "tool"]
    assert tool_msgs[0]["tool_call_id"] == "c1"
    assert json.loads(tool_msgs[0]["content"])["exit_code"] == 0


def test_multi_tool_call_message(harness, tmp_path: Path) -> None:
    make, _ = harness
    agent, client, _ = make(
        [
            _response(
                _assistant(
                    tool_calls=[
                        _tool_call("write_file", {"path": "a.txt", "content": "A"}, "c1"),
                        _tool_call("write_file", {"path": "b.txt", "content": "B"}, "c2"),
                    ]
                )
            ),
            _response(_assistant(tool_calls=[_tool_call("done", {"summary": "s", "status": "success"}, "c3")])),
        ]
    )
    result = _run(agent)
    assert result.terminal_state == "agent_done"
    assert (tmp_path / "a.txt").read_text() == "A"
    assert (tmp_path / "b.txt").read_text() == "B"
    tool_msgs = [m for m in client.calls[1]["messages"] if m["role"] == "tool"]
    assert {m["tool_call_id"] for m in tool_msgs} == {"c1", "c2"}


def test_tool_error_returned_not_raised(harness) -> None:
    make, _ = harness
    agent, _, _ = make(
        [
            _response(_assistant(tool_calls=[_tool_call("read_file", {"path": "missing.txt"}, "c1")])),
            _response(_assistant(tool_calls=[_tool_call("done", {"summary": "s", "status": "success"}, "c2")])),
        ]
    )
    result = _run(agent)
    assert result.terminal_state == "agent_done"


def test_unknown_tool_error_result(harness) -> None:
    make, _ = harness
    agent, client, _ = make(
        [
            _response(_assistant(tool_calls=[_tool_call("frobnicate", {}, "c1")])),
            _response(_assistant(tool_calls=[_tool_call("done", {"summary": "s", "status": "success"}, "c2")])),
        ]
    )
    result = _run(agent)
    assert result.terminal_state == "agent_done"
    tool_msgs = [m for m in client.calls[1]["messages"] if m["role"] == "tool"]
    assert "unknown tool" in tool_msgs[0]["content"]


# ---------------------------------------------------------------------------
# context compaction
# ---------------------------------------------------------------------------


def test_context_compaction_emits_event(harness) -> None:
    make, out = harness
    # num_ctx=200, compact_ratio=0.7 → threshold 140; prompt_tokens=200 each call
    # 10 iterations of distinct read_file calls → >8 tool results → compaction
    responses = [
        _response(
            _assistant(tool_calls=[_tool_call("read_file", {"path": f"f{i}.txt"}, f"c{i}")]),
            prompt_tokens=200,
        )
        for i in range(10)
    ]
    agent, client, _ = make(responses, max_iters=10, num_ctx=200, compact_ratio=0.7)
    result = _run(agent)
    assert result.terminal_state == "harness_complete"

    compact_events = [e for e in _events(out) if e["type"] == "context_compacted"]
    assert compact_events, "expected a context_compacted event"

    # the request after compaction shows compacted tool contents
    last_call_msgs = client.calls[-1]["messages"]
    tool_msgs = [m for m in last_call_msgs if m["role"] == "tool"]
    assert any('"compacted": true' in str(m["content"]) for m in tool_msgs)
    # most recent tool results kept intact
    assert any('"error"' in str(m["content"]) for m in tool_msgs[-3:])


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------


def test_config_from_env_minimal(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("GATEWAY_BASE_URL", "http://gw:8080")
    monkeypatch.setenv("GATEWAY_CRED", "cred")
    monkeypatch.setenv("MODEL_NAME", "qwen3:8b")
    monkeypatch.setenv("TASK_PROMPT", "do it")
    monkeypatch.setenv("WORKSPACE", str(tmp_path))
    cfg = AgentConfig.from_env()
    assert cfg.gateway_base_url == "http://gw:8080"
    assert cfg.model == "qwen3:8b"
    assert cfg.workspace == tmp_path.resolve()
    assert cfg.max_iters == 50
    assert cfg.events_ingest_url is None


def test_config_requires_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GATEWAY_BASE_URL", raising=False)
    monkeypatch.setenv("MODEL_NAME", "m")
    with pytest.raises(ConfigError):
        AgentConfig.from_env()


def test_config_bad_max_iters_defaults(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("GATEWAY_BASE_URL", "http://gw")
    monkeypatch.setenv("MODEL_NAME", "m")
    monkeypatch.setenv("TASK_PROMPT", "t")
    monkeypatch.setenv("WORKSPACE", str(tmp_path))
    monkeypatch.setenv("MAX_ITERS", "banana")
    assert AgentConfig.from_env().max_iters == 50


# ---------------------------------------------------------------------------
# HttpChatClient request formation (MockTransport — still hermetic)
# ---------------------------------------------------------------------------


def test_http_client_request_shape() -> None:
    import httpx

    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"role": "assistant", "content": "ok"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        )

    client = HttpChatClient(
        "http://gw.local:8080/",
        "cred123",
        "qwen3:8b",
        transport=httpx.MockTransport(handler),
    )
    resp = client.chat([{"role": "user", "content": "hi"}], tools=[{"type": "function"}])
    assert resp["choices"][0]["message"]["content"] == "ok"

    assert seen["url"] == "http://gw.local:8080/v1/chat/completions"
    assert seen["auth"] == "Bearer cred123"
    body = seen["body"]
    assert body["model"] == "qwen3:8b"
    assert body["temperature"] == 0.2
    assert body["reasoning_effort"] == "none"
    assert body["num_ctx"] == 32768
    assert body["keep_alive"] == "30m"
    assert body["max_tokens"] == 4096
    assert body["tools"]
    client.close()


# ---------------------------------------------------------------------------
# main() smoke — MINILOOP_RESULT line
# ---------------------------------------------------------------------------


def test_main_prints_miniloop_result(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.setenv("GATEWAY_BASE_URL", "http://gw")
    monkeypatch.setenv("GATEWAY_CRED", "c")
    monkeypatch.setenv("MODEL_NAME", "m")
    monkeypatch.setenv("TASK_PROMPT", "t")
    monkeypatch.setenv("WORKSPACE", str(tmp_path))
    monkeypatch.setenv("SKILLPACKS_ROOT", str(tmp_path / "no-skills"))

    fake = FakeClient(
        [_response(_assistant(tool_calls=[_tool_call("done", {"summary": "s", "status": "success"}, "c")]))]
    )
    monkeypatch.setattr(agent_mod, "HttpChatClient", lambda *a, **k: fake)

    rc = agent_mod.main()
    assert rc == 0
    out = capsys.readouterr().out
    result_lines = [l for l in out.splitlines() if l.startswith("MINILOOP_RESULT ")]
    assert len(result_lines) == 1
    payload = json.loads(result_lines[0][len("MINILOOP_RESULT ") :])
    assert payload["terminal_state"] == "agent_done"
    assert payload["iters"] == 1
