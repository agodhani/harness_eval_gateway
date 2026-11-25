/**
 * Unit tests for the PR36 /dashboard transforms (`lib/dashboard.ts`):
 * week labels + null-preserving cost rows, the donut slice math, and the
 * queue headline. Runs under Node's built-in test runner — `cd web && npm test`.
 */
import assert from "node:assert/strict";
import { describe, test } from "node:test";
import {
  donutData,
  queueHeadline,
  unpricedWeeks,
  weekLabel,
  weekRows,
} from "./dashboard.ts";
import type { DashboardQueue, DashboardWeekPoint } from "./api.ts";

const wk = (over: Partial<DashboardWeekPoint> = {}): DashboardWeekPoint => ({
  week: "2026-W02",
  week_start: "2026-01-05T00:00:00+00:00",
  runs: 0,
  llm_turns: 0,
  estimated_cost_usd: 0,
  unpriced_llm_turns: 0,
  ...over,
});

describe("weekLabel", () => {
  test("slices MM-DD from the ISO week_start", () => {
    assert.equal(weekLabel("2026-01-05T00:00:00+00:00"), "01-05");
    assert.equal(weekLabel("2026-12-28T00:00:00Z"), "12-28");
  });
  test("non-ISO input passes through untouched", () => {
    assert.equal(weekLabel("W05"), "W05");
  });
});

describe("weekRows", () => {
  test("maps every bucket; null cost preserved, never zero-filled", () => {
    const rows = weekRows([
      wk({ runs: 2, llm_turns: 3, estimated_cost_usd: 0.0225 }),
      wk({
        week: "2026-W03",
        week_start: "2026-01-12T00:00:00Z",
        runs: 1,
        llm_turns: 1,
        estimated_cost_usd: null,
        unpriced_llm_turns: 1,
      }),
    ]);
    assert.deepEqual(rows[0], {
      label: "01-05",
      week: "2026-W02",
      runs: 2,
      llm_turns: 3,
      cost: 0.0225,
      unpriced: 0,
    });
    assert.equal(rows[1].cost, null);
    assert.equal(rows[1].unpriced, 1);
  });
  test("empty/missing weeks → empty rows", () => {
    assert.deepEqual(weekRows([]), []);
    assert.deepEqual(weekRows(undefined), []);
  });
});

describe("unpricedWeeks", () => {
  test("counts weeks with unpriced traffic", () => {
    assert.equal(
      unpricedWeeks(weekRows([wk(), wk({ unpriced_llm_turns: 2 }), wk({ unpriced_llm_turns: 0 })])),
      1,
    );
    assert.equal(unpricedWeeks([]), 0);
  });
});

describe("donutData", () => {
  test("share is over terminated + pending; slices sorted count-desc", () => {
    const d = donutData(
      [
        { reason: "timeout", count: 1 },
        { reason: "agent_done", count: 3 },
        { reason: "error", count: 2 },
      ],
      4,
    );
    assert.equal(d.terminated, 6);
    assert.equal(d.pending, 4);
    assert.equal(d.total, 10);
    assert.deepEqual(
      d.slices.map((s) => s.reason),
      ["agent_done", "error", "timeout"],
    );
    assert.equal(d.slices[0].share, 0.3);
  });
  test("all-pending → empty slices, honest total", () => {
    const d = donutData([], 5);
    assert.deepEqual(d.slices, []);
    assert.equal(d.total, 5);
  });
  test("zero everything → zero shares, no NaN", () => {
    const d = donutData(null, null);
    assert.equal(d.total, 0);
    assert.deepEqual(d.slices, []);
  });
});

describe("queueHeadline", () => {
  const q = (over: Partial<DashboardQueue> = {}): DashboardQueue => ({
    active: [],
    queued: [],
    awaiting_review: 0,
    ...over,
  });
  const qr = (status: string): DashboardQueue["active"][number] => ({
    run_id: "r",
    status,
    task_prompt: "t",
    model_name: "m",
    preset_ref: null,
    queue_position: null,
    created_at: null,
    started_at: null,
  });

  test("idle when nothing in flight", () => {
    assert.equal(queueHeadline(q()), "idle");
    assert.equal(queueHeadline(null), "—");
  });
  test("composes active + queued + awaiting parts", () => {
    assert.equal(
      queueHeadline(
        q({
          active: [qr("running")],
          queued: [qr("queued"), qr("queued")],
          awaiting_review: 3,
        }),
      ),
      "1 running · 2 queued · 3 awaiting review",
    );
  });
  test("distinct active statuses join with a slash", () => {
    assert.equal(
      queueHeadline(q({ active: [qr("provisioning"), qr("running")] })),
      "2 provisioning/running",
    );
  });
});
