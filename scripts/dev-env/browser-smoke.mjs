import { chromium } from "@playwright/test";
import http from "node:http";
import { WebSocketServer } from "ws";

const pageHtml = `<!doctype html>
<html lang="en">
  <head><meta charset="utf-8"><title>SpeechNexus browser probe</title></head>
  <body><main>ready</main></body>
</html>`;

const server = http.createServer((request, response) => {
  response.writeHead(200, { "content-type": "text/html; charset=utf-8" });
  response.end(pageHtml);
});

await new Promise((resolve, reject) => {
  server.once("error", reject);
  server.listen(0, "127.0.0.1", resolve);
});

const address = server.address();
if (!address || typeof address === "string") {
  server.close();
  throw new Error("Unable to allocate a loopback port for the browser probe");
}

const websocketServer = new WebSocketServer({ server, path: "/ws" });
websocketServer.on("connection", (socket) => {
  socket.on("message", (data, isBinary) => socket.send(data, { binary: isBinary }));
});

const browser = await chromium.launch({
  headless: true,
  args: ["--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream"],
});

try {
  const context = await browser.newContext({
    permissions: ["microphone"],
    serviceWorkers: "allow",
  });
  const page = await context.newPage();
  await page.goto(`http://127.0.0.1:${address.port}/`);
  const result = await page.evaluate(async () => {
    if (!navigator.mediaDevices?.getUserMedia || !window.MediaRecorder) {
      throw new Error("MediaDevices or MediaRecorder is unavailable");
    }
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    const recorder = new MediaRecorder(stream);
    const chunks = [];
    const stopped = new Promise((resolve, reject) => {
      recorder.ondataavailable = (event) => chunks.push(event.data);
      recorder.onerror = () => reject(new Error("MediaRecorder failed"));
      recorder.onstop = resolve;
    });
    recorder.start();
    await new Promise((resolve) => setTimeout(resolve, 250));
    recorder.stop();
    await stopped;
    stream.getTracks().forEach((track) => track.stop());

    const offline = new OfflineAudioContext(1, 4800, 48_000);
    const oscillator = offline.createOscillator();
    oscillator.frequency.value = 440;
    oscillator.connect(offline.destination);
    oscillator.start();
    oscillator.stop(0.1);
    const rendered = await offline.startRendering();

    const socket = new WebSocket(`ws://${window.location.host}/ws`);
    socket.binaryType = "arraybuffer";
    await new Promise((resolve, reject) => {
      socket.addEventListener("open", resolve, { once: true });
      socket.addEventListener("error", () => reject(new Error("WebSocket open failed")), {
        once: true,
      });
    });
    const textRoundTrip = await new Promise((resolve, reject) => {
      socket.addEventListener(
        "message",
        (event) => {
          if (typeof event.data === "string") {
            resolve(event.data);
          } else {
            reject(new Error("Expected a text WebSocket frame"));
          }
        },
        { once: true },
      );
      socket.send("socket-ready");
    });
    const binaryRoundTrip = await new Promise((resolve, reject) => {
      socket.addEventListener(
        "message",
        (event) => {
          if (event.data instanceof ArrayBuffer) {
            resolve(Array.from(new Uint8Array(event.data)));
          } else {
            reject(new Error("Expected a binary WebSocket frame"));
          }
        },
        { once: true },
      );
      socket.send(new Uint8Array([1, 2, 3, 4]));
    });
    socket.close();

    return {
      recordedBytes: chunks.reduce((total, chunk) => total + chunk.size, 0),
      renderedSamples: rendered.length,
      textRoundTrip,
      binaryRoundTrip,
      origin: window.location.origin,
    };
  });

  if (
    result.textRoundTrip !== "socket-ready" ||
    JSON.stringify(result.binaryRoundTrip) !== JSON.stringify([1, 2, 3, 4]) ||
    result.recordedBytes === 0 ||
    result.renderedSamples !== 4800
  ) {
    throw new Error(`Browser capability probe failed: ${JSON.stringify(result)}`);
  }
  console.log(
    `browser toolchain: ready (Chromium ${browser.version()}, fake microphone ${result.recordedBytes} bytes, Web Audio ${result.renderedSamples} samples, WebSocket text/binary round trip)`,
  );
} finally {
  await browser.close();
  await new Promise((resolve) => websocketServer.close(resolve));
  await new Promise((resolve) => server.close(resolve));
}
