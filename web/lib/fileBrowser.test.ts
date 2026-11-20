/**
 * Unit tests for the PR34 file-browser transforms (`buildFileTree`,
 * `mergeFileLists`, `diffSummary`, `parseDiffLines`).
 *
 * Runs under Node's built-in test runner — no framework:
 *   cd web && npm test      →   node --test lib/*.test.ts
 *
 * Oracles are literal hand-computed expectations over the fixtures — e.g.
 * the tree oracle names the exact nested node order a wrong sort (files
 * before dirs, or input-order passthrough) would reject, and the merge
 * oracle asserts that a file present on one side renders an empty lane on
 * the other (a plausible wrong behavior: dropping single-sided paths, or
 * leaving a nested file's parents unsynthesized so it shows as root-level).
 */
import assert from "node:assert/strict";
import { describe, test } from "node:test";
import type { FileEntry } from "./api.ts";
import {
  buildFileTree,
  diffSummary,
  mergeFileLists,
  parseDiffLines,
  type FileNode,
} from "./fileBrowser.ts";

const file = (path: string, size = 1): FileEntry => ({
  path,
  kind: "file",
  size,
});
const dir = (path: string): FileEntry => ({ path, kind: "dir", size: null });

const A_ENTRIES: FileEntry[] = [
  file("index.html", 120),
  file("README.md", 30),
  dir("src"),
  file("src/app.py", 45),
  file("src/util/deep.py", 10),
  dir("assets"),
  file("assets/icon.png", 999),
];

describe("buildFileTree", () => {
  test("nests flat entries, dirs first then name at every level", () => {
    const roots = buildFileTree(A_ENTRIES);
    // Root level: dirs (assets, src) sorted, then files (README, index).
    assert.deepEqual(
      roots.map((n) => n.path),
      ["assets", "src", "README.md", "index.html"],
    );
    const byPath = new Map<string, FileNode>();
    const walk = (ns: FileNode[]) => {
      for (const n of ns) {
        byPath.set(n.path, n);
        walk(n.children);
      }
    };
    walk(roots);
    assert.equal(byPath.get("src/app.py")?.size, 45);
    assert.equal(byPath.get("src/app.py")?.kind, "file");
    assert.equal(byPath.get("src/app.py")?.depth, 1);
    // 'src/util' is a synthesized parent dir (not in A_ENTRIES)
    assert.equal(byPath.get("src/util")?.kind, "dir");
    assert.deepEqual(
      byPath.get("src/util")?.children.map((n) => n.path),
      ["src/util/deep.py"],
    );
    // leaf file has an empty children array (UI never null-checks)
    assert.deepEqual(byPath.get("index.html")?.children, []);
  });

  test("a nested file whose dirs were never emitted still nests", () => {
    const roots = buildFileTree([file("a/b/c.txt")]);
    assert.equal(roots.length, 1);
    assert.equal(roots[0].path, "a");
    assert.equal(roots[0].children[0].path, "a/b");
    assert.equal(roots[0].children[0].children[0].path, "a/b/c.txt");
    assert.equal(roots[0].children[0].children[0].kind, "file");
  });

  test("empty input → empty tree", () => {
    assert.deepEqual(buildFileTree([]), []);
  });
});

describe("mergeFileLists (PR10 split idiom for files)", () => {
  const B_ENTRIES: FileEntry[] = [
    file("index.html", 140), // differs — both sides
    dir("src"),
    file("src/app.py", 45), // identical path — both
    file("solo.txt", 8), // only on b
    file("new/thing.js", 20), // only on b, nested (parent synthesized)
  ];

  test("one merged ordering; single-sided files face an empty lane", () => {
    const rows = mergeFileLists(A_ENTRIES, B_ENTRIES);
    const byPath = new Map(rows.map((r) => [r.path, r]));
    // union of paths — nothing dropped
    for (const p of [
      "assets",
      "assets/icon.png",
      "index.html",
      "new",
      "new/thing.js",
      "README.md",
      "solo.txt",
      "src",
      "src/app.py",
      "src/util",
      "src/util/deep.py",
    ]) {
      assert.ok(byPath.has(p), `missing row for ${p}`);
    }
    // left-only: README + the assets subtree
    assert.equal(byPath.get("README.md")?.side, "left");
    assert.equal(byPath.get("README.md")?.right, null);
    assert.equal(byPath.get("assets")?.side, "left");
    // right-only: solo.txt + the 'new' subtree — 'new' dir SYNTHESIZED on
    // the right (B_ENTRIES never listed it) and absent on the left.
    assert.equal(byPath.get("solo.txt")?.side, "right");
    assert.equal(byPath.get("solo.txt")?.left, null);
    assert.equal(byPath.get("new")?.side, "right");
    assert.equal(byPath.get("new")?.kind, "dir");
    // 'src/util' synthesized on A only — B holds nothing beneath it.
    assert.equal(byPath.get("src/util")?.side, "left");
    // both: shared paths.
    assert.equal(byPath.get("index.html")?.side, "both");
    assert.equal(byPath.get("src")?.side, "both");
    assert.equal(byPath.get("src/app.py")?.side, "both");
    // sizes surface per side
    assert.equal(byPath.get("index.html")?.left?.size, 120);
    assert.equal(byPath.get("index.html")?.right?.size, 140);
  });

  test("parents precede children; ordering is deterministic", () => {
    const rows = mergeFileLists(A_ENTRIES, B_ENTRIES);
    const order = rows.map((r) => r.path);
    // every parent path sorts before its descendants
    const idx = (p: string) => order.indexOf(p);
    assert.ok(idx("src") < idx("src/app.py"));
    assert.ok(idx("src/util") < idx("src/util/deep.py"));
    assert.ok(idx("new") < idx("new/thing.js"));
    assert.deepEqual(
      order,
      [...order].sort(),
      "rows are one path-sorted ordering",
    );
  });

  test("empty side → every row single-sided", () => {
    const rows = mergeFileLists(A_ENTRIES, []);
    assert.ok(rows.every((r) => r.side === "left" && r.right === null));
  });
});

describe("diffSummary", () => {
  test("counts files per side; dirs never count", () => {
    const rows = mergeFileLists(
      [dir("d"), file("d/a.py"), file("only-a.txt"), file("same.txt")],
      [dir("d"), file("d/a.py"), file("only-b.txt"), file("same.txt")],
    );
    assert.deepEqual(diffSummary(rows), {
      onlyLeft: 1,
      onlyRight: 1,
      both: 2,
    });
  });
});

describe("parseDiffLines", () => {
  const DIFF = [
    "--- a/index.html",
    "+++ b/index.html",
    "@@ -1,2 +1,2 @@",
    "-<h1>2048</h1>",
    "+<h1>2048!</h1>",
    ' <script src="game.js"></script>',
    "\\ No newline at end of file",
  ].join("\n");

  test("classifies unified-diff line kinds by prefix", () => {
    const lines = parseDiffLines(DIFF);
    assert.deepEqual(
      lines.map((l) => l.kind),
      ["meta", "meta", "hunk", "del", "add", "context", "meta"],
    );
    // text survives verbatim — including the leading space of context
    assert.equal(lines[5].text, ' <script src="game.js"></script>');
  });

  test("content lines starting with +/- in context keep context kind", () => {
    // A context line whose payload begins with '+' still arrives prefixed
    // by ' ' in unified format → context, not add.
    const lines = parseDiffLines("@@ -1 +1 @@\n +literal\n");
    assert.equal(lines[1].kind, "context");
  });

  test("empty diff → no lines", () => {
    assert.deepEqual(parseDiffLines(""), []);
  });
});
