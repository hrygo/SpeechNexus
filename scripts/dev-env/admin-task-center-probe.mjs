/** Real browser exercise of synthetic development tasks; no live service is allowed. */
import { chromium, expect } from "@playwright/test";
import assert from "node:assert/strict";
import { mkdir, readdir, readFile, writeFile } from "node:fs/promises";
import { createRequire } from "node:module";
import { dirname, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { parseArgs } from "node:util";

const { values } = parseArgs({
  options: { output: { type: "string" }, screenshot: { type: "string" } },
});
const root = fileURLToPath(new URL("../../", import.meta.url));
const admin = resolve(root, "web/admin");
const require = createRequire(resolve(admin, "package.json"));
const { createServer, preview } = await import(pathToFileURL(require.resolve("vite")).href);
const server = await createServer({
  root: admin,
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
  const context = await browser.newContext({ reducedMotion: "reduce" });
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
  page.on("pageerror", () => errors.push("Browser page error"));
  page.on("websocket", (socket) => {
    const url = new URL(socket.url());
    if (url.hostname !== "127.0.0.1") unexpected.push("Unexpected websocket");
  });
  await page.goto(`${origin}/?dev-scenario=task-center`);
  await expect(page.getByRole("heading", { name: "任务状态演练", exact: true })).toBeVisible();
  await expect(page.getByLabel("部署数据边界")).toContainText("没有登录或角色隔离");
  const read = () => page.getByRole("button", { name: "读取任务列表", exact: true }).click();
  const view = (id) => page.getByRole("button", { name: `查看 ${id}`, exact: true }).click();
  const detail = page.getByTestId("task-detail-status");
  const choose = async (id) => {
    await page.getByLabel("演练场景").selectOption(id);
    await read();
  };
  const cancel = async () => {
    await page.getByRole("button", { name: "准备取消", exact: true }).click();
    const confirm = page.getByRole("button", { name: "确认取消本任务", exact: true });
    await expect(confirm).toBeFocused();
    await confirm.press("Enter");
  };

  await read();
  await view("job-demo");
  await expect(detail).toHaveText("queued");
  await page.getByRole("button", { name: "下一页", exact: true }).click();
  await view("job-second");
  await expect(page.getByTestId("task-detail-id")).toHaveText("job-second");
  await expect(page.getByRole("button", { name: "下一页", exact: true })).toBeDisabled();
  await read();
  await view("job-demo");
  await cancel();
  await expect(detail).toHaveText("cancelling");
  await expect(page.getByTestId("task-cleanup")).toHaveText("pending");
  await expect(
    page.getByText("accepted=true / cancelling / pending", { exact: true }),
  ).toBeVisible();

  await choose("cancel-unknown");
  await view("job-demo");
  await cancel();
  await expect(page.getByRole("alert")).toContainText("取消结果未知");
  await page.getByRole("button", { name: "下一页", exact: true }).click();
  await view("job-second");
  await expect(page.getByRole("button", { name: "准备取消", exact: true })).toBeDisabled();
  await page.getByRole("button", { name: "按原键重放取消", exact: true }).click();
  await expect(page.getByTestId("synthetic-cancel-identity")).toHaveText("同键同目标重放：一致");
  await expect(page.getByTestId("synthetic-cancel-count")).toHaveText("本地演练取消提交次数：2");
  await expect(page.getByTestId("task-detail-id")).toHaveText("job-second");
  await expect(detail).toHaveText("queued");
  await page.getByRole("button", { name: "重读取消原任务", exact: true }).click();
  await expect(detail).toHaveText("cancelling");
  await expect(page.getByTestId("task-cleanup")).toHaveText("pending");

  await choose("empty");
  await expect(page.getByText("当前快照没有任务。可重新读取列表。", { exact: true })).toBeVisible();
  await choose("list-failed");
  await expect(page.getByRole("alert")).toContainText("列表读取失败");
  await read();
  await view("job-demo");
  await expect(detail).toHaveText("queued");
  await choose("job-failed");
  await view("job-demo");
  await expect(detail).toHaveText("failed");
  await expect(page.getByTestId("task-cleanup")).toHaveText("quarantined");
  await expect(page.getByRole("button", { name: "准备取消", exact: true })).toBeDisabled();
  await expect(page.getByRole("alert")).toContainText("engine_failure");
  await page.setViewportSize({ width: 320, height: 768 });
  assert(
    await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth),
    "Mobile layout overflowed",
  );
  await page.emulateMedia({ colorScheme: "dark" });
  assert(
    await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth),
    "Dark mobile layout overflowed",
  );
  await page.setViewportSize({ width: 1365, height: 1100 });
  if (values.screenshot) {
    const path = resolve(values.screenshot);
    await mkdir(dirname(path), { recursive: true });
    await page.screenshot({ path, fullPage: true });
  }

  production = await preview({
    root: admin,
    logLevel: "error",
    preview: { host: "127.0.0.1", port: 0, strictPort: false },
  });
  const productionAddress = production.httpServer.address();
  assert(productionAddress && typeof productionAddress !== "string");
  const productionOrigin = `http://127.0.0.1:${productionAddress.port}`;
  allowedOrigins.add(productionOrigin);
  await page.goto(`${productionOrigin}/?dev-scenario=task-center`);
  await expect(page.getByRole("heading", { name: "运行事实管理", exact: true })).toBeVisible();
  await expect(page.getByLabel("演练场景")).toHaveCount(0);
  const assets = resolve(admin, "dist/assets");
  for (const file of await readdir(assets)) {
    if (!/\.(js|css)$/.test(file)) continue;
    const content = await readFile(resolve(assets, file), "utf8");
    assert(
      !content.includes("synthetic-cancel-identity") && !content.includes("synthetic-page-2"),
      "Production includes synthetic task code",
    );
  }
  assert.deepEqual(errors, []);
  assert.deepEqual(unexpected, []);
  const report = {
    checkedAt: new Date().toISOString(),
    kind: "development_admin_task_center",
    browser: browser.version(),
    scenariosPassed: 5,
    keyboardConfirmation: true,
    originalCancellationIdentity: true,
    acceptanceNotRelease: true,
    mobileOverflow: false,
    productionScenarioExcluded: true,
    browserErrors: errors.length,
    unexpectedRequests: unexpected.length,
    liveServiceVerified: false,
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
  await server.close();
  if (production)
    await new Promise((resolve, reject) =>
      production.httpServer.close((error) => (error ? reject(error) : resolve())),
    );
}
