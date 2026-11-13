"use client";

/**
 * F1 — submission form: task prompt + model + harness config matrix +
 * evaluator model + budgets → POST /api/runs → redirect to run detail.
 *
 * Client-side validation mirrors the API contract (plan §1): task_prompt and
 * model_name required, ≥1 harness config, known adapter/skill-pack names.
 */
import { useRouter } from "next/navigation";
import { useEffect, useMemo, useState, type FormEvent } from "react";
import {
  ADAPTERS,
  ApiError,
  createRun,
  DEFAULT_BUDGETS,
  listHarnesses,
  listSkillPacks,
  SKILL_PACKS,
  type Budgets,
  type HarnessConfig,
  type HarnessManifest,
  type SkillPack,
  type SkillpackImportResult,
} from "@/lib/api";
import { ComboInput } from "@/components/ComboInput";
import { Badge, Card, ErrorNote, InfoNote } from "@/components/ui";
import { ImportSkillPackDialog } from "./ImportSkillPackDialog";

const MODEL_SUGGESTIONS = ["qwen3:8b", "qwen2.5-coder:3b", "qwen2.5-coder:7b"];
const MAX_CONFIGS = 8;

interface ConfigRow {
  key: number;
  adapter: string;
  skill_packs: string[];
  system_prompt_extra: string;
}

let nextKey = 1;
function blankConfig(): ConfigRow {
  return { key: nextKey++, adapter: "miniloop", skill_packs: [], system_prompt_extra: "" };
}

export default function SubmitPage() {
  const router = useRouter();

  const [taskPrompt, setTaskPrompt] = useState("");
  const [modelName, setModelName] = useState("qwen3:8b");
  const [evaluatorModel, setEvaluatorModel] = useState("");
  const [configs, setConfigs] = useState<ConfigRow[]>(() => [blankConfig()]);
  const [budgets, setBudgets] = useState<Budgets>({ ...DEFAULT_BUDGETS });
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});
  // Harness registry from GET /api/harnesses; static ADAPTERS is the fallback
  // when the API isn't running (page convention: degrade gracefully).
  const [harnesses, setHarnesses] = useState<HarnessManifest[]>([]);

  useEffect(() => {
    let cancelled = false;
    listHarnesses()
      .then((ms) => {
        if (!cancelled && ms.length > 0) setHarnesses(ms);
      })
      .catch(() => {
        /* offline fallback: static ADAPTERS list */
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const adapterNames = useMemo(
    () => (harnesses.length > 0 ? harnesses.map((h) => h.name) : [...ADAPTERS]),
    [harnesses],
  );
  const adapterLabel = (name: string) =>
    harnesses.find((h) => h.name === name)?.display_name ?? name;

  // Selectable packs — vendored fallback until GET /api/skillpacks lands
  // (imported packs only exist server-side). `importFor` = config key the
  // open GitHub-import dialog applies to (null = closed).
  const [packs, setPacks] = useState<SkillPack[]>(() =>
    SKILL_PACKS.map((name) => ({
      name,
      source: "vendored",
      skill_count: 0,
      skills: [],
      origin_url: null,
      imported_at: null,
    })),
  );
  const [importFor, setImportFor] = useState<number | null>(null);

  useEffect(() => {
    listSkillPacks()
      .then((list) => {
        if (list.length) setPacks(list);
      })
      .catch(() => {
        /* API down — vendored fallback stays */
      });
  }, []);

  const packNames = useMemo(() => new Set(packs.map((p) => p.name)), [packs]);

  function onPackImported(result: SkillpackImportResult) {
    const key = importFor;
    // Refetch for accurate merged state (skills/count/origin on appends).
    listSkillPacks()
      .then(setPacks)
      .catch(() => {
        setPacks((prev) =>
          prev.some((p) => p.name === result.pack)
            ? prev
            : [
                ...prev,
                {
                  name: result.pack,
                  source: "imported",
                  skill_count: result.skill_count,
                  skills: result.imported.map((s) => s.name),
                  origin_url: null,
                  imported_at: new Date().toISOString(),
                },
              ],
        );
      });
    // Auto-select the imported pack in the config the dialog was opened from.
    if (key !== null) {
      setConfigs((cs) =>
        cs.map((c) =>
          c.key === key && !c.skill_packs.includes(result.pack)
            ? { ...c, skill_packs: [...c.skill_packs, result.pack] }
            : c,
        ),
      );
    }
    setImportFor(null);
  }

  const effectiveEvaluator = evaluatorModel.trim() || modelName.trim();
  const sameModelEval = effectiveEvaluator !== "" && effectiveEvaluator === modelName.trim();

  const canAddConfig = configs.length < MAX_CONFIGS;

  const validation = useMemo(() => {
    const errs: Record<string, string> = {};
    if (!taskPrompt.trim()) errs.task_prompt = "Task prompt is required.";
    if (!modelName.trim()) errs.model_name = "Model name is required.";
    if (configs.length === 0) errs.configs = "At least one harness config is required.";
    configs.forEach((c, i) => {
      if (!adapterNames.includes(c.adapter)) {
        errs[`cfg_${i}_adapter`] = `Unknown adapter "${c.adapter}".`;
      }
      for (const p of c.skill_packs) {
        if (!packNames.has(p)) errs[`cfg_${i}_packs`] = `Unknown skill pack "${p}".`;
      }
    });
    if (budgets.max_iters < 1 || budgets.max_iters > 1000)
      errs.max_iters = "max_iters must be 1–1000.";
    if (budgets.token_budget < 1000)
      errs.token_budget = "token_budget must be ≥ 1000 (per-run).";
    if (budgets.wall_clock_seconds < 60 || budgets.wall_clock_seconds > 86400)
      errs.wall_clock_seconds = "wall_clock_seconds must be 60–86400.";
    return errs;
  }, [taskPrompt, modelName, configs, budgets, adapterNames, packNames]);

  const isValid = Object.keys(validation).length === 0;

  function updateConfig(key: number, patch: Partial<ConfigRow>) {
    setConfigs((cs) => cs.map((c) => (c.key === key ? { ...c, ...patch } : c)));
  }

  function togglePack(cfg: ConfigRow, pack: string) {
    const has = cfg.skill_packs.includes(pack);
    updateConfig(cfg.key, {
      skill_packs: has ? cfg.skill_packs.filter((p) => p !== pack) : [...cfg.skill_packs, pack],
    });
  }

  function addConfig() {
    if (canAddConfig) setConfigs((cs) => [...cs, blankConfig()]);
  }

  function removeConfig(key: number) {
    setConfigs((cs) => (cs.length > 1 ? cs.filter((c) => c.key !== key) : cs));
  }

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    setFieldErrors(validation);
    if (!isValid || submitting) return;

    const harness_configs: HarnessConfig[] = configs.map((c) => ({
      adapter: c.adapter,
      skill_packs: c.skill_packs,
      system_prompt_extra: c.system_prompt_extra.trim() || null,
    }));

    setSubmitting(true);
    try {
      const run = await createRun({
        task_prompt: taskPrompt.trim(),
        model_name: modelName.trim(),
        harness_configs,
        evaluator_model: evaluatorModel.trim() || null,
        budgets,
      });
      router.push(`/runs/${run.id}`);
    } catch (err) {
      setSubmitting(false);
      setError(
        err instanceof ApiError
          ? err.detail
          : err instanceof Error
            ? err.message
            : "Submit failed.",
      );
    }
  }

  return (
    <main className="container container--narrow">
      <div className="page-header">
        <div>
          <h1>Submit a run</h1>
          <p className="subtitle">
            One task, one model, N harness configurations — evaluated side by side.
          </p>
        </div>
      </div>

      <form onSubmit={onSubmit} className="form-grid" noValidate>
        <Card title="Task">
          <div className="field">
            <label className="field__label" htmlFor="task_prompt">
              Task prompt <span className="faint">(required)</span>
            </label>
            <textarea
              id="task_prompt"
              className="textarea"
              rows={6}
              placeholder='e.g. "Build a replica of the game 2048 in a single HTML file."'
              value={taskPrompt}
              onChange={(e) => setTaskPrompt(e.target.value)}
              disabled={submitting}
            />
            {fieldErrors.task_prompt ? (
              <span className="field__error">{fieldErrors.task_prompt}</span>
            ) : (
              <span className="field__hint">
                {taskPrompt.trim().length}/100,000 chars — the rubric is generated from this.
              </span>
            )}
          </div>
        </Card>

        <Card title="Model">
          <div className="input-row">
            <div className="field">
              <label className="field__label" htmlFor="model_name">
                Participant model <span className="faint">(required)</span>
              </label>
              <ComboInput
                id="model_name"
                value={modelName}
                onChange={setModelName}
                suggestions={MODEL_SUGGESTIONS}
                placeholder="qwen3:8b"
                disabled={submitting}
                mono
              />
              {fieldErrors.model_name ? (
                <span className="field__error">{fieldErrors.model_name}</span>
              ) : (
                <span className="field__hint">Same model runs in every sandbox — we compare harnesses, not models.</span>
              )}
            </div>
            <div className="field">
              <label className="field__label" htmlFor="evaluator_model">
                Evaluator model
              </label>
              <ComboInput
                id="evaluator_model"
                value={evaluatorModel}
                onChange={setEvaluatorModel}
                suggestions={MODEL_SUGGESTIONS}
                placeholder={modelName.trim() || "same as participant"}
                disabled={submitting}
                mono
              />
              {sameModelEval ? (
                <InfoNote>
                  Evaluator uses the <strong>same model</strong> as the participants
                  {modelName.trim() ? <> (<code>{modelName.trim()}</code>)</> : null} —
                  scores may not be fully independent. This is allowed; the results page flags it.
                </InfoNote>
              ) : (
                <span className="field__hint">Leave empty to reuse the participant model.</span>
              )}
            </div>
          </div>
        </Card>

        <Card
          title="Harness configurations"
          actions={
            <Badge tone="outline">
              {configs.length}/{MAX_CONFIGS}
            </Badge>
          }
        >
          <div className="stack">
            {configs.map((cfg, i) => (
              <div className="config-row" key={cfg.key}>
                <div className="config-row__head">
                  <span className="config-row__title">config {i + 1}</span>
                  <button
                    type="button"
                    className="btn btn--sm btn--ghost"
                    onClick={() => removeConfig(cfg.key)}
                    disabled={submitting || configs.length <= 1}
                    title={configs.length <= 1 ? "A run needs at least one config" : "Remove config"}
                  >
                    remove
                  </button>
                </div>
                <div className="input-row">
                  <div className="field">
                    <label className="field__label">Adapter</label>
                    <select
                      className="select"
                      value={cfg.adapter}
                      onChange={(e) => updateConfig(cfg.key, { adapter: e.target.value })}
                      disabled={submitting}
                    >
                      {adapterNames.map((a) => (
                        <option key={a} value={a}>
                          {adapterLabel(a)}
                        </option>
                      ))}
                    </select>
                  </div>
                  <div className="field">
                    <label className="field__label">
                      Skill packs{" "}
                      <button
                        type="button"
                        className="btn btn--sm btn--ghost"
                        onClick={() => setImportFor(cfg.key)}
                        disabled={submitting}
                        title="Import skills from a GitHub repo link"
                      >
                        + import from GitHub
                      </button>
                    </label>
                    <div className="check-list">
                      {packs.map((p) => (
                        <label key={p.name} className="check-item">
                          <input
                            type="checkbox"
                            checked={cfg.skill_packs.includes(p.name)}
                            onChange={() => togglePack(cfg, p.name)}
                            disabled={submitting}
                          />
                          <code>{p.name}</code>
                          {p.source === "imported" ? (
                            <Badge tone="violet" title={p.origin_url ?? "user-imported"}>
                              imported
                            </Badge>
                          ) : null}
                          {p.skill_count > 0 ? (
                            <span className="faint">({p.skill_count})</span>
                          ) : null}
                        </label>
                      ))}
                    </div>
                    <span className="field__hint">
                      {cfg.skill_packs.length === 0
                        ? "None — sandbox gets an empty packs dir."
                        : `${cfg.skill_packs.length} pack${cfg.skill_packs.length > 1 ? "s" : ""} staged.`}
                    </span>
                  </div>
                </div>
                <div className="field">
                  <label className="field__label">System prompt extra</label>
                  <textarea
                    className="textarea"
                    rows={2}
                    placeholder="Optional instructions appended to the harness system prompt (a comparison axis)."
                    value={cfg.system_prompt_extra}
                    onChange={(e) => updateConfig(cfg.key, { system_prompt_extra: e.target.value })}
                    disabled={submitting}
                  />
                </div>
                {fieldErrors[`cfg_${i}_adapter`] || fieldErrors[`cfg_${i}_packs`] ? (
                  <span className="field__error">
                    {fieldErrors[`cfg_${i}_adapter`] ?? fieldErrors[`cfg_${i}_packs`]}
                  </span>
                ) : null}
              </div>
            ))}
            <div>
              <button
                type="button"
                className="btn btn--sm"
                onClick={addConfig}
                disabled={submitting || !canAddConfig}
              >
                + Add configuration
              </button>
              {!canAddConfig ? (
                <span className="field__hint" style={{ marginLeft: "0.6rem" }}>
                  max {MAX_CONFIGS} per run
                </span>
              ) : null}
            </div>
            {fieldErrors.configs ? <span className="field__error">{fieldErrors.configs}</span> : null}
          </div>
        </Card>

        <Card title="Budgets">
          <details className="advanced">
            <summary>Termination policy — defaults shown</summary>
            <div className="advanced__body">
              <div className="input-row">
                <div className="field">
                  <label className="field__label" htmlFor="max_iters">Max iterations / sandbox</label>
                  <input
                    id="max_iters"
                    className="input"
                    type="number"
                    min={1}
                    max={1000}
                    value={budgets.max_iters}
                    onChange={(e) => setBudgets({ ...budgets, max_iters: Number(e.target.value) })}
                    disabled={submitting}
                  />
                  {fieldErrors.max_iters ? <span className="field__error">{fieldErrors.max_iters}</span> : null}
                </div>
                <div className="field">
                  <label className="field__label" htmlFor="token_budget">Token budget (per run)</label>
                  <input
                    id="token_budget"
                    className="input"
                    type="number"
                    min={1000}
                    step={1000}
                    value={budgets.token_budget}
                    onChange={(e) => setBudgets({ ...budgets, token_budget: Number(e.target.value) })}
                    disabled={submitting}
                  />
                  {fieldErrors.token_budget ? <span className="field__error">{fieldErrors.token_budget}</span> : null}
                </div>
                <div className="field">
                  <label className="field__label" htmlFor="wall_clock">Wall clock (seconds)</label>
                  <input
                    id="wall_clock"
                    className="input"
                    type="number"
                    min={60}
                    max={86400}
                    step={60}
                    value={budgets.wall_clock_seconds}
                    onChange={(e) => setBudgets({ ...budgets, wall_clock_seconds: Number(e.target.value) })}
                    disabled={submitting}
                  />
                  {fieldErrors.wall_clock_seconds ? <span className="field__error">{fieldErrors.wall_clock_seconds}</span> : null}
                </div>
              </div>
            </div>
          </details>
        </Card>

        {error ? <ErrorNote title="Submit failed">{error}</ErrorNote> : null}

        <div className="row">
          <button type="submit" className="btn btn--primary" disabled={submitting || !isValid}>
            {submitting ? (
              <>
                <span className="spinner" aria-hidden /> Submitting…
              </>
            ) : (
              "Submit run"
            )}
          </button>
          {!isValid && !submitting ? (
            <span className="field__hint">Fix the highlighted fields to submit.</span>
          ) : null}
        </div>
      </form>

      {importFor !== null ? (
        <ImportSkillPackDialog
          importedPacks={packs.filter((p) => p.source === "imported")}
          onClose={() => setImportFor(null)}
          onImported={onPackImported}
        />
      ) : null}
    </main>
  );
}
