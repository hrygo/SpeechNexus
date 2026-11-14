# 取消路径终态结算的非作者审查（PR #724 / issue #723）

> 过程记录文档：保留时间线、独立复现的反例与命令输出。结论面向合并决定，不修改产品代码/测试。
> 审查对象：`fix/723-cancel-settled`，head `0d9e5b61c33805faa82b5a7551f07701c4f87855`（下称 head），分支基点 `cef394336929e4f520d11ebe962034ffc7dd02e2`（= PR 分支时的 main）。
> 本报告分支 `review/729-cancel-settled` **基于 `origin/main`** 创建（开始时 `404bfecdd6f62a119ddb415eab2f7fdb5d4ea3b1`，写完前 main 前进到 `405a725a4b341e3874bb09b18d795cb77ac19114`，已 rebase 到新 main），`git diff origin/main --stat` 只含本文件。被审内容一律用 `git show 0d9e5b61:<path>` 或 detached 快照 worktree `/private/tmp/sn729-pr` 读取，**没有**从被审 head 拉分支。
> main 在审查期间新增的两笔（`404bfecd`、`405a725a`）都只加 `docs/reviews/` 下的审查记录，`git diff --name-only cef39433 405a725a` 仅这两个文档，**没有触及 `web/workbench/` 或任何测试**，因此下文所有 head 例数与门禁结果保持有效；基线 817 例在 `404bfecd` 实测，受两笔 docs 影响为零。
> 实际审查发生在 2026-11-14 10:15–10:32（本地时间，Asia/Shanghai）。

## 结论

**通过，无功能阻塞**。`#723` 的缺陷修对了，`#707`/`#691` 的未知保护一条都没有放宽，`canFinishTask` 的放宽经实测不越界，被改的那条既有测试**没有丢掉 `#691` 的判据覆盖**。合并前只有 **1 条论据必须修**（注释层，不需要改行为）：`404 job_not_found` 的证明引用了一条与结论相反、且仅仅依赖实现事实才成立的契约句子。另有 3 条建议（P2 精度、P3 测试、P4 文案，均不阻塞）。

判定依据（全部独立实跑，见后文各节）：

- **A1/A2**：那条测试的判据来自 `#691`（提交 `29c13c7e`），而本 PR 删掉的两行断言是 `#707`（提交 `80e67675`）**追加**的范围边界，注释原文即 `#707 scope`。改后 `#691` 的五条断言一条不少，且**仍然咬得住**：把「cancel 路径 `409 execution_cleanup_pending` 归 terminal」改坏，被改的测试自身在 `file-task-rejection.test.ts:594` 红。
- **B3/B9**：反例 2 复现——写入侧去掉可证明门 → **6 红**（与作者 6 条名单逐条一致）；记录形状侧去掉 `operationId` 门与形状门 → **4 红**（与作者 4 条一致）。反例 1 → **5 红**（名单与作者完全一致）。反例 3 → **12 红**（我的变异比作者口径更宽，见 D9）。
- **C7**：8 个可达状态里，新旧 `canFinishTask` 只在「已结算的 cancel」上分歧（新 true / 旧 false）；四种「unknown + 带 Job」状态（调用方 401、响应丢失、`503 job_outcome_unknown`、崩溃窗口重载）在新旧谓词下**都是 false**。
- **B4**：三个码都确实「关于目标 Job」，`jobId` 与 404 所针对的 Job 结构上必然同一；但 `404 job_not_found` 的**论据引错了**——PR 引用 `contracts/speech-openapi.yaml:990` 说「Job 不存在」，而该行原文是「Unknown Jobs **and Jobs outside the caller's workspace** are 404」。今天安全的真实原因是实现层面 cancel 查询**不带 workspace 过滤**、且 `speech_jobs` 行永不删除；契约文字一旦被实现，这条结算就会变成误判。属必须修的论据（非今天的可达缺陷）。
- 门禁：head `pnpm test` **846 passed / 62 files**、main 实测基线 **817 / 62**（+29）；`pnpm typecheck`、`pnpm lint`、`pnpm format:check`、`uv run ruff format --check .`（1830 files）全绿；CI 路由脚本对 PR 精确 diff 实算得 `{"mode": "web"}`。

## 审查对象与方法

- 审查分支（本报告所在）：`git worktree add /private/tmp/sn729 -b review/729-cancel-settled origin/main`，落点 `404bfecd`。先确认 `git diff origin/main --stat` 为空再动笔。
- 被审快照（只读、detached、不建分支）：`git worktree add --detach /private/tmp/sn729-pr 0d9e5b61`。所有测试、变异、探针都在该快照内做，**未 push 任何一个变异**，结束后 `git checkout -- .` 复原并删除探针文件。
- 复现环境：系统 node **v26.12.1**（`~/.local/share/mise/shims/node`）、pnpm **12.14.0**；`pnpm install --frozen-lockfile` 在两个 worktree 各跑一次，复用 store，未改动 `pnpm-lock.yaml` / `uv.lock`。
- `uv run ruff format --check .` 与 `uv run --no-project ruff format` 均用仓库 `.venv`（`uv` 就地创建，未提交）。
- 后端/契约核对：`contracts/speech-openapi.yaml`（cancel 路由 400/401/404/405/409/503 与 975–996 行描述）、`backend/.../interfaces/http/jobs.py`（`_cancel` :600–617、路由 :794–804）、`backend/.../application/public_jobs.py`（服务层 `cancel` :1274–1285）、`backend/.../infrastructure/public_jobs_postgres.py`（`_lock_cancel_target` :1014–1031、`cancel` :741–768）。

## 核心：那条被改的既有测试原来守什么、改后有没有丢覆盖

被测测试：`web/workbench/src/file-workflow/file-task-rejection.test.ts` → `treats 409 execution_cleanup_pending on the cancel path as a settled attempt`。

### A1 它原本守的是 `#707` 的范围边界，不是 `#691` 的判据

证据链（`git log -S` 逐版回溯）：

1. **`#691` 引入**（`29c13c7e`，提交信息 `fix: 按状态码/错误码/操作上下文分类拒绝，保留原执行身份 (#691)`）。当时的测试体只有五条断言，没有任何 `rejected`／`settled` 断言：

   ```ts
   it("treats 409 execution_cleanup_pending on the cancel path as a settled attempt", async () => {
     // The cancel request was deterministically refused against a known Job identity, so the
     // same operation context that keeps a create unknown must clear it here.
     ...
     expect(snapshot.unknown).toBe(false);
     expect(snapshot.canReplay).toBe(false);
     expect(snapshot.recovery).toBeUndefined();
     expect(snapshot.notice).toContain("execution_cleanup_pending");
     expect(snapshot.cancellation).toBeUndefined();
   });
   ```

   这五条就是 `#691` 的判据本体：cancel 路径的 `409 execution_cleanup_pending` 必须归 terminal（未知态被清、不可重放、不保留恢复信封、错误码可见、不产生取消回执）。

2. **`#707` 追加两行**（`80e67675`），并在注释里写明这是**范围边界**：

   ```ts
   // A cancel attempt against a known Job is not a settled create intent (#707 scope).
   expect(snapshot.rejected).toBeUndefined();
   expect(f.stored()).not.toHaveProperty("settled");
   ```

3. `#707` 自己的非作者审查报告也把它记成边界而非判据：`docs/reviews/2026-11-13-terminal-exit.md:79` —— 「cancel 路径 `spent` 因 `operation === "create"` 为假而**不会**写 `settled`，取消意图的持久状态未被本 PR 改变」。

**结论**：这两行守的是 `#707`「cancel 不结算」的**范围边界**，与 `#691` 的 terminal 判据是两个不同的东西。本 PR 改这两行正是本卡目的，`#691` 的成果没有被改掉。

### A2 改后没有丢掉 `#691` 的判据覆盖——并做了正面验证

head 上该测试仍保留 `#691` 的全部五条断言（`file-task-rejection.test.ts:594–598`），随后才换成 `#723` 的新断言（`:601–605`，`rejected:true` + 存储 `pending:"cancel" / settled:"terminal" / jobId:"job-demo"`）。也就是说它同时守两件事，`#691` 的五条一条没删。

不止「仍在」，而且**仍然咬得住**。变异 A2：把 cancel 路径的 `execution_cleanup_pending` 并入 `unknown-outcome`（删掉 `rejectionDisposition` 里的 `operation !== "cancel"` 守卫，即直接改坏 `#691` 的判据）：

```text
$ npx vitest run src/file-workflow/file-task-rejection.test.ts -t "treats 409 execution_cleanup_pending on the cancel path"
 FAIL  ... > treats 409 execution_cleanup_pending on the cancel path as a settled attempt
AssertionError: expected true to be false // Object.is equality
 ❯ src/file-workflow/file-task-rejection.test.ts:594:30      ← expect(snapshot.unknown).toBe(false)
 Tests  3 failed | 69 passed (72)
```

失败的**第一条断言就是 `#691` 的**（`unknown`，`canReplay` 同批），不是新加的 `#723` 断言。所以：改后的测试对 `#691` 判据的覆盖**没有丢**，而且比改前更强（多了「被持久结算」的断言）。

**判定：A1 通过（原守 `#707` 边界）、A2 通过（`#691` 覆盖保留且实测有效）。**

## 十一条逐条判定

| # | 项 | 判定 |
| --- | --- | --- |
| A1 | 被改测试原本守什么 | **通过**：原守 `#707` 范围边界（`80e67675` 注释即 `#707 scope`），非 `#691` 判据 |
| A2 | 是否丢失 `#691` 判据覆盖 | **通过**：五条断言保留，变异证明仍在 `:594` 咬住 |
| B3 | 反例 2（settled 放宽到任何 pending 必须红） | **通过（附精度注）**：写入侧 6 红、形状侧 4 红均复现；但「纯 shape 放宽」与基线 schema 逻辑等价（0 红），作者的 4 红是复合变异 |
| B4 | 三个码是否真「关于目标 Job」 | **必须修（论据层）**：三码成立、`jobId` 同一性成立；`404 job_not_found` 的契约引用与此行原文相反，真实依据是实现事实 |
| B5 | `outcome_unknown` 与 `execution_cleanup_pending` 是否被合并 | **通过**：两处独立规则，终态分支对 `unknown-outcome` 不可达，变异可见 3 红 |
| B6 | 崩溃窗口必须仍 unknown、无 `settled` | **通过**：`pending:"cancel"` 先落盘、丢失分支不写 `settled`；用例 + 探针 + 变异三向验证 |
| C7 | `canFinishTask` 去掉 `!state.job` 是否安全 | **通过**：8 状态矩阵，only-divergence 是已结算 cancel；未知态全 false |
| C8 | `cancel()` 对 `rejected` 早退是否永久禁止重试 | **通过**：显式放弃路径 `FileTaskHost.clearCompleted()` 存在且实测可用 |
| D9 | 反例 1（5 红）/ 反例 3（崩溃窗口写 `settled`） | **通过**：反例 1 精确 5 红；反例 3 我 12 红（作者 10，变异口径更宽） |
| D10 | 846 例 / 62 文件、新增用例与声明对应 | **通过**：基线 817 → 846，+29 逐文件可分解且与声明一一对应 |
| E11 | 范围、`uv.lock`、`cursor` 缺口 | **通过（附建议）**：仅 8 个文件在 `web/workbench/src/file-workflow/`，`uv.lock` 不在提交内；`cursor` 缺口实测**行为正确**，缺的是回归用例 |

### B3 反例 2（最重要的一条）——独立复现

两组分别复现，且我额外量出「纯形状放宽」的等价性（这是本卡唯一需要补的精度说明）。

**写入侧（去掉可证明门）**：把 `settlesCreate` 改成 `operation === "create"`、`settlesCancel` 改成 `operation === "cancel"`（即任何 terminal 4xx 都结算）：

```text
 Tests  6 failed | 840 passed (846)
 FAIL … keeps an earlier unknown outcome unknown when a replay is refused before the key check
 FAIL … keeps a reloaded unknown outcome unknown when its replay is refused before the key check
 FAIL … never settles a refusal that names an identified execution
 FAIL … keeps a body-level refusal unknown on a first attempt
 FAIL … keeps a cancel refusal about the caller and envelope unknown
 FAIL … never settles a cancel refusal that names an identified execution
```

6 条名单与作者声明逐条对应（create 401-after-lost / 重载未知 / body-level 422 / 两处 identified-execution / cancel 401 调用方拒绝）。**反例 2 写入侧精确复现。**

**记录形状侧**：作者声明「journal 允许任意 pending 自称 settled → 4 红」。拆开量：

| 变异 | 结果 |
| --- | --- |
| 仅去掉 `!shape`（形状门），保留 `settled !== "terminal"` 与 `operationId !== null` | 2 红 |
| 仅去掉 `operationId !== null`（identified-execution 门），保留形状门 | 2 红 |
| 两者同时去掉（= 作者口径的「4 种形状被放行」） | **4 红** |
| 纯「任意 pending」：`const shape = row.pending !== null` | **0 红** |

「0 红」不是漏测，而是**逻辑等价**：在 `validRecord` 既有的基础不变式下，`shape` 的 create/cancel 两个分支本来恒为真——`pending:"create" ⇒ jobId===null`（:194）且 `jobId===null ⇒ cursor===null`（:196），`pending:"cancel" ⇒ jobId!==null`（:195）。因此 `shape` 仅有的判别力就是「`pending` 不为 null」，也就是排除「已确认 Job / 无 pending」自称 settled 的那种形状。这一条不影响正确性（没有任何未知被一起结算），但它说明 PR 文案「收敛为两种精确形状」在行为上等价于「`settled` ⇒ `pending !== null`」，作者给的 4 红是**形状门与 identified-execution 门的复合变异**，不是纯 shape 放宽。

**判定**：反例 2 成立（放宽必红），附精度注（建议改注释口径，见建议清单）。

### B4 三个码是否真「关于目标 Job」

**`409 execution_cleanup_pending`** ✅ 关于 Job 且在 key 检查之后：后端只在 cancel/retry 处理器发出（`jobs.py:606-607`），`PostgresPublicJobs.cancel` 在锁住 Job 行（`public_jobs_postgres.py:751`）并解析 workspace 取消键（`:758-765`）之后才返回 `cleanup_pending`，语义是「Job 已终态、执行未确认停止」。

**`409 idempotency_conflict`** ✅ key 检查本身给出的答案（同一处 `:758-765`）。附注：本客户端的 `cancelKey` 是每记录一个 UUID、`request_digest` 绑定 `job_id`（`public_jobs.py:1287`），同记录同 Job 不可能冲突，因此该码在当前客户端**实际不可达**；结算它无害。

**`404 job_not_found`** ⚠️ **论据必须修**。PR 注释写「the protocol says the Job this record names does not exist (contracts/speech-openapi.yaml:990)」。但该行原文（`contracts/speech-openapi.yaml:989-990`）是：

```text
        reusing that key for another Job is 409 `idempotency_conflict`. Unknown
        Jobs and Jobs outside the caller's workspace are 404.
```

即契约明确承认 404 **可以表示「Job 存在、但在调用方 workspace 之外」**——那正是「Job 不存在」的反面。今天之所以仍安全，靠的是实现事实、而不是这行契约：

- 路由 `cancel_job`（`jobs.py:794-804`）调用 `_cancel(service, job_id, request_id, key)`，**不传 workspace**；
- 服务层 `cancel(..., workspace_id: str | None = None)`（`public_jobs.py:1274-1285`）默认 `None`；
- `_lock_cancel_target(connection, job_id, workspace_id)`（`public_jobs_postgres.py:1014-1031`）仅在 `workspace_id is not None` 时追加 `AND j.workspace_id = %s` → **cancel 路径实际不带 workspace 过滤**；
- `speech_jobs` 行在 `backend/src` 内**从不删除**（`grep -rn "DELETE FROM speech_jobs"` 仅命中 tests/integration），因此「Job 行不存在」是稳定事实。

另有一处细节值得同一处注释带过：`_cancel` 把 `PublicJobNotFoundError` **和 `ValueError`** 一起映射成 `job_not_found`（`jobs.py:612-613`），即「id 格式非法」也是 404——对已结算判定无害（这个 id 同样作废），但论据里混着两种含义。

**记录里的 `jobId` 与 404 所针对的 Job 是否一定同一个**：是，结构保证，实测复核：

- `cancel()` 用 `this.#view.job?.id` 发起取消（`file-task-session.ts:983`）；
- `view.job` 只能经 `#acceptJob` 写入，而 `#checkJob` 要求 `record.jobId === null || job.id === record.jobId`（`:376-383`）；
- `record.jobId` 只在提交被接受时写成服务端返回的 `job.id`（`:850`），且 `save()` 在 settled 后禁止改指向（`task-journal.ts:303-308`）。

探针实测（`/private/tmp/sn729-pr` 内临时探针，已删除）：

```text
PROBE-B4-identity
 { "cancelPaths": ["/v1/nexus/jobs/job-demo/cancellations"],
   "recordJobId": "job-demo" }

PROBE-B4-mismatch        # 恢复时服务端返回 id=job-other 的 Job
 { "recordedJobId": "job-demo", "restoredJob": undefined,
   "cancellations": 0, "stored": { … "jobId": "job-demo", "pending": null, 无 settled } }
```

即：请求路径里的 id 恒等于记录里的 `jobId`；一旦服务端回读的 Job 身份不同，`#checkJob` 直接拒绝，页面根本没有可取消的目标，404 不可能被安到别的 Job 上。

**一个必须记下的行为**（探针 `PROBE-B4-after-lost`）：先丢失一次 cancel 响应（unknown、无 `settled`），随后「按原键重放」得到 `404 job_not_found` → **会结算**（`unknown:false, rejected:true, settled:"terminal"`，共 2 次 POST）。这正是 `#707` M1 为 create 排除的时间序类别（`settlesReplayedIntent` 明确不收 404）。它安全的前提就是上面两条实现事实（cancel 查询不带 workspace + Job 行不删）：Job 若在，重放会命中 Job 而不是 404，所以 404 一定是「Job 真的不存在」。但这层推理 PR 注释没有写出来，只写了「记录还带着可读 Job」。→ 并入同一条必须修。

### B5 `outcome_unknown` 系与 `execution_cleanup_pending` 的区分

**没有被合并**，三处独立：

1. `rejectionTable`（`file-task-session.ts:60-66`）把 `503 job_outcome_unknown` 定为 `unknown-outcome`；
2. `rejectionDisposition` 的 `code.endsWith("unknown")` 规则（`:91-95`）覆盖 4xx 的 `*_outcome_unknown`；
3. `409 execution_cleanup_pending` 单独一条：**只有** `operation !== "cancel"` 时才归 `unknown-outcome`（`:103-104`），cancel 路径落到 4xx→`terminal`。

而 `settlesCancelAttempt`（`:185-194`）只在 `terminal` 分支里被调用（`:621-628`），且白名单不含任何 `*unknown` 码；`unknown-outcome` 分支在更早的 `:542` 已被接走，物理上到不了。变异 A2（把 cancel 的 `cleanup_pending` 并进 unknown）→ 3 红，其中包含被改的既有测试（见 A2）。用例侧另有 `it.each` 矩阵行 `[503,"job_outcome_unknown",false]`、`[409,"voice_draft_outcome_unknown",false]`、`[409,"operation_in_progress",false]`、`[409,"response_not_replayable",false]`，以及 e2e `never settles an unknown-outcome cancel`（503/409 unknown + 500）。

### B6 崩溃窗口

`#cancelIntent` 在发请求**之前**落 `pending:"cancel"`（`file-task-session.ts:993`），失败分支 `mutation && !(error instanceof SpeechResponseError)` 只置 unknown、不写 `settled`。用例（rejection + persistence 各一条 `keeps a lost cancellation response unknown and never records a settled refusal`）与探针 C/G 行均证实。变异 B9d（在 `#cancelIntent` 发请求前就写 `settled`）→ 12 红，包含两条 lost-response 用例与 `keeps a cancel intent that was refused before it acted…`。

### C7 `canFinishTask` 去掉 `!state.job` 是否安全（构造「unknown + 带 job」实测）

谓词改动（`file-task-policy.ts:12-17`）：`(state.rejected === true && !state.job)` → `(state.rejected === true)`。

关键事实：`canFinishTask` 首两个合取仍是 `!state.busy && !state.unknown`，所以问题等价于「是否存在 `rejected && unknown` 或「`rejected` 但意图未花掉」的可达状态」。探针覆盖 8 个真实可达状态，同时计算新旧谓词：

```text
scenario                                            unknown  rejected  job        newCanFinish  oldCanFinish
A accepted/running                                  false    undefined job-demo   false         false
B cancel 401 caller refusal                         true     undefined job-demo   false         false
C cancel lost answer (crash window)                 true     undefined job-demo   false         false
D cancel 503 job_outcome_unknown                     true     undefined job-demo   false         false
E cancel 409 cleanup_pending (settled)               false    true      job-demo   true          false   ← #723 唯一分歧
F create 422 terminal (settled, no Job)              false    true      undefined  true          true
G reload crash window (pending cancel, no settled)   true     undefined job-demo   false         false
H reload settled cancel (404 job_not_found)          false    true      job-demo   true          false   ← #723 唯一分歧
```

探针断言：四个 `unknown` 行新旧皆 `false`；**唯一分歧**就是 E/H 两个「已结算取消」，而它们 `rejected:true`、Job 可读、意图确实已花掉。结构上也封死：`rejected` 只在两个地方被置 true——构造时 `record.settled === "terminal"`（此时 `unknown` 恒为 `false`），或终态结算分支（同时 `unknown:false`）——所以不存在 `rejected && unknown`。

补充：`canFinishTask` 第三条 `job.cleanup_state === "complete" && 终态` 本来就允许「带 Job 的可收尾」状态，`!state.job` 从来只是 rejected 分支的额外限制，而不是「带 Job 不可收尾」的总闸。**判定：安全。**

### C8 `cancel()` 对 `rejected` 早退

`cancel()`（`:982-989`）加 `|| this.#view.rejected`，面板 `cancellable` 同步排除 `rejected`（`FileTaskPanel.tsx:49`）。探针 `PROBE-C8`：结算后连续调用 3 次 `cancel()`，POST 计数恒为 1；`refresh()` 后 Job 仍可读；随后用 Host 的显式放弃路径 `clearCompleted()`（`FileTaskHost.tsx:57-62`，即面板「准备新任务 → 放弃这次意图并新建」，`:164`）删记录后新会话 `rejected` 为 undefined、可正常提交。

因此**不是**「永久禁止重试取消」——存在显式放弃路径。语义上也合理：`execution_cleanup_pending` 的 Job 已终态，重发取消无意义；`idempotency_conflict` 的键按设计已作废（且该码在本客户端不可达）。唯一可记的低风险点：清记录后 Job id 会离开 UI，若某天真出现「仍在跑却冲突」的 Job，页面就没有再取消它的入口；以当前不可达性看可接受。

### D9 反例 1 / 反例 3 独立复现

| 变异 | 我的实测 | 作者声明 | 一致性 |
| --- | --- | --- | --- |
| 反例 1：cancel 归 terminal 但不写 `settled` | 5 failed | 5 failed | **完全一致**（名单同） |
| 反例 2-写入侧：去掉可证明门 | 6 failed | 6 failed | **完全一致**（名单同） |
| 反例 2-形状侧：去 `operationId` + 形状门 | 4 failed | 4 failed | **一致**（需复合变异；拆分各 2） |
| 反例 3：`#cancelIntent` 发请求前写 `settled` | 12 failed | 10 failed | 方向一致，**口径更宽**（含 lost-response ×2、intent-kept、unknown 三行） |
| A2 附带：cancel `cleanup_pending` 并入 unknown | 3 failed | —（本卡新增验证） | 被改测试在 `:594` 红 |

反例 1 的 5 条名单与作者逐字一致：`restores a terminally refused cancel…`、`treats 409 execution_cleanup_pending on the cancel path…`、两条 `persists a cancel refusal answered after the key check as spent…`、`settles a cancel for a Job the protocol reports as absent`。

### D10 例数与新增用例命名

```text
$ cd web/workbench && pnpm test            # /private/tmp/sn729-pr (head 0d9e5b61)
 Test Files  62 passed (62)
      Tests  846 passed (846)

$ cd web/workbench && pnpm test            # /private/tmp/sn729 (origin/main 404bfecd)
 Test Files  62 passed (62)
      Tests  817 passed (817)
```

+29 可逐文件分解，与声明一一对应：`task-journal.test.ts` +4（1 个 `it` + 3 个 `it.each` 形状行）、`file-task-rejection.test.ts` +22（12 行 `settlesCancelAttempt` 矩阵 + 2 行 `persists…` + `settles a cancel for a Job the protocol reports as absent` + `keeps a cancel refusal about the caller and envelope unknown` + `keeps a lost cancellation response unknown…` + 3 行 `never settles an unknown-outcome cancel` + `keeps a cancel intent that was refused before it acted…` + `never settles a cancel refusal that names an identified execution`）、`file-task-persistence.test.ts` +2、`FileTaskHost.test.tsx` +1。矩阵 12 行恰好覆盖 `true` 的 3 个 Job 答案码与 `false` 的 400/401/403/405 + 7 个 outcome-unknown/其它码。

### E11 范围、`uv.lock`、`cursor` 缺口

- 范围：`git diff --name-status cef39433 0d9e5b61` 恰好 8 个文件，全部在 `web/workbench/src/file-workflow/`；无契约、无后端、无文档改动。
- `uv.lock`：`git show --name-only 0d9e5b61 | grep -c uv.lock` → **0**，提交内不含 `uv.lock`。
- CI 路由：用 PR 精确 diff 跑 `scripts/dev-env/ci-routing.py --event=pull_request --base=<cef39433 全 sha>` → `{"mode": "web", "reason": "Web sources changed; …"}`，与作者声明一致。
- `cursor` 缺口（作者自承「cancel 记录带非空 `cursor` 的 `settled` 只在 journal 层做过 roundtrip，会话路径未构造端到端用例」）：**该路径真实可达且行为正确，缺的只是回归用例**。可达链：结算后 `cancel()` 虽被早退，但 `reconnect()` 没有 `rejected` 守卫 → 事件水位前进 → `#acceptJob(job, { cursor })` → `#persist({ cursor })`，即「已结算记录上继续写 cursor」是运行时路径。探针实测：

  ```text
  PROBE-E11                 # 已结算 cancel + 记录 cursor "1" 重载
   { "unknown": false, "rejected": true, "cursor": "1", "job": "job-demo", "canFinish": true }

  PROBE-E11-after-reconnect # reconnect() 拿到 id=2 的事件后回写水位
   { "jobId": "job-demo", "pending": "cancel", "cursor": "2", "settled": "terminal" }
  ```

  即：带 cursor 的 settled cancel 重载后**不显示未知**、cursor 保留、Job 可读、`canFinishTask` 为真；结算后的 cursor 前进不会触发 `save()` 的 `changed`（单调性只钉 `pending`/`jobId`）。所以缺口是测试覆盖缺口，不是缺陷。**判定：不阻塞合并，建议补 1 条 e2e 回归**（见建议清单 P3）。

## 必须修清单

1. **（论据层，2–3 行注释，不阻塞行为）`404 job_not_found` 的证明引用必须换掉。**
   `file-task-session.ts:175-177` 现写：

   > `404 job_not_found`: the protocol says the Job this record names does not exist (contracts/speech-openapi.yaml:990) …

   而 `contracts/speech-openapi.yaml:990` 原文是「Unknown Jobs **and Jobs outside the caller's workspace** are 404」——这句明确允许「Job 存在但不在调用方 workspace」也返回 404，正好否掉「不存在」的推论。应改为引用真实依据并留下残余风险：
   - cancel 查询**不带 workspace 过滤**（`jobs.py:794-804` 不传 workspace；`public_jobs.py:1274-1285` 默认 `None`；`public_jobs_postgres.py:1014-1031` 仅在非 None 时拼 workspace 条件）；
   - `speech_jobs` 行永不删除，所以「行不存在」稳定；
   - 并记明：**如果后端按契约实现 workspace 作用域的 404**，这条结算会把「别的 workspace 里仍可取消的 Job」误判成已作废，届时应改为 unknown；
   - 顺带说明 `_cancel` 把 `ValueError`（id 非法）也映射成 `job_not_found`（`jobs.py:612-613`），这不影响结论但应写在同处。

   改动量约 3 行注释，不动逻辑。之所以列为「必须修」：`settled` 是**持久**判定，其正确性论据必须自洽；现在的引用与结论相反，且真实保证（实现不带 workspace 过滤）是契约要求之外的事实，将来极易在无人察觉时失效。

## 建议（不阻塞）

- **P2 精度**：`task-journal.ts:200-212` 的「两种精确形状」在行为上等价于 `row.pending !== null`——create/cancel 两个分支已被 `validRecord` 更早的不变式（`:194-196`）蕴含。要么把注释改成这个更强的说法，要么直接简化（`shape` 换成 `row.pending !== null`）；同时 PR 描述里「记录形状放宽 → 4 红」应写明是「形状门 + identified-execution 门」的复合变异（我在下面反例表里已给出拆分数据）。
- **P3 测试**：补 1 条「已结算 cancel 带 `cursor` + `reconnect()` 前进水位」的会话级 e2e（探针已证明行为正确，缺的只是钉住）。
- **P4 文案**：`FileTaskPanel.tsx:100` 把「原提交已被协议确定性拒绝」改成「原请求已被协议确定性拒绝」是对的（cancel 不是提交），但同一句里「本页只能明确结束并重新开始」在 cancel-settled 时略歧义（Job 还在、只是取消意图作废）；可选微调，不影响判定。

## 独立复现的反例（完整命令与结果）

所有变异只发生在 detached 快照 `/private/tmp/sn729-pr`，跑完 `git checkout -- .` 复原；**没有任何变异进入提交或远端**。

```text
# 反例 1：cancel 保留 terminal 但不写 settled
$ npx vitest run
5 failed | 841 passed (846)     # 名单与作者 5 条完全一致

# 反例 2（写入侧）：settlesCreate/settlesCancel 退化为 operation 判定
6 failed | 840 passed (846)     # 与作者 6 条完全一致

# 反例 2（形状侧，拆分测量）
去掉 !shape                       → 2 failed
去掉 operationId !== null         → 2 failed
两者同时去掉                       → 4 failed      # = 作者口径
shape = row.pending !== null      → 0 failed       # 与基线 schema 逻辑等价
shape = true                      → 2 failed

# 反例 3：结算写点提前
#cancelIntent 发请求前写 settled   → 12 failed      # 作者 10，口径更宽
所有非契约错误一律写 settled        → 24 failed

# A2 附带：把 cancel 的 cleanup_pending 并入 unknown
3 failed | 69 passed (72)，被改测试在 file-task-rejection.test.ts:594 红
```

## 门禁实测（head `0d9e5b61`，detached 快照，系统 node v26.12.1）

```text
$ cd web/workbench && pnpm test
 Test Files  62 passed (62)
      Tests  846 passed (846)

$ pnpm typecheck
 web/shared, packages/contracts, web/admin, web/workbench … Done        # exit 0

$ pnpm lint
 [PASSED] Design tokens, copy boundaries, and capability states validated successfully.
 ESLint: passed                                                        # exit 0

$ pnpm format:check
 All matched files use Prettier code style!                            # exit 0

$ uv run ruff format --check .
 1830 files already formatted                                          # exit 0
```

基线（`origin/main` `404bfecd`）：`Tests 817 passed (817)` / `62 files`。作者的「846 / 62，基线 817，+29」成立。`uv.lock` 在两个 worktree 均未被改动（主检出里那份既有的 `M uv.lock` 与本审查无关，未触碰）。

## 只能依赖作者陈述的点

- **反例 3 的具体变异口径**：作者说 10 红，我用「发请求前写 settled」得 12 红，用「所有非契约错误写 settled」得 24 红。作者的变异细节未公开，我只能确认方向与提到的用例（两条 lost-response、`keeps a cancel intent that was refused before it acted…`）都确实变红，**具体 10 这个数字无法逐一复刻**。
- 作者「开工基线 `4ad6cad7`，期间 main 前进到 `cef39433`，已 rebase 并重跑门禁」的过程（历史动作），我只核验了 head 与当前 `origin/main` 的差异、以及自己重跑的门禁，不复核其过程。
- 作者关于「`#691` 的既有判据未动」的自述——这一条我**独立验证过**（见 A2），不需要依赖陈述。

## 时间线（本地时间，Asia/Shanghai）

- 10:15 核对主检出（停在陈旧分支 `docs/tracker-batch12`，带既有 `M uv.lock`，未触碰）；`git fetch`，记下 `origin/main` = `404bfecd`。
- 10:16 建 `review/729-cancel-settled`（基于 `origin/main`），确认 `git diff origin/main --stat` 为空；另建 detached 快照 `/private/tmp/sn729-pr` @ `0d9e5b61`。
- 10:17 `pnpm install --frozen-lockfile`（两个 worktree，复用 store）；`git show` 读全量 PR diff（8 文件、+486/−48）。
- 10:18 head 全量 `pnpm test` 846/62；`origin/main` 基线 817/62。
- 10:19 回溯 `#691`（`29c13c7e`）与 `#707`（`80e67675`）里该测试的三个版本，确认断言归属；读 `#707` 审查报告 `:79` 确认「边界」表述。
- 10:20 读实现：`settlesCancelAttempt`、`rejectionDisposition`、终态分支、`validRecord`、`save()`、`cancel()`/`#cancelIntent`、`#acceptJob`/`#checkJob`。
- 10:21 变异 A2：cancel `cleanup_pending` 并入 unknown → 3 红，被改测试在 `:594` 红（A2 结论）。
- 10:22 变异反例 1 → 5 红；变异反例 2 形状侧（纯 shape 0 红 / shape=true 2 红 / 复合 4 红）；写入侧 → 6 红。
- 10:23 变异反例 3（两种口径）→ 12 红 / 24 红；复原快照。
- 10:24 写 C7 探针（8 状态新旧谓词对照）+ B4 探针（身份、错身份、丢失后 404）+ C8 探针，全部实跑。
- 10:25 核后端：`_cancel` 路由与 404 映射、服务层 `workspace_id=None`、`_lock_cancel_target` 过滤、`speech_jobs` 无删除；核契约 989–990 行原文 → 发现 B4 论据反证。
- 10:26 门禁：`pnpm typecheck` / `pnpm lint` / `pnpm format:check` / `uv run ruff format --check .` 全绿；CI 路由脚本算得 `mode: web`。
- 10:27 E11 `cursor` 探针：settled cancel + cursor 重载与 `reconnect()` 回写均正确 → 缺口属测试覆盖。
- 10:28 复原快照并删除探针文件（`git status` 干净），开始写本报告。
- 10:29 发现 `origin/main` 期间被另一路审查推进到 `405a725a`（只加两篇 `docs/reviews/`），把本报告分支 rebase 到新 `origin/main`，确认 `git diff --name-only cef39433 405a725a` 只有两个文档、不影响任何实测。
- 10:31 报告完成；`uv run --no-project ruff format` + `uv run ruff format --check .` 复核后提交。
