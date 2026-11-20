/**
 * PR31 — config × model score grid ("heatmap") for the results page.
 *
 * The results matrix lists every sandbox as its own column; this module
 * folds the same `RunResults.sandboxes` back into the run's comparison
 * matrix — rows = submitted harness configs (the config dict minus its
 * per-cell `model` injection), cols = participant models — so a
 * multi-model run reads at a glance. Cells roll their member sandboxes up
 * to mean±sd: PR27 trials need no special handling — repeats of the same
 * (config, model) are just extra members of one cell (the optional
 * `trial_index` marker is stripped from the config identity so a backend
 * that injects it can't split a row).
 *
 * The "winner" card ranks cells by the PR21 weighted total
 * (`weightedDimTotal` — score/max × weight rescaled 0–10), not the judge's
 * self-reported `overall`, and the heat scale is fixed 0–10 so colours
 * compare across cells/runs honestly.
 *
 * Pure + import-light on purpose: runs under `node --test` (type-stripped)
 * — keep runtime imports extension-qualified (`./api.ts`), same pattern as
 * `lib/timelineAnalytics.ts` / `lib/scoreMath.ts`.
 */
import {
  harnessConfigLabel,
  type SandboxAnalytics,
  type SandboxResult,
} from "./api.ts";
import { weightedDimTotal, type DimSpec } from "./scoreMath.ts";

/** Keys that vary per matrix cell rather than per submitted config —
 * `model` is injected by matrix expansion (PR14); `trial_index`/`trial`/
 * `cell_index` are plausible repeat markers (PR27). Stripped from the row
 * identity so one config on N models / T trials stays ONE row. */
const CELL_VARIANT_KEYS = new Set([
  "model",
  "trial_index",
  "trial",
  "cell_index",
]);

function stripCellKeys(
  hc: Record<string, unknown> | undefined,
): Record<string, unknown> {
  const clean: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(hc ?? {})) {
    if (!CELL_VARIANT_KEYS.has(k)) clean[k] = v;
  }
  return clean;
}

/** Stable stringify — object keys sorted recursively; array order kept
 * (skill_packs order is meaningful). */
function stableStringify(v: unknown): string {
  if (v === null || typeof v !== "object") return JSON.stringify(v) ?? "null";
  if (Array.isArray(v)) return `[${v.map(stableStringify).join(",")}]`;
  const o = v as Record<string, unknown>;
  return `{${Object.keys(o)
    .sort()
    .map((k) => `${JSON.stringify(k)}:${stableStringify(o[k])}`)
    .join(",")}}`;
}

/**
 * Identity of one submitted harness config — the config dict minus
 * per-cell keys. Two sandboxes share a grid row iff their
 * `harness_config`s are equal ignoring `model`/trial markers. NOT the
 * sandbox `name` — the default `<adapter>-<shortid>` (and the ` · model`
 * suffix on multi-model runs) is unique per sandbox and would shard every
 * row into singletons.
 */
export function configGridKey(
  hc: Record<string, unknown> | undefined,
): string {
  return stableStringify(stripCellKeys(hc));
}

/** `adapter · packs` for the row detail line — never carries the model
 * (it is the column axis, not the row). */
export function configRowDetail(
  hc: Record<string, unknown> | undefined,
): string {
  return harnessConfigLabel(stripCellKeys(hc));
}

/** Row label — the submit form's config `name` when set, else the
 * adapter·packs detail. Matches how the results matrix header reads. */
export function configRowLabel(
  hc: Record<string, unknown> | undefined,
): string {
  const name = typeof hc?.name === "string" ? hc.name.trim() : "";
  return name || configRowDetail(hc);
}

/** Mean + population sd (÷n — the spread of THIS cell's members, not a
 * sample estimate) over the finite values, plus the contributing count;
 * empty → all null/0. sd is null under 2 contributors (a lone value has
 * no spread to report — renders "±" misleadingly if 0). */
function meanSd(values: Array<number | null | undefined>): {
  mean: number | null;
  sd: number | null;
  count: number;
} {
  const xs = values.filter(
    (v): v is number => typeof v === "number" && Number.isFinite(v),
  );
  if (xs.length === 0) return { mean: null, sd: null, count: 0 };
  const mean = xs.reduce((a, b) => a + b, 0) / xs.length;
  const sd =
    xs.length > 1
      ? Math.sqrt(xs.reduce((a, x) => a + (x - mean) ** 2, 0) / xs.length)
      : null;
  return { mean, sd, count: xs.length };
}

/** One populated (config, model) cell — the grid's atom. */
export interface GridCell {
  /** Row identity ({@link configGridKey}). */
  rowKey: string;
  /** Row label denormalized for the winner card / a11y text. */
  rowLabel: string;
  /** Column model. */
  model: string;
  /** Member sandboxes — >1 = trials (PR27) or repeated cells. */
  sandboxes: SandboxResult[];
  n: number;
  /** Mean judge `overall` across members that have one — the card's cell
   * value. Null when no member carries an aggregate. */
  meanOverall: number | null;
  sdOverall: number | null;
  /** Members contributing to `meanOverall`. */
  nOverall: number;
  /** Mean PR21 weighted total across scored members — the winner-card
   * ranking basis. Null when no member has dimension scores. */
  meanWeighted: number | null;
  sdWeighted: number | null;
  /** Members contributing to `meanWeighted`. */
  nWeighted: number;
}

/** One grid row = one submitted harness config. */
export interface GridRow {
  key: string;
  label: string;
  /** `adapter · packs` detail line (empty of model). */
  detail: string;
  /** model → cell (sparse — absent key = no sandbox ran that cell). */
  cells: Map<string, GridCell>;
}

export interface ScoreGrid {
  /** First-seen submission order (matches the matrix's column order). */
  rows: GridRow[];
  /** Column order: the run's participant list, then observed extras. */
  models: string[];
  /** Every populated cell — the winner pick's input. */
  cells: GridCell[];
}

/**
 * Fold `RunResults.sandboxes` into the config × model grid.
 *
 * `models` is the run's participant set (`RunResults.models`) — it decides
 * column order and renders EMPTY cells for configs that never ran a model
 * (e.g. a config pinned to one model); sandbox-observed models missing
 * from the list append as extra columns. `dimKeys`/`specMap` are the same
 * PR21 inputs the matrix's totals row uses — the grid never invents a
 * second weighting.
 */
export function buildScoreGrid(
  sandboxes: SandboxResult[],
  models: readonly string[] | undefined,
  dimKeys: string[],
  specMap: Map<string, DimSpec>,
): ScoreGrid {
  const cols: string[] = [];
  for (const m of models ?? []) {
    const mm = String(m ?? "").trim();
    if (mm && !cols.includes(mm)) cols.push(mm);
  }

  const rows: GridRow[] = [];
  const rowByKey = new Map<string, GridRow>();
  const cells: GridCell[] = [];

  for (const sb of sandboxes) {
    const model = String(sb.model ?? "").trim() || "?";
    if (!cols.includes(model)) cols.push(model);

    const key = configGridKey(sb.harness_config);
    let row = rowByKey.get(key);
    if (!row) {
      row = {
        key,
        label: configRowLabel(sb.harness_config),
        detail: configRowDetail(sb.harness_config),
        cells: new Map(),
      };
      rowByKey.set(key, row);
      rows.push(row);
    }
    let cell = row.cells.get(model);
    if (!cell) {
      cell = {
        rowKey: key,
        rowLabel: row.label,
        model,
        sandboxes: [],
        n: 0,
        meanOverall: null,
        sdOverall: null,
        nOverall: 0,
        meanWeighted: null,
        sdWeighted: null,
        nWeighted: 0,
      };
      row.cells.set(model, cell);
      cells.push(cell);
    }
    cell.sandboxes.push(sb);
  }

  for (const cell of cells) {
    cell.n = cell.sandboxes.length;
    const o = meanSd(cell.sandboxes.map((s) => s.overall?.score));
    cell.meanOverall = o.mean;
    cell.sdOverall = o.sd;
    cell.nOverall = o.count;
    const w = meanSd(
      cell.sandboxes.map((s) => {
        const t = weightedDimTotal(s.dimensions, dimKeys, specMap);
        return t.scored > 0 ? t.total : null;
      }),
    );
    cell.meanWeighted = w.mean;
    cell.sdWeighted = w.sd;
    cell.nWeighted = w.count;
  }
  return { rows, models: cols, cells };
}

/** Which number the cell displays + colours by — the card's "mean
 * overall", falling back to the weighted total when the judge emitted no
 * aggregate (`basis` flags the different basis to the UI). */
export function cellDisplayValue(cell: GridCell): {
  value: number | null;
  basis: "overall" | "weighted" | null;
} {
  if (cell.meanOverall != null)
    return { value: cell.meanOverall, basis: "overall" };
  if (cell.meanWeighted != null)
    return { value: cell.meanWeighted, basis: "weighted" };
  return { value: null, basis: null };
}

export interface WinnerPick {
  cell: GridCell;
  /** Next-best weighted cell — null when only one cell is ranked. */
  runnerUp: GridCell | null;
  /** `cell.meanWeighted − runnerUp.meanWeighted`; null with no runner-up. */
  delta: number | null;
}

/**
 * Winner-card pick: the cell with the highest mean WEIGHTED total (the
 * honest aggregate — a judge `overall` alone can't win). Ties break on
 * mean overall, then n (more trials = more evidence), then stable
 * label/model order so the pick never flickers between polls. Cells
 * without a single weighted score aren't ranked — and when NOTHING is
 * scored there is no winner (null).
 */
export function pickWinnerCell(cells: GridCell[]): WinnerPick | null {
  const ranked = cells
    .filter((c) => c.meanWeighted != null)
    .sort(
      (a, b) =>
        b.meanWeighted! - a.meanWeighted! ||
        (b.meanOverall ?? -Infinity) - (a.meanOverall ?? -Infinity) ||
        b.n - a.n ||
        a.rowLabel.localeCompare(b.rowLabel) ||
        a.model.localeCompare(b.model),
    );
  if (ranked.length === 0) return null;
  const [winner, runnerUp] = ranked;
  return {
    cell: winner,
    runnerUp: runnerUp ?? null,
    delta:
      runnerUp != null
        ? winner.meanWeighted! - runnerUp.meanWeighted!
        : null,
  };
}

/* Theme-token stops on the fixed 0–10 scale (globals.css):
 * 0 = --red #ff6b6b, 5 = --amber #e8b45a, 10 = --green #7dd069. */
const HEAT_STOPS: ReadonlyArray<readonly [number, readonly [number, number, number]]> = [
  [0, [255, 107, 107]],
  [5, [232, 180, 90]],
  [10, [125, 208, 105]],
];

/**
 * Score → `rgb(r, g, b)` on the fixed 0–10 red→amber→green heat scale —
 * absolute, so a colour means the same quality in every cell and run
 * (no per-grid renormalisation flattering a weak field). Out-of-range
 * clamps to the nearest stop; non-finite → null (no fill).
 */
export function heatColor(value: number | null | undefined): string | null {
  if (typeof value !== "number" || !Number.isFinite(value)) return null;
  const v = Math.min(10, Math.max(0, value));
  const [lo, hi] =
    v <= HEAT_STOPS[1][0]
      ? [HEAT_STOPS[0], HEAT_STOPS[1]]
      : [HEAT_STOPS[1], HEAT_STOPS[2]];
  const t = (v - lo[0]) / (hi[0] - lo[0]);
  const [r, g, b] = lo[1].map((c, i) =>
    Math.round(c + (hi[1][i] - c) * t),
  );
  return `rgb(${r}, ${g}, ${b})`;
}

/**
 * One sandbox's measured test pass-rate as a 0–1 fraction — the
 * evaluator-authored `generated_tests` suite's `pass_rate` preferred
 * (it's the submission-independent signal), else the submission's own
 * tests via the `exec` signal (`tests_passed`/`tests_collected`). A
 * reported rate >1 is read as a percent (defends a 0–100 drift upstream).
 * Nothing measured → null.
 */
export function sandboxPassRate(sb: SandboxResult): number | null {
  const gt = sb.generated_tests;
  if (
    typeof gt?.pass_rate === "number" &&
    Number.isFinite(gt.pass_rate) &&
    gt.pass_rate >= 0
  ) {
    return gt.pass_rate > 1 ? Math.min(1, gt.pass_rate / 100) : gt.pass_rate;
  }
  const ex = sb.exec;
  const collected =
    typeof ex?.tests_collected === "number" ? ex.tests_collected : 0;
  const passed = typeof ex?.tests_passed === "number" ? ex.tests_passed : 0;
  if (collected > 0) return Math.min(1, Math.max(0, passed / collected));
  return null;
}

/** Cost/time/pass-rate side-metrics for a cell or the winner card. */
export interface CellExtras {
  /** Mean per-sandbox estimated cost across PRICED members only; null
   * when no member has a resolved price (PR21 unpriced convention). */
  meanCostUsd: number | null;
  /** Members with a resolved price — compare against `cell.n` to spot
   * partially-priced cells (a partial mean is still shown, labelled). */
  pricedCount: number;
  /** Mean wall-clock across members present in the analytics payload. */
  meanWallClockSeconds: number | null;
  /** Mean sandbox pass-rate (0–1) across members that measured one. */
  meanPassRate: number | null;
  /** Members contributing to `meanPassRate`. */
  passRateCount: number;
}

/**
 * Fold the F9 analytics columns + per-sandbox pass-rates into one cell's
 * winner-card metrics. `analyticsById` may be absent (endpoint down) —
 * cost/time just come back null while pass-rate still resolves from the
 * results payload.
 */
export function cellExtras(
  cell: GridCell,
  analyticsById: Map<string, SandboxAnalytics> | undefined,
): CellExtras {
  const costs: number[] = [];
  const clocks: number[] = [];
  const rates: number[] = [];
  for (const sb of cell.sandboxes) {
    const a = analyticsById?.get(sb.sandbox_id);
    if (a) {
      if (
        typeof a.estimated_cost_usd === "number" &&
        Number.isFinite(a.estimated_cost_usd)
      ) {
        costs.push(a.estimated_cost_usd);
      }
      if (
        typeof a.wall_clock_seconds === "number" &&
        Number.isFinite(a.wall_clock_seconds)
      ) {
        clocks.push(a.wall_clock_seconds);
      }
    }
    const r = sandboxPassRate(sb);
    if (r != null) rates.push(r);
  }
  const mean = (xs: number[]) =>
    xs.length ? xs.reduce((a, b) => a + b, 0) / xs.length : null;
  return {
    meanCostUsd: mean(costs),
    pricedCount: costs.length,
    meanWallClockSeconds: mean(clocks),
    meanPassRate: mean(rates),
    passRateCount: rates.length,
  };
}
