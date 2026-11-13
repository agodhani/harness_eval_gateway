"""Contract test for every registered harness adapter (Phase 2.5).

Two guarantees for every adapter in ``orchestrator.adapters.ADAPTERS``:

1. The AGENTS.md contract surface exists and returns sane shapes:
   ``image_spec`` (with an existing Dockerfile for CLI adapters), ``env``,
   ``launch``, ``translate_events``, ``parse_terminal_state``,
   ``completion_signal``.
2. Every event type any adapter package can emit is a valid
   ``api.schemas.EventType`` — ingest 422s unknown types and drops the
   events silently. The AST scan covers ``emit("<type>")`` calls
   (runner.py) and ``{"type": "<literal>", "payload": ...}`` dicts
   (translate.py modules).
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from api.schemas import EventType
from orchestrator import adapters as orch

REPO_ROOT = Path(__file__).resolve().parents[3]
ADAPTERS_DIR = REPO_ROOT / "adapters"
VALID_TYPES = {member.value for member in EventType}

#: Every adapter registered with the orchestrator.
REGISTERED = orch.adapter_names()
CLI_ADAPTERS = [n for n in REGISTERED if n != "miniloop"]


# ---------------------------------------------------------------------------
# AST event-type scan
# ---------------------------------------------------------------------------


def _emit_type_literal(node: ast.Call) -> str | None:
    if node.args:
        arg = node.args[0]
    else:
        arg = next((kw.value for kw in node.keywords if kw.arg == "event_type"), None)
    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
        return arg.value
    return None


def _is_emit_func(func: ast.expr) -> bool:
    if isinstance(func, ast.Attribute):
        return func.attr == "emit"
    return isinstance(func, ast.Name) and func.id == "emit"


def _event_dict_type_literal(node: ast.Dict) -> str | None:
    """``"type"`` literal of a ``{"type": ..., "payload": ...}`` dict."""
    keys = {k.value for k in node.keys if isinstance(k, ast.Constant)}
    if "payload" not in keys:
        return None
    for key, value in zip(node.keys, node.values):
        if (
            isinstance(key, ast.Constant)
            and key.value == "type"
            and isinstance(value, ast.Constant)
            and isinstance(value.value, str)
        ):
            return value.value
    return None


def _adapter_event_type_literals(package_dir: Path) -> set[str]:
    found: set[str] = set()
    for path in sorted(package_dir.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _is_emit_func(node.func):
                literal = _emit_type_literal(node)
                if literal is not None:
                    found.add(literal)
            elif isinstance(node, ast.Dict):
                literal = _event_dict_type_literal(node)
                if literal is not None:
                    found.add(literal)
    return found


@pytest.mark.parametrize("name", REGISTERED)
def test_event_type_literals_are_valid(name: str) -> None:
    literals = _adapter_event_type_literals(ADAPTERS_DIR / name)
    assert literals, f"adapter {name}: scan found no event-type literals"
    unknown = literals - VALID_TYPES
    assert not unknown, (
        f"adapter {name}: event types missing from EventType: {sorted(unknown)}"
    )


def test_common_runner_emit_literals_are_valid() -> None:
    literals = _adapter_event_type_literals(ADAPTERS_DIR / "common")
    assert {"run_started", "error", "terminal_state"} <= literals
    unknown = literals - VALID_TYPES
    assert not unknown, (
        f"adapters/common: event types missing from EventType: {sorted(unknown)}"
    )


# ---------------------------------------------------------------------------
# contract surface
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", REGISTERED)
def test_contract_surface(name: str) -> None:
    adapter = orch.get_adapter(name)
    for fn in (
        "image_spec",
        "env",
        "launch",
        "translate_events",
        "completion_signal",
        "parse_terminal_state",
    ):
        assert callable(getattr(adapter, fn, None)), f"{name}: missing {fn}"


@pytest.mark.parametrize("name", CLI_ADAPTERS)
def test_cli_image_spec(name: str) -> None:
    spec = orch.get_adapter(name).image_spec()
    assert spec["name"] == name
    assert spec["image"].startswith("heg-sandbox-")
    # The Dockerfile the manifest points at must exist in the repo.
    assert (REPO_ROOT / spec["dockerfile"]).is_file()
    assert spec["entrypoint"] == ["python3", "-m", f"adapters.{name}"]


@pytest.mark.parametrize("name", CLI_ADAPTERS)
def test_cli_env_common_contract(name: str) -> None:
    env = orch.get_adapter(name).env(
        "http://gateway:8080",
        "heg.test-cred",
        "build a thing",
        "qwen2.5-coder:3b",
        events_ingest_url="http://gateway:8080/internal/events",
        max_iters=50,
        system_prompt_extra="be terse",
    )
    assert env["GATEWAY_BASE_URL"] == "http://gateway:8080"
    assert env["GATEWAY_CRED"] == "heg.test-cred"
    assert env["MODEL_NAME"] == "qwen2.5-coder:3b"
    assert env["TASK_PROMPT"] == "build a thing"
    assert env["WORKSPACE"] == "/workspace"
    assert env["EVENTS_INGEST_URL"] == "http://gateway:8080/internal/events"
    assert env["SYSTEM_PROMPT_EXTRA"] == "be terse"
    assert env["PYTHONPATH"] == "/opt/harness"


@pytest.mark.parametrize("name", CLI_ADAPTERS)
def test_cli_launch(name: str) -> None:
    launch = orch.get_adapter(name).launch(
        {
            "task_prompt": "do it",
            "model": "m",
            "gateway_url": "http://gateway:8080",
            "cred": "heg.x",
        }
    )
    assert launch["cmd"] == ["python3", "-m", f"adapters.{name}"]
    assert launch["cwd"] == "/workspace"
    assert launch["env"]["GATEWAY_CRED"] == "heg.x"


@pytest.mark.parametrize("name", CLI_ADAPTERS)
def test_cli_parse_terminal_state(name: str) -> None:
    adapter = orch.get_adapter(name)
    logs = (
        'noise line\nHEG_RESULT {"terminal_state": "harness_complete", '
        '"exit_code": 0}'
    )
    assert adapter.parse_terminal_state(logs) == "harness_complete"
    assert adapter.parse_terminal_state("no markers here") is None


@pytest.mark.parametrize("name", CLI_ADAPTERS)
def test_cli_completion_signal(name: str) -> None:
    signal = orch.get_adapter(name).completion_signal()
    assert signal["mechanism"] == "stdout_marker"
    assert signal["result_marker"] == "HEG_RESULT "
    assert {"agent_done", "harness_complete", "error"} <= set(
        signal["terminal_states"]
    )


@pytest.mark.parametrize("name", CLI_ADAPTERS)
def test_cli_translate_events_types_are_valid(name: str) -> None:
    """translate_events output types must all be ingestable EventTypes."""
    adapter = orch.get_adapter(name)
    events = adapter.translate_events(
        [
            "not json",
            json.dumps({"type": "no_such_record"}),
            'HEG_RESULT {"terminal_state": "harness_complete"}',
        ]
    )
    assert events, f"{name}: translate_events produced nothing"
    for ev in events:
        assert ev["type"] in VALID_TYPES, f"{name}: bad type {ev['type']!r}"


def test_adapter_image_resolution() -> None:
    """adapter_image: CLI adapters declare their image; miniloop falls back."""
    for name in CLI_ADAPTERS:
        image = orch.adapter_image(orch.get_adapter(name), "fallback:latest")
        assert image == orch.get_adapter(name).image_spec()["image"]
    assert (
        orch.adapter_image(orch.get_adapter("miniloop"), "heg-sandbox:latest")
        == "heg-sandbox:latest"
    )


def test_harnesses_manifests() -> None:
    manifests = {m["name"]: m for m in orch.harnesses()}
    assert set(manifests) == set(REGISTERED)
    for name in CLI_ADAPTERS:
        m = manifests[name]
        for key in ("display_name", "llm_protocol", "image", "requires"):
            assert m.get(key), f"{name} manifest missing {key}"
