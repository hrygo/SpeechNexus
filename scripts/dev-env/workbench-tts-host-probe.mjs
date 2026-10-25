/** Production Workbench with loopback synthetic HTTP; no real speech/model qualification. */
import { chromium, expect } from "@playwright/test";
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { mkdir, readFile, readdir, writeFile } from "node:fs/promises";
import { createRequire } from "node:module";
import { dirname, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { parseArgs } from "node:util";
import { parse } from "../../packages/contracts/src/runtime.mjs";

const { values } = parseArgs({
  options: { output: { type: "string" }, screenshot: { type: "string" } },
});
const root = fileURLToPath(new URL("../../", import.meta.url));
const workbench = resolve(root, "web/workbench");
const fixtures = JSON.parse(
  await readFile(resolve(root, "tests/fixtures/contracts/file-workflow.json"), "utf8"),
);
const example = (id) => structuredClone(fixtures.examples.find((e) => e.id === id).value);
// The page also reads the project catalogue, whose response example lives in the workbench
// contract fixture rather than the file-workflow one.
const workbenchFixtures = JSON.parse(
  await readFile(resolve(root, "tests/fixtures/contracts/workbench-http.json"), "utf8"),
);
const workbenchExample = (id) =>
  structuredClone(workbenchFixtures.examples.find((e) => e.id === id).value);
const key = "speechnexus.workbench.tts-task.v1";
const text = "合成测试，不是真实业务文本。";
const payload = Buffer.from(text);
const digest = (bytes) => `sha256:${createHash("sha256").update(bytes).digest("hex")}`;
const audio = Buffer.alloc(44 + 24000 * 2);
audio.write("RIFF");
audio.writeUInt32LE(audio.length - 8, 4);
audio.write("WAVEfmt ", 8);
audio.writeUInt32LE(16, 16);
audio.writeUInt16LE(1, 20);
audio.writeUInt16LE(1, 22);
audio.writeUInt32LE(24000, 24);
audio.writeUInt32LE(48000, 28);
audio.writeUInt16LE(2, 32);
audio.writeUInt16LE(16, 34);
audio.write("data", 36);
audio.writeUInt32LE(audio.length - 44, 40);
for (let i = 0; i < 24000; i++)
  audio.writeInt16LE(Math.round(2000 * Math.sin((i * Math.PI * 880) / 24000)), 44 + i * 2);
const artifact = parse("Artifact", {
  ...example("artifact"),
  id: "artifact-audio",
  kind: "audio",
  media_type: "audio/wav",
  bytes: audio.length,
  sha256: digest(audio),
});
const asset = (status = "ready") =>
  parse("Asset", {
    ...example("ready-audio"),
    id: "asset-text",
    purpose: "text_input",
    media_type: "text/plain",
    bytes: payload.length,
    sha256: digest(payload),
    duration_ms: null,
    sample_rate_hz: null,
    channels: null,
    status,
  });
let phase = "queued",
  mode = "hint",
  requests = 0,
  uploadsInMode = 0,
  loseCreate = true;
let contentDelay = 0;
const uploads = [],
  creates = [],
  cursors = [],
  errors = [];
function job() {
  return parse("Job", {
    ...example("queued-job"),
    kind: "tts",
    status: phase,
    ...(phase === "succeeded"
      ? {
          cleanup_state: "complete",
          artifacts: [
            {
              artifact_id: artifact.id,
              kind: artifact.kind,
              media_type: artifact.media_type,
              bytes: artifact.bytes,
              sha256: artifact.sha256,
              receipt_id: "receipt-demo",
            },
          ],
          result: { kind: "tts", audio_artifact_id: artifact.id, receipt_id: "receipt-demo" },
        }
      : {}),
  });
}
const error = (code) =>
  parse("ErrorResponse", {
    error: {
      code,
      type: "api_error",
      message: "Synthetic test error",
      retryable: false,
      request_id: "req-demo",
      param: null,
    },
  });
const json = (res, value, status = 200, headers = {}) => {
  res.writeHead(status, {
    "Content-Type": "application/json",
    "Cache-Control": "no-store",
    ...headers,
  });
  res.end(JSON.stringify(value));
};
const middleware = (vite) => {
  vite.middlewares.use(async (req, res, next) => {
    const path = new URL(req.url, "http://127.0.0.1").pathname;
    if (!path.startsWith("/v1/")) return next();
    requests++;
    try {
      if (path === "/v1/nexus/assets" && req.method === "POST") {
        const chunks = [];
        for await (const chunk of req) chunks.push(chunk);
        const form = await new Request("http://127.0.0.1/upload", {
          method: "POST",
          headers: { "Content-Type": req.headers["content-type"] },
          body: Buffer.concat(chunks),
        }).formData();
        assert.deepEqual([...form.keys()].sort(), ["file", "purpose"]);
        assert.equal(form.get("purpose"), "text_input");
        const file = form.get("file");
        assert.equal(file.name, "speech-text.txt");
        assert.equal(file.type, "text/plain");
        assert.deepEqual(Buffer.from(await file.arrayBuffer()), payload);
        uploads.push({ key: req.headers["idempotency-key"], mode, bytes: file.size });
        uploadsInMode++;
        if (uploadsInMode === 1)
          return json(
            res,
            error(mode === "hint" ? "asset_upload_in_progress" : "asset_upload_unknown"),
            mode === "hint" ? 409 : 503,
            {
              ...(mode === "hint" ? { Location: "/v1/nexus/assets/asset-text" } : {}),
              "X-Should-Retry": "false",
            },
          );
        return json(res, asset(), 201);
      }
      if (path === "/v1/nexus/assets/asset-text") return json(res, asset());
      if (path === "/v1/nexus/jobs" && req.method === "POST") {
        const chunks = [];
        for await (const chunk of req) chunks.push(chunk);
        const body = parse("JobCreateRequest", JSON.parse(Buffer.concat(chunks)));
        assert.equal(body.kind, "tts");
        assert.equal(body.model, "tts-quality");
        assert.deepEqual(body.input, { text_asset_id: "asset-text" });
        assert.deepEqual(body.voice, { id: "voice-demo", revision: "1" });
        assert.deepEqual(body.profile, { id: "profile-demo", revision: "2" });
        assert.deepEqual(body.output, { audio_format: "wav", save_artifacts: true });
        creates.push({ key: req.headers["idempotency-key"], body });
        if (loseCreate) {
          loseCreate = false;
          res.writeHead(200, { "Content-Type": "application/json" });
          return res.end("{");
        }
        return json(res, job(), 202);
      }
      if (path === "/v1/nexus/jobs/job-demo") return json(res, job());
      if (path === "/v1/nexus/jobs/job-demo/events") {
        const after = req.headers["last-event-id"] ?? null;
        cursors.push(after);
        const event = parse("JobEvent", {
          ...example("job.queued"),
          job_id: "job-demo",
          id: after ? "9007199254740994" : "9007199254740993",
        });
        res.writeHead(200, { "Content-Type": "text/event-stream" });
        return res.end(`id: ${event.id}\nevent: ${event.type}\ndata: ${JSON.stringify(event)}\n\n`);
      }
      if (path === "/v1/workbench/projects") {
        return json(res, workbenchExample("listProjects-response-declared-200"));
      }
      if (path === "/v1/nexus/artifacts/artifact-audio") return json(res, artifact);
      if (path === "/v1/nexus/artifacts/artifact-audio/content") {
        if (contentDelay) await new Promise((done) => setTimeout(done, contentDelay));
        res.writeHead(200, { "Content-Type": "audio/wav", "Content-Length": audio.length });
        return res.end(audio);
      }
      throw new Error(`Unexpected synthetic route: ${req.method} ${path}`);
    } catch (cause) {
      errors.push(cause instanceof Error ? cause.message : "handler error");
      json(res, error("synthetic_handler_error"), 500);
    }
  });
};
const require = createRequire(resolve(workbench, "package.json"));
const { preview } = await import(pathToFileURL(require.resolve("vite")).href);
let server, browser;
try {
  server = await preview({
    root: workbench,
    logLevel: "error",
    plugins: [{ name: "team-c-tts-host-probe", configurePreviewServer: middleware }],
    preview: { host: "127.0.0.1", port: 0, strictPort: false },
  });
  const origin = `http://127.0.0.1:${server.httpServer.address().port}`;
  browser = await chromium.launch({ headless: true });
  const outbound = [],
    pageErrors = [],
    contexts = [];
  async function context() {
    const context = await browser.newContext({ acceptDownloads: true, reducedMotion: "reduce" });
    contexts.push(context);
    await context.route("**/*", async (route) => {
      if (new URL(route.request().url()).origin === origin) await route.continue();
      else {
        outbound.push("Unexpected outbound");
        await route.abort();
      }
    });
    context.on("page", (page) => page.on("pageerror", (error) => pageErrors.push(error.message)));
    await context.addInitScript(() => {
      const create = URL.createObjectURL.bind(URL),
        revoke = URL.revokeObjectURL.bind(URL);
      window.probeUrls = { created: 0, revoked: 0 };
      URL.createObjectURL = (blob) => {
        window.probeUrls.created++;
        return create(blob);
      };
      URL.revokeObjectURL = (url) => {
        window.probeUrls.revoked++;
        return revoke(url);
      };
    });
    return context;
  }
  const main = await context(),
    page = await main.newPage();
  const button = (name) => page.getByRole("button", { name, exact: true });
  const choose = (p) => p.getByRole("button", { name: "文本配音", exact: true }).click();
  async function fill(p) {
    for (const [label, value] of [
      ["配音模型", "tts-quality"],
      ["声音标识", "voice-demo"],
      ["声音版本", "1"],
      ["参数标识", "profile-demo"],
      ["参数版本", "2"],
      ["配音文本", text],
    ])
      await p.getByLabel(label, { exact: true }).fill(value);
  }
  async function reloadTts() {
    const before = requests;
    await page.reload();
    await choose(page);
    await expect(page.getByLabel("配音模型", { exact: true })).toBeVisible();
    assert.equal(requests, before);
  }
  const raw = () => page.evaluate((name) => sessionStorage.getItem(name), key);
  await page.goto(origin);
  await expect(page.getByLabel("音频文件")).toBeVisible();
  assert.equal(requests, 0);
  await choose(page);
  await expect(page.getByLabel("配音模型")).toHaveValue("");
  await fill(page);
  await page.getByLabel("配音文本").focus();
  await page.keyboard.press("Tab");
  await expect(button("创建配音任务")).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(page.getByRole("alert")).toBeVisible();
  const original = JSON.parse(await raw());
  assert.equal(original.version, 6);
  assert.equal(original.assetHintId, "asset-text");
  assert(!(await raw()).includes(text));
  await reloadTts();
  await expect(page.getByLabel("声音版本")).toHaveValue("1");
  await expect(page.getByLabel("配音文本")).toBeDisabled();
  await button("读取原状态").click();
  await expect(button("继续创建原任务")).toBeVisible();
  assert.equal(uploads.length, 1);
  assert.equal(creates.length, 0);
  await button("继续创建原任务").click();
  await expect(button("按原键重放")).toBeEnabled();
  await reloadTts();
  await button("按原键重放").click();
  await expect(page.getByText("job-demo", { exact: true })).toBeVisible();
  assert.equal(creates.length, 2);
  assert.deepEqual(creates[0], creates[1]);
  assert.equal(creates[0].key, original.createKey);
  phase = "succeeded";
  await button("连接任务事件").click();
  await expect(page.getByTestId("file-job-status")).toHaveText("succeeded");
  await expect(page.getByTestId("file-event-cursor")).toHaveText("9007199254740993");
  await button("试听音频").click();
  await expect
    .poll(() => page.locator("audio").evaluate((audio) => audio.readyState))
    .toBeGreaterThanOrEqual(2);
  assert.equal(await page.locator("audio").evaluate((audio) => audio.duration), 1);
  await expect
    .poll(() => page.locator("audio").evaluate((audio) => audio.currentTime))
    .toBeGreaterThan(0);
  await button("停止试听").click();
  assert.equal(await page.locator("audio").getAttribute("src"), null);
  const downloadEvent = page.waitForEvent("download");
  await button("下载 WAV").click();
  const download = await downloadEvent;
  assert.equal(download.suggestedFilename(), "speech.wav");
  assert.deepEqual(await readFile(await download.path()), audio);
  await expect
    .poll(() => page.evaluate(() => window.probeUrls))
    .toEqual({ created: 2, revoked: 2 });
  // Late content after explicit stop must not produce a URL, play, or a download.
  contentDelay = 100;
  const content = page.waitForRequest((request) =>
    request.url().endsWith("/artifact-audio/content"),
  );
  await button("试听音频").click();
  await content;
  await button("停止试听").click();
  await expect(button("试听音频")).toBeEnabled();
  assert.deepEqual(await page.evaluate(() => window.probeUrls), { created: 2, revoked: 2 });
  contentDelay = 0;
  // Switching unmounts the audio owner while preserving both recovery slots.
  await button("试听音频").click();
  await expect
    .poll(() => page.locator("audio").evaluate((audio) => audio.readyState))
    .toBeGreaterThanOrEqual(2);
  const saved = await raw();
  await button("文件转写").click();
  await expect(page.locator("audio")).toHaveCount(0);
  assert.deepEqual(await page.evaluate(() => window.probeUrls), { created: 3, revoked: 3 });
  assert.equal(await raw(), saved);
  await choose(page);
  assert.equal(await raw(), saved);
  await button("读取原状态").click();
  await expect(page.getByText("job-demo", { exact: true })).toBeVisible();
  await button("连接任务事件").click();
  await expect(page.getByTestId("file-event-cursor")).toHaveText("9007199254740994");
  assert.equal(cursors[1], "9007199254740993");
  await page.setViewportSize({ width: 360, height: 800 });
  assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
  await page.setViewportSize({ width: 1280, height: 900 });
  if (values.screenshot) {
    await mkdir(dirname(resolve(values.screenshot)), { recursive: true });
    await page.screenshot({ path: resolve(values.screenshot), fullPage: true });
  }
  await button("准备新任务").click();
  await expect(button("结束本页任务并新建")).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(page.getByLabel("配音模型")).toHaveValue("");
  await expect(page.getByLabel("配音模型")).toBeFocused();
  assert.equal(await raw(), null);
  // Missing hint: fresh monitored context, original UTF-8 text required after reload.
  mode = "nohint";
  phase = "queued";
  uploadsInMode = 0;
  const recovery = await context(),
    resume = await recovery.newPage();
  await resume.goto(origin);
  await choose(resume);
  await fill(resume);
  await resume.getByRole("button", { name: "创建配音任务", exact: true }).click();
  await expect(resume.getByRole("alert")).toBeVisible();
  const pending = await resume.evaluate((name) => JSON.parse(sessionStorage.getItem(name)), key);
  assert.equal(pending.version, 4);
  const before = requests;
  await resume.reload();
  await choose(resume);
  assert.equal(requests, before);
  await resume.getByLabel("配音文本").fill("wrong text");
  await resume.getByRole("button", { name: "按原文本恢复上传" }).click();
  await expect(resume.getByText("输入与原素材不一致，请重新提供原文件或原文本。")).toBeVisible();
  assert.equal(uploads.length, 2);
  await resume.getByLabel("配音文本").fill(text);
  await resume.getByRole("button", { name: "按原文本恢复上传" }).click();
  await expect(resume.getByText("job-demo", { exact: true })).toBeVisible();
  assert.equal(uploads[1].key, uploads[2].key);
  assert.equal(uploads[2].key, pending.uploadKey);
  assert.equal(creates[2].key, pending.createKey);
  // Corrupt/other-kind record must not create a replacement. Every context is monitored.
  const blocked = await context();
  await blocked.addInitScript(
    ({ key }) => {
      if (location.protocol === "http:") sessionStorage.setItem(key, "{");
    },
    { key },
  );
  const blockedPage = await blocked.newPage();
  const beforeBlocked = requests;
  await blockedPage.goto(origin);
  await choose(blockedPage);
  await expect(blockedPage.getByRole("alert")).toContainText("恢复信息不可用");
  assert.equal(requests, beforeBlocked);
  const production = (await readdir(resolve(workbench, "dist/assets"))).filter((name) =>
    name.endsWith(".js"),
  );
  const bundle = (
    await Promise.all(
      production.map((name) => readFile(resolve(workbench, "dist/assets", name), "utf8")),
    )
  ).join("\n");
  for (const fake of [
    "Synthetic development scenario",
    "createTtsDemoTransport",
    "故障场景",
    "Development transport refuses",
  ])
    assert(!bundle.includes(fake));
  assert.deepEqual(outbound, []);
  assert.deepEqual(pageErrors, []);
  assert.deepEqual(errors, []);
  const report = {
    checked_at: new Date().toISOString(),
    chromium: browser.version(),
    production_ui: true,
    synthetic_loopback: true,
    monitored_contexts: contexts.length,
    uploads: uploads.length,
    creates: creates.length,
    input_utf8_bytes: payload.length,
    audio_bytes: audio.length,
    audio_duration_seconds: 1,
    original_keys_and_versions: true,
    refresh_read_only: true,
    hint_and_original_input_recovery: true,
    native_wav_preview_and_download: true,
    late_content_discarded: true,
    switch_revokes_url: true,
    keyboard_and_360px: true,
    fake_excluded: true,
    handler_errors: errors,
    outbound_requests: outbound.length,
    page_errors: pageErrors.length,
    real_backend_or_model_qualification: false,
  };
  if (values.output) {
    await mkdir(dirname(resolve(values.output)), { recursive: true });
    await writeFile(resolve(values.output), JSON.stringify(report, null, 2) + "\n");
  }
  console.log(JSON.stringify(report, null, 2));
} finally {
  await browser?.close();
  await new Promise((done) => (server?.httpServer ? server.httpServer.close(done) : done()));
}
