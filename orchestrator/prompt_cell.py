"""PR25 — prompt-only pseudo-adapter cells (zero-container matrix cells).

A matrix cell whose harness config selects ``adapter="prompt"`` runs ONE
chat-completions call through the gateway instead of a containerized agent.
Everything around that call stays identical to a container cell: the same
``Sandbox`` row lifecycle, the same sandbox-scoped gateway credential (so
budgets, metering, and ``llm_request``/``llm_response`` event capture are
all automatic), the same artifact dir convention, the same terminal-state
machine, and the same evaluator/results pipeline — the only thing skipped
is Docker itself.

Flow of :func:`run_prompt_cell` (the ``spawn_one`` counterpart the
lifecycle routes to):

1. Insert the Sandbox row (``provisioning``) and mint a sandbox-scoped
   cred — same helpers ``spawn_one`` uses.
2. ``running`` + orchestrator-side ``run_started`` (there is no in-sandbox
   agent to emit it).
3. ``POST {GATEWAY_URL}/v1/chat/completions`` with ``stream: false`` —
   the task prompt as the user message plus ``system_prompt_extra`` as a
   system message when set. One caller-level retry on upstream 5xx or a
   timeout/transport failure (the gateway's own upstream retries already
   ran inside the first call); 429 ``budget_exceeded`` and other 4xx are
   terminal immediately. The run's ``wall_clock_seconds`` bounds the whole
   cell like a container's cap: the deadline aborts an in-flight call and
   skips any remaining retry (terminal ``timeout``).
4. Write the artifact: ``output.txt`` (the reply text), ``transcript.log``
   (readable request/response), ``meta.json`` (provenance + error detail)
   under ``<artifact_root>/<run>/<sandbox>/`` and index them as Artifact
   rows (``output``/``transcript``/``meta`` kinds — no ``workspace`` row,
   there is no workspace).
5. ``terminal_state`` + ``sandbox_finalized`` events and the sandbox
   transition — ``harness_complete`` on a usable response, ``timeout`` /
   ``budget_exceeded`` / ``error`` on the matching failure modes.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy.orm import Session, sessionmaker

from db.models import Artifact, Run, Sandbox
from sandbox.runtime import SandboxRuntime

from . import provision, state_machine
from .events import emit
from .matrix import sandbox_model

log = logging.getLogger(__name__)

#: Registry/API name — a harness config selecting it never spawns a container.
ADAPTER_NAME = "prompt"

#: Served by ``GET /api/harnesses`` so the submit form can group it under
#: "no sandbox"; it is NOT in ``ADAPTERS`` (there is no spawnable module —
#: contract-surface tests iterate that registry).
MANIFEST: dict[str, Any] = {
    "name": ADAPTER_NAME,
    "display_name": "Prompt (no sandbox)",
    "description": (
        "Single chat-completions call per matrix cell — no container, no "
        "tools, no workspace. The reply text lands as output.txt and is "
        "judged like any other artifact."
    ),
    "llm_protocol": "openai-chat-completions",
    "no_sandbox": True,
    "requires": ["gateway:/v1/chat/completions"],
    # PR27: every ModelOptions key folds straight into the request body.
    "model_options": ["temperature", "seed", "max_tokens", "reasoning"],
}

#: Artifact filenames under <artifact_root>/<run>/<sandbox>/ — output.txt is
#: the submission the evaluator stages; the results surface exposes it as a
#: downloadable ``output`` artifact.
OUTPUT_FILENAME = "output.txt"
TRANSCRIPT_FILENAME = "transcript.log"
META_FILENAME = "meta.json"

#: Per-attempt HTTP timeout — like rubric/testgen/direct-judge it must
#: exceed the gateway's worst-case silent hold on one call
#: ((1 + UPSTREAM_RETRY_ATTEMPTS) x UPSTREAM_TIMEOUT_SECONDS + backoff,
#: ~915s under shipped defaults) so a prompt cell never abandons a call the
#: gateway is still paying for upstream.
DEFAULT_TIMEOUT_SECONDS = 960.0
#: Reply cap — the cell's whole output budget for the single call.
DEFAULT_MAX_TOKENS = 8192
#: PR25: "one retry on upstream 5xx/timeout" — the gateway already retries
#: inside each call, so attempts here are whole-call retries on top.
MAX_ATTEMPTS = 2
RETRY_BACKOFF_SECONDS = 1.0


def is_prompt_config(harness_config: dict[str, Any] | None) -> bool:
    """True when the harness config selects the prompt pseudo-adapter."""
    return (
        isinstance(harness_config, dict)
        and harness_config.get("adapter") == ADAPTER_NAME
    )


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


@dataclass
class _CallResult:
    """Outcome of the (possibly retried) gateway call — never raises."""

    text: str = ""
    usage: dict[str, Any] | None = None
    finish_reason: str | None = None
    http_status: int | None = None
    attempts: int = 0
    duration_ms: int = 0
    terminal_reason: str = "harness_complete"
    error: str | None = None
    deadline_hit: bool = False

    @property
    def ok(self) -> bool:
        return self.error is None


def _response_error_type(resp: httpx.Response) -> str | None:
    """``error.type`` of a gateway error payload (e.g. ``budget_exceeded``)."""
    try:
        err = resp.json().get("error")
        if isinstance(err, dict):
            t = err.get("type")
            return str(t) if t else None
    except Exception:  # noqa: BLE001 — non-JSON error body
        return None
    return None


def _body_excerpt(resp: httpx.Response, *, limit: int = 300) -> str:
    try:
        text = resp.text
    except Exception:  # noqa: BLE001
        return "<unreadable body>"
    text = " ".join(text.split())
    return text[:limit] if text else "<empty body>"


def _emit_cell_event(
    session_factory: sessionmaker[Session],
    *,
    run_id: uuid.UUID,
    sandbox_id: uuid.UUID,
    event_type: str,
    payload: dict[str, Any],
) -> None:
    """One orchestrator-sourced event for the cell — own commit, never raises."""
    try:
        with session_factory() as session:
            emit(
                session,
                run_id=run_id,
                sandbox_id=sandbox_id,
                event_type=event_type,
                payload=payload,
            )
            session.commit()
    except Exception:  # noqa: BLE001 — observability only
        log.warning("prompt_cell: could not emit %s for %s", event_type, sandbox_id)


async def _call_gateway(
    client: httpx.AsyncClient,
    *,
    session_factory: sessionmaker[Session],
    run_id: uuid.UUID,
    sandbox_id: uuid.UUID,
    gateway_url: str,
    cred: str,
    model: str,
    messages: list[dict[str, str]],
    max_tokens: int,
    deadline: float,
    model_options: dict[str, Any] | None = None,
) -> _CallResult:
    """One chat-completions call + ONE retry on upstream 5xx/timeout.

    Retryable: ``resp.status_code >= 500`` (the gateway's own
    502/503/504 upstream retries already ran inside the call) and
    ``httpx.TransportError`` subclasses (timeouts + connect/read errors).
    429s and other 4xx are deterministic — no retry.

    ``deadline`` (monotonic) is the cell's wall-clock cap, the same
    ``started_at + wall_clock_seconds`` bound the monitor applies to
    containers: an attempt still in flight at the deadline is cancelled —
    the orphaned upstream call mirrors a container being killed — and an
    already-elapsed deadline skips any remaining retry; either way the
    cell ends ``timeout`` rather than the httpx timeout's own verdicts.
    """
    url = f"{gateway_url.rstrip('/')}/v1/chat/completions"
    headers = {"Authorization": f"Bearer {cred}"}
    body: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "stream": False,
    }
    # PR27: prompt cells honor every model_options key directly — they fold
    # into the chat-completions body (gateway passes known keys through).
    opts = model_options or {}
    if (t := opts.get("temperature")) is not None:
        body["temperature"] = t
    if (s := opts.get("seed")) is not None:
        body["seed"] = s
    if r := opts.get("reasoning"):
        body["reasoning_effort"] = r
    result = _CallResult()
    started = time.monotonic()
    for attempt in range(1, MAX_ATTEMPTS + 1):
        if time.monotonic() >= deadline:
            result.terminal_reason = "timeout"
            result.error = "wall_clock_seconds deadline exceeded"
            result.deadline_hit = True
            break
        result.attempts = attempt
        try:
            resp = await asyncio.wait_for(
                client.post(url, json=body, headers=headers),
                timeout=max(0.0, deadline - time.monotonic()),
            )
        except (TimeoutError, httpx.TimeoutException) as exc:
            expired = time.monotonic() >= deadline
            if attempt < MAX_ATTEMPTS and not expired:
                _emit_retry(
                    session_factory,
                    run_id=run_id,
                    sandbox_id=sandbox_id,
                    attempt=attempt + 1,
                    reason=f"timeout: {type(exc).__name__}",
                )
                await asyncio.sleep(
                    min(RETRY_BACKOFF_SECONDS, deadline - time.monotonic())
                )
                continue
            result.terminal_reason = "timeout"
            if expired:
                result.error = "wall_clock_seconds deadline exceeded"
                result.deadline_hit = True
            else:
                result.error = (
                    f"gateway call timed out ({type(exc).__name__}) "
                    f"after {attempt} attempt(s)"
                )
            break
        except httpx.TransportError as exc:
            expired = time.monotonic() >= deadline
            if attempt < MAX_ATTEMPTS and not expired:
                _emit_retry(
                    session_factory,
                    run_id=run_id,
                    sandbox_id=sandbox_id,
                    attempt=attempt + 1,
                    reason=f"transport: {type(exc).__name__}",
                )
                await asyncio.sleep(
                    min(RETRY_BACKOFF_SECONDS, deadline - time.monotonic())
                )
                continue
            if expired:
                result.terminal_reason = "timeout"
                result.error = "wall_clock_seconds deadline exceeded"
                result.deadline_hit = True
            else:
                result.terminal_reason = "error"
                result.error = (
                    f"gateway unreachable ({type(exc).__name__}: {exc}) "
                    f"after {attempt} attempt(s)"
                )
            break

        result.http_status = resp.status_code
        if resp.status_code >= 500:
            expired = time.monotonic() >= deadline
            if attempt < MAX_ATTEMPTS and not expired:
                _emit_retry(
                    session_factory,
                    run_id=run_id,
                    sandbox_id=sandbox_id,
                    attempt=attempt + 1,
                    reason=f"upstream HTTP {resp.status_code}",
                )
                await asyncio.sleep(
                    min(RETRY_BACKOFF_SECONDS, deadline - time.monotonic())
                )
                continue
            if expired:
                result.terminal_reason = "timeout"
                result.error = "wall_clock_seconds deadline exceeded"
                result.deadline_hit = True
            else:
                result.terminal_reason = "error"
                result.error = (
                    f"upstream HTTP {resp.status_code} after {attempt} attempt(s): "
                    f"{_body_excerpt(resp)}"
                )
            break
        if resp.status_code == 429:
            etype = _response_error_type(resp)
            result.terminal_reason = (
                "budget_exceeded" if etype == "budget_exceeded" else "error"
            )
            result.error = f"gateway HTTP 429 ({etype or 'rate limited'}): {_body_excerpt(resp)}"
            break
        if resp.status_code != 200:
            result.terminal_reason = "error"
            result.error = f"gateway HTTP {resp.status_code}: {_body_excerpt(resp)}"
            break
        try:
            data = resp.json()
            choice = (data.get("choices") or [None])[0] or {}
            content = (choice.get("message") or {}).get("content")
            if not isinstance(content, str):
                raise TypeError("response has no message content")
            result.text = content
            result.finish_reason = choice.get("finish_reason")
            usage = data.get("usage")
            result.usage = usage if isinstance(usage, dict) else None
        except Exception as exc:  # noqa: BLE001 — malformed 200 body
            result.terminal_reason = "error"
            result.error = f"malformed chat response: {type(exc).__name__}: {exc}"
        break

    result.duration_ms = int((time.monotonic() - started) * 1000)
    return result


def _emit_retry(
    session_factory: sessionmaker[Session],
    *,
    run_id: uuid.UUID,
    sandbox_id: uuid.UUID,
    attempt: int,
    reason: str,
) -> None:
    _emit_cell_event(
        session_factory,
        run_id=run_id,
        sandbox_id=sandbox_id,
        event_type="checkpoint",
        payload={
            "stage": "prompt_cell_retry",
            "attempt": attempt,
            "reason": reason,
        },
    )


def _render_transcript(
    *,
    run: Run,
    model: str,
    system_extra: str,
    call: _CallResult,
) -> str:
    """Readable call record — the container-transcript analogue for a cell
    that produced no container logs."""
    parts = [
        "=== prompt cell — adapter 'prompt' (no container) ===",
        f"model: {model}",
        f"run_id: {run.id}",
        "",
    ]
    if system_extra:
        parts += ["=== system ===", system_extra, ""]
    parts += ["=== user ===", run.task_prompt, ""]
    parts += ["=== assistant ===", call.text or "(no response — see meta.json)", ""]
    parts += [
        "=== meta ===",
        (
            f"http_status={call.http_status} finish_reason={call.finish_reason} "
            f"attempts={call.attempts} duration_ms={call.duration_ms}"
        ),
    ]
    if call.usage:
        parts.append(f"usage={json.dumps(call.usage, sort_keys=True)}")
    if call.error:
        parts.append(f"error={call.error}")
    return "\n".join(parts) + "\n"


def _write_artifacts(
    artifact_dir: Path,
    *,
    run: Run,
    sandbox_id: uuid.UUID,
    model: str,
    system_extra: str,
    call: _CallResult,
) -> None:
    """Persist output.txt + transcript.log + meta.json (sync — to_thread)."""
    artifact_dir.mkdir(parents=True, exist_ok=True)
    (artifact_dir / OUTPUT_FILENAME).write_text(call.text, encoding="utf-8")
    (artifact_dir / TRANSCRIPT_FILENAME).write_text(
        _render_transcript(
            run=run, model=model, system_extra=system_extra, call=call
        ),
        encoding="utf-8",
    )
    now = datetime.now(UTC).isoformat()
    meta = {
        "sandbox_id": str(sandbox_id),
        "run_id": str(run.id),
        "adapter": ADAPTER_NAME,
        "no_sandbox": True,
        "model": model,
        "terminal_reason": call.terminal_reason,
        "attempts": call.attempts,
        "http_status": call.http_status,
        "finish_reason": call.finish_reason,
        "duration_ms": call.duration_ms,
        "usage": call.usage,
        "error": call.error,
        "finished_at": now,
    }
    (artifact_dir / META_FILENAME).write_text(
        json.dumps(meta, indent=2) + "\n", encoding="utf-8"
    )


def _mark_running(
    session_factory: sessionmaker[Session],
    *,
    run_id: uuid.UUID,
    sandbox_id: uuid.UUID,
    model: str,
    max_tokens: int,
) -> None:
    """provisioning → running + the orchestrator-side ``run_started``."""
    with session_factory() as session:
        sb = session.get(Sandbox, sandbox_id)
        if sb is not None:
            sb.started_at = datetime.now(UTC)
            try:
                state_machine.transition(session, sb, "running")
            except state_machine.InvalidTransition:
                sb.status = "running"
        emit(
            session,
            run_id=run_id,
            sandbox_id=sandbox_id,
            event_type="run_started",
            payload={
                "adapter": ADAPTER_NAME,
                "model": model,
                "no_sandbox": True,
                "max_iters": 1,
                "max_tokens": max_tokens,
            },
        )
        session.commit()


def _finalize(
    session_factory: sessionmaker[Session],
    *,
    run_id: uuid.UUID,
    sandbox_id: uuid.UUID,
    artifact_dir: Path,
    call: _CallResult,
) -> None:
    """Artifact rows + terminal fields + terminal_state/finalized events.

    Mirrors ``monitor._finalize_db`` — a done-ish exit whose output.txt is
    empty carries the same ``empty_workspace`` warning markers so results
    read it identically to a container that finished without files.
    """
    reason = call.terminal_reason
    empty = reason in state_machine.DONE_REASONS and not call.text.strip()
    with session_factory() as session:
        sb = session.get(Sandbox, sandbox_id)
        if sb is None:
            return
        sb.artifact_path = str(artifact_dir)
        for kind, name in (
            ("output", OUTPUT_FILENAME),
            ("transcript", TRANSCRIPT_FILENAME),
            ("meta", META_FILENAME),
        ):
            p = artifact_dir / name
            if not p.exists():
                continue
            session.add(
                Artifact(
                    sandbox_id=sandbox_id,
                    kind=kind,
                    path=str(p),
                    size_bytes=p.stat().st_size,
                )
            )
        to_status = "done" if reason in state_machine.DONE_REASONS else "failed"
        try:
            state_machine.transition(session, sb, to_status)
        except state_machine.InvalidTransition:
            sb.status = to_status
        sb.terminal_reason = reason
        sb.finished_at = datetime.now(UTC)
        ts_payload: dict[str, Any] = {
            "terminal_state": reason,
            "adapter": ADAPTER_NAME,
            "iters": 1,
            "attempts": call.attempts,
            "usage": call.usage,
        }
        if call.deadline_hit:
            ts_payload["reason"] = "wall_clock"
        if call.error:
            ts_payload["error"] = call.error
        emit(
            session,
            run_id=run_id,
            sandbox_id=sandbox_id,
            event_type="terminal_state",
            payload=ts_payload,
        )
        fin_payload: dict[str, Any] = {
            "stage": "sandbox_finalized",
            "terminal_reason": reason,
            "sandbox_status": sb.status,
            "artifact_path": sb.artifact_path,
        }
        if empty:
            fin_payload["empty_workspace"] = True
        emit(
            session,
            run_id=run_id,
            sandbox_id=sandbox_id,
            event_type="checkpoint",
            payload=fin_payload,
        )
        if empty:
            emit(
                session,
                run_id=run_id,
                sandbox_id=sandbox_id,
                event_type="checkpoint",
                payload={
                    "stage": "empty_workspace",
                    "severity": "warning",
                    "terminal_reason": reason,
                },
            )
        session.commit()


def _mark_cell_failed(
    session_factory: sessionmaker[Session],
    *,
    run_id: uuid.UUID,
    sandbox_id: uuid.UUID,
    message: str,
) -> None:
    """Unexpected internal failure — mirror ``provision._mark_spawn_failed``."""
    with session_factory() as session:
        sb = session.get(Sandbox, sandbox_id)
        if sb is not None and sb.status not in ("done", "failed"):
            try:
                state_machine.transition(session, sb, "failed")
            except state_machine.InvalidTransition:
                sb.status = "failed"
            sb.terminal_reason = "error"
            sb.finished_at = datetime.now(UTC)
        emit(
            session,
            run_id=run_id,
            sandbox_id=sandbox_id,
            event_type="error",
            payload={"stage": "prompt_cell", "message": message},
        )
        session.commit()


async def run_prompt_cell(
    *,
    session_factory: sessionmaker[Session],
    runtime: SandboxRuntime,
    run_id: uuid.UUID,
    harness_config: dict[str, Any],
    settings: Any,
    client: httpx.AsyncClient | None = None,
) -> uuid.UUID | None:
    """Execute one prompt-only matrix cell; return the sandbox id.

    ``spawn_one``-shaped contract: the Sandbox row is always created, a
    failure marks it ``failed``/``error`` (and still lands its artifact
    when the gateway call itself answered), and the caller decides run
    failure by what survived. ``client`` is a DI/test knob — production
    builds its own bounded ``httpx.AsyncClient``.
    """
    run = provision._load_run(session_factory, run_id)
    if run is None:
        log.error("run_prompt_cell: run %s not found", run_id)
        return None
    sid = provision._insert_sandbox_row(session_factory, run_id, harness_config)
    try:
        budgets = provision.budgets_for(run)
        cred = provision.mint_sandbox_cred(
            run_id=run_id,
            sandbox_id=sid,
            secret=settings.GATEWAY_CRED_SECRET,
            budgets=budgets,
        )
        model = sandbox_model(harness_config, run.model_name)
        system_extra = str(
            harness_config.get("system_prompt_extra") or ""
        ).strip()
        # PR27: a configured model_options.max_tokens wins; the env knob
        # fills in when the config didn't set the key.
        model_options = harness_config.get("model_options")
        opt_max_tokens = (
            model_options.get("max_tokens")
            if isinstance(model_options, dict)
            else None
        )
        max_tokens = (
            int(opt_max_tokens)
            if opt_max_tokens
            else _env_int("PROMPT_CELL_MAX_TOKENS", DEFAULT_MAX_TOKENS)
        )
        deadline = time.monotonic() + float(budgets["wall_clock_seconds"])
        _mark_running(
            session_factory,
            run_id=run_id,
            sandbox_id=sid,
            model=model,
            max_tokens=max_tokens,
        )

        messages: list[dict[str, str]] = []
        if system_extra:
            messages.append({"role": "system", "content": system_extra})
        messages.append({"role": "user", "content": run.task_prompt})

        own_client = client is None
        if client is None:
            client = httpx.AsyncClient(
                timeout=_env_float(
                    "PROMPT_CELL_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS
                )
            )
        try:
            call = await _call_gateway(
                client,
                session_factory=session_factory,
                run_id=run_id,
                sandbox_id=sid,
                gateway_url=settings.GATEWAY_URL,
                cred=cred,
                model=model,
                messages=messages,
                max_tokens=max_tokens,
                deadline=deadline,
                model_options=model_options
                if isinstance(model_options, dict)
                else None,
            )
        finally:
            if own_client:
                await client.aclose()

        artifact_dir = Path(runtime.artifact_root) / str(run_id) / str(sid)
        await asyncio.to_thread(
            _write_artifacts,
            artifact_dir,
            run=run,
            sandbox_id=sid,
            model=model,
            system_extra=system_extra,
            call=call,
        )
        _finalize(
            session_factory,
            run_id=run_id,
            sandbox_id=sid,
            artifact_dir=artifact_dir,
            call=call,
        )
        log.info(
            "prompt_cell: sandbox %s -> %s (%d attempt(s))",
            sid,
            call.terminal_reason,
            call.attempts,
        )
        return sid
    except Exception as exc:
        log.exception("run_prompt_cell: sandbox %s failed", sid)
        _mark_cell_failed(
            session_factory,
            run_id=run_id,
            sandbox_id=sid,
            message=f"{type(exc).__name__}: {exc}",
        )
        return None
