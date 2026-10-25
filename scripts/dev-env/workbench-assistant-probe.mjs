/**
 * Real C HTTP + private PG + production bundle: the assistant surface's honest state.
 * The deployment's own capability verdict decides whether the create form exists at all,
 * so this probe refuses to find one when the server did not say `available`.
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
    expect: { type: "string" },
  },
});
assert(values.url && values.output && values.expect);
const expected = values.expect;
assert(["disabled", "unavailable"].includes(expected));
const origin = new URL(values.url).origin;
assert.equal(new URL(origin).hostname, "127.0.0.1");
const output = resolve(values.output);
await mkdir(output, { recursive: true });

const COPY = {
  disabled: "本部署未启用语音助手",
  unavailable: "本部署缺少语音助手依赖",
};

const browser = await chromium.launch({ headless: true });
const context = await browser.newContext({
  reducedMotion: "reduce",
  viewport: { width: 1280, height: 900 },
});
const page = await context.newPage();
const outbound = [],
  failed = [],
  pageErrors = [],
  creates = [];
page.on("pageerror", (error) => pageErrors.push(error.name));
page.on("response", (response) => {
  if (response.status() >= 400)
    failed.push({ path: new URL(response.url()).pathname, status: response.status() });
});
page.on("request", (request) => {
  if (request.method() === "POST" && new URL(request.url()).pathname.endsWith("/assistant-turns"))
    creates.push(new URL(request.url()).pathname);
});
await context.route("**/*", async (route) => {
  if (new URL(route.request().url()).origin === origin) return route.continue();
  outbound.push("Unexpected outbound");
  return route.abort();
});

try {
  await page.goto(origin);
  await page.waitForLoadState("networkidle");
  await page.getByRole("button", { name: "语音助手", exact: true }).click();

  const panel = page.getByRole("region", { name: "语音助手工作台" });
  await expect(panel).toBeVisible();
  await expect(panel.getByText(COPY[expected], { exact: false })).toBeVisible();
  // The server's own verdict is shown verbatim, not paraphrased into readiness.
  const state = panel.locator(".assistant-capability span[data-state]");
  await expect(state).toHaveText(expected);

  // No form, no submit, and the browser never attempted a turn.
  assert.equal(await panel.getByRole("form", { name: "创建助手回合" }).count(), 0);
  assert.equal(await panel.getByRole("button", { name: "提交回合" }).count(), 0);
  await page.screenshot({ path: resolve(output, `assistant-${expected}.png`), fullPage: true });

  await page.reload();
  await page.waitForLoadState("networkidle");
  await page.getByRole("button", { name: "语音助手", exact: true }).click();
  await expect(
    page
      .getByRole("region", { name: "语音助手工作台" })
      .getByText(COPY[expected], { exact: false }),
  ).toBeVisible();

  const result = {
    kind: "production-browser-real-c-http-private-pg",
    browser: browser.version(),
    capability: expected,
    createFormPresent: false,
    turnRequests: creates.length,
    capabilityAfterReload: expected,
    narrowViewport: 360,
    outbound: outbound.length,
    unexpectedErrors: pageErrors.length,
    failed,
  };
  assert.equal(result.turnRequests, 0);
  assert.equal(result.outbound, 0);
  assert.equal(result.unexpectedErrors, 0);
  assert.deepEqual(failed, []);
  await writeFile(resolve(output, "result.json"), JSON.stringify(result, null, 2) + "\n");
  process.stdout.write(JSON.stringify({ stage: "done", ...result }) + "\n");
} finally {
  await context.close();
  await browser.close();
}
