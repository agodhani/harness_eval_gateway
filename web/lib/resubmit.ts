/**
 * Resubmit handoff — carries a finished/failed run's spec from the detail
 * and results pages to /submit via sessionStorage (the spec is far too
 * large for query params: task prompt + config matrix + budgets).
 *
 * `stageResubmit` writes a single-slot key; the submit page consumes it
 * once on mount (read + clear) so a later plain visit to /submit isn't
 * contaminated by a stale prefill.
 *
 * Pure-ish + import-light like the other lib modules — `node --test`
 * (type-stripped) compatible; sessionStorage access is guarded so the
 * module is safe to import during SSR/prerender.
 */
import type { Budgets, HarnessConfig, RunOut, TaskInputs } from "./api.ts";

/** Everything the submit form needs to re-create a run's spec. */
export interface ResubmitSpec {
  task_prompt: string;
  /** PR22: preset slug the task_prompt resolved from (null for freeform
   * prompts). Carried so resubmit re-attaches to the preset — the new run
   * resolves the preset's CURRENT revision verbatim (an updated preset
   * submits its updated body; the staged task_prompt is the "edit as
   * custom copy" fallback if the user detaches it). */
  preset_ref?: string | null;
  /** Participant models, primary first (row 1 → model_name). */
  models: string[];
  evaluator_model: string | null;
  harness_configs: HarnessConfig[];
  budgets: Budgets | null;
  /** PR24: the run's seed spec — repo seeds restore fully; archive seeds
   * restore as an already-staged upload id (the staged bytes persist
   * server-side under data/uploads). */
  task_inputs?: TaskInputs | null;
  /** PR27: repeats per matrix cell (undefined = the form's default 1). */
  trials?: number;
}

const STORAGE_KEY = "heg:resubmit-spec";

/** Run row → spec: participant set = persisted `config.models`
 * ([model_name] + extras) with model_name leading as a fallback. */
export function specFromRun(run: RunOut): ResubmitSpec {
  const cfg = run.config ?? {};
  const models = Array.isArray(cfg.models)
    ? (cfg.models as unknown[]).map(String).filter(Boolean)
    : [];
  if (run.model_name && !models.includes(run.model_name)) {
    models.unshift(run.model_name);
  }
  return {
    task_prompt: run.task_prompt ?? "",
    preset_ref: run.preset_ref ?? null,
    models: models.length ? models : [run.model_name].filter(Boolean),
    evaluator_model:
      (cfg.evaluator_model as string | null | undefined) ?? null,
    harness_configs: (cfg.harness_configs ?? []).map((c) => ({
      adapter: c.adapter,
      skill_packs: Array.isArray(c.skill_packs) ? [...c.skill_packs] : [],
      system_prompt_extra: c.system_prompt_extra ?? null,
      name: c.name ?? null,
      // Per-config model pins (PR14) ride along — the form has no pin UI
      // but re-submitting shouldn't silently drop them from the payload.
      model: c.model ?? null,
      // PR27: keep the config's model knobs (unsupported keys were already
      // stripped server-side at the original submit).
      model_options: c.model_options ?? null,
    })),
    budgets: cfg.budgets ?? null,
    task_inputs: (cfg.task_inputs as TaskInputs | null | undefined) ?? null,
    // PR27: repeats ride along — the stored column first, then the config
    // copy (specs written before the column existed), defaulting to 1.
    trials: run.trials ?? (cfg.trials as number | undefined) ?? 1,
  };
}

/** Stash a spec for the submit page to pick up (best-effort — storage can
 * be unavailable or full; a lost handoff just means an empty form). */
export function stageResubmit(spec: ResubmitSpec): void {
  try {
    sessionStorage.setItem(STORAGE_KEY, JSON.stringify(spec));
  } catch {
    /* storage unavailable — the submit page just opens empty */
  }
}

/** Read + clear the staged spec. Returns null when nothing was staged or
 * the payload doesn't look like a spec (corrupt / foreign write). */
export function consumeResubmit(): ResubmitSpec | null {
  try {
    const raw = sessionStorage.getItem(STORAGE_KEY);
    if (!raw) return null;
    sessionStorage.removeItem(STORAGE_KEY);
    const spec = JSON.parse(raw) as ResubmitSpec;
    if (
      !spec ||
      typeof spec !== "object" ||
      typeof spec.task_prompt !== "string" ||
      (spec.preset_ref != null && typeof spec.preset_ref !== "string") ||
      !Array.isArray(spec.models) ||
      !spec.models.every((m) => typeof m === "string") ||
      (spec.evaluator_model != null &&
        typeof spec.evaluator_model !== "string") ||
      !Array.isArray(spec.harness_configs) ||
      !spec.harness_configs.every(
        (c) =>
          c &&
          typeof c === "object" &&
          typeof c.adapter === "string" &&
          Array.isArray(c.skill_packs),
      ) ||
      (spec.budgets != null &&
        (typeof spec.budgets !== "object" || Array.isArray(spec.budgets)))
    ) {
      return null;
    }
    return spec;
  } catch {
    return null;
  }
}

// ---------------------------------------------------------------------------
// run-create warnings handoff — POST /api/runs may return `warnings` (new
// backend field); the submit page stashes them and the run detail page
// surfaces them once as an amber notice.
// ---------------------------------------------------------------------------

const WARNINGS_KEY = "heg:last-run-warnings";

interface StagedWarnings {
  run_id: string;
  warnings: string[];
}

/** Stash run-create warnings keyed to the new run's id. */
export function stageRunWarnings(runId: string, warnings: string[]): void {
  if (!warnings.length) return;
  try {
    sessionStorage.setItem(
      WARNINGS_KEY,
      JSON.stringify({ run_id: runId, warnings } satisfies StagedWarnings),
    );
  } catch {
    /* storage unavailable — warnings just don't surface */
  }
}

/** Read + clear staged warnings — only when they belong to `runId`. */
export function consumeRunWarnings(runId: string): string[] {
  try {
    const raw = sessionStorage.getItem(WARNINGS_KEY);
    if (!raw) return [];
    const staged = JSON.parse(raw) as StagedWarnings;
    if (!staged || staged.run_id !== runId || !Array.isArray(staged.warnings)) {
      return [];
    }
    sessionStorage.removeItem(WARNINGS_KEY);
    return staged.warnings.filter((w): w is string => typeof w === "string");
  } catch {
    return [];
  }
}
