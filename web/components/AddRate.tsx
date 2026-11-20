"use client";

/**
 * PR28 — "add rate" inline control for the price pickers.
 *
 * PUTs a user `model_prices` row (glob-matched rate layered over the static
 * table), then hands the entry back so the caller can fold it into its
 * local price_table — the picker's options and every cost recompute update
 * immediately, and the row persists for future analytics.
 */
import { useState } from "react";
import { ApiError, upsertPrice, type PriceEntry } from "@/lib/api";

function errMsg(err: unknown): string {
  if (err instanceof ApiError) return err.detail;
  if (err instanceof Error) return err.message;
  return "couldn't save rate";
}

export default function AddRate({
  onAdded,
  suggestGlob = "",
  disabled = false,
}: {
  /** Called with the saved entry — parent folds it into its price table. */
  onAdded: (entry: PriceEntry) => void;
  /** Prefill for the glob — e.g. an unlisted model the run just priced. */
  suggestGlob?: string;
  disabled?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const [glob, setGlob] = useState(suggestGlob);
  const [input, setInput] = useState("");
  const [output, setOutput] = useState("");
  const [cacheRead, setCacheRead] = useState("");
  const [local, setLocal] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  function toggle() {
    if (!open) setGlob((g) => g || suggestGlob);
    setError(null);
    setOpen(!open);
  }

  async function save() {
    const g = glob.trim();
    if (!g) {
      setError("glob is required (e.g. gpt-4* or my-model)");
      return;
    }
    const inp = local ? 0 : Number(input);
    const out = local ? 0 : Number(output);
    // isFinite — "1e999" parses to Infinity, passes `>= 0`, then
    // JSON.stringify emits null (a "saved" rate with no rate).
    if (
      !local &&
      (!(Number.isFinite(inp) && inp >= 0) || !(Number.isFinite(out) && out >= 0))
    ) {
      setError("input/output rates must be numbers ≥ 0 ($/Mtok)");
      return;
    }
    const cr = cacheRead.trim();
    const cache = cr === "" ? null : Number(cr);
    if (cache !== null && !(Number.isFinite(cache) && cache >= 0)) {
      setError("cache-read rate must be a number ≥ 0 ($/Mtok)");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const entry = await upsertPrice({
        model_glob: g,
        input_per_mtok_usd: inp,
        output_per_mtok_usd: out,
        cache_read_per_mtok_usd: cache,
        local,
      });
      onAdded(entry);
      setOpen(false);
    } catch (err) {
      setError(errMsg(err));
    } finally {
      setBusy(false);
    }
  }

  if (!open) {
    return (
      <button
        type="button"
        className="btn btn--sm btn--ghost"
        onClick={toggle}
        disabled={disabled}
        title="Add a user price rate (PUT /api/prices) — a glob like gpt-4* reprices every model it covers"
      >
        + add rate
      </button>
    );
  }

  const numField = (
    label: string,
    value: string,
    set: (v: string) => void,
    hint?: string,
  ) => (
    <label className="row" style={{ gap: "0.3rem" }} title={hint}>
      <span className="faint">{label}</span>
      <input
        className="input"
        style={{ width: "5.5rem", padding: "0.25rem 0.4rem", fontSize: "0.8rem" }}
        type="number"
        min={0}
        step="any"
        value={value}
        onChange={(e) => set(e.target.value)}
        disabled={busy || local}
        placeholder="0"
      />
    </label>
  );

  return (
    <span className="row" style={{ gap: "0.4rem", flexWrap: "wrap" }}>
      <input
        className="input mono"
        style={{ width: "10rem", padding: "0.25rem 0.4rem", fontSize: "0.8rem" }}
        value={glob}
        onChange={(e) => setGlob(e.target.value)}
        placeholder="model glob e.g. gpt-4*"
        disabled={busy}
        aria-label="Model glob"
      />
      {numField("in $/Mtok", input, setInput, "USD per million input tokens")}
      {numField("out $/Mtok", output, setOutput, "USD per million output tokens")}
      {numField(
        "cache $/Mtok",
        cacheRead,
        setCacheRead,
        "Optional: discounted rate for cache-read input tokens — empty bills them at the input rate",
      )}
      <label className="row" style={{ gap: "0.25rem" }} title="Free/local inference — metered for tokens, never billed">
        <input
          type="checkbox"
          checked={local}
          onChange={(e) => setLocal(e.target.checked)}
          disabled={busy}
        />
        <span className="faint">local</span>
      </label>
      <button
        type="button"
        className="btn btn--sm"
        onClick={() => void save()}
        disabled={busy}
      >
        {busy ? "saving…" : "save rate"}
      </button>
      <button
        type="button"
        className="btn btn--sm btn--ghost"
        onClick={toggle}
        disabled={busy}
      >
        cancel
      </button>
      {error ? (
        <span className="field__error" role="alert">
          {error}
        </span>
      ) : null}
    </span>
  );
}
