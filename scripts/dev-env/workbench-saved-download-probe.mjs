/** Production UI + real C runtime/PG/MinIO; the synthetic source is already offline. */
import { chromium, expect } from "@playwright/test";
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { resolve } from "node:path";
import { parseArgs } from "node:util";

const { values } = parseArgs({
  options: Object.fromEntries(
    ["url", "output", "book-title", "asset-id", "sha256", "bytes"].map((key) => [
      key,
      { type: "string" },
    ]),
  ),
});
for (const key of ["url", "output", "book-title", "asset-id", "sha256", "bytes"])
  assert(values[key], `Missing ${key}`);
const origin = new URL(values.url).origin;
assert.equal(new URL(origin).hostname, "127.0.0.1");
const output = resolve(values.output);
await mkdir(output, { recursive: true });
const browser = await chromium.launch({ headless: true });
const context = await browser.newContext({ acceptDownloads: true, reducedMotion: "reduce" });
const page = await context.newPage();
const errors = [],
  outbound = [],
  reads = [],
  downloads = [];
const metadataPath = `/v1/workbench/saved-assets/${values["asset-id"]}`;
page.on("pageerror", (error) => errors.push(error.name));
page.on("request", (request) => {
  const path = new URL(request.url()).pathname;
  if (path === metadataPath) reads.push(request.method());
  if (path === `${metadataPath}/content`) downloads.push(request.method());
});
await context.route("**/*", async (route) => {
  if (new URL(route.request().url()).origin === origin) return route.continue();
  outbound.push("Unexpected outbound");
  return route.abort();
});
try {
  await page.goto(origin);
  await page.getByRole("button", { name: "有声书导出", exact: true }).click();
  const section = page.getByRole("region", { name: "有声书与导出工作台" });
  await section.getByRole("button", { name: "合成导出项目", exact: true }).click();
  await section
    .getByRole("list", { name: "书目列表" })
    .getByRole("button", { name: new RegExp(values["book-title"]) })
    .click();
  await section
    .getByRole("list", { name: "章节列表" })
    .getByRole("button", { name: /第一章/ })
    .click();
  const read = section.getByRole("button", { name: "单元 1 读取副本", exact: true });
  await expect(read).toBeVisible();
  const save = section.getByRole("button", { name: "单元 1 校验并下载副本", exact: true });
  await expect(save).toHaveCount(0);
  assert.equal(reads.length, 0);
  assert.equal(downloads.length, 0);
  await read.focus();
  await read.press("Enter");
  await expect(section.getByRole("status", { name: "单元 1 副本信息" })).toContainText(
    values.sha256,
  );
  await expect(save).toBeVisible();
  assert.deepEqual(reads, ["GET"]);
  assert.equal(downloads.length, 0);
  const saving = page.waitForEvent("download");
  await save.focus();
  await save.press("Enter");
  const saved = await saving;
  assert.equal(await saved.failure(), null);
  assert.equal(saved.suggestedFilename(), `${values["asset-id"]}.wav`);
  const content = await readFile(await saved.path());
  assert.equal(content.byteLength, Number(values.bytes));
  assert.equal("sha256:" + createHash("sha256").update(content).digest("hex"), values.sha256);
  await expect(
    section.getByText("完整性校验通过，已交给浏览器保存。", { exact: true }),
  ).toBeVisible();
  assert.deepEqual(downloads, ["GET"]);
  assert.equal(outbound.length, 0);
  assert.equal(errors.length, 0);
  await page.screenshot({ path: resolve(output, "saved-download.png"), fullPage: true });
  const result = {
    downloadedViaProductionUi: true,
    metadataAndDownloadActivatedByKeyboard: true,
    bytes: content.byteLength,
    sha256: values.sha256,
    metadataRequests: reads.length,
    downloadRequests: downloads.length,
    outbound: outbound.length,
    pageErrors: errors.length,
    evidence: "synthetic-render-fixture/real-runtime-pg-minio/chromium",
  };
  await writeFile(resolve(output, "result.json"), JSON.stringify(result, null, 2) + "\n");
  process.stdout.write(JSON.stringify(result) + "\n");
} finally {
  await context.close();
  await browser.close();
}
