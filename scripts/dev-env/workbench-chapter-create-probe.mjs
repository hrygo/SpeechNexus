/**
 * Real C HTTP + private PG + production bundle: adding a chapter to an open book.
 *
 * The audiobook flow could list chapters but never create one, so the book could not be
 * filled from the UI at all. This drives the production bundle: select the deployment's
 * project, create a book, open it, add a chapter, and confirm the chapter the page shows.
 * The Python side then reads the same chapter back over HTTP.
 */
import { chromium, expect } from "@playwright/test";
import assert from "node:assert/strict";
import { mkdir, writeFile } from "node:fs/promises";
import { resolve } from "node:path";
import { parseArgs } from "node:util";

const { values } = parseArgs({
  options: {
    url: { type: "string" },
    output: { type: "string" },
    "project-name": { type: "string" },
    "book-title": { type: "string" },
    "chapter-title": { type: "string" },
    "unit-text": { type: "string" },
  },
});
assert(
  values.url &&
    values.output &&
    values["project-name"] &&
    values["book-title"] &&
    values["chapter-title"],
);
const origin = new URL(values.url).origin;
assert.equal(new URL(origin).hostname, "127.0.0.1");
const UNIT_TEXT = values["unit-text"];
const KEYBOARD_TEXT = `${UNIT_TEXT}（键盘）`;
const output = resolve(values.output);
await mkdir(output, { recursive: true });

const browser = await chromium.launch({ headless: true });
const context = await browser.newContext({
  reducedMotion: "reduce",
  viewport: { width: 1280, height: 1000 },
});
const page = await context.newPage();
const outbound = [],
  failed = [],
  pageErrors = [];
let chapterPosts = 0;
page.on("pageerror", (error) => pageErrors.push(error.name));
page.on("request", (request) => {
  if (request.method() === "POST" && request.url().endsWith("/chapters")) chapterPosts += 1;
});
page.on("response", (response) => {
  if (response.status() >= 400)
    failed.push({ path: new URL(response.url()).pathname, status: response.status() });
});
await context.route("**/*", async (route) => {
  if (new URL(route.request().url()).origin === origin) return route.continue();
  outbound.push("Unexpected outbound");
  return route.abort();
});

try {
  await page.goto(origin);
  await page.waitForLoadState("networkidle");
  await page.getByRole("button", { name: "有声书导出", exact: true }).click();

  const section = page.getByRole("region", { name: "有声书与导出工作台" });
  await expect(section).toBeVisible();
  await section.getByRole("button", { name: values["project-name"], exact: true }).click();

  await section.getByLabel("新有声书标题").fill(values["book-title"]);
  // Creating a book is a submit button labelled 创建有声书; the count proves one attempt.
  await section.getByRole("button", { name: "创建有声书" }).click();
  const books = section.getByRole("list", { name: "书目列表" });
  await expect(books.getByRole("button", { name: new RegExp(values["book-title"]) })).toBeVisible();
  await books.getByRole("button", { name: new RegExp(values["book-title"]) }).click();

  await section.getByLabel("新章节标题").fill(values["chapter-title"]);
  await section.getByLabel("新章节序号").fill("0");
  await section.getByLabel("新章节正文").fill("合成章节正文，仅用于浏览器资格验证。");
  await section.getByRole("button", { name: "新增章节" }).click();
  await expect(section.getByRole("list", { name: "章节列表" })).toContainText(
    `第 1 章 · ${values["chapter-title"]}`,
  );
  await expect(section.getByText(/正文修订 1/)).toBeVisible();
  // This deployment has no speech base, so the panel must say the catalogue is missing
  // instead of offering voices it cannot verify.
  await expect(section.getByText(/音色目录不可用/)).toBeVisible();

  // No published catalogue in this deployment, so the pin is entered explicitly. Registering
  // a unit is C's; whether it can be generated is the deployment's business.
  await section.getByLabel("新单元序号").fill("0");
  await section.getByLabel("新单元文本").fill(UNIT_TEXT);
  await section.getByLabel("新单元音色 id").fill("voice_synthetic");
  await section.getByLabel("新单元音色修订").fill("1");
  await section.getByLabel("新单元模型").fill("tts-quality");
  await section.getByLabel("新单元绑定 id").fill("binding_synthetic");
  await section.getByLabel("新单元绑定修订").fill("1");
  await section.getByRole("button", { name: "新增渲染单元" }).click();
  const units = section.getByRole("list", { name: "单元列表" });
  await expect(units).toContainText(UNIT_TEXT);
  await expect(units).toContainText("修订 1");
  await expect(section.getByText(/单元 1 已创建为修订 1。/)).toBeVisible();
  await page.screenshot({ path: resolve(output, "unit-created.png"), fullPage: true });

  // A second unit, keyboard only: fill each field, Tab to the next one (asserting the focus
  // order), Tab to the submit button and press Enter. The first field gets focus back after.
  const keyboardFields = [
    "新单元序号",
    "新单元文本",
    "新单元音色 id",
    "新单元音色修订",
    "新单元模型",
    "新单元绑定 id",
    "新单元绑定修订",
  ];
  const keyboardValues = [
    "1",
    KEYBOARD_TEXT,
    "voice_synthetic",
    "1",
    "tts-quality",
    "binding_synthetic",
    "1",
  ];
  for (const [index, field] of keyboardFields.entries()) {
    await section.getByLabel(field).fill(keyboardValues[index]);
    if (index < keyboardFields.length - 1) {
      await page.keyboard.press("Tab");
      await expect(page.locator(":focus")).toHaveAccessibleName(keyboardFields[index + 1]);
    }
  }
  await page.keyboard.press("Tab");
  await expect(page.locator(":focus")).toHaveAccessibleName("新增渲染单元");
  await page.keyboard.press("Enter");
  await expect(units).toContainText(KEYBOARD_TEXT);
  await expect(page.locator(":focus")).toHaveAccessibleName("新单元序号");
  await page.screenshot({ path: resolve(output, "keyboard-unit.png"), fullPage: true });

  // The page must stay usable in a narrow window: no horizontal scrolling at 360px.
  await page.setViewportSize({ width: 360, height: 800 });
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
  );
  assert.equal(overflow, 0, "the books page must not scroll horizontally at 360px");
  await page.screenshot({ path: resolve(output, "narrow-360.png"), fullPage: true });
  await page.setViewportSize({ width: 1280, height: 900 });

  // Reading one voice revision is the only way to see retirement/disable facts; without a
  // speech base the deployment must refuse instead of showing a stale revision.
  await section.getByLabel("核对音色 id").fill("voice_synthetic");
  await section.getByLabel("核对音色修订").fill("1");
  await section.getByRole("button", { name: "读取该音色修订" }).click();
  await expect(section.getByRole("alert", { name: "音色修订错误" })).toContainText(
    "音色目录不可用",
  );
  await expect(section.getByRole("status", { name: "音色修订信息" })).toHaveCount(0);
  await page.screenshot({ path: resolve(output, "chapter-created.png"), fullPage: true });

  // Renaming is version-checked. The page renames once, then the test advances the
  // deployment's metadata revision out of band and the page tries again with what it saw.
  const RENAMED = `${values["project-name"]}（已改名）`;
  const INJECTED = `${values["project-name"]}（测试推进）`;
  await section.getByLabel("新项目名称").fill(RENAMED);
  await section.getByRole("button", { name: "重命名项目" }).click();
  await expect(section.getByRole("button", { name: RENAMED, exact: true })).toBeVisible();

  const listed = await context.request.get(`${origin}/v1/workbench/projects`);
  assert.equal(listed.status(), 200);
  const current = (await listed.json()).data.find((item) => item.name === RENAMED);
  assert(current, "the renamed project must be visible to the deployment");
  const injected = await context.request.patch(`${origin}/v1/workbench/projects/${current.id}`, {
    headers: { "Idempotency-Key": `probe-injected-rename-${current.metadata_revision}` },
    data: { expected_metadata_revision: current.metadata_revision, name: INJECTED },
  });
  assert.equal(injected.status(), 200);

  await section.getByLabel("新项目名称").fill(RENAMED);
  await section.getByRole("button", { name: "重命名项目" }).click();
  const stale = section.getByRole("alert", { name: "项目错误" });
  await expect(stale).toContainText("修订冲突");
  // A refused rename must not be presented as saved.
  await expect(section.getByRole("button", { name: INJECTED, exact: true })).toHaveCount(0);
  await page.screenshot({ path: resolve(output, "rename-conflict.png"), fullPage: true });

  const result = {
    kind: "production-browser-real-c-http-private-pg",
    renamedViaUi: RENAMED,
    injectedName: INJECTED,
    staleRenameRefused: true,
    browser: browser.version(),
    createdBookViaUi: true,
    createdChapterViaUi: true,
    createdUnitViaUi: true,
    keyboardUnitText: KEYBOARD_TEXT,
    keyboardOrderChecked: true,
    narrowOverflow: overflow,
    chapterPosts,
    outbound: outbound.length,
    unexpectedErrors: pageErrors.length,
    failed,
  };
  assert.equal(result.outbound, 0);
  assert.equal(result.unexpectedErrors, 0);
  assert.equal(result.chapterPosts, 1);
  // Two non-2xx answers are provoked on purpose: this deployment has no speech base, so the
  // published-voice catalogue is unavailable, and the stale rename is refused (409).
  // Three non-2xx answers are provoked on purpose: no speech base means the catalogue and the
  // single-revision read are both unavailable, and the stale rename is refused.
  assert.equal(result.failed.length, 3);
  assert.deepEqual(result.failed.map((item) => item.status).sort(), [409, 503, 503]);
  assert.ok(
    result.failed.some(
      (item) =>
        item.path === "/v1/workbench/voices/voice_synthetic/revisions/1" && item.status === 503,
    ),
  );
  assert.ok(
    result.failed.some((item) => item.path === "/v1/workbench/voices" && item.status === 503),
  );
  assert.ok(
    result.failed.some(
      (item) => item.path.endsWith(`/projects/${current.id}`) && item.status === 409,
    ),
  );
  await writeFile(resolve(output, "result.json"), JSON.stringify(result, null, 2) + "\n");
  process.stdout.write(JSON.stringify({ stage: "done", ...result }) + "\n");
} finally {
  await context.close();
  await browser.close();
}
