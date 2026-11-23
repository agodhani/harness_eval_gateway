/**
 * PR33 — quality × cost (or wall-clock) scatter data + deterministic
 * verdict for the analysis page.
 *
 * Points are config × model matrix cells ({@link GridCell} — the same
 * fold the PR31 grid/winner card uses, so trials mean into their cell).
 * y is the mean PR21 weighted total — never the judge's self-reported
 * `overall`. x is the cell's mean per-sandbox estimated cost (PR28
 * honest prices — unpriced cells are excluded, never plotted at $0) or,
 * on the wall-clock toggle, mean sandbox wall-clock. A cell missing the
 * active axis stays in `points` with `x:null` so the UI can name it in
 * the exclusion note — sparse data is disclosed, not silently dropped.
 *
 * The Pareto frontier is the set of non-dominated plottable points —
 * nothing else is simultaneously cheaper/faster AND better. The verdict
 * is a deterministic template (no LLM call): best-quality cell with the
 * runner-up delta, the value pick (max quality-per-x over the frontier),
 * and the cheap/fast alternative.
 *
 * Pure + import-light on purpose: runs under `node --test` (type-stripped)
 * — same pattern as `lib/gridMatrix.ts` / `lib/scoreMath.ts`.
 */
import type { SandboxAnalytics } from "./api.ts";
import { formatDuration, formatUsd } from "./format.ts";
import { cellExtras, type GridCell } from "./gridMatrix.ts";

/** The x axis — estimated per-sandbox cost or wall-clock seconds. */
export type ScatterXAxis = "cost" | "wall_clock";

/** One scored matrix cell folded into a scatter point. */
export interface ScatterPoint {
  /** Stable identity — `${rowKey}::${model}` (GridView's winnerKey form). */
  key: string;
  /** Config row label (PR31 disambiguated — attributable). */
  rowLabel: string;
  model: string;
  /** Member sandboxes folded into the cell (trials). */
  n: number;
  /** y — mean PR21 weighted total across members (0–10). */
  y: number;
  /** Population sd of member weighted totals; null under 2 contributors. */
  sdY: number | null;
  /** Members contributing to `y`. */
  nY: number;
  /** Active-axis value — null when the cell is excluded on this axis.
   * Set by {@link withAxis}; `buildScatterData` returns points already
   * resolved. */
  x: number | null;
  /** Mean per-sandbox estimated cost over PRICED members; null when none
   * of the members' models resolve a rate (PR21 unpriced honesty). */
  costUsd: number | null;
  /** Members with a resolved price — `0 < pricedCount < n` = partial. */
  pricedCount: number;
  /** Mean member wall-clock; null when analytics carried none. */
  wallClockSeconds: number | null;
  /** Mean per-call output rate (avg tok/s) across members that reported
   * one — the dot-size channel (PR30). */
  tps: number | null;
  /** Members contributing to `tps`. */
  tpsCount: number;
}

/**
 * Fold cells into axis-agnostic points. Only cells with a weighted mean
 * become points — an unscored cell has no y to place, and faking one
 * (e.g. judge `overall`) would put a different-basis number on the same
 * axis. Points preserve cell order (first-seen grid order — stable).
 */
export function buildScatterPoints(
  cells: GridCell[],
  analyticsById: Map<string, SandboxAnalytics> | undefined,
): ScatterPoint[] {
  const points: ScatterPoint[] = [];
  for (const cell of cells) {
    if (cell.meanWeighted == null) continue;
    const ex = cellExtras(cell, analyticsById);
    let tpsSum = 0;
    let tpsCount = 0;
    for (const sb of cell.sandboxes) {
      const t = analyticsById?.get(sb.sandbox_id)?.avg_tokens_per_sec;
      if (typeof t === "number" && Number.isFinite(t) && t >= 0) {
        tpsSum += t;
        tpsCount += 1;
      }
    }
    points.push({
      key: `${cell.rowKey}::${cell.model}`,
      rowLabel: cell.rowLabel,
      model: cell.model,
      n: cell.n,
      y: cell.meanWeighted,
      sdY: cell.sdWeighted,
      nY: cell.nWeighted,
      x: null,
      costUsd: ex.meanCostUsd,
      pricedCount: ex.pricedCount,
      wallClockSeconds: ex.meanWallClockSeconds,
      tps: tpsCount > 0 ? tpsSum / tpsCount : null,
      tpsCount,
    });
  }
  return points;
}

/** The point's value on `axis` — cost needs a resolved price; wall-clock
 * needs a measured duration. Missing/negative/non-finite → null (the
 * cell is excluded, disclosed in the legend — never clamped to zero). */
export function axisValue(p: ScatterPoint, axis: ScatterXAxis): number | null {
  const v = axis === "cost" ? p.costUsd : p.wallClockSeconds;
  return typeof v === "number" && Number.isFinite(v) && v >= 0 ? v : null;
}

/** `points` with `x` resolved on `axis` — new array, input untouched. */
export function withAxis(
  points: ScatterPoint[],
  axis: ScatterXAxis,
): ScatterPoint[] {
  return points.map((p) => ({ ...p, x: axisValue(p, axis) }));
}

/** Total ordering for tiebreaks — stable across polls and shuffles. */
function byKey(a: ScatterPoint, b: ScatterPoint): number {
  return a.key.localeCompare(b.key);
}

/**
 * Pareto frontier over the axis-resolved plottable points (x non-null):
 * p is dominated iff some q has q.x ≤ p.x ∧ q.y ≥ p.y with at least one
 * strict — i.e. another cell is cheaper/faster AND at least as good.
 * Equal-coordinate cells don't dominate each other, so exact duplicates
 * all appear. Returned x-ascending (ties y-descending, then key) — the
 * order the overlay polyline connects.
 */
export function paretoFrontier(points: ScatterPoint[]): ScatterPoint[] {
  const plottable = points.filter(
    (p) => p.x != null && Number.isFinite(p.x) && p.x >= 0,
  );
  const frontier = plottable.filter(
    (p) =>
      !plottable.some(
        (q) =>
          q !== p &&
          q.x! <= p.x! &&
          q.y >= p.y &&
          (q.x! < p.x! || q.y > p.y),
      ),
  );
  return frontier.sort((a, b) => a.x! - b.x! || b.y - a.y || byKey(a, b));
}

export interface ScatterVerdict {
  /** Highest-quality plottable cell — ties break on lower x, then key. */
  best: ScatterPoint;
  /** Second in the same ranking; null when one cell plots. */
  runnerUp: ScatterPoint | null;
  /** `best.y − runnerUp.y`; null without a runner-up. */
  qualityDelta: number | null;
  /** `runnerUp.x − best.x`; null without a runner-up. Signed — positive
   * means the best cell is also cheaper/faster. */
  xDelta: number | null;
  /** Max quality-per-x among frontier points with x > 0 — the
   * "best-per-dollar" (cost) / "best-per-minute" (clock) pick. Null when
   * no positive-x frontier point exists (e.g. an all-local $0 run). A
   * dominated point can never out-ratio a frontier point (q.x ≤ p.x ∧
   * q.y ≥ p.y ⇒ q.y/q.x ≥ p.y/p.x), so frontier-only is exact. */
  value: ScatterPoint | null;
  /** `value.y / value.x` — quality per x unit on the axis's own scale. */
  valueRatio: number | null;
  /** Lowest-x frontier point — the spend/time-floor alternative. Always
   * the frontier head; equals `best` when the best is already the floor. */
  cheapest: ScatterPoint | null;
}

/**
 * Deterministic picks for the summary card. Ranking is quality-first —
 * the card's headline names the best-QUALITY cell, matching the PR31
 * winner card's basis; cost/speed frame the tradeoff underneath.
 */
export function buildVerdict(
  points: ScatterPoint[],
  frontier: ScatterPoint[],
): ScatterVerdict | null {
  const plottable = points.filter((p) => p.x != null);
  if (plottable.length === 0) return null;
  const ranked = [...plottable].sort(
    (a, b) => b.y - a.y || a.x! - b.x! || byKey(a, b),
  );
  const [best, runnerUp] = ranked;
  let value: ScatterPoint | null = null;
  let valueRatio: number | null = null;
  for (const p of frontier) {
    if (p.x! <= 0) continue;
    const ratio = p.y / p.x!;
    if (
      value == null ||
      ratio > valueRatio! ||
      (ratio === valueRatio && (p.y > value.y || byKey(p, value) < 0))
    ) {
      value = p;
      valueRatio = ratio;
    }
  }
  return {
    best,
    runnerUp: runnerUp ?? null,
    qualityDelta: runnerUp ? best.y - runnerUp.y : null,
    xDelta: runnerUp ? runnerUp.x! - best.x! : null,
    value,
    valueRatio,
    cheapest: frontier.length ? frontier[0] : null,
  };
}

/** Everything the chart + summary card need for one axis setting. */
export interface ScatterData {
  axis: ScatterXAxis;
  /** Every scored cell, `x` resolved (null = excluded on this axis). */
  points: ScatterPoint[];
  /** Non-dominated plottable points, x-ascending. */
  frontier: ScatterPoint[];
  /** Scored cells with no active-axis value — disclosed, not dropped. */
  excluded: ScatterPoint[];
  /** Matrix cells with no weighted score at all (no y to place). */
  unscoredCells: number;
  /** Matrix cells total (points.length + unscoredCells). */
  totalCells: number;
  /** Plottable cells priced over only some members — the mean is honest
   * but partial (rest unpriced), so the legend flags it. */
  partialCostCells: number;
  /** Deterministic summary picks; null when nothing plots on this axis. */
  verdict: ScatterVerdict | null;
}

export function buildScatterData(
  cells: GridCell[],
  analyticsById: Map<string, SandboxAnalytics> | undefined,
  axis: ScatterXAxis,
): ScatterData {
  const points = withAxis(buildScatterPoints(cells, analyticsById), axis);
  const frontier = paretoFrontier(points);
  return {
    axis,
    points,
    frontier,
    excluded: points.filter((p) => p.x == null),
    unscoredCells: cells.length - points.length,
    totalCells: cells.length,
    partialCostCells: points.filter(
      (p) => p.pricedCount > 0 && p.pricedCount < p.n,
    ).length,
    verdict: buildVerdict(points, frontier),
  };
}

// ---------------------------------------------------------------------------
// verdict text — deterministic template, no LLM call
// ---------------------------------------------------------------------------

const r1 = (v: number) => Math.round(v * 10) / 10;
const r2 = (v: number) => Math.round(v * 100) / 100;

/** Axis value → display string (usd on cost, duration on clock). */
export function formatAxisX(p: ScatterPoint, axis: ScatterXAxis): string {
  if (p.x == null) return "—";
  return axis === "cost"
    ? formatUsd(p.x)
    : formatDuration(p.x * 1000);
}

/** Compact metric tail for a named cell — the numbers the sentence omits
 * so a reader can still verify the pick (speed when measured, sd when
 * the cell ran trials). */
function pointMetrics(p: ScatterPoint): string {
  const bits: string[] = [];
  if (p.tps != null) bits.push(`${r1(p.tps)} t/s`);
  if (p.sdY != null) bits.push(`±${r2(p.sdY)}`);
  if (p.n > 1) bits.push(`×${p.n}`);
  return bits.length ? ` (${bits.join(" · ")})` : "";
}

/**
 * The auto summary card's lines — a deterministic template over the
 * verdict (no LLM call, stable across polls):
 *
 *   "cfg × model — 8.7 weighted at $0.42 — highest weighted score (+0.4
 *    vs runner-up)"
 *   "runner-up: cfg2 × model — 8.3 weighted at $0.57"
 *   "value pick: cfg3 × model — 7.9 at $0.02 (395.0 pts/$)"
 *   "cheap pick: cfg4 × model — $0.01 for 5.2 weighted (98% cheaper for
 *    −3.5)"
 *
 * The runner-up/value/cheap lines drop when they would restate the
 * headline pick. Axis chooses the x wording ($↔min, cheaper↔faster,
 * pts/$↔pts/min).
 */
export function verdictLines(v: ScatterVerdict, axis: ScatterXAxis): string[] {
  const { best, runnerUp, qualityDelta, xDelta, value, valueRatio, cheapest } = v;
  const unit = axis === "cost" ? "$" : "min";
  const lines: string[] = [];
  const delta =
    qualityDelta != null && qualityDelta > 0
      ? ` (+${r2(qualityDelta)} vs runner-up)`
      : runnerUp != null
        ? " (tied with runner-up)"
        : "";
  lines.push(
    `${best.rowLabel} × ${best.model} — ${r1(best.y)} weighted at ` +
      `${formatAxisX(best, axis)} — highest weighted score${delta}` +
      pointMetrics(best),
  );
  if (runnerUp) {
    const rel =
      xDelta != null && Math.abs(xDelta) > 1e-12
        ? xDelta > 0
          ? ` — ${formatAxisX({ ...runnerUp, x: xDelta }, axis)} ${
              axis === "cost" ? "dearer" : "slower"
            } than the pick`
          : ` — ${formatAxisX({ ...runnerUp, x: -xDelta }, axis)} ${
              axis === "cost" ? "cheaper" : "faster"
            } than the pick`
        : "";
    lines.push(
      `runner-up: ${runnerUp.rowLabel} × ${runnerUp.model} — ` +
        `${r1(runnerUp.y)} weighted at ${formatAxisX(runnerUp, axis)}${rel}` +
        pointMetrics(runnerUp),
    );
  }
  if (value && valueRatio != null && value.key !== best.key) {
    lines.push(
      `value pick: ${value.rowLabel} × ${value.model} — ` +
        `${r1(value.y)} at ${formatAxisX(value, axis)} ` +
        `(${r1(axis === "cost" ? valueRatio : valueRatio * 60)} pts/${unit})`,
    );
  }
  if (
    cheapest &&
    cheapest.key !== best.key &&
    cheapest.x! < best.x! &&
    best.x! > 0
  ) {
    const pct = Math.round((1 - cheapest.x! / best.x!) * 100);
    lines.push(
      `${axis === "cost" ? "cheap" : "fast"} pick: ` +
        `${cheapest.rowLabel} × ${cheapest.model} — ` +
        `${formatAxisX(cheapest, axis)} for ${r1(cheapest.y)} weighted ` +
        `(${pct}% ${axis === "cost" ? "cheaper" : "faster"} for ` +
        `−${r2(best.y - cheapest.y)})`,
    );
  }
  return lines;
}
