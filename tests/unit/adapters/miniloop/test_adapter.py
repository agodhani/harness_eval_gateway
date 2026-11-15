"""Tests for the miniloop adapter contract functions."""

import json

from adapters.miniloop import adapter


def test_image_spec() -> None:
    spec = adapter.image_spec()
    assert spec["name"] == "miniloop"
    assert spec["dockerfile"].endswith("sandbox/images/miniloop.Dockerfile")
    assert spec["entrypoint"] == ["python", "-m", "miniloop"]
    assert spec["skillpacks_mount"]["container"] == "/opt/skillpacks"
    assert spec["skillpacks_mount"]["mode"] == "ro"


def test_env_contract() -> None:
    env = adapter.env(
        gateway_url="http://gateway:8080",
        cred="run-cred",
        task_prompt="build a thing",
        model="qwen3:8b",
        workspace="/workspace",
    )
    assert env["GATEWAY_BASE_URL"] == "http://gateway:8080"
    assert env["GATEWAY_CRED"] == "run-cred"
    assert env["MODEL_NAME"] == "qwen3:8b"
    assert env["TASK_PROMPT"] == "build a thing"
    assert env["WORKSPACE"] == "/workspace"
    assert env["SKILLPACKS_ROOT"] == "/opt/skillpacks"
    assert "PYTHONPATH" in env
    # optional vars absent unless provided
    assert "EVENTS_INGEST_URL" not in env
    assert "INGEST_TOKEN" not in env
    assert "MAX_ITERS" not in env
    assert "MAX_TOKENS" not in env


def test_env_optional_vars() -> None:
    env = adapter.env(
        "http://g", "c", "t", "m",
        events_ingest_url="http://g/internal/events",
        ingest_token="tok",
        max_iters=25,
        max_tokens=8192,
    )
    assert env["EVENTS_INGEST_URL"] == "http://g/internal/events"
    assert env["INGEST_TOKEN"] == "tok"
    assert env["MAX_ITERS"] == "25"
    assert env["MAX_TOKENS"] == "8192"


def test_launch_returns_subprocess_spec(monkeypatch) -> None:
    monkeypatch.setenv("GATEWAY_BASE_URL", "http://gw:8080")
    monkeypatch.setenv("GATEWAY_CRED", "cred")
    spec = adapter.launch(
        {
            "prompt": "create hello.txt",
            "model": "qwen3:8b",
            "workspace": "/workspace",
            "events_ingest_url": "http://gw:8080/internal/events",
            "max_iters": 10,
        }
    )
    assert spec["cmd"] == ["python", "-m", "miniloop"]
    assert spec["cwd"] == "/workspace"
    env = spec["env"]
    assert env["GATEWAY_BASE_URL"] == "http://gw:8080"
    assert env["MODEL_NAME"] == "qwen3:8b"
    assert env["TASK_PROMPT"] == "create hello.txt"
    assert env["EVENTS_INGEST_URL"] == "http://gw:8080/internal/events"
    assert env["MAX_ITERS"] == "10"


def test_translate_events_parses_stdout() -> None:
    lines = [
        json.dumps({"ts": "t1", "seq": 1, "source": "harness", "type": "checkpoint", "payload": {"iter": 1}}),
        json.dumps({"ts": "t2", "seq": 2, "source": "harness", "type": "tool_call", "payload": {"name": "shell"}}),
        "MINILOOP_RESULT " + json.dumps({"terminal_state": "agent_done", "iters": 2, "usage": {}}),
        "some non-json noise line",
        "{not json",
        json.dumps([1, 2, 3]),  # valid JSON but not an event dict
        "",
    ]
    events = adapter.translate_events(lines)
    types = [e["type"] for e in events]
    assert types == ["checkpoint", "tool_call", "miniloop_result"]
    assert events[0]["payload"] == {"iter": 1}
    assert events[0]["source"] == "harness"
    assert events[2]["payload"]["terminal_state"] == "agent_done"


def test_translate_events_malformed_result_line() -> None:
    events = adapter.translate_events(["MINILOOP_RESULT {bad json"])
    assert events[0]["type"] == "miniloop_result"
    assert "raw" in events[0]["payload"]


def test_completion_signal() -> None:
    sig = adapter.completion_signal()
    assert sig["result_marker"] == "MINILOOP_RESULT "
    assert "agent_done" in sig["terminal_states"]
    assert sig["exit_codes"]["agent_done"] == 0
    assert sig["exit_codes"]["error"] == 2
