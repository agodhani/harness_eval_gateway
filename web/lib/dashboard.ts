/**
 * PR36 — /dashboard transforms: ISO-week chart rows (spend + runs),
 * terminal-mix donut slices, queue headline.
 *
 * Pure + import-light on purpose — runs under `node --test`
 * (type-stripped); keep runtime imports extension-qualified (`./api.ts`),
 * same pattern as `lib/compareRuns.ts`.
 */
import type {
  DashboardQueue,
  DashboardTerminalSlice,
  DashboardWeekPoint,
} from "./api.ts";

/** `"2026-01-05T00:00:00+00:00"` → `"01-05"` — the x-tick label for week
 * buckets (string-slice, no Date TZ dependence — same rule as formatTs). */
export function weekLabel(weekStart: string): string {
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(weekStart);
  return m ? `${m[2]}-${m[3]}` : weekStart;
}

/** One bar-chart row per ISO week. `cost` is null when the week carried
 * unpriced traffic (PR21 — never zero-filled); `unpriced` counts the
 * calls lacking a rate so the legend can name the gap. */
export interface WeekRow {
  label: string; // "MM-DD" tick
  week: string; // "2026-W05" tooltip label
  runs: number;
  llm_turns: number;
  cost: number | null;
  unpriced: number;
}

export function weekRows(weeks: DashboardWeekPoint[] | null | undefined): WeekRow[] {
  return (weeks ?? []).map((w) => ({
    label: weekLabel(w.week_start),
    week: w.week,
    runs: w.runs,
    llm_turns: w.llm_turns,
    cost: w.estimated_cost_usd,
    unpriced: w.unpriced_llm_turns,
  }));
}

/** Weeks whose spend is unpriced — the spend card names the gap. */
export function unpricedWeeks(rows: WeekRow[]): number {
  return rows.filter((r) => r.unpriced > 0).length;
}

/** Donut geometry for the terminal-mix card. `share` is over ALL
 * sandboxes incl. `pending` — pending lanes stay visibly unaccounted
 * (compareRuns.terminalMix semantics), so shares sum < 1 while live. */
export interface DonutSlice {
  reason: string;
  count: number;
  share: number;
}

export interface DonutData {
  slices: DonutSlice[];
  /** Sandboxes that reached a terminal state. */
  terminated: number;
  /** Sandboxes with no terminal_reason yet (still live or failed pre-term). */
  pending: number;
  total: number;
}

export function donutData(
  mix: DashboardTerminalSlice[] | null | undefined,
  pending: number | null | undefined,
): DonutData {
  const slices = (mix ?? []).slice();
  const pend = pending ?? 0;
  const terminated = slices.reduce((n, s) => n + s.count, 0);
  const total = terminated + pend;
  // Defensive re-sort — count desc, reason asc (the API's order).
  slices.sort((a, b) => b.count - a.count || a.reason.localeCompare(b.reason));
  return {
    slices: slices.map((s) => ({
      reason: s.reason,
      count: s.count,
      share: total > 0 ? s.count / total : 0,
    })),
    terminated,
    pending: pend,
    total,
  };
}

/** One-line queue state for the card header — "1 running · 2 queued ·
 * 3 awaiting review", or "idle" when nothing is in flight. */
export function queueHeadline(q: DashboardQueue | null | undefined): string {
  if (!q) return "—";
  const bits: string[] = [];
  const active = q.active.length;
  if (active) {
    const statuses = [...new Set(q.active.map((r) => r.status))].join("/");
    bits.push(`${active} ${statuses}`);
  }
  if (q.queued.length) bits.push(`${q.queued.length} queued`);
  if (q.awaiting_review) bits.push(`${q.awaiting_review} awaiting review`);
  return bits.length ? bits.join(" · ") : "idle";
}
