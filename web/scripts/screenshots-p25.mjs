// Phase 2.5 screenshot capture — submit-page multi-harness selection.
//
// Mocks GET /api/harnesses (page.route) with the real registry manifests so
// the adapter dropdown shows live-manifest display names; no backend needed.
// Usage:
//   npm run dev -- -p <port>            (or WEB_URL to point elsewhere)
//   node scripts/screenshots-p25.mjs
//
// Output: ../docs/screenshots/p25-harness-adapters/*.png

import { chromium } from "playwright";
import { mkdirSync } from "node:fs";

const BASE = process.env.WEB_URL ?? "http://localhost:3399";
const OUT = "../docs/screenshots/p25-harness-adapters/";

// Real output of orchestrator.adapters.harnesses() (registry order).
const MANIFESTS = [
  {
    name: "claude_code",
    display_name: "Claude Code CLI",
    description:
      "claude -p --output-format stream-json in a Node sandbox; model traffic is the Anthropic Messages API proxied by the AI gateway to Ollama",
    llm_protocol: "anthropic-messages",
    cli: "claude",
    cli_version: "2.1.325",
    install: "npm i -g @anthropic-ai/claude-code",
    image: "heg-sandbox-claude:latest",
    requires: ["gateway:/v1/messages"],
  },
  {
    name: "codex",
    display_name: "OpenAI Codex CLI",
    description:
      "codex exec --json in a Node sandbox; model traffic is the OpenAI Responses API proxied by the AI gateway to Ollama",
    llm_protocol: "openai-responses",
    cli: "codex",
    cli_version: "0.169.0",
    install: "npm i -g @openai/codex",
    image: "heg-sandbox-codex:latest",
    requires: ["gateway:/v1/responses"],
  },
  {
    name: "deepseek",
    display_name: "DeepSeek Harness (dsh)",
    description:
      "dsh --profile headless --json in a Node sandbox; model traffic goes through DEEPSEEK_BASE_URL → the AI gateway (Anthropic-compatible messages API)",
    llm_protocol: "anthropic-messages",
    cli: "dsh",
    cli_version: "0.2.2-rc.1",
    install: "npm i -g @deepseek-ai/dsh",
    image: "heg-sandbox-deepseek:latest",
    requires: ["gateway:/v1/messages"],
  },
  {
    name: "miniloop",
    display_name: "Miniloop (built-in)",
    description:
      "Custom minimal tool-call loop (shell, file read/write, done) running on the configured SANDBOX_IMAGE; reference harness + evaluator image",
    llm_protocol: "openai-chat-completions",
    cli: "python -m miniloop",
    requires: ["gateway:/v1/chat/completions"],
  },
  {
    name: "pi",
    display_name: "Pi (pi-mono)",
    description:
      "pi --mode json in a Node sandbox; model traffic is OpenAI chat completions via a heg provider in ~/.pi/agent/models.json",
    llm_protocol: "openai-chat-completions",
    cli: "pi",
    cli_version: "0.73.1",
    install: "npm i -g @mariozechner/pi-coding-agent",
    image: "heg-sandbox-pi:latest",
    requires: ["gateway:/v1/chat/completions"],
  },
];

mkdirSync(OUT, { recursive: true });
const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });

// Mock the manifest endpoint before any navigation.
await page.route("**/api/harnesses", (route) =>
  route.fulfill({ json: MANIFESTS }),
);

await page.goto(`${BASE}/submit`, { waitUntil: "networkidle" });

// Wait until the manifest populated the select (5 options, display names).
const firstSelect = page.locator(".config-row select.select").first();
await firstSelect
  .locator("option", { hasText: "DeepSeek Harness (dsh)" })
  .waitFor({ state: "attached" });

// Fill realistic fields + two more configs → three-harness comparison.
await page
  .locator("textarea")
  .first()
  .fill("Build a single-file HTML replica of 2048 with keyboard controls.");
await page.locator("#model_name").fill("qwen3:8b");
for (let i = 0; i < 2; i++) {
  await page.getByRole("button", { name: "+ Add configuration" }).click();
}
const selects = page.locator(".config-row select.select");
await selects.nth(0).selectOption("miniloop");
await selects.nth(1).selectOption("codex");
await selects.nth(2).selectOption("claude_code");
await page.waitForTimeout(400);

// 1 — full page: three configs, different adapters selected.
await page.screenshot({ path: `${OUT}submit-harness-configs.png`, fullPage: true });
console.log("captured submit-harness-configs.png");

// 2 — manifest-visible dropdown: expand config 1's select inline (size=N)
//      so all six display names render in-page, then crop the card.
await page.evaluate(() => {
  const el = document.querySelector(".config-row select.select");
  el.size = el.options.length;
});
const card = page.locator("section.card", { hasText: "Harness configurations" }).first();
await page.waitForTimeout(200);
await card.screenshot({ path: `${OUT}submit-adapter-dropdown.png` });
console.log("captured submit-adapter-dropdown.png");
await page.evaluate(() => {
  const el = document.querySelector(".config-row select.select");
  el.size = 1;
});

// 3 — detail crop: one harness config row (adapter + skill packs + prompt).
const row = page.locator(".config-row").nth(1);
await row.screenshot({ path: `${OUT}submit-config-detail.png` });
console.log("captured submit-config-detail.png");

await browser.close();
