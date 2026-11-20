"use client";

/**
 * PR34 — artifact file browser + side-by-side diff modal.
 *
 * Opened from the results artifacts row ("📂 browse"). Two panes:
 *
 * - **browse** — the artifact's jailed listing as a collapsible file tree
 *   on the left, a size-capped text viewer on the right (binary files offer
 *   the `?download=1` attachment instead).
 * - **diff vs** — picking a peer sandbox switches the left pane to the
 *   PR10 split idiom: ONE merged ordering of both sandboxes' paths, each
 *   row carrying its left (this sandbox) and right (peer) cell — a file
 *   only on one side renders as a populated cell facing an empty lane.
 *   Selecting a file renders the server's unified diff in a colored
 *   `<pre>` (no client diff library — the format arrives ready-made).
 *
 * The browse root is server-side: `workspace/` when the snapshot exists,
 * else the artifact dir itself (prompt cells — `output.txt` surfaces
 * there). All endpoints 404 on traversal; this component never builds a
 * path the jail wouldn't allow anyway (paths come from the listing).
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ApiError,
  getSandboxFile,
  getSandboxFileDiff,
  listSandboxFiles,
  sandboxFileDownloadUrl,
  type FileContentOut,
  type FileDiffOut,
  type FileListOut,
} from "@/lib/api";
import { formatBytes, shortId } from "@/lib/format";
import {
  buildFileTree,
  diffSummary,
  mergeFileLists,
  parseDiffLines,
  type FileNode,
} from "@/lib/fileBrowser";
import { Badge, EmptyState, ErrorNote, Loading } from "@/components/ui";

/** A sibling sandbox the diff picker offers. */
export interface FileBrowserPeer {
  id: string;
  label: string;
}

function errMsg(err: unknown, fallback: string): string {
  if (err instanceof ApiError) return err.detail;
  if (err instanceof Error) return err.message;
  return fallback;
}

/** Pick the file to auto-open: `index.html` when present (the most common
 * static artifact), else the first file in listing order. */
function preferredEntry(list: FileListOut): string | null {
  const files = list.entries.filter((e) => e.kind === "file");
  if (files.length === 0) return null;
  const index = files.find((e) => e.path.toLowerCase() === "index.html");
  return (index ?? files[0]).path;
}

function TreeView({
  nodes,
  selected,
  collapsed,
  onToggleDir,
  onOpen,
}: {
  nodes: FileNode[];
  selected: string | null;
  collapsed: Set<string>;
  onToggleDir: (path: string) => void;
  onOpen: (path: string) => void;
}) {
  return (
    <ul className="fb-tree">
      {nodes.map((n) => (
        <li key={n.path}>
          {n.kind === "dir" ? (
            <>
              <button
                type="button"
                className="fb-node fb-node--dir"
                onClick={() => onToggleDir(n.path)}
                title={collapsed.has(n.path) ? `expand ${n.path}` : `collapse ${n.path}`}
              >
                {collapsed.has(n.path) ? "▸" : "▾"} {n.name}/
              </button>
              {!collapsed.has(n.path) ? (
                <TreeView
                  nodes={n.children}
                  selected={selected}
                  collapsed={collapsed}
                  onToggleDir={onToggleDir}
                  onOpen={onOpen}
                />
              ) : null}
            </>
          ) : (
            <button
              type="button"
              className={`fb-node${selected === n.path ? " fb-node--sel" : ""}`}
              onClick={() => onOpen(n.path)}
              title={n.path}
            >
              {n.name}
              {n.size != null ? (
                <span className="fb-node__size">{formatBytes(n.size)}</span>
              ) : null}
            </button>
          )}
        </li>
      ))}
    </ul>
  );
}

export default function FileBrowser({
  sandboxId,
  label,
  peers,
  onClose,
}: {
  sandboxId: string;
  /** Row label (config name / adapter) for the modal title. */
  label: string;
  /** Sibling sandboxes for the "diff vs" picker. */
  peers: FileBrowserPeer[];
  onClose: () => void;
}) {
  const [list, setList] = useState<FileListOut | null>(null);
  const [listError, setListError] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [content, setContent] = useState<FileContentOut | null>(null);
  const [diff, setDiff] = useState<FileDiffOut | null>(null);
  const [viewError, setViewError] = useState<string | null>(null);
  const [viewLoading, setViewLoading] = useState(false);
  // "" = browse mode; a peer sandbox id = diff mode.
  const [diffWith, setDiffWith] = useState("");
  const [otherList, setOtherList] = useState<FileListOut | null>(null);
  const [otherError, setOtherError] = useState<string | null>(null);
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());
  // Stale-response guard: rapid file clicks race; only the latest request
  // may land its result.
  const reqSeq = useRef(0);

  const openFile = useCallback(
    async (path: string, otherId: string) => {
      const seq = ++reqSeq.current;
      setSelected(path);
      setViewLoading(true);
      setViewError(null);
      setContent(null);
      setDiff(null);
      try {
        if (otherId) {
          const d = await getSandboxFileDiff(sandboxId, path, otherId);
          if (reqSeq.current === seq) setDiff(d);
        } else {
          const c = await getSandboxFile(sandboxId, path);
          if (reqSeq.current === seq) setContent(c);
        }
      } catch (e) {
        if (reqSeq.current === seq) {
          setViewError(errMsg(e, "could not load file"));
        }
      } finally {
        if (reqSeq.current === seq) setViewLoading(false);
      }
    },
    [sandboxId],
  );

  // Initial listing load (deferred setState — react-hooks/set-state-in-effect).
  useEffect(() => {
    let cancelled = false;
    void (async () => {
      await Promise.resolve();
      if (cancelled) return;
      try {
        const l = await listSandboxFiles(sandboxId);
        if (cancelled) return;
        setList(l);
        const preferred = preferredEntry(l);
        if (preferred) void openFile(preferred, "");
      } catch (e) {
        if (!cancelled) setListError(errMsg(e, "could not list files"));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [sandboxId, openFile]);

  // Peer listing for diff mode — fetched once per pick.
  useEffect(() => {
    if (!diffWith) return;
    let cancelled = false;
    void (async () => {
      await Promise.resolve();
      if (cancelled) return;
      try {
        const l = await listSandboxFiles(diffWith);
        if (cancelled) return;
        setOtherList(l);
        setOtherError(null);
      } catch (e) {
        if (!cancelled) {
          setOtherList(null);
          setOtherError(errMsg(e, "could not list peer files"));
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [diffWith]);

  /** "diff vs" picker — switching sides reloads the open file's view. */
  function pickDiff(otherId: string) {
    setDiffWith(otherId);
    setOtherList(null);
    setOtherError(null);
    setDiff(null);
    setContent(null);
    setViewError(null);
    if (selected) void openFile(selected, otherId);
  }

  function toggleDir(path: string) {
    setCollapsed((prev) => {
      const next = new Set(prev);
      if (next.has(path)) next.delete(path);
      else next.add(path);
      return next;
    });
  }

  const tree = useMemo(
    () => (list ? buildFileTree(list.entries) : []),
    [list],
  );
  const mergedRows = useMemo(
    () => (list && otherList ? mergeFileLists(list.entries, otherList.entries) : []),
    [list, otherList],
  );
  const summary = useMemo(() => diffSummary(mergedRows), [mergedRows]);
  const diffLines = useMemo(
    () => (diff ? parseDiffLines(diff.diff) : []),
    [diff],
  );

  const otherLabel = peers.find((p) => p.id === diffWith)?.label ?? "";

  return (
    <div className="modal-backdrop" role="presentation" onClick={onClose}>
      <div
        className="modal modal--wide"
        role="dialog"
        aria-modal="true"
        aria-label={`Artifact files — ${label}`}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="modal__head">
          <span className="modal__title">
            artifact files — {label}
            <span className="mono" title={sandboxId}>
              {" "}
              · {shortId(sandboxId)}
            </span>
          </span>
          <div className="row" style={{ gap: "0.5rem" }}>
            {peers.length > 0 ? (
              <select
                value={diffWith}
                onChange={(e) => pickDiff(e.target.value)}
                title="Compare this sandbox's files against another sandbox side by side"
                style={{ fontSize: "0.8rem", maxWidth: 240 }}
              >
                <option value="">browse only</option>
                {peers.map((p) => (
                  <option key={p.id} value={p.id}>
                    diff vs {p.label}
                  </option>
                ))}
              </select>
            ) : null}
            <button
              type="button"
              className="btn btn--sm btn--ghost"
              onClick={onClose}
            >
              ✕
            </button>
          </div>
        </div>
        <div className="modal__body fb-body">
          {listError ? (
            <ErrorNote title="Could not list files">{listError}</ErrorNote>
          ) : !list ? (
            <Loading label="Listing artifact files…" />
          ) : (
            <div className="fb-split">
              <div className="fb-left">
                <div className="fb-note">
                  {list.root === "artifact"
                    ? "artifact root — no workspace snapshot (prompt cell / early failure)"
                    : "workspace/"}
                  {list.truncated ? " · listing truncated" : ""}
                </div>
                {diffWith ? (
                  <>
                    <div className="fb-lanes">
                      <span className="fb-lane" />
                      <span
                        className="fb-lane"
                        title={`left — this sandbox (${shortId(sandboxId)})`}
                      >
                        a: {shortId(sandboxId)}
                      </span>
                      <span
                        className="fb-lane"
                        title={`right — ${otherLabel} (${shortId(diffWith)})`}
                      >
                        b: {shortId(diffWith)}
                      </span>
                    </div>
                    {otherError ? (
                      <ErrorNote title="Peer listing failed">{otherError}</ErrorNote>
                    ) : !otherList ? (
                      <Loading label="Listing peer files…" />
                    ) : (
                      <>
                        <div className="fb-note">
                          {summary.onlyLeft} only left · {summary.onlyRight}{" "}
                          only right · {summary.both} shared
                          {otherList.truncated ? " · peer listing truncated" : ""}
                        </div>
                        <div className="fb-mlist">
                          {mergedRows.map((row) => (
                            <button
                              key={row.key}
                              type="button"
                              className={`fb-mrow${selected === row.path ? " fb-mrow--sel" : ""}`}
                              onClick={() =>
                                row.kind === "file"
                                  ? void openFile(row.path, diffWith)
                                  : undefined
                              }
                              disabled={row.kind !== "file"}
                              title={
                                row.side === "left"
                                  ? `${row.path} — only in ${shortId(sandboxId)}`
                                  : row.side === "right"
                                    ? `${row.path} — only in ${otherLabel || shortId(diffWith)}`
                                    : row.path
                              }
                            >
                              <span
                                className="fb-mcell"
                                style={{ paddingLeft: `${row.depth * 0.9}rem` }}
                              >
                                {row.kind === "dir" ? `${row.name}/` : row.name}
                              </span>
                              <span className="fb-mcell fb-mcell--lane">
                                {row.left
                                  ? row.left.kind === "dir"
                                    ? "dir"
                                    : formatBytes(row.left.size)
                                  : "—"}
                              </span>
                              <span className="fb-mcell fb-mcell--lane">
                                {row.right
                                  ? row.right.kind === "dir"
                                    ? "dir"
                                    : formatBytes(row.right.size)
                                  : "—"}
                              </span>
                            </button>
                          ))}
                        </div>
                      </>
                    )}
                  </>
                ) : list.entries.length === 0 ? (
                  <EmptyState>The artifact root is empty.</EmptyState>
                ) : (
                  <TreeView
                    nodes={tree}
                    selected={selected}
                    collapsed={collapsed}
                    onToggleDir={toggleDir}
                    onOpen={(p) => void openFile(p, diffWith)}
                  />
                )}
              </div>
              <div className="fb-right">
                {viewLoading ? (
                  <Loading label="Loading…" />
                ) : viewError ? (
                  <ErrorNote title="Could not load file">{viewError}</ErrorNote>
                ) : diffWith && diff ? (
                  <>
                    <div className="fb-path mono">
                      {diff.path}
                      {"  "}
                      {diff.a_missing ? (
                        <Badge tone="amber">only in b</Badge>
                      ) : null}
                      {diff.b_missing ? (
                        <Badge tone="amber">only in a</Badge>
                      ) : null}
                      {diff.identical ? (
                        <Badge tone="green">identical</Badge>
                      ) : null}
                      {diff.truncated ? (
                        <Badge tone="amber">truncated</Badge>
                      ) : null}
                    </div>
                    {diff.binary ? (
                      <div className="fb-note">
                        binary content — can&apos;t diff.{" "}
                        <a
                          className="chip"
                          href={sandboxFileDownloadUrl(sandboxId, diff.path)}
                        >
                          ⬇ a
                        </a>{" "}
                        <a
                          className="chip"
                          href={sandboxFileDownloadUrl(diffWith, diff.path)}
                        >
                          ⬇ b
                        </a>
                      </div>
                    ) : diff.identical ? (
                      <div className="fb-note">Files are identical.</div>
                    ) : (
                      <pre className="code fb-pre">
                        {diffLines.map((l, i) => (
                          <span key={i} className={`fb-dl fb-dl--${l.kind}`}>
                            {l.text}
                            {"\n"}
                          </span>
                        ))}
                      </pre>
                    )}
                  </>
                ) : content ? (
                  <>
                    <div className="fb-path mono" title={content.path}>
                      {content.path} · {formatBytes(content.size)}
                      {content.truncated ? " · truncated" : ""}{" "}
                      <a
                        className="chip"
                        href={sandboxFileDownloadUrl(sandboxId, content.path)}
                        title="Download this file"
                      >
                        ⬇
                      </a>
                    </div>
                    {content.binary ? (
                      <div className="fb-note">
                        binary file ({formatBytes(content.size)}) — download to
                        inspect.
                      </div>
                    ) : (
                      <pre className="code fb-pre">{content.text}</pre>
                    )}
                  </>
                ) : (
                  <EmptyState>Select a file to view.</EmptyState>
                )}
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
