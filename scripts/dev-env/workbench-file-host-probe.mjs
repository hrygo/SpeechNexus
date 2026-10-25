/** Actual production Workbench + synthetic loopback HTTP; no backend, device, or model qualification. */
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
const key = "speechnexus.workbench.asr-task.v1";
const payload = Buffer.from(JSON.stringify({ text: "Synthetic browser probe transcript." }));
const digest = (value) => `sha256:${createHash("sha256").update(value).digest("hex")}`;
const audio = Buffer.alloc(556);
audio.write("RIFF");
audio.writeUInt32LE(548, 4);
audio.write("WAVEfmt ", 8);
audio.writeUInt32LE(16, 16);
audio.writeUInt16LE(1, 20);
audio.writeUInt16LE(1, 22);
audio.writeUInt32LE(24000, 24);
audio.writeUInt32LE(48000, 28);
audio.writeUInt16LE(2, 32);
audio.writeUInt16LE(16, 34);
audio.write("data", 36);
audio.writeUInt32LE(512, 40);
const artifact = parse("Artifact", {
  ...example("artifact"),
  bytes: payload.length,
  sha256: digest(payload),
});
let mode = "hint",
  assetReads = 0,
  phase = "queued",
  requests = 0,
  uploadNumber = 0;
let loseCreate = true,
  loseCancel = true;
const smallerAudio = Buffer.from(audio.subarray(0, 300));
smallerAudio.writeUInt32LE(292, 4);
smallerAudio.writeUInt32LE(256, 40);
let uploadAudio = audio;
const uploads = [],
  creates = [],
  cancels = [],
  cursors = [],
  handlerErrors = [];
const asset = (id, status = "ready") =>
  parse("Asset", {
    ...example("ready-audio"),
    id,
    status,
    bytes: uploadAudio.length,
    sha256: digest(uploadAudio),
  });
const job = (id) => {
  const value = example(phase === "succeeded" ? "succeeded-job" : "queued-job");
  value.id = id;
  value.status = phase;
  if (phase === "succeeded")
    value.artifacts = value.artifacts.map((a) => ({
      ...a,
      bytes: artifact.bytes,
      sha256: artifact.sha256,
    }));
  return parse("Job", value);
};
const json = (res, value, status = 200, headers = {}) => {
  res.writeHead(status, {
    "Content-Type": "application/json",
    "Cache-Control": "no-store",
    ...headers,
  });
  res.end(JSON.stringify(value));
};
const error = (code) =>
  parse("ErrorResponse", {
    error: {
      code,
      type: "api_error",
      message: "synthetic-private-error-body",
      retryable: false,
      request_id: "req-probe",
      param: null,
    },
  });
const middleware = (vite) => {
  vite.middlewares.use(async (req, res, next) => {
    const path = new URL(req.url, "http://127.0.0.1").pathname;
    if (!path.startsWith("/v1/")) return next();
    requests++;
    try {
      if (path === "/v1/nexus/assets" && req.method === "POST") {
        const chunks = [];
        for await (const chunk of req) chunks.push(chunk);
        const body = Buffer.concat(chunks);
        const form = await new Request("http://127.0.0.1/upload", {
          method: "POST",
          headers: { "Content-Type": req.headers["content-type"] },
          body,
        }).formData();
        assert.deepEqual([...form.keys()].sort(), ["file", "purpose"]);
        assert.equal(form.get("purpose"), "asr_input");
        const file = form.get("file");
        assert.equal(file.name, "synthetic-probe.wav");
        assert.equal(file.type, "audio/wav");
        uploadAudio = mode === "rejected" && uploadNumber > 0 ? smallerAudio : audio;
        assert.deepEqual(Buffer.from(await file.arrayBuffer()), uploadAudio);
        uploads.push({ key: req.headers["idempotency-key"], purpose: form.get("purpose") });
        uploadNumber++;
        if (mode === "hint" && uploadNumber === 1)
          return json(res, error("asset_upload_in_progress"), 409, {
            Location: "/v1/nexus/assets/asset-demo",
            "X-Should-Retry": "false",
          });
        if (mode === "rejected" && uploadNumber === 1)
          return json(res, error("asset_too_large"), 413, { "X-Should-Retry": "false" });
        if (mode === "nohint" && uploadNumber === 1)
          return json(res, error("asset_upload_unknown"), 503, { "X-Should-Retry": "false" });
        return json(res, asset(mode === "hint" ? "asset-next" : "asset-demo"), 201);
      }
      if (/^\/v1\/nexus\/assets\/(asset-demo|asset-next)$/.test(path) && req.method === "GET") {
        assetReads++;
        if (mode === "hint" && assetReads === 1)
          return json(res, error("asset_metadata_unknown"), 503);
        return json(
          res,
          asset(path.split("/").at(-1), mode === "hint" && assetReads === 2 ? "staging" : "ready"),
        );
      }
      if (path === "/v1/nexus/jobs" && req.method === "POST") {
        const chunks = [];
        for await (const c of req) chunks.push(c);
        const body = parse("JobCreateRequest", JSON.parse(Buffer.concat(chunks).toString()));
        assert.equal(body.kind, "asr");
        assert.equal(body.model, "asr-quality");
        assert.equal(body.workspace_id, "ws-default");
        assert.deepEqual(body.output, { transcript_format: "json", save_artifacts: true });
        assert.ok(["asset-demo", "asset-next"].includes(body.input.audio_asset_id));
        creates.push({ key: req.headers["idempotency-key"], body });
        if (loseCreate) {
          loseCreate = false;
          res.writeHead(200, { "Content-Type": "application/json" });
          return res.end("{");
        }
        return json(
          res,
          job(body.input.audio_asset_id === "asset-next" ? "job-next" : "job-demo"),
          201,
        );
      }
      const match = /^\/v1\/nexus\/jobs\/(job-demo|job-next)(\/events|\/cancellations)?$/.exec(
        path,
      );
      if (match && req.method === "GET" && !match[2]) return json(res, job(match[1]));
      if (match?.[2] === "/events" && req.method === "GET") {
        const after = req.headers["last-event-id"] ?? null;
        cursors.push(after);
        const event = parse("JobEvent", {
          ...example("job.queued"),
          job_id: match[1],
          id: after ? "9007199254740994" : "9007199254740993",
        });
        res.writeHead(200, { "Content-Type": "text/event-stream", "Cache-Control": "no-store" });
        return res.end(`id: ${event.id}\nevent: ${event.type}\ndata: ${JSON.stringify(event)}\n\n`);
      }
      if (match?.[2] === "/cancellations" && req.method === "POST") {
        cancels.push({ id: match[1], key: req.headers["idempotency-key"] });
        phase = "cancelling";
        if (loseCancel) {
          loseCancel = false;
          res.writeHead(200, { "Content-Type": "application/json" });
          return res.end("{");
        }
        return json(res, parse("JobCancellation", { ...example("cancel-job"), job_id: match[1] }));
      }
      if (path === "/v1/nexus/artifacts/artifact-demo" && req.method === "GET")
        return json(res, artifact);
      if (path === "/v1/nexus/artifacts/artifact-demo/content" && req.method === "GET") {
        res.writeHead(200, {
          "Content-Type": "application/json",
          "Content-Length": payload.length,
        });
        return res.end(payload);
      }
      throw new Error("Unexpected synthetic route");
    } catch (cause) {
      handlerErrors.push(cause instanceof Error ? cause.message : "handler error");
      return json(res, error("synthetic_handler_error"), 500);
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
    plugins: [{ name: "team-c-file-host-probe", configurePreviewServer: middleware }],
    preview: { host: "127.0.0.1", port: 0, strictPort: false },
  });
  const address = server.httpServer.address();
  assert(address && typeof address !== "string");
  const origin = `http://127.0.0.1:${address.port}`;
  browser = await chromium.launch({ headless: true });
  const outbound = [],
    pageErrors = [];
  const context = await browser.newContext({ reducedMotion: "reduce", acceptDownloads: true });
  await context.route("**/*", async (route) => {
    if (new URL(route.request().url()).origin === origin) await route.continue();
    else {
      outbound.push("outbound");
      await route.abort();
    }
  });
  context.on("page", (page) => page.on("pageerror", () => pageErrors.push("pageerror")));
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
  const page = await context.newPage();
  const button = (name) => page.getByRole("button", { name, exact: true });
  const raw = () => page.evaluate((name) => sessionStorage.getItem(name), key);
  const record = async () => JSON.parse(await raw());
  const reload = async () => {
    const before = requests;
    await page.reload();
    await expect(page.getByLabel("音频文件", { exact: true })).toBeVisible();
    assert.equal(requests, before, "reload must not automatically access an API");
  };
  const selectFile = async () => {
    await page
      .getByLabel("音频文件", { exact: true })
      .setInputFiles({ name: "synthetic-probe.wav", mimeType: "audio/wav", buffer: audio });
  };
  await page.goto(`${origin}/?dev-scenario=file-task`);
  await expect(button("上传并转写")).toBeVisible();
  assert.equal(requests, 0);
  await expect(page.getByText("故障场景", { exact: true })).toHaveCount(0);
  await page.getByLabel("转写模型", { exact: true }).focus();
  await page.keyboard.press("Tab");
  await expect(page.getByLabel("音频文件", { exact: true })).toBeFocused();
  await page.getByLabel("转写模型", { exact: true }).fill("asr-quality");
  await selectFile();
  await button("上传并转写").click();
  await expect(page.getByRole("alert")).toContainText("请求结果尚未确认");
  assert.equal((await record()).version, 5);
  assert.equal(uploads.length, 1);
  assert.equal(creates.length, 0);
  await reload();
  const hint = await record();
  await expect(page.getByLabel("转写模型", { exact: true })).toBeDisabled();
  await button("读取原状态").click();
  await expect(
    page.getByRole("region", { name: "任务与原始结果" }).getByRole("status"),
  ).toContainText("503");
  assert.equal(creates.length, 0);
  await button("读取原状态").click();
  await expect(page.getByText("staging", { exact: true })).toBeVisible();
  await button("读取原状态").click();
  await expect(button("继续创建原任务")).toBeVisible();
  assert.equal(creates.length, 0);
  assert.equal(uploads.length, 1);
  await button("继续创建原任务").click();
  await expect(button("按原键重放")).toBeEnabled();
  assert.equal((await record()).pending, "create");
  await reload();
  await button("按原键重放").click();
  await expect(page.getByTestId("file-job-status")).toHaveText("queued");
  assert.equal(creates.length, 2);
  assert.deepEqual(creates[0], creates[1]);
  assert.equal(creates[0].key, hint.createKey);
  await button("连接任务事件").click();
  await expect(page.getByTestId("file-event-cursor")).toHaveText("9007199254740993");
  await reload();
  await button("读取原状态").click();
  await expect(page.getByTestId("file-job-status")).toHaveText("queued");
  await button("连接任务事件").click();
  await expect(page.getByTestId("file-event-cursor")).toHaveText("9007199254740994");
  assert.deepEqual(cursors, [null, "9007199254740993"]);
  phase = "succeeded";
  await button("读取原状态").click();
  await expect(page.getByTestId("file-job-status")).toHaveText("succeeded");
  const downloadReady = page.waitForEvent("download");
  await button("下载原始制品 artifact-demo").click();
  const downloaded = await downloadReady;
  assert.equal(downloaded.suggestedFilename(), "transcript.json");
  assert.deepEqual(await readFile(await downloaded.path()), payload);
  await expect
    .poll(() => page.evaluate(() => window.probeUrls))
    .toEqual({ created: 1, revoked: 1 });
  if (values.screenshot) {
    await mkdir(dirname(resolve(values.screenshot)), { recursive: true });
    await page.screenshot({ path: resolve(values.screenshot), fullPage: true });
  }
  await page.setViewportSize({ width: 360, height: 800 });
  assert.equal(
    await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth),
    false,
  );
  const preserved = await raw();
  await button("准备新任务").click();
  await expect(button("结束本页任务并新建")).toBeFocused();
  await page.evaluate((name) => sessionStorage.setItem(name, "foreign-owner"), key);
  await page.keyboard.press("Enter");
  await expect(page.getByRole("alert")).toContainText("本地任务信息已变化或无法清理");
  assert.equal(await raw(), "foreign-owner");
  await page.evaluate(({ name, value }) => sessionStorage.setItem(name, value), {
    name: key,
    value: preserved,
  });
  await reload();
  await button("读取原状态").click();
  await expect(page.getByTestId("file-job-status")).toHaveText("succeeded");
  await button("准备新任务").click();
  await button("结束本页任务并新建").click();
  assert.equal(await raw(), null);
  await expect(page.getByLabel("转写模型", { exact: true })).toBeFocused();
  phase = "queued";
  await page.getByLabel("转写模型", { exact: true }).fill("asr-quality");
  await selectFile();
  await button("上传并转写").click();
  await expect(page.getByTestId("file-job-status")).toHaveText("queued");
  assert.notEqual(creates[2].key, creates[0].key);
  assert.notEqual(uploads[1].key, uploads[0].key);
  await button("准备取消").click();
  assert.equal(cancels.length, 0);
  await expect(button("确认取消 job-next")).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(button("按原键重放")).toBeEnabled();
  const cancelIntent = await record();
  assert.equal(cancelIntent.pending, "cancel");
  await reload();
  await button("读取原状态").click();
  await expect(page.getByTestId("file-job-status")).toHaveText("cancelling");
  assert.equal(cancels.length, 1);
  assert.equal((await record()).pending, "cancel");
  await button("按原键重放").click();
  await expect(page.getByText("取消响应：accepted=true / pending", { exact: true })).toBeVisible();
  assert.equal(cancels.length, 2);
  assert.deepEqual(cancels[0], cancels[1]);
  assert.equal(cancels[0].key, cancelIntent.cancelKey);
  assert.equal(cancels[0].id, "job-next");
  assert.doesNotMatch(await raw(), /synthetic-probe|Synthetic browser|RIFF/);
  await context.close();
  // Fresh tab: no Location requires reselecting the original file and original upload key.
  mode = "nohint";
  uploadNumber = 0;
  phase = "queued";
  const resumeContext = await browser.newContext();
  await resumeContext.route("**/*", async (route) => {
    if (new URL(route.request().url()).origin === origin) await route.continue();
    else {
      outbound.push("outbound");
      await route.abort();
    }
  });
  const resume = await resumeContext.newPage();
  resume.on("pageerror", () => pageErrors.push("pageerror"));
  await resume.goto(origin);
  await resume.getByLabel("转写模型", { exact: true }).fill("asr-quality");
  const original = { name: "synthetic-probe.wav", mimeType: "audio/wav", buffer: audio };
  await resume.getByLabel("音频文件", { exact: true }).setInputFiles(original);
  await resume.getByRole("button", { name: "上传并转写", exact: true }).click();
  await expect(resume.getByRole("alert")).toContainText("请求结果尚未确认");
  const beforeResume = requests;
  await resume.reload();
  await expect(resume.getByRole("button", { name: "按原输入恢复上传", exact: true })).toBeVisible();
  assert.equal(requests, beforeResume);
  await resume.getByLabel("音频文件", { exact: true }).setInputFiles(original);
  await resume.getByRole("button", { name: "按原输入恢复上传", exact: true }).click();
  await expect(resume.getByTestId("file-job-status")).toHaveText("queued");
  assert.equal(uploads.at(-1).key, uploads.at(-2).key);
  await resumeContext.close();
  // P2 regression: a fresh, explicitly rejected upload can be corrected through the real UI.
  mode = "rejected";
  uploadNumber = 0;
  phase = "queued";
  const rejectionContext = await browser.newContext();
  await rejectionContext.route("**/*", async (route) => {
    if (new URL(route.request().url()).origin === origin) await route.continue();
    else {
      outbound.push("outbound");
      await route.abort();
    }
  });
  const correction = await rejectionContext.newPage();
  correction.on("pageerror", () => pageErrors.push("pageerror"));
  await correction.goto(origin);
  await correction.getByLabel("转写模型", { exact: true }).fill("asr-quality");
  await correction.getByLabel("音频文件", { exact: true }).setInputFiles(original);
  const createsBeforeRejection = creates.length;
  await correction.getByRole("button", { name: "上传并转写", exact: true }).click();
  await expect(correction.getByRole("button", { name: "准备新任务", exact: true })).toBeVisible();
  assert.equal(creates.length, createsBeforeRejection);
  const rejectedIntent = JSON.parse(
    await correction.evaluate((name) => sessionStorage.getItem(name), key),
  );
  await correction.getByRole("button", { name: "准备新任务", exact: true }).click();
  await expect(
    correction.getByText("原上传已明确拒绝。结束本页输入并重新选择？不会删除服务端数据。", {
      exact: true,
    }),
  ).toBeVisible();
  await correction.getByRole("button", { name: "结束本页任务并新建", exact: true }).click();
  await expect(correction.getByLabel("转写模型", { exact: true })).toBeFocused();
  await correction.getByLabel("转写模型", { exact: true }).fill("asr-quality");
  await correction
    .getByLabel("音频文件", { exact: true })
    .setInputFiles({ ...original, buffer: smallerAudio });
  await correction.getByRole("button", { name: "上传并转写", exact: true }).click();
  await expect(correction.getByTestId("file-job-status")).toHaveText("queued");
  assert.equal(creates.length, createsBeforeRejection + 1);
  assert.notEqual(uploads.at(-1).key, rejectedIntent.uploadKey);
  await rejectionContext.close();
  const blockedContext = await browser.newContext();
  await blockedContext.addInitScript((name) => {
    const original = Storage.prototype.getItem;
    Storage.prototype.getItem = function (key) {
      if (key === name) throw new Error("synthetic private storage failure");
      return original.call(this, key);
    };
  }, key);
  const blocked = await blockedContext.newPage();
  const beforeBlocked = requests;
  await blocked.goto(origin);
  await expect(blocked.getByRole("alert")).toContainText("恢复信息不可用");
  assert.equal(requests, beforeBlocked);
  await expect(blocked.getByRole("button", { name: "上传并转写", exact: true })).toHaveCount(0);
  await blockedContext.close();
  const assets = resolve(workbench, "dist/assets");
  const scripts = await readdir(assets);
  for (const file of scripts.filter((f) => f.endsWith(".js")))
    assert.doesNotMatch(
      await readFile(resolve(assets, file), "utf8"),
      /故障场景|合成演练执行数|synthetic-silence\.wav/,
    );
  assert.deepEqual(handlerErrors, []);
  assert.deepEqual(pageErrors, []);
  assert.deepEqual(outbound, []);
  const report = {
    checkedAt: new Date().toISOString(),
    productionUI: true,
    syntheticHTTP: true,
    browser: browser.version(),
    assetHintReload: true,
    unknownCreateOriginalKey: true,
    cursorReload: true,
    downloadBytesVerified: payload.length,
    blobUrlsRevoked: true,
    ownerChangeBlocked: true,
    terminalExplicitNewIntent: true,
    cancellationOriginalTargetKey: true,
    noLocationOriginalInputKey: true,
    storageUnavailableBlocked: true,
    firstRejectedUploadCorrected: true,
    keyboardFocus: true,
    width360NoOverflow: true,
    fakeExcluded: true,
    uploadRequests: uploads.length,
    createRequests: creates.length,
    cancellationRequests: cancels.length,
    pageErrors: 0,
    outbound: 0,
    handlerErrors: 0,
    backendDeviceModelQualified: false,
  };
  if (values.output) {
    await mkdir(dirname(resolve(values.output)), { recursive: true });
    await writeFile(resolve(values.output), `${JSON.stringify(report, null, 2)}\n`);
  }
  console.log(JSON.stringify(report));
} finally {
  await browser?.close();
  await server?.close();
}
