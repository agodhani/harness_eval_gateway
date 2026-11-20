"use client";

/**
 * PR31 — config × model heatmap grid + winner card.
 *
 * The "grid" half of the results page's `matrix | grid` toggle: rows are
 * submitted harness configs (the per-cell `model` injection stripped —
 * sandbox `name` can't group, it's unique per sandbox), columns are the
 * run's participant models, and each populated cell shows the mean judge
 * overall across its members (trials, when PR27 lands repeats) ±sd with
 * the PR21 weighted total underneath. Cells colour on the fixed 0–10
 * red→amber→green scale — readable at 32 cells and honest across runs.
 *
 * The winner card ranks cells by the mean WEIGHTED total (never the
 * judge's self-reported overall) and shows the pick's cost/wall-clock/
 * pass-rate beside the runner-up Δ. Cost+time come from the F9 analytics
 * endpoint — fetched lazily here so the card is self-contained; pass-rate
 * folds out of the results payload itself (generated_tests → exec).
 */
import { useEffect, useMemo, useState } from "react";
import {
  getRunAnalytics,
  type SandboxAnalytics,
  type SandboxResult,
} from "@/lib/api";
import type { DimSpec } from "@/lib/scoreMath";
import {
  buildScoreGrid,
  cellDisplayValue,
  cellExtras,
  heatColor,
  pickWinnerCell,
  type GridCell,
  type WinnerPick,
} from "@/lib/gridMatrix";
import { formatDuration, formatUsd } from "@/lib/format";
import { Badge, InfoNote } from "@/components/ui";

const r1 = (v: number) => Math.round(v * 10) / 10;
const r2 = (v: number) => Math.round(v * 100) / 100;

/** `0.75` → `75%` (1dp under 10% reads "8.3%", else whole). */
function formatRate(rate: number): string {
  const pct = rate * 100;
  return `${pct < 10 && pct !== Math.round(pct) ? pct.toFixed(1) : Math.round(pct)}%`;
}

/** Cost cell text — mean per sandbox across priced members; "unpriced"
 * when nothing resolved (PR21 honesty — never a $0 stand-in). */
function costText(extras: { meanCostUsd: number | null; pricedCount: number }, n: number): {
  text: string;
  title: string;
} {
  if (extras.pricedCount === 0 || extras.meanCostUsd == null) {
    return {
      text: "unpriced",
      title: "No member's model is in the price table — no honest rate",
    };
  }
  const partial = extras.pricedCount < n;
  return {
    text: formatUsd(extras.meanCostUsd) + (partial ? "*" : ""),
    title: partial
      ? `Mean over ${extras.pricedCount}/${n} priced sandboxes — the rest are unpriced`
      : `Mean estimated cost per sandbox (n=${n})`,
  };
}

function WinnerCard({
  pick,
  analytics,
  analyticsState,
}: {
  pick: WinnerPick | null;
  analytics: Map<string, SandboxAnalytics> | null;
  /** "loading"/"failed" keep cost+clock placeholders honest — "unpriced"
   * claims a resolved absence of price, which a pending/failed fetch
   * can't. */
  analyticsState: "loading" | "ready" | "failed";
}) {
  const extras = useMemo(
    () => (pick ? cellExtras(pick.cell, analytics ?? undefined) : null),
    [pick, analytics],
  );
  if (!pick || !extras) {
    return (
      <div className="winner winner--empty">
        <span className="faint">
          No weighted scores yet — the winner card appears once at least one
          cell has dimension scores.
        </span>
      </div>
    );
  }
  const { cell, runnerUp, delta } = pick;
  const cost = costText(extras, cell.n);
  return (
    <div className="winner">
      <div className="winner__head">
        <Badge tone="green">winner</Badge>
        <span className="winner__name" title={cell.rowLabel}>
          {cell.rowLabel}
        </span>
        <span className="mono faint">×</span>
        <span className="mono winner__model" title={cell.model}>
          {cell.model}
        </span>
      </div>
      <div className="winner__stats">
        <div className="winner__stat">
          <span className="winner__value score-value">
            {r1(cell.meanWeighted!)}
          </span>
          <span className="winner__label">
            weighted total{cell.n > 1 ? ` · mean of ${cell.n}` : ""}
            {cell.sdWeighted ? ` ±${r2(cell.sdWeighted)}` : ""}
          </span>
        </div>
        {cell.meanOverall != null ? (
          <div className="winner__stat">
            <span className="winner__value">{r1(cell.meanOverall)}</span>
            <span className="winner__label">judge overall</span>
          </div>
        ) : null}
        {delta != null && runnerUp ? (
          <div className="winner__stat">
            <span className="winner__value winner__value--delta">
              +{r2(delta)}
            </span>
            <span className="winner__label">
              vs {runnerUp.rowLabel} · {runnerUp.model}
            </span>
          </div>
        ) : null}
        <div className="winner__stat">
          <span className="winner__value" title={cost.title}>
            {analyticsState === "loading"
              ? "…"
              : analyticsState === "failed"
                ? "—"
                : cost.text}
          </span>
          <span className="winner__label">est. cost / sandbox</span>
        </div>
        <div className="winner__stat">
          <span className="winner__value">
            {extras.meanWallClockSeconds != null
              ? formatDuration(extras.meanWallClockSeconds * 1000)
              : analyticsState === "loading"
                ? "…"
                : "—"}
          </span>
          <span className="winner__label">wall clock</span>
        </div>
        <div className="winner__stat">
          <span className="winner__value">
            {extras.meanPassRate != null
              ? formatRate(extras.meanPassRate)
              : "—"}
          </span>
          <span className="winner__label">
            pass rate{extras.passRateCount < cell.n ? ` (${extras.passRateCount}/${cell.n} measured)` : ""}
          </span>
        </div>
      </div>
      {runnerUp ? (
        <div className="winner__runnerup faint">
          runner-up: <b>{runnerUp.rowLabel}</b> ×{" "}
          <span className="mono">{runnerUp.model}</span> — weighted{" "}
          {r1(runnerUp.meanWeighted!)}
        </div>
      ) : null}
    </div>
  );
}

function GridCellView({
  cell,
  isWinner,
}: {
  cell: GridCell | undefined;
  isWinner: boolean;
}) {
  if (!cell) {
    return (
      <td className="grid-cell grid-cell--empty" title="No sandbox ran this cell">
        <span className="score-na">—</span>
      </td>
    );
  }
  const disp = cellDisplayValue(cell);
  const color = heatColor(disp.value);
  const members = cell.sandboxes
    .map(
      (s) =>
        `${s.name ?? s.sandbox_id.slice(0, 8)} — ${s.status}` +
        (s.trial_index != null ? ` (trial ${s.trial_index})` : ""),
    )
    .join("\n");
  return (
    <td
      className={`grid-cell${isWinner ? " grid-cell--winner" : ""}`}
      style={
        color
          ? {
              background: `color-mix(in srgb, ${color} 34%, var(--surface))`,
            }
          : undefined
      }
      title={members || undefined}
    >
      {disp.value != null ? (
        <>
          <span className="grid-cell__score">
            {disp.basis === "weighted" ? "w " : ""}
            {r1(disp.value)}
          </span>
          <span className="grid-cell__sub">
            {(() => {
              const sd =
                disp.basis === "weighted" ? cell.sdWeighted : cell.sdOverall;
              const scored =
                disp.basis === "weighted" ? cell.nWeighted : cell.nOverall;
              return (
                <>
                  {cell.n > 1 && sd != null ? `±${r2(sd)} · ` : ""}×{cell.n}
                  {cell.n > 1 && scored < cell.n
                    ? ` (${scored} scored)`
                    : ""}
                </>
              );
            })()}
          </span>
          {disp.basis === "overall" && cell.meanWeighted != null ? (
            <span className="grid-cell__sub grid-cell__sub--w">
              w {r1(cell.meanWeighted)}
            </span>
          ) : null}
          {isWinner ? <span className="grid-cell__crown">★</span> : null}
        </>
      ) : (
        <span className="score-na" title="No scores for this cell yet">
          —
        </span>
      )}
    </td>
  );
}

export default function GridView({
  runId,
  sandboxes,
  models,
  dimensionKeys,
  specMap,
}: {
  runId: string;
  sandboxes: SandboxResult[];
  /** RunResults.models — participant-model column order. */
  models: string[] | undefined;
  /** Same inputs as the matrix totals row — shared PR21 weighting. */
  dimensionKeys: string[];
  specMap: Map<string, DimSpec>;
}) {
  const [analytics, setAnalytics] = useState<Map<
    string,
    SandboxAnalytics
  > | null>(null);
  const [analyticsState, setAnalyticsState] = useState<
    "loading" | "ready" | "failed"
  >("loading");

  // Winner-card cost/time come from the F9 analytics endpoint — fetched
  // lazily here so the grid stays self-contained (AnalyticsSection keeps
  // its own copy + price-model toggle).
  useEffect(() => {
    let cancelled = false;
    void (async () => {
      await Promise.resolve(); // async context — no setState-in-effect
      if (cancelled) return;
      setAnalyticsState("loading"); // run switch resets stale cost/clock
      try {
        const a = await getRunAnalytics(runId);
        if (cancelled) return;
        setAnalytics(new Map(a.sandboxes.map((s) => [s.sandbox_id, s])));
        setAnalyticsState("ready");
      } catch {
        if (!cancelled) setAnalyticsState("failed");
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [runId]);

  const grid = useMemo(
    () => buildScoreGrid(sandboxes, models, dimensionKeys, specMap),
    [sandboxes, models, dimensionKeys, specMap],
  );
  const winner = useMemo(() => pickWinnerCell(grid.cells), [grid]);
  const winnerKey = winner
    ? `${winner.cell.rowKey}::${winner.cell.model}`
    : null;

  if (grid.rows.length === 0) {
    return <InfoNote>No scored sandboxes to grid yet.</InfoNote>;
  }

  return (
    <div className="grid-view">
      <WinnerCard
        pick={winner}
        analytics={analytics}
        analyticsState={analyticsState}
      />
      <div className="matrix-wrap">
        <table className="matrix grid-table">
          <thead>
            <tr>
              <th className="grid-corner">config \ model</th>
              {grid.models.map((m) => (
                <th key={m} className="grid-colhead" title={m}>
                  <span className="mono">{m}</span>
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {grid.rows.map((row) => (
              <tr key={row.key}>
                <th className="grid-rowhead" scope="row">
                  <div className="dim-name">{row.label}</div>
                  {row.detail && row.detail !== row.label ? (
                    <div className="dim-desc">{row.detail}</div>
                  ) : null}
                </th>
                {grid.models.map((m) => {
                  const cell = row.cells.get(m);
                  return (
                    <GridCellView
                      key={m}
                      cell={cell}
                      isWinner={winnerKey === `${row.key}::${m}`}
                    />
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="faint grid-legend">
        cell = mean judge <b>overall</b> across its sandboxes (±sd · ×n over
        trials) · <b>w</b> = PR21 weighted total — it ranks the winner, not
        the judge&apos;s own aggregate · colour fixed on the 0–10 scale so
        cells compare honestly
      </p>
    </div>
  );
}
