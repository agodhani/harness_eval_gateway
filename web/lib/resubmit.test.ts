/**
 * Unit tests for the resubmit spec builder (lib/resubmit.ts::specFromRun) —
 * the pure half of the sessionStorage handoff.
 *
 * Runs under Node's built-in test runner — no framework:
 *   cd web && npm test
 */
import assert from "node:assert/strict";
import { describe, test } from "node:test";
import type { RunOut } from "./api.ts";
import { specFromRun } from "./resubmit.ts";

function run(overrides: Partial<RunOut> = {}): RunOut {
  return {
    id: "run-1",
    status: "done",
    task_prompt: "build 2048",
    model_name: "qwen3:8b",
    config: {},
    same_model_eval: false,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: null,
    started_at: null,
    finished_at: null,
    sandboxes: [],
    rubric: null,
    ...overrides,
  };
}

describe("specFromRun", () => {
  test("single-model run → one participant, evaluator + budgets carried", () => {
    const spec = specFromRun(
      run({
        config: {
          models: ["qwen3:8b"],
          evaluator_model: "qwen2.5-coder:7b",
          budgets: { max_iters: 10, token_budget: 5000, wall_clock_seconds: 600 },
          harness_configs: [
            { adapter: "miniloop", skill_packs: ["superpowers"], system_prompt_extra: "be terse", name: "cfg-a" },
          ],
        },
      }),
    );
    assert.deepEqual(spec.models, ["qwen3:8b"]);
    assert.equal(spec.evaluator_model, "qwen2.5-coder:7b");
    assert.equal(spec.task_prompt, "build 2048");
    assert.deepEqual(spec.budgets, {
      max_iters: 10,
      token_budget: 5000,
      wall_clock_seconds: 600,
    });
    assert.equal(spec.harness_configs.length, 1);
    assert.equal(spec.harness_configs[0].name, "cfg-a");
    assert.equal(spec.harness_configs[0].system_prompt_extra, "be terse");
  });

  test("multi-model run → participant order preserved, extras ride along", () => {
    const spec = specFromRun(
      run({ config: { models: ["qwen3:8b", "gpt-4o"] } }),
    );
    assert.deepEqual(spec.models, ["qwen3:8b", "gpt-4o"]);
  });

  test("model_name is guaranteed to lead even when config.models is absent", () => {
    const spec = specFromRun(run({ config: {} }));
    assert.deepEqual(spec.models, ["qwen3:8b"]);
    // a config.models list missing model_name gets it unshifted
    const spec2 = specFromRun(
      run({ config: { models: ["gpt-4o"] } }),
    );
    assert.deepEqual(spec2.models, ["qwen3:8b", "gpt-4o"]);
  });

  test("per-config model pins (PR14) are preserved", () => {
    const spec = specFromRun(
      run({
        config: {
          harness_configs: [
            { adapter: "miniloop", skill_packs: [], model: "gpt-4o" },
            { adapter: "aider", skill_packs: [] },
          ],
        },
      }),
    );
    assert.equal(spec.harness_configs[0].model, "gpt-4o");
    assert.equal(spec.harness_configs[1].model, null);
  });
});
