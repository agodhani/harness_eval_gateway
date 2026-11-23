/**
 * PR27 — trial grouping + variance math for the results matrix and
 * analytics.
 *
 * A run submitted with `trials > 1` spawns N sandboxes per config × model
 * cell; each carries `harness_config.trial_index` (1-based). Results group
 * every cell's trials under one header showing mean ± sample sd (n−1)
 * instead of N independent columns.
 */

export const TRIAL_INDEX_KEY = "trial_index";

/** Deterministic stringify with recursively sorted object keys — two
 * structurally equal configs always produce the same key regardless of
 * property insertion order. */
export function stableSerialize(value: unknown): string {
  if (value === null || typeof value !== "object") {
    return JSON.stringify(value ?? null);
  }
  if (Array.isArray(value)) {
    return `[${value.map(stableSerialize).join(",")}]`;
  }
  const obj = value as Record<string, unknown>;
  const inner = Object.keys(obj)
    .sort()
    .map((k) => `${JSON.stringify(k)}:${stableSerialize(obj[k])}`)
    .join(",");
  return `{${inner}}`;
}

/**
 * Stable identity of a sandbox's config × model cell — the harness config
 * minus `trial_index`, serialized. Every trial of one cell produces the
 * identical key (mirrors orchestrator.matrix.cell_key — a compact
 * serialization, not byte-compatible, but equally deterministic for
 * grouping).
 */
export function cellKey(
  harnessConfig: Record<string, unknown> | null | undefined,
): string {
  const entries = Object.entries(harnessConfig ?? {}).filter(
    ([k]) => k !== TRIAL_INDEX_KEY,
  );
  return stableSerialize(Object.fromEntries(entries));
}

interface CellItem {
  harness_config?: Record<string, unknown> | null;
}

export interface TrialGroup<T> {
  key: string;
  items: T[];
}

/** Group items (SandboxResult / SandboxAnalytics / …) into trial groups,
 * preserving first-seen cell order and item order within each cell. */
export function groupByCell<T extends CellItem>(items: T[]): TrialGroup<T>[] {
  const order: string[] = [];
  const byKey = new Map<string, T[]>();
  for (const item of items) {
    const key = cellKey(item.harness_config);
    let bucket = byKey.get(key);
    if (bucket === undefined) {
      bucket = [];
      byKey.set(key, bucket);
      order.push(key);
    }
    bucket.push(item);
  }
  return order.map((key) => ({ key, items: byKey.get(key) ?? [] }));
}

/** The 1-based repeat index stamped on a trialed sandbox — the API's
 * `trial_index` field first, then the harness_config key, then 1
 * (untrialed rows are "trial 1 of 1" for display). */
export function trialIndex(
  item: CellItem & { trial_index?: number | null },
): number {
  const raw =
    item.trial_index ?? item.harness_config?.[TRIAL_INDEX_KEY] ?? null;
  const n = typeof raw === "number" ? raw : Number(raw);
  return Number.isFinite(n) && n > 0 ? Math.trunc(n) : 1;
}

export interface MeanSd {
  /** null when no values survived the null-drop. */
  mean: number | null;
  /** Sample standard deviation (n−1); null when fewer than 2 values. */
  sd: number | null;
  n: number;
}

/** Mean + sample sd over the non-null values (mirrors statistics.stdev). */
export function meanSd(values: Array<number | null | undefined>): MeanSd {
  const xs = values.filter(
    (v): v is number => typeof v === "number" && Number.isFinite(v),
  );
  if (xs.length === 0) return { mean: null, sd: null, n: 0 };
  const mean = xs.reduce((a, b) => a + b, 0) / xs.length;
  if (xs.length < 2) return { mean, sd: null, n: xs.length };
  const variance =
    xs.reduce((acc, x) => acc + (x - mean) * (x - mean), 0) / (xs.length - 1);
  return { mean, sd: Math.sqrt(variance), n: xs.length };
}

function fmtNum(v: number, digits: number): string {
  return v.toFixed(digits);
}

/** "7.8 ±0.6 (n=3)" / "7.8 (n=1)" / "—" for the matrix's aggregate cells. */
export function fmtMeanSd(m: MeanSd, digits = 1): string {
  if (m.mean === null || m.n === 0) return "—";
  const base = fmtNum(m.mean, digits);
  const sd = m.sd !== null ? ` ±${fmtNum(m.sd, digits)}` : "";
  return `${base}${sd} (n=${m.n})`;
}

/** Strip the " · tN" trial suffix provision appends to sandbox names —
 * the group header should read "cfg · model", not "cfg · model · t3". */
export function stripTrialSuffix(name: string | null | undefined): string {
  return (name ?? "").replace(/ · t\d+$/, "");
}
