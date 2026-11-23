/**
 * Unit tests for sanitizeHarnessConfigs (lib/api.ts) — the PR25 submit
 * payload rule: no-sandbox pseudo-adapters (prompt cells) stage nothing,
 * so skill_packs selections left over from a previous adapter pick must
 * not ship on the config.
 *
 * Runs under Node's built-in test runner — no framework:
 *   cd web && npm test
 */
import assert from "node:assert/strict";
import { describe, test } from "node:test";
import type { HarnessConfig } from "./api.ts";
import { sanitizeHarnessConfigs } from "./api.ts";

const NO_SANDBOX = new Set(["prompt"]);
const isNoSandbox = (a: string) => NO_SANDBOX.has(a);

function cfg(overrides: Partial<HarnessConfig> = {}): HarnessConfig {
  return {
    adapter: "miniloop",
    skill_packs: ["superpowers"],
    system_prompt_extra: null,
    name: null,
    model: null,
    ...overrides,
  };
}

describe("sanitizeHarnessConfigs", () => {
  test("strips packs from no-sandbox adapters", () => {
    const out = sanitizeHarnessConfigs(
      [cfg({ adapter: "prompt", skill_packs: ["superpowers", "spec-kit"] })],
      isNoSandbox,
    );
    assert.equal(out.length, 1);
    assert.deepEqual(out[0].skill_packs, []);
    // everything else passes through untouched
    assert.equal(out[0].adapter, "prompt");
  });

  test("keeps packs on container adapters", () => {
    const out = sanitizeHarnessConfigs(
      [cfg({ adapter: "miniloop", skill_packs: ["superpowers"] })],
      isNoSandbox,
    );
    assert.deepEqual(out[0].skill_packs, ["superpowers"]);
  });

  test("mixed matrix strips only the prompt cells", () => {
    const out = sanitizeHarnessConfigs(
      [
        cfg({ adapter: "prompt", skill_packs: ["superpowers"] }),
        cfg({ adapter: "aider", skill_packs: ["spec-kit"] }),
        cfg({ adapter: "prompt", skill_packs: [] }),
      ],
      isNoSandbox,
    );
    assert.deepEqual(out.map((c) => c.skill_packs), [[], ["spec-kit"], []]);
  });

  test("does not mutate the input configs", () => {
    const original = cfg({ adapter: "prompt", skill_packs: ["superpowers"] });
    sanitizeHarnessConfigs([original], isNoSandbox);
    assert.deepEqual(original.skill_packs, ["superpowers"]);
  });

  test("system_prompt_extra survives stripping (it IS the system message)", () => {
    const out = sanitizeHarnessConfigs(
      [
        cfg({
          adapter: "prompt",
          skill_packs: ["superpowers"],
          system_prompt_extra: "be terse",
        }),
      ],
      isNoSandbox,
    );
    assert.equal(out[0].system_prompt_extra, "be terse");
  });
});
