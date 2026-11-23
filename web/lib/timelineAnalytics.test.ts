/**
 * Unit tests for the PR9 timeline-metrics transform (`sandboxStrips`).
 *
 * Runs under Node's built-in test runner — no framework:
 *   cd web && npm test      →   node --test lib/timelineAnalytics.test.ts
 * Requires Node ≥22.6 (type-stripped TS); CI/local Node 22.18+/25 qualify.
 *
 * Oracles are computed by hand from the fixture constants — e.g. the cost
 * expectation is `tokens × rate / 1e6` evaluated against the fixture's own
 * numbers, and a plausible wrong behavior the tests reject is cost shown in
 * raw dollars (not Mtok-scaled) or per-event instead of per-sandbox totals.
 */
import assert from "node:assert/strict";
import { describe, test } from "node:test";
import type { RunAnalytics } from "./api.ts";
import { sandboxStrips, type StatChip } from "./timelineAnalytics.ts";

// gpt-4o-style fixture rate: $2.50 in / $10.00 out per Mtok (values mirror
// api/pricing.py table shape; the math is verified independently below).
const PRICE_TABLE = {
  "gpt-4o": { input_per_mtok_usd: 2.5, output_per_mtok_usd: 10 },
  "qwen3:8b": { input_per_mtok_usd: 0, output_per_mtok_usd: 0, local: true },
};

function fixture(): RunAnalytics {
  return {
    run_id: "run-1",
    model_name: "qwen3:8b",
    price_model: "gpt-4o",
    price_table: PRICE_TABLE,
    models: [],
    sandboxes: [
      {
        sandbox_id: "aaaaaaa1-1111-1111-1111-111111111111",
        label: "miniloop · superpowers",
        harness_config: { adapter: "miniloop", skill_packs: ["superpowers"] },
        model: "qwen3:8b",
        price_model: "qwen3:8b",
        status: "done",
        terminal_reason: "agent_done",
        llm_turns: 12,
        tokens: { input: 4204, output: 913, cache_read: 128, total: 5117 },
        latency_ms: { avg: 812.4, p95: 1500, max: 2100 },
        ttft_ms: { avg: 210.5, p95: 400, max: 500 },
        avg_tokens_per_sec: 47.3,
        tool_calls: { total: 30, by_tool: { shell: 20, read_file: 10 } },
        skill_calls: { total: 2, by_skill: { brainstorming: 2 } },
        shell_commands: 20,
        file_ops: 9,
        test_executions: 1,
        errors: 2,
        wall_clock_seconds: 183,
        estimated_cost_usd: 0,
        evaluator: {
          llm_turns: 3,
          tokens: { input: 2000, output: 400, total: 2400 },
          errors: 1,
          estimated_cost_usd: 0.00712,
        },
      },
      {
        sandbox_id: "bbbbbbb2-2222-2222-2222-222222222222",
        label: "codex",
        harness_config: { adapter: "codex", skill_packs: [] },
        model: "gpt-4o",
        price_model: "gpt-4o",
        status: "running",
        terminal_reason: null,
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
      },
    ],
  };
}

function chip(strip: { chips: StatChip[] }, label: string): StatChip {
  const c = strip.chips.find((x) => x.label === label);
  assert.ok(c, `chip ${label} missing`);
  return c;
}

describe("sandboxStrips", () => {
  test("maps every sandbox, preserving order and identity", () => {
    const strips = sandboxStrips(fixture());
    assert.equal(strips.length, 2);
    assert.equal(strips[0].sandboxId, "aaaaaaa1-1111-1111-1111-111111111111");
    assert.equal(strips[0].label, "miniloop · superpowers");
    assert.equal(strips[0].status, "done");
    assert.equal(strips[0].terminalReason, "agent_done");
    assert.equal(strips[1].terminalReason, null);
  });

  test("renders turns, tokens, latency, cost, errors as chips", () => {
    const [a] = sandboxStrips(fixture());
    assert.equal(chip(a, "llm").value, "12");
    assert.equal(chip(a, "tok").value, "4,204→913");
    assert.match(chip(a, "tok").title ?? "", /128 cached/);
    assert.equal(chip(a, "lat").value, "812ms");
    assert.match(chip(a, "lat").title ?? "", /p95 1500ms/);
    // PR30 speed chip: avg output tok/s + ttft detail on the hover.
    assert.equal(chip(a, "spd").value, "47.3 t/s");
    assert.match(chip(a, "spd").title ?? "", /ttft avg 211ms/);
    assert.match(chip(a, "spd").title ?? "", /p95 400ms/);
    // cost oracle: (4204 × 2.5 + 913 × 10) / 1e6 = 0.01964 → 2dp → $0.02
    assert.equal(chip(a, "cost").value, "$0.02");
    assert.match(chip(a, "cost").title ?? "", /gpt-4o/);
    assert.equal(chip(a, "err").value, "2");
    assert.equal(chip(a, "err").tone, "bad");
  });

  test("zero-activity sandbox renders dashes/zeros without NaN", () => {
    const [, b] = sandboxStrips(fixture());
    assert.equal(chip(b, "llm").value, "0");
    assert.equal(chip(b, "tok").value, "0→0");
    assert.equal(chip(b, "lat").value, "—");
    // PR30: no speed fields on this sandbox → an honest dash, not 0 t/s.
    assert.equal(chip(b, "spd").value, "—");
    assert.equal(chip(b, "cost").value, "$0.00");
    assert.equal(chip(b, "err").tone, "default");
    assert.equal(b.evalLine, null);
  });

  test("local price model short-circuits cost", () => {
    const f = fixture();
    f.price_model = "qwen3:8b";
    const [a] = sandboxStrips(f);
    assert.equal(chip(a, "cost").value, "$0");
    assert.match(chip(a, "cost").title ?? "", /local/);
  });

  test("evaluator traffic becomes a separate judge line, not mixed into chips", () => {
    const [a] = sandboxStrips(fixture());
    // sub-cent judge cost exercises the 4dp branch: 0.00712 → $0.0071
    assert.equal(a.evalLine, "judge 3 turns · 2,400 tok · $0.0071 · 1 err");
    // participant chips stay participant-only: 12 turns, not 12+3
    assert.equal(chip(a, "llm").value, "12");
  });

  test("auto basis (null run price_model) prices each sandbox at its own model", () => {
    const f = fixture();
    f.price_model = null;
    const [a, b] = sandboxStrips(f);
    // sandbox a's own rate is local qwen → the $0 short-circuit chip
    assert.equal(chip(a, "cost").value, "$0");
    assert.match(chip(a, "cost").title ?? "", /qwen3:8b — local/);
    // sandbox b's own rate is gpt-4o → normal estimate path on zero tokens
    assert.equal(chip(b, "cost").value, "$0.00");
    assert.match(chip(b, "cost").title ?? "", /gpt-4o/);
  });

  test("unlisted model → 'unpriced' chip, never a fallback rate (PR21)", () => {
    const f = fixture();
    f.price_model = null;
    // server resolves an unlisted model to a null price_model
    f.sandboxes[0].model = "mystery-model";
    f.sandboxes[0].price_model = null;
    const [a] = sandboxStrips(f);
    assert.equal(chip(a, "cost").value, "unpriced");
    assert.match(chip(a, "cost").title ?? "", /mystery-model.*price table/);
  });

  test("unpriced evaluator model → 'unpriced' judge cost (PR21)", () => {
    const f = fixture();
    f.sandboxes[0].evaluator = {
      llm_turns: 2,
      tokens: { input: 100, output: 20, total: 120 },
      errors: 0,
      estimated_cost_usd: null,
    };
    const [a] = sandboxStrips(f);
    assert.equal(a.evalLine, "judge 2 turns · 120 tok · unpriced · 0 err");
  });

  test("empty run → no strips", () => {
    assert.deepEqual(
      sandboxStrips({
        run_id: "r",
        model_name: "m",
        price_model: "gpt-4o",
        price_table: PRICE_TABLE,
        models: [],
        sandboxes: [],
      }),
      [],
    );
  });
});
