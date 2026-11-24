"use client";

/**
 * PR29 — blind A/B pairwise review card.
 *
 * "new pair" → POST /api/runs/{id}/pair picks two artifact-bearing
 * sandboxes, blinds them behind A/B labels, and returns each side's
 * anonymized file listing (workspace tree, or output.txt for prompt
 * cells — meta.json/transcript/analysis never surface). The reviewer
 * browses both sides, then votes a winner per rubric dimension; the
 * vote resolves to real sandbox ids server-side and the response
 * reveals which side won.
 *
 * Pair sessions are in-memory server-side (2h TTL) — a stale session
 * 404s with "post a new pair", handled here as a soft reset.
 */
import { useCallback, useState } from "react";
import {
  ApiError,
  createPair,
  getPairFile,
  postPairVote,
  type PairFileOut,
  type PairOut,
  type PairwiseVoteOut,
} from "@/lib/api";
import { buildFileTree, type FileNode } from "@/lib/fileBrowser";
import { shortId } from "@/lib/format";
import { Badge, EmptyState, ErrorNote, InfoNote, Loading } from "@/components/ui";

function errMsg(err: unknown, fallback: string): string {
  if (err instanceof ApiError) return err.detail;
  if (err instanceof Error) return err.message;
  return fallback;
}

/** Flat indented file rows for one side's listing — simpler than the
 * collapsible tree: pair listings are small (one workspace). */
function SideFiles({
  nodes,
  selected,
  onOpen,
}: {
  nodes: FileNode[];
  selected: string | null;
  onOpen: (path: string) => void;
}) {
  const rows: FileNode[] = [];
  const walk = (ns: FileNode[]) => {
    for (const n of ns) {
      rows.push(n);
      walk(n.children);
    }
  };
  walk(nodes);
  if (rows.length === 0) {
    return <EmptyState>no files</EmptyState>;
  }
  return (
    <div className="fb-mlist" style={{ maxHeight: "16rem", overflowY: "auto" }}>
      {rows.map((n) => (
        <button
          key={n.path}
          type="button"
          className={`fb-mrow${selected === n.path ? " fb-mrow--sel" : ""}`}
          style={{ paddingLeft: `${0.4 + n.depth * 0.9}rem` }}
          onClick={() => (n.kind === "file" ? onOpen(n.path) : undefined)}
          disabled={n.kind !== "file"}
          title={n.path}
        >
          <span className="fb-mcell">
            {n.kind === "dir" ? `${n.name}/` : n.name}
          </span>
        </button>
      ))}
    </div>
  );
}

/** One blinded side: label header, file list, size-capped text viewer. */
function PairSide({
  side,
  pair,
  runId,
}: {
  side: "A" | "B";
  pair: PairOut;
  runId: string;
}) {
  const listing = pair.sides.find((s) => s.label === side);
  const [selected, setSelected] = useState<string | null>(null);
  const [content, setContent] = useState<PairFileOut | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const open = useCallback(
    async (path: string) => {
      setSelected(path);
      setLoading(true);
      setError(null);
      try {
        setContent(await getPairFile(runId, pair.pair_id, side, path));
      } catch (e) {
        setContent(null);
        setError(errMsg(e, "could not load file"));
      } finally {
        setLoading(false);
      }
    },
    [runId, pair.pair_id, side],
  );

  const tree = buildFileTree(listing?.entries ?? []);
  return (
    <div style={{ flex: 1, minWidth: 0 }}>
      <div className="fb-note">
        <strong>{side}</strong> · {listing?.root === "artifact" ? "output.txt only (prompt cell)" : "workspace"}
        {listing?.truncated ? " · truncated" : ""}
      </div>
      <SideFiles nodes={tree} selected={selected} onOpen={(p) => void open(p)} />
      {loading ? (
        <Loading label="Loading…" />
      ) : error ? (
        <ErrorNote>{error}</ErrorNote>
      ) : content ? (
        <>
          <div className="fb-path mono" title={content.path}>
            {content.path}
            {content.truncated ? " · truncated" : ""}
          </div>
          {content.binary ? (
            <div className="fb-note">binary file — can&apos;t preview.</div>
          ) : (
            <pre
              className="code fb-pre"
              style={{ maxHeight: "14rem", overflowY: "auto" }}
            >
              {content.text}
            </pre>
          )}
        </>
      ) : null}
    </div>
  );
}

export default function PairReview({
  runId,
  onVoted,
}: {
  runId: string;
  /** Results refresh hook — the votes summary card re-derives. */
  onVoted: () => void;
}) {
  const [pair, setPair] = useState<PairOut | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [dimension, setDimension] = useState("");
  const [note, setNote] = useState("");
  const [vote, setVote] = useState<PairwiseVoteOut | null>(null);
  const [expired, setExpired] = useState(false);

  async function newPair() {
    setBusy(true);
    setError(null);
    setExpired(false);
    setVote(null);
    try {
      const p = await createPair(runId);
      setPair(p);
      setDimension(p.dimensions[0] ?? "overall");
    } catch (e) {
      setPair(null);
      setError(errMsg(e, "could not open a pair"));
    } finally {
      setBusy(false);
    }
  }

  async function castVote(winner: "A" | "B" | "tie") {
    if (!pair || !dimension) return;
    setBusy(true);
    setError(null);
    try {
      const v = await postPairVote(runId, pair.pair_id, {
        dimension,
        winner,
        note: note.trim() || null,
      });
      setVote(v);
      onVoted();
    } catch (e) {
      const msg = errMsg(e, "vote failed");
      setError(msg);
      // Session TTL expiry (2h, in-memory server-side) → offer a fresh pair.
      if (e instanceof ApiError && e.status === 404) setExpired(true);
    } finally {
      setBusy(false);
    }
  }

  /** The reveal: map the stored winner back to the A/B label the
   * reviewer picked — left := the sandbox behind "A". */
  function reveal(v: PairwiseVoteOut): string {
    if (v.winner === "tie") return "tie";
    const winId = v.winner === "left" ? v.left_sandbox : v.right_sandbox;
    const label = v.winner === "left" ? "A" : "B";
    return `${label} (${shortId(winId)})`;
  }

  return (
    <>
      {!pair ? (
        <div className="row" style={{ gap: "0.6rem", flexWrap: "wrap" }}>
          <button
            type="button"
            className="btn"
            onClick={() => void newPair()}
            disabled={busy}
            title="Pick two artifact-bearing sandboxes at random and review their files blind (A/B)"
          >
            {busy ? "opening…" : "⚖ new blind pair"}
          </button>
          {error ? <ErrorNote>{error}</ErrorNote> : null}
          <span className="faint" style={{ fontSize: "0.78rem" }}>
            Side labels stay anonymized until you vote — votes feed the
            win/loss roll-up + judge-vs-human agreement above.
          </span>
        </div>
      ) : (
        <div className="stack">
          <div className="row" style={{ gap: "0.75rem", alignItems: "stretch" }}>
            <PairSide side="A" pair={pair} runId={runId} />
            <PairSide side="B" pair={pair} runId={runId} />
          </div>

          {expired ? (
            <InfoNote>
              This pair session expired — open a new one to keep reviewing.
            </InfoNote>
          ) : null}
          {error ? <ErrorNote>{error}</ErrorNote> : null}
          {vote ? (
            <div>
              <Badge tone="green" title={`vote #${vote.id}`}>
                recorded: {vote.dimension} → {reveal(vote)}
              </Badge>
              <span className="faint" style={{ marginLeft: "0.5rem", fontSize: "0.78rem" }}>
                A was {shortId(vote.left_sandbox)} · B was {shortId(vote.right_sandbox)}
              </span>
            </div>
          ) : null}

          <div className="row" style={{ gap: "0.5rem", flexWrap: "wrap", alignItems: "center" }}>
            <select
              className="select"
              aria-label="vote dimension"
              value={dimension}
              onChange={(e) => setDimension(e.target.value)}
              disabled={busy}
            >
              {pair.dimensions.map((d) => (
                <option key={d} value={d}>
                  {d}
                </option>
              ))}
            </select>
            <input
              className="input"
              style={{ flex: 1, minWidth: "12rem" }}
              placeholder="vote note (optional)"
              aria-label="vote note"
              value={note}
              onChange={(e) => setNote(e.target.value)}
              disabled={busy}
              maxLength={2000}
            />
            <button type="button" className="btn" disabled={busy} onClick={() => void castVote("A")}>
              A wins
            </button>
            <button type="button" className="btn" disabled={busy} onClick={() => void castVote("tie")}>
              tie
            </button>
            <button type="button" className="btn" disabled={busy} onClick={() => void castVote("B")}>
              B wins
            </button>
            <button
              type="button"
              className="btn btn--sm btn--ghost"
              disabled={busy}
              onClick={() => void newPair()}
              title="Discard this pair and open a fresh one"
            >
              ↻ new pair
            </button>
          </div>
        </div>
      )}
    </>
  );
}
