"use client";

/**
 * F13 — graphical analysis page: charts over the F9 analytics + F8 scores.
 *
 * Fetches run + results + analytics + cross-run trends in parallel; each
 * section degrades to an InfoNote if its endpoint is unavailable (the
 * trends endpoint is new — older backends 404 it). Cost charts recompute
 * client-side from tokens × price_table for the selected price model —
 * same trick as the Analytics table, no refetch.
 *
 * Charts: score-by-dimension (grouped), tool-call mix (stacked), tokens
 * (stacked in/out), latency (avg/p95/max), estimated cost, participant-vs-
 * evaluator spend split, and a cross-run trend (tokens + mean score over
 * the last 20 runs with the current run highlighted).
 */
import Link from "next/link";
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  ApiError,
  CANONICAL_DIMENSIONS,
  estimateCostUsd,
  getRun,
  getRunAnalytics,
  getRunResults,
  getRunTrends,
  TERMINAL_RUN_STATUSES,
  type ModelPrice,
  type RunAnalytics,
  type RunOut,
  type RunResults,
  type RunTrends,
} from "@/lib/api";
import { formatNum, formatUsd, shortId } from "@/lib/format";
import {
  Badge,
  Card,
  EmptyState,
  ErrorNote,
  InfoNote,
  Loading,
  statusTone,
} from "@/components/ui";
import {
  C,
  ColoredBars,
  HBarGroup,
  MIX_COLORS,
  SANDBOX_COLORS,
  SeriesChips,
  TrendChart,
  VBars,
  type Row,
  type Series,
} from "./charts";

const POLL_MS = 10_000;
const TREND_LIMIT = 20;
const TOOL_TOP_N = 7; // beyond this, tool bars collapse into "other"

function errMsg(err: unknown, fallback: string): string {
  if (err instanceof ApiError) return err.detail;
  if (err instanceof Error) return err.message;
  return fallback;
}

/** Order dimension rows: canonical rubric dimensions first, then alpha.
 * (Mirrors ResultsClient's dimensionOrder — keep in sync.) */
function dimensionOrder(keys: string[]): string[] {
  const canon = CANONICAL_DIMENSIONS.filter((d) => keys.includes(d));
  const rest = keys.filter((d) => !CANONICAL_DIMENSIONS.includes(d)).sort();
  return [...canon, ...rest];
}

/** adapter · pack1, pack2 — mirrors analytics._sandbox_label. */
function hcLabel(hc: Record<string, unknown> | undefined): string {
  const adapter = String(hc?.adapter ?? hc?.harness ?? "?");
  const packs = Array.isArray(hc?.skill_packs) ? (hc.skill_packs as unknown[]) : [];
  return packs.length ? `${adapter} · ${packs.join(", ")}` : adapter;
}

/** "2026-11-14T03:…" → "11-14" (UTC slice, hydration-safe). */
function dayTag(iso: string | null | undefined): string {
  const m = /^\d{4}-(\d{2}-\d{2})T/.exec(iso ?? "");
  return m ? m[1] : "??-??";
}

interface Bundle {
  run: RunOut | null;
  results: RunResults | null;
  analytics: RunAnalytics | null;
  trends: RunTrends | null;
}

export default function AnalysisClient({ runId }: { runId: string }) {
  const [data, setData] = useState<Bundle>({
    run: null,
    results: null,
    analytics: null,
    trends: null,
  });
  const [loadedFor, setLoadedFor] = useState<string | null>(null);
  const [fatal, setFatal] = useState<string | null>(null);
  const [notes, setNotes] = useState<{ results?: string; analytics?: string; trends?: string }>({});
  const [priceModel, setPriceModel] = useState<string | null>(null);

  const refresh = useCallback(
    async (silent = true) => {
      const [r, res, an, tr] = await Promise.allSettled([
        getRun(runId),
        getRunResults(runId),
        getRunAnalytics(runId),
        getRunTrends({ limit: TREND_LIMIT }),
      ]);
      if (r.status === "fulfilled") {
        setData((d) => ({ ...d, run: r.value }));
        setFatal(null);
      } else if (!silent) {
        setFatal(errMsg(r.reason, "Run not found."));
      }
      setData((d) => ({
        ...d,
        results: res.status === "fulfilled" ? res.value : null,
        analytics: an.status === "fulfilled" ? an.value : null,
        trends: tr.status === "fulfilled" ? tr.value : null,
      }));
      setNotes({
        results: res.status === "rejected" ? errMsg(res.reason, "results unavailable") : undefined,
        analytics:
          an.status === "rejected" ? errMsg(an.reason, "analytics unavailable") : undefined,
        trends: tr.status === "rejected" ? errMsg(tr.reason, "trends unavailable") : undefined,
      });
      setLoadedFor(runId);
    },
    [runId],
  );

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      await Promise.resolve(); // async context → no setState-in-effect
      if (!cancelled) await refresh(false);
    })();
    return () => {
      cancelled = true;
    };
  }, [runId, refresh]);

  const loaded = loadedFor === runId;
  const status = data.run?.status ?? data.results?.status ?? "";
  useEffect(() => {
    if (!loaded || !status || TERMINAL_RUN_STATUSES.includes(status)) return;
    const t = setInterval(() => void refresh(true), POLL_MS);
    return () => clearInterval(t);
  }, [loaded, status, refresh]);

  // ---- derived chart data --------------------------------------------------

  const sbs = useMemo(
    () => (loaded ? (data.analytics?.sandboxes ?? []) : []),
    [loaded, data.analytics],
  );
  const resSandboxes = useMemo(
    () => (loaded ? (data.results?.sandboxes ?? []) : []),
    [loaded, data.results],
  );

  /** Stable sandbox order — analytics order, else results order — so every
   * chart colors a given sandbox identically. */
  const sbSeries: Series[] = useMemo(() => {
    const order = sbs.length
      ? sbs.map((s) => s.sandbox_id)
      : resSandboxes.map((s) => s.sandbox_id);
    return order.map((id, i) => ({
      key: id,
      name: shortId(id),
      color: SANDBOX_COLORS[i % SANDBOX_COLORS.length],
    }));
  }, [sbs, resSandboxes]);

  const labelOf = useCallback(
    (id: string) =>
      sbs.find((s) => s.sandbox_id === id)?.label ??
      hcLabel(resSandboxes.find((s) => s.sandbox_id === id)?.harness_config),
    [sbs, resSandboxes],
  );

  const sbChips = useMemo(
    () => sbSeries.map((s) => ({ color: s.color, label: `${s.name} · ${labelOf(s.key)}` })),
    [sbSeries, labelOf],
  );

  /** Score-by-dimension rows: {dim, [sandbox_id]: score}. */
  const scoreRows: Row[] = useMemo(() => {
    const keys = new Set<string>();
    for (const sb of resSandboxes) {
      for (const d of Object.keys(sb.dimensions ?? {})) keys.add(d);
    }
    return dimensionOrder([...keys]).map((dim) => {
      const row: Row = { dim };
      for (const sb of resSandboxes) {
        const e = sb.dimensions?.[dim];
        if (e) row[sb.sandbox_id] = e.score;
      }
      return row;
    });
  }, [resSandboxes]);

  /** Computed dimension totals → the at-a-glance leader (matches the
   * results-matrix totals row). */
  const leader = useMemo(() => {
    let best: { id: string; sum: number } | null = null;
    for (const sb of resSandboxes) {
      let sum = 0;
      let count = 0;
      for (const row of scoreRows) {
        const e = sb.dimensions?.[String(row.dim)];
        if (e) {
          sum += e.score;
          count++;
        }
      }
      if (count > 0 && (best === null || sum > best.sum)) best = { id: sb.sandbox_id, sum };
    }
    return best;
  }, [resSandboxes, scoreRows]);

  const tokenRows: Row[] = useMemo(
    () =>
      sbs.map((sb) => ({
        name: shortId(sb.sandbox_id),
        input: sb.tokens.input,
        output: sb.tokens.output,
      })),
    [sbs],
  );

  const latencyRows: Row[] = useMemo(
    () =>
      sbs.map((sb) => ({
        name: shortId(sb.sandbox_id),
        avg: sb.latency_ms.avg ?? 0,
        p95: sb.latency_ms.p95 ?? 0,
        max: sb.latency_ms.max ?? 0,
      })),
    [sbs],
  );

  // Cost recompute — mirrors the server: participant at the selected price,
  // evaluator at the evaluator model's rate when listed, else the selected.
  const selected = priceModel ?? data.analytics?.price_model ?? "";
  const price: ModelPrice | undefined = data.analytics?.price_table?.[selected];
  const evaluatorModel =
    data.results?.evaluator_model ?? data.run?.config?.evaluator_model ?? null;
  const evalPrice: ModelPrice | undefined =
    data.analytics?.price_table?.[evaluatorModel ?? ""] ?? price;

  const costRows: Row[] = useMemo(
    () =>
      sbs.map((sb, i) => ({
        name: shortId(sb.sandbox_id),
        color: SANDBOX_COLORS[i % SANDBOX_COLORS.length],
        value: price ? estimateCostUsd(price, sb.tokens) : 0,
      })),
    [sbs, price],
  );

  const splitRows: Row[] = useMemo(
    () =>
      sbs.map((sb) => ({
        name: shortId(sb.sandbox_id),
        participant: price ? estimateCostUsd(price, sb.tokens) : 0,
        evaluator:
          sb.evaluator && evalPrice ? estimateCostUsd(evalPrice, sb.evaluator.tokens) : 0,
      })),
    [sbs, price, evalPrice],
  );

  /** Tool-call mix: top-N tool names across sandboxes + "other". */
  const { toolRows, toolSeries } = useMemo(() => {
    const totals = new Map<string, number>();
    for (const sb of sbs) {
      for (const [t, c] of Object.entries(sb.tool_calls.by_tool)) {
        totals.set(t, (totals.get(t) ?? 0) + c);
      }
    }
    const ranked = [...totals.entries()].sort((a, b) => b[1] - a[1]);
    const top = ranked.slice(0, TOOL_TOP_N).map(([t]) => t);
    const rest = new Set(ranked.slice(TOOL_TOP_N).map(([t]) => t));
    const rows = sbs.map((sb) => {
      const row: Row = { name: shortId(sb.sandbox_id) };
      let other = 0;
      for (const [t, c] of Object.entries(sb.tool_calls.by_tool)) {
        if (rest.has(t)) other += c;
        else row[t] = c;
      }
      if (other) row.other = other;
      return row;
    });
    const series: Series[] = top.map((t, i) => ({
      key: t,
      name: t,
      color: MIX_COLORS[i % MIX_COLORS.length],
    }));
    if (rest.size) series.push({ key: "other", name: "other", color: C.faint });
    return { toolRows: rows, toolSeries: series };
  }, [sbs]);

  /** Cross-run trend rows — oldest → newest for a left-to-right timeline. */
  const trendRows: Row[] = useMemo(() => {
    const pts = data.trends?.points ?? [];
    return pts
      .slice()
      .reverse()
      .map((p) => ({
        label: `${dayTag(p.created_at)}·${shortId(p.run_id, 6)}`,
        participant: p.tokens.total,
        evaluator: p.evaluator_tokens.total,
        score: p.mean_overall_score,
        isCurrent: p.run_id === runId,
        meta: p,
      }));
  }, [data.trends, runId]);

  // ---- render --------------------------------------------------------------

  if (!loaded) {
    return (
      <main className="container">
        <Loading label="Loading analysis…" />
      </main>
    );
  }

  const anyToolCalls = sbs.some((sb) => sb.tool_calls.total > 0);
  const anyLatency = sbs.some((sb) => sb.latency_ms.avg != null);
  const anyEval = sbs.some((sb) => sb.evaluator != null && sb.evaluator.llm_turns > 0);

  return (
    <main className="container">
      <div className="page-header">
        <div>
          <span className="kicker">
            <b>05</b> — graphical analysis · cross-run trends
          </span>
          <div className="row" style={{ marginBottom: "0.3rem" }}>
            <h1>Analysis</h1>
            <span className="mono faint" title={runId}>
              {shortId(runId, 13)}…
            </span>
            {status ? <Badge tone={statusTone(status)}>{status}</Badge> : null}
          </div>
          <p className="subtitle">
            <Link href={`/runs/${runId}`}>← run detail</Link>
            {" · "}
            <Link href={`/runs/${runId}/timeline`}>timeline</Link>
            {" · "}
            <Link href={`/runs/${runId}/results`}>results</Link>
            {data.run?.task_prompt ? (
              <>
                {" "}
                · {data.run.task_prompt.slice(0, 80)}
                {data.run.task_prompt.length > 80 ? "…" : ""}
              </>
            ) : null}
          </p>
        </div>
      </div>

      {fatal ? <ErrorNote title="Could not load run">{fatal}</ErrorNote> : null}

      <div className="analysis-grid">
        <Card title="Score by rubric dimension" className="card--flush">
          {resSandboxes.length === 0 || scoreRows.length === 0 ? (
            notes.results ? (
              <InfoNote>Scores unavailable: {notes.results}</InfoNote>
            ) : (
              <EmptyState>No scores yet — they appear after evaluation.</EmptyState>
            )
          ) : (
            <>
              <HBarGroup
                rows={scoreRows}
                series={sbSeries}
                catKey="dim"
                domain={[0, 10]}
                fmt={(v) => v.toFixed(1)}
              />
              <SeriesChips items={sbChips} />
              {leader ? (
                <p className="chart-legend">
                  Σ dimensions leader:{" "}
                  <strong style={{ color: "var(--accent)" }}>
                    {shortId(leader.id)} ({Math.round(leader.sum * 10) / 10})
                  </strong>
                </p>
              ) : null}
            </>
          )}
        </Card>

        <Card title="Tool-call mix" className="card--flush">
          {notes.analytics && !data.analytics ? (
            <InfoNote>Metrics unavailable: {notes.analytics}</InfoNote>
          ) : !anyToolCalls ? (
            <EmptyState>No tool calls recorded.</EmptyState>
          ) : (
            <>
              <VBars rows={toolRows} series={toolSeries} stacked fmt={(v) => formatNum(v)} />
              <SeriesChips
                items={toolSeries.map((s) => ({ color: s.color, label: s.name }))}
              />
            </>
          )}
        </Card>

        <Card title="Tokens per sandbox" className="card--flush">
          {notes.analytics && !data.analytics ? (
            <InfoNote>Metrics unavailable: {notes.analytics}</InfoNote>
          ) : sbs.length === 0 ? (
            <EmptyState>No sandbox metrics yet.</EmptyState>
          ) : (
            <>
              <VBars
                rows={tokenRows}
                series={[
                  { key: "input", name: "tokens in", color: C.blue },
                  { key: "output", name: "tokens out", color: C.violet },
                ]}
                stacked
                fmt={(v) => formatNum(v)}
              />
              <SeriesChips
                items={[
                  { color: C.blue, label: "in" },
                  { color: C.violet, label: "out" },
                ]}
              />
            </>
          )}
        </Card>

        <Card title="LLM latency (ms)" className="card--flush">
          {notes.analytics && !data.analytics ? (
            <InfoNote>Metrics unavailable: {notes.analytics}</InfoNote>
          ) : !anyLatency ? (
            <EmptyState>No LLM responses recorded.</EmptyState>
          ) : (
            <>
              <VBars
                rows={latencyRows}
                series={[
                  { key: "avg", name: "avg", color: C.teal },
                  { key: "p95", name: "p95", color: C.amber },
                  { key: "max", name: "max", color: C.red },
                ]}
                fmt={(v) => `${formatNum(Math.round(v))}`}
              />
              <SeriesChips
                items={[
                  { color: C.teal, label: "avg" },
                  { color: C.amber, label: "p95" },
                  { color: C.red, label: "max" },
                ]}
              />
            </>
          )}
        </Card>

        <Card
          title="Estimated cost per sandbox"
          className="card--flush"
          actions={
            <label className="row" style={{ gap: "0.4rem", fontSize: "0.8rem" }}>
              <span className="faint">at</span>
              <select
                className="select"
                style={{ width: "auto", padding: "0.3rem 0.5rem", fontSize: "0.8rem" }}
                value={selected}
                onChange={(e) => setPriceModel(e.target.value)}
                aria-label="Estimated cost at"
              >
                {Object.entries(data.analytics?.price_table ?? {}).map(([name, p]) => (
                  <option key={name} value={name}>
                    {p.local ? `${name} (local — $0)` : name}
                  </option>
                ))}
              </select>
            </label>
          }
        >
          {notes.analytics && !data.analytics ? (
            <InfoNote>Metrics unavailable: {notes.analytics}</InfoNote>
          ) : sbs.length === 0 ? (
            <EmptyState>No sandbox metrics yet.</EmptyState>
          ) : price?.local ? (
            <InfoNote>
              Local model — $0 participant spend at <code>{selected}</code>. Pick a
              priced model above for an API-equivalent estimate.
            </InfoNote>
          ) : !costRows.some((r) => Number(r.value) > 0) ? (
            <EmptyState>No billable token usage recorded.</EmptyState>
          ) : (
            <>
              <ColoredBars rows={costRows} dataKey="value" name="est. cost" fmt={formatUsd} />
              <SeriesChips items={sbChips} />
            </>
          )}
        </Card>

        <Card title="Participant vs evaluator spend" className="card--flush">
          {notes.analytics && !data.analytics ? (
            <InfoNote>Metrics unavailable: {notes.analytics}</InfoNote>
          ) : sbs.length === 0 ? (
            <EmptyState>No sandbox metrics yet.</EmptyState>
          ) : !anyEval ? (
            <EmptyState>No evaluator traffic recorded for this run.</EmptyState>
          ) : !splitRows.some(
              (r) => Number(r.participant) > 0 || Number(r.evaluator) > 0,
            ) ? (
            <InfoNote>
              Participant and judge spend are both $0 at the current price basis
              — pick a priced model in the cost card to compare.
            </InfoNote>
          ) : (
            <>
              <VBars
                rows={splitRows}
                series={[
                  { key: "participant", name: "participant", color: C.accent },
                  { key: "evaluator", name: "judge", color: C.amber },
                ]}
                stacked
                fmt={formatUsd}
              />
              <p className="chart-legend">
                judge priced at {evaluatorModel ?? selected} when listed ·
                participant at {selected}
              </p>
            </>
          )}
        </Card>

        <Card title="Cross-run trends" className="card--flush card--span2">
          {notes.trends && !data.trends ? (
            <InfoNote>
              Cross-run trends unavailable: {notes.trends} (needs a backend with{" "}
              <code>GET /api/analytics/runs</code>)
            </InfoNote>
          ) : trendRows.length === 0 ? (
            <EmptyState>No runs to trend yet.</EmptyState>
          ) : (
            <>
              <TrendChart rows={trendRows} />
              <SeriesChips
                items={[
                  { color: C.accent, label: "this run · participant tokens" },
                  { color: C.blue, label: "participant tokens" },
                  { color: C.amber, label: "evaluator tokens" },
                  { color: C.violet, label: "mean overall score (right axis, 0–10)" },
                ]}
              />
              <p className="chart-legend">
                newest {TREND_LIMIT} runs, oldest → newest left to right · hover
                a bar for run details
              </p>
            </>
          )}
        </Card>
      </div>
    </main>
  );
}
