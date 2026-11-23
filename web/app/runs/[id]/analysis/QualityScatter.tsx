"use client";

/**
 * PR33 — quality × cost (or wall-clock) scatter + auto summary card.
 *
 * Each dot is a config × model matrix cell (the PR31 grid fold — trials
 * mean in): y = mean weighted score, x = mean estimated cost per sandbox
 * or wall-clock, color/shape = model, size = avg output tok/s. Rings +
 * the dashed line mark the Pareto frontier — no other cell is both
 * cheaper/faster AND better. Cells missing the active axis are excluded
 * and named in the legend (PR21 honesty — an unpriced cell never plots
 * at $0). The summary card under the chart is a deterministic template
 * from the same transform — no LLM call.
 */
import { useMemo, useState } from "react";
import {
  CartesianGrid,
  ResponsiveContainer,
  Scatter,
  ScatterChart,
  Tooltip,
  XAxis,
  YAxis,
  ZAxis,
} from "recharts";
import type { SandboxAnalytics } from "@/lib/api";
import type { GridCell } from "@/lib/gridMatrix";
import {
  buildScatterData,
  verdictLines,
  type ScatterPoint,
  type ScatterXAxis,
} from "@/lib/scatterMatrix";
import { formatDuration, formatNum, formatUsd } from "@/lib/format";
import { Card, EmptyState, InfoNote } from "@/components/ui";
import { C, MIX_COLORS, SeriesChips } from "./charts";

const TICK = {
  fill: C.text,
  fontSize: 11,
  fontFamily: "var(--mono)",
};

/** Model → marker shape cycle (color alone shouldn't carry meaning). */
const MODEL_SHAPES = [
  "circle",
  "diamond",
  "triangle",
  "square",
  "star",
  "cross",
  "wye",
] as const;

/** Chart row — the point plus the recharts z (size) + pareto flag. */
interface ScatterRow extends ScatterPoint {
  z: number;
  pareto: boolean;
}

/** Hollow accent ring drawn over frontier cells — marks membership
 * without hiding the model color underneath. `size` is the recharts
 * symbol area; a ring 3px past its radius sits just outside the dot. */
function FrontierRing(props: {
  cx?: number;
  cy?: number;
  size?: number;
}) {
  const r = Math.sqrt(Math.max(props.size ?? 64, 1) / Math.PI) + 3;
  return (
    <circle
      cx={props.cx}
      cy={props.cy}
      r={r}
      fill="none"
      stroke={C.accent}
      strokeWidth={1.4}
    />
  );
}

function ScatterTip({
  active,
  payload,
  axis,
}: {
  active?: boolean;
  payload?: readonly { payload?: ScatterRow }[];
  axis: ScatterXAxis;
}) {
  const p = payload?.[0]?.payload;
  if (!active || !p) return null;
  return (
    <div className="chart-tip">
      <div className="chart-tip__label">{p.rowLabel}</div>
      <div className="chart-tip__row">
        <span>model</span>
        <span className="chart-tip__val">{p.model}</span>
      </div>
      <div className="chart-tip__row">
        <span>weighted</span>
        <span className="chart-tip__val">
          {p.y.toFixed(1)}
          {p.sdY != null ? ` ±${p.sdY.toFixed(2)}` : ""}
        </span>
      </div>
      <div className="chart-tip__row">
        <span>{axis === "cost" ? "est. cost" : "wall clock"}</span>
        <span className="chart-tip__val">
          {axis === "cost"
            ? p.costUsd != null
              ? formatUsd(p.costUsd)
              : "unpriced"
            : p.wallClockSeconds != null
              ? formatDuration(p.wallClockSeconds * 1000)
              : "—"}
        </span>
      </div>
      {axis === "cost" && p.wallClockSeconds != null ? (
        <div className="chart-tip__row">
          <span>wall clock</span>
          <span className="chart-tip__val">
            {formatDuration(p.wallClockSeconds * 1000)}
          </span>
        </div>
      ) : null}
      <div className="chart-tip__row">
        <span>speed</span>
        <span className="chart-tip__val">
          {p.tps != null ? `${formatNum(Math.round(p.tps * 10) / 10)} t/s` : "—"}
        </span>
      </div>
      <div className="chart-tip__foot">
        {p.n > 1 ? `mean of ${p.n} sandboxes` : "1 sandbox"}
        {p.pricedCount > 0 && p.pricedCount < p.n
          ? ` · priced over ${p.pricedCount}/${p.n}`
          : ""}
        {p.pareto ? " · Pareto frontier" : ""}
      </div>
    </div>
  );
}

export default function QualityScatter({
  cells,
  analyticsById,
}: {
  cells: GridCell[];
  analyticsById: Map<string, SandboxAnalytics> | undefined;
}) {
  const [axis, setAxis] = useState<ScatterXAxis>("cost");
  const data = useMemo(
    () => buildScatterData(cells, analyticsById, axis),
    [cells, analyticsById, axis],
  );

  /** Model → color/shape — first-seen point order keeps assignments
   * stable across axis toggles and polls. */
  const series = useMemo(() => {
    const frontierKeys = new Set(data.frontier.map((p) => p.key));
    const order: string[] = [];
    for (const p of data.points) {
      if (!order.includes(p.model)) order.push(p.model);
    }
    return order.map((model, i) => ({
      model,
      color: MIX_COLORS[i % MIX_COLORS.length],
      shape: MODEL_SHAPES[i % MODEL_SHAPES.length],
      rows: data.points
        .filter((p) => p.model === model && p.x != null)
        .map((p) => ({ ...p, z: p.tps ?? 0, pareto: frontierKeys.has(p.key) })),
    }));
  }, [data]);

  /** Frontier as chart rows — same z/flag fields as the model dots so
   * ring size + tooltip stay consistent with what's underneath. */
  const frontierRows: ScatterRow[] = useMemo(
    () => data.frontier.map((p) => ({ ...p, z: p.tps ?? 0, pareto: true })),
    [data],
  );

  const plottable = data.points.filter((p) => p.x != null).length;
  const lines = data.verdict ? verdictLines(data.verdict, axis) : [];

  return (
    <Card
      title={`Quality × ${axis === "cost" ? "cost" : "wall clock"}`}
      className="card--flush card--span2"
      actions={
        <div className="seg" role="tablist" aria-label="Scatter x axis">
          <button
            type="button"
            role="tab"
            aria-selected={axis === "cost"}
            className={`seg__btn${axis === "cost" ? " seg__btn--active" : ""}`}
            onClick={() => setAxis("cost")}
          >
            cost
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={axis === "wall_clock"}
            className={`seg__btn${axis === "wall_clock" ? " seg__btn--active" : ""}`}
            onClick={() => setAxis("wall_clock")}
          >
            wall clock
          </button>
        </div>
      }
    >
      {data.points.length === 0 ? (
        <EmptyState>
          No weighted scores yet — the scatter appears once at least one
          cell is scored{data.unscoredCells > 0 ? " (cells exist but carry none)" : ""}.
        </EmptyState>
      ) : plottable === 0 ? (
        <InfoNote>
          No scored cell has {axis === "cost" ? "a resolved price" : "wall-clock data"} —{" "}
          {data.excluded
            .map((p) => `${p.rowLabel} × ${p.model}`)
            .join(", ")}{" "}
          excluded.{" "}
          {axis === "cost"
            ? "Add a rate on the cost card or switch to wall clock."
            : "Switch to cost."}
        </InfoNote>
      ) : (
        <>
          <div className="chart" style={{ height: 300 }}>
            <ResponsiveContainer width="100%" height="100%">
              <ScatterChart margin={{ top: 10, right: 18, bottom: 4, left: 0 }}>
                <CartesianGrid stroke={C.grid} />
                <XAxis
                  type="number"
                  dataKey="x"
                  name={axis === "cost" ? "est. cost" : "wall clock"}
                  tick={TICK}
                  axisLine={{ stroke: C.axis }}
                  tickLine={false}
                  tickFormatter={(v: number) =>
                    axis === "cost"
                      ? formatUsd(v)
                      : formatDuration(v * 1000)
                  }
                />
                <YAxis
                  type="number"
                  dataKey="y"
                  name="weighted"
                  domain={[0, 10]}
                  tick={TICK}
                  tickLine={false}
                  axisLine={false}
                  width={32}
                />
                <ZAxis type="number" dataKey="z" range={[70, 380]} />
                <Tooltip
                  content={<ScatterTip axis={axis} />}
                  cursor={{ strokeDasharray: "4 3", stroke: C.faint }}
                />
                {series.map((s) => (
                  <Scatter
                    key={s.model}
                    name={s.model}
                    data={s.rows}
                    fill={s.color}
                    shape={s.shape}
                    isAnimationActive={false}
                  />
                ))}
                {/* Frontier overlay — dashed connector + rings on the
                    non-dominated cells (drawn last, on top). */}
                {frontierRows.length > 1 ? (
                  <Scatter
                    data={frontierRows}
                    line={{
                      stroke: C.accent,
                      strokeWidth: 1.5,
                      strokeDasharray: "5 3",
                    }}
                    shape={FrontierRing}
                    isAnimationActive={false}
                  />
                ) : frontierRows.length === 1 ? (
                  <Scatter
                    data={frontierRows}
                    shape={FrontierRing}
                    isAnimationActive={false}
                  />
                ) : null}
              </ScatterChart>
            </ResponsiveContainer>
          </div>
          <SeriesChips
            items={series.map((s) => ({ color: s.color, label: s.model }))}
          />
          <p className="chart-legend">
            rings + dashed line = Pareto frontier — no cell is both{" "}
            {axis === "cost" ? "cheaper" : "faster"} and better · dot size =
            mean tok/s (smallest = unmeasured) · cell = mean of its sandboxes
            {data.partialCostCells > 0 && axis === "cost"
              ? ` · ${data.partialCostCells} cell${
                  data.partialCostCells === 1 ? "" : "s"
                } priced over only some members`
              : ""}
          </p>
          {data.excluded.length > 0 ? (
            <p className="chart-legend">
              excluded — {axis === "cost" ? "model not in the price table" : "no wall-clock data"}:{" "}
              {data.excluded
                .map((p) => `${p.rowLabel} × ${p.model}`)
                .join(", ")}
            </p>
          ) : null}
          {data.unscoredCells > 0 ? (
            <p className="chart-legend">
              {data.unscoredCells} cell{data.unscoredCells === 1 ? "" : "s"}{" "}
              have no weighted scores — not plotted
            </p>
          ) : null}
          {lines.length > 0 ? (
            <div className="verdict">
              <span className="verdict__tag">verdict</span>
              <ul className="verdict__lines">
                {lines.map((line, i) => (
                  <li key={i} className={i === 0 ? "verdict__line verdict__line--head" : "verdict__line"}>
                    {line}
                  </li>
                ))}
              </ul>
            </div>
          ) : null}
        </>
      )}
    </Card>
  );
}
