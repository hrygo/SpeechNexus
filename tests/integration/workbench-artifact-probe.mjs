/**
 * The workbench page reading one Job's own Artifact in a real browser.
 *
 * The file-transcription host resumes a durable task from sessionStorage; the page
 * then reads the Job over its own origin, renders the task identity rows, and
 * downloads the transcript Artifact the Job committed. This probe drives exactly
 * that path and reports the facts a caller can compare against the API:
 *
 *   * every `/v1/nexus/…` response (status, `etag`, `content-length`,
 *     `content-type`) the browser itself received;
 *   * the bytes the browser wrote to disk for the download, and their SHA-256;
 *   * the identity rows the page rendered (Job id, status, Artifact button label);
 *   * the page's own verdicts: `role="alert"` texts, page errors, console errors,
 *     and any request that left the origin.
 *
 * It never invents an artifact: it only reports what the browser got.
 */
import { chromium } from "@playwright/test";
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { parseArgs } from "node:util";

const { values } = parseArgs({
  options: {
    url: { type: "string" },
    output: { type: "string" },
    /** The Job the resumed task already names. Required in `read` mode. */
    job: { type: "string" },
    /** The Artifact the page must download. Required in `read` mode. */
    artifact: { type: "string" },
    model: { type: "string", default: "asr-quality" },
    workspace: { type: "string", default: "workspace-default" },
    /** `read` resumes the task and downloads the Artifact; `identity` only reads /v1/nexus/version. */
    mode: { type: "string", default: "read" },
    name: { type: "string", default: "probe" },
  },
});
assert(values.url && values.output);
const mode = values.mode;
assert.ok(["read", "identity"].includes(mode), "mode must be read or identity");
assert.equal(new URL(values.url).hostname, "127.0.0.1");
if (mode === "read") assert.ok(values.job && values.artifact && values.model);

const output = values.output;
await mkdir(output, { recursive: true });
const origin = new URL(values.url).origin;

const journalKey = "speechnexus.workbench.asr-task.v1";
const journal = JSON.stringify({
  version: 1,
  model: values.model ?? "asr-quality",
  assetId: "asset-probe-input",
  workspaceId: values.workspace ?? "workspace-default",
  createKey: "0d1b1d4e-3c1a-4c1b-8f7e-2b6a5c4d3e2f",
  cancelKey: "9f8e7d6c-5b4a-4938-8271-6f5e4d3c2b1a",
  jobId: values.job ?? "job-probe",
  pending: null,
  operationId: null,
  cursor: null,
});

const browser = await chromium.launch({ headless: true });
const context = await browser.newContext({
  reducedMotion: "reduce",
  viewport: { width: 1280, height: 1200 },
  acceptDownloads: true,
});
const page = await context.newPage();
const pageErrors = [];
const consoleErrors = [];
const outbound = [];
const responses = [];

page.on("pageerror", (error) => pageErrors.push(String(error)));
page.on("console", (message) => {
  if (message.type() === "error") consoleErrors.push(message.text());
});
page.on("request", (request) => {
  const url = new URL(request.url());
  if (url.origin !== origin) outbound.push(url.origin);
});
page.on("response", async (response) => {
  const url = new URL(response.url());
  if (url.origin !== origin || !url.pathname.startsWith("/v1/")) return;
  const headers = response.headers();
  responses.push({
    method: response.request().method(),
    path: url.pathname,
    status: response.status(),
    etag: headers["etag"] ?? null,
    contentLength: headers["content-length"] ?? null,
    contentType: headers["content-type"] ?? null,
    // Small JSON answers only: this probe never persists artifact bytes.
    body:
      response.request().method() === "GET" &&
      (headers["content-type"] ?? "").startsWith("application/json") &&
      Number(headers["content-length"] ?? "0") <= 4096
        ? await response.text()
        : null,
  });
});
await context.route("**/*", async (route) => {
  const url = new URL(route.request().url());
  if (url.origin === origin) return route.continue();
  outbound.push(url.origin);
  return route.abort();
});

if (mode === "read") {
  await page.addInitScript(
    ([key, value]) => {
      window.sessionStorage.setItem(key, value);
    },
    [journalKey, journal],
  );
}

const result = {
  mode,
  name: values.name,
  origin,
  job: null,
  download: null,
  alerts: [],
  notices: [],
  responses,
  pageErrors,
  consoleErrors,
  outbound,
};

try {
  await page.goto(origin, { waitUntil: "load", timeout: 30_000 });

  if (mode === "identity") {
    // The same-version banner is the page's own verdict on the digest pair.
    await page.waitForFunction(() => document.body.innerText.length > 0, undefined, {
      timeout: 20_000,
    });
    // Give the identity check time to answer and render its verdict.
    await page.waitForTimeout(2500);
  } else {
    const read = page.getByRole("button", { name: "读取原状态" });
    await read.waitFor({ state: "visible", timeout: 20_000 });
    await read.click();
    const statusRow = page.getByTestId("file-job-status");
    await statusRow.waitFor({ state: "visible", timeout: 20_000 });
    await page.waitForFunction(
      () => document.querySelector('[data-testid="file-job-status"]')?.textContent === "succeeded",
      undefined,
      { timeout: 20_000 },
    );
    await page
      .getByRole("button", { name: `下载原始制品 ${values.artifact}` })
      .waitFor({ state: "visible", timeout: 20_000 });
    const [download] = await Promise.all([
      page.waitForEvent("download", { timeout: 30_000 }),
      page.getByRole("button", { name: `下载原始制品 ${values.artifact}` }).click(),
    ]);
    const file = await download.path();
    assert.ok(file, "the browser did not persist the downloaded artifact");
    const bytes = await readFile(file);
    await page.waitForTimeout(800);
    result.download = {
      suggestedFilename: download.suggestedFilename(),
      bytes: bytes.length,
      sha256: "sha256:" + createHash("sha256").update(bytes).digest("hex"),
      isValidJson: (() => {
        try {
          JSON.parse(bytes.toString("utf8"));
          return true;
        } catch {
          return false;
        }
      })(),
    };
    result.job = {
      identityRows: await page.locator("dl").first().innerText(),
      status: (await statusRow.textContent()) ?? null,
      artifactButtons: await page
        .getByRole("button", { name: /^下载原始制品 / })
        .allTextContents(),
    };
  }

  result.alerts = await page.getByRole("alert").allTextContents();
  result.notices = await page.getByRole("status").allTextContents();
} finally {
  await writeFile(`${output}/${values.name}.json`, JSON.stringify(result, null, 2) + "\n", "utf8");
  await browser.close();
}
console.log(JSON.stringify({ name: values.name, alerts: result.alerts.length > 0 }));
