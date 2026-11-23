/**
 * Unit tests for the PR32 /compare transforms (`lib/compareRuns.ts`):
 * `?runs=` parsing, the spec-diff row set, analytics totals with the
 * unpriced-poison rule, terminal mix, pooled overall stats.
 *
 * Runs under Node's built-in test runner — `cd web && npm test`.
 * Oracles are hand-worked (e.g. the pinned-model cell lands on gpt-4o's
 * model bucket, and an unlisted-but-zero-traffic model does NOT poison
 * the run's cost total while a trafficking unlisted one must).
 */
import assert from "node:assert/strict";
import { describe, test } from "node:test";
import {
  analyticsTotals,
  configSummary,
  delta,
  overallStats,
  parseRunIds,
  participantModels,
  specDiffRows,
  terminalMix,
} from "./compareRuns.ts";
import type { RunAnalytics, RunOut, RunResults } from "./api.ts";

function mkRun(over: Partial<RunOut> = {}): RunOut {
  return {
    id: "aaaaaaaa-0000-0000-0000-00000000000a",
    status: "done",
    task_prompt: "build 2048",
    model_name: "qwen3:8b",
    trials: 1,
    config: {
      harness_configs: [{ adapter: "miniloop", skill_packs: ["superpowers"] }],
      budgets: { max_iters: 50, token_budget: 200000, wall_clock_seconds: 3600 },
    },
    same_model_eval: false,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: null,
    started_at: null,
    finished_at: null,
    sandboxes: [],
    rubric: {
      id: "r1",
      run_id: "x",
      version: 2,
      status: "approved",
      parent_id: null,
      content: { dimensions: [{ name: "functional_requirements" }] },
      created_at: "2026-01-01T00:00:00Z",
    },
    ...over,
  };
}

describe("parseRunIds", () => {
  test("splits on comma/space, trims, dedupes", () => {
    assert.deepEqual(parseRunIds("a1b2, c3d4 ,,a1b2 e5f6"), ["a1b2", "c3d4", "e5f6"]);
    assert.deepEqual(parseRunIds(""), []);
    assert.deepEqual(parseRunIds(null), []);
    assert.deepEqual(parseRunIds("  , ,"), []);
  });
});

describe("specDiffRows", () => {
  test("identical specs → no diff flags except the config-count row", () => {
    const a = mkRun();
    const b = mkRun({ id: "bbbbbbbb-0000-0000-0000-00000000000b" });
    const rows = specDiffRows(a, b);
    assert.equal(rows.filter((r) => r.differs).length, 0);
  });

  test("changed spec fields flag; per-config rows align by slot", () => {
    const a = mkRun();
    const b = mkRun({
      model_name: "gpt-4o",
      trials: 3,
      preset_ref: "p2048",
      preset_revision: 4,
      config: {
        harness_configs: [
          { adapter: "codex", skill_packs: [], name: "cx" },
          { adapter: "miniloop", skill_packs: ["spec-kit"], model: "qwen3:8b" },
        ],
        budgets: { max_iters: 80, token_budget: 100000, wall_clock_seconds: 900 },
        evaluator_model: "gpt-4o-mini",
      },
      rubric: {
        id: "r2", run_id: "y", version: 1, status: "approved", parent_id: null,
        content: { dimensions: [] }, created_at: "2026-01-01T00:00:00Z",
      },
    });
    const rows = specDiffRows(a, b);
    const byLabel = new Map(rows.map((r) => [r.label, r]));
    assert.equal(byLabel.get("primary model")?.differs, true);
    assert.equal(byLabel.get("preset")?.differs, true);
    assert.equal(byLabel.get("preset")?.b, "p2048 @rev 4");
    assert.equal(byLabel.get("trials / cell")?.b, "3");
    assert.equal(byLabel.get("evaluator model")?.b, "gpt-4o-mini");
    assert.equal(byLabel.get("evaluator model")?.a, "qwen3:8b (default)");
    assert.equal(byLabel.get("run token budget")?.differs, true);
    // A shows the DEFAULTS the server persisted; B shows its overrides.
    assert.equal(byLabel.get("sandbox token cap")?.a, "—");
    // aligned config slots: config 1 differs (miniloop+superpowers vs codex "cx"),
    // config 2 exists only on B.
    assert.equal(byLabel.get("  config 1")?.differs, true);
    assert.equal(byLabel.get("  config 1")?.b, '"cx" · codex');
    assert.equal(byLabel.get("  config 2")?.a, "—");
    assert.equal(byLabel.get("rubric")?.a, "v2 · approved · 1 dims");
    assert.equal(byLabel.get("rubric")?.b, "v1 · approved · 0 dims");
  });

  test("participantModels keeps model_name + extras + pins", () => {
    const r = mkRun({
      config: {
        models: ["qwen3:8b", "gpt-4o"],
        harness_configs: [
          { adapter: "miniloop", skill_packs: [], model: "special-7b" },
        ],
      },
    });
    assert.deepEqual(participantModels(r), ["qwen3:8b", "gpt-4o", "special-7b"]);
  });
});

describe("configSummary", () => {
  test("name + adapter · packs + prompt-extra hint", () => {
    assert.equal(
      configSummary({ adapter: "miniloop", skill_packs: ["a", "b"], name: "cfg" }),
      '"cfg" · miniloop · a, b',
    );
    assert.equal(
      configSummary({ adapter: "codex", system_prompt_extra: "be terse" }),
      "codex · 'be terse'",
    );
    assert.equal(configSummary(undefined), "?");
  });
});

function mkAnalytics(models: Partial<RunAnalytics["models"][number]>[]): RunAnalytics {
  const emptyTok = { input: 0, output: 0, cache_read: 0, total: 0 };
  const emptyLat = { avg: null, p95: null, max: null };
  return {
    run_id: "r",
    model_name: "m",
    price_model: null,
    price_table: {},
    sandboxes: [{ sandbox_id: "s1" } as never],
    models: models.map((m) => ({
      model: "m",
      price_model: null,
      sandbox_count: 1,
      llm_turns: 0,
      tokens: emptyTok,
      latency_ms: emptyLat,
      ttft_ms: emptyLat,
      avg_tokens_per_sec: null,
      tool_calls: { total: 0, by_tool: {} },
      skill_calls: { total: 0, by_skill: {} },
      shell_commands: 0,
      file_ops: 0,
      test_executions: 0,
      errors: 0,
      wall_clock_seconds: 0,
      estimated_cost_usd: null,
      evaluator: null,
      ...m,
    })),
  };
}

describe("analyticsTotals", () => {
  test("sums participant metrics + prices each model bucket", () => {
    const t = analyticsTotals(
      mkAnalytics([
        {
          model: "a",
          llm_turns: 4,
          tokens: { input: 100, output: 40, cache_read: 0, total: 140 },
          errors: 1,
          wall_clock_seconds: 120,
          estimated_cost_usd: 0.5,
          avg_tokens_per_sec: 40,
          ttft_ms: { avg: 100, p95: 100, max: 100 },
          evaluator: {
            llm_turns: 2,
            tokens: { input: 20, output: 10, total: 30 },
            errors: 0,
            estimated_cost_usd: 0.1,
          },
        },
        {
          model: "b",
          llm_turns: 2,
          tokens: { input: 50, output: 10, cache_read: 0, total: 60 },
          errors: 0,
          wall_clock_seconds: 60,
          estimated_cost_usd: 0.25,
          avg_tokens_per_sec: 20,
          ttft_ms: { avg: 200, p95: 200, max: 200 },
        },
      ]),
    );
    assert.equal(t.sandboxCount, 1);
    assert.equal(t.llmTurns, 6);
    assert.equal(t.tokensIn, 150);
    assert.equal(t.tokensOut, 50);
    assert.equal(t.tokensTotal, 200);
    assert.equal(t.errors, 1);
    assert.equal(t.busySeconds, 180);
    assert.ok(Math.abs((t.costUsd ?? 0) - 0.75) < 1e-9);
    // tps pooled by turns: (40*4 + 20*2)/6 = 33.33…
    assert.ok(Math.abs((t.avgTokensPerSec ?? 0) - 200 / 6) < 1e-9);
    // ttft pooled by turns: (100*4 + 200*2)/6 = 133.33…
    assert.ok(Math.abs((t.ttftAvgMs ?? 0) - 800 / 6) < 1e-9);
    assert.equal(t.evalTurns, 2);
    assert.equal(t.evalTokensTotal, 30);
    assert.ok(Math.abs((t.evalCostUsd ?? 0) - 0.1) < 1e-9);
  });

  test("a trafficking unpriced model poisons cost; a zero-traffic one doesn't", () => {
    const poisoned = analyticsTotals(
      mkAnalytics([
        { model: "known", llm_turns: 1, tokens: { input: 1, output: 1, cache_read: 0, total: 2 }, estimated_cost_usd: 0.01 },
        { model: "mystery", llm_turns: 1, tokens: { input: 1, output: 1, cache_read: 0, total: 2 }, estimated_cost_usd: null },
      ]),
    );
    assert.equal(poisoned.costUsd, null);
    const clean = analyticsTotals(
      mkAnalytics([
        { model: "known", llm_turns: 1, tokens: { input: 1, output: 1, cache_read: 0, total: 2 }, estimated_cost_usd: 0.01 },
        { model: "mystery", llm_turns: 0, estimated_cost_usd: null },
      ]),
    );
    assert.ok(Math.abs((clean.costUsd ?? 0) - 0.01) < 1e-9);
  });
});

describe("terminalMix", () => {
  test("counts reasons, shares over ALL lanes, pending stays visible", () => {
    const m = terminalMix([
      { terminal_reason: "agent_done" },
      { terminal_reason: "agent_done" },
      { terminal_reason: "timeout" },
      { terminal_reason: null },
    ]);
    assert.equal(m.total, 4);
    assert.equal(m.pending, 1);
    assert.deepEqual(
      m.slices.map((s) => [s.reason, s.count, s.share]),
      [
        ["agent_done", 2, 0.5],
        ["timeout", 1, 0.25],
      ],
    );
  });
});

describe("overallStats + delta", () => {
  test("pools sandbox overalls with sample sd; delta is honest on nulls", () => {
    const results = {
      sandboxes: [
        { overall: { score: 8, rationale: "" } },
        { overall: { score: 6, rationale: "" } },
        { overall: null },
      ],
    } as unknown as RunResults;
    const s = overallStats(results);
    assert.equal(s.n, 2);
    assert.ok(Math.abs((s.mean ?? 0) - 7) < 1e-9);
    assert.ok(Math.abs((s.sd ?? 0) - Math.SQRT2) < 1e-9);
    assert.equal(delta(1, 3), 2);
    assert.equal(delta(null, 3), null);
  });
});
