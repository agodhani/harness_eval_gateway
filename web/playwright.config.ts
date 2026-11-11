import { defineConfig, devices } from "@playwright/test";

/**
 * E2E harness — runs against a production build of the Next.js app.
 *
 * `webServer` executes `npm run build && npm run start` (next start), waits
 * for it to respond, runs the suite, then shuts it down. `npm run test:e2e`
 * is therefore the single entrypoint both locally and in CI — no manual
 * server management needed.
 *
 * Port: defaults to 3000 (what `next start` uses). Set E2E_PORT to run
 * against a different port when :3000 is already taken locally, e.g.
 * `E2E_PORT=3100 npm run test:e2e`.
 *
 * Future specs live in `web/e2e/` and inherit `baseURL` + the managed server.
 * Specs that need backend services (API, Postgres, gateway) should be gated
 * once those exist — see FEATURE_MAP.md phases.
 */
const PORT = Number(process.env.E2E_PORT ?? 3000);
const BASE_URL = `http://localhost:${PORT}`;

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  reporter: "list",
  use: {
    baseURL: BASE_URL,
    trace: "on-first-retry",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: {
    command: `npm run build && npm run start -- -p ${PORT}`,
    url: BASE_URL,
    // Reuse an already-running dev/prod server locally; always manage our own
    // in CI so results are deterministic.
    reuseExistingServer: !process.env.CI,
    // `next build` + `next start` cold start can take a couple of minutes.
    timeout: 300_000,
  },
});
