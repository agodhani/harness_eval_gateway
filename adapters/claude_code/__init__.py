"""Claude Code CLI harness adapter (Phase 2.5).

Runs ``claude -p --output-format stream-json --verbose
--dangerously-skip-permissions`` inside ``heg-sandbox-claude`` images.
Claude Code speaks the Anthropic Messages API, which the AI gateway exposes
at ``/v1/messages`` — backed by Ollama's native Anthropic-compatible endpoint
by default (``PROVIDER_ROUTES`` can route it to real Anthropic) — see
docs/p25-harness-adapters.md.
"""
