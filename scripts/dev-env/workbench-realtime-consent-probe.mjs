/**
 * Real C HTTP + production bundle: the realtime tab must not touch the microphone.
 *
 * The page promises the operator decides when the microphone opens ("先创建并连接…再由你明确
 * 开启麦克风"). This drives the production bundle against a deployment with no realtime base and
 * checks that promise holds: no microphone request before a session is connected, no capture
 * control even offered yet, an honest answer to a session request this deployment cannot serve,
 * and no microphone request even after that answer.
 *
 * "Answered openly" is three different facts that used to look alike:
 *
 * - `refused` — a response arrived and it was `status >= 400`;
 * - `networkFailed` — no response ever arrived because the transport failed (`requestfailed`);
 * - nothing at all — the request was never sent, or the page showed a prompt that was already
 *   there before the click.
 *
 * A transient busy notice ("正在提交原请求…") is not an answer about the request, so it is
 * excluded from the answer wait. The wait itself is the request's own outcome, not a fixed
 * sleep: a response that arrives later is still the answer, and reading the arrays before it
 * arrives can no longer pass.
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
// The one path whose refusal this probe is about.
const CREATE_PATH = "/v1/nexus/realtime/sessions";
// Shown while the create request is in flight. It answers nothing about the request.
const BUSY_COPY = "正在提交原请求";
const ANSWER_TIMEOUT_MS = 30000;
// The page aborts the create request after this long (RealtimeClient's transport timeout).
// A refusal has to be the server's answer, not the page giving up on a server that never
// answered, so the click-to-answer time is recorded and capped below this value.
const PAGE_TIMEOUT_MS = 15000;

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
  refused = [],
  networkFailed = [],
  apiRequests = [],
  pageErrors = [];
const sameOriginPath = (url) => {
  const parsed = new URL(url);
  return parsed.origin === origin ? parsed.pathname : null;
};
page.on("pageerror", (error) => pageErrors.push(error.name));
// A response proves the request reached a server; its status is the server's answer.
page.on("response", (response) => {
  const path = sameOriginPath(response.url());
  if (path === null || response.status() < 400) return;
  refused.push({ path, method: response.request().method(), status: response.status() });
});
// No response at all: refused/reset/aborted below HTTP. A failure without its text is
// indistinguishable from "the probe did not look", so the browser's reason is kept.
page.on("requestfailed", (request) => {
  const path = sameOriginPath(request.url());
  if (path === null) return; // The probe's own outbound abort is counted in `outbound`.
  networkFailed.push({
    path,
    method: request.method(),
    failure: request.failure()?.errorText ?? "",
  });
});
// Which API calls the page actually made: the request must leave the page at all.
page.on("request", (request) => {
  const path = sameOriginPath(request.url());
  if (path !== null && /^\/(?:v1|v2\/workbench)(?:\/|$)/u.test(path))
    apiRequests.push({ path, method: request.method() });
});
await context.route("**/*", async (route) => {
  if (new URL(route.request().url()).origin === origin) return route.continue();
  outbound.push("Unexpected outbound");
  return route.abort();
});
const microphoneAttempts = () => page.evaluate(() => window.__microphoneAttempts);
// Every alert/status the operator can actually see, within the realtime workbench.
const visibleAnswers = async (root) => {
  const nodes = root.locator('[role="alert"], [role="status"]');
  const texts = [];
  for (let index = 0; index < (await nodes.count()); index += 1) {
    const node = nodes.nth(index);
    if (await node.isVisible()) {
      // A re-render between the count and the read must not make the probe throw.
      const text = await node.innerText().catch(() => "");
      if (text) texts.push(text.replace(/\s+/g, " ").trim());
    }
  }
  return texts;
};
const answersNotBusy = async (root) =>
  (await visibleAnswers(root)).filter((text) => !text.startsWith(BUSY_COPY));

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
  let answersBeforeClick = [],
    answersAfterClick = [],
    answer = "notAttempted",
    elapsedMs = null,
    problemShown = false;
  if (!createFormDisabled) {
    await section.getByLabel("会话预设编号").fill("preset_synthetic");
    await section.getByLabel("预设版本").fill("1");
    await section.getByLabel("实时转写模型编号").fill("asr_synthetic");
    await section.getByLabel("模型版本").fill("1");
    // `not.toHaveAttribute` also passes when the attribute is absent, so pin it while the
    // page is idle: a build that stops exposing `aria-busy` must fail here instead of leaving
    // the post-click wait spinning on nothing.
    await expect(section).toHaveAttribute("aria-busy");
    // A prompt that is already on screen before the click is not an answer to the click.
    answersBeforeClick = await visibleAnswers(section);
    assert.deepEqual(
      answersBeforeClick,
      [],
      `an answer was already visible before the click: ${JSON.stringify(answersBeforeClick)}`,
    );
    // Arm the outcome before the click: whichever way the create request ends — a response
    // or a transport failure — becomes the fact this probe waits for, however long it takes.
    const settled = Promise.race([
      page
        .waitForResponse(
          (response) => sameOriginPath(response.url()) === CREATE_PATH && response.status() >= 400,
          { timeout: ANSWER_TIMEOUT_MS },
        )
        .then(() => "refused")
        .catch(() => "timeout"),
      page
        .waitForResponse(
          (response) => sameOriginPath(response.url()) === CREATE_PATH && response.status() < 400,
          { timeout: ANSWER_TIMEOUT_MS },
        )
        .then(() => "accepted")
        .catch(() => "timeout"),
      page
        .waitForEvent("requestfailed", {
          predicate: (request) => sameOriginPath(request.url()) === CREATE_PATH,
          timeout: ANSWER_TIMEOUT_MS,
        })
        .then(() => "networkFailed")
        .catch(() => "timeout"),
    ]);
    const clickedAt = Date.now();
    await section.getByRole("button", { name: "创建实时会话" }).click();
    answer = await settled;
    elapsedMs = Date.now() - clickedAt;
    if (answer !== "timeout") {
      // The page shows a busy notice while the request is in flight; it is not an answer.
      // Wait until the section is no longer busy, then for the answer the operator is told
      // about: a notice that outlived `busy` is about the request, whichever copy the page uses.
      await expect(section).not.toHaveAttribute("aria-busy", "true", { timeout: 20000 });
      await expect
        .poll(async () => (await answersNotBusy(section)).length, { timeout: 20000 })
        .toBeGreaterThan(0);
      answersAfterClick = await visibleAnswers(section);
      problemShown = (await answersNotBusy(section)).length > 0;
    }
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
    // How the create request itself ended: refused / networkFailed / accepted / timeout.
    answer,
    // Click to that ending: a page that gave up after its own timeout is not an answer.
    elapsedMs,
    pageTimeoutMs: PAGE_TIMEOUT_MS,
    answersBeforeClick,
    answersAfterClick,
    problemShown,
    createRequestIssued: apiRequests.some(
      (item) => item.path === CREATE_PATH && item.method === "POST",
    ),
    apiRequests,
    refused,
    networkFailed,
    microphoneAttempts: afterCreate,
    narrowOverflow,
    observedCopy: (await section.innerText()).replace(/\s+/g, " ").slice(0, 600),
    outbound: outbound.length,
    pageErrors: pageErrors.length,
  };
} finally {
  await context.close();
  await browser.close();
}
await writeFile(resolve(output, "result.json"), `${JSON.stringify(result, null, 2)}\n`);
process.stdout.write(`${JSON.stringify(result)}\n`);
