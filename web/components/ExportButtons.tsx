"use client";

/**
 * PR35 (F28) — export downloads + share-link control, shown in the
 * results + analysis page headers.
 *
 * `⬇ json` / `⬇ csv` are plain anchor downloads (the endpoint sends
 * Content-Disposition: attachment, same as artifactDownloadUrl). The
 * share button mints `/share/{token}` on first click and copies the
 * absolute URL; once a link exists it shows open/copy/revoke chips.
 */
import { useState } from "react";
import { runExportUrl, shareRun, unshareRun } from "@/lib/api";

export default function ExportButtons({
  runId,
  shareUrl = null,
}: {
  runId: string;
  /** Share state from RunOut.share_url (null when never minted). */
  shareUrl?: string | null;
}) {
  const [url, setUrl] = useState<string | null>(shareUrl ?? null);
  // Track the prop by adjusting state during render (mount sites render
  // before run data resolves, so a minted link arrives as a late prop) —
  // the set-state-in-effect form trips react-hooks lint.
  const [prevShareUrl, setPrevShareUrl] = useState(shareUrl ?? null);
  if (prevShareUrl !== (shareUrl ?? null)) {
    setPrevShareUrl(shareUrl ?? null);
    setUrl(shareUrl ?? null);
  }
  const [busy, setBusy] = useState(false);
  const [copied, setCopied] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  async function copy(link: string) {
    try {
      await navigator.clipboard.writeText(
        `${window.location.origin}${link}`,
      );
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      // Clipboard unavailable (non-secure context, denied permission) —
      // the link chip is still visible for manual copying.
    }
  }

  async function mint() {
    setBusy(true);
    setErr(null);
    try {
      const link = await shareRun(runId);
      setUrl(link.url);
      void copy(link.url);
    } catch (e) {
      setErr(e instanceof Error ? e.message : "share failed");
    } finally {
      setBusy(false);
    }
  }

  async function revoke() {
    setBusy(true);
    setErr(null);
    try {
      await unshareRun(runId);
      setUrl(null);
    } catch (e) {
      setErr(e instanceof Error ? e.message : "revoke failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="row" style={{ gap: "0.4rem", flexWrap: "wrap" }}>
      <a
        className="btn btn--sm"
        href={runExportUrl(runId, "json")}
        title="Download the full run bundle — spec, rubric versions, score matrix, analytics, events-lite — as JSON"
      >
        ⬇ json
      </a>
      <a
        className="btn btn--sm"
        href={runExportUrl(runId, "csv")}
        title="Download the flattened score sheet — one row per sandbox × rubric dimension — as CSV"
      >
        ⬇ csv
      </a>
      {url ? (
        <>
          <a
            className="btn btn--sm"
            href={url}
            target="_blank"
            rel="noreferrer"
            title="Open the read-only share view (results + events-lite timeline)"
          >
            🔗 shared
          </a>
          <button
            type="button"
            className="btn btn--sm"
            onClick={() => void copy(url)}
          >
            {copied ? "copied ✓" : "copy link"}
          </button>
          <button
            type="button"
            className="btn btn--sm"
            disabled={busy}
            onClick={() => void revoke()}
            title="Revoke the share link"
          >
            ✕
          </button>
        </>
      ) : (
        <button
          type="button"
          className="btn btn--sm"
          disabled={busy}
          onClick={() => void mint()}
          title="Mint a read-only share link (score matrix + events-lite timeline) and copy it"
        >
          🔗 share
        </button>
      )}
      {err ? (
        <span className="faint" style={{ color: "var(--red)" }}>
          {err}
        </span>
      ) : null}
    </div>
  );
}
