"""Hook receiver for CLI-harness native hooks/plugins (PR17).

Harnesses with a real hook/plugin surface report agent-level detail that
stdout translation alone cannot reach. Each adapter registers this module as
the hook command so every mechanism lands on the same normalized-event sink:

- ``claude_code`` — ``~/.claude/settings.json`` ``hooks`` command entries
  (``PostToolUse`` → ``tool_result``; ``SessionStart``/``SessionEnd``/
  ``Stop``/``SubagentStop``/``Notification``/``PreCompact`` → ``checkpoint``)
  pipe one JSON payload on stdin per firing.
- ``codex`` — ``notify = [...]`` in ``config.toml`` runs the receiver once
  per ``agent-turn-complete`` with the JSON appended as the last argv token
  (NOT stdin — the docs get this backwards constantly).
- ``pi`` — a generated extension (``extensions/heg-events.ts``) forwards
  ``tool_execution_end`` → ``tool_result`` via ``execFileSync`` with the
  payload on argv.

How events flow:

1. The CLI invokes ``python3 -m adapters.common.hooks <adapter> [payload]``.
2. :func:`parse_payload` reads the JSON from argv (codex/pi) or stdin
   (claude) — argv wins when both could exist.
3. The adapter's ``translate_hook`` (sibling of ``translate_obj`` in each
   ``adapters/<name>/translate.py``) maps the native payload to normalized
   event dicts — the same shape ``translate_events`` produces, so the event
   contract test's AST scan covers hook types too.
4. ``EventSink`` emits each event tagged ``via: "hook"`` — the ingest POST
   to ``EVENTS_INGEST_URL`` is the durable channel; the JSONL mirror goes to
   **stderr**, never stdout, because claude feeds ``SessionStart`` /
   ``UserPromptSubmit`` hook stdout back into the agent's context.

The receiver ALWAYS exits 0 — telemetry must never block or break the agent
(exit 2 refuses claude tool calls; pi's ``execFileSync`` throws on non-zero).

Stdlib-only (runs inside the sandbox).
"""

from __future__ import annotations

import importlib
import json
import os
import select
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any, TextIO

from adapters.common.runner import EventSink, ShimConfig

#: Stdin read bound — claude writes then closes, but a pipe held open (or an
#: argv-only caller that also leaves stdin open) must not hang the hook.
_STDIN_READ_TIMEOUT = 2.0
_STDIN_READ_CAP = 1 << 20  # 1 MiB — generous; payloads carry at most a tool result

#: Normalized-event producer for one native hook payload — same contract as
#: the stream translators in each adapter's translate.py.
HookTranslateFn = Callable[[dict[str, Any]], list[dict[str, Any]]]


def translate_hook_fn(adapter: str) -> HookTranslateFn | None:
    """The adapter's ``translate_hook``, or None when it supports no hooks."""
    try:
        module = importlib.import_module(f"adapters.{adapter}.translate")
    except ImportError:
        return None
    fn = getattr(module, "translate_hook", None)
    return fn if callable(fn) else None


def _json_dict(raw: str) -> dict[str, Any] | None:
    try:
        obj = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    return obj if isinstance(obj, dict) else None


def _read_stdin(stdin: TextIO) -> str:
    """Bounded stdin read with a deadline — empty when nothing is piped in.

    ``select`` gates each ``os.read`` so a caller that never writes stdin
    (codex notify, pi forwarder) doesn't hang, and looping covers payloads
    larger than the pipe buffer without blocking for EOF like
    ``stdin.read()`` would.
    """
    try:
        fileno = stdin.fileno()
    except (OSError, AttributeError):
        fileno = None
    if fileno is None:
        # Non-fd streams (StringIO in tests): read what is buffered.
        try:
            return stdin.read(_STDIN_READ_CAP) or ""
        except OSError:
            return ""
    chunks: list[bytes] = []
    total = 0
    deadline = time.monotonic() + _STDIN_READ_TIMEOUT
    while total < _STDIN_READ_CAP:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            ready, _, _ = select.select([stdin], [], [], remaining)
        except (OSError, ValueError):
            break
        if not ready:
            break
        try:
            chunk = os.read(fileno, min(_STDIN_READ_CAP - total, 65536))
        except OSError:
            break
        if not chunk:  # EOF
            break
        chunks.append(chunk)
        total += len(chunk)
    return b"".join(chunks).decode("utf-8", "replace")


def parse_payload(
    argv_tail: Sequence[str], stdin_text: str | None = None, *, stdin: TextIO | None = None
) -> dict[str, Any] | None:
    """Hook JSON from argv (codex notify / pi forwarder) or stdin (claude).

    ``argv_tail`` is scanned right-to-left for the first dict-JSON token —
    callers like codex append the payload after any configured argv.
    """
    for raw in reversed(list(argv_tail)):
        obj = _json_dict(raw)
        if obj is not None:
            return obj
    if stdin_text is None:
        stdin_text = _read_stdin(stdin if stdin is not None else sys.stdin)
    return _json_dict(stdin_text)


def _error_events(adapter: str, exc: BaseException) -> list[dict[str, Any]]:
    return [
        {
            "type": "error",
            "payload": {
                "stage": "hook",
                "harness": adapter,
                "message": f"{type(exc).__name__}: {exc}",
            },
        }
    ]


def run_hook(
    adapter: str,
    payload: Mapping[str, Any],
    *,
    cfg: ShimConfig | None = None,
    sink: EventSink | None = None,
) -> int:
    """Normalize + emit one native hook payload. Always returns 0.

    ``via: "hook"`` is stamped into every emitted payload so the timeline and
    analytics can tell hook-sourced events from translated-stream ones.
    """
    if cfg is None:
        cfg = ShimConfig.from_env()
    if sink is None:
        # Mirror to stderr — stdout must stay clean: claude injects
        # SessionStart/UserPromptSubmit hook stdout into the agent's context.
        sink = EventSink(cfg.ingest_url, cfg.cred, stdout=sys.stderr)

    translate = translate_hook_fn(adapter)
    if translate is None:
        print(
            f"heg-hook: adapter {adapter!r} has no translate_hook; dropping payload",
            file=sys.stderr,
        )
        return 0
    try:
        events = translate(dict(payload))
    except Exception as exc:  # noqa: BLE001 — a bad payload must not kill the run
        events = _error_events(adapter, exc)

    for ev in events:
        etype = ev.get("type")
        if not isinstance(etype, str) or not etype:
            continue
        body = ev.get("payload")
        body = {**body, "via": "hook"} if isinstance(body, dict) else {"value": body, "via": "hook"}
        sink.emit(etype, body)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """``python3 -m adapters.common.hooks <adapter> [payload-json]``."""
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        print("usage: python3 -m adapters.common.hooks <adapter> [payload-json]", file=sys.stderr)
        return 0
    adapter, tail = args[0], args[1:]
    payload = parse_payload(tail)
    if payload is None:
        print(f"heg-hook: no JSON payload for adapter {adapter!r} (argv or stdin)", file=sys.stderr)
        return 0
    return run_hook(adapter, payload)


if __name__ == "__main__":
    sys.exit(main())
