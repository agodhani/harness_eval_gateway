/**
 * PR32 — /compare transforms: run-spec diff rows, participant/evaluator
 * analytics totals, terminal-reason mix, `?runs=` param parsing.
 *
 * Pure + import-light on purpose — runs under `node --test`
 * (type-stripped); keep runtime imports extension-qualified (`./api.ts`,
 * `./trialGroups.ts`), same pattern as `lib/gridMatrix.ts`.
 */
import {
  harnessConfigLabel,
  runModels,
  type RunAnalytics,
  type RunOut,
  type RunResults,
  type SandboxResult,
} from "./api.ts";
import { meanSd, stableSerialize, type MeanSd } from "./trialGroups.ts";

/** Parse `?runs=` into run ids — comma- or whitespace-separated, trimmed,
 * deduped, order preserved. The page compares the first two. */
export function parseRunIds(raw: string | null | undefined): string[] {
  if (!raw) return [];
  const out: string[] = [];
  for (const part of raw.split(/[\s,]+/)) {
    const id = part.trim();
    if (id && !out.includes(id)) out.push(id);
  }
  return out;
}

// ---------------------------------------------------------------------------
// spec diff
// ---------------------------------------------------------------------------

export interface SpecDiffRow {
  label: string;
  a: string;
  b: string;
  /** Values differ — the row gets the diff highlight. */
  differs: boolean;
  /** Optional long-form values for the cell's title tooltip. */
  titleA?: string;
  titleB?: string;
}

function row(label: string, a: string, b: string, titleA?: string, titleB?: string): SpecDiffRow {
  return { label, a, b, differs: a !== b, titleA, titleB };
}

/** Display value for one submitted harness config — the config `name`
 * when set (PR1), then the matrix-header label, plus the prompt-extra /
 * model-options hint when present so the diff axis reads honestly. */
export function configSummary(hc: Record<string, unknown> | undefined): string {
  const label = harnessConfigLabel(hc);
  const bits: string[] = [];
  const name = String(hc?.name ?? "").trim();
  if (name && name !== label) bits.unshift(`"${name}"`);
  bits.push(label);
  const extra = String(hc?.system_prompt_extra ?? "").trim();
  if (extra) bits.push(`'${extra.length <= 32 ? extra : `${extra.slice(0, 32)}…`}'`);
  const opts = hc?.model_options;
  if (opts && typeof opts === "object") {
    const keys = Object.entries(opts as Record<string, unknown>)
      .filter(([, v]) => v !== null && v !== undefined)
      .map(([k, v]) => `${k}=${String(v)}`);
    if (keys.length) bits.push(`opts{${keys.join(",")}}`);
  }
  return bits.join(" · ");
}

/** One line per config, aligned by submission slot — `config N` rows in
 * the diff table. Long-form (every config on its own line) rides the
 * title tooltips. */
function configRows(a: RunOut, b: RunOut): SpecDiffRow[] {
  // HarnessConfig has no index signature → widen through unknown for the
  // Record<string, unknown>-typed helpers (configSummary/stableSerialize).
  const cfgsA = (a.config?.harness_configs ?? []) as unknown as Record<string, unknown>[];
  const cfgsB = (b.config?.harness_configs ?? []) as unknown as Record<string, unknown>[];
  const rows: SpecDiffRow[] = [];
  rows.push(
    row(
      "harness configs",
      `${cfgsA.length} config${cfgsA.length === 1 ? "" : "s"}`,
      `${cfgsB.length} config${cfgsB.length === 1 ? "" : "s"}`,
      cfgsA.map(configSummary).join("\n"),
      cfgsB.map(configSummary).join("\n"),
    ),
  );
  const n = Math.max(cfgsA.length, cfgsB.length);
  for (let i = 0; i < n; i++) {
    const ca = cfgsA[i];
    const cb = cfgsB[i];
    rows.push({
      label: `  config ${i + 1}`,
      a: ca ? configSummary(ca) : "—",
      b: cb ? configSummary(cb) : "—",
      differs: stableSerialize(ca ?? null) !== stableSerialize(cb ?? null),
      titleA: ca ? stableSerialize(ca) : undefined,
      titleB: cb ? stableSerialize(cb) : undefined,
    });
  }
  return rows;
}

function budgetVal(run: RunOut, key: string): string {
  const b = (run.config?.budgets ?? {}) as Record<string, unknown>;
  const v = b[key];
  if (v === null || v === undefined) return "—";
  if (key === "max_cost_usd") return `$${v}`;
  if (key === "wall_clock_seconds") return `${v}s`;
  return String(v);
}

/** Participant-model set shown on the diff — the shared `runModels` rule:
 * [model_name] + `config.models` extras + per-config model pins. */
export function participantModels(run: RunOut): string[] {
  return runModels(run.model_name, run.config?.models, run.config?.harness_configs);
}

/**
 * The spec-diff table's row set — task + provenance, model axes, per-config
 * alignment, budgets, seed inputs, rubric version. `differs` is a plain
 * string compare on the displayed value EXCEPT the per-config rows, which
 * compare the serialized config (equal configs in different display order
 * still differ honestly — submission order is the alignment axis).
 */
export function specDiffRows(a: RunOut, b: RunOut): SpecDiffRow[] {
  const rows: SpecDiffRow[] = [];
  const trunc = (s: string) => (s.length <= 80 ? s : `${s.slice(0, 80)}…`);
  rows.push({
    label: "task prompt",
    a: trunc(a.task_prompt ?? ""),
    b: trunc(b.task_prompt ?? ""),
    differs: (a.task_prompt ?? "") !== (b.task_prompt ?? ""),
    titleA: a.task_prompt,
    titleB: b.task_prompt,
  });
  const presetLabel = (r: RunOut) =>
    r.preset_ref
      ? `${r.preset_ref}${r.preset_revision != null ? ` @rev ${r.preset_revision}` : ""}`
      : "—";
  rows.push(row("preset", presetLabel(a), presetLabel(b)));
  const suiteLabel = (r: RunOut) =>
    r.suite_ref ? `${r.suite_ref} #${r.suite_index ?? "?"}` : "—";
  rows.push(row("suite", suiteLabel(a), suiteLabel(b)));
  rows.push(row("primary model", a.model_name || "—", b.model_name || "—"));
  const modelsA = participantModels(a).join(", ");
  const modelsB = participantModels(b).join(", ");
  rows.push(row("participant models", modelsA || "—", modelsB || "—", modelsA, modelsB));
  const evalOf = (r: RunOut) =>
    String((r.config?.evaluator_model as string | null | undefined) ?? "").trim() ||
    `${r.model_name} (default)`;
  rows.push(row("evaluator model", evalOf(a), evalOf(b)));
  rows.push(...configRows(a, b));
  const trialsOf = (r: RunOut) =>
    String(r.trials ?? (r.config?.trials as number | undefined) ?? 1);
  rows.push(row("trials / cell", trialsOf(a), trialsOf(b)));
  for (const [key, label] of [
    ["token_budget", "run token budget"],
    ["sandbox_token_budget", "sandbox token cap"],
    ["max_cost_usd", "cost cap"],
    ["max_iters", "max iterations"],
    ["wall_clock_seconds", "wall clock cap"],
  ] as const) {
    rows.push(row(label, budgetVal(a, key), budgetVal(b, key)));
  }
  const inputsOf = (r: RunOut) => {
    const ti = r.config?.task_inputs as
      | { repo_url?: string | null; ref?: string | null; archive_id?: string | null }
      | null
      | undefined;
    if (ti?.repo_url) return `${ti.repo_url}${ti.ref ? ` @${ti.ref}` : ""}`;
    if (ti?.archive_id) return `archive ${String(ti.archive_id).slice(0, 8)}`;
    return "—";
  };
  rows.push(row("task inputs", inputsOf(a), inputsOf(b), inputsOf(a), inputsOf(b)));
  const rubricOf = (r: RunOut) =>
    r.rubric
      ? `v${r.rubric.version} · ${r.rubric.status} · ${(r.rubric.content?.dimensions ?? []).length} dims`
      : "—";
  rows.push(row("rubric", rubricOf(a), rubricOf(b)));
  const sme = (r: RunOut) => (r.same_model_eval ? "yes" : "no");
  rows.push(row("judge ∈ participants", sme(a), sme(b)));
  return rows;
}

// ---------------------------------------------------------------------------
// analytics totals (one number per run for the delta table)
// ---------------------------------------------------------------------------

export interface AnalyticsTotals {
  sandboxCount: number;
  llmTurns: number;
  tokensIn: number;
  tokensOut: number;
  tokensTotal: number;
  errors: number;
  /** Summed per-cell participant spans — busy-time, not run elapsed:
   * cells running in parallel count in full. */
  busySeconds: number;
  /** Summed estimated participant cost; null when any CONTRIBUTING model
   * is unpriced — a partial total would read as complete (PR21 rule).
   * A zero-traffic unlisted model contributes a legitimate $0 and does
   * not poison the total. */
  costUsd: number | null;
  /** Turns-weighted mean of per-model tps means (null when no model
   * reported the PR30 speed fields). */
  avgTokensPerSec: number | null;
  /** Turns-weighted mean of per-model ttft averages. */
  ttftAvgMs: number | null;
  evalTurns: number;
  evalTokensTotal: number;
  evalErrors: number;
  /** Whether the run produced judge-phase activity — distinguishes "no
   * eval traffic" from "unpriced judge model" on a null evalCostUsd. */
  evalSeen: boolean;
  /** Summed judge cost; null when the evaluator model is unpriced or no
   * judge traffic was recorded. */
  evalCostUsd: number | null;
}

/** Roll one run's `RunAnalytics` up to the delta table's totals — the
 * per-model rollups already keep participant vs judge traffic split. */
export function analyticsTotals(a: RunAnalytics | null | undefined): AnalyticsTotals {
  const t: AnalyticsTotals = {
    sandboxCount: 0,
    llmTurns: 0,
    tokensIn: 0,
    tokensOut: 0,
    tokensTotal: 0,
    errors: 0,
    busySeconds: 0,
    costUsd: null,
    avgTokensPerSec: null,
    ttftAvgMs: null,
    evalTurns: 0,
    evalTokensTotal: 0,
    evalErrors: 0,
    evalSeen: false,
    evalCostUsd: null,
  };
  if (!a) return t;
  t.sandboxCount = a.sandboxes?.length ?? 0;
  let cost = 0;
  let costKnown = true;
  let tpsSum = 0;
  let tpsTurns = 0;
  let ttftSum = 0;
  let ttftTurns = 0;
  let evalCost = 0;
  let evalCostKnown = true;
  for (const m of a.models ?? []) {
    t.llmTurns += m.llm_turns;
    t.tokensIn += m.tokens.input;
    t.tokensOut += m.tokens.output;
    t.tokensTotal += m.tokens.total;
    t.errors += m.errors;
    t.busySeconds += m.wall_clock_seconds;
    const hasTraffic = m.llm_turns > 0 || m.tokens.total > 0;
    if (m.estimated_cost_usd == null) {
      if (hasTraffic) costKnown = false;
    } else {
      cost += m.estimated_cost_usd;
    }
    if (m.avg_tokens_per_sec != null && m.llm_turns > 0) {
      tpsSum += m.avg_tokens_per_sec * m.llm_turns;
      tpsTurns += m.llm_turns;
    }
    if (m.ttft_ms?.avg != null && m.llm_turns > 0) {
      ttftSum += m.ttft_ms.avg * m.llm_turns;
      ttftTurns += m.llm_turns;
    }
    const ev = m.evaluator;
    if (ev) {
      t.evalTurns += ev.llm_turns;
      t.evalTokensTotal += ev.tokens.total;
      t.evalErrors += ev.errors;
      t.evalSeen = true;
      if (ev.estimated_cost_usd == null) evalCostKnown = false;
      else evalCost += ev.estimated_cost_usd;
    }
  }
  t.costUsd = costKnown ? cost : null;
  t.avgTokensPerSec = tpsTurns > 0 ? tpsSum / tpsTurns : null;
  t.ttftAvgMs = ttftTurns > 0 ? ttftSum / ttftTurns : null;
  t.evalCostUsd = t.evalSeen ? (evalCostKnown ? evalCost : null) : null;
  return t;
}

// ---------------------------------------------------------------------------
// terminal-reason mix + pooled score
// ---------------------------------------------------------------------------

export interface TerminalSlice {
  reason: string;
  count: number;
  /** share of ALL sandboxes (unterminated lanes stay visibly unaccounted). */
  share: number;
}

export interface TerminalMix {
  slices: TerminalSlice[];
  total: number;
  /** Sandboxes with no terminal_reason yet (still live or failed pre-term). */
  pending: number;
}

/** Terminal-reason histogram over a run's sandboxes — accepts anything
 * carrying `terminal_reason` (SandboxResult or SandboxAnalytics). */
export function terminalMix(
  items: ReadonlyArray<{ terminal_reason: string | null }>,
): TerminalMix {
  const counts = new Map<string, number>();
  let pending = 0;
  for (const it of items) {
    const r = it.terminal_reason;
    if (r) counts.set(r, (counts.get(r) ?? 0) + 1);
    else pending += 1;
  }
  const total = items.length;
  const slices = [...counts.entries()]
    .map(([reason, count]) => ({
      reason,
      count,
      share: total > 0 ? count / total : 0,
    }))
    .sort((x, y) => y.count - x.count || x.reason.localeCompare(y.reason));
  return { slices, total, pending };
}

/** Pooled mean ± sample sd of the run's latest judge `overall` scores. */
export function overallStats(results: RunResults | null | undefined): MeanSd {
  return meanSd(
    (results?.sandboxes ?? []).map((s: SandboxResult) => s.overall?.score),
  );
}

/** Signed delta `b − a` — null when either side is missing (never a 0
 * stand-in for unpriced/unmeasured data). */
export function delta(a: number | null | undefined, b: number | null | undefined): number | null {
  if (a == null || b == null) return null;
  return b - a;
}
