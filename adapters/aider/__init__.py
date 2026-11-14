"""Aider adapter (PR13).

Runs ``aider --yes-always --message "<task>"`` inside ``heg-sandbox-aider``
images. ``aider-chat`` (pip) is the Python pair-programming CLI — a
git-native edit loop rather than a tool-call agent: the model emits edit
blocks that aider applies locally and auto-commits, and may suggest shell
commands (auto-confirmed under ``--yes-always``, which is also how the
``done`` binary gets invoked).

Aider has no machine-readable output mode: ``--no-pretty --no-stream``
yields stable plain-text status lines that the shim's ``translate_text``
hook maps onto the normalized event vocabulary. Model traffic goes through
litellm's ``openai`` provider — ``OPENAI_API_BASE``/``OPENAI_API_KEY`` →
the gateway's ``/v1/chat/completions``. See docs/p25-harness-adapters.md.
"""
