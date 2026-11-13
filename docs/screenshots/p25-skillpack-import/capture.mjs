/**
 * Screenshot capture for the F11 GitHub skillpack import PR.
 *
 * Usage:
 *   cd web && npx next dev -p 3111 &          # dev server (any free port)
 *   cd web && node ../docs/screenshots/p25-skillpack-import/capture.mjs
 *
 * BASE_URL env overrides the default http://localhost:3111.
 * All /api/skillpacks* calls are mocked via page.route — no backend needed.
 * Writes PNGs next to this script (1280px viewport, light scheme, 2x DPR).
 */
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const require = createRequire(`${process.cwd()}/package.json`); // web/'s deps
const { chromium } = require("@playwright/test");

const BASE = process.env.BASE_URL ?? "http://localhost:3111";
const OUT = dirname(fileURLToPath(import.meta.url));

// -- realistic mock payloads (mirrors anthropics/skills) --------------------

const PACKS = [
  {
    name: "mattpocock-skills",
    source: "vendored",
    skill_count: 27,
    skills: [],
    origin_url: "https://github.com/mattpocock/skills",
    imported_at: null,
  },
  {
    name: "spec-kit",
    source: "vendored",
    skill_count: 9,
    skills: [],
    origin_url: "https://github.com/github/spec-kit",
    imported_at: null,
  },
  {
    name: "superpowers",
    source: "vendored",
    skill_count: 15,
    skills: [],
    origin_url: "https://github.com/obra/superpowers",
    imported_at: null,
  },
  {
    name: "frontend-design",
    source: "imported",
    skill_count: 1,
    skills: ["frontend-design"],
    origin_url: "https://github.com/anthropics/skills",
    imported_at: "2026-11-12T18:22:41Z",
  },
];

const DISCOVER = {
  url: "https://github.com/anthropics/skills/tree/main/skills",
  repo: "anthropics/skills",
  ref: "main",
  commit_sha: "f93e5b1c4d2a88c1e0f7a2b9c4d6e8f0a1b3c5d7",
  subpath: "skills",
  kind: "tree",
  skills: [
    {
      dir: "skills/frontend-design",
      name: "frontend-design",
      description:
        "Create distinctive, production-grade frontend interfaces with high design quality.",
      file_count: 2,
      has_frontmatter: true,
    },
    {
      dir: "skills/web-artifacts-builder",
      name: "web-artifacts-builder",
      description:
        "Build elaborate multi-file artifacts: React apps, HTML pages, interactive demos.",
      file_count: 4,
      has_frontmatter: true,
    },
    {
      dir: "skills/canvas-design",
      name: "canvas-design",
      description: "Design posters, art and visual layouts as PNG/PDF canvases.",
      file_count: 6,
      has_frontmatter: true,
    },
    {
      dir: "skills/slack-gif-creator",
      name: "slack-gif-creator",
      description: "Create animated GIFs sized correctly for Slack emoji and messages.",
      file_count: 3,
      has_frontmatter: true,
    },
    {
      dir: "skills/pdf-toolkit",
      name: "pdf-toolkit",
      description: "Extract text, merge documents, and transform PDF files.",
      file_count: 5,
      has_frontmatter: true,
    },
  ],
};

// -- run --------------------------------------------------------------------

let importSucceeded = false;

const browser = await chromium.launch();
const ctx = await browser.newContext({
  viewport: { width: 1280, height: 940 },
  colorScheme: "light",
  deviceScaleFactor: 2,
});
const page = await ctx.newPage();

await page.route("**/api/skillpacks", (route) =>
  route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify(
      importSucceeded
        ? [
            ...PACKS,
            {
              name: "frontend-design-2",
              source: "imported",
              skill_count: 1,
              skills: ["frontend-design"],
              origin_url: "https://github.com/anthropics/skills",
              imported_at: "2026-11-13T17:40:02Z",
            },
          ]
        : PACKS,
    ),
  }),
);
await page.route("**/api/skillpacks/discover", (route) =>
  route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify(DISCOVER),
  }),
);
await page.route("**/api/skillpacks/import", async (route) => {
  const req = route.request().postDataJSON();
  if (req.mode === "pack" && req.pack_name === "frontend-design") {
    // confirm-and-rename: structured 409 with the api's suggested free name
    return route.fulfill({
      status: 409,
      contentType: "application/json",
      body: JSON.stringify({
        detail: {
          kind: "name_conflict",
          message: "a pack named 'frontend-design' already exists (imported)",
          name: "frontend-design",
          source: "imported",
          suggested_name: "frontend-design-2",
        },
      }),
    });
  }
  importSucceeded = true;
  return route.fulfill({
    status: 201,
    contentType: "application/json",
    body: JSON.stringify({
      packs: (req.mode === "split" ? req.skills : ["x"]).map((d) => ({
        pack: req.mode === "split" ? d.split("/").pop() : req.pack_name,
        source: "imported",
        skill_count: req.mode === "split" ? 1 : req.skills.length,
        imported: req.skills.map((s) => ({
          source_dir: s,
          name: s.split("/").pop(),
          dir: s.split("/").pop(),
          file_count: 3,
          renamed: false,
        })),
        issues: [],
      })),
    }),
  });
});

const shot = (name) => join(OUT, name);

// 01 — submit page: skill-pack section + "+ import from GitHub" affordance
await page.goto(`${BASE}/submit`, { waitUntil: "networkidle" });
const cfgRow = page.locator(".config-row").first();
await cfgRow.scrollIntoViewIfNeeded();
await page.waitForTimeout(300);
await cfgRow.screenshot({ path: shot("01-submit-skillpacks.png") });

// 02 — dialog: URL step
// NB: the trigger lives inside a <label>, so its accessible name isn't just
// the button text — has-text on the button element is reliable here.
await page.locator("button", { hasText: "import from GitHub" }).first().click();
const modal = page.locator(".modal");
await page.locator("#gh-url").fill("https://github.com/anthropics/skills/tree/main/skills");
await modal.screenshot({ path: shot("02-dialog-url.png") });

// 03 — dialog: discovered candidates
await page.getByRole("button", { name: "Discover", exact: true }).click();
await page.locator(".skill-pick__item").first().waitFor();
await modal.screenshot({ path: shot("03-dialog-candidates.png") });

// 04 — dialog: import modes incl. split (scroll modal to the bottom)
await modal.evaluate((el) => el.scrollTo(0, el.scrollHeight));
await page.getByText("Each selected skill as its own pack").click();
await page.waitForTimeout(150);
await modal.screenshot({ path: shot("04-dialog-modes.png") });

// 05 — collision: back to "new pack", name collides with imported pack.
// .fill() doesn't click — select the radio first so mode goes back to "new".
const newPackItem = page.locator(".check-item", { hasText: "New pack named" });
await newPackItem.locator("input[type=radio]").click();
await newPackItem.locator("input.input").fill("frontend-design");
await page.getByRole("button", { name: /^Import \d+ skills/ }).click();
const notice = page.getByText("Name taken");
await notice.waitFor();
await notice.scrollIntoViewIfNeeded();
await page.waitForTimeout(150);
await modal.screenshot({ path: shot("05-dialog-collision.png") });

// 06 — accept the suggested rename -> import succeeds -> pack auto-selected
await page.getByRole("button", { name: /^Import \d+ skills/ }).click();
await modal.waitFor({ state: "hidden" });
await cfgRow.scrollIntoViewIfNeeded();
await page.waitForTimeout(300);
await cfgRow.screenshot({ path: shot("06-submit-after-import.png") });

await browser.close();
console.log(`wrote 6 screenshots to ${OUT}`);
