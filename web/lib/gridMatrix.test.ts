/**
 * Unit tests for the PR31 config × model score-grid transform
 * (`gridMatrix.ts`) — cell assembly, mean±sd rollups, the fixed 0–10 heat
 * scale, winner/runner-up selection, and the cost/time/pass-rate extras.
 *
 * Runs under Node's built-in test runner — no framework:
 *   cd web && npm test      →   node --test "lib/…/*.test.ts" (glob)
 *
 * Oracles are worked by hand (see inline arithmetic). Plausible wrong
 * behaviours these reject: grouping by sandbox `name` (unique per
 * sandbox → singleton rows), letting the injected `model` key split a
 * config into N rows, ranking the winner by the judge's self-reported
 * `overall` instead of the weighted total, and a per-grid min/max
 * renormalised heat scale.
 */
import assert from "node:assert/strict";
import { describe, test } from "node:test";
import type { SandboxResult } from "./api.ts";
import { dimensionSpecMap } from "./scoreMath.ts";
import {
  buildScoreGrid,
  cellDisplayValue,
  cellExtras,
  configGridKey,
  configRowLabel,
  heatColor,
  pickWinnerCell,
  sandboxPassRate,
} from "./gridMatrix.ts";

/** Weighted rubric: functional w3 · 0–10, quality w1 · 0–10, tests w1 · 0–5. */
const RUBRIC = [
  { name: "functional_requirements", weight: 3, min_score: 0, max_score: 10 },
  { name: "code_quality", weight: 1, min_score: 0, max_score: 10 },
  { name: "tests", weight: 1, min_score: 0, max_score: 5 },
];
const SPEC = dimensionSpecMap(RUBRIC);
const DIM_KEYS = RUBRIC.map((d) => d.name);

let seq = 0;
function sb(over: Partial<SandboxResult> & { score?: number }): SandboxResult {
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
            // Every dim at the same raw score — keeps the hand oracle
            // readable: norm = score/10, score/10, score/5.
            functional_requirements: { score, rationale: "" },
            code_quality: { score, rationale: "" },
            tests: { score: Math.min(5, score / 2), rationale: "" },
          },
    overall:
      score == null ? null : { score, rationale: "" },
    ...rest,
  };
}

describe("configGridKey", () => {
  test("ignores the injected `model` — one config on N models stays one row", () => {
    const a = configGridKey({ adapter: "miniloop", skill_packs: ["superpowers"] });
    const b = configGridKey({
      adapter: "miniloop",
      skill_packs: ["superpowers"],
      model: "qwen2.5-coder:3b",
    });
    assert.equal(a, b);
  });

  test("ignores trial/cell markers (PR27 repeats stay in one cell)", () => {
    const a = configGridKey({ adapter: "miniloop", name: "base" });
    const b = configGridKey({ adapter: "miniloop", name: "base", trial_index: 2 });
    const c = configGridKey({ adapter: "miniloop", name: "base", cell_index: 1 });
    assert.equal(a, b);
    assert.equal(a, c);
  });

  test("distinguishes real config differences (adapter, packs, name)", () => {
    const base = { adapter: "miniloop", skill_packs: ["superpowers"] };
    assert.notEqual(
      configGridKey(base),
      configGridKey({ ...base, adapter: "claude_code" }),
    );
    assert.notEqual(
      configGridKey(base),
      configGridKey({ ...base, skill_packs: ["spec-kit"] }),
    );
    assert.notEqual(
      configGridKey(base),
      configGridKey({ ...base, name: "labelled" }),
    );
  });

  test("is key-order independent", () => {
    assert.equal(
      configGridKey({ adapter: "a", skill_packs: ["x"], name: "n" }),
      configGridKey({ name: "n", skill_packs: ["x"], adapter: "a" }),
    );
  });
});

describe("configRowLabel", () => {
  test("the config's own `name` wins", () => {
    assert.equal(
      configRowLabel({ adapter: "miniloop", name: "sp-full", model: "m1" }),
      "sp-full",
    );
  });
  test("falls back to `adapter · packs` with the model stripped", () => {
    assert.equal(
      configRowLabel({
        adapter: "miniloop",
        skill_packs: ["superpowers", "spec-kit"],
        model: "m1",
      }),
      "miniloop · superpowers, spec-kit",
    );
  });
});

describe("row-label disambiguation", () => {
  test("configs differing only by `system_prompt_extra` get distinct labels", () => {
    const sandboxes = [
      sb({
        score: 5,
        model: "m1",
        harness_config: {
          adapter: "miniloop",
          system_prompt_extra: "be terse",
        },
      }),
      sb({
        score: 5,
        model: "m1",
        harness_config: {
          adapter: "miniloop",
          system_prompt_extra: "be verbose",
        },
      }),
      // A row missing the differing key keeps the bare label — still
      // distinct from its hinted siblings.
      sb({ score: 5, model: "m1", harness_config: { adapter: "miniloop" } }),
    ];
    const grid = buildScoreGrid(sandboxes, ["m1"], DIM_KEYS, SPEC);
    assert.equal(grid.rows.length, 3);
    const labels = grid.rows.map((r) => r.label);
    assert.equal(new Set(labels).size, 3);
    assert.equal(labels[0], "miniloop · system_prompt_extra='be terse'");
    assert.equal(labels[1], "miniloop · system_prompt_extra='be verbose'");
    assert.equal(labels[2], "miniloop");
    // The detail line carries the same hint; the winner card's rowLabel
    // attribution inherits the disambiguated label.
    assert.equal(grid.rows[0].detail, labels[0]);
    assert.equal(grid.cells[0].rowLabel, labels[0]);
    assert.equal(grid.cells[1].rowLabel, labels[1]);
  });

  test("different `name`s never collide — the name wins, no hint", () => {
    const grid = buildScoreGrid(
      [
        sb({
          score: 5,
          model: "m1",
          harness_config: {
            adapter: "miniloop",
            name: "terse",
            system_prompt_extra: "be terse",
          },
        }),
        sb({
          score: 5,
          model: "m1",
          harness_config: {
            adapter: "miniloop",
            name: "verbose",
            system_prompt_extra: "be verbose",
          },
        }),
      ],
      ["m1"],
      DIM_KEYS,
      SPEC,
    );
    assert.deepEqual(
      grid.rows.map((r) => r.label),
      ["terse", "verbose"],
    );
  });

  test("already-unique rows keep plain `adapter · packs` labels", () => {
    const grid = buildScoreGrid(
      [
        sb({
          score: 5,
          model: "m1",
          harness_config: { adapter: "a", system_prompt_extra: "x" },
        }),
        sb({
          score: 5,
          model: "m1",
          harness_config: { adapter: "b", system_prompt_extra: "x" },
        }),
      ],
      ["m1"],
      DIM_KEYS,
      SPEC,
    );
    assert.deepEqual(
      grid.rows.map((r) => r.label),
      ["a", "b"],
    );
  });
});

describe("buildScoreGrid", () => {
  test("assembles an 8-config × 4-model grid (32 cells)", () => {
    const models = ["m1", "m2", "m3", "m4"];
    const sandboxes: SandboxResult[] = [];
    for (let c = 0; c < 8; c++) {
      for (const m of models) {
        sandboxes.push(
          sb({
            score: 5,
            model: m,
            harness_config: {
              adapter: "miniloop",
              name: `cfg-${c}`,
              model: m,
            },
          }),
        );
      }
    }
    const grid = buildScoreGrid(sandboxes, models, DIM_KEYS, SPEC);
    assert.equal(grid.rows.length, 8);
    assert.deepEqual(grid.models, models);
    assert.equal(grid.cells.length, 32);
    for (const row of grid.rows) {
      assert.equal(row.cells.size, 4);
      for (const cell of row.cells.values()) {
        assert.equal(cell.n, 1);
      }
    }
  });

  test("column order follows the participant list; observed extras append", () => {
    const sandboxes = [
      sb({ score: 5, model: "b-model", harness_config: { adapter: "a" } }),
      sb({ score: 5, model: "a-model", harness_config: { adapter: "a" } }),
      sb({ score: 5, model: "surprise", harness_config: { adapter: "a" } }),
    ];
    const grid = buildScoreGrid(sandboxes, ["a-model", "b-model"], DIM_KEYS, SPEC);
    assert.deepEqual(grid.models, ["a-model", "b-model", "surprise"]);
  });

  test("pinned configs leave the un-run cells empty (sparse, not fabricated)", () => {
    const pinned = sb({
      score: 8,
      model: "m2",
      harness_config: { adapter: "a", model: "m2" },
    });
    const grid = buildScoreGrid([pinned], ["m1", "m2"], DIM_KEYS, SPEC);
    assert.equal(grid.rows.length, 1);
    assert.equal(grid.rows[0].cells.has("m1"), false);
    assert.equal(grid.rows[0].cells.get("m2")!.n, 1);
  });

  test("rows keep first-seen order; a `model`-injected config shares one row", () => {
    const sandboxes = [
      sb({ score: 5, model: "m1", harness_config: { adapter: "a", model: "m1" } }),
      sb({ score: 5, model: "m1", harness_config: { adapter: "b", model: "m1" } }),
      sb({ score: 5, model: "m2", harness_config: { adapter: "a", model: "m2" } }),
    ];
    const grid = buildScoreGrid(sandboxes, ["m1", "m2"], DIM_KEYS, SPEC);
    assert.equal(grid.rows.length, 2);
    assert.equal(grid.rows[0].label, "a");
    assert.equal(grid.rows[1].label, "b");
    assert.equal(grid.rows[0].cells.size, 2); // m1 + m2 cells on ONE row
  });

  test("cell rollups: mean ± population sd over the members (trials)", () => {
    const cell = {
      adapter: "miniloop",
      skill_packs: ["x"],
    };
    const sandboxes = [
      // trial 1: overall 8, weighted oracle below
      sb({
        score: 8,
        model: "m1",
        harness_config: { ...cell, model: "m1", trial_index: 0 },
      }),
      // trial 2: overall 4
      sb({
        score: 4,
        model: "m1",
        harness_config: { ...cell, model: "m1", trial_index: 1 },
      }),
    ];
    const grid = buildScoreGrid(sandboxes, ["m1"], DIM_KEYS, SPEC);
    const c = grid.cells[0];
    assert.equal(c.n, 2);
    // mean of 8,4 = 6; population sd of {8,4} around 6 = sqrt((4+4)/2) = 2
    assert.equal(c.meanOverall, 6);
    assert.ok(Math.abs(c.sdOverall! - 2) < 1e-9, `sd ${c.sdOverall} ≈ 2`);
    // weighted: member1 dims 8,8,4 → norm .8·3 + .8·1 + .8·1 = 4 → 8.0;
    //           member2 dims 4,4,2 → .4·3 + .4·1 + .4·1 = 2 → 4.0
    assert.ok(Math.abs(c.meanWeighted! - 6) < 1e-9, `${c.meanWeighted} ≈ 6`);
    assert.ok(Math.abs(c.sdWeighted! - 2) < 1e-9);
  });

  test("weighted mean ≠ raw Σ / plain mean on a weighted rubric (PR21 honest basis)", () => {
    // One member scoring 10 on the w3 dim and 0 elsewhere, another at 0.
    const heavy = sb({
      model: "m1",
      harness_config: { adapter: "a", model: "m1" },
      dimensions: {
        functional_requirements: { score: 10, rationale: "" },
        code_quality: { score: 0, rationale: "" },
        tests: { score: 0, rationale: "" },
      },
      overall: { score: 6, rationale: "" },
    });
    const grid = buildScoreGrid([heavy], ["m1"], DIM_KEYS, SPEC);
    const c = grid.cells[0];
    // weighted: acc = 1·3 + 0 + 0 over wSum 5 → 6.0 (raw Σ = 10, plain
    // mean = 10/3 ≈ 3.33 — three different answers; only 6 is honest).
    assert.ok(Math.abs(c.meanWeighted! - 6.0) < 1e-9);
    assert.notEqual(c.meanWeighted, 10);
  });

  test("partial cells don't pretend: unscored members drop out of the mean", () => {
    const sandboxes = [
      sb({ score: 8, model: "m1", harness_config: { adapter: "a" } }),
      sb({ model: "m1", harness_config: { adapter: "a" } }), // unscored trial
    ];
    const grid = buildScoreGrid(sandboxes, ["m1"], DIM_KEYS, SPEC);
    const c = grid.cells[0];
    assert.equal(c.n, 2);
    assert.equal(c.meanOverall, 8);
    assert.equal(c.meanWeighted, 8); // only the scored member counts
  });
});

describe("cellDisplayValue", () => {
  test("mean overall is primary; weighted is the fallback basis", () => {
    const grid = buildScoreGrid(
      [
        sb({
          score: 7,
          model: "m1",
          harness_config: { adapter: "a" },
          overall: null, // dims scored but no judge aggregate
        }),
      ],
      ["m1"],
      DIM_KEYS,
      SPEC,
    );
    const c = grid.cells[0];
    const d = cellDisplayValue(c);
    assert.equal(d.basis, "weighted");
    assert.ok(Math.abs(d.value! - 7) < 1e-9); // dims 7,7,3.5 → w 7
  });

  test("empty cell → null value/basis", () => {
    const grid = buildScoreGrid(
      [sb({ model: "m1", harness_config: { adapter: "a" } })],
      ["m1"],
      DIM_KEYS,
      SPEC,
    );
    assert.deepEqual(cellDisplayValue(grid.cells[0]), {
      value: null,
      basis: null,
    });
  });
});

describe("pickWinnerCell", () => {
  test("winner = highest mean weighted total (not the judge's overall)", () => {
    // Cell A: judge says 9 overall but dims are weak → weighted 4.
    // Cell B: judge says 7 overall, strong weighted dims → weighted 8.
    const a = sb({
      model: "m1",
      harness_config: { adapter: "cfgA" },
      overall: { score: 9, rationale: "" },
      dimensions: {
        functional_requirements: { score: 4, rationale: "" },
        code_quality: { score: 4, rationale: "" },
        tests: { score: 2, rationale: "" },
      },
    });
    const b = sb({
      model: "m1",
      harness_config: { adapter: "cfgB" },
      overall: { score: 7, rationale: "" },
      dimensions: {
        functional_requirements: { score: 8, rationale: "" },
        code_quality: { score: 8, rationale: "" },
        tests: { score: 4, rationale: "" },
      },
    });
    const grid = buildScoreGrid([a, b], ["m1"], DIM_KEYS, SPEC);
    const pick = pickWinnerCell(grid.cells)!;
    // weighted A: .4·3+.4·1+.4·1 = 2 → 4.0; B: .8·3+.8·1+.8·1 = 4 → 8.0
    assert.equal(pick.cell.rowLabel, "cfgB");
    assert.equal(pick.runnerUp!.rowLabel, "cfgA");
    assert.ok(Math.abs(pick.delta! - 4.0) < 1e-9, `Δ ${pick.delta} ≈ 4.0`);
  });

  test("a cell with judge overall but no dims cannot win", () => {
    const overallOnly = sb({
      model: "m1",
      harness_config: { adapter: "loud" },
      overall: { score: 9.9, rationale: "" },
      dimensions: {},
    });
    const dimmed = sb({
      score: 5,
      model: "m1",
      harness_config: { adapter: "quiet" },
    });
    const grid = buildScoreGrid([overallOnly, dimmed], ["m1"], DIM_KEYS, SPEC);
    const pick = pickWinnerCell(grid.cells)!;
    assert.equal(pick.cell.rowLabel, "quiet");
    assert.equal(pick.runnerUp, null);
    assert.equal(pick.delta, null);
  });

  test("ties break on mean overall, then n, then stable order", () => {
    // Two cells, identical weighted; B has the better mean overall.
    const mk = (adapter: string, overall: number) =>
      sb({
        model: "m1",
        harness_config: { adapter },
        overall: { score: overall, rationale: "" },
        dimensions: {
          functional_requirements: { score: 5, rationale: "" },
          code_quality: { score: 5, rationale: "" },
          tests: { score: 2.5, rationale: "" },
        },
      });
    const grid = buildScoreGrid(
      [mk("aaa", 6), mk("bbb", 8)],
      ["m1"],
      DIM_KEYS,
      SPEC,
    );
    const pick = pickWinnerCell(grid.cells)!;
    assert.equal(pick.cell.rowLabel, "bbb");
    assert.ok(Math.abs(pick.delta! - 0) < 1e-9);
  });

  test("no scored cells → no winner (null, not a crash or a fake pick)", () => {
    const grid = buildScoreGrid(
      [sb({ model: "m1", harness_config: { adapter: "a" } })],
      ["m1"],
      DIM_KEYS,
      SPEC,
    );
    assert.equal(pickWinnerCell(grid.cells), null);
    assert.equal(pickWinnerCell([]), null);
  });
});

describe("heatColor", () => {
  test("hits the theme stops exactly: 0→red, 5→amber, 10→green", () => {
    assert.equal(heatColor(0), "rgb(255, 107, 107)");
    assert.equal(heatColor(5), "rgb(232, 180, 90)");
    assert.equal(heatColor(10), "rgb(125, 208, 105)");
  });

  test("interpolates mid-segments (fixed scale — no per-grid renorm)", () => {
    // 2.5 = halfway red→amber: (255+232)/2, (107+180)/2, (107+90)/2
    assert.equal(heatColor(2.5), "rgb(244, 144, 99)");
    // 7.5 = halfway amber→green: (232+125)/2, (180+208)/2, (90+105)/2
    assert.equal(heatColor(7.5), "rgb(179, 194, 98)");
  });

  test("clamps out-of-range scores to the nearest stop", () => {
    assert.equal(heatColor(-3), "rgb(255, 107, 107)");
    assert.equal(heatColor(42), "rgb(125, 208, 105)");
  });

  test("non-finite → null (cell renders unfilled, never black-boxed)", () => {
    assert.equal(heatColor(null), null);
    assert.equal(heatColor(undefined), null);
    assert.equal(heatColor(Number.NaN), null);
    assert.equal(heatColor(Number.POSITIVE_INFINITY), null);
  });
});

describe("sandboxPassRate", () => {
  test("generated_tests.pass_rate (0–1) is the preferred signal", () => {
    const s = sb({ score: 5 });
    s.generated_tests = { status: "ok", pass_rate: 0.75 };
    s.exec = { status: "ok", tests_collected: 4, tests_passed: 1 };
    assert.equal(sandboxPassRate(s), 0.75);
  });

  test("falls back to exec tests_passed/tests_collected", () => {
    const s = sb({ score: 5 });
    s.exec = { status: "ok", tests_collected: 4, tests_passed: 3 };
    assert.equal(sandboxPassRate(s), 0.75);
  });

  test("a >1 pass_rate reads as a percent; nothing measured → null", () => {
    const s = sb({ score: 5 });
    s.generated_tests = { status: "ok", pass_rate: 75 };
    assert.equal(sandboxPassRate(s), 0.75);
    assert.equal(sandboxPassRate(sb({ score: 5 })), null);
    const zero = sb({ score: 5 });
    zero.exec = { status: "ok", tests_collected: 0, tests_passed: 0 };
    assert.equal(sandboxPassRate(zero), null);
  });
});

describe("cellExtras", () => {
  test("means cost/clock over priced members; pass-rate folds results data", () => {
    const cfg = { adapter: "a" };
    const s1 = sb({ score: 8, model: "m1", harness_config: cfg });
    const s2 = sb({ score: 6, model: "m1", harness_config: cfg });
    s1.generated_tests = { status: "ok", pass_rate: 1 };
    s2.generated_tests = { status: "ok", pass_rate: 0.5 };
    const grid = buildScoreGrid([s1, s2], ["m1"], DIM_KEYS, SPEC);
    const cell = grid.cells[0];
    const analytics = new Map([
      [
        s1.sandbox_id,
        { estimated_cost_usd: 0.04, wall_clock_seconds: 120 },
      ],
      // s2 unpriced (estimated_cost_usd: null) — PR21 convention
      [
        s2.sandbox_id,
        { estimated_cost_usd: null, wall_clock_seconds: 60 },
      ],
    ]);
    const x = cellExtras(cell, analytics as never);
    assert.equal(x.meanCostUsd, 0.04);
    assert.equal(x.pricedCount, 1); // partial coverage is surfaced
    assert.equal(x.meanWallClockSeconds, 90);
    assert.equal(x.passRateCount, 2);
    assert.ok(Math.abs(x.meanPassRate! - 0.75) < 1e-9);
  });

  test("absent analytics → null cost/clock, pass-rate still resolves", () => {
    const s = sb({ score: 5 });
    s.exec = { status: "ok", tests_collected: 2, tests_passed: 2 };
    const grid = buildScoreGrid([s], ["m1"], DIM_KEYS, SPEC);
    const x = cellExtras(grid.cells[0], undefined);
    assert.equal(x.meanCostUsd, null);
    assert.equal(x.pricedCount, 0);
    assert.equal(x.meanWallClockSeconds, null);
    assert.equal(x.meanPassRate, 1);
  });
});
