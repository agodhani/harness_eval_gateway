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
  dimensionOrder,
  estimateCostUsd,
  getRun,
  getRunAnalytics,
  getRunResults,
  getRunTrends,
  harnessConfigLabel,
  matchPriceKey,
  resolvePricePick,
  TERMINAL_RUN_STATUSES,
  type ModelPrice,
  type PriceEntry,
  type RunAnalytics,
  type RunOut,
  type RunResults,
  type RunTrends,
} from "@/lib/api";
import { latencyChartRows, speedChartRows } from "@/lib/analysisRows";
import { formatNum, formatUsd, shortId } from "@/lib/format";
import { dimensionSpecMap, weightedDimTotal } from "@/lib/scoreMath";
import { fmtMeanSd } from "@/lib/trialGroups";
import {
  Badge,
  Card,
  EmptyState,
  ErrorNote,
  InfoNote,
  Loading,
  statusTone,
} from "@/components/ui";
import AddRate from "@/components/AddRate";
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
  // PR28: rates added inline via "+ add rate" merge into the picker table —
  // the analytics payload was fetched before they existed (they persist
  // server-side as model_prices rows for the next fetch).
  const [extraPrices, setExtraPrices] = useState<Record<string, ModelPrice>>({});

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

  /** PR27: cells with more than one trial — the variance table only makes
   * sense when a run actually repeated matrix cells. */
  const trialCells = useMemo(
    () => (data.analytics?.cells ?? []).filter((c) => c.sandbox_count > 1),
    [data.analytics],
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
      sbs.find((s) => s.sandbox_id === id)?.name ??
      resSandboxes.find((s) => s.sandbox_id === id)?.name ??
      sbs.find((s) => s.sandbox_id === id)?.label ??
      harnessConfigLabel(
        resSandboxes.find((s) => s.sandbox_id === id)?.harness_config,
      ),
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

  /** Computed dimension totals → the at-a-glance leader(s) (matches the
   * results-matrix totals row: weighted score/max × weight on 0–10, same
   * rubric-dimension spec — every sandbox tied at the max leads). */
  const leaders = useMemo(() => {
    const specMap = dimensionSpecMap(data.run?.rubric?.content?.dimensions);
    const keys = scoreRows.map((r) => String(r.dim));
    const totals: { id: string; total: number }[] = [];
    for (const sb of resSandboxes) {
      const t = weightedDimTotal(sb.dimensions, keys, specMap);
      if (t.scored > 0) totals.push({ id: sb.sandbox_id, total: t.total });
    }
    const max = Math.max(0, ...totals.map((s) => s.total));
    const ids = totals.filter((s) => s.total === max).map((s) => s.id);
    return ids.length ? { ids, total: max } : null;
  }, [resSandboxes, scoreRows, data.run]);

  const tokenRows: Row[] = useMemo(
    () =>
      sbs.map((sb) => ({
        name: shortId(sb.sandbox_id),
        input: sb.tokens.input,
        output: sb.tokens.output,
      })),
    [sbs],
  );

  const latencyRows: Row[] = useMemo(() => latencyChartRows(sbs), [sbs]);

  // PR30 speed axis: mean output tok/s per cell — the model-comparison
  // chart for how fast each lane actually generates.
  const speedRows: Row[] = useMemo(() => speedChartRows(sbs), [sbs]);

  /** PR28: the effective picker table — server price_table plus rates added
   * inline this session (persisted server-side as model_prices rows). */
  const priceTable = useMemo(
    () => ({ ...(data.analytics?.price_table ?? {}), ...extraPrices }),
    [data.analytics, extraPrices],
  );
  /** Row globs in declaration order — the fetched list plus rates added
   * inline this session (they land at the end, a new row's position). */
  const priceGlobs = useMemo(
    () => [...(data.analytics?.price_globs ?? []), ...Object.keys(extraPrices)],
    [data.analytics, extraPrices],
  );
  // An explicit pick resolves like the server's resolve_price_model — a
  // verbatim row glob (any case) selects that row; a model-like pick goes
  // through the row-glob scan so an earlier glob can shadow a static key.
  const resolvedPick = useMemo(
    () => resolvePricePick(priceModel, priceTable, priceGlobs),
    [priceModel, priceTable, priceGlobs],
  );

  // Cost recompute — mirrors the server: an explicit pick reprices every
  // sandbox uniformly; auto ("") resolves each sandbox's model through the
  // layered book itself (row globs first — including rates added inline
  // this session, which the fetch-time sb.price_model can't see — then
  // the static fallback; unlisted → unpriced, PR21).
  const priceFor = useCallback(
    (sb: { model: string; price_model?: string | null }) => {
      const key = priceModel
        ? resolvedPick
        : matchPriceKey(sb.model, priceTable, priceGlobs);
      return key ? priceTable[key] : undefined;
    },
    [priceTable, priceModel, priceGlobs, resolvedPick],
  );
  const evaluatorModel =
    data.results?.evaluator_model ??
    data.run?.config?.evaluator_model ??
    data.run?.model_name ??
    data.analytics?.model_name ??
    null;
  // Judge price mirrors the server: the evaluator model's own resolved
  // rate (glob-aware via matchPriceKey, PR28), else the explicit pick
  // (what-if) — unlisted and unpicked means unpriced, never another
  // model's rate (PR21).
  const evalKey = useMemo(
    () => matchPriceKey(evaluatorModel, priceTable, priceGlobs),
    [evaluatorModel, priceTable, priceGlobs],
  );
  const evalPrice = useMemo(() => {
    if (evalKey !== undefined) return priceTable[evalKey];
    if (resolvedPick !== undefined) return priceTable[resolvedPick];
    return undefined;
  }, [evalKey, priceTable, resolvedPick]);

  const costRows: Row[] = useMemo(
    () =>
      sbs.map((sb, i) => {
        const p = priceFor(sb);
        return {
          name: shortId(sb.sandbox_id),
          color: SANDBOX_COLORS[i % SANDBOX_COLORS.length],
          value: p ? estimateCostUsd(p, sb.tokens) : 0,
        };
      }),
    [sbs, priceFor],
  );

  /** PR21: sandboxes whose model isn't in the price table — their cost
   * renders as 0, so the card says "unpriced" rather than implying $0. */
  const unpricedCount = useMemo(
    () => sbs.filter((sb) => !priceFor(sb)).length,
    [sbs, priceFor],
  );

  const splitRows: Row[] = useMemo(
    () =>
      sbs.map((sb) => {
        const p = priceFor(sb);
        return {
          name: shortId(sb.sandbox_id),
          participant: p ? estimateCostUsd(p, sb.tokens) : 0,
          evaluator:
            sb.evaluator && evalPrice
              ? estimateCostUsd(evalPrice, sb.evaluator.tokens)
              : 0,
        };
      }),
    [sbs, priceFor, evalPrice],
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
  const anyTtft = sbs.some((sb) => sb.ttft_ms?.avg != null);
  const anySpeed = sbs.some((sb) => sb.avg_tokens_per_sec != null);
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
              {leaders ? (
                <p className="chart-legend">
                  weighted total leader{leaders.ids.length > 1 ? "s (tie)" : ""}:{" "}
                  <strong style={{ color: "var(--accent)" }}>
                    {leaders.ids.map((id) => shortId(id)).join(", ")} (
                    {Math.round(leaders.total * 10) / 10})
                  </strong>
                </p>
              ) : null}
            </>
          )}
        </Card>

        {trialCells.length > 0 ? (
          <Card title="Score variance by cell" className="card--flush">
            <div className="table-wrap">
              <table className="table">
                <thead>
                  <tr>
                    <th>Cell</th>
                    <th>Model</th>
                    <th>Trials</th>
                    <th>Overall mean ±sd</th>
                  </tr>
                </thead>
                <tbody>
                  {trialCells.map((c) => (
                    <tr key={c.cell}>
                      <td>
                        <div className="dim-name">{c.label}</div>
                      </td>
                      <td className="mono" style={{ fontSize: "0.82rem" }}>
                        {c.model}
                      </td>
                      <td className="num">{c.sandbox_count}</td>
                      <td className="num">
                        {c.overall_mean != null ? (
                          <span className="mono" style={{ fontSize: "0.82rem" }}>
                            {fmtMeanSd({
                              mean: c.overall_mean,
                              sd: c.overall_sd,
                              n: c.overall_n,
                            })}
                          </span>
                        ) : (
                          <span className="score-na">—</span>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <p className="chart-legend">
              Sample sd (n−1) across the cell&apos;s trial scores — null when
              fewer than two trials were judged.
            </p>
          </Card>
        ) : null}

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

        <Card title="LLM latency / TTFT (ms)" className="card--flush">
          {notes.analytics && !data.analytics ? (
            <InfoNote>Metrics unavailable: {notes.analytics}</InfoNote>
          ) : !anyLatency ? (
            <EmptyState>No LLM responses recorded.</EmptyState>
          ) : (
            <>
              <VBars
                rows={latencyRows}
                series={[
                  // TTFT bars first — first-byte time precedes the rest of
                  // the call; only rendered when the gateway records it.
                  ...(anyTtft
                    ? [
                        { key: "ttft_avg", name: "ttft avg", color: C.blue },
                        { key: "ttft_p95", name: "ttft p95", color: C.violet },
                      ]
                    : []),
                  { key: "avg", name: "avg", color: C.teal },
                  { key: "p95", name: "p95", color: C.amber },
                  { key: "max", name: "max", color: C.red },
                ]}
                fmt={(v) => `${formatNum(Math.round(v))}`}
              />
              <SeriesChips
                items={[
                  ...(anyTtft
                    ? [
                        { color: C.blue, label: "ttft avg" },
                        { color: C.violet, label: "ttft p95" },
                      ]
                    : []),
                  { color: C.teal, label: "avg" },
                  { color: C.amber, label: "p95" },
                  { color: C.red, label: "max" },
                ]}
              />
            </>
          )}
        </Card>

        <Card title="Output speed (avg tok/s)" className="card--flush">
          {notes.analytics && !data.analytics ? (
            <InfoNote>Metrics unavailable: {notes.analytics}</InfoNote>
          ) : !anySpeed ? (
            <EmptyState>No output-rate measurements recorded.</EmptyState>
          ) : (
            <>
              <VBars
                rows={speedRows}
                series={[{ key: "tps", name: "avg tok/s", color: C.blue }]}
                fmt={(v) => `${formatNum(v)} t/s`}
              />
              <SeriesChips
                items={[{ color: C.blue, label: "completion tokens ÷ call wall time" }]}
              />
            </>
          )}
        </Card>

        <Card
          title="Estimated cost per sandbox"
          className="card--flush"
          actions={
            <div className="row" style={{ gap: "0.6rem", flexWrap: "wrap" }}>
              <label className="row" style={{ gap: "0.4rem", fontSize: "0.8rem" }}>
                <span className="faint">at</span>
                <select
                  className="select"
                  style={{ width: "auto", padding: "0.3rem 0.5rem", fontSize: "0.8rem" }}
                  value={priceModel ?? ""}
                  onChange={(e) => setPriceModel(e.target.value || null)}
                  aria-label="Estimated cost at"
                >
                  <option value="">auto — per-model</option>
                  {Object.entries(priceTable).map(([name, p]) => (
                    <option key={name} value={name}>
                      {p.local ? `${name} (local — $0)` : name}
                    </option>
                  ))}
                </select>
              </label>
              <AddRate
                suggestGlob={
                  sbs.find(
                    (sb) =>
                      matchPriceKey(sb.model, priceTable, priceGlobs) ===
                      undefined,
                  )?.model ?? ""
                }
                onAdded={(entry: PriceEntry) => {
                  setExtraPrices((m) => ({
                    ...m,
                    [entry.model_glob]: {
                      input_per_mtok_usd: entry.input_per_mtok_usd,
                      output_per_mtok_usd: entry.output_per_mtok_usd,
                      cache_read_per_mtok_usd: entry.cache_read_per_mtok_usd,
                      local: entry.local,
                    },
                  }));
                  setPriceModel(entry.model_glob);
                }}
              />
            </div>
          }
        >
          {notes.analytics && !data.analytics ? (
            <InfoNote>Metrics unavailable: {notes.analytics}</InfoNote>
          ) : sbs.length === 0 ? (
            <EmptyState>No sandbox metrics yet.</EmptyState>
          ) : resolvedPick !== undefined && priceTable[resolvedPick]?.local ? (
            <InfoNote>
              Local model — $0 participant spend at <code>{resolvedPick}</code>. Pick a
              priced model above for an API-equivalent estimate.
            </InfoNote>
          ) : !costRows.some((r) => Number(r.value) > 0) ? (
            unpricedCount > 0 ? (
              <InfoNote>
                {unpricedCount} of {sbs.length} sandbox
                {unpricedCount === 1 ? "" : "es"} ran a model that isn&apos;t in the
                price table — cost is unpriced. Pick a priced model above for a
                what-if estimate.
              </InfoNote>
            ) : (
              <EmptyState>No billable token usage recorded.</EmptyState>
            )
          ) : (
            <>
              <ColoredBars rows={costRows} dataKey="value" name="est. cost" fmt={formatUsd} />
              <SeriesChips items={sbChips} />
              {unpricedCount > 0 ? (
                <p className="chart-legend">
                  {unpricedCount} sandbox{unpricedCount === 1 ? "" : "es"} unpriced —
                  model not in the price table
                </p>
              ) : null}
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
                judge{" "}
                {evalPrice
                  ? `priced at ${evalKey ?? resolvedPick ?? "the picked rate"}`
                  : "unpriced (evaluator model not in the price table)"}{" "}
                · participant at {resolvedPick ?? priceModel ?? "each model's own rate"}
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
