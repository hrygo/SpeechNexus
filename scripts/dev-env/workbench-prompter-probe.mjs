/** Production manual reading; synthetic text only, no speech/device qualification. */
import { chromium, expect } from "@playwright/test";
import assert from "node:assert/strict";
import { mkdir, writeFile } from "node:fs/promises";
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
const { preview } = await import(pathToFileURL(require.resolve("vite")).href);
const key = "speechnexus.workbench.prompter.v1";
const text =
  "😀 合成阅读稿，不是真实业务内容。\n" +
  "保留空白与换行，键盘可以控制当前位置。\n".repeat(18) +
  "<img src=x onerror=alert(1)>\n结束。";
const errors = [],
  outbound = [],
  api = [];
let server, browser;
try {
  server = await preview({
    root: workbench,
    logLevel: "error",
    preview: { host: "127.0.0.1", port: 0, strictPort: false },
  });
  const origin = `http://127.0.0.1:${server.httpServer.address().port}`;
  browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({
    reducedMotion: "reduce",
    viewport: { width: 1280, height: 1000 },
  });
  await context.route("**/*", async (route) => {
    const url = new URL(route.request().url());
    if (url.origin !== origin) {
      outbound.push(url.origin);
      return route.abort();
    }
    if (url.pathname.startsWith("/v1/")) {
      api.push(url.pathname);
      return route.abort();
    }
    return route.continue();
  });
  await context.addInitScript(() => {
    window.probeMicrophoneCalls = 0;
    if (navigator.mediaDevices)
      navigator.mediaDevices.getUserMedia = async () => {
        window.probeMicrophoneCalls++;
        throw new Error("Unexpected microphone request");
      };
  });
  const page = await context.newPage();
  page.setDefaultTimeout(5000);
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto(`${origin}/?dev-scenario=tts-task`);
  await page.getByRole("button", { name: "提词阅读", exact: true }).click();
  await expect(page.getByText("手动提词阅读可用", { exact: true })).toBeVisible();
  await page.getByLabel("阅读稿", { exact: true }).fill(text);
  await page.getByRole("button", { name: "使用此稿", exact: true }).click();
  assert.equal(await page.getByLabel("固定阅读稿", { exact: true }).textContent(), text);
  assert.equal(await page.getByLabel("固定阅读稿", { exact: true }).locator("img").count(), 0);
  assert.equal(await page.evaluate((key) => localStorage.getItem(key), key), null);
  const reading = page.getByLabel("固定阅读稿", { exact: true });
  await reading.focus();
  await page.keyboard.press("End");
  await expect(page.getByLabel("阅读位置", { exact: true })).toHaveValue(
    String(Array.from(text).length),
  );
  assert.ok(await reading.evaluate((element) => element.scrollTop > 0));
  await page.keyboard.press("Home");
  await page.keyboard.press("PageDown");
  await expect(page.getByLabel("阅读位置", { exact: true })).toHaveValue("80");
  await page.getByRole("button", { name: "保存到此浏览器", exact: true }).click();
  const saved = JSON.parse(await page.evaluate((key) => localStorage.getItem(key), key));
  assert.equal(saved.script.text, text);
  assert.equal(saved.position, 80);
  assert.equal(Object.hasOwn(saved, "streamEpoch"), false);
  assert.equal(await page.locator("label").first().textContent(), "阅读稿");
  await page.getByLabel("阅读稿", { exact: true }).fill("未应用也未保存的编辑");
  await page.reload();
  await page.getByRole("button", { name: "提词阅读", exact: true }).click();
  await expect(page.getByLabel("阅读稿", { exact: true })).toHaveValue("");
  await page.getByRole("button", { name: "恢复并替换当前稿", exact: true }).click();
  await expect(page.getByLabel("阅读稿", { exact: true })).toHaveValue(text);
  await expect(page.getByLabel("阅读位置", { exact: true })).toHaveValue("80");
  // The region holds two live regions with different meanings (a local operation status and a
  // saved-deployment snapshot), so target the one this assertion is about instead of relying on
  // a count that would break the moment a second honest region appears.
  await expect(page.getByRole("status", { name: "本地提词状态", exact: true })).toContainText(
    "手动阅读",
  );
  await page.getByLabel("阅读稿", { exact: true }).fill("替换当前阅读稿，尚未保存。");
  await page.getByRole("button", { name: "使用此稿", exact: true }).click();
  await page.getByRole("button", { name: "文件转写", exact: true }).click();
  await page.getByRole("button", { name: "提词阅读", exact: true }).click();
  await expect(page.getByLabel("阅读稿", { exact: true })).toHaveValue("");
  await page.getByRole("button", { name: "恢复并替换当前稿", exact: true }).click();
  await expect(page.getByLabel("阅读稿", { exact: true })).toHaveValue(text);
  await page.setViewportSize({ width: 360, height: 900 });
  assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
  await expect
    .poll(async () => {
      const geometry = await reading.evaluate((element) => {
        const selected = element.querySelector('[data-current="true"]');
        if (!selected) return { missing: true };
        const viewport = element.getBoundingClientRect(),
          current = selected.getBoundingClientRect();
        return {
          top: viewport.top,
          bottom: viewport.bottom,
          currentTop: current.top,
          currentBottom: current.bottom,
          scrollTop: element.scrollTop,
          scrollHeight: element.scrollHeight,
          clientHeight: element.clientHeight,
          visible: current.top >= viewport.top && current.bottom <= viewport.bottom,
        };
      });
      return geometry.visible;
    })
    .toBe(true);
  await page.getByRole("button", { name: "回到起点", exact: true }).focus();
  await page.keyboard.press("Tab");
  await expect(reading).toBeFocused();
  assert.ok(
    await reading.evaluate(
      (element) =>
        element.matches(":focus-visible") && getComputedStyle(element).outlineStyle !== "none",
    ),
  );
  if (values.screenshot) {
    await mkdir(dirname(resolve(values.screenshot)), { recursive: true });
    await page.screenshot({ path: resolve(values.screenshot), fullPage: true });
  }
  assert.equal(await page.evaluate(() => window.probeMicrophoneCalls), 0);
  assert.deepEqual(errors, []);
  assert.deepEqual(outbound, []);
  assert.deepEqual(api, []);
  const result = {
    checked_at: new Date().toISOString(),
    kind: "production-manual-prompter-synthetic",
    browser: browser.version(),
    viewport_width: 360,
    text_codepoints: Array.from(text).length,
    checkpoint_position: 80,
    exact_text: true,
    explicit_restore_manual: true,
    keyboard_and_focus: true,
    horizontal_overflow: false,
    microphone_calls: 0,
    api_requests: 0,
    outbound_requests: 0,
    page_errors: 0,
    qualification: "manual-browser-reading-only",
  };
  if (values.output) {
    await mkdir(dirname(resolve(values.output)), { recursive: true });
    await writeFile(resolve(values.output), JSON.stringify(result, null, 2) + "\n");
  }
  console.log(JSON.stringify(result, null, 2));
} finally {
  await browser?.close();
  if (server)
    await new Promise((done, reject) =>
      server.httpServer.close((error) => (error ? reject(error) : done())),
    );
}
