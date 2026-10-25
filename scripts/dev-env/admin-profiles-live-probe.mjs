/** Production Admin against the isolated real profile service owned by the pytest harness. */
import assert from "node:assert/strict";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { resolve } from "node:path";
import { createInterface } from "node:readline";
import { parseArgs } from "node:util";
import { chromium, expect } from "@playwright/test";
import { parse } from "../../packages/contracts/src/admin.ts";

const { values } = parseArgs({ options: { url: { type: "string" }, output: { type: "string" } } });
assert(values.url && values.output);
const origin = new URL(values.url);
assert.equal(origin.protocol, "http:");
assert.equal(origin.hostname, "127.0.0.1");
assert.equal(origin.username + origin.password + origin.search + origin.hash, "");
assert.equal(origin.pathname, "/");
const output = resolve(values.output);
await mkdir(output, { recursive: true });
const fixtures = JSON.parse(
  await readFile("tests/fixtures/contracts/consumer-profiles.json", "utf8"),
);
const source = parse(
  "ProfileRevisionCreateRequest",
  fixtures.examples.find((e) => e.schema === "ProfileRevisionCreateRequest" && e.valid).value,
);
const definition = Object.fromEntries(
  Object.entries(source).filter(
    ([key]) => !["profile_id", "expected_latest_revision"].includes(key),
  ),
);
const journalKey = "speechnexus.admin.profile-draft.v1";
const outbound = [],
  errors = [],
  writes = [];
const reader = createInterface({ input: process.stdin });
const controls = reader[Symbol.asyncIterator]();
const emit = (stage) => process.stdout.write(JSON.stringify({ stage }) + "\n");
async function control(expected) {
  const item = await controls.next();
  assert.equal(item.done, false);
  assert.equal(item.value, expected);
}
const browser = await chromium.launch({ headless: true });
const contexts = [];
async function open({ storageFailure = false } = {}) {
  const context = await browser.newContext({ viewport: { width: 360, height: 900 } });
  contexts.push(context);
  if (storageFailure)
    await context.addInitScript(
      ({ key }) => {
        const original = Storage.prototype.setItem;
        Storage.prototype.setItem = function (name, value) {
          if (this === window.sessionStorage && name === key)
            throw new DOMException("Synthetic quota fault", "QuotaExceededError");
          return original.call(this, name, value);
        };
      },
      { key: journalKey },
    );
  await context.route("**/*", (route) => {
    if (new URL(route.request().url()).origin !== origin.origin) {
      outbound.push(route.request().url());
      return route.abort();
    }
    if (route.request().method() === "POST") writes.push(route.request());
    return route.continue();
  });
  const page = await context.newPage();
  page.on("pageerror", (e) => errors.push(String(e)));
  await page.goto(origin.origin);
  await page.waitForLoadState("networkidle");
  const scope = page.getByRole("region", { name: "场景预设草稿管理" });
  await expect(scope.getByRole("heading", { name: "场景预设草稿" })).toBeVisible();
  return { page, scope, context };
}
async function journal(page) {
  return page.evaluate((key) => JSON.parse(sessionStorage.getItem(key)), journalKey);
}
async function prepare(scope, id, body = definition) {
  await scope.getByLabel("预设系列编号", { exact: true }).fill(id);
  await scope.getByLabel("完整预设配置（JSON）").fill(JSON.stringify(body));
  await scope.getByRole("button", { name: "核对草稿", exact: true }).click();
}
async function confirm(scope) {
  const button = scope.getByRole("button", { name: "确认提交草稿", exact: true });
  await expect(button).toBeFocused();
  await button.click();
}
async function readFixed(scope, id, revision) {
  await scope.getByLabel("读取预设系列编号").fill(id);
  await scope.getByLabel("读取固定修订", { exact: true }).fill(revision);
  await scope.getByRole("button", { name: "读取固定修订", exact: true }).click();
  await expect(scope.getByText("本次固定读取的修订快照", { exact: true })).toBeVisible();
  await expect
    .poll(async () => JSON.parse(await scope.getByLabel("固定修订快照").innerText()).revision)
    .toBe(revision);
  return JSON.parse(await scope.getByLabel("固定修订快照").innerText());
}
async function unknown(scope) {
  await expect(scope.getByRole("button", { name: "按原键重放草稿" })).toBeEnabled();
  await expect(scope.getByRole("button", { name: "返回编辑" })).toBeDisabled();
  await expect(scope.getByLabel("草稿回执")).toHaveCount(0);
}

try {
  const { page, scope, context } = await open();
  assert.equal(writes.length, 0);
  await expect(scope.getByLabel("完整预设配置（JSON）")).toHaveValue("");
  await prepare(scope, "browser-profile");
  const intent = await journal(page);
  assert.equal(intent.phase, "prepared");
  assert.deepEqual(intent.body, {
    ...definition,
    profile_id: "browser-profile",
    expected_latest_revision: null,
  });
  assert.equal(writes.length, 0);
  await confirm(scope);
  await unknown(scope);
  assert.equal((await journal(page)).phase, "unknown");
  assert.equal(writes.length, 1);
  await page.reload();
  await page.waitForLoadState("networkidle");
  await unknown(scope);
  assert.equal(writes.length, 1);
  const revisionOne = await readFixed(scope, "browser-profile", "1");
  await unknown(scope);
  const replay = scope.getByRole("button", { name: "按原键重放草稿" });
  await replay.click();
  await expect(scope.getByRole("alert")).toContainText("提交结果未知");
  await unknown(scope);
  assert.equal(writes.length, 2);
  assert.deepEqual((await journal(page)).body, intent.body);
  assert.equal((await journal(page)).key, intent.key);
  await replay.click();
  await expect(scope.getByLabel("草稿回执")).toContainText("browser-profile · 1 · draft");
  assert.equal(writes.length, 3);
  await page.reload();
  await page.waitForLoadState("networkidle");
  await expect(scope.getByLabel("草稿回执")).toContainText("历史缓存草稿回执");
  assert.equal(writes.length, 3);
  await scope.getByRole("button", { name: "返回编辑" }).click();
  await expect(scope.getByLabel("已知的最新分配修订")).toHaveValue("1");
  const changed = { ...definition, display_name: "Changed from revision one" };
  await scope.getByLabel("完整预设配置（JSON）").fill(JSON.stringify(changed));
  await scope.getByRole("button", { name: "核对草稿" }).click();
  await confirm(scope);
  await expect(scope.getByRole("alert")).toContainText("分配修订已改变");
  assert.equal((await journal(page)).phase, "rejected");
  assert.equal((await journal(page)).body.expected_latest_revision, "1");
  await scope.getByRole("button", { name: "返回编辑" }).click();
  await expect(scope.getByLabel("已知的最新分配修订")).toHaveValue("1");
  assert.deepEqual(
    JSON.parse(await scope.getByLabel("完整预设配置（JSON）").inputValue()),
    changed,
  );
  await readFixed(scope, "browser-profile", "2");
  await scope.getByRole("button", { name: "以此修订为基础编辑" }).click();
  await expect(scope.getByLabel("已知的最新分配修订")).toHaveValue("2");
  const copied = JSON.parse(await scope.getByLabel("完整预设配置（JSON）").inputValue());
  for (const key of ["publication", "validation", "profile_id", "revision", "schema_version"])
    assert(!Object.hasOwn(copied, key));
  assert.equal(copied.display_name, "External revision");
  await scope.getByRole("button", { name: "核对草稿" }).click();
  await confirm(scope);
  await expect(scope.getByLabel("草稿回执")).toContainText("browser-profile · 3 · draft");
  assert.deepEqual(await readFixed(scope, "browser-profile", "1"), revisionOne);
  assert.equal(writes.length, 5);
  await expect(scope.getByText(/PRIVATE_PROFILE_PROBE_DETAIL/)).toHaveCount(0);
  assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
  await page.screenshot({ path: resolve(output, "profiles-360.png"), fullPage: true });
  await context.close();

  const late = await open();
  await prepare(late.scope, "browser-late");
  await confirm(late.scope);
  emit("await-late-commit");
  await control("committed");
  await late.page.evaluate(() => {
    Object.defineProperty(document, "hidden", { configurable: true, get: () => true });
    document.dispatchEvent(new Event("visibilitychange"));
  });
  emit("hidden");
  await control("released");
  await late.page.waitForLoadState("networkidle");
  assert.equal((await journal(late.page)).phase, "unknown");
  await expect(late.scope.getByLabel("草稿回执")).toHaveCount(0);
  await late.page.evaluate(() => {
    Object.defineProperty(document, "hidden", { configurable: true, get: () => false });
    document.dispatchEvent(new Event("visibilitychange"));
  });
  await late.page.waitForLoadState("networkidle");
  assert.equal(writes.length, 6);
  await unknown(late.scope);
  await late.scope.getByRole("button", { name: "按原键重放草稿" }).click();
  await expect(late.scope.getByLabel("草稿回执")).toContainText("browser-late · 1 · draft");
  await late.context.close();

  const blocked = await open({ storageFailure: true });
  const before = writes.length;
  await prepare(blocked.scope, "browser-blocked");
  await expect(blocked.scope.getByText(/本地请求记录不可用/)).toBeVisible();
  await expect(blocked.scope.getByRole("button", { name: "核对草稿" })).toBeDisabled();
  await expect(blocked.scope.getByRole("button", { name: "确认提交草稿" })).toHaveCount(0);
  assert.equal(writes.length, before);
  await blocked.context.close();

  const foreign = await open();
  await prepare(foreign.scope, "browser-foreign");
  await confirm(foreign.scope);
  await unknown(foreign.scope);
  assert.equal((await journal(foreign.page)).phase, "unknown");
  await foreign.context.close();
  assert.equal(writes.length, 8);
  assert.deepEqual(outbound, []);
  assert.deepEqual(errors, []);
  const result = {
    browser: browser.version(),
    production_bundle: true,
    real_profile_http_pg: true,
    synthetic_configuration_and_transport_faults: true,
    contexts: 4,
    browser_posts: writes.length,
    unknown_survives_reload: true,
    fixed_get_not_write_result: true,
    ingress_refusal_not_write_result: true,
    original_key_replay: true,
    explicit_cas_conflict: true,
    history_immutable: true,
    late_reply_ignored: true,
    storage_failure_zero_writes: true,
    wrong_identity_rejected: true,
    viewport360: true,
    confirmation_focus: true,
    hidden_visibility_dom_fault_injection: true,
    outbound: outbound.length,
    page_errors: errors.length,
    real_speech_or_published_lifecycle_or_physical_device: false,
  };
  await writeFile(resolve(output, "result.json"), JSON.stringify(result, null, 2) + "\n");
  emit("done");
} finally {
  reader.close();
  process.stdin.destroy();
  for (const context of contexts) await context.close();
  await browser.close();
}
