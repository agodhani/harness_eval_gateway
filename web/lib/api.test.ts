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
import {
  estimateCostUsd,
  globMatch,
  matchPriceKey,
  resolvePricePick,
  type ModelPrice,
} from "./api.ts";

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

// resolvePricePick mirrors api/pricing.py::resolve_price_model — the
// explicit-pick contract:
//   1. a pick naming a model_prices row glob (case-insensitively — row
//      identity is) selects THAT row, so an earlier broader glob can't
//      shadow it;
//   2. any other pick resolves like a model through the layered book.
// Oracles are derived from those rules — e.g. picking a static key that a
// row glob covers resolves to the GLOB, while picking the glob itself (in
// any casing) keeps the row's own key.
describe("resolvePricePick", () => {
  const PICK_TABLE: Record<string, ModelPrice> = {
    "gpt-4o": rate(2.5),
    "*": rate(0.5),
    "acme-*": rate(9),
  };
  const PICK_GLOBS = ["*", "acme-*"];

  test("a verbatim row-glob pick selects that row, unshadowed by earlier globs", () => {
    assert.equal(resolvePricePick("acme-*", PICK_TABLE, PICK_GLOBS), "acme-*");
  });

  test("a case-variant row pick still names the row", () => {
    // server: price_model.lower() in _by_glob → the row's own glob
    assert.equal(resolvePricePick("ACME-*", PICK_TABLE, PICK_GLOBS), "acme-*");
  });

  test("a model-like pick resolves through the book — earlier glob shadows a static key", () => {
    // server: key_for("gpt-4o") hits the "*" row before the static layer
    assert.equal(resolvePricePick("gpt-4o", PICK_TABLE, PICK_GLOBS), "*");
    // with no covering row, the static key is its own pick
    assert.equal(resolvePricePick("gpt-4o", TABLE, GLOBS), "gpt-4o");
  });

  test("an unlisted pick resolves to no key", () => {
    assert.equal(resolvePricePick("nope-1b", TABLE, GLOBS), undefined);
    assert.equal(resolvePricePick(null, TABLE, GLOBS), undefined);
    assert.equal(resolvePricePick(undefined, TABLE, GLOBS), undefined);
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

// estimateCostUsd mirrors api/pricing.py::estimate_cost_usd — oracles from
// the Python contract: a non-positive cache_read counts zero cached
// tokens, and cache_read is clamped to the input total.
describe("estimateCostUsd", () => {
  const price: ModelPrice = {
    input_per_mtok_usd: 10,
    output_per_mtok_usd: 20,
    cache_read_per_mtok_usd: 1,
  };

  test("a negative cache_read bills nothing at the cache rate", () => {
    // cached=0 → 10 in @ $10 + 20 out @ $20 = $500 worth /1e6
    assert.equal(
      estimateCostUsd(price, { input: 10, output: 20, cache_read: -5 }),
      (10 * 10 + 20 * 20) / 1_000_000,
    );
  });

  test("cache_read over the input total clamps to it", () => {
    // all 10 input cached @ $1 + 20 out @ $20 = $210 worth /1e6
    assert.equal(
      estimateCostUsd(price, { input: 10, output: 20, cache_read: 50 }),
      (10 * 1 + 20 * 20) / 1_000_000,
    );
  });
});
