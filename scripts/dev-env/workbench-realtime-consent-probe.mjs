/**
 * Real C HTTP + production bundle: the realtime tab must not touch the microphone.
 *
 * The page promises the operator decides when the microphone opens ("先创建并连接…再由你明确
 * 开启麦克风"). This drives the production bundle against a deployment with no realtime base and
 * checks that promise holds: no microphone request before a session is connected, no capture
 * control even offered yet, an honest answer to a session request this deployment cannot serve,
 * and no microphone request even after that answer.
 *
 * It also records the real tab order of whatever controls the page currently offers, and the
 * 360px layout. Nothing here is a device, capture or speech qualification.
 */
import { chromium, expect } from "@playwright/test";
import assert from "node:assert/strict";
import { mkdir, writeFile } from "node:fs/promises";
import { resolve } from "node:path";
import { parseArgs } from "node:util";

const { values } = parseArgs({
  options: {
    url: { type: "string" },
    output: { type: "string" },
  },
});
assert(values.url && values.output);
const origin = new URL(values.url).origin;
assert.equal(new URL(origin).hostname, "127.0.0.1");
const output = resolve(values.output);
await mkdir(output, { recursive: true });

// The create form as it exists before any session is connected.
const FORM = ["会话预设编号", "预设版本", "实时转写模型编号", "模型版本", "创建实时会话"];

const browser = await chromium.launch({ headless: true });
const context = await browser.newContext({
  reducedMotion: "reduce",
  viewport: { width: 1280, height: 1000 },
});
// Count every microphone request instead of granting one. A request before a connected
// session would break the promise the page makes to the operator.
await context.addInitScript(() => {
  window.__microphoneAttempts = 0;
  const devices = navigator.mediaDevices;
  if (devices) {
    Object.defineProperty(devices, "getUserMedia", {
      configurable: true,
      value: () => {
        window.__microphoneAttempts += 1;
        return Promise.reject(new Error("microphone_requested_by_probe"));
      },
    });
  }
});
const page = await context.newPage();
const outbound = [],
  failed = [],
  pageErrors = [];
page.on("pageerror", (error) => pageErrors.push(error.name));
page.on("response", (response) => {
  if (response.status() >= 400)
    failed.push({ path: new URL(response.url()).pathname, status: response.status() });
});
await context.route("**/*", async (route) => {
  if (new URL(route.request().url()).origin === origin) return route.continue();
  outbound.push("Unexpected outbound");
  return route.abort();
});
const microphoneAttempts = () => page.evaluate(() => window.__microphoneAttempts);

let result;
try {
  await page.goto(origin);
  await page.waitForLoadState("networkidle");
  await page.getByRole("button", { name: "实时字幕", exact: true }).click();
  const section = page.getByRole("region", { name: "实时字幕工作台" });
  await expect(section).toBeVisible();

  // Opening the tab moves focus to the heading: a keyboard user is told where they are.
  await expect(page.locator(":focus")).toHaveAccessibleName("实时字幕");
  await expect(section).toContainText("再由你明确开启麦克风");
  const beforeAnything = await microphoneAttempts();
  assert.equal(beforeAnything, 0, "no microphone request before a session");

  // Read the tab order as it really is: `document.activeElement` always answers, unlike a
  // `:focus` locator, and a wrapped label names an input while a button names itself.
  const reached = [];
  for (let step = 0; step < 12; step += 1) {
    await page.keyboard.press("Tab");
    reached.push(
      await page.evaluate(() => {
        const node = document.activeElement;
        if (!node) return "";
        const labels = node.labels;
        if (labels && labels.length > 0) return labels[0].textContent ?? "";
        return node.textContent ?? "";
      }),
    );
    if (FORM.every((name) => reached.some((text) => text.includes(name)))) break;
  }
  const formSteps = reached.filter((text) => FORM.some((name) => text.includes(name)));
  assert.deepEqual(formSteps, FORM, `create form not reachable by keyboard: ${reached}`);

  // Nothing may offer capture before a session exists, and asking for a session this
  // deployment cannot serve must be answered openly.
  const captureBeforeSession = await section
    .getByRole("button", { name: "开始麦克风采集" })
    .count();
  assert.equal(captureBeforeSession, 0, "capture must not be offered before a session");
  const createFormDisabled = await section
    .getByRole("button", { name: "创建实时会话" })
    .isDisabled();
  let problemShown = false;
  if (!createFormDisabled) {
    await section.getByLabel("会话预设编号").fill("preset_synthetic");
    await section.getByLabel("预设版本").fill("1");
    await section.getByLabel("实时转写模型编号").fill("asr_synthetic");
    await section.getByLabel("模型版本").fill("1");
    await section.getByRole("button", { name: "创建实时会话" }).click();
    const answer = section.locator('[role="alert"], [role="status"]').first();
    problemShown = await answer
      .waitFor({ state: "visible", timeout: 20000 })
      .then(() => true)
      .catch(() => false);
  }
  const afterCreate = await microphoneAttempts();
  assert.equal(afterCreate, 0, "no microphone request after asking for a session");
  await page.screenshot({ path: resolve(output, "realtime-after-create.png"), fullPage: true });

  await page.setViewportSize({ width: 360, height: 800 });
  const narrowOverflow = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
  );
  assert.equal(narrowOverflow, 0, "the realtime page must not scroll horizontally at 360px");
  await page.screenshot({ path: resolve(output, "narrow-360.png"), fullPage: true });

  result = {
    stage: "done",
    kind: "production-browser-real-c-http-realtime-consent",
    focusOnOpen: true,
    consentCopyShown: true,
    expectedForm: FORM,
    formReachable: formSteps,
    captureControlBeforeSession: captureBeforeSession,
    createFormDisabled,
    problemShown,
    microphoneAttempts: afterCreate,
    narrowOverflow,
    observedCopy: (await section.innerText()).replace(/\s+/g, " ").slice(0, 600),
    outbound: outbound.length,
    pageErrors: pageErrors.length,
    failed,
  };
} finally {
  await context.close();
  await browser.close();
}
await writeFile(resolve(output, "result.json"), `${JSON.stringify(result, null, 2)}\n`);
process.stdout.write(`${JSON.stringify(result)}\n`);
