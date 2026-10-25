/** One-shot synthetic producer and production browser. No models, physical devices or production service. */
import assert from "node:assert/strict";
import { createServer } from "node:http";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { resolve, sep, extname } from "node:path";
import { parseArgs } from "node:util";
import { WebSocketServer } from "ws";
import { chromium, expect } from "@playwright/test";
import { parse as rest } from "../../packages/contracts/src/realtime.ts";
import { parse as event } from "../../packages/contracts/src/realtime_events.ts";

const { values } = parseArgs({
  options: { output: { type: "string" }, "cleanup-mode": { type: "string" } },
});
assert(values.output);
const cleanupMode = values["cleanup-mode"];
assert(cleanupMode === undefined || ["delayed", "rejected"].includes(cleanupMode));
const output = resolve(values.output),
  dist = resolve("web/workbench/dist");
await mkdir(output, { recursive: true });
const sessions = new Map(),
  intents = new Map(),
  writes = [],
  commands = [],
  errors = [],
  outbound = [],
  serverErrors = [];
const profile = { id: "profile-tts", revision: "1" },
  models = { tts: { id: "tts-demo", revision: "1" } },
  voice = { id: "voice-demo", revision: "1" };
const descriptor = { encoding: "pcm_s16le", sample_rate_hz: 24000, channels: 1 };
const config = { profile, models, voice, policy_revision: "1", catalog_revision: "1" };
const limits = {
  max_input_chunk_ms: 100,
  max_message_bytes: 65536,
  input_credit_samples: 0,
  output_credit_samples: 24000,
  max_segment_ms: 20000,
  max_session_duration_ms: 3600000,
};
const parameters = {
  tts: {
    language: "zh",
    text_commit_mode: "explicit",
    audio_delivery: "chunked",
    speed: 1,
    planner: { max_chars: 400, max_wait_ms: 3000 },
  },
};
let origin;
function json(res, status, value) {
  const raw = JSON.stringify(value);
  res.writeHead(status, {
    "Content-Type": "application/json",
    "Content-Length": Buffer.byteLength(raw),
  });
  res.end(raw);
}
function lost(res, status) {
  res.writeHead(status, { "Content-Type": "application/json", "Content-Length": 10000 });
  res.flushHeaders();
  res.write('{"synthetic":');
  setTimeout(() => res.destroy(), 20);
}
function snapshot(s) {
  return rest("RealtimeSession", structuredClone(s.value));
}
function send(s, type, fields = {}) {
  const value = {
    type,
    event_id: `server-${s.value.id}-${++s.seq}`,
    session_id: s.value.id,
    seq: s.seq,
    stream_epoch: 1,
    sent_at: new Date().toISOString(),
    ...fields,
  };
  event("RealtimeEvent", value);
  if (s.ws.readyState === 1) s.ws.send(JSON.stringify(value));
  return value;
}
function terminal(s, status) {
  const operation_id = `op-${s.utterance}`,
    receipt =
      status === "succeeded"
        ? {
            receipt_id: `receipt-${s.utterance}`,
            requested: {},
            effective: {},
            observed: {},
            missing: [],
            unknown: [],
          }
        : null;
  send(s, "nexus.operation.done", {
    operation_id,
    kind: "tts",
    status,
    cleanup_state: "complete",
    ...(receipt ? { receipt } : {}),
  });
  s.value.terminal_operations.push({
    operation_id,
    kind: "tts",
    status,
    cleanup_state: "complete",
    receipt_id: receipt?.receipt_id ?? null,
  });
}
const server = createServer(async (req, res) => {
  try {
    const url = new URL(req.url, origin ?? "http://127.0.0.1");
    if (req.method === "POST") {
      const parts = [];
      let bytes = 0;
      for await (const p of req) {
        bytes += p.length;
        assert(bytes <= 65536);
        parts.push(p);
      }
      const body = JSON.parse(Buffer.concat(parts).toString()),
        key = req.headers["idempotency-key"];
      assert(key);
      writes.push({ path: url.pathname, key, body });
      const old = intents.get(key);
      if (old) {
        assert.deepEqual(old.body, body);
        assert.equal(old.path, url.pathname);
        json(res, old.status, old.reply);
        return;
      }
      if (url.pathname === "/v1/nexus/realtime/sessions") {
        rest("RealtimeSessionCreateRequest", body);
        assert.deepEqual(body.profile, profile);
        assert.deepEqual(body.models, models);
        assert.deepEqual(body.voice, voice);
        const id = `session-${sessions.size + 1}`,
          s = {
            seq: 0,
            ws: null,
            utterance: null,
            value: {
              id,
              object: "nexus.realtime_session",
              status: "created",
              expires_at: new Date(Date.now() + 30000).toISOString(),
              connection_url: `/v1/realtime?session_id=${id}`,
              effective_configuration: config,
              limits,
              cleanup_state: "not_required",
              terminal_operations: [],
              closed_at: null,
              error: null,
            },
          };
        sessions.set(id, s);
        const reply = snapshot(s);
        intents.set(key, { path: url.pathname, body, status: 201, reply });
        if (sessions.size === 1) lost(res, 201);
        else json(res, 201, reply);
        return;
      }
      const match = url.pathname.match(
        /^\/v1\/nexus\/realtime\/sessions\/(session-\d+)\/cancellations$/u,
      );
      assert(match);
      const s = sessions.get(match[1]);
      assert(s);
      assert(["drain", "abort"].includes(body.mode));
      s.value.status = "draining";
      s.value.cleanup_state = "pending";
      const reply = snapshot(s);
      intents.set(key, { path: url.pathname, body, status: 202, reply });
      if (s.value.id === "session-1") lost(res, 202);
      else json(res, 202, reply);
      return;
    }
    if (url.pathname.startsWith("/v1/nexus/realtime/sessions/")) {
      const s = sessions.get(url.pathname.split("/").at(-1));
      assert(s);
      json(res, 200, snapshot(s));
      return;
    }
    if (url.pathname.startsWith("/v1/")) {
      json(res, 503, {
        error: {
          code: "synthetic_unavailable",
          type: "service_unavailable_error",
          message: "Synthetic only",
          param: null,
          request_id: "request-demo",
          retryable: false,
        },
      });
      return;
    }
    const path =
      url.pathname === "/"
        ? resolve(dist, "index.html")
        : resolve(dist, "." + decodeURIComponent(url.pathname));
    assert(path.startsWith(dist + sep));
    const data = await readFile(path);
    res.writeHead(200, {
      "Content-Type":
        {
          ".js": "text/javascript",
          ".css": "text/css",
          ".html": "text/html",
          ".woff2": "font/woff2",
          ".svg": "image/svg+xml",
        }[extname(path)] ?? "application/octet-stream",
    });
    res.end(data);
  } catch (e) {
    serverErrors.push(String(e));
    if (!res.headersSent) res.writeHead(500);
    res.end();
  }
});
const wss = new WebSocketServer({ noServer: true, maxPayload: 65536 });
server.on("upgrade", (req, socket, head) => {
  try {
    const url = new URL(req.url, origin);
    assert.equal(req.headers.origin, origin);
    assert.equal(url.pathname, "/v1/realtime");
    const s = sessions.get(url.searchParams.get("session_id"));
    assert(s);
    wss.handleUpgrade(req, socket, head, (ws) => {
      s.ws = ws;
      s.value.status = "active";
      s.value.cleanup_state = "pending";
      send(s, "nexus.session.ready", {
        effective_configuration: { config_revision: "1", ...config, parameters },
        limits,
        input_credit_samples: 0,
      });
      ws.on("message", (raw) => {
        try {
          const value = JSON.parse(raw.toString());
          event("RealtimeEvent", value);
          commands.push(value);
          if (value.type === "nexus.tts.start") {
            s.utterance = value.utterance_id;
            s.text = "";
            s.seqText = 0;
            const started = () =>
              send(s, "nexus.tts.started", {
                utterance_id: s.utterance,
                operation_id: `op-${s.utterance}`,
                audio: descriptor,
                text_credit: 2000,
              });
            s.starts = (s.starts ?? 0) + 1;
            if (s.starts === 2) {
              s.startedLater = started;
            } else started();
          } else {
            assert.equal(value.utterance_id, s.utterance);
            if (value.type === "nexus.tts.append_text") {
              assert.equal(value.text_seq, ++s.seqText);
              s.text += value.text;
              send(s, "nexus.tts.text_ack", {
                utterance_id: s.utterance,
                operation_id: `op-${s.utterance}`,
                accepted_through_text_seq: s.seqText,
                text_credit: 2000,
              });
            } else if (value.type === "nexus.tts.commit_text")
              assert.equal(value.through_text_seq, s.seqText);
            else if (value.type === "nexus.tts.finish_text") {
              for (let i = 0; i < 8; i++) {
                const pcm = Buffer.alloc(12000);
                for (let n = 0; n < 6000; n++)
                  pcm.writeInt16LE(
                    Math.round(500 * Math.sin((2 * Math.PI * 220 * (i * 6000 + n)) / 24000)),
                    n * 2,
                  );
                const delta = send(s, "nexus.tts.audio.delta", {
                  utterance_id: s.utterance,
                  operation_id: `op-${s.utterance}`,
                  chunk_seq: i + 1,
                  audio: pcm.toString("base64"),
                  start_sample: i * 6000,
                  sample_count: 6000,
                });
                if (i === 0) ws.send(JSON.stringify(delta));
              }
              terminal(s, "succeeded");
            } else if (value.type === "nexus.tts.cancel") {
              if (s.startedLater) {
                s.cancelBeforeStarted = true;
                s.startedLater();
                s.startedLater = null;
                send(s, "nexus.tts.audio.delta", {
                  utterance_id: s.utterance,
                  operation_id: `op-${s.utterance}`,
                  chunk_seq: 1,
                  audio: Buffer.alloc(6).toString("base64"),
                  start_sample: 0,
                  sample_count: 3,
                });
              }
              terminal(s, "cancelled");
            } else assert.fail("Unexpected client command");
          }
        } catch (e) {
          serverErrors.push(String(e));
          ws.close(1002);
        }
      });
    });
  } catch (e) {
    serverErrors.push(String(e));
    socket.destroy();
  }
});
await new Promise((r) => server.listen(0, "127.0.0.1", r));
origin = `http://127.0.0.1:${server.address().port}`;
const browser = await chromium.launch({
  headless: true,
  args: ["--use-fake-device-for-media-stream"],
});
const context = await browser.newContext({ viewport: { width: 1280, height: 960 } });
const page = await context.newPage();
await context.addInitScript((cleanupMode) => {
  window.__ttsContexts = [];
  const Native = window.AudioContext;
  window.AudioContext = class extends Native {
    constructor(...args) {
      super(...args);
      window.__ttsContexts.push(this);
    }
    close() {
      if (!cleanupMode) return super.close();
      window.__ttsCloseCalls = (window.__ttsCloseCalls ?? 0) + 1;
      return new Promise((resolve, reject) => {
        window.__ttsReleaseClose = () => {
          if (cleanupMode === "rejected") reject(new Error("Synthetic close rejection"));
          else super.close().then(resolve, reject);
        };
      });
    }
  };
}, cleanupMode);
await context.route("**/*", (route) => {
  const url = new URL(route.request().url());
  if (url.origin !== origin) {
    outbound.push(url.origin);
    return route.abort();
  }
  return route.continue();
});
page.on("pageerror", (e) => errors.push(e.message));
const focusChecks = [];
const usableFocus = async (checkpoint) => {
  await expect
    .poll(
      () =>
        page.evaluate((name) => {
          const host = document.querySelector(`[aria-label="${name}"]`),
            focused = document.activeElement;
          return (
            !!host?.contains(focused) &&
            focused instanceof HTMLElement &&
            focused.matches("button, input, textarea, select, a[href], [tabindex]") &&
            !focused.matches(":disabled, [aria-disabled='true']") &&
            focused.getClientRects().length > 0 &&
            getComputedStyle(focused).visibility !== "hidden"
          );
        }, "实时配音工作台"),
      { message: `usable keyboard focus after ${checkpoint}` },
    )
    .toBe(true);
  focusChecks.push({
    checkpoint,
    ...(await page.evaluate(() => ({
      tag: document.activeElement.tagName,
      disabled: document.activeElement.matches(":disabled"),
    }))),
  });
};
const activate = async (name) => {
  const button = page.getByRole("button", { name, exact: true });
  await expect(button).toBeEnabled();
  await button.focus();
  await expect(button).toBeFocused();
  await page.keyboard.press("Enter");
  await usableFocus(name);
};
try {
  await page.goto(origin);
  await page.getByRole("button", { name: "实时配音", exact: true }).click();
  await page.getByLabel("TTS profile ID", { exact: true }).fill(profile.id);
  await page.getByLabel("TTS profile revision", { exact: true }).fill("1");
  await page.getByLabel("TTS model ID", { exact: true }).fill(models.tts.id);
  await page.getByLabel("TTS model revision", { exact: true }).fill("1");
  await page.getByLabel("Voice ID", { exact: false }).fill(voice.id);
  await page.getByLabel("Voice revision", { exact: true }).fill("1");
  await activate("创建实时配音会话");
  await expect(page.getByRole("alert")).toContainText("请求结果尚未确认");
  await page.reload();
  await page.getByRole("button", { name: "实时配音", exact: true }).click();
  assert.equal(await page.evaluate(() => window.__ttsContexts.length), 0);
  await page.evaluate(() => {
    Object.defineProperty(document, "hidden", { configurable: true, get: () => true });
    document.dispatchEvent(new Event("visibilitychange"));
    Object.defineProperty(document, "hidden", { configurable: true, get: () => false });
    document.dispatchEvent(new Event("visibilitychange"));
    delete document.hidden;
  });
  assert.equal(writes.length, 1);
  await activate("按原键重放");
  await expect(page.getByRole("button", { name: "连接配音会话", exact: true })).toBeEnabled();
  assert.equal(writes[0].key, writes[1].key);
  assert.deepEqual(writes[0].body, writes[1].body);
  await activate("连接配音会话");
  await expect(page.getByTestId("tts-phase")).toHaveText("握手完成");
  await activate("开始一轮配音");
  await expect(page.getByRole("button", { name: "取消本轮并停止播放" })).toBeEnabled();
  const text = "<script>合成配音</script>\nUnicode 😀";
  await page.getByLabel("待追加文本", { exact: true }).fill(text);
  await activate("追加这段文本");
  await expect(page.getByRole("button", { name: "冻结已确认前缀" })).toBeEnabled();
  await activate("冻结已确认前缀");
  await activate("结束文本输入");
  await expect(page.getByTestId("tts-received")).toHaveText("48000");
  await expect(page.getByText("succeeded / complete", { exact: true })).toBeVisible();
  const before = Number(await page.getByTestId("tts-rendered").innerText());
  assert(before < 48000);
  if (cleanupMode) {
    // Real AudioContext/Worklet, with only the native close boundary delayed or rejected.
    const stop = page.getByRole("button", { name: "停止本轮播放", exact: true });
    await expect(stop).toBeEnabled();
    await stop.focus();
    await page.keyboard.press("Enter");
    await usableFocus("停止本轮播放");
    await expect(page.getByTestId("tts-playback-state")).toContainText("正在停止");
    await expect(stop).toBeDisabled();
    await expect(page.getByRole("button", { name: "开始一轮配音", exact: true })).toBeDisabled();
    assert.equal(commands.filter((c) => c.type === "nexus.tts.cancel").length, 0);
    assert.equal(await page.evaluate(() => window.__ttsCloseCalls), 1);
    assert.equal(await page.evaluate(() => window.__ttsContexts[0].state), "running");
    const stoppedAt = Number(await page.getByTestId("tts-rendered").innerText());
    assert(stoppedAt < 48000);
    await activate("查询当前清理状态");
    await expect(page.getByTestId("tts-playback-state")).toContainText("正在停止");
    await page.evaluate(() => window.__ttsReleaseClose());
    if (cleanupMode === "delayed") {
      await expect(page.getByTestId("tts-playback-state")).toHaveText("stopped");
      await expect(page.getByRole("button", { name: "开始一轮配音", exact: true })).toBeEnabled();
      assert.equal(await page.evaluate(() => window.__ttsContexts[0].state), "closed");
    } else {
      await expect(page.getByRole("alert")).toContainText("尚未确认停止");
      await activate("查询当前清理状态");
      await expect(page.getByRole("alert")).toContainText("尚未确认停止");
      await expect(page.getByRole("button", { name: "开始一轮配音", exact: true })).toBeDisabled();
      assert.equal(await page.evaluate(() => window.__ttsContexts[0].state), "running");
    }
    assert.equal(Number(await page.getByTestId("tts-rendered").innerText()), stoppedAt);
    assert.equal(await page.evaluate(() => window.__ttsCloseCalls), 1);
    assert.deepEqual(errors, []);
    assert.deepEqual(serverErrors, []);
    assert.deepEqual(outbound, []);
    await page.screenshot({ path: resolve(output, "cleanup.png"), fullPage: true });
    const result = {
      ok: true,
      cleanup_mode: cleanupMode,
      browser: browser.version(),
      synthetic_rest_ws: true,
      production_bundle: true,
      actual_audio_worklet: true,
      native_close_fault_injection: true,
      physical_device: false,
      stopped_at: stoppedAt,
      received: 48000,
      close_calls: 1,
      remote_cancel_commands: 0,
      errors,
      serverErrors,
      outbound,
    };
    await writeFile(resolve(output, "result.json"), JSON.stringify(result, null, 2));
    console.log(JSON.stringify(result));
  } else {
    await expect(page.getByTestId("tts-rendered")).toHaveText("48000");
    await expect
      .poll(() => page.evaluate(() => window.__ttsContexts.every((c) => c.state === "closed")))
      .toBe(true);
    assert.equal(commands.filter((c) => c.type === "nexus.tts.append_text")[0].text, text);
    await expect(
      page.getByRole("region", { name: "配音事实" }).getByText(text, { exact: true }),
    ).toBeVisible();
    await activate("开始一轮配音");
    await expect(page.getByRole("button", { name: "取消本轮并停止播放" })).toBeEnabled();
    await activate("取消本轮并停止播放");
    await expect(page.getByText("cancelled / complete", { exact: true })).toBeVisible();
    assert.equal(sessions.get("session-1").cancelBeforeStarted, true);
    await expect(page.getByTestId("tts-received")).toHaveText("0");
    await activate("开始一轮配音");
    const drainText = "排空前已接收的文本";
    await page.getByRole("textbox", { name: "待追加文本", exact: true }).fill(drainText);
    await activate("追加这段文本");
    await expect(page.getByRole("button", { name: "冻结已确认前缀" })).toBeEnabled();
    await activate("排空并关闭");
    await expect(page.getByRole("alert")).toContainText("请求结果尚未确认");
    await activate("按原键重放");
    await expect(page.getByRole("button", { name: "按原键重放", exact: true })).toBeDisabled();
    assert.equal(writes[2].key, writes[3].key);
    assert.deepEqual(writes[2].body, writes[3].body);
    await expect(page.getByTestId("tts-phase")).toHaveText("排空中，可结束已接受的文本");
    await expect(page.getByRole("button", { name: "开始一轮配音", exact: true })).toBeDisabled();
    await expect(page.getByRole("button", { name: "追加这段文本", exact: true })).toBeDisabled();
    await activate("冻结已确认前缀");
    await activate("结束文本输入");
    await expect(page.getByTestId("tts-received")).toHaveText("48000");
    await expect(page.getByTestId("tts-rendered")).toHaveText("48000");
    await expect(page.getByText("succeeded / complete", { exact: true })).toBeVisible();
    await activate("查询当前清理状态");
    await expect(page.getByText("draining / pending", { exact: true })).toBeVisible();
    await page.setViewportSize({ width: 360, height: 900 });
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    await page.screenshot({ path: resolve(output, "tts-360.png"), fullPage: true });
    await page.getByRole("button", { name: "文件转写", exact: true }).click();
    await expect
      .poll(() => page.evaluate(() => window.__ttsContexts.every((c) => c.state === "closed")))
      .toBe(true);
    await page.getByRole("button", { name: "实时配音", exact: true }).click();
    await expect(page.getByRole("textbox", { name: "待追加文本", exact: true })).toHaveValue(
      drainText,
    );
    await page.getByRole("textbox", { name: "待追加文本", exact: true }).focus();
    await page.keyboard.press("Shift+Tab");
    assert(await page.evaluate(() => document.activeElement?.tagName !== "BODY"));
    assert.deepEqual(outbound, []);
    assert.deepEqual(errors, []);
    assert.deepEqual(serverErrors, []);
    await writeFile(
      resolve(output, "result.json"),
      JSON.stringify(
        {
          browser: browser.version(),
          synthetic_rest_ws: true,
          production_bundle: true,
          actual_audio_worklet: true,
          physical_device: false,
          sessions: sessions.size,
          writes,
          commands: commands.map(({ audio, ...c }) => c),
          received: 48000,
          rendered: 48000,
          rendered_at_generation_done: before,
          hidden_visible_dom_fault_injection: true,
          cancel_before_started: true,
          finish_after_drain: true,
          utterances: 3,
          total_samples_rendered: 96000,
          errors,
          outbound,
          serverErrors,
        },
        null,
        2,
      ),
    );
    console.log(
      JSON.stringify({
        ok: true,
        output,
        sessions: sessions.size,
        writes: writes.length,
        received: 48000,
        rendered: 48000,
      }),
    );
  }
} catch (error) {
  await writeFile(
    resolve(output, "failure.json"),
    JSON.stringify(
      {
        error: String(error),
        errors,
        outbound,
        serverErrors,
        writes,
        commands,
        body: await page.locator("body").innerText(),
        inputs: await page.locator("textarea").evaluateAll((nodes) =>
          nodes.map((n) => ({
            html: n.outerHTML,
            hidden: !!n.closest("[hidden]"),
            labels: [...n.labels].map((l) => l.textContent),
          })),
        ),
      },
      null,
      2,
    ),
  );
  throw error;
} finally {
  await writeFile(resolve(output, "keyboard-focus.json"), JSON.stringify(focusChecks, null, 2));
  await context.close();
  await browser.close();
  for (const ws of wss.clients) ws.terminate();
  await new Promise((r) => wss.close(r));
  await new Promise((r) => server.close(r));
}
