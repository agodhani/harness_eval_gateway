"use client";

/**
 * PR32 — /compare?runs=id1,id2: two runs side by side.
 *
 * Fetches each run's detail (`getRun` — spec + rubric version), results
 * (`getRunResults` — score matrices) and analytics (`getRunAnalytics` —
 * the delta table) client-side; no new endpoint. Sections: spec diff
 * (models/configs/budgets/rubric version — differing rows highlighted),
 * per-run config × model score grids (same `buildScoreGrid` transform the
 * results-page heatmap uses), analytics totals with a Δ (B−A) column,
 * and the terminal-reason mix.
 *
 * With fewer/more than two ids the page degrades to a run picker —
 * two inputs against a datalist of recent runs.
 */
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  ApiError,
  dimensionOrder,
  getRun,
  getRunAnalytics,
  getRunResults,
  listRuns,
  type RunAnalytics,
  type RunOut,
  type RunResults,
  type RunSummary,
} from "@/lib/api";
import {
  analyticsTotals,
  delta,
  overallStats,
  parseRunIds,
  specDiffRows,
  terminalMix,
  type SpecDiffRow,
} from "@/lib/compareRuns";
import { formatDuration, formatTs, formatUsd, shortId, truncate } from "@/lib/format";
import { buildScoreGrid, cellDisplayValue, heatColor } from "@/lib/gridMatrix";
import { dimensionSpecMap } from "@/lib/scoreMath";
import {
  Badge,
  Card,
  EmptyState,
  ErrorNote,
  InfoNote,
  Loading,
  statusTone,
} from "@/components/ui";

const r1 = (v: number) => Math.round(v * 10) / 10;
const r2 = (v: number) => Math.round(v * 100) / 100;

function errMsg(err: unknown, fallback: string): string {
  if (err instanceof ApiError) return err.detail;
  if (err instanceof Error) return err.message;
  return fallback;
}

interface RunBundle {
  id: string;
  run: RunOut | null;
  results: RunResults | null;
  analytics: RunAnalytics | null;
  error: string | null;
}

async function loadBundle(id: string): Promise<RunBundle> {
  const [run, results, analytics] = await Promise.allSettled([
    getRun(id),
    getRunResults(id),
    getRunAnalytics(id),
  ]);
  return {
    id,
    run: run.status === "fulfilled" ? run.value : null,
    results: results.status === "fulfilled" ? results.value : null,
    analytics: analytics.status === "fulfilled" ? analytics.value : null,
    error:
      run.status === "rejected"
        ? errMsg(run.reason, "run not found")
        : results.status === "rejected" &&
            (results.reason as unknown) instanceof ApiError &&
            (results.reason as ApiError).status === 404
          ? "results unavailable — run not evaluated yet"
          : null,
  };
}

/** Picker — shown when `?runs=` doesn't carry exactly two ids. */
function RunPicker({ recent }: { recent: RunSummary[] }) {
  const router = useRouter();
  const [a, setA] = useState("");
  const [b, setB] = useState("");
  const canGo = a.trim() && b.trim() && a.trim() !== b.trim();
  return (
    <Card title="Pick two runs to compare">
      <div className="row" style={{ flexWrap: "wrap", gap: "0.6rem" }}>
        <input
          className="input input--mono"
          style={{ minWidth: "19rem" }}
          placeholder="run A — id"
          list="compare-run-ids"
          value={a}
          onChange={(e) => setA(e.target.value)}
        />
        <span className="faint">vs</span>
        <input
          className="input input--mono"
          style={{ minWidth: "19rem" }}
          placeholder="run B — id"
          list="compare-run-ids"
          value={b}
          onChange={(e) => setB(e.target.value)}
        />
        <button
          type="button"
          className="btn btn--sm btn--primary"
          disabled={!canGo}
          onClick={() =>
            router.push(
              `/compare?runs=${encodeURIComponent(a.trim())},${encodeURIComponent(b.trim())}`,
            )
          }
        >
          Compare →
        </button>
      </div>
      <datalist id="compare-run-ids">
        {recent.map((r) => (
          <option key={r.id} value={r.id}>
            {r.status} · {truncate(r.task_prompt, 60)}
          </option>
        ))}
      </datalist>
      {recent.length > 0 ? (
        <div className="table-wrap" style={{ marginTop: "0.8rem" }}>
          <table className="table">
            <thead>
              <tr>
                <th>Run</th>
                <th>Status</th>
                <th>Task</th>
                <th>Models</th>
                <th>Created</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {recent.map((r) => (
                <tr key={r.id}>
                  <td className="num">
                    <Link href={`/runs/${r.id}`} title={r.id}>
                      {shortId(r.id)}
                    </Link>
                  </td>
                  <td>
                    <Badge tone={statusTone(r.status)}>{r.status}</Badge>
                  </td>
                  <td className="faint">{truncate(r.task_prompt, 52)}</td>
                  <td className="mono faint">{(r.models ?? [r.model_name]).join(", ")}</td>
                  <td className="num faint">{formatTs(r.created_at)}</td>
                  <td className="num">
                    <button
                      type="button"
                      className="btn btn--sm btn--ghost"
                      onClick={() => (!a.trim() ? setA(r.id) : setB(r.id))}
                    >
                      + as {a.trim() ? "B" : "A"}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <EmptyState>
          No recent runs — <Link href="/submit">submit one first</Link>.
        </EmptyState>
      )}
    </Card>
  );
}

function RunHeaderCard({ label, bundle }: { label: string; bundle: RunBundle }) {
  const run = bundle.run;
  return (
    <div className="card">
      <div className="card__body">
        <div className="row" style={{ marginBottom: "0.3rem" }}>
          <Badge tone="violet">{label}</Badge>
          {run ? (
            <>
              <Link href={`/runs/${run.id}`} className="mono" title={run.id}>
                {shortId(run.id)}
              </Link>
              <Badge tone={statusTone(run.status)}>{run.status}</Badge>
              {run.preset_ref ? (
                <Link href={`/presets/${run.preset_ref}`} className="mono">
                  {run.preset_ref}
                </Link>
              ) : null}
            </>
          ) : (
            <span className="mono faint" title={bundle.id}>
              {shortId(bundle.id, 12)}…
            </span>
          )}
        </div>
        <div className="faint" style={{ fontSize: "0.78rem" }}>
          {run ? truncate(run.task_prompt, 110) : bundle.error ?? "could not load"}
        </div>
        {run ? (
          <div className="faint" style={{ fontSize: "0.7rem", marginTop: "0.3rem" }}>
            {formatTs(run.created_at)}
            {run.finished_at ? ` → ${formatTs(run.finished_at)}` : ""} ·{" "}
            {run.sandboxes.length} sandbox{run.sandboxes.length === 1 ? "" : "es"}
            {run.suite_ref ? ` · suite ${run.suite_ref}` : ""}
          </div>
        ) : null}
      </div>
    </div>
  );
}

function SpecDiffTable({ rows }: { rows: SpecDiffRow[] }) {
  return (
    <div className="table-wrap">
      <table className="table">
        <thead>
          <tr>
            <th>spec</th>
            <th>A</th>
            <th>B</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr
              key={r.label}
              className={r.differs ? "diff-row" : undefined}
              style={r.differs ? { background: "rgba(232, 180, 90, 0.07)" } : undefined}
            >
              <td className="faint" style={{ whiteSpace: "nowrap" }}>
                {r.label}
              </td>
              <td className="mono" title={r.titleA}>
                {r.a}
              </td>
              <td className="mono" title={r.titleB}>
                {r.b}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** One run's config × model score grid — the results heatmap's transform
 * without the winner card (cross-run winners aren't comparable anyway). */
function MiniScoreGrid({
  run,
  results,
}: {
  run: RunOut | null;
  results: RunResults | null;
}) {
  const specMap = useMemo(
    () => dimensionSpecMap(run?.rubric?.content?.dimensions ?? []),
    [run],
  );
  const dimKeys = useMemo(() => {
    const set = new Set<string>();
    for (const sb of results?.sandboxes ?? []) {
      for (const d of Object.keys(sb.dimensions ?? {})) set.add(d);
    }
    return dimensionOrder([...set]);
  }, [results]);
  const grid = useMemo(
    () =>
      buildScoreGrid(
        results?.sandboxes ?? [],
        results?.models,
        dimKeys,
        specMap,
      ),
    [results, dimKeys, specMap],
  );

  if (!results) {
    return <EmptyState>No results — run not evaluated yet.</EmptyState>;
  }
  if (grid.rows.length === 0) {
    return <EmptyState>No sandboxes on this run.</EmptyState>;
  }
  return (
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
                if (!cell) {
                  return (
                    <td key={m} className="grid-cell grid-cell--empty">
                      <span className="score-na">—</span>
                    </td>
                  );
                }
                const disp = cellDisplayValue(cell);
                const color = heatColor(disp.value);
                const sd =
                  disp.basis === "weighted" ? cell.sdWeighted : cell.sdOverall;
                return (
                  <td
                    key={m}
                    className="grid-cell"
                    style={
                      color
                        ? { background: `color-mix(in srgb, ${color} 34%, var(--surface))` }
                        : undefined
                    }
                    title={`${cell.sandboxes.length} sandbox${cell.sandboxes.length === 1 ? "" : "es"}`}
                  >
                    {disp.value != null ? (
                      <>
                        <span className="grid-cell__score">
                          {disp.basis === "weighted" ? "w " : ""}
                          {r1(disp.value)}
                        </span>
                        <span className="grid-cell__sub">
                          {cell.n > 1 && sd != null ? `±${r2(sd)} · ` : ""}×{cell.n}
                        </span>
                      </>
                    ) : (
                      <span className="score-na">—</span>
                    )}
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

interface DeltaRow {
  label: string;
  a: number | null;
  b: number | null;
  /** display formatter */
  fmt: (v: number) => string;
  /** "unpriced" marker for null cost cells instead of "—" */
  nullText?: string;
  /** lower-is-better metrics get their Δ tone flipped */
  lowerBetter?: boolean;
}

export default function CompareClient() {
  const router = useRouter();
  const params = useSearchParams();
  const ids = useMemo(() => parseRunIds(params.get("runs")), [params]);
  const pair = ids.length === 2 ? (ids as [string, string]) : null;

  const [bundles, setBundles] = useState<{ key: string; a: RunBundle; b: RunBundle } | null>(null);
  const [recent, setRecent] = useState<RunSummary[]>([]);

  useEffect(() => {
    if (!pair) {
      let cancelled = false;
      void (async () => {
        await Promise.resolve();
        try {
          const rs = await listRuns({ limit: 25 });
          if (!cancelled) setRecent(rs);
        } catch {
          /* picker stays usable without suggestions */
        }
      })();
      return () => {
        cancelled = true;
      };
    }
    return undefined;
  }, [pair]);

  useEffect(() => {
    if (!pair) return;
    const key = pair.join(",");
    let cancelled = false;
    void (async () => {
      await Promise.resolve();
      const [a, b] = await Promise.all([loadBundle(pair[0]), loadBundle(pair[1])]);
      if (!cancelled) setBundles({ key, a, b });
    })();
    return () => {
      cancelled = true;
    };
  }, [pair]);

  const data = bundles && bundles.key === pair?.join(",") ? bundles : null;

  const specRows = useMemo(() => {
    if (!data?.a.run || !data.b.run) return null;
    return specDiffRows(data.a.run, data.b.run);
  }, [data]);

  const totalsA = useMemo(() => analyticsTotals(data?.a.analytics), [data]);
  const totalsB = useMemo(() => analyticsTotals(data?.b.analytics), [data]);
  const mixA = useMemo(
    () => terminalMix(data?.a.analytics?.sandboxes ?? data?.a.results?.sandboxes ?? []),
    [data],
  );
  const mixB = useMemo(
    () => terminalMix(data?.b.analytics?.sandboxes ?? data?.b.results?.sandboxes ?? []),
    [data],
  );
  const scoreA = useMemo(() => overallStats(data?.a.results), [data]);
  const scoreB = useMemo(() => overallStats(data?.b.results), [data]);

  const deltaRows = useMemo<DeltaRow[]>(() => {
    const usd = (v: number) => formatUsd(v);
    const num = (v: number) => String(Math.round(v * 100) / 100);
    const ms = (v: number) => formatDuration(v);
    const sec = (v: number) => formatDuration(v * 1000);
    return [
      { label: "mean judge overall", a: scoreA.mean, b: scoreB.mean, fmt: (v) => v.toFixed(2) },
      { label: "scored cells", a: scoreA.n, b: scoreB.n, fmt: num },
      { label: "llm turns", a: totalsA.llmTurns, b: totalsB.llmTurns, fmt: num },
      { label: "tokens in", a: totalsA.tokensIn, b: totalsB.tokensIn, fmt: num },
      { label: "tokens out", a: totalsA.tokensOut, b: totalsB.tokensOut, fmt: num },
      { label: "tokens total", a: totalsA.tokensTotal, b: totalsB.tokensTotal, fmt: num, lowerBetter: true },
      { label: "wall clock", a: totalsA.wallClockSeconds, b: totalsB.wallClockSeconds, fmt: sec, lowerBetter: true },
      { label: "est. cost", a: totalsA.costUsd, b: totalsB.costUsd, fmt: usd, nullText: "unpriced", lowerBetter: true },
      { label: "avg tok/s", a: totalsA.avgTokensPerSec, b: totalsB.avgTokensPerSec, fmt: (v) => v.toFixed(1) },
      { label: "avg ttft", a: totalsA.ttftAvgMs, b: totalsB.ttftAvgMs, fmt: ms, lowerBetter: true },
      { label: "errors", a: totalsA.errors, b: totalsB.errors, fmt: num, lowerBetter: true },
      { label: "eval turns", a: totalsA.evalTurns, b: totalsB.evalTurns, fmt: num },
      { label: "eval cost", a: totalsA.evalCostUsd, b: totalsB.evalCostUsd, fmt: usd, nullText: "unpriced" },
      { label: "sandboxes", a: totalsA.sandboxCount, b: totalsB.sandboxCount, fmt: num },
    ];
  }, [totalsA, totalsB, scoreA, scoreB]);

  const mixRows = useMemo(() => {
    const reasons = new Set<string>();
    for (const s of mixA.slices) reasons.add(s.reason);
    for (const s of mixB.slices) reasons.add(s.reason);
    const amap = new Map(mixA.slices.map((s) => [s.reason, s]));
    const bmap = new Map(mixB.slices.map((s) => [s.reason, s]));
    return [...reasons]
      .sort((x, y) => (bmap.get(y)?.count ?? 0) + (amap.get(y)?.count ?? 0) - ((bmap.get(x)?.count ?? 0) + (amap.get(x)?.count ?? 0)))
      .map((reason) => ({ reason, a: amap.get(reason), b: bmap.get(reason) }));
  }, [mixA, mixB]);

  return (
    <main className="container">
      <div className="page-header">
        <div>
          <span className="kicker"><b>07</b> — cross-run compare</span>
          <h1 className="mono" style={{ fontSize: "1.15rem", marginBottom: "0.35rem" }}>
            Run vs run
          </h1>
          <p className="subtitle">
            spec diff · score grids · analytics deltas · terminal mix —
            share as <code>/compare?runs=idA,idB</code>
          </p>
        </div>
        <div className="row">
          {pair ? (
            <button
              type="button"
              className="btn btn--sm btn--ghost"
              onClick={() => router.push("/compare")}
            >
              ← pick other runs
            </button>
          ) : null}
          <Link href="/leaderboard" className="btn btn--sm btn--ghost">
            leaderboard →
          </Link>
        </div>
      </div>

      {!pair ? (
        <>
          <InfoNote>
            Compare needs exactly two run ids — pick them below (or pass
            <code> ?runs=idA,idB</code>).
          </InfoNote>
          <RunPicker recent={recent} />
        </>
      ) : !data ? (
        <Loading label="Loading both runs…" />
      ) : (
        <>
          <div className="split" style={{ marginBottom: "1rem" }}>
            <RunHeaderCard label="A" bundle={data.a} />
            <RunHeaderCard label="B" bundle={data.b} />
          </div>

          {(data.a.error || data.b.error) && (data.a.run || data.b.run) ? (
            <InfoNote>
              {data.a.error ? `A: ${data.a.error} ` : ""}
              {data.b.error ? `B: ${data.b.error}` : ""}
            </InfoNote>
          ) : null}
          {!data.a.run || !data.b.run ? (
            <ErrorNote title="A run failed to load">
              {data.a.error ? `A (${shortId(data.a.id, 12)}…): ${data.a.error}` : ""}
              {data.b.error ? ` B (${shortId(data.b.id, 12)}…): ${data.b.error}` : ""}
            </ErrorNote>
          ) : null}

          <div className="stack">
            {specRows ? (
              <Card title="Spec diff">
                <SpecDiffTable rows={specRows} />
              </Card>
            ) : null}

            <div className="split">
              <Card title={`Score grid — A ${shortId(data.a.id)}`}>
                <MiniScoreGrid run={data.a.run} results={data.a.results} />
              </Card>
              <Card title={`Score grid — B ${shortId(data.b.id)}`}>
                <MiniScoreGrid run={data.b.run} results={data.b.results} />
              </Card>
            </div>

            <Card title="Analytics deltas">
              <div className="table-wrap">
                <table className="table">
                  <thead>
                    <tr>
                      <th>metric</th>
                      <th>A</th>
                      <th>B</th>
                      <th>Δ (B−A)</th>
                    </tr>
                  </thead>
                  <tbody>
                    {deltaRows.map((r) => {
                      const d = delta(r.a, r.b);
                      const cell = (v: number | null) =>
                        v == null ? (
                          <span className="faint">{r.nullText ?? "—"}</span>
                        ) : (
                          r.fmt(v)
                        );
                      let dCell: React.ReactNode = <span className="faint">—</span>;
                      if (d != null) {
                        const positiveGood = !r.lowerBetter;
                        const cls =
                          d === 0
                            ? "faint"
                            : (d > 0) === positiveGood
                              ? "score-value score-value--good"
                              : "score-value score-value--bad";
                        const abs = Math.abs(d);
                        dCell = (
                          <span className={cls}>
                            {d > 0 ? "+" : "−"}
                            {r.fmt(abs)}
                          </span>
                        );
                      }
                      return (
                        <tr key={r.label}>
                          <td className="faint">{r.label}</td>
                          <td className="num">{cell(r.a)}</td>
                          <td className="num">{cell(r.b)}</td>
                          <td className="num">{dCell}</td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
              <p className="faint" style={{ marginTop: "0.6rem", fontSize: "0.72rem" }}>
                participant-phase totals only (judge traffic reported separately
                under eval rows) · green Δ = better for B · cost is the summed
                per-model estimate — <b>unpriced</b> when any contributing model
                has no price-table rate (never a partial total)
              </p>
            </Card>

            <Card title="Terminal-reason mix">
              <div className="table-wrap">
                <table className="table">
                  <thead>
                    <tr>
                      <th>reason</th>
                      <th>A ({mixA.total} cells)</th>
                      <th>B ({mixB.total} cells)</th>
                    </tr>
                  </thead>
                  <tbody>
                    {mixRows.map((r) => (
                      <tr key={r.reason}>
                        <td>
                          <Badge tone={statusTone(r.reason)}>{r.reason}</Badge>
                        </td>
                        <td className="num">
                          {r.a ? `${r.a.count} (${Math.round(r.a.share * 100)}%)` : "—"}
                        </td>
                        <td className="num">
                          {r.b ? `${r.b.count} (${Math.round(r.b.share * 100)}%)` : "—"}
                        </td>
                      </tr>
                    ))}
                    {mixA.pending + mixB.pending > 0 ? (
                      <tr>
                        <td className="faint">no terminal state yet</td>
                        <td className="num faint">{mixA.pending || "—"}</td>
                        <td className="num faint">{mixB.pending || "—"}</td>
                      </tr>
                    ) : null}
                    {mixRows.length === 0 && mixA.pending + mixB.pending === 0 ? (
                      <tr>
                        <td colSpan={3}>
                          <EmptyState>No sandboxes on either run.</EmptyState>
                        </td>
                      </tr>
                    ) : null}
                  </tbody>
                </table>
              </div>
            </Card>
          </div>
        </>
      )}
    </main>
  );
}
