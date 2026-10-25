/** Actual Chromium + built production consumer classes + loopback synthetic HTTP; no backend/device. */
import { chromium } from "@playwright/test";
import assert from "node:assert/strict";
import { mkdir, mkdtemp, readFile, writeFile, rm } from "node:fs/promises";
import { createServer } from "node:http";
import { createRequire } from "node:module";
import { tmpdir } from "node:os";
import { dirname, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { parseArgs } from "node:util";
import { parse } from "../../packages/contracts/src/runtime.mjs";
const { values } = parseArgs({ options: { output: { type: "string" } } });
const root = fileURLToPath(new URL("../../", import.meta.url));
const wb = resolve(root, "web/workbench");
const require = createRequire(resolve(wb, "package.json"));
const { build } = await import(pathToFileURL(require.resolve("vite")).href);
const scratch = await mkdtemp(resolve(tmpdir(), "speechnexus-asset-consumer-"));
const fixtures = JSON.parse(
  await readFile(resolve(root, "tests/fixtures/contracts/file-workflow.json"), "utf8"),
);
const example = (id) => structuredClone(fixtures.examples.find((e) => e.id === id).value);
const key = "speechnexus:asset-reconcile-probe";
const error = (code) =>
  parse("ErrorResponse", {
    error: {
      code,
      type: "api_error",
      message: "synthetic-safe-error",
      retryable: false,
      request_id: "req-demo",
      param: null,
    },
  });
let state;
const server = createServer(async (req, res) => {
  try {
    const path = new URL(req.url, "http://127.0.0.1").pathname;
    const json = (data, status = 200, headers = {}) => {
      res.writeHead(status, {
        "Content-Type": "application/json",
        "Cache-Control": "no-store",
        ...headers,
      });
      res.end(JSON.stringify(data));
    };
    if (path === "/") {
      res.writeHead(200, { "Content-Type": "text/html" });
      res.end("<!doctype html><title>Asset recovery probe</title><main>合成素材恢复探针</main>");
      return;
    }
    if (path === "/consumer.js") {
      res.writeHead(200, { "Content-Type": "text/javascript" });
      res.end(await readFile(resolve(scratch, "consumer.js")));
      return;
    }
    const current = state;
    current.requests++;
    const asset = () =>
      parse("Asset", {
        ...example("ready-audio"),
        id: "asset-original",
        purpose: current.kind === "asr" ? "asr_input" : "text_input",
        ...(current.kind === "tts"
          ? { media_type: "text/plain", duration_ms: null, sample_rate_hz: null, channels: null }
          : {}),
        status: current.ready ? "ready" : "staging",
      });
    if (path === "/v1/nexus/assets" && req.method === "POST") {
      const bytes = [];
      for await (const part of req) bytes.push(part);
      const form = await new Request("http://127.0.0.1", {
        method: "POST",
        headers: req.headers,
        body: Buffer.concat(bytes),
        duplex: "half",
      }).formData();
      const input = form.get("file");
      assert(input instanceof File);
      assert.equal(
        await input.text(),
        current.kind === "asr" ? "synthetic audio" : "synthetic original text",
      );
      assert.equal(form.get("purpose"), current.kind === "asr" ? "asr_input" : "text_input");
      assert.equal(form.has("ttl_seconds"), false);
      assert.equal(form.has("workspace_id"), false);
      current.posts.push(req.headers["idempotency-key"]);
      if (current.acceptUpload) return json(asset(), 201);
      return json(
        error(current.kind === "asr" ? "asset_upload_in_progress" : "asset_upload_unknown"),
        current.kind === "asr" ? 409 : 503,
        current.location
          ? { Location: current.location, "X-Should-Retry": "false" }
          : { "X-Should-Retry": "false" },
      );
    }
    if (path === "/v1/nexus/assets/asset-original" && req.method === "GET") {
      current.gets++;
      return current.failRead ? json(error("asset_service_unavailable"), 503) : json(asset());
    }
    if (path === "/v1/nexus/jobs" && req.method === "POST") {
      const bytes = [];
      for await (const part of req) bytes.push(part);
      const command = parse("JobCreateRequest", JSON.parse(Buffer.concat(bytes).toString("utf8")));
      assert.equal(command.kind, current.kind);
      assert.equal(
        command.input[current.kind === "asr" ? "audio_asset_id" : "text_asset_id"],
        "asset-original",
      );
      if (current.kind === "tts") {
        assert.deepEqual(command.voice, { id: "voice-demo", revision: "1" });
        assert.deepEqual(command.profile, { id: "profile-demo", revision: "2" });
      }
      current.jobs.push(req.headers["idempotency-key"]);
      return json(parse("Job", { ...example("queued-job"), kind: current.kind }), 201);
    }
    current.unexpected++;
    return json(error("invalid_request"), 400);
  } catch {
    state.handlerErrors++;
    res.destroy();
  }
});
let browser;
const errors = [],
  outbound = [];
const results = [];
try {
  const entry = resolve(scratch, "entry.ts");
  await writeFile(
    entry,
    [
      `export {FileTaskSession} from ${JSON.stringify(resolve(wb, "src/file-workflow/file-task-session.ts"))};`,
      `export {SpeechFileClient} from ${JSON.stringify(resolve(wb, "src/file-workflow/speech-file-client.ts"))};`,
      `export {SessionTaskJournal} from ${JSON.stringify(resolve(wb, "src/file-workflow/task-journal.ts"))};`,
    ].join("\n"),
  );
  await build({
    root: wb,
    configFile: false,
    logLevel: "error",
    build: {
      outDir: scratch,
      emptyOutDir: false,
      lib: { entry, formats: ["es"], fileName: () => "consumer.js" },
    },
  });
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", resolve);
  });
  const address = server.address();
  assert(address && typeof address !== "string");
  const origin = `http://127.0.0.1:${address.port}`;
  browser = await chromium.launch({ headless: true });
  for (const kind of ["asr", "tts"]) {
    for (const scenario of [
      "hint",
      "no-location",
      "external-location",
      "storage-failed",
      "owner-changed",
    ]) {
      state = {
        kind,
        location:
          scenario === "no-location"
            ? null
            : scenario === "external-location"
              ? "https://external.invalid/v1/nexus/assets/asset-original"
              : "/v1/nexus/assets/asset-original",
        posts: [],
        jobs: [],
        gets: 0,
        requests: 0,
        unexpected: 0,
        handlerErrors: 0,
        ready: false,
        acceptUpload: false,
        failRead: false,
      };
      const context = await browser.newContext();
      await context.route("**/*", async (route) => {
        if (new URL(route.request().url()).origin === origin) await route.continue();
        else {
          outbound.push("outbound");
          await route.abort();
        }
      });
      const page = await context.newPage();
      page.on("pageerror", () => errors.push("pageerror"));
      const attach = async () => {
        const before = state.requests;
        const view = await page.evaluate(async (key) => {
          const { FileTaskSession, SpeechFileClient, SessionTaskJournal } =
            await import("/consumer.js");
          window.task = new FileTaskSession(
            new SpeechFileClient(location.origin),
            new SessionTaskJournal(sessionStorage, key),
          );
          return window.task.getSnapshot();
        }, key);
        assert.equal(state.requests, before);
        return view;
      };
      const row = () => page.evaluate((key) => JSON.parse(sessionStorage.getItem(key)), key);
      await page.goto(origin);
      await attach();
      if (scenario === "storage-failed")
        await page.evaluate(() => {
          const save = Storage.prototype.setItem;
          Storage.prototype.setItem = function (key, value) {
            if (JSON.parse(value).version >= 5) throw new Error("synthetic-denied");
            return save.call(this, key, value);
          };
        });
      const initial = await page.evaluate(async (kind) => {
        if (kind === "asr")
          await window.task.start(
            new File(["synthetic audio"], "original.wav", { type: "audio/wav" }),
            "asr-quality",
          );
        else
          await window.task.startText("synthetic original text", {
            model: "tts-quality",
            voiceId: "voice-demo",
            voiceRevision: "1",
            profileId: "profile-demo",
            profileRevision: "2",
          });
        return window.task.getSnapshot();
      }, kind);
      assert.equal(initial.unknown, true);
      assert.equal(state.posts.length, 1);
      assert.equal(state.jobs.length, 0);
      assert.equal(state.gets, 0);
      const original = await row();
      if (scenario === "hint" || scenario === "owner-changed") {
        assert.equal(original.version, kind === "asr" ? 5 : 6);
        assert.equal(original.assetHintId, "asset-original");
        assert.equal(initial.canReplay, false);
        assert.equal(initial.requiresInput, false);
        await page.reload();
        const restored = await attach();
        assert.equal(restored.assetRecoveryId, "asset-original");
        assert.equal(restored.asset, undefined);
        if (scenario === "hint") {
          state.failRead = true;
          await page.evaluate(() => window.task.restore());
          assert.equal((await page.evaluate(() => window.task.getSnapshot())).unknown, true);
          assert.equal(state.jobs.length, 0);
          state.failRead = false;
          await page.evaluate(() => window.task.restore());
          assert.equal(
            (await page.evaluate(() => window.task.getSnapshot())).asset.status,
            "staging",
          );
          state.ready = true;
          await page.evaluate(() => window.task.refresh());
          assert.equal(state.jobs.length, 0);
          await page.evaluate(() => window.task.continueAfterValidation());
          assert.deepEqual(state.jobs, [original.createKey]);
          assert.equal(state.posts.length, 1);
          assert.equal((await row()).version, kind === "asr" ? 1 : 2);
        } else {
          const other = { ...original, assetHintId: "other" };
          await page.evaluate(
            ({ key, other }) => sessionStorage.setItem(key, JSON.stringify(other)),
            { key, other },
          );
          state.ready = true;
          await page.evaluate(() => window.task.restore());
          await page.evaluate(() => window.task.continueAfterValidation());
          assert.deepEqual(await row(), other);
          assert.equal(state.jobs.length, 0);
          assert.equal(state.posts.length, 1);
        }
      } else if (scenario === "no-location") {
        assert.equal(original.version, kind === "asr" ? 3 : 4);
        await page.reload();
        const restored = await attach();
        assert.equal(restored.requiresInput, true);
        state.acceptUpload = true;
        state.ready = true;
        await page.evaluate(async (kind) => {
          if (kind === "asr")
            await window.task.resumeFile(
              new File(["synthetic audio"], "original.wav", { type: "audio/wav" }),
            );
          else await window.task.resumeText("synthetic original text");
        }, kind);
        assert.deepEqual(state.posts, [original.uploadKey, original.uploadKey]);
        assert.deepEqual(state.jobs, [original.createKey]);
      } else {
        assert.equal(original.version, kind === "asr" ? 3 : 4);
        assert.equal(state.jobs.length, 0);
        assert.equal(state.gets, 0);
        if (scenario === "storage-failed") assert.equal(initial.canReplay, false);
      }
      assert.equal(state.handlerErrors, 0);
      assert.equal(state.unexpected, 0);
      const stored = await page.evaluate((key) => sessionStorage.getItem(key), key);
      assert(
        !stored.includes("synthetic original text") &&
          !stored.includes("synthetic audio") &&
          !stored.includes("original.wav"),
      );
      results.push({
        kind,
        scenario,
        uploads: state.posts.length,
        metadataReads: state.gets,
        jobs: state.jobs.length,
      });
      await context.close();
    }
  }
  assert.deepEqual(errors, []);
  assert.deepEqual(outbound, []);
  const report = {
    checkedAt: new Date().toISOString(),
    browser: browser.version(),
    kind: "synthetic_loopback_asset_recovery",
    builtConsumerClasses: true,
    productionUI: false,
    scenarios: results,
    noAutomaticRestoreRequests: true,
    noOperationRequests: true,
    originalKeys: true,
    metadataOnly: true,
    pageErrors: errors.length,
    outboundRequests: outbound.length,
  };
  if (values.output) {
    await mkdir(dirname(resolve(values.output)), { recursive: true });
    await writeFile(resolve(values.output), JSON.stringify(report, null, 2) + "\n");
  }
  console.log(JSON.stringify(report));
} finally {
  if (browser) await browser.close();
  if (server.listening)
    await new Promise((resolve, reject) =>
      server.close((error) => (error ? reject(error) : resolve())),
    );
  await rm(scratch, { recursive: true, force: true });
}
