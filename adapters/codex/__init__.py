"""OpenAI Codex CLI harness adapter (Phase 2.5).

Runs ``codex exec --json`` inside ``heg-sandbox-codex`` images. Codex speaks
the OpenAI **Responses** API (``wire_api = "chat"`` was removed upstream in
Feb 2026), which the AI gateway exposes at ``/v1/responses`` — backed by
Ollama's native implementation by default (``PROVIDER_ROUTES`` can route it
to real OpenAI) — see docs/p25-harness-adapters.md.
"""
