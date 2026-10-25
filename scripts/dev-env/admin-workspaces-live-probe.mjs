/** Chromium-only probe of an explicitly supplied disposable A HTTP/PG fixture. */
import assert from "node:assert/strict";
import { mkdir, writeFile } from "node:fs/promises";
import { resolve } from "node:path";
import { parseArgs } from "node:util";
import { chromium, expect } from "@playwright/test";

const { values } = parseArgs({ options: { url: { type: "string" }, output: { type: "string" } } });
assert(values.url && values.output);
const base = new URL(values.url);
assert.equal(base.protocol, "http:");
assert.equal(base.hostname, "127.0.0.1");
assert(base.port && base.username === "" && base.password === "");
const origin = base.origin,
  output = resolve(values.output),
  prefix = "/v1/nexus/workspaces",
  requests = [],
  writes = [],
  errors = [],
  outbound = [],
  ids = [];
await mkdir(output, { recursive: true });
const browser = await chromium.launch({ headless: true });
try {
  const context = await browser.newContext({ viewport: { width: 360, height: 900 } });
  await context.route("**/*", (route) => {
    const url = new URL(route.request().url());
    if (url.origin !== origin) {
      outbound.push(url.origin);
      return route.abort();
    }
    return route.continue();
  });
  const page = await context.newPage();
  page.on("pageerror", (e) => errors.push(String(e)));
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (!["GET", "HEAD"].includes(request.method())) writes.push(request.method());
    if (url.pathname.startsWith(prefix))
      requests.push({
        method: request.method(),
        path: url.pathname,
        cursor: url.searchParams.get("cursor"),
      });
  });
  await page.goto(origin);
  const scope = page.getByRole("region", { name: "当前部署工作空间" });
  await expect(scope.getByRole("heading", { name: "共享工作空间" })).toBeVisible();
  assert.equal(requests.length, 0);
  async function list(button) {
    const response = page.waitForResponse(
      (r) => new URL(r.url()).pathname === prefix && r.request().method() === "GET",
    );
    await button.click();
    const reply = await response;
    assert.equal(reply.status(), 200);
    const body = await reply.json();
    assert(body.data.length > 0);
    await expect(
      scope.getByRole("button", { name: `读取工作空间 ${body.data[0].id}`, exact: true }),
    ).toBeVisible();
    ids.push(...body.data.map((item) => item.id));
    return body;
  }
  let body = await list(scope.getByRole("button", { name: "读取工作空间", exact: true }));
  assert.equal(body.has_more, true, "fixture must exceed the server's default page size");
  await scope.getByRole("button", { name: "读取工作空间 workspace-001", exact: true }).click();
  await expect(scope.getByTestId("workspace-status")).toHaveText("disabled");
  await expect(scope.getByTestId("workspace-metadata-version")).toHaveText("7");
  await expect(scope.getByText("9007199254740993", { exact: true })).toBeVisible();
  await expect(scope.getByText("1970-01-01T00:00:00.000001Z", { exact: true })).toBeVisible();
  await expect(scope.getByRole("heading", { name: "工作空间详情" })).toBeFocused();
  assert.equal(await scope.locator("script").count(), 0);
  await expect(
    scope.getByRole("strong").filter({ hasText: "<script>共享😀 001</script>" }),
  ).toHaveCount(1);
  await scope.getByRole("button", { name: "读取工作空间 workspace-000", exact: true }).click();
  await expect(scope.getByRole("alert")).toContainText("workspace_not_found");
  await expect(scope.getByRole("heading", { name: "工作空间详情" })).toHaveCount(0);
  let pages = 1;
  while (body.has_more) {
    assert(pages < 10, "bounded pagination must terminate");
    body = await list(scope.getByRole("button", { name: "读取下一页工作空间", exact: true }));
    pages++;
  }
  await expect(
    scope.getByRole("button", { name: "读取下一页工作空间", exact: true }),
  ).toBeDisabled();
  const expected = [
    ...Array.from({ length: 102 }, (_, i) => `workspace-${String(i).padStart(3, "0")}`),
    "workspace-default",
  ];
  assert.deepEqual(ids, expected);
  assert.equal(new Set(ids).size, 103);
  await scope.getByRole("button", { name: "读取工作空间 workspace-default", exact: true }).click();
  await expect(scope.getByTestId("workspace-status")).toHaveText("active");
  await expect(scope.getByRole("heading", { name: "工作空间详情" })).toBeFocused();
  await scope.getByRole("button", { name: "读取工作空间", exact: true }).focus();
  await page.keyboard.press("Tab");
  assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
  assert.equal(requests.length, pages + 3);
  assert.deepEqual(writes, []);
  assert.deepEqual(outbound, []);
  assert.deepEqual(errors, []);
  await page.screenshot({ path: resolve(output, "workspaces-live-360.png"), fullPage: true });
  const result = {
    browser: browser.version(),
    production_bundle: true,
    real_http_pg: true,
    explicit_service_injection: true,
    asset_runtime_s3: false,
    full_migrations: false,
    contexts: 1,
    pages: 1,
    list_pages: pages,
    ids_seen: ids.length,
    writes: 0,
    disabled_readable: true,
    lossless_versions: true,
    missing_detail_cleared: true,
    html_as_text: true,
    viewport360: true,
    basic_focus_tab: true,
    requests,
    errors,
    outbound,
  };
  await writeFile(resolve(output, "result.json"), JSON.stringify(result, null, 2) + "\n");
  console.log(
    JSON.stringify({ gets: requests.length, ids: ids.length, list_pages: pages, writes: 0 }),
  );
  await context.close();
} finally {
  await browser.close();
}
