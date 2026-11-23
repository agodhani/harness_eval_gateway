"use client";

/**
 * PR23 — structured rubric editor, shared by the run-detail draft review
 * and the results-page "new version" editor.
 *
 * Replaces the raw-JSON textarea with a form: summary, dimension rows
 * (name/title/description/weight/min–max scale) and a criteria list
 * (id/text/dimension/weight). A "Raw JSON" toggle drops back to the
 * free-text editor for the fields the form doesn't model (extras ride
 * along on the content object untouched).
 *
 * Controlled: the parent owns the `content` working copy; every edit calls
 * `onChange(content, meta)` where `meta.valid` reports whether the current
 * state could save (form-validation or JSON-parse outcome) — parents wire
 * that into their save-button `disabled`. Remount via `key=` when the base
 * rubric changes so the internal draft re-derives.
 */
import { useMemo, useState } from "react";
import type { RubricContent } from "@/lib/api";
import {
  buildRubricContent,
  canonicalDimension,
  draftFromContent,
  emptyCriterion,
  emptyDimension,
  MAX_DIMENSIONS,
  validateDraft,
  type RubricDraft,
} from "@/lib/rubricEdit";

export interface RubricEditMeta {
  valid: boolean;
  errors: string[];
}

export default function RubricEditor({
  content,
  onChange,
  disabled = false,
  /** Text under the summary field — callers pass their version context. */
  note,
}: {
  content: RubricContent;
  onChange: (content: RubricContent, meta: RubricEditMeta) => void;
  disabled?: boolean;
  note?: React.ReactNode;
}) {
  const [mode, setMode] = useState<"form" | "json">("form");
  const [draft, setDraft] = useState<RubricDraft>(() => draftFromContent(content));
  const [raw, setRaw] = useState("");
  const [rawError, setRawError] = useState<string | null>(null);

  const validation = useMemo(() => validateDraft(draft), [draft]);
  const dimNames = useMemo(
    () => validation.normalized.filter((n): n is string => !!n),
    [validation.normalized],
  );

  function emit(next: RubricDraft) {
    setDraft(next);
    const v = validateDraft(next);
    onChange(buildRubricContent(next, content), {
      valid: v.errors.length === 0 && Object.keys(v.fieldErrors).length === 0,
      errors: v.errors,
    });
  }

  function setDim(i: number, patch: Partial<RubricDraft["dimensions"][number]>) {
    emit({
      ...draft,
      dimensions: draft.dimensions.map((d, j) => (j === i ? { ...d, ...patch } : d)),
    });
  }

  function setCrit(i: number, patch: Partial<RubricDraft["criteria"][number]>) {
    emit({
      ...draft,
      criteria: draft.criteria.map((c, j) => (j === i ? { ...c, ...patch } : c)),
    });
  }

  function toJson() {
    // Serialize the structured draft so form edits aren't lost across the
    // toggle — the raw editor starts from the current working copy.
    setRaw(JSON.stringify(buildRubricContent(draft, content), null, 2));
    setRawError(null);
    setMode("json");
  }

  function onRaw(next: string) {
    setRaw(next);
    try {
      const v: unknown = JSON.parse(next);
      if (!v || typeof v !== "object" || Array.isArray(v)) {
        throw new Error("rubric content must be a JSON object");
      }
      setRawError(null);
      onChange(v as RubricContent, { valid: true, errors: [] });
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      setRawError(msg);
      onChange(content, { valid: false, errors: [msg] });
    }
  }

  function toForm() {
    // Re-derive the form from the last valid raw parse; invalid raw keeps
    // the prior draft so a half-typed JSON blob can't destroy form edits.
    try {
      const v: unknown = JSON.parse(raw);
      if (v && typeof v === "object" && !Array.isArray(v)) {
        const next = draftFromContent(v as RubricContent);
        setDraft(next);
        const check = validateDraft(next);
        onChange(buildRubricContent(next, content), {
          valid: check.errors.length === 0 && Object.keys(check.fieldErrors).length === 0,
          errors: check.errors,
        });
      }
    } catch {
      /* stay on the previous draft */
    }
    setMode("form");
  }

  return (
    <div className="stack">
      <div className="row" style={{ justifyContent: "space-between", flexWrap: "wrap", gap: "0.5rem" }}>
        {note ? <p className="field__hint" style={{ margin: 0 }}>{note}</p> : <span />}
        <button
          type="button"
          className="btn btn--sm btn--ghost"
          onClick={mode === "form" ? toJson : toForm}
          disabled={disabled}
        >
          {mode === "form" ? "Edit raw JSON" : "Structured editor"}
        </button>
      </div>

      {mode === "json" ? (
        <div className="stack">
          <p className="field__hint" style={{ margin: 0 }}>
            Rubric JSON — 1–{MAX_DIMENSIONS} dimensions, snake_case names,
            unique after normalization. Structured fields sync back when the
            JSON parses.
          </p>
          <textarea
            className="textarea"
            style={{ minHeight: "320px" }}
            value={raw}
            onChange={(e) => onRaw(e.target.value)}
            disabled={disabled}
            spellCheck={false}
          />
          {rawError ? <p className="field__error">Invalid JSON: {rawError}</p> : null}
        </div>
      ) : (
        <div className="stack">
          <label className="field">
            <span className="field__label">Summary</span>
            <input
              className="input"
              value={draft.summary}
              onChange={(e) => emit({ ...draft, summary: e.target.value })}
              disabled={disabled}
              placeholder="What this rubric grades, in one line"
            />
          </label>

          <div className="stack" style={{ gap: "0.5rem" }}>
            <div className="row" style={{ justifyContent: "space-between" }}>
              <span className="field__label" style={{ marginBottom: 0 }}>
                Dimensions{" "}
                <span className="faint">
                  ({draft.dimensions.length}/{MAX_DIMENSIONS})
                </span>
              </span>
              <button
                type="button"
                className="btn btn--sm btn--ghost"
                onClick={() =>
                  emit({ ...draft, dimensions: [...draft.dimensions, emptyDimension()] })
                }
                disabled={disabled || draft.dimensions.length >= MAX_DIMENSIONS}
              >
                + Add dimension
              </button>
            </div>

            {draft.dimensions.map((d, i) => {
              const canon = canonicalDimension(d.name);
              const err = validation.fieldErrors[`dim.${i}`];
              return (
                <div
                  key={i}
                  className="card"
                  style={{ padding: "0.6rem", display: "grid", gap: "0.45rem" }}
                >
                  <div className="row" style={{ gap: "0.45rem", flexWrap: "wrap" }}>
                    <input
                      className="input input--mono"
                      style={{ flex: "2 1 12rem" }}
                      value={d.name}
                      onChange={(e) => setDim(i, { name: e.target.value })}
                      disabled={disabled}
                      placeholder="dimension_name"
                    />
                    <input
                      className="input"
                      style={{ flex: "2 1 10rem" }}
                      value={d.title}
                      onChange={(e) => setDim(i, { title: e.target.value })}
                      disabled={disabled}
                      placeholder="Title"
                    />
                    <label className="faint" style={{ fontSize: "0.75rem" }}>
                      w{" "}
                      <input
                        className="input"
                        style={{ width: "4.5rem" }}
                        value={d.weight}
                        onChange={(e) => setDim(i, { weight: e.target.value })}
                        disabled={disabled}
                        inputMode="decimal"
                      />
                    </label>
                    <label className="faint" style={{ fontSize: "0.75rem" }}>
                      min{" "}
                      <input
                        className="input"
                        style={{ width: "4.5rem" }}
                        value={d.min_score}
                        onChange={(e) => setDim(i, { min_score: e.target.value })}
                        disabled={disabled}
                        inputMode="decimal"
                      />
                    </label>
                    <label className="faint" style={{ fontSize: "0.75rem" }}>
                      max{" "}
                      <input
                        className="input"
                        style={{ width: "4.5rem" }}
                        value={d.max_score}
                        onChange={(e) => setDim(i, { max_score: e.target.value })}
                        disabled={disabled}
                        inputMode="decimal"
                      />
                    </label>
                    <button
                      type="button"
                      className="btn btn--sm btn--ghost"
                      onClick={() =>
                        emit({
                          ...draft,
                          dimensions: draft.dimensions.filter((_, j) => j !== i),
                        })
                      }
                      disabled={disabled}
                      title="Remove dimension"
                    >
                      ✕
                    </button>
                  </div>
                  <input
                    className="input"
                    value={d.description}
                    onChange={(e) => setDim(i, { description: e.target.value })}
                    disabled={disabled}
                    placeholder="What this axis measures — shown to the judge"
                  />
                  <div className="row" style={{ justifyContent: "space-between" }}>
                    {canon && canon !== d.name.trim() ? (
                      <span className="faint" style={{ fontSize: "0.72rem" }}>
                        scores under <code>{canon}</code>
                      </span>
                    ) : (
                      <span />
                    )}
                    {err ? (
                      <span className="field__error" style={{ fontSize: "0.75rem" }}>{err}</span>
                    ) : null}
                  </div>
                </div>
              );
            })}
            {draft.dimensions.length === 0 ? (
              <p className="faint" style={{ fontSize: "0.8rem", margin: 0 }}>
                No dimensions yet — add at least one scored axis.
              </p>
            ) : null}
          </div>

          <div className="stack" style={{ gap: "0.5rem" }}>
            <div className="row" style={{ justifyContent: "space-between" }}>
              <span className="field__label" style={{ marginBottom: 0 }}>
                Criteria <span className="faint">({draft.criteria.length})</span>
              </span>
              <button
                type="button"
                className="btn btn--sm btn--ghost"
                onClick={() =>
                  emit({ ...draft, criteria: [...draft.criteria, emptyCriterion()] })
                }
                disabled={disabled}
              >
                + Add criterion
              </button>
            </div>
            {draft.criteria.map((c, i) => {
              const err = validation.fieldErrors[`crit.${i}`];
              const custom =
                c.dimension.trim() !== "" &&
                !dimNames.includes(canonicalDimension(c.dimension));
              return (
                <div key={i} className="stack" style={{ gap: "0.3rem" }}>
                  <div className="row" style={{ gap: "0.45rem", flexWrap: "wrap" }}>
                    <input
                      className="input input--mono"
                      style={{ width: "6rem" }}
                      value={c.id}
                      onChange={(e) => setCrit(i, { id: e.target.value })}
                      disabled={disabled}
                      placeholder="id"
                    />
                    <input
                      className="input"
                      style={{ flex: "3 1 16rem" }}
                      value={c.text}
                      onChange={(e) => setCrit(i, { text: e.target.value })}
                      disabled={disabled}
                      placeholder="Checkable requirement the submission must meet"
                    />
                    <select
                      className="select"
                      value={custom ? "__custom__" : canonicalDimension(c.dimension)}
                      onChange={(e) => {
                        if (e.target.value !== "__custom__") {
                          setCrit(i, { dimension: e.target.value });
                        }
                      }}
                      disabled={disabled}
                      style={{ minWidth: "12rem" }}
                    >
                      <option value="">dimension…</option>
                      {dimNames.map((n) => (
                        <option key={n} value={n}>
                          {n}
                        </option>
                      ))}
                      {custom ? (
                        <option value="__custom__">{c.dimension}</option>
                      ) : null}
                    </select>
                    <label className="faint" style={{ fontSize: "0.75rem" }}>
                      w{" "}
                      <input
                        className="input"
                        style={{ width: "4.5rem" }}
                        value={c.weight}
                        onChange={(e) => setCrit(i, { weight: e.target.value })}
                        disabled={disabled}
                        inputMode="decimal"
                      />
                    </label>
                    <button
                      type="button"
                      className="btn btn--sm btn--ghost"
                      onClick={() =>
                        emit({
                          ...draft,
                          criteria: draft.criteria.filter((_, j) => j !== i),
                        })
                      }
                      disabled={disabled}
                      title="Remove criterion"
                    >
                      ✕
                    </button>
                  </div>
                  {err ? (
                    <span className="field__error" style={{ fontSize: "0.75rem" }}>{err}</span>
                  ) : null}
                </div>
              );
            })}
          </div>

          {validation.errors.length > 0 ? (
            <ul className="field__error" style={{ margin: 0, paddingLeft: "1.2rem" }}>
              {validation.errors.map((e, i) => (
                <li key={i}>{e}</li>
              ))}
            </ul>
          ) : null}
        </div>
      )}
    </div>
  );
}
