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
import { matchPriceKey, type ModelPrice } from "./api.ts";

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
