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
  matchPriceKey,
  resolvePricePick,
  type CellAnalytics,
  type ModelAnalytics,
  type ModelPrice,
  type PriceEntry,
  type RunAnalytics,
  type SandboxAnalytics,
} from "@/lib/api";
import { formatDuration, formatNum, formatUsd, shortId } from "@/lib/format";
import { fmtMeanSd } from "@/lib/trialGroups";
import { Badge, Card, InfoNote, statusTone } from "@/components/ui";
import AddRate from "@/components/AddRate";

function errMsg(err: unknown): string {
  if (err instanceof ApiError) return err.detail;
  if (err instanceof Error) return err.message;
  return "analytics endpoint unavailable";
}

/** A table column — one sandbox, one participant-model rollup (PR14), or
 * one config × model trial-group rollup (PR27). */
type Column = SandboxAnalytics | ModelAnalytics | CellAnalytics;

function isCellCol(c: Column): c is CellAnalytics {
  return "sandbox_count" in c && "cell" in c;
}

function isModelCol(c: Column): c is ModelAnalytics {
  return "sandbox_count" in c && !isCellCol(c);
}

function isSandboxCol(c: Column): c is SandboxAnalytics {
  return "sandbox_id" in c;
}

function colKey(c: Column): string {
  if (isCellCol(c)) return `cell:${c.cell}`;
  return isModelCol(c) ? `model:${c.model}` : c.sandbox_id;
}

interface Row {
  label: string;
  hint?: string;
  cell: (col: Column) => React.ReactNode;
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
  // null = auto: each column costed at its own model's rate (server
  // resolved); a price-table key reprices every column uniformly (what-if).
  const [priceModel, setPriceModel] = useState<string | null>(null);
  // PR28: rates added inline via "+ add rate" — the response's price_table
  // was fetched before they existed, so they're merged in client-side and
  // also persist server-side for the next fetch.
  const [extraPrices, setExtraPrices] = useState<Record<string, ModelPrice>>({});
  // PR14: "sandbox" = per-cell metrics; "model" = per-model rollup (only
  // offered when the run spans >1 participant model). PR27: "cell" = the
  // trial-group rollup (only when a run repeated cells — mean ± sd (n)).
  const [view, setView] = useState<"sandbox" | "model" | "cell">("sandbox");

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
  const multiModel = (current?.models?.length ?? 0) > 1;
  const hasTrialCells = (current?.cells ?? []).some(
    (c) => c.sandbox_count > 1,
  );
  const byModel = view === "model" && multiModel;
  const byCell = view === "cell" && hasTrialCells;
  const bySandbox = !byModel && !byCell;
  const columns: Column[] = byModel
    ? (current?.models ?? [])
    : byCell
      ? (current?.cells ?? [])
      : (current?.sandboxes ?? []);
  // PR28: the effective picker table — server price_table plus rates added
  // inline this session (they persist as model_prices rows; on refetch the
  // server's layered book returns them anyway).
  const priceTable: Record<string, ModelPrice> = {
    ...(current?.price_table ?? {}),
    ...extraPrices,
  };
  // Row globs in declaration order — the fetched list plus rates added
  // inline this session (a new row lands at the end of the book's scan).
  const priceGlobs = [
    ...(current?.price_globs ?? []),
    ...Object.keys(extraPrices),
  ];
  // An explicit pick resolves like the server's resolve_price_model — a
  // verbatim row glob (any case) selects that row; a model-like pick goes
  // through the row-glob scan so an earlier glob can shadow a static key.
  const resolvedPick = priceModel
    ? resolvePricePick(priceModel, priceTable, priceGlobs)
    : undefined;
  // Prefill the add-rate glob from a column whose model resolved no rate
  // — through the SAME matcher auto mode uses, so a just-saved glob is
  // what lifts the column out of "unpriced".
  const unpricedModel = columns.find(
    (c) => matchPriceKey(c.model, priceTable, priceGlobs) === undefined,
  )?.model;

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
    ...(byCell
      ? [
          {
            label: "score (overall)",
            hint: "mean ±sd (n) across the cell's trials",
            cell: (col: Column) =>
              isCellCol(col) && col.overall_mean != null ? (
                <span className="mono" style={{ fontSize: "0.8rem" }}>
                  {fmtMeanSd({
                    mean: col.overall_mean,
                    sd: col.overall_sd,
                    n: col.overall_n,
                  })}
                </span>
              ) : (
                <span className="score-na">—</span>
              ),
          },
        ]
      : []),
    {
      label: "wall clock",
      // By-model rollups sum member cells' event spans — parallel cells
      // exceed any real elapsed time, so say so rather than imply it.
      hint: byModel ? "sum of per-cell event spans" : undefined,
      cell: (sb) => formatDuration(sb.wall_clock_seconds * 1000) || "—",
    },
    { label: "LLM turns", cell: (sb) => formatNum(sb.llm_turns) },
    { label: "tokens in", cell: (sb) => formatNum(sb.tokens.input) },
    { label: "tokens out", cell: (sb) => formatNum(sb.tokens.output) },
    {
      label: "tokens total",
      cell: (sb) => (
        <>
          {formatNum(sb.tokens.total)}
          {sb.tokens.cache_read > 0 ? (
            <span className="faint"> · {formatNum(sb.tokens.cache_read)} cached</span>
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
          <div>{formatNum(sb.tool_calls.total)}</div>
          {breakdown(sb.tool_calls.by_tool)}
        </>
      ),
    },
    {
      label: "skill calls",
      cell: (sb) => (
        <>
          <div>{formatNum(sb.skill_calls.total)}</div>
          {breakdown(sb.skill_calls.by_skill)}
        </>
      ),
    },
    { label: "shell commands", cell: (sb) => formatNum(sb.shell_commands) },
    { label: "file ops", cell: (sb) => formatNum(sb.file_ops) },
    { label: "test runs", cell: (sb) => formatNum(sb.test_executions) },
    {
      label: "errors",
      cell: (sb) =>
        sb.errors > 0 ? (
          <span style={{ color: "var(--red)" }}>{formatNum(sb.errors)}</span>
        ) : (
          "0"
        ),
    },
    {
      label: "estimated cost",
      hint: priceModel
        ? `at ${resolvedPick ?? priceModel}`
        : "auto — per-model rates · unlisted = unpriced",
      cell: (col) => {
        // Explicit pick reprices every column uniformly (what-if); auto
        // resolves each column's model through the layered book itself
        // (row globs first — including rates added inline this session,
        // which the fetch-time col.price_model can't see — then the
        // static fallback). An unmatched key is an unlisted model (PR21)
        // — unpriced, never billed at a guessed rate. PR27 cell columns
        // price at their model's key through the same matcher (all trials
        // of a cell share one model).
        const rateKey = priceModel
          ? resolvedPick
          : matchPriceKey(col.model, priceTable, priceGlobs);
        const p = rateKey ? priceTable[rateKey] : undefined;
        if (!p) {
          return (
            <span
              className="faint"
              title={`${col.model} isn't in the price table — no honest rate`}
            >
              unpriced
            </span>
          );
        }
        if (p.local) return <span className="mono">local — $0</span>;
        return (
          <span className="mono">{formatUsd(estimateCostUsd(p, col.tokens))}</span>
        );
      },
    },
  ];

  const anyEval = columns.some((c) => c.evaluator != null);

  return (
    <Card
      title="Analytics"
      className="card--flush"
      actions={
        <div className="row" style={{ gap: "0.6rem", flexWrap: "wrap" }}>
          {multiModel ? (
            <div
              className="seg"
              role="tablist"
              aria-label="Analytics view"
            >
              <button
                type="button"
                role="tab"
                aria-selected={bySandbox}
                className={`seg__btn${bySandbox ? " seg__btn--active" : ""}`}
                onClick={() => setView("sandbox")}
              >
                per sandbox
              </button>
              <button
                type="button"
                role="tab"
                aria-selected={byModel}
                className={`seg__btn${byModel ? " seg__btn--active" : ""}`}
                onClick={() => setView("model")}
              >
                by model
              </button>
            </div>
          ) : null}
          {hasTrialCells ? (
            <div className="seg" role="tablist" aria-label="Trial rollup">
              <button
                type="button"
                role="tab"
                aria-selected={bySandbox}
                className={`seg__btn${bySandbox ? " seg__btn--active" : ""}`}
                onClick={() => setView("sandbox")}
              >
                per sandbox
              </button>
              <button
                type="button"
                role="tab"
                aria-selected={byCell}
                className={`seg__btn${byCell ? " seg__btn--active" : ""}`}
                onClick={() => setView("cell")}
              >
                by cell
              </button>
            </div>
          ) : null}
          <label className="row" style={{ gap: "0.4rem", fontSize: "0.8rem" }}>
            <span className="faint">Estimated cost at:</span>
            <select
              className="select"
              style={{ width: "auto", padding: "0.3rem 0.5rem", fontSize: "0.8rem" }}
              value={priceModel ?? ""}
              onChange={(e) => setPriceModel(e.target.value || null)}
              aria-label="Estimated cost at"
            >
              <option value="">
                auto — per-model rates
              </option>
              {Object.entries(priceTable).map(([name, p]) => (
                <option key={name} value={name}>
                  {p.local
                    ? `${name} (local — $0)`
                    : `${name} — $${p.input_per_mtok_usd}/$${p.output_per_mtok_usd} per Mtok`}
                </option>
              ))}
            </select>
          </label>
          <AddRate
            suggestGlob={unpricedModel ?? ""}
            onAdded={(entry: PriceEntry) => {
              setExtraPrices((m) => ({
                ...m,
                [entry.model_glob]: {
                  input_per_mtok_usd: entry.input_per_mtok_usd,
                  output_per_mtok_usd: entry.output_per_mtok_usd,
                  cache_read_per_mtok_usd: entry.cache_read_per_mtok_usd,
                  local: entry.local,
                },
              }));
              // select the new rate — the what-if reprice is why it exists
              setPriceModel(entry.model_glob);
            }}
          />
        </div>
      }
    >
      <div className="matrix-wrap">
        <table className="matrix">
          <thead>
            <tr>
              <th style={{ minWidth: "150px" }}>Metric</th>
              {columns.map((col) =>
                isCellCol(col) ? (
                  <th key={colKey(col)}>
                    <div className="row" style={{ gap: "0.4rem" }}>
                      <span className="mono">{col.model}</span>
                      <Badge tone="outline">
                        {col.sandbox_count} trial{col.sandbox_count === 1 ? "" : "s"}
                      </Badge>
                    </div>
                    <div className="dim-desc">{col.label}</div>
                  </th>
                ) : isModelCol(col) ? (
                  <th key={colKey(col)}>
                    <div className="row" style={{ gap: "0.4rem" }}>
                      <span className="mono">{col.model}</span>
                      <Badge tone="outline">
                        {col.sandbox_count} sandbox{col.sandbox_count === 1 ? "" : "es"}
                      </Badge>
                    </div>
                  </th>
                ) : (
                  <th key={colKey(col)}>
                    <div className="row" style={{ gap: "0.4rem" }}>
                      <span className="mono" title={col.sandbox_id}>
                        {shortId(col.sandbox_id)}
                      </span>
                      {col.trial_index ? (
                        <Badge tone="outline" title="trial index within its cell">
                          t{col.trial_index}
                        </Badge>
                      ) : null}
                      <Badge tone={statusTone(col.status)}>{col.status}</Badge>
                    </div>
                    {col.name ? (
                      <div className="dim-name" title={col.name}>
                        {col.name}
                      </div>
                    ) : null}
                    <div className="dim-desc">{col.label}</div>
                  </th>
                ),
              )}
            </tr>
          </thead>
          <tbody>
            {byModel ? (
              <tr>
                <td>
                  <div className="dim-name">sandboxes</div>
                  <div className="dim-desc">matrix cells on this model</div>
                </td>
                {columns.map((col) => (
                  <td key={colKey(col)} className="num">
                    {isModelCol(col) ? col.sandbox_count : "—"}
                  </td>
                ))}
              </tr>
            ) : byCell ? (
              <tr>
                <td>
                  <div className="dim-name">trials</div>
                  <div className="dim-desc">repeats of this cell</div>
                </td>
                {columns.map((col) => (
                  <td key={colKey(col)} className="num">
                    {isCellCol(col) ? col.sandbox_count : "—"}
                  </td>
                ))}
              </tr>
            ) : (
              <tr>
                <td>
                  <div className="dim-name">terminal state</div>
                </td>
                {columns.map((col) => (
                  <td key={colKey(col)} className="num">
                    {isSandboxCol(col) && col.terminal_reason ? (
                      <Badge tone={statusTone(col.terminal_reason)}>
                        {col.terminal_reason}
                      </Badge>
                    ) : (
                      <span className="score-na">—</span>
                    )}
                  </td>
                ))}
              </tr>
            )}
            {rows.map((r) => (
              <tr key={r.label}>
                <td>
                  <div className="dim-name">{r.label}</div>
                  {r.hint ? <div className="dim-desc">{r.hint}</div> : null}
                </td>
                {columns.map((col) => (
                  <td key={colKey(col)} className="num">
                    {r.cell(col)}
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
                {columns.map((col) => (
                  <td key={colKey(col)} className="num">
                    {col.evaluator ? (
                      <span className="faint" style={{ fontSize: "0.8rem" }}>
                        {formatNum(col.evaluator.llm_turns)} ·{" "}
                        {formatNum(col.evaluator.tokens.total)} ·{" "}
                        {col.evaluator.estimated_cost_usd == null
                          ? "unpriced"
                          : formatUsd(col.evaluator.estimated_cost_usd)}{" "}
                        · {formatNum(col.evaluator.errors)} err
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
