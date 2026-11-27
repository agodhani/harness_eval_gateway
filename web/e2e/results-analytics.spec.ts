import { expect, test } from "@playwright/test";

test.use({ viewport: { width: 390, height: 844 } });

test("mobile results keep trial analytics and pricing controls inside the card", async ({ page }) => {
  const runId = "abc123";
  const model = "qwen2.5-coder:3b";
  const rate = "example-provider-long-model-name-with-a-long-price-option";
  const metrics = {
    model,
    tokens: { input: 10, output: 5, total: 15, cache_read: 0 },
    latency_ms: { avg: null },
    tool_calls: { total: 0, by_tool: {} },
    skill_calls: { total: 0, by_skill: {} },
    llm_turns: 1,
    wall_clock_seconds: 1,
    errors: 0,
    file_ops: 0,
    shell_commands: 0,
    test_executions: 0,
  };
  const results = [1, 2].map((trial_index) => ({
    sandbox_id: `sandbox-${trial_index}`,
    model,
    name: `miniloop · ${model} · t${trial_index}`,
    status: "harness_complete",
    harness_config: { adapter: "miniloop", trial_index },
    trial_index,
    dimensions: {},
  }));
  await page.route(`**/api/runs/${runId}`, (route) => route.fulfill({
    json: { id: runId, status: "done", task_prompt: "Test task", model_name: model },
  }));
  await page.route(`**/api/runs/${runId}/results`, (route) => route.fulfill({
    json: { run_id: runId, status: "done", model_name: model, trials: 2, sandboxes: results },
  }));
  await page.route(`**/api/runs/${runId}/analytics`, (route) => route.fulfill({
    json: {
      sandboxes: results.map((sb) => ({ ...sb, ...metrics })),
      cells: [{ ...metrics, cell: "miniloop", label: "miniloop", sandbox_count: 2, overall_mean: 8, overall_sd: 0, overall_n: 2 }],
      price_table: { [rate]: { input_per_mtok_usd: 10, output_per_mtok_usd: 20 } },
    },
  }));

  await page.goto(`/runs/${runId}/results`);
  const card = page.locator("section").filter({ has: page.getByRole("heading", { name: "Analytics", exact: true }) });
  const header = card.locator("header");
  const picker = card.getByRole("combobox", { name: "Estimated cost at" });
  await expect(picker).toBeVisible();
  const bounds = (await header.boundingBox())!;
  const pickerBounds = (await picker.boundingBox())!;
  expect(pickerBounds.x).toBeGreaterThanOrEqual(bounds.x);
  expect(pickerBounds.x + pickerBounds.width).toBeLessThanOrEqual(bounds.x + bounds.width);
  await picker.selectOption(rate);
  await expect(picker).toHaveValue(rate);

  const matrix = card.locator(".matrix-wrap");
  const lastColumn = matrix.getByRole("columnheader").last();
  await expect(lastColumn).toContainText("t2");
  const { scrollWidth, clientWidth, overflowX } = await matrix.evaluate((el) => ({
    scrollWidth: el.scrollWidth,
    clientWidth: el.clientWidth,
    overflowX: getComputedStyle(el).overflowX,
  }));
  expect(scrollWidth).toBeGreaterThan(clientWidth);
  expect(overflowX).toMatch(/^(auto|scroll)$/);
  await matrix.evaluate((el) => {
    el.scrollIntoView({ block: "start", inline: "nearest" });
    el.scrollLeft = el.scrollWidth;
  });
  await expect(lastColumn).toBeInViewport({ ratio: 0.99 });

  await card.getByRole("tab", { name: "by cell" }).click();
  await expect(card.getByText("8.0 ±0.0 (n=2)")).toBeVisible();
});
