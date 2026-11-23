"use client";

/**
 * PR22 — "save as preset" / "edit preset" dialog.
 *
 * Create mode (no `existing`): saves a task prompt as a user preset via
 * `POST /api/presets/{slug}` — the slug lives in the path. A taken slug
 * 409s with a structured name_conflict; the suggested_name prefills a
 * rename retry (same confirm-and-rename flow as skillpack import).
 *
 * Edit mode (`existing` preset): PATCHes a user preset — the slug is
 * immutable (the ref runs point at), every successful save bumps
 * revision server-side.
 */
import { useState } from "react";
import {
  ApiError,
  createPreset,
  presetConflictOf,
  presetSlugify,
  updatePreset,
  type TaskPreset,
} from "@/lib/api";
import { ErrorNote, InfoNote } from "@/components/ui";

interface Props {
  /** The task prompt being saved (create) — fixed for the dialog's life. */
  taskPrompt?: string;
  /** Edit mode — the existing USER preset being patched. */
  existing?: TaskPreset;
  initialTitle?: string;
  onClose: () => void;
  onSaved: (preset: TaskPreset) => void;
}

export function SavePresetDialog({
  taskPrompt,
  existing,
  initialTitle,
  onClose,
  onSaved,
}: Props) {
  const editing = existing !== undefined;
  const [slug, setSlug] = useState(
    presetSlugify(existing?.title ?? initialTitle ?? taskPrompt ?? "").slice(0, 48).replace(/-+$/, ""),
  );
  const [title, setTitle] = useState(existing?.title ?? initialTitle ?? "");
  const [editPrompt, setEditPrompt] = useState(existing?.task_prompt ?? "");
  const [description, setDescription] = useState(existing?.description ?? "");
  const [category, setCategory] = useState(existing?.category ?? "");
  const [tags, setTags] = useState((existing?.tags ?? []).join(", "));
  const [suggAdapter, setSuggAdapter] = useState(existing?.suggested_adapter ?? "");
  const [suggModels, setSuggModels] = useState(
    (existing?.suggested_models ?? []).join(", "),
  );
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [collision, setCollision] = useState<{ attempted: string; suggested: string; source: string | null } | null>(null);

  const slugOk = /^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$/.test(slug.trim());
  const canSave =
    !busy &&
    title.trim().length > 0 &&
    (editing
      ? editPrompt.trim().length > 0
      : slugOk && (taskPrompt ?? "").trim().length > 0);

  async function onSave() {
    if (!canSave) return;
    setBusy(true);
    setError(null);
    const parsedTags = tags
      .split(",")
      .map((t) => t.trim())
      .filter(Boolean)
      .slice(0, 16);
    const parsedSuggModels = suggModels
      .split(",")
      .map((m) => m.trim())
      .filter(Boolean)
      .slice(0, 8);
    try {
      const saved = editing
        ? await updatePreset(existing.slug, {
            title: title.trim(),
            task_prompt: editPrompt.trim(),
            description: description.trim(),
            category: category.trim(),
            tags: parsedTags,
            suggested_adapter: suggAdapter.trim() || null,
            suggested_models: parsedSuggModels.length ? parsedSuggModels : null,
          })
        : await createPreset(slug.trim().toLowerCase(), {
            title: title.trim(),
            task_prompt: taskPrompt ?? "",
            description: description.trim(),
            category: category.trim(),
            tags: parsedTags,
          });
      onSaved(saved);
    } catch (err) {
      const conflict = presetConflictOf(err);
      if (conflict) {
        setCollision({
          attempted: slug.trim(),
          suggested: conflict.suggested_name ?? `${slug.trim()}-2`,
          source: conflict.source ?? null,
        });
        setSlug(conflict.suggested_name ?? `${slug.trim()}-2`);
      } else {
        setError(err instanceof ApiError ? err.detail : "Save failed.");
      }
      setBusy(false);
    }
  }

  return (
    <div className="modal-backdrop" role="presentation" onClick={onClose}>
      <div
        className="modal"
        role="dialog"
        aria-modal="true"
        aria-label={editing ? "Edit preset" : "Save task as preset"}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="modal__head">
          <span className="modal__title">
            {editing ? `Edit preset · ${existing.slug}` : "Save task as preset"}
          </span>
          <button type="button" className="btn btn--sm btn--ghost" onClick={onClose}>
            ✕
          </button>
        </div>
        <div className="modal__body">
          {collision ? (
            <ErrorNote title="Slug taken">
              <code>{collision.attempted}</code> already exists
              {collision.source ? ` (${collision.source})` : ""} — save as{" "}
              <code>{collision.suggested}</code> instead, or edit the slug.
            </ErrorNote>
          ) : null}
          {error ? <ErrorNote title="Save failed">{error}</ErrorNote> : null}

          {!editing ? (
            <div className="field">
              <label className="field__label" htmlFor="preset-slug">
                Slug <span className="faint">(url id — lowercase, digits, dashes)</span>
              </label>
              <input
                id="preset-slug"
                className="input input--mono"
                value={slug}
                onChange={(e) => {
                  setSlug(e.target.value);
                  setCollision(null);
                }}
                disabled={busy}
                maxLength={64}
                placeholder="my-task-preset"
              />
              {!slugOk && slug.trim() ? (
                <span className="field__error">
                  Must match ^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$ — lowercase letters, digits,
                  dashes; no leading/trailing dash.
                </span>
              ) : null}
            </div>
          ) : null}

          <div className="field">
            <label className="field__label" htmlFor="preset-title">
              Title <span className="faint">(required)</span>
            </label>
            <input
              id="preset-title"
              className="input"
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              disabled={busy}
              maxLength={255}
              placeholder="e.g. Wordle clone — single HTML file"
            />
          </div>

          <div className="input-row">
            <div className="field" style={{ flex: 1 }}>
              <label className="field__label" htmlFor="preset-category">
                Category
              </label>
              <input
                id="preset-category"
                className="input"
                value={category}
                onChange={(e) => setCategory(e.target.value)}
                disabled={busy}
                maxLength={64}
                placeholder="e.g. game-app, support, sql"
              />
            </div>
            <div className="field" style={{ flex: 1 }}>
              <label className="field__label" htmlFor="preset-tags">
                Tags <span className="faint">(comma-separated)</span>
              </label>
              <input
                id="preset-tags"
                className="input"
                value={tags}
                onChange={(e) => setTags(e.target.value)}
                disabled={busy}
                placeholder="game, html, live-validation"
              />
            </div>
          </div>

          <div className="field">
            <label className="field__label" htmlFor="preset-desc">
              Description
            </label>
            <textarea
              id="preset-desc"
              className="textarea"
              rows={2}
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              disabled={busy}
              maxLength={2000}
              placeholder="One line — what this task measures."
            />
          </div>

          {editing ? (
            <div className="field">
              <label className="field__label" htmlFor="preset-prompt">
                Task prompt <span className="faint">(verbatim — stored as revision {(existing.revision ?? 1) + 1})</span>
              </label>
              <textarea
                id="preset-prompt"
                className="textarea"
                rows={8}
                value={editPrompt}
                onChange={(e) => setEditPrompt(e.target.value)}
                disabled={busy}
                maxLength={100000}
              />
            </div>
          ) : null}

          {editing ? (
            <div className="input-row">
              <div className="field" style={{ flex: 1 }}>
                <label className="field__label" htmlFor="preset-sugg-adapter">
                  Suggested adapter <span className="faint">(empty clears)</span>
                </label>
                <input
                  id="preset-sugg-adapter"
                  className="input input--mono"
                  value={suggAdapter}
                  onChange={(e) => setSuggAdapter(e.target.value)}
                  disabled={busy}
                  maxLength={64}
                  placeholder="e.g. miniloop"
                />
              </div>
              <div className="field" style={{ flex: 1 }}>
                <label className="field__label" htmlFor="preset-sugg-models">
                  Suggested models{" "}
                  <span className="faint">(comma-separated — empty clears)</span>
                </label>
                <input
                  id="preset-sugg-models"
                  className="input input--mono"
                  value={suggModels}
                  onChange={(e) => setSuggModels(e.target.value)}
                  disabled={busy}
                  placeholder="qwen3:8b, qwen2.5-coder:7b"
                />
              </div>
            </div>
          ) : null}

          {editing ? (
            <InfoNote>
              Saving bumps the preset to revision {(existing.revision ?? 1) + 1} — runs
              keep pointing at the revision they were submitted on.
            </InfoNote>
          ) : (
            <InfoNote>
              The preset becomes selectable on the submit form and reusable across
              runs — the task prompt text is stored verbatim.
            </InfoNote>
          )}

          <div className="row">
            <button
              type="button"
              className="btn btn--primary"
              onClick={() => void onSave()}
              disabled={!canSave}
            >
              {busy ? "Saving…" : editing ? "Save changes" : "Save preset"}
            </button>
            <button type="button" className="btn" onClick={onClose} disabled={busy}>
              Cancel
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
