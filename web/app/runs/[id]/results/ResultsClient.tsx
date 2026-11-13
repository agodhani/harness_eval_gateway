"use client";

/**
 * F8 — results matrix: sandboxes × rubric dimensions, score + expandable
 * rationale, overall score, artifact links, same-model-eval flag.
 *
 * Primary source: GET /api/runs/{id}/results. If that 404s (endpoint still
 * landing) but the run exists, falls back to per-sandbox
 * GET /api/sandboxes/{id}/scores and synthesizes the same matrix shape.
 * Polls while the run is non-terminal.
 */
import Link from "next/link";
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  ApiError,
  artifactDownloadUrl,
  CANONICAL_DIMENSIONS,
  getRun,
  getRunResults,
  getSandboxScores,
  TERMINAL_RUN_STATUSES,
  type DimensionScore,
  type RunOut,
  type RunResults,
  type SandboxResult,
} from "@/lib/api";
import { formatTs, shortId } from "@/lib/format";
import {
  Badge,
  Card,
  EmptyState,
  ErrorNote,
  InfoNote,
  Loading,
  statusTone,
  WarnNote,
} from "@/components/ui";
import AnalyticsSection from "./AnalyticsSection";
import PreviewControls from "./PreviewControls";

const POLL_MS = 5_000;

function errMsg(err: unknown, fallback: string): string {
  if (err instanceof ApiError) return err.detail;
  if (err instanceof Error) return err.message;
  return fallback;
}

function scoreTone(score: number): "good" | "mid" | "bad" {
  // MVP rubrics score 0–10; treat ≥8 strong, ≥5 middling, else weak.
  if (score >= 8) return "good";
  if (score >= 5) return "mid";
  return "bad";
}

/** Order dimension rows: canonical rubric dimensions first, then alpha. */
function dimensionOrder(keys: string[]): string[] {
  const canon = CANONICAL_DIMENSIONS.filter((d) => keys.includes(d));
  const rest = keys.filter((d) => !CANONICAL_DIMENSIONS.includes(d)).sort();
  return [...canon, ...rest];
}

/** Synthesize SandboxResult[] from per-sandbox ScoreOut rows. */
async function fetchResultsViaScores(run: RunOut): Promise<RunResults> {
  const sandboxes: SandboxResult[] = [];
  for (const sb of run.sandboxes) {
    const scores = await getSandboxScores(sb.id);
    const dimensions: Record<string, DimensionScore> = {};
    let overall: DimensionScore | null = null;
    let rubricId: string | null = null;
    for (const sc of scores) {
      rubricId = sc.rubric_id ?? rubricId;
      const entry = { score: sc.score, rationale: sc.rationale ?? "" };
      if (sc.dimension === "overall") overall = entry;
      else dimensions[sc.dimension] = entry;
    }
    sandboxes.push({
      sandbox_id: sb.id,
      harness_config: sb.harness_config ?? {},
      status: sb.status,
      terminal_reason: sb.terminal_reason,
      artifact_path: sb.artifact_path,
      rubric_id: rubricId,
      dimensions,
      overall,
    });
  }
  return {
    run_id: run.id,
    status: run.status,
    model_name: run.model_name,
    evaluator_model: run.config?.evaluator_model ?? null,
    same_model_eval: run.same_model_eval ?? null,
    sandboxes,
  };
}

export default function ResultsClient({ runId }: { runId: string }) {
  const [runData, setRunData] = useState<RunOut | null>(null);
  const [loadedRunId, setLoadedRunId] = useState<string | null>(null);
  const [results, setResults] = useState<RunResults | null>(null);
  const [loadedResultsId, setLoadedResultsId] = useState<string | null>(null);
  // Set once a refresh attempt for this runId completes (success or failure)
  // — drives the loading state without setState-in-effect.
  const [attemptedId, setAttemptedId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [unavailable, setUnavailable] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());

  // Scope loaded data to the current runId — survives client-side navigation
  // between /runs/A/results and /runs/B/results without a remount.
  const run = loadedRunId === runId ? runData : null;
  const currentResults = loadedResultsId === runId ? results : null;
  const loading = attemptedId !== runId;

  const refresh = useCallback(
    async (silent = true) => {
      try {
        // Run detail gives task/status context; results gives the matrix.
        const [r, res] = await Promise.allSettled([getRun(runId), getRunResults(runId)]);
        if (r.status === "fulfilled") {
          setRunData(r.value);
          setLoadedRunId(runId);
        } else if (!silent) {
          setError(errMsg(r.reason, "Run not found."));
        }

        if (res.status === "fulfilled") {
          setResults(res.value);
          setLoadedResultsId(runId);
          setUnavailable(null);
        } else {
          const resErr = res.reason;
          if (resErr instanceof ApiError && resErr.status === 404 && r.status === "fulfilled") {
            // Results endpoint not deployed yet — try per-sandbox scores.
            try {
              const synthesized = await fetchResultsViaScores(r.value);
              setResults(synthesized);
              setLoadedResultsId(runId);
              setUnavailable(null);
            } catch {
              setResults(null);
              setUnavailable(
                "Results endpoint unavailable (404) and per-sandbox scores could not be loaded either.",
              );
            }
          } else {
            setResults(null);
            setUnavailable(
              resErr instanceof ApiError
                ? `Results unavailable: ${resErr.detail}`
                : "Results unavailable — the evaluator backend may still be rolling out.",
            );
          }
        }
        setAttemptedId(runId);
        // Clear a stale error only when the run fetch itself succeeded —
        // a rejected run fetch sets `error` above and it should persist.
        if (r.status === "fulfilled") setError(null);
        return true;
      } catch (err) {
        setAttemptedId(runId);
        if (!silent) setError(errMsg(err, "Failed to load results."));
        return false;
      }
    },
    [runId],
  );

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      // Defer to a microtask: state updates then land in async context
      // (react-hooks/set-state-in-effect forbids sync setState in effects).
      await Promise.resolve();
      if (!cancelled) await refresh(false);
    })();
    return () => {
      cancelled = true;
    };
  }, [runId, refresh]);

  const status = currentResults?.status ?? run?.status ?? "";
  useEffect(() => {
    if (!status || TERMINAL_RUN_STATUSES.includes(status)) return;
    const t = setInterval(() => refresh(true), POLL_MS);
    return () => clearInterval(t);
  }, [status, refresh]);

  const sandboxes = useMemo(() => currentResults?.sandboxes ?? [], [currentResults]);

  const dimensionKeys = useMemo(() => {
    const set = new Set<string>();
    for (const sb of sandboxes) {
      for (const d of Object.keys(sb.dimensions ?? {})) set.add(d);
    }
    return dimensionOrder([...set]);
  }, [sandboxes]);

  function toggleCell(sbId: string, dim: string) {
    const key = `${sbId}::${dim}`;
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  }

  function scoreCell(
    sb: SandboxResult,
    dim: string,
    entry: DimensionScore | null | undefined,
  ): React.ReactNode {
    if (!entry) {
      return <span className="score-na">—</span>;
    }
    const key = `${sb.sandbox_id}::${dim}`;
    const open = expanded.has(key);
    const hasRationale = !!entry.rationale;
    return (
      <div>
        <span className={`score-value score-value--${scoreTone(entry.score)}`}>
          {entry.score}
        </span>
        {hasRationale ? (
          <div className="score-expand">
            <button
              type="button"
              className="btn btn--sm"
              onClick={() => toggleCell(sb.sandbox_id, dim)}
              title={open ? "hide rationale" : "show rationale"}
            >
              {open ? "▾ why" : "▸ why"}
            </button>
            {open ? <div className="rationale">{entry.rationale}</div> : null}
          </div>
        ) : null}
      </div>
    );
  }

  // ---- render ------------------------------------------------------------

  if (loading && !currentResults && !run) {
    return (
      <main className="container">
        <Loading label="Loading results…" />
      </main>
    );
  }

  if (error && !currentResults && !run) {
    return (
      <main className="container container--narrow">
        <ErrorNote title="Could not load results">{error}</ErrorNote>
        <p style={{ marginTop: "1rem" }}>
          <Link href={`/runs/${runId}`} className="btn btn--sm">
            ← Back to run
          </Link>
        </p>
      </main>
    );
  }

  const modelName = currentResults?.model_name ?? run?.model_name ?? "?";
  const evaluatorModel = currentResults?.evaluator_model ?? run?.config?.evaluator_model ?? null;
  const sameModel = currentResults?.same_model_eval ?? run?.same_model_eval ?? null;

  return (
    <main className="container">
      <div className="page-header">
        <div>
          <div className="row" style={{ marginBottom: "0.3rem" }}>
            <h1>Results</h1>
            <span className="mono faint" title={runId}>
              {shortId(runId, 13)}…
            </span>
            {status ? <Badge tone={statusTone(status)}>{status}</Badge> : null}
          </div>
          <p className="subtitle">
            <Link href={`/runs/${runId}`}>← run detail</Link>
            {" · "}
            <Link href={`/runs/${runId}/timeline`}>timeline</Link>
            {run?.task_prompt ? <> · {run.task_prompt.slice(0, 80)}{run.task_prompt.length > 80 ? "…" : ""}</> : null}
          </p>
        </div>
      </div>

      <div className="stack">
        {sameModel === true ? (
          <WarnNote>
            <strong>Same-model evaluation:</strong> the evaluator model equals the
            participant model (<code>{modelName}</code>). Scores are produced by the
            same weights being judged — treat rankings as indicative, not independent.
          </WarnNote>
        ) : evaluatorModel ? (
          <InfoNote>
            Evaluator: <code>{evaluatorModel}</code> · participants:{" "}
            <code>{modelName}</code>
          </InfoNote>
        ) : null}

        {error ? <ErrorNote>{error}</ErrorNote> : null}
        {unavailable ? <InfoNote>{unavailable}</InfoNote> : null}

        {sandboxes.length === 0 ? (
          <Card>
            <EmptyState>
              {currentResults
                ? "No sandboxes in this run yet — results appear after evaluation."
                : "No results available yet."}
            </EmptyState>
          </Card>
        ) : (
          <>
            <Card title="Score matrix" className="card--flush">
              <div className="matrix-wrap">
                <table className="matrix">
                  <thead>
                    <tr>
                      <th style={{ minWidth: "200px" }}>Dimension</th>
                      {sandboxes.map((sb) => (
                        <th key={sb.sandbox_id}>
                          <div className="row" style={{ gap: "0.4rem" }}>
                            <Link
                              href={`/runs/${runId}/timeline?sandbox=${sb.sandbox_id}`}
                              className="mono"
                              title={sb.sandbox_id}
                            >
                              {shortId(sb.sandbox_id)}
                            </Link>
                            <Badge tone={statusTone(sb.status)}>{sb.status}</Badge>
                          </div>
                          <div className="dim-desc">
                            {String(sb.harness_config?.adapter ?? sb.harness_config?.harness ?? "?")}
                            {Array.isArray(sb.harness_config?.skill_packs) &&
                            sb.harness_config.skill_packs.length > 0
                              ? ` · ${(sb.harness_config.skill_packs as string[]).join(", ")}`
                              : ""}
                          </div>
                          {sb.terminal_reason ? (
                            <div className="dim-desc">
                              <Badge tone={statusTone(sb.terminal_reason)}>{sb.terminal_reason}</Badge>
                            </div>
                          ) : null}
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    <tr className="row-overall">
                      <td>
                        <div className="dim-name">overall</div>
                        <div className="dim-desc">judge-reported aggregate</div>
                      </td>
                      {sandboxes.map((sb) => (
                        <td key={sb.sandbox_id} className="score-cell">
                          {sb.overall
                            ? scoreCell(sb, "overall", sb.overall)
                            : <span className="score-na">—</span>}
                        </td>
                      ))}
                    </tr>
                    {dimensionKeys.map((dim) => (
                      <tr key={dim}>
                        <td>
                          <div className="dim-name">{dim}</div>
                        </td>
                        {sandboxes.map((sb) => (
                          <td key={sb.sandbox_id} className="score-cell">
                            {scoreCell(sb, dim, sb.dimensions?.[dim] ?? null)}
                          </td>
                        ))}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </Card>

            <AnalyticsSection runId={runId} />

            <Card title="Artifacts">
              <div className="table-wrap">
                <table className="table">
                  <thead>
                    <tr>
                      <th>Sandbox</th>
                      <th>Artifact path</th>
                      <th>Downloads</th>
                      <th>Preview</th>
                    </tr>
                  </thead>
                  <tbody>
                    {sandboxes.map((sb) => {
                      const artifactIds =
                        sb.artifact_ids ??
                        (sb.artifacts ?? []).map((a) => a.id).filter(Boolean);
                      return (
                        <tr key={sb.sandbox_id}>
                          <td className="num">{shortId(sb.sandbox_id)}</td>
                          <td className="mono" style={{ fontSize: "0.78rem", wordBreak: "break-all" }}>
                            {sb.artifact_path ?? <span className="faint">—</span>}
                          </td>
                          <td>
                            {artifactIds.length > 0 ? (
                              <div className="chip-row">
                                {artifactIds.map((id) => (
                                  <a
                                    key={id}
                                    className="chip"
                                    style={{ textDecoration: "none" }}
                                    href={artifactDownloadUrl(id)}
                                    title={`Download artifact ${id}`}
                                  >
                                    ⬇ {shortId(id)}
                                  </a>
                                ))}
                              </div>
                            ) : (
                              <span className="faint" style={{ fontSize: "0.8rem" }}>
                                {sb.artifact_path
                                  ? "id not exposed by API yet"
                                  : "no artifact"}
                              </span>
                            )}
                          </td>
                          <td>
                            {sb.artifact_path ? (
                              <PreviewControls
                                runId={runId}
                                sandboxId={sb.sandbox_id}
                                previewable={sb.previewable ?? false}
                                candidates={sb.preview_candidates ?? []}
                              />
                            ) : (
                              <span className="faint" style={{ fontSize: "0.8rem" }}>
                                no artifact
                              </span>
                            )}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            </Card>

            <p className="faint" style={{ fontSize: "0.78rem" }}>
              Scores are written by the evaluator against the frozen rubric
              {run?.finished_at ? <> · run finished {formatTs(run.finished_at)}</> : null}.
            </p>
          </>
        )}
      </div>
    </main>
  );
}
