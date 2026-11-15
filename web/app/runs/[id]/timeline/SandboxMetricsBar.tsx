"use client";

/**
 * PR9 — per-sandbox metrics strip rendered inside the timeline card, between
 * the filter toolbar and the event stream. Data is pre-computed by
 * `sandboxStrips` (lib/timelineAnalytics.ts) from the F9 analytics response —
 * this component is presentational only.
 *
 * Clicking a chip toggles selection: in merged mode that is the timeline's
 * sandbox filter (same state the toolbar dropdown drives); in split mode
 * (PR10) TimelineClient routes the click to lane assignment instead — the
 * strip keeps working as the sandbox picker for both views. The strip is a
 * single horizontal row — PR10's split view keeps it untouched above the
 * stream area.
 */
import { shortId } from "@/lib/format";
import { Badge, statusTone } from "@/components/ui";
import type { SandboxStrip } from "@/lib/timelineAnalytics";

export default function SandboxMetricsBar({
  strips,
  selectedIds,
  onSelect,
}: {
  strips: SandboxStrip[];
  /** Chips rendered active — merged: the one filtered sandbox; split: both lanes. */
  selectedIds: ReadonlySet<string>;
  onSelect: (sandboxId: string) => void;
}) {
  if (strips.length === 0) return null;
  return (
    <div className="tl-metrics" role="group" aria-label="Per-sandbox metrics">
      {strips.map((s) => {
        const active = selectedIds.has(s.sandboxId);
        const shown = s.terminalReason ?? s.status;
        return (
          <button
            type="button"
            key={s.sandboxId}
            className={`tl-metric${active ? " is-active" : ""}`}
            onClick={() => onSelect(s.sandboxId)}
            title={`${s.sandboxId}${s.label ? ` · ${s.label}` : ""} — ${active ? "deselect" : "select"} (merged: filter · split: assign to a lane)`}
            aria-pressed={active}
          >
            <span className="tl-metric__head">
              <span className="mono tl-metric__id">{shortId(s.sandboxId)}</span>
              <span className="tl-metric__label">{s.label}</span>
              <Badge tone={statusTone(shown)}>{shown}</Badge>
            </span>
            <span className="tl-metric__stats">
              {s.chips.map((c) => (
                <span
                  key={c.label}
                  className={`tl-metric__stat${c.tone === "bad" ? " tl-metric__stat--bad" : ""}`}
                  title={c.title}
                >
                  <span className="tl-metric__k">{c.label}</span> {c.value}
                </span>
              ))}
            </span>
            {s.evalLine ? (
              <span className="tl-metric__eval">{s.evalLine}</span>
            ) : null}
          </button>
        );
      })}
    </div>
  );
}
