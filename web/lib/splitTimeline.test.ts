/**
 * Unit tests for the PR10 split-view transforms (`buildSplitRows`, lane
 * selection helpers, `laneEventCounts`).
 *
 * Runs under Node's built-in test runner — no framework:
 *   cd web && npm test      →   node --test lib/*.test.ts
 * Requires Node ≥22.6 (type-stripped TS); CI/local Node 22.18+/25 qualify.
 *
 * Oracles are computed by hand from the fixture constants — e.g. the merged
 * ordering expectation is a literal `(ts, id)` sort of the fixture ids, and
 * a plausible wrong behavior the tests reject is ordering by id only (batch
 * ingestion interleaves ids across sandboxes) or dropping non-lane events
 * instead of rendering them as shared rows.
 */
import assert from "node:assert/strict";
import { describe, test } from "node:test";
import type { EventOut } from "./api.ts";
import {
  buildSplitRows,
  defaultLanes,
  laneEventCounts,
  pickLane,
  toggleLane,
  type LaneSelection,
} from "./splitTimeline.ts";

const A = "aaaaaaa1-1111-1111-1111-111111111111";
const B = "bbbbbbb2-2222-2222-2222-222222222222";
const C = "ccccccc3-3333-3333-3333-333333333333";

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
    ts: `2026-11-${ts}`,
    seq: null,
    source: "harness",
    type,
    payload: {},
  };
}

/** Fresh fixture per call — `ev` stamps incrementing ids in creation order. */
function fixture(): { events: EventOut[]; ids: number[] } {
  nextId = 0;
  const events = [
    ev("11T10:00:02Z", A, "llm_request"), // id 1 — second by ts
    ev("11T10:00:01Z", B, "run_started"), // id 2 — first by ts (out-of-order id)
    ev("11T10:00:01.500000Z", B, "llm_response"), // id 3 — same second as 2
    ev("11T10:00:03Z", null, "checkpoint"), // id 4 — run-level → shared
    ev("11T10:00:04Z", C, "tool_call"), // id 5 — third sandbox → shared
    ev("12T00:00:01Z", A, "done_signal"), // id 6 — next day
  ];
  return { events, ids: events.map((e) => e.id) };
}

const AB: LaneSelection = { left: A, right: B };

describe("buildSplitRows", () => {
  test("orders rows by (ts, id) — not append id", () => {
    const { events, ids } = fixture();
    // ids arrive as [1..6] but ts order is 2, 3, 1, 4, 5, 6 — an id-sorted
    // implementation puts event 1 first and fails this oracle.
    const rows = buildSplitRows(events, AB).filter((r) => r.kind === "event");
    const order = rows.map((r) => (r.left ?? r.right ?? r.shared)!.id);
    assert.deepEqual(order, [ids[1], ids[2], ids[0], ids[3], ids[4], ids[5]]);
  });

  test("each lane event occupies only its lane's cell", () => {
    const { events, ids } = fixture();
    const rows = buildSplitRows(events, AB).filter((r) => r.kind === "event");
    const first = rows[0]; // B event
    assert.equal(first.right?.id, ids[1]);
    assert.equal(first.left, null);
    assert.equal(first.shared, null);
    const third = rows[2]; // A event
    assert.equal(third.left?.id, ids[0]);
    assert.equal(third.right, null);
    // same-second A/B events never share a row — one event per row
    assert.ok(rows.every((r) => !(r.left && r.right)));
  });

  test("run-level and non-lane sandboxes become shared full-width rows", () => {
    const { events, ids } = fixture();
    const rows = buildSplitRows(events, AB).filter((r) => r.kind === "event");
    assert.equal(rows[3].shared?.id, ids[3]); // sandbox_id null
    assert.equal(rows[4].shared?.id, ids[4]); // sandbox C, not picked
    assert.equal(rows[3].left, null);
    assert.equal(rows[3].right, null);
  });

  test("inserts a day marker before the first row and at each MM-DD boundary", () => {
    const { events } = fixture();
    const rows = buildSplitRows(events, AB);
    assert.equal(rows[0].kind, "day");
    assert.equal(rows[0].day, "11-11");
    const days = rows.filter((r) => r.kind === "day");
    assert.deepEqual(days.map((d) => d.day), ["11-11", "11-12"]);
    // marker sits immediately before the first event of the next day
    const idx = rows.findIndex((r) => r.day === "11-12");
    assert.equal(rows[idx + 1].left?.type, "done_signal");
  });

  test("empty lane selection leaves every event shared", () => {
    const { events } = fixture();
    const rows = buildSplitRows(events, { left: "", right: "" });
    const evs = rows.filter((r) => r.kind === "event");
    assert.equal(evs.length, events.length);
    assert.ok(evs.every((r) => r.shared && !r.left && !r.right));
  });

  test("empty input → no rows", () => {
    assert.deepEqual(buildSplitRows([], AB), []);
  });
});

describe("laneEventCounts", () => {
  test("counts lane cells and shared rows separately", () => {
    const { events } = fixture();
    assert.deepEqual(laneEventCounts(events, AB), {
      left: 2,
      right: 2,
      shared: 2,
    });
  });

  test("empty lanes count everything as shared", () => {
    const { events } = fixture();
    assert.deepEqual(laneEventCounts(events, { left: "", right: "" }), {
      left: 0,
      right: 0,
      shared: 6,
    });
  });
});

describe("defaultLanes", () => {
  test("picks the first two ids", () => {
    assert.deepEqual(defaultLanes([A, B, C]), { left: A, right: B });
  });
  test("degrades gracefully with fewer than two ids", () => {
    assert.deepEqual(defaultLanes([A]), { left: A, right: "" });
    assert.deepEqual(defaultLanes([]), { left: "", right: "" });
  });
});

describe("toggleLane (metrics-chip click in split mode)", () => {
  test("clicking a lane occupant clears that lane", () => {
    assert.deepEqual(toggleLane(AB, A), { left: "", right: B });
    assert.deepEqual(toggleLane(AB, B), { left: A, right: "" });
  });
  test("clicking a new sandbox fills the first empty lane", () => {
    assert.deepEqual(toggleLane({ left: "", right: "" }, C), {
      left: C,
      right: "",
    });
    assert.deepEqual(toggleLane({ left: A, right: "" }, C), {
      left: A,
      right: C,
    });
  });
  test("both lanes occupied → replaces the right lane", () => {
    assert.deepEqual(toggleLane(AB, C), { left: A, right: C });
  });
});

describe("pickLane (lane dropdown)", () => {
  test("setting a lane to the other lane's id swaps them", () => {
    assert.deepEqual(pickLane(AB, "left", B), { left: B, right: A });
    assert.deepEqual(pickLane(AB, "right", A), { left: B, right: A });
  });
  test("picking a free id just assigns it", () => {
    assert.deepEqual(pickLane(AB, "right", C), { left: A, right: C });
  });
  test("picking empty clears the lane", () => {
    assert.deepEqual(pickLane(AB, "left", ""), { left: "", right: B });
  });
  test("picking an id into an empty other lane does not duplicate", () => {
    assert.deepEqual(pickLane({ left: "", right: B }, "left", B), {
      left: B,
      right: "",
    });
  });
});
