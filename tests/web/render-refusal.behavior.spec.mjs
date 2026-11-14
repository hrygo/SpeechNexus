import { test, expect } from "@playwright/test";
import { readFileSync } from "node:fs";

import { lockToPreview, expectNetworkLocked } from "./network-lock.mjs";

/**
 * Browser-level specs for the render unit panel's dispatch refusals (#790).
 *
 * The panel's new refusal dispositions (#754/#755) were proven only in jsdom, where
 * three things cannot be shown: that the server's declared retry delay becomes the
 * operator's "wait at least N seconds" sentence, that the retry the panel offers really
 * re-sends through the real client and the real bundle, and that the next-step copy is
 * actually laid out on screen rather than merely present in the DOM. This file drives
 * the production preview bundle of the workbench, so every string it asserts is the
 * product copy from `web/workbench/src/books/`, and every request it counts is built by
 * the real `WorkbenchClient` (`web/workbench/src/manuscripts/workbench-client.ts`).
 *
 * The HTTP boundary is the only thing replaced: `page.route` answers the workbench API
 * from the committed contract fixtures in `tests/fixtures/contracts/workbench-http.json`
 * (`*.value.body` is a contract-valid example), so the page still parses real shapes
 * with the generated contract parser and a malformed answer fails the spec. The network
 * lock (`tests/web/network-lock.mjs`) lets only the two preview origins through, and
 * `installBackend` records any request this spec did not declare, so a hidden extra call
 * (a second dispatch, an automatic retry, a leaked origin) fails instead of passing.
 *
 * Reaching the panel costs four real clicks (功能 nav → book → chapter), because the
 * render unit panel is composed only inside `BooksHost` in the production bundle; there
 * is no `?dev-scenario` demo for it (the demos in `AppEntry.tsx` cover the file task
 * only). Each spec therefore drives the same composition root a user does.
 */

const workbench = "http://127.0.0.1:4174";

const fixtures = JSON.parse(
  readFileSync(new URL("../fixtures/contracts/workbench-http.json", import.meta.url), "utf8"),
);

/** The fixture's contract-valid response body for one recorded example. */
const body = (id) => structuredClone(fixtures.examples.find((entry) => entry.id === id).value.body);

/**
 * One consistent identity set across every answer.
 *
 * The examples are recorded per operation and do not share one id set (`project-demo`
 * vs `project_demo`, `unit-demo` vs `unit_demo`), while the client enforces
 * `data.every(item => item.chapter_id === chapterId)` and friends. The specs patch only
 * the identities and leave every other field exactly as the contract example states.
 */
const ids = {
  project: "project_demo",
  book: "book_demo",
  chapter: "chapter_demo",
  unit: "unit_demo",
  job: "job_behavior",
};

function responses() {
  const projects = body("listProjects-response-declared-200");
  projects.data[0].id = ids.project;

  const books = body("listBooks-response-200");
  books.data[0].id = ids.book;
  books.data[0].project_id = ids.project;

  const book = body("getBook-response-declared-200");
  book.id = ids.book;
  book.project_id = ids.project;

  const chapters = body("listChapters-response-200");
  chapters.data[0].id = ids.chapter;
  chapters.data[0].book_id = ids.book;

  const units = body("listRenderUnits-response-200-v2");
  units.data[0].id = ids.unit;
  units.data[0].chapter_id = ids.chapter;
  const unit = units.data[0];

  const accepted = body("generateRenderUnitV2-response-valid");
  accepted.render_unit.id = ids.unit;
  accepted.render_unit.chapter_id = ids.chapter;
  accepted.render_unit.status = "generating";
  accepted.render_unit.speech_job_id = ids.job;
  accepted.speech_job_id = ids.job;

  return {
    projects,
    books,
    book,
    chapters,
    units,
    unit,
    accepted,
    voices: body("listVoices-response-valid"),
  };
}

const refusalTypes = {
  401: "authentication_error",
  403: "permission_error",
  409: "invalid_request_error",
  415: "invalid_request_error",
  429: "rate_limit_error",
};

/**
 * A contract-valid `ErrorResponse`: the page parses this with the generated parser, so
 * the vocabulary the panel answers is the contract's own machine code.
 */
function refusal(status, code, { retryable = false, retryAfterMs = null } = {}) {
  const error = {
    code,
    type: refusalTypes[status],
    message: `Synthetic ${status} ${code} for the behavior gate`,
    param: null,
    request_id: "request-behavior",
    retryable,
  };
  if (retryAfterMs !== null) error.retry_after_ms = retryAfterMs;
  return { error };
}

/**
 * Install the scripted HTTP boundary and count what the page actually sends.
 *
 * `generations` is the dispatch log: one entry per POST to
 * `/v2/workbench/render-units/{unit}/generations`, carrying the request's own
 * `Idempotency-Key`. `executions` is what the refused upstream would have created: only
 * an answer this stub gives as `accepted` adds a speech Job identity, so the count of
 * started executions is a property of the scripted server, not of the page's copy.
 */
async function installBackend(page) {
  const calls = { generations: [], executions: [], reads: 0, unexpected: [] };
  const plan = {
    /** First answer and every later answer: a spec scripts both sides of a retry. */
    generate: () => ({ status: 202, json: responses().accepted }),
  };

  await page.route("**/*", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (!path.startsWith("/v1/workbench/") && !path.startsWith("/v2/workbench/"))
      return route.fallback();
    const answer = () => {
      const state = responses();
      if (path === "/v1/workbench/projects" && request.method() === "GET")
        return { status: 200, json: state.projects };
      if (path === `/v1/workbench/projects/${ids.project}/books` && request.method() === "GET")
        return { status: 200, json: state.books };
      if (path === `/v1/workbench/books/${ids.book}` && request.method() === "GET")
        return { status: 200, json: state.book };
      if (path === `/v1/workbench/books/${ids.book}/chapters` && request.method() === "GET")
        return { status: 200, json: state.chapters };
      if (
        path === `/v2/workbench/chapters/${ids.chapter}/render-units` &&
        request.method() === "GET"
      )
        return { status: 200, json: state.units };
      if (path === "/v1/workbench/voices" && request.method() === "GET")
        return { status: 200, json: state.voices };
      if (path === `/v2/workbench/render-units/${ids.unit}` && request.method() === "GET") {
        calls.reads += 1;
        return { status: 200, json: { ...state.unit, status: "generating" } };
      }
      return null;
    };
    if (
      path === `/v2/workbench/render-units/${ids.unit}/generations` &&
      request.method() === "POST"
    ) {
      const index = calls.generations.length;
      calls.generations.push(request.headers()["idempotency-key"] ?? "");
      const reply = plan.generate(index);
      if (reply.status === 202) calls.executions.push(reply.json.speech_job_id);
      return route.fulfill({
        status: reply.status,
        json: reply.json,
        headers: reply.headers ?? {},
      });
    }
    const reply = answer();
    if (reply === null) {
      calls.unexpected.push(`${request.method()} ${path}`);
      return route.fulfill({ status: 500, json: refusal(500, "internal_error") });
    }
    return route.fulfill(reply);
  });
  return { calls, plan };
}

/** Four real clicks to the panel: nav → book list → chapter list. */
async function openRenderUnits(page) {
  await page.goto(`${workbench}/`);
  await page.getByRole("button", { name: "有声书导出" }).click();
  await expect(page.getByRole("heading", { name: "有声书与导出" })).toBeVisible();
  await page
    .getByRole("list", { name: "书目列表" })
    .getByRole("button", { name: /合成有声书/ })
    .click();
  await page
    .getByRole("list", { name: "章节列表" })
    .getByRole("button", { name: /第 1 章/ })
    .click();
  const units = page.getByRole("region", { name: "渲染单元" });
  await expect(units.getByRole("list", { name: "单元列表" })).toBeVisible();
  return units;
}

/** The panel's single refusal paragraph (`RenderUnitPanel.tsx` `problem` alert). */
const problem = (units) => units.locator('p.books-problem[role="alert"]');

const dispatch = (units) => units.getByRole("button", { name: "单元 1 生成配音" });
const replay = (units) => units.getByRole("button", { name: "按原键重放单元请求" });
const dismiss = (units) => units.getByRole("button", { name: "结束已拒绝单元请求" });

/**
 * Assert the refusal paragraph is really laid out where the operator reads it.
 *
 * jsdom reports a non-zero box for every element and cannot say whether something else
 * paints over it; `elementFromPoint` at the paragraph's own centre is the browser's
 * answer to "is this the topmost element at this pixel", which is the property the
 * issue asks for (visible and not covered).
 */
async function expectLaidOutAndUnobscured(alert) {
  await alert.scrollIntoViewIfNeeded();
  await expect(alert).toBeVisible();
  const box = await alert.boundingBox();
  expect(box, "the refusal must occupy a real layout box").not.toBeNull();
  expect(box.width).toBeGreaterThan(0);
  expect(box.height).toBeGreaterThan(0);
  const covered = await alert.evaluate((element) => {
    const rect = element.getBoundingClientRect();
    const topmost = document.elementFromPoint(
      rect.left + rect.width / 2,
      rect.top + rect.height / 2,
    );
    return topmost === null || !(element === topmost || element.contains(topmost));
  });
  expect(covered, "the refusal must be the topmost element at its own centre").toBe(false);
}

/**
 * A settle window for the "nothing else fires" assertions.
 *
 * The panel has no timer that could re-dispatch, so this is the assertion's own bound:
 * a mutation that retried on a timer would have to fire inside it. It is not a
 * substitute for the request counts, which are read before and after it.
 */
async function holdForUnexpectedRetry(page) {
  await page.waitForTimeout(1200);
}

test.beforeEach(async ({ context }) => {
  await lockToPreview(context);
});

test("429：按服务端声明的延时渲染计时文案，重试按钮真的重发且不产生第二次执行", async ({
  page,
}) => {
  const backend = await installBackend(page);
  backend.plan.generate = (index) =>
    index === 0
      ? {
          status: 429,
          json: refusal(429, "render_submission_rate_limited", {
            retryable: true,
            retryAfterMs: 3000,
          }),
          // The envelope carries the delay the refusing layer named; the header is sent
          // too because the contract declares it on the 429 answer.
          headers: { "Retry-After": "3" },
        }
      : { status: 202, json: responses().accepted };

  const units = await openRenderUnits(page);
  await expectNetworkLocked(page);

  // The barrier before any dispatch: one unit button, one key, no pending record.
  await expect(dispatch(units)).toBeEnabled();
  expect(backend.calls.generations).toHaveLength(0);

  await dispatch(units).click();
  const alert = problem(units);
  await expect(alert).toBeVisible();

  // #754's copy: the 429 proves "not now", never "already generated", and the delay
  // the server stated is the operator's sentence (`render-refusals.ts`
  // `refusalStep` → "服务端要求至少等待 N 秒（Retry-After）").
  await expect(alert).toContainText("限流或容量不足");
  await expect(alert).toContainText("没有创建作业");
  await expect(alert).toContainText("至少等待 3 秒");
  await expect(alert).toContainText("（Retry-After）");
  await expect(alert).toContainText("可重试");
  await expect(alert).not.toContainText("结果尚未确认");
  await expect(alert).not.toContainText("render_submission_rate_limited");
  await expect(alert).toHaveAttribute("title", "render_submission_rate_limited");
  await expectLaidOutAndUnobscured(alert);

  // Request-count assertion (first half): the refused dispatch really left the page
  // once, and it started no execution -- the 429 answer created no speech Job.
  expect(backend.calls.generations, "one refused dispatch").toHaveLength(1);
  expect(backend.calls.executions, "a 429 creates no execution").toHaveLength(0);
  // A refused dispatch is not an in-flight one: the panel may not start polling a unit
  // whose generation never started (the poll would be an undeclared request).
  expect(backend.calls.reads, "a refused unit is not polled").toBe(0);
  await expect(units.getByText(`语音作业 ${ids.job}`)).toHaveCount(0);

  // The retry the panel offers is usable, and it is the keyed one: the panel keeps the
  // original intent so a retry reconciles instead of inventing a second dispatch.
  await expect(replay(units)).toBeEnabled();
  await expect(dismiss(units)).toBeEnabled();
  // Nothing may be dispatchable behind the retry: the unit button is the fresh-dispatch
  // path and it stays locked while the original intent is unresolved.
  await expect(dispatch(units)).toBeDisabled();

  await replay(units).click();
  await expect(units.getByText(new RegExp(`语音作业 ${ids.job}`))).toHaveCount(1);

  // Request-count assertion (second half): exactly one more request, carrying the
  // original key, and exactly one execution exists in total -- the earlier refusal
  // produced no second execution to deduplicate against.
  expect(backend.calls.generations, "the retry is one further request").toHaveLength(2);
  expect(backend.calls.generations[1], "the retry reuses the original key").toBe(
    backend.calls.generations[0],
  );
  expect(backend.calls.executions, "exactly one execution exists").toEqual([ids.job]);
  expect(backend.calls.unexpected).toEqual([]);
});

test("429：计时文案由服务端声明的延时驱动，换成 7 秒文案即变", async ({ page }) => {
  const backend = await installBackend(page);
  backend.plan.generate = () => ({
    status: 429,
    json: refusal(429, "render_submission_rate_limited", { retryable: true, retryAfterMs: 7000 }),
    headers: { "Retry-After": "7" },
  });

  const units = await openRenderUnits(page);
  await expectNetworkLocked(page);
  await dispatch(units).click();

  const alert = problem(units);
  // The same code with a different declared delay must render a different sentence:
  // a hard-coded "3 秒" cannot satisfy this and cannot satisfy the case above either.
  await expect(alert).toContainText("至少等待 7 秒");
  await expect(alert).not.toContainText("至少等待 3 秒");
  expect(backend.calls.generations).toHaveLength(1);
  expect(backend.calls.unexpected).toEqual([]);
});

test("409 idempotency_conflict：指向先查该执行再换新键，且不承诺无条件重试", async ({ page }) => {
  const backend = await installBackend(page);
  backend.plan.generate = () => ({
    status: 409,
    json: refusal(409, "render_submission_idempotency_conflict"),
  });

  const units = await openRenderUnits(page);
  await expectNetworkLocked(page);
  await dispatch(units).click();

  const alert = problem(units);
  // The copy states the two real steps: inspect the execution that owns the key, then
  // dispatch under a new key. It may not offer a plain retry of the spent key.
  await expect(alert).toContainText("请求键");
  await expect(alert).toContainText("先刷新单元状态");
  await expect(alert).toContainText("不要复用旧键");
  await expect(alert).not.toContainText("可重试");
  await expect(alert).not.toContainText("至少等待");
  await expect(alert).not.toContainText("结果尚未确认");
  await expect(alert).toHaveAttribute("title", "render_submission_idempotency_conflict");
  await expectLaidOutAndUnobscured(alert);

  // No unconditional retry: no fresh dispatch is enabled, no retry-labelled control is
  // offered, and the refused attempt is the only request. (The panel does still render
  // the keyed `按原键重放单元请求` control for a settled refusal; whether a settled
  // refusal should keep that handle is a panel question reported on #790, so this spec
  // asserts the copy and the dispatch affordances and deliberately not that handle.)
  await expect(dispatch(units)).toBeDisabled();
  await expect(units.getByRole("button", { name: /重试|重新派发/ })).toHaveCount(0);
  await holdForUnexpectedRetry(page);
  expect(backend.calls.generations, "a spent key may not be re-sent on its own").toHaveLength(1);
  expect(backend.calls.executions).toHaveLength(0);
  expect(backend.calls.unexpected).toEqual([]);
});

test("415：提示指向媒体类型配置，不提供重试", async ({ page }) => {
  const backend = await installBackend(page);
  backend.plan.generate = () => ({
    status: 415,
    json: refusal(415, "render_submission_media_unsupported"),
  });

  const units = await openRenderUnits(page);
  await expectNetworkLocked(page);
  await dispatch(units).click();

  const alert = problem(units);
  // A 415 is a deployment fact, not the unit's content: the copy names the media type
  // the page always sends and says repeating the request cannot change the answer.
  await expect(alert).toContainText("媒体类型");
  await expect(alert).toContainText("application/json");
  await expect(alert).toContainText("重复提交不会改变结果");
  await expect(alert).not.toContainText("可重试");
  await expect(alert).not.toContainText("至少等待");
  await expect(alert).not.toContainText("结果尚未确认");
  await expect(alert).toHaveAttribute("title", "render_submission_media_unsupported");
  // Layout evidence jsdom cannot give: a real box, visible, topmost at its own centre.
  await expectLaidOutAndUnobscured(alert);

  // No retry is offered: the fresh-dispatch button stays locked, nothing labelled as a
  // retry exists, and no timer or effect re-sends the refused request.
  await expect(dispatch(units)).toBeDisabled();
  await expect(units.getByRole("button", { name: /重试|重新派发/ })).toHaveCount(0);
  await holdForUnexpectedRetry(page);
  expect(backend.calls.generations, "a 415 is not re-sent").toHaveLength(1);
  expect(backend.calls.executions).toHaveLength(0);
  expect(backend.calls.unexpected).toEqual([]);
});

test("403：权限提示在真实布局里可见且不提供重试", async ({ page }) => {
  // #790 asks for 415 *and* 403: both are deployment facts whose next step is not on
  // this page, and both were only proven in jsdom. 403 shares 415's shape (terminal,
  // no retry) but not its copy -- a spec for one cannot stand in for the other.
  const backend = await installBackend(page);
  backend.plan.generate = () => ({
    status: 403,
    json: refusal(403, "render_submission_permission_denied"),
  });

  const units = await openRenderUnits(page);
  await expectNetworkLocked(page);
  await dispatch(units).click();

  const alert = problem(units);
  await expect(alert).toContainText("权限");
  await expect(alert).toContainText("由运维放开");
  await expect(alert).toContainText("重复提交都不会成功");
  await expect(alert).not.toContainText("可重试");
  await expect(alert).not.toContainText("至少等待");
  await expect(alert).not.toContainText("结果尚未确认");
  await expect(alert).toHaveAttribute("title", "render_submission_permission_denied");
  await expectLaidOutAndUnobscured(alert);

  await expect(dispatch(units)).toBeDisabled();
  await expect(units.getByRole("button", { name: /重试|重新派发/ })).toHaveCount(0);
  await holdForUnexpectedRetry(page);
  expect(backend.calls.generations).toHaveLength(1);
  expect(backend.calls.executions).toHaveLength(0);
  expect(backend.calls.unexpected).toEqual([]);
});

test("409 execution_cleanup_pending：提示等清理完成再重试，且按原键重试即可", async ({ page }) => {
  // `execution_cleanup_pending` is declared by the contract but unreachable through this
  // operation: the upstream raises it from its cancel and retry handlers only, and no
  // dispatch calls either (see `render-refusals.ts` `RENDER_REFUSAL_RETRYABLE` and
  // `_CONFLICT_CODES_RESERVED`). This spec therefore verifies the PANEL MAPPING for that
  // code -- the disposition the operator would read -- and NOT a reachable refusal path
  // of `generateRenderUnitV2`; the reachability of the code is guarded on the producer
  // side by `tests/contract/test_render_refusal_vocabulary.py`.
  const backend = await installBackend(page);
  backend.plan.generate = (index) =>
    index === 0
      ? {
          status: 409,
          json: refusal(409, "render_submission_cleanup_pending", { retryable: true }),
        }
      : { status: 202, json: responses().accepted };

  const units = await openRenderUnits(page);
  await expectNetworkLocked(page);
  await dispatch(units).click();

  const alert = problem(units);
  // The most error-prone direction of #755: a 409 cleanup wait is retryable, and it does
  // not need a new key (no Job was written for the refused dispatch).
  await expect(alert).toContainText("清理");
  await expect(alert).toContainText("可重试");
  await expect(alert).toContainText("不需要换键");
  await expect(alert).toContainText("不要把它当成结果未知");
  // No delay was declared with this refusal, so no sentence may invent one.
  await expect(alert).not.toContainText("至少等待");
  await expect(alert).not.toContainText("结果尚未确认");
  await expect(alert).toHaveAttribute("title", "render_submission_cleanup_pending");
  await expectLaidOutAndUnobscured(alert);

  // The retry is offered and really re-sends: one further request, same key, one Job.
  await expect(replay(units)).toBeEnabled();
  expect(backend.calls.generations).toHaveLength(1);
  expect(backend.calls.executions).toHaveLength(0);
  expect(backend.calls.reads).toBe(0);

  await replay(units).click();
  await expect(units.getByText(new RegExp(`语音作业 ${ids.job}`))).toHaveCount(1);
  expect(backend.calls.generations).toHaveLength(2);
  expect(backend.calls.generations[1]).toBe(backend.calls.generations[0]);
  expect(backend.calls.executions).toEqual([ids.job]);
  expect(backend.calls.unexpected).toEqual([]);
});
