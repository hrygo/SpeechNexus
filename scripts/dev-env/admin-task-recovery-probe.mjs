/** Production Admin + actual loopback HTTP in Chromium. Synthetic metadata, no backend/device. */
import { chromium, expect } from "@playwright/test";
import assert from "node:assert/strict";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { createRequire } from "node:module";
import { dirname, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { parseArgs } from "node:util";
import { parse } from "../../packages/contracts/src/runtime.mjs";

const { values } = parseArgs({
  options: { output: { type: "string" }, screenshot: { type: "string" } },
});
const root = fileURLToPath(new URL("../../", import.meta.url));
const admin = resolve(root, "web/admin");
const fixtures = JSON.parse(
  await readFile(resolve(root, "tests/fixtures/contracts/file-workflow.json"), "utf8"),
);
const example = (id) => structuredClone(fixtures.examples.find((e) => e.id === id).value);
const job = (id) => parse("Job", { ...example("queued-job"), id });
const key = "speechnexus.admin.task-recovery.v1";
let mode = "lost";
let requests = 0;
const cancellations = [],
  events = [],
  held = new Set();
const json = (res, data, status = 200) => {
  res.writeHead(status, { "Content-Type": "application/json", "Cache-Control": "no-store" });
  res.end(JSON.stringify(data));
};
const middleware = (vite) => {
  vite.middlewares.use((req, res, next) => {
    const url = new URL(req.url, "http://127.0.0.1");
    if (!url.pathname.startsWith("/v1/")) return next();
    requests++;
    if (url.pathname === "/v1/nexus/jobs" && req.method === "GET") {
      return json(
        res,
        parse("JobList", {
          object: "nexus.list",
          data: [job("job-demo"), job("job-other")],
          has_more: false,
          next_cursor: null,
        }),
      );
    }
    const match = /^\/v1\/nexus\/jobs\/(job-demo|job-other)(\/cancellations|\/events)?$/.exec(
      url.pathname,
    );
    if (!match) return json(res, {}, 404);
    const id = match[1];
    if (!match[2] && req.method === "GET") return json(res, job(id));
    if (match[2] === "/cancellations" && req.method === "POST") {
      const intent = { id, key: req.headers["idempotency-key"] };
      cancellations.push(intent);
      if (mode === "lost") {
        res.writeHead(200, { "Content-Type": "application/json" });
        res.end("{"); // Original request reached the server; its acknowledgement is unreadable.
        return;
      }
      if (mode === "inflight" || mode === "ack-wait") {
        held.add(res);
        res.on("close", () => held.delete(res));
        return;
      }
      return json(res, parse("JobCancellation", { ...example("cancel-job"), job_id: id }));
    }
    if (match[2] === "/events" && req.method === "GET") {
      const after = req.headers["last-event-id"] ?? null;
      events.push({ id, after });
      if (mode === "expired" && after) {
        const error = example("recover-operation");
        error.error.code = "event_history_expired";
        return json(res, parse("ErrorResponse", error), 410);
      }
      const event = parse("JobEvent", {
        ...example("job.queued"),
        job_id: id,
        id: after ? "9007199254740994" : "9007199254740993",
      });
      res.writeHead(200, { "Content-Type": "text/event-stream", "Cache-Control": "no-store" });
      res.end(`id: ${event.id}\nevent: ${event.type}\ndata: ${JSON.stringify(event)}\n\n`);
      return;
    }
    return json(res, {}, 405);
  });
};
const require = createRequire(resolve(admin, "package.json"));
const { preview } = await import(pathToFileURL(require.resolve("vite")).href);
let server, browser;
try {
  server = await preview({
    root: admin,
    logLevel: "error",
    plugins: [{ name: "team-c-recovery-probe", configurePreviewServer: middleware }],
    preview: { host: "127.0.0.1", port: 0, strictPort: false },
  });
  const address = server.httpServer.address();
  assert(address && typeof address !== "string");
  const origin = `http://127.0.0.1:${address.port}`;
  browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({ reducedMotion: "reduce" });
  const outbound = [],
    errors = [];
  await context.route("**/*", async (route) => {
    if (new URL(route.request().url()).origin === origin) await route.continue();
    else {
      outbound.push("outbound");
      await route.abort();
    }
  });
  context.on("page", (page) => page.on("pageerror", () => errors.push("pageerror")));
  const page = await context.newPage();
  const button = (name) => page.getByRole("button", { name, exact: true });
  const read = async () => {
    await button("读取任务列表").click();
    await button("查看 job-demo").click();
    await expect(page.getByTestId("task-detail-status")).toHaveText("queued");
  };
  const confirm = async () => {
    await button("准备取消").click();
    await expect(button("确认取消本任务")).toBeFocused();
    await page.keyboard.press("Enter");
  };
  const accepted = async () => {
    await expect(page.getByText("accepted=true / cancelling / pending")).toBeVisible();
    // Acceptance can render before the explicit cancellation's follow-up GET.
    // Start recovery measurements only after that operation has reached ready.
    await expect(button("连接任务事件")).toBeEnabled();
  };
  const raw = () => page.evaluate((key) => sessionStorage.getItem(key), key);
  const record = async () => JSON.parse(await raw());
  const reload = async () => {
    const before = requests;
    await page.reload();
    await expect(button("读取任务列表")).toBeVisible();
    await page.waitForLoadState("networkidle");
    assert.equal(requests, before, "recovery must not automatically access any API");
  };
  const reset = async (nextMode) => {
    mode = nextMode;
    await page.evaluate((key) => sessionStorage.removeItem(key), key);
    await reload();
    await read();
  };
  await page.goto(origin);
  await expect(button("读取任务列表")).toBeVisible();
  assert.equal(requests, 0);
  await read();
  await confirm();
  await expect(page.getByRole("region", { name: "取消请求证据" })).toContainText("取消结果未知");
  assert.equal(cancellations.length, 1);
  const original = cancellations[0];
  assert.equal((await record()).cancellation.key, original.key);
  await reload();
  await expect(page.getByText("待核对任务：job-demo")).toBeVisible();
  await expect(page.getByTestId("task-detail-status")).toHaveCount(0);
  await button("读取任务列表").click();
  await button("查看 job-other").click();
  await expect(page.getByTestId("task-detail-id")).toHaveText("job-other");
  await expect(button("准备取消")).toBeDisabled();
  mode = "accepted";
  await button("按原键重放取消").click();
  await accepted();
  assert.deepEqual(cancellations[1], original);
  assert.equal((await record()).cancellation, null);
  await expect(page.getByTestId("task-detail-id")).toHaveText("job-other");
  await reload();
  await expect(page.getByRole("region", { name: "取消请求证据" })).toHaveCount(0);

  await reset("inflight");
  await confirm();
  await expect.poll(() => cancellations.length).toBe(3);
  const inFlight = cancellations[2];
  await reload();
  await expect(page.getByRole("region", { name: "取消请求证据" })).toContainText("取消结果未知");
  assert.equal((await record()).cancellation.key, inFlight.key);
  mode = "accepted";
  await button("按原键重放取消").click();
  await accepted();
  assert.deepEqual(cancellations[3], inFlight);

  await reset("accepted");
  await button("连接任务事件").click();
  await expect(page.getByTestId("task-event-cursor")).toHaveText("9007199254740993");
  await reload();
  await expect(page.getByTestId("task-event-cursor")).toHaveText("9007199254740993");
  await expect(button("连接任务事件")).toBeDisabled();
  await button("重读所选任务").click();
  await expect(page.getByTestId("task-detail-status")).toHaveText("queued");
  await button("连接任务事件").click();
  await expect(page.getByTestId("task-event-cursor")).toHaveText("9007199254740994");
  assert.equal(events.at(-1).after, "9007199254740993");
  mode = "expired";
  await button("连接任务事件").click();
  await expect(page.getByTestId("task-event-state")).toContainText("事件历史已过期，已重读");
  await reload();
  await expect(page.getByTestId("task-event-cursor")).toHaveText("无已确认水位");
  mode = "accepted";
  await button("重读所选任务").click();
  await button("连接任务事件").click();
  await expect(page.getByTestId("task-event-cursor")).toHaveText("9007199254740993");
  assert.equal(events.at(-1).after, null);

  await reset("ack-wait");
  await confirm();
  await expect.poll(() => cancellations.length).toBe(5);
  await page.evaluate(() => {
    Storage.prototype.setItem = () => {
      throw new Error("synthetic-storage-denied");
    };
  });
  await expect.poll(() => held.size).toBe(1);
  for (const res of held)
    json(res, parse("JobCancellation", { ...example("cancel-job"), job_id: "job-demo" }));
  await expect(page.getByTestId("task-recovery-state")).toContainText("已阻止取消与事件推进");
  await expect(page.getByRole("region", { name: "取消请求证据" })).toContainText("取消结果未知");
  await expect(page.getByText("accepted=true / cancelling / pending")).toHaveCount(0);
  assert.equal((await record()).cancellation.key, cancellations[4].key);
  await reload();
  mode = "accepted";
  await button("按原键重放取消").click();
  await accepted();
  assert.deepEqual(cancellations[5], cancellations[4]);

  await reset("accepted");
  const beforeDenied = cancellations.length;
  const saved = await raw();
  await page.evaluate(() => {
    Storage.prototype.setItem = () => {
      throw new Error("synthetic-storage-denied");
    };
  });
  await confirm();
  await expect(page.getByTestId("task-recovery-state")).toContainText("已阻止取消与事件推进");
  assert.equal(cancellations.length, beforeDenied);
  assert.equal(await raw(), saved);

  await reset("accepted");
  const changed = { version: 1, selectedId: "job-other", cursor: null, cancellation: null };
  await page.evaluate(({ key, changed }) => sessionStorage.setItem(key, JSON.stringify(changed)), {
    key,
    changed,
  });
  await confirm();
  await expect(page.getByTestId("task-recovery-state")).toContainText("已被其他页面修改");
  assert.equal(cancellations.length, beforeDenied);
  assert.deepEqual(await record(), changed);
  await reload();
  await expect(page.getByText("待核对任务：job-other")).toBeVisible();
  await page.evaluate(
    (key) => sessionStorage.setItem(key, '{"version":99,"content":"synthetic-do-not-use"}'),
    key,
  );
  await reload();
  await expect(page.getByTestId("task-recovery-state")).toContainText("已阻止取消与事件推进");
  await read();
  await expect(button("准备取消")).toBeDisabled();
  assert.equal(cancellations.length, beforeDenied);
  assert.equal((await record()).version, 99);

  const deniedPage = await context.newPage();
  await deniedPage.addInitScript(() => {
    Object.defineProperty(window, "sessionStorage", {
      get() {
        throw new Error("synthetic-storage-denied");
      },
    });
  });
  await deniedPage.goto(origin);
  await expect(deniedPage.getByTestId("task-recovery-state")).toContainText("已阻止取消与事件推进");
  await deniedPage.close();
  let mobileOverflow = false;
  for (const colorScheme of ["light", "dark"]) {
    await page.emulateMedia({ colorScheme });
    await page.setViewportSize({ width: 320, height: 850 });
    mobileOverflow ||= await page.evaluate(() => document.documentElement.scrollWidth > innerWidth);
  }
  assert.equal(mobileOverflow, false);
  assert.deepEqual(outbound, []);
  assert.deepEqual(errors, []);
  const result = {
    checkedAt: new Date().toISOString(),
    browser: browser.version(),
    productionBundle: true,
    noAutomaticRecoveryRequests: true,
    lostCancelOriginalKeyAndTarget: true,
    inflightReloadOriginalKey: true,
    acknowledgementSaveFailureUnknown: true,
    largeCursorReloadResume: true,
    expiredCursorClearedAcrossReload: true,
    deniedWritePreventedPost: true,
    changedOwnerPreserved: true,
    corruptStoragePreserved: true,
    deniedReadNoRenderCrash: true,
    keyboardConfirmation: true,
    cancellations: cancellations.length,
    mobileOverflow,
    outboundRequests: outbound.length,
    pageErrors: errors.length,
  };
  if (values.screenshot) {
    await mkdir(dirname(resolve(values.screenshot)), { recursive: true });
    await page.screenshot({ path: resolve(values.screenshot), fullPage: true });
  }
  if (values.output) {
    await mkdir(dirname(resolve(values.output)), { recursive: true });
    await writeFile(resolve(values.output), JSON.stringify(result, null, 2) + "\n");
  }
  console.log(JSON.stringify(result));
} finally {
  for (const res of held) res.destroy();
  if (browser) await browser.close();
  if (server)
    await new Promise((resolve, reject) =>
      server.httpServer.close((error) => (error ? reject(error) : resolve())),
    );
}
