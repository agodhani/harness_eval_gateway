/**
 * Unit tests for the F31 timeline clock-mode transform (`laneStartMs`,
 * `relMs`, `formatRelativeMs`, `formatClockS`, `lastEventMs`).
 *
 * Runs under Node's built-in test runner — no framework:
 *   cd web && npm test      →   node --test lib/*.test.ts
 * Requires Node ≥22.6 (type-stripped TS); CI/local Node 22.18+/25 qualify.
 *
 * Oracles are computed by hand from the fixture constants — e.g. a sandbox
 * whose `run_started` lands 5 minutes after a sibling's must yield t+0 at
 * its own run_started (not the run's first event), and a `stage:"seed"`
 * checkpoint emitted BEFORE that run_started must keep a negative offset
 * (the provisioning prelude) rather than clamp to zero or re-base on the
 * lane's earliest event.
 */
import assert from "node:assert/strict";
import { describe, test } from "node:test";
import type { EventOut } from "./api.ts";
import {
  RUN_LANE_KEY,
  formatClockS,
  formatRelativeMs,
  laneKey,
  laneStartMs,
  lastEventMs,
  relMs,
} from "./timelineModes.ts";

const A = "aaaaaaa1-1111-1111-1111-111111111111";
const B = "bbbbbbb2-2222-2222-2222-222222222222";

let nextId = 0;
function ev(
  ts: string,
  sandbox_id: string | null,
  type = "checkpoint",
): EventOut {
  nextId += 1;
  return {
    id: nextId,
    run_id: "run-1",
    sandbox_id,
    ts,
    seq: null,
    source: "harness",
    type,
    payload: {},
  };
}

/**
 * Staggered-start fixture: A spawns at 10:00:00, B (paced) at 10:05:00.
 * A emits a `stage:"seed"` checkpoint 3s BEFORE its run_started — the
 * pre-start prelude that must surface as a negative relative offset.
 * Run-level events (sandbox_id null) bracket the whole run.
 */
function fixture(): EventOut[] {
  nextId = 0;
  return [
    ev("2026-11-11T09:59:50Z", null, "checkpoint"), // run-level: rubric etc.
    ev("2026-11-11T09:59:57Z", A, "checkpoint"), // A seed — pre-run_started
    ev("2026-11-11T10:00:00Z", A, "run_started"),
    ev("2026-11-11T10:00:30Z", A, "llm_request"),
    ev("2026-11-11T10:05:00Z", B, "run_started"),
    ev("2026-11-11T10:05:30Z", B, "llm_request"),
    ev("2026-11-11T10:20:00Z", null, "checkpoint"), // run-level eval note
  ];
}

describe("laneStartMs", () => {
  test("t0 = the lane's own run_started, not the run's first event", () => {
    const starts = laneStartMs(fixture());
    assert.equal(starts.get(A), Date.parse("2026-11-11T10:00:00Z"));
    assert.equal(starts.get(B), Date.parse("2026-11-11T10:05:00Z"));
    // …so the same logical event (+30s in) labels identically on both cells
    // even though B spawned five wall-clock minutes later.
    const t30 = formatRelativeMs(
      relMs("2026-11-11T10:00:30Z", starts.get(A))!,
    );
    const t30b = formatRelativeMs(
      relMs("2026-11-11T10:05:30Z", starts.get(B))!,
    );
    assert.equal(t30, t30b);
  });

  test("run_started wins over an earlier lane event (prelude stays negative)", () => {
    const starts = laneStartMs(fixture());
    // A's seed checkpoint (10:00:00 − 3s) precedes its own run_started.
    assert.equal(relMs("2026-11-11T09:59:57Z", starts.get(A)), -3000);
  });

  test("lane without run_started falls back to its first event", () => {
    const starts = laneStartMs(fixture());
    // Run-level lane has no run_started → earliest run-level event is t0.
    assert.equal(starts.get(RUN_LANE_KEY), Date.parse("2026-11-11T09:59:50Z"));
    const noStart = laneStartMs([
      ev("2026-11-11T10:00:10Z", A, "tool_call"),
      ev("2026-11-11T10:00:05Z", A, "checkpoint"), // out-of-order: earlier ts
      ev("2026-11-11T10:00:20Z", A, "tool_call"),
    ]);
    assert.equal(noStart.get(A), Date.parse("2026-11-11T10:00:05Z"));
  });

  test("a later duplicate run_started does not move t0", () => {
    const starts = laneStartMs([
      ev("2026-11-11T10:00:00Z", A, "run_started"),
      ev("2026-11-11T10:00:01Z", A, "checkpoint"),
      ev("2026-11-11T10:00:09Z", A, "run_started"), // retry re-emit — earliest wins
    ]);
    assert.equal(starts.get(A), Date.parse("2026-11-11T10:00:00Z"));
  });

  test("unparseable ts rows are ignored — they can't poison a baseline", () => {
    const starts = laneStartMs([
      ev("not-a-date", A, "checkpoint"),
      ev("2026-11-11T10:00:00Z", A, "run_started"),
    ]);
    assert.equal(starts.get(A), Date.parse("2026-11-11T10:00:00Z"));
  });

  test("empty input → empty map; input array is not reordered", () => {
    const events = fixture();
    const before = events.map((e) => e.id);
    assert.equal(laneStartMs([]).size, 0);
    laneStartMs(events);
    assert.deepEqual(events.map((e) => e.id), before);
  });
});

describe("relMs", () => {
  test("signed offset from t0", () => {
    const t0 = Date.parse("2026-11-11T10:00:00Z");
    assert.equal(relMs("2026-11-11T10:02:14Z", t0), 134_000);
    assert.equal(relMs("2026-11-11T09:59:58.500Z", t0), -1500);
  });

  test("null when t0 missing or ts unparseable — callers fall back", () => {
    assert.equal(relMs("2026-11-11T10:00:00Z", undefined), null);
    assert.equal(relMs("garbage", 1000), null);
  });
});

describe("formatRelativeMs", () => {
  test("sub-10s keeps one decimal (llm-call scale)", () => {
    assert.equal(formatRelativeMs(0), "+0.0s");
    assert.equal(formatRelativeMs(357), "+0.4s");
    assert.equal(formatRelativeMs(9_400), "+9.4s");
  });
  test("10s..60s → integer seconds", () => {
    assert.equal(formatRelativeMs(10_000), "+10s");
    assert.equal(formatRelativeMs(42_900), "+42s");
    assert.equal(formatRelativeMs(59_999), "+59s");
  });
  test("minutes → +MmSSs (the +2m14s shape)", () => {
    assert.equal(formatRelativeMs(134_000), "+2m14s");
    assert.equal(formatRelativeMs(60_000), "+1m00s");
    assert.equal(formatRelativeMs(3_599_000), "+59m59s");
  });
  test("hours → +HhMMm", () => {
    assert.equal(formatRelativeMs(3_600_000), "+1h00m");
    assert.equal(formatRelativeMs(4_020_000), "+1h07m");
    assert.equal(formatRelativeMs(90_000_000), "+25h00m");
  });
  test("negative offsets keep a - sign (pre-run_started prelude)", () => {
    assert.equal(formatRelativeMs(-500), "-0.5s");
    assert.equal(formatRelativeMs(-125_000), "-2m05s");
    assert.equal(formatRelativeMs(-3_660_000), "-1h01m");
  });
});

describe("formatClockS", () => {
  test("UTC HH:MM:SS for the lane footer start readout", () => {
    assert.equal(
      formatClockS(Date.parse("2026-11-11T10:00:00.357Z")),
      "10:00:00",
    );
    assert.equal(formatClockS(Date.parse("2026-11-11T23:59:59Z")), "23:59:59");
  });
});

describe("lastEventMs", () => {
  test("max ts wins even when ids arrive out of ts order", () => {
    const events = [
      ev("2026-11-11T10:00:30Z", A),
      ev("2026-11-11T10:00:05Z", A), // batch-ingested: earlier ts, later id
      ev("not-a-date", A), // skipped
    ];
    assert.equal(lastEventMs(events), Date.parse("2026-11-11T10:00:30Z"));
  });
  test("empty / all-unparseable → null", () => {
    assert.equal(lastEventMs([]), null);
    assert.equal(lastEventMs([ev("nope", A)]), null);
  });
});

describe("laneKey", () => {
  test("null sandbox_id maps to the run-level lane key", () => {
    assert.equal(laneKey(null), RUN_LANE_KEY);
    assert.equal(laneKey(A), A);
  });
});
