/**
 * Real C HTTP + private PG + production bundle: the authoring/proofreading surface.
 * The synthetic manuscripts probe beside this one keeps a vite preview and contract
 * fixtures and is never invoked; this probe talks to a running deployment instead.
 */
import { chromium, expect } from "@playwright/test";
import assert from "node:assert/strict";
import { mkdir, writeFile } from "node:fs/promises";
import { resolve } from "node:path";
import { parseArgs } from "node:util";

const { values } = parseArgs({
  options: { url: { type: "string" }, output: { type: "string" } },
});
assert(values.url && values.output);
const origin = new URL(values.url).origin;
assert.equal(new URL(origin).hostname, "127.0.0.1");
const output = resolve(values.output);
await mkdir(output, { recursive: true });

const browser = await chromium.launch({ headless: true });
const context = await browser.newContext({ reducedMotion: "reduce" });
const page = await context.newPage();
const outbound = [],
  failed = [],
  pageErrors = [],
  saves = [];
page.on("pageerror", (error) => pageErrors.push(error.name));
page.on("response", (response) => {
  if (response.status() >= 400)
    failed.push({ path: new URL(response.url()).pathname, status: response.status() });
});
page.on("request", (request) => {
  if (request.method() === "POST" && request.url().endsWith("/revisions"))
    saves.push(request.postDataJSON());
});
await context.route("**/*", async (route) => {
  if (new URL(route.request().url()).origin === origin) return route.continue();
  outbound.push("Unexpected outbound");
  return route.abort();
});

const button = (page, name) => page.getByRole("button", { name, exact: true });
const draft = (page) => page.getByLabel("校对草稿");
async function enter(page) {
  await button(page, "项目与稿件").click();
}
async function saved(page) {
  await expect(page.getByText("新修订已保存至当前部署。", { exact: true })).toBeVisible();
}

try {
  await page.goto(origin);
  await page.waitForLoadState("networkidle");
  await enter(page);

  // Create the project and its original transcript through the real UI.
  await page.getByLabel("项目名称").fill("真实浏览器合成项目");
  await button(page, "创建项目").click();
  await expect(button(page, "真实浏览器合成项目")).toBeVisible();
  await page.getByLabel("稿件名称").fill("真实浏览器合成原稿");
  await page.getByLabel("原稿正文").fill("合成原稿 e\u0301😀\n ");
  await button(page, "保存原稿").click();
  // The deployment normalises the sealed source, so compare against what it stored.
  const originalText = await draft(page).inputValue();
  assert(originalText.includes("😀") && originalText.trim().length > 0);

  // Proofread and commit a new revision; the server head must move with it.
  await draft(page).fill("合成校对第一版");
  await button(page, "保存新修订").click();
  await saved(page);
  // The only head statement the page makes outside a conflict panel is this one.
  await expect(page.getByText("编辑来源版本 2，保存基线版本 2。", { exact: true })).toBeVisible();
  assert.equal(saves.length, 1);
  assert.equal(saves[0].expected_revision, "1");
  assert.equal(saves[0].text, "合成校对第一版");

  // A fixed historical revision loads, and saving from it keeps the source relation.
  await page.getByLabel("历史版本号").fill("1");
  await button(page, "读取固定版本").click();
  await expect(draft(page)).toHaveValue(originalText);
  await expect(page.getByText("编辑来源版本 1，保存基线版本 2。", { exact: true })).toBeVisible();
  await expect(page.getByText("正在查看历史版本。保存会追加新修订，保留所选来源。")).toBeVisible();
  await draft(page).fill("从历史版本追加的合成稿");
  await button(page, "保存新修订").click();
  await saved(page);
  // The next draft re-bases to the new head; the wire request keeps source_revision 1.
  await expect(page.getByText("编辑来源版本 3，保存基线版本 3。", { exact: true })).toBeVisible();
  assert.equal(saves.length, 2);
  assert.equal(saves[1].source_revision, "1");
  assert.equal(saves[1].expected_revision, "2");

  // The local draft survives a reload without an extra write.
  await draft(page).fill("刷新后仍保留的合成草稿");
  await page.reload();
  await page.waitForLoadState("networkidle");
  await enter(page);
  await expect(draft(page)).toBeEnabled();
  await expect(draft(page)).toHaveValue("刷新后仍保留的合成草稿");
  assert.equal(saves.length, 2);

  // Narrow viewport: the authoring surface must not scroll sideways.
  await page.setViewportSize({ width: 360, height: 800 });
  assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
  await page.screenshot({ path: resolve(output, "manuscripts-360.png"), fullPage: true });
  await page.setViewportSize({ width: 1280, height: 900 });

  // Verifying the head is a read: it leaves the editor and lists the transcript again.
  await button(page, "核对当前版本").click();
  await expect(button(page, "真实浏览器合成原稿")).toBeVisible();

  const result = {
    kind: "production-browser-real-c-http-private-pg",
    browser: browser.version(),
    contexts: 1,
    revisionsWritten: saves.length,
    headAfterSave: "合成校对第一版",
    sealedSource: originalText,
    historyLoadRestoresOriginal: true,
    saveFromHistoryKeepsSource: true,
    draftSurvivesReload: true,
    narrowViewport: 360,
    outbound: outbound.length,
    unexpectedErrors: pageErrors.length,
    failed,
  };
  assert.equal(result.outbound, 0);
  assert.equal(result.unexpectedErrors, 0);
  assert.deepEqual(failed, []);
  await writeFile(resolve(output, "result.json"), JSON.stringify(result, null, 2) + "\n");
  process.stdout.write(JSON.stringify({ stage: "done", ...result }) + "\n");
} finally {
  await context.close();
  await browser.close();
}
