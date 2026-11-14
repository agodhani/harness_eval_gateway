"use client";

/**
 * Run console — per-sandbox event lanes on the run detail page.
 *
 * Same visual shell as the landing page's replay console (evc-* classes in
 * globals.css), but driven by the real event stream: backlog via
 * listAllEvents → SSE tail (events/stream) → after_id poll as a safety net,
 * all merged through an id-deduped sorted list (same ingest as the timeline).
 *
 * Lane routing: evaluator-source events ride participant creds, so they're
 * pulled out of sandbox lanes into the bottom lane along with run-level
 * (orchestrator, null-sandbox) traffic.
 */
import Link from "next/link";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ApiError,
  eventsStreamUrl,
  listAllEvents,
  listEvents,
  runModels,
  type EventOut,
  type RunOut,
  type SandboxOut,
} from "@/lib/api";
import { shortId, truncate } from "@/lib/format";
import { Badge, statusTone } from "@/components/ui";

const POLL_MS = 6_000;
const MAX_EVENTS = 10_000;
const LANE_RENDER_CAP = 120;

// ---------------------------------------------------------------------------
// terse one-line summaries — mono console register, no badges
// ---------------------------------------------------------------------------

function str(v: unknown): string {
  return typeof v === "string" ? v : v == null ? "" : String(v);
}
function num(v: unknown): number | null {
  return typeof v === "number" && Number.isFinite(v) ? v : null;
}
function jpreview(v: unknown, max = 120): string {
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
    .map((k) => `${k}=${jpreview(payload[k], 48)}`)
    .join(" · ");
}

function summarize(ev: EventOut): string {
  const p = ev.payload ?? {};
  switch (ev.type) {
    case "llm_request": {
      const parts = [
        p.model ? str(p.model) : null,
        num(p.message_count) != null ? `${p.message_count} msgs` : null,
        num(p.tool_count) != null ? `${p.tool_count} tools` : null,
      ].filter(Boolean);
      return `LLM request${parts.length ? ` · ${parts.join(" · ")}` : ""}`;
    }
    case "llm_response": {
      const usage = (p.usage ?? {}) as Record<string, unknown>;
      const status = num(p.http_status);
      const parts = [
        p.model ? str(p.model) : null,
        num(usage.prompt_tokens) != null || num(usage.completion_tokens) != null
          ? `${num(usage.prompt_tokens) ?? "?"}→${num(usage.completion_tokens) ?? "?"}`
          : null,
        num(p.latency_ms) != null ? `${Math.round(num(p.latency_ms)!)}ms` : null,
        status != null && status >= 400 ? `HTTP ${status}` : null,
      ].filter(Boolean);
      return `LLM response${parts.length ? ` · ${parts.join(" · ")}` : ""}`;
    }
    case "tool_call": {
      const args = p.args && typeof p.args === "object" ? jpreview(p.args, 90) : "";
      return `tool ${str(p.name) || "?"}${args ? ` ${args}` : ""}`;
    }
    case "shell_command":
      return `$ ${truncate(str(p.command) || "(no command)", 140)}`;
    case "file_op":
      return `${str(p.op) || "op"} ${str(p.path) || "?"}${num(p.bytes) != null ? ` · ${p.bytes} B` : ""}`;
    case "checkpoint": {
      const s = kvPairs(p, ["phase", "stage", "iter", "prompt_tokens", "terminal_reason", "model"]);
      return `checkpoint${s ? ` · ${s}` : ""}`;
    }
    case "done_signal": {
      const s = str(p.summary);
      return `done · ${str(p.status) || "signal"}${s ? ` · ${truncate(s, 80)}` : ""}`;
    }
    case "error": {
      const d = str(p.detail) || str(p.reason) || str(p.message) || jpreview(p, 120);
      return `error${d ? ` · ${truncate(d, 140)}` : ""}`;
    }
    case "skill_invocation":
      return `use_skill · ${str(p.skill ?? p.name ?? p.skill_name) || jpreview(p, 60)}`;
    case "test_execution":
      return `test${p.command ? ` · ${truncate(str(p.command), 100)}` : ""}${p.exit_code != null ? ` · exit ${p.exit_code}` : ""}`;
    case "terminal_state": {
      const s = str(p.terminal_state) || str(p.state) || kvPairs(p, ["reason"]);
      return `terminal · ${s || jpreview(p, 90)}`;
    }
    case "run_started":
      return `run started${p.image ? ` · ${str(p.image)}` : ""}`;
    default: {
      const s = kvPairs(p, ["terminal_state", "status", "reason", "iter", "model", "phase"]);
      return `${ev.type.replace(/_/g, " ")}${s ? ` · ${s}` : jpreview(p, 100) ? ` · ${jpreview(p, 100)}` : ""}`;
    }
  }
}

/** Total prompt+completion tokens from llm_response events in a lane. */
function laneTokens(events: EventOut[]): number {
  let t = 0;
  for (const e of events) {
    if (e.type !== "llm_response") continue;
    const u = (e.payload?.usage ?? {}) as Record<string, unknown>;
    t += (num(u.prompt_tokens) ?? 0) + (num(u.completion_tokens) ?? 0);
  }
  return t;
}

function laneSpec(s: SandboxOut): { adapter: string; model: string; packs: string } {
  const hc = (s.harness_config ?? {}) as Record<string, unknown>;
  const adapter = String(hc.adapter ?? hc.harness ?? "?");
  const model = String(hc.model ?? "").trim();
  const packs = Array.isArray(hc.skill_packs)
    ? (hc.skill_packs as string[]).join(" · ")
    : "";
  return { adapter, model, packs };
}

type SseState = "connecting" | "open" | "reconnecting";

export default function RunConsoleLive({ run }: { run: RunOut }) {
  const runId = run.id;
  const [events, setEvents] = useState<EventOut[]>([]);
  const [loadInfo, setLoadInfo] = useState<{ forId: string; error: string | null } | null>(null);
  const [sseState, setSseState] = useState<SseState>("connecting");
  const lastIdRef = useRef(0);
  const laneRefs = useRef(new Map<string, HTMLDivElement>());
  const evalRef = useRef<HTMLDivElement>(null);
  const stickRefs = useRef(new Map<string, boolean>());

  const loading = loadInfo?.forId !== runId;
  const fatalError = loadInfo?.forId === runId ? loadInfo.error : null;

  const mergeEvents = useCallback((incoming: EventOut[]) => {
    if (incoming.length === 0) return;
    setEvents((prev) => {
      const map = new Map<number, EventOut>(prev.map((e) => [e.id, e]));
      let changed = false;
      for (const e of incoming) {
        if (!map.has(e.id)) {
          map.set(e.id, e);
          changed = true;
        }
      }
      if (!changed) return prev;
      const arr = [...map.values()].sort((a, b) => a.id - b.id);
      return arr.length > MAX_EVENTS ? arr.slice(arr.length - MAX_EVENTS) : arr;
    });
    const maxId = Math.max(...incoming.map((e) => e.id));
    if (maxId > lastIdRef.current) lastIdRef.current = maxId;
  }, []);

  // initial backlog
  useEffect(() => {
    let cancelled = false;
    (async () => {
      let errMsg: string | null = null;
      try {
        const evs = await listAllEvents(runId);
        if (!cancelled) mergeEvents(evs);
      } catch (err) {
        if (!cancelled) {
          errMsg =
            err instanceof ApiError
              ? err.detail
              : err instanceof Error
                ? err.message
                : "Failed to load events.";
        }
      }
      if (!cancelled) setLoadInfo({ forId: runId, error: errMsg });
    })();
    return () => {
      cancelled = true;
    };
  }, [runId, mergeEvents]);

  // SSE tail
  useEffect(() => {
    const es = new EventSource(eventsStreamUrl(runId));
    es.onopen = () => setSseState("open");
    es.onmessage = (m) => {
      try {
        const ev = JSON.parse(m.data) as EventOut;
        if (typeof ev.id === "number") mergeEvents([ev]);
      } catch {
        // malformed frame — ignore
      }
    };
    es.onerror = () => setSseState("reconnecting");
    return () => es.close();
  }, [runId, mergeEvents]);

  // poll safety-net
  useEffect(() => {
    const t = setInterval(async () => {
      try {
        const page = await listEvents(runId, { after_id: lastIdRef.current, limit: 1000 });
        mergeEvents(page);
      } catch {
        // transient — next tick retries
      }
    }, POLL_MS);
    return () => clearInterval(t);
  }, [runId, mergeEvents]);

  // ---- lane routing ---------------------------------------------------------

  const lanes = useMemo(() => {
    const ids = new Set(run.sandboxes.map((s) => s.id));
    const perSandbox = new Map<string, EventOut[]>();
    const system: EventOut[] = [];
    for (const e of events) {
      // evaluator traffic shares participant creds — route by source, not id
      if (e.source === "evaluator" || e.sandbox_id === null || !ids.has(e.sandbox_id)) {
        system.push(e);
      } else {
        const list = perSandbox.get(e.sandbox_id);
        if (list) list.push(e);
        else perSandbox.set(e.sandbox_id, [e]);
      }
    }
    return { perSandbox, system };
  }, [events, run.sandboxes]);

  // ---- stick-to-bottom per lane --------------------------------------------

  useEffect(() => {
    for (const s of run.sandboxes) {
      const el = laneRefs.current.get(s.id);
      if (el && stickRefs.current.get(s.id) !== false) {
        el.scrollTop = el.scrollHeight;
      }
    }
    const el = evalRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [events, run.sandboxes]);

  function laneScroll(sandboxId: string) {
    const el = laneRefs.current.get(sandboxId);
    if (!el) return;
    const dist = el.scrollHeight - el.scrollTop - el.clientHeight;
    stickRefs.current.set(sandboxId, dist < 60);
  }

  // ---- render ---------------------------------------------------------------

  const live = sseState === "open";
  const runActive = !["done", "failed"].includes(run.status);

  return (
    <div>
      <div className="evc-caption">
        <span className="kicker">
          run console — {runActive ? "live" : "replay"}
        </span>
        <Link href={`/runs/${run.id}/timeline`} className="kicker" style={{ textTransform: "none", letterSpacing: "0.06em" }}>
          open full timeline →
        </Link>
      </div>

      {fatalError ? (
        <div className="notice notice--error" style={{ marginBottom: "0.75rem" }}>
          {fatalError}
        </div>
      ) : null}

      <div className="evc card ticks">
        <span className="tick-b" />
        {/* console header */}
        <div className="evc__head">
          <span className="evc__run">
            <span
              className={`evc-dot${runActive && live ? " evc-dot-live" : ""}`}
              style={runActive && live ? undefined : { background: "var(--text-faint)" }}
            />
            RUN&nbsp;{shortId(run.id)}
          </span>
          <span className="evc__meta">task — “{truncate(run.task_prompt, 48)}”</span>
          <span className="evc__meta">
            model —{" "}
            {runModels(run.model_name, run.config?.models).join(", ")}
          </span>
          <span className="evc__right">
            <span>{events.length} events</span>
            <Badge tone={statusTone(run.status)}>{run.status}</Badge>
            <Badge tone={live ? "green" : sseState === "reconnecting" ? "amber" : "gray"}>
              {live ? "SSE" : sseState === "reconnecting" ? "reconnect" : "…"}
            </Badge>
          </span>
        </div>

        {/* sandbox lanes */}
        {run.sandboxes.length === 0 ? (
          <div className="evc-empty">
            {loading
              ? "loading…"
              : "no sandboxes yet — lanes appear when provisioning starts"}
          </div>
        ) : (
          <div className="evc-lanes">
            {run.sandboxes.map((s) => {
              const spec = laneSpec(s);
              const all = lanes.perSandbox.get(s.id) ?? [];
              const laneEvs = all.slice(-LANE_RENDER_CAP);
              const toks = laneTokens(all);
              const laneDone = Boolean(s.terminal_reason) || s.status === "done";
              return (
                <div key={s.id} className="evc-lane">
                  <div className="evc-lane__head">
                    <span className="evc-lane__id">
                      <Link
                        href={`/runs/${run.id}/timeline?sandbox=${s.id}`}
                        title={s.name ? `${s.name} · ${s.id}` : s.id}
                      >
                        {s.name || shortId(s.id)}
                      </Link>
                    </span>
                    <span className="evc-lane__spec">
                      {spec.adapter}
                      {spec.model ? ` · ${spec.model}` : ""}
                      {spec.packs ? ` · ${spec.packs}` : ""}
                    </span>
                    {s.terminal_reason ? (
                      <span className="evc-lane__state">
                        <Badge tone={statusTone(s.terminal_reason)}>
                          {s.terminal_reason}
                        </Badge>
                      </span>
                    ) : (
                      <span className="evc-lane__state">
                        {s.status === "running" ? "exec" : s.status}
                      </span>
                    )}
                  </div>
                  <div
                    ref={(el) => {
                      if (el) laneRefs.current.set(s.id, el);
                      else laneRefs.current.delete(s.id);
                    }}
                    onScroll={() => laneScroll(s.id)}
                    className="evc-lane__events"
                  >
                    {laneEvs.length === 0 ? (
                      <span className="lp-t-faint">
                        {loading ? "loading…" : "awaiting events…"}
                      </span>
                    ) : (
                      laneEvs.map((e) => (
                        <div key={e.id} className="evc-ev evc-line">
                          <span className="evc-ev__type" data-type={e.type}>
                            {e.type}
                          </span>
                          <span className="evc-ev__text" title={summarize(e)}>
                            {summarize(e)}
                          </span>
                        </div>
                      ))
                    )}
                    {!laneDone && all.length > 0 && (
                      <span className="evc-caret evc-caret-block" />
                    )}
                  </div>
                  <div className="evc-lane__foot">
                    <span>{all.length} events</span>
                    <span>{toks.toLocaleString("en-US")} tok</span>
                    <span className="evc-lane__score">
                      <b className={s.terminal_reason ? "lp-t-signal" : "lp-t-faint"}>
                        {s.terminal_reason ?? s.status}
                      </b>
                    </span>
                  </div>
                </div>
              );
            })}
          </div>
        )}

        {/* evaluator / run-level lane */}
        <div className="evc-eval">
          <div className="evc-eval__head">
            <span className="evc-eval__label">evaluator · run-level</span>
            <span className="evc-eval__sub">
              orchestrator + judge traffic · {lanes.system.length} events
            </span>
          </div>
          <div ref={evalRef} className="evc-eval__events">
            {lanes.system.length === 0 ? (
              <span className="lp-t-faint">
                {loading ? "loading…" : "awaiting events…"}
              </span>
            ) : (
              lanes.system.slice(-LANE_RENDER_CAP).map((e) => (
                <div key={e.id} className="evc-ev evc-line">
                  <span className="evc-ev__type" data-type={e.type}>
                    {e.type}
                  </span>
                  <span className="evc-ev__text" title={summarize(e)}>
                    {summarize(e)}
                  </span>
                </div>
              ))
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
