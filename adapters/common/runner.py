"""Shared shim runner for CLI-harness adapters (Phase 2.5).

One ``run_shim`` loop serves every CLI adapter:

1. ``ShimConfig.from_env`` reads the common sandbox env contract
   (``GATEWAY_BASE_URL`` / ``GATEWAY_CRED`` / ``MODEL_NAME`` / ``TASK_PROMPT``
   / ``WORKSPACE`` / ``SKILLPACKS_ROOT`` / ``EVENTS_INGEST_URL`` /
   ``SYSTEM_PROMPT_EXTRA``).
2. An adapter ``setup`` hook writes the CLI's native config
   (codex ``config.toml``, pi ``models.json``, claude ``~/.claude.json``,
   dsh ``cordis.patch.yml``).
3. The CLI is spawned in its JSONL machine mode (cwd = workspace, stderr
   merged into stdout so docker logs keep one ordered transcript).
4. Every stdout line is echoed to the shim's stderr (raw transcript) and
   JSON-parsed; ``translate(obj)`` maps native records to normalized event
   dicts which are printed as harness JSONL **and** POSTed to
   ``EVENTS_INGEST_URL`` (the gateway's ``/internal/events``, cred auth —
   run/sandbox ids are stamped server-side). Lines that are not JSON
   objects go through the optional ``translate_text`` hook — plain-text
   CLIs like aider have no JSONL mode, so their status lines
   (``Applied edit to``, ``Tokens:``, ``Commit``…) are mapped
   heuristically instead.
5. On process exit the shim emits ``terminal_state`` and prints
   ``HEG_RESULT {json}`` — the marker ``parse_terminal_state`` scans for in
   captured logs.

Stdlib-only (see package docstring): urllib for ingest, subprocess for the
CLI, json for everything else.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import urllib.request
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, TextIO

#: stdout marker line the orchestrator's parse_terminal_state scans for.
RESULT_MARKER = "HEG_RESULT "

#: Terminal states a shim can report (same vocabulary as miniloop's).
TERMINAL_STATES = ("agent_done", "harness_complete", "stuck_loop", "error")

#: Cap for free-text fields copied into event payloads (output snippets,
#: assistant text) — keeps the event stream and artifacts readable.
MAX_SNIPPET = 4000

SOURCE = "harness"

#: Normalized-event producer: native parsed record -> normalized event dicts.
TranslateFn = Callable[[dict[str, Any]], list[dict[str, Any]]]

#: Plain-text-line translator for CLIs with no machine-readable mode
#: (aider) — one stdout line -> normalized event dicts.
TranslateTextFn = Callable[[str], list[dict[str, Any]]]

#: Adapter-provided config writer, run before the CLI is spawned.
SetupFn = Callable[["ShimConfig"], None]


@dataclass
class ShimConfig:
    """The common env contract every CLI-harness sandbox gets."""

    gateway_url: str
    cred: str
    model: str
    task_prompt: str
    workspace: str = "/workspace"
    skillpacks_root: str = "/opt/skillpacks"
    ingest_url: str | None = None
    system_prompt_extra: str | None = None
    env: dict[str, str] = field(default_factory=dict)

    @property
    def home(self) -> str:
        return self.env.get("HOME") or "/root"

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> ShimConfig:
        e = dict(os.environ if environ is None else environ)
        return cls(
            gateway_url=(e.get("GATEWAY_BASE_URL") or "").rstrip("/"),
            cred=e.get("GATEWAY_CRED") or "",
            model=e.get("MODEL_NAME") or "",
            task_prompt=e.get("TASK_PROMPT") or "",
            workspace=e.get("WORKSPACE") or "/workspace",
            skillpacks_root=e.get("SKILLPACKS_ROOT") or "/opt/skillpacks",
            ingest_url=e.get("EVENTS_INGEST_URL") or None,
            system_prompt_extra=e.get("SYSTEM_PROMPT_EXTRA") or None,
            env=e,
        )


class EventSink:
    """Emits normalized harness events: stdout JSONL + best-effort ingest POST.

    The stdout stream is the transcript channel (``docker logs`` →
    ``transcript.log``); the HTTP channel powers the live timeline via the
    gateway's ``/internal/events``. Ingest failures are counted, never
    raised — telemetry must never crash the agent.
    """

    def __init__(
        self,
        ingest_url: str | None,
        cred: str,
        *,
        stdout: TextIO | None = None,
        ingest_timeout: float = 5.0,
    ) -> None:
        self.ingest_url = ingest_url.rstrip("/") if ingest_url else None
        self._cred = cred
        self._stdout = stdout if stdout is not None else sys.stdout
        self._seq = 0
        self.ingest_failures = 0
        self.events_emitted = 0
        self._ingest_timeout = ingest_timeout

    def emit(
        self, event_type: str, payload: dict[str, Any], *, source: str = SOURCE
    ) -> dict[str, Any]:
        self._seq += 1
        event = {
            "ts": datetime.now(UTC).isoformat(),
            "seq": self._seq,
            "source": source,
            "type": event_type,
            "payload": payload,
        }
        self.events_emitted += 1
        line = json.dumps(event, ensure_ascii=False, default=str)
        print(line, file=self._stdout, flush=True)
        self._post(event)
        return event

    def _post(self, event: dict[str, Any]) -> None:
        if not self.ingest_url:
            return
        try:
            body = json.dumps(event, default=str).encode("utf-8")
            req = urllib.request.Request(
                self.ingest_url,
                data=body,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self._cred}",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=self._ingest_timeout) as resp:
                if resp.status >= 400:
                    self.ingest_failures += 1
        except Exception:  # noqa: BLE001 — telemetry must never crash the agent
            self.ingest_failures += 1


def staged_skillpacks(root: str) -> list[str]:
    """Directory names under the mounted skill-pack root (staged selection)."""
    try:
        return sorted(
            name
            for name in os.listdir(root)
            if os.path.isdir(os.path.join(root, name))
        )
    except OSError:
        return []


def build_task_text(cfg: ShimConfig, *, extra_inline: bool = True) -> str:
    """Task prompt + harness-environment footer.

    The footer is how generic CLIs learn the sandbox conventions they have no
    native flag for: the workspace boundary, staged skill packs, and the
    ``done`` signalling binary. ``system_prompt_extra`` is appended as its
    own section when ``extra_inline`` (harnesses with a native
    ``--append-system-prompt`` pass it there instead and call this with
    ``extra_inline=False``).
    """
    parts = [cfg.task_prompt.strip()]
    footer = [
        "",
        "[sandbox environment]",
        f"- Your working directory is {cfg.workspace}; keep all work inside it.",
    ]
    packs = staged_skillpacks(cfg.skillpacks_root)
    if packs:
        footer.append(
            "- Skill packs are staged under "
            f"{cfg.skillpacks_root} ({', '.join(packs)}); read the relevant "
            "SKILL.md files and apply them when useful."
        )
    footer.append(
        "- When the task is fully complete, signal completion by running "
        '`/usr/local/bin/done "<short summary>"` — IMPORTANT: `done` is a '
        "shell keyword in sh/bash, so you MUST invoke the binary by its full "
        "path /usr/local/bin/done (or `command done`); a bare `done ...` "
        "fails with a syntax error. Then stop."
    )
    parts.append("\n".join(footer))
    if extra_inline and cfg.system_prompt_extra:
        parts.append("\n[additional instructions]\n" + cfg.system_prompt_extra.strip())
    return "\n".join(parts)


def _iter_json_objects(lines: Iterable[str]) -> Iterable[dict[str, Any]]:
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            yield obj


def translate_lines(
    lines: Iterable[str], translate: TranslateFn
) -> list[dict[str, Any]]:
    """Parse + translate every JSONL line — the test-time entry point."""
    events: list[dict[str, Any]] = []
    for obj in _iter_json_objects(lines):
        events.extend(translate(obj))
    return events


def _terminal_of(events: Iterable[dict[str, Any]]) -> str | None:
    """Last ``terminal_state`` event's state, if any."""
    state: str | None = None
    for ev in events:
        if ev.get("type") == "terminal_state":
            payload = ev.get("payload")
            if isinstance(payload, dict):
                s = payload.get("terminal_state")
                if isinstance(s, str) and s:
                    state = s
    return state


def run_shim(
    *,
    harness: str,
    cfg: ShimConfig,
    argv: list[str],
    translate: TranslateFn,
    translate_text: TranslateTextFn | None = None,
    sink: EventSink | None = None,
    setup: SetupFn | None = None,
    env: Mapping[str, str] | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    """Spawn the CLI, stream-translate its events, report the terminal state.

    Returns the shim's exit code (0 normally, 2 on shim/setup failures). The
    CLI's raw stdout is echoed to ``stderr`` so ``docker logs`` keeps both
    channels; normalized events go to ``stdout`` as JSONL.

    ``translate`` handles JSON-object lines; when a line is not a JSON
    object the optional ``translate_text`` handles it (plain-text harnesses
    like aider — pass both so ``adapters.<name>.translate_events`` and the
    live loop share the same folding).
    """
    out = stdout if stdout is not None else sys.stdout
    err = stderr if stderr is not None else sys.stderr
    if sink is None:
        sink = EventSink(cfg.ingest_url, cfg.cred, stdout=out)

    result_state: str | None = None
    exit_code = 0

    def _finish(state: str, *, note: str | None = None) -> None:
        payload: dict[str, Any] = {
            "terminal_state": state,
            "harness": harness,
            "exit_code": exit_code,
        }
        if note:
            payload["note"] = note
        sink.emit("terminal_state", payload)
        print(RESULT_MARKER + json.dumps(payload, ensure_ascii=False), file=out, flush=True)

    sink.emit(
        "run_started",
        {"harness": harness, "model": cfg.model, "workspace": cfg.workspace},
    )

    try:
        if setup is not None:
            setup(cfg)
    except Exception as exc:  # noqa: BLE001 — config failure is fatal, report it
        exit_code = 2
        sink.emit(
            "error",
            {"stage": "setup", "message": f"{type(exc).__name__}: {exc}"},
        )
        _finish("error", note="adapter setup failed")
        return exit_code

    try:
        proc = subprocess.Popen(
            argv,
            cwd=cfg.workspace,
            env=dict(env) if env is not None else None,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
    except OSError as exc:
        exit_code = 2
        sink.emit(
            "error",
            {"stage": "spawn", "message": f"{type(exc).__name__}: {exc}", "argv": argv[:1]},
        )
        _finish("error", note="harness binary not runnable")
        return exit_code

    def _sigterm(signum: int, _frame: Any) -> None:
        # docker stop → SIGTERM: forward to the CLI so the container can exit;
        # the monitor's verdict covers the rest (policy kills need no marker).
        try:
            proc.terminate()
        except OSError:
            pass
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, _sigterm)

    def _emit_events(events: Iterable[dict[str, Any]]) -> None:
        nonlocal result_state
        for ev in events:
            etype = ev.get("type")
            if not isinstance(etype, str) or not etype:
                continue
            payload = ev.get("payload")
            sink.emit(
                etype,
                payload if isinstance(payload, dict) else {"value": payload},
            )
            if etype == "terminal_state" and isinstance(payload, dict):
                s = payload.get("terminal_state")
                if isinstance(s, str) and s:
                    result_state = s

    def _translate_error(exc: Exception) -> list[dict[str, Any]]:
        # A bad record must not kill the run — surface it as an error event.
        return [
            {
                "type": "error",
                "payload": {
                    "stage": "translate",
                    "message": f"{type(exc).__name__}: {exc}",
                },
            }
        ]

    assert proc.stdout is not None
    try:
        for line in proc.stdout:
            # Raw transcript channel — stderr so stdout stays machine-clean.
            print(line.rstrip("\n"), file=err, flush=True)
            objs = list(_iter_json_objects([line]))
            if objs:
                for obj in objs:
                    try:
                        events = translate(obj)
                    except Exception as exc:  # noqa: BLE001
                        events = _translate_error(exc)
                    _emit_events(events)
            elif translate_text is not None:
                try:
                    events = translate_text(line.rstrip("\n"))
                except Exception as exc:  # noqa: BLE001
                    events = _translate_error(exc)
                _emit_events(events)
    finally:
        try:
            proc.stdout.close()
        except OSError:
            pass
    exit_code = proc.wait()

    if result_state is None:
        result_state = "harness_complete" if exit_code == 0 else "error"
    _finish(result_state)
    # Exit-code convention matches miniloop's adapter: 0 for any non-error
    # terminal state, 2 for error. HEG_RESULT is the authoritative channel.
    return 2 if result_state == "error" else 0


def parse_heg_result(logs: str) -> str | None:
    """``terminal_state`` from the LAST ``HEG_RESULT {json}`` line, or None."""
    state: str | None = None
    for line in logs.splitlines():
        line = line.strip()
        if not line.startswith(RESULT_MARKER):
            continue
        try:
            payload = json.loads(line[len(RESULT_MARKER) :])
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            s = payload.get("terminal_state")
            if isinstance(s, str) and s:
                state = s
    return state


def translate_heg_marker(lines: Iterable[str]) -> list[dict[str, Any]]:
    """Fold ``HEG_RESULT`` lines into ``terminal_state`` events (adapter-side).

    Adapter ``translate_events`` implementations combine
    :func:`translate_lines` (native JSONL) with this helper so captured
    transcripts that include the marker yield a terminal event.
    """
    out: list[dict[str, Any]] = []
    for raw in lines:
        line = raw.strip()
        if not line.startswith(RESULT_MARKER):
            continue
        try:
            payload = json.loads(line[len(RESULT_MARKER) :])
        except json.JSONDecodeError:
            payload = {"raw": line}
        if isinstance(payload, dict):
            out.append({"type": "terminal_state", "payload": payload})
    return out
