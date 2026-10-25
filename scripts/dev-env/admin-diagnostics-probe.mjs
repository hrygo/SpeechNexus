/**
 * Real C HTTP + private PG + production AdminUI bundle: the deployment diagnostics page.
 * The page must report the deployment's own verdict and read nothing until asked, so this
 * probe clicks the read button and then holds the page to exactly what the server said.
 */
import { chromium, expect } from "@playwright/test";
import assert from "node:assert/strict";
import { mkdir, writeFile } from "node:fs/promises";
import { resolve } from "node:path";
import { parseArgs } from "node:util";

const { values } = parseArgs({
  options: {
    url: { type: "string" },
    output: { type: "string" },
    "expect-model": { type: "string" },
  },
});
assert(values.url && values.output && values["expect-model"]);
const expectedModel = values["expect-model"] === "-" ? null : values["expect-model"];
const origin = new URL(values.url).origin;
assert.equal(new URL(origin).hostname, "127.0.0.1");
const output = resolve(values.output);
await mkdir(output, { recursive: true });

const browser = await chromium.launch({ headless: true });
const context = await browser.newContext({
  reducedMotion: "reduce",
  viewport: { width: 1280, height: 1000 },
});
const page = await context.newPage();
const outbound = [],
  failed = [],
  pageErrors = [],
  statusReads = [];
page.on("pageerror", (error) => pageErrors.push(error.name));
page.on("response", (response) => {
  if (response.status() >= 400)
    failed.push({ path: new URL(response.url()).pathname, status: response.status() });
});
page.on("request", (request) => {
  if (new URL(request.url()).pathname.endsWith("/v1/workbench/status"))
    statusReads.push(request.method());
});
await context.route("**/*", async (route) => {
  if (new URL(route.request().url()).origin === origin) return route.continue();
  outbound.push("Unexpected outbound");
  return route.abort();
});

try {
  await page.goto(origin);
  await page.waitForLoadState("networkidle");

  const panel = page.getByRole("region", { name: "部署诊断" });
  await expect(panel).toBeVisible();
  // Reading the deployment is an explicit act, never a side effect of opening the page.
  assert.deepEqual(statusReads, []);
  assert.equal(await panel.getByRole("region", { name: "部署能力三态" }).count(), 0);

  await panel.getByRole("button", { name: "读取部署状态" }).click();
  const capabilities = panel.getByRole("region", { name: "部署能力三态" });
  await expect(capabilities).toBeVisible();
  const cards = capabilities.getByRole("article");
  assert.equal(await cards.count(), 5);
  for (let index = 0; index < 5; index += 1) {
    await expect(cards.nth(index)).toContainText("服务端状态：");
  }
  if (expectedModel === null) {
    await expect(panel.getByText("未配置（默认关闭）", { exact: false })).toBeVisible();
  } else {
    await expect(panel.getByText(expectedModel, { exact: false })).toBeVisible();
  }
  // The contract carries a code; the page must localize it, not echo it.
  await expect(panel.getByText("本部署为共享部署", { exact: false })).toBeVisible();
  assert.equal(await panel.getByText("shared_deployment", { exact: false }).count(), 0);
  await expect(panel.getByText("尚未接入", { exact: false })).toBeVisible();
  await page.screenshot({ path: resolve(output, "admin-diagnostics.png"), fullPage: true });

  const result = {
    kind: "production-admin-real-c-http-private-pg",
    browser: browser.version(),
    capabilityCards: await cards.count(),
    statusReadMethods: statusReads,
    modelShown: expectedModel === null ? "disabled" : expectedModel,
    outbound: outbound.length,
    unexpectedErrors: pageErrors.length,
    failed,
  };
  assert.equal(result.outbound, 0);
  assert.equal(result.unexpectedErrors, 0);
  assert.deepEqual(failed, []);
  assert.deepEqual(statusReads, ["GET"]);
  await writeFile(resolve(output, "result.json"), JSON.stringify(result, null, 2) + "\n");
  process.stdout.write(JSON.stringify({ stage: "done", ...result }) + "\n");
} finally {
  await context.close();
  await browser.close();
}
