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
    await expect(heading).toContainText("MVP scaffold");
  });
});
