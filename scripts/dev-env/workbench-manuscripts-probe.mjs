/** Production bundle + generated C1 synthetic loopback; no real backend/model qualification. */
import { chromium, expect } from "@playwright/test";
import assert from "node:assert/strict";
import { mkdir, readFile, readdir, writeFile } from "node:fs/promises";
import { createRequire } from "node:module";
import { dirname, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { parseArgs } from "node:util";
import { createHttpValidator } from "../../packages/contracts/src/http-runtime.mjs";

const { values } = parseArgs({
  options: { output: { type: "string" }, screenshot: { type: "string" } },
});
const root = fileURLToPath(new URL("../../", import.meta.url));
const workbench = resolve(root, "web/workbench");
const examples = JSON.parse(
  await readFile(resolve(root, "tests/fixtures/contracts/workbench-http.json"), "utf8"),
).examples;
const validator = createHttpValidator(
  JSON.parse(
    await readFile(
      resolve(root, "packages/contracts/src/speechnexus_contracts/workbench-http.schema.json"),
      "utf8",
    ),
  ),
);
const sample = (operation) =>
  structuredClone(
    examples.find(
      (e) =>
        e.operation === operation &&
        e.kind === "response" &&
        e.valid &&
        [200, 201].includes(e.status),
    ).value.body,
  );
const project = { ...sample("createProject"), name: "合成项目" };
const second = { ...project, id: "project-second", name: "第二个合成项目" };
const transcript = { ...sample("getTranscript").transcript, display_name: "合成稿件" };
const original = { ...sample("getTranscript").revision, text: "合成原文 e\u0301😀\n " };
const projects = new Map([
  [project.id, project],
  [second.id, second],
]);
const transcripts = new Map([[transcript.id, transcript]]);
const versions = new Map([[transcript.id, [original]]]);
const receipts = new Map();
const mutations = [];
// A failing run used to swallow every collected fact; print what was attempted instead.
process.on("exit", (code) => {
  if (code === 0) return;
  console.error(
    "DIAG mutations on failure:",
    JSON.stringify(mutations.map((m) => [m.operation, m.key ?? null])),
  );
  console.error("DIAG handler errors:", JSON.stringify(handlerErrors));
});
const handlerErrors = [];
let enabled = true,
  loseRevision = true,
  requests = 0,
  sequence = 0;
const key = "speechnexus.workbench.manuscripts.v1";
function error(code) {
  return {
    error: {
      code,
      type: "invalid_request_error",
      message: "Synthetic rejection.",
      param: null,
      request_id: "request-synthetic",
      retryable: false,
    },
  };
}
function view(id, revision) {
  const items = versions.get(id);
  if (!items) return null;
  const selected =
    revision === undefined ? items.at(-1) : items.find((r) => r.revision === revision);
  return selected
    ? {
        object: "nexus.transcript_view",
        transcript: {
          ...transcripts.get(id),
          head_revision: items.at(-1).revision,
        },
        revision: selected,
      }
    : null;
}
function externalRevision(text) {
  const items = versions.get(transcript.id);
  const last = items.at(-1);
  items.push({
    ...last,
    id: `revision-external-${items.length + 1}`,
    revision: String(items.length + 1),
    source_revision: last.revision,
    status: "draft",
    text,
  });
}
const middleware = (vite) => {
  vite.middlewares.use(async (req, res, next) => {
    const url = new URL(req.url, "http://127.0.0.1");
    if (!url.pathname.startsWith("/v1/") && url.pathname !== "/v2/workbench/status") return next();
    requests++;
    let operation;
    try {
      const path = {};
      if (url.pathname === "/v2/workbench/status") operation = "getWorkbenchStatusV2";
      else if (url.pathname === "/v1/workbench/projects")
        operation = req.method === "POST" ? "createProject" : "listProjects";
      else {
        const p = /^\/v1\/workbench\/projects\/([^/]+)(\/transcripts)?$/.exec(url.pathname);
        const t = /^\/v1\/workbench\/transcripts\/([^/]+)(\/revisions)?$/.exec(url.pathname);
        if (p) {
          path.project_id = p[1];
          operation = p[2]
            ? req.method === "POST"
              ? "createTranscript"
              : "listTranscripts"
            : "getProject";
        } else if (t) {
          path.transcript_id = t[1];
          operation = t[2] ? "createTranscriptRevision" : "getTranscript";
        } else throw new Error("Unexpected synthetic route");
      }
      const chunks = [];
      for await (const chunk of req) chunks.push(chunk);
      const raw = Buffer.concat(chunks);
      const body = raw.length ? JSON.parse(raw.toString("utf8")) : undefined;
      const envelope = {
        path,
        query: Object.fromEntries(url.searchParams),
        headers: req.headers,
        ...(body === undefined ? {} : { body }),
      };
      validator.validateRequest(operation, envelope);
      const send = (data, status = 200) => {
        validator.validateResponse(operation, status, { body: data });
        res.writeHead(status, { "Content-Type": "application/json", "Cache-Control": "no-store" });
        res.end(JSON.stringify(data));
      };
      if (req.method === "POST") {
        const mutation = { operation, path, body, key: req.headers["idempotency-key"] };
        mutations.push(structuredClone(mutation));
        const slot = JSON.stringify([operation, path, mutation.key]);
        const receipt = receipts.get(slot);
        if (receipt) {
          if (JSON.stringify(receipt.body) !== JSON.stringify(body))
            return send(error("idempotency_conflict"), 409);
          return send(receipt.data, 201);
        }
        let data;
        if (operation === "createProject") {
          data = { ...project, id: `project-created-${++sequence}`, name: body.name };
          projects.set(data.id, data);
        } else if (operation === "createTranscript") {
          if (!projects.has(path.project_id)) return send(error("resource_not_found"), 404);
          const id = `transcript-created-${++sequence}`;
          // A read example may be sealed; a manual create must return its draft original.
          const created = sample("createTranscript");
          const metadata = {
            ...created.transcript,
            id,
            project_id: path.project_id,
            display_name: body.display_name,
          };
          const revision = {
            ...created.revision,
            id: `revision-${id}-1`,
            transcript_id: id,
            text: body.source.text,
          };
          transcripts.set(id, metadata);
          versions.set(id, [revision]);
          data = view(id);
        } else {
          const items = versions.get(path.transcript_id);
          if (!items) return send(error("resource_not_found"), 404);
          if (body.expected_revision !== items.at(-1).revision)
            return send(error("revision_conflict"), 409);
          const revision = String(BigInt(items.at(-1).revision) + 1n);
          data = {
            ...items.at(-1),
            id: `revision-${path.transcript_id}-${revision}`,
            revision,
            status: "draft",
            source_revision: body.source_revision ?? body.expected_revision,
            text: body.text,
          };
          items.push(data);
        }
        validator.validateResponse(operation, 201, { body: data });
        receipts.set(slot, { body: structuredClone(body), data: structuredClone(data) });
        return send(data, 201);
      }
      if (operation === "getWorkbenchStatusV2")
        return send({ ...sample(operation), workbench_enabled: enabled, capabilities: [] });
      if (operation === "listProjects")
        return send({
          object: "nexus.list",
          data: url.searchParams.has("cursor") ? [second] : [project],
          has_more: !url.searchParams.has("cursor"),
          next_cursor: url.searchParams.has("cursor") ? null : "cursor-project-next",
        });
      if (operation === "getProject")
        return projects.has(path.project_id)
          ? send(projects.get(path.project_id))
          : send(error("resource_not_found"), 404);
      if (operation === "listTranscripts")
        return send({
          object: "nexus.list",
          data: [...transcripts.values()]
            .filter((t) => t.project_id === path.project_id)
            .map((t) => ({ ...t, head_revision: versions.get(t.id).at(-1).revision })),
          has_more: false,
          next_cursor: null,
        });
      const data = view(path.transcript_id, url.searchParams.get("revision") ?? undefined);
      return data ? send(data) : send(error("resource_not_found"), 404);
    } catch (cause) {
      // Keep the actual complaint: a bare message made this probe's failures unreadable.
      handlerErrors.push(cause instanceof Error ? cause.message : String(cause));
      if (!res.headersSent) res.writeHead(500, { "Content-Type": "application/json" });
      res.end(JSON.stringify(error("synthetic_handler_failure")));
    }
  });
};

const require = createRequire(resolve(workbench, "package.json"));
const { preview } = await import(pathToFileURL(require.resolve("vite")).href);
let server, browser;
try {
  server = await preview({
    root: workbench,
    logLevel: "error",
    plugins: [{ name: "team-c-manuscripts-probe", configurePreviewServer: middleware }],
    preview: { host: "127.0.0.1", port: 0, strictPort: false },
  });
  const origin = `http://127.0.0.1:${server.httpServer.address().port}`;
  browser = await chromium.launch({ headless: true });
  const outbound = [],
    pageErrors = [];
  let contexts = 0;
  async function context() {
    const context = await browser.newContext({ reducedMotion: "reduce" });
    contexts++;
    await context.route("**/*", async (route) => {
      if (new URL(route.request().url()).origin === origin) {
        if (
          loseRevision &&
          route.request().method() === "POST" &&
          new URL(route.request().url()).pathname.endsWith("/revisions")
        ) {
          loseRevision = false;
          // Commit and read the server response, then drop it at the browser boundary.
          // Socket destruction alone may trigger Chromium's transparent transport retry.
          await route.fetch({ maxRetries: 0, maxRedirects: 0 });
          await route.abort("failed");
        } else await route.continue();
      } else {
        outbound.push("Unexpected outbound");
        await route.abort();
      }
    });
    context.on("page", (page) =>
      page.on("pageerror", (error) =>
        pageErrors.push(`${error.name}: ${error.message.slice(0, 200)}`),
      ),
    );
    return context;
  }
  const main = await context();
  const page = await main.newPage();
  const button = (name) => page.getByRole("button", { name, exact: true });
  async function choose(p) {
    await p.getByRole("button", { name: "项目与稿件", exact: true }).click();
  }
  async function open(p) {
    await p.goto(origin);
    await p.waitForLoadState("networkidle");
    await choose(p);
    await p.getByRole("button", { name: "合成项目", exact: true }).click();
    await p.getByRole("button", { name: "合成稿件", exact: true }).click();
    await expect(p.getByLabel("校对草稿")).toBeEnabled();
  }
  await open(page);
  await expect(page.getByLabel("部署数据边界")).toContainText("共享");
  await button("更多项目").click();
  await expect(button("第二个合成项目")).toBeVisible();
  await page.getByLabel("校对草稿").fill("合成校对 e\u0301😀\n ");
  await page.getByLabel("校对草稿").press("Tab");
  await expect(button("保存新修订")).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(button("按原键重放")).toBeEnabled();
  assert.equal(mutations.length, 1);
  const originalIntent = structuredClone(mutations[0]);
  await page.reload();
  await choose(page);
  await expect(button("按原键重放")).toBeEnabled();
  assert.equal(mutations.length, 1);
  await button("按原键重放").click();
  await expect(page.getByText("新修订已保存至当前部署。", { exact: true })).toBeVisible();
  assert.deepEqual(mutations[1], originalIntent);
  assert.equal(versions.get(transcript.id).length, 2);
  externalRevision("其他编辑者的合成版本");
  await page.getByLabel("校对草稿").fill("仍需保留的合成草稿");
  await button("保存新修订").click();
  await expect(page.getByLabel("服务端最新版本", { exact: true })).toHaveText(
    "其他编辑者的合成版本",
  );
  await expect(page.getByLabel("校对草稿")).toHaveValue("仍需保留的合成草稿");
  assert.equal(mutations.length, 3);
  await button("保留草稿并基于最新版本继续").click();
  assert.equal(mutations.length, 3);
  await button("保存新修订").click();
  await expect(page.getByText("新修订已保存至当前部署。", { exact: true })).toBeVisible();
  assert.notEqual(mutations[3].key, originalIntent.key);
  assert.equal(mutations[3].body.expected_revision, "3");
  await page.getByLabel("历史版本号").fill("1");
  await button("读取固定版本").click();
  await expect(page.getByLabel("校对草稿")).toHaveValue(original.text);
  await page.getByLabel("校对草稿").fill("从历史版本追加的合成稿");
  await button("保存新修订").click();
  await expect(page.getByText("新修订已保存至当前部署。", { exact: true })).toBeVisible();
  assert.equal(mutations[4].body.source_revision, "1");
  assert.equal(mutations[4].body.expected_revision, "4");
  assert.equal(versions.get(transcript.id)[0].text, original.text);
  await page.getByLabel("校对草稿").fill("刷新后仍保留的合成草稿");
  const before = mutations.length;
  await page.reload();
  await choose(page);
  await expect(page.getByLabel("校对草稿")).toBeEnabled();
  await expect(page.getByLabel("校对草稿")).toHaveValue("刷新后仍保留的合成草稿");
  assert.equal(mutations.length, before);
  await button("放弃本地修改").click();
  await expect(button("继续编辑")).toBeFocused();
  await page.keyboard.press("Tab");
  await expect(button("确认放弃本地修改")).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(page.getByLabel("校对草稿")).toHaveCount(0);
  await page.getByLabel("项目名称").fill("新合成项目");
  await button("创建项目").click();
  await expect(button("新合成项目")).toBeVisible();
  await page.getByLabel("稿件名称").fill("新合成原稿");
  await page.getByLabel("原稿正文").fill("合成原稿😀");
  await button("保存原稿").click();
  // This response is delivered normally. The earlier lost-revision scenario already proves
  // original-key recovery; an invalid sealed manual-create response must not be replayed as success.
  await expect(button("核对当前版本")).toBeEnabled({ timeout: 15000 });
  await button("核对当前版本").click();
  await expect(button("新合成原稿")).toBeVisible();
  await button("新合成原稿").click();
  await expect(page.getByLabel("校对草稿")).toHaveValue("合成原稿😀");
  const createdTranscript = [...transcripts.values()].find(
    (item) => item.display_name === "新合成原稿",
  );
  const createdOriginal = versions.get(createdTranscript.id)[0];
  assert.equal(createdOriginal.status, "draft");
  assert.equal(createdOriginal.source_revision, null);
  await page.setViewportSize({ width: 360, height: 800 });
  assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
  await page.setViewportSize({ width: 1280, height: 900 });
  if (values.screenshot) {
    await mkdir(dirname(resolve(values.screenshot)), { recursive: true });
    await page.evaluate(() => window.scrollTo(0, 0));
    await page.screenshot({ path: resolve(values.screenshot), fullPage: true });
  }
  // A new context with corrupt recovery must not even contact the API.
  const corrupt = await context();
  await corrupt.addInitScript((name) => {
    if (location.protocol === "http:") sessionStorage.setItem(name, "{");
  }, key);
  const damaged = await corrupt.newPage();
  const beforeDamaged = requests;
  await damaged.goto(origin);
  await choose(damaged);
  await expect(damaged.getByRole("alert")).toContainText("恢复信息不可用");
  assert.equal(requests, beforeDamaged);
  // Simulate quota failure only inside this disposable context, before any write intent.
  const quota = await context();
  const stopped = await quota.newPage();
  await open(stopped);
  await stopped.evaluate((name) => {
    const original = Storage.prototype.setItem;
    Storage.prototype.setItem = function (k, value) {
      if (k === name) throw new Error("Synthetic quota");
      original.call(this, k, value);
    };
  }, key);
  await stopped.getByLabel("校对草稿").fill("配额失败仍保留的合成草稿");
  await expect(stopped.getByRole("alert")).toContainText("本地恢复信息未能保存");
  await expect(stopped.getByRole("button", { name: "保存新修订", exact: true })).toBeDisabled();
  await expect(stopped.getByLabel("校对草稿")).toHaveValue("配额失败仍保留的合成草稿");
  const beforeDisabled = mutations.length;
  enabled = false;
  const disabled = await context();
  const unavailable = await disabled.newPage();
  await unavailable.goto(origin);
  await choose(unavailable);
  await expect(
    unavailable.getByText("当前部署尚未启用工作台，已有本地草稿仍保留。", { exact: true }),
  ).toBeVisible();
  assert.equal(mutations.length, beforeDisabled);
  const bundle = (
    await Promise.all(
      (await readdir(resolve(workbench, "dist/assets")))
        .filter((name) => name.endsWith(".js"))
        .map((name) => readFile(resolve(workbench, "dist/assets", name), "utf8")),
    )
  ).join("\n");
  assert(!bundle.includes("Synthetic project") && !bundle.includes("createTtsDemoTransport"));
  assert.deepEqual(outbound, []);
  assert.deepEqual(pageErrors, []);
  assert.deepEqual(handlerErrors, []);
  const report = {
    checked_at: new Date().toISOString(),
    chromium: browser.version(),
    production_ui: true,
    synthetic_loopback: true,
    monitored_contexts: contexts,
    mutation_requests: mutations.length,
    generated_request_response_validation: true,
    original_key_refresh_replay: true,
    exact_unicode: true,
    explicit_conflict_rebase: true,
    immutable_history: true,
    unsaved_refresh_recovery: true,
    project_transcript_create: true,
    manual_create_status: createdOriginal.status,
    manual_create_source_revision: createdOriginal.source_revision,
    paging: true,
    corrupt_and_quota_fail_closed: true,
    disabled_service: true,
    keyboard_and_360px: true,
    fake_excluded: true,
    outbound_requests: outbound.length,
    page_errors: pageErrors.length,
    handler_errors: handlerErrors,
    real_backend_or_model_qualification: false,
  };
  if (values.output) {
    await mkdir(dirname(resolve(values.output)), { recursive: true });
    await writeFile(resolve(values.output), JSON.stringify(report, null, 2) + "\n");
  }
  console.log(JSON.stringify(report, null, 2));
} finally {
  await browser?.close();
  await new Promise((done) => (server?.httpServer ? server.httpServer.close(done) : done()));
}
