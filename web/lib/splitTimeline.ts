/**
 * PR10 — split-view trace comparison: transform the (already source/type-
 * filtered) event stream into rows for the timeline's two-lane view.
 *
 * Design: instead of two scrollports with mirrored scroll positions, the
 * split view renders ONE scrollport whose rows form a single merged
 * ordering of both lanes' events. Each row is a two-column grid — an event
 * occupies its lane's cell, the other cell stays empty. Synchronized
 * scroll and time alignment are therefore structural, not scripted: a lane
 * that goes quiet reads as a run of empty cells while the busy lane fills.
 *
 * Ordering is `(ts, id)` — wall clock first, append id as the stable
 * tiebreak — rather than the merged view's pure id order, because batch
 * ingestion can interleave ids across sandboxes while `ts` remains the
 * true per-sandbox clock. Timestamps compare via `Date.parse` (NOT string
 * order — `…:01.5Z` sorts before `…:01Z` lexicographically yet is later);
 * unparseable values fall back to string-then-id for determinism.
 *
 * Events that belong to neither lane — run-level (`sandbox_id: null`) or a
 * sandbox that isn't picked — render as `shared` full-width rows so global
 * milestones (eval_started, run checkpoints, a third sandbox's traffic)
 * stay visible in context.
 *
 * Pure + import-light on purpose: runs under `node --test` (type-stripped)
 * — keep runtime imports extension-qualified (`./api.ts`).
 */
import type { EventOut } from "./api.ts";

/** Which lane a split cell belongs to. `""` = lane not picked. */
export type LaneSide = "left" | "right";

export interface LaneSelection {
  left: string;
  right: string;
}

/**
 * One rendered row in the split stream.
 * - `kind: "day"`  — full-width day separator (`day` = "MM-DD").
 * - `kind: "event"` — `left`/`right` hold the event for its lane (at most
 *   one non-null) OR `shared` holds a non-lane event rendered full-width.
 */
export interface SplitRow {
  key: string;
  kind: "day" | "event";
  day?: string;
  left?: EventOut | null;
  right?: EventOut | null;
  shared?: EventOut | null;
}

/** Wall clock first (`Date.parse` — see module doc), id as stable tiebreak. */
function cmpEvent(a: EventOut, b: EventOut): number {
  const ta = Date.parse(a.ts);
  const tb = Date.parse(b.ts);
  if (Number.isFinite(ta) && Number.isFinite(tb)) {
    if (ta !== tb) return ta - tb;
    return a.id - b.id;
  }
  if (a.ts !== b.ts) return a.ts < b.ts ? -1 : 1;
  return a.id - b.id;
}

/**
 * Merge `events` (any order) into split rows: day separators interleaved
 * exactly like the merged view (a marker before the first event and at
 * every MM-DD boundary), then one row per event carrying it in its lane
 * cell — or `shared` when the event belongs to no picked lane.
 */
export function buildSplitRows(
  events: EventOut[],
  sel: LaneSelection,
): SplitRow[] {
  const sorted = [...events].sort(cmpEvent);
  const rows: SplitRow[] = [];
  let lastDay: string | null = null;
  for (const ev of sorted) {
    const day = ev.ts.slice(5, 10); // MM-DD
    if (day !== lastDay) {
      rows.push({ key: `day-${day}@${rows.length}`, kind: "day", day });
      lastDay = day;
    }
    const side =
      sel.left !== "" && ev.sandbox_id === sel.left
        ? "left"
        : sel.right !== "" && ev.sandbox_id === sel.right
          ? "right"
          : "shared";
    rows.push({
      key: `ev-${ev.id}`,
      kind: "event",
      left: side === "left" ? ev : null,
      right: side === "right" ? ev : null,
      shared: side === "shared" ? ev : null,
    });
  }
  return rows;
}

/** Per-lane + shared event counts for the lane headers. */
export function laneEventCounts(
  events: EventOut[],
  sel: LaneSelection,
): { left: number; right: number; shared: number } {
  let left = 0;
  let right = 0;
  let shared = 0;
  for (const e of events) {
    if (sel.left !== "" && e.sandbox_id === sel.left) left += 1;
    else if (sel.right !== "" && e.sandbox_id === sel.right) right += 1;
    else shared += 1;
  }
  return { left, right, shared };
}

/** Default lane assignment: first two sandbox ids, either may be absent. */
export function defaultLanes(sandboxIds: string[]): LaneSelection {
  return { left: sandboxIds[0] ?? "", right: sandboxIds[1] ?? "" };
}

/**
 * Metrics-chip click semantics in split mode (mirrors the merged view's
 * "click to toggle" contract):
 * - sandbox already occupies a lane → clear that lane
 * - otherwise fill the first empty lane (left preferred); when both lanes
 *   are occupied, replace the right lane (left stays the reference).
 */
export function toggleLane(sel: LaneSelection, sandboxId: string): LaneSelection {
  if (sel.left === sandboxId) return { ...sel, left: "" };
  if (sel.right === sandboxId) return { ...sel, right: "" };
  if (sel.left === "") return { ...sel, left: sandboxId };
  if (sel.right === "") return { ...sel, right: sandboxId };
  return { ...sel, right: sandboxId };
}

/**
 * Lane-picker semantics: setting a lane to the id already held by the
 * other lane swaps them (no selection is silently dropped); picking ""
 * clears the lane.
 */
export function pickLane(
  sel: LaneSelection,
  side: LaneSide,
  sandboxId: string,
): LaneSelection {
  const other: LaneSide = side === "left" ? "right" : "left";
  const next: LaneSelection = { ...sel, [side]: sandboxId };
  if (sandboxId !== "" && sel[other] === sandboxId) {
    next[other] = sel[side];
  }
  return next;
}
