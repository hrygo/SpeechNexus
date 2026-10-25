/** One-shot production bundle + synthetic public HTTP. No models or private audio. */
import { createServer } from "node:http";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { resolve, sep, extname } from "node:path";
import { parseArgs } from "node:util";
import assert from "node:assert/strict";
import { chromium, expect } from "@playwright/test";
import { parse } from "../../packages/contracts/src/runtime.mjs";

const { values } = parseArgs({ options: { output: { type: "string" } } });
assert(values.output);
const output = resolve(values.output),
  dist = resolve("web/workbench/dist");
await mkdir(output, { recursive: true });
const fixtures = JSON.parse(await readFile("tests/fixtures/contracts/file-workflow.json", "utf8"));
const sample = (id) => structuredClone(fixtures.examples.find((x) => x.id === id).value);
const jobs = new Map(),
  intents = new Map(),
  cancelIntents = new Map(),
  writes = [],
  outbound = [],
  errors = [],
  serverErrors = [];
let operationGets = 0;
let origin,
  lostCreate = false,
  lostCancel = false;
function json(res, value, status = 200) {
  res.writeHead(status, { "Content-Type": "application/json" });
  res.end(JSON.stringify(value));
}
function lost(res) {
  res.writeHead(202, { "Content-Type": "application/json", "Content-Length": 10000 });
  res.flushHeaders();
  res.write('{"synthetic":');
  setTimeout(() => res.destroy(), 20);
}
const server = createServer(async (req, res) => {
  try {
    const path = new URL(req.url, origin).pathname;
    if (path === "/v1/nexus/jobs" && req.method === "POST") {
      const chunks = [];
      for await (const chunk of req) chunks.push(chunk);
      const body = JSON.parse(Buffer.concat(chunks).toString());
      parse("JobCreateRequest", body);
      assert(["voice_design", "voice_clone"].includes(body.kind));
      const key = req.headers["idempotency-key"];
      assert(key);
      writes.push({ path, key, body });
      if (body.model === "unsupported-model") {
        json(
          res,
          parse("ErrorResponse", {
            error: {
              code: "unsupported_parameter",
              type: "invalid_request_error",
              message: "PRIVATE_PROVIDER_BODY",
              param: "model",
              request_id: "req-rejected",
              retryable: false,
            },
          }),
          422,
        );
        return;
      }
      let accepted = intents.get(key);
      if (accepted) assert.deepEqual(accepted.body, body);
      else {
        const job = sample("queued-job");
        job.id = `job-${jobs.size + 1}`;
        job.kind = body.kind;
        job.progress = { unit: "items", completed: 0, total: 1 };
        job.effective_configuration.models = { voice: { id: body.model, revision: "1" } };
        parse("Job", job);
        accepted = { body, job };
        intents.set(key, accepted);
        jobs.set(job.id, structuredClone(job));
      }
      if (body.model === "operation-model") {
        json(
          res,
          parse("ErrorResponse", {
            error: {
              code: "response_not_replayable",
              type: "invalid_request_error",
              message: "PRIVATE_PROVIDER_BODY",
              param: null,
              request_id: "req-operation",
              retryable: false,
              nexus: {
                operation_id: "op-original",
                receipt: {
                  receipt_id: "receipt-original",
                  requested: {},
                  effective: {},
                  observed: {},
                  missing: [],
                  unknown: [],
                },
              },
            },
          }),
          409,
        );
      } else if (!lostCreate) {
        lostCreate = true;
        lost(res);
      } else json(res, accepted.job, 202);
      return;
    }
    if (path === "/v1/nexus/operations/op-original" && req.method === "GET") {
      operationGets++;
      json(
        res,
        parse("Operation", {
          id: "op-original",
          object: "nexus.operation",
          kind: "voice_preview",
          status: "running",
          cleanup_state: "pending",
          execution_revision: "1",
          job_id: "job-4",
          created_at: "2026-10-24T00:00:00Z",
          updated_at: "2026-10-24T00:00:00Z",
        }),
      );
      return;
    }
    const get = /^\/v1\/nexus\/jobs\/(job-\d+)$/.exec(path);
    if (get && req.method === "GET") {
      assert(jobs.has(get[1]));
      json(res, parse("Job", jobs.get(get[1])));
      return;
    }
    const cancel = /^\/v1\/nexus\/jobs\/(job-\d+)\/cancellations$/.exec(path);
    if (cancel && req.method === "POST") {
      const key = req.headers["idempotency-key"];
      assert(key);
      writes.push({ path, key });
      let accepted = cancelIntents.get(key);
      if (accepted) assert.equal(accepted.job_id, cancel[1]);
      else {
        accepted = {
          job_id: cancel[1],
          accepted: true,
          status: "cancelling",
          cleanup_state: "pending",
          cancellation_requested_at: "2026-10-24T00:00:00Z",
          operations: [],
        };
        parse("JobCancellation", accepted);
        cancelIntents.set(key, accepted);
      }
      if (!lostCancel) {
        lostCancel = true;
        Object.assign(jobs.get(cancel[1]), { status: "cancelling" });
        lost(res);
      } else {
        Object.assign(jobs.get(cancel[1]), { status: "cancelled", cleanup_state: "complete" });
        json(res, accepted, 202);
      }
      return;
    }
    if (path.startsWith("/v1/nexus/assets/")) {
      const asset = sample("ready-audio");
      asset.id = path.split("/").at(-1);
      asset.purpose = asset.id === "wrong-purpose" ? "asr_input" : "voice_reference";
      json(res, parse("Asset", asset));
      return;
    }
    const file = path === "/" ? resolve(dist, "index.html") : resolve(dist, "." + path);
    if (!file.startsWith(dist + sep)) {
      res.writeHead(404);
      res.end();
      return;
    }
    const type =
      { ".html": "text/html", ".js": "text/javascript", ".css": "text/css" }[extname(file)] ??
      "application/octet-stream";
    res.writeHead(200, { "Content-Type": type });
    res.end(await readFile(file));
  } catch (e) {
    serverErrors.push(String(e));
    if (!res.headersSent) res.writeHead(500);
    res.end();
  }
});
await new Promise((r) => server.listen(0, "127.0.0.1", r));
origin = `http://127.0.0.1:${server.address().port}`;
let browser;
try {
  browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({ viewport: { width: 360, height: 900 } });
  await context.route("**/*", (route) => {
    if (new URL(route.request().url()).origin !== origin) {
      outbound.push(route.request().url());
      return route.abort();
    }
    return route.continue();
  });
  const page = await context.newPage();
  page.on("pageerror", (e) => errors.push(String(e)));
  const open = async () => {
    await page.goto(origin);
    await page.getByRole("button", { name: "音色创作", exact: true }).click();
    await expect(page.getByRole("heading", { name: "音色创作任务" })).toBeVisible();
  };
  await open();
  const text = "  <script>合成设计😀</script>\n保留空白";
  await page.getByLabel("工作空间编号", { exact: true }).fill("ws-default");
  await page.getByLabel("创作模型编号", { exact: true }).fill("voice-design");
  await page.getByLabel("声音描述", { exact: true }).fill(text);
  await page.getByLabel("试听文本", { exact: true }).fill("设计试听");
  await page.getByRole("button", { name: "确认并创建任务", exact: true }).click();
  await expect(page.getByRole("button", { name: "重放原操作", exact: true })).toBeEnabled();
  assert.equal(writes.length, 1);
  await open();
  assert.equal(writes.length, 1);
  await expect(page.getByLabel("声音描述", { exact: true })).toHaveValue(text);
  await page.getByRole("button", { name: "重放原操作", exact: true }).click();
  await expect(page.getByRole("button", { name: "读取当前任务", exact: true })).toBeEnabled();
  assert.deepEqual(writes[0], writes[1]);
  await page.getByRole("button", { name: "读取当前任务", exact: true }).click();
  await expect(page.getByRole("button", { name: "准备取消", exact: true })).toBeEnabled();
  await page.getByRole("button", { name: "准备取消", exact: true }).click();
  await expect(page.getByRole("button", { name: "确认取消此任务", exact: true })).toBeFocused();
  await page.getByRole("button", { name: "确认取消此任务", exact: true }).click();
  await expect(page.getByRole("button", { name: "重放原操作", exact: true })).toBeEnabled();
  await open();
  await page.getByRole("button", { name: "重放原操作", exact: true }).click();
  await expect(page.getByRole("button", { name: "读取当前任务", exact: true })).toBeEnabled();
  assert.deepEqual(writes[2], writes[3]);
  await page.getByRole("button", { name: "读取当前任务", exact: true }).click();
  await expect(page.getByRole("button", { name: "新建下一项任务", exact: true })).toBeEnabled();
  await page.getByRole("button", { name: "新建下一项任务", exact: true }).click();
  await page.getByRole("button", { name: "样本克隆", exact: true }).click();
  await page.getByLabel("创作模型编号", { exact: true }).fill("voice-base");
  await page.getByLabel("参考音频素材编号", { exact: true }).fill("wrong-purpose");
  const confirm = page.getByRole("checkbox", { name: /确认已获准/ });
  await confirm.check();
  await page.getByRole("button", { name: "确认并创建任务", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText("输入或参考素材不可用");
  assert.equal(writes.length, 4);
  await page.getByLabel("参考音频素材编号", { exact: true }).fill("asset-demo");
  await expect(confirm).not.toBeChecked();
  const reference = " 合成参考😀\n";
  await page.getByLabel("参考文本", { exact: true }).fill(reference);
  await confirm.check();
  await page.getByRole("button", { name: "确认并创建任务", exact: true }).click();
  await expect(page.getByRole("button", { name: "读取当前任务", exact: true })).toBeEnabled();
  assert.equal(writes.length, 5);
  assert.equal(writes[4].body.input.reference_text, reference);
  await open();
  await expect(page.getByRole("button", { name: "样本克隆", exact: true })).toHaveAttribute(
    "aria-pressed",
    "true",
  );
  await expect(page.getByLabel("参考文本", { exact: true })).toHaveValue(reference);
  await page.getByRole("button", { name: "读取当前任务", exact: true }).click();
  await expect(page.getByRole("button", { name: "准备取消", exact: true })).toBeEnabled();
  await page.evaluate(() =>
    sessionStorage.setItem("speechnexus.workbench.voice-create.v1", "foreign"),
  );
  await page.getByRole("button", { name: "准备取消", exact: true }).click();
  await page.getByRole("button", { name: "确认取消此任务", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText("已阻止网络写入");
  assert.equal(writes.length, 5);
  await page.keyboard.press("Tab");
  assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
  await page.screenshot({ path: resolve(output, "voice-360.png"), fullPage: true });
  const quota = await context.newPage();
  await quota.goto(origin);
  await quota.getByRole("button", { name: "音色创作", exact: true }).click();
  await quota.evaluate(() => {
    const old = Storage.prototype.setItem;
    Storage.prototype.setItem = function (k, v) {
      if (k === "speechnexus.workbench.voice-create.v1")
        throw new DOMException("quota", "QuotaExceededError");
      return old.call(this, k, v);
    };
  });
  await quota.getByLabel("工作空间编号", { exact: true }).fill("ws-default");
  await quota.getByLabel("创作模型编号", { exact: true }).fill("voice-design");
  await quota.getByLabel("声音描述", { exact: true }).fill("存储失败保留文本");
  await quota.getByLabel("试听文本", { exact: true }).fill("试听");
  await quota.getByRole("button", { name: "确认并创建任务", exact: true }).click();
  await expect(quota.getByRole("alert")).toContainText("已阻止网络写入");
  assert.equal(writes.length, 5);
  await expect(quota.getByLabel("声音描述", { exact: true })).toHaveValue("存储失败保留文本");
  const corrected = await context.newPage();
  corrected.on("pageerror", (e) => errors.push(String(e)));
  await corrected.goto(origin);
  await corrected.getByRole("button", { name: "音色创作", exact: true }).click();
  for (const [name, value] of [
    ["工作空间编号", "ws-default"],
    ["创作模型编号", "unsupported-model"],
    ["声音描述", " 拒绝后原文😀\n"],
    ["试听文本", "试听"],
  ])
    await corrected.getByLabel(name, { exact: true }).fill(value);
  await corrected.getByRole("button", { name: "确认并创建任务", exact: true }).click();
  await expect(corrected.getByRole("alert")).toContainText("unsupported_parameter");
  await expect(corrected.getByLabel("声音描述", { exact: true })).toHaveValue(" 拒绝后原文😀\n");
  await expect(corrected.getByLabel("创作模型编号", { exact: true })).toBeEnabled();
  assert.equal(await corrected.getByText("PRIVATE_PROVIDER_BODY").count(), 0);
  await corrected.getByLabel("创作模型编号", { exact: true }).fill("corrected-model");
  await corrected.getByRole("button", { name: "确认并创建任务", exact: true }).click();
  await expect(corrected.getByRole("button", { name: "读取当前任务", exact: true })).toBeEnabled();
  assert.notEqual(writes[5].key, writes[6].key);
  const recovery = await context.newPage();
  recovery.on("pageerror", (e) => errors.push(String(e)));
  const openRecovery = async () => {
    await recovery.goto(origin);
    await recovery.getByRole("button", { name: "音色创作", exact: true }).click();
  };
  await openRecovery();
  for (const [name, value] of [
    ["工作空间编号", "ws-default"],
    ["创作模型编号", "operation-model"],
    ["声音描述", "原执行恢复"],
    ["试听文本", "试听"],
  ])
    await recovery.getByLabel(name, { exact: true }).fill(value);
  await recovery.getByRole("button", { name: "确认并创建任务", exact: true }).click();
  await expect(recovery.getByText(/原执行编号：op-original/)).toBeVisible();
  assert.equal(operationGets, 0);
  await openRecovery();
  assert.equal(writes.length, 8);
  assert.equal(operationGets, 0);
  await expect(recovery.getByRole("button", { name: "重放原操作", exact: true })).toHaveCount(0);
  await recovery.getByRole("button", { name: "读取当前任务", exact: true }).click();
  await expect(recovery.getByRole("button", { name: "准备取消", exact: true })).toBeEnabled();
  assert.equal(operationGets, 1);
  assert.equal(writes.length, 8);
  assert.equal(jobs.size, 4);
  assert.deepEqual(errors, []);
  assert.deepEqual(outbound, []);
  assert.deepEqual(serverErrors, []);
  const result = {
    browser: browser.version(),
    production_bundle: true,
    synthetic_public_http: true,
    physical_device: false,
    model_execution: false,
    contexts: 1,
    pages: 4,
    created_jobs: 4,
    writes,
    original_create_replayed: true,
    original_cancel_replayed: true,
    clone_material_checked: true,
    consent_reset: true,
    clone_reload_exact: true,
    cas_blocks_cancel: true,
    quota_blocks_post: true,
    first_rejection_correctable: true,
    original_operation_restored_by_explicit_get: true,
    operation_gets: operationGets,
    viewport360: true,
    basic_focus_tab: true,
    outbound,
    errors,
    serverErrors,
  };
  await writeFile(resolve(output, "result.json"), JSON.stringify(result, null, 2) + "\n");
  console.log(
    JSON.stringify({ jobs: jobs.size, writes: writes.length, browser: result.browser, errors: 0 }),
  );
  await context.close();
} finally {
  await browser?.close();
  await new Promise((r) => server.close(r));
}
