"use client";

/**
 * PR10 — split-view trace comparison.
 *
 * Renders the {@link buildSplitRows} merged ordering inside the timeline's
 * single scroll container: a sticky two-lane header (per-lane sandbox
 * pickers, status, event count) followed by two-column rows. Each event
 * fills only its lane's cell — the other cell stays empty — so scrolling is
 * synchronized and time-aligned by construction (no scroll mirroring), and
 * a quiet lane reads as a run of gaps. Run-level and non-lane-sandbox
 * events (`row.shared`) span both columns as full-width rows.
 *
 * Rows reuse {@link EventRow} — identical `.tl-*` markup and per-type
 * colors to the merged stream. Presentational only: all state (lanes,
 * expansion, filters) lives in TimelineClient.
 */
import { shortId } from "@/lib/format";
import type { SplitRow, LaneSelection, LaneSide } from "@/lib/splitTimeline";
import type { SandboxStrip } from "@/lib/timelineAnalytics";
import { Badge, statusTone } from "@/components/ui";
import { DayMarker, EventRow } from "./EventRow";

export interface SplitLaneCounts {
  left: number;
  right: number;
  /** Non-lane events (run-level + unpicked sandboxes) shown full-width. */
  shared: number;
}

export default function SplitTimeline({
  rows,
  lanes,
  sandboxIds,
  labels,
  strips,
  counts,
  expanded,
  onToggle,
  onPickLane,
}: {
  rows: SplitRow[];
  lanes: LaneSelection;
  /** Every known sandbox id — options for the lane pickers. */
  sandboxIds: string[];
  /** Adapter/skill-pack label per sandbox id (analytics `label`). */
  labels: Map<string, string>;
  /** PR9 analytics strips — lane header status source. */
  strips: SandboxStrip[];
  counts: SplitLaneCounts;
  expanded: Set<number>;
  onToggle: (id: number) => void;
  onPickLane: (side: LaneSide, sandboxId: string) => void;
}) {
  const stripById = new Map(strips.map((s) => [s.sandboxId, s]));
  return (
    <>
      <div className="tl-lanes" role="group" aria-label="Split view lanes">
        <LaneHead
          side="left"
          sel={lanes.left}
          sandboxIds={sandboxIds}
          labels={labels}
          strip={lanes.left ? stripById.get(lanes.left) : undefined}
          count={counts.left}
          onPick={onPickLane}
        />
        <LaneHead
          side="right"
          sel={lanes.right}
          sandboxIds={sandboxIds}
          labels={labels}
          strip={lanes.right ? stripById.get(lanes.right) : undefined}
          count={counts.right}
          onPick={onPickLane}
        />
        {counts.shared > 0 ? (
          <div className="tl-lanes-note">
            {counts.shared} run-level / other-sandbox{" "}
            {counts.shared === 1 ? "event" : "events"} span both columns
          </div>
        ) : null}
      </div>

      {rows.map((row) => {
        if (row.kind === "day") {
          return <DayMarker key={row.key} day={row.day ?? ""} />;
        }
        if (row.shared) {
          const ev = row.shared;
          return (
            <EventRow
              key={row.key}
              ev={ev}
              expanded={expanded.has(ev.id)}
              onToggle={onToggle}
            />
          );
        }
        return (
          <div className="tl-srow" key={row.key}>
            <div className="tl-scell">
              {row.left ? (
                <EventRow
                  ev={row.left}
                  expanded={expanded.has(row.left.id)}
                  onToggle={onToggle}
                  showSandbox={false}
                />
              ) : null}
            </div>
            <div className="tl-scell">
              {row.right ? (
                <EventRow
                  ev={row.right}
                  expanded={expanded.has(row.right.id)}
                  onToggle={onToggle}
                  showSandbox={false}
                />
              ) : null}
            </div>
          </div>
        );
      })}
    </>
  );
}

function LaneHead({
  side,
  sel,
  sandboxIds,
  labels,
  strip,
  count,
  onPick,
}: {
  side: LaneSide;
  sel: string;
  sandboxIds: string[];
  labels: Map<string, string>;
  strip: SandboxStrip | undefined;
  count: number;
  onPick: (side: LaneSide, sandboxId: string) => void;
}) {
  const shown = strip ? (strip.terminalReason ?? strip.status) : null;
  const label = sel ? (labels.get(sel) ?? strip?.label ?? "") : "";
  return (
    <div className="tl-lane">
      <span className="tl-lane__tag">lane {side === "left" ? "a" : "b"}</span>
      <select
        className="select tl-lane__select"
        value={sel}
        onChange={(e) => onPick(side, e.target.value)}
        aria-label={`${side} lane sandbox`}
      >
        <option value="">— pick sandbox —</option>
        {sel && !sandboxIds.includes(sel) ? (
          <option value={sel}>{shortId(sel)} — (unknown)</option>
        ) : null}
        {sandboxIds.map((id) => (
          <option key={id} value={id}>
            {shortId(id)}
            {labels.get(id) ? ` — ${labels.get(id)}` : ""}
          </option>
        ))}
      </select>
      {shown ? <Badge tone={statusTone(shown)}>{shown}</Badge> : null}
      <span className="tl-lane__label" title={label}>
        {label}
      </span>
      <span className="tl-lane__count">{count} ev</span>
    </div>
  );
}
