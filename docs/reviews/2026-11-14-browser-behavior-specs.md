---
title: PR #741 非作者审查：文件任务面板的浏览器行为规格（#740）
status: active
---

# PR #741（`test/740-behavior-specs`）非作者审查记录

被审对象：PR #741《test: 文件任务面板的四条关键状态转换加真实浏览器规格 (#740)》，
head `c5fa60f39f0319b6c43e0e86bdcde5e1033b23f0`，单 commit，相对 `origin/main`
（merge-base `d58127c027`，落后主线 2 个提交）`+394 / −21`，9 文件。

审查方式：独立 worktree `/private/tmp/sn744`、**基于 `origin/main`**
（`6359ff28f3`）拉分支 `review/744-behavior-specs`；被审内容在 detached 快照
`/private/tmp/sn743`（`c5fa60f3`，含 `node_modules` 与已构建 `dist`）内读取与实跑，
不在被审 head 上开分支。审查只读：未改被审产品代码与测试；变异一律在快照内临时应用、
跑完 `git checkout --` 复原，未 push 任何变异。环境用系统 node `v26.12.1`
（`/opt/homebrew/bin/node`），避开 dsh 自带 node 24 与 rolldown native binding 的
macOS Team ID 校验冲突。

## 结论

**可以合并。十三条全部成立，作者的每一条可核断言我都独立复核到，且独立复现了它声称的
三条反例——红的断言位置与作者描述逐字一致。**

这是审查过的少数「自述与实测完全对齐」的卡：`零产品代码改动`、`四条转换都走产品代码`、
`a11y 未削弱`、`CI 未放宽路由`、`无新增依赖`、`连续三次不 flaky` 六条主张全部为真；
更重要的是，**它没有一处把「换掉 HTTP 边界」包装成「端到端联调」**——PR 正文的
「未验证项」自己就写明「真实后端 + 真实语音服务仍未联调」「413 的真实触发未在真实网关
复现，只按契约应答建模」。本卡的核心质疑（测的是产品还是桩）因此有明确答案：**测的是
产品**，理由见下节。

无必须修项。两条建议（不阻塞）见「建议」。

### 本卡两个核心问题的答案

**测的是产品还是桩：测的是产品。** 规格断言的每一个字符串、class、DOM 结构与
`sessionStorage` 键，都能在 `web/workbench/src/` 找到唯一出处（A2 逐条 grep，21 条
全部命中，零条只在测试里拼）；三条关键状态文案由**产品 session 层**在分类后写入
`state.notice` / `state.rejected`，面板只做条件渲染。反例可证：把 `rejectionDisposition`
的 413 分支改回 `unknown-outcome`，第 1 条立刻红在第三态文案（B5-M2），说明文案并非
测试自造、而是产品按分类产出的分支结果。

**分类逻辑是否被绕过：没有被绕过，但分类的**输入**是测试选的。** 规格只替换 HTTP 边界，
`page.route` 注入的是 `(HTTP status, contract error code)` 这一对**分类入参**；
`terminal / unknown-outcome / intent-kept / unreplayable` 的判定完全发生在产品代码
（`file-task-session.ts:96-148`）。因为规格没有直接把 `rejected` / `unreplayable` 状态
喂给 UI，状态的产生路径与真实运行相同。第 2 条（非首答）尤其干净：它由两次真实的
`#submit()` 产生——第一次 `route.abort("failed")` 走完「持久化 → 标记已发出 → 请求失败」
全流程，第二次重放才让 `#createAttemptIsFirst` 为 `false`（A4）。**残余风险不在分类
逻辑，而在「真实网关是否真的发出这一对 (status, code)」——这一点作者已在未验证项里
自认。**

## 十三条逐条判定

| # | 检查项 | 判定 | 关键证据 |
| --- | --- | --- | --- |
| A1 | 零产品代码改动 | ✅ 成立 | `git show c5fa60f3 --stat` 9 文件，`web/workbench/src/` 下 0 个 |
| A2 | 断言文案/结构由产品产生 | ✅ 成立 | 21 条断言全部 grep 到产品出处，无一条只存在于测试 |
| A3 | 分类在产品代码里 | ✅ 成立 | `rejectionDisposition` `file-task-session.ts:96-148`；409 cancel `:120`；413 `:134-135` |
| A4 | 非首答走产品判据 | ✅ 成立 | `#createAttemptIsFirst` `:331/:972/:1011`，`#createEverIssued` `:329/:358/:1012`，守卫 `:685` |
| B5 | 独立复现反例 | ✅ 3/3 复现 | 三条全复现，红在预期断言；另加网络锁第 4 条 |
| B6 | 变异单点 | ✅ | 每条 1 hunk，1 行或 −2/+1，见 B5 表 |
| C7 | 三次连跑不 flaky | ✅ | 3×4 passed，2.2s，单测 439–637ms |
| C8 | 网络锁真的拒掉非本机 | ✅ | 削弱锁后 4/4 红在 `ERR_BLOCKED_BY_CLIENT` 判据（非空断言） |
| C9 | CI 接法 + 路由 | ✅ | 日志 4 passed；`ci-routing.py` 未被本 PR 修改，`full` 是既有规则 |
| D10 | 共享改造不削弱 a11y | ✅ 等价 | `["4173","4174"].includes` → `Set.has`，语义与 abort 理由逐字等价 |
| D11 | a11y 仍全绿 | ✅ | 本地 52 passed；CI 日志 52 passed |
| E12 | 范围/依赖/锁文件 | ✅ 附一条记录 | 无越界；`package.json`+`eslint.config.js` 为必要配套（见下） |
| E13 | fixtures 来源 | ✅ 既有且被钉住 | 主线既有（#424 起），152 examples，由 `test_generated_bindings.py` 逐条对源 schema 校验 |

### A1 零产品代码改动（成立）

```
$ git show c5fa60f3 --stat
 .github/workflows/ci.yml              |  21 +++
 eslint.config.js                      |   2 +-
 package.json                          |   3 +-
 playwright.a11y.config.mjs            |  16 +-
 playwright.behavior.config.mjs        |  34 +++++
 tests/web/file-task.behavior.spec.mjs | 266 +++++++++++++++++++
 tests/web/network-lock.mjs            |  44 +++++
 tests/web/pages.a11y.spec.mjs         |   8 +-
 tests/web/preview-servers.mjs         |  21 +++
 9 files changed, 394 insertions(+), 21 deletions(-)
```

`web/workbench/src/`、`contracts/`、`backend/`、`scripts/dev-env/` 全部为 0 个文件。
「零产品代码改动」成立，且这是本卡最容易被含糊过去的点——它没有含糊。

### A2 断言字符串的产品出处（21/21 命中，逐条）

| 规格里的断言 | 产品出处 |
| --- | --- |
| `该意图此前已有提交尝试` | `file-task-session.ts:704`（notice 模板） |
| `请求结果尚未确认` | `FileTaskPanel.tsx:96`（`state.unknown` 的 `p[role=alert]`） |
| `原请求因请求体超过本部署的接收上限被拒绝` | `FileTaskPanel.tsx:106`（`state.rejected && state.unreplayable`） |
| `原请求已被协议确定性拒绝，原意图作废` | `FileTaskPanel.tsx:100`（`state.rejected && !state.unreplayable`） |
| `刷新也不会再变回` | `FileTaskPanel.tsx:100` |
| `确认本页新任务`（`role=group`） | `FileTaskPanel.tsx:269` |
| `确认取消原任务`（`role=group`） | `FileTaskPanel.tsx:209` |
| `保留本页任务` | `FileTaskPanel.tsx:291` |
| `放弃这次意图并新建` | `FileTaskPanel.tsx:288`（`state.rejected` 分支） |
| `准备新任务` | `FileTaskPanel.tsx:265` |
| `准备取消` | `FileTaskPanel.tsx:205` |
| `按原键重放` | `FileTaskPanel.tsx:172` |
| `该取消请求已被协议确定性拒绝并作废` | `file-task-session.ts:424`（notice 模板） |
| `尚未确认`（`file-job-status`） | `FileTaskPanel.tsx:118` |
| `.file-task-detail` | `FileTaskPanel.tsx:89` |
| `#file-task-model` | `FileTaskInput.tsx:39` |
| `#file-task-input` | `FileTaskInput.tsx:49` |
| `data-testid="file-job-status"` | `FileTaskPanel.tsx:118` |
| `文件转写`（heading） | `FileTaskInput.tsx:24` |
| `上传并转写` | `FileTaskInput.tsx:61` |
| journal 键 `speechnexus.workbench.asr-task.v1` | `FileTaskHost.tsx:17` `speechnexus.workbench.${kind}-task.v1`，`App.tsx:32` 默认 `kind="asr"` |

两条曾疑似「只存在于测试」的项，查证后均为真产品事实：

- journal 键在 `web/workbench/src/` 里 `grep` 不到字面量——它由
  `FileTaskHost.tsx:17` 模板拼出，`kind` 来自 `App.tsx` 的 `useState("asr")` 默认值，
  渲染路径为 `FileTaskInput`（`#file-task-*` 的 asr 侧）而非 `TtsTaskInput`（`tts-*`）。
  键与规格常量一致，断言非空洞。
- `#file-task-model` / `#file-task-input` 在 `FileTaskInput.tsx:39/49` 明确写死，不是
  测试猜的。

### A3 分类在产品代码里（成立）

`file-task-session.ts:96-148` `rejectionDisposition(status, code, operation)` 是唯一
分类点，按 (status, code) 精确配对查表，413 单列第三态：

```
134:  if (status === 413 && code === "job_request_too_large" && operation === "create")
135:    return "unreplayable";
120:  if (operation !== "cancel" && status === 409 && code === "execution_cleanup_pending")
121:    return "unknown-outcome";
```

取消路径的结算判据同样在产品代码：`settlesCancelAttempt` `:241-251`
（404 `job_not_found` / 409 `execution_cleanup_pending` / `idempotency_conflict`），
调用点 `:782`。

**规格从未直接设置 `state.rejected` / `state.unreplayable`**——它只选
`(status, code)` 这一对入参（`refusal(413, "job_request_too_large")` 等），
判定链完整跑在产品里。补充一条加固证据：错误信封并非任意 JSON，客户端用**生成契约
解析器**校验它——`speech-file-client.ts:37`
`throw new SpeechResponseError(response.status, parse("ErrorResponse", error))`。
信封形状不合契约会在页面侧抛解析错误，而不是被静默接受，所以 PR 正文
「由页面自己的生成契约解析器校验」这句是可核事实。

### A4 第 2 条「非首答」的造法（成立，且是全场最干净的一处）

产品判据链：

```
329:  #createEverIssued = false;
331:  #createAttemptIsFirst = false;
358:    this.#createEverIssued = this.#record !== null;      // 重建时从 journal 恢复
972:    const firstCreateAttempt = !this.#createEverIssued;  // 在读记录之前、发请求之前取快照
1008:   this.#persist({ pending: "create" });
1011:   this.#createAttemptIsFirst = firstCreateAttempt;
1012:   this.#createEverIssued = true;
685:    if (!this.#createAttemptIsFirst) { ... }              // 413 出口守卫
```

规格对它的造法：第 1 次 create 用 `route.abort("failed")`（响应丢失，页面走完
`#persist → #createAttemptIsFirst=first → #createEverIssued=true → create() 抛错`，
进入未知态），第 2 次点「按原键重放」才让 `firstCreateAttempt = !true = false`，
于是第二次的 413 落进 `:685` 的 `!this.#createAttemptIsFirst` 分支。

**这不是测试构造一个「非首答」状态，而是两次真实 `#submit()` 产生的历史。** 判据是
「是否已经发过 create 请求」而不是「是否有记录」，规格恰好用「响应丢失但请求已发出」
来区分这两种——这正是该守卫存在的理由，也是本卡最值得看的一段。

### B5 独立复现的反例（3/3，全部红在预期断言）

变异均在快照 `/private/tmp/sn743` 内临时应用，`pnpm --filter @speechnexus/workbench build`
后跑 `pnpm test:behavior`，跑完 `git checkout --` 复原（`git status` 已确认干净）。

| 变异 | hunk / 行数 | 结果 |
| --- | --- | --- |
| **M1** 「413 首答才有出路」守卫去掉：`file-task-session.ts:1011` `= firstCreateAttempt` → `= true` | 1 hunk / −1 +1 | **第 2 条红**，位置 `spec:193` `该意图此前已有提交尝试` 不可见；其余 3 条绿 |
| **M2** 413 处置改回未知：`file-task-session.ts:135` `"unreplayable"` → `"unknown-outcome"` | 1 hunk / −1 +1 | **第 1 条红**，位置 `spec:142` 第三态文案不可见；第 2 条亦红（`:193`，同因状态塌缩，预期内） |
| **M3** 去掉「放弃」二次确认：`FileTaskPanel.tsx:259-264` `准备新任务` onClick 直接 `onNew()` | 1 hunk / −2 +1 | **第 1 条红**，位置 `spec:152` `确认本页新任务` 组不可见；第 3 条亦红（同一两步确认） |
| **L** 网络锁去掉：`network-lock.mjs` `abort("blockedbyclient")` → `continue()` | 1 hunk / −1 +1 | 4/4 红在 `network-lock.mjs:43`，实测错误为 `net::ERR_CONNECTION_CLOSED` 而非 `ERR_BLOCKED_BY_CLIENT` |

M1 原文（红在预期断言，不是别处）：

```
Error: expect(locator).toBeVisible() failed
  Locator: locator('.file-task-detail').getByText('该意图此前已有提交尝试')
  > 193 |   await expect(panel(page).getByText("该意图此前已有提交尝试")).toBeVisible();
```

M2 第 1 条原文：

```
Error: expect(locator).toBeVisible() failed
  Locator: locator('.file-task-detail').getByText('原请求因请求体超过本部署的接收上限被拒绝')
  > 142 |   await expect(panel(page).getByText("原请求因请求体超过本部署的接收上限被拒绝")).toBeVisible();
```

M3 第 1 条原文：

```
Error: expect(locator).toBeVisible() failed
  Locator: getByRole('group', { name: '确认本页新任务' })
  > 152 |   await expect(confirmFinish).toBeVisible();
```

三条与作者 PR 正文「反例实测」一节所列的失败位置**逐字一致**。

### C7 三次连跑（不 flaky）

```
RUN 1  4 passed (2.2s)   exit=0
RUN 2  4 passed (2.2s)   exit=0
RUN 3  4 passed (2.2s)   exit=0
```

单测耗时区间 439–637ms（`retries: 0`，无重试掩盖），三条运行耗时几乎不变。

### C8 网络锁（判据非空洞）

`network-lock.mjs` 每次运行实测：内部 `fetch("https://blocked.invalid/...")` 必须被拒，
且 `requestfailed` 的 `errorText` 必须以 `net::ERR_BLOCKED_BY_CLIENT` 开头。作者称
「DNS/连接失败不会带这个前缀」。我把 `abort` 改成 `continue`（等价于不设锁）后，
4 条规格全部红在**同一个判据**上，实测错误为 `net::ERR_CONNECTION_CLOSED`：

```
Error: Network lock refused without Chromium's blocked-by-client error: net::ERR_CONNECTION_CLOSED
  at network-lock.mjs:43
```

这证明两件事：锁的安装与探针都是真的；探针能区分「被客户端拦」与「真实网络失败」，
不是只靠「fetch 抛了」这种空断言。

### C9 CI 接法（日志核到，路由未被放宽）

`.github/workflows/ci.yml` 在 `web` job 内、`Preserve accessibility evidence` 之后追加
两步（`Check file task state transitions in a real browser` → `pnpm test:behavior`，
`Preserve behavior evidence`）。该 job 在 `pnpm build` 与 a11y 之后，因此 preview 直接
复用已构建产物，无需重装/重建。

Run `43562135395` / job `132348014688` 的日志（我逐行核到）：

```
$ playwright test --config playwright.behavior.config.mjs
[WebServer] $ vite preview --host 127.0.0.1 --port 4173 --strictPort
[WebServer] $ vite preview --host 127.0.0.1 --port 4174 --strictPort
Running 4 tests using 2 workers
  ✓  2 ... 413 非首答：仍显示未知且仍不可放弃，按原键重放只复用原键 (1.1s)
  ✓  1 ... 413 首答：显示已作废而非结果未知，两步确认后输入解锁且幂等键换新 (1.3s)
  ✓  4 ... 未知结果（503 job_outcome_unknown）：显示未知、禁止新建、可按原键重放 (838ms)
  ✓  3 ... 取消被确定拒绝：可放弃，且刷新后仍是作废态而不复活成未知 (1.3s)
  4 passed (3.7s)
```

同一 job 的 a11y 步骤为 `52 passed (44.5s)`。**新 spec 真的执行了**，不是只加不改。

**路由是否被顺手放宽：没有。** `scripts/dev-env/ci-routing.py` 不在本 PR 的 9 个文件内。
`classify()` 的既有规则里，只有
`web/{shared,admin,workbench}/{src,public}` 且后缀属 web 集合才降级为 `web`，
其余一律 `full`。我直接加载该脚本按路径分类验证：

```
this PR paths                       -> full
tests/web only                      -> full
package.json only                   -> full
playwright.behavior.config.mjs only -> full
web/workbench/src/App.tsx (对照)    -> web
```

即：`tests/web/`、根配置本来就不在 `web` 降级集合里，`full` 是既有保守规则的必然结果，
与本次改动无关。

### D10 共享改造不削弱 a11y（抽取等价）

a11y 规格的真实改动只有「把内联路由改成调用共享 helper」：

```diff
-  await context.route("**/*", (route) => {
-    const url = new URL(route.request().url());
-    if (url.hostname === "127.0.0.1" && ["4173", "4174"].includes(url.port))
-      return route.continue();
-    return route.abort("blockedbyclient");
-  });
+  await lockToPreview(context);
```

对照 `network-lock.mjs`：

```js
const allowedPorts = new Set(["4173", "4174"]);
if (url.hostname === "127.0.0.1" && allowedPorts.has(url.port)) return route.continue();
return route.abort("blockedbyclient");
```

逐项等价：允许集合 `["4173","4174"]` ≡ `Set(["4173","4174"])`；hostname 判据相同；
abort 理由同为 `"blockedbyclient"`。**a11y 的锁既没放宽也没收紧**——它只多了一层
「每次运行实测」的探针，而那层探针只加在 behavior 规格里（a11y 规格没有调用
`expectNetworkLocked`），因此 a11y 行为不变。

`playwright.a11y.config.mjs` 的 `webServer` 改动是纯抽取，两个条目的
`command` / `url` / `reuseExistingServer: false` 与抽取前逐字相同。

### D11 a11y 实跑

```
$ pnpm test:a11y
  52 passed (18.4s)
```

与 CI 日志的 `52 passed (44.5s)`、作者陈述的 52 一致。

### E12 范围与依赖

- 越界：无。`contracts/`、`backend/`、`scripts/dev-env/` 均 0 文件。
- `uv.lock` / `pnpm-lock.yaml`：均不在本 PR 文件列表内，未被触碰。
- 新增依赖：无。`package.json` 只加 `test:behavior` 脚本并把它加进 `lint` 的 eslint
  目标；`dependencies` / `devDependencies` 无任何增删。
- **一处记录（非缺陷）**：本清单列举的允许范围是 `tests/web/`、
  `playwright.*.config.mjs`、`.github/workflows/ci.yml`，而实际还改了
  `package.json` 与 `eslint.config.js`。两者都是新增 config 的**必要配套**，且方向是
  收紧而非放宽——`eslint.config.js` 把忽略项从 `playwright.a11y.config.mjs` 泛化为
  `playwright.*.config.mjs`，新 config 因此也进入 lint 覆盖。不构成范围问题，仅记录。

### E13 fixtures 来源（既有 + 被钉住，不是手写漂移副本）

`tests/fixtures/contracts/file-workflow.json` **不是本 PR 新增**：它自
`0b8e85a5`（`feat: 公开 Job 取消的持久受理与停止确认 (#424)`）起就在主线上，本 PR
只读不写。共 152 个 examples，规格用到的 `queued-job` / `cancel-job` / `ready-audio`
三个 id 均存在。

它也不是无人管的「手写契约副本」：`tests/contract/test_generated_bindings.py` 对**每一个**
example 用生成的 Python 校验器 + 源 schema（`contracts/speech-openapi.yaml`，经
`contracts/bindings.json` 解析引用）双向校验，并断言 `is_valid(value) is example["valid"]`。
schema 漂移会让该测试红，因此规格取用的 payload 天然受契约门禁约束。

## 必须修清单

**无。**

## 建议（不阻塞，可留待后续卡）

1. **给 journal 断言加正向对照。** 第 1 条与第 3 条都断言退出后
   `sessionStorage.getItem(journalKey) === null`，但**没有先断言放弃前它非空**。键值
   本身是正确的（`FileTaskHost.tsx:17` × `App.tsx:32` 默认 `asr`，我已核实），所以
   当前断言有效；但一旦将来键名或 kind 变化，这条断言会静默退化为恒真。建议在点击
   「准备新任务」前加一行 `expect(await journal(page)).not.toBeNull()`。
2. **把两条 Playwright config 的端口互斥写进文档。** 两个 config 都对
   `4173/4174` 用 `--strictPort` 且 `reuseExistingServer: false`，因此 `test:a11y` 与
   `test:behavior` **不能并发**。CI 在同一 job 内顺序执行，无问题；我自己在本地并发
   跑时确实撞到 `http://127.0.0.1:4173 is already used`。建议在
   `playwright.behavior.config.mjs` 顶部注释或 Web 验证指南里点明，避免后续把两者塞进
   matrix 或并行 job 时踩同样的坑。

## 新绿

本卡带来的唯一新增门禁就是行为规格本身，且已进既有 `web` job：

- 4 条真实浏览器规格（`pnpm test:behavior`），本地 3 次连跑全绿、CI 实跑绿；
- 网络锁探针从「装了就算」升级为「每次运行实测 `net::ERR_BLOCKED_BY_CLIENT`」；
- 它关闭的正是连续三片 Web 交付反复出现的「未在真实浏览器手点」未验证项
  （#724 取消终态、#731 413 第三态、#729 413 出口）。

既有门禁无一处被替代或放宽：a11y 仍 52 条全绿且锁等价，`pnpm test` 全绿，
路由分类规则未改。

## 只能依赖作者陈述的点 / 未验证项

我未亲自核到、只能依赖作者陈述或外部事实的有：

1. **真实网关确实会发出 `413 job_request_too_large`。** 规格按契约建模该应答，
   信封形状由生成解析器校验，但「真实部署在什么条件下回 413、回的是这一对
   (status, code)」未被任何一次实跑证明。作者在 PR 正文「未验证项」里已自认同一点。
2. **真实后端 + 真实语音服务联调。** 规格证明的是「浏览器里面板状态机对契约应答序列的
   反应」，不是端到端链路。作者同样已自认。
3. **上游 run/job 的归属关系。** CI 证据取自作者给出的 run `43562135395` /
   job `132348014688`；我用 `gh` 核到该 job 属于 PR #741 的 head，且日志含 4 passed，
   但「这次 run 是否对应被审的这个 head」我按 PR 关联判断，未逐字节比对 commit SHA。

## 过程记录（时间线）

1. `git fetch` → 在 `/private/tmp/sn744` 从 `origin/main`（`6359ff28`）建
   `review/744-behavior-specs`；确认基线干净、`git diff origin/main...HEAD --stat` 为空。
2. `git cat-file -t c5fa60f3` 确认 head 可达；`git show c5fa60f3 --stat` 取 9 文件清单
   （A1），注意到 `eslint.config.js` / `package.json` 两处超出本清单枚举范围的配套改动。
3. 读 `file-task.behavior.spec.mjs` 全文（266 行）；对 16 条文案断言逐个
   `grep -rn web/workbench/src/`（A2 首轮）。
4. 追 `#file-task-model` / `#file-task-input` 与 journal 键：初查 grep 不到
   journal 字面量，顺 `FileTaskHost.tsx:17` 模板 + `App.tsx:32` 默认值确认键为
   `asr-task.v1`，排除「断言恒真」误判（A2 收口）。
5. 读 `file-task-session.ts:40-190`（分类表 + `rejectionDisposition`）与
   `:236-258`（`settlesCancelAttempt`）、`:660-720`（413 守卫分支）、`:955-1020`
   （`#createAttemptIsFirst` 快照与赋值）→ A3/A4 成立。
6. 读新增/改动基础设施：`playwright.behavior.config.mjs`、`preview-servers.mjs`、
   `network-lock.mjs`、a11y config 与 spec 的 diff、`ci.yml` diff、
   `package.json` / `eslint.config.js` diff（D10/E12）。
7. 定位路由脚本在 `scripts/dev-env/ci-routing.py`（非 `scripts/ci-routing.py`），
   确认未被本 PR 修改；直接 `import` 其 `classify()` 对实际路径与单路径各跑一遍
   （C9）。
8. 环境：系统 node `v26.12.1` + pnpm `12.14.0`；用快照 `/private/tmp/sn743`
   （已含 `node_modules` 与 `dist`）作为实跑基线。
9. 基线 `pnpm test:behavior` → 4 passed；后台起 `pnpm test:a11y` → 52 passed（D11）。
10. 变异 M1（`#createAttemptIsFirst = true`）→ 重建 workbench（约 4s）→ 第 2 条红在
    `:193`；`git checkout --` 复原。
11. 变异 M2（`"unreplayable"` → `"unknown-outcome"`）→ 第 1 条红在 `:142`（第 2 条亦红）；
    复原。
12. 变异 M3（`准备新任务` 直连 `onNew()`）→ 第 1 条红在 `:152`（第 3 条亦红）；复原，
    重建 `dist` 回原始源。
13. 三次连跑行为规格（C7）；随后变异网络锁 `abort→continue`，4/4 红在 `:43` 的
    `ERR_BLOCKED_BY_CLIENT` 判据（C8），立即复原。
14. 并行跑 `pnpm test`（contracts 2241 passed）、`pnpm typecheck`（全部 Done）、
    `pnpm lint`（ESLint passed）、`pnpm format:check`（All matched files use Prettier
    code style）→ 全绿；单独复核 workbench `873 passed / 62 files`，与作者陈述一致。
15. `uv run --no-project ruff format --check .` → `1843 files already formatted`；
    `uv run --no-project ruff check .` → `All checks passed!`。
16. 写本报告，再跑一次 `ruff format --check .` 与格式门禁确认报告本身不破坏
    `Docs: markdown code block formatting`（该 job 的判据是
    `uvx --from ruff==0.17.2 ruff format --check .`）。
