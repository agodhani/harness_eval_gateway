/**
 * Participant-model row helpers for the submit form (provider-picker
 * redesign). Pure functions — unit-tested in modelRows.test.ts under
 * `node --test`.
 *
 * Mirrors `orchestrator/matrix.py::participant_models` + the `RunCreate`
 * normalizer: row 1 is the primary model (`model_name`, always leads),
 * extras dedupe against it and each other, blanks drop.
 *
 * A row's `provider` is picker metadata only — `RunCreate` carries no
 * per-model provider, so it never enters the payload: it only selects which
 * upstream catalog `getModels(provider)` browses. The run's actual provider
 * routing resolves per model name gateway-side via PROVIDER_ROUTES.
 */

/** One model-picker row's payload-relevant fields. */
export interface ModelRowInput {
  provider: string;
  model: string;
}

/** Deduped, trimmed, non-empty model names in row order — row 1 leads.
 * This is the effective participant-model set the run matrix expands to
 * (`harness_configs × participantModels`). */
export function participantModels(rows: readonly ModelRowInput[]): string[] {
  const out: string[] = [];
  for (const row of rows) {
    const m = row.model.trim();
    if (m && !out.includes(m)) out.push(m);
  }
  return out;
}

export function matrixParticipantModels(
  models: readonly string[],
  configs: readonly { model?: string | null }[],
): string[] {
  const pins = configs.map((config) => config.model?.trim() || null);
  return [...new Set(pins.flatMap((pin) => (pin ? [pin] : models)))];
}

/** Rows → `RunCreate` fields: `model_name` = row 1 verbatim (blank → "",
 * required-validation's job to catch); `models` = the deduped remainder —
 * EXCLUDING the primary, matching the server's "additional models" contract
 * (`max_length=8`, deduped against `model_name` anyway) — `null` when
 * single-model. */
export function modelsPayload(rows: readonly ModelRowInput[]): {
  model_name: string;
  models: string[] | null;
} {
  const model_name = rows[0]?.model.trim() ?? "";
  const extras = participantModels(rows.slice(1)).filter(
    (m) => m !== model_name,
  );
  return { model_name, models: extras.length ? extras : null };
}
