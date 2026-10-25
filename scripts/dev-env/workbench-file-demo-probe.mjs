/** Exercises only explicit development scenarios with synthetic data; no live speech service. */
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
  server: { host: "127.0.0.1", port: 0, strictPort: false },
});
let browser;
let production;
try {
  await server.listen();
  const address = server.httpServer.address();
  assert(address && typeof address !== "string");
  const origin = `http://127.0.0.1:${address.port}`;
  const allowedOrigins = new Set([origin]);
  browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({ acceptDownloads: true });
  const unexpected = [];
  await context.route("**/*", async (route) => {
    const url = new URL(route.request().url());
    if (allowedOrigins.has(url.origin) && !url.pathname.startsWith("/v1/")) await route.continue();
    else {
      unexpected.push("Unexpected service or outbound request");
      await route.abort();
    }
  });
  const page = await context.newPage();
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto(`${origin}/?dev-scenario=file-task`);
  await expect(page.getByRole("heading", { name: "文件转写演练" })).toBeVisible();
  const choose = async (scenario) => {
    await page.getByLabel("故障场景").selectOption(scenario);
    await page.getByRole("button", { name: "上传合成音频", exact: true }).click();
  };
  const reconnect = () => page.getByRole("button", { name: "连接或重连事件", exact: true }).click();
  const status = page.getByTestId("job-status");
  await choose("success");
  await expect(status).toHaveText("queued");
  await reconnect();
  await expect(status).toHaveText("succeeded");
  const downloadEvent = page.waitForEvent("download");
  await page.getByRole("button", { name: "下载原始制品 artifact-demo", exact: true }).click();
  const download = await downloadEvent;
  assert.equal(download.suggestedFilename(), "synthetic-transcript.json");
  assert.deepEqual(JSON.parse(await readFile(await download.path(), "utf8")), {
    text: "合成转写，仅供演练。",
  });
  await download.delete();
  await choose("create-unknown");
  await expect(page.getByRole("alert")).toContainText("结果未知");
  await page.getByRole("button", { name: "按原幂等键重放", exact: true }).click();
  await expect(status).toHaveText("queued");
  await expect(page.getByText("1（仅本地演练计数）")).toBeVisible();
  await choose("cancel-unknown");
  await expect(status).toHaveText("queued");
  await page.getByRole("button", { name: "请求取消", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText("结果未知");
  await page.getByRole("button", { name: "重新读取状态", exact: true }).click();
  await expect(status).toHaveText("cancelling");
  await expect(page.getByTestId("cleanup-state")).toHaveText("pending");
  await page.getByRole("button", { name: "按原幂等键重放", exact: true }).click();
  await expect(page.getByText("accepted=true / pending")).toBeVisible();
  await choose("conflict");
  await expect(page.getByRole("status").filter({ hasText: "idempotency_conflict" })).toBeVisible();
  await choose("history-expired");
  await expect(status).toHaveText("queued");
  await reconnect();
  await expect(page.getByText("history-expired", { exact: true })).toBeVisible();
  await reconnect();
  await expect(status).toHaveText("succeeded");
  await expect(page.getByText("9007199254740993", { exact: true })).toBeVisible();
  await reconnect();
  await expect(page.getByText("9007199254740993", { exact: true })).toBeVisible();
  await choose("artifact-expired");
  await expect(status).toHaveText("queued");
  await reconnect();
  await expect(status).toHaveText("succeeded");
  await page.getByRole("button", { name: "下载原始制品 artifact-demo", exact: true }).click();
  await expect(page.getByRole("status").filter({ hasText: "artifact_expired" })).toBeVisible();
  await choose("operation-recovery");
  await expect(page.getByText("op-demo", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "按原幂等键重放", exact: true })).toBeDisabled();
  await page.getByRole("button", { name: "重新读取状态", exact: true }).click();
  await expect(status).toHaveText("queued");
  await expect(page.getByText("running / pending", { exact: true })).toBeVisible();
  await choose("persistent-events");
  await expect(status).toHaveText("queued");
  await reconnect();
  await expect(page.getByText("connecting", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "请求取消", exact: true })).toBeEnabled();
  await expect(page.getByRole("button", { name: "重新读取状态", exact: true })).toBeEnabled();
  await expect(page.getByRole("button", { name: "连接或重连事件", exact: true })).toBeEnabled();
  await page.getByRole("button", { name: "请求取消", exact: true }).click();
  await expect(status).toHaveText("cancelling");
  await expect(page.getByTestId("cleanup-state")).toHaveText("pending");
  await choose("failed");
  await expect(status).toHaveText("queued");
  await reconnect();
  await expect(status).toHaveText("failed");
  await expect(page.getByTestId("cleanup-state")).toHaveText("quarantined");
  await page.setViewportSize({ width: 390, height: 844 });
  assert(
    await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth),
    "Mobile layout overflowed",
  );
  await page.setViewportSize({ width: 1365, height: 1100 });
  if (values.screenshot) {
    const path = resolve(values.screenshot);
    await mkdir(dirname(path), { recursive: true });
    await page.screenshot({ path, fullPage: true });
  }
  // Also open the real production build with the same query: the dev page must be absent.
  production = await preview({
    root: workbench,
    logLevel: "error",
    preview: { host: "127.0.0.1", port: 0, strictPort: false },
  });
  const productionAddress = production.httpServer.address();
  assert(productionAddress && typeof productionAddress !== "string");
  const productionOrigin = `http://127.0.0.1:${productionAddress.port}`;
  allowedOrigins.add(productionOrigin);
  await page.goto(`${productionOrigin}/?dev-scenario=file-task`);
  await expect(page.getByRole("heading", { name: "语音任务集中完成", exact: true })).toBeVisible();
  await expect(page.getByLabel("故障场景")).toHaveCount(0);
  assert.deepEqual(errors, [], "Browser page errors");
  assert.deepEqual(unexpected, [], "No service request is allowed from the fake scenario");
  const report = {
    kind: "development_file_workflow",
    browser: browser.version(),
    contractPackage: "0.1.0",
    scenariosPassed: 9,
    originalKeyReplay: true,
    cleanupNotAssumed: true,
    exactDecimalSseCursor: true,
    syntheticDownload: true,
    productionScenarioExcluded: true,
    mobileOverflow: false,
    devicePermissionsRequested: false,
    liveSpeechVerified: false,
  };
  const serialized = `${JSON.stringify(report, null, 2)}\n`;
  if (values.output) {
    const path = resolve(values.output);
    await mkdir(dirname(path), { recursive: true });
    await writeFile(path, serialized);
  }
  process.stdout.write(serialized);
} finally {
  await browser?.close();
  if (production)
    await new Promise((resolve, reject) =>
      production.httpServer.close((error) => (error ? reject(error) : resolve())),
    );
  await server.close();
}
