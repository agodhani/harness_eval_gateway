"use client";

/**
 * F1 — submission form: task prompt + participant-model rows + harness
 * config matrix + evaluator model + budgets → POST /api/runs → redirect to
 * run detail.
 *
 * Participant models are a row list: each row is a provider select (which
 * upstream catalog its picker browses — informational only; the run's
 * actual provider routing resolves per model name gateway-side via
 * PROVIDER_ROUTES) plus a model ComboInput. Row 1 is the primary model
 * (→ `model_name`); "+ Add Another Model" appends participant rows
 * (→ `models`). The evaluator gets the same single-row treatment.
 *
 * Client-side validation mirrors the API contract (plan §1): task_prompt and
 * model_name required, ≥1 harness config, known adapter/skill-pack names.
 */
import { useRouter } from "next/navigation";
import { useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import Link from "next/link";
import {
  ADAPTERS,
  ApiError,
  createRun,
  DEFAULT_BUDGETS,
  getModels,
  listHarnesses,
  listPresets,
  listSkillPacks,
  NO_SANDBOX_ADAPTERS,
  PROVIDERS,
  pullModel,
  sanitizeHarnessConfigs,
  SKILL_PACKS,
  uploadArchive,
  type Budgets,
  type HarnessConfig,
  type HarnessManifest,
  type ModelOptions,
  type SkillPack,
  type SkillpackImportResult,
  type TaskInputs,
  type TaskPreset,
  type UploadOut,
} from "@/lib/api";
import { truncate } from "@/lib/format";
import { modelsPayload, participantModels } from "@/lib/modelRows";
import {
  consumeResubmit,
  stageRunWarnings,
} from "@/lib/resubmit";
import { ComboInput } from "@/components/ComboInput";
import { SavePresetDialog } from "@/components/SavePresetDialog";
import {
  Badge,
  Card,
  EmptyState,
  ErrorNote,
  InfoNote,
  Loading,
  WarnNote,
} from "@/components/ui";
import { ImportSkillPackDialog } from "./ImportSkillPackDialog";

// Offline fallback for the model pickers — the live list comes from
// GET /api/models (gateway → upstream) when the backend is reachable.
const MODEL_SUGGESTIONS = ["qwen3:8b", "qwen2.5-coder:3b", "qwen2.5-coder:7b"];
const DEFAULT_ADAPTER = "miniloop";
const MAX_CONFIGS = 8;
// Mirrors api/schemas.py MAX_MATRIX_CELLS — configs × models sandbox cap.
const MAX_MATRIX_CELLS = 32;
// RunCreate.models caps at 8 extras → at most 1 primary + 8 extra rows.
const MAX_MODEL_ROWS = 9;

/** PR27 per-config model-sampling knobs — form state stays stringly-typed
 * (empty = unset) and serializes to numbers/null in the payload. */
interface ModelOptsRow {
  temperature: string;
  seed: string;
  max_tokens: string;
  reasoning: string;
}

const BLANK_OPTS: ModelOptsRow = {
  temperature: "",
  seed: "",
  max_tokens: "",
  reasoning: "",
};

// Reasoning-effort vocabularies are adapter-scoped: codex writes the value
// verbatim into model_reasoning_effort (minimal|low|medium|high|xhigh —
// max/none fail config parse); pass-through adapters (miniloop, prompt)
// forward it to the provider, which accepts the broader set.
const DEFAULT_REASONING_EFFORTS = [
  "none",
  "minimal",
  "low",
  "medium",
  "high",
  "max",
];
const REASONING_EFFORTS: Record<string, string[]> = {
  codex: ["minimal", "low", "medium", "high", "xhigh"],
};

interface ConfigRow {
  key: number;
  name: string;
  adapter: string;
  skill_packs: string[];
  system_prompt_extra: string;
  /** PR14 per-config model pin — no picker UI, but preserved on resubmit so
   * re-submitting a run doesn't silently drop pins from the payload. */
  model?: string | null;
  /** PR27: model_options inputs (advanced disclosure per config). */
  opts: ModelOptsRow;
}

interface ModelRow {
  key: number;
  provider: string;
  model: string;
  /** User explicitly picked this row's provider — the mount-time catalog
   * fetch (which resolves the gateway's DEFAULT upstream) must not re-align
   * a picked row to that default. */
  picked?: boolean;
}

/** One upstream's cached catalog from GET /api/models?provider=. */
interface Catalog {
  models: string[];
  pullSupported: boolean;
}

let nextKey = 1;
function blankConfig(): ConfigRow {
  return {
    key: nextKey++,
    name: "",
    adapter: DEFAULT_ADAPTER,
    skill_packs: [],
    system_prompt_extra: "",
    opts: { ...BLANK_OPTS },
  };
}

function blankModelRow(provider: string): ModelRow {
  return { key: nextKey++, provider, model: "" };
}

export default function SubmitPage() {
  const router = useRouter();

  const [taskPrompt, setTaskPrompt] = useState("");
  // PR22 task source: "custom" (freeform textarea — the original shape) or
  // "preset" (a library entry's body; preset_ref rides on the submit and the
  // server snapshots the resolved revision). taskPrompt stays live in both
  // modes — it's the "edit as custom copy" target and the resubmit fallback.
  const [taskMode, setTaskMode] = useState<"custom" | "preset">("custom");
  // Selected preset slug. A slug absent from the loaded library is NOT
  // auto-detached — `presetMissing` surfaces it and validation forces a
  // re-pick (submitting it would 422 anyway); an unloaded library leaves
  // it submittable (offline resubmit — the server resolves it).
  const [presetSlug, setPresetSlug] = useState<string | null>(null);
  const [presets, setPresets] = useState<TaskPreset[] | null>(null);
  const [presetsFailed, setPresetsFailed] = useState(false);
  const [presetQuery, setPresetQuery] = useState("");
  const [presetCategory, setPresetCategory] = useState("");
  const [presetNotice, setPresetNotice] = useState<string | null>(null);
  const [savePresetOpen, setSavePresetOpen] = useState(false);
  // Participant-model rows — row 1 is the required primary (→ model_name);
  // extra rows are the PR14 additional models (→ models, deduped). Model
  // starts EMPTY: prefilling a provider-specific name (e.g. an ollama tag)
  // before the mount catalog resolves the gateway's default provider
  // guarantees a provider/model mismatch (provider realigns, the name
  // doesn't). The row's provider is also a placeholder — the mount fetch
  // re-aligns every unpicked row to the gateway default.
  const [modelRows, setModelRows] = useState<ModelRow[]>(() => [
    { key: nextKey++, provider: "ollama", model: "" },
  ]);
  const [evalRow, setEvalRow] = useState<ModelRow>(() =>
    blankModelRow("ollama"),
  );
  // Gateway's resolved default provider — learned from the no-param mount
  // fetch; new rows + unpicked selects track it.
  const [defaultProvider, setDefaultProvider] = useState("ollama");
  // Live model catalogs keyed by provider — GET /api/models?provider=
  // (gateway → that upstream). A missing entry (never fetched / failed)
  // → MODEL_SUGGESTIONS fallback.
  const [catalogs, setCatalogs] = useState<Record<string, Catalog>>({});
  const catalogsInflight = useRef(new Set<string>());
  const [configs, setConfigs] = useState<ConfigRow[]>(() => [blankConfig()]);
  const [budgets, setBudgets] = useState<Budgets>({ ...DEFAULT_BUDGETS });
  // PR27: repeats per matrix cell (1–8) — every config × model cell runs
  // this many times and results report mean ± sd (n).
  const [trials, setTrials] = useState(1);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});
  // Advisory warnings on the run-create response (new backend field —
  // absent on older backends). Stashed for the run detail page before we
  // navigate; also rendered here while the push is in flight.
  const [warnings, setWarnings] = useState<string[]>([]);
  // Harness registry from GET /api/harnesses; static ADAPTERS is the fallback
  // when the API isn't running (page convention: degrade gracefully).
  const [harnesses, setHarnesses] = useState<HarnessManifest[]>([]);
  // ollama pull ("install model") state — pullMsg doubles as progress text.
  const [pullName, setPullName] = useState("");
  const [pulling, setPulling] = useState(false);
  const [pullMsg, setPullMsg] = useState<string | null>(null);
  const [pullFailed, setPullFailed] = useState(false);
  // PR24 — task inputs (the seeded workspace). `none` = legacy empty
  // workspace; `repo` clones a public https repo at `repoRef` (empty =
  // remote HEAD); `archive` submits a staged upload id (files POST to
  // /api/uploads the moment they're picked — uploadToken guards the race
  // when a second pick lands before the first finishes).
  const [inputsMode, setInputsMode] = useState<"none" | "repo" | "archive">(
    "none",
  );
  const [repoUrl, setRepoUrl] = useState("");
  const [repoRef, setRepoRef] = useState("");
  const [stagedUpload, setStagedUpload] = useState<UploadOut | null>(null);
  const [uploading, setUploading] = useState(false);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const uploadToken = useRef(0);

  /** Model ids one row's picker suggests — live catalog for its provider,
   * MODEL_SUGGESTIONS when that catalog is missing/empty. */
  const suggestionsFor = (provider: string): string[] => {
    const cat = catalogs[provider];
    return cat && cat.models.length ? cat.models : [...MODEL_SUGGESTIONS];
  };

  /** Fetch + cache one provider's catalog (in-flight deduped). */
  function ensureCatalog(provider: string) {
    if (catalogs[provider] !== undefined) return;
    if (catalogsInflight.current.has(provider)) return;
    catalogsInflight.current.add(provider);
    getModels(provider)
      .then((r) => {
        const resolved = r.provider ?? provider;
        setCatalogs((c) => ({
          ...c,
          [resolved]: {
            models: r.models.map((m) => m.id),
            pullSupported: r.pull_supported,
          },
        }));
      })
      .catch(() => {
        /* provider unreachable/unkeyed — MODEL_SUGGESTIONS fallback stays */
      })
      .finally(() => {
        catalogsInflight.current.delete(provider);
      });
  }

  // Mount: fetch the gateway's DEFAULT provider catalog (no param → the
  // resolved upstream stamps x-heg-provider on the response) and align every
  // row whose provider the user hasn't explicitly picked to that default, so
  // the select names the upstream actually serving its suggestions.
  useEffect(() => {
    let cancelled = false;
    getModels()
      .then((r) => {
        if (cancelled) return;
        const provider = r.provider ?? "ollama";
        setCatalogs((c) => ({
          ...c,
          [provider]: {
            models: r.models.map((m) => m.id),
            pullSupported: r.pull_supported,
          },
        }));
        setDefaultProvider(provider);
        setModelRows((rs) =>
          rs.map((row) => (row.picked ? row : { ...row, provider })),
        );
        setEvalRow((e) => (e.picked ? e : { ...e, provider }));
      })
      .catch(() => {
        /* offline fallback: static MODEL_SUGGESTIONS */
      });
    return () => {
      cancelled = true;
    };
  }, []);

  // Resubmit handoff: run detail/results "Resubmit" stages the spec in
  // sessionStorage (too big for query params); consume it once on mount so
  // later plain visits to /submit open empty. Providers stay unpicked — the
  // catalog effect above realigns them to the gateway default.
  useEffect(() => {
    let cancelled = false;
    void Promise.resolve().then(() => {
      if (cancelled) return;
      const spec = consumeResubmit();
      if (!spec) return;
      setTaskPrompt(spec.task_prompt);
      // PR22: a preset-backed run resubmits AS a preset run — the ref re-
      // resolves the CURRENT revision server-side; the staged task_prompt
      // remains the "edit as custom copy" fallback.
      if (spec.preset_ref) {
        selectPreset(spec.preset_ref);
        setTaskMode("preset");
      }
      setModelRows(
        spec.models.length
          ? spec.models.map((m) => ({
              key: nextKey++,
              provider: "ollama",
              model: m,
            }))
          : [blankModelRow("ollama")],
      );
      setEvalRow({
        key: nextKey++,
        provider: "ollama",
        model: spec.evaluator_model ?? "",
      });
      setConfigs(
        spec.harness_configs.length
          ? spec.harness_configs.map((c) => ({
              key: nextKey++,
              name: c.name ?? "",
              adapter: c.adapter,
              skill_packs: [...c.skill_packs],
              system_prompt_extra: c.system_prompt_extra ?? "",
              model: c.model ?? null,
              opts: {
                temperature:
                  c.model_options?.temperature != null
                    ? String(c.model_options.temperature)
                    : "",
                seed:
                  c.model_options?.seed != null
                    ? String(c.model_options.seed)
                    : "",
                max_tokens:
                  c.model_options?.max_tokens != null
                    ? String(c.model_options.max_tokens)
                    : "",
                reasoning: c.model_options?.reasoning ?? "",
              },
            }))
          : [blankConfig()],
      );
      if (spec.budgets) setBudgets({ ...spec.budgets });
      // PR24: restore the seed — repo seeds repopulate the inputs; an
      // archive seed restores as an already-staged id (the server keeps the
      // bytes under data/uploads, so the id still resolves).
      const ti = spec.task_inputs;
      if (ti?.repo_url) {
        setInputsMode("repo");
        setRepoUrl(ti.repo_url);
        setRepoRef(ti.ref ?? "");
      } else if (ti?.archive_id) {
        setInputsMode("archive");
        setStagedUpload({
          id: ti.archive_id,
          filename: "previously staged archive",
          size: 0,
          sha256: "",
          kind: "archive",
          uploaded_at: "",
        });
      }
      if (spec.trials && spec.trials >= 1) {
        setTrials(Math.min(8, Math.trunc(spec.trials)));
      }
    });
    return () => {
      cancelled = true;
    };
  }, []);

  /** Refetch the ollama catalog — called after a successful pull. */
  async function refreshModels() {
    try {
      const r = await getModels("ollama");
      const resolved = r.provider ?? "ollama";
      setCatalogs((c) => ({
        ...c,
        [resolved]: {
          models: r.models.map((m) => m.id),
          pullSupported: r.pull_supported,
        },
      }));
    } catch {
      /* gateway/api down — existing catalog (or fallback) stays */
    }
  }

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
  /** PR25 no-sandbox pseudo-adapters (prompt): manifest flag wins, static
   * list is the offline fallback. Prompt cells run one gateway call — no
   * container, no skill packs to stage. */
  const isNoSandboxAdapter = (name: string) =>
    harnesses.length > 0
      ? harnesses.find((h) => h.name === name)?.no_sandbox === true
      : NO_SANDBOX_ADAPTERS.includes(name);
  const sandboxedAdapters = adapterNames.filter((a) => !isNoSandboxAdapter(a));
  const noSandboxAdapters = adapterNames.filter(isNoSandboxAdapter);
  /** PR27: the ModelOptions keys an adapter's manifest declares — null
   * when the manifest predates the field (don't gate the UI on it). */
  const supportedOpts = (adapter: string): string[] | null => {
    const m = harnesses.find((h) => h.name === adapter);
    return m && Array.isArray(m.model_options)
      ? (m.model_options as string[])
      : null;
  };

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

  // PR22 preset library — merged vendored + user presets. The
  // ?preset=<slug> param (detail-page "run this preset" links) is explicit
  // user intent applied on mount regardless of the fetch outcome — queued in
  // a microtask so it lands after the resubmit effect's staged ref: the URL
  // param wins. A slug absent from the loaded library isn't detached — the
  // picker surfaces the mismatch and validation makes the user re-pick or
  // switch to custom (a missing slug would 422 server-side anyway).
  useEffect(() => {
    let cancelled = false;
    const qp = new URLSearchParams(window.location.search)
      .get("preset")
      ?.trim()
      .toLowerCase();
    void Promise.resolve().then(() => {
      if (cancelled || !qp) return;
      selectPreset(qp);
      setTaskMode("preset");
    });
    listPresets()
      .then((list) => {
        if (cancelled) return;
        setPresets((prev) => {
          const fetched = new Set(list.map((p) => p.slug));
          return [...list, ...(prev ?? []).filter((p) => !fetched.has(p.slug))];
        });
      })
      .catch(() => {
        /* API down — preset mode stays selectable but unverifiable;
           a preset_ref slug still submits (server resolves it). */
        if (!cancelled) setPresetsFailed(true);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const packNames = useMemo(() => new Set(packs.map((p) => p.name)), [packs]);

  // ------------------------------------------------------------------
  // PR22 preset derived state
  // ------------------------------------------------------------------

  function selectPreset(slug: string | null) {
    setPresetSlug(slug);
    setPresetNotice(null);
  }

  const selectedPreset = useMemo(
    () => (presets ?? []).find((p) => p.slug === presetSlug) ?? null,
    [presets, presetSlug],
  );
  /** Slug set but absent from a LOADED library — the picker flags it and
   * validation forces a re-pick (submitting it would 422 anyway). */
  const presetMissing =
    taskMode === "preset" &&
    presetSlug !== null &&
    presets !== null &&
    selectedPreset === null;

  const presetCategories = useMemo(
    () =>
      [...new Set((presets ?? []).map((p) => p.category).filter(Boolean))].sort(),
    [presets],
  );
  const pq = presetQuery.trim().toLowerCase();
  const filteredPresets = useMemo(
    () =>
      (presets ?? []).filter((p) => {
        if (presetCategory && p.category !== presetCategory) return false;
        if (!pq) return true;
        return [p.title, p.slug, p.description, p.category, ...p.tags].some((h) =>
          h.toLowerCase().includes(pq),
        );
      }),
    [presets, presetCategory, pq],
  );

  /** One-click preset suggestions — fills EMPTY participant-model fields
   * with suggested_models and applies suggested_adapter (when it's a known
   * adapter) to configs still at the form default. Never overwrites a
   * field the user already filled. */
  function applyPresetSuggestions() {
    const p = selectedPreset;
    if (!p) return;
    if (p.suggested_adapter && adapterNames.includes(p.suggested_adapter)) {
      const a = p.suggested_adapter;
      setConfigs((cs) =>
        cs.map((c) =>
          !c.adapter || c.adapter === DEFAULT_ADAPTER ? { ...c, adapter: a } : c,
        ),
      );
    }
    const sugg = p.suggested_models ?? [];
    if (sugg.length > 0) {
      setModelRows((rs) => {
        const taken = new Set<string>();
        for (const r of rs) {
          const m = r.model.trim();
          if (m) taken.add(m);
        }
        const fresh: string[] = [];
        for (const s of sugg) {
          const m = s.trim();
          if (m && !taken.has(m)) {
            taken.add(m);
            fresh.push(m);
          }
        }
        if (fresh.length === 0) return rs;
        const out = [...rs];
        let si = 0;
        for (let i = 0; i < out.length && si < fresh.length; i++) {
          if (!out[i].model.trim()) out[i] = { ...out[i], model: fresh[si++] };
        }
        while (si < fresh.length && out.length < MAX_MODEL_ROWS) {
          out.push({ key: nextKey++, provider: defaultProvider, model: fresh[si++] });
        }
        return out;
      });
    }
  }

  function onPackImported(result: SkillpackImportResult) {
    const key = importFor;
    const newPacks = result.packs;
    // Refetch for accurate merged state (skills/count/origin on appends).
    listSkillPacks()
      .then(setPacks)
      .catch(() => {
        setPacks((prev) => {
          const have = new Set(prev.map((p) => p.name));
          const missing = newPacks
            .filter((r) => !have.has(r.pack))
            .map((r) => ({
              name: r.pack,
              source: "imported",
              skill_count: r.skill_count,
              skills: r.imported.map((s) => s.name),
              origin_url: null,
              imported_at: new Date().toISOString(),
            }));
          return [...prev, ...missing];
        });
      });
    // Auto-select the imported pack(s) in the config the dialog opened from
    // (split mode can produce several — all get selected).
    if (key !== null) {
      const names = newPacks.map((r) => r.pack);
      setConfigs((cs) =>
        cs.map((c) =>
          c.key === key
            ? { ...c, skill_packs: [...new Set([...c.skill_packs, ...names])] }
            : c,
        ),
      );
    }
    setImportFor(null);
  }

  // Effective participant-model set: primary first, extras deduped against
  // it and each other (mirrors orchestrator.matrix.participant_models).
  const participants = useMemo(() => participantModels(modelRows), [modelRows]);
  const primaryModel = modelRows[0]?.model.trim() ?? "";
  const modelCount = participants.length;
  const sandboxCount = configs.length * Math.max(modelCount, 1) * trials;

  const effectiveEvaluator = evalRow.model.trim() || primaryModel;
  // PR14: flagged when the judge equals ANY participant model —
  // non-independent judging for that matrix cell.
  const sameModelEval =
    effectiveEvaluator !== "" && participants.includes(effectiveEvaluator);

  const canAddConfig = configs.length < MAX_CONFIGS;
  // Row cap: 8 extras max (RunCreate.models bound) AND adding one more
  // model must not blow the configs × models matrix cap.
  const canAddModel =
    modelRows.length < MAX_MODEL_ROWS &&
    configs.length * (modelCount + 1) * trials <= MAX_MATRIX_CELLS;
  const canAddModelHint =
    modelRows.length >= MAX_MODEL_ROWS
      ? `max ${MAX_MODEL_ROWS} participant models`
      : `matrix cap — ${MAX_MATRIX_CELLS} sandboxes max`;

  // The ollama install control only makes sense while a row actually picks
  // from ollama AND that upstream reported pull support — a non-ollama row
  // (or an unkeyed/unreachable ollama) hides it.
  const ollamaInUse =
    modelRows.some((r) => r.provider === "ollama") ||
    evalRow.provider === "ollama";
  const pullVisible = ollamaInUse && (catalogs.ollama?.pullSupported ?? false);

  const validation = useMemo(() => {
    const errs: Record<string, string> = {};
    if (taskMode === "custom") {
      if (!taskPrompt.trim()) errs.task_prompt = "Task prompt is required.";
    } else if (!presetSlug) {
      errs.task = "Pick a preset or switch to a custom prompt.";
    } else if (presetMissing) {
      errs.task = `Preset "${presetSlug}" isn't in the library — pick another or switch to a custom prompt.`;
    }
    if (!primaryModel) errs.model_name = "Model name is required.";
    if (configs.length === 0) errs.configs = "At least one harness config is required.";
    configs.forEach((c, i) => {
      if (!adapterNames.includes(c.adapter)) {
        errs[`cfg_${i}_adapter`] = `Unknown adapter "${c.adapter}".`;
      }
      if (isNoSandboxAdapter(c.adapter)) return; // no packs to stage
      for (const p of c.skill_packs) {
        if (!packNames.has(p)) errs[`cfg_${i}_packs`] = `Unknown skill pack "${p}".`;
      }
    });
    if (budgets.max_iters < 1 || budgets.max_iters > 1000)
      errs.max_iters = "max_iters must be 1–1000.";
    if (budgets.token_budget < 1000)
      errs.token_budget = "token_budget must be ≥ 1000 (per-run).";
    if (
      budgets.sandbox_token_budget != null &&
      !(Number.isFinite(budgets.sandbox_token_budget) &&
        budgets.sandbox_token_budget >= 1000)
    )
      // Number.isFinite first — "1e999" parses to Infinity, passes range
      // checks, then JSON.stringify silently emits null (cap dropped).
      errs.sandbox_token_budget = "sandbox_token_budget must be ≥ 1000 when set.";
    if (
      budgets.max_cost_usd != null &&
      !(Number.isFinite(budgets.max_cost_usd) && budgets.max_cost_usd > 0)
    )
      errs.max_cost_usd = "max_cost_usd must be > 0 when set.";
    if (budgets.wall_clock_seconds < 60 || budgets.wall_clock_seconds > 86400)
      errs.wall_clock_seconds = "wall_clock_seconds must be 60–86400.";
    if (sandboxCount > MAX_MATRIX_CELLS)
      errs.models =
        `Matrix too large: ${configs.length} configs × ${modelCount} models ` +
        `× ${trials} trials = ${sandboxCount} sandboxes (max ${MAX_MATRIX_CELLS}).`;
    // PR24 inputs: a picked mode must actually resolve to a seed — repo
    // needs a public https URL (mirrors TaskInputsIn), archive needs the
    // upload staged (file picked + POST /api/uploads succeeded).
    if (inputsMode === "repo") {
      const url = repoUrl.trim();
      if (!url) {
        errs.task_inputs =
          "Enter a repo URL, or switch Inputs back to none.";
      } else if (!url.startsWith("https://")) {
        errs.task_inputs = "Repo URL must start with https:// (public git only).";
      }
    }
    if (inputsMode === "archive" && !stagedUpload) {
      errs.task_inputs = uploading
        ? "Archive is still uploading…"
        : "Pick an archive file, or switch Inputs back to none.";
    }
    configs.forEach((c, i) => {
      const t = parseFloat(c.opts.temperature);
      if (c.opts.temperature.trim() && (!Number.isFinite(t) || t < 0 || t > 2))
        errs[`cfg_${i}_opts`] = "temperature must be 0–2.";
      const mt = Number(c.opts.max_tokens);
      if (
        c.opts.max_tokens.trim() &&
        (!Number.isInteger(mt) || mt < 1 || mt > 131_072)
      )
        errs[`cfg_${i}_opts`] = "max_tokens must be an integer 1–131072.";
      const sd = Number(c.opts.seed);
      if (c.opts.seed.trim() && (!Number.isInteger(sd) || sd < 0))
        errs[`cfg_${i}_opts`] = "seed must be an integer ≥ 0.";
    });
    return errs;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [taskMode, taskPrompt, presetSlug, presetMissing, primaryModel, configs, budgets, adapterNames, packNames, sandboxCount, modelCount, trials, inputsMode, repoUrl, stagedUpload, uploading]);

  const isValid = Object.keys(validation).length === 0;

  function updateConfig(key: number, patch: Partial<ConfigRow>) {
    setConfigs((cs) => cs.map((c) => (c.key === key ? { ...c, ...patch } : c)));
  }

  function updateModelRow(key: number, patch: Partial<ModelRow>) {
    setModelRows((rs) =>
      rs.map((r) => (r.key === key ? { ...r, ...patch } : r)),
    );
  }

  function pickProvider(key: number, provider: string) {
    updateModelRow(key, { provider, picked: true });
    ensureCatalog(provider);
  }

  function pickEvalProvider(provider: string) {
    setEvalRow((e) => ({ ...e, provider, picked: true }));
    ensureCatalog(provider);
  }

  function addModelRow() {
    if (!canAddModel) return;
    setModelRows((rs) => [...rs, blankModelRow(defaultProvider)]);
  }

  function removeModelRow(key: number) {
    setModelRows((rs) => (rs.length > 1 ? rs.filter((r) => r.key !== key) : rs));
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

  /** ollama pull via POST /api/models/pull — streams progress into pullMsg. */
  async function onPull() {
    const name = pullName.trim();
    if (!name || pulling) return;
    setPulling(true);
    setPullFailed(false);
    setPullMsg("starting pull…");
    try {
      const last = await pullModel(name, (ev) => {
        const pct =
          ev.total && ev.completed != null
            ? ` ${Math.min(100, Math.round((100 * ev.completed) / ev.total))}%`
            : "";
        setPullMsg(`${ev.status ?? "pulling"}${pct}`);
      });
      setPullMsg(
        last?.status === "success"
          ? `installed ${name} — pick it from the model dropdown.`
          : `pull finished (${last?.status ?? "done"})`,
      );
      setPullName("");
      void refreshModels();
    } catch (err) {
      setPullFailed(true);
      setPullMsg(
        err instanceof ApiError
          ? err.detail
          : err instanceof Error
            ? err.message
            : "Pull failed.",
      );
    } finally {
      setPulling(false);
    }
  }

  function removeConfig(key: number) {
    setConfigs((cs) => (cs.length > 1 ? cs.filter((c) => c.key !== key) : cs));
  }

  /** PR24: stage the picked archive the moment it's chosen — POST
   * /api/uploads keeps only safe zip/tar member sets. */
  async function onSeedFile(file: File | null) {
    if (!file) return;
    const token = ++uploadToken.current;
    setUploading(true);
    setUploadError(null);
    try {
      const out = await uploadArchive(file);
      if (uploadToken.current === token) setStagedUpload(out);
    } catch (err) {
      if (uploadToken.current === token) {
        setStagedUpload(null);
        setUploadError(
          err instanceof ApiError
            ? err.detail
            : err instanceof Error
              ? err.message
              : "Upload failed.",
        );
      }
    } finally {
      if (uploadToken.current === token) setUploading(false);
    }
  }

  function clearSeedUpload() {
    uploadToken.current += 1; // cancel any in-flight upload
    setStagedUpload(null);
    setUploadError(null);
    setUploading(false);
  }

  /** Effective task_inputs for createRun — undefined on `none`. */
  function taskInputsPayload(): TaskInputs | undefined {
    if (inputsMode === "repo") {
      const url = repoUrl.trim();
      const ref = repoRef.trim();
      return url ? { repo_url: url, ...(ref ? { ref } : {}) } : undefined;
    }
    if (inputsMode === "archive" && stagedUpload) {
      return { archive_id: stagedUpload.id };
    }
    return undefined;
  }

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    setFieldErrors(validation);
    if (!isValid || submitting) return;

    const harness_configs: HarnessConfig[] = sanitizeHarnessConfigs(
      configs.map((c) => {
        // PR27: only provided knobs ship; the backend warns + drops keys
        // the adapter doesn't declare in its manifest's model_options.
        const opts: ModelOptions = {};
        if (c.opts.temperature.trim())
          opts.temperature = parseFloat(c.opts.temperature);
        if (c.opts.seed.trim()) opts.seed = Number(c.opts.seed);
        if (c.opts.max_tokens.trim())
          opts.max_tokens = Number(c.opts.max_tokens);
        if (c.opts.reasoning.trim()) opts.reasoning = c.opts.reasoning.trim();
        return {
          adapter: c.adapter,
          skill_packs: c.skill_packs,
          system_prompt_extra: c.system_prompt_extra.trim() || null,
          name: c.name.trim() || null,
          // Per-config model pin (PR14) — only ever set by a resubmit
          // restore; the form itself has no pin UI.
          model: c.model ?? null,
          model_options: Object.keys(opts).length ? opts : null,
        };
      }),
      isNoSandboxAdapter,
    );
    // Row 1 → model_name; remaining deduped rows → models (null when
    // single-model). Providers stay client-side — RunCreate has no
    // per-model provider field; routing resolves gateway-side.
    const { model_name, models } = modelsPayload(modelRows);

    setSubmitting(true);
    try {
      const run = await createRun({
        // Preset mode sends the selected preset's body (the server replaces
        // task_prompt with the preset verbatim anyway — it must just be
        // non-empty for the schema); a slug staged offline falls back to the
        // textarea's staged text. preset_ref carries the provenance.
        task_prompt:
          taskMode === "preset"
            ? (selectedPreset?.task_prompt ?? taskPrompt).trim() ||
              "(resolved server-side from preset)"
            : taskPrompt.trim(),
        preset_ref: taskMode === "preset" ? presetSlug : null,
        model_name,
        models,
        harness_configs,
        evaluator_model: evalRow.model.trim() || null,
        budgets,
        task_inputs: taskInputsPayload() ?? null,
        trials,
      });
      // Run-create warnings (new backend field — absent on older backends):
      // stash them so the run detail page can surface them once we land,
      // and keep them on screen here while the navigation is in flight.
      const w = Array.isArray(run.warnings)
        ? run.warnings.filter((x): x is string => typeof x === "string")
        : [];
      if (w.length) {
        setWarnings(w);
        stageRunWarnings(run.id, w);
      }
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
          <span className="kicker"><b>01</b> — experiment spec</span>
          <h1>Submit a run</h1>
          <p className="subtitle">
            One task, N harness configurations × M models — evaluated side by side.
          </p>
        </div>
      </div>

      <form onSubmit={onSubmit} className="form-grid" noValidate>
        <Card
          title="Task"
          actions={
            <div className="row" role="group" aria-label="Task source">
              <button
                type="button"
                className={`filter-chip${taskMode === "custom" ? " is-active" : ""}`}
                onClick={() => {
                  setTaskMode("custom");
                  setPresetNotice(null);
                }}
                disabled={submitting}
              >
                custom prompt
              </button>
              <button
                type="button"
                className={`filter-chip${taskMode === "preset" ? " is-active" : ""}`}
                onClick={() => {
                  setTaskMode("preset");
                  setPresetNotice(null);
                }}
                disabled={submitting}
              >
                task preset
              </button>
            </div>
          }
        >
          {presetNotice ? <InfoNote>{presetNotice}</InfoNote> : null}
          {taskMode === "custom" ? (
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
              <div className="row">
                <button
                  type="button"
                  className="btn btn--sm"
                  onClick={() => setSavePresetOpen(true)}
                  disabled={submitting || !taskPrompt.trim()}
                  title="Save this prompt to the preset library"
                >
                  save as preset
                </button>
                <span className="field__hint" style={{ alignSelf: "center" }}>
                  …or{" "}
                  <Link href="/presets">browse the preset library</Link> to run a
                  saved task verbatim.
                </span>
              </div>
            </div>
          ) : (
            <div className="field">
              <label className="field__label">
                Task preset <span className="faint">(the body runs verbatim)</span>
              </label>
              {presets !== null && presets.length > 0 ? (
                <>
                  <input
                    className="input"
                    type="search"
                    placeholder="Filter: title, slug, tag…"
                    aria-label="Filter presets"
                    value={presetQuery}
                    onChange={(e) => setPresetQuery(e.target.value)}
                    disabled={submitting}
                    style={{ marginBottom: "0.4rem" }}
                  />
                  <div
                    className="filter-bar"
                    style={{ border: "none", padding: "0 0 0.4rem" }}
                    role="group"
                    aria-label="Filter by category"
                  >
                    <button
                      type="button"
                      className={`filter-chip${presetCategory === "" ? " is-active" : ""}`}
                      onClick={() => setPresetCategory("")}
                    >
                      all
                    </button>
                    {presetCategories.map((c) => (
                      <button
                        key={c}
                        type="button"
                        className={`filter-chip${presetCategory === c ? " is-active" : ""}`}
                        onClick={() =>
                          setPresetCategory(presetCategory === c ? "" : c)
                        }
                      >
                        {c}
                      </button>
                    ))}
                  </div>
                  {filteredPresets.length === 0 ? (
                    <EmptyState>No presets match — adjust the filters.</EmptyState>
                  ) : (
                    <div
                      className="preset-pick"
                      role="listbox"
                      aria-label="Task presets"
                      aria-activedescendant={presetSlug ? `preset-${presetSlug}` : undefined}
                    >
                      {filteredPresets.map((p) => (
                        <button
                          type="button"
                          key={p.slug}
                          id={`preset-${p.slug}`}
                          role="option"
                          aria-selected={presetSlug === p.slug}
                          className={`preset-item${presetSlug === p.slug ? " preset-item--active" : ""}`}
                          onClick={() => selectPreset(p.slug)}
                          disabled={submitting}
                        >
                          <span className="preset-item__head">
                            <span className="preset-item__title">{p.title}</span>
                            <Badge tone={p.source === "vendored" ? "violet" : "blue"}>
                              {p.source}
                            </Badge>
                            {p.category ? <Badge tone="outline">{p.category}</Badge> : null}
                          </span>
                          {p.description ? (
                            <span className="preset-item__desc">
                              {truncate(p.description, 110)}
                            </span>
                          ) : null}
                          <span className="preset-item__meta">
                            <code>{p.slug}</code>
                            {" · "}
                            {p.runs_count} run{p.runs_count === 1 ? "" : "s"}
                            {p.last_run_mean != null
                              ? ` · last mean ${p.last_run_mean}`
                              : ""}
                          </span>
                        </button>
                      ))}
                    </div>
                  )}
                </>
              ) : presets === null ? (
                presetsFailed ? (
                  <WarnNote>
                    Preset library unavailable — a preset staged via a ?preset=
                    link or resubmit still submits (the server resolves the
                    slug); otherwise switch to a custom prompt.
                  </WarnNote>
                ) : (
                  <Loading label="Loading presets…" />
                )
              ) : (
                <EmptyState>
                  No presets in the library — switch to a custom prompt.
                </EmptyState>
              )}

              {selectedPreset ? (
                <div className="field" style={{ marginTop: "0.6rem" }}>
                  <label className="field__label" htmlFor="preset_preview">
                    Selected prompt{" "}
                    <span className="faint">
                      (read-only — rev {selectedPreset.revision}; runs snapshot this body)
                    </span>
                  </label>
                  <textarea
                    id="preset_preview"
                    className="textarea"
                    rows={6}
                    readOnly
                    value={selectedPreset.task_prompt}
                  />
                  <div className="row" style={{ justifyContent: "space-between" }}>
                    <span className="field__hint" style={{ alignSelf: "center" }}>
                      <Link href={`/presets/${selectedPreset.slug}`}>
                        preset detail →
                      </Link>
                    </span>
                    <button
                      type="button"
                      className="btn btn--sm"
                      onClick={() => {
                        setTaskPrompt(selectedPreset.task_prompt);
                        selectPreset(null);
                        setTaskMode("custom");
                      }}
                      disabled={submitting}
                      title="Detach the ref and edit this prompt as a one-off"
                    >
                      edit as custom copy
                    </button>
                  </div>
                  {selectedPreset.suggested_adapter ||
                  selectedPreset.suggested_models.length ? (
                    <InfoNote>
                      This preset suggests{" "}
                      {selectedPreset.suggested_adapter ? (
                        <>
                          adapter <code>{selectedPreset.suggested_adapter}</code>
                        </>
                      ) : null}
                      {selectedPreset.suggested_adapter &&
                      selectedPreset.suggested_models.length
                        ? " and "
                        : ""}
                      {selectedPreset.suggested_models.length ? (
                        <>
                          model
                          {selectedPreset.suggested_models.length > 1 ? "s" : ""}{" "}
                          <code>{selectedPreset.suggested_models.join(", ")}</code>
                        </>
                      ) : null}
                      .{" "}
                      <button
                        type="button"
                        className="btn btn--sm btn--ghost"
                        onClick={applyPresetSuggestions}
                        disabled={submitting}
                      >
                        apply to empty fields
                      </button>
                    </InfoNote>
                  ) : null}
                </div>
              ) : presetSlug ? (
                <WarnNote>
                  Preset <code>{presetSlug}</code>
                  {presets !== null
                    ? " isn't in the library — pick another or switch to a custom prompt."
                    : " — the server resolves it at submit."}
                </WarnNote>
              ) : null}
              {fieldErrors.task ? (
                <span className="field__error">{fieldErrors.task}</span>
              ) : null}
            </div>
          )}

          {/* PR24 — task inputs: seed every sandbox's workspace from a
              public repo or a staged archive before the agent starts. */}
          <div className="field" style={{ marginTop: "0.9rem" }}>
            <label className="field__label" htmlFor="inputs_mode">
              Inputs <span className="faint">(optional — seeds the workspace)</span>
            </label>
            <select
              id="inputs_mode"
              className="select"
              value={inputsMode}
              onChange={(e) =>
                setInputsMode(e.target.value as "none" | "repo" | "archive")
              }
              disabled={submitting}
            >
              <option value="none">none — empty workspace</option>
              <option value="repo">git repo (public, https)</option>
              <option value="archive">archive upload (.zip / .tar*)</option>
            </select>

            {inputsMode === "repo" ? (
              <div className="stack" style={{ marginTop: "0.6rem", gap: "0.5rem" }}>
                <input
                  id="task_inputs_repo"
                  className="input input--mono"
                  placeholder="https://github.com/owner/repo"
                  value={repoUrl}
                  onChange={(e) => setRepoUrl(e.target.value)}
                  disabled={submitting}
                  maxLength={2000}
                />
                <input
                  id="task_inputs_ref"
                  className="input input--mono"
                  placeholder="ref — branch, tag, or sha (empty = default branch)"
                  value={repoRef}
                  onChange={(e) => setRepoRef(e.target.value)}
                  disabled={submitting}
                  maxLength={255}
                />
                {fieldErrors.task_inputs ? (
                  <span className="field__error">{fieldErrors.task_inputs}</span>
                ) : (
                  <span className="field__hint">
                    Shallow-cloned at the ref — every sandbox starts from
                    identical bytes (a seed hash is recorded per cell).
                  </span>
                )}
              </div>
            ) : null}

            {inputsMode === "archive" ? (
              <div style={{ marginTop: "0.6rem" }}>
                {stagedUpload ? (
                  <div className="row" style={{ flexWrap: "nowrap" }}>
                    <Badge tone="teal" title={`sha256 ${stagedUpload.sha256}`}>
                      {stagedUpload.filename}
                      {stagedUpload.size > 0
                        ? ` · ${(stagedUpload.size / 1024).toFixed(1)} KiB`
                        : ""}
                    </Badge>
                    <button
                      type="button"
                      className="btn btn--sm btn--ghost"
                      onClick={clearSeedUpload}
                      disabled={submitting}
                    >
                      remove
                    </button>
                  </div>
                ) : (
                  <input
                    id="task_inputs_file"
                    className="input"
                    type="file"
                    accept=".zip,.tar,.tar.gz,.tgz,.tar.bz2,.tbz2,.tar.xz,.txz"
                    onChange={(e) => {
                      const f = e.target.files?.[0] ?? null;
                      e.target.value = ""; // same file re-picked must re-upload
                      void onSeedFile(f);
                    }}
                    disabled={submitting || uploading}
                  />
                )}
                {uploading ? (
                  <span className="field__hint">
                    <span className="spinner" aria-hidden /> staging archive…
                  </span>
                ) : uploadError ? (
                  <span className="field__error">{uploadError}</span>
                ) : fieldErrors.task_inputs ? (
                  <span className="field__error">{fieldErrors.task_inputs}</span>
                ) : !stagedUpload ? (
                  <span className="field__hint">
                    Zip or tar — staged via POST /api/uploads; unsafe members
                    (traversal, links) are rejected server-side.
                  </span>
                ) : (
                  <span className="field__hint">
                    Extracted into every sandbox&apos;s workspace before the
                    agent starts (a seed hash is recorded per cell).
                  </span>
                )}
              </div>
            ) : null}
          </div>
        </Card>

        <Card title="Models">
          <div className="field">
            <label className="field__label">
              Participant models{" "}
              <span className="faint">(participant 1 is the primary — required)</span>
            </label>
            <div
              className="model-rows"
              role="group"
              aria-label="Participant models"
            >
              {modelRows.map((row, i) => (
                <div className="model-row" key={row.key}>
                  <span className="model-row__label">participant {i + 1}</span>
                  <select
                    className="select"
                    aria-label={`participant ${i + 1} provider`}
                    title="Which upstream's catalog to browse — informational only"
                    value={row.provider}
                    onChange={(e) => pickProvider(row.key, e.target.value)}
                    disabled={submitting}
                  >
                    {PROVIDERS.map((p) => (
                      <option key={p} value={p}>
                        {p}
                      </option>
                    ))}
                  </select>
                  <ComboInput
                    id={i === 0 ? "model_name" : `model_${i + 1}`}
                    value={row.model}
                    onChange={(v) => updateModelRow(row.key, { model: v })}
                    suggestions={suggestionsFor(row.provider)}
                    placeholder={i === 0 ? "qwen3:8b" : "model id…"}
                    disabled={submitting}
                    mono
                  />
                  <button
                    type="button"
                    className="btn btn--sm btn--ghost"
                    onClick={() => removeModelRow(row.key)}
                    // Row 1 keeps the slot (alignment) but can't be removed —
                    // the primary model is required.
                    disabled={submitting || i === 0}
                    style={i === 0 ? { visibility: "hidden" } : undefined}
                    tabIndex={i === 0 ? -1 : undefined}
                    aria-hidden={i === 0 || undefined}
                  >
                    remove
                  </button>
                </div>
              ))}
            </div>
            <div className="row">
              <button
                type="button"
                className="btn btn--sm"
                onClick={addModelRow}
                disabled={submitting || !canAddModel}
              >
                + Add Another Model
              </button>
              {!canAddModel ? (
                <span className="field__hint">{canAddModelHint}</span>
              ) : null}
            </div>
            {fieldErrors.model_name ? (
              <span className="field__error">{fieldErrors.model_name}</span>
            ) : null}
            {fieldErrors.models ? (
              <span className="field__error">{fieldErrors.models}</span>
            ) : null}
            <span className="field__hint">
              {modelCount > 1 ? (
                <>
                  <strong>
                    {modelCount} models × {configs.length} configs
                    {trials > 1 ? ` × ${trials} trials` : ""} = {sandboxCount}{" "}
                    sandboxes
                  </strong>{" "}
                  — every config runs {trials > 1 ? `${trials}× ` : "once "}per
                  model.{" "}
                </>
              ) : (
                "Add another model to compare harness AND model in one run — each config runs once per model. "
              )}
              A row&apos;s provider only picks which upstream catalog to browse —
              the payload sends just the model name; actual routing resolves per
              model via the gateway&apos;s PROVIDER_ROUTES.
            </span>
          </div>

          <div className="field" style={{ marginTop: "0.9rem" }}>
            <label className="field__label" htmlFor="trials">
              Trials per cell <span className="faint">(repeats)</span>
            </label>
            <div className="row" style={{ flexWrap: "nowrap" }}>
              <button
                type="button"
                className="btn btn--sm"
                aria-label="Fewer trials"
                disabled={submitting || trials <= 1}
                onClick={() => setTrials((t) => Math.max(1, t - 1))}
              >
                −
              </button>
              <input
                id="trials"
                className="input"
                type="number"
                min={1}
                max={8}
                step={1}
                style={{ width: "4.5rem", textAlign: "center" }}
                value={trials}
                onChange={(e) => {
                  const n = Number(e.target.value);
                  if (Number.isInteger(n)) setTrials(Math.min(8, Math.max(1, n)));
                }}
                disabled={submitting}
              />
              <button
                type="button"
                className="btn btn--sm"
                aria-label="More trials"
                disabled={submitting || trials >= 8}
                onClick={() => setTrials((t) => Math.min(8, t + 1))}
              >
                +
              </button>
              <span className="field__hint" style={{ margin: 0 }}>
                {trials > 1
                  ? `each config × model cell repeats ${trials}× — results report mean ± sd (n=${trials})`
                  : "1 = every matrix cell runs once (single sample)"}
              </span>
            </div>
            {trials > 1 ? (
              <span className="field__hint">
                <strong>
                  {configs.length} configs × {modelCount} models × {trials}{" "}
                  trials = {sandboxCount} sandboxes
                </strong>{" "}
                — trials of one cell share a grouped header on the results
                matrix.
              </span>
            ) : null}
          </div>

          <div className="field" style={{ marginTop: "0.9rem" }}>
            <label className="field__label" htmlFor="evaluator_model">
              Evaluator model
            </label>
            <div className="model-row">
              <span className="model-row__label">evaluator</span>
              <select
                className="select"
                aria-label="evaluator provider"
                title="Which upstream's catalog to browse — informational only"
                value={evalRow.provider}
                onChange={(e) => pickEvalProvider(e.target.value)}
                disabled={submitting}
              >
                {PROVIDERS.map((p) => (
                  <option key={p} value={p}>
                    {p}
                  </option>
                ))}
              </select>
              <ComboInput
                id="evaluator_model"
                value={evalRow.model}
                onChange={(v) => setEvalRow((e) => ({ ...e, model: v }))}
                suggestions={suggestionsFor(evalRow.provider)}
                placeholder={primaryModel || "same as participant"}
                disabled={submitting}
                mono
              />
            </div>
            {sameModelEval ? (
              <InfoNote>
                Evaluator uses the <strong>same model</strong> as a participant
                {effectiveEvaluator ? (
                  <>
                    {" "}
                    (<code>{effectiveEvaluator}</code>)
                  </>
                ) : null}{" "}
                — scores may not be fully independent. This is allowed; the
                results page flags it.
              </InfoNote>
            ) : (
              <span className="field__hint">
                Leave empty to reuse the primary participant model.
              </span>
            )}
          </div>

          {pullVisible ? (
            <div className="field" style={{ marginTop: "0.9rem" }}>
              <label className="field__label" htmlFor="pull_model">
                Install model <span className="faint">(ollama pull)</span>
              </label>
              <div className="row" style={{ flexWrap: "nowrap" }}>
                <input
                  id="pull_model"
                  className="input input--mono"
                  placeholder="name:tag — e.g. llama3.2:3b"
                  value={pullName}
                  onChange={(e) => setPullName(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") {
                      e.preventDefault();
                      void onPull();
                    }
                  }}
                  disabled={pulling || submitting}
                  maxLength={255}
                />
                <button
                  type="button"
                  className="btn"
                  onClick={() => void onPull()}
                  disabled={pulling || submitting || !pullName.trim()}
                >
                  {pulling ? (
                    <>
                      <span className="spinner" aria-hidden /> pulling…
                    </>
                  ) : (
                    "pull"
                  )}
                </button>
              </div>
              {pullMsg ? (
                pullFailed ? (
                  <span className="field__error">{pullMsg}</span>
                ) : (
                  <span className="field__hint">{pullMsg}</span>
                )
              ) : (
                <span className="field__hint">
                  Downloads a model onto the gateway&apos;s Ollama upstream; progress streams here.
                </span>
              )}
            </div>
          ) : null}
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
                    <label className="field__label">
                      Name <span className="faint">(optional)</span>
                    </label>
                    <input
                      className="input"
                      value={cfg.name}
                      maxLength={64}
                      placeholder={`${cfg.adapter}-<id>`}
                      onChange={(e) => updateConfig(cfg.key, { name: e.target.value })}
                      disabled={submitting}
                      title="Label shown in the timeline filter, results matrix, and analytics"
                    />
                  </div>
                  <div className="field">
                    <label className="field__label">Adapter</label>
                    <select
                      className="select"
                      value={cfg.adapter}
                      onChange={(e) => updateConfig(cfg.key, { adapter: e.target.value })}
                      disabled={submitting}
                    >
                      <optgroup label="Agent harnesses">
                        {sandboxedAdapters.map((a) => (
                          <option key={a} value={a}>
                            {adapterLabel(a)}
                          </option>
                        ))}
                      </optgroup>
                      {noSandboxAdapters.length > 0 ? (
                        <optgroup label="No sandbox">
                          {noSandboxAdapters.map((a) => (
                            <option key={a} value={a}>
                              {adapterLabel(a)}
                            </option>
                          ))}
                        </optgroup>
                      ) : null}
                    </select>
                    {isNoSandboxAdapter(cfg.adapter) ? (
                      <span className="field__hint">
                        Prompt cell — one model call per cell, no container.
                      </span>
                    ) : null}
                  </div>
                  {!isNoSandboxAdapter(cfg.adapter) ? (
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
                  ) : null}
                </div>
                <div className="field">
                  <label className="field__label">System prompt extra</label>
                  <textarea
                    className="textarea"
                    rows={2}
                    placeholder={
                      isNoSandboxAdapter(cfg.adapter)
                        ? "Sent as the system message of the cell's single model call (a comparison axis)."
                        : "Optional instructions appended to the harness system prompt (a comparison axis)."
                    }
                    value={cfg.system_prompt_extra}
                    onChange={(e) => updateConfig(cfg.key, { system_prompt_extra: e.target.value })}
                    disabled={submitting}
                  />
                </div>
                <details className="advanced">
                  <summary>
                    Model options{" "}
                    <span className="faint">
                      {(() => {
                        const sup = supportedOpts(cfg.adapter);
                        return sup === null
                          ? "(adapter-declared support unknown)"
                          : sup.length
                            ? `(adapter maps: ${sup.join(", ")})`
                            : "(adapter maps none — dropped with a warning)";
                      })()}
                    </span>
                  </summary>
                  <div className="advanced__body">
                    <div className="input-row">
                      {(
                        [
                          ["temperature", "Temperature", "0–2", "0.7"],
                          ["seed", "Seed", "integer ≥ 0", "42"],
                          ["max_tokens", "Max tokens", "≥ 1", "8192"],
                        ] as const
                      ).map(([keyName, label, hint, placeholder]) => {
                        const sup = supportedOpts(cfg.adapter);
                        const unsupported =
                          sup !== null && !sup.includes(keyName);
                        return (
                          <div className="field" key={keyName}>
                            <label className="field__label">{label}</label>
                            <input
                              className="input"
                              type="number"
                              placeholder={placeholder}
                              min={keyName === "temperature" ? 0 : keyName === "seed" ? 0 : 1}
                              max={keyName === "temperature" ? 2 : undefined}
                              step={keyName === "temperature" ? 0.1 : 1}
                              value={cfg.opts[keyName]}
                              onChange={(e) =>
                                updateConfig(cfg.key, {
                                  opts: { ...cfg.opts, [keyName]: e.target.value },
                                })
                              }
                              disabled={submitting || unsupported}
                              title={
                                unsupported
                                  ? `${cfg.adapter} doesn't map ${keyName} — the submit warns and drops it`
                                  : hint
                              }
                            />
                            {unsupported ? (
                              <span className="field__hint">
                                unsupported by {cfg.adapter} — dropped
                              </span>
                            ) : null}
                          </div>
                        );
                      })}
                      {(() => {
                        const sup = supportedOpts(cfg.adapter);
                        const unsupported =
                          sup !== null && !sup.includes("reasoning");
                        const efforts =
                          REASONING_EFFORTS[cfg.adapter] ??
                          DEFAULT_REASONING_EFFORTS;
                        const options =
                          cfg.opts.reasoning &&
                          !efforts.includes(cfg.opts.reasoning)
                            ? [...efforts, cfg.opts.reasoning]
                            : efforts;
                        return (
                          <div className="field">
                            <label className="field__label">Reasoning</label>
                            <select
                              className="select"
                              value={cfg.opts.reasoning}
                              onChange={(e) =>
                                updateConfig(cfg.key, {
                                  opts: { ...cfg.opts, reasoning: e.target.value },
                                })
                              }
                              disabled={submitting || unsupported}
                              title={
                                unsupported
                                  ? `${cfg.adapter} doesn't map reasoning — the submit warns and drops it`
                                  : "reasoning_effort hint passed to the model request"
                              }
                            >
                              <option value="">adapter default</option>
                              {options.map((r) => (
                                <option key={r} value={r}>
                                  {r}
                                </option>
                              ))}
                            </select>
                            {unsupported ? (
                              <span className="field__hint">
                                unsupported by {cfg.adapter} — dropped
                              </span>
                            ) : null}
                          </div>
                        );
                      })()}
                    </div>
                    {fieldErrors[`cfg_${i}_opts`] ? (
                      <span className="field__error">{fieldErrors[`cfg_${i}_opts`]}</span>
                    ) : null}
                    <span className="field__hint">
                      Honored where the adapter exposes the knob — miniloop and
                      prompt cells map all four; codex maps reasoning. Anything
                      else warns on submit instead of silently no-oping.
                    </span>
                  </div>
                </details>
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
              <div className="input-row" style={{ marginTop: "0.6rem" }}>
                <div className="field">
                  <label className="field__label" htmlFor="sandbox_token_budget">
                    Token budget per sandbox <span className="faint">(optional)</span>
                  </label>
                  <input
                    id="sandbox_token_budget"
                    className="input"
                    type="number"
                    min={1000}
                    step={1000}
                    placeholder="unset — shares the run budget"
                    value={budgets.sandbox_token_budget ?? ""}
                    onChange={(e) =>
                      setBudgets({
                        ...budgets,
                        sandbox_token_budget:
                          e.target.value === "" ? null : Number(e.target.value),
                      })
                    }
                    disabled={submitting}
                  />
                  <span className="dim-desc">
                    PR28: caps each cell&apos;s own tokens — a runaway cell starves
                    itself, not its siblings.
                  </span>
                  {fieldErrors.sandbox_token_budget ? <span className="field__error">{fieldErrors.sandbox_token_budget}</span> : null}
                </div>
                <div className="field">
                  <label className="field__label" htmlFor="max_cost_usd">
                    Max cost per sandbox (USD) <span className="faint">(optional)</span>
                  </label>
                  <input
                    id="max_cost_usd"
                    className="input"
                    type="number"
                    min={0}
                    step="any"
                    placeholder="unset — no spend cap"
                    value={budgets.max_cost_usd ?? ""}
                    onChange={(e) =>
                      setBudgets({
                        ...budgets,
                        max_cost_usd:
                          e.target.value === "" ? null : Number(e.target.value),
                      })
                    }
                    disabled={submitting}
                  />
                  <span className="dim-desc">
                    Priced models only — each cell&apos;s estimated spend at list
                    prices; unpriced models can&apos;t trip it.
                  </span>
                  {fieldErrors.max_cost_usd ? <span className="field__error">{fieldErrors.max_cost_usd}</span> : null}
                </div>
              </div>
            </div>
          </details>
        </Card>

        {error ? <ErrorNote title="Submit failed">{error}</ErrorNote> : null}
        {warnings.length > 0 ? (
          <WarnNote>
            <strong>Run created with warnings:</strong>
            <ul style={{ margin: "0.35rem 0 0 1.1rem", padding: 0 }}>
              {warnings.map((w, i) => (
                <li key={i}>{w}</li>
              ))}
            </ul>
          </WarnNote>
        ) : null}

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

      {savePresetOpen ? (
        <SavePresetDialog
          taskPrompt={taskPrompt}
          initialTitle={taskPrompt.trim().split("\n")[0]?.slice(0, 80) ?? ""}
          onClose={() => setSavePresetOpen(false)}
          onSaved={(p) => {
            setSavePresetOpen(false);
            // Add the fresh preset to the loaded library and select it —
            // the natural next step after saving.
            setPresets((prev) =>
              prev ? [...prev.filter((x) => x.slug !== p.slug), p] : [p],
            );
            selectPreset(p.slug);
            setTaskMode("preset");
            setPresetNotice(`Saved "${p.title}" — now selected as the task preset.`);
          }}
        />
      ) : null}
    </main>
  );
}
