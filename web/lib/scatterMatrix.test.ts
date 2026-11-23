/**
 * Unit tests for the PR33 quality × cost/speed scatter transform
 * (`scatterMatrix.ts`) — point folding, the Pareto frontier, the
 * deterministic verdict + summary lines, and sparse-data handling.
 *
 * Runs under Node's built-in test runner — no framework:
 *   cd web && npm test      →   node --test "lib/…/*.test.ts" (glob)
 *
 * Pareto oracles are worked by hand (see inline maps). Plausible wrong
 * behaviours these reject: plotting unpriced cells at $0 instead of
 * excluding them (PR21 honesty), ranking the winner by judge `overall`
 * instead of the weighted total, marking dominated points as frontier
 * (or losing equal-coordinate ones), and a runner-up measured by the
 * frontier instead of second-highest quality.
 */
import assert from "node:assert/strict";
import { describe, test } from "node:test";
import type { SandboxResult } from "./api.ts";
import { dimensionSpecMap } from "./scoreMath.ts";
import { buildScoreGrid, type GridCell } from "./gridMatrix.ts";
import {
  axisValue,
  buildScatterData,
  buildScatterPoints,
  buildVerdict,
  paretoFrontier,
  verdictLines,
  withAxis,
  type ScatterPoint,
} from "./scatterMatrix.ts";

/** Weighted rubric: functional w3 · 0–10, quality w1 · 0–10, tests w1 · 0–5.
 * Same fixture as gridMatrix.test.ts — every norm is score/10, so the
 * weighted total equals `score` and the oracle stays readable. */
const RUBRIC = [
  { name: "functional_requirements", weight: 3, min_score: 0, max_score: 10 },
  { name: "code_quality", weight: 1, min_score: 0, max_score: 10 },
  { name: "tests", weight: 1, min_score: 0, max_score: 5 },
];
const SPEC = dimensionSpecMap(RUBRIC);
const DIM_KEYS = RUBRIC.map((d) => d.name);

let seq = 0;
function sb(
  over: Partial<SandboxResult> & { score?: number | null },
): SandboxResult {
  seq += 1;
  const { score, ...rest } = over;
  return {
    sandbox_id: `00000000-0000-4000-8000-${String(seq).padStart(12, "0")}`,
    harness_config: { adapter: "miniloop", skill_packs: [] },
    status: "done",
    terminal_reason: "agent_done",
    artifact_path: null,
    rubric_id: null,
    dimensions:
      score == null
        ? {}
        : {
            functional_requirements: { score, rationale: "" },
            code_quality: { score, rationale: "" },
            tests: { score: Math.min(5, score / 2), rationale: "" },
          },
    overall: score == null ? null : { score, rationale: "" },
    ...rest,
  };
}

/** Cell fixture: `name`d config on `model`, one member per `score`. */
function mkCell(
  name: string,
  model: string,
  scores: number[],
): { cell: GridCell; sbs: SandboxResult[] } {
  const sbs = scores.map((score) =>
    sb({ score, model, harness_config: { adapter: "miniloop", name } }),
  );
  const grid = buildScoreGrid(sbs, [model], DIM_KEYS, SPEC);
  assert.equal(grid.cells.length, 1);
  return { cell: grid.cells[0], sbs };
}

/** Partial analytics rows — cast like gridMatrix.test.ts does. */
function analyticsOf(
  sbs: SandboxResult[],
  rows: Array<{
    estimated_cost_usd?: number | null;
    wall_clock_seconds?: number;
    avg_tokens_per_sec?: number | null;
  }>,
): Map<string, never> {
  const m = new Map<string, never>();
  sbs.forEach((s, i) => {
    if (rows[i] !== undefined) m.set(s.sandbox_id, rows[i] as never);
  });
  return m;
}

/**
 * Hand-checked 4-cell field — the main oracle fixture:
 *
 *   cell   y    cost    clock
 *   ─────────────────────────────
 *   a·m1   9    $0.40   300s   ← best quality
 *   b·m1   8    $0.50   200s   ← dominated on cost, frontier on clock
 *   c·m2   7    $0.10   400s   ← cost frontier, dominated on clock (by b)
 *   d·m2   5    $0.05   100s   ← cost+clock floor, best ratio on cost
 *
 * COST frontier (x-asc): d(.05,5) c(.10,7) a(.40,9) — b is dominated
 * (.40 < .50 ∧ 9 > 8). CLOCK frontier: d(100,5) b(200,8) a(300,9) —
 * c dominated (200 < 400 ∧ 8 > 7). Value ratios on cost: d 100, c 70,
 * a 22.5 → value pick d.
 */
function mainField() {
  const a = mkCell("cfg-a", "m1", [9]);
  const b = mkCell("cfg-b", "m1", [8]);
  const c = mkCell("cfg-c", "m2", [7]);
  const d = mkCell("cfg-d", "m2", [5]);
  const all = [...a.sbs, ...b.sbs, ...c.sbs, ...d.sbs];
  const analytics = analyticsOf(all, [
    { estimated_cost_usd: 0.4, wall_clock_seconds: 300, avg_tokens_per_sec: 12 },
    { estimated_cost_usd: 0.5, wall_clock_seconds: 200, avg_tokens_per_sec: 12 },
    { estimated_cost_usd: 0.1, wall_clock_seconds: 400, avg_tokens_per_sec: 30 },
    { estimated_cost_usd: 0.05, wall_clock_seconds: 100 },
  ]);
  const cells = [a.cell, b.cell, c.cell, d.cell];
  const byLabel = Object.fromEntries(cells.map((cl) => [cl.rowLabel, cl]));
  return { cells, analytics, byLabel };
}

describe("buildScatterPoints", () => {
  test("one point per scored cell; weighted y, never judge overall", () => {
    // overall = score+1 would read y=10 for a 9 — assert the weighted
    // basis was used, not the judge's aggregate.
    const { cell, sbs } = mkCell("cfg", "m1", [9]);
    sbs[0].overall = { score: 10, rationale: "" };
    const pts = buildScatterPoints(
      [cell],
      analyticsOf(sbs, [{ estimated_cost_usd: 0.4 }]),
    );
    assert.equal(pts.length, 1);
    assert.equal(pts[0].y, 9);
    assert.equal(pts[0].rowLabel, "cfg");
    assert.equal(pts[0].model, "m1");
    assert.equal(pts[0].costUsd, 0.4);
    assert.equal(pts[0].pricedCount, 1);
    assert.equal(pts[0].x, null); // unresolved until withAxis
  });

  test("cells without a weighted score produce no point", () => {
    const scored = sb({ score: 8, model: "m1" });
    const unscored = sb({ score: null, model: "m1" });
    const grid = buildScoreGrid(
      [scored, unscored],
      ["m1"],
      DIM_KEYS,
      SPEC,
    );
    // different harness_config name → two cells, only one scored.
    scored.harness_config = { adapter: "miniloop", name: "s" };
    unscored.harness_config = { adapter: "miniloop", name: "u" };
    const g2 = buildScoreGrid([scored, unscored], ["m1"], DIM_KEYS, SPEC);
    assert.equal(grid.cells.length >= 1, true);
    const pts = buildScatterPoints(g2.cells, undefined);
    assert.equal(pts.length, 1);
    assert.equal(pts[0].rowLabel, "s");
  });

  test("tps means over members that measured one; absent → null", () => {
    const { cell, sbs } = mkCell("cfg", "m1", [9, 9, 9]);
    const pts = buildScatterPoints(
      [cell],
      analyticsOf(sbs, [
        { avg_tokens_per_sec: 10 },
        { avg_tokens_per_sec: 20 },
        {}, // unmeasured member — no phantom contribution
      ]),
    );
    assert.equal(pts[0].tps, 15);
    assert.equal(pts[0].tpsCount, 2);
    const none = buildScatterPoints([cell], undefined);
    assert.equal(none[0].tps, null);
    assert.equal(none[0].tpsCount, 0);
  });
});

describe("axisValue / withAxis", () => {
  function pt(over: Partial<ScatterPoint>): ScatterPoint {
    return {
      key: "k",
      rowLabel: "r",
      model: "m",
      n: 1,
      y: 7,
      sdY: null,
      nY: 1,
      x: null,
      costUsd: null,
      pricedCount: 0,
      wallClockSeconds: null,
      tps: null,
      tpsCount: 0,
      ...over,
    };
  }

  test("cost axis needs a resolved price — unpriced cells exclude, not $0", () => {
    assert.equal(axisValue(pt({ costUsd: null }), "cost"), null);
    assert.equal(axisValue(pt({ costUsd: 0 }), "cost"), 0); // priced $0 plots
    assert.equal(axisValue(pt({ costUsd: 0.4 }), "cost"), 0.4);
  });

  test("negative/non-finite values are excluded, never clamped", () => {
    assert.equal(axisValue(pt({ costUsd: -0.5 }), "cost"), null);
    assert.equal(axisValue(pt({ costUsd: NaN }), "cost"), null);
    assert.equal(axisValue(pt({ costUsd: Infinity }), "cost"), null);
    assert.equal(
      axisValue(pt({ wallClockSeconds: -1 }), "wall_clock"),
      null,
    );
  });

  test("withAxis resolves x without mutating the input points", () => {
    const orig = [pt({ costUsd: 0.2, wallClockSeconds: 50 })];
    const resolved = withAxis(orig, "cost");
    assert.equal(resolved[0].x, 0.2);
    assert.equal(orig[0].x, null); // untouched
    assert.equal(withAxis(orig, "wall_clock")[0].x, 50);
  });
});

describe("paretoFrontier", () => {
  test("cost axis — hand-computed non-dominated set, x-ascending", () => {
    const { cells, analytics } = mainField();
    const points = withAxis(buildScatterPoints(cells, analytics), "cost");
    const frontier = paretoFrontier(points);
    assert.deepEqual(
      frontier.map((p) => p.rowLabel),
      ["cfg-d", "cfg-c", "cfg-a"],
    );
    // cfg-b (.50, 8) is dominated by cfg-a (.40, 9): cheaper AND better.
    assert.ok(!frontier.some((p) => p.rowLabel === "cfg-b"));
    assert.deepEqual(
      frontier.map((p) => p.x),
      [0.05, 0.1, 0.4],
    );
  });

  test("wall_clock axis — different frontier (cfg-c dominated here)", () => {
    const { cells, analytics } = mainField();
    const points = withAxis(
      buildScatterPoints(cells, analytics),
      "wall_clock",
    );
    const frontier = paretoFrontier(points);
    assert.deepEqual(
      frontier.map((p) => p.rowLabel),
      ["cfg-d", "cfg-b", "cfg-a"],
    );
    assert.deepEqual(
      frontier.map((p) => p.x),
      [100, 200, 300],
    );
  });

  test("equal-x cells: only the higher-y one survives; exact ties both stay", () => {
    const mk = (key: string, x: number, y: number): ScatterPoint => ({
      key,
      rowLabel: key,
      model: "m",
      n: 1,
      y,
      sdY: null,
      nY: 1,
      x,
      costUsd: x,
      pricedCount: 1,
      wallClockSeconds: null,
      tps: null,
      tpsCount: 0,
    });
    const pts = [mk("a", 0.1, 7), mk("b", 0.1, 8), mk("c", 0.1, 8)];
    const frontier = paretoFrontier(pts);
    // a is dominated by b AND c (same x, higher y); b/c tie → both stay.
    assert.deepEqual(
      frontier.map((p) => p.key),
      ["b", "c"],
    );
  });

  test("x-null points never enter the frontier", () => {
    const { cells, analytics } = mainField();
    const points = withAxis(buildScatterPoints(cells, analytics), "cost");
    points[0].x = null;
    const frontier = paretoFrontier(points);
    assert.ok(!frontier.some((p) => p.rowLabel === "cfg-a"));
  });
});

describe("buildVerdict", () => {
  test("best = highest quality; runner-up delta; value & cheap picks", () => {
    const { cells, analytics } = mainField();
    const points = withAxis(buildScatterPoints(cells, analytics), "cost");
    const v = buildVerdict(points, paretoFrontier(points))!;
    assert.equal(v.best.rowLabel, "cfg-a");
    assert.equal(v.runnerUp?.rowLabel, "cfg-b");
    assert.equal(v.qualityDelta, 1);
    assert.ok(Math.abs(v.xDelta! - 0.1) < 1e-9); // .50 − .40
    assert.equal(v.value?.rowLabel, "cfg-d"); // 5/.05 = 100 pts/$
    assert.ok(Math.abs(v.valueRatio! - 100) < 1e-9);
    assert.equal(v.cheapest?.rowLabel, "cfg-d");
  });

  test("quality ties break on lower x so the pick never flips", () => {
    const a = mkCell("pricey", "m1", [8]);
    const b = mkCell("cheap", "m1", [8]);
    const all = [...a.sbs, ...b.sbs];
    const analytics = analyticsOf(all, [
      { estimated_cost_usd: 0.9 },
      { estimated_cost_usd: 0.1 },
    ]);
    const points = withAxis(
      buildScatterPoints([a.cell, b.cell], analytics),
      "cost",
    );
    const v = buildVerdict(points, paretoFrontier(points))!;
    assert.equal(v.best.rowLabel, "cheap");
    assert.equal(v.qualityDelta, 0); // tied quality — no fake "+0" delta
  });

  test("all-$0 field: frontier is just the best cell; no value pick", () => {
    const a = mkCell("a", "m1", [9]);
    const b = mkCell("b", "m1", [7]);
    const all = [...a.sbs, ...b.sbs];
    const analytics = analyticsOf(all, [
      { estimated_cost_usd: 0 },
      { estimated_cost_usd: 0 },
    ]);
    const points = withAxis(
      buildScatterPoints([a.cell, b.cell], analytics),
      "cost",
    );
    const frontier = paretoFrontier(points);
    // Same x for both — b is dominated (equal x, lower y).
    assert.deepEqual(
      frontier.map((p) => p.rowLabel),
      ["a"],
    );
    const v = buildVerdict(points, frontier)!;
    assert.equal(v.best.rowLabel, "a");
    assert.equal(v.value, null); // no positive-x frontier point
    assert.equal(v.cheapest?.rowLabel, "a"); // the floor IS the best
  });

  test("nothing plottable → null verdict (not a bogus pick)", () => {
    const { cell, sbs } = mkCell("cfg", "m1", [9]);
    const points = withAxis(
      buildScatterPoints([cell], analyticsOf(sbs, [{ estimated_cost_usd: null }])),
      "cost",
    );
    assert.equal(buildVerdict(points, []), null);
  });
});

describe("verdictLines", () => {
  test("cost axis — exact deterministic wording", () => {
    const { cells, analytics } = mainField();
    const points = withAxis(buildScatterPoints(cells, analytics), "cost");
    const lines = verdictLines(buildVerdict(points, paretoFrontier(points))!, "cost");
    // cfg-d is both the value AND the cheap pick — two framings, both kept.
    assert.equal(
      lines[0],
      "cfg-a × m1 — 9 weighted at $0.40 — highest weighted score " +
        "(+1 vs runner-up) (12 t/s)",
    );
    assert.equal(
      lines[1],
      "runner-up: cfg-b × m1 — 8 weighted at $0.50 — $0.10 dearer " +
        "than the pick (12 t/s)",
    );
    assert.equal(
      lines[2],
      "value pick: cfg-d × m2 — 5 at $0.05 (100 pts/$)",
    );
    assert.equal(
      lines[3],
      "cheap pick: cfg-d × m2 — $0.05 for 5 weighted (88% cheaper for −4)",
    );
  });

  test("wall_clock axis — 'faster'/'pts/min' wording", () => {
    const { cells, analytics } = mainField();
    const points = withAxis(
      buildScatterPoints(cells, analytics),
      "wall_clock",
    );
    const lines = verdictLines(
      buildVerdict(points, paretoFrontier(points))!,
      "wall_clock",
    );
    // runner-up b (200s) is FASTER than best a (300s) — xDelta −100s.
    assert.equal(
      lines[1],
      "runner-up: cfg-b × m1 — 8 weighted at 3m 20s — 1m 40s faster " +
        "than the pick (12 t/s)",
    );
    // value = d: 5/100 s → 3 pts/min.
    assert.equal(
      lines[2],
      "value pick: cfg-d × m2 — 5 at 1m 40s (3 pts/min)",
    );
    assert.equal(
      lines[3],
      "fast pick: cfg-d × m2 — 1m 40s for 5 weighted (67% faster for −4)",
    );
  });

  test("tied runner-up is disclosed, not a +0 delta", () => {
    const a = mkCell("pricey", "m1", [8]);
    const b = mkCell("cheap", "m1", [8]);
    const all = [...a.sbs, ...b.sbs];
    const analytics = analyticsOf(all, [
      { estimated_cost_usd: 0.9 },
      { estimated_cost_usd: 0.1 },
    ]);
    const points = withAxis(
      buildScatterPoints([a.cell, b.cell], analytics),
      "cost",
    );
    const lines = verdictLines(
      buildVerdict(points, paretoFrontier(points))!,
      "cost",
    );
    assert.ok(lines[0].includes("(tied with runner-up)"));
    assert.equal(lines[1].startsWith("runner-up: pricey × m1"), true);
    // cheap IS the best → no "cheap pick" line; value=cheap=best →
    // no separate value line either.
    assert.equal(lines.length, 2);
  });
});

describe("buildScatterData — sparse handling", () => {
  test("unpriced cells land in `excluded` — disclosed, not dropped", () => {
    const priced = mkCell("priced", "m1", [9]);
    const unpriced = mkCell("unpriced", "m2", [8]);
    const all = [...priced.sbs, ...unpriced.sbs];
    const analytics = analyticsOf(all, [
      { estimated_cost_usd: 0.2 },
      { estimated_cost_usd: null }, // model not in the price table
    ]);
    const data = buildScatterData(
      [priced.cell, unpriced.cell],
      analytics,
      "cost",
    );
    assert.equal(data.totalCells, 2);
    assert.equal(data.points.length, 2); // both still tracked
    assert.deepEqual(
      data.excluded.map((p) => p.rowLabel),
      ["unpriced"],
    );
    assert.equal(data.verdict?.best.rowLabel, "priced");
  });

  test("unscored cells never become points; the count is disclosed", () => {
    const scored = mkCell("scored", "m1", [9]);
    const empty = sb({ score: null, model: "m1" });
    empty.harness_config = { adapter: "miniloop", name: "empty" };
    const grid = buildScoreGrid(
      [...scored.sbs, empty],
      ["m1"],
      DIM_KEYS,
      SPEC,
    );
    const data = buildScatterData(grid.cells, undefined, "wall_clock");
    assert.equal(data.totalCells, 2);
    assert.equal(data.unscoredCells, 1);
    assert.equal(data.points.length, 1);
  });

  test("partial pricing plots but is counted; absent analytics excludes clock", () => {
    const { cell, sbs } = mkCell("cfg", "m1", [9, 7]);
    const analytics = analyticsOf(sbs, [
      { estimated_cost_usd: 0.4 },
      { estimated_cost_usd: null },
    ]);
    const costData = buildScatterData([cell], analytics, "cost");
    // priced over 1/2 members — the $0.40 mean plots AND is flagged.
    assert.equal(costData.points[0].x, 0.4);
    assert.equal(costData.partialCostCells, 1);
    // wall clock missing entirely → excluded on that axis.
    const clockData = buildScatterData([cell], analytics, "wall_clock");
    assert.equal(clockData.points[0].x, null);
    assert.equal(clockData.excluded.length, 1);
    assert.equal(clockData.verdict, null);
  });

  test("absent analytics map → clock/tps null; priced cells still plot", () => {
    const { cell } = mkCell("cfg", "m1", [9]);
    const data = buildScatterData([cell], undefined, "wall_clock");
    assert.equal(data.points[0].x, null);
    assert.equal(data.excluded.length, 1);
  });
});

describe("determinism", () => {
  test("frontier + verdict are stable under input shuffle (key tiebreaks)", () => {
    const { cells, analytics } = mainField();
    const forward = buildScatterData(cells, analytics, "cost");
    const shuffled = buildScatterData(
      [...cells].reverse(),
      analytics,
      "cost",
    );
    assert.deepEqual(
      shuffled.frontier.map((p) => p.key),
      forward.frontier.map((p) => p.key),
    );
    assert.equal(shuffled.verdict?.best.key, forward.verdict?.best.key);
    assert.equal(
      shuffled.verdict?.runnerUp?.key,
      forward.verdict?.runnerUp?.key,
    );
  });
});
