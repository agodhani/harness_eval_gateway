"use client";

/**
 * F8 (list side) — all runs, newest first. Polls lightly while any run is
 * non-terminal so live statuses update without a refresh.
 *
 * Client-side text search covers the task prompt, model(s), adapter(s), and
 * status. Mean overall scores come from ONE GET /api/analytics/runs call
 * (F13 trends — keyed by run_id) rather than N per-run results fetches.
 */
import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import {
  ApiError,
  getRunTrends,
  listPresets,
  listRuns,
  RUN_STATUSES,
  TERMINAL_RUN_STATUSES,
  type RunSummary,
  type TaskPreset,
} from "@/lib/api";
import { formatTs, shortId, truncate } from "@/lib/format";
import { Badge, Card, EmptyState, ErrorNote, Loading, statusTone } from "@/components/ui";

const POLL_MS = 10_000;

function scoreTone(score: number): "good" | "mid" | "bad" {
  if (score >= 8) return "good";
  if (score >= 5) return "mid";
  return "bad";
}

/** Case-insensitive substring match across the searchable fields of a run
 * row: task text, model(s), adapter(s) (when the payload carries them),
 * status, and the run id itself. */
function matchesQuery(r: RunSummary, q: string): boolean {
  const hay: string[] = [
    r.task_prompt,
    r.model_name,
    ...(r.models ?? []),
    r.status,
    r.id,
    // `adapters` isn't on RunSummary yet — optional field that lights up
    // when the backend exposes it.
    ...(r.adapters ?? []),
    // PR22 — preset provenance on the row.
    ...(r.preset_ref ? [r.preset_ref] : []),
  ];
  return hay.some((h) => h.toLowerCase().includes(q));
}

export default function RunsPage() {
  const [runs, setRuns] = useState<RunSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [statusFilter, setStatusFilter] = useState<string>("");
  const [presetFilter, setPresetFilter] = useState<string>("");
  const [query, setQuery] = useState("");
  // Preset library for the filter dropdown labels — silent on failure.
  const [presets, setPresets] = useState<TaskPreset[]>([]);
  // run_id → mean overall score (one trends call, not N results fetches);
  // null until the analytics endpoint responds / stays empty when it 404s.
  const [scores, setScores] = useState<Map<string, number | null>>(new Map());

  // Fetch on mount + whenever the status filter changes. Runs inside an
  // async IIFE so setState lands in async context (hooks lint rule).
  // Previous rows stay on screen while the new page loads (no flash).
  useEffect(() => {
    let cancelled = false;
    void (async () => {
      await Promise.resolve();
      try {
        const data = await listRuns({
          status: statusFilter || undefined,
          preset: presetFilter || undefined,
          limit: 100,
        });
        if (cancelled) return;
        setRuns(data);
        setError(null);
      } catch (err) {
        if (cancelled) return;
        setError(
          err instanceof ApiError
            ? err.status === 404
              ? "Runs API not available yet — the orchestrator backend is still being built."
              : err.detail
            : "Failed to load runs.",
        );
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [statusFilter, presetFilter]);

  // Preset dropdown — one list fetch; failure just means a slimmer select.
  useEffect(() => {
    let cancelled = false;
    void (async () => {
      await Promise.resolve();
      try {
        const data = await listPresets();
        if (!cancelled) setPresets(data);
      } catch {
        /* preset lane unavailable — the select falls back to run refs */
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  // Mean overall scores — a single trends call keyed by run_id. Failure is
  // silent: the column just shows "—" for every row.
  useEffect(() => {
    let cancelled = false;
    void (async () => {
      await Promise.resolve();
      try {
        const trends = await getRunTrends({ limit: 100 });
        if (cancelled) return;
        setScores(
          new Map(trends.points.map((p) => [p.run_id, p.mean_overall_score])),
        );
      } catch {
        /* analytics lane not landed — score column stays empty */
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  // Poll while any visible run is non-terminal (silent — keeps last data).
  useEffect(() => {
    const anyActive = (runs ?? []).some((r) => !TERMINAL_RUN_STATUSES.includes(r.status));
    if (!anyActive) return;
    const t = setInterval(() => {
      listRuns({
        status: statusFilter || undefined,
        preset: presetFilter || undefined,
        limit: 100,
      })
        .then((data) => setRuns(data))
        .catch(() => {
          /* transient — next tick retries */
        });
    }, POLL_MS);
    return () => clearInterval(t);
  }, [runs, statusFilter, presetFilter]);

  // Filter dropdown: library slugs first, then any preset_ref seen on runs
  // that the library doesn't know (deleted presets keep their provenance).
  const presetOptions = useMemo(() => {
    const slugs = new Set(presets.map((p) => p.slug));
    for (const r of runs ?? []) {
      if (r.preset_ref) slugs.add(r.preset_ref);
    }
    return [...slugs].sort();
  }, [presets, runs]);
  const presetTitle = (slug: string) =>
    presets.find((p) => p.slug === slug)?.title;

  const q = query.trim().toLowerCase();
  const filtered = useMemo(
    () => (runs ?? []).filter((r) => !q || matchesQuery(r, q)),
    [runs, q],
  );

  return (
    <main className="container">
      <div className="page-header">
        <div>
          <span className="kicker"><b>01</b> — run index</span>
          <h1>Runs</h1>
          <p className="subtitle">Every evaluation submission, newest first.</p>
        </div>
        <div className="row">
          <input
            className="input"
            style={{ width: "220px" }}
            type="search"
            placeholder="Filter: task, model, adapter, status…"
            aria-label="Filter runs"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
          <select
            className="select"
            style={{ width: "auto" }}
            value={statusFilter}
            onChange={(e) => setStatusFilter(e.target.value)}
            aria-label="Filter by status"
          >
            <option value="">All statuses</option>
            {RUN_STATUSES.map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </select>
          {presetOptions.length > 0 ? (
            <select
              className="select"
              style={{ width: "auto" }}
              value={presetFilter}
              onChange={(e) => setPresetFilter(e.target.value)}
              aria-label="Filter by preset"
              title="Filter runs by task preset"
            >
              <option value="">All presets</option>
              {presetOptions.map((slug) => (
                <option key={slug} value={slug}>
                  {presetTitle(slug) ?? slug}
                </option>
              ))}
            </select>
          ) : null}
          <Link href="/submit" className="btn btn--primary btn--sm">
            + New run
          </Link>
        </div>
      </div>

      {error ? <ErrorNote>{error}</ErrorNote> : null}

      <Card className="card--flush">
        {runs === null ? (
          error ? (
            <EmptyState>Run list unavailable — see the error above.</EmptyState>
          ) : (
            <Loading label="Loading runs…" />
          )
        ) : filtered.length === 0 ? (
          <EmptyState>
            {q ? (
              <>
                No runs match <code>{query.trim()}</code>
                {statusFilter ? (
                  <>
                    {" "}with status <code>{statusFilter}</code>
                  </>
                ) : null}
                {presetFilter ? (
                  <>
                    {" "}on preset <code>{presetFilter}</code>
                  </>
                ) : null}
                .{" "}
                <button
                  type="button"
                  className="btn btn--sm btn--ghost"
                  onClick={() => {
                    setQuery("");
                    setStatusFilter("");
                    setPresetFilter("");
                  }}
                >
                  clear filters
                </button>
              </>
            ) : statusFilter || presetFilter ? (
              <>
                No runs
                {statusFilter ? (
                  <>
                    {" "}with status <code>{statusFilter}</code>
                  </>
                ) : null}
                {presetFilter ? (
                  <>
                    {" "}on preset <code>{presetFilter}</code>
                  </>
                ) : null}
                .{" "}
                <button
                  type="button"
                  className="btn btn--sm btn--ghost"
                  onClick={() => {
                    setStatusFilter("");
                    setPresetFilter("");
                  }}
                >
                  clear filters
                </button>
              </>
            ) : (
              <div className="stack" style={{ alignItems: "center", gap: "0.6rem" }}>
                <span>
                  No runs yet — submit a task and two or more harness
                  configurations to compare them side by side.
                </span>
                <Link href="/submit" className="btn btn--primary btn--sm">
                  + Submit the first run
                </Link>
              </div>
            )}
          </EmptyState>
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
                  <th>Sandboxes</th>
                  <th>Score</th>
                  <th>Created</th>
                  <th>Finished</th>
                </tr>
              </thead>
              <tbody>
                {filtered.map((r) => {
                  const score = scores.get(r.id);
                  return (
                    <tr key={r.id}>
                      <td className="num">
                        <Link href={`/runs/${r.id}`} title={r.id}>
                          {shortId(r.id)}
                        </Link>
                      </td>
                      <td style={{ maxWidth: "340px" }}>{truncate(r.task_prompt, 90)}</td>
                      <td className="num">
                        {r.preset_ref ? (
                          <Link
                            href={`/presets/${r.preset_ref}`}
                            className="mono"
                            title={presetTitle(r.preset_ref) ?? r.preset_ref}
                          >
                            {r.preset_ref}
                          </Link>
                        ) : (
                          <span className="faint">—</span>
                        )}
                      </td>
                      <td className="num">
                        {r.model_name}
                        {(r.models?.length ?? 0) > 1 ? (
                          <span
                            className="faint"
                            title={`participants: ${(r.models ?? []).join(", ")}`}
                          >
                            {" "}
                            +{(r.models?.length ?? 1) - 1}
                          </span>
                        ) : null}
                      </td>
                      <td>
                        <Badge tone={statusTone(r.status)}>
                          {r.status === "queued" && r.queue_position != null
                            ? `queued · #${r.queue_position}`
                            : r.status}
                        </Badge>
                      </td>
                      <td className="num">{r.sandbox_count}</td>
                      <td className="num">
                        {typeof score === "number" ? (
                          <span
                            className={`score-value score-value--${scoreTone(score)}`}
                            title="mean overall score across sandboxes"
                            style={{ fontSize: "0.85rem" }}
                          >
                            {Math.round(score * 10) / 10}
                          </span>
                        ) : (
                          <span className="faint">—</span>
                        )}
                      </td>
                      <td className="num faint">{formatTs(r.created_at)}</td>
                      <td className="num faint">{formatTs(r.finished_at)}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </main>
  );
}
