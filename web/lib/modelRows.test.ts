/**
 * Unit tests for the submit form's participant-model row helpers
 * (`participantModels`, `modelsPayload`).
 *
 * Runs under Node's built-in test runner — no framework:
 *   cd web && npm test      →   node --test lib/*.test.ts
 * Requires Node ≥22.6 (type-stripped TS).
 *
 * Oracles mirror `orchestrator/matrix.py::participant_models` and the
 * `RunCreate._normalize_models` validator: primary first, dedupe preserving
 * order, blanks dropped; `models` on the wire carries only the extras —
 * the server re-dedupes against `model_name` and caps it at 8 entries, so
 * including the primary there would corrupt the count.
 */
import assert from "node:assert/strict";
import { describe, test } from "node:test";
import {
  modelsPayload,
  participantModels,
  type ModelRowInput,
} from "./modelRows.ts";

function rows(...models: string[]): ModelRowInput[] {
  return models.map((model) => ({ provider: "ollama", model }));
}

describe("participantModels", () => {
  test("single row yields the primary only", () => {
    assert.deepEqual(participantModels(rows("qwen3:8b")), ["qwen3:8b"]);
  });

  test("trims, drops blanks, dedupes preserving row order", () => {
    assert.deepEqual(
      participantModels(rows(" a ", "", "b", "a", "  ", "c", "b")),
      ["a", "b", "c"],
    );
  });

  test("providers are irrelevant to the model set", () => {
    const mixed: ModelRowInput[] = [
      { provider: "ollama", model: "m1" },
      { provider: "openrouter", model: "m2" },
      { provider: "ollama", model: "m1" },
    ];
    assert.deepEqual(participantModels(mixed), ["m1", "m2"]);
  });

  test("blank primary does not suppress a later model", () => {
    assert.deepEqual(participantModels(rows("", "b")), ["b"]);
  });
});

describe("modelsPayload", () => {
  test("single-model run sends models:null (pre-PR14 shape)", () => {
    assert.deepEqual(modelsPayload(rows("a")), {
      model_name: "a",
      models: null,
    });
  });

  test("model_name is row 1 verbatim-trimmed; extras exclude it", () => {
    assert.deepEqual(modelsPayload(rows(" a ", "b", "a", "c", "b")), {
      model_name: "a",
      models: ["b", "c"],
    });
  });

  test("all-duplicate rows collapse back to single-model", () => {
    assert.deepEqual(modelsPayload(rows("a", "a", " a ")), {
      model_name: "a",
      models: null,
    });
  });

  test("blank primary stays blank — required validation flags it", () => {
    assert.deepEqual(modelsPayload(rows("", "b")), {
      model_name: "",
      models: ["b"],
    });
  });

  test("empty row list yields blank primary, null extras", () => {
    assert.deepEqual(modelsPayload([]), { model_name: "", models: null });
  });
});
