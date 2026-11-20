/**
 * matchPriceKey mirrors api/pricing.py::PriceBook.key_for — the layered
 * price book's resolution contract:
 *   1. model_prices rows in declaration order, first glob-match wins
 *      (glob matching is case-insensitive; a bare pattern never matches a
 *      vendor/model id);
 *   2. else the model itself when it is a static PRICE_TABLE key
 *      (case-sensitive, like Python's `in` on the dict);
 *   3. else unpriced.
 *
 * Oracles are derived by hand from those rules — e.g. a row glob covering
 * a static key returns the GLOB key, and a literal row key matches a
 * differently-cased model while a static key does not.
 *
 *   cd web && npm test  →  node --test (type-stripped TS, Node ≥22.6)
 */
import assert from "node:assert/strict";
import { describe, test } from "node:test";
import { globMatch, matchPriceKey, type ModelPrice } from "./api.ts";

const rate = (n: number): ModelPrice => ({
  input_per_mtok_usd: n,
  output_per_mtok_usd: n,
});

// Merged price_table shape: static keys first, then model_prices row
// globs appended in declaration order (a row glob equal to a static key
// overlays it in place).
const TABLE: Record<string, ModelPrice> = {
  "claude-sonnet-4.5": rate(3),
  "gpt-4o": rate(2.5),
  "qwen3:8b": { ...rate(0), local: true },
  "claude-*": rate(1),
};
const GLOBS = ["claude-*"];

describe("matchPriceKey", () => {
  test("a row glob covering a static key wins over the static layer", () => {
    // server: key_for("claude-sonnet-4.5") → "claude-*" (row declared
    // before the static fallback is even consulted)
    assert.equal(
      matchPriceKey("claude-sonnet-4.5", TABLE, GLOBS),
      "claude-*",
    );
  });

  test("first declared matching row wins", () => {
    const table = { ...TABLE, "claude-sonnet-*": rate(9) };
    assert.equal(
      matchPriceKey("claude-sonnet-4.5", table, [
        "claude-*",
        "claude-sonnet-*",
      ]),
      "claude-*",
    );
    // reverse the declaration order and the specific row wins instead
    assert.equal(
      matchPriceKey("claude-sonnet-4.5", table, [
        "claude-sonnet-*",
        "claude-*",
      ]),
      "claude-sonnet-*",
    );
  });

  test("a literal row (no glob chars) matches the model case-insensitively", () => {
    const table = { ...TABLE, MyModel: rate(7) };
    assert.equal(matchPriceKey("mymodel", table, [...GLOBS, "MyModel"]), "MyModel");
  });

  test("a literal row declared before a glob row keeps precedence", () => {
    const table = { ...TABLE, "gpt-4o-pro": rate(9), "gpt-*": rate(1) };
    assert.equal(
      matchPriceKey("gpt-4o-pro", table, ["gpt-4o-pro", "gpt-*"]),
      "gpt-4o-pro",
    );
  });

  test("static key is the fallback when no row matches", () => {
    assert.equal(matchPriceKey("gpt-4o", TABLE, GLOBS), "gpt-4o");
  });

  test("unlisted model resolves to no key — case-variant static misses too", () => {
    // server: "GPT-4O" in PRICE_TABLE is a case-sensitive dict lookup →
    // unpriced; glob rows are still consulted case-insensitively.
    assert.equal(matchPriceKey("GPT-4O", TABLE, GLOBS), undefined);
    assert.equal(matchPriceKey("nope-1b", TABLE, GLOBS), undefined);
    assert.equal(matchPriceKey(null, TABLE, GLOBS), undefined);
  });

  test("a glob row still covers a differently-cased model", () => {
    assert.equal(matchPriceKey("CLAUDE-SONNET-4.5", TABLE, GLOBS), "claude-*");
  });
});

// globMatch ports Python's fnmatch semantics — oracles below derived from
// fnmatch.fnmatchcase behavior: [!seq] is a NEGATED class, a leading "]"
// inside a class is a literal, and an unclosed "[" matches literal text.
describe("globMatch", () => {
  test("* and ? wildcards", () => {
    assert.equal(globMatch("gpt-4*", "gpt-4o-mini"), true);
    assert.equal(globMatch("gpt-4?", "gpt-4o"), true);
    assert.equal(globMatch("gpt-4?", "gpt-4o-mini"), false);
  });

  test("[seq] matches exactly one char in the set; [a-z] ranges work", () => {
    // a class consumes ONE char — "modelv2" is model + TWO chars → no.
    assert.equal(globMatch("model[v12]", "modelv"), true);
    assert.equal(globMatch("model[v12]", "model1"), true);
    assert.equal(globMatch("model[v12]", "modelx"), false);
    assert.equal(globMatch("model[v12]", "modelv2"), false);
    assert.equal(globMatch("model-[a-c]", "model-b"), true);
    assert.equal(globMatch("model-[a-c]", "model-d"), false);
  });

  test("[!seq] NEGATES the class (fnmatch semantics)", () => {
    // modelc: "c" ∉ {a,b} → matches — the naive regex pass inverted this
    // ([!ab] as a JS class would match literal !, a, b).
    assert.equal(globMatch("model[!ab]", "modelc"), true);
    assert.equal(globMatch("model[!ab]", "model!"), true);
    assert.equal(globMatch("model[!ab]", "modela"), false);
    assert.equal(globMatch("model[!ab]", "modelb"), false);
  });

  test("a leading ] inside the class is a literal", () => {
    // Python: glob "[]a]" = class matching "]" or "a".
    assert.equal(globMatch("m[]a]", "m]"), true);
    assert.equal(globMatch("m[]a]", "ma"), true);
    assert.equal(globMatch("m[]a]", "mb"), false);
  });

  test("an unclosed [ matches the literal text", () => {
    // Python: "model[v2" has no closing "]" → the "[" is literal.
    assert.equal(globMatch("model[v2", "model[v2"), true);
    assert.equal(globMatch("model[v2", "modelv2"), false);
  });

  test("case-insensitive; bare pattern never spans a vendor/ id", () => {
    assert.equal(globMatch("GPT-4*", "gpt-4o"), true);
    assert.equal(globMatch("gpt-4*", "openai/gpt-4o"), false);
    assert.equal(globMatch("*/gpt-4o", "openai/gpt-4o"), true);
  });

  test("matchPriceKey sees [seq] rows through globMatch", () => {
    const table = { ...TABLE, "acme-[v2]*": rate(4) };
    assert.equal(
      matchPriceKey("acme-v2x-7b", table, ["acme-[v2]*"]),
      "acme-[v2]*",
    );
    // "x" ∉ {v,2} → no match (and "v3x" DOES match — v is in the class)
    assert.equal(
      matchPriceKey("acme-v3x-7b", table, ["acme-[v2]*"]),
      "acme-[v2]*",
    );
    assert.equal(matchPriceKey("acme-x3-7b", table, ["acme-[v2]*"]), undefined);
  });
});
