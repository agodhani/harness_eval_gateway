import { expect, test } from "@playwright/test";

/**
 * Smoke suite — the template every future e2e spec follows.
 *
 * Asserts the app boots and the home page renders its scaffold content.
 * Extend with route-level checks as pages land (F1 submit form, F6 timeline,
 * F8 results — see FEATURE_MAP.md).
 */
test.describe("smoke", () => {
  test("home page returns 200 and renders scaffold content", async ({ page }) => {
    const response = await page.goto("/");
    expect(response?.status()).toBe(200);

    const heading = page.getByRole("heading", { level: 1 });
    await expect(heading).toContainText("harness_eval_gateway");
    await expect(heading).toContainText("harness");
  });

  test("landing quickstart builds the sandbox images and uses the Compose API", async ({ page }) => {
    await page.goto("/");

    const quickstart = page.locator("#start");
    await expect(quickstart).toContainText(
      /docker network inspect bridge[\s\S]*export OLLAMA_HOST[\s\S]*ollama serve[\s\S]*ollama pull qwen2\.5-coder:3b/,
    );
    await expect(quickstart).toContainText("docker compose run --rm --build api alembic upgrade head");
    await expect(quickstart).toContainText("-t heg-sandbox:latest");
    await expect(quickstart).toContainText("-t heg-exec:latest");
    await expect(quickstart.getByText("docker compose up -d --wait", { exact: true })).toBeVisible();
    await expect(quickstart).not.toContainText("uvicorn");
    await expect(quickstart.getByRole("link", { name: /native Linux Ollama/ })).toHaveAttribute(
      "href", "https://github.com/agodhani/harness_eval_gateway#quickstart",
    );
  });
});
