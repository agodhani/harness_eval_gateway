"use client";

/**
 * PR36 — /dashboard: the home surface.
 *
 * Two fetches: `GET /api/analytics/summary` (weekly spend/runs series,
 * terminal mix, queue, totals, recent runs) + `GET
 * /api/analytics/leaderboard` (top-5 by model + config family — the PR32
 * aggregate reused). Polls while anything is in flight or parked so the
 * queue card stays live.
 *
 * Honest-empties rules (PR21/F22): unpriced spend renders "unpriced"
 * never $0; a week whose traffic lacked a rate leaves a gap in the bar
 * series and is named in the legend; pending (no terminal_reason)
 * sandboxes sit beside the donut, not inside it.
 */
import Link from "next/link";
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Pie,
  PieChart,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import {
  ApiError,
  getAnalyticsSummary,
  getLeaderboard,
  type DashboardSummary,
  type Leaderboard,
} from "@/lib/api";
import {
  donutData,
  queueHeadline,
  unpricedWeeks,
  weekRows,
} from "@/lib/dashboard";
import { formatNum, formatTs, formatUsd, shortId, truncate } from "@/lib/format";
import {
  C,
  ChartFrame,
  ChartTip,
  MIX_COLORS,
  SeriesChips,
} from "@/app/runs/[id]/analysis/charts";
import { BoardTable } from "@/components/LeaderboardBoards";
import {
  Badge,
  Card,
  EmptyState,
  ErrorNote,
  InfoNote,
  Loading,
  statusTone,
} from "@/components/ui";

const POLL_MS = 15_000;
const TOP_N = 5;

const TICK = {
  fill: C.text,
  fontSize: 11,
  fontFamily: "var(--mono)",
};
const CURSOR = { fill: "rgba(255, 255, 255, 0.045)" };
const GRID = <CartesianGrid stroke={C.grid} vertical={false} />;

/** Terminal reason → slice color (statusTone's palette, chart hexes). */
const REASON_COLORS: Record<string, string> = {
  agent_done: C.green,
  harness_complete: C.teal,
  timeout: C.amber,
  budget_exceeded: C.violet,
  stuck_loop: C.blue,
  error: C.red,
};

function SpendTip({
  active,
  label,
  payload,
}: {
  active?: boolean;
  label?: unknown;
  payload?: readonly { payload?: Record<string, unknown> }[];
}) {
  const p = payload?.[0]?.payload;
  if (!active || !p) return null;
  return (
    <div className="chart-tip">
      <div className="chart-tip__label">{String(p.week ?? label ?? "")}</div>
      <div className="chart-tip__row">
        <span>est. spend</span>
        <span className="chart-tip__val">
          {typeof p.cost === "number" ? formatUsd(p.cost) : "unpriced"}
        </span>
      </div>
      <div className="chart-tip__row">
        <span>llm turns</span>
        <span className="chart-tip__val">{String(p.llm_turns ?? 0)}</span>
      </div>
      {typeof p.unpriced === "number" && p.unpriced > 0 ? (
        <div className="chart-tip__foot">
          {p.unpriced} metered call{Number(p.unpriced) === 1 ? "" : "s"} with no
          rate — week total hidden, not zeroed
        </div>
      ) : null}
    </div>
  );
}

export default function DashboardClient() {
  const [summary, setSummary] = useState<DashboardSummary | null>(null);
  const [board, setBoard] = useState<Leaderboard | null>(null);
  const [boardError, setBoardError] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    try {
      const s = await getAnalyticsSummary({ weeks: 12, recent: 8 });
      setSummary(s);
      setError(null);
    } catch (err) {
      setError(
        err instanceof ApiError
          ? err.detail
          : "Failed to load the dashboard summary.",
      );
    } finally {
      setLoading(false);
    }
    // Top-5 boards — a leaderboard failure hides the cards, not the page.
    try {
      const b = await getLeaderboard();
      setBoard(b);
      setBoardError(null);
    } catch (err) {
      setBoardError(
        err instanceof ApiError ? err.detail : "Failed to load the leaderboard.",
      );
    }
  }, []);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      await Promise.resolve();
      if (!cancelled) await load();
    })();
    return () => {
      cancelled = true;
    };
  }, [load]);

  // Poll while the lane is busy — an active run, a queued promotion, or a
  // rubric awaiting review will all change the card without a refresh.
  useEffect(() => {
    const q = summary?.queue;
    const busy = Boolean(
      q && (q.active.length || q.queued.length || q.awaiting_review),
    );
    if (!busy) return;
    const t = setInterval(() => void load(), POLL_MS);
    return () => clearInterval(t);
  }, [summary, load]);

  const rows = useMemo(() => weekRows(summary?.weeks), [summary]);
  const donut = useMemo(
    () => donutData(summary?.terminal_mix, summary?.terminal_pending),
    [summary],
  );
  const spendAvailable = rows.some((r) => r.cost != null);
  const gaps = unpricedWeeks(rows);
  const sliceColors = useMemo(
    () =>
      donut.slices.map(
        (s, i) => REASON_COLORS[s.reason] ?? MIX_COLORS[i % MIX_COLORS.length],
      ),
    [donut],
  );

  const totals = summary?.totals;

  return (
    <main className="container">
      <div className="page-header">
        <div>
          <span className="kicker"><b>00</b> — dashboard</span>
          <h1 className="mono" style={{ fontSize: "1.15rem", marginBottom: "0.35rem" }}>
            Mission control
          </h1>
          <p className="subtitle">
            spend · throughput · rankings · outcomes — everything metered
            through the gateway
          </p>
        </div>
        <div className="row">
          <Link href="/submit" className="btn btn--primary btn--sm">
            + New run
          </Link>
        </div>
      </div>

      {error ? <ErrorNote>{error}</ErrorNote> : null}

      {loading && !summary ? (
        <Loading label="Aggregating…" />
      ) : summary && totals ? (
        totals.runs === 0 ? (
          <EmptyState>
            <div className="stack" style={{ alignItems: "center", gap: "0.6rem" }}>
              <span>
                No runs yet — the dashboard fills in once the first
                evaluation completes.
              </span>
              <Link href="/submit" className="btn btn--primary btn--sm">
                + Submit the first run
              </Link>
            </div>
          </EmptyState>
        ) : (
          <>
            {/* KPI strip — all-time totals from the summary aggregate. */}
            <div className="kpi-strip" style={{ marginBottom: "1rem" }}>
              <div className="kpi">
                <div className="kpi__label">runs</div>
                <div className="kpi__value">{formatNum(totals.runs)}</div>
                <div className="kpi__sub">all time</div>
              </div>
              <div className="kpi">
                <div className="kpi__label">sandboxes</div>
                <div className="kpi__value">{formatNum(totals.sandboxes)}</div>
                <div className="kpi__sub">
                  {totals.scored_sandboxes} scored
                </div>
              </div>
              <div className="kpi">
                <div className="kpi__label">llm turns</div>
                <div className="kpi__value">{formatNum(totals.llm_turns)}</div>
                <div className="kpi__sub">
                  {formatNum(totals.tokens.total)} tokens
                </div>
              </div>
              <div className="kpi">
                <div className="kpi__label">est. spend</div>
                <div className="kpi__value">
                  {totals.estimated_cost_usd != null
                    ? formatUsd(totals.estimated_cost_usd)
                    : "unpriced"}
                </div>
                <div className="kpi__sub">
                  {totals.estimated_cost_usd != null
                    ? summary.price_model
                      ? `at ${summary.price_model}`
                      : "at per-model rates"
                    : `${totals.unpriced_llm_turns} call${
                        totals.unpriced_llm_turns === 1 ? "" : "s"
                      } lack a rate`}
                </div>
              </div>
              <div className="kpi">
                <div className="kpi__label">queue</div>
                <div className="kpi__value" style={{ fontSize: "0.95rem" }}>
                  {queueHeadline(summary.queue)}
                </div>
                <div className="kpi__sub">pacing lane</div>
              </div>
            </div>

            <div className="analysis-grid">
              {/* Est. spend per week */}
              <Card title="Est. spend per week" className="card--flush">
                {rows.every((r) => r.llm_turns === 0) ? (
                  <EmptyState>No metered traffic yet.</EmptyState>
                ) : !spendAvailable ? (
                  <InfoNote>
                    Every metered call in the window is unpriced — add a rate
                    on the results page to see spend. (
                    {totals.unpriced_llm_turns} calls lack a rate.)
                  </InfoNote>
                ) : (
                  <>
                    <ChartFrame height={220}>
                      <BarChart data={rows} margin={{ top: 6, right: 12, bottom: 4, left: 0 }}>
                        {GRID}
                        <XAxis dataKey="label" tick={TICK} axisLine={{ stroke: C.axis }} tickLine={false} />
                        <YAxis
                          tick={TICK}
                          tickLine={false}
                          axisLine={false}
                          tickFormatter={(v: number) => formatUsd(v)}
                          width={56}
                        />
                        <Tooltip content={<SpendTip />} cursor={CURSOR} />
                        <Bar dataKey="cost" name="est. spend" fill={C.accent} barSize={18} />
                      </BarChart>
                    </ChartFrame>
                    <p className="chart-legend">
                      participant + judge + control-plane calls priced at
                      their own model rates
                      {gaps > 0
                        ? ` · ${gaps} week${gaps === 1 ? "" : "s"} hidden — unpriced traffic`
                        : ""}
                    </p>
                  </>
                )}
              </Card>

              {/* Runs per week */}
              <Card title="Runs per week" className="card--flush">
                {rows.every((r) => r.runs === 0) ? (
                  <EmptyState>No runs in the window.</EmptyState>
                ) : (
                  <>
                    <ChartFrame height={220}>
                      <BarChart data={rows} margin={{ top: 6, right: 12, bottom: 4, left: 0 }}>
                        {GRID}
                        <XAxis dataKey="label" tick={TICK} axisLine={{ stroke: C.axis }} tickLine={false} />
                        <YAxis
                          tick={TICK}
                          tickLine={false}
                          axisLine={false}
                          allowDecimals={false}
                          width={32}
                        />
                        <Tooltip
                          content={<ChartTip />}
                          cursor={CURSOR}
                        />
                        <Bar dataKey="runs" name="runs" fill={C.blue} barSize={18} />
                      </BarChart>
                    </ChartFrame>
                    <p className="chart-legend">
                      ISO weeks (Mon–Sun, UTC) — created_at buckets
                    </p>
                  </>
                )}
              </Card>

              {/* Terminal-reason mix donut */}
              <Card title="Terminal-reason mix" className="card--flush">
                {donut.terminated === 0 ? (
                  <EmptyState>
                    No terminal sandboxes yet
                    {donut.pending > 0 ? ` — ${donut.pending} still in flight` : ""}.
                  </EmptyState>
                ) : (
                  <>
                    <ChartFrame height={220}>
                      <PieChart>
                        <Pie
                          data={donut.slices}
                          dataKey="count"
                          nameKey="reason"
                          innerRadius="58%"
                          outerRadius="82%"
                          paddingAngle={2}
                          strokeWidth={0}
                          isAnimationActive={false}
                        >
                          {donut.slices.map((s, i) => (
                            <Cell key={s.reason} fill={sliceColors[i]} />
                          ))}
                        </Pie>
                        <Tooltip content={<ChartTip />} />
                        <text
                          x="50%"
                          y="47%"
                          textAnchor="middle"
                          fill={C.text}
                          style={{ fontFamily: "var(--mono)", fontSize: 18 }}
                        >
                          {donut.terminated}
                        </text>
                        <text
                          x="50%"
                          y="58%"
                          textAnchor="middle"
                          fill={C.faint}
                          style={{ fontFamily: "var(--mono)", fontSize: 9, letterSpacing: "0.1em" }}
                        >
                          TERMINAL
                        </text>
                      </PieChart>
                    </ChartFrame>
                    <SeriesChips
                      items={donut.slices.map((s, i) => ({
                        color: sliceColors[i],
                        label: `${s.reason} ${s.count} (${Math.round(s.share * 100)}%)`,
                      }))}
                    />
                    {donut.pending > 0 ? (
                      <p className="chart-legend">
                        + {donut.pending} sandbox
                        {donut.pending === 1 ? "" : "es"} with no terminal
                        state yet — kept out of the donut
                      </p>
                    ) : null}
                  </>
                )}
              </Card>

              {/* Queue / active status */}
              <Card title="Queue · active" className="card--flush">
                {summary.queue.active.length === 0 &&
                summary.queue.queued.length === 0 &&
                summary.queue.awaiting_review === 0 ? (
                  <EmptyState>Lane idle — nothing in flight or parked.</EmptyState>
                ) : (
                  <div className="table-wrap">
                    <table className="table">
                      <thead>
                        <tr>
                          <th>Lane</th>
                          <th>Run</th>
                          <th>Task</th>
                          <th>Model</th>
                        </tr>
                      </thead>
                      <tbody>
                        {summary.queue.active.map((r) => (
                          <tr key={r.run_id}>
                            <td>
                              <Badge tone={statusTone(r.status)}>{r.status}</Badge>
                            </td>
                            <td className="num">
                              <Link href={`/runs/${r.run_id}`} title={r.run_id}>
                                {shortId(r.run_id)}
                              </Link>
                            </td>
                            <td style={{ maxWidth: "240px" }}>
                              {truncate(r.task_prompt, 60)}
                            </td>
                            <td className="num">{r.model_name}</td>
                          </tr>
                        ))}
                        {summary.queue.queued.map((r) => (
                          <tr key={r.run_id}>
                            <td>
                              <Badge tone="amber">queued #{r.queue_position}</Badge>
                            </td>
                            <td className="num">
                              <Link href={`/runs/${r.run_id}`} title={r.run_id}>
                                {shortId(r.run_id)}
                              </Link>
                            </td>
                            <td style={{ maxWidth: "240px" }}>
                              {truncate(r.task_prompt, 60)}
                            </td>
                            <td className="num">{r.model_name}</td>
                          </tr>
                        ))}
                        {summary.queue.awaiting_review > 0 ? (
                          <tr>
                            <td>
                              <Badge tone="teal">pre-queue</Badge>
                            </td>
                            <td className="num faint" colSpan={3}>
                              {summary.queue.awaiting_review} run
                              {summary.queue.awaiting_review === 1 ? "" : "s"}{" "}
                              awaiting rubric review
                            </td>
                          </tr>
                        ) : null}
                      </tbody>
                    </table>
                  </div>
                )}
              </Card>
            </div>

            {/* Top-5 boards — the PR32 aggregate reused. */}
            <div className="split" style={{ marginTop: "1rem" }}>
              {board ? (
                <>
                  <BoardTable
                    title="Top models — by mean overall"
                    entries={board.by_model.slice(0, TOP_N)}
                    kind="model"
                  />
                  <BoardTable
                    title="Top config families — by mean overall"
                    entries={board.by_config.slice(0, TOP_N)}
                    kind="config"
                  />
                </>
              ) : (
                <Card title="Leaderboards">
                  {boardError ? (
                    <ErrorNote>{boardError}</ErrorNote>
                  ) : (
                    <Loading label="Ranking…" />
                  )}
                </Card>
              )}
            </div>
            <p className="faint" style={{ marginTop: "0.6rem", fontSize: "0.75rem" }}>
              top {TOP_N} only —{" "}
              <Link href="/leaderboard">full rankings + filters →</Link>
            </p>

            {/* Recent runs */}
            <div style={{ marginTop: "1rem" }}>
            <Card title="Recent runs" className="card--flush">
              {summary.recent_runs.length === 0 ? (
                <EmptyState>No runs yet.</EmptyState>
              ) : (
                <div className="table-wrap">
                  <table className="table">
                    <thead>
                      <tr>
                        <th>Run</th>
                        <th>Task</th>
                        <th>Preset</th>
                        <th>Model</th>
                        <th>Status</th>
                        <th>Cells</th>
                        <th>Score</th>
                        <th>Created</th>
                      </tr>
                    </thead>
                    <tbody>
                      {summary.recent_runs.map((r) => (
                        <tr key={r.run_id}>
                          <td className="num">
                            <Link href={`/runs/${r.run_id}`} title={r.run_id}>
                              {shortId(r.run_id)}
                            </Link>
                          </td>
                          <td style={{ maxWidth: "340px" }}>
                            {truncate(r.task_prompt, 90)}
                          </td>
                          <td className="num">
                            {r.preset_ref ? (
                              <Link href={`/presets/${r.preset_ref}`} className="mono">
                                {r.preset_ref}
                              </Link>
                            ) : (
                              <span className="faint">—</span>
                            )}
                          </td>
                          <td className="num">{r.model_name}</td>
                          <td>
                            <Badge tone={statusTone(r.status)}>{r.status}</Badge>
                          </td>
                          <td className="num">{r.sandbox_count}</td>
                          <td className="num">
                            {r.mean_overall_score != null ? (
                              <span className="score-value">
                                {Math.round(r.mean_overall_score * 10) / 10}
                              </span>
                            ) : (
                              <span className="faint">—</span>
                            )}
                          </td>
                          <td className="num faint">{formatTs(r.created_at)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </Card>
            </div>
            <p className="faint" style={{ marginTop: "0.6rem", fontSize: "0.75rem" }}>
              latest {summary.recent_runs.length} ·{" "}
              <Link href="/runs">all runs →</Link>
            </p>
          </>
        )
      ) : (
        <EmptyState>Dashboard unavailable — see the error above.</EmptyState>
      )}
    </main>
  );
}
