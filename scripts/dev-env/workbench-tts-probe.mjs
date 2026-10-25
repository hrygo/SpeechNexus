/** Real Chromium UI with synthetic in-memory TTS transport; never live speech. */
import { chromium, expect } from "@playwright/test";
import assert from "node:assert/strict";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { createRequire } from "node:module";
import { dirname, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { parseArgs } from "node:util";
const { values } = parseArgs({
  options: { output: { type: "string" }, screenshot: { type: "string" } },
});
const root = fileURLToPath(new URL("../../", import.meta.url));
const workbench = resolve(root, "web/workbench");
const require = createRequire(resolve(workbench, "package.json"));
const { createServer, preview } = await import(pathToFileURL(require.resolve("vite")).href);
const server = await createServer({
  root: workbench,
  logLevel: "error",
  server: { host: "127.0.0.1", port: 0 },
});
let browser;
let production;
try {
  await server.listen();
  const origin = `http://127.0.0.1:${server.httpServer.address().port}`;
  const allowed = new Set([origin]);
  browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({ acceptDownloads: true });
  const unexpected = [];
  const errors = [];
  await context.route("**/*", async (route) => {
    const url = new URL(route.request().url());
    if (allowed.has(url.origin) && !url.pathname.startsWith("/v1/")) await route.continue();
    else {
      unexpected.push("Unexpected network request");
      await route.abort();
    }
  });
  await context.addInitScript(() => {
    window.probeUrls = { made: [], revoked: [] };
    const create = URL.createObjectURL.bind(URL);
    const revoke = URL.revokeObjectURL.bind(URL);
    URL.createObjectURL = (blob) => {
      const url = create(blob);
      window.probeUrls.made.push(url);
      return url;
    };
    URL.revokeObjectURL = (url) => {
      window.probeUrls.revoked.push(url);
      revoke(url);
    };
  });
  const page = await context.newPage();
  page.on("pageerror", () => errors.push("Page error"));
  await page.goto(`${origin}/?dev-scenario=tts-task`);
  await expect(page.getByRole("heading", { name: "文本配音演练", exact: true })).toBeVisible({
    timeout: 3000,
  });
  const start = () => page.getByRole("button", { name: "创建配音任务", exact: true }).click();
  const events = () => page.getByRole("button", { name: "读取任务事件", exact: true }).click();
  const choose = (scenario) => page.getByLabel("配音演练场景").selectOption(scenario);
  await page.getByLabel("配音文本").fill("合成测试，不是真实书稿。");
  await page.getByLabel("配音文本").focus();
  await page.keyboard.press("Tab");
  await expect(page.getByRole("button", { name: "创建配音任务", exact: true })).toBeFocused();
  await page.keyboard.press("Enter");
  await events();
  await expect(page.getByTestId("tts-status")).toHaveText("succeeded");
  await page.getByRole("button", { name: "试听合成音频", exact: true }).click();
  await expect
    .poll(() => page.locator("audio").evaluate((audio) => audio.readyState))
    .toBeGreaterThanOrEqual(2);
  assert((await page.locator("audio").evaluate((audio) => audio.duration)) > 0);
  await expect
    .poll(() => page.locator("audio").evaluate((audio) => audio.currentTime))
    .toBeGreaterThan(0);
  await expect(page.getByRole("status").filter({ hasText: "试听已结束" })).toBeVisible();
  const downloadEvent = page.waitForEvent("download");
  await page.getByRole("button", { name: "下载合成音频", exact: true }).click();
  const download = await downloadEvent;
  assert.equal(download.suggestedFilename(), "synthetic-speech.wav");
  assert.equal((await readFile(await download.path())).subarray(0, 4).toString(), "RIFF");
  await page.setViewportSize({ width: 390, height: 844 });
  assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
  await page.getByLabel("配音文本").focus();
  await page.keyboard.press("Tab");
  await expect(page.getByRole("button", { name: "创建配音任务", exact: true })).not.toBeFocused();
  if (values.screenshot) {
    await mkdir(dirname(resolve(values.screenshot)), { recursive: true });
    await page.screenshot({ path: resolve(values.screenshot), fullPage: true });
  }
  await choose("create-unknown");
  await expect
    .poll(() =>
      page.evaluate(() => window.probeUrls.made.length === window.probeUrls.revoked.length),
    )
    .toBe(true);
  await start();
  await expect(page.getByRole("alert")).toContainText("结果未知");
  await page.getByRole("button", { name: "按原键重放", exact: true }).click();
  await expect(page.getByTestId("tts-status")).toHaveText("queued");
  await expect(page.getByTestId("tts-executions")).toHaveText("1");
  await expect(page.getByTestId("tts-attempts")).toHaveText("2");
  await choose("cancel-unknown");
  await start();
  await page.getByRole("button", { name: "请求取消", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText("结果未知");
  await page.getByRole("button", { name: "按原键重放", exact: true }).click();
  await expect(page.getByTestId("tts-cleanup")).toHaveText("pending");
  await expect(page.getByTestId("tts-cancellation")).toHaveText("accepted=true");
  await choose("artifact-expired");
  await start();
  await events();
  await page.getByRole("button", { name: "试听合成音频", exact: true }).click();
  await expect(page.getByRole("status").filter({ hasText: "artifact_expired" })).toBeVisible();
  await expect(page.locator("audio")).not.toHaveAttribute("src");
  production = await preview({
    root: workbench,
    logLevel: "error",
    preview: { host: "127.0.0.1", port: 0 },
  });
  const productionOrigin = `http://127.0.0.1:${production.httpServer.address().port}`;
  allowed.add(productionOrigin);
  await page.goto(`${productionOrigin}/?dev-scenario=tts-task`);
  await expect(page.getByRole("heading", { name: "语音任务集中完成", exact: true })).toBeVisible();
  await expect(page.getByLabel("配音演练场景")).toHaveCount(0);
  assert.deepEqual(errors, []);
  assert.deepEqual(unexpected, []);
  const report = {
    checked_at: new Date().toISOString(),
    kind: "development_tts_workflow",
    browser: browser.version(),
    scenariosPassed: 4,
    nativeWavDecoded: true,
    originalKeyReplay: true,
    cleanupNotAssumed: true,
    blobUrlsRevokedOnUnmount: true,
    mobileOverflow: false,
    productionScenarioExcluded: true,
    liveSpeechVerified: false,
    devicePermissionsRequested: false,
    outboundRequests: 0,
    pageErrors: 0,
  };
  if (values.output) {
    await mkdir(dirname(resolve(values.output)), { recursive: true });
    await writeFile(resolve(values.output), JSON.stringify(report, null, 2) + "\n");
  }
  process.stdout.write(JSON.stringify(report, null, 2) + "\n");
} finally {
  await browser?.close();
  if (production)
    await new Promise((resolve, reject) =>
      production.httpServer.close((error) => (error ? reject(error) : resolve())),
    );
  await server.close();
}
