"use client";

/**
 * F8 (list side) — all runs, newest first. Polls lightly while any run is
 * non-terminal so live statuses update without a refresh.
 */
import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import {
  ApiError,
  listRuns,
  RUN_STATUSES,
  TERMINAL_RUN_STATUSES,
  type RunSummary,
} from "@/lib/api";
import { formatTs, shortId, truncate } from "@/lib/format";
import { Badge, Card, EmptyState, ErrorNote, Loading, statusTone } from "@/components/ui";

const POLL_MS = 10_000;

export default function RunsPage() {
  const [runs, setRuns] = useState<RunSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [statusFilter, setStatusFilter] = useState<string>("");

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
  }, [statusFilter]);

  // Poll while any visible run is non-terminal (silent — keeps last data).
  useEffect(() => {
    const anyActive = (runs ?? []).some((r) => !TERMINAL_RUN_STATUSES.includes(r.status));
    if (!anyActive) return;
    const t = setInterval(() => {
      listRuns({ status: statusFilter || undefined, limit: 100 })
        .then((data) => setRuns(data))
        .catch(() => {
          /* transient — next tick retries */
        });
    }, POLL_MS);
    return () => clearInterval(t);
  }, [runs, statusFilter]);

  const filtered = useMemo(() => runs ?? [], [runs]);

  return (
    <main className="container">
      <div className="page-header">
        <div>
          <h1>Runs</h1>
          <p className="subtitle">Every evaluation submission, newest first.</p>
        </div>
        <div className="row">
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
            {statusFilter ? (
              <>No runs with status <code>{statusFilter}</code>.</>
            ) : (
              <>
                No runs yet.{" "}
                <Link href="/submit">Submit the first one</Link> — the form needs the
                control API, which may still be starting up.
              </>
            )}
          </EmptyState>
        ) : (
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th>Run</th>
                  <th>Task</th>
                  <th>Model</th>
                  <th>Status</th>
                  <th>Sandboxes</th>
                  <th>Created</th>
                  <th>Finished</th>
                </tr>
              </thead>
              <tbody>
                {filtered.map((r) => (
                  <tr key={r.id}>
                    <td className="num">
                      <Link href={`/runs/${r.id}`} title={r.id}>
                        {shortId(r.id)}
                      </Link>
                    </td>
                    <td style={{ maxWidth: "340px" }}>{truncate(r.task_prompt, 90)}</td>
                    <td className="num">{r.model_name}</td>
                    <td>
                      <Badge tone={statusTone(r.status)}>{r.status}</Badge>
                    </td>
                    <td className="num">{r.sandbox_count}</td>
                    <td className="num faint">{formatTs(r.created_at)}</td>
                    <td className="num faint">{formatTs(r.finished_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </main>
  );
}
