"use client";

/**
 * PR25 — inline viewer for a prompt cell's `output.txt` artifact.
 *
 * Prompt cells have no workspace and no preview — the downloadable
 * `output`-kind artifact IS the submission. The text is fetched lazily on
 * first expand (the results GET is polled; fetching bodies up-front would
 * re-download every poll cycle) and rendered bounded.
 */
import { useState } from "react";
import { artifactDownloadUrl } from "@/lib/api";

const MAX_CHARS = 50_000;

export default function OutputView({
  artifactId,
  filename = "output.txt",
}: {
  artifactId: string;
  filename?: string;
}) {
  const [text, setText] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function load() {
    if (text !== null || loading) return;
    setLoading(true);
    setError(null);
    try {
      const resp = await fetch(artifactDownloadUrl(artifactId));
      if (!resp.ok) {
        setError(`fetch failed (HTTP ${resp.status})`);
        return;
      }
      const body = await resp.text();
      setText(
        body.length > MAX_CHARS
          ? body.slice(0, MAX_CHARS) + `\n… [truncated — download for the full ${filename}]`
          : body,
      );
    } catch (e) {
      setError(e instanceof Error ? e.message : "fetch failed");
    } finally {
      setLoading(false);
    }
  }

  return (
    <details
      onToggle={(e) => {
        if ((e.target as HTMLDetailsElement).open) void load();
      }}
    >
      <summary
        className="chip"
        style={{ cursor: "pointer", display: "inline-block" }}
        title={`Read ${filename} inline`}
      >
        view {filename}
      </summary>
      {loading ? (
        <span className="faint" style={{ fontSize: "0.8rem" }}> loading…</span>
      ) : null}
      {error ? (
        <span className="faint" style={{ fontSize: "0.8rem", color: "var(--bad)" }}>
          {" "}
          {error}
        </span>
      ) : null}
      {text !== null ? (
        <pre
          className="code"
          style={{
            maxHeight: "20rem",
            overflow: "auto",
            whiteSpace: "pre-wrap",
            wordBreak: "break-word",
            fontSize: "0.78rem",
            marginTop: "0.4rem",
          }}
        >
          {text || <span className="faint">(empty output)</span>}
        </pre>
      ) : null}
    </details>
  );
}
