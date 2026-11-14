"use client";

/**
 * F12 — per-sandbox artifact preview controls on the results page.
 *
 * On mount, GETs the current preview state (a container may still be running
 * from a previous session — previews live on docker labels, api restarts
 * don't kill them). "Preview" POSTs a spawn and opens the URL in a new tab;
 * "stop" DELETEs it. Containers auto-stop after a server-side TTL — the
 * expiry shown here is informational.
 *
 * The dropdown lists detected candidates plus type-override variants: a
 * `.py` entry can run as a desktop/server app or be served as a file
 * ("view source"). An explicit pick that differs from the live preview's
 * plan replaces the running container server-side.
 *
 * PR4: the row is headed by the artifact's captured thumbnail
 * (results-embedded `thumbnail`; "📷 snapshot" POSTs a headless-chromium
 * capture through the preview stack). Clicking the shot opens the live
 * preview — spawning one first if needed.
 */
import { useEffect, useState } from "react";
import {
  ApiError,
  captureThumbnail,
  getPreview,
  startPreview,
  stopPreview,
  thumbnailUrl,
  type PreviewCandidate,
  type PreviewOut,
  type ThumbRef,
} from "@/lib/api";

type Phase = "checking" | "idle" | "starting" | "active" | "stopping";

const LIVE_STATUSES = new Set(["created", "running", "restarting", "paused"]);

/** One selectable option: a detected candidate, or a type-override variant
 *  of one (a `.py` can be served as a file instead of executed). */
interface Option {
  key: string;
  entry: string;
  type: string;
  label: string;
}

/** Short dropdown label for a preview type. */
function typeLabel(type: string): string {
  if (type === "static") return "site";
  if (type === "python-server") return "server";
  return type;
}

function buildOptions(candidates: PreviewCandidate[]): Option[] {
  return candidates.flatMap((c) => {
    const base: Option = {
      key: `${c.entry}|${c.type}`,
      entry: c.entry,
      type: c.type,
      label: `${c.entry} (${typeLabel(c.type)})`,
    };
    // Every executed-code candidate (.py) can also be served as a file.
    return c.type !== "static"
      ? [
          base,
          {
            key: `${c.entry}|static`,
            entry: c.entry,
            type: "static",
            label: `${c.entry} (view source)`,
          },
        ]
      : [base];
  });
}

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
  thumbnail = null,
}: {
  runId: string;
  sandboxId: string;
  previewable: boolean;
  /** Every detected runnable entry, default-first — >1 renders a dropdown
   *  and the pick is POSTed as {entry, type}. */
  candidates?: PreviewCandidate[];
  /** PR4: captured screenshot ref from the results payload (null when
   *  never captured). Local state overrides after a fresh capture. */
  thumbnail?: ThumbRef | null;
}) {
  const options = buildOptions(candidates);
  const [phase, setPhase] = useState<Phase>("checking");
  const [preview, setPreview] = useState<PreviewOut | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [pick, setPick] = useState<string>(options[0]?.key ?? "");
  const [thumbOverride, setThumbOverride] = useState<ThumbRef | null>();
  const [capturing, setCapturing] = useState(false);
  const thumb = thumbOverride !== undefined ? thumbOverride : thumbnail;

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

  async function start(opt?: Option) {
    setPhase("starting");
    setError(null);
    try {
      const p = await startPreview(
        runId,
        sandboxId,
        opt ? { entry: opt.entry, type: opt.type } : undefined,
      );
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

  /** PR4 — synchronous bounded capture; the api returns 502 on failure
   *  (a missing thumbnail is a fine outcome, never a broken page). */
  async function capture() {
    setCapturing(true);
    setError(null);
    try {
      const t = await captureThumbnail(runId, sandboxId);
      setThumbOverride(t);
    } catch (e) {
      setError(
        e instanceof ApiError ? e.detail : "thumbnail capture failed",
      );
    } finally {
      setCapturing(false);
    }
  }

  /** Thumbnail click → full live preview: open the running one, else
   *  spawn the default plan (start() opens the URL on success). */
  function openFull() {
    if (phase === "active" && preview?.url) {
      window.open(preview.url, "_blank", "noopener,noreferrer");
    } else {
      void start();
    }
  }

  const busy = phase === "starting" || phase === "stopping";
  const canCapture = previewable || options.length > 0;

  /** PR4 thumbnail block — heads the cell in both idle and active
   *  states; the shot itself is the "open full preview" affordance. */
  const thumbBlock = (
    <div className="thumb-row">
      {thumb ? (
        <button
          type="button"
          className="thumb-btn"
          onClick={openFull}
          disabled={busy || capturing}
          title={
            phase === "active" && preview?.url
              ? "Open the live preview"
              : `Captured ${thumb.captured_at ?? "earlier"}${thumb.entry ? ` — ${thumb.type}: ${thumb.entry}` : ""}. Click to spawn a full live preview.`
          }
        >
          {/* eslint-disable-next-line @next/next/no-img-element -- small
              artifact PNG served by the api; next/image isn't configured
              for it */}
          <img
            className="thumb-img"
            src={thumbnailUrl(runId, sandboxId, thumb.captured_at)}
            alt={`artifact screenshot${thumb.type ? ` — ${thumb.type}` : ""}`}
          />
        </button>
      ) : (
        <span className="thumb-empty faint">no snapshot</span>
      )}
      <button
        type="button"
        className="btn btn--sm"
        onClick={() => void capture()}
        disabled={busy || capturing || !canCapture}
        title={
          canCapture
            ? "Screenshot the artifact through its preview stack (stored under the artifact dir)"
            : "Thumbnail capture isn't supported for this artifact — it needs an HTML entry, GUI app, or web app"
        }
      >
        {capturing ? "capturing…" : thumb ? "↻ recapture" : "📷 snapshot"}
      </button>
    </div>
  );

  if (phase === "checking") {
    return <span className="faint" style={{ fontSize: "0.8rem" }}>…</span>;
  }

  if (phase === "active" && preview) {
    const expiry = expiryLabel(preview.expires_at);
    return (
      <div className="preview-cell">
        {thumbBlock}
        <div className="chip-row">
          <span
            className="faint"
            style={{ fontSize: "0.75rem" }}
            title={
              preview.type === "desktop"
                ? "Desktop preview — the artifact executes in an isolated container (no egress), streamed via noVNC"
                : preview.type === "python-server"
                  ? "Server preview — the artifact's web app executes in an isolated container (no egress) and is served on localhost"
                  : "Static preview — workspace files served read-only, nothing executed"
            }
          >
            {preview.type === "desktop"
              ? "🖥 desktop"
              : preview.type === "python-server"
                ? "🌐 server"
                : preview.type}
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
        {error ? (
          <span className="field__error" title={error}>
            {error}
          </span>
        ) : null}
      </div>
    );
  }

  return (
    <div className="preview-cell">
      {thumbBlock}
      <div className="chip-row">
        {options.length > 1 ? (
          <>
            <select
              value={pick}
              onChange={(e) => setPick(e.target.value)}
              disabled={busy}
              title="Entry point + how to preview it — 'view source' serves a .py file instead of executing it"
              style={{ fontSize: "0.8rem", maxWidth: 220 }}
            >
              {options.map((o) => (
                <option key={o.key} value={o.key}>
                  {o.label}
                </option>
              ))}
            </select>
            <button
              type="button"
              className="btn btn--sm"
              onClick={() => void start(options.find((o) => o.key === pick))}
              disabled={busy || !pick}
              title="Serve the selected entry in a throwaway container (new tab)"
            >
              {phase === "starting" ? "starting…" : "▶ Preview"}
            </button>
          </>
        ) : previewable ? (
          <button
            type="button"
            className="btn btn--sm"
            onClick={() => void start()}
            disabled={busy}
            title="Serve this sandbox's artifact in a throwaway container (new tab)"
          >
            {phase === "starting" ? "starting…" : "▶ Preview"}
          </button>
        ) : (
          <>
            <button
              type="button"
              className="btn btn--sm"
              disabled
              title="Preview isn't supported for this artifact — it needs an HTML entry point, a GUI app, or a web app. The artifact itself is fine and still downloadable above."
            >
              ▶ Preview
            </button>
            <span
              className="faint"
              style={{ fontSize: "0.75rem" }}
              title="Preview supports static sites, GUI apps (pygame/pyglet/arcade/tkinter), and web apps (flask/fastapi/django/…)"
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
    </div>
  );
}
