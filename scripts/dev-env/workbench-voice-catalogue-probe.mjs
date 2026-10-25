/** Production bundle -> actual C runtime -> synthetic native public voice HTTP. */
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
  errors = [],
  failed = [];
let revisionReads = 0;
page.on("pageerror", (error) => errors.push(error.name));
page.on("request", (request) => {
  if (/\/voices\/[^/]+\/revisions\//.test(new URL(request.url()).pathname)) revisionReads++;
});
page.on("response", (response) => {
  if (response.status() >= 400)
    failed.push({ path: new URL(response.url()).pathname, status: response.status() });
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
  await section.getByRole("button", { name: "合成音色目录项目", exact: true }).click();
  await section.getByLabel("新有声书标题").fill("合成音色目录书");
  await section.getByRole("button", { name: "创建有声书", exact: true }).click();
  await section
    .getByRole("list", { name: "书目列表" })
    .getByRole("button", { name: /合成音色目录书/ })
    .click();
  await section.getByLabel("新章节标题").fill("合成目录章节");
  await section.getByLabel("新章节序号").fill("0");
  await section.getByLabel("新章节正文").fill("合成内容，仅验证公开音色目录读取。");
  await section.getByRole("button", { name: "新增章节", exact: true }).click();
  await expect(section.getByRole("list", { name: "章节列表" })).toContainText("合成目录章节");
  const check = section.getByRole("form", { name: "音色修订核对" });
  await check.getByLabel("核对音色 id").fill("voice-demo-1");
  await check.getByLabel("核对音色修订").fill("2");
  await page.keyboard.press("Tab");
  await expect(page.locator(":focus")).toHaveAccessibleName("读取该音色修订");
  await page.keyboard.press("Enter");
  const info = section.getByRole("status", { name: "音色修订信息" });
  await expect(info).toContainText("disabled");
  await expect(info).toContainText("2026-10-25T08:00:00Z");
  await expect(info).toContainText("binding-demo-1@1");
  await expect(info).toContainText("zh-CN");
  await expect(info).toContainText("wav");
  const reason = (await info.textContent()).split("禁用原因")[1].trim();
  assert.equal(reason, "停".repeat(2000));
  await page.screenshot({ path: resolve(output, "canonical-disabled.png"), fullPage: true });
  await check.getByLabel("核对音色 id").fill("voice-other-1");
  await check.getByRole("button", { name: "读取该音色修订", exact: true }).click();
  await expect(section.getByRole("alert", { name: "音色修订错误" })).toContainText(
    "音色目录不可用",
  );
  await expect(info).toHaveCount(0);
  assert.equal(revisionReads, 2);
  assert.deepEqual(failed, [
    { path: "/v2/workbench/voices/voice-other-1/revisions/2", status: 503 },
  ]);
  assert.deepEqual(outbound, []);
  assert.deepEqual(errors, []);
  const result = {
    keyboardRead: true,
    disabledReasonLength: reason.length,
    retirementVisible: true,
    mismatchedIdentityRefused: true,
    staleRecordCleared: true,
    revisionReads,
    outbound: outbound.length,
    pageErrors: errors.length,
    failed,
  };
  await writeFile(resolve(output, "result.json"), JSON.stringify(result, null, 2) + "\n");
  console.log(JSON.stringify(result));
} finally {
  await browser.close();
}
