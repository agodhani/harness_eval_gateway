/**
 * Unit tests for the PR21 weighted rubric totals (`weightedDimTotal` +
 * `dimensionSpecMap`).
 *
 * Runs under Node's built-in test runner — no framework:
 *   cd web && npm test      →   node --test "lib/(*.)test.ts" (glob)
 *
 * Oracles are worked by hand — e.g. a 2:1 weighting between two dims is
 * checked against the literal weighted mean, and a plausible wrong
 * behaviour the tests reject is the old raw Σ that ignored `weight` and
 * `min_score`/`max_score` entirely.
 */
import assert from "node:assert/strict";
import { describe, test } from "node:test";
import { dimensionSpecMap, weightedDimTotal } from "./scoreMath.ts";

const RUBRIC = [
  { name: "functional_requirements", weight: 3, min_score: 0, max_score: 10 },
  { name: "code_quality", weight: 1, min_score: 0, max_score: 10 },
  { name: "tests", weight: 1, min_score: 0, max_score: 5 },
];

describe("dimensionSpecMap", () => {
  test("carries weight + scale per dimension, defaults for missing fields", () => {
    const m = dimensionSpecMap([
      ...RUBRIC,
      { name: "bare" }, // no weight/scale → 1 · 0–10
      { name: "bad-weight", weight: -2 },
      { name: "nan-weight", weight: Number.NaN },
    ]);
    assert.deepEqual(m.get("functional_requirements"), { weight: 3, min: 0, max: 10 });
    assert.deepEqual(m.get("tests"), { weight: 1, min: 0, max: 5 });
    assert.deepEqual(m.get("bare"), { weight: 1, min: 0, max: 10 });
    assert.deepEqual(m.get("bad-weight"), { weight: 1, min: 0, max: 10 });
    assert.deepEqual(m.get("nan-weight"), { weight: 1, min: 0, max: 10 });
  });
});

describe("weightedDimTotal", () => {
  const spec = dimensionSpecMap(RUBRIC);
  const keys = RUBRIC.map((d) => d.name);

  test("weights matter: same scores, different weighting → different total", () => {
    // 10 on the w3 dim, 4 on the two w1 dims:
    // acc = 1·3 + 0.4·1 + 0.4·1 = 3.8 over wSum 5 → 7.6
    const { total, scored } = weightedDimTotal(
      {
        functional_requirements: { score: 10 },
        code_quality: { score: 4 },
        tests: { score: 2 }, // 2/5 → 0.4
      },
      keys,
      spec,
    );
    assert.equal(scored, 3);
    assert.ok(Math.abs(total - 7.6) < 1e-9, `total ${total} ≈ 7.6`);
    // the old unweighted Σ would be 16 — and even a plain mean (5.33)
    // differs from the weight-respecting 7.6
  });

  test("respects non-10 scales via min/max normalisation", () => {
    // tests is 0–5: a 5 there is perfect, a 2.5 is half.
    const { total } = weightedDimTotal(
      { functional_requirements: { score: 0 }, code_quality: { score: 0 }, tests: { score: 5 } },
      keys,
      spec,
    );
    // acc = 0 + 0 + 1·1 = 1 over wSum 5 → 2.0
    assert.ok(Math.abs(total - 2.0) < 1e-9, `total ${total} ≈ 2.0`);
  });

  test("unscored dims count in the denominator (partial matrix not flattered)", () => {
    // only functional_requirements scored 10 → acc 1·3 = 3 over wSum 5 → 6
    const { total, scored } = weightedDimTotal(
      { functional_requirements: { score: 10 } },
      keys,
      spec,
    );
    assert.equal(scored, 1);
    assert.ok(Math.abs(total - 6.0) < 1e-9, `total ${total} ≈ 6.0`);
  });

  test("dims absent from the rubric score at the default 1 · 0–10 spec", () => {
    const m = dimensionSpecMap(undefined);
    const { total } = weightedDimTotal(
      { anything: { score: 7.5 } },
      ["anything"],
      m,
    );
    assert.ok(Math.abs(total - 7.5) < 1e-9, `total ${total} ≈ 7.5`);
  });

  test("scores clamp into their scale (judge overshoot can't exceed max)", () => {
    const { total } = weightedDimTotal(
      { functional_requirements: { score: 12 }, code_quality: { score: 10 }, tests: { score: 6 } },
      keys,
      spec,
    );
    // all three at/above max → acc = 3 + 1 + 1 = 5 → 10
    assert.ok(Math.abs(total - 10) < 1e-9, `total ${total} ≈ 10`);
  });

  test("degenerate scale (max ≤ min) contributes 0 weight×norm, not NaN", () => {
    const m = dimensionSpecMap([{ name: "flat", weight: 1, min_score: 5, max_score: 5 }]);
    const { total } = weightedDimTotal({ flat: { score: 9 } }, ["flat"], m);
    assert.equal(total, 0);
    assert.ok(Number.isFinite(total));
  });
});
