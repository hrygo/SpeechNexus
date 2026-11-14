---
title: PR #769 非作者审查：文件任务面板成功路径与 409/重载恢复的浏览器行为规格（#740）
status: active
---

# PR #769（`test/740-behavior-states`）非作者审查记录

被审对象：PR #769《test: 文件任务面板成功路径与 409/重载恢复的真实浏览器规格 (#740)》，
head `09768f927e128103c64a49465f4a767301b268fe`，单 commit，相对 `origin/main`
（`edaab848`）`+254 / −5`，**1 文件**（`tests/web/file-task.behavior.spec.mjs`），
规格数 4 → 9。前置 #741（`cdf60a9e`）已建立 4 条规格并单独审查过
（`docs/reviews/2026-11-14-browser-behavior-specs.md`）。

审查方式：独立 worktree `/private/tmp/sn772`，**基于 `origin/main`（`edaab848`）拉分支**
`review/772-behavior-states`（`git rev-list --count origin/main..HEAD` 在写报告前为 0），
被审内容用 `git show 09768f92:tests/web/file-task.behavior.spec.mjs` 读出后临时覆盖到
worktree 的运行副本里实跑，**不在被审 head 上开分支**。审查只读产品代码与测试：四组变异
一律临时应用、`pnpm --filter @speechnexus/workbench build` 后跑、跑完 `git checkout --`
复原（`git status` 已确认），**未 push 任何变异**。环境用系统 node `v26.12.1`
（`/Users/hrygo/.local/share/mise/shims/node`）、pnpm `12.14.0`。

## 结论

**可以合并。十一条全部成立，无必须修项。**

这是继 #741 之后同一作者的第二张行为规格卡，同样**零产品代码改动**、同样把
「换掉 HTTP 边界」说清楚、同样没有把分类逻辑搬进测试。作者自述的每一项可核事实
（1 文件范围、21 条字面量有产品出处、状态来自两次以上真实动作、共享网络锁未改、
三条反例实测红、9/52/904/CI 全绿）我都独立复核到，**没有发现一处夸大或错报**；
唯一两处偏差是**行号引用差一**（见建议 S2）与**条数计数口径差 2**（作者表格实际列 23 条）。

三条反例我全部独立复现，红的断言位置与作者描述一致（B5 节）；我另加了两条作者没做的
变异（「首次点击就结束记录」与「只丢持久记录」）来单独验证「单次点击不生效」这组断言
是否真的会红（A3 节）。`#771`（作者发现但未修的产品缺陷）的处置边界**划得对**，
且我独立核实：**现存 9 条规格没有一条无意中钉住那条错误 notice**（D11 节）。

### 本卡两个核心问题的答案

**「21 条字面量是否都测产品」：是，全部有 `web/workbench/src/` 的产品出处，零条只在测试里拼。**
作者表格列了 23 条可见文案（正文自称 21 条，计数口径差 2），我逐条 grep，23/23 命中；
含三条模板拼接项（`下载原始制品 {artifact_id}`、`确认取消 {job.id}`、`取消请求被拒绝（${status} ${code}）…`），
它们渲染出的完整字符串在规格里出现、在产品里由模板产出，属**产品填充**而非**测试自造**。
另有 5 条结构/身份断言（`.file-task-detail`、`data-testid="file-job-status"`、
`#file-task-model`、`#file-task-input`、journal 键 `speechnexus.workbench.asr-task.v1`）
同样逐条落到产品行号。逐条表见 A1。

**「有没有测试自造状态」：没有。** 规格里唯一的 `page.evaluate` 是
`sessionStorage.getItem`（spec:139），全文无 `setItem`/`addInitScript`/直接喂 `rejected`、
`unreplayable`、`settled` 的写入；`page.route` 只注入 `(status, error code)` 这一对**分类入参**
与契约 fixture 应答。唯一「从外部造持久态」的是两条重载恢复条目，它们造的是
**产品自己 `#persist` 写出的形态**，且**都先有正控**（重载前断言 journal 的 `settled`
已经存在，见 A2）。唯一由测试直接断言的内部字段是 journal 的 `settled`/`pending`，
那是**读**产品写入结果，不是写。

## 十一条逐条判定

| # | 检查项 | 判定 | 关键证据 |
| --- | --- | --- | --- |
| A1 | 字面量逐条有产品唯一出处 | ✅ | 23/23 命中（含 3 条模板拼接），另 5 条结构断言命中 |
| A2 | 状态来自两次以上真实动作、非直接设字段 | ✅ | 唯一 `evaluate` 是 `getItem`；两条重载条目有正控 |
| A3 | 「只点一次不生效」证据真的会红 | ✅ 但判据层次需说明 | 变异 D1：4 条红（首红 `spec:358` 确认组不可见）；journal 断言单独可红（`spec:363`） |
| A4 | `job_retry_not_allowed` 钉的是分类边界 | ✅ 有效（非后端应答覆盖） | `settlesCancelAttempt` `:241-250` 只收 3 个码；契约 `JobCancellationConflict` 只列 2 个取消码 |
| B5 | 三条反例独立复现 | ✅ 3/3 | 见 B5，红在预期断言，位置与作者一致 |
| B6 | 弱化网络锁使全部 9 条红 | ✅ 9/9 | 9 failed，全部 `network-lock.mjs:43`，`net::ERR_CONNECTION_CLOSED` |
| C7 | `network-lock.mjs` 未改动 | ✅ | blob 同一 `1e6c35d2…`，`git diff` 0 行 |
| C8 | a11y 52 条仍绿且未被影响 | ✅ | 本地 52 passed；CI 52 passed；两 gate 共用同一 `previewServers` |
| D9 | 只改 1 文件、零产品/契约/后端、无新依赖 | ✅ | `git diff --name-status` 仅 1 个 M；锁文件 0 改动 |
| D10 | 例数与 CI 实跑 | ✅ | 9 / 52 / 904；run `43585428915` 全绿，Integration 0–5 六片全绿 |
| D11 | 对 `#771` 的处置边界 | ✅ 判断正确 | 未钉错误文案；现存 9 条无一触发该分支 |

### A1 字面量核对表（23 条可见文案 + 5 条结构断言）

全部用 `grep -rn --include=*.ts --include=*.tsx -F <literal> web/workbench/src/` 实跑，
下表「产品命中」列只保留非 `*.test.*` 的行：

| 规格断言的字面量 | 产品命中（唯一出处） |
| --- | --- |
| `任务已接收。读取当前状态或连接任务事件。` | `file-task-session.ts:1015` |
| `已重新读取当前任务。` | `file-task-session.ts:1119` |
| `任务与原始结果` | `FileTaskPanel.tsx:93` |
| `原始转写制品` | `FileTaskPanel.tsx:227`（`aria-label`）/`:228`（`h3`，同一可见文案） |
| `下载本任务已提交的原始 JSON，单个制品上限 16 MiB。` | `FileTaskPanel.tsx:240` |
| `导入到项目稿件` | `FileTaskPanel.tsx:237` |
| `下载原始制品 artifact-demo` | **模板**：`FileTaskPanel.tsx:251` `下载原始制品 {artifact.artifact_id}`；`artifact-demo` 来自契约 fixture `succeeded-job` |
| `准备新任务` | `FileTaskPanel.tsx:265`（按钮；`:101` 是 notice 内引用同一词的文案） |
| `确认本页新任务`（`role=group`） | `FileTaskPanel.tsx:269`（`aria-label`） |
| `结束本页任务恢复并新建？服务器上的原任务与制品继续保留。` | `FileTaskPanel.tsx:277` |
| `保留本页任务` | `FileTaskPanel.tsx:291` |
| `结束本页任务并新建` | `FileTaskPanel.tsx:288` |
| `选择输入开始任务。` | `file-task-session.ts:338` |
| `尚未确认`（`file-job-status` 缺省值） | `FileTaskPanel.tsx:118`（该词全仓 32 处，但**这个 testid 唯一**） |
| `原请求已被协议确定性拒绝，原意图作废` | `FileTaskPanel.tsx:100` |
| `刷新也不会再变回` | `FileTaskPanel.tsx:100`（同一行） |
| `已找到原任务身份；该取消请求已被协议确定性拒绝并作废。请读取原任务核对，再显式收尾。` | `file-task-session.ts:424`（`settledCancel` 分支） |
| `取消请求被拒绝（409 job_retry_not_allowed），…请读取原任务。` | **模板**：`file-task-session.ts:799`（`${contractError.status} ${contractError.code}`） |
| `请求结果尚未确认。请核对原素材或任务；重放只使用原输入和原键。` | `FileTaskPanel.tsx:96` |
| `已收到取消响应；资源释放以 cleanup_state 为准。` | `file-task-session.ts:1161` |
| `原请求因请求体超过本部署的接收上限被拒绝` | `FileTaskPanel.tsx:106` |
| `已找到原任务身份；该请求因请求体超过本部署的接收上限被拒绝，原意图作废。` | `file-task-session.ts:422`（`settled === "unreplayable"` 分支） |
| `这次请求被拒绝，原意图作废` | `FileTaskPanel.tsx:272`（收尾组内文案） |
| `放弃这次意图并新建` | `FileTaskPanel.tsx:288`（`state.rejected` 分支） |
| `读取原状态` | `FileTaskPanel.tsx:165` |
| `准备取消` | `FileTaskPanel.tsx:205` |
| `确认取消原任务`（`role=group`） | `FileTaskPanel.tsx:209` |
| `确认取消 job-demo` | **模板**：`FileTaskPanel.tsx:219` `确认取消 {job.id}` |
| `按原键重放` | `FileTaskPanel.tsx:172` |
| `.file-task-detail` | `FileTaskPanel.tsx:89`（`className`） |
| `data-testid="file-job-status"` | `FileTaskPanel.tsx:118` |
| `#file-task-model` / `#file-task-input` | `FileTaskInput.tsx:39` / `:49` |
| `文件转写`（heading） | `FileTaskInput.tsx:24` |
| journal 键 `speechnexus.workbench.asr-task.v1` | `FileTaskHost.tsx:17` 模板 `speechnexus.workbench.${kind}-task.v1`，`kind` 为 `asr` |

三点说明：

1. **模板拼接项不是测试自造。** 作者在 PR 正文里自己举的例子就是
   `下载原始制品 artifact-demo`：字符串的**骨架**由产品模板（`:251`）产出，`artifact-demo`
   来自被断言的那个 Job 自己的 `artifact_id`（契约 fixture `succeeded-job`
   `artifacts[0].artifact_id`）。同样 `确认取消 job-demo`（`:219` 用 `job.id`）、
   `取消请求被拒绝（409 …）`（`:799` 用 `contractError.status/code`）。
   这比「测试里拼字符串去匹配产品」更强：如果产品把模板改成别的措辞，或 status/code
   取错来源，这三条都会红（B5-M1 就是这么红的，见下）。
2. **`尚未确认` 与 `准备新任务` 不是全仓唯一字符串**（分别 32 处 / 17 处，含测试文件与
   `dev/FileTaskDemo.tsx`）。但规格的定位器把它们收敛成唯一：前者用
   `getByTestId("file-job-status")`（`:118` 唯一 `data-testid`），后者用
   `getByRole("button", { name: "准备新任务", exact: true })`；且 `dev/` 下的 demo 由
   `import.meta.env.DEV` 门控、不进 `vite preview`（spec 头注释已说明）。
   **「唯一出处」在断言粒度上成立，在裸字符串粒度上不成立**——这一点作者表格没写明，
   但也不影响任何一条断言的有效性。
3. 我抽查了 **24 条**（上表全部 + `上传并转写`），无一落空；没有发现任何一条只在测试里
   拼出来、产品侧无对应渲染的断言。

### A2 「状态来自两次以上真实动作」逐条核对

先给全局判据：**规格对页面施加的写操作只有真实 UI 动作**（click / `setInputFiles` /
`reload`），对存储只有读。

```
$ grep -n "setItem|evaluate|addInitScript|localStorage|sessionStorage" /tmp/pr769-spec.mjs
139:const journal = (page) => page.evaluate((key) => sessionStorage.getItem(key), journalKey);
```

逐条（5 条新规格）：

| 规格 | 动作序列 | 是否直接设字段 | 持久态正控 |
| --- | --- | --- | --- |
| A 成功路径 | ① 提交（202 `queued-job`）② 点「读取原状态」（200 `succeeded-job`） | 否 | 不需要（状态全来自应答） |
| B 成功路径收尾 | ① 提交 ② 读取→`succeeded` ③ 点「准备新任务」④ 点「保留本页任务」⑤ 再「准备新任务」→「结束本页任务并新建」 | 否 | `durable` 先 `not.toBeNull()`（`:352`）再用于比较（`:363/:371`） |
| C 409 `idempotency_conflict` | ① 提交 ② 「准备取消」→「确认取消 job-demo」（409）③ 重载 | 否 | 重载前 `persisted.settled === "terminal"` 且 `not.toBeNull()`（`:413-415`） |
| D 非结算 409 | ① 提交 ② 两步取消（409 `job_retry_not_allowed`）③ 点「按原键重放」（202） | 否 | `persisted.pending === "cancel"` 且**无** `settled` 属性（`:466-468`） |
| E 413 首答重载 | ① 提交（413）② 重载 ③ 「准备新任务」→「放弃这次意图并新建」 | 否 | 重载前 `settled === "unreplayable"`（`:497`），重载后再断言一次（`:511`） |

两条重载恢复条目（C、E）**必然要从外部造持久态**，核实它们是产品写出的形态而非测试
构造的形态：

- C 的 `settled: "terminal"` 来自产品 `file-task-session.ts:811`（`if (settles) #persist({ settled: "terminal" })`），
  且 `settledCancel = settled === "terminal" && record.pending === "cancel"`（`:405`）才走
  `:424` 的取消文案——规格正是靠「先点两步取消」让 `pending` 停在 `"cancel"`。
- E 的 `settled: "unreplayable"` 来自 `:722`（413 非首答分支）。
- 两条都**先断言 journal 里已有该值、再 `reload()`**，所以「重载后看到文案」不是靠空 journal
  恰好成立——这正是 #741 审查里点出的「正控」写法，这里被继承下来且用对了。

### A3 「只点一次不生效」的证据是否真的证明它

规格里的证据（`:356-365`）：

```
356  await finishButton(page).click();                       // 第 1 次点击
358  await expect(confirmFinish).toBeVisible();              // 只展开确认组
363  expect(await journal(page), "one click may not drop the page record").toBe(durable);
364  await expect(page.getByTestId("file-job-status")).toHaveText("succeeded");
365  expect(backend.calls.creates, "one click may not submit a new intent").toHaveLength(1);
```

这三条断言**都在规格里、都会红**，但**红的顺序有意义**，作者没说明这层：

- **变异 D1**（把 `准备新任务` 的 `onClick` 直接改成 `onNew()`，即「单击就结束记录」）：
  `4 failed`，目标条首红在 **`spec:358`「确认组不可见」**，而不是 journal 断言——
  因为 `onNew()` 会 `setGeneration(+1)` 让面板重挂载。
- **变异 D2**（`onNew` 清记录但不重挂载，模拟「忘了重挂载」的回归）：红在
  **`spec:384`**（`#file-task-model` 未被清空），说明尾部重置断言也是活的。
- **变异 D3（定向探针）**：让第 1 次点击在**仍展开确认组**的同时清掉 journal，唯一被撞的
  就是 `:363`：

```
Error: one click may not drop the page record
Expected: "{\"model\":\"asr-quality\",…,\"jobId\":\"job-demo\",\"pending\":null,…}"
Received: null
> 363 |   expect(await journal(page), "one click may not drop the page record").toBe(durable);
```

结论：**「单次点击不生效」被真实证明**，但主判据是**确认组可见性**（`:358`），
journal / `creates` / 状态三条是**顺序在后的加固断言**（现实回归会先撞 `:358`）。
这不是缺陷，只是作者把「证据」写成了三条加固断言，实际最强的一条是可见性。
变异 D3 是**人为构造的定向探针**（不是可信回归），目的是证明该断言非空洞。

### A4 `job_retry_not_allowed` 钉的到底是什么（分类边界判定）

三份证据逐层看：

1. **产品判据**（`file-task-session.ts:241-250`）只结算三种应答：
   `404 job_not_found`、`409 execution_cleanup_pending`、`409 idempotency_conflict`。
   其余一律走 `refusalProvesNothing` 分支（`:786-801`），保留 `unknown`、原键和 re播放句柄。
2. **后端实际发出的取消码**（`interfaces/http/jobs.py:_cancel`，`:600-614`）：
   `execution_cleanup_pending`、`idempotency_conflict`、`job_outcome_unknown`、
   `job_not_found`、`job_unavailable`——**没有 `job_retry_not_allowed`**；
   后者只由 `_retry`（`:642`）发出。作者自述准确。
3. **契约层（作者没引的一条更硬的证据）**：`contracts/speech-openapi.yaml:2891-2900`
   的 `JobCancellationConflict` 描述里，取消 409 **只列了 `execution_cleanup_pending`
   与 `idempotency_conflict`**。也就是说 `409 job_retry_not_allowed` 不只是「当前后端恰好不发」，
   而是**不在契约声明的取消 409 词汇里**。

**判定：算有效覆盖，但不是对「后端取消应答」的覆盖。** 理由：

- 它驱动的是**真实产品判据**（`settlesCancelAttempt` 默认分支）+ **真实持久化路径**，
  断言的是浏览器层独有的后果：`pending: "cancel"` 留在 journal、无 `settled`、页面保持
  未知、重放用**原键**（`cancels[1] === cancels[0]`）。这一层后果 `file-task-rejection.test.ts`
  的单测表（`:1032-1052`，已有 `[404 job_not_found, true]` 与多条 `false`）**测不到**，
  所以本条目有增量价值。
- 但它**不能**被读成「服务器会在取消路径上这样回答」。要同时体现后端事实，同一分支可用
  契约允许的 `400 invalid_job_request` / `401`（`_cancel` 的 400/401 走同一条
  `refusalProvesNothing` 分支，渲染同一模板），代价为零。见建议 S1。
- 残余空白：结算判据的**第三个成员 `404 job_not_found` 在浏览器层仍无规格**（只有单测），
  即「接受集」只被 2/3 覆盖。不阻塞（单测覆盖了判据本身），但值得知道。

### B5 三条反例独立复现（3/3，红在预期断言）

变异均在 `/private/tmp/sn772` 内临时应用，`pnpm --filter @speechnexus/workbench build`
后跑 `pnpm test:behavior`，跑完 `git checkout --` 复原。

| 变异 | hunk / 行数 | 结果 |
| --- | --- | --- |
| **M1** `settlesCancelAttempt` 多认一个码：整条 `return error.status === 409;` | 1 hunk / −4 +1 | **1 failed**：`spec:436`「结算码之外的 409 不结算」 |
| **M2** 改产品文案一个字：`任务已接收。` → `任务已接受。`（`file-task-session.ts:1015`） | 1 hunk / −1 +1 | **1 failed**：`spec:300`「成功路径」红在 `:312` |
| **M3** 弱化网络锁：`route.abort("blockedbyclient")` → `route.continue()` | 1 hunk / −1 +1 | **9 failed**，全部 `network-lock.mjs:43` |

M1 输出（完整）：

```
  1) tests/web/file-task.behavior.spec.mjs:436:1 › 结算码之外的 409 不结算：保持未知并按原键重放取消
    Expected: "取消请求被拒绝（409 job_retry_not_allowed），但该拒绝不能证明原取消未生效；保留原键与未知状态，请读取原任务。"
    Received: "Speech response 409: job_retry_not_allowed"
    Call log:
      - Expect "toHaveText" locator('.file-task-detail > p[role=status]').first() with timeout 5000ms
        - locator resolved to <p role="status">任务已接收。读取当前状态或连接任务事件。</p>
        - unexpected value "任务已接收。读取当前状态或连接任务事件。"
        13 × locator resolved to <p role="status">Speech response 409: job_retry_not_allowed</p>
      454 |   // The refusal rules out this answer, not the earlier request: the intent keeps its
      455 |   // unknown state, its original key and its replay handle (file-task-session.ts:795-805).
    > 456 |   await expect(notice(page)).toHaveText(
        |                              ^
      457 |     "取消请求被拒绝（409 job_retry_not_allowed），但该拒绝不能证明原取消未生效；保留原键与未知状态，请读取原任务。",
      458 |   );
        at /private/tmp/sn772/tests/web/file-task.behavior.spec.mjs:456:30
  1 failed
    tests/web/file-task.behavior.spec.mjs:436:1 › 结算码之外的 409 不结算：保持未知并按原键重放取消 ──────────────────────
  8 passed (8.2s)
```

M2 输出（完整）：

```
  ✘  5 tests/web/file-task.behavior.spec.mjs:300:1 › 成功路径：提交得到 Job，读取后结果与制品可见 (5.4s)
  1) tests/web/file-task.behavior.spec.mjs:300:1 › 成功路径：提交得到 Job，读取后结果与制品可见 ────────────────────────
    Error: expect(locator).toHaveText(expected) failed
    Expected: "任务已接收。读取当前状态或连接任务事件。"
    Received: "任务已接受。读取当前状态或连接任务事件。"
        14 × locator resolved to <p role="status">任务已接受。读取当前状态或连接任务事件。</p>
        at /private/tmp/sn772/tests/web/file-task.behavior.spec.mjs:312:30
  1 failed
    tests/web/file-task.behavior.spec.mjs:300:1 › 成功路径：提交得到 Job，读取后结果与制品可见 ─────────────────────────
  8 passed (7.4s)
```

M3（B6 的同一变异）输出摘录：

```
  Error: Network lock refused without Chromium's blocked-by-client error: net::ERR_CONNECTION_CLOSED
     at network-lock.mjs:43
  > 43 |     throw new Error(`Network lock refused without Chromium's blocked-by-client error: ${failure}`);
         |           ^
  9 failed
```

**与作者自述一致**：作者的反例 2 改的是另一条文案（`已重新读取当前任务。` 去掉句号，红在
`:319`），我用的是 `任务已接收→接受`（红在 `:312`）——两条互相独立、结论相同，说明「改一个
字就红」不是对某一条文案的特例。

### B6 弱化网络锁使全部 9 条红（成立）

```
9 failed —— 每条都红在 tests/web/network-lock.mjs:43
Error: Network lock refused without Chromium's blocked-by-client error: net::ERR_CONNECTION_CLOSED
```

与作者描述逐字一致（9 failed / `network-lock.mjs:43` / `ERR_CONNECTION_CLOSED`）。
这条同时证明两件事：**新加的 5 条规格都调用了 `expectNetworkLocked`**（不是只让老 4 条守着），
且探针能区分「客户端拦截」与「真实网络失败」。

### C7 共享网络锁未改动（成立）

```
$ git diff --stat origin/main...09768f92
 tests/web/file-task.behavior.spec.mjs | 259 +++++++++++++++++++++++++++++++++-
$ git diff origin/main...09768f92 -- tests/web/network-lock.mjs | wc -l
0
$ git rev-parse origin/main:tests/web/network-lock.mjs 09768f92:tests/web/network-lock.mjs
1e6c35d25d35c54b522129c0d750249a141e73a2
1e6c35d25d35c54b522129c0d750249a141e73a2
```

同一 blob，零行改动。a11y 的锁因此既没收紧也没放宽。

### C8 `test:a11y` 52 条仍绿、未被新规格影响（成立）

- 本地（顺序跑，**先 behavior 后 a11y**）：`52 passed (18.2s)`。
- CI：`52 passed (58.4s)`。
- 是否被影响：两个 gate 共用 `tests/web/preview-servers.mjs`（同两端口、同
  `--strictPort` + `reuseExistingServer:false`），本 PR **没有改**该文件、没有改
  `playwright.a11y.config.mjs`、没有改 `network-lock.mjs`，所以 a11y 的端口/构建方式
  与合并前逐字相同。`playwright.behavior.config.mjs` 的 `outputDir: "test-results/behavior"`
  是 #741 引入的既有隔离，本 PR 未动。
- **顺序的事实更正**：CI 里 a11y 步骤在 behavior **之前**
  （`ci.yml:924` `Check rendered accessibility and contrast`，
  `ci.yml:947` `Check file task state transitions in a real browser`；步骤注释也写着
  "the accessibility gate that runs before it"）。卡面转述「a11y 是 behavior 之后跑的」
  与 CI 实际相反。两者串行、共用同一份已构建产物，**两种顺序我都实测绿**，结论不受影响，
  但转述需要修正。

### D9 范围与依赖（成立）

```
$ git diff --name-status origin/main...09768f92
M	tests/web/file-task.behavior.spec.mjs
```

`web/workbench/src/`、`contracts/`、`backend/`、`.github/`、`playwright.*.config.mjs`、
`tests/web/network-lock.mjs`、`preview-servers.mjs`、`package.json` 全部 0 文件；
`uv.lock` / `pnpm-lock.yaml` 0 改动（`git diff --stat uv.lock pnpm-lock.yaml` 空）。
`pnpm install --frozen-lockfile` 在本 worktree 上直接成功（359ms），无锁文件漂移。
无新增依赖。

### D10 例数与 CI（成立，且比题面更全）

本地（本 worktree，`origin/main` + 被审 spec）：

| 命令 | 结果 |
| --- | --- |
| `pnpm build` | 通过 |
| `pnpm test:behavior` | **9 passed**（3.6s），连跑 3 次 `9 passed (3.6s)` 无 flaky |
| `pnpm test:a11y` | **52 passed**（18.2s） |
| `pnpm test` | workbench **904 passed / 63 files**，admin **151 passed / 19 files**，contracts 全绿 |
| `pnpm typecheck` / `pnpm lint` / `pnpm format:check` | 全绿（`lint` 含 `tests/web` 的 eslint，`format:check` 含本 PR 的 spec） |

CI（`gh pr checks 769`，run `43585428915`）：**20 项全部 pass / skipping，零 fail**。关键项：

```
Web: lint, types, tests, build and accessibility   pass  3m49s
Integration 0..5: PG/MySQL/MinIO                  pass  9m32s / 9m51s / 9m38s / 9m53s / 13m4s / 6m26s
Gate: six-shard integration evidence and merged coverage   pass  36s
Gate: required checks for this change             pass  3s
Python: tests, coverage and package build         pass  7m39s
```

即 `full` 路由的**六片全绿**（不是「4/5 绿」）。Web job 日志逐行核到：

```
Check rendered accessibility and contrast   →  52 passed (58.4s)
Check file task state transitions in a real browser →  9 passed (9.1s)
Run workspace unit and component tests → workbench Tests 904 passed (904) / admin 151 passed (151)
```

作者说题面里的「902」已过期、当前基线是 904：**我本地与 CI 都读到 904**，成立。

### D11 对 `#771` 的处置判定（判断正确）

作者在 PR #769 的自述里报告、但**没有修**、也**没有在规格里钉**这条产品缺陷：
`file-task-session.ts` 的重载 `notice` 三段选择里，最后那个 `else` 同时兜住
`settled === "terminal"`（create）与 `settled === undefined`，于是「崩溃窗口」
（`pending` 有值、无 `settled`）重载后同页出现
`role=alert`「请求结果尚未确认…」+ `role=status`「已找到原任务身份；该意图已被协议确定性拒绝并作废。」
且 `canReplay` 仍为 true（可重放）。已开 `#771`（OPEN，正文含四种
`(pending, settled)` 组合 × 四个字段的验收标准与三条反例）。

我的独立判断：**边界划得对**，理由三条：

1. **规格不该钉已知错误行为。** 把矛盾文案写进 `toHaveText` 会让绿锁把缺陷固定住，
   修 #771 时必须先改规格——而规格的作用是防回归，不是防修复。
2. **这条 notice 不是本 PR 引入的**，也不在 #740 的 5 条转换里；本 PR 的重载条目
   （C、E）故意走 `settled` 存在的两条分支，**没有一条触发那个 `else`**。我逐条核对
   `#restore`（`:405-435`）：C 命中 `settledCancel` 分支（`:424`）、E 命中
   `unreplayable` 分支（`:422`），都不碰 `else`。
3. **风险是「以后有人无意钉住它」，而 #771 的 AC 已经覆盖**
   （它明确要求修完保持 9 条绿、必要时补一条）。本轮我另外确认：现存 9 条**没有**任何一条
   在 `pending` 有值且无 `settled` 的状态下 `reload()`，所以当前不存在隐性钉子。

唯一可以更强的地方（不阻塞）：规格头注释只写了「状态总由两次以上真实动作产生」，
没写「本规格刻意不覆盖 #771 的崩溃窗口 notice」。加一句会省下一次这样的审查提问——
但 PR 正文与 #771 正文都已写明，我不认为这是必须修。

## 必须修清单

**无。**

十一条全部成立，`gh pr checks 769` 20 项全绿，本地 9 / 52 / 904 全绿，
三条反例独立复现且红在作者描述的位置，`network-lock.mjs` 与锁文件均未动，
`#771` 的边界判断正确且不留下隐性钉子。

## 建议（不阻塞）

- **S1（A4）**：D 条用的 `409 job_retry_not_allowed` 按契约不在取消 409 的词汇里
  （`speech-openapi.yaml:2891-2900` 只列 `execution_cleanup_pending` /
  `idempotency_conflict`）。同一分支与 `400 invalid_job_request` / `401` 完全等价
  （都走 `refusalProvesNothing`，渲染同一模板），换成契约允许的码可让这条同时体现后端事实；
  若要保留现码，建议把注释里的依据从 `job_errors.py`（通用错误表）补到
  `JobCancellationConflict`（取消专属契约）。零成本，非阻塞。
- **S2（引用精度）**：两处行号差一：`job_errors.py:28-31` → 实际 `:27-31`；
  `FileTaskPanel.tsx:252` → 文案实际在 `:251`（`<button>` 为 `:245-252`）。
  其余引用（`file-task-session.ts:241-250/338/422/424/717-724/795-805/1015/1119/1161`、
  `FileTaskPanel.tsx:93/96/100/106/118/165/205/209/219/227-228/237/240/251/265/269/272/277/288/291`、
  `file-task-policy.ts:22-23`、`FileTaskHost.tsx:162-172`、`FileTaskInput.tsx:24/39/49`）
  我逐条比对，**全部准确**。
- **S3（证据表述）**：A3 的「单次点击不生效」实际主判据是确认组可见性（`spec:358`），
  journal/`creates`/状态三条顺序在后、属加固断言。若想让最强证据更显眼，可把
  `:363` 的 journal 断言移到 `:358` 之后立即执行——不改变结论，只是让首红点更贴近语义。
- **S4（计数口径）**：正文说「21 条字面量」，表格实际列 23 条，我核到 24 条（含
  `上传并转写`）。差 2 属口径差异（可能没算两条复用文案），不影响任何判定，但
  下一轮若引用这个数字，建议以表格为准。

## 只能依赖作者陈述/本轮未覆盖的点

- **真实后端联调**：`page.route` 替换 HTTP 边界，应答是仓库既有契约 fixture
  （`tests/fixtures/contracts/file-workflow.json`，由生成绑定测试逐条对源 schema 校验）。
  「真实网关是否会以这些 (status, code) 回答」不在本轮证据内——作者已在「未验证项」自认，
  我复核了这段自认是准确的（含 D 条的 `job_retry_not_allowed` 只由 retry 发出）。
- **CI 的 browser 二进制一致性**：CI 用
  `scripts/dev-env/prepare-ci-browser.py` 装 Chromium headless shell，我在本机用已装
  chromium 跑同一配置，版本差异未核（不影响断言语义：都是 Chromium 的
  `ERR_BLOCKED_BY_CLIENT` 前缀）。
- **`#771` 的修复**：本 PR 不含，按 #771 单独验收；本轮的结论只是「不该由本 PR 修/钉」。

## 时间线（本地时钟 Asia/Shanghai）

1. `18:47` 从 `origin/main`（`edaab848`，含 #765）建 worktree `/private/tmp/sn772` +
   分支 `review/772-behavior-states`；`git fetch` 后主线前进 1 个提交（`56d2e05e..edaab848`）。
2. `18:47–18:48` `gh pr view 769` / `gh api` 确认报告文件名未被占用
   （`docs/reviews/` 41 个文件，无 `2026-11-14-behavior-states.md`；同系列只有
   `2026-11-14-browser-behavior-specs.md` = #741）。`git diff --name-status` 确认 1 文件。
3. `18:48` `pnpm install --frozen-lockfile`（359ms）。`gh pr checks 769` 20 项全绿。
4. `18:48–18:49` `pnpm build` → 覆盖被审 spec → `pnpm test:behavior` **9 passed (3.6s)**
   → `pnpm test:a11y` **52 passed (19.1s)**（顺序跑，未并发）。
5. `18:49–18:52` A1 逐条 grep（`node` 脚本批量，24 条）；A2 读 `#restore`/`#persist`/
   `canFinishTask`/`FileTaskHost.onNew`；A4 读 `_cancel`/`_retry`/`job_errors.py`/
   `JobCancellationConflict`。
6. `18:52–18:55` 变异 M1（分类放宽）→ 1 failed；M2（改一字文案）→ 1 failed；
   M3（弱化网络锁）→ 9 failed；D1/D2/D3（单击生效的两个变体 + 定向探针）→
   首红 `spec:358` / `spec:384` / `spec:363`。每次跑完 `git checkout --` 复原。
7. `18:55–18:56` `pnpm test`（904 / 151）、`pnpm typecheck`、`pnpm lint`、
   `pnpm format:check` 全绿；最终 `pnpm build` → 连跑 3× `9 passed (3.6s)` →
   `pnpm test:a11y` `52 passed (18.2s)`。
8. `18:56–19:0x` 写本报告；`uv run --no-project ruff format --check` 复核；单文件提交、
   push 分支、开 PR（不合并、不动 `main`）。
