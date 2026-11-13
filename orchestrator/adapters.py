"""Harness-adapter registry (F2 + Phase 2.5).

Maps the ``adapter`` name in a run's harness config to the adapter module
implementing the AGENTS.md contract (``image_spec``, ``env``, ``launch``,
``translate_events``, ``completion_signal``, plus orchestrator-facing
``parse_terminal_state``).

Phase 2.5 adds real harness CLIs beside miniloop — ``claude_code``, ``codex``,
``pi``, ``deepseek`` — each a shim'd sandbox image plus a module-level
``MANIFEST`` dict powering ``GET /api/harnesses`` and the submit form's
adapter dropdown.
"""

from __future__ import annotations

from types import ModuleType
from typing import Any

from adapters.claude_code import adapter as claude_code_adapter
from adapters.codex import adapter as codex_adapter
from adapters.deepseek import adapter as deepseek_adapter
from adapters.miniloop import adapter as miniloop_adapter
from adapters.pi import adapter as pi_adapter

ADAPTERS: dict[str, ModuleType] = {
    "miniloop": miniloop_adapter,
    "claude_code": claude_code_adapter,
    "codex": codex_adapter,
    "pi": pi_adapter,
    "deepseek": deepseek_adapter,
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


def adapter_image(adapter: ModuleType, fallback: str) -> str:
    """The docker image tag to spawn for ``adapter``.

    Per-adapter ``image_spec()["image"]`` wins; adapters that don't declare
    one (miniloop) fall back to ``settings.SANDBOX_IMAGE`` — keeping the
    pre-Phase-2.5 override path (evaluator + tests) intact.
    """
    image_spec = getattr(adapter, "image_spec", None)
    if image_spec is None:
        return fallback
    try:
        spec = image_spec()
    except Exception:  # noqa: BLE001 — a broken adapter degrades to the default
        return fallback
    image = spec.get("image") if isinstance(spec, dict) else None
    return str(image) if image else fallback


def adapter_manifest(adapter: ModuleType, name: str) -> dict[str, Any]:
    """The adapter's ``MANIFEST`` dict (or a minimal stub when undeclared)."""
    manifest = getattr(adapter, "MANIFEST", None)
    if isinstance(manifest, dict):
        out = dict(manifest)
        out.setdefault("name", name)
        return out
    return {"name": name}


def harnesses() -> list[dict[str, Any]]:
    """Manifest list for ``GET /api/harnesses`` — registered adapter order."""
    return [adapter_manifest(ADAPTERS[name], name) for name in adapter_names()]


def parse_terminal_state(adapter: ModuleType, logs: str) -> str | None:
    """Adapter-agnostic terminal-state extraction from container logs.

    Falls back to ``None`` for adapters without ``parse_terminal_state``.
    """
    fn = getattr(adapter, "parse_terminal_state", None)
    if fn is None:
        return None
    result: Any = fn(logs)
    return result if isinstance(result, str) else None
