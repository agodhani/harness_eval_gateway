"use client";

/**
 * PR22 — /presets: the task preset library.
 *
 * Merged vendored + user-saved presets (GET /api/presets), browsable by
 * category chips + text filter. Each row links to the preset detail page
 * and carries its browse aggregates — runs count and the newest scored
 * run's mean overall — computed server-side.
 */
import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import { ApiError, listPresets, type TaskPreset } from "@/lib/api";
import { truncate } from "@/lib/format";
import { Badge, Card, EmptyState, ErrorNote, Loading } from "@/components/ui";

function scoreTone(score: number): "good" | "mid" | "bad" {
  if (score >= 8) return "good";
  if (score >= 5) return "mid";
  return "bad";
}

export default function PresetsPage() {
  const [presets, setPresets] = useState<TaskPreset[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [category, setCategory] = useState("");

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      await Promise.resolve();
      try {
        const data = await listPresets();
        if (cancelled) return;
        setPresets(data);
        setError(null);
      } catch (err) {
        if (cancelled) return;
        setError(
          err instanceof ApiError
            ? err.detail
            : "Failed to load presets — is the API running?",
        );
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  const categories = useMemo(
    () =>
      [...new Set((presets ?? []).map((p) => p.category).filter(Boolean))].sort(),
    [presets],
  );

  const q = query.trim().toLowerCase();
  const filtered = useMemo(
    () =>
      (presets ?? []).filter((p) => {
        if (category && p.category !== category) return false;
        if (!q) return true;
        return [p.title, p.slug, p.description, p.category, ...p.tags].some((h) =>
          h.toLowerCase().includes(q),
        );
      }),
    [presets, category, q],
  );

  return (
    <main className="container">
      <div className="page-header">
        <div>
          <span className="kicker"><b>03</b> — task library</span>
          <h1>Task presets</h1>
          <p className="subtitle">
            Reusable evaluation tasks — pick one on the submit form instead of a
            freeform prompt. Vendored presets ship with the repo; yours save to the
            shared library.
          </p>
        </div>
        <div className="row">
          <input
            className="input"
            style={{ width: "220px" }}
            type="search"
            placeholder="Filter: title, slug, tag…"
            aria-label="Filter presets"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
          <Link href="/submit" className="btn btn--primary btn--sm">
            + New run
          </Link>
        </div>
      </div>

      {error ? <ErrorNote>{error}</ErrorNote> : null}

      <Card className="card--flush">
        {categories.length > 0 ? (
          <div className="filter-bar" role="group" aria-label="Filter by category">
            <button
              type="button"
              className={`filter-chip${category === "" ? " is-active" : ""}`}
              onClick={() => setCategory("")}
            >
              all
            </button>
            {categories.map((c) => (
              <button
                key={c}
                type="button"
                className={`filter-chip${category === c ? " is-active" : ""}`}
                onClick={() => setCategory(category === c ? "" : c)}
              >
                {c}
              </button>
            ))}
          </div>
        ) : null}

        {presets === null ? (
          error ? (
            <EmptyState>Preset library unavailable — see the error above.</EmptyState>
          ) : (
            <Loading label="Loading presets…" />
          )
        ) : filtered.length === 0 ? (
          <EmptyState>
            {q || category ? (
              <>
                No presets match
                {q ? (
                  <>
                    {" "}<code>{query.trim()}</code>
                  </>
                ) : null}
                {category ? (
                  <>
                    {" "}in <code>{category}</code>
                  </>
                ) : null}
                .{" "}
                <button
                  type="button"
                  className="btn btn--sm btn--ghost"
                  onClick={() => {
                    setQuery("");
                    setCategory("");
                  }}
                >
                  clear filters
                </button>
              </>
            ) : (
              "No presets — the vendored library lives under task_presets/."
            )}
          </EmptyState>
        ) : (
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th>Preset</th>
                  <th>Category</th>
                  <th>Source</th>
                  <th>Runs</th>
                  <th>Last-run mean</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {filtered.map((p) => (
                  <tr key={p.slug}>
                    <td style={{ maxWidth: "380px" }}>
                      <Link href={`/presets/${p.slug}`} className="mono" title={p.slug}>
                        {p.title}
                      </Link>
                      <div className="faint" style={{ fontSize: "0.72rem" }}>
                        <code>{p.slug}</code>
                        {p.description ? ` · ${truncate(p.description, 80)}` : ""}
                      </div>
                    </td>
                    <td>
                      {p.category ? (
                        <Badge tone="outline">{p.category}</Badge>
                      ) : (
                        <span className="faint">—</span>
                      )}
                    </td>
                    <td>
                      <Badge tone={p.source === "vendored" ? "violet" : "blue"}>
                        {p.source}
                      </Badge>
                      {p.revision > 1 ? (
                        <span className="faint" style={{ fontSize: "0.7rem" }}>
                          {" "}v{p.revision}
                        </span>
                      ) : null}
                    </td>
                    <td className="num">{p.runs_count}</td>
                    <td className="num">
                      {typeof p.last_run_mean === "number" ? (
                        <span
                          className={`score-value score-value--${scoreTone(p.last_run_mean)}`}
                          title="newest scored run's mean overall"
                        >
                          {p.last_run_mean}
                        </span>
                      ) : (
                        <span className="faint">—</span>
                      )}
                    </td>
                    <td className="num" style={{ whiteSpace: "nowrap" }}>
                      <Link
                        href={`/submit?preset=${encodeURIComponent(p.slug)}`}
                        className="btn btn--sm btn--ghost"
                        title="Start a new run on this preset"
                      >
                        run →
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
