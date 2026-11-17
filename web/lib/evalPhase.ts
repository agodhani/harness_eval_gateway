/**
 * Judge-phase attribution for the event stream — client-side mirror of
 * `api/routers/analytics.py::_eval_boundaries` / `::_is_eval_event`.
 *
 * Eval creds carry the *participant* sandbox_id, so judge-phase events land
 * on the participant's timeline — including the eval sandbox's own miniloop
 * rows (tool_call / file_op / shell_command on scores.json), which arrive
 * with `source="harness"` and are otherwise indistinguishable from
 * candidate activity. The first `source="evaluator"` event for a sandbox
 * (the `eval_started` checkpoint, emitted before the judge container
 * spawns) marks the boundary; everything at/after it for that sandbox is
 * judge activity, not participant activity.
 *
 * Pure + import-light: runs under `node --test` (type-stripped) — keep
 * runtime imports extension-qualified (`./api.ts`).
 */
import type { EventOut } from "./api.ts";

/** First `source="evaluator"` event timestamp (ms since epoch) per sandbox —
 * the judge boundary. Run-level events (sandbox_id null) never start one. */
export function evalBoundaries(
  events: readonly Pick<EventOut, "sandbox_id" | "source" | "ts">[],
): Map<string, number> {
  const boundaries = new Map<string, number>();
  for (const ev of events) {
    if (!ev.sandbox_id || ev.source !== "evaluator" || !ev.ts) continue;
    const t = Date.parse(ev.ts);
    if (!Number.isFinite(t)) continue;
    const prev = boundaries.get(ev.sandbox_id);
    if (prev === undefined || t < prev) boundaries.set(ev.sandbox_id, t);
  }
  return boundaries;
}

/** True when the event belongs to the judge phase, not the participant:
 * evaluator-sourced outright, or at/after its sandbox's boundary. */
export function isJudgeEvent(
  ev: Pick<EventOut, "sandbox_id" | "source" | "ts">,
  boundaries: ReadonlyMap<string, number>,
): boolean {
  if (ev.source === "evaluator") return true;
  if (!ev.sandbox_id || !ev.ts) return false;
  const boundary = boundaries.get(ev.sandbox_id);
  if (boundary === undefined) return false;
  const t = Date.parse(ev.ts);
  return Number.isFinite(t) && t >= boundary;
}

/** Effective display/filter source: judge-phase rows read "evaluator"
 * regardless of the raw `source` column (eval-sandbox miniloop events are
 * emitted as "harness" on participant creds). */
export function effectiveSource(
  ev: Pick<EventOut, "sandbox_id" | "source" | "ts">,
  boundaries: ReadonlyMap<string, number>,
): string {
  return isJudgeEvent(ev, boundaries) ? "evaluator" : ev.source;
}
