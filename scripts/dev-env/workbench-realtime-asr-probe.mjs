/** One-shot loopback synthetic speech producer + production Chromium consumer. No model or physical device. */
import assert from "node:assert/strict";
import { createServer } from "node:http";
import { readFile, mkdir, writeFile } from "node:fs/promises";
import { resolve, extname, sep } from "node:path";
import { parseArgs } from "node:util";
import { WebSocketServer } from "ws";
import { chromium, expect } from "@playwright/test";
import { parse as parseRest } from "../../packages/contracts/src/realtime.ts";
import { parse as parseEvent } from "../../packages/contracts/src/realtime_events.ts";

const { values } = parseArgs({
  options: {
    output: { type: "string" },
    "cleanup-mode": { type: "string" },
    "stop-kind": { type: "string", default: "abort" },
  },
});
assert(values.output);
const cleanupMode = values["cleanup-mode"];
assert(cleanupMode === undefined || ["delayed", "rejected"].includes(cleanupMode));
const stopKind = values["stop-kind"];
assert(["abort", "interruption"].includes(stopKind));
const output = resolve(values.output),
  dist = resolve("web/workbench/dist");
await mkdir(output, { recursive: true });
const traces = JSON.parse(
  await readFile("tests/fixtures/contracts/transcription-sequences.json", "utf8"),
);
const sessions = new Map(),
  intents = new Map(),
  writes = [],
  serverErrors = [],
  outbound = [],
  errors = [],
  sockets = [];
let origin,
  createCount = 0;
const limits = {
  max_input_chunk_ms: 100,
  max_message_bytes: 65536,
  input_credit_samples: 24000,
  output_credit_samples: 0,
  max_segment_ms: 20000,
  max_session_duration_ms: 3600000,
};
const params = {
  language: "zh",
  preview_interval_ms: 500,
  max_segment_ms: 20000,
  finalization: "full_segment",
  final_deadline_ms: 5000,
  auxiliary_deadline_ms: 5000,
  rollback_tokens: 0,
  turn_detection: { mode: "manual", silence_duration_ms: 500 },
  alignment: "off",
  diarization: "off",
};
const config = {
  profile: { id: "profile-demo", revision: "1" },
  models: { asr: { id: "asr-quality", revision: "1" } },
  policy_revision: "1",
  catalog_revision: "1",
};
const snapshot = (s) => parseRest("RealtimeSession", structuredClone(s.value));
function send(s, value) {
  parseEvent("RealtimeEvent", value);
  if (s.ws?.readyState === 1) s.ws.send(JSON.stringify(value));
}
function nexus(s, type, fields = {}) {
  send(s, {
    type,
    event_id: `server-${s.value.id}-${++s.seq}`,
    session_id: s.value.id,
    seq: s.seq,
    stream_epoch: 1,
    sent_at: new Date().toISOString(),
    ...fields,
  });
}
function admit(s) {
  s.value.status = "active";
  s.value.cleanup_state = "pending";
  nexus(s, "nexus.session.ready", {
    effective_configuration: {
      config_revision: "1",
      profile: config.profile,
      models: config.models,
      policy_revision: "1",
      parameters: { asr: params },
    },
    limits,
    input_credit_samples: s.number === 2 ? 2400 : 24000,
  });
}
function complete(s) {
  s.value.status = "closed";
  s.value.cleanup_state = "complete";
  s.value.closed_at = new Date().toISOString();
  s.value.connection_url = null;
  s.value.terminal_operations = s.value.terminal_operations.map((o) => ({
    ...o,
    cleanup_state: "complete",
  }));
  snapshot(s);
  nexus(s, "nexus.session.closed", {
    status: "closed",
    cleanup_state: "complete",
    operations: s.value.terminal_operations.map(({ operation_id, kind, status }) => ({
      operation_id,
      kind,
      status,
    })),
  });
}
function json(res, status, value) {
  const raw = JSON.stringify(value);
  res.writeHead(status, {
    "Content-Type": "application/json",
    "Content-Length": Buffer.byteLength(raw),
  });
  res.end(raw);
}
function lostReply(res, status) {
  res.writeHead(status, { "Content-Type": "application/json", "Content-Length": 10000 });
  res.flushHeaders();
  res.write('{"synthetic":');
  setTimeout(() => res.destroy(), 20);
}
const server = createServer(async (req, res) => {
  try {
    const url = new URL(req.url, origin ?? "http://127.0.0.1");
    if (req.method === "POST") {
      const parts = [];
      let bytes = 0;
      for await (const part of req) {
        bytes += part.length;
        assert(bytes <= 65536);
        parts.push(part);
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
        parseRest("RealtimeSessionCreateRequest", body);
        assert.deepEqual(body.models, config.models);
        assert.deepEqual(body.profile, config.profile);
        const n = ++createCount,
          id = `session-${n}`;
        const s = {
          number: n,
          seq: 0,
          sent: 0,
          commits: 0,
          segmentStart: 0,
          ws: null,
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
        if (n === 1) {
          lostReply(res, 201);
          return;
        }
        json(res, 201, reply);
        return;
      }
      const match = url.pathname.match(
        /^\/v1\/nexus\/realtime\/sessions\/(session-\d+)\/cancellations$/u,
      );
      assert(match);
      const s = sessions.get(match[1]);
      assert(s);
      parseEvent("RealtimeEvent", {
        type: "nexus.session.close",
        event_id: "validate-mode",
        mode: body.mode,
      });
      assert.deepEqual(Object.keys(body), ["mode"]);
      s.value.status = "draining";
      s.value.cleanup_state = "pending";
      const reply = snapshot(s);
      intents.set(key, { path: url.pathname, body, status: 202, reply });
      if (s.number === 1) {
        lostReply(res, 202);
        return;
      }
      json(res, 202, reply);
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
          message: "Synthetic test only",
          type: "service_unavailable_error",
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
    const mime = {
      ".js": "text/javascript",
      ".css": "text/css",
      ".html": "text/html",
      ".woff2": "font/woff2",
      ".svg": "image/svg+xml",
      ".json": "application/json",
    };
    res.writeHead(200, { "Content-Type": mime[extname(path)] ?? "application/octet-stream" });
    res.end(data);
  } catch (e) {
    serverErrors.push(e.name);
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
    assert.equal([...url.searchParams].length, 1);
    const s = sessions.get(url.searchParams.get("session_id"));
    assert(s && s.value.status === "created");
    wss.handleUpgrade(req, socket, head, (ws) => {
      s.ws = ws;
      sockets.push(ws);
      const created = structuredClone(traces.examples[0].steps[0].value);
      created.session.id = s.value.id;
      send(s, created);
      ws.on("message", (raw) => {
        try {
          assert(raw.length <= 65536);
          const e = parseEvent("RealtimeEvent", JSON.parse(raw.toString()));
          if (e.type === "input_audio_buffer.append") {
            const bytes = Buffer.from(e.audio, "base64");
            assert.equal(bytes.length, 4800);
            assert.equal(s.value.status, "active");
            const before = s.sent;
            s.sent += bytes.length / 2;
            if (s.number === 2) {
              assert.equal(s.sent, 2400);
              return;
            }
            setTimeout(() => {
              if (s.value.status === "active")
                nexus(s, "nexus.input.ack", {
                  accepted_through_sample: s.sent,
                  credit_samples: 24000,
                });
            }, 10);
            if (before === 0) {
              nexus(s, "nexus.transcription.hypothesis", {
                operation_id: `operation-${s.number}-1`,
                segment_id: `segment-${s.number}-1`,
                revision: 1,
                text: "错误预览",
                sample_span: { start: 0, end: s.sent },
              });
              nexus(s, "nexus.transcription.hypothesis", {
                operation_id: `operation-${s.number}-1`,
                segment_id: `segment-${s.number}-1`,
                revision: 2,
                text: "<b>合成预览</b>",
                sample_span: { start: 0, end: s.sent },
              });
            }
          } else if (e.type === "input_audio_buffer.commit") {
            assert(s.sent > s.segmentStart);
            const n = ++s.commits,
              id = `item-${s.number}-${n}`,
              segment = `segment-${s.number}-${n}`,
              operation = `operation-${s.number}-${n}`;
            send(s, {
              type: "input_audio_buffer.committed",
              event_id: `committed-${id}`,
              item_id: id,
              previous_item_id: n === 1 ? null : `item-${s.number}-${n - 1}`,
            });
            nexus(s, "nexus.transcription.segment_closed", {
              operation_id: operation,
              segment_id: segment,
              item_id: id,
              content_index: 0,
              reason: "client_commit",
              sample_span: { start: s.segmentStart, end: s.sent },
            });
            s.segmentStart = s.sent;
            // Distinct operations may finish out of input order; empty final is also immutable.
            if (n === 2) {
              for (const finished of [2, 1])
                send(s, {
                  type: "conversation.item.input_audio_transcription.completed",
                  event_id: `final-item-${s.number}-${finished}`,
                  item_id: `item-${s.number}-${finished}`,
                  content_index: 0,
                  transcript: finished === 1 ? "<script>合成最终正文</script>\nUnicode 🗣️" : "",
                  usage: { type: "duration", seconds: 0.1 },
                });
              nexus(s, "nexus.transcription.metadata", {
                operation_id: `operation-${s.number}-1`,
                segment_id: `segment-${s.number}-1`,
                text_revision: 999,
                metadata_revision: 1,
                alignment: { status: "not_requested" },
                diarization: { status: "not_requested" },
              });
              for (const finished of [1, 2]) {
                const operation = `operation-${s.number}-${finished}`,
                  status = finished === 1 ? "failed" : "cancelled",
                  cleanup_state = finished === 1 ? "quarantined" : "complete";
                nexus(s, "nexus.operation.done", {
                  operation_id: operation,
                  kind: "asr",
                  status,
                  cleanup_state,
                  ...(finished === 1 ? { reason: "required_auxiliary_failed" } : {}),
                });
                s.value.terminal_operations.push({
                  operation_id: operation,
                  kind: "asr",
                  status,
                  cleanup_state,
                  receipt_id: null,
                });
              }
            }
          } else assert.fail("Unexpected synthetic client event");
        } catch (e) {
          serverErrors.push(e.name);
          ws.close(1002);
        }
      });
    });
  } catch (e) {
    serverErrors.push(e.name);
    socket.destroy();
  }
});
await new Promise((r) => server.listen(0, "127.0.0.1", r));
origin = `http://127.0.0.1:${server.address().port}`;
const browser = await chromium.launch({
  headless: true,
  args: ["--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream"],
});
const context = await browser.newContext({
    permissions: ["microphone"],
    viewport: { width: 1280, height: 960 },
  }),
  page = await context.newPage();
await context.addInitScript((cleanupMode) => {
  window.__realtimeMedia = { streams: [], contexts: [] };
  const get = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
  navigator.mediaDevices.getUserMedia = async function (options) {
    const stream = await get(options);
    window.__realtimeMedia.streams.push(stream);
    return stream;
  };
  window.AudioContext = new Proxy(window.AudioContext, {
    construct(target, args) {
      const context = Reflect.construct(target, args);
      window.__realtimeMedia.contexts.push(context);
      if (cleanupMode) {
        const close = context.close.bind(context);
        context.close = () => {
          window.__captureCloseCalls = (window.__captureCloseCalls ?? 0) + 1;
          return new Promise((resolve, reject) => {
            window.__captureRelease = () => {
              if (cleanupMode === "rejected") reject(new Error("synthetic capture close failure"));
              else close().then(resolve, reject);
            };
          });
        };
      }
      return context;
    },
  });
}, cleanupMode);
await context.route("**/*", (route) => {
  if (new URL(route.request().url()).origin !== origin) {
    outbound.push("external");
    return route.abort();
  }
  return route.continue();
});
page.on("websocket", (ws) => {
  if (new URL(ws.url()).host !== new URL(origin).host) outbound.push("external-ws");
});
page.on("pageerror", (e) => errors.push(e.name));
page.on("console", (m) => {
  if (m.type() === "error" && !m.text().startsWith("Failed to load resource:"))
    errors.push("console");
});
const enter = async () => {
  await page.getByRole("button", { name: "实时字幕", exact: true }).click();
  await expect(page.getByRole("region", { name: "实时字幕工作台" })).toBeVisible();
};
const create = async () => {
  for (const [label, value] of [
    ["会话预设编号", "profile-demo"],
    ["预设版本", "1"],
    ["实时转写模型编号", "asr-quality"],
    ["模型版本", "1"],
  ])
    await page.getByLabel(label, { exact: true }).fill(value);
  await page.getByRole("button", { name: "创建实时会话", exact: true }).click();
};
const connect = async (s) => {
  await page.getByRole("button", { name: "连接此会话" }).click();
  await expect.poll(() => s.ws?.readyState).toBe(1);
  await expect(page.getByRole("button", { name: "开始麦克风采集" })).toBeDisabled();
  admit(s);
  await expect(page.getByRole("button", { name: "开始麦克风采集" })).toBeEnabled();
};
const mediaStopped = async () => {
  await expect
    .poll(() =>
      page.evaluate(
        () =>
          window.__realtimeMedia.streams.every((s) =>
            s.getTracks().every((t) => t.readyState === "ended"),
          ) && window.__realtimeMedia.contexts.every((c) => c.state === "closed"),
      ),
    )
    .toBe(true);
};
try {
  await page.goto(origin);
  await enter();
  await expect(page.getByLabel("会话预设编号")).toHaveValue("");
  await create();
  await expect(page.getByRole("alert")).toContainText("提交结果尚未确认");
  await page.reload();
  await enter();
  assert.equal(writes.length, 1);
  await page.getByRole("button", { name: "按原键重放" }).click();
  await expect(page.getByText("session-1", { exact: true })).toBeVisible();
  assert.equal(writes[0].key, writes[1].key);
  assert.deepEqual(writes[0].body, writes[1].body);
  const first = sessions.get("session-1");
  await connect(first);
  await page.getByRole("button", { name: "开始麦克风采集" }).click();
  await expect(page.getByText("<b>合成预览</b>", { exact: true })).toBeVisible();
  if (cleanupMode) {
    if (stopKind === "interruption")
      await page.evaluate(() => window.__realtimeMedia.contexts[0].suspend());
    else await page.getByRole("button", { name: "停止采集并中止" }).click();
    await expect(page.getByRole("alert")).toContainText("提交结果尚未确认");
    await expect(page.getByTestId("asr-microphone-state")).toContainText("正在停止");
    await expect
      .poll(() =>
        page.evaluate(
          () =>
            window.__realtimeMedia.streams.length === 1 &&
            window.__realtimeMedia.streams[0].getTracks().every((t) => t.readyState === "ended"),
        ),
      )
      .toBe(true);
    assert.equal(
      await page.evaluate(() => window.__realtimeMedia.contexts[0].state),
      stopKind === "interruption" ? "suspended" : "running",
    );
    assert.equal(await page.evaluate(() => window.__captureCloseCalls), 1);
    await page.getByRole("button", { name: "按原键重放" }).click();
    await expect(page.getByText(/正在结束.*接受时快照/)).toBeVisible();
    assert.equal(writes.at(-1).key, writes.at(-2).key);
    assert.deepEqual(writes.at(-1).body, writes.at(-2).body);
    complete(first);
    await page.getByRole("button", { name: "读取当前会话状态" }).click();
    await expect(page.getByText("已关闭 · 清理完成", { exact: true })).toBeVisible();
    await expect(page.getByTestId("asr-microphone-state")).toContainText("正在停止");
    await expect(page.getByRole("button", { name: "创建实时会话", exact: true })).toBeDisabled();
    const sentAtStop = first.sent;
    const cancellations = writes.filter((w) => w.path.endsWith("/cancellations")).length;
    await page.evaluate(() => window.__captureRelease());
    if (cleanupMode === "delayed") {
      await expect(page.getByTestId("asr-microphone-state")).toHaveText("已停止");
      await expect(page.getByRole("button", { name: "创建实时会话", exact: true })).toBeEnabled();
      await mediaStopped();
    } else {
      await expect(page.getByRole("alert")).toContainText("释放未确认");
      await page.getByRole("button", { name: "读取当前会话状态" }).click();
      await expect(page.getByRole("alert")).toContainText("释放未确认");
      await expect(page.getByRole("button", { name: "创建实时会话", exact: true })).toBeDisabled();
      assert.equal(
        await page.evaluate(() => window.__realtimeMedia.contexts[0].state),
        stopKind === "interruption" ? "suspended" : "running",
      );
    }
    assert.equal(first.sent, sentAtStop);
    assert.equal(writes.filter((w) => w.path.endsWith("/cancellations")).length, cancellations);
    assert.equal(await page.evaluate(() => window.__captureCloseCalls), 1);
    assert.deepEqual(errors, []);
    assert.deepEqual(serverErrors, []);
    assert.deepEqual(outbound, []);
    await page.screenshot({ path: resolve(output, "cleanup.png"), fullPage: true });
    const result = {
      passed: true,
      cleanup_mode: cleanupMode,
      stop_kind: stopKind,
      synthetic_microphone: true,
      synthetic_rest_ws: true,
      production_bundle: true,
      actual_audio_worklet: true,
      native_close_fault_injection: true,
      physical_device: false,
      close_calls: 1,
      sent_at_stop: sentAtStop,
      no_late_audio_sent: true,
      no_redundant_remote_cancel: true,
      errors,
      serverErrors,
      outbound,
    };
    await writeFile(resolve(output, "result.json"), JSON.stringify(result, null, 2));
    process.stdout.write(JSON.stringify(result) + "\n");
  } else {
    assert.equal(await page.locator(".realtime-segment b").count(), 0);
    await page.getByRole("button", { name: "提交当前语音段" }).evaluate((button) => {
      button.click();
      button.click();
    });
    await expect.poll(() => first.commits).toBe(1);
    assert(!writes.some((w) => w.path.endsWith("/cancellations")));
    await expect.poll(() => first.sent > first.segmentStart).toBe(true);
    await page.getByRole("button", { name: "提交当前语音段" }).click();
    await expect(page.getByText(/整体执行失败/)).toBeVisible();
    await expect(page.getByText(/辅助结果正文版本待核验/)).toBeVisible();
    await expect(page.getByLabel("本标签页最终正文草稿")).toHaveValue(
      "<script>合成最终正文</script>\nUnicode 🗣️\n",
    );
    assert.equal(await page.locator(".realtime-segment script").count(), 0);
    await expect(page.getByText("正文已完成（空文本）")).toBeVisible();
    await page.getByRole("button", { name: "停止采集并排空" }).click();
    await expect(page.getByRole("alert")).toContainText("提交结果尚未确认");
    await mediaStopped();
    assert.equal(first.ws.readyState, 1);
    const before = writes.length;
    await page.evaluate(() => {
      window.__realtimeOriginal = sessionStorage.getItem("speechnexus.workbench.realtime-asr.v1");
      sessionStorage.setItem("speechnexus.workbench.realtime-asr.v1", "corrupt");
    });
    await page.getByRole("button", { name: "按原键重放" }).click();
    await expect(page.getByRole("alert")).toContainText("恢复信息未能保存");
    assert.equal(writes.length, before);
    await page.evaluate(() =>
      sessionStorage.setItem("speechnexus.workbench.realtime-asr.v1", window.__realtimeOriginal),
    );
    await page.getByRole("button", { name: "重试保存恢复信息" }).click();
    await page.getByRole("button", { name: "按原键重放" }).click();
    await expect(page.getByText(/正在结束.*接受时快照/)).toBeVisible();
    assert.equal(writes.at(-1).key, writes.at(-2).key);
    await page.getByRole("button", { name: "读取当前会话状态" }).click();
    await expect(page.getByText("正在结束 · 清理待确认", { exact: true })).toBeVisible();
    const drainKey = writes.at(-1).key;
    await page.getByRole("button", { name: "停止采集并中止" }).click();
    await expect(page.getByRole("alert")).toContainText("提交结果尚未确认");
    const abortKey = writes.at(-1).key;
    assert.notEqual(abortKey, drainKey);
    assert.deepEqual(writes.at(-1).body, { mode: "abort" });
    await page.reload();
    await enter();
    await page.getByRole("button", { name: "按原键重放" }).click();
    await expect(page.getByText(/正在结束.*接受时快照/)).toBeVisible();
    assert.equal(writes.at(-1).key, abortKey);
    complete(first);
    await page.getByRole("button", { name: "读取当前会话状态" }).click();
    await expect(page.getByText("已关闭 · 清理完成", { exact: true })).toBeVisible();
    const downloadPromise = page.waitForEvent("download");
    await page.getByRole("button", { name: "下载最终正文草稿" }).click();
    const download = await downloadPromise;
    await download.saveAs(resolve(output, "synthetic-final.txt"));
    assert.equal(
      await readFile(resolve(output, "synthetic-final.txt"), "utf8"),
      "<script>合成最终正文</script>\nUnicode 🗣️\n",
    );
    await create();
    await expect(page.getByText("session-2", { exact: true })).toBeVisible();
    const second = sessions.get("session-2");
    await connect(second);
    await page.getByRole("button", { name: "开始麦克风采集" }).click();
    await expect(page.getByRole("alert")).toContainText("麦克风采集已中断", { timeout: 10000 });
    await mediaStopped();
    assert.equal(second.sent, 2400);
    await expect(page.getByText("存在输入或连接缺口")).toBeVisible();
    complete(second);
    await page.getByRole("button", { name: "读取当前会话状态" }).click();
    await expect(page.getByText("已关闭 · 清理完成", { exact: true })).toBeVisible();
    await create();
    await expect(page.getByText("session-3", { exact: true })).toBeVisible();
    const third = sessions.get("session-3");
    await connect(third);
    await page.getByRole("button", { name: "开始麦克风采集" }).click();
    await expect.poll(() => third.sent).toBeGreaterThan(0);
    await page.getByRole("button", { name: "文件转写", exact: true }).click();
    await mediaStopped();
    await expect
      .poll(() => writes.some((w) => w.path.endsWith("/session-3/cancellations")))
      .toBe(true);
    await enter();
    await expect(page.getByText("存在输入或连接缺口")).toBeVisible();
    await expect(page.getByLabel("本标签页最终正文草稿")).toHaveValue(
      "<script>合成最终正文</script>\nUnicode 🗣️\n",
    );
    await expect(page.getByRole("button", { name: "连接此会话" })).toBeDisabled();
    await page.setViewportSize({ width: 360, height: 900 });
    await expect
      .poll(() => page.evaluate(() => document.documentElement.scrollWidth <= innerWidth))
      .toBe(true);
    await page.getByRole("button", { name: "读取当前会话状态" }).focus();
    await expect(page.getByRole("button", { name: "读取当前会话状态" })).toBeFocused();
    await page.keyboard.press("Tab");
    await page.screenshot({ path: resolve(output, "realtime-360.png"), fullPage: true });
    const journal = await page.evaluate(() =>
      JSON.parse(sessionStorage.getItem("speechnexus.workbench.realtime-asr.v1")),
    );
    assert.equal(journal.captions.length, 2);
    assert(!JSON.stringify(journal).includes("input_audio_buffer.append"));
    assert.deepEqual(serverErrors, []);
    assert.deepEqual(errors, []);
    assert.deepEqual(outbound, []);
    await writeFile(
      resolve(output, "result.json"),
      JSON.stringify(
        {
          browser: browser.version(),
          production_bundle: true,
          synthetic_rest_ws: true,
          synthetic_microphone: true,
          physical_device: false,
          sessions: 3,
          writes,
          frames:
            sessions.size > 0
              ? [...sessions.values()].map((s) => ({
                  session: s.value.id,
                  sent_samples: s.sent,
                  commits: s.commits,
                }))
              : [],
          create_original_key_replayed: true,
          cancel_original_key_replayed: true,
          abort_upgrade_original_replayed: true,
          repeated_empty_commit_kept_session: true,
          reversed_finals_ordered_by_input: true,
          replay_storage_cas_blocked: true,
          credit_exhaustion_stopped: true,
          drain_kept_socket: true,
          navigation_released_media: true,
          immutable_final_empty_and_required_failure: true,
          auxiliary_version_unverified: true,
          download_exact: true,
          viewport360: true,
          basic_focus_tab: true,
          outbound,
          errors,
          serverErrors,
        },
        null,
        2,
      ),
    );
    process.stdout.write(
      JSON.stringify({ passed: true, output, writes: writes.length, sessions: sessions.size }) +
        "\n",
    );
  }
} catch (error) {
  await writeFile(
    resolve(output, "failure.json"),
    JSON.stringify(
      {
        error: String(error),
        errors,
        serverErrors,
        outbound,
        cleanup_mode: cleanupMode,
        body: await page.locator("body").innerText(),
      },
      null,
      2,
    ),
  );
  throw error;
} finally {
  await context.close();
  await browser.close();
  for (const ws of sockets) ws.terminate();
  await new Promise((r) => wss.close(r));
  await new Promise((r) => server.close(r));
}
