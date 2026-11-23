"use client";

/**
 * F8 — results matrix: sandboxes × rubric dimensions, score + expandable
 * rationale, overall score, artifact links, same-model-eval flag.
 *
 * Primary source: GET /api/runs/{id}/results. If that 404s (endpoint still
 * landing) but the run exists, falls back to per-sandbox
 * GET /api/sandboxes/{id}/scores and synthesizes the same matrix shape.
 * Polls while the run is non-terminal.
 *
 * PR5 adds eval iteration: a rubric-version selector (?rubric_version=) plus
 * re-score / edit-rubric controls ({@link RescoreControls}).
 */
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  ApiError,
  artifactDownloadUrl,
  dimensionOrder,
  getRun,
  getRunResults,
  getSandboxScores,
  harnessConfigLabel,
  listEvents,
  runModels,
  TERMINAL_RUN_STATUSES,
  type CheckerResult,
  type DimensionScore,
  type EventOut,
  type RunOut,
  type RunResults,
  type SandboxResult,
} from "@/lib/api";
import { formatTs, shortId } from "@/lib/format";
import { specFromRun, stageResubmit } from "@/lib/resubmit";
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
import RescoreControls from "./RescoreControls";

const POLL_MS = 5_000;

function errMsg(err: unknown, fallback: string): string {
  if (err instanceof ApiError) return err.detail;
  if (err instanceof Error) return err.message;
  return fallback;
}

function isTerminal(status: string): boolean {
  return TERMINAL_RUN_STATUSES.includes(status) || status === "cancelled";
}

/** `eval_scored` checkpoint payload → judge pipeline mode. Backend modes:
 * "agentic" (eval-sandbox miniloop wrote scores.json) vs "direct_fallback"
 * (single control-plane completion). */
function evalScoredMode(ev: EventOut): string | null {
  const p = ev.payload ?? {};
  if (p.phase !== "eval_scored" || typeof p.mode !== "string") return null;
  return p.mode;
}

function evalScoredVersion(ev: EventOut): number | null {
  const v = (ev.payload ?? {}).rubric_version;
  return typeof v === "number" ? v : null;
}

/** Badge label/tone for a judge mode string. */
function modeBadge(mode: string): { label: string; tone: "teal" | "amber" | "gray"; title: string } {
  switch (mode) {
    case "agentic":
      return {
        label: "agentic",
        tone: "teal",
        title: "Scored by the agentic judge — a miniloop sandbox read the workspace and wrote scores.json",
      };
    case "direct_fallback":
      return {
        label: "fallback",
        tone: "amber",
        title: "Scored by the direct fallback — a single control-plane LLM call (the eval sandbox failed or was disabled)",
      };
    default:
      return { label: mode, tone: "gray", title: `Judge mode: ${mode}` };
  }
}

function scoreTone(score: number): "good" | "mid" | "bad" {
  // MVP rubrics score 0–10; treat ≥8 strong, ≥5 middling, else weak.
  if (score >= 8) return "good";
  if (score >= 5) return "mid";
  return "bad";
}

/** PR26 — deterministic suite checker status → badge tone + glyph. */
function checkTone(
  status: string,
): { tone: "green" | "red" | "amber" | "gray"; glyph: string } {
  switch (status) {
    case "pass":
      return { tone: "green", glyph: "✓" };
    case "fail":
      return { tone: "red", glyph: "✗" };
    case "skipped":
      return { tone: "gray", glyph: "⊘" };
    default:
      return { tone: "amber", glyph: "!" };
  }
}

/** One checker_results row — ✓/✗/⊘/! glyph + check type + detail. */
function CheckerBadge({ c }: { c: CheckerResult }) {
  const { tone, glyph } = checkTone(c.status);
  return (
    <Badge
      tone={tone}
      title={`${c.check} ${JSON.stringify(c.spec)}${c.detail ? ` — ${c.detail}` : ""}`}
    >
      {glyph} {c.check}
      {c.detail ? (
        <span className="faint" style={{ marginLeft: "0.3rem" }}>
          {c.detail.length > 60 ? `${c.detail.slice(0, 60)}…` : c.detail}
        </span>
      ) : null}
    </Badge>
  );
}

/** PR26 — per-sandbox deterministic checker card row (suite-fanned runs). */
function CheckerRow({ sb }: { sb: SandboxResult }) {
  const results = sb.checker_results ?? [];
  const s = sb.suite ?? null;
  if (results.length === 0 && !s) return null;
  return (
    <tr>
      <td className="num" title={sb.sandbox_id}>
        {shortId(sb.sandbox_id)}
        {sb.name ? (
          <div className="faint" style={{ fontSize: "0.7rem" }}>
            {sb.name}
          </div>
        ) : null}
      </td>
      <td className="num">
        {s ? (
          <>
            <Badge tone={s.passed === s.total && s.total > 0 ? "green" : s.failed > 0 ? "red" : "gray"}>
              {s.passed}/{s.total}
            </Badge>
            <div className="faint" style={{ fontSize: "0.68rem" }}>
              {s.pass_rate != null ? `${Math.round(s.pass_rate * 100)}% · ` : ""}
              ⊘{s.skipped} !{s.errors}
            </div>
          </>
        ) : (
          <span className="faint">—</span>
        )}
      </td>
      <td className="num">
        {s?.suite_pass != null ? (
          <span
            className={`score-value score-value--${scoreTone(s.suite_pass)}`}
            title="suite_pass score row — checker pass rate on the 0–10 scale"
          >
            {s.suite_pass}
          </span>
        ) : (
          <span className="score-na">—</span>
        )}
      </td>
      <td>
        <div className="chip-row">
          {results.map((c, i) => (
            <CheckerBadge key={i} c={c} />
          ))}
        </div>
      </td>
    </tr>
  );
}

/** Synthesize SandboxResult[] from per-sandbox ScoreOut rows. */
async function fetchResultsViaScores(run: RunOut): Promise<RunResults> {
  const sandboxes: SandboxResult[] = [];
  for (const sb of run.sandboxes) {
    const scores = await getSandboxScores(sb.id);
    const dimensions: Record<string, DimensionScore> = {};
    let overall: DimensionScore | null = null;
    let rubricId: string | null = null;
    let rubricVersion: number | null = null;
    for (const sc of scores) {
      rubricId = sc.rubric_id ?? rubricId;
      rubricVersion = sc.rubric_version ?? rubricVersion;
      const entry = { score: sc.score, rationale: sc.rationale ?? "" };
      if (sc.dimension === "overall") overall = entry;
      else dimensions[sc.dimension] = entry;
    }
    sandboxes.push({
      sandbox_id: sb.id,
      harness_config: sb.harness_config ?? {},
      name: sb.name ?? null,
      model:
        String(sb.harness_config?.model ?? "").trim() || run.model_name,
      status: sb.status,
      terminal_reason: sb.terminal_reason,
      artifact_path: sb.artifact_path,
      rubric_id: rubricId,
      rubric_version: rubricVersion,
      dimensions,
      overall,
    });
  }
  return {
    run_id: run.id,
    status: run.status,
    model_name: run.model_name,
    models: runModels(
      run.model_name,
      run.config?.models,
      run.config?.harness_configs,
    ),
    evaluator_model: run.config?.evaluator_model ?? null,
    same_model_eval: run.same_model_eval ?? null,
    sandboxes,
  };
}

export default function ResultsClient({ runId }: { runId: string }) {
  const router = useRouter();
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
  // PR5: pinned rubric version for the score matrix — null = newest-wins.
  // Scoped to runId like the other loaded state (no remount between runs).
  const [versionSel, setVersionSel] = useState<{ id: string; v: number | null }>({
    id: runId,
    v: null,
  });
  const selectedVersion = versionSel.id === runId ? versionSel.v : null;
  // `eval_scored` checkpoint events — source of the per-sandbox judge-mode
  // badge when the results payload doesn't carry `eval_mode` yet.
  const [scoredEvents, setScoredEvents] = useState<EventOut[]>([]);

  // Scope loaded data to the current runId — survives client-side navigation
  // between /runs/A/results and /runs/B/results without a remount.
  const run = loadedRunId === runId ? runData : null;
  const currentResults = loadedResultsId === runId ? results : null;
  const loading = attemptedId !== runId;

  const refresh = useCallback(
    async (silent = true, version: number | null = selectedVersion) => {
      try {
        // Run detail gives task/status context; results gives the matrix.
        const [r, res] = await Promise.allSettled([
          getRun(runId),
          getRunResults(runId, version ?? undefined),
        ]);
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
    [runId, selectedVersion],
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

  // Judge-mode badges: pull checkpoint events once results exist; re-pull
  // while evaluating so `eval_scored` lands without a reload. One query
  // total (type=checkpoint is a small stream), not per-sandbox.
  useEffect(() => {
    if (!["evaluating", "done", "failed"].includes(status)) return;
    let cancelled = false;
    const load = async () => {
      try {
        const evs = await listEvents(runId, { type: "checkpoint", limit: 1000 });
        if (!cancelled) setScoredEvents(evs.filter((e) => evalScoredMode(e)));
      } catch {
        /* events endpoint down — mode badges just don't render */
      }
    };
    void load();
    if (status !== "evaluating") {
      return () => {
        cancelled = true;
      };
    }
    const t = setInterval(load, POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(t);
    };
  }, [runId, status]);

  /** sandbox_id → judge mode. `sb.eval_mode` (new results field) wins;
   * else the latest `eval_scored` checkpoint — preferring the pinned rubric
   * version's pass when a version is selected. */
  const modeBySandbox = useMemo(() => {
    const pinned =
      selectedVersion != null
        ? scoredEvents.filter((e) => evalScoredVersion(e) === selectedVersion)
        : scoredEvents;
    const pool = pinned.length ? pinned : scoredEvents;
    const m = new Map<string, string>();
    for (const e of pool) {
      // ascending event id → the latest scoring pass wins per sandbox
      if (e.sandbox_id) m.set(e.sandbox_id, evalScoredMode(e)!);
    }
    return m;
  }, [scoredEvents, selectedVersion]);

  /** Resubmit — stage this run's spec and open /submit prefilled. */
  function resubmit() {
    if (!run) return;
    stageResubmit(specFromRun(run));
    router.push("/submit");
  }

  function selectVersion(v: number | null) {
    setVersionSel({ id: runId, v });
    // refresh's identity changes with selectedVersion → the mount effect
    // refetches the matrix at the new pin.
  }

  const sandboxes = useMemo(() => currentResults?.sandboxes ?? [], [currentResults]);

  const dimensionKeys = useMemo(() => {
    const set = new Set<string>();
    for (const sb of sandboxes) {
      for (const d of Object.keys(sb.dimensions ?? {})) set.add(d);
    }
    return dimensionOrder([...set]);
  }, [sandboxes]);

  /** F13 — per-sandbox Σ of dimension scores for the matrix totals row.
   * Distinct from `overall` (judge-reported aggregate); the max gets the
   * "leader" highlight so the winner is visible at a glance. */
  const dimTotals = useMemo(() => {
    const m = new Map<string, { sum: number; count: number }>();
    for (const sb of sandboxes) {
      let sum = 0;
      let count = 0;
      for (const d of dimensionKeys) {
        const e = sb.dimensions?.[d];
        if (e) {
          sum += e.score;
          count++;
        }
      }
      m.set(sb.sandbox_id, { sum, count });
    }
    return m;
  }, [sandboxes, dimensionKeys]);
  const maxTotal = useMemo(
    () =>
      Math.max(
        0,
        ...[...dimTotals.values()].filter((t) => t.count > 0).map((t) => t.sum),
      ),
    [dimTotals],
  );

  /** PR14 — group sandboxes by participant model for the compare-by-model
   * card: sandbox count, mean judge `overall`, mean Σ-dimensions total. */
  const modelGroups = useMemo(() => {
    const map = new Map<string, SandboxResult[]>();
    for (const sb of sandboxes) {
      const m = sb.model || run?.model_name || "?";
      const list = map.get(m);
      if (list) list.push(sb);
      else map.set(m, [sb]);
    }
    return [...map.entries()].map(([model, sbs]) => {
      const overalls = sbs
        .map((s) => s.overall?.score)
        .filter((v): v is number => typeof v === "number");
      const totals = sbs
        .map((s) => dimTotals.get(s.sandbox_id))
        .filter((t): t is { sum: number; count: number } => !!t && t.count > 0);
      return {
        model,
        count: sbs.length,
        meanOverall:
          overalls.length > 0
            ? overalls.reduce((a, b) => a + b, 0) / overalls.length
            : null,
        meanTotal:
          totals.length > 0
            ? totals.reduce((a, t) => a + t.sum, 0) / totals.length
            : null,
      };
    });
  }, [sandboxes, dimTotals, run]);

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
  // PR14: full participant-model set for the header note.
  const participantModels =
    currentResults?.models ??
    runModels(modelName, run?.config?.models, run?.config?.harness_configs);

  return (
    <main className="container">
      <div className="page-header">
        <div>
          <span className="kicker"><b>04</b> — score matrix · artifacts</span>
          <div className="row" style={{ marginBottom: "0.3rem" }}>
            <h1>Results</h1>
            <span className="mono faint" title={runId}>
              {shortId(runId, 13)}…
            </span>
            {status ? <Badge tone={statusTone(status)}>{status}</Badge> : null}
            {run?.suite_ref ? (
              <Badge tone="violet" title={`Suite task #${run.suite_index ?? "?"} in ${run.suite_ref}`}>
                suite {run.suite_ref}
              </Badge>
            ) : null}
          </div>
          <p className="subtitle">
            <Link href={`/runs/${runId}`}>← run detail</Link>
            {" · "}
            <Link href={`/runs/${runId}/timeline`}>timeline</Link>
            {" · "}
            <Link href={`/runs/${runId}/analysis`}>analysis</Link>
            {run?.suite_run_id ? (
              <>
                {" · "}
                <Link href={`/suite-runs/${run.suite_run_id}`}>suite batch</Link>
              </>
            ) : null}
            {run?.task_prompt ? <> · {run.task_prompt.slice(0, 80)}{run.task_prompt.length > 80 ? "…" : ""}</> : null}
          </p>
        </div>
        {run && isTerminal(status) ? (
          <div className="row">
            <button
              type="button"
              className="btn btn--sm"
              onClick={resubmit}
              title="Open the submit form prefilled with this run's spec"
            >
              ↻ Resubmit
            </button>
          </div>
        ) : null}
      </div>

      <div className="stack">
        {sameModel === true ? (
          <WarnNote>
            <strong>Same-model evaluation:</strong> the evaluator model equals a
            participant model (<code>{evaluatorModel ?? modelName}</code>). Scores
            are produced by the same weights being judged — treat rankings as
            indicative, not independent.
          </WarnNote>
        ) : evaluatorModel ? (
          <InfoNote>
            Evaluator: <code>{evaluatorModel}</code> · participants:{" "}
            <code>{participantModels.join(", ") || modelName}</code>
          </InfoNote>
        ) : null}

        {error ? <ErrorNote>{error}</ErrorNote> : null}
        {unavailable ? <InfoNote>{unavailable}</InfoNote> : null}

        {currentResults ? (
          <Card title="Re-score &amp; rubric versions">
            <RescoreControls
              runId={runId}
              results={currentResults}
              latestRubric={run?.rubric ?? null}
              selectedVersion={selectedVersion}
              onSelectVersion={selectVersion}
              onChanged={() => void refresh(true)}
            />
          </Card>
        ) : null}

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
                          {sb.name ? (
                            <div className="dim-name" title={sb.name}>
                              {sb.name}
                            </div>
                          ) : null}
                          <div className="dim-desc">
                            {harnessConfigLabel(sb.harness_config)}
                          </div>
                          {sb.terminal_reason ? (
                            <div className="dim-desc">
                              <Badge tone={statusTone(sb.terminal_reason)}>{sb.terminal_reason}</Badge>
                            </div>
                          ) : null}
                          {sb.eval_error ? (
                            <div className="dim-desc">
                              <Badge tone="red" title={sb.eval_error}>
                                eval failed
                              </Badge>
                            </div>
                          ) : null}
                          {sb.empty_workspace ? (
                            <div className="dim-desc">
                              <Badge
                                tone="amber"
                                title="The snapshot workspace was empty — the agent wrote nothing to score"
                              >
                                empty workspace
                              </Badge>
                            </div>
                          ) : null}
                          {(() => {
                            const mode = sb.eval_mode ?? modeBySandbox.get(sb.sandbox_id) ?? null;
                            if (!mode) return null;
                            const b = modeBadge(mode);
                            return (
                              <div className="dim-desc">
                                <Badge tone={b.tone} title={b.title}>
                                  {b.label}
                                </Badge>
                              </div>
                            );
                          })()}
                          {sb.rubric_version != null ? (
                            <div className="dim-desc">judged: rubric v{sb.rubric_version}</div>
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
                    {dimensionKeys.length > 0 ? (
                      <tr className="row-total">
                        <td>
                          <div className="dim-name">total</div>
                          <div className="dim-desc">Σ dimension scores · computed</div>
                        </td>
                        {sandboxes.map((sb) => {
                          const t = dimTotals.get(sb.sandbox_id);
                          const isLeader = !!t && t.count > 0 && t.sum === maxTotal;
                          return (
                            <td
                              key={sb.sandbox_id}
                              className={`score-cell${isLeader ? " score-cell--leader" : ""}`}
                            >
                              {t && t.count > 0 ? (
                                <>
                                  <span className="score-value">
                                    {Math.round(t.sum * 10) / 10}
                                  </span>
                                  <div
                                    className={`dim-desc${isLeader ? " dim-desc--leader" : ""}`}
                                  >
                                    {t.count} dim{t.count === 1 ? "" : "s"}
                                    {isLeader ? " · leader" : ""}
                                  </div>
                                </>
                              ) : (
                                <span className="score-na">—</span>
                              )}
                            </td>
                          );
                        })}
                      </tr>
                    ) : null}
                  </tbody>
                </table>
              </div>
            </Card>

            {sandboxes.some(
              (sb) => (sb.checker_results?.length ?? 0) > 0 || sb.suite,
            ) ? (
              <Card title="Deterministic checks" className="card--flush">
                <div className="table-wrap">
                  <table className="table">
                    <thead>
                      <tr>
                        <th>Sandbox</th>
                        <th>Pass</th>
                        <th>suite_pass</th>
                        <th>Checks</th>
                      </tr>
                    </thead>
                    <tbody>
                      {sandboxes.map((sb) => (
                        <CheckerRow key={sb.sandbox_id} sb={sb} />
                      ))}
                    </tbody>
                  </table>
                </div>
                <p className="faint" style={{ fontSize: "0.72rem", margin: "0.5rem 0 0" }}>
                  Objective outcomes from the suite&apos;s deterministic checkers
                  — pass/fail/skipped/error measured post-artifact, kept beside
                  (never folded into) judge scores except the explicit{" "}
                  <code>suite_pass</code> row.
                </p>
              </Card>
            ) : null}

            {modelGroups.length > 1 ? (
              <Card title="By model" className="card--flush">
                <div className="table-wrap">
                  <table className="table">
                    <thead>
                      <tr>
                        <th>Model</th>
                        <th>Sandboxes</th>
                        <th>Mean overall</th>
                        <th>Mean total (Σ dims)</th>
                      </tr>
                    </thead>
                    <tbody>
                      {modelGroups.map((g) => (
                        <tr key={g.model}>
                          <td className="mono" style={{ fontSize: "0.82rem" }}>
                            {g.model}
                          </td>
                          <td className="num">{g.count}</td>
                          <td className="num">
                            {g.meanOverall !== null ? (
                              <span
                                className={`score-value score-value--${scoreTone(g.meanOverall)}`}
                              >
                                {Math.round(g.meanOverall * 10) / 10}
                              </span>
                            ) : (
                              <span className="score-na">—</span>
                            )}
                          </td>
                          <td className="num">
                            {g.meanTotal !== null ? (
                              <span className="score-value">
                                {Math.round(g.meanTotal * 10) / 10}
                              </span>
                            ) : (
                              <span className="score-na">—</span>
                            )}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </Card>
            ) : null}

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
                          <td className="num" title={sb.sandbox_id}>
                            {shortId(sb.sandbox_id)}
                            {sb.name ? (
                              <div className="faint" style={{ fontSize: "0.7rem" }}>
                                {sb.name}
                              </div>
                            ) : null}
                          </td>
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
                                thumbnail={sb.thumbnail ?? null}
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
