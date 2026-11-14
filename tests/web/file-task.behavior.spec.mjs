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
 *
 * Every asserted string is product copy, cited next to the assertion as the single
 * `web/workbench/src/` occurrence that renders it; nothing here is assembled from test
 * vocabulary. Page state is likewise always produced by two or more real actions
 * (submit, read, cancel, reload), never by writing a record or a view field directly.
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
 * Install the scripted HTTP boundary. `create`/`cancel`/`getJob` answer per test (the
 * default read is the queued Job the create answer names); anything else the page asks
 * for is recorded and refused, so an unexpected request is a loud failure instead of a
 * silent network call.
 */
async function installBackend(page) {
  const calls = { assets: 0, creates: [], cancels: [], reads: 0, unexpected: [] };
  const plan = {
    create: (route) => route.fulfill({ status: 202, json: example("queued-job") }),
    cancel: (route) => route.fulfill({ status: 202, json: example("cancel-job") }),
    getJob: (route) => route.fulfill({ status: 200, json: example("queued-job") }),
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
      calls.reads += 1;
      await plan.getJob(route);
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

/** Both real steps of the cancel confirmation: open it, then confirm the named Job. */
async function requestCancel(page) {
  await page.getByRole("button", { name: "准备取消" }).click();
  const confirmCancel = page.getByRole("group", { name: "确认取消原任务" });
  await expect(confirmCancel).toBeVisible();
  await confirmCancel.getByRole("button", { name: "确认取消 job-demo" }).click();
}

const panel = (page) => page.locator(".file-task-detail");
const notice = (page) => page.locator(".file-task-detail > p[role=status]").first();
const unknownAlert = (page) => page.locator(".file-task-detail [role=alert]");
const finishButton = (page) => page.getByRole("button", { name: "准备新任务", exact: true });
const replayButton = (page) => page.getByRole("button", { name: "按原键重放" });
const journal = (page) => page.evaluate((key) => sessionStorage.getItem(key), journalKey);
const journalRecord = async (page) => {
  const raw = await journal(page);
  return raw === null ? null : JSON.parse(raw);
};

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

  // Positive control: the durable record this abandonment deletes must exist first.
  // Without it the post-abandonment null check would keep passing even if the journal
  // key or the default task kind changed -- an assertion that silently becomes vacuous.
  const persistedRefusal = await journalRecord(page);
  expect(persistedRefusal, "the 413 refusal must be persisted before abandonment").not.toBeNull();
  expect(persistedRefusal.settled).toBe("unreplayable");

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

  // Positive control: the reloaded verdict must come from the durable record itself.
  const persistedCancel = await journalRecord(page);
  expect(persistedCancel, "the cancel refusal must be persisted across the reload").not.toBeNull();
  expect(persistedCancel.settled).toBe("terminal");

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

test("成功路径：提交得到 Job，读取后结果与制品可见", async ({ page }) => {
  const backend = await installBackend(page);
  backend.plan.getJob = (route) => route.fulfill({ status: 200, json: example("succeeded-job") });

  await openWorkbench(page);
  await expectNetworkLocked(page);
  await submitFileTask(page);

  // Action one: the create answer names this Job, and the page shows exactly that Job
  // (FileTaskPanel.tsx:118 `任务状态` testid) plus the session's own receipt notice
  // (file-task-session.ts:1015).
  await expect(page.getByTestId("file-job-status")).toHaveText("queued");
  await expect(notice(page)).toHaveText("任务已接收。读取当前状态或连接任务事件。");
  await expect(panel(page).getByRole("heading", { name: "任务与原始结果" })).toBeVisible();

  // Action two: an explicit read of the same Job. The page never polls, so the succeeded
  // state below can only come from this answer (file-task-session.ts:1119).
  await page.getByRole("button", { name: "读取原状态" }).click();
  await expect(page.getByTestId("file-job-status")).toHaveText("succeeded");
  await expect(notice(page)).toHaveText("已重新读取当前任务。");
  expect(backend.calls.reads).toBe(1);

  // The succeeded Job's result is rendered by product code: the transcript artifact
  // section heading (FileTaskPanel.tsx:228), its download policy line (:240), the import
  // action the artifact/receipt match unlocks (:237, canImport at :28-38), and the
  // artifact button built from the Job's own artifact id (:252).
  await expect(panel(page).getByRole("heading", { name: "原始转写制品" })).toBeVisible();
  await expect(
    panel(page).getByText("下载本任务已提交的原始 JSON，单个制品上限 16 MiB。"),
  ).toBeVisible();
  await expect(panel(page).getByRole("button", { name: "导入到项目稿件" })).toBeEnabled();
  await expect(
    panel(page).getByRole("button", { name: "下载原始制品 artifact-demo" }),
  ).toBeEnabled();
  // A succeeded Job whose cleanup is complete offers the explicit end-of-page record
  // (FileTaskPanel.tsx:257-267 `准备新任务`, via canFinishTask at file-task-policy.ts:22-23).
  await expect(finishButton(page)).toBeVisible();
  expect(backend.calls.creates).toHaveLength(1);
  expect(backend.calls.unexpected).toEqual([]);
});

test("成功路径收尾：准备新任务只展开确认，单次点击不结束记录", async ({ page }) => {
  const backend = await installBackend(page);
  backend.plan.getJob = (route) => route.fulfill({ status: 200, json: example("succeeded-job") });

  await openWorkbench(page);
  await expectNetworkLocked(page);
  await submitFileTask(page);
  await page.getByRole("button", { name: "读取原状态" }).click();
  await expect(page.getByTestId("file-job-status")).toHaveText("succeeded");

  const durable = await journal(page);
  expect(durable, "the succeeded Job must already own the page record").not.toBeNull();

  // Step one opens the confirmation group (FileTaskPanel.tsx:268-294, aria-label
  // "确认本页新任务"); it must not end the record, clear the journal, or submit anything.
  await finishButton(page).click();
  const confirmFinish = page.getByRole("group", { name: "确认本页新任务" });
  await expect(confirmFinish).toBeVisible();
  await expect(
    confirmFinish.getByText("结束本页任务恢复并新建？服务器上的原任务与制品继续保留。"),
  ).toBeVisible();
  await expect(confirmFinish.getByRole("button", { name: "保留本页任务" })).toBeVisible();
  expect(await journal(page), "one click may not drop the page record").toBe(durable);
  await expect(page.getByTestId("file-job-status")).toHaveText("succeeded");
  expect(backend.calls.creates, "one click may not submit a new intent").toHaveLength(1);

  // Step two can also be declined without touching the record.
  await confirmFinish.getByRole("button", { name: "保留本页任务" }).click();
  await expect(page.getByRole("group", { name: "确认本页新任务" })).toHaveCount(0);
  await expect(page.getByTestId("file-job-status")).toHaveText("succeeded");
  expect(await journal(page)).toBe(durable);

  // Confirmed, step two ends the record and starts a fresh, empty intent
  // (FileTaskPanel.tsx:288 `结束本页任务并新建`; FileTaskHost.tsx:162-172 clearCompleted
  // plus a new session, whose initial notice is file-task-session.ts:338 "选择输入开始任务。").
  await finishButton(page).click();
  await page
    .getByRole("group", { name: "确认本页新任务" })
    .getByRole("button", { name: "结束本页任务并新建" })
    .click();
  await expect(page.getByRole("group", { name: "确认本页新任务" })).toHaveCount(0);
  expect(await journal(page)).toBeNull();
  await expect(page.locator("#file-task-model")).toBeEnabled();
  await expect(page.locator("#file-task-model")).toHaveValue("");
  await expect(page.locator("#file-task-input")).toBeEnabled();
  await expect(page.getByTestId("file-job-status")).toHaveText("尚未确认");
  await expect(panel(page).getByText("选择输入开始任务。")).toBeVisible();
  expect(backend.calls.unexpected).toEqual([]);
});

test("取消被 409 idempotency_conflict 拒绝：结算为作废，重载保持终态", async ({ page }) => {
  const backend = await installBackend(page);
  backend.plan.cancel = (route) =>
    route.fulfill({ status: 409, json: refusal(409, "idempotency_conflict") });

  await openWorkbench(page);
  await expectNetworkLocked(page);
  await submitFileTask(page);
  await expect(page.getByTestId("file-job-status")).toHaveText("queued");
  await requestCancel(page);

  // The second settlement class at the cancel boundary (the first,
  // 409 `execution_cleanup_pending`, is covered above): this key identifies another
  // cancellation intent, so this attempt is spent while the Job stays readable
  // (file-task-session.ts:241-250 `settlesCancelAttempt`). The page may then say the
  // intent is void (FileTaskPanel.tsx:100) and offer the explicit end (:257).
  await expect(panel(page).getByText("原请求已被协议确定性拒绝，原意图作废")).toBeVisible();
  await expect(unknownAlert(page)).toHaveCount(0);
  await expect(finishButton(page)).toBeVisible();
  await expect(replayButton(page)).toBeDisabled();
  expect(backend.calls.cancels).toHaveLength(1);

  const persisted = await journalRecord(page);
  expect(persisted, "the cancel verdict must be durable before it is shown").not.toBeNull();
  expect(persisted.settled).toBe("terminal");

  // Reload rebuilds the page from the journal alone: the restored notice is the session's
  // own settled-cancel wording (file-task-session.ts:424), no request is made, and both
  // classes of the verdict survive.
  await page.reload();
  await expect(page.getByRole("heading", { name: "文件转写" })).toBeVisible();
  await expect(notice(page)).toHaveText(
    "已找到原任务身份；该取消请求已被协议确定性拒绝并作废。请读取原任务核对，再显式收尾。",
  );
  await expect(panel(page).getByText("原请求已被协议确定性拒绝，原意图作废")).toBeVisible();
  await expect(panel(page).getByText("刷新也不会再变回")).toBeVisible();
  await expect(unknownAlert(page)).toHaveCount(0);
  await expect(finishButton(page)).toBeVisible();
  await expect(replayButton(page)).toBeDisabled();
  expect(backend.calls.creates).toHaveLength(1);
  expect(backend.calls.cancels).toHaveLength(1);
  expect(backend.calls.reads).toBe(0);
  expect(backend.calls.unexpected).toEqual([]);
});

test("结算码之外的 409 不结算：保持未知并按原键重放取消", async ({ page }) => {
  const backend = await installBackend(page);
  // A real 409 from the same public Job error table (backend/.../http/job_errors.py:28-31)
  // that answers the retry operation, not this cancellation key, and carries no
  // `nexus.operation_id`. It is therefore not one of the three answers
  // `settlesCancelAttempt` accepts (file-task-session.ts:241-250) and cannot prove
  // anything about this cancellation request.
  backend.plan.cancel = (route) =>
    backend.calls.cancels.length === 1
      ? route.fulfill({ status: 409, json: refusal(409, "job_retry_not_allowed") })
      : route.fulfill({ status: 202, json: example("cancel-job") });

  await openWorkbench(page);
  await expectNetworkLocked(page);
  await submitFileTask(page);
  await expect(page.getByTestId("file-job-status")).toHaveText("queued");
  await requestCancel(page);

  // The refusal rules out this answer, not the earlier request: the intent keeps its
  // unknown state, its original key and its replay handle (file-task-session.ts:795-805).
  await expect(notice(page)).toHaveText(
    "取消请求被拒绝（409 job_retry_not_allowed），但该拒绝不能证明原取消未生效；保留原键与未知状态，请读取原任务。",
  );
  await expect(
    panel(page).getByText("请求结果尚未确认。请核对原素材或任务；重放只使用原输入和原键。"),
  ).toBeVisible();
  await expect(finishButton(page)).toHaveCount(0);
  await expect(replayButton(page)).toBeEnabled();

  const persisted = await journalRecord(page);
  expect(persisted, "the pending cancellation must stay durable").not.toBeNull();
  expect(persisted.pending).toBe("cancel");
  expect(persisted).not.toHaveProperty("settled");

  // The original key is still the only reconciliation: replaying it moves the page
  // forward without a replacement intent (file-task-session.ts:1161).
  await replayButton(page).click();
  await expect(page.getByTestId("file-job-status")).toHaveText("queued");
  await expect(unknownAlert(page)).toHaveCount(0);
  await expect(notice(page)).toHaveText("已收到取消响应；资源释放以 cleanup_state 为准。");
  expect(backend.calls.cancels).toHaveLength(2);
  expect(backend.calls.cancels[1]).toBe(backend.calls.cancels[0]);
  expect(backend.calls.unexpected).toEqual([]);
});

test("413 首答重载：保持不可重放的作废态，三步显式放弃", async ({ page }) => {
  const backend = await installBackend(page);
  backend.plan.create = (route) =>
    backend.calls.creates.length === 1
      ? route.fulfill({ status: 413, json: refusal(413, "job_request_too_large") })
      : route.fulfill({ status: 202, json: example("queued-job") });

  await openWorkbench(page);
  await expectNetworkLocked(page);
  await submitFileTask(page);

  // The third class (#731) is persisted as `unreplayable`, not `terminal`: the refusal
  // precedes the original key lookup, so it proves nothing about an earlier attempt
  // (file-task-session.ts:717-724). The product shows the verdict only after persisting
  // it, so waiting for the rendered paragraph also waits for the durable write.
  await expect(panel(page).getByText("原请求因请求体超过本部署的接收上限被拒绝")).toBeVisible();
  expect((await journalRecord(page))?.settled).toBe("unreplayable");

  // Reload restores the third class from the journal alone; the restored notice is the
  // session's own unreplayable wording (file-task-session.ts:422).
  await page.reload();
  await expect(page.getByRole("heading", { name: "文件转写" })).toBeVisible();
  await expect(notice(page)).toHaveText(
    "已找到原任务身份；该请求因请求体超过本部署的接收上限被拒绝，原意图作废。",
  );
  await expect(panel(page).getByText("原请求因请求体超过本部署的接收上限被拒绝")).toBeVisible();
  await expect(panel(page).getByText("请求结果尚未确认")).toHaveCount(0);
  await expect(unknownAlert(page)).toHaveCount(0);
  await expect(replayButton(page)).toBeDisabled();
  await expect(page.locator("#file-task-model")).toBeDisabled();
  expect((await journalRecord(page))?.settled).toBe("unreplayable");
  expect(backend.calls.creates, "a reload must not resend the void intent").toHaveLength(1);

  // The restored verdict ends only through the explicit second step: the group's own
  // unreplayable sentence (FileTaskPanel.tsx:272) and its abandon label (:288).
  await finishButton(page).click();
  const confirmFinish = page.getByRole("group", { name: "确认本页新任务" });
  await expect(confirmFinish).toBeVisible();
  await expect(confirmFinish.getByText("这次请求被拒绝，原意图作废")).toBeVisible();
  await expect(confirmFinish.getByRole("button", { name: "保留本页任务" })).toBeVisible();
  expect(await journal(page), "one click may not drop the void record").not.toBeNull();
  await expect(replayButton(page)).toBeDisabled();

  await confirmFinish.getByRole("button", { name: "放弃这次意图并新建" }).click();
  await expect(page.getByRole("group", { name: "确认本页新任务" })).toHaveCount(0);
  expect(await journal(page)).toBeNull();
  await expect(page.locator("#file-task-model")).toBeEnabled();
  await expect(page.locator("#file-task-model")).toHaveValue("");
  await expect(page.getByTestId("file-job-status")).toHaveText("尚未确认");
  expect(backend.calls.unexpected).toEqual([]);
});
