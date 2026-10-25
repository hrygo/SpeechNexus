/** Real Chromium reload + real loopback HTTP; all API responses are synthetic fixtures. */
import { chromium } from "@playwright/test";
import assert from "node:assert/strict";
import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { createServer } from "node:http";
import { createRequire } from "node:module";
import { tmpdir } from "node:os";
import { dirname, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { parseArgs } from "node:util";

const { values } = parseArgs({
  options: {
    output: { type: "string" },
    kind: { type: "string", default: "asr" },
    "upload-unknown": { type: "boolean", default: false },
  },
});
assert(["asr", "tts"].includes(values.kind));
const tts = values.kind === "tts";
const root = fileURLToPath(new URL("../../", import.meta.url));
const workbench = resolve(root, "web/workbench");
const require = createRequire(resolve(workbench, "package.json"));
const { build } = await import(pathToFileURL(require.resolve("vite")).href);
const directory = await mkdtemp(resolve(tmpdir(), "speechnexus-recovery-"));
const fixtures = JSON.parse(
  await readFile(resolve(root, "tests/fixtures/contracts/file-workflow.json"), "utf8"),
);
const sample = (id) => structuredClone(fixtures.examples.find((entry) => entry.id === id).value);
const jobSample = (id) => {
  const value = sample(id);
  if (!tts) return value;
  value.kind = "tts";
  value.effective_configuration.models = { tts: { id: "tts-quality", revision: "1" } };
  if (value.result) {
    value.artifacts = value.artifacts.map((artifact) => ({
      ...artifact,
      kind: "audio",
      media_type: "audio/wav",
    }));
    value.result = {
      kind: "tts",
      audio_artifact_id: value.artifacts[0].artifact_id,
      receipt_id: value.artifacts[0].receipt_id,
    };
  }
  return value;
};
let job = jobSample("queued-job");
const creates = [];
const cancels = [];
const cursors = [];
const intents = new Map();
const uploads = [];
const uploadIntents = new Map();
const errors = [];
const outbound = [];
let lostCreate = false;
let lostCancel = false;
let lostUpload = false;
const server = createServer(async (request, response) => {
  try {
    const path = new URL(request.url ?? "/", "http://127.0.0.1").pathname;
    const json = (value, status = 200) => {
      response.writeHead(status, { "content-type": "application/json" });
      response.end(JSON.stringify(value));
    };
    const lose = () => {
      response.writeHead(200, { "content-type": "application/json", "content-length": "100000" });
      response.flushHeaders();
      response.write("{");
      response.destroy();
    };
    if (path === "/") {
      response.writeHead(200, { "content-type": "text/html; charset=utf-8" });
      response.end(
        "<!doctype html><title>Synthetic recovery probe</title><main>Recovery probe</main>",
      );
    } else if (path === "/recovery.js") {
      response.writeHead(200, { "content-type": "text/javascript" });
      response.end(await readFile(resolve(directory, "recovery.js")));
    } else if (path === "/v1/nexus/assets" && request.method === "POST") {
      const chunks = [];
      for await (const chunk of request) chunks.push(chunk);
      const form = await new Request("http://127.0.0.1", {
        method: "POST",
        headers: { "content-type": request.headers["content-type"] },
        body: Buffer.concat(chunks),
      }).formData();
      const file = form.get("file");
      assert(file instanceof File);
      assert.equal(form.get("purpose"), tts ? "text_input" : "asr_input");
      const sha256 =
        "sha256:" +
        Array.from(
          new Uint8Array(await crypto.subtle.digest("SHA-256", await file.arrayBuffer())),
          (byte) => byte.toString(16).padStart(2, "0"),
        ).join("");
      const key = request.headers["idempotency-key"];
      assert.equal(typeof key, "string");
      const identity = JSON.stringify([
        form.get("purpose"),
        file.name,
        file.type,
        file.size,
        sha256,
      ]);
      uploads.push(key);
      if (uploadIntents.has(key)) assert.equal(uploadIntents.get(key), identity);
      else uploadIntents.set(key, identity);
      if (values["upload-unknown"] && !lostUpload) {
        lostUpload = true;
        lose();
        return;
      }
      const asset = sample("ready-audio");
      asset.bytes = file.size;
      asset.sha256 = sha256;
      json(
        tts
          ? {
              ...asset,
              purpose: "text_input",
              media_type: "text/plain",
              duration_ms: null,
              sample_rate_hz: null,
              channels: null,
            }
          : asset,
        201,
      );
    } else if (path === "/v1/nexus/jobs" && request.method === "POST") {
      const chunks = [];
      for await (const chunk of request) chunks.push(chunk);
      const body = Buffer.concat(chunks).toString();
      const key = request.headers["idempotency-key"];
      assert.equal(typeof key, "string");
      const payload = JSON.parse(body);
      assert.equal(payload.workspace_id, "ws-default");
      assert.equal(payload.kind, values.kind);
      assert.equal(tts ? payload.input.text_asset_id : payload.input.audio_asset_id, "asset-demo");
      if (tts) {
        assert.deepEqual(payload.voice, { id: "voice-demo", revision: "1" });
        assert.deepEqual(payload.profile, { id: "profile-demo", revision: "2" });
        assert.equal(payload.output.audio_format, "wav");
      }
      creates.push({ key, body });
      if (intents.has(key)) assert.equal(intents.get(key), body);
      else intents.set(key, body);
      if (!lostCreate) {
        lostCreate = true;
        lose();
      } else json(jobSample("queued-job"), 202);
    } else if (path === "/v1/nexus/jobs/job-demo/cancellations" && request.method === "POST") {
      for await (const chunk of request) void chunk;
      cancels.push(request.headers["idempotency-key"]);
      job = { ...job, status: "cancelling" };
      if (!lostCancel) {
        lostCancel = true;
        lose();
      } else json(sample("cancel-job"), 202);
    } else if (path === "/v1/nexus/jobs/job-demo") json(job);
    else if (path === "/v1/nexus/jobs/job-demo/events") {
      const cursor = request.headers["last-event-id"] ?? null;
      cursors.push(cursor);
      job = jobSample("succeeded-job");
      const event = {
        ...sample("job.succeeded"),
        id: "9007199254740993",
        job_id: job.id,
        data: {
          status: job.status,
          cleanup_state: job.cleanup_state,
          artifacts: job.artifacts,
          result: job.result,
        },
      };
      response.writeHead(200, { "content-type": "text/event-stream" });
      response.end(
        cursor
          ? ": current\n\n"
          : `id: ${event.id}\nevent: ${event.type}\ndata: ${JSON.stringify(event)}\n\n`,
      );
    } else {
      response.writeHead(404);
      response.end();
    }
  } catch {
    errors.push("synthetic HTTP handler failed");
    response.destroy();
  }
});
let browser;
let report;
try {
  const entry = resolve(directory, "entry.ts");
  await writeFile(
    entry,
    [
      `export { FileTaskSession } from ${JSON.stringify(resolve(workbench, "src/file-workflow/file-task-session.ts"))};`,
      `export { SpeechFileClient } from ${JSON.stringify(resolve(workbench, "src/file-workflow/speech-file-client.ts"))};`,
      `export { SessionTaskJournal } from ${JSON.stringify(resolve(workbench, "src/file-workflow/task-journal.ts"))};`,
    ].join("\n"),
  );
  await build({
    root: workbench,
    configFile: false,
    logLevel: "error",
    build: {
      outDir: directory,
      emptyOutDir: false,
      lib: { entry, formats: ["es"], fileName: () => "recovery.js" },
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
  const context = await browser.newContext();
  await context.route("**/*", async (route) => {
    if (new URL(route.request().url()).origin === origin) await route.continue();
    else {
      outbound.push("unexpected outbound request");
      await route.abort();
    }
  });
  const page = await context.newPage();
  page.on("pageerror", () => errors.push("unexpected browser error"));
  const attach = () =>
    page.evaluate(async () => {
      const { FileTaskSession, SpeechFileClient, SessionTaskJournal } =
        await import("/recovery.js");
      window.task = new FileTaskSession(
        new SpeechFileClient(location.origin),
        new SessionTaskJournal(sessionStorage, "speechnexus:recovery-probe"),
      );
      await window.task.restore();
      return window.task.getSnapshot();
    });
  await page.goto(origin);
  await attach();
  const unknown = await page.evaluate(async (synthesis) => {
    if (synthesis)
      await window.task.startText("synthetic text only", {
        model: "tts-quality",
        voiceId: "voice-demo",
        voiceRevision: "1",
        profileId: "profile-demo",
        profileRevision: "2",
      });
    else
      await window.task.start(
        new File(["synthetic audio only"], "synthetic-private-name.wav", { type: "audio/wav" }),
        "asr-quality",
      );
    return window.task.getSnapshot().unknown;
  }, tts);
  assert.equal(unknown, true);
  if (values["upload-unknown"]) {
    assert.equal(creates.length, 0);
    assert.equal(uploads.length, 1);
    await page.reload();
    const pending = await attach();
    assert.equal(pending.requiresInput, true);
    assert.equal(pending.canReplay, false);
    await page.evaluate(() => window.task.replay());
    const saved = await page.evaluate(() => sessionStorage.getItem("speechnexus:recovery-probe"));
    assert(!/synthetic audio|synthetic text|private-name|speech-text.txt|"text"/.test(saved));
    await page.evaluate(async (synthesis) => {
      if (synthesis) await window.task.resumeText("other text");
      else
        await window.task.resumeFile(
          new File(["other audio"], "synthetic-private-name.wav", { type: "audio/wav" }),
        );
    }, tts);
    assert.equal(uploads.length, 1);
    assert.equal(
      await page.evaluate(() => sessionStorage.getItem("speechnexus:recovery-probe")),
      saved,
    );
    await page.evaluate(async (synthesis) => {
      if (synthesis) await window.task.resumeText("synthetic text only");
      else
        await window.task.resumeFile(
          new File(["synthetic audio only"], "synthetic-private-name.wav", { type: "audio/wav" }),
        );
    }, tts);
    assert.equal(uploads.length, 2);
    assert.equal(uploads[0], uploads[1]);
    assert.equal(uploadIntents.size, 1);
  }
  assert.equal(creates.length, 1);
  await page.reload();
  const restored = await attach();
  assert.equal(restored.canReplay, true);
  assert.equal(creates.length, 1);
  await page.evaluate(() => window.task.replay());
  assert.equal(creates.length, 2);
  assert.equal(intents.size, 1);
  assert.deepEqual(creates[0], creates[1]);
  await page.evaluate(() => window.task.cancel());
  assert.equal(cancels.length, 1);
  await page.reload();
  const cancelled = await attach();
  assert.equal(cancelled.unknown, true);
  assert.equal(cancelled.job.cleanup_state, "pending");
  assert.equal(cancels.length, 1);
  await page.evaluate(() => window.task.replay());
  assert.equal(cancels.length, 2);
  assert.equal(cancels[0], cancels[1]);
  const confirmed = await page.evaluate(() => window.task.getSnapshot());
  assert.equal(confirmed.cancellation.accepted, true);
  assert.equal(confirmed.job.cleanup_state, "pending");
  await page.evaluate(() => window.task.reconnect());
  await page.reload();
  const withCursor = await attach();
  assert.equal(withCursor.cursor, "9007199254740993");
  await page.evaluate(() => window.task.reconnect());
  assert.equal(cursors.at(-1), "9007199254740993");
  const raw = await page.evaluate(() => sessionStorage.getItem("speechnexus:recovery-probe"));
  assert(raw.length <= 8192);
  assert(!/synthetic audio|synthetic text|private-name|"text"|"result"|"artifacts"/.test(raw));
  assert.equal(errors.length, 0);
  assert.equal(outbound.length, 0);
  report = {
    checked_at: new Date().toISOString(),
    kind: "chromium-reload-loopback-http-synthetic",
    jobKind: values.kind,
    fixedTtsReferences: tts ? true : undefined,
    passed: true,
    browser: browser.version(),
    reloads: values["upload-unknown"] ? 4 : 3,
    unknownUploadRecovered: values["upload-unknown"],
    uploadPosts: uploads.length,
    uniqueUploadIntents: uploadIntents.size,
    createPosts: creates.length,
    uniqueCreateIntents: intents.size,
    cancelPosts: cancels.length,
    originalCreateKeyAndBody: true,
    originalCancelKey: true,
    exactCursorRestored: true,
    noAudioOrContentStored: true,
    storageCharacters: raw.length,
    pageOrHandlerErrors: errors.length,
    outboundRequests: outbound.length,
    qualifiesLiveSpeechOrGpu: false,
  };
} finally {
  await browser?.close();
  if (server.listening) await new Promise((resolve) => server.close(resolve));
  await rm(directory, { recursive: true, force: true });
}
assert(report);
const output = resolve(root, values.output ?? ".runtime/team-c/c02-recovery-browser.json");
report.serverStopped = !server.listening;
await mkdir(dirname(output), { recursive: true });
await writeFile(output, `${JSON.stringify(report, null, 2)}\n`);
console.log(JSON.stringify(report, null, 2));
