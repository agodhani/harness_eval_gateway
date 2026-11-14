"use client";

/**
 * PR5 — eval iteration controls for the results page.
 *
 * - Rubric-version selector drives `?rubric_version=` on the results matrix
 *   ("Latest" = server's newest-wins view across all passes).
 * - "Re-score" re-runs the evaluator on stored artifacts (no participant
 *   sandboxes), optionally pinned to the selected version.
 * - "Edit rubric" prefills a JSON editor from the selected/latest version and
 *   saves as a NEW version (the frozen row is never mutated); optionally
 *   queues a re-score against it in the same request.
 *
 * Only meaningful once the run is terminal — buttons are disabled otherwise.
 */
import { useMemo, useState } from "react";
import {
  ApiError,
  createRubricVersion,
  getRubricVersion,
  rescoreRun,
  TERMINAL_RUN_STATUSES,
  type RubricContent,
  type RubricOut,
  type RunResults,
} from "@/lib/api";
import { ErrorNote, InfoNote } from "@/components/ui";

function errMsg(err: unknown, fallback: string): string {
  if (err instanceof ApiError) return err.detail;
  if (err instanceof Error) return err.message;
  return fallback;
}

export default function RescoreControls({
  runId,
  results,
  latestRubric,
  selectedVersion,
  onSelectVersion,
  onChanged,
}: {
  runId: string;
  results: RunResults;
  /** Latest rubric row from run detail (null if the run has none). */
  latestRubric: RubricOut | null;
  /** Pinned version number, or null for the newest-wins view. */
  selectedVersion: number | null;
  onSelectVersion: (v: number | null) => void;
  /** Called after any mutation (re-score queued, version created). */
  onChanged: () => void;
}) {
  const [busy, setBusy] = useState<"rescore" | "save" | "save_rescore" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [editing, setEditing] = useState(false);
  const [draftJson, setDraftJson] = useState("");
  const [baseRubricId, setBaseRubricId] = useState<string | null>(null);
  const [baseLabel, setBaseLabel] = useState("");

  const versions = useMemo(
    // results lists newest first; the selector reads oldest → newest.
    () => [...(results.rubric_versions ?? [])].sort((a, b) => a.version - b.version),
    [results.rubric_versions],
  );
  const selectedRow = versions.find((v) => v.version === selectedVersion) ?? null;
  const terminal = TERMINAL_RUN_STATUSES.includes(results.status);
  const hasArtifacts = results.sandboxes.some((sb) => !!sb.artifact_path);
  const nextVersion = (versions[versions.length - 1]?.version ?? 0) + 1;

  async function startEdit() {
    setError(null);
    setNotice(null);
    // Edit against the pinned version when one is selected, else the latest.
    let base = latestRubric;
    if (selectedRow && selectedRow.id !== latestRubric?.id) {
      try {
        base = await getRubricVersion(selectedRow.id);
      } catch (err) {
        setError(errMsg(err, "Could not load that rubric version."));
        return;
      }
    }
    if (!base) {
      setError("No rubric to edit — this run has none.");
      return;
    }
    setBaseRubricId(base.id);
    setBaseLabel(`v${base.version}`);
    setDraftJson(JSON.stringify(base.content ?? {}, null, 2));
    setEditing(true);
  }

  async function doRescore() {
    setError(null);
    setNotice(null);
    setBusy("rescore");
    try {
      const out = await rescoreRun(
        runId,
        selectedRow ? { rubric_id: selectedRow.id } : {},
      );
      setNotice(
        `Re-score queued — evaluator re-judging ${out.sandboxes} artifact(s) ` +
          `against rubric v${out.rubric_version} (${out.model}).`,
      );
      onChanged();
    } catch (err) {
      setError(errMsg(err, "Re-score failed."));
    } finally {
      setBusy(null);
    }
  }

  async function saveVersion(andRescore: boolean) {
    setError(null);
    setNotice(null);
    let parsed: RubricContent;
    try {
      const v: unknown = JSON.parse(draftJson);
      if (v === null || typeof v !== "object" || Array.isArray(v)) {
        throw new Error("rubric content must be a JSON object");
      }
      parsed = v as RubricContent;
    } catch (err) {
      setError(`Invalid JSON: ${err instanceof Error ? err.message : String(err)}`);
      return;
    }
    setBusy(andRescore ? "save_rescore" : "save");
    try {
      const out = await createRubricVersion(runId, {
        content: parsed,
        base_rubric_id: baseRubricId,
        rescore: andRescore,
      });
      setEditing(false);
      setNotice(
        andRescore
          ? `Rubric v${out.version} saved (edit of ${baseLabel}) — re-score queued.`
          : `Rubric v${out.version} saved (edit of ${baseLabel}).`,
      );
      // The new version becomes the latest — drop the pinned view (which
      // also triggers a refetch through the parent).
      onSelectVersion(null);
    } catch (err) {
      setError(errMsg(err, "Failed to save rubric version."));
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="stack">
      <div className="row" style={{ flexWrap: "wrap", gap: "0.5rem" }}>
        <label className="field__label" style={{ marginBottom: 0 }}>
          Rubric version
        </label>
        <select
          className="select"
          value={selectedVersion === null ? "" : String(selectedVersion)}
          onChange={(e) =>
            onSelectVersion(e.target.value === "" ? null : Number(e.target.value))
          }
          disabled={versions.length === 0}
        >
          <option value="">Latest scores (newest pass wins)</option>
          {versions.map((v) => (
            <option key={v.id} value={v.version}>
              v{v.version} · {v.status}
              {v.score_count > 0 ? ` · ${v.score_count} scores` : " · unscored"}
            </option>
          ))}
        </select>
        <button
          type="button"
          className="btn btn--sm btn--primary"
          onClick={doRescore}
          disabled={busy !== null || !terminal || !hasArtifacts}
          title={
            !terminal
              ? "re-score is available once the run is terminal"
              : !hasArtifacts
                ? "no artifact-bearing sandboxes to re-score"
                : selectedRow
                  ? `re-run the evaluator against rubric v${selectedRow.version}`
                  : "re-run the evaluator against the latest approved rubric"
          }
        >
          {busy === "rescore" ? "Queuing…" : "Re-score artifacts"}
        </button>
        <button
          type="button"
          className="btn btn--sm"
          onClick={editing ? () => setEditing(false) : startEdit}
          disabled={busy !== null || !terminal || !latestRubric}
          title={
            !terminal
              ? "rubric edits are available once the run is terminal"
              : "save the rubric as a new version"
          }
        >
          {editing ? "Close editor" : "Edit rubric"}
        </button>
      </div>

      {selectedRow ? (
        <p className="faint" style={{ fontSize: "0.8rem", margin: 0 }}>
          Viewing scores judged under rubric v{selectedRow.version}
          {selectedRow.status === "superseded" ? " (superseded)" : ""} —{" "}
          {selectedRow.score_count} score row{selectedRow.score_count === 1 ? "" : "s"}.
        </p>
      ) : versions.length > 1 ? (
        <p className="faint" style={{ fontSize: "0.8rem", margin: 0 }}>
          Latest view — each sandbox shows its newest judged scores. Pick a
          version to compare older score sets.
        </p>
      ) : null}

      {editing ? (
        <div className="stack">
          <p className="field__hint" style={{ margin: 0 }}>
            Editing <b>{baseLabel}</b> — saving creates <b>v{nextVersion}</b>;{" "}
            {baseLabel} stays frozen. <code>dimensions</code> must contain exactly:{" "}
            <code>functional_requirements</code>, <code>acceptance_criteria</code>,{" "}
            <code>code_quality</code>, <code>cyclomatic_complexity</code>.
          </p>
          <textarea
            className="textarea"
            style={{ minHeight: "280px" }}
            value={draftJson}
            onChange={(e) => setDraftJson(e.target.value)}
            disabled={busy !== null}
            spellCheck={false}
          />
          <div className="row" style={{ gap: "0.5rem" }}>
            <button
              type="button"
              className="btn btn--sm"
              onClick={() => saveVersion(false)}
              disabled={busy !== null}
            >
              {busy === "save" ? "Saving…" : `Save as v${nextVersion}`}
            </button>
            <button
              type="button"
              className="btn btn--sm btn--primary"
              onClick={() => saveVersion(true)}
              disabled={busy !== null || !hasArtifacts}
            >
              {busy === "save_rescore" ? "Saving…" : `Save v${nextVersion} & re-score`}
            </button>
          </div>
        </div>
      ) : null}

      {notice ? <InfoNote>{notice}</InfoNote> : null}
      {error ? <ErrorNote>{error}</ErrorNote> : null}
    </div>
  );
}
