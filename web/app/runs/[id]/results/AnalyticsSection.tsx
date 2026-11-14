"use client";

/**
 * F9 — analytics: side-by-side per-sandbox metrics + estimated cost.
 *
 * Fetches GET /api/runs/{id}/analytics once; the "Estimated cost at:"
 * dropdown recomputes each sandbox's cost client-side from the response's
 * tokens + price_table (no refetch). Failure renders a quiet InfoNote —
 * analytics are supplementary to the score matrix.
 */
import { useEffect, useState } from "react";
import {
  ApiError,
  estimateCostUsd,
  getRunAnalytics,
  type ModelPrice,
  type RunAnalytics,
  type SandboxAnalytics,
} from "@/lib/api";
import { formatDuration, shortId } from "@/lib/format";
import { Badge, Card, InfoNote, statusTone } from "@/components/ui";

/** Deterministic thousands separator (no toLocaleString — hydration-safe). */
function num(n: number): string {
  return String(n).replace(/\B(?=(\d{3})+(?!\d))/g, ",");
}

/** $-formatting for small estimate values: 4dp under a cent, else 2dp. */
function formatUsd(cost: number): string {
  if (!Number.isFinite(cost)) return "—";
  if (cost === 0) return "$0.00";
  if (cost < 0.01) return `$${cost.toFixed(4)}`;
  return `$${cost.toFixed(2)}`;
}

function errMsg(err: unknown): string {
  if (err instanceof ApiError) return err.detail;
  if (err instanceof Error) return err.message;
  return "analytics endpoint unavailable";
}

interface Row {
  label: string;
  hint?: string;
  cell: (sb: SandboxAnalytics) => React.ReactNode;
}

function breakdown(by: Record<string, number>): React.ReactNode {
  const entries = Object.entries(by).sort((a, b) => b[1] - a[1]);
  if (entries.length === 0) return <span className="score-na">—</span>;
  return (
    <div>
      {entries.map(([name, count]) => (
        <div key={name} className="dim-desc">
          <code>{name}</code> ×{count}
        </div>
      ))}
    </div>
  );
}

export default function AnalyticsSection({ runId }: { runId: string }) {
  const [analytics, setAnalytics] = useState<RunAnalytics | null>(null);
  const [loadedFor, setLoadedFor] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [priceModel, setPriceModel] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      await Promise.resolve(); // async context → no setState-in-effect
      try {
        const a = await getRunAnalytics(runId);
        if (cancelled) return;
        setAnalytics(a);
        setLoadedFor(runId);
        setError(null);
      } catch (err) {
        if (cancelled) return;
        setAnalytics(null);
        setLoadedFor(runId);
        setError(errMsg(err));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [runId]);

  const current = loadedFor === runId ? analytics : null;
  // Effective price model: user override > server-selected default.
  const selected = priceModel ?? current?.price_model ?? "";
  const price: ModelPrice | undefined = current?.price_table?.[selected];

  if (current === null) {
    if (loadedFor !== runId) return null; // still loading — page shows spinners
    return (
      <Card title="Analytics">
        <InfoNote>Analytics unavailable: {error}</InfoNote>
      </Card>
    );
  }
  if (current.sandboxes.length === 0) return null;

  const rows: Row[] = [
    {
      label: "terminal state",
      cell: (sb) =>
        sb.terminal_reason ? (
          <Badge tone={statusTone(sb.terminal_reason)}>{sb.terminal_reason}</Badge>
        ) : (
          <span className="score-na">—</span>
        ),
    },
    {
      label: "wall clock",
      cell: (sb) => formatDuration(sb.wall_clock_seconds * 1000) || "—",
    },
    { label: "LLM turns", cell: (sb) => num(sb.llm_turns) },
    { label: "tokens in", cell: (sb) => num(sb.tokens.input) },
    { label: "tokens out", cell: (sb) => num(sb.tokens.output) },
    {
      label: "tokens total",
      cell: (sb) => (
        <>
          {num(sb.tokens.total)}
          {sb.tokens.cache_read > 0 ? (
            <span className="faint"> · {num(sb.tokens.cache_read)} cached</span>
          ) : null}
        </>
      ),
    },
    {
      label: "latency ms",
      hint: "avg / p95 / max",
      cell: (sb) =>
        sb.latency_ms.avg == null ? (
          <span className="score-na">—</span>
        ) : (
          <span className="mono" style={{ fontSize: "0.8rem" }}>
            {Math.round(sb.latency_ms.avg)} / {Math.round(sb.latency_ms.p95 ?? 0)} /{" "}
            {Math.round(sb.latency_ms.max ?? 0)}
          </span>
        ),
    },
    {
      label: "tool calls",
      cell: (sb) => (
        <>
          <div>{num(sb.tool_calls.total)}</div>
          {breakdown(sb.tool_calls.by_tool)}
        </>
      ),
    },
    {
      label: "skill calls",
      cell: (sb) => (
        <>
          <div>{num(sb.skill_calls.total)}</div>
          {breakdown(sb.skill_calls.by_skill)}
        </>
      ),
    },
    { label: "shell commands", cell: (sb) => num(sb.shell_commands) },
    { label: "file ops", cell: (sb) => num(sb.file_ops) },
    { label: "test runs", cell: (sb) => num(sb.test_executions) },
    {
      label: "errors",
      cell: (sb) =>
        sb.errors > 0 ? (
          <span style={{ color: "var(--red)" }}>{num(sb.errors)}</span>
        ) : (
          "0"
        ),
    },
    {
      label: "estimated cost",
      hint: selected ? `at ${selected}` : undefined,
      cell: (sb) => {
        if (!price) return <span className="score-na">—</span>;
        if (price.local) return <span className="mono">local — $0</span>;
        return (
          <span className="mono">{formatUsd(estimateCostUsd(price, sb.tokens))}</span>
        );
      },
    },
  ];

  const anyEval = current.sandboxes.some((sb) => sb.evaluator != null);

  return (
    <Card
      title="Analytics"
      className="card--flush"
      actions={
        <label className="row" style={{ gap: "0.4rem", fontSize: "0.8rem" }}>
          <span className="faint">Estimated cost at:</span>
          <select
            className="select"
            style={{ width: "auto", padding: "0.3rem 0.5rem", fontSize: "0.8rem" }}
            value={selected}
            onChange={(e) => setPriceModel(e.target.value)}
            aria-label="Estimated cost at"
          >
            {Object.entries(current.price_table).map(([name, p]) => (
              <option key={name} value={name}>
                {p.local
                  ? `${name} (local — $0)`
                  : `${name} — $${p.input_per_mtok_usd}/$${p.output_per_mtok_usd} per Mtok`}
              </option>
            ))}
          </select>
        </label>
      }
    >
      <div className="matrix-wrap">
        <table className="matrix">
          <thead>
            <tr>
              <th style={{ minWidth: "150px" }}>Metric</th>
              {current.sandboxes.map((sb) => (
                <th key={sb.sandbox_id}>
                  <div className="row" style={{ gap: "0.4rem" }}>
                    <span className="mono" title={sb.sandbox_id}>
                      {shortId(sb.sandbox_id)}
                    </span>
                    <Badge tone={statusTone(sb.status)}>{sb.status}</Badge>
                  </div>
                  <div className="dim-desc">{sb.label}</div>
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.label}>
                <td>
                  <div className="dim-name">{r.label}</div>
                  {r.hint ? <div className="dim-desc">{r.hint}</div> : null}
                </td>
                {current.sandboxes.map((sb) => (
                  <td key={sb.sandbox_id} className="num">
                    {r.cell(sb)}
                  </td>
                ))}
              </tr>
            ))}
            {anyEval ? (
              <tr>
                <td>
                  <div className="dim-name">evaluator</div>
                  <div className="dim-desc">
                    judge turns · tokens · est. cost · err
                  </div>
                </td>
                {current.sandboxes.map((sb) => (
                  <td key={sb.sandbox_id} className="num">
                    {sb.evaluator ? (
                      <span className="faint" style={{ fontSize: "0.8rem" }}>
                        {num(sb.evaluator.llm_turns)} ·{" "}
                        {num(sb.evaluator.tokens.total)} ·{" "}
                        {formatUsd(sb.evaluator.estimated_cost_usd)} ·{" "}
                        {num(sb.evaluator.errors)} err
                      </span>
                    ) : (
                      <span className="score-na">—</span>
                    )}
                  </td>
                ))}
              </tr>
            ) : null}
          </tbody>
        </table>
      </div>
      <p className="faint" style={{ fontSize: "0.78rem", padding: "0.6rem 0.7rem 0" }}>
        Metrics from the append-only event stream · evaluator (judge) traffic
        shares the sandbox&apos;s creds and is reported separately · cost is an
        estimate at list prices, not billing.
      </p>
    </Card>
  );
}
