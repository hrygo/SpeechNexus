/** Actual Chromium + loopback HTTP/SSE, synthetic metadata only; no backend or device. */
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
const job = (id, status = "queued") => parse("Job", { ...example("queued-job"), id, status });
const envelope = (code) => {
  const row = example("recover-operation");
  row.error.code = code;
  row.error.message = "synthetic-sensitive-value";
  return parse("ErrorResponse", row);
};
const stateFor = (scenario) => ({
  scenario,
  jobs: new Map(["job-demo", "job-other"].map((id) => [id, job(id)])),
  events: [],
  reads: 0,
  httpRequests: 0,
  closed: 0,
  held: new Set(),
  failRead: false,
});
let state = stateFor("sequence");
const json = (res, value, status = 200) => {
  res.writeHead(status, { "Content-Type": "application/json", "Cache-Control": "no-store" });
  res.end(JSON.stringify(value));
};
const require = createRequire(resolve(admin, "package.json"));
const { createServer, preview } = await import(pathToFileURL(require.resolve("vite")).href);
const install = (vite) => {
  vite.middlewares.use((req, res, next) => {
    const url = new URL(req.url, "http://127.0.0.1");
    if (!url.pathname.startsWith("/v1/")) return next();
    if (req.method !== "GET") return json(res, envelope("invalid_request"), 400);
    const current = state;
    current.httpRequests++;
    if (url.pathname === "/v1/nexus/jobs")
      return json(
        res,
        parse("JobList", {
          object: "nexus.list",
          data: [...current.jobs.values()],
          has_more: false,
          next_cursor: null,
        }),
      );
    const match = /^\/v1\/nexus\/jobs\/([^/]+)(\/events)?$/.exec(url.pathname);
    if (!match || !current.jobs.has(match[1]))
      return json(res, envelope("resource_not_found"), 404);
    const id = match[1];
    if (!match[2]) {
      current.reads++;
      return current.failRead
        ? json(res, envelope("service_unavailable"), 503)
        : json(res, current.jobs.get(id));
    }
    const after = req.headers["last-event-id"] ?? null;
    current.events.push({ id, after });
    if (current.scenario === "expired" && after)
      return json(res, envelope("event_history_expired"), 410);
    res.writeHead(200, {
      "Content-Type": "text/event-stream; charset=utf-8",
      "Cache-Control": "no-cache",
    });
    if (current.scenario === "waiting" || current.scenario === "disconnect") {
      res.write(": synthetic keepalive\n\n");
      current.held.add(res);
      res.on("close", () => {
        current.closed++;
        current.held.delete(res);
      });
      return;
    }
    current.jobs.set(id, job(id, after ? "cancelling" : "running"));
    current.failRead = current.scenario === "snapshot-failed";
    const value = parse("JobEvent", {
      ...example("job.queued"),
      id: after ? "9007199254740994" : "9007199254740993",
      job_id: current.scenario === "wrong-identity" ? "job-other" : id,
    });
    const frame = `id: ${value.id}\r\nevent: ${value.type}\r\ndata: ${JSON.stringify(value)}\r\n\r\n`;
    res.write(": synthetic comment\r");
    res.end("\n" + frame + frame); // Duplicate delivery must cause just one snapshot read.
  });
};
const plugin = {
  name: "team-c-loopback-events-probe",
  configureServer: install,
  configurePreviewServer: install,
};
const server = await createServer({
  root: admin,
  logLevel: "error",
  server: { host: "127.0.0.1", port: 0, strictPort: false },
  plugins: [plugin],
});
let production;
let browser;
try {
  await server.listen();
  const address = server.httpServer.address();
  assert(address && typeof address !== "string");
  const developmentOrigin = `http://127.0.0.1:${address.port}`;
  production = await preview({
    root: admin,
    logLevel: "error",
    plugins: [plugin],
    preview: { host: "127.0.0.1", port: 0, strictPort: false },
  });
  const productionAddress = production.httpServer.address();
  assert(productionAddress && typeof productionAddress !== "string");
  const origin = `http://127.0.0.1:${productionAddress.port}`;
  const allowedOrigins = new Set([origin, developmentOrigin]);
  browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({ reducedMotion: "reduce" });
  // Each synthetic scenario starts clean; refresh persistence has its own recovery probe.
  await context.addInitScript(() =>
    sessionStorage.removeItem("speechnexus.admin.task-recovery.v1"),
  );
  const unexpected = [],
    errors = [];
  await context.route("**/*", async (route) => {
    if (allowedOrigins.has(new URL(route.request().url()).origin)) await route.continue();
    else {
      unexpected.push("outbound request");
      await route.abort();
    }
  });
  const page = await context.newPage();
  page.on("pageerror", () => errors.push("pageerror"));
  const eventState = () => page.getByTestId("task-event-state");
  const cursor = () => page.getByTestId("task-event-cursor");
  const connect = () => page.getByRole("button", { name: "连接任务事件", exact: true }).click();
  const view = (id) => page.getByRole("button", { name: `查看 ${id}`, exact: true }).click();
  const start = async (scenario) => {
    for (const response of state.held) response.destroy();
    state = stateFor(scenario);
    await page.goto(`${origin}/`);
    await expect(page.getByRole("heading", { name: "运行事实管理", exact: true })).toBeVisible();
    assert.equal(state.httpRequests, 0, "Production host accessed API without explicit action");
    await page.getByRole("button", { name: "读取任务列表", exact: true }).click();
    await view("job-demo");
    await expect(page.getByTestId("task-detail-status")).toHaveText("queued");
  };

  await start("sequence");
  await connect();
  await expect(eventState()).toContainText("事件连接已结束");
  await expect(cursor()).toHaveText("9007199254740993");
  await expect(page.getByTestId("task-detail-status")).toHaveText("running");
  assert.equal(state.reads, 2);
  await connect();
  await expect(cursor()).toHaveText("9007199254740994");
  assert.equal(state.events[1].after, "9007199254740993");
  await expect(page.getByTestId("task-detail-status")).toHaveText("cancelling");
  await expect(page.getByTestId("task-cleanup")).toHaveText("pending");

  await start("expired");
  await connect();
  await expect(cursor()).toHaveText("9007199254740993");
  await expect(eventState()).toContainText("事件连接已结束");
  await connect();
  await expect(eventState()).toContainText("已重读当前任务");
  await expect(cursor()).toHaveText("无已确认水位");
  assert.equal(state.events.length, 2);
  assert.equal(state.reads, 3);
  await connect();
  await expect(cursor()).toHaveText("9007199254740993");
  assert.equal(state.events[2].after, null);

  await start("disconnect");
  await connect();
  await expect.poll(() => state.held.size).toBe(1);
  for (const response of state.held) response.destroy();
  await expect(eventState()).toContainText("事件连接中断");
  await expect(cursor()).toHaveText("无已确认水位");
  assert.equal(state.events.length, 1);

  await start("waiting");
  await connect();
  await expect.poll(() => state.held.size).toBe(1);
  await page.getByRole("button", { name: "停止事件连接", exact: true }).click();
  await expect(eventState()).toContainText("事件连接已停止");
  await expect.poll(() => state.closed).toBe(1);
  await connect();
  await expect.poll(() => state.held.size).toBe(1);
  await view("job-other");
  await expect(page.getByTestId("task-detail-id")).toHaveText("job-other");
  await expect.poll(() => state.closed).toBe(2);
  await expect(cursor()).toHaveText("无已确认水位");
  await connect();
  await expect.poll(() => state.held.size).toBe(1);
  const old = state;
  await page.goto(`${origin}/`);
  await expect.poll(() => old.closed).toBe(3);

  await start("wrong-identity");
  await connect();
  await expect(eventState()).toContainText("事件连接中断");
  assert.equal(state.reads, 1);
  await expect(cursor()).toHaveText("无已确认水位");
  await start("snapshot-failed");
  await connect();
  await expect(eventState()).toContainText("事件连接中断");
  await expect(page.getByRole("button", { name: "准备取消", exact: true })).toBeDisabled();
  await expect(cursor()).toHaveText("无已确认水位");
  await expect(page.getByText("synthetic-sensitive-value")).toHaveCount(0);

  // Exercise the real development entry too; its transport must remain entirely in memory.
  const httpCount = state.events.length;
  await page.goto(`${developmentOrigin}/?dev-scenario=task-center`);
  for (const scenario of ["events", "events-expired", "events-disconnected", "events-wait"]) {
    await page.getByLabel("演练场景").selectOption(scenario);
    await page.getByRole("button", { name: "读取任务列表", exact: true }).click();
    await view("job-demo");
    await connect();
    if (scenario === "events" || scenario === "events-expired") {
      await expect(eventState()).toContainText("事件连接已结束");
      await expect(cursor()).toHaveText("9007199254740993");
      await connect();
      await expect(eventState()).toContainText(
        scenario === "events" ? "事件连接已结束" : "已重读当前任务",
      );
    } else if (scenario === "events-disconnected")
      await expect(eventState()).toContainText("事件连接中断");
    else {
      await page.getByRole("button", { name: "停止事件连接", exact: true }).click();
      await expect(eventState()).toContainText("事件连接已停止");
    }
  }
  assert.equal(state.events.length, httpCount);
  await start("sequence");
  await connect();
  await expect(eventState()).toContainText("事件连接已结束");
  for (const colorScheme of ["light", "dark"]) {
    await page.emulateMedia({ colorScheme });
    await page.setViewportSize({ width: 320, height: 768 });
    assert(
      await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth),
      "Mobile layout overflow",
    );
  }
  await page.setViewportSize({ width: 1365, height: 1100 });
  if (values.screenshot) {
    const path = resolve(values.screenshot);
    await mkdir(dirname(path), { recursive: true });
    await page.screenshot({ path, fullPage: true });
  }
  assert.deepEqual(unexpected, []);
  assert.deepEqual(errors, []);
  const report = {
    checkedAt: new Date().toISOString(),
    browser: browser.version(),
    kind: "synthetic_loopback_admin_job_sse",
    productionBundle: true,
    productionHostNoAutomaticReads: true,
    httpScenariosPassed: 6,
    developmentScenariosPassed: 4,
    largeCursorResume: true,
    duplicateReadSuppression: true,
    historyExpiredReread: true,
    noAutomaticReconnect: true,
    ownerAbortObservedByServer: 3,
    wrongIdentityRejected: true,
    failedSnapshotDidNotAdvance: true,
    mobileOverflow: false,
    outboundRequests: 0,
    pageErrors: 0,
  };
  if (values.output) {
    const path = resolve(values.output);
    await mkdir(dirname(path), { recursive: true });
    await writeFile(path, JSON.stringify(report, null, 2) + "\n");
  }
  console.log(JSON.stringify(report));
} finally {
  for (const response of state.held) response.destroy();
  await browser?.close();
  await server.close();
  await production?.close();
}
