/**
 * PR34 — artifact file browser + side-by-side diff: pure transforms for the
 * modal (buildFileTree, mergeFileLists, parseDiffLines).
 *
 * The API returns a FLAT entry list ({@link FileEntry}: `{path, kind,
 * size}`), so both views are derived here:
 *
 * - **browse** — `buildFileTree` nests entries into a collapsible tree,
 *   synthesizing parent dirs the server didn't emit (defensive — the
 *   backend does emit them, but a partial/truncated listing shouldn't
 *   orphan a nested file row). Dirs sort before files, then name.
 * - **diff** — `mergeFileLists` is the PR10 split idiom applied to file
 *   lists: ONE merged ordering of both sides' paths, each row carrying its
 *   left (a) and/or right (b) cell — a file only on one side renders as a
 *   populated cell facing an empty one. Implicit parents are synthesized
 *   per side so a lone nested file still shows its dirs.
 * - **diff body** — `parseDiffLines` classifies unified-diff text into
 *   meta/hunk/add/del/context rows for colored `<pre>` rendering (no diff
 *   library — the server already produced the format).
 *
 * Pure + import-light on purpose: runs under `node --test` (type-stripped)
 * — keep runtime imports extension-qualified (`./api.ts`).
 */
import type { FileEntry } from "./api.ts";

/** One node of the rendered tree. `children` is always present (empty for
 * files) so the UI never null-checks. */
export interface FileNode {
  name: string;
  path: string;
  kind: "file" | "dir";
  size: number | null;
  /** 0 at the browse root — indentation unit. */
  depth: number;
  children: FileNode[];
}

/** Get-or-create a dir node in `byPath` (recursive through ancestors). */
function dirNode(byPath: Map<string, FileNode>, path: string): FileNode {
  const existing = byPath.get(path);
  if (existing) return existing;
  const name = path.split("/").pop() ?? path;
  const parent = path.includes("/") ? path.slice(0, path.lastIndexOf("/")) : "";
  const node: FileNode = {
    name,
    path,
    kind: "dir",
    size: null,
    depth: path.split("/").length - 1,
    children: [],
  };
  byPath.set(path, node);
  if (parent) dirNode(byPath, parent).children.push(node);
  return node;
}

/**
 * Nest flat `entries` into a tree. Returns root-level nodes sorted
 * dirs-first then name; children sorted the same way. Duplicate paths
 * collapse (the first kind wins — a dir entry that also appears as a
 * parent stays a dir).
 */
export function buildFileTree(entries: FileEntry[]): FileNode[] {
  const byPath = new Map<string, FileNode>();
  // Pass 1: materialize every node + synthesize parents. Iterate in input
  // order so child lists are append-ordered; sorting happens once below.
  for (const e of entries) {
    if (!e.path) continue;
    if (byPath.has(e.path)) continue;
    const node: FileNode = {
      name: e.path.split("/").pop() ?? e.path,
      path: e.path,
      kind: e.kind === "dir" ? "dir" : "file",
      size: e.size ?? null,
      depth: e.path.split("/").length - 1,
      children: [],
    };
    byPath.set(e.path, node);
    const parent = e.path.includes("/")
      ? e.path.slice(0, e.path.lastIndexOf("/"))
      : "";
    if (parent) dirNode(byPath, parent).children.push(node);
  }
  const sortLevel = (nodes: FileNode[]) => {
    nodes.sort((a, b) =>
      a.kind !== b.kind ? (a.kind === "dir" ? -1 : 1) : a.name < b.name ? -1 : a.name > b.name ? 1 : 0,
    );
    for (const n of nodes) sortLevel(n.children);
  };
  const roots = [...byPath.values()].filter((n) => !n.path.includes("/"));
  sortLevel(roots);
  return roots;
}

/** One row of the merged two-lane listing — a path present on left (a),
 *  right (b), or both. `side` drives the +/- presence badge. `kind` is
 *  "file" when EITHER side holds a file there — a file-vs-dir path is
 *  still diffable (the server resolves the dir side as missing), so the
 *  row stays clickable and counts in `diffSummary`. */
export interface MergedFileRow {
  key: string;
  path: string;
  name: string;
  kind: "file" | "dir";
  depth: number;
  left: FileEntry | null;
  right: FileEntry | null;
  side: "both" | "left" | "right";
}

/**
 * Union of both sides' paths in ONE path-sorted ordering (parents precede
 * children lexicographically) — the PR10 merged-ordering idiom: a file on
 * only one side gets a populated cell facing an empty lane. Parent dirs
 * are synthesized per side from descendant paths, so a nested file's dirs
 * appear on both lanes even when the listing didn't emit them.
 */
export function mergeFileLists(
  a: FileEntry[],
  b: FileEntry[],
): MergedFileRow[] {
  // All dir ancestors of a path — synthesized with `kind: "dir"` so a lone
  // nested file still displays its containing dirs.
  const withParents = (entries: FileEntry[]): Map<string, FileEntry> => {
    const m = new Map<string, FileEntry>();
    const addDir = (path: string) => {
      if (!path || m.has(path)) return;
      m.set(path, { path, kind: "dir", size: null });
    };
    for (const e of entries) {
      m.set(e.path, e);
      let p = e.path;
      while (p.includes("/")) {
        p = p.slice(0, p.lastIndexOf("/"));
        if (!m.has(p)) addDir(p);
      }
    }
    return m;
  };
  const left = withParents(a);
  const right = withParents(b);
  const paths = [...new Set([...left.keys(), ...right.keys()])].sort();
  return paths.map((path) => {
    const l = left.get(path) ?? null;
    const r = right.get(path) ?? null;
    const kind = l?.kind === "file" || r?.kind === "file" ? "file" : "dir";
    return {
      key: path,
      path,
      name: path.split("/").pop() ?? path,
      kind,
      depth: path.split("/").length - 1,
      left: l,
      right: r,
      side: l && r ? "both" : l ? "left" : "right",
    };
  });
}

/** Presence counts for the diff header — files only (dirs don't diff). */
export function diffSummary(rows: MergedFileRow[]): {
  onlyLeft: number;
  onlyRight: number;
  both: number;
} {
  let onlyLeft = 0;
  let onlyRight = 0;
  let both = 0;
  for (const row of rows) {
    if (row.kind !== "file") continue;
    if (row.side === "left") onlyLeft += 1;
    else if (row.side === "right") onlyRight += 1;
    else both += 1;
  }
  return { onlyLeft, onlyRight, both };
}

/** One classified line of a unified diff. */
export interface DiffLine {
  kind: "meta" | "hunk" | "add" | "del" | "context";
  text: string;
}

/**
 * Classify server-side unified-diff text for colored `<pre>` rendering:
 * `---`/`+++` → meta ONLY as the first two lines (the file headers
 * difflib always emits first) — later `---`/`+++`-prefixed lines are
 * in-hunk content (a deleted `-- comment`, an added `++x`) and classify
 * del/add. `\` → meta anywhere, `@@` → hunk, `+` → add, `-` → del, else
 * context (context lines carry a leading space; a trailing bare line is
 * still context). No diff parsing beyond line prefix — the format is
 * stable.
 */
export function parseDiffLines(diff: string): DiffLine[] {
  if (!diff) return [];
  return diff.split("\n").map((text, i) => {
    let kind: DiffLine["kind"];
    if (
      text.startsWith("\\") ||
      (i === 0 && text.startsWith("---")) ||
      (i === 1 && text.startsWith("+++"))
    ) {
      kind = "meta";
    } else if (text.startsWith("@@")) {
      kind = "hunk";
    } else if (text.startsWith("+")) {
      kind = "add";
    } else if (text.startsWith("-")) {
      kind = "del";
    } else {
      kind = "context";
    }
    return { kind, text };
  });
}
