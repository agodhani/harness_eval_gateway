"""Shared machinery for CLI-harness adapters (Phase 2.5).

External harness CLIs (claude, codex, pi, dsh) don't speak the platform's
normalized event protocol or call the gateway's ``/internal/*`` endpoints.
Each CLI adapter therefore ships a small **shim** baked into its sandbox
image — ``python3 -m adapters.<name>`` — that writes the CLI's native config,
spawns it in its machine-readable mode, translates its event stream, and
reports the terminal state back.

This package is **stdlib-only on purpose**: the CLI sandbox images are
node-based with a bare ``python3`` — no pip installs — so nothing here (or
in the ``__main__``/``translate`` modules it drives) may import third-party
code. Adapter ``adapter.py`` modules run in the orchestrator process, not in
the sandbox, and may use whatever the repo already depends on.
"""
