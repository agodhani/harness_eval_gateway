import { chromium } from "playwright";
const BASE = process.env.WEB_URL ?? "http://localhost:3002";
const OUT = "../docs/screenshots/";
const SCORED = "1777049a-872d-43bc-8109-e16dbbe54bc5";
const STUCK = "1d03964d-a3ba-4fcd-8cdd-6f851f064bdd";
const shots = [
  [`${BASE}/`, "01-run-list.png"],
  [`${BASE}/submit`, "02-submit-form.png"],
  [`${BASE}/runs/${STUCK}`, "03-run-detail.png"],
  [`${BASE}/runs/${STUCK}/timeline`, "04-timeline.png"],
  [`${BASE}/runs/${SCORED}/results`, "05-results.png"],
];
const b = await chromium.launch();
const p = await b.newPage({ viewport: { width: 1440, height: 900 } });
for (const [u, n] of shots) {
  await p.goto(u, { waitUntil: "networkidle" });
  await p.waitForTimeout(1200);
  await p.screenshot({ path: OUT + n, fullPage: true });
  console.log("captured", n);
}
await b.close();
