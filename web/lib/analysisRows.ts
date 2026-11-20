/**
 * F13/PR30 — chart-row shapers for the analysis page's per-sandbox
 * comparison charts (AnalysisClient).
 *
 * Pure + import-light like timelineAnalytics.ts: it runs under
 * `node --test` (type-stripped) — keep runtime imports
 * extension-qualified (`./api.ts`).
 *
 * An absent metric stays absent in the row: recharts draws no bar for an
 * undefined value, so an unmeasured lane — a failed cell with zero
 * llm_responses, a fully cache-replayed lane, a pre-speed-metrics
 * backend — shows an empty slot instead of a real 0-height bar reading
 * as "0 ms" / "0 t/s". The chart mirror of the results table's "—"
 * (AnalyticsSection).
 */
import type { SandboxAnalytics } from "./api.ts";
import { shortId } from "./format.ts";

export type AnalysisChartRow = Record<string, unknown>;

/** Latency/TTFT rows — keys match the chart series
 * (ttft_avg, ttft_p95, avg, p95, max). */
export function latencyChartRows(
  sbs: SandboxAnalytics[],
): AnalysisChartRow[] {
  return sbs.map((sb) => ({
    name: shortId(sb.sandbox_id),
    ttft_avg: sb.ttft_ms?.avg ?? undefined,
    ttft_p95: sb.ttft_ms?.p95 ?? undefined,
    avg: sb.latency_ms.avg ?? undefined,
    p95: sb.latency_ms.p95 ?? undefined,
    max: sb.latency_ms.max ?? undefined,
  }));
}

/** PR30 speed rows — mean output tok/s per cell. */
export function speedChartRows(sbs: SandboxAnalytics[]): AnalysisChartRow[] {
  return sbs.map((sb) => ({
    name: shortId(sb.sandbox_id),
    tps: sb.avg_tokens_per_sec ?? undefined,
  }));
}
