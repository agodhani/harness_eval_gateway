/**
 * Typed client for the harness_eval_gateway control-plane API.
 *
 * Contract source: docs/orchestrator-plan.md §1 (+ api/routers/*.py, which is
 * the authoritative shape once it lands). The API may not be running or may
 * 404 while backend lanes are in flight — every call throws {@link ApiError}
 * and pages are expected to degrade gracefully.
 *
 * Base URL: NEXT_PUBLIC_API_URL env var, default http://localhost:8001.
 */

export const API_BASE: string =
  process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8001";

// ---------------------------------------------------------------------------
// Domain types — mirror api/schemas.py and api/routers/results.py
// ---------------------------------------------------------------------------

export type RunStatus =
  | "submitted"
  | "rubric_review"
  | "queued"
  | "provisioning"
  | "running"
  | "evaluating"
  | "done"
  | "failed"
  | "cancelled";

export const RUN_STATUSES: readonly RunStatus[] = [
  "submitted",
  "rubric_review",
  "queued",
  "provisioning",
  "running",
  "evaluating",
  "done",
  "failed",
  "cancelled",
];

/** Statuses after which nothing changes — polling can stop. */
export const TERMINAL_RUN_STATUSES: readonly string[] = [
  "done",
  "failed",
  "cancelled",
];

/** Statuses DELETE /api/runs/{id} cancels (nothing spawned yet). Runs in
 * provisioning/running/evaluating 409 — in-flight sandbox teardown isn't
 * supported; terminal runs hard-delete instead. */
export const CANCELLABLE_RUN_STATUSES: readonly string[] = [
  "submitted",
  "rubric_review",
  "queued",
];

export type SandboxStatus =
  | "provisioning"
  | "running"
  | "terminating"
  | "done"
  | "failed";

export type TerminalReason =
  | "agent_done"
  | "harness_complete"
  | "timeout"
  | "budget_exceeded"
  | "stuck_loop"
  | "error";

export type EventSourceName =
  | "gateway"
  | "harness"
  | "sandbox"
  | "evaluator"
  | "orchestrator";

export const EVENT_SOURCES: readonly EventSourceName[] = [
  "gateway",
  "harness",
  "sandbox",
  "evaluator",
  "orchestrator",
];

/** Known event types (api/schemas.py EventType). `(string & {})` keeps the
 * union autocomplete-friendly while allowing types added later. */
export type EventType =
  | "llm_request"
  | "llm_response"
  | "tool_call"
  | "tool_result"
  | "shell_command"
  | "file_op"
  | "test_execution"
  | "checkpoint"
  | "skill_invocation"
  | "done_signal"
  | "run_started"
  | "terminal_state"
  | "context_compacted"
  | "miniloop_result"
  | "error"
  | (string & {});

export const EVENT_TYPES: readonly string[] = [
  "llm_request",
  "llm_response",
  "tool_call",
  "tool_result",
  "shell_command",
  "file_op",
  "test_execution",
  "checkpoint",
  "skill_invocation",
  "done_signal",
  "run_started",
  "terminal_state",
  "context_compacted",
  "miniloop_result",
  "error",
];

/** Vendored skill packs under ./skillpacks (dirs only — README.md excluded).
 * Fallback for the submit form until GET /api/skillpacks responds — the API
 * list additionally carries user-imported packs. */
export const SKILL_PACKS: readonly string[] = [
  "superpowers",
  "mattpocock-skills",
  "spec-kit",
];

/** Registered harness adapters (orchestrator/adapters.py). The submit form
 * prefers live manifests from {@link listHarnesses}; this static list is the
 * offline fallback when the API is unreachable. */
export const ADAPTERS: readonly string[] = [
  "miniloop",
  "aider",
  "claude_code",
  "codex",
  "deepseek",
  "pi",
  "prompt",
];

/** PR25 pseudo-adapters that never spawn a container — offline fallback
 * for {@link HarnessManifest.no_sandbox} when the API is unreachable. */
export const NO_SANDBOX_ADAPTERS: readonly string[] = ["prompt"];

/** Adapter manifest as served by GET /api/harnesses (Phase 2.5). */
export interface HarnessManifest {
  name: string;
  display_name?: string;
  description?: string;
  llm_protocol?: string;
  cli?: string;
  cli_version?: string;
  install?: string;
  image?: string;
  requires?: string[];
  /** PR25: pseudo-adapter — the cell runs one gateway call, no container. */
  no_sandbox?: boolean;
  /** PR27: ModelOptions keys this adapter honors (absent = unknown —
   * don't gate the UI on it). */
  model_options?: string[];
  [key: string]: unknown;
}

export function listHarnesses(): Promise<HarnessManifest[]> {
  return request<HarnessManifest[]>("/api/harnesses");
}

/** Canonical rubric dimensions the evaluator scores against. */
export const CANONICAL_DIMENSIONS: readonly string[] = [
  "functional_requirements",
  "acceptance_criteria",
  "code_quality",
  "cyclomatic_complexity",
];

/** Order dimension rows: canonical rubric dimensions first, then alpha. */
export function dimensionOrder(keys: string[]): string[] {
  const canon = CANONICAL_DIMENSIONS.filter((d) => keys.includes(d));
  const rest = keys.filter((d) => !CANONICAL_DIMENSIONS.includes(d)).sort();
  return [...canon, ...rest];
}

/** `adapter · model · pack1, pack2` — mirrors analytics._sandbox_label.
 * The model segment only appears on multi-model matrix cells (PR14 — the
 * ``model`` key is injected by matrix expansion); single-model runs keep
 * the ``adapter · packs`` label. */
export function harnessConfigLabel(
  hc: Record<string, unknown> | undefined,
): string {
  const adapter = String(hc?.adapter ?? hc?.harness ?? "?");
  const parts = [adapter];
  const model = String(hc?.model ?? "").trim();
  if (model) parts.push(model);
  const packs = Array.isArray(hc?.skill_packs) ? (hc.skill_packs as unknown[]) : [];
  if (packs.length) parts.push(packs.join(", "));
  return parts.join(" · ");
}

/** Effective participant-model set for a run — [model_name] + config extras
 * + any per-config ``model`` pins (PR14 — a pin counts as a participant even
 * outside the run's model list). Falls back to [modelName] for runs that
 * predate config.models. */
export function runModels(
  modelName: string,
  configModels: unknown,
  harnessConfigs?: { model?: unknown }[],
): string[] {
  const list = Array.isArray(configModels)
    ? (configModels as unknown[]).map(String).filter(Boolean)
    : [];
  const out = [modelName];
  for (const m of list) {
    if (m && !out.includes(m)) out.push(m);
  }
  for (const hc of harnessConfigs ?? []) {
    const m = String(hc?.model ?? "").trim();
    if (m && !out.includes(m)) out.push(m);
  }
  return out;
}

/** PR27 — per-config model-sampling knobs. Keys the selected adapter
 * doesn't support are dropped at submit (warning on the create response)
 * — the UI greys out keys the adapter's manifest doesn't declare. */
export interface ModelOptions {
  temperature?: number | null;
  seed?: number | null;
  max_tokens?: number | null;
  /** Maps to the request-level reasoning_effort hint (e.g. "none", "low",
   * "medium", "high") — provider vocabularies differ, kept free-form. */
  reasoning?: string | null;
}

export interface HarnessConfig {
  adapter: string;
  skill_packs: string[];
  system_prompt_extra?: string | null;
  /** Optional label persisted onto the sandbox row (PR1 named configs). */
  name?: string | null;
  /** PR14: pin this config to a single participant model, overriding the
   * run's model list for this config (one cell at the pinned model). */
  model?: string | null;
  /** PR27: optional model knobs honored where the adapter supports them. */
  model_options?: ModelOptions | null;
}

/** PR25: no-sandbox pseudo-adapters run a single model call and stage
 * nothing — a skill_packs selection left over from a previous adapter
 * pick must not ship on a prompt cell (the backend ignores it, but the
 * payload shouldn't claim packs it never used). */
export function sanitizeHarnessConfigs(
  configs: HarnessConfig[],
  isNoSandbox: (adapter: string) => boolean,
): HarnessConfig[] {
  return configs.map((c) =>
    isNoSandbox(c.adapter) ? { ...c, skill_packs: [] } : c,
  );
}

export interface Budgets {
  max_iters: number;
  token_budget: number;
  wall_clock_seconds: number;
  /** PR28: optional per-sandbox token cap (null/undefined = no per-cell
   * limit — a hot cell then only draws down the shared run ceiling). */
  sandbox_token_budget?: number | null;
  /** PR28: optional per-sandbox estimated-cost cap in USD. Only bites on
   * priced models — unpriced cells have no honest rate to bill against. */
  max_cost_usd?: number | null;
}

export const DEFAULT_BUDGETS: Budgets = {
  max_iters: 50,
  token_budget: 200_000,
  wall_clock_seconds: 3600,
  sandbox_token_budget: null,
  max_cost_usd: null,
};

export interface RunCreate {
  task_prompt: string;
  model_name: string;
  /** PR14: ADDITIONAL participant models — matrix expands to
   * harness_configs × ([model_name] + models). Omit for single-model. */
  models?: string[] | null;
  harness_configs: HarnessConfig[];
  evaluator_model?: string | null;
  budgets?: Budgets;
  /** PR27: repeats per matrix cell (1–8, default 1). */
  trials?: number;
}

export interface RunConfig {
  harness_configs?: HarnessConfig[];
  /** PR14: effective participant-model set persisted at submit. */
  models?: string[];
  evaluator_model?: string | null;
  budgets?: Budgets;
  same_model_eval?: boolean;
  /** PR27: repeats per matrix cell persisted at submit. */
  trials?: number;
  [key: string]: unknown;
}

export interface SandboxOut {
  id: string;
  harness_config: Record<string, unknown>;
  /** Config label; "<adapter>-<shortid>" default assigned at provisioning. */
  name: string | null;
  status: string;
  terminal_reason: string | null;
  artifact_path: string | null;
  started_at: string | null;
  finished_at: string | null;
}

export interface RubricDimension {
  name: string;
  title?: string;
  description?: string;
  weight?: number;
  min_score?: number;
  max_score?: number;
  [key: string]: unknown;
}

export interface RubricCriterion {
  id?: string;
  text: string;
  dimension?: string;
  weight?: number;
  [key: string]: unknown;
}

export interface RubricContent {
  summary?: string;
  dimensions?: RubricDimension[];
  criteria?: RubricCriterion[];
  generated_by?: Record<string, unknown>;
  [key: string]: unknown;
}

export interface RubricOut {
  id: string;
  run_id: string;
  version: number;
  status: string; // draft | approved | superseded
  /** PR5: the version this row was edited from (null on the original). */
  parent_id: string | null;
  content: RubricContent;
  created_at: string;
}

export interface RunOut {
  id: string;
  status: RunStatus | string;
  task_prompt: string;
  model_name: string;
  /** PR27: repeats per matrix cell (1 = single-shot). */
  trials?: number;
  config: RunConfig;
  /** Submit-time advisory warnings (e.g. dropped model_options keys,
   * model not in provider catalog) — populated on POST /api/runs only. */
  warnings?: string[];
  same_model_eval: boolean;
  created_at: string;
  updated_at: string | null;
  started_at: string | null;
  finished_at: string | null;
  sandboxes: SandboxOut[];
  rubric: RubricOut | null;
}

export interface RunSummary {
  id: string;
  status: string;
  task_prompt: string; // truncated to 200 chars server-side
  model_name: string;
  /** PR14: full participant-model set (model_name + extras). */
  models?: string[];
  created_at: string;
  finished_at: string | null;
  sandbox_count: number;
}

export interface EventOut {
  id: number;
  run_id: string;
  sandbox_id: string | null;
  ts: string; // ISO-8601 UTC
  seq: number | null;
  source: string;
  type: string;
  payload: Record<string, unknown>;
}

export interface DimensionScore {
  score: number;
  rationale: string;
}

/** PR6 — objective scoring signals (scoring/ AnalysisReport).
 * Measured on the workspace before judging; analysis.json is re-validated
 * server-side and embedded on SandboxResult.analysis. Collector-agnostic:
 * PR7's exec/coverage collector and PR8's evaluator-appended
 * `generated_tests` signal add further SignalReports to `signals`. */
export interface FunctionMetric {
  name: string;
  file: string;
  line: number;
  cyclomatic_complexity: number;
  nloc: number;
  params: number;
  max_nesting_depth: number;
}

export interface FileMetric {
  path: string;
  language: string;
  lines: number;
  nloc: number;
  function_count: number;
  avg_cyclomatic_complexity: number;
  max_cyclomatic_complexity: number;
  max_nesting_depth: number;
}

export interface SignalReport {
  name: string; // e.g. "static_analysis", "exec", "generated_tests"
  status: "ok" | "error" | "skipped" | string;
  metrics: Record<string, unknown>;
  files: FileMetric[];
  functions: FunctionMetric[];
  warnings: string[];
  error: string | null;
}

export interface AnalysisReport {
  schema_version: number;
  generated_at: string; // ISO-8601
  workspace: string;
  signals: SignalReport[];
}

export interface SandboxResult {
  sandbox_id: string;
  harness_config: Record<string, unknown>;
  /** Config label from the sandbox row (PR1 named harness configs). */
  name?: string | null;
  /** PR14: participant model this sandbox ran (config ``model`` cell else
   * the run's model_name). */
  model?: string;
  /** PR27: which repeat of its config × model cell this sandbox is
   * (harness_config.trial_index; null on untrialed runs). */
  trial_index?: number | null;
  status: string;
  terminal_reason: string | null;
  artifact_path: string | null;
  rubric_id: string | null;
  /** PR5: rubric version the displayed scores were judged under. */
  rubric_version?: number | null;
  dimensions: Record<string, DimensionScore>;
  overall: DimensionScore | null;
  /** Judge/eval pipeline failure detail when the sandbox produced no Score
   * rows — distinguishes "judge failed" from "no rubric scores" (pending or
   * never evaluated). Null/absent when scored or not yet evaluated. */
  eval_error?: string | null;
  /** PR6: measured signals (analysis.json); null when not collected. */
  analysis?: AnalysisReport | null;
  /** Optional artifact ids if/when the results API exposes them. */
  artifact_ids?: string[];
  artifacts?: { id: string; kind?: string; path?: string }[];
  /** F12: snapshot workspace has a previewable entry point. */
  previewable?: boolean;
  preview_type?: string | null;
  /**
   * F12: every detected runnable entry, default-first — the UI renders a
   * dropdown and POSTs {entry, type}. Empty + previewable=false = unsupported.
   */
  preview_candidates?: PreviewCandidate[];
  /**
   * PR4: captured artifact screenshot ({url, captured_at, type, entry}) —
   * present only after an explicit POST .../thumbnail; the results GET
   * never spawns captures. `url` is api-relative (prefix API_BASE).
   */
  thumbnail?: ThumbRef | null;
}

/** PR5: one rubric version as listed in results — drives the version
 * selector. `score_count` = Score rows carrying this version's provenance. */
export interface RubricVersionOut {
  id: string;
  version: number;
  status: string; // draft | approved | superseded
  parent_id: string | null;
  created_at: string;
  score_count: number;
}

export interface RunResults {
  run_id: string;
  status: string;
  model_name: string;
  /** PR14: effective participant-model set ([model_name] + extras). */
  models?: string[];
  evaluator_model: string | null;
  same_model_eval: boolean | null;
  /** PR27: repeats-per-cell submitted on this run (1 = single-shot). */
  trials?: number;
  /** PR5: the ?rubric_version= filter echoed back (null = newest-wins). */
  selected_rubric_version?: number | null;
  /** PR5: every rubric version + score counts for the selector. */
  rubric_versions?: RubricVersionOut[];
  sandboxes: SandboxResult[];
}

export interface ScoreOut {
  id: string;
  sandbox_id: string;
  rubric_id: string | null;
  /** PR5 provenance — denormalized rubrics.version at write time. */
  rubric_version?: number | null;
  dimension: string;
  score: number;
  rationale: string;
  created_at: string;
}

/** PR5: POST /api/runs/{id}/rubrics — post-run rubric edit → new version. */
export interface RubricVersionCreate {
  content: RubricContent;
  /** Which row the edit derives from (default: latest version). */
  base_rubric_id?: string | null;
  /** Also queue a re-score against the new version. */
  rescore?: boolean;
}

/** PR5: POST /api/runs/{id}/rescore body — everything optional. */
export interface RescoreIn {
  /** Pin a specific rubric version (default: latest approved). */
  rubric_id?: string | null;
  /** Restrict re-judging to these sandboxes. */
  sandbox_ids?: string[] | null;
  /** Judge-model override for this pass only. */
  model?: string | null;
}

/** PR5: 202 response — re-score accepted and queued. */
export interface RescoreOut {
  run_id: string;
  status: string; // "evaluating"
  rubric_id: string;
  rubric_version: number;
  model: string;
  sandboxes: number;
}

// ---------------------------------------------------------------------------
// errors + fetch plumbing
// ---------------------------------------------------------------------------

export class ApiError extends Error {
  status: number;
  /** Parsed `detail` from a FastAPI error body, when present. */
  detail: string;
  /** Raw `detail` value — for structured errors like name_conflict. */
  rawDetail: unknown;

  constructor(status: number, detail: string, rawDetail?: unknown) {
    super(`API ${status}: ${detail}`);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
    this.rawDetail = rawDetail;
  }
}

async function parseError(res: Response): Promise<ApiError> {
  let detail = `${res.status} ${res.statusText}`;
  let raw: unknown;
  try {
    const body: unknown = await res.json();
    if (body && typeof body === "object") {
      const d = (body as { detail?: unknown }).detail;
      raw = d;
      if (typeof d === "string") detail = d;
      else if (Array.isArray(d)) {
        // FastAPI 422 validation errors: list of {loc, msg}.
        detail = d
          .map((item) => {
            if (item && typeof item === "object") {
              const o = item as { loc?: unknown[]; msg?: string };
              const loc = Array.isArray(o.loc)
                ? o.loc.filter((p) => p !== "body").join(".")
                : "";
              return loc ? `${loc}: ${o.msg ?? "invalid"}` : String(o.msg ?? item);
            }
            return String(item);
          })
          .join("; ");
      } else if (d != null) {
        detail = JSON.stringify(d);
      }
    }
  } catch {
    // non-JSON error body — keep status text
  }
  return new ApiError(res.status, detail, raw);
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(`${API_BASE}${path}`, {
      ...init,
      headers: {
        Accept: "application/json",
        ...(init?.body ? { "Content-Type": "application/json" } : {}),
        ...init?.headers,
      },
    });
  } catch (err) {
    throw new ApiError(
      0,
      `cannot reach API at ${API_BASE} — is the backend running? (${err instanceof Error ? err.message : String(err)})`,
    );
  }
  if (!res.ok) throw await parseError(res);
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

function qs(params: Record<string, string | number | undefined | null>): string {
  const sp = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== null && v !== "") sp.set(k, String(v));
  }
  const s = sp.toString();
  return s ? `?${s}` : "";
}

// ---------------------------------------------------------------------------
// runs
// ---------------------------------------------------------------------------

export function createRun(body: RunCreate): Promise<RunOut> {
  return request<RunOut>("/api/runs", { method: "POST", body: JSON.stringify(body) });
}

export interface ListRunsOptions {
  status?: string;
  limit?: number;
  offset?: number;
}

export function listRuns(opts: ListRunsOptions = {}): Promise<RunSummary[]> {
  return request<RunSummary[]>(
    `/api/runs${qs({ status: opts.status, limit: opts.limit ?? 100, offset: opts.offset ?? 0 })}`,
  );
}

export function getRun(runId: string): Promise<RunOut> {
  return request<RunOut>(`/api/runs/${encodeURIComponent(runId)}`);
}

export function getRubric(runId: string): Promise<RubricOut> {
  return request<RubricOut>(`/api/runs/${encodeURIComponent(runId)}/rubric`);
}

export function updateRubric(runId: string, content: RubricContent): Promise<RubricOut> {
  return request<RubricOut>(`/api/runs/${encodeURIComponent(runId)}/rubric`, {
    method: "PUT",
    body: JSON.stringify({ content }),
  });
}

export function approveRun(runId: string): Promise<RunOut> {
  return request<RunOut>(`/api/runs/${encodeURIComponent(runId)}/approve`, {
    method: "POST",
  });
}

// ---------------------------------------------------------------------------
// events (F6 — live today)
// ---------------------------------------------------------------------------

export interface ListEventsOptions {
  source?: string;
  type?: string;
  sandbox_id?: string;
  after_id?: number;
  limit?: number;
}

export function listEvents(runId: string, opts: ListEventsOptions = {}): Promise<EventOut[]> {
  return request<EventOut[]>(
    `/api/runs/${encodeURIComponent(runId)}/events${qs({
      source: opts.source,
      type: opts.type,
      sandbox_id: opts.sandbox_id,
      after_id: opts.after_id,
      limit: opts.limit ?? 500,
    })}`,
  );
}

/** Fetch the full event history via `after_id` cursor pagination. Capped to
 * avoid unbounded memory on pathological runs. */
export async function listAllEvents(
  runId: string,
  opts: Omit<ListEventsOptions, "after_id" | "limit"> = {},
  maxEvents = 10_000,
): Promise<EventOut[]> {
  const all: EventOut[] = [];
  let after = 0;
  for (;;) {
    const page = await listEvents(runId, { ...opts, after_id: after, limit: 1000 });
    if (page.length === 0) break;
    all.push(...page);
    after = page[page.length - 1].id;
    if (page.length < 1000 || all.length >= maxEvents) break;
  }
  return all;
}

/** SSE endpoint URL for EventSource. */
export function eventsStreamUrl(runId: string): string {
  return `${API_BASE}/api/runs/${encodeURIComponent(runId)}/events/stream`;
}

// ---------------------------------------------------------------------------
// results (F7/F8 — may 404 until the evaluator lane lands)
// ---------------------------------------------------------------------------

export function getRunResults(runId: string, rubricVersion?: number): Promise<RunResults> {
  return request<RunResults>(
    `/api/runs/${encodeURIComponent(runId)}/results${qs({ rubric_version: rubricVersion })}`,
  );
}

// ---------------------------------------------------------------------------
// eval iteration (PR5 — rubric versions + re-score)
// ---------------------------------------------------------------------------

/** One rubric version incl. content — prefills the post-run edit editor. */
export function getRubricVersion(rubricId: string): Promise<RubricOut> {
  return request<RubricOut>(`/api/rubrics/${encodeURIComponent(rubricId)}`);
}

/** Post-run rubric edit — creates a new approved version (vN+1); the previous
 * approved version is superseded, never mutated. Pass `rescore: true` to also
 * queue a re-score against the new version. */
export function createRubricVersion(
  runId: string,
  body: RubricVersionCreate,
): Promise<RubricOut> {
  return request<RubricOut>(`/api/runs/${encodeURIComponent(runId)}/rubrics`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

/** Re-run the evaluator on stored artifacts (terminal runs only). Empty body
 * = latest approved rubric on every artifact-bearing sandbox. */
export function rescoreRun(runId: string, body: RescoreIn = {}): Promise<RescoreOut> {
  return request<RescoreOut>(`/api/runs/${encodeURIComponent(runId)}/rescore`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

// ---------------------------------------------------------------------------
// analytics (F9)
// ---------------------------------------------------------------------------

/** USD-per-million-token rate for one model; `local` = free/local inference.
 * `cache_read_per_mtok_usd` (PR28): discounted rate for cache-read input
 * tokens — absent/null bills them at the input rate. */
export interface ModelPrice {
  input_per_mtok_usd: number;
  output_per_mtok_usd: number;
  cache_read_per_mtok_usd?: number | null;
  local?: boolean;
}

export interface TokenUsage {
  input: number;
  output: number;
  cache_read: number;
  total: number;
}

export interface LatencyStats {
  avg: number | null;
  p95: number | null;
  max: number | null;
}

export interface ToolCallStats {
  total: number;
  by_tool: Record<string, number>;
}

export interface SkillCallStats {
  total: number;
  by_skill: Record<string, number>;
}

/** Judge traffic attributed to the sandbox's stream (eval creds reuse the
 * participant sandbox_id); partitioned out of participant metrics.
 * `estimated_cost_usd` is null when the evaluator model is unpriced (PR21 —
 * unlisted models get no guessed rate). */
export interface EvaluatorActivity {
  llm_turns: number;
  tokens: { input: number; output: number; total: number };
  errors: number;
  estimated_cost_usd: number | null;
}

export interface SandboxAnalytics {
  sandbox_id: string;
  /** Config label from the sandbox row (PR1 named harness configs). */
  name?: string | null;
  /** PR27: which repeat of its cell this sandbox is (null = untrialed). */
  trial_index?: number | null;
  /** PR27: stable config × model cell key — identical across a cell's
   * trials (grouped on for the per-cell variance rollup). */
  cell?: string;
  label: string;
  harness_config: Record<string, unknown>;
  /** PR14: participant model this sandbox ran + the price-table key its
   * estimated_cost_usd was computed at — null when the model isn't listed
   * (unpriced, PR21). */
  model: string;
  price_model: string | null;
  status: string;
  terminal_reason: string | null;
  llm_turns: number;
  tokens: TokenUsage;
  latency_ms: LatencyStats;
  /** PR30 speed metrics: time-to-first-byte stats + mean output tok/s.
   * Optional — absent on backends predating the gateway speed fields, and
   * null-valued when no event carried them. */
  ttft_ms?: LatencyStats;
  avg_tokens_per_sec?: number | null;
  tool_calls: ToolCallStats;
  skill_calls: SkillCallStats;
  shell_commands: number;
  file_ops: number;
  test_executions: number;
  errors: number;
  wall_clock_seconds: number;
  estimated_cost_usd: number | null;
  evaluator: EvaluatorActivity | null;
}

/** PR14 — per-model rollup across a run's sandboxes (compare-by-model). */
export interface ModelAnalytics {
  model: string;
  price_model: string | null;
  sandbox_count: number;
  llm_turns: number;
  tokens: TokenUsage;
  latency_ms: LatencyStats;
  /** PR30 speed metrics, pooled across the model's sandboxes. */
  ttft_ms?: LatencyStats;
  avg_tokens_per_sec?: number | null;
  tool_calls: ToolCallStats;
  skill_calls: SkillCallStats;
  shell_commands: number;
  file_ops: number;
  test_executions: number;
  errors: number;
  wall_clock_seconds: number;
  estimated_cost_usd: number | null;
  evaluator: EvaluatorActivity | null;
}

/** PR27 — per-cell (config × model) rollup: same metric aggregation as
 * ModelAnalytics plus the trial-score variance columns the results matrix
 * reports (sample sd, n−1 denominator; overall_sd null when fewer than two
 * trials produced an overall score). */
export interface CellAnalytics {
  cell: string;
  label: string;
  model: string;
  sandbox_count: number;
  llm_turns: number;
  tokens: TokenUsage;
  latency_ms: LatencyStats;
  /** PR30 speed metrics, pooled across the cell's trials. */
  ttft_ms?: LatencyStats;
  avg_tokens_per_sec?: number | null;
  tool_calls: ToolCallStats;
  skill_calls: SkillCallStats;
  shell_commands: number;
  file_ops: number;
  test_executions: number;
  errors: number;
  wall_clock_seconds: number;
  estimated_cost_usd: number | null;
  evaluator: EvaluatorActivity | null;
  overall_mean: number | null;
  overall_sd: number | null;
  overall_n: number;
}

export interface RunAnalytics {
  run_id: string;
  model_name: string;
  /** Explicit ?price_model= basis, or null for auto per-model pricing. */
  price_model: string | null;
  price_table: Record<string, ModelPrice>;
  /** model_prices row globs in declaration (first-match) order — lets
   * matchPriceKey replay key_for's rows-before-static layering. */
  price_globs?: string[];
  sandboxes: SandboxAnalytics[];
  /** PR14: participant-model rollups (one per model in the run's matrix). */
  models: ModelAnalytics[];
  /** PR27: per-cell rollups — one entry per config × model trial group. */
  cells?: CellAnalytics[];
}

export function getRunAnalytics(runId: string, priceModel?: string): Promise<RunAnalytics> {
  return request<RunAnalytics>(
    `/api/runs/${encodeURIComponent(runId)}/analytics${qs({ price_model: priceModel })}`,
  );
}

/** fnmatch-style glob test mirroring api/pricing.py::glob_matches —
 * case-insensitive; a pattern with no "/" never matches a vendor/model id.
 * The translator ports Python's fnmatch.translate: `*`/`?` wildcards,
 * `[seq]` character classes with `[!seq]` negation (a leading "]" inside a
 * class is a literal), and an unclosed "[" matching the literal text —
 * where a naive escape-everything pass inverts [!seq] semantics and
 * throws on a dangling bracket. */
export function globMatch(pattern: string, model: string): boolean {
  const pat = pattern.trim().toLowerCase();
  const name = model.trim().toLowerCase();
  if (!pat || !name) return false;
  if (name.includes("/") && !pat.includes("/")) return false;
  let re = "^";
  let i = 0;
  while (i < pat.length) {
    const c = pat[i++];
    if (c === "*") {
      re += ".*";
    } else if (c === "?") {
      re += ".";
    } else if (c === "[") {
      // scan the class body: "!" negates, a leading "]" is a literal
      let j = i;
      if (j < pat.length && pat[j] === "!") j++;
      if (j < pat.length && pat[j] === "]") j++;
      while (j < pat.length && pat[j] !== "]") j++;
      if (j >= pat.length) {
        re += "\\["; // unclosed "[" — fnmatch treats it as the literal char
      } else {
        // escape \ and ] inside the class (JS needs \] for a literal "]"),
        // then apply fnmatch's "!" → "^" negation; a leading ^/[ also
        // needs escaping so it can't re-negate or nest a class
        let stuff = pat.slice(i, j).replace(/[\\\]]/g, "\\$&");
        if (stuff.startsWith("!")) {
          stuff = "^" + stuff.slice(1);
        } else if (stuff.startsWith("^") || stuff.startsWith("[")) {
          stuff = "\\" + stuff;
        }
        re += `[${stuff}]`;
        i = j + 1;
      }
    } else {
      re += c.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    }
  }
  re += "$";
  try {
    return new RegExp(re).test(name);
  } catch {
    return false;
  }
}

/** Resolve a model to its effective price-table KEY — mirrors
 * PriceBook.key_for: the FIRST row glob (declaration order —
 * RunAnalytics.price_globs, plus rates added inline this session) that
 * glob-matches the model wins, else the model itself when it's a key in
 * the merged table (the static layer — a row glob equal to it already won
 * above), else undefined (unlisted → unpriced, PR21). The row rate sits
 * at table[key]; row ordering can't be recovered from the merged table
 * alone (literal rows are indistinguishable from static keys), which is
 * why price_globs rides the response. */
export function matchPriceKey(
  model: string | null | undefined,
  table: Record<string, ModelPrice>,
  rowGlobs: readonly string[],
): string | undefined {
  if (!model) return undefined;
  for (const g of rowGlobs) {
    if (Object.hasOwn(table, g) && globMatch(g, model)) return g;
  }
  return Object.hasOwn(table, model) ? model : undefined;
}

/** Resolve an explicit price pick to its effective price-table key —
 * mirrors api/pricing.py::resolve_price_model: a pick naming a
 * model_prices row glob (case-insensitively — the same row identity
 * upsert/sync use) selects THAT row's rate so an earlier broader glob
 * can't shadow it; any other pick resolves like a model through
 * matchPriceKey's rows-first layering. Unresolvable → undefined (the
 * server 422s it; the client renders "unpriced"). */
export function resolvePricePick(
  pick: string | null | undefined,
  table: Record<string, ModelPrice>,
  rowGlobs: readonly string[],
): string | undefined {
  if (!pick) return undefined;
  const lower = pick.toLowerCase();
  const row = rowGlobs.find((g) => g.toLowerCase() === lower);
  return row ?? matchPriceKey(pick, table, rowGlobs);
}

/** Client-side cost estimate: tokens × rate. Mirrors api/pricing.py —
 * cache-read tokens (inside `input`) bill at the cache rate when one is
 * declared, else at the input rate. A non-positive cache_read counts
 * zero cached tokens, matching estimate_cost_usd's guard. */
export function estimateCostUsd(
  price: ModelPrice,
  tokens: { input: number; output: number; cache_read?: number },
): number {
  const cacheRate = price.cache_read_per_mtok_usd;
  const cacheRead = tokens.cache_read ?? 0;
  const cached =
    cacheRate == null || cacheRead <= 0 ? 0 : Math.min(cacheRead, tokens.input);
  return (
    ((tokens.input - cached) * price.input_per_mtok_usd +
      cached * (cacheRate ?? 0) +
      tokens.output * price.output_per_mtok_usd) /
    1_000_000
  );
}

// ---------------------------------------------------------------------------
// cross-run trends (F13 — GET /api/analytics/runs)
// ---------------------------------------------------------------------------

/** One run's aggregate for the cross-run trend chart. */
export interface RunTrendPoint {
  run_id: string;
  status: string;
  task_prompt: string; // truncated to 120 chars server-side
  model_name: string;
  evaluator_model: string | null;
  created_at: string | null;
  finished_at: string | null;
  sandbox_count: number;
  /** Participant-phase totals across all of the run's sandboxes. */
  llm_turns: number;
  tokens: TokenUsage;
  errors: number;
  /** Judge-phase totals (evaluator creds share the participant sandbox_id). */
  evaluator_llm_turns: number;
  evaluator_tokens: TokenUsage;
  evaluator_errors: number;
  /** Price-table key `estimated_cost_usd` was computed at — the explicit
   * param or the run's resolved rate; null when auto mode mixed per-model
   * rates (no single honest key) or the point is unpriced. */
  price_model: string | null;
  /** Participant cost; null when any cell's model is unpriced (PR21 — a
   * partial total would read as the run's full cost). */
  estimated_cost_usd: number | null;
  /** Judge tokens priced at the evaluator model's rate when listed; null
   * when the evaluator model is unpriced. */
  evaluator_cost_usd: number | null;
  /** Mean of the run's `overall` score rows; null when unscored. */
  mean_overall_score: number | null;
}

export interface RunTrends {
  /** The explicit `?price_model=` basis, or null for per-run auto pricing. */
  price_model: string | null;
  price_table: Record<string, ModelPrice>;
  /** Newest-first points (server ordering). */
  points: RunTrendPoint[];
}

export function getRunTrends(opts: {
  limit?: number;
  priceModel?: string;
} = {}): Promise<RunTrends> {
  return request<RunTrends>(
    `/api/analytics/runs${qs({ limit: opts.limit ?? 20, price_model: opts.priceModel })}`,
  );
}

// ---------------------------------------------------------------------------
// skillpacks (F11 — vendored listing + GitHub import)
// ---------------------------------------------------------------------------

/** One selectable pack (vendored preset or user-imported from GitHub). */
export interface SkillPack {
  name: string;
  source: "vendored" | "imported" | string;
  skill_count: number;
  skills: string[];
  origin_url: string | null;
  imported_at: string | null;
}

/** One SKILL.md discovered in a repo — `dir` is the selection token. */
export interface DiscoveredSkill {
  dir: string;
  name: string;
  description: string;
  file_count: number;
  has_frontmatter: boolean;
}

export interface SkillpackDiscoverResult {
  url: string;
  repo: string;
  /** Resolved ref — pass back (or commit_sha) as `ref` on import to pin the tree. */
  ref: string;
  commit_sha: string | null;
  subpath: string;
  kind: "root" | "tree" | "blob" | string;
  skills: DiscoveredSkill[];
}

export interface ImportedSkill {
  source_dir: string;
  name: string;
  dir: string;
  file_count: number;
  renamed: boolean;
}

/** One pack created/merged by an import call. */
export interface PackImportResult {
  pack: string;
  source: string;
  skill_count: number;
  imported: ImportedSkill[];
  issues: string[];
}

export interface SkillpackImportResult {
  /** One entry per pack touched — >1 only for mode "split". */
  packs: PackImportResult[];
}

/** Structured 409 detail for pack-name collisions (confirm-and-rename). */
export interface SkillpackNameConflict {
  kind: "name_conflict";
  message: string;
  name: string;
  source: "vendored" | "imported";
  suggested_name: string | null;
}

/** Extract the collision payload from an import 409, if it is one. */
export function nameConflictOf(err: unknown): SkillpackNameConflict | null {
  if (err instanceof ApiError && err.status === 409) {
    const d = err.rawDetail;
    if (
      d !== null &&
      typeof d === "object" &&
      (d as { kind?: unknown }).kind === "name_conflict"
    ) {
      return d as SkillpackNameConflict;
    }
  }
  return null;
}

export function listSkillPacks(): Promise<SkillPack[]> {
  return request<SkillPack[]>("/api/skillpacks");
}

export function discoverSkillPack(url: string): Promise<SkillpackDiscoverResult> {
  return request<SkillpackDiscoverResult>("/api/skillpacks/discover", {
    method: "POST",
    body: JSON.stringify({ url }),
  });
}

export interface SkillpackImportRequest {
  url: string;
  skills: string[]; // DiscoveredSkill.dir values
  /**
   * "pack" (default) — one pack named pack_name; taken name → 409 +
   * suggested_name (confirm-and-rename). "merge" — append into an existing
   * imported pack. "split" — one pack per skill, auto-renamed on collision.
   */
  mode?: "pack" | "merge" | "split";
  /** Required for pack/merge, ignored for split. */
  pack_name?: string;
  ref?: string | null;
  subpath?: string | null;
}

export function importSkillPack(body: SkillpackImportRequest): Promise<SkillpackImportResult> {
  return request<SkillpackImportResult>("/api/skillpacks/import", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function deleteSkillPack(name: string): Promise<void> {
  return request<void>(`/api/skillpacks/${encodeURIComponent(name)}`, {
    method: "DELETE",
  });
}

export function getSandboxScores(sandboxId: string): Promise<ScoreOut[]> {
  return request<ScoreOut[]>(`/api/sandboxes/${encodeURIComponent(sandboxId)}/scores`);
}

export function artifactDownloadUrl(artifactId: string): string {
  return `${API_BASE}/api/artifacts/${encodeURIComponent(artifactId)}/download`;
}

// ---------------------------------------------------------------------------
// artifact preview (F12)
// ---------------------------------------------------------------------------

export interface PreviewCandidate {
  /** Workspace-relative entry path, e.g. "index.html" or "game/main.py". */
  entry: string;
  /** Preview type — "static" | "desktop" (GUI via noVNC) | "python-server" (web app). */
  type: string;
}

export interface PreviewOut {
  preview_id: string;
  run_id: string;
  sandbox_id: string;
  /** Preview type — "static" | "desktop" (GUI via noVNC) | "python-server" (web app). */
  type: string;
  /** Docker container status: created | running | exited | ... */
  status: string;
  url: string | null;
  entry: string | null;
  container: string;
  created_at: string | null;
  expires_at: string | null;
}

function previewPath(runId: string, sandboxId: string): string {
  return `/api/runs/${encodeURIComponent(runId)}/sandboxes/${encodeURIComponent(sandboxId)}/preview`;
}

/** 404 when no preview exists — callers treat it as "not started". */
export function getPreview(runId: string, sandboxId: string): Promise<PreviewOut> {
  return request<PreviewOut>(previewPath(runId, sandboxId));
}

/**
 * Idempotent per sandbox: returns the live preview if one already runs and
 * the requested plan matches (an explicit different entry/type replaces it).
 * `entry` (workspace-relative) picks an explicit entry point — for
 * ambiguous scans or artifacts detection missed; `.html` → static,
 * `.py` → desktop, `manage.py` → python-server. `type` overrides the
 * pick: alone it selects the first candidate of that type; with `entry`
 * it forces the plan type — `{entry: "x.py", type: "static"}` serves the
 * file instead of executing it ("view source"), while `desktop` and
 * `python-server` require a `.py` entry.
 */
export function startPreview(
  runId: string,
  sandboxId: string,
  opts?: { entry?: string; type?: string },
): Promise<PreviewOut> {
  const body =
    opts && (opts.entry || opts.type) ? JSON.stringify(opts) : undefined;
  return request<PreviewOut>(previewPath(runId, sandboxId), {
    method: "POST",
    ...(body ? { body } : {}),
  });
}

export function stopPreview(runId: string, sandboxId: string): Promise<void> {
  return request<void>(previewPath(runId, sandboxId), { method: "DELETE" });
}

// ---------------------------------------------------------------------------
// artifact thumbnails (PR4 — headless screenshots via the preview stack)
// ---------------------------------------------------------------------------

/** Thumbnail ref embedded on SandboxResult (and the POST response minus
 * `type` narrowing). `url` is api-relative — prefix with API_BASE. */
export interface ThumbRef {
  url: string;
  captured_at: string | null;
  type: string | null;
  entry: string | null;
}

export interface ThumbOut {
  url: string;
  captured_at: string | null;
  type: string;
  entry: string | null;
}

function thumbnailPath(runId: string, sandboxId: string): string {
  return `/api/runs/${encodeURIComponent(runId)}/sandboxes/${encodeURIComponent(sandboxId)}/thumbnail`;
}

/** Absolute URL for the captured PNG (`<img src>`). `v` cache-busts
 * re-captures — pass captured_at when known. */
export function thumbnailUrl(
  runId: string,
  sandboxId: string,
  v?: string | null,
): string {
  return `${API_BASE}${thumbnailPath(runId, sandboxId)}${v ? `?v=${encodeURIComponent(v)}` : ""}`;
}

/**
 * Explicit screenshot capture: spawns the artifact's preview stack
 * (purpose=thumb — invisible to the interactive preview endpoints), waits
 * for it to serve, shoots it with headless chromium, and stores
 * <artifact>/thumbnail.png. Synchronous but bounded server-side; 502 on
 * capture failure (no thumbnail is still a fine outcome). Optional
 * {entry, type} picks the captured candidate like startPreview.
 */
export function captureThumbnail(
  runId: string,
  sandboxId: string,
  opts?: { entry?: string; type?: string },
): Promise<ThumbOut> {
  const body =
    opts && (opts.entry || opts.type) ? JSON.stringify(opts) : undefined;
  return request<ThumbOut>(thumbnailPath(runId, sandboxId), {
    method: "POST",
    ...(body ? { body } : {}),
  });
}

export function deleteThumbnail(runId: string, sandboxId: string): Promise<void> {
  return request<void>(thumbnailPath(runId, sandboxId), { method: "DELETE" });
}

/** Liveness probe for the "backend not ready" UI states. */
export async function checkHealth(): Promise<boolean> {
  try {
    const res = await fetch(`${API_BASE}/health`, { cache: "no-store" });
    return res.ok;
  } catch {
    return false;
  }
}

// ---------------------------------------------------------------------------
// models (PR1 — live picker + ollama install via the gateway)
// ---------------------------------------------------------------------------

/** One upstream model entry (OpenAI /v1/models shape, extra fields kept). */
export interface ModelInfo {
  id: string;
  [key: string]: unknown;
}

export interface ModelsOut {
  models: ModelInfo[];
  /** Gateway provider name — "ollama" | "openrouter" | "openai" |
   * "anthropic" (PR12 adds these + PROVIDER_ROUTES per-model providers;
   * pull stays ollama-only). Reflects the gateway's RESOLVED provider for
   * the request — with `provider=` this is the forced upstream, without it
   * the gateway's UPSTREAM_PROVIDER default. */
  provider: string | null;
  /** True when the upstream can install models (ollama pull). */
  pull_supported: boolean;
}

/** Upstream provider names the gateway can route to
 * (gateway/config.py `ProviderName`). The submit form's per-row provider
 * picker uses this to fetch that upstream's catalog via
 * `getModels(provider)` — picker metadata only; a run still resolves the
 * provider per model name gateway-side via PROVIDER_ROUTES. */
export const PROVIDERS: readonly string[] = [
  "ollama",
  "openrouter",
  "openai",
  "anthropic",
];

/** Live model catalog. `provider` forwards as the gateway's
 * `x-heg-provider` override (service cred) — omit for the gateway's
 * configured default upstream. */
export function getModels(provider?: string): Promise<ModelsOut> {
  return request<ModelsOut>(`/api/models${qs({ provider })}`);
}

/** One NDJSON progress line from `ollama pull` (status/digest/total/completed). */
export interface ModelPullEvent {
  status?: string;
  digest?: string;
  total?: number;
  completed?: number;
  error?: string;
  [key: string]: unknown;
}

/**
 * POST /api/models/pull — streams the gateway's NDJSON progress lines;
 * `onProgress` fires per line. Resolves with the last event; throws ApiError
 * on non-2xx or when ANY stream line reports `{"error": ...}` — mid-stream
 * errors surface too, not just a terminal one (the first error wins).
 */
export async function pullModel(
  name: string,
  onProgress?: (ev: ModelPullEvent) => void,
): Promise<ModelPullEvent | null> {
  let res: Response;
  try {
    res = await fetch(`${API_BASE}/api/models/pull`, {
      method: "POST",
      headers: {
        Accept: "application/x-ndjson",
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ name }),
    });
  } catch (err) {
    throw new ApiError(
      0,
      `cannot reach API at ${API_BASE} — is the backend running? (${err instanceof Error ? err.message : String(err)})`,
    );
  }
  if (!res.ok) throw await parseError(res);
  if (!res.body) return null;

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  let last: ModelPullEvent | null = null;
  let firstError: ModelPullEvent | null = null;

  const take = (line: string): ModelPullEvent | null => {
    const s = line.trim();
    if (!s) return null;
    try {
      return JSON.parse(s) as ModelPullEvent;
    } catch {
      return null; // non-JSON progress line — ignore
    }
  };

  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    let nl: number;
    while ((nl = buf.indexOf("\n")) >= 0) {
      const ev = take(buf.slice(0, nl));
      buf = buf.slice(nl + 1);
      if (ev) {
        last = ev;
        if (ev.error && !firstError) firstError = ev;
        onProgress?.(ev);
      }
    }
  }
  buf += decoder.decode();
  const tailEv = take(buf);
  if (tailEv) {
    last = tailEv;
    if (tailEv.error && !firstError) firstError = tailEv;
    onProgress?.(tailEv);
  }

  if (firstError) {
    throw new ApiError(502, String(firstError.error), firstError);
  }
  return last;
}

// ---------------------------------------------------------------------------
// BUGHUNT-BATCH ADDITIONS — fields/endpoints landing on a parallel backend
// branch. Everything below is OPTIONAL (interface merging) or additive so
// the UI compiles + degrades cleanly before the backend ships; when the
// backend lands these fields simply light up.
// ---------------------------------------------------------------------------

export interface RunOut {
  /** Position among queued runs (1-based) — present only while
   *  status === "queued" once the queue endpoint lands. */
  queue_position?: number | null;
  /** Submit-time advisory warnings from run-create (e.g. "model not found
   *  on the resolved upstream") — absent on older backends. */
  warnings?: string[];
}

export interface RunSummary {
  /** Queue position among queued runs (1-based); absent until the backend
   *  queue work lands. */
  queue_position?: number | null;
  /** Optional adapter list when the list payload exposes it — drives the
   *  runs-page text filter. */
  adapters?: string[];
}

export interface SandboxResult {
  /** Judge pipeline mode — "agentic" (eval-sandbox miniloop) or
   *  "direct_fallback" (single control-plane completion). The results API
   *  may not expose it yet; the page falls back to the `eval_scored`
   *  checkpoint event's `mode`. */
  eval_mode?: string | null;
  /** True when the sandbox's snapshot workspace was empty (the agent wrote
   *  nothing) — a 0-score outcome reads differently with it flagged. */
  empty_workspace?: boolean | null;
  /** PR31: flat `exec`-signal summary (PR7 measured build/tests in the
   *  locked heg-exec container) — feeds the results grid's pass-rate. */
  exec?: {
    status?: string;
    tests_detected?: boolean;
    tests_ok?: boolean | null;
    tests_collected?: number;
    tests_passed?: number;
    tests_failed?: number;
    coverage_percent?: number | null;
    detail?: string | null;
  } | null;
  /** PR31: flat `generated_tests`-signal summary (PR8 evaluator-authored
   *  suite run against the submission) — the preferred pass-rate signal. */
  generated_tests?: {
    status?: string;
    model?: string | null;
    tests_collected?: number;
    tests_passed?: number;
    pass_rate?: number | null;
    coverage_percent?: number | null;
    detail?: string | null;
  } | null;
  /** PR27 (when landed): repeat index of a matrix cell. The results grid
   *  groups same-(config, model) members as trials regardless — this is
   *  display-only. */
  trial_index?: number | null;
}

export interface PreviewOut {
  /** Dead/failed preview diagnostics landing with the preview backend work —
   *  container logs tail, process exit code, and a human-readable detail. */
  logs?: string | null;
  exit_code?: number | null;
  detail?: string | null;
}

/** DELETE /api/runs/{id} outcome — "cancelled" for pre-spawn runs
 *  (submitted/rubric_review/queued), "deleted" for terminal runs that were
 *  hard-removed. In-flight runs (provisioning/running/evaluating) 409. */
export interface RunDeleteOut {
  status: "cancelled" | "deleted";
}

/** DELETE /api/runs/{id} — cancel/remove a run (parallel backend work).
 *  Non-terminal runs cancel; the backend may 409 for states it refuses
 *  (running/evaluating) — callers surface `ApiError.detail`. */
export function deleteRun(runId: string): Promise<RunDeleteOut> {
  return request<RunDeleteOut>(`/api/runs/${encodeURIComponent(runId)}`, {
    method: "DELETE",
  });
}

// ---------------------------------------------------------------------------
// prices (PR28 — user-editable glob rate table layered over the static one)
// ---------------------------------------------------------------------------

/** One `model_prices` row — `model_glob` is an fnmatch pattern (`gpt-4*`);
 * rows match in declaration order, first hit wins over the static table. */
export interface PriceEntry {
  id: string;
  model_glob: string;
  input_per_mtok_usd: number;
  output_per_mtok_usd: number;
  cache_read_per_mtok_usd: number | null;
  local: boolean;
  /** "user" (PUT /api/prices — never overwritten by sync) | "openrouter". */
  source: string;
  created_at: string;
  updated_at: string;
}

export interface PricesOut {
  prices: PriceEntry[];
  /** Static fallback layer under the user table. */
  static: Record<string, ModelPrice>;
  /** POST /api/prices/sync is meaningful only when openrouter is configured. */
  sync_available: boolean;
}

export interface PriceUpsert {
  model_glob: string;
  input_per_mtok_usd: number;
  output_per_mtok_usd: number;
  cache_read_per_mtok_usd?: number | null;
  local?: boolean;
}

export interface PriceSyncOut {
  synced: number;
  added: number;
  updated: number;
  skipped_user: number;
  skipped_unpriced: number;
  remote_count: number;
  prices: PriceEntry[];
}

export function getPrices(): Promise<PricesOut> {
  return request<PricesOut>("/api/prices");
}

/** Upsert a user rate by `model_glob` — matching row keeps its
 * declaration-order position, its rate updates. */
export function upsertPrice(body: PriceUpsert): Promise<PriceEntry> {
  return request<PriceEntry>("/api/prices", {
    method: "PUT",
    body: JSON.stringify(body),
  });
}

export function deletePrice(id: string): Promise<void> {
  return request<void>(`/api/prices/${encodeURIComponent(id)}`, {
    method: "DELETE",
  });
}

/** Pull OpenRouter /models pricing into model_prices (via the gateway's
 * pass-through — the api never talks to providers directly). */
export function syncPrices(): Promise<PriceSyncOut> {
  return request<PriceSyncOut>("/api/prices/sync", { method: "POST" });
}

// ---------------------------------------------------------------------------
// artifact file browser + side-by-side diff (PR34)
// ---------------------------------------------------------------------------

/** One browsable path inside a sandbox's artifact browse root —
 * `path` is POSIX-style relative; `size` is null for dirs. */
export interface FileEntry {
  path: string;
  kind: "file" | "dir" | string;
  size: number | null;
}

/** GET /api/sandboxes/{id}/files — bounded listing.
 * `root` = "workspace" (snapshot workspace) | "artifact" (artifact dir —
 * prompt cells / workspace-less artifacts, where output.txt lives). */
export interface FileListOut {
  sandbox_id: string;
  root: string;
  entries: FileEntry[];
  /** Listing hit the server's depth/entry caps. */
  truncated: boolean;
}

/** GET /api/sandboxes/{id}/files/{path} — size-capped text read.
 * `binary` = NUL-carrying content → `text` null (offer the download). */
export interface FileContentOut {
  sandbox_id: string;
  path: string;
  size: number;
  truncated: boolean;
  binary: boolean;
  text: string | null;
}

/** GET .../files/{path}?diff=<other> — unified diff across browse roots.
 * `sandbox_id` = a/left, `other_sandbox_id` = b/right. A path absent on
 * one side diffs against empty; `binary` sides return an empty `diff`. */
export interface FileDiffOut {
  sandbox_id: string;
  other_sandbox_id: string;
  path: string;
  a_missing: boolean;
  b_missing: boolean;
  binary: boolean;
  identical: boolean;
  truncated: boolean;
  diff: string;
}

function sandboxFilesPath(sandboxId: string): string {
  return `/api/sandboxes/${encodeURIComponent(sandboxId)}/files`;
}

/** Jailed, depth/entry-bounded listing of the sandbox's artifact browse
 * root (workspace/ when present, else the artifact dir — prompt cells). */
export function listSandboxFiles(sandboxId: string): Promise<FileListOut> {
  return request<FileListOut>(sandboxFilesPath(sandboxId));
}

/** Segments of `path` are encoded individually so a legitimately nested
 * artifact path keeps its slashes (and any `..`/absolute games 404
 * server-side anyway — the browse root is jailed). */
function fileUrlPath(path: string): string {
  return path.split("/").map(encodeURIComponent).join("/");
}

/** Size-capped text read of one artifact file (JSON). */
export function getSandboxFile(
  sandboxId: string,
  path: string,
): Promise<FileContentOut> {
  return request<FileContentOut>(
    `${sandboxFilesPath(sandboxId)}/${fileUrlPath(path)}`,
  );
}

/** Server-side unified diff of `path` across `sandboxId` (a) and
 * `otherId` (b). 404s when the path exists on neither side. */
export function getSandboxFileDiff(
  sandboxId: string,
  path: string,
  otherId: string,
): Promise<FileDiffOut> {
  return request<FileDiffOut>(
    `${sandboxFilesPath(sandboxId)}/${fileUrlPath(path)}${qs({ diff: otherId })}`,
  );
}

/** Absolute URL for the `?download=1` attachment of a browsed file. */
export function sandboxFileDownloadUrl(sandboxId: string, path: string): string {
  return `${API_BASE}${sandboxFilesPath(sandboxId)}/${fileUrlPath(path)}?download=1`;
}
