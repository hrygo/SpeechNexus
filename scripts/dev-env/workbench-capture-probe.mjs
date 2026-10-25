/** Real Chromium capture graph, synthetic device only; no audio content is persisted. */
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
const bundleDirectory = await mkdtemp(resolve(tmpdir(), "speechnexus-capture-"));
const server = createServer(async (request, response) => {
  try {
    const url = new URL(request.url ?? "/", "http://127.0.0.1");
    const pathname = url.pathname;
    if (pathname === "/") {
      if (url.searchParams.has("deny-microphone"))
        response.setHeader("Permissions-Policy", "microphone=()");
      response.writeHead(200, { "content-type": "text/html; charset=utf-8" });
      response.end(
        "<!doctype html><title>Synthetic capture probe</title><main>Capture probe</main>",
      );
      return;
    }
    const path = resolve(bundleDirectory, `.${decodeURIComponent(pathname)}`);
    if (!path.startsWith(`${bundleDirectory}${sep}`)) throw new Error("outside probe bundle");
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
        entry: resolve(workbench, "src/media/microphone-capture.ts"),
        formats: ["es"],
        fileName: () => "capture.js",
      },
    },
  });
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", resolve);
  });
  const address = server.address();
  assert(address && typeof address !== "string");
  const origin = `http://127.0.0.1:${address.port}`;
  browser = await chromium.launch({
    headless: true,
    args: [
      "--use-fake-device-for-media-stream",
      "--use-fake-ui-for-media-stream",
      "--autoplay-policy=no-user-gesture-required",
    ],
  });
  const context = await browser.newContext({ permissions: ["microphone"] });
  const outbound = [];
  await context.route("**/*", async (route) => {
    if (new URL(route.request().url()).origin === origin) await route.continue();
    else {
      outbound.push("unexpected outbound request");
      await route.abort();
    }
  });
  await context.addInitScript(() => {
    window.probeStreams = [];
    window.probeContexts = [];
    window.probeTaps = [];
    window.probeFailures = [];
    const getUserMedia = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
    navigator.mediaDevices.getUserMedia = async (constraints) => {
      const stream = await getUserMedia(constraints).catch((error) => {
        window.probeFailures.push(`permission:${error.name}`);
        throw error;
      });
      window.probeStreams.push(stream);
      return stream;
    };
    const Context = window.AudioContext;
    window.AudioContext = class extends Context {
      constructor(options) {
        super(options);
        window.probeContexts.push(this);
        const load = this.audioWorklet.addModule.bind(this.audioWorklet);
        this.audioWorklet.addModule = (...args) =>
          load(...args).catch((error) => {
            window.probeFailures.push(`module:${error.name}`);
            throw error;
          });
      }
    };
    const Worklet = window.AudioWorkletNode;
    window.AudioWorkletNode = class extends Worklet {
      constructor(context, name, options) {
        super(context, name, options);
        const analyser = context.createAnalyser();
        analyser.fftSize = 256;
        const mute = context.createGain();
        mute.gain.value = 0;
        // Observe before this probe-only mute. The production destination edge
        // is still connected by the actual module; this tap cannot hide feedback.
        this.connect(analyser);
        analyser.connect(mute);
        mute.connect(context.destination);
        window.probeTaps.push({ analyser, samples: new Float32Array(256) });
      }
    };
  });
  const page = await context.newPage();
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto(origin);
  const checks = await page.evaluate(async () => {
    const { createMicrophoneCapture } = await import("/capture.js");
    const require = (value, message) => {
      if (!value) throw new Error(message);
    };
    const until = async (predicate) => {
      const end = performance.now() + 5000;
      while (!predicate()) {
        if (performance.now() > end) throw new Error("capture condition timed out");
        await new Promise((resolve) => setTimeout(resolve, 10));
      }
    };
    const clocks = [];
    for (const sampleRate of [16000, 24000, 48000]) {
      let frames = 0;
      let through = 0;
      let nonzero = false;
      let observedOutputPeak = 0;
      const session = await createMicrophoneCapture({
        sampleRate,
        frameSamples: 512,
        maxPendingFrames: 32,
        onFrame: (frame) => {
          require(frame.startSample === through &&
            frame.sampleRate === sampleRate, "capture clock or ordering mismatch");
          through += frame.samples.length;
          nonzero ||= frame.samples.some((sample) => sample !== 0);
          const tap = window.probeTaps.at(-1);
          tap.analyser.getFloatTimeDomainData(tap.samples);
          for (const sample of tap.samples)
            observedOutputPeak = Math.max(observedOutputPeak, Math.abs(sample));
          require(observedOutputPeak === 0, "capture worklet monitored microphone output");
          require(frame.samples.every(Number.isFinite), "invalid captured sample");
          structuredClone(frame.samples, { transfer: [frame.samples.buffer] });
          frames += 1;
        },
      }).catch((error) => {
        throw new Error(`${error.code}; stages=${window.probeFailures.join(",")}`);
      });
      try {
        await until(() => frames >= 5 && nonzero);
      } finally {
        await session.stop();
      }
      require(session.snapshot.state === "stopped", "capture did not stop");
      clocks.push({
        sampleRate,
        frames,
        throughSample: through,
        nonzeroSyntheticInput: nonzero,
        observedOutputPeak,
      });
    }
    const stalled = await createMicrophoneCapture({
      sampleRate: 24000,
      frameSamples: 128,
      maxPendingFrames: 1,
      onFrame: () => new Promise(() => {}),
    });
    await until(() => stalled.snapshot.state === "failed");
    await stalled.closed;
    require(stalled.snapshot.reason === "capture_overrun", "backpressure silently lost input");
    const abort = new AbortController();
    const starting = createMicrophoneCapture({
      sampleRate: 24000,
      frameSamples: 512,
      maxPendingFrames: 8,
      signal: abort.signal,
      onFrame: () => {},
    });
    abort.abort();
    const cancelled = await starting.catch((error) => error.code);
    require(cancelled === "capture_cancelled", "startup did not cancel");
    await until(() =>
      window.probeStreams.every((stream) =>
        stream.getTracks().every((track) => track.readyState === "ended"),
      ),
    );
    // Wait for a native getUserMedia result that may arrive after cancellation.
    await new Promise((resolve) => setTimeout(resolve, 250));
    require(window.probeStreams.every((stream) =>
      stream.getTracks().every((track) => track.readyState === "ended"),
    ), "late permission leaked a track");
    require(window.probeContexts.every(
      (context) => context.state === "closed",
    ), "owned context remained open");
    return {
      clocks,
      stalledConsumerFailsExplicitly: true,
      cancelledStartupReleased: true,
      syntheticStreams: window.probeStreams.length,
      ownedContextsClosed: window.probeContexts.length,
    };
  });
  // Navigate a same-origin child while the parent retains resource references.
  // This observes actual pagehide cleanup rather than relying on GC after unloading.
  await page.evaluate((origin) => {
    const frame = document.createElement("iframe");
    frame.src = origin;
    document.body.append(frame);
  }, origin);
  const frame = page.frames().find((frame) => frame.parentFrame());
  assert(frame);
  await frame.waitForLoadState();
  await frame.evaluate(async () => {
    const { createMicrophoneCapture } = await import("/capture.js");
    window.probeCapture = await createMicrophoneCapture({
      sampleRate: 24000,
      frameSamples: 512,
      maxPendingFrames: 32,
      onFrame: () => {},
    });
  });
  await page.evaluate(() => {
    const iframe = document.querySelector("iframe");
    const child = iframe.contentWindow;
    window.retained = {
      session: child.probeCapture,
      streams: child.probeStreams,
      contexts: child.probeContexts,
    };
    iframe.src = "/?after-navigation";
  });
  await page.waitForFunction(
    () =>
      window.retained.session.snapshot.state === "stopped" &&
      window.retained.streams.every((stream) =>
        stream.getTracks().every((track) => track.readyState === "ended"),
      ) &&
      window.retained.contexts.every((context) => context.state === "closed"),
  );
  checks.actualNavigationReleased = true;

  // A real browser Permissions-Policy rejection. Fake UI cannot prove a human's
  // permission prompt choice, which remains a separate device qualification.
  const denialPage = await context.newPage();
  denialPage.on("pageerror", (error) => errors.push(error.message));
  await denialPage.goto(`${origin}/?deny-microphone`);
  const denied = await denialPage.evaluate(async () => {
    const { createMicrophoneCapture } = await import("/capture.js");
    return createMicrophoneCapture({
      sampleRate: 24000,
      frameSamples: 512,
      maxPendingFrames: 8,
      onFrame: () => {},
    }).then(
      () => "unexpected-success",
      (error) => error.code,
    );
  });
  assert.equal(denied, "capture_permission_denied");
  await denialPage.waitForFunction(() =>
    window.probeContexts.every((context) => context.state === "closed"),
  );
  checks.nativePermissionPolicyDenial = true;
  assert.deepEqual(errors, []);
  assert.deepEqual(outbound, []);
  const report = {
    checkedAt: new Date().toISOString(),
    kind: "synthetic_device_audio_worklet_capture",
    bundle: "production",
    browser: browser.version(),
    node: process.version,
    physicalMicrophoneAccessed: false,
    autoplayPolicyBypassedForProbe: true,
    publicRealtimeOrGpuVerified: false,
    unexpectedNetworkRequests: outbound.length,
    pageErrors: errors.length,
    checks,
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
