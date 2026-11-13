"""miniloop agent loop — a minimal tool-call coding agent.

Runs against any OpenAI-compatible ``/v1/chat/completions`` endpoint (the AI
gateway in production; host Ollama for local iteration). Sequential and
synchronous by design: one request → dispatch tool calls → repeat.

Terminal states (emitted on the final ``MINILOOP_RESULT`` stdout line):
``agent_done`` (done tool), ``harness_complete`` (MAX_ITERS reached),
``stuck_loop`` (3 consecutive identical tool-call signatures, or 4 consecutive
text-only replies), ``error`` (unhandled exception / bad config).
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import httpx

from .events import EventEmitter
from .prompts import NUDGE_TEXT_STREAK_2, NUDGE_TEXT_STREAK_3, build_system_prompt
from .skills import SkillCatalog
from .tools import TOOL_IMPLS, ToolContext

TERMINAL_STATES = ("agent_done", "harness_complete", "stuck_loop", "error")

EXIT_CODES = {
    "agent_done": 0,
    "harness_complete": 0,
    "stuck_loop": 0,
    "error": 2,
}

IDENTICAL_SIG_STREAK = 3
TEXT_ONLY_STREAK = 4
COMPACT_KEEP_RECENT_TOOLS = 8


class ConfigError(Exception):
    """Raised when required environment configuration is missing."""


# ---------------------------------------------------------------------------
# configuration
# ---------------------------------------------------------------------------


@dataclass
class AgentConfig:
    gateway_base_url: str
    cred: str
    model: str
    task_prompt: str
    workspace: Path
    events_ingest_url: str | None = None
    ingest_token: str | None = None
    max_iters: int = 50
    max_tokens: int = 4096
    temperature: float = 0.2
    num_ctx: int = 32768
    keep_alive: str = "30m"
    compact_ratio: float = 0.7
    request_timeout: float = 600.0
    # Optional harness-config axis (F2): extra instructions appended to the
    # system prompt verbatim — e.g. comparing skill packs vs. prompt tweaks.
    system_prompt_extra: str = ""

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> AgentConfig:
        env = os.environ if env is None else env

        base = (env.get("GATEWAY_BASE_URL") or "").strip()
        if not base:
            raise ConfigError("GATEWAY_BASE_URL is required")
        model = (env.get("MODEL_NAME") or env.get("MODEL") or "").strip()
        if not model:
            raise ConfigError("MODEL_NAME is required")

        workspace = Path(env.get("WORKSPACE") or ".").expanduser()
        try:
            workspace = workspace.resolve()
            workspace.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ConfigError(f"cannot use WORKSPACE {workspace}: {exc}") from exc

        try:
            max_iters = max(1, int(env.get("MAX_ITERS", "50")))
        except ValueError:
            max_iters = 50

        return cls(
            gateway_base_url=base,
            cred=(env.get("GATEWAY_CRED") or "").strip(),
            model=model,
            task_prompt=env.get("TASK_PROMPT", ""),
            workspace=workspace,
            events_ingest_url=(env.get("EVENTS_INGEST_URL") or "").strip() or None,
            ingest_token=(env.get("INGEST_TOKEN") or "").strip() or None,
            max_iters=max_iters,
            system_prompt_extra=(env.get("SYSTEM_PROMPT_EXTRA") or "").strip(),
        )


# ---------------------------------------------------------------------------
# chat client
# ---------------------------------------------------------------------------


class ChatClient(Protocol):
    """Minimal interface the agent needs — easy to fake in tests."""

    def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> dict[str, Any]: ...

    def close(self) -> None: ...


class HttpChatClient:
    """OpenAI-compatible chat-completions client (httpx, non-streaming)."""

    def __init__(
        self,
        base_url: str,
        cred: str,
        model: str,
        *,
        timeout: float = 600.0,
        transport: httpx.BaseTransport | None = None,
        extra_body: dict[str, Any] | None = None,
        temperature: float = 0.2,
        num_ctx: int = 32768,
        keep_alive: str = "30m",
        max_tokens: int = 4096,
    ) -> None:
        headers = {"Content-Type": "application/json"}
        if cred:
            headers["Authorization"] = f"Bearer {cred}"
        self._http = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers=headers,
            timeout=timeout,
            transport=transport,
        )
        self.model = model
        self._params = {
            "temperature": temperature,
            # "none" asks reasoning-capable upstreams (e.g. qwen3) to skip
            # thinking blocks; unknown fields are ignored by Ollama.
            "reasoning_effort": "none",
            "num_ctx": num_ctx,
            "keep_alive": keep_alive,
            "max_tokens": max_tokens,
            # Ollama extension: disables qwen3-style thinking when supported.
            "think": False,
        }
        if extra_body:
            self._params.update(extra_body)

    def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> dict[str, Any]:
        body = {"model": self.model, "messages": messages, "tools": tools, **self._params}
        resp = self._http.post("/v1/chat/completions", json=body)
        resp.raise_for_status()
        return resp.json()

    def close(self) -> None:
        self._http.close()


# ---------------------------------------------------------------------------
# tool schemas (OpenAI function format)
# ---------------------------------------------------------------------------

TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "shell",
            "description": (
                "Run a bash command in the workspace. Returns JSON with "
                "exit_code, stdout, stderr (truncated ~8KB), timed_out."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "The bash command to run."},
                    "timeout_seconds": {
                        "type": "number",
                        "description": "Timeout in seconds (default 30, max 120).",
                    },
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": (
                "Read a text file from the workspace. offset/limit are 1-based "
                "line numbers; default 200 lines, max 500."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path relative to the workspace."},
                    "offset": {"type": "integer", "description": "1-based start line (default 1)."},
                    "limit": {"type": "integer", "description": "Max lines (default 200, max 500)."},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write a UTF-8 text file in the workspace, creating parent dirs.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path relative to the workspace."},
                    "content": {"type": "string", "description": "Full file content."},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "use_skill",
            "description": "Load a skill's full instructions before using it.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Skill name or pack:name from the Available skills list.",
                    },
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "done",
            "description": "Signal task completion. Call exactly once, as your final action.",
            "parameters": {
                "type": "object",
                "properties": {
                    "summary": {"type": "string", "description": "What was accomplished."},
                    "status": {
                        "type": "string",
                        "enum": ["success", "blocked"],
                        "description": "success or blocked.",
                    },
                },
                "required": ["summary", "status"],
            },
        },
    },
]


# ---------------------------------------------------------------------------
# loop helpers
# ---------------------------------------------------------------------------


def _tool_signature(tool_calls: list[dict[str, Any]]) -> str:
    """Canonical signature of an assistant message's tool calls — used for
    stuck-loop detection (3 consecutive identical signatures → stuck_loop)."""
    parts = []
    for tc in tool_calls:
        fn = tc.get("function") or {}
        raw = fn.get("arguments", "{}")
        try:
            args_obj = json.loads(raw) if isinstance(raw, str) else raw
        except (json.JSONDecodeError, TypeError):
            args_obj = raw
        parts.append({"name": fn.get("name", ""), "arguments": args_obj})
    return json.dumps(parts, sort_keys=True, ensure_ascii=False, default=str)


def _args_summary(args: dict[str, Any]) -> dict[str, Any]:
    """Truncated, JSON-safe summary of tool arguments for the tool_call event."""
    out: dict[str, Any] = {}
    for k, v in args.items():
        s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, default=str)
        out[k] = s if len(s) <= 500 else s[:500] + "…"
    return out


def _parse_args(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            obj = json.loads(raw)
            return obj if isinstance(obj, dict) else {"_raw": raw}
        except json.JSONDecodeError:
            return {"_raw": raw}
    return {}


@dataclass
class RunResult:
    terminal_state: str
    iters: int
    usage: dict[str, int] = field(
        default_factory=lambda: {
            "requests": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        }
    )
    done_summary: str | None = None
    done_status: str | None = None
    error: str | None = None


# ---------------------------------------------------------------------------
# agent
# ---------------------------------------------------------------------------


class Agent:
    """The miniloop tool-call loop."""

    def __init__(
        self,
        *,
        client: ChatClient,
        ctx: ToolContext,
        max_iters: int = 50,
        num_ctx: int = 32768,
        compact_ratio: float = 0.7,
    ) -> None:
        self.client = client
        self.ctx = ctx
        self.emitter = ctx.emitter
        self.max_iters = max_iters
        self.num_ctx = num_ctx
        self.compact_ratio = compact_ratio
        self.compact_threshold = int(num_ctx * compact_ratio)

    # -- public -------------------------------------------------------------

    def run(self, system_prompt: str, task_prompt: str) -> RunResult:
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": task_prompt},
        ]
        usage = {
            "requests": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        }
        last_prompt_tokens = 0
        no_tool_streak = 0
        last_sig: str | None = None
        sig_streak = 0
        done_summary: str | None = None
        done_status: str | None = None

        iters = 0
        try:
            for it in range(self.max_iters):
                iters = it + 1
                self._maybe_compact(messages, last_prompt_tokens)
                self.emitter.emit(
                    "checkpoint", {"iter": iters, "prompt_tokens": last_prompt_tokens}
                )

                resp = self.client.chat(messages, tools=TOOLS)
                msg = _extract_message(resp)
                pt, ct, tt = _extract_usage(resp)
                last_prompt_tokens = pt
                usage["requests"] += 1
                usage["prompt_tokens"] += pt
                usage["completion_tokens"] += ct
                usage["total_tokens"] += tt or (pt + ct)

                # Append the assistant message verbatim (content + tool_calls).
                messages.append(msg)
                tool_calls = msg.get("tool_calls") or []

                if not tool_calls:
                    no_tool_streak += 1
                    if no_tool_streak >= TEXT_ONLY_STREAK:
                        return self._finish(
                            "stuck_loop", iters, usage, done_summary, done_status,
                            error="assistant produced no tool calls 4 times consecutively",
                        )
                    if no_tool_streak in (2, 3):
                        nudge = (
                            NUDGE_TEXT_STREAK_2
                            if no_tool_streak == 2
                            else NUDGE_TEXT_STREAK_3
                        )
                        messages.append({"role": "user", "content": nudge})
                    continue

                no_tool_streak = 0
                sig = _tool_signature(tool_calls)
                sig_streak = sig_streak + 1 if sig == last_sig else 1
                last_sig = sig

                done_hit = self._dispatch(tool_calls, messages)
                if done_hit is not None:
                    done_summary, done_status = done_hit
                    return self._finish(
                        "agent_done", iters, usage, done_summary, done_status
                    )
                if sig_streak >= IDENTICAL_SIG_STREAK:
                    return self._finish(
                        "stuck_loop", iters, usage, done_summary, done_status,
                        error="identical tool-call signature 3 times consecutively",
                    )
        except Exception as exc:  # noqa: BLE001 — any failure → error terminal
            self.emitter.emit("error", {"message": str(exc), "iter": iters})
            return self._finish("error", iters, usage, done_summary, done_status, error=str(exc))

        return self._finish("harness_complete", iters, usage, done_summary, done_status)

    # -- internals ----------------------------------------------------------

    def _dispatch(
        self, tool_calls: list[dict[str, Any]], messages: list[dict[str, Any]]
    ) -> tuple[str | None, str | None] | None:
        """Run each tool call, appending role:"tool" results. Returns
        (summary, status) when a `done` call was dispatched, else None."""
        for tc in tool_calls:
            fn = tc.get("function") or {}
            name = str(fn.get("name") or "")
            args = _parse_args(fn.get("arguments"))
            call_id = str(tc.get("id") or "")

            self.emitter.emit(
                "tool_call", {"name": name, "args": _args_summary(args), "id": call_id}
            )

            if name == "done":
                # tool_done emits done_signal; we still append a tool result
                # so the transcript stays well-formed, then terminate.
                content = TOOL_IMPLS["done"](self.ctx, args)
                messages.append(
                    {"role": "tool", "tool_call_id": call_id, "content": content}
                )
                status = str(args.get("status", "success"))
                if status not in ("success", "blocked"):
                    status = "success"
                return (str(args.get("summary", ""))[:2000] or None, status)

            impl = TOOL_IMPLS.get(name)
            if impl is None:
                content = json.dumps({"error": f"unknown tool: {name}"})
            else:
                try:
                    content = impl(self.ctx, args)
                except Exception as exc:  # noqa: BLE001 — tools must not kill the loop
                    content = json.dumps({"error": f"{name} failed: {exc}"})
            messages.append({"role": "tool", "tool_call_id": call_id, "content": content})
        return None

    def _maybe_compact(self, messages: list[dict[str, Any]], prompt_tokens: int) -> bool:
        """When prompt_tokens exceed ~70% of num_ctx, replace the oldest
        tool-result contents with one-line summaries (keep system + task +
        the most recent tool results)."""
        if prompt_tokens < self.compact_threshold:
            return False
        tool_idx = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
        if len(tool_idx) <= COMPACT_KEEP_RECENT_TOOLS:
            return False
        compacted = 0
        for i in tool_idx[: -COMPACT_KEEP_RECENT_TOOLS]:
            m = messages[i]
            content = str(m.get("content") or "")
            if '"compacted": true' in content or '"compacted":true' in content:
                continue
            preview = content[:160].replace("\n", " ")
            m["content"] = json.dumps(
                {"compacted": True, "preview": preview}, ensure_ascii=False
            )
            compacted += 1
        if compacted:
            self.emitter.emit(
                "context_compacted",
                {"prompt_tokens": prompt_tokens, "messages_compacted": compacted},
            )
        return compacted > 0

    def _finish(
        self,
        terminal_state: str,
        iters: int,
        usage: dict[str, int],
        done_summary: str | None,
        done_status: str | None,
        *,
        error: str | None = None,
    ) -> RunResult:
        result = RunResult(
            terminal_state=terminal_state,
            iters=iters,
            usage=usage,
            done_summary=done_summary,
            done_status=done_status,
            error=error,
        )
        self.emitter.emit(
            "terminal_state",
            {
                "terminal_state": terminal_state,
                "iters": iters,
                "usage": usage,
                "error": error,
            },
        )
        return result


# ---------------------------------------------------------------------------
# response parsing helpers
# ---------------------------------------------------------------------------


def _extract_message(resp: dict[str, Any]) -> dict[str, Any]:
    choices = resp.get("choices") or []
    if not choices:
        raise RuntimeError(f"chat response has no choices: {json.dumps(resp)[:500]}")
    msg = choices[0].get("message")
    if not isinstance(msg, dict):
        raise TypeError(f"chat response message missing: {json.dumps(resp)[:500]}")
    msg = dict(msg)
    msg.setdefault("role", "assistant")
    return msg


def _extract_usage(resp: dict[str, Any]) -> tuple[int, int, int]:
    u = resp.get("usage") or {}
    pt = int(u.get("prompt_tokens") or 0)
    ct = int(u.get("completion_tokens") or 0)
    tt = int(u.get("total_tokens") or 0)
    return pt, ct, tt


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """Console entry: ``python -m miniloop`` (or ``python -m adapters.miniloop``)."""
    try:
        cfg = AgentConfig.from_env()
    except ConfigError as exc:
        print(
            "MINILOOP_RESULT "
            + json.dumps({"terminal_state": "error", "iters": 0, "error": str(exc)}),
            flush=True,
        )
        return EXIT_CODES["error"]

    emitter = EventEmitter(
        ingest_url=cfg.events_ingest_url,
        token=cfg.ingest_token or cfg.cred or None,
    )
    client: ChatClient | None = None
    try:
        skills = SkillCatalog.load()
        client = HttpChatClient(
            cfg.gateway_base_url,
            cfg.cred,
            cfg.model,
            timeout=cfg.request_timeout,
            temperature=cfg.temperature,
            num_ctx=cfg.num_ctx,
            keep_alive=cfg.keep_alive,
            max_tokens=cfg.max_tokens,
        )
        ctx = ToolContext(workspace=cfg.workspace, emitter=emitter, skills=skills)
        agent = Agent(
            client=client,
            ctx=ctx,
            max_iters=cfg.max_iters,
            num_ctx=cfg.num_ctx,
            compact_ratio=cfg.compact_ratio,
        )
        system_prompt = build_system_prompt(
            workspace=str(cfg.workspace),
            catalog_lines=skills.catalog_lines(),
            has_skills=bool(skills.entries),
        )
        if cfg.system_prompt_extra:
            system_prompt += (
                "\n\n## Additional instructions\n" + cfg.system_prompt_extra
            )
        emitter.emit(
            "run_started",
            {
                "model": cfg.model,
                "workspace": str(cfg.workspace),
                "max_iters": cfg.max_iters,
                "skills": len(skills.entries),
            },
        )
        result = agent.run(system_prompt, cfg.task_prompt)
    except Exception as exc:  # noqa: BLE001 — last-resort guard
        emitter.emit("error", {"message": f"fatal: {exc}"})
        result = RunResult(terminal_state="error", iters=0, error=str(exc))
    finally:
        if client is not None:
            client.close()

    payload: dict[str, Any] = {
        "terminal_state": result.terminal_state,
        "iters": result.iters,
        "usage": result.usage,
    }
    if result.done_summary:
        payload["done_summary"] = result.done_summary
    if result.done_status:
        payload["done_status"] = result.done_status
    if result.error:
        payload["error"] = result.error
    print("MINILOOP_RESULT " + json.dumps(payload, ensure_ascii=False), flush=True)
    emitter.close()
    return EXIT_CODES.get(result.terminal_state, 1)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
