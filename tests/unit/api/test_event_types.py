"""Contract test: every event type miniloop can produce must be a valid
``api.schemas.EventType``.

Why this exists: ``EventEmitter.emit`` accepts any string, and the agent
POSTs each event to the gateway's ``/internal/events`` → control-plane
``/api/events/ingest``, which 422s types outside the enum — the events are
then silently dropped. This test AST-scans ``adapters/miniloop`` for every
event-type literal the package can emit so a new ``emit("...")`` call site
(or a new synthetic type in ``translate_events``) can't land without a
matching enum member.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

from adapters.miniloop import adapter
from api.schemas import EventType

REPO_ROOT = Path(__file__).resolve().parents[3]
MINILOOP_DIR = REPO_ROOT / "adapters" / "miniloop"
ADAPTER_FILENAME = "adapter.py"

VALID_TYPES = {member.value for member in EventType}


def _emit_type_literal(node: ast.Call) -> str | None:
    """Event-type literal of an ``emit(...)`` call, if statically known.

    Covers positional ``emit("type", ...)`` and keyword ``emit(event_type=...)``;
    non-literal arguments return None (not collectable statically).
    """
    if node.args:
        arg = node.args[0]
    else:
        arg = next((kw.value for kw in node.keywords if kw.arg == "event_type"), None)
    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
        return arg.value
    return None


def _is_emit_func(func: ast.expr) -> bool:
    """True for ``x.emit`` attribute funcs and bare ``emit`` names."""
    if isinstance(func, ast.Attribute):
        return func.attr == "emit"
    return isinstance(func, ast.Name) and func.id == "emit"


def _dict_type_literal(node: ast.Dict) -> str | None:
    """``{"type": "<literal>"}`` value of a dict literal, if present."""
    for key, value in zip(node.keys, node.values):
        if (
            isinstance(key, ast.Constant)
            and key.value == "type"
            and isinstance(value, ast.Constant)
            and isinstance(value.value, str)
        ):
            return value.value
    return None


def _miniloop_event_type_literals() -> set[str]:
    """Every event-type string literal ``adapters/miniloop`` can produce.

    Sources:
    - ``.emit("<type>", ...)`` call sites (agent.py, tools.py) — the emitter
      itself (events.py) takes free-form strings and defines no type list.
    - ``{"type": "<literal>"}`` dict entries in adapter.py — covers the
      synthetic ``miniloop_result`` event that ``translate_events`` builds
      for the ``MINILOOP_RESULT`` stdout line. Restricted to adapter.py:
      agent.py's TOOLS schemas also use ``"type"`` keys for JSON Schema
      ("function", "object", ...) which are NOT event types.
    """
    found: set[str] = set()
    for path in sorted(MINILOOP_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _is_emit_func(node.func):
                literal = _emit_type_literal(node)
                if literal is not None:
                    found.add(literal)
            elif path.name == ADAPTER_FILENAME and isinstance(node, ast.Dict):
                literal = _dict_type_literal(node)
                if literal is not None:
                    found.add(literal)
    return found


def test_miniloop_emit_literals_are_valid_event_types() -> None:
    literals = _miniloop_event_type_literals()
    # Sanity: the scan must actually find the known emit sites — an empty or
    # gutted result would make the subset check below vacuous.
    assert {"run_started", "done_signal", "terminal_state"} <= literals
    unknown = literals - VALID_TYPES
    assert not unknown, f"event types missing from EventType: {sorted(unknown)}"


def test_translate_events_output_types_are_valid() -> None:
    """Types on translate_events' normalized output are ingestable too."""
    lines = [
        # Harness JSONL line → passthrough of an emit-site type.
        json.dumps(
            {
                "ts": "2025-01-01T00:00:00+00:00",
                "seq": 1,
                "source": "harness",
                "type": "terminal_state",
                "payload": {"terminal_state": "agent_done"},
            }
        ),
        # MINILOOP_RESULT marker → synthetic adapter event type.
        "MINILOOP_RESULT " + json.dumps({"terminal_state": "agent_done", "iters": 3}),
    ]
    events = adapter.translate_events(lines)
    assert len(events) == 2
    for ev in events:
        assert ev["type"] in VALID_TYPES, f"unknown event type: {ev['type']!r}"
