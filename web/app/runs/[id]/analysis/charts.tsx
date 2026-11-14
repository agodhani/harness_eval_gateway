"use client";

/**
 * F13 — themed recharts primitives for the analysis page.
 *
 * Recharts needs concrete colors — CSS vars can't be read from here — so
 * the palette mirrors the globals.css tokens (keep in sync: --accent
 * #c8f04a, --blue #7ab8ff, --violet #b79aff, --teal #4fd1c5, --amber
 * #e8b45a, --green #7dd069, --red #ff6b6b, --border #1b2024,
 * --border-strong #2a3138, --text-dim #9aa4ad, --text-faint #57616a).
 * Sharp corners + hairline grids, mono ticks — same instrument-panel look
 * as the tables elsewhere.
 */
import type { ReactNode } from "react";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  ComposedChart,
  Line,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { formatNum } from "@/lib/format";

/** Theme colors — mirrors globals.css (see header comment). */
export const C = {
  accent: "#c8f04a",
  blue: "#7ab8ff",
  violet: "#b79aff",
  teal: "#4fd1c5",
  amber: "#e8b45a",
  green: "#7dd069",
  red: "#ff6b6b",
  grid: "#1b2024",
  axis: "#2a3138",
  text: "#9aa4ad",
  faint: "#57616a",
};

/** Per-sandbox series color, by position (matches the chip rows). */
export const SANDBOX_COLORS = [C.accent, C.blue, C.violet, C.teal, C.amber, C.green];

/** Series colors for non-sandbox keys (tool names, metric names). */
export const MIX_COLORS = [C.blue, C.violet, C.teal, C.amber, C.accent, C.green, C.red];

export interface Series {
  key: string;
  name: string;
  color: string;
}

export type Row = Record<string, unknown>;

const TICK = {
  fill: C.text,
  fontSize: 11,
  fontFamily: "var(--mono)",
};

function toNum(v: unknown): number {
  const n = typeof v === "number" ? v : Number(v);
  return Number.isFinite(n) ? n : 0;
}

// ---------------------------------------------------------------------------
// ChartTip — dark tooltip matching the panel theme (recharts default is white)
// ---------------------------------------------------------------------------

interface TipEntry {
  name?: unknown;
  value?: unknown;
  color?: string;
  payload?: Row;
}

export interface ChartTipProps {
  active?: boolean;
  label?: unknown;
  payload?: readonly TipEntry[];
  /** Formatter applied to every entry value. */
  fmt?: (v: number) => string;
  /** Optional extra lines rendered under the entries (from payload[0].payload). */
  footer?: (datum: Row | undefined) => ReactNode;
}

export function ChartTip({ active, label, payload, fmt, footer }: ChartTipProps) {
  if (!active || !payload || payload.length === 0) return null;
  const entries = payload.filter((p) => p.value !== undefined && p.value !== null);
  return (
    <div className="chart-tip">
      <div className="chart-tip__label">{String(label ?? "")}</div>
      {entries.map((p, i) => (
        <div className="chart-tip__row" key={i}>
          <span>
            <i className="chart-tip__sw" style={{ background: String(p.color ?? C.text) }} />
            {String(p.name ?? "")}
          </span>
          <span className="chart-tip__val">
            {fmt ? fmt(toNum(p.value)) : String(p.value)}
          </span>
        </div>
      ))}
      {footer ? <div className="chart-tip__foot">{footer(entries[0]?.payload)}</div> : null}
    </div>
  );
}

// ---------------------------------------------------------------------------
// shared frame: sizing container + grid + axis conventions
// ---------------------------------------------------------------------------

export function ChartFrame({ children, height = 240 }: { children: ReactNode; height?: number }) {
  return (
    <div className="chart" style={{ height }}>
      <ResponsiveContainer width="100%" height="100%">
        {children}
      </ResponsiveContainer>
    </div>
  );
}

const MARGIN = { top: 6, right: 12, bottom: 4, left: 0 };
const CURSOR = { fill: "rgba(255, 255, 255, 0.045)" };

/** Grouped horizontal bars — one row per category (long labels fit on Y). */
export function HBarGroup({
  rows,
  series,
  catKey,
  domain,
  fmt,
}: {
  rows: Row[];
  series: Series[];
  catKey: string;
  domain: [number, number];
  fmt?: (v: number) => string;
}) {
  return (
    <ChartFrame height={Math.max(180, rows.length * (series.length * 12 + 26))}>
      <BarChart data={rows} layout="vertical" margin={{ ...MARGIN, left: 8 }}>
        <CartesianGrid stroke={C.grid} horizontal={false} />
        <XAxis
          type="number"
          domain={domain}
          tick={TICK}
          axisLine={{ stroke: C.axis }}
          tickLine={false}
          tickFormatter={fmt}
        />
        <YAxis
          type="category"
          dataKey={catKey}
          width={168}
          tick={TICK}
          axisLine={{ stroke: C.axis }}
          tickLine={false}
        />
        <Tooltip content={<ChartTip fmt={fmt} />} cursor={CURSOR} />
        {series.map((s) => (
          <Bar key={s.key} dataKey={s.key} name={s.name} fill={s.color} barSize={9} />
        ))}
      </BarChart>
    </ChartFrame>
  );
}

/** Vertical bars, stacked (`stacked`) or grouped, over a `name` category axis. */
export function VBars({
  rows,
  series,
  stacked = false,
  fmt,
  height = 240,
}: {
  rows: Row[];
  series: Series[];
  stacked?: boolean;
  fmt?: (v: number) => string;
  height?: number;
}) {
  return (
    <ChartFrame height={height}>
      <BarChart data={rows} margin={MARGIN}>
        <CartesianGrid stroke={C.grid} vertical={false} />
        <XAxis dataKey="name" tick={TICK} axisLine={{ stroke: C.axis }} tickLine={false} />
        <YAxis tick={TICK} tickLine={false} axisLine={false} tickFormatter={fmt} width={56} />
        <Tooltip content={<ChartTip fmt={fmt} />} cursor={CURSOR} />
        {series.map((s) => (
          <Bar
            key={s.key}
            dataKey={s.key}
            name={s.name}
            fill={s.color}
            {...(stacked ? { stackId: "stack" } : {})}
            barSize={stacked ? 30 : 14}
          />
        ))}
      </BarChart>
    </ChartFrame>
  );
}

/** Single-metric bars tinted per row (`row.color`) — e.g. cost per sandbox. */
export function ColoredBars({
  rows,
  dataKey,
  name,
  fmt,
}: {
  rows: Row[];
  dataKey: string;
  name: string;
  fmt?: (v: number) => string;
}) {
  return (
    <ChartFrame>
      <BarChart data={rows} margin={MARGIN}>
        <CartesianGrid stroke={C.grid} vertical={false} />
        <XAxis dataKey="name" tick={TICK} axisLine={{ stroke: C.axis }} tickLine={false} />
        <YAxis tick={TICK} tickLine={false} axisLine={false} tickFormatter={fmt} width={56} />
        <Tooltip content={<ChartTip fmt={fmt} />} cursor={CURSOR} />
        <Bar dataKey={dataKey} name={name} barSize={34}>
          {rows.map((r, i) => (
            <Cell key={i} fill={String(r.color ?? C.blue)} />
          ))}
        </Bar>
      </BarChart>
    </ChartFrame>
  );
}

/**
 * Cross-run trend: stacked participant+evaluator token bars (left axis) with
 * the mean overall score as a line on the right axis (fixed 0–10). The
 * current run's participant bar uses the accent color.
 */
export function TrendChart({ rows }: { rows: Row[] }) {
  return (
    <ChartFrame height={300}>
      <ComposedChart data={rows} margin={{ ...MARGIN, right: 4 }}>
        <CartesianGrid stroke={C.grid} vertical={false} />
        <XAxis
          dataKey="label"
          tick={{ ...TICK, fontSize: 10 }}
          axisLine={{ stroke: C.axis }}
          tickLine={false}
          interval={0}
          angle={-32}
          textAnchor="end"
          height={56}
        />
        <YAxis
          yAxisId="tok"
          tick={TICK}
          tickLine={false}
          axisLine={false}
          tickFormatter={(v: number) => formatNum(v)}
          width={56}
        />
        <YAxis
          yAxisId="score"
          orientation="right"
          domain={[0, 10]}
          tick={{ ...TICK, fill: C.violet }}
          tickLine={false}
          axisLine={false}
          width={28}
        />
        <Tooltip
          content={
            <ChartTip
              footer={(d) =>
                d?.meta != null && typeof d.meta === "object" ? (
                  <TrendMeta meta={d.meta as Row} />
                ) : null
              }
            />
          }
          cursor={CURSOR}
        />
        <Bar yAxisId="tok" dataKey="participant" name="participant tokens" stackId="tok" barSize={22}>
          {rows.map((r, i) => (
            <Cell
              key={i}
              fill={r.isCurrent ? C.accent : C.blue}
              fillOpacity={r.isCurrent ? 1 : 0.75}
            />
          ))}
        </Bar>
        <Bar
          yAxisId="tok"
          dataKey="evaluator"
          name="evaluator tokens"
          stackId="tok"
          fill={C.amber}
          fillOpacity={0.8}
          barSize={22}
        />
        <Line
          yAxisId="score"
          type="monotone"
          dataKey="score"
          name="mean score"
          stroke={C.violet}
          strokeWidth={1.5}
          dot={{ r: 2.5, fill: C.violet, strokeWidth: 0 }}
          connectNulls
        />
      </ComposedChart>
    </ChartFrame>
  );
}

function TrendMeta({ meta }: { meta: Row }) {
  const m = meta as {
    status?: string;
    model_name?: string;
    sandbox_count?: number;
    llm_turns?: number;
    errors?: number;
    evaluator_errors?: number;
    estimated_cost_usd?: number;
    price_model?: string;
    task_prompt?: string;
  };
  return (
    <>
      {m.task_prompt ? <div className="chart-tip__prompt">{m.task_prompt}</div> : null}
      <div>
        {m.status} · {m.model_name} · {m.sandbox_count} sandbox
        {m.sandbox_count === 1 ? "" : "es"} · {m.llm_turns} turns · {m.errors} err
        {m.evaluator_errors ? ` · ${m.evaluator_errors} judge err` : ""}
      </div>
      <div>
        est. ${(m.estimated_cost_usd ?? 0).toFixed(4)} at {m.price_model}
      </div>
    </>
  );
}

/** Legend chips under a chart — `■ label` per series/sandbox. */
export function SeriesChips({ items }: { items: { color: string; label: string }[] }) {
  return (
    <div className="chart-legend">
      {items.map((i) => (
        <span key={i.label}>
          <i className="chart-legend__sw" style={{ background: i.color }} />
          {i.label}
        </span>
      ))}
    </div>
  );
}
