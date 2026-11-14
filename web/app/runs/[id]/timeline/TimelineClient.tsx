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
 *
 * PR10 adds a merged/split view toggle: split renders two sandbox lanes on
 * one merged `(ts, id)` ordering inside the same scroll container —
 * synchronized scroll and time alignment by construction (see
 * lib/splitTimeline.ts + SplitTimeline.tsx). The sandbox filter dropdown is
 * merged-only; in split mode the lanes pick their own sandboxes.
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
import { shortId } from "@/lib/format";
import {
  buildSplitRows,
  laneEventCounts,
  pickLane,
  toggleLane,
  type LaneSelection,
  type LaneSide,
} from "@/lib/splitTimeline";
import { sandboxStrips } from "@/lib/timelineAnalytics";
import { Badge, Card, EmptyState, ErrorNote, InfoNote, Loading, statusTone } from "@/components/ui";
import { DayMarker, EventRow } from "./EventRow";
import SandboxMetricsBar from "./SandboxMetricsBar";
import SplitTimeline from "./SplitTimeline";

const POLL_MS = 6_000;
/** Metrics strip refresh — heavier query (server re-aggregates all events). */
const ANALYTICS_POLL_MS = 15_000;
const MAX_EVENTS = 10_000;
const STICK_THRESHOLD_PX = 90;

type ViewMode = "merged" | "split";

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

  // PR10 split view (?view=split, ?left=, ?right= seed the lanes). Once the
  // user touches a lane (picker, chip, or an explicit URL pair) the auto-
  // default effect below never overwrites their choice.
  const [viewMode, setViewMode] = useState<ViewMode>(() =>
    searchParams.get("view") === "split" ? "split" : "merged",
  );
  const [lanes, setLanes] = useState<LaneSelection>(() => ({
    left: searchParams.get("left") ?? "",
    right: searchParams.get("right") ?? "",
  }));
  const laneTouchedRef = useRef(
    searchParams.get("left") != null && searchParams.get("right") != null,
  );

  const loading = loadInfo?.forId !== runId;
  const fatalError = loadInfo?.forId === runId ? loadInfo.error : null;
  // Same scoping convention as loadInfo.forId — case-insensitive because the
  // route param is verbatim while Postgres serializes UUIDs lowercase.
  const scopedAnalytics =
    analytics?.run_id.toLowerCase() === runId.toLowerCase() ? analytics : null;

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
    // run_id guard (case-insensitive like scopedAnalytics) — key={runId} on the
    // page already remounts per run; this keeps foreign events from leaking a
    // sandbox into the filter options / split lanes if that ever regresses.
    for (const e of events)
      if (e.sandbox_id && e.run_id.toLowerCase() === runId.toLowerCase())
        set.add(e.sandbox_id);
    if (filterSandbox && filterSandbox !== "__none__") set.add(filterSandbox);
    return [...set].sort();
  }, [events, scopedAnalytics, filterSandbox, runId]);

  const typeOptions = useMemo(() => {
    const set = new Set(events.map((e) => e.type));
    return [...set].sort();
  }, [events]);

  const filtered = useMemo(
    () =>
      events.filter((e) => {
        // Same run_id guard as sandboxIds — only this run's events may render.
        if (e.run_id.toLowerCase() !== runId.toLowerCase()) return false;
        if (filterSource && e.source !== filterSource) return false;
        if (filterType && e.type !== filterType) return false;
        // The sandbox dropdown is a merged-view control — in split mode the
        // lane pickers govern which sandboxes appear (their events stay in
        // `filtered` so lane counts and shared rows see the full stream).
        if (viewMode === "merged") {
          if (filterSandbox === "__none__" && e.sandbox_id !== null) return false;
          if (filterSandbox && filterSandbox !== "__none__" && e.sandbox_id !== filterSandbox)
            return false;
        }
        return true;
      }),
    [events, filterSource, filterType, filterSandbox, viewMode, runId],
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

  // PR10 — split-view lanes. Default to the first two discovered sandboxes
  // until the user picks explicitly (laneTouchedRef latches on first touch);
  // the right lane never duplicates the left.
  //
  // Lanes are per-run: on client-side navigation to another run's timeline
  // the component instance is reused, so re-seed from the URL and unlatch
  // auto-default — foreign sandbox ids must not carry over.
  const prevRunIdRef = useRef(runId);
  useEffect(() => {
    if (prevRunIdRef.current === runId) return;
    prevRunIdRef.current = runId;
    laneTouchedRef.current =
      searchParams.get("left") != null && searchParams.get("right") != null;
    setLanes({
      left: searchParams.get("left") ?? "",
      right: searchParams.get("right") ?? "",
    });
  }, [runId, searchParams]);

  useEffect(() => {
    if (laneTouchedRef.current || sandboxIds.length === 0) return;
    setLanes((prev) => {
      if (prev.left && prev.right) return prev;
      const left = prev.left || sandboxIds[0];
      const right = prev.right || sandboxIds.find((id) => id !== left) || "";
      if (left === prev.left && right === prev.right) return prev;
      return { left, right };
    });
  }, [sandboxIds]);

  const splitRows = useMemo(
    () => (viewMode === "split" ? buildSplitRows(filtered, lanes) : []),
    [viewMode, filtered, lanes],
  );
  const splitCounts = useMemo(
    () =>
      viewMode === "split"
        ? laneEventCounts(filtered, lanes)
        : { left: 0, right: 0, shared: 0 },
    [viewMode, filtered, lanes],
  );
  /** Chips active in the metrics strip: merged → the filtered sandbox;
   * split → both lane occupants. */
  const chipSelected = useMemo(() => {
    if (viewMode === "split") {
      const s = new Set<string>();
      if (lanes.left) s.add(lanes.left);
      if (lanes.right) s.add(lanes.right);
      return s;
    }
    return new Set(
      filterSandbox && filterSandbox !== "__none__" ? [filterSandbox] : [],
    );
  }, [viewMode, lanes, filterSandbox]);

  function switchView(mode: ViewMode) {
    setViewMode(mode);
    // Carry a single-sandbox merged filter into the left lane as a starting
    // point — existing lane picks are never overwritten, and the auto-
    // default still fills whichever lane stays empty.
    if (mode === "split" && filterSandbox && filterSandbox !== "__none__") {
      setLanes((prev) => ({ ...prev, left: prev.left || filterSandbox }));
    }
  }

  const onPickLane = useCallback((side: LaneSide, sandboxId: string) => {
    laneTouchedRef.current = true;
    setLanes((prev) => pickLane(prev, side, sandboxId));
  }, []);

  /** Metrics-chip click: merged → toggle the sandbox filter; split → lane
   * assignment (occupant clears, new id fills first empty lane else right). */
  const onChipSelect = useCallback(
    (id: string) => {
      if (viewMode === "split") {
        laneTouchedRef.current = true;
        setLanes((prev) => toggleLane(prev, id));
      } else {
        setFilterSandbox((cur) => (cur === id ? "" : id));
      }
    },
    [viewMode],
  );

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
            {" · "}
            <Link href={`/runs/${runId}/analysis`}>analysis</Link>
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
          <div className="tl-viewtoggle" role="group" aria-label="Timeline view">
            <button
              type="button"
              className={viewMode === "merged" ? "is-active" : ""}
              onClick={() => switchView("merged")}
              aria-pressed={viewMode === "merged"}
            >
              merged
            </button>
            <button
              type="button"
              className={viewMode === "split" ? "is-active" : ""}
              onClick={() => switchView("split")}
              aria-pressed={viewMode === "split"}
              title="Two sandbox lanes on one time axis — pick lanes below or via the metrics chips"
            >
              split
            </button>
          </div>
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
          {viewMode === "merged" ? (
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
          ) : null}
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
          selectedIds={chipSelected}
          onSelect={onChipSelect}
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
          ) : viewMode === "split" ? (
            <SplitTimeline
              rows={splitRows}
              lanes={lanes}
              sandboxIds={sandboxIds}
              labels={sandboxLabels}
              strips={strips}
              counts={splitCounts}
              expanded={expanded}
              onToggle={toggleExpand}
              onPickLane={onPickLane}
            />
          ) : (
            filtered.map((ev, i) => {
              const prev = i > 0 ? filtered[i - 1] : null;
              const day = ev.ts.slice(5, 10); // MM-DD
              const newDay = !prev || prev.ts.slice(5, 10) !== day;
              return (
                <div key={ev.id}>
                  {newDay ? <DayMarker day={day} /> : null}
                  <EventRow
                    ev={ev}
                    expanded={expanded.has(ev.id)}
                    onToggle={toggleExpand}
                  />
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
        {viewMode === "split"
          ? "Split view — both lanes share one time axis; run-level and other-sandbox events span both columns. "
          : ""}
        Click a row for the raw payload · events are append-only — corrections
        arrive as new rows · timestamps are UTC.
      </p>
    </main>
  );
}
