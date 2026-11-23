"use client";

/**
 * PR26 — /suites: the benchmark suite library.
 *
 * Vendored suites (benchmark_suites/<slug>/suite.yaml) — each fans out to
 * one run per task via POST /api/suites/{slug}/run. Rows show task/check
 * counts and how many batches have been launched.
 */
import Link from "next/link";
import { useEffect, useState } from "react";
import { ApiError, listSuites, type Suite } from "@/lib/api";
import { truncate } from "@/lib/format";
import { Badge, Card, EmptyState, ErrorNote, Loading } from "@/components/ui";

export default function SuitesPage() {
  const [suites, setSuites] = useState<Suite[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      await Promise.resolve();
      try {
        const data = await listSuites();
        if (cancelled) return;
        setSuites(data);
        setError(null);
      } catch (err) {
        if (cancelled) return;
        setError(
          err instanceof ApiError
            ? err.detail
            : "Failed to load suites — is the API running?",
        );
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <main className="container">
      <div className="page-header">
        <div>
          <span className="kicker"><b>05</b> — benchmark suites</span>
          <h1>Benchmark suites</h1>
          <p className="subtitle">
            Vendored task batches with deterministic checkers — one run per
            suite task, objective pass/fail measured beside the judge scores.
            Suite files live under <code>benchmark_suites/</code>.
          </p>
        </div>
        <div className="row">
          <Link href="/submit" className="btn btn--primary btn--sm">
            + New run
          </Link>
        </div>
      </div>

      {error ? <ErrorNote>{error}</ErrorNote> : null}

      <Card className="card--flush">
        {suites === null ? (
          error ? (
            <EmptyState>Suite library unavailable — see the error above.</EmptyState>
          ) : (
            <Loading label="Loading suites…" />
          )
        ) : suites.length === 0 ? (
          <EmptyState>
            No suites — the vendored library lives under{" "}
            <code>benchmark_suites/</code>.
          </EmptyState>
        ) : (
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th>Suite</th>
                  <th>Version</th>
                  <th>Tasks</th>
                  <th>Checks</th>
                  <th>Batches</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {suites.map((s) => (
                  <tr key={s.slug}>
                    <td style={{ maxWidth: "380px" }}>
                      <Link href={`/suites/${s.slug}`} className="mono" title={s.slug}>
                        {s.title}
                      </Link>
                      <div className="faint" style={{ fontSize: "0.72rem" }}>
                        <code>{s.slug}</code>
                        {s.description ? ` · ${truncate(s.description, 80)}` : ""}
                      </div>
                    </td>
                    <td className="num">v{s.version}</td>
                    <td className="num">{s.task_count}</td>
                    <td className="num">
                      {s.checks_count > 0 ? (
                        <Badge tone="teal" title="Deterministic checkers declared across tasks">
                          {s.checks_count}
                        </Badge>
                      ) : (
                        <span className="faint">0</span>
                      )}
                    </td>
                    <td className="num">{s.suite_runs_count}</td>
                    <td className="num" style={{ whiteSpace: "nowrap" }}>
                      <Link
                        href={`/suites/${s.slug}`}
                        className="btn btn--sm btn--ghost"
                        title="Suite detail — tasks, checks, run the batch"
                      >
                        open →
                      </Link>
                    </td>
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
