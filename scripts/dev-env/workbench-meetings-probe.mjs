/** Actual C HTTP + private PG + production bundle. Test-only fake LLM; no model/device claim. */
import { chromium, expect } from "@playwright/test";
import assert from "node:assert/strict";
import { mkdir, writeFile } from "node:fs/promises";
import { createInterface } from "node:readline";
import { resolve } from "node:path";
import { parseArgs } from "node:util";

const { values } = parseArgs({
  options: {
    url: { type: "string" },
    output: { type: "string" },
    // Opt-in: keep the configured model reachable and accept whatever text it returns.
    "llm-always-on": { type: "boolean", default: false },
    "minutes-any-text": { type: "boolean", default: false },
  },
});
assert(values.url && values.output);
const origin = new URL(values.url).origin;
assert.equal(new URL(origin).hostname, "127.0.0.1");
const output = resolve(values.output);
await mkdir(output, { recursive: true });
const input = createInterface({ input: process.stdin, terminal: false })[Symbol.asyncIterator]();
async function stage(name, extra = {}) {
  process.stdout.write(JSON.stringify({ stage: name, ...extra }) + "\n");
  const line = await input.next();
  assert(!line.done && line.value === "continue");
}
const browser = await chromium.launch({ headless: true });
const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
const page = await context.newPage();
const errors = [],
  outbound = [],
  failed = [];
const writes = [];
page.on("pageerror", (e) => errors.push(e.name));
page.on("response", (r) => {
  if (r.status() >= 400) failed.push({ path: new URL(r.url()).pathname, status: r.status() });
});
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
  if (r.method() === "POST" && /\/meetings(?:\/|$)/.test(new URL(r.url()).pathname))
    writes.push({
      path: new URL(r.url()).pathname,
      key: r.headers()["idempotency-key"],
      body: r.postDataJSON(),
    });
});
const region = () => page.getByRole("region", { name: "会议记录工作台" });
async function enter() {
  await page.getByRole("button", { name: "会议记录", exact: true }).click();
  await expect(region()).toBeVisible();
}
try {
  await page.goto(origin);
  await enter();
  await page.getByRole("button", { name: "合成浏览器会议项目", exact: true }).click();
  await page.getByRole("button", { name: "合成浏览器会议稿", exact: true }).click();
  await expect(page.getByLabel("会议校对正文")).toHaveValue(" 合成原文 e\u0301😀\n ");
  await page.evaluate(() => {
    const original = Storage.prototype.setItem;
    window.__meetingProbeSetItem = original;
    Storage.prototype.setItem = function (key, value) {
      if (key === "speechnexus.workbench.meetings.v1")
        throw new DOMException("Synthetic quota", "QuotaExceededError");
      return original.call(this, key, value);
    };
  });
  await page.getByLabel("会议校对正文").fill(" 仅内存 e\u0301😀\n ");
  await expect(page.getByRole("alert")).toHaveText(/本地恢复信息未能保存/);
  await page.getByRole("button", { name: "文件转写", exact: true }).click();
  await expect(region()).toBeHidden();
  await enter();
  await expect(page.getByLabel("会议校对正文")).toHaveValue(" 仅内存 e\u0301😀\n ");
  await expect(page.getByRole("button", { name: "重试保存恢复信息" })).toBeEnabled();
  await page.evaluate(() => {
    Storage.prototype.setItem = window.__meetingProbeSetItem;
    delete window.__meetingProbeSetItem;
  });
  await page.getByRole("button", { name: "重试保存恢复信息" }).click();
  await expect(page.getByRole("button", { name: "重试保存恢复信息" })).toHaveCount(0);
  await page.getByLabel("会议名称", { exact: true }).fill("浏览器合成会议");
  await page.getByRole("button", { name: "创建会议记录", exact: true }).click();
  await expect(page.getByLabel("当前会议编号")).toBeVisible();
  const meetingId = await page.getByLabel("当前会议编号").inputValue();
  await page.getByLabel("会议校对正文").fill(" 校对正文 e\u0301😀\n ");
  await page.getByRole("button", { name: "封存校对稿", exact: true }).click();
  await expect(page.getByRole("button", { name: "按原键重放" })).toBeEnabled();
  await page.reload();
  await enter();
  await expect(page.getByLabel("会议校对正文")).toHaveValue(" 校对正文 e\u0301😀\n ");
  await page.getByRole("button", { name: "按原键重放" }).click();
  await expect(page.getByRole("button", { name: "按原键重放" })).toHaveCount(0);
  await expect(page.getByText(/封存 v2/)).toBeVisible();
  await page.getByRole("button", { name: "生成纪要", exact: true }).click();
  await expect(page.getByRole("button", { name: "按原键重放" })).toBeEnabled();
  if (!values["llm-always-on"]) {
    await stage("disable_llm", { meetingId });
    await page.reload();
    await enter();
    await expect(page.getByText(/尚未配置可用的纪要模板/)).toBeVisible();
    await expect(page.getByRole("button", { name: "生成纪要", exact: true })).toBeDisabled();
  }
  const replay = page.waitForResponse(
    (r) => r.request().method() === "POST" && r.url().endsWith("/minutes"),
  );
  await page.getByRole("button", { name: "按原键重放" }).click();
  const response = await replay;
  assert.equal(response.status(), 202);
  const accepted = await response.json();
  await expect(page.getByRole("button", { name: "按原键重放" })).toHaveCount(0);
  // A deployment that dispatches its own queue may already be generating by now.
  await expect(
    page.getByText(
      values["llm-always-on"] ? /纪要：(已排队|正在生成|已完成)/ : /纪要：已排队/,
    ),
  ).toBeVisible();
  await stage("execute", { minutesId: accepted.id, meetingId });
  await page.getByRole("button", { name: "刷新当前记录", exact: true }).click();
  if (values["minutes-any-text"]) {
    await expect(page.getByLabel("纪要正文")).toHaveText(/\S/, { timeout: 60000 });
  } else {
    await expect(page.getByLabel("纪要正文")).toHaveText("合成纪要 <script>not executed</script>");
  }
  assert.equal(await page.locator(".meetings-minutes script").count(), 0);
  await page.setViewportSize({ width: 360, height: 800 });
  await page.getByLabel("会议校对正文").focus();
  await expect(page.getByLabel("会议校对正文")).toBeFocused();
  await page.keyboard.press("Tab");
  assert((await page.locator(":focus").count()) === 1);
  assert(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth));
  await region().scrollIntoViewIfNeeded();
  await page.screenshot({ path: resolve(output, "meeting-360.png"), fullPage: true });
  await stage("stale");
  await page.getByRole("button", { name: "刷新当前记录", exact: true }).click();
  await expect(page.getByRole("region", { name: "比较来源版本" })).toBeVisible();
  await expect(page.getByLabel("纪要正文")).toHaveCount(0);
  await expect(page.getByText(/来源已变化，结果已过期/)).toBeVisible();
  await expect(page.getByLabel("会议校对正文")).toHaveValue(" 校对正文 e\u0301😀\n ");
  await stage("delete");
  await page.getByRole("button", { name: "刷新当前记录", exact: true }).click();
  await expect(page.getByRole("alert")).toHaveText(/已不可读取/);
  await expect(page.getByLabel("会议校对正文")).toHaveValue(" 校对正文 e\u0301😀\n ");
  assert.equal(writes.length, 5);
  assert.deepEqual(writes[1], writes[2]);
  assert.deepEqual(writes[3], writes[4]);
  assert(writes.every((w) => w.key));
  assert.equal(outbound.length, 0);
  assert.equal(errors.length, 0);
  assert.deepEqual(failed.map((f) => f.status).sort(), [404, 503, 503]);
  const result = {
    kind: values["llm-always-on"]
      ? "production-browser-real-c-http-private-pg-configured-local-llm"
      : "production-browser-real-c-http-private-pg-fake-llm",
    browser: browser.version(),
    contexts: 1,
    writes: writes.length,
    originalSealReplay: true,
    disabledMinutesReplay: true,
    plainText: true,
    expiredHidden: true,
    deletedDraftRetained: true,
    quotaNavigation: true,
    viewport: 360,
    outbound: 0,
    unexpectedErrors: 0,
    expectedFailures: failed,
  };
  await writeFile(resolve(output, "result.json"), JSON.stringify(result, null, 2) + "\n");
  process.stdout.write(JSON.stringify({ stage: "done", ...result }) + "\n");
} finally {
  await context.close();
  await browser.close();
  process.stdin.destroy();
}
