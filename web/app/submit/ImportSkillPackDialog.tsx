"use client";

/**
 * F10 — "Import from GitHub" dialog for the submit form.
 *
 * Flow: paste a GitHub link (repo root, /tree/<branch>/<path>, or a
 * SKILL.md /blob/ link) → POST /api/skillpacks/discover → pick discovered
 * skills → import as (a) one NEW named pack (name collision → 409 + the
 * api's suggested_name prefills a rename retry), (b) skills appended INTO
 * an existing imported pack, or (c) one pack per selected skill
 * (auto-renamed on collision). On success the parent adds the resulting
 * pack(s) to the harness config's selection.
 */
import { useMemo, useState } from "react";
import {
  ApiError,
  discoverSkillPack,
  importSkillPack,
  nameConflictOf,
  type DiscoveredSkill,
  type SkillPack,
  type SkillpackDiscoverResult,
  type SkillpackImportResult,
} from "@/lib/api";
import { ErrorNote, InfoNote } from "@/components/ui";

type Step = "url" | "select";
type ImportMode = "new" | "existing" | "split";

/** Last name_conflict hit — drives the rename-prefill + notice. */
interface Collision {
  attempted: string;
  suggested: string;
  source: "vendored" | "imported";
}

const PLACEHOLDER =
  "github.com/owner/repo · …/tree/<branch>/<path> · …/blob/<branch>/…/SKILL.md";

export function ImportSkillPackDialog({
  importedPacks,
  onClose,
  onImported,
}: {
  /** Existing imported packs — targets for "add to an existing pack". */
  importedPacks: SkillPack[];
  onClose: () => void;
  onImported: (result: SkillpackImportResult) => void;
}) {
  const [step, setStep] = useState<Step>("url");
  const [url, setUrl] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [discovery, setDiscovery] = useState<SkillpackDiscoverResult | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [mode, setMode] = useState<ImportMode>("new");
  const [packName, setPackName] = useState("");
  const [existingPack, setExistingPack] = useState(importedPacks[0]?.name ?? "");
  const [collision, setCollision] = useState<Collision | null>(null);

  const suggestedName = useMemo(() => {
    if (!discovery) return "";
    const repo = discovery.repo.split("/")[1] ?? "imported";
    return repo.toLowerCase().replace(/[^a-z0-9_-]+/g, "-").replace(/^-+|-+$/g, "");
  }, [discovery]);

  const targetPack = mode === "new" ? packName.trim() || suggestedName : existingPack;
  const canImport =
    !busy &&
    discovery !== null &&
    selected.size > 0 &&
    (mode === "split" || targetPack.length > 0);

  async function onDiscover() {
    setError(null);
    setBusy(true);
    try {
      const d = await discoverSkillPack(url.trim());
      setDiscovery(d);
      setSelected(new Set(d.skills.map((s: DiscoveredSkill) => s.dir)));
      setStep("select");
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : "Discovery failed.");
    } finally {
      setBusy(false);
    }
  }

  function toggle(dir: string) {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(dir)) next.delete(dir);
      else next.add(dir);
      return next;
    });
  }

  async function onImport() {
    if (!discovery || !canImport) return;
    setError(null);
    setBusy(true);
    const apiMode = mode === "new" ? "pack" : mode === "existing" ? "merge" : "split";
    try {
      const result = await importSkillPack({
        url: url.trim(),
        ref: discovery.commit_sha ?? discovery.ref,
        subpath: discovery.subpath || null,
        skills: [...selected],
        mode: apiMode,
        ...(mode === "split" ? {} : { pack_name: targetPack }),
      });
      onImported(result);
    } catch (err) {
      const conflict = nameConflictOf(err);
      if (conflict) {
        // Confirm-and-rename: prefill the suggested free name; the user can
        // edit it — a still-colliding retry just re-arms this notice.
        setCollision({
          attempted: conflict.name,
          suggested: conflict.suggested_name ?? `${conflict.name}-2`,
          source: conflict.source,
        });
        setPackName(conflict.suggested_name ?? `${conflict.name}-2`);
      } else {
        setError(err instanceof ApiError ? err.detail : "Import failed.");
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
        aria-label="Import skill pack from GitHub"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="modal__head">
          <span className="modal__title">Import skills from GitHub</span>
          <button type="button" className="btn btn--sm btn--ghost" onClick={onClose}>
            ✕
          </button>
        </div>
        <div className="modal__body">
          <div className="field">
            <label className="field__label" htmlFor="gh-url">
              GitHub link
            </label>
            <div className="row" style={{ alignItems: "flex-start" }}>
              <input
                id="gh-url"
                className="input input--mono"
                style={{ flex: 1 }}
                placeholder={PLACEHOLDER}
                value={url}
                onChange={(e) => setUrl(e.target.value)}
                disabled={busy}
              />
              <button
                type="button"
                className="btn btn--primary"
                onClick={onDiscover}
                disabled={busy || !url.trim()}
              >
                {busy && step === "url" ? "Scanning…" : "Discover"}
              </button>
            </div>
            <span className="field__hint">
              Repo root, a <code>/tree/&lt;branch&gt;/&lt;path&gt;</code> subdirectory, or a
              direct <code>SKILL.md</code> link. Public repos only (set{" "}
              <code>GITHUB_TOKEN</code> on the api for private ones).
            </span>
          </div>

          {error ? <ErrorNote title="Import">{error}</ErrorNote> : null}

          {step === "select" && discovery ? (
            <>
              <div className="field">
                <label className="field__label">
                  Skills found in <code>{discovery.repo}</code>
                  {discovery.subpath ? (
                    <>
                      {" "}
                      under <code>{discovery.subpath}</code>
                    </>
                  ) : null}{" "}
                  <span className="faint">(ref {discovery.ref})</span>
                </label>
                <div className="skill-pick">
                  {discovery.skills.map((s) => (
                    <label key={s.dir} className="skill-pick__item">
                      <input
                        type="checkbox"
                        checked={selected.has(s.dir)}
                        onChange={() => toggle(s.dir)}
                        disabled={busy}
                      />
                      <span className="skill-pick__meta">
                        <span>
                          <code>{s.name}</code>{" "}
                          <span className="faint">
                            {s.dir} · {s.file_count} file{s.file_count === 1 ? "" : "s"}
                          </span>
                        </span>
                        {s.description ? (
                          <span className="skill-pick__desc">{s.description}</span>
                        ) : null}
                      </span>
                    </label>
                  ))}
                </div>
                <span className="field__hint">
                  {selected.size}/{discovery.skills.length} selected
                </span>
              </div>

              <div className="field">
                <label className="field__label">Import into</label>
                {collision ? (
                  <ErrorNote title="Name taken">
                    A pack named <code>{collision.attempted}</code> already exists
                    {collision.source === "vendored" ? " (vendored)" : ""} — import
                    as <code>{collision.suggested}</code> instead, or edit the name.
                  </ErrorNote>
                ) : null}
                <div className="check-list" style={{ flexDirection: "column", gap: "0.5rem" }}>
                  <label className="check-item">
                    <input
                      type="radio"
                      name="import-mode"
                      checked={mode === "new"}
                      onChange={() => setMode("new")}
                      disabled={busy}
                    />
                    New pack named{" "}
                    <input
                      className="input input--mono"
                      style={{ width: "16rem", display: "inline-block" }}
                      placeholder={suggestedName || "my-skills"}
                      value={packName}
                      onChange={(e) => {
                        setPackName(e.target.value);
                        setCollision(null);
                      }}
                      onClick={() => setMode("new")}
                      disabled={busy}
                    />
                  </label>
                  {importedPacks.length > 0 ? (
                    <label className="check-item">
                      <input
                        type="radio"
                        name="import-mode"
                        checked={mode === "existing"}
                        onChange={() => setMode("existing")}
                        disabled={busy}
                      />
                      Existing pack{" "}
                      <select
                        className="select"
                        style={{ width: "auto", display: "inline-block" }}
                        value={existingPack}
                        onChange={(e) => {
                          setExistingPack(e.target.value);
                          setMode("existing");
                        }}
                        onClick={() => setMode("existing")}
                        disabled={busy}
                      >
                        {importedPacks.map((p) => (
                          <option key={p.name} value={p.name}>
                            {p.name} ({p.skill_count})
                          </option>
                        ))}
                      </select>
                    </label>
                  ) : (
                    <span className="field__hint">
                      No imported packs yet — a new one will be created.
                    </span>
                  )}
                  <label className="check-item">
                    <input
                      type="radio"
                      name="import-mode"
                      checked={mode === "split"}
                      onChange={() => setMode("split")}
                      disabled={busy}
                    />
                    Each selected skill as its own pack
                  </label>
                </div>
                <span className="field__hint">
                  {mode === "split" ? (
                    <>
                      Packs are named after each skill (e.g.{" "}
                      <code>data/skillpacks/{discovery.skills[0]?.name ?? "skill"}</code>);
                      names already taken get a <code>-2</code>/<code>-3</code> suffix.
                    </>
                  ) : (
                    <>
                      Saved under <code>data/skillpacks/{targetPack || "…"}</code> —
                      selectable in every harness config, staged into sandboxes like
                      vendored packs.
                    </>
                  )}
                </span>
              </div>

              <InfoNote>
                Only selected skill directories (SKILL.md + sibling files) are copied —
                the rest of the repo is ignored.
              </InfoNote>

              <div className="row">
                <button
                  type="button"
                  className="btn btn--primary"
                  onClick={onImport}
                  disabled={!canImport}
                >
                  {busy ? "Importing…" : `Import ${selected.size} skill${selected.size === 1 ? "" : "s"}`}
                </button>
                <button type="button" className="btn" onClick={onClose} disabled={busy}>
                  Cancel
                </button>
              </div>
            </>
          ) : null}
        </div>
      </div>
    </div>
  );
}
