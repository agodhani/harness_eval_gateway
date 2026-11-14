"use client";

/**
 * PR10 — single-event rendering shared by the merged timeline stream and
 * the split-view lanes, extracted from TimelineClient so both views render
 * events identically: same `.tl-*` markup, same per-type color language
 * (`data-type` drives the shared palette), same expandable payload.
 *
 * - {@link summarizeEvent} — one-line human summary per event type.
 * - {@link DayMarker} — the `— MM-DD —` separator row.
 * - {@link EventRow} — timestamp/id/source/type/summary row + raw payload.
 *   `showSandbox` hides the sandbox chip inside split lanes (the lane
 *   header already identifies the sandbox); shared full-width rows keep it.
 */
import type { ReactNode } from "react";
import type { EventOut } from "@/lib/api";
import { formatTimeMs, shortId, truncate } from "@/lib/format";
import { Badge, sourceTone } from "@/components/ui";

// ---------------------------------------------------------------------------
// payload summarizers — one-line human summaries per event type
// ---------------------------------------------------------------------------

function str(v: unknown): string {
  return typeof v === "string" ? v : v == null ? "" : String(v);
}

function num(v: unknown): number | null {
  return typeof v === "number" && Number.isFinite(v) ? v : null;
}

function jpreview(v: unknown, max = 160): string {
  if (v == null) return "";
  let s: string;
  try {
    s = typeof v === "string" ? v : JSON.stringify(v);
  } catch {
    s = String(v);
  }
  s = s.replace(/\s+/g, " ").trim();
  return s.length > max ? `${s.slice(0, max - 1)}…` : s;
}

function kvPairs(payload: Record<string, unknown>, keys: string[]): string {
  return keys
    .filter((k) => payload[k] != null && payload[k] !== "")
    .map((k) => `${k}=${jpreview(payload[k], 60)}`)
    .join(" · ");
}

export function summarizeEvent(ev: EventOut): ReactNode {
  const p = ev.payload ?? {};
  switch (ev.type) {
    case "llm_request": {
      const parts = [
        p.model ? <code key="m">{str(p.model)}</code> : null,
        num(p.message_count) != null ? `${p.message_count} msgs` : null,
        num(p.tool_count) != null ? `${p.tool_count} tools` : null,
      ].filter(Boolean);
      return <>LLM request{parts.length ? <> · {parts}</> : null}</>;
    }
    case "llm_response": {
      const usage = (p.usage ?? {}) as Record<string, unknown>;
      const status = num(p.http_status);
      const bad = status != null && status >= 400;
      return (
        <>
          LLM response
          {p.model ? <> · <code>{str(p.model)}</code></> : null}
          {num(usage.prompt_tokens) != null || num(usage.completion_tokens) != null ? (
            <>
              {" · "}
              <span className="mono">
                {num(usage.prompt_tokens) ?? "?"}→{num(usage.completion_tokens) ?? "?"} tok
              </span>
            </>
          ) : null}
          {num(p.latency_ms) != null ? <> · {Math.round(num(p.latency_ms)!)}ms</> : null}
          {status != null ? (
            <> · <Badge tone={bad ? "red" : "green"}>HTTP {status}</Badge></>
          ) : null}
        </>
      );
    }
    case "tool_call": {
      const args = p.args;
      const argStr = args && typeof args === "object" ? jpreview(args, 140) : "";
      return (
        <>
          tool <code>{str(p.name) || "?"}</code>
          {argStr ? <span className="cmd muted"> {argStr}</span> : null}
        </>
      );
    }
    case "shell_command": {
      const code = num(p.exit_code);
      const timedOut = p.timed_out === true;
      return (
        <>
          <span className="cmd">$ {truncate(str(p.command) || "(no command)", 160)}</span>
          {code != null ? (
            <> · <Badge tone={code === 0 && !timedOut ? "green" : "red"}>exit {code}</Badge></>
          ) : null}
          {timedOut ? <Badge tone="amber">timed out</Badge> : null}
          {num(p.duration_ms) != null ? <span className="faint"> {Math.round(num(p.duration_ms)!)}ms</span> : null}
        </>
      );
    }
    case "file_op": {
      const bytes = num(p.bytes);
      return (
        <>
          <span className="mono">{str(p.op) || "op"}</span>{" "}
          <code>{str(p.path) || "?"}</code>
          {bytes != null ? <span className="faint"> · {bytes} B</span> : null}
        </>
      );
    }
    case "checkpoint": {
      const keys = ["phase", "stage", "iter", "prompt_tokens", "terminal_reason", "model"];
      const s = kvPairs(p, keys) || jpreview(p, 120);
      return <>checkpoint{s ? ` · ${s}` : ""}</>;
    }
    case "done_signal": {
      return (
        <>
          <Badge tone={p.status === "success" ? "green" : "amber"}>{str(p.status) || "done"}</Badge>
          {p.summary ? <span className="muted"> {truncate(str(p.summary), 140)}</span> : null}
        </>
      );
    }
    case "error": {
      const detail = str(p.detail) || str(p.reason) || str(p.message) || jpreview(p, 120);
      return (
        <span style={{ color: "var(--red)" }}>
          error{detail ? ` · ${truncate(detail, 160)}` : ""}
        </span>
      );
    }
    case "skill_invocation": {
      return <>skill · <code>{str(p.skill ?? p.name ?? p.skill_name) || jpreview(p, 80)}</code></>;
    }
    case "test_execution": {
      return (
        <>
          test{p.command ? <> · <span className="cmd">{truncate(str(p.command), 120)}</span></> : null}
          {p.exit_code != null ? <> · exit {String(p.exit_code)}</> : null}
        </>
      );
    }
    case "run_started":
    case "terminal_state":
    case "context_compacted":
    case "miniloop_result": {
      const s = kvPairs(p, ["terminal_state", "status", "reason", "iter", "model", "phase"]);
      return (
        <>
          {ev.type.replace(/_/g, " ")}
          {s ? ` · ${s}` : p && Object.keys(p).length ? ` · ${jpreview(p, 120)}` : ""}
        </>
      );
    }
    default:
      return <span className="muted">{jpreview(p, 140) || "—"}</span>;
  }
}

// ---------------------------------------------------------------------------
// rows
// ---------------------------------------------------------------------------

/** `— MM-DD —` day separator — same look the merged stream has always used. */
export function DayMarker({ day }: { day: string }) {
  return <div className="tl-day">— {day} —</div>;
}

export function EventRow({
  ev,
  expanded,
  onToggle,
  showSandbox = true,
}: {
  ev: EventOut;
  expanded: boolean;
  onToggle: (id: number) => void;
  /** Hide the sandbox chip — split lane cells already identify the sandbox. */
  showSandbox?: boolean;
}) {
  const isErr = ev.type === "error";
  return (
    <>
      <div
        className={`tl-row${expanded ? " tl-row--expanded" : ""}${isErr ? " tl-row--error" : ""}`}
        onClick={() => onToggle(ev.id)}
        role="button"
        tabIndex={0}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            onToggle(ev.id);
          }
        }}
        title={`event #${ev.id} · ${ev.ts}`}
      >
        <span className="tl-id">#{ev.id}</span>
        <span className="tl-time">{formatTimeMs(ev.ts)}</span>
        <span className="tl-src">
          <Badge tone={sourceTone(ev.source)}>{ev.source}</Badge>
        </span>
        <span className="tl-type" data-type={ev.type}>{ev.type}</span>
        {showSandbox ? (
          ev.sandbox_id ? (
            <span className="tl-sandbox" title={ev.sandbox_id}>
              {shortId(ev.sandbox_id)}
            </span>
          ) : (
            <span className="tl-sandbox faint">run</span>
          )
        ) : null}
        <span className="tl-summary">{summarizeEvent(ev)}</span>
        <span className="tl-expand-hint">{expanded ? "▾" : "▸"}</span>
      </div>
      {expanded ? (
        <div className="tl-payload">
          <pre>{JSON.stringify(ev.payload, null, 2)}</pre>
        </div>
      ) : null}
    </>
  );
}
