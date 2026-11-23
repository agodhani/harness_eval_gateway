"use client";

/**
 * Run detail — status, task, config summary, sandboxes, rubric review/approve.
 *
 * Polls GET /api/runs/{id} every 5s while the run is non-terminal. The rubric
 * section renders the draft readably, allows JSON editing (PUT) while the run
 * is in `rubric_review`, and freezes it via POST /approve.
 */
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import {
  ApiError,
  approveRun,
  CANCELLABLE_RUN_STATUSES,
  deleteRun,
  getRun,
  getRunResults,
  runModels,
  TERMINAL_RUN_STATUSES,
  updateRubric,
  type HarnessConfig,
  type RubricContent,
  type RubricOut,
  type RunOut,
  type RunResults,
  type SandboxOut,
} from "@/lib/api";
import { formatTs, shortId, truncate } from "@/lib/format";
import {
  consumeRunWarnings,
  specFromRun,
  stageResubmit,
} from "@/lib/resubmit";
import RunConsoleLive from "@/components/RunConsoleLive";
import { SavePresetDialog } from "@/components/SavePresetDialog";
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
  // "cancelled" lands with the DELETE /api/runs/{id} backend work — treat it
  // as terminal so polling stops once a cancel lands.
  return TERMINAL_RUN_STATUSES.includes(status) || status === "cancelled";
}

function scoreTone(score: number): "good" | "mid" | "bad" {
  if (score >= 8) return "good";
  if (score >= 5) return "mid";
  return "bad";
}

/** PR24 — one-line seed description for the Task card: the task_inputs
 * source (repo url + resolved ref, or staged archive name/id) plus the
 * seed_sha every cell shares. */
function seedLabel(
  ti: { repo_url?: string | null; ref?: string | null; archive_id?: string | null } | null | undefined,
  sandboxes: SandboxOut[],
): string | null {
  if (!ti) return null;
  const hcSeed = sandboxes
    .map((s) => (s.harness_config ?? {}) as Record<string, unknown>)
    .map((hc) => hc.seed as Record<string, unknown> | undefined)
    .find((seed) => seed && typeof seed === "object");
  const sha = sandboxes
    .map((s) => (s.harness_config ?? {}) as Record<string, unknown>)
    .map((hc) => hc.seed_sha)
    .find((v): v is string => typeof v === "string" && v.length > 0);
  const shaBit = sha ? ` · seed_sha ${sha.slice(0, 10)}` : "";
  if (ti.repo_url) {
    const resolved =
      (hcSeed?.resolved_sha as string | undefined) ?? ti.ref ?? "HEAD";
    return `repo ${ti.repo_url} @ ${String(resolved).slice(0, 12)}${shaBit}`;
  }
  if (ti.archive_id) {
    const fname =
      (hcSeed?.filename as string | undefined) ??
      `${ti.archive_id.slice(0, 8)}…`;
    return `archive ${fname}${shaBit}`;
  }
  return sha ? `seeded${shaBit}` : null;
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
          {c.name ? (
            <Badge tone="gray" title="config label">
              {c.name}
            </Badge>
          ) : null}
          {c.model ? (
            <Badge tone="amber" title="pinned model — overrides the run's model list for this config">
              {c.model}
            </Badge>
          ) : null}
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

function RubricView({
  rubric,
  results,
  runId,
}: {
  rubric: RubricOut;
  results: RunResults | null;
  runId: string;
}) {
  const content = rubric.content ?? {};
  const dims = content.dimensions ?? [];
  const criteria = content.criteria ?? [];
  const [showCriteria, setShowCriteria] = useState(false);
  const scored = results?.sandboxes ?? [];
  const hasScores = scored.length > 0;
  const anyOverall = scored.some((s) => s.overall);
  // PR14: show the model under each column only on multi-model runs —
  // single-model runs already carry it in the page subtitle.
  const multiModel = new Set(scored.map((s) => s.model || "")).size > 1;

  function scoreCell(sb: RunResults["sandboxes"][number], dim: string) {
    const entry = sb.dimensions?.[dim];
    if (!entry) return <span className="score-na">—</span>;
    return (
      <span
        className={`score-value score-value--${scoreTone(entry.score)}`}
        title={entry.rationale || undefined}
      >
        {entry.score}
      </span>
    );
  }

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
                {scored.map((sb) => (
                  <th
                    key={sb.sandbox_id}
                    className="num"
                    style={{ whiteSpace: "nowrap", fontWeight: 500 }}
                  >
                    <Link
                      href={`/runs/${runId}/timeline?sandbox=${sb.sandbox_id}`}
                      className="mono"
                      title={sb.sandbox_id}
                      style={{ fontSize: "0.72rem" }}
                    >
                      {shortId(sb.sandbox_id)}
                    </Link>
                    <div className="faint" style={{ fontSize: "0.66rem", fontWeight: 400 }}>
                      {multiModel && sb.model ? `${sb.model} · ` : ""}
                      {sb.name ? `${sb.name} · ` : ""}
                      {String(sb.harness_config?.adapter ?? sb.harness_config?.harness ?? "?")}
                    </div>
                    {sb.eval_error ? (
                      <div style={{ fontSize: "0.66rem", fontWeight: 400 }}>
                        <Badge tone="red" title={sb.eval_error}>
                          eval failed
                        </Badge>
                      </div>
                    ) : null}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {hasScores && anyOverall ? (
                <tr className="row-overall">
                  <td>
                    <div className="mono" style={{ fontSize: "0.8rem", fontWeight: 600 }}>
                      overall
                    </div>
                    <div className="faint" style={{ fontSize: "0.78rem" }}>
                      judge-reported aggregate
                    </div>
                  </td>
                  <td className="num faint">—</td>
                  <td className="muted" style={{ fontSize: "0.82rem" }}>
                    evaluator aggregate score
                  </td>
                  {scored.map((sb) => (
                    <td key={sb.sandbox_id} className="num">
                      {sb.overall ? (
                        <span
                          className={`score-value score-value--${scoreTone(sb.overall.score)}`}
                          title={sb.overall.rationale || undefined}
                        >
                          {sb.overall.score}
                        </span>
                      ) : (
                        <span className="score-na">—</span>
                      )}
                    </td>
                  ))}
                </tr>
              ) : null}
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
                  {scored.map((sb) => (
                    <td key={sb.sandbox_id} className="num">
                      {scoreCell(sb, d.name ?? "")}
                    </td>
                  ))}
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
  const router = useRouter();
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

  // evaluator scores for the rubric table (null until results endpoint has data)
  const [results, setResults] = useState<RunResults | null>(null);

  // Run-create warnings handed off from /submit (sessionStorage, consumed
  // once) — the backend may attach advisories to the run-create response.
  const [createWarnings, setCreateWarnings] = useState<string[]>([]);
  // cancel/delete in-flight state
  const [cancelling, setCancelling] = useState(false);
  // PR22 — save this run's task prompt as a reusable preset.
  const [savePresetOpen, setSavePresetOpen] = useState(false);

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

  // Fetch evaluator scores once the run reaches evaluating/done/failed; poll
  // while evaluating so per-dimension scores stream into the rubric table.
  useEffect(() => {
    if (!["evaluating", "done", "failed"].includes(status)) return;
    let cancelled = false;
    const load = async () => {
      try {
        const r = await getRunResults(runId);
        if (!cancelled) setResults(r);
      } catch {
        // results 404s until the evaluator lane lands — keep table scoreless
      }
    };
    void load();
    if (status !== "evaluating") return () => { cancelled = true; };
    const t = setInterval(load, 6_000);
    return () => {
      cancelled = true;
      clearInterval(t);
    };
  }, [runId, status]);

  // Run-create warnings staged by /submit — read once (they're cleared).
  useEffect(() => {
    let cancelled = false;
    void Promise.resolve().then(() => {
      if (cancelled) return;
      const w = consumeRunWarnings(runId);
      if (w.length) setCreateWarnings(w);
    });
    return () => {
      cancelled = true;
    };
  }, [runId]);

  const rubric = run?.rubric ?? null;
  const rubricEditable =
    run?.status === "rubric_review" && rubric?.status === "draft";

  /** Resubmit — stage this run's spec and open /submit prefilled. */
  function resubmit() {
    if (!run) return;
    stageResubmit(specFromRun(run));
    router.push("/submit");
  }

  /** Cancel a pre-spawn run or delete a terminal one — DELETE /api/runs/{id}
   * decides which ("cancelled" → status flip; "deleted" → hard removal).
   * In-flight runs (provisioning/running/evaluating) are gated off the
   * button entirely since the API 409s them. A deleted row 404s the next
   * refresh → head back to the list. */
  async function remove() {
    if (!run || cancelling) return;
    const terminal = isTerminal(run.status);
    if (
      !window.confirm(
        terminal
          ? `Delete run ${shortId(run.id)}? The run and its events, scores, rubric, and stored artifacts are permanently removed.`
          : `Cancel run ${shortId(run.id)}? It is marked cancelled — nothing has been spawned yet, so no sandboxes are stopped.`,
      )
    ) {
      return;
    }
    setCancelling(true);
    setError(null);
    try {
      const out = await deleteRun(run.id);
      if (out.status === "deleted") {
        router.push("/runs");
        return;
      }
      const refreshed = await refresh(true);
      if (refreshed === null) router.push("/runs");
      else setNotice("Run cancelled.");
    } catch (err) {
      setError(errMsg(err, terminal ? "Delete failed." : "Cancel failed."));
    } finally {
      setCancelling(false);
    }
  }

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
      setNotice(
        updated.status === "queued"
          ? "Rubric approved — run queued behind an active run."
          : "Rubric approved — provisioning sandboxes.",
      );
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
  const evaluatorModel = run.config?.evaluator_model ?? run.model_name;
  // PR14: effective participant-model set ([model_name] + config extras).
  const participantModels = runModels(
    run.model_name,
    run.config?.models,
    run.config?.harness_configs,
  );

  return (
    <main className="container">
      <div className="page-header">
        <div>
          <span className="kicker"><b>02</b> — run detail</span>
          <div className="row" style={{ marginBottom: "0.35rem" }}>
            <h1 className="mono" style={{ fontSize: "1.15rem" }} title={run.id}>
              {shortId(run.id, 13)}…
            </h1>
            <Badge tone={statusTone(run.status)}>
              {run.status === "queued" && run.queue_position != null
                ? `queued · #${run.queue_position}`
                : run.status}
            </Badge>
            {run.same_model_eval ? (
              <Badge tone="amber" title="evaluator model == a participant model">
                same-model eval
              </Badge>
            ) : null}
          </div>
          <p className="subtitle">
            <span className="mono">
              {participantModels.length > 1
                ? participantModels.join(", ")
                : run.model_name}
            </span>
            {participantModels.length > 1 ? (
              <span className="faint"> ({participantModels.length} models)</span>
            ) : null}
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
          <Link href={`/runs/${run.id}/analysis`} className="btn btn--sm">
            Analysis →
          </Link>
          {isTerminal(run.status) ? (
            <>
              <button
                type="button"
                className="btn btn--sm"
                onClick={resubmit}
                title="Open the submit form prefilled with this run's spec"
              >
                ↻ Resubmit
              </button>
              <button
                type="button"
                className="btn btn--sm btn--ghost"
                onClick={() => void remove()}
                disabled={cancelling}
                title="Permanently delete this run and its artifacts"
              >
                {cancelling ? "Deleting…" : "Delete run"}
              </button>
            </>
          ) : CANCELLABLE_RUN_STATUSES.includes(run.status) ? (
            // Pre-spawn only — DELETE flips these to cancelled. In-flight
            // runs (provisioning/running/evaluating) 409 server-side, so no
            // control renders for them at all.
            <button
              type="button"
              className="btn btn--sm btn--ghost"
              onClick={() => void remove()}
              disabled={cancelling}
              title="Cancel this run — it is marked cancelled (nothing spawned to stop)"
            >
              {cancelling ? "Cancelling…" : "Cancel run"}
            </button>
          ) : null}
          <Link href="/runs" className="btn btn--sm btn--ghost">
            ← All runs
          </Link>
        </div>
      </div>

      <div className="stack">
        {notice ? <InfoNote>{notice}</InfoNote> : null}
        {error ? <ErrorNote>{error}</ErrorNote> : null}
        {createWarnings.length > 0 ? (
          <WarnNote>
            <strong>Run created with warnings:</strong>
            <ul style={{ margin: "0.35rem 0 0 1.1rem", padding: 0 }}>
              {createWarnings.map((w, i) => (
                <li key={i}>{w}</li>
              ))}
            </ul>
          </WarnNote>
        ) : null}

        <Card
          title="Task"
          actions={
            run.preset_ref ? (
              <Badge tone="outline" title="Task preset this prompt resolved from">
                <Link href={`/presets/${run.preset_ref}`}>{run.preset_ref}</Link>
                {run.preset_revision != null ? ` · rev ${run.preset_revision}` : ""}
              </Badge>
            ) : (
              <button
                type="button"
                className="btn btn--sm btn--ghost"
                onClick={() => setSavePresetOpen(true)}
                title="Save this task prompt as a reusable preset"
              >
                save prompt as preset
              </button>
            )
          }
        >
          <p className="task-text">{run.task_prompt}</p>
          <dl className="kv" style={{ marginTop: "0.9rem" }}>
            <dt>Run ID</dt>
            <dd className="mono">{run.id}</dd>
            {run.preset_ref ? (
              <>
                <dt>Preset</dt>
                <dd className="mono">
                  <Link href={`/presets/${run.preset_ref}`}>{run.preset_ref}</Link>
                  {run.preset_revision != null ? (
                    <span className="faint">
                      {" "}· rev {run.preset_revision}
                      {" "}(the prompt above is the snapshot taken at submit)
                    </span>
                  ) : null}
                </dd>
              </>
            ) : null}
            {run.suite_ref ? (
              <>
                <dt>Suite</dt>
                <dd className="mono">
                  <Link href={`/suites/${run.suite_ref}`}>{run.suite_ref}</Link>
                  <span className="faint">
                    {" "}· task #{run.suite_index ?? "?"}
                  </span>
                  {run.suite_run_id ? (
                    <>
                      {" "}·{" "}
                      <Link href={`/suite-runs/${run.suite_run_id}`}>
                        batch {run.suite_run_id.slice(0, 8)}
                      </Link>
                    </>
                  ) : null}
                </dd>
              </>
            ) : null}
            <dt>Created</dt>
            <dd className="mono">{formatTs(run.created_at)}</dd>
            <dt>Started</dt>
            <dd className="mono">{formatTs(run.started_at)}</dd>
            <dt>Finished</dt>
            <dd className="mono">{formatTs(run.finished_at)}</dd>
            {seedLabel(run.config?.task_inputs, run.sandboxes) ? (
              <>
                <dt>Seed</dt>
                <dd
                  className="mono"
                  title="task inputs — every sandbox's workspace started from these identical bytes"
                >
                  {seedLabel(run.config?.task_inputs, run.sandboxes)}
                </dd>
              </>
            ) : null}
          </dl>
        </Card>

        <div className="split">
          <Card title="Harness configs">
            <ConfigSummary configs={configs} />
            {participantModels.length > 1 ? (
              <div className="chip-row" style={{ marginTop: "0.6rem" }}>
                <span className="faint" style={{ fontSize: "0.78rem" }}>
                  models:
                </span>
                {participantModels.map((m) => (
                  <span key={m} className="chip">
                    {m}
                  </span>
                ))}
              </div>
            ) : null}
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
                            {s.name ? (
                              <div className="faint" style={{ fontSize: "0.7rem" }} title={s.name}>
                                {s.name}
                              </div>
                            ) : null}
                          </td>
                          <td>
                            <div className="row" style={{ gap: "0.35rem" }}>
                              <Badge tone="outline">{adapter}</Badge>
                              {typeof hc.model === "string" && hc.model ? (
                                <Badge tone="teal" title="participant model">
                                  {hc.model}
                                </Badge>
                              ) : null}
                              {typeof hc.seed_sha === "string" && hc.seed_sha ? (
                                <Badge
                                  tone="gray"
                                  title={`task-input seed ${hc.seed_sha} — identical across every seeded cell`}
                                >
                                  seed {String(hc.seed_sha).slice(0, 8)}
                                </Badge>
                              ) : null}
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

        <RunConsoleLive run={run} />

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
            <RubricView rubric={rubric} results={results} runId={run.id} />
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

      {savePresetOpen ? (
        <SavePresetDialog
          taskPrompt={run.task_prompt}
          initialTitle={run.task_prompt.trim().split("\n")[0]?.slice(0, 80) ?? ""}
          onClose={() => setSavePresetOpen(false)}
          onSaved={(p) => {
            setSavePresetOpen(false);
            setNotice(`Saved as preset "${p.slug}" — see it under Presets.`);
          }}
        />
      ) : null}
    </main>
  );
}
