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
  | "provisioning"
  | "running"
  | "evaluating"
  | "done"
  | "failed";

export const RUN_STATUSES: readonly RunStatus[] = [
  "submitted",
  "rubric_review",
  "provisioning",
  "running",
  "evaluating",
  "done",
  "failed",
];

/** Statuses after which nothing changes — polling can stop. */
export const TERMINAL_RUN_STATUSES: readonly string[] = ["done", "failed"];

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
  "claude_code",
  "codex",
  "deepseek",
  "pi",
];

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

export interface HarnessConfig {
  adapter: string;
  skill_packs: string[];
  system_prompt_extra?: string | null;
}

export interface Budgets {
  max_iters: number;
  token_budget: number;
  wall_clock_seconds: number;
}

export const DEFAULT_BUDGETS: Budgets = {
  max_iters: 50,
  token_budget: 200_000,
  wall_clock_seconds: 3600,
};

export interface RunCreate {
  task_prompt: string;
  model_name: string;
  harness_configs: HarnessConfig[];
  evaluator_model?: string | null;
  budgets?: Budgets;
}

export interface RunConfig {
  harness_configs?: HarnessConfig[];
  evaluator_model?: string | null;
  budgets?: Budgets;
  same_model_eval?: boolean;
  [key: string]: unknown;
}

export interface SandboxOut {
  id: string;
  harness_config: Record<string, unknown>;
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
  status: string; // draft | approved
  content: RubricContent;
  created_at: string;
}

export interface RunOut {
  id: string;
  status: RunStatus | string;
  task_prompt: string;
  model_name: string;
  config: RunConfig;
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
 * PR7's exec/coverage collector adds another SignalReport to `signals`. */
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
  name: string; // e.g. "static_analysis"; PR7 adds exec-derived signals
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
  status: string;
  terminal_reason: string | null;
  artifact_path: string | null;
  rubric_id: string | null;
  dimensions: Record<string, DimensionScore>;
  overall: DimensionScore | null;
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
}

export interface RunResults {
  run_id: string;
  status: string;
  model_name: string;
  evaluator_model: string | null;
  same_model_eval: boolean | null;
  sandboxes: SandboxResult[];
}

export interface ScoreOut {
  id: string;
  sandbox_id: string;
  rubric_id: string | null;
  dimension: string;
  score: number;
  rationale: string;
  created_at: string;
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

export function getRunResults(runId: string): Promise<RunResults> {
  return request<RunResults>(`/api/runs/${encodeURIComponent(runId)}/results`);
}

// ---------------------------------------------------------------------------
// analytics (F9)
// ---------------------------------------------------------------------------

/** USD-per-million-token rate for one model; `local` = free/local inference. */
export interface ModelPrice {
  input_per_mtok_usd: number;
  output_per_mtok_usd: number;
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
 * participant sandbox_id); partitioned out of participant metrics. */
export interface EvaluatorActivity {
  llm_turns: number;
  tokens: { input: number; output: number; total: number };
  estimated_cost_usd: number;
}

export interface SandboxAnalytics {
  sandbox_id: string;
  label: string;
  harness_config: Record<string, unknown>;
  status: string;
  terminal_reason: string | null;
  llm_turns: number;
  tokens: TokenUsage;
  latency_ms: LatencyStats;
  tool_calls: ToolCallStats;
  skill_calls: SkillCallStats;
  shell_commands: number;
  file_ops: number;
  test_executions: number;
  errors: number;
  wall_clock_seconds: number;
  estimated_cost_usd: number;
  evaluator: EvaluatorActivity | null;
}

export interface RunAnalytics {
  run_id: string;
  model_name: string;
  /** Which price_table entry `estimated_cost_usd` was computed at. */
  price_model: string;
  price_table: Record<string, ModelPrice>;
  sandboxes: SandboxAnalytics[];
}

export function getRunAnalytics(runId: string, priceModel?: string): Promise<RunAnalytics> {
  return request<RunAnalytics>(
    `/api/runs/${encodeURIComponent(runId)}/analytics${qs({ price_model: priceModel })}`,
  );
}

/** Client-side cost estimate: tokens × rate. Mirrors api/pricing.py. */
export function estimateCostUsd(
  price: ModelPrice,
  tokens: { input: number; output: number },
): number {
  return (
    (tokens.input * price.input_per_mtok_usd +
      tokens.output * price.output_per_mtok_usd) /
    1_000_000
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
  /** Preview type — "static" | "desktop" (pygame via noVNC). */
  type: string;
}

export interface PreviewOut {
  preview_id: string;
  run_id: string;
  sandbox_id: string;
  /** Preview type — "static" | "desktop" (pygame via noVNC); python-server planned. */
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
 * `.py` → desktop. `type` overrides the pick: alone it selects the first
 * candidate of that type; with `entry` it forces the plan type —
 * `{entry: "x.py", type: "static"}` serves the file instead of executing
 * it ("view source"), while `desktop` requires a `.py` entry.
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

/** Liveness probe for the "backend not ready" UI states. */
export async function checkHealth(): Promise<boolean> {
  try {
    const res = await fetch(`${API_BASE}/health`, { cache: "no-store" });
    return res.ok;
  } catch {
    return false;
  }
}
