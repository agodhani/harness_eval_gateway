/**
 * F31 — timeline clock modes: the transform behind the timeline/console
 * `absolute | relative` toggle.
 *
 * Absolute mode (default) renders the shared wall clock — every row shows its
 * own UTC timestamp, so cells that started at different times (paced/queued
 * spawns, retries) appear staggered. Relative mode re-bases each cell to its
 * own `t+0` — the sandbox's `run_started` event when present, else its first
 * event — so the same `+2m14s` label on two cells means the same amount of
 * progress time regardless of when each cell actually spawned.
 *
 * Baselines are derived per `sandbox_id`; run-level events (`sandbox_id:
 * null`) form their own baseline under the `""` key (their earliest event ≈
 * the run's submission — `run_started` is emitted per sandbox, never at run
 * scope). Events a cell logged BEFORE its `run_started` (provisioning /
 * `stage:"seed"` checkpoints) keep a faithful negative offset (`-3.0s`) —
 * the pre-start prelude stays visible instead of being clamped to zero.
 *
 * Pure + import-light on purpose: runs under `node --test` (type-stripped)
 * — keep runtime imports extension-qualified (`./api.ts`).
 */
import type { EventOut } from "./api.ts";

export type TimelineMode = "absolute" | "relative";

/** Map key for the run-level baseline (`sandbox_id === null` events). */
export const RUN_LANE_KEY = "";

/** Map lookup that treats `null` sandbox_id as the run-level lane. */
export function laneKey(sandboxId: string | null): string {
  return sandboxId ?? RUN_LANE_KEY;
}

/**
 * Per-lane t0 (epoch ms): the lane's earliest `run_started` ts when one
 * exists, else the lane's earliest event ts. Lanes are keyed by
 * `sandbox_id` (`""` = run-level); events with unparseable timestamps are
 * ignored so one bad row can't poison a baseline.
 */
export function laneStartMs(events: EventOut[]): Map<string, number> {
  const first = new Map<string, number>();
  const started = new Map<string, number>();
  for (const ev of events) {
    const t = Date.parse(ev.ts);
    if (!Number.isFinite(t)) continue;
    const key = laneKey(ev.sandbox_id);
    const f = first.get(key);
    if (f === undefined || t < f) first.set(key, t);
    if (ev.type === "run_started") {
      const s = started.get(key);
      if (s === undefined || t < s) started.set(key, t);
    }
  }
  const out = new Map<string, number>();
  for (const [key, t] of first) out.set(key, started.get(key) ?? t);
  return out;
}

/**
 * Signed ms from `t0` to `tsIso` — negative when the event precedes its
 * lane's `run_started` baseline. `null` when either side is missing/
 * unparseable (callers fall back to the absolute label).
 */
export function relMs(tsIso: string, t0: number | undefined): number | null {
  if (t0 === undefined) return null;
  const t = Date.parse(tsIso);
  return Number.isFinite(t) ? t - t0 : null;
}

/**
 * Compact relative label — `+0.0s`, `+9.4s`, `+42s`, `+2m14s`, `+1h07m`;
 * negative offsets keep a `-` sign (pre-`run_started` provisioning events).
 * Sub-10s offsets show one decimal (llm-call scale); beyond that integer
 * resolution — the `+2m14s` shape — is the readable unit.
 */
export function formatRelativeMs(ms: number): string {
  const sign = ms < 0 ? "-" : "+";
  const a = Math.abs(ms);
  if (a < 10_000) return `${sign}${(a / 1000).toFixed(1)}s`;
  if (a < 60_000) return `${sign}${Math.floor(a / 1000)}s`;
  if (a < 3_600_000) {
    const m = Math.floor(a / 60_000);
    const s = Math.floor((a % 60_000) / 1000);
    return `${sign}${m}m${String(s).padStart(2, "0")}s`;
  }
  const h = Math.floor(a / 3_600_000);
  const m = Math.floor((a % 3_600_000) / 60_000);
  return `${sign}${h}h${String(m).padStart(2, "0")}m`;
}

/** `HH:MM:SS` UTC for an epoch-ms value — the lane footer's `start` readout. */
export function formatClockS(ms: number): string {
  const d = new Date(ms);
  const p = (n: number) => String(n).padStart(2, "0");
  return `${p(d.getUTCHours())}:${p(d.getUTCMinutes())}:${p(d.getUTCSeconds())}`;
}

/** Latest parseable `ts` among `events` (epoch ms) — a lane's elapsed edge. */
export function lastEventMs(events: EventOut[]): number | null {
  let max: number | null = null;
  for (const ev of events) {
    const t = Date.parse(ev.ts);
    if (Number.isFinite(t) && (max === null || t > max)) max = t;
  }
  return max;
}
