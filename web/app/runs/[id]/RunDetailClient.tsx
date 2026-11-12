"use client";

/**
 * Run detail — status, task, config summary, sandboxes, rubric review/approve.
 *
 * Polls GET /api/runs/{id} every 5s while the run is non-terminal. The rubric
 * section renders the draft readably, allows JSON editing (PUT) while the run
 * is in `rubric_review`, and freezes it via POST /approve.
 */
import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import {
  ApiError,
  approveRun,
  getRun,
  TERMINAL_RUN_STATUSES,
  updateRubric,
  type HarnessConfig,
  type RubricContent,
  type RubricOut,
  type RunOut,
} from "@/lib/api";
import { formatTs, shortId, truncate } from "@/lib/format";
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

const POLL_MS = 5_000;

function errMsg(err: unknown, fallback: string): string {
  if (err instanceof ApiError) return err.detail;
  if (err instanceof Error) return err.message;
  return fallback;
}

function isTerminal(status: string): boolean {
  return TERMINAL_RUN_STATUSES.includes(status);
}

// ---------------------------------------------------------------------------
// small render helpers
// ---------------------------------------------------------------------------

function ConfigSummary({ configs }: { configs: HarnessConfig[] }) {
  if (configs.length === 0) return <span className="muted">no harness configs</span>;
  return (
    <div className="stack" style={{ gap: "0.6rem" }}>
      {configs.map((c, i) => (
        <div key={i} className="row" style={{ gap: "0.5rem" }}>
          <Badge tone="outline">#{i + 1}</Badge>
          <Badge tone="violet">{c.adapter}</Badge>
          {c.skill_packs.length === 0 ? (
            <span className="faint" style={{ fontSize: "0.8rem" }}>no skill packs</span>
          ) : (
            c.skill_packs.map((p) => (
              <span key={p} className="chip">
                {p}
              </span>
            ))
          )}
          {c.system_prompt_extra ? (
            <span className="faint" title={c.system_prompt_extra} style={{ fontSize: "0.78rem" }}>
              +prompt: {truncate(c.system_prompt_extra, 60)}
            </span>
          ) : null}
        </div>
      ))}
    </div>
  );
}

function RubricView({ rubric }: { rubric: RubricOut }) {
  const content = rubric.content ?? {};
  const dims = content.dimensions ?? [];
  const criteria = content.criteria ?? [];
  const [showCriteria, setShowCriteria] = useState(false);

  return (
    <div className="stack">
      {content.summary ? <p className="task-text muted">{content.summary}</p> : null}
      {dims.length > 0 ? (
        <div className="table-wrap">
          <table className="table">
            <thead>
              <tr>
                <th>Dimension</th>
                <th>Scale</th>
                <th>Description</th>
              </tr>
            </thead>
            <tbody>
              {dims.map((d, i) => (
                <tr key={i}>
                  <td>
                    <div className="mono" style={{ fontSize: "0.8rem", fontWeight: 600 }}>
                      {d.name}
                    </div>
                    {d.title ? <div className="faint" style={{ fontSize: "0.78rem" }}>{d.title}</div> : null}
                  </td>
                  <td className="num" style={{ whiteSpace: "nowrap" }}>
                    {d.min_score ?? 0}–{d.max_score ?? 10}
                    {d.weight != null && d.weight !== 1 ? (
                      <span className="faint"> ×{d.weight}</span>
                    ) : null}
                  </td>
                  <td className="muted" style={{ fontSize: "0.82rem" }}>
                    {d.description ?? "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
      {criteria.length > 0 ? (
        <div>
          <button
            type="button"
            className="btn btn--sm btn--ghost"
            onClick={() => setShowCriteria((s) => !s)}
          >
            {showCriteria ? "▾" : "▸"} {criteria.length} criteria
          </button>
          {showCriteria ? (
            <ol style={{ margin: "0.5rem 0 0 1.2rem", fontSize: "0.85rem" }}>
              {criteria.map((c, i) => (
                <li key={i} style={{ marginBottom: "0.35rem" }}>
                  <span className="mono faint" style={{ fontSize: "0.75rem" }}>
                    [{c.dimension ?? "?"}]
                  </span>{" "}
                  {c.text}
                </li>
              ))}
            </ol>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

// ---------------------------------------------------------------------------
// main client component
// ---------------------------------------------------------------------------

export default function RunDetailClient({ runId }: { runId: string }) {
  const [runData, setRunData] = useState<RunOut | null>(null);
  // Which runId the loaded data belongs to — deriving `run`/`loading` from
  // this (instead of setState-in-effect) keeps the hooks lint clean and
  // correctly resets when navigating between runs without a remount.
  const [loadedId, setLoadedId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  // `run` is only exposed when it belongs to the current runId — prevents a
  // previous run's data (and its polling status) leaking across navigation.
  const run = loadedId === runId ? runData : null;

  // rubric editing state
  const [editing, setEditing] = useState(false);
  const [draftJson, setDraftJson] = useState("");
  const [rubricBusy, setRubricBusy] = useState(false);
  const [rubricError, setRubricError] = useState<string | null>(null);

  const refresh = useCallback(
    async (silent = true) => {
      try {
        const r = await getRun(runId);
        setRunData(r);
        setLoadedId(runId);
        setError(null);
        return r;
      } catch (err) {
        // Silent poll failures keep last-good data on screen; only the
        // initial/explicit load surfaces an error.
        if (!silent) {
          setError(errMsg(err, "Failed to load run."));
        }
        return null;
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

  const status = run?.status ?? "";
  useEffect(() => {
    if (!status || isTerminal(status)) return;
    const t = setInterval(() => {
      refresh(true);
    }, POLL_MS);
    return () => clearInterval(t);
  }, [status, refresh]);

  const rubric = run?.rubric ?? null;
  const rubricEditable =
    run?.status === "rubric_review" && rubric?.status === "draft";

  function startEdit() {
    if (!rubric) return;
    setDraftJson(JSON.stringify(rubric.content, null, 2));
    setRubricError(null);
    setEditing(true);
  }

  async function saveRubric() {
    if (!rubric) return;
    setRubricError(null);
    let parsed: RubricContent;
    try {
      const v: unknown = JSON.parse(draftJson);
      if (!v || typeof v !== "object" || Array.isArray(v)) {
        throw new Error("rubric content must be a JSON object");
      }
      parsed = v as RubricContent;
    } catch (err) {
      setRubricError(`Invalid JSON: ${err instanceof Error ? err.message : String(err)}`);
      return;
    }
    setRubricBusy(true);
    try {
      await updateRubric(runId, parsed);
      setEditing(false);
      setNotice("Rubric saved.");
      await refresh(true);
    } catch (err) {
      setRubricError(errMsg(err, "Failed to save rubric."));
    } finally {
      setRubricBusy(false);
    }
  }

  async function approve() {
    if (!rubric) return;
    if (
      !window.confirm(
        "Approve this rubric and launch the sandboxes? This freezes the rubric and cannot be undone.",
      )
    ) {
      return;
    }
    setRubricBusy(true);
    setRubricError(null);
    try {
      const updated = await approveRun(runId);
      setRunData(updated);
      setLoadedId(runId);
      setNotice("Rubric approved — provisioning sandboxes.");
    } catch (err) {
      setRubricError(errMsg(err, "Approve failed."));
    } finally {
      setRubricBusy(false);
    }
  }

  // ---- render -----------------------------------------------------------

  if (!run) {
    if (error) {
      return (
        <main className="container container--narrow">
          <ErrorNote title="Could not load run">
            {error}
            {error.includes("404") || error.toLowerCase().includes("not found") ? (
              <div className="notice__detail">
                The run may not exist, or the control API is not running yet.
              </div>
            ) : null}
          </ErrorNote>
          <p style={{ marginTop: "1rem" }}>
            <Link href="/runs" className="btn btn--sm">
              ← Back to runs
            </Link>
          </p>
        </main>
      );
    }
    return (
      <main className="container">
        <Loading label="Loading run…" />
      </main>
    );
  }

  const configs: HarnessConfig[] = run.config?.harness_configs ?? [];
  const budgets = run.config?.budgets;
  const evaluatorModel = run.config?.evaluator_model ?? null;

  return (
    <main className="container">
      <div className="page-header">
        <div>
          <div className="row" style={{ marginBottom: "0.35rem" }}>
            <h1 className="mono" style={{ fontSize: "1.15rem" }} title={run.id}>
              {shortId(run.id, 13)}…
            </h1>
            <Badge tone={statusTone(run.status)}>{run.status}</Badge>
            {run.same_model_eval ? (
              <Badge tone="amber" title="evaluator model == participant model">
                same-model eval
              </Badge>
            ) : null}
          </div>
          <p className="subtitle">
            <span className="mono">{run.model_name}</span>
            {evaluatorModel ? (
              <>
                {" "}· evaluator <span className="mono">{evaluatorModel}</span>
              </>
            ) : null}
          </p>
        </div>
        <div className="row">
          <Link href={`/runs/${run.id}/timeline`} className="btn btn--sm">
            Timeline →
          </Link>
          <Link href={`/runs/${run.id}/results`} className="btn btn--sm">
            Results →
          </Link>
          <Link href="/runs" className="btn btn--sm btn--ghost">
            ← All runs
          </Link>
        </div>
      </div>

      <div className="stack">
        {notice ? <InfoNote>{notice}</InfoNote> : null}
        {error ? <ErrorNote>{error}</ErrorNote> : null}

        <Card title="Task">
          <p className="task-text">{run.task_prompt}</p>
          <dl className="kv" style={{ marginTop: "0.9rem" }}>
            <dt>Run ID</dt>
            <dd className="mono">{run.id}</dd>
            <dt>Created</dt>
            <dd className="mono">{formatTs(run.created_at)}</dd>
            <dt>Started</dt>
            <dd className="mono">{formatTs(run.started_at)}</dd>
            <dt>Finished</dt>
            <dd className="mono">{formatTs(run.finished_at)}</dd>
          </dl>
        </Card>

        <div className="split">
          <Card title="Harness configs">
            <ConfigSummary configs={configs} />
            {budgets ? (
              <dl className="kv" style={{ marginTop: "0.9rem" }}>
                <dt>Budgets</dt>
                <dd className="mono">
                  {budgets.max_iters} iters · {budgets.token_budget.toLocaleString()} tokens ·{" "}
                  {Math.round(budgets.wall_clock_seconds / 60)} min wall clock
                </dd>
              </dl>
            ) : null}
          </Card>

          <Card title={`Sandboxes (${run.sandboxes.length})`}>
            {run.sandboxes.length === 0 ? (
              <EmptyState>No sandboxes provisioned yet.</EmptyState>
            ) : (
              <div className="table-wrap">
                <table className="table">
                  <thead>
                    <tr>
                      <th>Sandbox</th>
                      <th>Config</th>
                      <th>Status</th>
                      <th>Terminal reason</th>
                      <th>Finished</th>
                    </tr>
                  </thead>
                  <tbody>
                    {run.sandboxes.map((s) => {
                      const hc = (s.harness_config ?? {}) as Record<string, unknown>;
                      // Pre-orchestrator rows used `harness` instead of `adapter`.
                      const adapter = String(hc.adapter ?? hc.harness ?? "?");
                      const packs = Array.isArray(hc.skill_packs)
                        ? (hc.skill_packs as string[])
                        : [];
                      return (
                        <tr key={s.id}>
                          <td className="num">
                            <Link href={`/runs/${run.id}/timeline?sandbox=${s.id}`} title={s.id}>
                              {shortId(s.id)}
                            </Link>
                          </td>
                          <td>
                            <div className="row" style={{ gap: "0.35rem" }}>
                              <Badge tone="outline">{adapter}</Badge>
                              {packs.map((p) => (
                                <span key={p} className="chip">
                                  {p}
                                </span>
                              ))}
                            </div>
                          </td>
                          <td>
                            <Badge tone={statusTone(s.status)}>{s.status}</Badge>
                          </td>
                          <td>
                            {s.terminal_reason ? (
                              <Badge tone={statusTone(s.terminal_reason)}>{s.terminal_reason}</Badge>
                            ) : (
                              <span className="faint">—</span>
                            )}
                          </td>
                          <td className="num faint">{formatTs(s.finished_at)}</td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            )}
          </Card>
        </div>

        <Card
          title={
            <span className="row" style={{ gap: "0.5rem" }}>
              Rubric
              {rubric ? (
                <>
                  <Badge tone={statusTone(rubric.status)}>v{rubric.version} · {rubric.status}</Badge>
                  <span className="faint" style={{ textTransform: "none", fontWeight: 400 }}>
                    {formatTs(rubric.created_at)}
                  </span>
                </>
              ) : null}
            </span>
          }
          actions={
            rubricEditable ? (
              editing ? (
                <>
                  <button
                    type="button"
                    className="btn btn--sm"
                    onClick={saveRubric}
                    disabled={rubricBusy}
                  >
                    {rubricBusy ? "Saving…" : "Save rubric"}
                  </button>
                  <button
                    type="button"
                    className="btn btn--sm btn--primary"
                    onClick={approve}
                    disabled={rubricBusy}
                  >
                    Approve &amp; launch
                  </button>
                  <button
                    type="button"
                    className="btn btn--sm btn--ghost"
                    onClick={() => {
                      setEditing(false);
                      setRubricError(null);
                    }}
                    disabled={rubricBusy}
                  >
                    Cancel
                  </button>
                </>
              ) : (
                <>
                  <button type="button" className="btn btn--sm" onClick={startEdit}>
                    Edit rubric
                  </button>
                  <button
                    type="button"
                    className="btn btn--sm btn--primary"
                    onClick={approve}
                    disabled={rubricBusy}
                  >
                    Approve &amp; launch
                  </button>
                </>
              )
            ) : null
          }
        >
          {rubric === null ? (
            run.status === "submitted" ? (
              <Loading label="Drafting rubric via the gateway — this can take a minute on small models…" />
            ) : (
              <EmptyState>No rubric for this run.</EmptyState>
            )
          ) : editing ? (
            <div className="stack">
              <p className="field__hint">
                Edit the rubric JSON. <code>dimensions</code> must contain exactly:{" "}
                <code>functional_requirements</code>, <code>acceptance_criteria</code>,{" "}
                <code>code_quality</code>, <code>cyclomatic_complexity</code>.
              </p>
              <textarea
                className="textarea"
                style={{ minHeight: "320px" }}
                value={draftJson}
                onChange={(e) => setDraftJson(e.target.value)}
                disabled={rubricBusy}
                spellCheck={false}
              />
              {rubricError ? <ErrorNote>{rubricError}</ErrorNote> : null}
            </div>
          ) : (
            <RubricView rubric={rubric} />
          )}
          {rubricError && !editing ? <ErrorNote>{rubricError}</ErrorNote> : null}
        </Card>

        {run.status === "rubric_review" && rubric ? (
          <WarnNote>
            This run is waiting for rubric approval — sandboxes launch only after you
            approve. Review the rubric above, edit if needed, then hit{" "}
            <strong>Approve &amp; launch</strong>.
          </WarnNote>
        ) : null}
      </div>
    </main>
  );
}
