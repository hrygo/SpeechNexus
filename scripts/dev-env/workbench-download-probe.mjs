/** Production Web + C runtime + PG/MinIO, using only synthetic export media. */
import { chromium, expect } from "@playwright/test";
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { resolve } from "node:path";
import { parseArgs } from "node:util";

const { values } = parseArgs({
  options: {
    url: { type: "string" },
    output: { type: "string" },
    "book-title": { type: "string" },
    sha256: { type: "string" },
    bytes: { type: "string" },
  },
});
assert(values.url && values.output && values["book-title"] && values.sha256 && values.bytes);
const origin = new URL(values.url).origin;
assert.equal(new URL(origin).hostname, "127.0.0.1");
const output = resolve(values.output);
await mkdir(output, { recursive: true });
const browser = await chromium.launch({ headless: true });
const context = await browser.newContext({ acceptDownloads: true, reducedMotion: "reduce" });
const page = await context.newPage();
const errors = [],
  outbound = [],
  downloads = [];
page.on("pageerror", (error) => errors.push(error.name));
page.on("request", (request) => {
  if (/\/exports\/[^/]+\/content$/.test(new URL(request.url()).pathname))
    downloads.push({ method: request.method(), path: new URL(request.url()).pathname });
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
  const panel = section.getByRole("region", { name: "有声书导出", exact: true });
  await expect(panel.getByRole("button", { name: "校验并下载导出" })).toHaveCount(0);
  assert.equal(downloads.length, 0);
  const accepting = page.waitForResponse(
    (response) =>
      response.request().method() === "POST" &&
      new URL(response.url()).pathname.endsWith("/exports"),
  );
  await panel.getByRole("button", { name: "发起导出", exact: true }).click();
  const accepted = await (await accepting).json();
  assert.equal(typeof accepted.id, "string");
  const button = panel.getByRole("button", { name: "校验并下载导出", exact: true });
  await expect(button).toBeVisible({ timeout: 30_000 });
  assert.equal(downloads.length, 0);
  const saving = page.waitForEvent("download");
  await button.click();
  const saved = await saving;
  assert.equal(await saved.failure(), null);
  assert.equal(saved.suggestedFilename(), `${accepted.id}.zip`);
  const content = await readFile(await saved.path());
  assert.equal(content.byteLength, Number(values.bytes));
  assert.equal("sha256:" + createHash("sha256").update(content).digest("hex"), values.sha256);
  await expect(panel.getByRole("status")).toContainText("完整性校验通过");
  assert.equal(downloads.length, 1);
  assert.equal(downloads[0].method, "GET");
  assert.equal(outbound.length, 0);
  assert.equal(errors.length, 0);
  await page.screenshot({ path: resolve(output, "download.png"), fullPage: true });
  const result = {
    downloadedViaProductionUi: true,
    bytes: content.byteLength,
    sha256: values.sha256,
    downloadRequests: downloads.length,
    outbound: outbound.length,
    pageErrors: errors.length,
    evidence: "synthetic-export/real-runtime-pg-minio/chromium",
  };
  await writeFile(resolve(output, "result.json"), JSON.stringify(result, null, 2) + "\n");
  process.stdout.write(JSON.stringify(result) + "\n");
} finally {
  await context.close();
  await browser.close();
}
