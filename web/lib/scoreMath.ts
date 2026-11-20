/**
 * PR21 — weighted, scale-normalised rubric totals for the results matrix.
 *
 * The old totals row summed raw dimension scores, ignoring each rubric
 * dimension's `weight` and `min_score`/`max_score` scale. This module
 * computes the honest aggregate: `Σ ((score − min) / (max − min)) × weight`,
 * rescaled onto the familiar 0–10 axis by dividing by total weight.
 *
 * Pure + import-free on purpose: it runs under `node --test` (type-stripped)
 * for unit tests — the same pattern as `lib/timelineAnalytics.ts`.
 */

/** Minimal rubric-dimension shape needed for weighting (RubricDimension
 * in `lib/api.ts` is a superset — it also carries title/description). */
export interface WeightedDim {
  name?: string;
  weight?: number;
  min_score?: number;
  max_score?: number;
}

/** Resolved weight + scale for one dimension. */
export interface DimSpec {
  weight: number;
  min: number;
  max: number;
}

/** MVP evaluator scale — dims missing from the rubric (or with unusable
 * fields) weight at 1 on 0–10, matching the judge's default output. */
const DEFAULT_SPEC: DimSpec = { weight: 1, min: 0, max: 10 };

/** name → DimSpec from a rubric's `dimensions` list. */
export function dimensionSpecMap(dims: WeightedDim[] | undefined): Map<string, DimSpec> {
  const m = new Map<string, DimSpec>();
  for (const d of dims ?? []) {
    const name = d.name ?? "";
    if (!name) continue;
    m.set(name, {
      weight:
        typeof d.weight === "number" && Number.isFinite(d.weight) && d.weight > 0
          ? d.weight
          : DEFAULT_SPEC.weight,
      min: typeof d.min_score === "number" ? d.min_score : DEFAULT_SPEC.min,
      max: typeof d.max_score === "number" ? d.max_score : DEFAULT_SPEC.max,
    });
  }
  return m;
}

export interface WeightedTotal {
  /** Weighted normalised total on 0–10 — Σ norm×w / Σw · 10. */
  total: number;
  /** How many of `dimKeys` actually carried a score. */
  scored: number;
}

/**
 * One sandbox's weighted total across `dimKeys` (the matrix's dimension set).
 *
 * Denominator is Σ weights over ALL keys — a dimension with no score
 * contributes 0 rather than shrinking the denominator, so a partially
 * judged matrix isn't flattered. Scores clamp into [min, max] before
 * normalising; degenerate scales (max ≤ min) contribute 0.
 */
export function weightedDimTotal(
  scoredDims: Record<string, { score: number }> | undefined,
  dimKeys: string[],
  specMap: Map<string, DimSpec>,
): WeightedTotal {
  let wSum = 0;
  for (const k of dimKeys) wSum += (specMap.get(k) ?? DEFAULT_SPEC).weight;
  let acc = 0;
  let scored = 0;
  for (const k of dimKeys) {
    const e = scoredDims?.[k];
    if (!e) continue;
    const spec = specMap.get(k) ?? DEFAULT_SPEC;
    const span = spec.max - spec.min;
    const norm = span > 0 ? (e.score - spec.min) / span : 0;
    acc += Math.min(1, Math.max(0, norm)) * spec.weight;
    scored++;
  }
  return { total: wSum > 0 ? (acc / wSum) * 10 : 0, scored };
}
