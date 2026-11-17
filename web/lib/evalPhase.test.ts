/**
 * Unit tests for the judge-phase boundary helpers (lib/evalPhase.ts) — the
 * client-side mirror of api/routers/analytics.py's `_eval_boundaries` /
 * `_is_eval_event`.
 *
 * Runs under Node's built-in test runner — no framework:
 *   cd web && npm test
 */
import assert from "node:assert/strict";
import { describe, test } from "node:test";
import { effectiveSource, evalBoundaries, isJudgeEvent } from "./evalPhase.ts";

const SB_A = "aaaaaaaa-0000-0000-0000-000000000001";
const SB_B = "bbbbbbbb-0000-0000-0000-000000000002";

function ev(
  source: string,
  ts: string,
  sandbox_id: string | null = SB_A,
): { sandbox_id: string | null; source: string; ts: string } {
  return { sandbox_id, source, ts };
}

describe("evalBoundaries", () => {
  test("first evaluator-sourced ts per sandbox wins", () => {
    const bounds = evalBoundaries([
      ev("evaluator", "2026-01-01T00:10:00Z"),
      ev("evaluator", "2026-01-01T00:20:00Z"), // later — must not overwrite
      ev("evaluator", "2026-01-01T00:05:00Z", SB_B),
      ev("harness", "2026-01-01T00:01:00Z"), // non-evaluator never sets one
      ev("evaluator", "2026-01-01T00:30:00Z", null), // run-level → ignored
    ]);
    assert.equal(bounds.get(SB_A), Date.parse("2026-01-01T00:10:00Z"));
    assert.equal(bounds.get(SB_B), Date.parse("2026-01-01T00:05:00Z"));
    assert.equal(bounds.size, 2);
  });
});

describe("isJudgeEvent / effectiveSource", () => {
  const bounds = evalBoundaries([ev("evaluator", "2026-01-01T00:10:00Z")]);

  test("evaluator-sourced events are always judge events", () => {
    assert.equal(isJudgeEvent(ev("evaluator", "2026-01-01T00:00:00Z"), bounds), true);
    assert.equal(effectiveSource(ev("evaluator", "2026-01-01T00:00:00Z"), bounds), "evaluator");
  });

  test("harness events BEFORE the boundary stay candidate activity", () => {
    const e = ev("harness", "2026-01-01T00:05:00Z");
    assert.equal(isJudgeEvent(e, bounds), false);
    assert.equal(effectiveSource(e, bounds), "harness");
  });

  test("harness/gateway events AT/AFTER the boundary are judge activity", () => {
    // the eval sandbox's own miniloop rows (judge file reads, scores.json
    // writes) arrive with source="harness" on participant creds
    assert.equal(effectiveSource(ev("harness", "2026-01-01T00:10:00Z"), bounds), "evaluator");
    assert.equal(effectiveSource(ev("gateway", "2026-01-01T00:15:00Z"), bounds), "evaluator");
  });

  test("other sandboxes / run-level events are unaffected", () => {
    assert.equal(effectiveSource(ev("harness", "2026-01-01T00:15:00Z", SB_B), bounds), "harness");
    assert.equal(effectiveSource(ev("orchestrator", "2026-01-01T00:15:00Z", null), bounds), "orchestrator");
  });
});
