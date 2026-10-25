/** Production bundle + actual C HTTP + private PG. Synthetic content only. */
import { chromium, expect } from "@playwright/test";
import assert from "node:assert/strict";
import { mkdir, writeFile } from "node:fs/promises";
import { createInterface } from "node:readline";
import { resolve } from "node:path";
import { parseArgs } from "node:util";

const { values } = parseArgs({ options: { url: { type: "string" }, output: { type: "string" } } });
assert(values.url && values.output);
const origin = new URL(values.url).origin,
  output = resolve(values.output);
assert.equal(new URL(origin).hostname, "127.0.0.1");
await mkdir(output, { recursive: true });
const input = createInterface({ input: process.stdin, terminal: false })[Symbol.asyncIterator]();
async function stage(name, extra = {}) {
  process.stdout.write(JSON.stringify({ stage: name, ...extra }) + "\n");
  const line = await input.next();
  assert(!line.done && line.value === "continue");
}
const browser = await chromium.launch({ headless: true }),
  context = await browser.newContext({ viewport: { width: 1280, height: 900 } }),
  page = await context.newPage();
const errors = [],
  outbound = [],
  writes = [];
page.on("pageerror", (e) => errors.push(e.name));
page.on("console", (m) => {
  if (m.type() === "error" && !m.text().startsWith("Failed to load resource:"))
    errors.push(m.type());
});
await context.route("**/*", (route) => {
  if (new URL(route.request().url()).origin !== origin) {
    outbound.push("external");
    return route.abort();
  }
  return route.continue();
});
page.on("request", (r) => {
  if (r.method() === "POST" && /\/deletion(?:-plans|s)$/.test(new URL(r.url()).pathname))
    writes.push({
      path: new URL(r.url()).pathname,
      key: r.headers()["idempotency-key"],
      body: r.postDataJSON(),
    });
});
const region = () => page.getByRole("region", { name: "删除与恢复工作台" });
const enter = async () => {
  await page.getByRole("button", { name: "删除与恢复", exact: true }).click();
  await expect(region()).toBeVisible();
};
const select = async () => {
  await page.getByRole("button", { name: "合成待删除项目", exact: true }).click();
};
try {
  await page.goto(origin);
  await enter();
  await select();
  await page.getByRole("button", { name: "生成删除预检" }).click();
  await expect(page.getByRole("alert")).toContainText("提交结果尚未确认");
  await page.evaluate(() => {
    window.__deletionOriginal = sessionStorage.getItem("speechnexus.workbench.deletions.v1");
    sessionStorage.setItem("speechnexus.workbench.deletions.v1", "{synthetic-corruption");
  });
  await page.getByRole("button", { name: "按原键重放" }).click();
  await expect(page.getByRole("alert")).toContainText("恢复信息未能保存");
  assert.equal(writes.length, 1);
  await page.evaluate(() =>
    sessionStorage.setItem("speechnexus.workbench.deletions.v1", window.__deletionOriginal),
  );
  await page.getByRole("button", { name: "重试保存恢复信息" }).click();
  await page.reload();
  await enter();
  assert.equal(writes.length, 1);
  await page.getByRole("button", { name: "按原键重放" }).click();
  await expect(page.getByText(/存在阻断，不能确认/)).toBeVisible();
  assert.equal(await page.getByRole("checkbox").count(), 0);
  await page.getByRole("button", { name: "结束本次预检并重新选择" }).click();
  await select();
  await page.getByLabel("删除方式").selectOption("cascade");
  await page.getByRole("button", { name: "生成删除预检" }).click();
  const checks = page.getByRole("checkbox");
  await expect(checks).toHaveCount(2);
  const submit = page.getByRole("button", { name: "确认所列范围并提交删除" });
  await expect(submit).toBeDisabled();
  await checks.nth(0).check();
  await expect(submit).toBeDisabled();
  await checks.nth(1).check();
  await page.evaluate(() => {
    const original = Storage.prototype.setItem;
    window.__deletionSetItem = original;
    Storage.prototype.setItem = function (k, v) {
      if (k === "speechnexus.workbench.deletions.v1")
        throw new DOMException("quota", "QuotaExceededError");
      return original.call(this, k, v);
    };
  });
  await submit.click();
  await expect(page.getByRole("alert")).toContainText("恢复信息未能保存");
  assert.equal(writes.length, 3);
  await page.getByRole("button", { name: "文件转写", exact: true }).click();
  await enter();
  await expect(page.getByRole("button", { name: "按原键重放" })).toBeDisabled();
  await page.evaluate(() => {
    Storage.prototype.setItem = window.__deletionSetItem;
  });
  await page.getByRole("button", { name: "重试保存恢复信息" }).click();
  await page.getByRole("button", { name: "按原键重放" }).click();
  await expect(page.getByRole("alert")).toContainText("提交结果尚未确认");
  await page.reload();
  await enter();
  await page.getByRole("button", { name: "按原键重放" }).click();
  await expect(page.getByRole("heading", { name: "原始接受回执" })).toBeVisible();
  await expect(page.getByText("已排队 · 等待清理", { exact: true })).toBeVisible();
  assert.equal(writes.length, 5);
  assert.deepEqual(writes[0], writes[1]);
  assert.deepEqual(writes[3], writes[4]);
  assert.equal(writes[3].body.confirm_targets.length, 2);
  const id = await page.evaluate(
    () => JSON.parse(sessionStorage.getItem("speechnexus.workbench.deletions.v1")).accepted.id,
  );
  await stage("cleanup", { deletionId: id });
  await page.getByRole("button", { name: "读取当前清理状态" }).click();
  await expect(page.getByText("清理完成 · 已完成物理清理", { exact: true })).toBeVisible();
  await expect(page.getByText(/接受时状态：已排队/)).toBeVisible();
  await page.setViewportSize({ width: 360, height: 800 });
  assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
  await region().getByRole("heading", { name: "删除与恢复", exact: true }).focus();
  await expect(region().getByRole("heading", { name: "删除与恢复", exact: true })).toBeFocused();
  await page.keyboard.press("Tab");
  await page.screenshot({ path: resolve(output, "deletion-360.png"), fullPage: true });
  assert.equal(errors.length, 0);
  assert.equal(outbound.length, 0);
  const result = {
    browser: browser.version(),
    contexts: 1,
    writeRequests: writes.length,
    originalPreviewReplay: true,
    externalCorruptionBlocksReplay: true,
    originalAcceptReplay: true,
    quotaNavigation: true,
    completeConfirmation: true,
    physicalCleanupCurrent: true,
    acceptedSnapshotPreserved: true,
    basicFocusAndTab: true,
    narrowViewport: 360,
    unexpectedErrors: errors.length,
    outbound: outbound.length,
  };
  await writeFile(resolve(output, "result.json"), JSON.stringify(result, null, 2) + "\n");
  process.stdout.write(JSON.stringify({ stage: "done", ...result }) + "\n");
} finally {
  await context.close();
  await browser.close();
  process.stdin.destroy();
}
