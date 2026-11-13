# terminal 拒绝出口与 `settled` 持久化字段的非作者审查（PR #708 / issue #707）

> 过程记录文档：保留时间线、独立复现的反例与命令输出。结论面向合并决定，不修改产品代码。
> 审查对象：`fix/707-terminal-exit`，head `317c957dcca18a510f58499111f5e90025f40572`（下称 head），base `7147f306`（= 审查时 `origin/main`）。
> 文件名沿用任务指定的 `2026-11-13`；实际审查发生在 2026-11-14 06:05–06:2x（本地时间，Asia/Shanghai），下文时间线用本地时间。
> 主检出停在陈旧分支 `docs/tracker-batch12`，所有内容一律在独立 worktree 或 `git show <rev>:<path>` 读取。

## 结论

**有条件通过**：`#707` 的两项缺陷都修对了，`settled` 的**格式面**限定得相当紧（严格形状校验、key 数校验、禁止 un-settle、旧 reader 安全失败、无自动清除），但**写入条件**有一个真实缺口——「先未知、后重放遇到非准入 4xx」会把一个真未知**持久**降级成 `rejected`。这一条必须收紧，其余为文案与加固项。

判定依据：

- 缺陷 (1)(2) 独立复现成立：head 上 terminal 创建拒绝确实给出可放弃出口（`canFinishTask` 从恒 false 变为 true），且刷新后不再复活成「结果未知」；基线 `7147f306` 上同一序列刷新后仍回到 unknown。
- 新增字段的**读**侧限定正确：新 reader 仍接受旧记录；用**真实的 pre-#707 模块**（`git show 7147f306:…/task-journal.ts`）读新记录得 `task recovery storage invalid` 且不改写存储；Host 在 invalid 时不发任何请求、不清记录。
- 新增字段的**写**侧基本正确：只有 terminal 分支、且 `operation==="create" && !view.job && !record.jobId && !record.operationId` 才写；两个崩溃窗口（响应丢失、响应已到但页已销毁）、以及 `#persist` 失败都**不会**写入 `settled`，失败方向安全（退回 pre-#707 的 unknown）。
- 但写侧漏了一个入口：terminal 分类包含「在原键重放检查**之前**就可能返回的 4xx」（401/403/400/404/405）。当该记录此前已处于 unknown（合同要求保留原键与原请求）时，用户在 UI 上「按原键重放」遇到这类 4xx，会**持久**写入 `settled:"terminal"` 并清掉 unknown，页面从此宣称「确定拒绝、刷新不会再变回结果未知」，唯一出口是放弃 → 新幂等键 → 若首次提交其实已执行，就会产生第二次执行。这正是 #691/#707 要防的重复执行，且比 pre-#707 更糟（pre-#707 刷新后能回到 unknown）。
- 门禁全绿：head `pnpm test` 786 passed、基线 770 passed、typecheck/lint/format:check 全通过、`ruff format --check .` 1820 files already formatted。作者的反例数字 `17 failed / 247 passed` 在 `src/file-workflow` 子集上精确复现（但它是**复合**变异：单一变异分别为 13 / 4）。

## 审查对象与方法

- 独立 worktree：`git fetch && git worktree add /private/tmp/sn709 -b review/709-terminal-exit 317c957d`（按任务指定）。
- 基线 worktree：`git worktree add --detach /private/tmp/sn709-base 7147f306`，用于例数 770 与「pre-#707 行为」对照。
- 临时副本（变异与探针，均未进入任何提交、未 push）：
  - `/private/tmp/sn709-probe`：head 源码 + 我的 13 例独立探针（`zz-review-probes.test.ts` 11 例 node、`zz-review-host-probe.test.tsx` 2 例 Host 级）；
  - `/private/tmp/sn709-mut`：head 源码 + 6 个变异实验；
  - `/private/tmp/sn709-base`：基线源码 + 1 例对照探针。
- 复现环境：系统 node v26.12.1、pnpm 12.14.0；`pnpm install --frozen-lockfile`（复用 store，未改动 `pnpm-lock.yaml`/`uv.lock`）。未使用 dsh 自带 node，故本报告所有数字与作者口径一致（系统 node）。
- 契约/后端核对：`contracts/speech-openapi.yaml`（create 路由 400/401/403/404/405/409/422/429/503、`Conflict` 与 `JobCancellationConflict` 组件）、`contracts/shared/errors.schema.json`（`code` 仅受正则约束、`nexus` 仅 `operation_in_progress`/`response_not_replayable` 强制）、`backend/.../interfaces/http/jobs.py`、`job_errors.py`。

基础门禁（`/private/tmp/sn709`）：

```text
$ cd web/workbench && pnpm test
 Test Files  62 passed (62)
      Tests  786 passed (786)

$ cd /private/tmp/sn709-base && cd web/workbench && pnpm test     # 基线 7147f306
      Tests  770 passed (770)                                     # 作者"770 → 786（+16）"成立

$ cd web/workbench && pnpm typecheck   → tsc -p tsconfig.json，exit 0
$ pnpm lint                            → Design tokens … [PASSED]；ESLint: passed
$ pnpm format:check                    → All matched files use Prettier code style!
$ uv run --no-project ruff format      → 1820 files left unchanged
$ uv run --no-project ruff format --check .  → 1820 files already formatted

$ cd web/workbench && pnpm vitest run src/file-workflow    # 作者的 247+17 口径子集
 Test Files  13 passed (13)
      Tests  264 passed (264)
```

## 八条逐条判定

### 1. `settled` 的写入条件是否真的安全 —— 通过（四个窗口自测无假写），但有一处「注释已承诺、代码未兑现」的加固项

head 的写入点只有一处（`file-task-session.ts:497-532`）：`rejection === "terminal"` 且

```text
spent = operation === "create" && !this.#view.job && !this.#record?.jobId && !this.#record?.operationId
```

我用真实 `FileTaskSession` 造窗口（探针在临时副本内）：

| 窗口 | 期望 | 实测 | 判定 |
| --- | --- | --- | --- |
| ① 请求已发出、响应丢失（`TypeError`） | 无 `settled`，重载 `unknown:true` | 存储无 `settled`；重载 `unknown=true, canReplay=true, canFinishTask=false` | 对 |
| ② 响应已到达、页面在 `#persist` 之前销毁（`dispose()` → lifetime abort） | 无 `settled` | `pending:"create"` 已先落盘，终态分支被 `#run` 的 abort 早退拦下；重载 `unknown:true` | 对 |
| ③ `#persist({settled})` 本身失败（`setItem` 抛错） | 不产生假声明，失败方向安全 | 盘上仍无 `settled`；页内 `rejected:true`；重载退回 `unknown=true, canReplay=true` | 对（退化为 pre-#707 的状态，见"新红"一节） |
| ④ 4xx 但不是契约信封（代理/网关返回 `{"detail":…}`） | 不得 settle | `parse("ErrorResponse")` 抛 `ContractValidationError`（不是 `SpeechResponseError`）→ 走「非契约错误」分支：`unknown=true, canReplay=true`，无 `settled` | 对 |

第 ④ 条是我认为最值得记的一层保险：`SpeechResponseError` 只在信封通过契约校验后才构造，因此**任意**无法解析的 4xx 都不会被当成「协议确定性拒绝」。

加固项（必须修清单第 2 条）：`spent` 只看**持久化**的 `record.operationId`，没有看**当前响应信封**里的 `contractError.detail.error.nexus?.operation_id`；而终态分支同时把 `recovery` 置 `undefined`。也就是说「终态 4xx + 信封带 operation_id」会写 `settled` 并丢掉那个可查询的执行身份，与分支自己刚写的注释「An identified original execution stays queryable, so it is never settled」直接矛盾。按当前契约只有 `operation_in_progress` / `response_not_replayable` 强制携带 `nexus`，二者都在更早的分支被接走（`file-task-session.ts:405-422`，仍 `unknown:true` 并持久化 `operationId`），所以**今天不可达**；但它是一行即可兑现的承诺。

### 2. 两个反直觉的码 —— 通过（两条都独立验证，未被放宽）

- `409 idempotency_conflict` → 可 settle，但新意图必须新 key。独立复现（我的 P6）：首次 create 得 409 后 `unknown:false, rejected:true, canReplay:false`，存储 `settled:"terminal"`；在放弃之前 `replay()`/`continueAfterValidation()`/`start()` 都不产生第二次 create（`createKeys` 长度仍 1）；只有 Host 的显式放弃（清键）后的新会话才提交第二次，且 `createKeys[1] !== spentKey`。契约依据成立：`speech-openapi.yaml:805` 的「a different intent returns 409 idempotency_conflict」意味着同一 key 重复提交本就会再次冲突，替换必须换 key。
- `409 execution_cleanup_pending`（create 路径）→ 必须仍 unknown、不写 `settled`。独立复现（P5）：`unknown:true`、`rejected` 为 `undefined`、存储无 `settled`、`canFinishTask=false`。守卫在 `file-task-session.ts:103-104`，`git show 7147f306:…` 证明它来自 #699 返修、本 PR **只改了注释未动判断**；把它改成 terminal 后恰好红 1 例（第 5 条 M-D）。
- 顺带核对另一侧：`409 execution_cleanup_pending` 在 **cancel** 路径仍 terminal（`file-task-session.ts:103` 的 `operation !== "cancel"`），而 cancel 路径 `spent` 因 `operation === "create"` 为假而**不会**写 `settled`，取消意图的持久状态未被本 PR 改变。

### 3. `save()` 不清除 `settled` —— 通过；其它写路径结构上不可达，「忘记携带」只有 `save()` 一道保险

- 丢弃字段确实抛错：把 `settled` 从记录里摘掉再 `save()` 得 `TaskJournalError("changed")`（我的 P9 覆盖 `{settled:undefined}` 变体；作者的 `task-journal.test.ts` 覆盖 `{...rest}` 变体）。删掉 `task-journal.ts:297` 那条判断后该用例立刻红（第 5 条 M-E），守卫被测试咬住。
- 其它写路径（`#rememberAssetHint`、cursor、`operationId`）会不会无意丢掉 `settled`：
  - 任务记录的**唯一**写入口是 `#persist`，它用 `{...this.#record, ...update}` 展开，天然携带 `settled`；其余 `journal.save` 调用点都写 **upload 记录**（`#prepareUpload` / `#rememberAssetHint` / `#acknowledgeUpload`），而任何一次任务记录写入都会把 `#upload` 置 `null`，所以 settle 之后这些路径写不了 upload。
  - cursor 写入（`reconnect` / `history-expired`）要求 `view.job.id`，settle 后无 job；`operationId` 写入只发生在两个 unknown 分支，settle 后不可能再提交。
  - 即便未来出现删除 `settled` 的写入，`save()` 会抛 `changed`，属 fail-closed。
  - 我还做了一个「调用侧忘记携带」的变异（M-F：让 `#persist` 展开前先剔掉 `settled`）：`src/file-workflow` 264 例**全绿**——因为今天不存在 settle 之后的写入路径，所以这条只能靠 `save()` 守卫兜住，不是测试咬住的。记为非阻塞建议。

### 4. 「旧代码读新记录 = 安全失败」是否属实 —— 属实（用真实旧模块验证，不是推演）

- (a) 新 reader 对不含 `settled` 的旧记录仍正常接受：P8 用 v1 记录直测 `load()` 成功且 `pending:"create"` 照旧；v2 记录由现有 786 例覆盖。
- (b) 我把 `git show 7147f306:web/workbench/src/file-workflow/task-journal.ts` 原样导入为 `task-journal-old.ts`，用它读一条**真实产生**的 `settled:"terminal"` 记录：抛 `TaskJournalError`，message 恰为 `task recovery storage invalid`，且**未改写**存储（P8）。不是「误读成 unknown」，是硬失败。
- (c) Host 侧：`FileTaskHost.tsx:31` 的 `journal.load()` 抛错会被 `initialize()` 的 try 捕获 → 返回 null → 渲染 `role="alert"`「恢复信息不可用。请修复浏览器存储后重新载入；本页已暂停提交，原记录未清除。」；`FileTaskSession` 从未构造，`upload`/`create`/`fetch` 零调用，`removeItem` 从未执行（P12 用「真实 settled 记录 + 未知字段」复现同一路径；作者的同路径用例也绿）。
- 需要写明的前提：`sessionStorage` 是**每标签页**的，所以「旧 reader」只在浏览器缓存了旧 bundle（部署回滚、旧页面前进后退）时才可能读到新记录。作者把它定性为「safe failure, not backward compatibility」是准确的，但这也是**唯一**的现实触发面，值得在注释里点明（现注释在 `task-journal.ts:31-34` 只提「A journal from before #707」，未说「回滚/旧 bundle」）。

### 5. 反例 1 是否咬住 —— 咬住，但作者给的数字是**复合**变异的结果

在 `src/file-workflow` 子集（264 例）上：

| 变异 | 结果 | 红因归属 |
| --- | --- | --- |
| `rejectionDisposition` 恒 `terminal` + `spent` 恒 `true`（复合） | **17 failed | 247 passed**（2 files） | 与作者数字**精确一致** |
| 只把 `rejectionDisposition` 恒 `terminal` | 13 failed | 251 passed | 全是 unknown/保留意图批 |
| 只把 `spent` 恒 `true` | 4 failed | 260 passed | 上传拒绝纠正批 + cancel 409 批 |
| `409 execution_cleanup_pending`（create）改判 terminal | 1 failed | 263 passed | 该码专用用例 |
| 删掉 `save()` 的 un-settle 守卫 | 1 failed | 263 passed | journal 用例 |
| `#persist` 丢掉 `settled`（M-F） | 0 failed | 264 passed | 无测试咬（见第 3 条） |

17 例逐条看：3 例在 `FileTaskHost.test.tsx`（`413 asset_too_large` / `422 asset_empty` / 首次拒绝证据被后续接受清除），14 例在 `file-task-rejection.test.ts`（`503 job_outcome_unknown` ×3、未列 5xx、`*unknown` 4xx、首次 429、unknown 后再遇 429、`503 job_unavailable`/`job_admission_contended`/`asset_inspection_busy`、create 409 cleanup、cancel 409 cleanup、recovery 保留、operation 身份采纳）。**全部落在「未知态/保留意图」与「上传拒绝纠正」两批**，其余 11 个测试文件全绿——无连带失败，红因不是环境或无关面板。

结论：反例成立且覆盖到位；但「17/247」不能当成「单点改动的覆盖面」，PR 正文应按上表更正（必须修清单第 3 条）。

### 6. 「放弃必须显式」—— 通过

- 全仓库 `web/workbench/src/file-workflow` 内只有一处清除记录：`FileTaskHost.tsx:62` 的 `storage.removeItem(key)`，位于 `clearCompleted()`；它只在 `onNew`（`:164`）被调用，而 `onNew` 只由 `FileTaskPanel` 的确认按钮在 `finishing` 内触发（`canFinishTask(state)` 且两步：先「准备新任务」再确认）。没有任何 effect、定时器、错误分支、`dispose`、`restore` 会删记录；`initialize()` 也不删。
- `clearCompleted()` 自身要求 `canFinishTask(session.getSnapshot())`，否则抛 `Task is not confirmed complete`；`storage.getItem(key) !== expected` 时抛 `Task owner changed`（防多标签页抢写）。
- 「保留本页任务」只做 `setPreparingNew(false)`；P13 实测点击后存储仍含 `settled:"terminal"`、`create` 零调用。
- `canFinishTask` 仍是 `!busy && !unknown && (…)`，`unknown` 为 true 时恒 false —— 崩溃窗口不会顺带打开放弃出口（P1/P2/P3 均断言 `canFinishTask=false`）。

### 7. 有没有新绿 —— **发现一条真降级**：重放遇到非准入 4xx 会把真未知持久标成 `rejected`

`rejected:true` 只有两个来源：恢复时 `record.settled === "terminal"`、以及终态分支的 `spent`。持久层只有后者。因此只要追问「终态分支能否在一个『请求可能已发出』的记录上跑」，就能定位缺口——能：

```text
attempt0  create → 503 job_outcome_unknown
          页内：unknown=true, canReplay=true
          notice「提交结果未知，原提交可能已执行。请读取原执行，或按原幂等键重放；不要新建任务。」
          存储：{…,"pending":"create","operationId":null}            ← 无 settled
用户点「按原键重放」
attempt1  create → 401 invalid_credentials（任何在原键重放检查之前返回的 4xx 同理）
          spent = create && !job && !jobId && !operationId = true
          → #persist({settled:"terminal"})
          存储：{…,"pending":"create","operationId":null,"settled":"terminal"}
          页内：unknown=false, rejected=true, recovery=undefined
```

head 与基线的对照（同一序列，原始输出见文末）：

| | head `317c957d` | base `7147f306` |
| --- | --- | --- |
| 重放遇 401 后的页内 | `unknown=false`（pre-#707 已如此） | `unknown=false`（同） |
| **重载后** | `unknown=false, rejected=true, canFinishTask=true`，存储含 `settled:"terminal"` | `unknown=true, canReplay=true, canFinishTask=false` |

即：pre-#707 只是「页内清空、刷新能自愈」；#707 把这个页内判断**持久化**了，于是合同 `speech-openapi.yaml:840-843`（「After 503 job_outcome_unknown, preserve the original key and entire request, replay or query the original Job; never switch the key/version to recover」）要求保留的对账线索被永久丢弃：页面宣称「原提交已被协议确定性拒绝，原意图作废：不能重放，刷新也不会再变回「结果未知」」，并引导用户放弃 → 主机用**新**幂等键开新意图 → 若 attempt0 其实已提交，就产生第二次执行。

可达性（必须诚实标注）：

- **当前后端代码路径上不可达**：`_create`（`jobs.py:442-490`）在重放检查之前能返回的 4xx 只有 `invalid_job_request`（畸形 `Idempotency-Key` / 超限或畸形 body）与 `unsupported_parameter`（body 不符合 `JobCreateRequest`），本客户端这两者都生成不出来（UUID key + 生成式合法 body）；`workspace_*` / `model_*` / `job_binding_*` / `idempotency_conflict` 都发生在 `service.submit` 内、契约要求「原键重放先于可变发布/工作区事实」检查，属**确定性意图拒绝**，据此 settle 是安全的。
- **但契约面可达**：create 路由声明的 `401`（`Unauthorized`：'Shared service credential required or invalid'）、`403`、`404`、`405` 都可以由部署层/网关以合法 `ErrorResponse` 信封发出，且都**在**原键重放检查之前；一旦共享服务凭据轮换/失效，用户在未知态点一次重放就会踩中。这也意味着「换一个前后端版本组合」或「加一层鉴权网关」都会把该路径变成活路径。

判定：第 7 条**不通过**。修法很小（见必须修清单第 1 条），且不会削弱 #707 的正向修复（真正的 #707 场景是「首次 create 得到 422」，那类码都在重放检查之后，仍可 settle）。

### 8. 边界 —— 通过

- 改动正好 9 个文件、`+435/−24`，全部位于 `web/workbench/src/file-workflow/`：`task-journal.ts`、`file-task-session.ts`、`file-task-policy.ts`、`FileTaskPanel.tsx` 与 5 个测试文件。没有动别的面板（`voices/`、`manuscripts/`、`deletions/`、`meetings/` 等零改动），没有动生成代码/契约/后端。
- `uv.lock` 未被本提交触碰（`git diff --name-only 7147f306 317c957d | grep uv.lock` 为空）；工作区 `git status` 干净。
- `pnpm-lock.yaml`、`package.json`、tsconfig 均未改，无新依赖。

## 核心判断：「新增持久化字段」这个决定是否被安全地限定

**格式面：是，限定得比一般做法更紧。** 逐项列出：

- 单字段、单值（`settled?: "terminal"`），没有布尔化或复用 `pending`，语义注释明确写了「`pending` keeps its original 'a request may be in flight' meaning and is never repurposed」；
- 合法组合被收成唯一形状（`pending:"create" && jobId:null && operationId:null && cursor:null`），并且**其它键数严格相等**（`Object.keys(row).length !== expected.length + Number(hasModelKey) + Number(hasSettled)`），多一个字段都判 invalid；
- `save()` 禁止 un-settle（`before.settled === "terminal" && record.settled !== "terminal"` → `changed`），且这是**唯一**语义上不可逆的字段；
- 旧 reader 读新记录是硬失败（key 数不符 → invalid），Host 保留记录、不发请求、不重建会话；
- 没有自动清除路径，放弃是显式两步；
- 读取侧只把 `settled` 映射成 `rejected`，并同时关掉 `canReplay`、`continueAfterValidation`、`#submit` 三个入口，不会一边「已作废」一边还能重放。

**写入面：基本收紧，但漏了「时间维度」。** `spent` 的四个条件是「当前状态」条件（是不是 create、有没有 job/jobId/operationId），而不是「这次尝试的时间序」条件（这条记录在此前是否可能已经发出过）。#707 的整个安全性论证建立在「crash window 没有 settled」上，但 crash window 的盘上状态恰好是 `pending:"create"` + 无 `settled`——和「已观测到未知结果」的记录**同形**。前者代表「可能已发出」，后者代表「可能已执行」，而对后者再叠一次非准入 4xx 就写入了 `settled`。所以字段本身没有被误用，是**写入谓词少考虑了一个状态维度**。这也是我认为它必须修、但不能因此否定方案 A 的原因：方案 A 的读侧与格式侧都做对了，补的是写侧的一个条件（外加一行 operation_id 守卫）。

## 必须修清单

1. **收紧 `settled` 的写入条件：不得在「该记录此前可能已发出」的情形上、仅凭一次非确定性 4xx 就 settle。** 现 `spent` 只看状态不看时间序。最小可选实现（任一）：
   - 把「可 settle 的拒绝」限定为**契约保证在原键重放检查之后返回**的确定性意图拒绝（`422 job_binding_*` / `job_text_asset_unsupported` / `voice_not_published` / `model_binding_*`，`409 idempotency_conflict`），其余 4xx（尤其 401/403/400/404/405）一律保持 `unknown`，让用户可以带着原键重放或读取；
   - 或要求「本次 create 是本站点为该记录发出的首次尝试」（记录在本次 `#submit` 内新建，而非从存储恢复），恢复出来的 pending 记录永不 settle。
   并补回归：「`503 job_outcome_unknown` → 按原键重放 → `401`/`403`/`400` → 仍 `unknown:true`、`canReplay:true`、存储无 `settled`」，以免以后又被压平。
2. **`spent` 同时排除当前信封已带执行身份的情形**：`&& !contractError.detail.error.nexus?.operation_id`。否则终态分支一边写 `settled` 一边把 `recovery` 置空，与它自己的注释「An identified original execution stays queryable, so it is never settled」相矛盾（今天契约不可达，但注释已作承诺，成本一行）。
3. **更正 PR 正文/注释里「`rejectionDisposition` 恒 terminal + `spent=true` → 17 failed / 247 passed」的表述**：那是复合变异；单一变异分别是 13 failed/251 passed 与 4 failed/260 passed。数字本身可复现，但当前写法会让人误读成「单点改动的覆盖面」。

非阻塞建议（可后续）：

- `#persist` 对 `settled` 的「展开携带」没有任何测试咬住（M-F 变异下 264 例全绿），因为今天不存在 settle 之后的写入路径。若后续新增写入点，建议至少留一条「settled 不可被任意 `#persist` 丢弃」的断言（或把该不变量写进 `#persist` 注释）。
- 恢复出来的 settled 页面点「读取原状态」不会发任何请求（记录里已无未确认身份，`refresh()` 走空）；面板素材/任务字段显示「尚未确认」，可在 UI 文案里说明「本页无未确认身份可读」，避免用户以为按钮坏了。
- `409 idempotency_conflict` settle 后，该 key 下「另一个意图的 Job」没有任何句柄可对账（记录里不存 jobId/operationId）。pre-#707 也没有句柄，故未丢失信息；但文案可补一句「该键下若另有任务，需按任务标识另行核对」。
- terminal 分支注释可把「该字段不证明该 key 没有 Job 行」这句话从分类函数挪到写入点旁边（现在只在 `rejectionDisposition` 里出现过，读者到写入点容易误解 `settled` 的含义）。

## 独立复现的反例（原始输出）

### 第 5 条：复合变异的 17/247（临时副本 `/private/tmp/sn709-mut`）

```text
$ cd /private/tmp/sn709-mut/web/workbench && pnpm vitest run src/file-workflow      # classifier 恒 terminal + spent 恒 true
 FAIL  src/file-workflow/FileTaskHost.test.tsx > production Workbench file host > clears first-rejection evidence when the original input is later explicitly accepted
 FAIL  src/file-workflow/FileTaskHost.test.tsx > production Workbench file host > allows explicit correction after first 413 asset_too_large rejection
 FAIL  src/file-workflow/FileTaskHost.test.tsx > production Workbench file host > allows explicit correction after first 422 asset_empty rejection
 FAIL  src/file-workflow/file-task-rejection.test.ts > … > keeps one unknown outcome and its recovery identity for 503 job_outcome_unknown
 FAIL  … > replays the original key after 503 job_outcome_unknown instead of starting over
 FAIL  … > treats an unlisted 5xx create failure as an unknown outcome, never a spent intent
 FAIL  … > never spends a 4xx intent whose code marks the outcome unknown
 FAIL  … > queries an identified original execution instead of replaying it after 503 job_outcome_unknown
 FAIL  … > keeps the original intent retryable for a first 429 job_capacity_exhausted
 FAIL  … > never clears an earlier unknown outcome when a replay is refused by 429 job_capacity_exhausted
 FAIL  … > retains the intent and the unknown state for 503 job_unavailable
 FAIL  … > retains the intent and the unknown state for 503 job_admission_contended
 FAIL  … > retains the intent and the unknown state for 503 asset_inspection_busy
 FAIL  … > keeps the intent unknown when a create attempt is refused with 409 execution_cleanup_pending
 FAIL  … > treats 409 execution_cleanup_pending on the cancel path as a settled attempt
 FAIL  … > keeps an earlier recovery envelope when a later 503 carries no operation identity
 FAIL  … > adopts a later operation identity so it stays queryable
 Test Files  2 failed | 11 passed (13)
      Tests  17 failed | 247 passed (264)
```

拆开后（同一副本，逐次复原）：

```text
classifier 恒 terminal：        Tests  13 failed | 251 passed (264)
spent 恒 true：                Tests   4 failed | 260 passed (264)
409 execution_cleanup_pending
  create 路径改判 terminal：    Tests   1 failed | 263 passed (264)
删 save() un-settle 守卫：      Tests   1 failed | 263 passed (264)
#persist 丢掉 settled：         Tests   0 failed | 264 passed (264)
```

### 第 7 条的降级（head 探针，临时副本）

```text
EVIDENCE attempt0 unknown: "提交结果未知，原提交可能已执行。请读取原执行，或按原幂等键重放；不要新建任务。"
EVIDENCE stored after attempt0: {"model":"asr-quality","assetId":"asset-demo","workspaceId":"ws-default","createKey":"fa4d08d8-…","cancelKey":"9b096fa7-…","jobId":null,"pending":"create","operationId":null,"cursor":null,"version":1}
EVIDENCE stored after replay:  {…,"createKey":"fa4d08d8-…",…,"pending":"create","operationId":null,"cursor":null,"version":1,"settled":"terminal"}
EVIDENCE notice after replay:  "Speech response 401: invalid_credentials"
```

同一序列在基线 `7147f306`（`/private/tmp/sn709-base`）：

```text
BASE in-page after 401 replay: {"unknown":false,"notice":"Speech response 401: invalid_credentials"}
BASE stored after 401 replay:  {"model":"asr-quality",…,"pending":"create","operationId":null,"cursor":null,"version":1}
BASE after reload:             {"unknown":true,"canReplay":true,"canFinishTask":false}
```

### 我的独立探针（`/private/tmp/sn709-probe`，13 例全绿）

```text
 ✓ src/file-workflow/zz-review-probes.test.ts (11 tests)
   ✓ P1 lost create response (TypeError) leaves pending without settled
   ✓ P2 dispose between the arrival of a terminal answer and the persist still leaves unknown
   ✓ P3 a storage failure while persisting settled degrades to unknown without a false claim
   ✓ P4 a 4xx that is not a contract envelope never settles the intent
   ✓ P5 409 execution_cleanup_pending on the create path keeps the intent unknown
   ✓ P6 409 idempotency_conflict settles, but the replacement uses a different key
   ✓ P7 REVIEW FINDING: a non-admission 4xx on a replay durably settles a previously unknown intent
   ✓ P8 an old reader rejects a settled record as invalid and the new reader still takes old records
   ✓ P9 a settled record cannot be rewritten into cursor/operationId shapes that drop it
   ✓ P10 restoring a settled record neither submits, replays, nor clears anything
   ✓ P11 unknown states keep canFinishTask false, so no abandonment path opens without a refusal
 ✓ src/file-workflow/zz-review-host-probe.test.tsx (2 tests)
   ✓ P12 a settled record an older reader would reject fails closed: no requests, record kept
   ✓ P13 a genuine settled record renders the settled page and offers explicit abandonment only
```

## 新红

- 无（786 全绿）。第 1 条 ③（`#persist` 失败）会让该页退回 pre-#707 的死锁（`canFinishTask=false`），这是**有意**的安全方向（不产生假声明），不是缺陷；但它说明「本地存储写入失败」时 #707 的出口不可用，与本 PR 目标一致（宁可不放行）。

## 只能依赖作者陈述的点

- **「17 failed / 247 passed」的原始变异定义**：PR 正文没有给出变异细节。我用「classifier 恒 terminal + spent 恒 true」精确复现到同一组数字，但不能断定作者用的就是这一对；只能说单一变异是 13 与 4，因此该数字必然是复合结果（第 5 条已给出三个拆分数）。
- **vitest 与 dsh 自带 node 24 的 `Cannot find native binding` 冲突**：我全程使用系统 node v26.12.1，未去复现 dsh node 下的失败形态；本报告的所有例数与作者口径可直接比较。
- **作者对「`settled` 只在 terminal 分支写入」的自述**：我不依赖它，已用探针在 4 个窗口独立验证。

## 时间线（本地时间，2026-11-14）

- 06:05 `git fetch`；建 `/private/tmp/sn709`（`review/709-terminal-exit` @ `317c957d`）与 `/private/tmp/sn709-base`（@ `7147f306`）；确认主检出停在 `docs/tracker-batch12`。
- 06:06 `pnpm install --frozen-lockfile`；head `pnpm test` = 62 files / 786 tests。
- 06:07 `pnpm typecheck` / `pnpm lint` / `pnpm format:check` 全绿；`ruff format --check .` = 1820 files already formatted。
- 06:09 基线 `pnpm test` = 770 tests（确认 +16）；`src/file-workflow` 子集 = 264 tests（= 247 + 17）。
- 06:10 读 head 源码（`task-journal.ts` 全文、`file-task-session.ts` 全文、`FileTaskHost.tsx`、`FileTaskPanel.tsx`、`file-task-policy.ts`）与契约（create 路由响应、`Conflict`/`JobCancellationConflict`、`errors.schema.json`）。
- 06:11 建 `/private/tmp/sn709-probe`，写 11 例 node 探针；首轮 2 例因我自己的探针写错（abort 早于 `pending` 落盘、恢复态不发读请求）修正后 11 例全绿。
- 06:12 建 `/private/tmp/sn709-mut`：M-A classifier 恒 terminal（13/251）、M-B spent 恒 true（4/260）、M-C 复合（**17/247**，与作者一致）、M-D cleanup_pending 放宽（1 红）、M-E 删 un-settle 守卫（1 红）、M-F `#persist` 丢 `settled`（0 红）。
- 06:13 补 2 例 Host 级探针（P12/P13）全绿；`zz-review-probes` 11 例复跑全绿。
- 06:14 基线探针证明 pre-#707「刷新回到 unknown」；head 探针证明同序列被持久写成 `settled`（第 7 条）。
- 06:15 复核改动边界（9 文件、无 `uv.lock`、无其它面板）、清除路径（唯一 `removeItem` 在 `clearCompleted`）。
- 06:16+ 在 `/private/tmp/sn709-doc`（基于 `origin/main` = `7147f306`）撰写本报告，`ruff format` / `--check` 复跑全绿后提交。
