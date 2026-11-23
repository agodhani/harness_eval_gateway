/**
 * Unit tests for the analysis-page chart-row shapers (lib/analysisRows.ts).
 *
 * Runs under Node's built-in test runner — no framework:
 *   cd web && npm test      →   node --test lib/analysisRows.test.ts
 *
 * Oracle: an absent metric stays absent in the row (undefined → recharts
 * draws no bar, matching the results table's "—"), while a measured 0
 * stays a real 0. A plausible wrong behavior these tests reject is the
 * `?? 0` coercion that plotted unmeasured lanes as "0 ms"/"0 t/s" bars.
 */
import assert from "node:assert/strict";
import { describe, test } from "node:test";
import type { SandboxAnalytics } from "./api.ts";
import { latencyChartRows, speedChartRows } from "./analysisRows.ts";

function sb(overrides: Partial<SandboxAnalytics>): SandboxAnalytics {
  return {
    sandbox_id: "aaaaaaa1-1111-1111-1111-111111111111",
    label: "miniloop",
    harness_config: {},
    model: "qwen3:8b",
    price_model: "qwen3:8b",
    status: "done",
    terminal_reason: "agent_done",
    llm_turns: 0,
    tokens: { input: 0, output: 0, cache_read: 0, total: 0 },
    latency_ms: { avg: null, p95: null, max: null },
    tool_calls: { total: 0, by_tool: {} },
    skill_calls: { total: 0, by_skill: {} },
    shell_commands: 0,
    file_ops: 0,
    test_executions: 0,
    errors: 0,
    wall_clock_seconds: 0,
    estimated_cost_usd: 0,
    evaluator: null,
    ...overrides,
  };
}

describe("latencyChartRows", () => {
  test("measured values pass through as numbers", () => {
    const [row] = latencyChartRows([
      sb({
        ttft_ms: { avg: 210.5, p95: 400, max: 500 },
        latency_ms: { avg: 812.4, p95: 1500, max: 2100 },
      }),
    ]);
    assert.equal(row.ttft_avg, 210.5);
    assert.equal(row.ttft_p95, 400);
    assert.equal(row.avg, 812.4);
    assert.equal(row.p95, 1500);
    assert.equal(row.max, 2100);
  });

  test("unmeasured lane keeps every key absent — no phantom zero bar", () => {
    // a failed cell: latency stats all null, no ttft_ms at all
    const [row] = latencyChartRows([sb({})]);
    assert.ok(typeof row.name === "string" && row.name.length > 0);
    for (const key of ["ttft_avg", "ttft_p95", "avg", "p95", "max"]) {
      assert.equal(row[key], undefined, `${key} should be absent, not 0`);
    }
  });

  test("sibling lanes don't leak measurements: per-lane absence", () => {
    const rows = latencyChartRows([
      sb({
        sandbox_id: "aaaaaaa1-1111-1111-1111-111111111111",
        latency_ms: { avg: 100, p95: 100, max: 100 },
      }),
      sb({ sandbox_id: "bbbbbbb2-2222-2222-2222-222222222222" }),
    ]);
    assert.equal(rows[0].avg, 100);
    assert.equal(rows[1].avg, undefined);
  });
});

describe("speedChartRows", () => {
  test("measured avg tok/s passes through", () => {
    const [row] = speedChartRows([sb({ avg_tokens_per_sec: 47.3 })]);
    assert.equal(row.tps, 47.3);
  });

  test("unmeasured lane keeps tps absent — not 0 t/s", () => {
    const [row] = speedChartRows([sb({})]);
    assert.equal(row.tps, undefined);
  });

  test("a real 0.0 tok/s measurement stays a bar, distinct from unmeasured", () => {
    const [row] = speedChartRows([sb({ avg_tokens_per_sec: 0 })]);
    assert.equal(row.tps, 0);
  });
});
