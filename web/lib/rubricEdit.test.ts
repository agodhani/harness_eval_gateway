/**
 * Unit tests for the rubric structured-edit helpers (lib/rubricEdit.ts) —
 * the pure form-state/validation layer behind RubricEditor.
 *
 * Runs under Node's built-in test runner — no framework:
 *   cd web && npm test
 */
import assert from "node:assert/strict";
import { describe, test } from "node:test";
import {
  buildRubricContent,
  canonicalDimension,
  draftFromContent,
  normalizeDimensionName,
  validateDraft,
  type RubricDraft,
} from "./rubricEdit.ts";

const BASE: RubricDraft = {
  summary: "s",
  dimensions: [
    {
      name: "accuracy",
      title: "",
      description: "",
      weight: "1",
      min_score: "0",
      max_score: "10",
    },
  ],
  criteria: [],
};

describe("normalizeDimensionName + canonicalDimension", () => {
  test("snake_cases case/punctuation variants", () => {
    assert.equal(normalizeDimensionName("  Code Quality!! "), "code_quality");
    assert.equal(normalizeDimensionName("Extraction-Accuracy"), "extraction_accuracy");
    assert.equal(normalizeDimensionName("!!!"), "");
  });

  test("aliases fold onto canonical names", () => {
    assert.equal(canonicalDimension("functional"), "functional_requirements");
    assert.equal(canonicalDimension("Acceptance Tests"), "acceptance_criteria");
    assert.equal(canonicalDimension("implementation_quality"), "code_quality");
    assert.equal(canonicalDimension("Complexity"), "cyclomatic_complexity");
  });

  test("arbitrary names pass through untouched", () => {
    assert.equal(canonicalDimension("tone"), "tone");
    assert.equal(canonicalDimension("json_validity"), "json_validity");
  });
});

describe("draftFromContent", () => {
  test("maps numeric fields to strings; missing lists → empty", () => {
    const d = draftFromContent({
      summary: "sum",
      dimensions: [
        { name: "tone", title: "T", description: "d", weight: 2, min_score: 0, max_score: 5 },
      ],
      criteria: [{ id: "c1", text: "t", dimension: "tone", weight: 1.5 }],
    });
    assert.equal(d.summary, "sum");
    assert.deepEqual(d.dimensions[0], {
      name: "tone",
      title: "T",
      description: "d",
      weight: "2",
      min_score: "0",
      max_score: "5",
    });
    assert.equal(d.criteria[0].weight, "1.5");
    assert.deepEqual(draftFromContent(null).dimensions, []);
    assert.deepEqual(draftFromContent({}).criteria, []);
  });
});

describe("validateDraft", () => {
  test("accepts a single arbitrary dimension", () => {
    const v = validateDraft(BASE);
    assert.deepEqual(v.errors, []);
    assert.deepEqual(v.fieldErrors, {});
    assert.deepEqual(v.normalized, ["accuracy"]);
  });

  test("rejects zero dimensions and >12", () => {
    assert.match(validateDraft({ ...BASE, dimensions: [] }).errors[0], /at least 1/);
    const many = Array.from({ length: 13 }, (_, i) => ({
      ...BASE.dimensions[0],
      name: `d${i}`,
    }));
    assert.match(validateDraft({ ...BASE, dimensions: many }).errors[0], /12-dimension cap/);
  });

  test("rejects duplicates after normalization", () => {
    const v = validateDraft({
      ...BASE,
      dimensions: [
        BASE.dimensions[0],
        { ...BASE.dimensions[0], name: "  ACCURACY  " },
        { ...BASE.dimensions[0], name: "ac-cur-acy" },
      ],
    });
    assert.match(v.errors[0], /Duplicate.*accuracy/);
  });

  test("rejects aliases colliding with canonical", () => {
    const v = validateDraft({
      ...BASE,
      dimensions: [
        { ...BASE.dimensions[0], name: "functional" },
        { ...BASE.dimensions[0], name: "functional_requirements" },
      ],
    });
    assert.match(v.errors[0], /Duplicate.*functional_requirements/);
  });

  test("flags empty names, inverted scale, NaN weight", () => {
    const v = validateDraft({
      ...BASE,
      dimensions: [
        { ...BASE.dimensions[0], name: "!!!" },
        { ...BASE.dimensions[0], name: "ok", min_score: "10", max_score: "10" },
        { ...BASE.dimensions[0], name: "other", weight: "abc" },
      ],
    });
    assert.match(v.fieldErrors["dim.0"], /no letters\/digits/);
    assert.match(v.fieldErrors["dim.1"], /min_score.*below max_score/);
    assert.match(v.fieldErrors["dim.2"], /weight must be a number/);
  });

  test("flags reserved score-field names", () => {
    for (const name of ["Overall", "suite_pass", "total"]) {
      const v = validateDraft({
        ...BASE,
        dimensions: [{ ...BASE.dimensions[0], name }],
      });
      assert.match(v.fieldErrors["dim.0"], /reserved score field/);
    }
  });

  test("flags criterion refs that resolve to undeclared dims", () => {
    const v = validateDraft({
      ...BASE,
      criteria: [
        { id: "", text: "x", dimension: "tone", weight: "1" },
        { id: "", text: "", dimension: "accuracy", weight: "1" },
      ],
    });
    assert.match(v.fieldErrors["crit.0"], /not a declared dimension/);
    assert.match(v.fieldErrors["crit.1"], /text is required/);
  });

  test("accepts criterion refs through aliases", () => {
    const v = validateDraft({
      summary: "",
      dimensions: [{ ...BASE.dimensions[0], name: "functional_requirements" }],
      criteria: [{ id: "", text: "t", dimension: "Functional", weight: "1" }],
    });
    assert.deepEqual(v.fieldErrors, {});
  });
});

describe("buildRubricContent", () => {
  test("emits normalized names + parsed numbers; drops empty criteria", () => {
    const c = buildRubricContent({
      summary: "s",
      dimensions: [
        { ...BASE.dimensions[0], name: "Code Implementation Quality", weight: "2" },
      ],
      criteria: [
        { id: "", text: "  ", dimension: "code_quality", weight: "1" },
        { id: "c1", text: "t", dimension: "Code Quality", weight: "1.5" },
      ],
    });
    // alias: code_implementation_quality → code_quality
    assert.equal(c.dimensions?.[0]?.name, "code_quality");
    assert.equal(c.dimensions?.[0]?.weight, 2);
    assert.equal(c.criteria?.length, 1);
    assert.equal(c.criteria?.[0]?.dimension, "code_quality");
    assert.equal(c.criteria?.[0]?.id, "c1");
  });

  test("preserves base extras (generated_by, unknown keys)", () => {
    const c = buildRubricContent(BASE, {
      summary: "old",
      generated_by: { model: "m" },
      custom_key: "keep me",
      dimensions: [{ name: "x" }],
    });
    assert.deepEqual(c.generated_by, { model: "m" });
    assert.equal(c.custom_key, "keep me");
    assert.equal(c.summary, "s");
    assert.equal(c.dimensions?.[0]?.name, "accuracy");
  });
});
