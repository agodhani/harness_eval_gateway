/**
 * PR9 — timeline-side analytics: transform the F9 `RunAnalytics` response into
 * compact, render-ready stat chips for the timeline page's per-sandbox
 * metrics strip (SandboxMetricsBar).
 *
 * Pure + import-light on purpose: it runs under `node --test` (type-stripped)
 * for unit tests — keep runtime imports extension-qualified (`./api.ts`) so
 * Node's ESM resolver finds them without a bundler. Reuses
 * {@link estimateCostUsd} so the strip's cost math stays the single
 * client-side mirror of api/pricing.py.
 */
import {
  estimateCostUsd,
  type ModelPrice,
  type RunAnalytics,
  type SandboxAnalytics,
} from "./api.ts";
import { formatInt, formatUsd } from "./format.ts";

/** One rendered stat on a sandbox chip — `label` pairs with `value`. */
export interface StatChip {
  label: string;
  value: string;
  /** "bad" highlights the value (errors); anything else renders neutral. */
  tone?: "default" | "bad";
  /** Hover text carrying the precise numbers behind the compact value. */
  title?: string;
}

/** One sandbox's strip card: identity/status header + stat chips + judge line. */
export interface SandboxStrip {
  sandboxId: string;
  /** Analytics label ("adapter · pack1, pack2") — mirrors the results matrix. */
  label: string;
  status: string;
  terminalReason: string | null;
  chips: StatChip[];
  /**
   * One-line judge-traffic summary ("judge N turns · T tok · $C"), or null
   * when the evaluator hasn't produced traffic for this sandbox yet.
   */
  evalLine: string | null;
}

/**
 * Convert the analytics response into one strip per sandbox, in API order
 * (server sorts by started_at). `price_model` is resolved server-side — the
 * API 422s unknown models — so it always indexes `price_table`.
 */
export function sandboxStrips(analytics: RunAnalytics): SandboxStrip[] {
  const price = analytics.price_table[analytics.price_model];
  return analytics.sandboxes.map((sb) =>
    toStrip(sb, price, analytics.price_model),
  );
}

function toStrip(
  sb: SandboxAnalytics,
  price: ModelPrice,
  priceName: string,
): SandboxStrip {
  const chips: StatChip[] = [
    {
      label: "llm",
      value: formatInt(sb.llm_turns),
      title: "LLM turns (llm_response events)",
    },
    {
      label: "tok",
      value: `${formatInt(sb.tokens.input)}→${formatInt(sb.tokens.output)}`,
      title:
        `tokens in→out · ${formatInt(sb.tokens.total)} total` +
        (sb.tokens.cache_read > 0
          ? ` · ${formatInt(sb.tokens.cache_read)} cached`
          : ""),
    },
    latencyChip(sb),
    costChip(sb, price, priceName),
    {
      label: "err",
      value: formatInt(sb.errors),
      tone: sb.errors > 0 ? "bad" : "default",
      title: "error events",
    },
  ];
  return {
    sandboxId: sb.sandbox_id,
    label: sb.label,
    status: sb.status,
    terminalReason: sb.terminal_reason,
    chips,
    evalLine: evalLine(sb),
  };
}

function latencyChip(sb: SandboxAnalytics): StatChip {
  const { avg, p95, max } = sb.latency_ms;
  if (avg == null) {
    return { label: "lat", value: "—", title: "no llm_response latency yet" };
  }
  return {
    label: "lat",
    value: `${Math.round(avg)}ms`,
    title: `latency avg · p95 ${Math.round(p95 ?? 0)}ms · max ${Math.round(max ?? 0)}ms`,
  };
}

function costChip(
  sb: SandboxAnalytics,
  price: ModelPrice,
  priceName: string,
): StatChip {
  if (price.local) {
    return { label: "cost", value: "$0", title: `${priceName} — local, $0` };
  }
  return {
    label: "cost",
    value: formatUsd(estimateCostUsd(price, sb.tokens)),
    title: `estimated at ${priceName} ($${price.input_per_mtok_usd}/$${price.output_per_mtok_usd} per Mtok)`,
  };
}

function evalLine(sb: SandboxAnalytics): string | null {
  const ev = sb.evaluator;
  if (!ev) return null;
  return `judge ${formatInt(ev.llm_turns)} turns · ${formatInt(ev.tokens.total)} tok · ${formatUsd(ev.estimated_cost_usd)}`;
}
