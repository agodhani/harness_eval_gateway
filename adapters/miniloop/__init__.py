"""miniloop — minimal coding-agent harness for harness_eval_gateway.

A small tool-call loop (shell, read_file, write_file, use_skill, done) that
runs against an OpenAI-compatible chat-completions endpoint — the AI gateway
in production, any compatible server (e.g. host Ollama) for local iteration.

Entry points:
  - ``python -m miniloop``            (requires ``adapters/`` on sys.path)
  - ``python -m adapters.miniloop``   (from the repo root)

Required env vars: ``GATEWAY_BASE_URL``, ``GATEWAY_CRED``, ``MODEL_NAME``,
``TASK_PROMPT``. Optional: ``WORKSPACE`` (default ``.``), ``EVENTS_INGEST_URL``,
``INGEST_TOKEN``, ``MAX_ITERS`` (default 50), ``SKILLPACKS_ROOT``
(default ``/opt/skillpacks``).
"""

__version__ = "0.1.0"
