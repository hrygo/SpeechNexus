---
title: PR #781 非作者审查：崩溃窗口 alert 的动作一致性（#780）
status: active
---

# PR #781（`fix/780-alert-actions`）非作者审查记录

被审对象：PR #781《fix: 崩溃窗口的 alert 只承诺该状态真正可用的动作 (#780)》，head
`ceff9ed04f6fa6fc6a57becd3f140010d157fc30`（单 commit），base `7cd0d8c6`（`origin/main`），
相对 `origin/main` `+287 / −22`，**5 文件**：`web/workbench/src/file-workflow/`
（`file-task-session.ts` `+128/−22`、`FileTaskPanel.tsx` `+16/−2`、
`FileTaskHost.test.tsx` `+58`、`file-task-persistence.test.ts` `+100/−1`）与
`tests/web/file-task.behavior.spec.mjs`（`+7/−2`）。关联 #780、#782，两个 issue 均 OPEN，
本 PR 未声明关闭。

审查方式：独立 worktree `/private/tmp/sn783`，**基于 `origin/main`（`7cd0d8c6`）拉分支**
`review/783-alert-actions`；被审内容一律用 `git show ceff9ed0:<path>` 读出。被审代码与全部
变异跑在第二个 detached 副本 `/private/tmp/sn783-pr`（`git worktree add … ceff9ed0 --detach`），
另有 `/private/tmp/sn783-base`（`e1f26c29`）只用于核基线例数；**未在被审 head 上开分支、
未 push 任何变异**。产品代码/测试零改动，临时探针文件跑完即删（两处副本 `git status` 干净）。
环境：系统 node `v26.12.1`、pnpm `12.14.0`、playwright chromium（已装）。

## 结论

**2 项必须修，七条判定成立。作者的 C5 两步自证与 D7 三条反例我逐条独立复现，红点数量与
位置与其描述完全一致；机制（唯一判据 + 由动作集合生成文案）方向正确，alert 这一路确实
同源了。但「同一含义在别处又写了一遍」没有清零：面板两行、`restore()` 闸门与 `#run` 的
十余条 notice 仍是独立判断，其中面板「原执行」行已经与 alert 漂移，在 2/18 个恢复格里
同屏矛盾（M1）；崩溃窗口的新文案还把一个不成立的否定写进了产品——「没有可核对的原素材」
与同屏「素材」行、与可读的 `GET /v1/nexus/assets/{id}` 相反（M2）。**

### 本卡两个核心问题的答案

**「判据是否真的唯一」：对 alert 这一路成立，对整页不成立。**
`readTargetOf`（`file-task-session.ts:288`）确实是 `refresh()` 分派（`:1212-1213`）、
恢复 notice（`:373-377`）、alert 与按钮（`FileTaskPanel.tsx:99` / `:172`）三者的唯一来源，
grep 全目录确认这三个消费者没有第二套条件。但同一含义的独立复写还剩四处：

1. 面板「原执行」行（`FileTaskPanel.tsx:147-148`）自己拼 `state.operation?.id ??
   state.recovery?.error.nexus?.operation_id ?? "无"`，缺 `record.operationId`；
2. 面板「素材」行（`:115-116`）用 `state.asset?.id ?? state.assetRecoveryId ?? savedAssetId`，
   来源集合与 `#readableIdentities()`（`:547-553`）不同（多 `record.assetId`，少 `upload.assetId`）；
3. `restore()` 的闸门 `if (this.#record || this.#upload)`（`:636`）是「点了会不会发请求」的
   另一条谓词，不由 `readTarget` 派生；
4. `#run` 里十余条手写 notice（`:714`、`:737`、`:919` 等，作者已认领 #782）。

因此「本卡只是把漂移点挪了个位置」**部分成立**：alert 与按钮之间的漂移关掉了，但漂移点
在面板「原执行」行上立刻可观测（见 M1），而且崩溃窗口里状态行仍能写出与 alert 相反的
句子（A1 末段实测复现 #782）。

**「『实际路由 == 文案动作』是否真的独立」：断言真实存在、路由级、有效，但它不是判据的
独立真值来源。**
`file-task-persistence.test.ts` 的第二个 `it.each(restoreMatrix)` 在 18 格上真跑
`restore()`、记录 `fetch` 的 pathname，再与 `namedReads(alert)` 比对——这条断言确实以
**可观测请求**为准，不是又抄一遍文案：我把 operation 分支改成读 `/jobs/{op}`（文案完全
不动）后，9 个 `operationId` 非空的路由行全部转红，而「文案 ⇔ `readTarget`」那组仍然全绿，
证明它抓的是分派漂移。但它不独立于 `readTargetOf` 本身：把判据优先级改成 `job` 先于
`operation` 后，文案与路由**同时**跟错，18 格路由断言全绿，只有
`expect(state.readTarget).toBe(expectedRead(row))`（从矩阵行事实独立推导的 oracle）抓住
（6 行红 + 2 条既有用例）。所以「判据只是同源自洽」不成立，但 PR 正文的「真正独立于同源
设计」应降级为「独立于文案字面量与 fixture 期望列」——判据本身的独立性由 `expectedRead`
提供，两层合起来才成立。

九条判定一览：

| # | 条目 | 判定 | 关键证据 |
| --- | --- | --- | --- |
| A1 | `readTargetOf` 是唯一判据、无第二套判断 | **部分成立** | 三消费者同源；面板 2 行 / `restore` 闸门 / `#run` notice 四处残留，M1 同屏矛盾 |
| A2 | `refresh()` 三分支行为不变、不新增请求 | 成立 | 8 个恢复态新旧实现差分：路由序列与 notice 全等；空串 id 边界经解析器/日志校验不可达 |
| B3 | `readTarget === null` 时禁用按钮 | 成立 | 旧按钮可点但零请求（实测 old 0 请求）；未改 `refresh()` 判定（差分） |
| B4 | alert 各分支点名动作都可执行 | **成立（文案事实另有 M2）** | 8 组合（4 句）全部只点名该快照启用的按钮；null 分支的事实断言错误（M2）；asset 分支零断言（S3） |
| C5 | 等式断言不靠逐字断言顺带覆盖 | 成立 | 复现：按钮错 1 红；再删两条按钮等式断言 → 29/29 绿 |
| C6 | 「实际路由 == 文案动作」的断言存在且独立 | **存在且路由级；独立性来自 `expectedRead`** | M5 分派漂移 9 行红；M6 判据换序 18 格路由全绿、oracle 抓 6 行 |
| D7 | 三条反例红点 | 成立 | 5 红 / 1 红 / 14+2 红，与作者数字一致 |
| E8 | 范围、lock、依赖 | 成立 | 5 文件全在允许范围；`--frozen-lockfile` 两处通过；无新依赖 |
| E9 | 例数、CI 路由与实跑 | 成立 | 964 / 923（e1f26c29）/ 974（CI 日志）/ 9 / 52；路由 full，六片 + Integration 0–5 全绿 |

## A1：唯一性排查（第二套判断在哪里）

判定点清单（`web/workbench/src/file-workflow/`，非测试代码）：

| 同一个含义 | 判定点 | 取自 | 同源？ |
| --- | --- | --- | --- |
| 这页能读什么（分派） | `refresh()` `:1212-1213` | `readTargetOf(#readableIdentities(view))` | ✅ |
| 恢复态状态行文案 | `resumedNoticeFor` `:373-377` | `readTargetOf`（但手工组装 identities，`assetId` 硬编码 `null`） | ⚠ 同函数、不同输入 |
| alert 与读取按钮 | `FileTaskPanel.tsx:99` / `:172` | `state.readTarget` | ✅ |
| 「原执行」显示 | `FileTaskPanel.tsx:147-148` | `operation?.id ?? recovery?.operation_id ?? "无"` | ❌ 缺 `record.operationId` |
| 「素材」显示 | `FileTaskPanel.tsx:115-116` | `asset?.id ?? assetRecoveryId ?? savedAssetId` | ❌ 来源集合不同 |
| 点击「读取原状态」是否发请求 | `restore()` `:636` | `#record || #upload` | ❌ 另一条谓词 |
| 状态行动作承诺 | `#run` `:714` / `:737` / `:919` 等十余条 | 手写字符串 | ❌（#782） |

grep 证据（只保留产品代码命中，省略测试与 `speech-file-client` 的端点定义）：

```
$ grep -rn "读取原\|核对原" --include=*.ts --include=*.tsx web/workbench/src/file-workflow/ | grep -v test
FileTaskPanel.tsx:105   读取原任务核对后，可用「准备新任务」明确结束；…
file-task-session.ts:309 "读取原执行核对"            ← unconfirmedAlertFor 生成
file-task-session.ts:311 "读取原任务核对"
file-task-session.ts:313 "核对原素材"
file-task-session.ts:379 "…请读取原执行核对，不要新建任务。"   ← resumedNoticeFor
file-task-session.ts:381 "…请读取原任务核对，或按原输入与原键重放。"
file-task-session.ts:516 "…请读取原任务核对，再显式收尾。"       ← 构造器手写 notice
file-task-session.ts:521 "已找到原任务身份；请读取原任务状态。"
file-task-session.ts:714 "原执行已有恢复身份。请重新读取原执行状态；…"
file-task-session.ts:737 "操作结果未知。请读取原任务，或按原幂等键重放；…"
file-task-session.ts:775/776/823/834/919  …请读取原执行/原任务…
```

`readTargetOf` 自身的引用点只有 `:373`、`:556`、`:1213` 三处，与上述三个消费者一一对应；
没有第四处调用。`state.readTarget` 的消费者只有 `FileTaskPanel.tsx:99`、`:172` 与测试。

### 残留 1（M1）：面板「原执行」行

恢复态 `record.operationId` 有值时，`#readableIdentities()` 会读 `record.operationId`，
`readTarget = "operation"`、alert 为「请读取原执行核对」、按钮可用且点击真的会
`GET /v1/nexus/operations/{id}`；但同一面板的「原执行」行只认 `state.operation` 与
`state.recovery`，两者在恢复态都是空，于是显示「无」。实测（`FileTaskHost` + 恢复记录
`pending=create, operationId=op-demo`，jsdom 渲染）：

```
PROBE op alert: 请求结果尚未确认。请读取原执行核对；不要新建任务。
PROBE op dd rows: ["asset-demo","尚未确认","尚未确认","尚未确认","尚无证据","无","尚未读取","idle","无"]
```

18 格里能同时渲染 alert 与这一行的是 `settled === undefined` 且 `operationId !== null` 的
两格（`pending ∈ {create, cancel}`），即 **2/18** 格同屏矛盾：文案让用户去读「原执行」，
下一行却写「原执行：无」。这是本卡立论「同一含义不能被写第二遍」的直接反例，且是新增
alert 文案把它变得可见的。

### 残留 2（M2）：面板「素材」行

崩溃窗口（`pending=create`、无 `jobId`/`operationId`/settled）里 alert 现在写
「没有可核对的原素材、原任务或原执行身份」，但同一面板「素材」行渲染的是
`savedAssetId`（`FileTaskHost.tsx` 传入 `record.assetId`），即持久化下来的原素材标识；
`SpeechFileClient.getAsset` 能直接读到它。实测：

```
PROBE crash alert: 请求结果尚未确认。没有可核对的原素材、原任务或原执行身份；重放只使用原输入和原键，不会新建任务。
PROBE crash dd rows: ["asset-demo","尚未确认","尚未确认","尚未确认","尚无证据","无","尚未读取","idle","无"]
PROBE crash snapshot: {"readTarget":null,"canReplay":true,"unknown":true}
PROBE crash restore() calls: []
PROBE direct getAsset(record.assetId) result id: asset-demo
```

即：`refresh()` 三支不命中是**实现**事实（零请求，实测），不是**能力**事实——原素材标识
在记录里、在面板上、在 API 上都是可读的。alert 把「本页没有实现这条读」写成了「没有可核对
的原素材」，换掉了一个假承诺，又引入了一个假否定（详见 M2）。

### 残留 3（S1）：`restore()` 闸门

`restore()` 只在 `#record || #upload` 时调用 `refresh()`。而 `readTarget` 还能由
`view.recovery`、`view.job`、`#assetHintId` 单独构造。生产组合根 `FileTaskHost.tsx:35`
恒有 journal，所以不可达；但 `dev/FileTaskDemo.tsx:47`、`dev/TtsTaskDemo.tsx:22` 构造
`FileTaskSession` 时不传 journal：此时上传失败带 `assetId` 会走
`#rememberAssetHint`（`:988-1000`，`#prepareUpload` 因无 journal 早退，`#upload` 仍为
`null`），得到 `readTarget = "asset"`、按钮可用，而点击 `restore()` 空转——正是本卡要
消灭的「可点但没反应」，只是方向相反且只在 demo 路径。

### 残留 4（S0，作者已认领 #782）：状态行

在崩溃窗口本身复现了 #782 的同屏矛盾：恢复崩溃窗口后点「按原键重放」，请求以非 HTTP
错误失败（如网络中断），走 `:727-738` 分支，状态行写「操作结果未知。请读取原任务…」，
而 alert 同时写「没有可核对的原素材、原任务或原执行身份」：

```
PROBE782 before: {"readTarget":null,"canReplay":true,"unknown":true}
PROBE782 after readTarget= null canReplay= true unknown= true
PROBE782 status: 操作结果未知。请读取原任务，或按原幂等键重放；不要创建替代任务。
PROBE782 alert : 请求结果尚未确认。没有可核对的原素材、原任务或原执行身份；重放只使用原输入和原键，不会新建任务。
```

作者在 PR 正文自报「#run 里十余条手写 notice 仍会要求做不到的读取（已开 #782）」，这条
自报**准确**，且问题就发生在本卡的目标状态里。所以「唯一判据」目前只覆盖 alert/按钮/
恢复 notice 一路，不等于整页自洽；PR 标题（「alert 只承诺…」）成立，PR 正文的「唯一判据」
若被读成整页则过宽。

## A2：`refresh()` 三分支等价与请求数

逐分支静态核对（旧 = `e1f26c29` 版，新 = PR head）：

| 旧条件 | 新条件 | 取 id | 等价性 |
| --- | --- | --- | --- |
| `operationId = view.recovery?.… ?? record?.operationId; if (operationId)` | `if (target === "operation")`，target 来自 `readTargetOf` 同一表达式 | 同一表达式 | 等价（真值性一致） |
| `else if (view.job \|\| record?.jobId)` | `if (target === "job")`，条件实际是 `Boolean(view.job?.id ?? record?.jobId)` | `view.job?.id ?? record?.jobId` | 等价，唯一理论差异见下 |
| `else if (view.asset \|\| upload?.assetId \|\| assetHintId)` | `if (target === "asset")`，条件实际是 `Boolean(view.asset?.id ?? upload?.assetId ?? hint)` | `view.asset?.id ?? upload?.assetId ?? #assetHintId` | 等价，唯一理论差异见下 |
| 三者皆不命中 → 什么也不做 | `target === null` → 什么也不做 | — | 等价（实测崩溃窗口 0 请求） |

理论差异只有一处：旧代码对 `view.job` / `view.asset` 判断**对象真值**，新代码判断**解引用后
的 id 真值**。若 `view.job.id === ""` 或 `view.asset.id === ""` 且另有回退 id，旧代码会发
`GET /…/`（空 id），新代码落到下一支或 `null`。该形状不可达：`Job.id` / `Asset.id` 走契约
`Identifier`（`contracts/shared/identifiers.schema.json`，`pattern`
`^[a-z0-9][a-z0-9._:-]{1,126}[a-z0-9]$`、`minLength: 3`），`view.job` / `view.asset` 只由
解析后的响应赋值；journal 侧 `task-journal.ts:195-199` 也拒绝空串 `jobId`/`operationId`。

真实流程差分（同一 record、同一 fake transport，新旧实现各建一个 session，跑同样的
`restore()`，比较 GET 序列与最终 notice）：

| 恢复态 | 旧实现路由 | 新实现路由 | notice |
| --- | --- | --- | --- |
| 崩溃窗口（create，无 id） | `[]` | `[]` | 相同 |
| create + `operationId` | `["GET /v1/nexus/operations/op-demo"]` | 同 | 相同 |
| cancel + `jobId` | `["GET /v1/nexus/jobs/job-demo"]` | 同 | 相同 |
| cancel + `jobId` + `operationId` | `["GET /v1/nexus/operations/op-demo"]` | 同 | 相同 |
| `pending: null` + `jobId` | `["GET /v1/nexus/jobs/job-demo"]` | 同 | 相同 |
| `pending: null`，无 id | `[]` | `[]` | 相同 |
| upload 记录 + `assetHintId` | `["GET /v1/nexus/assets/asset-original"]` | 同 | 相同 |
| upload 记录无 `assetHintId` | `[]` | `[]` | 相同 |

第二次 `refresh()`（此时已有 `view.asset`/`view.assetHintId` 两条 asset 子路径）新旧同样各
发一次 `GET /v1/nexus/assets/asset-original`。结论：**行为不变、请求数不变**，包括 brief
特别点名的 `asset` 支与「三者皆无」支；PR 的「条件与顺序逐字保持原样、不新增任何请求」
成立。全量 964 绿（含 `asset-reconcile-session.test.ts` 对读路由的逐条断言）与差分结论一致。

## B3：`readTarget === null` 时禁用按钮是不是正确的产品决定

- **正确，且与 #780 的缺陷同源。** 旧行为是按钮可点、点击后 `restore() → refresh()` 零请求、
  notice 不变（差分实测旧实现 0 请求），即「承诺了一个不存在的读」。禁用把不可用显式化，
  比空转好：空转会让用户以为操作失败或不生效，禁用至少是诚实的。
- **没有偷偷改 `refresh()` 的判定。** 差分里崩溃窗口新旧都是 0 请求；`refresh()` 的分派完全
  由 `readTargetOf` 决定，禁用只是同一值的第三个消费者。964 全绿也说明没有任何既有测试
  依赖旧的「可点」状态。
- **三态比较**：隐藏按钮更差（用户会认为功能消失，且本页仍有「按原键重放」可用，隐藏读取
  按钮会暗示整个恢复路径不可用）；保留可点更差（就是被修的病）。禁用 + 解释是正确选择。
  唯一可改进处：`<button disabled>` 不可聚焦，读屏用户拿不到该按钮的存在理由，现在只能
  靠同屏 alert 解释（恰好就在旁边，可接受）。可加 `title` / `aria-describedby` 指向 alert，
  见 S5。

## B4：alert 各分支点名的动作是否真的可执行

`unconfirmedAlertFor`（`:306-320`）的输出只依赖 `(read, replay)` 二元组，共 **8 组合 →
4 句文案**（不是九个分支；brief 的「九」我按实际代码计为 8）。逐组合判定：

| `read` | `replay` | 文案要点 | 可达性（静态） | 动作可用？ |
| --- | --- | --- | --- | --- |
| `operation` | `false` | 读取原执行核对；不要新建任务 | 恢复记录 `operationId` 非空（矩阵 9 格中 `unknown` 的 2 格 + 已作废格） | ✅ 读取按钮可用，重放禁用 |
| `operation` | `true` | 读取原执行核对；若重放… | `refusalProvesNothing`（`:913-916` 置 `canReplay: true` 且 `retainRecovery` 保留 op id）或非 HTTP 失败（`:731-733` 不动 recovery） | ✅ 两按钮都可用；**无测试覆盖（S4）** |
| `job` | `true` | 读取原任务核对；若重放… | 恢复 `pending: cancel` + `jobId`；In-session 413 非首答（behavior 规格第 8 条正是此态） | ✅ 两按钮都可用 |
| `job` | `false` | 读取原任务核对；不要新建任务 | 恢复 `pending: null` + `jobId`（`unknown` 为假时不渲染，仅测试直接调用） | ✅ |
| `asset` | `false` | 核对原素材；不要新建任务 | 上传失败带 `assetId`（`:716-745` 强制 `canReplay: false`；`asset-reconcile-session.test.ts` 覆盖此态） | ✅ 读取可用 |
| `asset` | `true` | 核对原素材；若重放… | 非 HTTP 失败（`:727-738`）时 `view.asset` 仍在、`canReplay: true` | ✅ 两按钮都可用；**无测试覆盖（S4）** |
| `null` | `true` | 没有可核对…；重放只使用原输入和原键 | 崩溃窗口（矩阵 6 格 + Host 首例） | ✅ 重放可用；**但事实断言错（M2）** |
| `null` | `false` | 当前页没有可核对…；请保留原请求 | 恢复的 upload-only 记录无 `assetHintId`（构造器 `:469-479`，`unknown=true, canReplay=false`） | ✅ 两按钮都禁用，文案不承诺动作 |

结论：**每句文案点名的动作在该状态下都真的可用**（`read` ⇔ `readTarget` ⇔ 读取按钮；
`replay` ⇔ `canReplay` ⇔ 重放按钮），唯一例外是 `busy` 瞬态——期间两个按钮都禁用而 alert
不变，这是瞬态而不是状态能力，可接受但注释应点明（S5）。需要注意两点：(a) `null` 分支的
「没有可核对的原素材」是事实错误（M2，不是可执行性问题）；(b) `asset` 分支的文案在
测试里**零断言**——18 格矩阵结构上产不出 `asset`（恢复记录不暴露 asset handle），Host 的
4 行也没有，所以「核对原素材」这句从未被任何断言钉住（S3）。

## C5：两步自证（独立复现）

第一步：只让按钮错（`FileTaskPanel.tsx:172` 追加 `|| (state.canReplay && state.readTarget === "job")`，
alert 文案不动）：

```
× reloaded pending cancel alerts exactly the actions it enables
AssertionError: expected true to be false // Object.is equality
Tests  1 failed | 28 passed (29)
```

第二步：保留同一变异，删掉 `FileTaskHost.test.tsx` 里那两条按钮等式断言
（`expect(named.length > 0).toBe(!refresh.hasAttribute("disabled"))`、
`expect(copy.includes("重放")).toBe(!replayButton.hasAttribute("disabled"))`）：

```
Tests  29 passed (29)
```

判定：**作者的诚实判定成立**——该等式只由那两条按钮断言覆盖，不是被逐字断言顺带覆盖。
补充：这两条等式只在「非 busy 的稳定态」成立（busy 期间按钮禁用、alert 不变），是稳定态
等式而非恒等式，测试名/注释宜点明（S6）。

## C6：「实际路由 == 文案动作」的断言是否真的存在、真的独立

存在性：`file-task-persistence.test.ts` 新增的第二个 `it.each(restoreMatrix)`
（「… reads only what the alert names」）用 `restoreWithRequests()` 建一个记录真实
`fetch` pathname 的 client，`await session.restore()` 后断言
`namedReads(alert) == 去重后的实际路由`，再断言 `before.readTarget` 与首条路由一致。
18 格逐格执行，是真实请求而不是 mock 期望。

三个变异（都在 detached 副本，跑完复原）：

| 变异 | 目的 | 结果 |
| --- | --- | --- |
| M5：`refresh()` 的 operation 分支改读 `/v1/nexus/jobs/{op}`，文案不动 | 只漂分派，不漂文案 | `11 failed \| 58 passed (69)`：9 个 `operationId` 非空的路由行全红；「文案 ⇔ `readTarget`」组全绿 |
| M6：`readTargetOf` 优先级改成 `job` 先于 `operation` | 只漂判据，文案与分派一起漂 | `8 failed \| 61 passed (69)`：**18 格路由断言全绿**，红点来自 `expectedRead` oracle 6 行 + 2 条既有用例 |
| D7-1：alert 改回共享句（见 D7） | 只漂文案 | Host `5 failed \| 24 passed (29)` |

判定：**断言真实存在，且是路由级的**——M5 证明它抓得住「文案对、分派错」这一类；它也独立
于文案字面量与 fixture 的期望列。但**「真正独立于同源设计」是过度声明**：M6 证明它对判据
本身没有独立真值，文案与路由两边都从 `readTargetOf` 派生；判据的独立性由
`expectedRead(row)`（从矩阵行事实推导）提供。两层合并后，判据漂移仍会被测到，所以
「整个判据只是同源自洽」的担心不成立，但 PR 正文那句话应改写为「独立于文案与 fixture 列」。

## D7：三条反例（独立复现）

| # | 变异 | 作者预期 | 实测 |
| --- | --- | --- | --- |
| 1 | alert 改回无条件共享文案 | Host 5 红 | `Tests 5 failed \| 24 passed (29)`：崩溃窗口逐字用例 + 4 条 `(read, replay)` 行 |
| 2 | 只让按钮错（`canReplay && readTarget === "job"` 时也禁用） | 1 红 | `Tests 1 failed \| 28 passed (29)`，`expected true to be false` |
| 3 | 让无重放（`canReplay === false`）态也提「重放」 | 矩阵 14 红 + Host 2 行 | 矩阵 `14 failed \| 55 passed (69)`；Host `2 failed \| 27 passed (29)`（两条 `operationId` 非空的行） |

三条数字与作者描述**完全一致**，红点位置正确。

## E8：范围与依赖

```
$ git diff --name-only origin/main...ceff9ed0
tests/web/file-task.behavior.spec.mjs
web/workbench/src/file-workflow/FileTaskHost.test.tsx
web/workbench/src/file-workflow/FileTaskPanel.tsx
web/workbench/src/file-workflow/file-task-persistence.test.ts
web/workbench/src/file-workflow/file-task-session.ts
```

全部落在「`web/workbench/src/file-workflow/` + 其单测 + `tests/web/`」范围内；无
`packages/`、`contracts/`、`backend/`、`migrations/`、`.github/`、`docs/`；无
`package.json`、无 `pnpm-lock.yaml`、无 `uv.lock`（两处副本 `pnpm install --frozen-lockfile`
均通过，等价于 lockfile 与 workspace 声明未变）；无新依赖。

## E9：例数、CI 路由与实跑

```
PR head ceff9ed0       : pnpm test → Test Files 63 passed (63) / Tests 964 passed (964)
PR 分支点 e1f26c29     : pnpm test → 923 passed (923)        ← PR 称的基线，实测一致
origin/main 7cd0d8c6   : pnpm test → 933 passed (933)        ← #770 在主干 +10
pnpm build && pnpm test:behavior → 9 passed (3.8s)           ← 顺序执行，未与 a11y 并发
pnpm test:a11y                   → 52 passed (19.0s)
pnpm typecheck                   → 4 个 workspace 全部 Done，exit 0
CI web job 日志                  → web/workbench test: Tests 974 passed (974)
```

差值归因：974 − 964 = 10 = 933 − 923，即 `7cd0d8c6`（#770）在主干
`web/workbench/src/books/RenderUnitPanel.test.tsx` 新增的用例；PR 的 +41 分解
（18 + 18 + 1 + 4）逐项对得上：`restoreMatrix` 实测 18 行、`it.each(restoreMatrix)` 3 处
（1 条既有投影 + 2 条新增）、Host 新增 1 条崩溃窗口 + 4 行 `it.each`。

CI 路由：本地重算 `classify(PR 的 5 个路径) = "full"`；去掉 `tests/web/` 后 `= "web"`；
`tests/web/` 单独 `= "full"` —— 与 PR 的「`tests/web/` 不在 web 白名单」一致。CI 实跑日志：

```
{"mode": "full", "reason": "Shared, unknown, executable or empty changes require every verification lane."}
```

`gh pr checks 781`：20 项，**18 pass / 2 skipping**；2 个 skipping 是路由语义
（`Docs: markdown code block formatting` 仅 docs 路由、`Node Controller image evidence /
Build and probe` 为矩阵占位）。`Gate: required checks for this change` 日志
`Verified scope=full`；六分片 `Integration 0–5` 全部 pass（Integration 4 `8m58s`、
Integration 5 `7m51s`，有时长即实跑），`Gate: six-shard integration evidence and merged
coverage` pass。

## 必须修清单

### M1（必须修）：面板「原执行」行是第三套读取身份判据，已与 alert 漂移

- **位置**：`web/workbench/src/file-workflow/FileTaskPanel.tsx:147-148`
  （`state.operation?.id ?? state.recovery?.error.nexus?.operation_id ?? "无"`）。
- **事实**：恢复态 `record.operationId` 非空时，`#readableIdentities()`
  （`file-task-session.ts:549`）会读它、`readTarget = "operation"`、alert 写「请读取原执行
  核对」、按钮可用且点击真的发 `GET /v1/nexus/operations/{id}`；同一面板的「原执行」行却
  显示「无」。18 格中 `settled === undefined` 且 `operationId !== null` 的 **2 格**
  （create / cancel）同时渲染 alert 与该行，同屏矛盾（实测输出见 A1）。
- **为什么算本卡范围**：本卡的验收标准就是「同一含义只能有一个判据」。这一行是同一含义的
  第二份实现，且新文案把它从「文案 vs 面板」升级为「文案指向的面板行自我否认」；不修则本卡
  只是把漂移点从 alert 挪到了面板。
- **修法**：让面板取同一值——在 `TaskView` 暴露由 `#readableIdentities()` 计算的
  `operationIdentity`（`string | null`），「原执行」行直接渲染它；或删掉该行的独立拼装，
  改为在 alert 已点名时显示 `state.readTarget === "operation"` 对应的读取入口。两种都不改
  `refresh()`。
- **配套测试**：在 Host 的 4 行 `it.each` 里断言 `operationId` 非空格渲染的「原执行」行
  文本包含该 id（现在只断言 alert 与按钮）。

### M2（必须修）：崩溃窗口 alert 的「没有可核对的原素材」是不成立的否定

- **位置**：`web/workbench/src/file-workflow/file-task-session.ts:318`（`read === null &&
  replay` 的句子），以及支撑该判断的注释 `:369-372`（「`record.assetId` 是已确认上传，不是
  `refresh()` 分支」）。
- **事实**：崩溃窗口的持久记录带 `assetId`（v1/v2 记录必填，`task-journal.ts:16`），
  `FileTaskHost` 把它作为 `savedAssetId` 传给面板，「素材」行就渲染这个 id；公网
  `GET /v1/nexus/assets/{asset_id}` 可读。实测：面板首列 `asset-demo`、
  `client.getAsset("asset-demo")` 成功（输出见 A1）。零请求是 `refresh()` 当前的**实现**
  选择，不是该状态**没有**可读对象。
- **为什么算本卡范围**：这是本 PR 新写进产品的句子，且 #780 的前提（「没有任何可读对象」）
  依赖它成立；把假承诺换成假否定，用户拿到的仍是错的事实。
- **修法（择一）**：
  1. 推荐：把 `record?.assetId` 纳入 `#readableIdentities().assetId`（`:551`）。崩溃窗口
     随即 `readTarget = "asset"`，alert 自动变成「请核对原素材；若重放，只使用原输入和原
     键」，按钮恢复可用，`restore()` 恰好发一次 `GET /v1/nexus/assets/{id}`——这就是旧共享
     句里真正可执行的那一半。代价是崩溃窗口从 0 请求变 1 个读请求（与 PR「不新增请求」的
     自设约束冲突，但它换来的是文案属实）。
  2. 保守：只否定「能判定请求是否已执行」的对象，不否定素材存在，例如
     「原请求是否已执行无法确认；本页没有可读取的原任务或原执行身份；重放只使用原输入和
     原键。」——同时改写注释 `:369-372`，不要把「不是 `refresh()` 分支」说成「不可读」。
- **配套测试**：`unconfirmedAlertFor` 的 asset 分支目前零断言（S3）；若选方案 1，崩溃窗口
  的 Host 用例应改为断言按钮可用 + alert 点名核对原素材。

## 建议（非必须）

- **S0（高优先，已开 #782）**：`#run` 的手写 notice 仍在承诺读取（`:714`、`:737`、`:919`
  最容易错，其余见 A1 grep）。我在崩溃窗口复现了同屏矛盾（网络失败后 status 要求「读取
  原任务」而 alert 说「没有原任务」）。建议 #782 里把这 3 条改成由 `readTargetOf` 生成或按
  handle 分派，其余 notice 至少加"此句与读取按钮同源"的注释。
- **S1**：`restore()` 的闸门（`:636`）与判据不同源。生产 `FileTaskHost` 恒有 journal，
  不可达；但 dev demo（`dev/FileTaskDemo.tsx:47`、`dev/TtsTaskDemo.tsx:22`）无 journal 时
  `readTarget = "asset"` 而 `restore()` 空转。建议 `restore()` 直接 `await this.refresh()`
  或把闸门换成 `readTarget !== null`，并在注释里说明这两者曾被分开。
- **S2**：`resumedNoticeFor`（`:373-377`）手工组装 identities，且硬编码 `assetId: null`。
  建议抽 `identitiesOfRecord(record)` 供它与将来的调用共用，避免记录新增 asset 语义时
  这条路径再次与 `#readableIdentities()` 分叉。
- **S3**：`unconfirmedAlertFor` 的 `asset` 分支没有任何文案断言（18 格矩阵结构上产不出
  asset，Host 4 行也没有）。建议用 `asset-reconcile-session.test.ts` 已有的
  「上传失败 + assetId」状态补一条 Host 断言（alert 含「核对原素材」、读取按钮可用）。
- **S4**：`(operation, true)` 与 `(asset, true)` 两个组合无测试。前者在
  `refusalProvesNothing`（`:913-916` 置 `canReplay: true` 且 `retainRecovery` 保留 op id）
  与非 HTTP 失败分支（`:731-733` 不动 recovery）下静态可达；后者在非 HTTP 失败时
  `view.asset` 仍在。建议补一行 `it.each`，或判断产品语义后收紧 `canReplay`
  （`operationId` 已识别时恒 `false`，与 #771 的「已识别执行只查询、不重放」更一致）——
  后者是行为变更，需要单独立卡。
- **S5**：面板注释（`FileTaskPanel.tsx:168`）「Disabled exactly when `refresh()` would send
  no request」在 `busy` 时不成立（busy 也禁用）。建议改成「when not busy and `refresh()`
  would send no request」，并考虑给禁用按钮加 `aria-describedby` 指向 alert，让读屏用户
  知道为什么不可用。
- **S6**：C5 里的「文案提及 ⇔ 按钮启用」是稳定态等式，不是恒等式（busy 期间破裂，且
  `#run` 的 notice 路径也会短暂让 alert 与按钮不同步）。建议在测试名或注释里写明，避免
  后来者把它当不变量使用。

## 只能依赖作者陈述的点

- 作者本地 `pnpm lint`、`pnpm format:check` 的结论：我未单独复跑，但 CI
  `Web: lint, types, tests, build and accessibility`（含 `format:check`、`lint`）在该 head
  上 pass，等价门禁已由 CI 独立覆盖。
- 反例的具体 hunk 写法（例如 D7-1 的「改回共享文案」是删成一行字面量还是包一层函数）：
  我按语义等价复现并得到相同红点数量与位置，未逐字复刻其 patch。
- `gh pr checks 781` 是平台侧运行结果，我只读取、未在本地复跑 CI（本地只复跑了
  `test` / `build && test:behavior` / `test:a11y` / `typecheck`）。
- 作者称 CI 报 974：我核对了 CI 日志原文（`web/workbench test: Tests 974 passed (974)`），
  不依赖陈述，但那个数字来自平台运行，我未在本地重建 974 的合并态。

## 过程记录（时间线）

1. `21:10` 主工作区 `git fetch`；`origin/main` = `7cd0d8c6`。确认 `docs/reviews/` 在
   `origin/main` 上有 44 个文件、**无 `2026-11-14-alert-actions.md`**（后缀未被占用）；
   `gh pr view 781` 读 head/base/`+287/−22`/5 文件与正文。
2. `21:11` 建独立 worktree：`git worktree add /private/tmp/sn783 -b review/783-alert-actions
   origin/main`；`git diff origin/main...HEAD --stat` 为空。另建 `/private/tmp/sn783-pr`
   （detached `ceff9ed0`）与 `/private/tmp/sn783-base`（detached `e1f26c29`）专跑被审内容、
   变异与基线；三处 `pnpm install --frozen-lockfile` 均 ~300ms（命中 store）。
3. `21:13` PR head `pnpm test`：**964 / 63 files**；`21:14` 基线 `e1f26c29`：**923**，
   `origin/main`：**933**。
4. `21:14–21:17` 读 `readTargetOf` / `unconfirmedAlertFor` / `resumedNoticeFor` /
   `#readableIdentities` / `#update` / `restore` / `refresh` 与全部 `#persist` 调用点；
   grep「读取原 / 核对原」与身份谓词，列 A1 判定点清单。
5. `21:15–21:18` 写新旧实现差分探针（8 个恢复态，比较 GET 序列与 notice）与二次 refresh
   探针；崩溃窗口 / `operationId` 恢复态的 jsdom 渲染探针；崩溃窗口 + 网络失败探针（#782）。
   探针跑完即删。
6. `21:18–21:20` 变异：C5 两步（1 红 / 29 绿）、D7 三条（5 / 1 / 14+2）、C6 三条
   （M5 分派漂移 11 红、M6 判据换序 8 红且路由断言全绿、D7-1 文案漂移 5 红）。每次跑完
   `cp` 复原，`/private/tmp/sn783-pr` 的 `git status` 干净。
7. `21:20` `pnpm build` → `pnpm test:behavior` **9 passed** → `pnpm test:a11y`
   **52 passed**（严格顺序，未并发）→ `pnpm typecheck` exit 0。
8. `21:20` 本地重算 CI 路由（`full`，根因 `tests/web/` 不进 web 白名单）；读
   `gh pr checks 781`（18 pass / 2 skipping）与 `Gate: required checks` 日志
   （`Verified scope=full`）、Web job 日志（974）。
9. `21:21–` 写本报告；`uv run --no-project ruff format --check
   docs/reviews/2026-11-14-alert-actions.md` 复核；单文件提交、push 分支、开评审 PR
   （标题 `review: 崩溃窗口 alert 动作一致性的非作者审查`；不合并、不动 `main`）。
