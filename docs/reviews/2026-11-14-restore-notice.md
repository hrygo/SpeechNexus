---
title: PR #774 非作者审查：重载恢复 notice 的自洽性（#771）
status: active
---

# PR #774（`fix/771-restore-notice`）非作者审查记录

被审对象：PR #774《fix: 重载恢复不再同时声称「结果未知」与「确定性拒绝」，四字段组合逐项钉住
(#771)》，head `4e69df7bc4706eabdfad3f39e751798b9ea07d61`（单 commit），base
`e4ba64b7`，相对 `origin/main`（`1d368577`）`+228 / −2`，**2 文件**
（`web/workbench/src/file-workflow/file-task-session.ts` `+12/−1`、
`web/workbench/src/file-workflow/file-task-persistence.test.ts` `+216/−1`），关联
#771（不自动关闭）。

审查方式：独立 worktree `/private/tmp/sn777`，**基于 `origin/main`（`1d368577`）拉分支**
`review/777-restore-notice`；被审内容用 `git show 4e69df7b:<path>` 读出。为满足「
`git diff origin/main...HEAD` 只含报告文件」且不污染审查分支，被审内容与全部变异都在
**第二个 detached 副本** `/private/tmp/sn777-test`（`git worktree add … 4e69df7b --detach`）
里实跑，**从未在被审 head 上开分支、未 push 任何变异**。产品代码/测试在本审查中零改动：
每次变异跑完 `cp` 原文件或 `git checkout --` 复原，`git status` 已确认干净。
环境：系统 node `v26.12.1`、pnpm `12.14.0`、uv `0.13.5`。

## 结论

**有 1 项必须修（M1：新文案的重放子句未受 `canReplay` 约束，在一个真实可达的恢复状态下
承诺了页面禁用、且 `replay()` 完全不发请求的动作）。其余八条成立，无夸大、无错报；
作者的“九宫格”判定与三条反例我逐条独立复现，红点位置与其描述一致。**

PR 正确地修掉了 #771 的主症状：崩溃窗口（`pending: "create"`、无 `settled`）重载后不再
冒充“确定性拒绝并作废”，且 `pending === null` 的兜底也不再冒充拒绝；四字段推导逐字未改
（B4 已核），三条相容性是**结构定理**而不是测试强行成立（B5 已核，见下）。留下的问题不是
回退，而是同一族缺陷最后一格没关上：`(pending, settled)` 二元组不足以描述投影，第三轴
`operationId` 才是 `canReplay` 的真正开关，而 `operationId !== null` 的两个可达格里，
新 notice 仍写着“或按原输入与原键重放”。矩阵按 `operationId = null` 固定构造，看不到这格。

### 本卡两个核心问题的答案

**「9 组合哪些真实可达」：6 个可达、3 个不可达；作者标的 3 个 ❌ 判定正确，且理由正确——
是 `task-journal.ts` 的 `settled` 不变量真的拒绝它们，不是“产品暂时没写到”。但矩阵的自变量
选得不够：真实状态是 `(pending, settled, operationId)` 三维，共 18 个格，矩阵钉了
`operationId = null` 的 9 格；另一轴 9 格里有 2 格可达（`create`/`cancel` × `settled:
undefined` × `operationId: "op-demo"`），恰是 notice 与按钮分裂处。这是本 PR 唯一的功能性
缺口（M1），也是矩阵未能覆盖它的原因。**

**「三条相容性是否恒成立」：恒成立，且是代码结构决定的定理，不是断言凑出来的。**
`canReplay` 的谓词是 `unknown` 谓词的**真子集合取**
（`pending !== null && settled === undefined` **且** `operationId === null`），所以
“`canReplay` 为真而 `unknown` 为假”在语法上就不可能构造；`rejected` 与 `unknown` 由
`settled === undefined` 互斥；`unreplayable` 只在 `settled === "unreplayable"` 时为真、
而 `rejected` 在任意 `settled` 为真时为真。我把枚举扩到 **18 格**（含 `operationId` 两值）
实跑：**违例 0 条**。B5 里担心的“`unknown=false` 而 `canReplay` 可为真”不存在，这条断言
钉的是真定理。

九条判定一览：

| # | 条目 | 判定 | 关键证据 |
| --- | --- | --- | --- |
| 1 | 9 组合覆盖真实可达状态 | **成立（轴上不全）** | 6 可达 / 3 不可达；3 个 ❌ 判断正确；见 A1 |
| 2 | 新文案与 `role=alert` 一致且可执行 | **不成立（M1）** | 崩溃窗口可执行；`operationId` 已识别时可达但 `canReplay=false` |
| 3 | `pending === null` 兜底文案 | 成立 | 该状态可达（`#acceptJob` 落 `pending: null`），有 `jobId` 可读 |
| 4 | 四字段逐字未改 | 成立 | `git diff` 只增注释与 notice 三元，四行逐字相同 |
| 5 | 三条相容性恒成立 | 成立 | 18 格枚举 0 违例；`canReplay` 谓词是 `unknown` 谓词子集 |
| 6 | 三条反例红在预期断言 | 成立 | 反例 1 = 4 红、反例 2 = 6 红、反例 3 = 删行后整文件绿而探针被真实流程用例抓住 |
| 7 | 既有 9 条浏览器规格未改 | 成立 | `tests/web/` diff 为 0；`test:behavior` 9 passed |
| 8 | 只改 2 文件、无锁文件、无新依赖 | 成立 | `--name-only` 仅 2 个 TS 文件 |
| 9 | 例数与 CI 路由 | 成立 | 913/904/9/52 全部实测；`excluded_shards=[0,1,2,3]`；Integration 4/5 实跑 pass |

## A1：9 组合的可达性（独立判断）

读 `#persist` 的全部调用点与 `task-journal.ts::validRecord`，逐个组合判断“产品真的会写出
该形状”。结论表：

| `pending` | `settled` | 作者标注 | 我的判断 | 写入点 / 拒绝点 |
| --- | --- | --- | --- | --- |
| `null` | `undefined` | valid | **可达** | 建单成功 `#acceptJob(job, { jobId: job.id, pending: null, operationId: null })`（`file-task-session.ts:1025`）；取消成功 `#acceptJob(job, { pending: null, operationId: null })`（`:1171`）；重载后是“已确认执行” |
| `null` | `terminal` | invalid | **不可达（正确）** | `task-journal.ts:221` `settled` 要求 `row.pending !== null` |
| `null` | `unreplayable` | invalid | **不可达（正确）** | 同上（`:221`） |
| `create` | `undefined` | valid | **可达** | `#persist({ pending: "create" })`（`:1019`）后崩溃/丢应答 = #771 的崩溃窗口 |
| `create` | `terminal` | valid | **可达** | `#persist({ settled: "terminal" })`（`:822`）在 `settlesCreate` 时 |
| `create` | `unreplayable` | valid | **可达** | `#persist({ settled: "unreplayable" })`（`:733`）在 413 首答且无已识别执行时 |
| `cancel` | `undefined` | valid | **可达** | `#persist({ pending: "cancel", operationId: null })`（`:1168`）后丢应答（#723） |
| `cancel` | `terminal` | valid | **可达** | `:822` 在 `settlesCancel` 时（409 `idempotency_conflict` 等） |
| `cancel` | `unreplayable` | invalid | **不可达（正确）** | `task-journal.ts:223` `unreplayable` 要求 `pending === "create"` 且 `jobId === null`，与 `pending === "cancel" && jobId !== null`（`:205`）矛盾 |

补充两点独立判断：

1. 三个 ❌ 不是“暂时没写到”，而是**持久层拒绝**：`validRecord` 在 `load()` 与 `save()` 两处
   都会跑，所以即使将来有代码写这种记录也会 `TaskJournalError("invalid")`，不会被读出。
   矩阵用 `journalVerdict()` 把这条拒绝也钉住了，这一设计是对的（防止将来放宽 journal 后
   投影开始过示拒绝）。
2. 这 3 行的 notice 断言（`terminalCreateNotice` / `unreplayableNotice`）**今天不可达**，
   属于防御性投影断言；作者在表格注释里明确写了 `invalid` 与其含义，没有把“钉住的字符串”
   说成“用户会看到的状态”。这一点是诚实的。

## A2：新文案是否可执行（M1 的证据）

崩溃窗口（`operationId === null`）这一格是成立的：矩阵第 4 行与既有用例
`keeps a lost create response unknown and never records a settled refusal` 都断言
`unknown: true / canReplay: true / rejected` 缺省（`file-task-persistence.test.ts:198-206`），
且该用例新加的 `notice` 断言正是这句新文案；`canReplay: true` 意味着
`disabled={state.busy || !state.canReplay}`（`FileTaskPanel.tsx:169`）的按钮可用、
`#replay` 已赋值（`:438-441`），重放真的会发请求。

与同页 `role=alert`「请求结果尚未确认。请核对原素材或任务；重放只使用原输入和原键。」
（`FileTaskPanel.tsx:96`）语义一致：都不再声称“确定性拒绝”。**但同一条文案会在
`operationId !== null` 的可达状态里出现，而那里 `canReplay === false`**：

```
saved:            { pending: "create", operationId: "op-demo", settled: false }
reloadedNotice:   "已找到原任务身份；该请求结果尚未确认，请读取原任务核对，或按原输入与原键重放。"
reloadedUnknown:  true
reloadedCanReplay:false
replayButton:     disabled   （FileTaskPanel.tsx:169 disabled={state.busy || !state.canReplay}）
replay() 请求数:  2 → 2       （#replay 未赋值，replay() 直接返回，零请求）
cancel 对应的同一格实测结果相同：pending=cancel / operationId=op-demo / canReplay=false
```

可达性不是构造出来的，是产品流程写的：`#persist({ operationId })`（`:596`）在
`operation_in_progress` / `response_not_replayable` 时把执行身份落盘；契约 409 的
`operation_in_progress` 明确要求“Query GET /v1/nexus/operations/{operation_id}”
（`contracts/speech-openapi.yaml:2904-2906`），即**查询而不是重放**。同一页在重载前说的是
「原执行已有恢复身份。请重新读取原执行状态；不要创建替代执行。」（`:605`），重载后却
改口让用户重放——这正是 #771 同族的“notice 与页面能力不一致”，只是换了一个轴。
既有用例 `keeps cancellation pending when operation recovery only reads its associated job`
（`file-task-persistence.test.ts:339-380`）已经把这个状态写进 journal，所以它不是我拼的。

修复方向很小，不需要改四字段：notice 的两个新分支按 `record.operationId === null` 再分一次，
非空时说“已有可查询的执行身份，请读取原执行/原任务”，与实时页对齐；并在矩阵里补上
`operationId: "op-demo"` 的两行。

## A3：`pending === null` 的兜底文案

状态存在且文案成立。`pending: null` 由 `#acceptJob`（`:1025`、`:1171`）写入，表示“执行已
确认、有 `jobId`”；重载后 `unknown=false / rejected=false / canReplay=false`，页面顶部
就是「读取原状态」按钮（`FileTaskPanel.tsx:163`），点下去 `refresh()` 走 `#record.jobId`
分支（`:1122-1131`）读到任务并把 notice 换成「已重新读取当前任务。」。所以
“请读取原任务状态”是可执行的，也比旧的“确定性拒绝并作废”正确。
（注意 `pending: null` 且 `operationId !== null` 被 `task-journal.ts:208` 拒绝，因此不存在
“已确认但仍有执行身份”的矛盾形状。）

## B4：四字段逐字未改

`file-task-session.ts` 与 `origin/main` 的差异只有两处：新增 7 行注释、notice 三元多两层
分支。四行推导逐字相同（`origin/main` 与 PR head 的值）：

```
unknown:      record.pending !== null && settled === undefined,
canReplay:    record.pending !== null && record.operationId === null && settled === undefined,
rejected:     ...(settled === undefined ? {} : { rejected: true }),
unreplayable: ...(settled === "unreplayable" ? { unreplayable: true } : {}),
```

（PR 行号 `:420` `:421` `:423` `:426`。）作者“四字段推导不变”的陈述属实。

## B5：三条相容性是否恒成立

**是，且是结构性的**：设 `U = (pending !== null ∧ settled === undefined)`，
`R = canReplay = U ∧ (operationId === null)`。`unknown = U`，故 `R ⇒ U ⇒ unknown`；
`rejected ↔ settled !== undefined`，与 `U` 的 `settled === undefined` 互斥；
`unreplayable → settled === "unreplayable" → rejected`。三条都不依赖测试里写什么。

独立验证（不是复用矩阵的 `expect`）：枚举 `pending ∈ {null, create, cancel}` ×
`settled ∈ {undefined, terminal, unreplayable}` × `operationId ∈ {null, "op-demo"}` = **18 格**，
用真实 `FileTaskSession` 构造快照后逐格判三条不变量：

```
pending=null   settled=undefined   op=null     unknown=false canReplay=false rejected=false unreplayable=false
pending=null   settled=undefined   op=op-demo  unknown=false canReplay=false rejected=false unreplayable=false
pending=null   settled=terminal    op=null     unknown=false canReplay=false rejected=true  unreplayable=false
pending=null   settled=terminal    op=op-demo  unknown=false canReplay=false rejected=true  unreplayable=false
pending=null   settled=unreplayable op=null    unknown=false canReplay=false rejected=true  unreplayable=true
pending=null   settled=unreplayable op=op-demo unknown=false canReplay=false rejected=true  unreplayable=true
pending=create settled=undefined   op=null     unknown=true  canReplay=true  rejected=false unreplayable=false
pending=create settled=undefined   op=op-demo  unknown=true  canReplay=false rejected=false unreplayable=false
pending=create settled=terminal    op=null     unknown=false canReplay=false rejected=true  unreplayable=false
pending=create settled=terminal    op=op-demo  unknown=false canReplay=false rejected=true  unreplayable=false
pending=create settled=unreplayable op=null    unknown=false canReplay=false rejected=true  unreplayable=true
pending=create settled=unreplayable op=op-demo unknown=false canReplay=false rejected=true  unreplayable=true
pending=cancel settled=undefined   op=null     unknown=true  canReplay=true  rejected=false unreplayable=false
pending=cancel settled=undefined   op=op-demo  unknown=true  canReplay=false rejected=false unreplayable=false
pending=cancel settled=terminal    op=null     unknown=false canReplay=false rejected=true  unreplayable=false
pending=cancel settled=terminal    op=op-demo  unknown=false canReplay=false rejected=true  unreplayable=false
pending=cancel settled=unreplayable op=null    unknown=false canReplay=false rejected=true  unreplayable=true
pending=cancel settled=unreplayable op=op-demo unknown=false canReplay=false rejected=true  unreplayable=true
violations: []
```

**0 违例**。所以矩阵里的三条相容性断言不是在钉假定理；B5 猜想的反例形态
（`unknown=false` 而 `canReplay=true`）不存在——`canReplay` 的条件里就带着 `unknown` 的条件。
表里第 8、14 两行（`op=op-demo`、`settled=undefined`）是矩阵没覆盖的可达格，也是 M1 的现场。

## C6：三条反例（独立复现）

全部在 `/private/tmp/sn777-test` 临时应用、跑完复原。红点与作者描述逐项吻合。

**反例 1：把 `settled === undefined` 并回“确定性拒绝”分支**（删掉 PR 新增的两层三元）：

```
× keeps a lost create response unknown and never records a settled refusal
× pending=null   settled=undefined projects one coherent verdict
× pending=create settled=undefined projects one coherent verdict
× pending=cancel settled=undefined projects one coherent verdict
      Tests  4 failed | 19 passed (23)
```

= 两条 unknown 组合 + crash-window 真实流程回归 + `pending=null` 行，**4 红**，与作者所述一致。

**反例 2：放宽 `canReplay`（去掉 `settled === undefined`）**：

```
× restores a terminally refused create as settled instead of an unknown submission
× restores a terminally refused cancel as settled instead of an unknown cancellation
× pending=create settled=terminal     projects one coherent verdict
× pending=create settled=unreplayable projects one coherent verdict
× pending=cancel settled=terminal     projects one coherent verdict
× pending=cancel settled=unreplayable projects one coherent verdict
      Tests  6 failed | 17 passed (23)
```

**6 红** = 4 个已结算组合行 + 2 条真实流程（terminal create / terminal cancel），与作者所述一致。
（`pending=null` 的两个已结算行保持绿，因为 `canReplay` 仍要求 `pending !== null`。）

**反例 3：删除 `(create, terminal)` 这一行**（矩阵 9 → 8 行）：

```
（整文件）      Tests  22 passed (22)                 ← 删行本身不红
（仅矩阵）      Tests  8 passed | 14 skipped (22)    ← 删行 + 探针（只让该组合可重放）仍全绿
（整文件）      Tests  1 failed | 21 passed (22)     ← 同一探针被真实流程用例抓住
      × restores a terminally refused create as settled instead of an unknown submission
（恢复该行 + 同一探针，仅矩阵）
      × pending=create settled=terminal projects one coherent verdict
```

结论与作者自述完全一致，且**“矩阵行提供的是 cell 级断言而非唯一守卫”这句话是诚实的**：
`(create, terminal)` 这一格的可重放性已被真实流程用例的 `canReplay: false` 断言守住
（`file-task-persistence.test.ts:164-169`），补上删掉的行只增加“同一格的字段+文案一起钉”
的本地性，而不是唯一防线。但这句话**不适用于全部 9 行**：例如
`(pending=null, settled=undefined)` 与 `(cancel, settled=undefined)` 的 notice 只有矩阵在钉，
删行就会丢掉文案级守卫。

## C7：既有 9 条浏览器规格

`git diff origin/main...4e69df7b --stat -- tests/web/` 输出为空；`grep -c '^test('`
`tests/web/file-task.behavior.spec.mjs` = 9。规格里对 notice 的断言只涉及**未改动**的三条
分支（`原请求已被协议确定性拒绝，原意图作废`、`该取消请求已被协议确定性拒绝并作废`、
`取消请求被拒绝（409 job_retry_not_allowed）…`，见 spec `:246` `:255-258` `:423` `:457`），
新文案没有触碰任何规格断言。`test:behavior` 实跑 9 passed 也印证。

## D8：范围与依赖

```
git diff origin/main...4e69df7b --name-only
web/workbench/src/file-workflow/file-task-persistence.test.ts
web/workbench/src/file-workflow/file-task-session.ts
```

无 `uv.lock`、无 `pnpm-lock.yaml`、无 `package.json`、无 `docs/`、无 `contracts/`；
无新依赖（`pnpm install --frozen-lockfile` 在 detached 副本上通过即证明 lockfile 未被改动）。

## D9：例数与 CI 路由

实测（本机，顺序执行）：

```
web/workbench: pnpm test          → Test Files 63 passed (63) / Tests 913 passed (913)
origin/main  : pnpm test          → 904 passed (904)              ← 基线一致，+9 = 矩阵 9 行
pnpm build && pnpm test:behavior  → 9 passed (5.2s)
pnpm test:a11y                    → 52 passed (29.6s)             ← 与 behavior 顺序跑，未并发
pnpm typecheck                    → 全 workspace Done，exit 0
```

CI 路由按 PR 口径本地重算（`scripts/dev-env/ci-routing.py --event pull_request --base
<PR base>`）：

```
{"mode": "web", "reason": "Web sources changed; retain Python consumer/unit checks, the complete Web lane and the Web-capable integration partitions."}
mode=web
excluded_shards=[{"shard": 0}, {"shard": 1}, {"shard": 2}, {"shard": 3}]
```

`gh pr checks 774`：16 项中 11 success / 5 skipping。
`Integration 4: Web + PG/MySQL/MinIO` **pass (9m14s)**、`Integration 5` **pass (9m36s)**
是**实跑**（有时长即非 skip）；`Web: lint, types, tests, build and accessibility`
pass (3m49s) 覆盖了 `format:check`、`lint`、`typecheck`、`test`、`build`、`a11y`、
`test:behavior`（`.github/workflows/ci.yml:924-949`，behavior 与 a11y 在该 job 内顺序执行）。
5 个 skipping **全部是路由语义，不是失败**：

| skipping 作业 | 条件 | 判定 |
| --- | --- | --- |
| `Gate: six-shard integration evidence and merged coverage` | `if mode == 'full'` | web 路由不跑六分片，由 required-checks gate 反向校验 `coordination=skipped` |
| `Compose: configuration validation` | `if full \|\| no-db` | 同上，gate 期望 `deployment=skipped` |
| `Deploy: reference acceptance entry (dry-run execution)` | `if full \|\| no-db` | 同上 |
| `Docs: markdown code block formatting` | `if mode == 'docs'` | web 路由非 docs |
| `Node Controller image evidence / Build and probe` | 复用工作流的 scope 判定不适用 | `Node Controller image evidence / Gate` 与 `scope` 均 success，required-checks gate 校验了 `CONTROLLER_REQUIRED` |

关键证据是 `Gate: required checks for this change` **pass**：该 job 在 `mode=web` 下
按 `case "$MODE" in web)` 的期望值**逐项硬校验**七项下游结果——
`checks=success; coordination=skipped; integration=success; web=success; deployment=skipped;
acceptance=skipped; docs_format=skipped`（`ci.yml:852-874`），并单独校验
`CONTROLLER_REQUIRED` 与 controller 结果（`ci.yml:834-841`）。任何“意外跳过”都会让它红
并打出 `Scope=…; Python=…` 一类的错误行。所以“这些 skipping 是路由语义”不是我的推断，
是这条 gate 自己的判定：上表 5 个 skipping 正好落在 web 路由要求的 `skipped` 集合里。

## 必须修清单

### M1（必须修）：notice 的“按原输入与原键重放”没有受 `canReplay` 约束

- **位置**：`web/workbench/src/file-workflow/file-task-session.ts:434-436`（新 notice 第三层
  三元），对应 `record.operationId !== null && settled === undefined && pending !== null`。
- **事实**：该状态下 `canReplay === false`（`canReplay` 要求 `operationId === null`，
  `:421`），`#replay` 未赋值（`:438-441`），按钮 `disabled`（`FileTaskPanel.tsx:169`），
  `replay()` 零请求。可达路径真实：`#persist({ operationId })`（`:596`）在 409
  `operation_in_progress` / `response_not_replayable` 时落盘，契约要求“查询”而不是重放
  （`contracts/speech-openapi.yaml:2904-2906`）。
- **为什么算本次范围**：PR 的目标就是“notice 不再与页面能力矛盾”；这一格的重载前文案
  （`:605`「原执行已有恢复身份…不要创建替代执行」）是对的，重载后被新文案改成
  “可以重放”，属于同一族缺陷换轴复发，且矩阵按 `operationId = null` 构造看不到。
- **修法**：在 `pending !== null && settled === undefined` 分支里按
  `record.operationId === null` 再分一次；非空时用“已取得原执行身份，请读取原执行/原任务，
  不会按此键重放”一类文案（与实时页一致）。四字段不动。
- **配套测试**：矩阵补两行（`(create, "op-demo", undefined)`、`(cancel, "op-demo", undefined)`），
  断言 `unknown=true / canReplay=false / rejected=false` 且 notice **不含**“重放”字样——
  这两行同时把 B5 的 18 格表里第 8、14 行钉住。

## 建议（非必须）

- **S1**：崩溃窗口（`operationId === null`、`jobId === null`、无 asset）下，“请读取原任务核对”
  这一半是空动作：`refresh()` 三个分支都不命中（`:1098-1136`），点「读取原状态」不发任何请求、
  notice 不变。该格真正可执行的只有重放。可考虑在该形状下把读取子句降为“读取本页记录”或
  直接只保留重放（alert 已给出“核对原素材或任务”的说明）。
- **S2**：矩阵注释“`operationId` is `null` in every row because that is the only shape where
  `canReplay` is still a question”是对的（`op != null` 时 `canReplay` 恒假），但这句话容易让
  后来者以为“另一轴不需要钉”。建议在注释里补一句“另一轴的 notice 仍需单独断言”，与 M1 的
  两行测试配套。

## 只能依赖作者陈述的点

- 作者本地 `pnpm lint` / `pnpm format:check` 的结论：我未单独复跑，但 CI
  `Web: lint, types, tests, build and accessibility`（含 `pnpm format:check`、`pnpm lint`）
  在该 head 上 pass，等价门禁已由 CI 独立覆盖。
- 作者三条反例**具体 hunk 的写法**（例如“放宽 `canReplay` 到 `settled !== undefined` 也允许”的
  字面改法）：我按语义等价复现并得到相同红点数量与位置，但红点与具体字符改法无关，故此处
  以“语义等价”为准，不逐字复刻其 patch。
- 作者称“矩阵 9 行由 `SessionTaskJournal` 证明 3 个不相容组合不可持久化”：我核对了
  `validRecord` 的三条不变量（`:205` `:221` `:223`）并确认 `journalVerdict()` 真的走
  `SessionTaskJournal.load()`，结论一致。

## 过程记录（时间线）

1. `19:26` 在仓库主工作区 `git fetch`；`docs/reviews/` 在 `origin/main` 上 42 个文件、
   无 `2026-11-14-restore-notice.md`（同名后缀未被占用）。`gh pr view 774` 确认
   head/base/`+228/−2`/2 文件。
2. `19:27` 按要求建独立 worktree：`git worktree add /private/tmp/sn777 -b
   review/777-restore-notice origin/main`（base `1d368577`）。随后另建
   `/private/tmp/sn777-test`（detached `4e69df7b`）专跑被审内容与变异，保证审查分支干净。
   两处 `pnpm install --frozen-lockfile --prefer-offline` 均 ~300ms（命中 store）。
3. `19:29` `pnpm test`：PR head **913 / 63 files**；`origin/main` **904**（基线一致，+9）。
4. `19:29–19:31` 读 `#persist` 全部调用点、`#acceptJob`、`#submit`、`#cancelIntent`、
   `refresh()`、`task-journal.ts::validRecord`，独立判 9 组合可达性（A1）。
5. `19:31–19:33` 写两个临时探针（跑完即删，留在 detached 副本）：
   (a) 真实流程重建 `operationId !== null` 且无 `settled` 的两格并读重载快照（M1 证据）；
   (b) 18 格枚举判三条相容性（B5，0 违例）。
6. `19:33–19:34` 三条反例变异：反例 1（并回拒绝分支）4 红；反例 2（放宽 `canReplay`）6 红；
   反例 3（删行 / 删行+探针 / 恢复行+探针）分别 22 绿 / 8 绿 / 1 红 / 该行红。每次跑完复原，
   `git status` 干净。
7. `19:34–19:36` `pnpm build` → `pnpm test:behavior` **9 passed** → `pnpm test:a11y`
   **52 passed**（严格顺序，未并发）→ `pnpm typecheck` exit 0。
8. `19:36` `gh pr checks 774` 读 16 项；本地重算 CI 路由得 `mode=web`、
   `excluded_shards=[0,1,2,3]`；逐个核对 5 个 skipping 的条件与 required-checks gate 的
   硬校验（D9）。
9. `19:37–19:40` 写本报告；`uv run --no-project ruff format --check
   docs/reviews/2026-11-14-restore-notice.md` 复核；单文件提交、push 分支、开评审 PR
   （标题 `review: 重载恢复 notice 自洽性修复的非作者审查`；不合并、不动 `main`）。
