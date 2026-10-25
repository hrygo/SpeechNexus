/** Synthetic AudioWorklet check. No device permission, live service, or audio export. */
import { chromium } from "@playwright/test";
import assert from "node:assert/strict";
import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { createServer } from "node:http";
import { createRequire } from "node:module";
import { tmpdir } from "node:os";
import { dirname, resolve, sep } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { parseArgs } from "node:util";

const { values } = parseArgs({ options: { output: { type: "string" } } });
const root = fileURLToPath(new URL("../../", import.meta.url));
const workbench = resolve(root, "web/workbench");
const require = createRequire(resolve(workbench, "package.json"));
const { build } = await import(pathToFileURL(require.resolve("vite")).href);
const bundleDirectory = await mkdtemp(resolve(tmpdir(), "speechnexus-worklet-"));
const server = createServer(async (request, response) => {
  try {
    const pathname = new URL(request.url ?? "/", "http://127.0.0.1").pathname;
    if (pathname === "/") {
      response.writeHead(200, { "content-type": "text/html; charset=utf-8" });
      response.end(
        "<!doctype html><title>Synthetic media probe</title><main>AudioWorklet probe</main>",
      );
      return;
    }
    const path = resolve(bundleDirectory, `.${decodeURIComponent(pathname)}`);
    if (!path.startsWith(`${bundleDirectory}${sep}`)) throw new Error("path outside probe bundle");
    const body = await readFile(path);
    response.writeHead(200, { "content-type": "text/javascript; charset=utf-8" });
    response.end(body);
  } catch {
    response.writeHead(404);
    response.end();
  }
});

let browser;
try {
  await build({
    root: workbench,
    configFile: false,
    base: "./",
    logLevel: "error",
    build: {
      outDir: bundleDirectory,
      emptyOutDir: false,
      lib: {
        entry: resolve(workbench, "src/media/worklet-playback.ts"),
        formats: ["es"],
        fileName: () => "playback.js",
      },
    },
  });
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", resolve);
  });
  const address = server.address();
  assert(address && typeof address !== "string", "loopback server did not start");
  const origin = `http://127.0.0.1:${address.port}`;
  browser = await chromium.launch({ headless: true });
  const context = await browser.newContext();
  const outbound = [];
  await context.route("**/*", async (route) => {
    if (new URL(route.request().url()).origin === origin) await route.continue();
    else {
      outbound.push("unexpected non-loopback request");
      await route.abort();
    }
  });
  const page = await context.newPage();
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto(origin, { waitUntil: "networkidle" });

  const checks = await page.evaluate(async () => {
    const { createWorkletPlayback } = await import("/playback.js");
    const pcmSampleRate = 24000;
    const identity = { epoch: 1, generation: 1 };
    const options = { identity, capacitySamples: 512, pcmSampleRate };
    const input = new Float32Array(256);
    const pattern = [0.25, -0.25, 0.5, -0.5];
    for (let index = 0; index < input.length; index += 1) input[index] = pattern[index % 4];
    const require = (condition, message) => {
      if (!condition) throw new Error(message);
    };
    const awaitCondition = async (condition) => {
      const deadline = performance.now() + 3000;
      while (!condition()) {
        if (performance.now() > deadline) throw new Error("processor report timed out");
        await new Promise((resolve) => setTimeout(resolve, 10));
      }
    };

    const audio = new OfflineAudioContext(1, 1024, pcmSampleRate);
    const playback = await createWorkletPlayback(audio, options);
    let renderedSamples;
    let offset;
    try {
      require(playback.push(identity, 0, input) === "accepted", "valid PCM rejected");
      require(playback.push(identity, 256, new Float32Array(300)) ===
        "backpressure", "unbounded in-flight PCM");
      await awaitCondition(() => playback.snapshot.inFlightMessages === 0);
      const rendered = await audio.startRendering();
      const output = rendered.getChannelData(0);
      offset = output.findIndex((sample) => sample !== 0);
      require(offset >= 0 && offset + input.length <= output.length, "PCM not rendered");
      require(input.every(
        (sample, index) => output[offset + index] === sample,
      ), "rendered PCM differs");
      renderedSamples = output.reduce((count, sample) => count + Number(sample !== 0), 0);
      require(renderedSamples === input.length, "missing or repeated PCM");
      await awaitCondition(
        () =>
          playback.snapshot.renderedThrough === input.length &&
          playback.snapshot.inFlightMessages === 0,
      );
      require(playback.snapshot.availableSamples === 512, "consumption credit did not recover");
    } finally {
      playback.stop();
    }

    const stoppedContext = new OfflineAudioContext(1, 512, pcmSampleRate);
    const stopped = await createWorkletPlayback(stoppedContext, {
      ...options,
      identity: { epoch: 1, generation: 2 },
    });
    stopped.push({ epoch: 1, generation: 2 }, 0, input);
    stopped.stop();
    require(stopped.push({ epoch: 1, generation: 2 }, input.length, input) ===
      "stopped", "late PCM accepted after stop");
    const stoppedOutput = await stoppedContext.startRendering();
    require(stoppedOutput
      .getChannelData(0)
      .every((sample) => sample === 0), "stopped node remained audible");

    const abortContext = new OfflineAudioContext(1, 512, pcmSampleRate);
    const abort = new AbortController();
    const aborted = await createWorkletPlayback(abortContext, { ...options, signal: abort.signal });
    aborted.push(identity, 0, input);
    abort.abort();
    require(aborted.snapshot.state === "stopped", "abort did not release playback");
    const abortedOutput = await abortContext.startRendering();
    require(abortedOutput
      .getChannelData(0)
      .every((sample) => sample === 0), "aborted node remained audible");

    const epochContext = new OfflineAudioContext(1, 512, pcmSampleRate);
    const nextIdentity = { epoch: 2, generation: 3 };
    const next = await createWorkletPlayback(epochContext, { ...options, identity: nextIdentity });
    try {
      require(next.push(identity, 0, input) === "stale", "old epoch/generation was accepted");
      require(next.push(nextIdentity, 0, input) === "accepted", "current epoch rejected");
      await awaitCondition(() => next.snapshot.inFlightMessages === 0);
      const output = await epochContext.startRendering();
      require(output.getChannelData(0).filter((sample) => sample !== 0).length ===
        input.length, "epoch output count differs");
    } finally {
      next.stop();
    }

    const replacementContext = new OfflineAudioContext(1, 512, pcmSampleRate);
    const old = await createWorkletPlayback(replacementContext, options);
    old.push(identity, 0, input);
    const replacement = await createWorkletPlayback(replacementContext, {
      ...options,
      identity: { epoch: 2, generation: 2 },
    });
    try {
      require(old.snapshot.state === "stopped", "previous generation remained connected");
      const silence = await replacementContext.startRendering();
      require(silence
        .getChannelData(0)
        .every((sample) => sample === 0), "old queued PCM survived replacement");
    } finally {
      replacement.stop();
    }
    return {
      renderedSamples,
      firstRenderedOffsetSamples: offset,
      pcmSampleRate,
      boundedInFlight: true,
      stopSilence: true,
      abortSilence: true,
      staleGenerationDropped: true,
      replacementSilence: true,
    };
  });
  assert.deepEqual(errors, [], "browser page errors");
  assert.deepEqual(outbound, [], "unexpected network requests");
  const report = {
    kind: "synthetic_offline_audio_worklet",
    bundle: "production",
    browser: browser.version(),
    node: process.version,
    checks,
    devicePermissionsRequested: false,
    publicRealtimeOrGpuVerified: false,
  };
  const json = `${JSON.stringify(report, null, 2)}\n`;
  if (values.output) {
    await mkdir(dirname(resolve(values.output)), { recursive: true });
    await writeFile(values.output, json);
  }
  process.stdout.write(json);
} finally {
  await browser?.close();
  if (server.listening)
    await new Promise((resolve, reject) =>
      server.close((error) => (error ? reject(error) : resolve())),
    );
  await rm(bundleDirectory, { recursive: true, force: true });
}
