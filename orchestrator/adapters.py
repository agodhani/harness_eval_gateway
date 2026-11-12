"""Harness-adapter registry (F2).

Maps the ``adapter`` name in a run's harness config to the adapter module
implementing the AGENTS.md contract (``env``, ``launch``, ``translate_events``,
``completion_signal``, plus orchestrator-facing ``parse_terminal_state``).
"""

from __future__ import annotations

from types import ModuleType
from typing import Any

from adapters.miniloop import adapter as miniloop_adapter

ADAPTERS: dict[str, ModuleType] = {
    "miniloop": miniloop_adapter,
}


def get_adapter(name: str) -> ModuleType:
    """Return the adapter module for ``name``; raises KeyError if unknown."""
    try:
        return ADAPTERS[name]
    except KeyError:
        raise KeyError(
            f"unknown harness adapter {name!r} (known: {sorted(ADAPTERS)})"
        ) from None


def adapter_names() -> list[str]:
    return sorted(ADAPTERS)


def parse_terminal_state(adapter: ModuleType, logs: str) -> str | None:
    """Adapter-agnostic terminal-state extraction from container logs.

    Falls back to ``None`` for adapters without ``parse_terminal_state``.
    """
    fn = getattr(adapter, "parse_terminal_state", None)
    if fn is None:
        return None
    result: Any = fn(logs)
    return result if isinstance(result, str) else None
