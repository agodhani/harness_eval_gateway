/**
 * PR23 — rubric structured-edit helpers.
 *
 * Converts between `RubricContent` (the frozen JSON shape) and the flat,
 * form-friendly `RubricDraft` the structured editor binds inputs to, and
 * validates a draft before save. Mirrors the backend contract in
 * `orchestrator/rubric.py` so client-side errors match what the API would
 * report:
 *
 *   - 1–12 dimensions; every `name` soft-normalizes to snake_case and the
 *     normalized set must be unique (the one structural rejection);
 *   - canonical aliases fold onto the four canonical spellings exactly like
 *     the backend's `_DIMENSION_ALIASES` — a soft normalizer, never a
 *     requirement;
 *   - criterion `dimension` refs normalize the same way; a ref that doesn't
 *     resolve to a declared dimension is flagged (backend leaves it to the
 *     judge but a typo'd ref is nearly always an authoring bug).
 *
 * Pure + import-light like the other lib modules — `node --test`
 * (type-stripped) compatible, no React, no fetch.
 */
import type {
  RubricContent,
  RubricCriterion,
  RubricDimension,
} from "./api.ts";

/** Form shape for one dimension row — every field a string so inputs bind
 * directly; numbers parse at build/validate time. */
export interface DimensionDraft {
  name: string;
  title: string;
  description: string;
  weight: string;
  min_score: string;
  max_score: string;
}

/** Form shape for one criterion row. `dimension` is a select over the
 * declared dim names (plus a free-text fallback for unusual JSON). */
export interface CriterionDraft {
  id: string;
  text: string;
  dimension: string;
  weight: string;
}

export interface RubricDraft {
  summary: string;
  dimensions: DimensionDraft[];
  criteria: CriterionDraft[];
}

export const MIN_DIMENSIONS = 1;
export const MAX_DIMENSIONS = 12;
/** `scores.dimension` is String(128) — keep names under the column bound. */
export const MAX_DIMENSION_NAME = 128;

/** Reserved `scores.dimension` pseudo-rows + flat-map keys the eval parser
 * owns — the backend rejects real dimensions under these names. */
export const RESERVED_DIMENSION_NAMES = new Set([
  "overall",
  "suite_pass",
  "scores",
  "summary",
  "total",
]);

/** Backend `_DIMENSION_ALIASES` — fold LLM re-phrasings onto canonical. */
const DIMENSION_ALIASES: Record<string, string> = {
  functional: "functional_requirements",
  functional_req: "functional_requirements",
  functional_reqs: "functional_requirements",
  requirements: "functional_requirements",
  acceptance: "acceptance_criteria",
  acceptance_tests: "acceptance_criteria",
  code: "code_quality",
  implementation: "code_quality",
  implementation_quality: "code_quality",
  code_implementation: "code_quality",
  code_implementation_quality: "code_quality",
  quality: "code_quality",
  complexity: "cyclomatic_complexity",
  cyclomatic: "cyclomatic_complexity",
};

const NON_ALNUM = /[^a-z0-9]+/g;

/** ``"Code Implementation Quality!!"`` → ``code_implementation_quality``
 * (backend `_normalize_dimension_name`). */
export function normalizeDimensionName(name: string): string {
  return name
    .trim()
    .toLowerCase()
    .replace(NON_ALNUM, "_")
    .replace(/^_+|_+$/g, "");
}

/** snake_case + alias fold — backend `_canonical_dimension`. Returns "" for
 * names made of nothing usable (punctuation only). */
export function canonicalDimension(name: string): string {
  const norm = normalizeDimensionName(name);
  return DIMENSION_ALIASES[norm] ?? norm;
}

const num = (v: unknown): string =>
  typeof v === "number" && Number.isFinite(v) ? String(v) : "";

/** Frozen JSON → form rows. Missing lists become empty; numeric fields
 * become strings; unknown extra keys are preserved by `buildRubricContent`
 * via the `content` it merges over (extras ride along untouched). */
export function draftFromContent(content: RubricContent | null | undefined): RubricDraft {
  const dims = content?.dimensions ?? [];
  const crits = content?.criteria ?? [];
  return {
    summary: typeof content?.summary === "string" ? content.summary : "",
    dimensions: dims.map((d) => ({
      name: d.name ?? "",
      title: d.title ?? "",
      description: d.description ?? "",
      weight: d.weight == null ? "" : num(d.weight),
      min_score: d.min_score == null ? "" : num(d.min_score),
      max_score: d.max_score == null ? "" : num(d.max_score),
    })),
    criteria: crits.map((c) => ({
      id: c.id ?? "",
      text: c.text ?? "",
      dimension: c.dimension ?? "",
      weight: c.weight == null ? "" : num(c.weight),
    })),
  };
}

/** Blank draft for a dimension row (canonical default scale 0–10, w=1). */
export function emptyDimension(name = ""): DimensionDraft {
  return {
    name,
    title: "",
    description: "",
    weight: "1",
    min_score: "0",
    max_score: "10",
  };
}

export function emptyCriterion(): CriterionDraft {
  return { id: "", text: "", dimension: "", weight: "1" };
}

export interface DraftValidation {
  /** Field-level notes keyed `dim.<i>` / `crit.<i>` for inline display. */
  fieldErrors: Record<string, string>;
  /** Blocking problems — shown as a summary list; save must be disabled. */
  errors: string[];
}

function parseNum(raw: string, fallback: number): number {
  const v = raw.trim();
  if (v === "") return fallback;
  const n = Number(v);
  return Number.isFinite(n) ? n : NaN;
}

/** Validate the draft. `normalized` returns the per-row canonical names so
 * the caller can preview the keys scores persist under. */
export function validateDraft(draft: RubricDraft): DraftValidation & {
  normalized: string[];
} {
  const errors: string[] = [];
  const fieldErrors: Record<string, string> = {};
  const normalized: string[] = [];

  if (draft.dimensions.length < MIN_DIMENSIONS) {
    errors.push(`Add at least ${MIN_DIMENSIONS} dimension.`);
  }
  if (draft.dimensions.length > MAX_DIMENSIONS) {
    errors.push(
      `${draft.dimensions.length} dimensions exceeds the ${MAX_DIMENSIONS}-dimension cap.`,
    );
  }

  const seen = new Set<string>();
  const dupes = new Set<string>();
  draft.dimensions.forEach((d, i) => {
    const key = `dim.${i}`;
    const canon = canonicalDimension(d.name);
    normalized.push(canon);
    if (!d.name.trim()) {
      fieldErrors[key] = "name is required";
    } else if (!canon) {
      fieldErrors[key] = `"${d.name}" has no letters/digits — nothing to score under`;
    } else if (RESERVED_DIMENSION_NAMES.has(canon)) {
      fieldErrors[key] = `"${canon}" is a reserved score field — pick another name`;
    } else if (canon.length > MAX_DIMENSION_NAME) {
      fieldErrors[key] = `normalized name is ${canon.length} chars (max ${MAX_DIMENSION_NAME})`;
    }
    if (canon) {
      if (seen.has(canon)) dupes.add(canon);
      seen.add(canon);
    }
    const minS = parseNum(d.min_score, 0);
    const maxS = parseNum(d.max_score, 10);
    const w = parseNum(d.weight, 1);
    if (Number.isNaN(w)) {
      fieldErrors[key] = "weight must be a number";
    } else if (Number.isNaN(minS) || Number.isNaN(maxS)) {
      fieldErrors[key] = "min/max must be numbers";
    } else if (minS >= maxS) {
      fieldErrors[key] = `min_score (${minS}) must be below max_score (${maxS})`;
    }
  });
  if (dupes.size) {
    errors.push(
      `Duplicate dimensions after normalization: ${[...dupes].sort().join(", ")}.`,
    );
  }

  const dimNames = new Set(seen);
  draft.criteria.forEach((c, i) => {
    const key = `crit.${i}`;
    if (!c.text.trim()) {
      fieldErrors[key] = "criterion text is required";
      return;
    }
    const ref = canonicalDimension(c.dimension);
    if (!c.dimension.trim()) {
      fieldErrors[key] = "pick the dimension this criterion informs";
    } else if (ref && dimNames.size && !dimNames.has(ref)) {
      fieldErrors[key] = `"${c.dimension}" → ${ref} — not a declared dimension`;
    }
  });

  return { errors, fieldErrors, normalized };
}

/** Form rows → the RubricContent JSON to persist. Merges over `base` so
 * unknown top-level extras the model added survive an edit round-trip. */
export function buildRubricContent(
  draft: RubricDraft,
  base?: RubricContent | null,
): RubricContent {
  const content: RubricContent = { ...(base ?? {}) };
  content.summary = draft.summary;
  content.dimensions = draft.dimensions.map((d): RubricDimension => {
    const dim: RubricDimension = {
      name: canonicalDimension(d.name) || d.name.trim(),
      title: d.title.trim(),
      description: d.description.trim(),
      weight: parseNum(d.weight, 1),
      min_score: parseNum(d.min_score, 0),
      max_score: parseNum(d.max_score, 10),
    };
    return dim;
  });
  content.criteria = draft.criteria
    .filter((c) => c.text.trim() !== "")
    .map((c): RubricCriterion => {
      const crit: RubricCriterion = {
        text: c.text.trim(),
        dimension: canonicalDimension(c.dimension) || c.dimension.trim(),
        weight: parseNum(c.weight, 1),
      };
      if (c.id.trim()) crit.id = c.id.trim();
      return crit;
    });
  return content;
}
