"use client";

/**
 * F6 — live execution timeline.
 *
 * Data path: initial backlog fetch (GET /events, cursor-paginated) → SSE tail
 * (GET /events/stream via EventSource) → a slow poll (after_id cursor) as a
 * safety net. All three merge through an id-deduped sorted list, so whichever
 * path delivers first wins and nothing renders twice.
 *
 * Filters (source / type / sandbox) are client-side over the loaded window.
 * Rows expand to raw payload JSON; auto-scroll follows the tail unless the
 * user scrolls up ("jump to latest" appears instead).
 */
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ApiError,
  eventsStreamUrl,
  getRun,
  getRunAnalytics,
  listAllEvents,
  listEvents,
  EVENT_SOURCES,
  TERMINAL_RUN_STATUSES,
  type EventOut,
  type RunAnalytics,
} from "@/lib/api";
import { formatTimeMs, shortId, truncate } from "@/lib/format";
import { sandboxStrips } from "@/lib/timelineAnalytics";
import { Badge, Card, EmptyState, ErrorNote, InfoNote, Loading, sourceTone, statusTone } from "@/components/ui";
import SandboxMetricsBar from "./SandboxMetricsBar";

const POLL_MS = 6_000;
/** Metrics strip refresh — heavier query (server re-aggregates all events). */
const ANALYTICS_POLL_MS = 15_000;
const MAX_EVENTS = 10_000;
const STICK_THRESHOLD_PX = 90;

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

function summarize(ev: EventOut): React.ReactNode {
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
// component
// ---------------------------------------------------------------------------

type SseState = "connecting" | "open" | "reconnecting";

export default function TimelineClient({ runId }: { runId: string }) {
  const searchParams = useSearchParams();
  const [events, setEvents] = useState<EventOut[]>([]);
  // Initial-load outcome scoped to runId — derives `loading`/`fatalError`
  // without setState-in-effect and resets correctly across client-side nav.
  const [loadInfo, setLoadInfo] = useState<{ forId: string; error: string | null } | null>(
    null,
  );
  const [sseState, setSseState] = useState<SseState>("connecting");
  const [runStatus, setRunStatus] = useState<string | null>(null);
  const [runMissing, setRunMissing] = useState(false);
  const [analytics, setAnalytics] = useState<RunAnalytics | null>(null);

  // Filters initialize from the URL (?sandbox=, ?source=, ?type=) — the page
  // wraps this component in <Suspense> so useSearchParams is allowed.
  const [filterSource, setFilterSource] = useState(() => searchParams.get("source") ?? "");
  const [filterType, setFilterType] = useState(() => searchParams.get("type") ?? "");
  const [filterSandbox, setFilterSandbox] = useState(() => searchParams.get("sandbox") ?? ""); // "" all | "__none__" run-level | uuid
  const [expanded, setExpanded] = useState<Set<number>>(new Set());

  const loading = loadInfo?.forId !== runId;
  const fatalError = loadInfo?.forId === runId ? loadInfo.error : null;
  const scopedAnalytics = analytics?.run_id === runId ? analytics : null;

  const scrollRef = useRef<HTMLDivElement>(null);
  const stickRef = useRef(true);
  const [atBottom, setAtBottom] = useState(true);
  const lastIdRef = useRef(0);

  // ---- data ingest -------------------------------------------------------

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
      const arr = [...map.values()];
      arr.sort((a, b) => a.id - b.id);
      return arr.length > MAX_EVENTS ? arr.slice(arr.length - MAX_EVENTS) : arr;
    });
    const maxId = Math.max(...incoming.map((e) => e.id));
    if (maxId > lastIdRef.current) lastIdRef.current = maxId;
  }, []);

  // initial backlog + run status
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
      // run status is context only — failure is non-fatal
      try {
        const run = await getRun(runId);
        if (!cancelled) setRunStatus(run.status);
      } catch (err) {
        if (!cancelled && err instanceof ApiError && err.status === 404) {
          setRunMissing(true);
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [runId, mergeEvents]);

  // SSE tail — EventSource auto-reconnects; state reflects connectivity.
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

  // poll safety-net — catches anything SSE misses (reconnect gaps, proxies).
  useEffect(() => {
    const t = setInterval(async () => {
      try {
        const page = await listEvents(runId, {
          after_id: lastIdRef.current,
          limit: 1000,
        });
        mergeEvents(page);
        // API reachable again — clear a stale initial-load error.
        setLoadInfo((prev) =>
          prev && prev.forId === runId && prev.error !== null
            ? { forId: runId, error: null }
            : prev,
        );
      } catch {
        // transient — next tick retries
      }
    }, POLL_MS);
    return () => clearInterval(t);
  }, [runId, mergeEvents]);

  // Metrics strip (PR9): F9 analytics powers the per-sandbox chips. Slower
  // cadence than the event tail (the server re-aggregates the whole stream
  // per call); polling stops once a terminal status is observed — the tick
  // that sees it still lands one final fetch, so end-state numbers are exact.
  useEffect(() => {
    let cancelled = false;
    const tick = async () => {
      // getRun doubles as a status-badge refresh, keeping the header live.
      try {
        const run = await getRun(runId);
        if (!cancelled) setRunStatus(run.status);
      } catch {
        // status refresh is non-fatal — the initial load already flags 404s
      }
      try {
        const a = await getRunAnalytics(runId);
        if (!cancelled) setAnalytics(a);
      } catch {
        // supplementary surface — a failed fetch just leaves the strip empty
      }
    };
    void Promise.resolve().then(tick); // async context → no setState-in-effect
    const t = setInterval(() => {
      if (!TERMINAL_RUN_STATUSES.includes(runStatus ?? "")) void tick();
    }, ANALYTICS_POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(t);
    };
  }, [runId, runStatus]);

  // ---- filtering ---------------------------------------------------------

  const sandboxIds = useMemo(() => {
    const set = new Set<string>();
    for (const sb of scopedAnalytics?.sandboxes ?? []) set.add(sb.sandbox_id);
    for (const e of events) if (e.sandbox_id) set.add(e.sandbox_id);
    if (filterSandbox && filterSandbox !== "__none__") set.add(filterSandbox);
    return [...set].sort();
  }, [events, scopedAnalytics, filterSandbox]);

  const typeOptions = useMemo(() => {
    const set = new Set(events.map((e) => e.type));
    return [...set].sort();
  }, [events]);

  const filtered = useMemo(
    () =>
      events.filter((e) => {
        if (filterSource && e.source !== filterSource) return false;
        if (filterType && e.type !== filterType) return false;
        if (filterSandbox === "__none__" && e.sandbox_id !== null) return false;
        if (filterSandbox && filterSandbox !== "__none__" && e.sandbox_id !== filterSandbox)
          return false;
        return true;
      }),
    [events, filterSource, filterType, filterSandbox],
  );

  // PR9 metrics strip data + adapter labels for the sandbox filter dropdown.
  const strips = useMemo(
    () => (scopedAnalytics ? sandboxStrips(scopedAnalytics) : []),
    [scopedAnalytics],
  );
  const sandboxLabels = useMemo(() => {
    const m = new Map<string, string>();
    for (const sb of scopedAnalytics?.sandboxes ?? [])
      m.set(sb.sandbox_id, sb.label);
    return m;
  }, [scopedAnalytics]);

  // ---- scroll behaviour --------------------------------------------------

  const handleScroll = useCallback(() => {
    const el = scrollRef.current;
    if (!el) return;
    const dist = el.scrollHeight - el.scrollTop - el.clientHeight;
    const stuck = dist < STICK_THRESHOLD_PX;
    stickRef.current = stuck;
    setAtBottom(stuck);
  }, []);

  useEffect(() => {
    const el = scrollRef.current;
    if (el && stickRef.current) {
      el.scrollTop = el.scrollHeight;
    }
  }, [filtered.length, expanded.size]);

  function jumpToLatest() {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
    stickRef.current = true;
    setAtBottom(true);
  }

  function toggleExpand(id: number) {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  // ---- render ------------------------------------------------------------

  const live = sseState === "open";
  const counts = useMemo(() => {
    const m = new Map<string, number>();
    for (const e of events) m.set(e.source, (m.get(e.source) ?? 0) + 1);
    return m;
  }, [events]);

  return (
    <main className="container" style={{ maxWidth: "1280px" }}>
      <div className="page-header">
        <div>
          <span className="kicker"><b>03</b> — event stream · append-only</span>
          <div className="row" style={{ marginBottom: "0.3rem" }}>
            <h1>Timeline</h1>
            <span className="mono faint" title={runId}>
              {shortId(runId, 13)}…
            </span>
            {runStatus ? <Badge tone={statusTone(runStatus)}>{runStatus}</Badge> : null}
            <Badge tone={live ? "green" : sseState === "reconnecting" ? "amber" : "gray"}>
              {live ? "live · SSE" : sseState === "reconnecting" ? "SSE reconnecting · polling" : "loading…"}
            </Badge>
          </div>
          <p className="subtitle">
            <Link href={`/runs/${runId}`}>← run detail</Link>
            {" · "}
            <Link href={`/runs/${runId}/results`}>results</Link>
          </p>
        </div>
      </div>

      {runMissing ? (
        <InfoNote>
          Run <code>{runId}</code> returned 404 — it may not exist, or events exist
          without a run row (ingestion does not require the run lookup).
        </InfoNote>
      ) : null}
      {fatalError ? (
        <ErrorNote title="Could not load events">
          {fatalError}
          <div className="notice__detail">
            The events API may be unreachable — check that the backend is running.
          </div>
        </ErrorNote>
      ) : null}

      <Card className="card--flush">
        <div className="timeline-toolbar">
          <select
            className="select"
            value={filterSource}
            onChange={(e) => setFilterSource(e.target.value)}
            aria-label="Filter by source"
          >
            <option value="">all sources</option>
            {EVENT_SOURCES.map((s) => (
              <option key={s} value={s}>
                {s}
                {counts.get(s) ? ` (${counts.get(s)})` : ""}
              </option>
            ))}
          </select>
          <select
            className="select"
            value={filterType}
            onChange={(e) => setFilterType(e.target.value)}
            aria-label="Filter by type"
          >
            <option value="">all types</option>
            {typeOptions.map((t) => (
              <option key={t} value={t}>
                {t}
              </option>
            ))}
          </select>
          <select
            className="select"
            value={filterSandbox}
            onChange={(e) => setFilterSandbox(e.target.value)}
            aria-label="Filter by sandbox"
          >
            <option value="">all sandboxes</option>
            <option value="__none__">run-level (no sandbox)</option>
            {sandboxIds.map((id) => (
              <option key={id} value={id}>
                sandbox {shortId(id)}
                {sandboxLabels.get(id) ? ` — ${sandboxLabels.get(id)}` : ""}
              </option>
            ))}
          </select>
          {(filterSource || filterType || filterSandbox) && (
            <button
              type="button"
              className="btn btn--sm btn--ghost"
              onClick={() => {
                setFilterSource("");
                setFilterType("");
                setFilterSandbox("");
              }}
            >
              clear filters
            </button>
          )}
          <span className="timeline-stats">
            {filtered.length}/{events.length} events
            {events.length >= MAX_EVENTS ? ` (capped ${MAX_EVENTS})` : ""}
          </span>
        </div>

        <SandboxMetricsBar
          strips={strips}
          selected={filterSandbox}
          onSelect={(id) =>
            setFilterSandbox((cur) => (cur === id ? "" : id))
          }
        />

        <div className="timeline-scroll" ref={scrollRef} onScroll={handleScroll}>
          {loading ? (
            <Loading label="Loading events…" />
          ) : filtered.length === 0 ? (
            <EmptyState>
              {events.length === 0
                ? "No events yet — they appear here live as the run executes."
                : "No events match the current filters."}
            </EmptyState>
          ) : (
            filtered.map((ev, i) => {
              const isOpen = expanded.has(ev.id);
              const prev = i > 0 ? filtered[i - 1] : null;
              const day = ev.ts.slice(5, 10); // MM-DD
              const newDay = !prev || prev.ts.slice(5, 10) !== day;
              const isErr = ev.type === "error";
              return (
                <div key={ev.id}>
                  {newDay ? (
                    <div className="tl-time" style={{ padding: "0.4rem 0.8rem 0.1rem", color: "var(--text-faint)", fontSize: "0.72rem" }}>
                      — {day} —
                    </div>
                  ) : null}
                  <div
                    className={`tl-row${isOpen ? " tl-row--expanded" : ""}${isErr ? " tl-row--error" : ""}`}
                    onClick={() => toggleExpand(ev.id)}
                    role="button"
                    tabIndex={0}
                    onKeyDown={(e) => {
                      if (e.key === "Enter" || e.key === " ") {
                        e.preventDefault();
                        toggleExpand(ev.id);
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
                    {ev.sandbox_id ? (
                      <span className="tl-sandbox" title={ev.sandbox_id}>
                        {shortId(ev.sandbox_id)}
                      </span>
                    ) : (
                      <span className="tl-sandbox faint">run</span>
                    )}
                    <span className="tl-summary">{summarize(ev)}</span>
                    <span className="tl-expand-hint">{isOpen ? "▾" : "▸"}</span>
                  </div>
                  {isOpen ? (
                    <div className="tl-payload">
                      <pre>{JSON.stringify(ev.payload, null, 2)}</pre>
                    </div>
                  ) : null}
                </div>
              );
            })
          )}
        </div>

        {!atBottom && filtered.length > 0 ? (
          <div className="timeline-footer">
            <button type="button" className="btn btn--sm" onClick={jumpToLatest}>
              ↓ jump to latest
            </button>
          </div>
        ) : null}
      </Card>

      <p className="footer-note">
        Click a row for the raw payload · events are append-only — corrections
        arrive as new rows · timestamps are UTC.
      </p>
    </main>
  );
}
