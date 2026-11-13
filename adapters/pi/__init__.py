"""Pi coding-agent harness adapter (Phase 2.5).

Runs ``pi --mode json`` inside ``heg-sandbox-pi`` images. Pi
(``@mariozechner/pi-coding-agent``, badlogic/pi-mono) supports
OpenAI-compatible providers via ``~/.pi/agent/models.json`` — the shim
declares a ``heg`` provider pointing at the gateway's
``/v1/chat/completions``. See docs/p25-harness-adapters.md.
"""
