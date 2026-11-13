"use client";

/**
 * F10 — per-sandbox artifact preview controls on the results page.
 *
 * On mount, GETs the current preview state (a container may still be running
 * from a previous session — previews live on docker labels, api restarts
 * don't kill them). "Preview" POSTs a spawn and opens the URL in a new tab;
 * "stop" DELETEs it. Containers auto-stop after a server-side TTL — the
 * expiry shown here is informational.
 */
import { useEffect, useState } from "react";
import {
  ApiError,
  getPreview,
  startPreview,
  stopPreview,
  type PreviewOut,
} from "@/lib/api";

type Phase = "checking" | "idle" | "starting" | "active" | "stopping";

const LIVE_STATUSES = new Set(["created", "running", "restarting", "paused"]);

function expiryLabel(expiresAt: string | null): string | null {
  if (!expiresAt) return null;
  const ms = Date.parse(expiresAt) - Date.now();
  if (!Number.isFinite(ms)) return null;
  if (ms <= 0) return "expired";
  const mins = Math.max(1, Math.round(ms / 60_000));
  return `auto-stops in ~${mins}m`;
}

export default function PreviewControls({
  runId,
  sandboxId,
  previewable,
  candidates = [],
}: {
  runId: string;
  sandboxId: string;
  previewable: boolean;
  /** Workspace-relative entries when detection is ambiguous — a chooser
   *  renders and the pick is POSTed as {entry}. */
  candidates?: string[];
}) {
  const [phase, setPhase] = useState<Phase>("checking");
  const [preview, setPreview] = useState<PreviewOut | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [pick, setPick] = useState<string>(candidates[0] ?? "");

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      // Defer to a microtask: state updates land in async context
      // (react-hooks/set-state-in-effect forbids sync setState in effects).
      await Promise.resolve();
      try {
        const p = await getPreview(runId, sandboxId);
        if (cancelled) return;
        setPreview(p);
        setPhase(LIVE_STATUSES.has(p.status) ? "active" : "idle");
      } catch {
        // 404 (no preview) and unreachable api both mean "idle".
        if (!cancelled) setPhase("idle");
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [runId, sandboxId]);

  async function start(entry?: string) {
    setPhase("starting");
    setError(null);
    try {
      const p = await startPreview(runId, sandboxId, entry);
      setPreview(p);
      setPhase("active");
      if (p.url) window.open(p.url, "_blank", "noopener,noreferrer");
    } catch (e) {
      setPhase("idle");
      setError(
        e instanceof ApiError ? e.detail : "preview failed to start",
      );
    }
  }

  async function stop() {
    setPhase("stopping");
    try {
      await stopPreview(runId, sandboxId);
    } catch {
      // 404 = already gone — same end state.
    }
    setPreview(null);
    setPhase("idle");
  }

  if (phase === "checking") {
    return <span className="faint" style={{ fontSize: "0.8rem" }}>…</span>;
  }

  if (phase === "active" && preview) {
    const expiry = expiryLabel(preview.expires_at);
    return (
      <div className="chip-row">
        <span
          className="faint"
          style={{ fontSize: "0.75rem" }}
          title={
            preview.type === "desktop"
              ? "Desktop preview — the artifact executes in an isolated container (no egress), streamed via noVNC"
              : "Static preview — workspace files served read-only, nothing executed"
          }
        >
          {preview.type === "desktop" ? "🖥 desktop" : preview.type}
        </span>
        {preview.url ? (
          <a
            className="chip"
            style={{ textDecoration: "none" }}
            href={preview.url}
            target="_blank"
            rel="noopener noreferrer"
            title={`Open preview (${preview.type}: ${preview.entry ?? "site"})`}
          >
            ↗ open
          </a>
        ) : (
          <span className="faint" style={{ fontSize: "0.8rem" }}>
            {preview.status}
          </span>
        )}
        <button
          type="button"
          className="btn btn--sm"
          onClick={stop}
          title="Stop the preview container"
        >
          ■ stop
        </button>
        {expiry ? (
          <span className="faint" style={{ fontSize: "0.75rem" }} title={preview.expires_at ?? undefined}>
            {expiry}
          </span>
        ) : null}
      </div>
    );
  }

  const busy = phase === "starting" || phase === "stopping";

  return (
    <div className="chip-row">
      {previewable ? (
        <button
          type="button"
          className="btn btn--sm"
          onClick={() => void start()}
          disabled={busy}
          title="Serve this sandbox's artifact in a throwaway container (new tab)"
        >
          {phase === "starting" ? "starting…" : "▶ Preview"}
        </button>
      ) : candidates.length > 0 ? (
        <>
          <select
            value={pick}
            onChange={(e) => setPick(e.target.value)}
            disabled={busy}
            title="Multiple entry points detected — pick which to preview"
            style={{ fontSize: "0.8rem", maxWidth: 180 }}
          >
            {candidates.map((c) => (
              <option key={c} value={c}>
                {c}
              </option>
            ))}
          </select>
          <button
            type="button"
            className="btn btn--sm"
            onClick={() => void start(pick)}
            disabled={busy || !pick}
            title="Serve the selected entry in a throwaway container (new tab)"
          >
            {phase === "starting" ? "starting…" : "▶ Preview"}
          </button>
        </>
      ) : (
        <>
          <button
            type="button"
            className="btn btn--sm"
            disabled
            title="Preview isn't supported for this artifact — it needs an HTML entry point or a pygame app. The artifact itself is fine and still downloadable above."
          >
            ▶ Preview
          </button>
          <span
            className="faint"
            style={{ fontSize: "0.75rem" }}
            title="Preview supports static sites and pygame apps for now"
          >
            not supported
          </span>
        </>
      )}
      {preview && !LIVE_STATUSES.has(preview.status) ? (
        <span className="faint" style={{ fontSize: "0.75rem" }}>
          ({preview.status})
        </span>
      ) : null}
      {error ? (
        <span className="field__error" title={error}>
          {error}
        </span>
      ) : null}
    </div>
  );
}
