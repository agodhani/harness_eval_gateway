"""Shared machinery for CLI-harness adapters (Phase 2.5).

External harness CLIs (claude, codex, pi, dsh, aider) don't speak the
platform's normalized event protocol or call the gateway's ``/internal/*``
endpoints. Each CLI adapter therefore ships a small **shim** baked into its
sandbox image — ``python3 -m adapters.<name>`` — that writes the CLI's
native config, spawns it in its machine-readable mode (aider has none —
plain-text lines go through the ``translate_text`` hook), translates its
event stream, and reports the terminal state back.

This package is **stdlib-only on purpose**: the npm-CLI sandbox images are
node-based with a bare ``python3`` — no pip installs — and aider's
``python:slim`` image keeps the same constraint so the shim stays
dependency-free; nothing here (or in the ``__main__``/``translate``
modules it drives) may import third-party code. Adapter ``adapter.py``
modules run in the orchestrator process, not in the sandbox, and may use
whatever the repo already depends on.
"""
