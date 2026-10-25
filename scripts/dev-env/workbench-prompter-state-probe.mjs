/**
 * Real C HTTP + private PG + production bundle: the deployment teleprompter state.
 * The panel must report an unsaved project as missing (never as position zero), write an
 * accepted revision/position/manual hold, and refuse an older revision without retrying.
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
    "project-name": { type: "string" },
  },
});
assert(values.url && values.output && values["project-name"]);
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
  writes = [];
page.on("pageerror", (error) => pageErrors.push(error.name));
page.on("response", (response) => {
  if (response.status() >= 400)
    failed.push({ path: new URL(response.url()).pathname, status: response.status() });
});
page.on("request", (request) => {
  if (request.method() === "PUT" && request.url().includes("/teleprompter"))
    writes.push(request.postDataJSON());
});
await context.route("**/*", async (route) => {
  if (new URL(route.request().url()).origin === origin) return route.continue();
  outbound.push("Unexpected outbound");
  return route.abort();
});

try {
  await page.goto(origin);
  await page.waitForLoadState("networkidle");
  await page.getByRole("button", { name: "提词阅读", exact: true }).click();

  const block = page.getByLabel("部署内提词状态");
  await expect(block).toBeVisible();
  // Reading the deployment is explicit: no project or status request before the click.
  assert.deepEqual(writes, []);
  await block.getByRole("button", { name: "读取部署项目" }).click();
  await block.getByLabel("项目").selectOption({ label: values["project-name"] });
  await block.getByRole("button", { name: "读取部署状态" }).click();
  // Nothing saved yet: the panel says so and never reports position zero.
  await expect(block.getByRole("alert")).toContainText("尚无已保存的提词状态");
  assert.equal(await block.getByText(/位置 0/).count(), 0);

  await block.getByLabel("脚本修订").fill("2");
  await block.getByLabel("可见位置").fill("42");
  await block.getByLabel("手动接管").check();
  await block.getByLabel(/请求键/).fill("prompter-browser-2");
  await block.getByRole("button", { name: "保存到当前部署" }).click();
  await expect(block.getByText(/部署内已保存：脚本修订 2，位置 42，手动接管 是/)).toBeVisible();
  await page.screenshot({ path: resolve(output, "prompter-state.png"), fullPage: true });

  // An older script revision must be refused, and the refusal must not move the reading.
  await block.getByLabel("脚本修订").fill("1");
  await block.getByLabel(/请求键/).fill("prompter-browser-1");
  await block.getByRole("button", { name: "保存到当前部署" }).click();
  await expect(block.getByRole("alert")).toContainText("比本次提交更新");
  await expect(block.getByText(/脚本修订 2，位置 42/)).toBeVisible();

  const result = {
    kind: "production-browser-real-c-http-private-pg",
    browser: browser.version(),
    missingReportedAsMissing: true,
    accepted: { script_revision: "2", visible_position: 42, manual_hold: true },
    conflictRetried: false,
    putRequests: writes.map((body) => body.script_revision),
    outbound: outbound.length,
    unexpectedErrors: pageErrors.length,
    failed,
  };
  assert.equal(result.outbound, 0);
  assert.equal(result.unexpectedErrors, 0);
  assert.deepEqual(result.putRequests, ["2", "1"]);
  await writeFile(resolve(output, "result.json"), JSON.stringify(result, null, 2) + "\n");
  process.stdout.write(JSON.stringify({ stage: "done", ...result }) + "\n");
} finally {
  await context.close();
  await browser.close();
}
