/** One-shot production Admin bundle + synthetic generated Workspace GETs. */
import { createServer } from "node:http";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { extname, resolve, sep } from "node:path";
import { parseArgs } from "node:util";
import assert from "node:assert/strict";
import { chromium, expect } from "@playwright/test";
import { validateRequest, validateResponse } from "../../packages/contracts/src/admin-http.ts";
const { values } = parseArgs({ options: { output: { type: "string" } } });
assert(values.output);
const output = resolve(values.output),
  dist = resolve("web/admin/dist");
await mkdir(output, { recursive: true });
const item = (id, status = "active") => ({
  id,
  object: "nexus.workspace",
  display_name: `<script>共享😀 ${id}</script>`,
  status,
  metadata_revision: 2,
  policy_revision: "4",
  created_at: "2026-10-24T00:00:00Z",
});
const requests = [],
  outbound = [],
  errors = [],
  serverErrors = [];
let origin,
  nextFailed = false,
  oneReads = 0,
  twoReads = 0;
function json(res, operation, body, status = 200) {
  validateResponse(operation, status, { body });
  res.writeHead(status, { "Content-Type": "application/json" });
  res.end(JSON.stringify(body));
}
const error = (code, type = "service_unavailable_error") => ({
  error: {
    code,
    type,
    message: "PRIVATE_PROVIDER_BODY",
    param: null,
    request_id: "req-synthetic",
    retryable: false,
  },
});
const server = createServer(async (req, res) => {
  try {
    const url = new URL(req.url, origin),
      path = url.pathname;
    if (path.startsWith("/v1/nexus/workspaces")) {
      requests.push({ method: req.method, path, cursor: url.searchParams.get("cursor") });
      assert.equal(req.method, "GET");
      if (path === "/v1/nexus/workspaces") {
        const cursor = url.searchParams.get("cursor");
        validateRequest("listWorkspaces", cursor ? { query: { cursor } } : {});
        assert.equal(url.searchParams.get("limit"), null);
        if (cursor) {
          assert.equal(cursor, "cursor-original");
          if (!nextFailed) {
            nextFailed = true;
            json(res, "listWorkspaces", error("service_unavailable"), 503);
          } else
            json(res, "listWorkspaces", {
              object: "nexus.list",
              data: [item("workspace-three")],
              has_more: false,
              next_cursor: "cursor-original",
            });
        } else
          json(res, "listWorkspaces", {
            object: "nexus.list",
            data: [item("workspace-one"), item("workspace-two", "disabled")],
            has_more: true,
            next_cursor: "cursor-original",
          });
      } else {
        const id = path.split("/").at(-1);
        validateRequest("getWorkspace", { path: { workspace_id: id } });
        if (id === "workspace-one") {
          if (++oneReads === 1) {
            await new Promise((r) => setTimeout(r, 200));
            if (!res.destroyed) json(res, "getWorkspace", item(id));
          } else json(res, "getWorkspace", error("workspace_not_found", "not_found_error"), 404);
        } else {
          assert.equal(id, "workspace-two");
          json(
            res,
            "getWorkspace",
            ++twoReads === 1
              ? item(id, "disabled")
              : { ...item("workspace-foreign"), display_name: "FORBIDDEN_DETAIL" },
          );
        }
      }
      return;
    }
    const file = path === "/" ? resolve(dist, "index.html") : resolve(dist, "." + path);
    if (!file.startsWith(dist + sep)) {
      res.writeHead(404);
      res.end();
      return;
    }
    res.writeHead(200, {
      "Content-Type":
        { ".html": "text/html", ".js": "text/javascript", ".css": "text/css" }[extname(file)] ??
        "application/octet-stream",
    });
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
  await page.goto(origin);
  await expect(page.getByRole("heading", { name: "共享工作空间" })).toBeVisible();
  assert.equal(requests.length, 0);
  const scope = page.getByRole("region", { name: "当前部署工作空间" });
  await scope.getByRole("button", { name: "读取工作空间", exact: true }).click();
  await expect(
    scope.getByRole("button", { name: "读取工作空间 workspace-one", exact: true }),
  ).toBeVisible();
  await scope.getByRole("button", { name: "读取工作空间 workspace-one", exact: true }).click();
  await scope.getByRole("button", { name: "读取工作空间 workspace-two", exact: true }).click();
  await expect(scope.getByTestId("workspace-status")).toHaveText("disabled");
  await expect(scope.getByRole("heading", { name: "工作空间详情" })).toBeFocused();
  await expect(scope.getByText("已选工作空间：workspace-two")).toBeVisible();
  await scope.getByRole("button", { name: "读取下一页工作空间", exact: true }).click();
  await expect(scope.getByRole("alert")).toContainText("service_unavailable");
  await expect(scope.getByText(/历史列表快照/)).toBeVisible();
  await expect(
    scope.getByRole("button", { name: "读取下一页工作空间", exact: true }),
  ).toBeDisabled();
  assert.equal(requests.length, 4);
  await scope.getByRole("button", { name: "重试原读取", exact: true }).click();
  await expect(
    scope.getByRole("button", { name: "读取工作空间 workspace-three", exact: true }),
  ).toBeVisible();
  await expect(
    scope.getByRole("button", { name: "读取下一页工作空间", exact: true }),
  ).toBeDisabled();
  assert.equal(requests.length, 5);
  assert.equal(requests[3].cursor, requests[4].cursor);
  await expect(scope.getByRole("heading", { name: "工作空间详情" })).toHaveCount(0);
  await scope.getByRole("button", { name: "读取工作空间", exact: true }).click();
  await expect(
    scope.getByRole("button", { name: "读取工作空间 workspace-one", exact: true }),
  ).toBeVisible();
  await scope.getByRole("button", { name: "读取工作空间 workspace-one", exact: true }).click();
  await expect(scope.getByRole("alert")).toContainText("workspace_not_found");
  await expect(scope.getByRole("heading", { name: "工作空间详情" })).toHaveCount(0);
  await scope.getByRole("button", { name: "读取工作空间 workspace-two", exact: true }).click();
  await expect(scope.getByRole("alert")).toContainText("workspace_read_unavailable");
  assert.equal(await page.getByText(/FORBIDDEN_DETAIL|PRIVATE_PROVIDER_BODY/).count(), 0);
  const before = requests.length;
  await page.evaluate(() => {
    Object.defineProperty(document, "hidden", { configurable: true, get: () => true });
    document.dispatchEvent(new Event("visibilitychange"));
  });
  await expect(scope.getByText(/历史列表快照/)).toBeVisible();
  await page.evaluate(() => {
    Object.defineProperty(document, "hidden", { configurable: true, get: () => false });
    document.dispatchEvent(new Event("visibilitychange"));
  });
  assert.equal(requests.length, before);
  await scope.getByRole("button", { name: "读取工作空间", exact: true }).focus();
  await expect(scope.getByRole("button", { name: "读取工作空间", exact: true })).toBeFocused();
  await page.keyboard.press("Tab");
  assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
  await page.screenshot({ path: resolve(output, "workspaces-360.png"), fullPage: true });
  assert.deepEqual(errors, []);
  assert.deepEqual(outbound, []);
  assert.deepEqual(serverErrors, []);
  assert.equal(requests.length, 8);
  const result = {
    browser: browser.version(),
    production_bundle: true,
    synthetic_generated_http: true,
    contexts: 1,
    pages: 1,
    requests,
    writes: 0,
    explicit_reads: true,
    pagination_retry_original: true,
    terminal_cursor_ignored: true,
    late_selection_ignored: true,
    wrong_identity_rejected: true,
    private_errors_hidden: true,
    hidden_visible_dom_fault_injection: true,
    viewport360: true,
    basic_focus_tab: true,
    errors,
    outbound,
    serverErrors,
  };
  await writeFile(resolve(output, "result.json"), JSON.stringify(result, null, 2) + "\n");
  console.log(
    JSON.stringify({ browser: result.browser, gets: requests.length, writes: 0, errors: 0 }),
  );
  await context.close();
} finally {
  await browser?.close();
  await new Promise((r) => server.close(r));
}
