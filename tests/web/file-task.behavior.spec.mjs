import { test, expect } from "@playwright/test";
import { readFileSync } from "node:fs";

import { lockToPreview, expectNetworkLocked } from "./network-lock.mjs";

/**
 * Browser-level specs for the file task panel's state transitions (#740).
 *
 * The panel is composed in the production bundle only (the `?dev-scenario` demo is
 * gated on `import.meta.env.DEV` and never reaches `vite preview`, and that demo does
 * not render `FileTaskPanel` at all). So these specs drive the real `FileTaskHost` in
 * the preview build and replace only the HTTP boundary: every `/v1/nexus/**` request
 * is answered from the committed contract fixtures, which lets a spec script the exact
 * answer sequence (lost response, 413, cancel refusal, unknown outcome) without a
 * backend and without touching product code.
 *
 * The public protocol itself is still exercised in the page: job commands are built by
 * the real client, answered payloads are parsed by the generated contract parser, and a
 * malformed shape would fail the test because the page rejects it.
 */

const workbench = "http://127.0.0.1:4174";
const journalKey = "speechnexus.workbench.asr-task.v1";

const fixtures = JSON.parse(
  readFileSync(new URL("../fixtures/contracts/file-workflow.json", import.meta.url), "utf8"),
);

const example = (id) => structuredClone(fixtures.examples.find((entry) => entry.id === id).value);

/** A contract-valid refusal envelope; the page parses it with the generated parser. */
function refusal(status, code) {
  return {
    error: {
      code,
      type: status >= 500 ? "service_unavailable_error" : "invalid_request_error",
      message: `Synthetic ${status} ${code} for the behavior gate`,
      param: null,
      request_id: "req-behavior",
      retryable: status >= 500,
    },
  };
}

/** Synthetic 24 kHz mono WAV; the bytes only need to be a stable upload identity. */
function syntheticWav() {
  const bytes = Buffer.alloc(44 + 512);
  bytes.write("RIFF", 0, "ascii");
  bytes.writeUInt32LE(bytes.length - 8, 4);
  bytes.write("WAVEfmt ", 8, "ascii");
  bytes.writeUInt32LE(16, 16);
  bytes.writeUInt16LE(1, 20);
  bytes.writeUInt16LE(1, 22);
  bytes.writeUInt32LE(24000, 24);
  bytes.writeUInt32LE(48000, 28);
  bytes.writeUInt16LE(2, 32);
  bytes.writeUInt16LE(16, 34);
  bytes.write("data", 36, "ascii");
  bytes.writeUInt32LE(512, 40);
  return bytes;
}

/**
 * Install the scripted HTTP boundary. `create`/`cancel` answer per test; anything else
 * the page asks for is recorded and refused, so an unexpected request is a loud failure
 * instead of a silent network call.
 */
async function installBackend(page) {
  const calls = { assets: 0, creates: [], cancels: [], unexpected: [] };
  const plan = {
    create: (route) => route.fulfill({ status: 202, json: example("queued-job") }),
    cancel: (route) => route.fulfill({ status: 202, json: example("cancel-job") }),
  };
  await page.route("**/v1/nexus/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path === "/v1/nexus/assets" && request.method() === "POST") {
      calls.assets += 1;
      await route.fulfill({ status: 201, json: example("ready-audio") });
      return;
    }
    if (path === "/v1/nexus/jobs" && request.method() === "POST") {
      calls.creates.push(request.headers()["idempotency-key"] ?? "");
      await plan.create(route);
      return;
    }
    if (path === "/v1/nexus/jobs/job-demo" && request.method() === "GET") {
      await route.fulfill({ status: 200, json: example("queued-job") });
      return;
    }
    if (path === "/v1/nexus/jobs/job-demo/cancellations" && request.method() === "POST") {
      calls.cancels.push(request.headers()["idempotency-key"] ?? "");
      await plan.cancel(route);
      return;
    }
    calls.unexpected.push(`${request.method()} ${path}`);
    await route.fulfill({ status: 500, json: refusal(500, "internal_error") });
  });
  return { calls, plan };
}

async function openWorkbench(page) {
  await page.goto(`${workbench}/`);
  await expect(page.getByRole("heading", { name: "文件转写" })).toBeVisible();
  await expect(page.locator("#file-task-model")).toBeEnabled();
}

async function submitFileTask(page) {
  await page.locator("#file-task-model").fill("asr-quality");
  await page.locator("#file-task-input").setInputFiles({
    name: "synthetic-silence.wav",
    mimeType: "audio/wav",
    buffer: syntheticWav(),
  });
  await page.getByRole("button", { name: "上传并转写" }).click();
}

const panel = (page) => page.locator(".file-task-detail");
const notice = (page) => page.locator(".file-task-detail > p[role=status]").first();
const unknownAlert = (page) => page.locator(".file-task-detail [role=alert]");
const finishButton = (page) => page.getByRole("button", { name: "准备新任务", exact: true });
const replayButton = (page) => page.getByRole("button", { name: "按原键重放" });
const journal = (page) => page.evaluate((key) => sessionStorage.getItem(key), journalKey);

test.beforeEach(async ({ context }) => {
  await lockToPreview(context);
});

test("413 首答：显示已作废而非结果未知，两步确认后输入解锁且幂等键换新", async ({ page }) => {
  const backend = await installBackend(page);
  backend.plan.create = (route) =>
    backend.calls.creates.length === 1
      ? route.fulfill({ status: 413, json: refusal(413, "job_request_too_large") })
      : route.fulfill({ status: 202, json: example("queued-job") });

  await openWorkbench(page);
  await expectNetworkLocked(page);
  await submitFileTask(page);

  // Third state (#731): the refusal is about these bytes, so the page must not claim
  // the outcome is unconfirmed, yet it may not claim "nothing executed" either.
  await expect(panel(page).getByText("原请求因请求体超过本部署的接收上限被拒绝")).toBeVisible();
  await expect(unknownAlert(page)).toHaveCount(0);
  await expect(panel(page).getByText("请求结果尚未确认")).toHaveCount(0);
  await expect(replayButton(page)).toBeDisabled();
  await expect(finishButton(page)).toBeVisible();
  await expect(page.locator("#file-task-model")).toBeDisabled();

  // The abandonment is a real second step: opening it alone must not end the record.
  await finishButton(page).click();
  const confirmFinish = page.getByRole("group", { name: "确认本页新任务" });
  await expect(confirmFinish).toBeVisible();
  await expect(confirmFinish.getByRole("button", { name: "保留本页任务" })).toBeVisible();
  await expect(page.locator("#file-task-model")).toBeDisabled();
  await expect(page.getByTestId("file-job-status")).toHaveText("尚未确认");

  await confirmFinish.getByRole("button", { name: "放弃这次意图并新建" }).click();
  await expect(page.getByRole("group", { name: "确认本页新任务" })).toHaveCount(0);
  await expect(page.locator("#file-task-model")).toBeEnabled();
  await expect(page.locator("#file-task-model")).toHaveValue("");
  await expect(page.locator("#file-task-input")).toBeEnabled();
  expect(await journal(page)).toBeNull();

  // The new intent must not reuse the spent idempotency key.
  await submitFileTask(page);
  await expect(page.getByTestId("file-job-status")).toHaveText("queued");
  expect(backend.calls.creates).toHaveLength(2);
  expect(backend.calls.creates[0]).toMatch(/^[0-9a-f-]{16,}$/i);
  expect(backend.calls.creates[1]).not.toBe(backend.calls.creates[0]);
  expect(backend.calls.unexpected).toEqual([]);
});

test("413 非首答：仍显示未知且仍不可放弃，按原键重放只复用原键", async ({ page }) => {
  const backend = await installBackend(page);
  backend.plan.create = (route) =>
    backend.calls.creates.length === 1
      ? route.abort("failed")
      : route.fulfill({ status: 413, json: refusal(413, "job_request_too_large") });

  await openWorkbench(page);
  await expectNetworkLocked(page);
  await submitFileTask(page);

  // A lost answer leaves the original intent unknown and replayable.
  await expect(panel(page).getByText("请求结果尚未确认")).toBeVisible();
  await expect(finishButton(page)).toHaveCount(0);
  await expect(replayButton(page)).toBeEnabled();

  await replayButton(page).click();

  // #729's guard: a 413 answered before the key lookup says nothing about the earlier
  // attempt, so it may not settle the intent, offer abandonment, or lose the replay.
  await expect(panel(page).getByText("该意图此前已有提交尝试")).toBeVisible();
  await expect(panel(page).getByText("请求结果尚未确认")).toBeVisible();
  await expect(finishButton(page)).toHaveCount(0);
  await expect(replayButton(page)).toBeEnabled();
  expect(backend.calls.creates).toHaveLength(2);
  expect(backend.calls.creates[1]).toBe(backend.calls.creates[0]);
  expect(backend.calls.unexpected).toEqual([]);
});

test("取消被确定拒绝：可放弃，且刷新后仍是作废态而不复活成未知", async ({ page }) => {
  const backend = await installBackend(page);
  backend.plan.cancel = (route) =>
    route.fulfill({ status: 409, json: refusal(409, "execution_cleanup_pending") });

  await openWorkbench(page);
  await expectNetworkLocked(page);
  await submitFileTask(page);
  await expect(page.getByTestId("file-job-status")).toHaveText("queued");

  await page.getByRole("button", { name: "准备取消" }).click();
  const confirmCancel = page.getByRole("group", { name: "确认取消原任务" });
  await expect(confirmCancel).toBeVisible();
  await confirmCancel.getByRole("button", { name: "确认取消 job-demo" }).click();

  // The refusal answers the cancellation request (#723): the attempt is spent and the
  // page may offer explicit abandonment, while the Job itself stays readable.
  await expect(panel(page).getByText("原请求已被协议确定性拒绝，原意图作废")).toBeVisible();
  await expect(unknownAlert(page)).toHaveCount(0);
  await expect(finishButton(page)).toBeVisible();
  expect(backend.calls.cancels).toHaveLength(1);

  // The verdict is persisted before it is shown, so a reload keeps it instead of
  // rebuilding "result unconfirmed" from a pending record.
  await page.reload();
  await expect(page.getByRole("heading", { name: "文件转写" })).toBeVisible();
  await expect(panel(page).getByText("原请求已被协议确定性拒绝，原意图作废")).toBeVisible();
  await expect(panel(page).getByText("刷新也不会再变回")).toBeVisible();
  await expect(notice(page)).toContainText("该取消请求已被协议确定性拒绝并作废");
  await expect(panel(page).getByText("请求结果尚未确认")).toHaveCount(0);
  await expect(finishButton(page)).toBeVisible();

  await finishButton(page).click();
  const confirmFinish = page.getByRole("group", { name: "确认本页新任务" });
  await expect(confirmFinish).toBeVisible();
  await confirmFinish.getByRole("button", { name: "放弃这次意图并新建" }).click();
  await expect(page.getByRole("group", { name: "确认本页新任务" })).toHaveCount(0);
  expect(await journal(page)).toBeNull();
  expect(backend.calls.unexpected).toEqual([]);
});

test("未知结果（503 job_outcome_unknown）：显示未知、禁止新建、可按原键重放", async ({ page }) => {
  const backend = await installBackend(page);
  backend.plan.create = (route) =>
    backend.calls.creates.length === 1
      ? route.fulfill({ status: 503, json: refusal(503, "job_outcome_unknown") })
      : route.fulfill({ status: 202, json: example("queued-job") });

  await openWorkbench(page);
  await expectNetworkLocked(page);
  await submitFileTask(page);

  await expect(panel(page).getByText("请求结果尚未确认")).toBeVisible();
  await expect(finishButton(page)).toHaveCount(0);
  await expect(replayButton(page)).toBeEnabled();
  await expect(page.locator("#file-task-model")).toBeDisabled();

  // Replay reconciles by the original key; it never starts a second intent.
  await replayButton(page).click();
  await expect(page.getByTestId("file-job-status")).toHaveText("queued");
  await expect(panel(page).getByText("请求结果尚未确认")).toHaveCount(0);
  expect(backend.calls.creates).toHaveLength(2);
  expect(backend.calls.creates[1]).toBe(backend.calls.creates[0]);
  expect(backend.calls.unexpected).toEqual([]);
});
