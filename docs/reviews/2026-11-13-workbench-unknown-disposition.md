# 工作台失败处置三分类的非作者审查（PR #692 / issue #691）

> 过程记录文档：保留时间线、独立复现的反例与命令输出。结论面向合并决定，不修改产品代码。
> 审查对象：`fix/691-unknown-clearing`，head `8d858e686cbcae21ed855a3fb205e9ec16e59358`（下称 head），base `e58e16e7`（= 审查时 `origin/main`）。
> 文件名沿用任务指定的 `2026-11-13`；实际审查发生在 2026-11-14 凌晨 03:40–04:00（本地时间，Asia/Shanghai），下文时间线用本地时间。
> 主检出停在陈旧分支 `docs/tracker-batch12`，所有内容一律在独立 worktree 或 `git show <rev>:<path>` 读取。

## 结论

**有条件通过**（核心缺陷已修复且方向正确，但需先补四项：两处注释事实错误、一处契约声明与分类函数上下文的缺口、以及作者明确声称却零覆盖的 recovery 保留回归测试）。

判定依据：

- issue #691 的缺陷归属正确：head 的 `rejectionDisposition` 确实把 `503 job_outcome_unknown` 从「终态清空」改为「保留未知态 + 保留/记录 recovery」，我在基线 worktree 与 head worktree 两侧独立复现了「修前清空、修后保留」。
- 作者最反直觉的判断——`409 *_outcome_unknown` 属「结果未知」而非 terminal——**成立**；`contracts/speech-openapi.yaml` 的 `VoiceDraftCreationConflict` 与 `PronunciationSetConflict` 确实把这两个码挂在 409，并写明提交可能已发生。只按状态码分类会错，这一点作者判断正确。
- 但代码把「4xx 一律 terminal」的理由写成「4xx 拒绝证明该 key 什么都没写」，这个全称判断在契约里**不成立**（`409 execution_cleanup_pending` 证明存在一个终态 Job；`409 idempotency_conflict` 证明该 key 已有另一个意图的 Job）。本 issue 的核心正是「注释与代码对不上」，新注释不能再留同类错误。
- 763 例全绿、typecheck/lint/format/ruff 全绿、作者的反例数字（10 failed | 2 passed）与红因我逐条复现一致；未发现产品逻辑回归。

## 审查对象与方法

- 独立 worktree：`git worktree add /private/tmp/sn695 -b review/695-unknown 8d858e68`（按任务指定）。
- 基线 worktree：`git worktree add --detach /private/tmp/sn695-base e58e16e7`，用于复核「修前清空」与基线例数 751。
- 复现环境：node v26.12.1、pnpm 12.14.0；`pnpm install --frozen-lockfile --offline`（离线复用 store，未改动 `pnpm-lock.yaml`/`uv.lock`）。
- 独立探针（只在 `/private/tmp/sn695-mut` 临时副本内，未进入任何提交）：
  - `review-695-probe.test.ts`（50 例）：把 `rejectionDisposition` 临时加 `export` 后直测边界输入，并用真实 `FileTaskSession` 驱动 6+ 个码、recovery 保留、取消路径、UI 新建入口；
  - `review-695-base-probe.test.ts`（基线 3 例）：证明修前 `503/429/500` 都会清空 `unknown/canReplay/recovery`。
- 变异实验：在 `/private/tmp/sn695-mut` 内把 `rejectionDisposition` 恒返回 `terminal`，跑作者新增用例，复现 10 failed | 2 passed；变异只在临时副本，未 push。
- 契约/后端核对：`contracts/speech-openapi.yaml`、`contracts/shared/errors.schema.json`、`backend/.../interfaces/http/job_errors.py`、`asset_errors.py`、`jobs.py`、`asset_ingress.py`、`application/public_jobs.py`。

基础门禁（在 `/private/tmp/sn695`）：

```text
$ cd web/workbench && pnpm test
 Test Files  62 passed (62)
      Tests  763 passed (763)

$ cd /private/tmp/sn695-base && cd web/workbench && pnpm test     # 基线 e58e16e7
 Test Files  61 passed (61)
      Tests  751 passed (751)                                      # 作者"751 → 763（+12）"成立

$ pnpm typecheck      → web/shared、packages/contracts、web/admin、web/workbench 全 Done
$ pnpm lint           → Design tokens … [PASSED]；ESLint: passed
$ pnpm format:check   → All matched files use Prettier code style!
$ uv run --no-project ruff format --check .
1814 files already formatted
```

新增文件正好 12 例（9 个 `it` + 1 个 3 例 `it.each`），与 763−751 一致。

## 八条逐条判定

### 1. 分类是否与契约一致 —— 通过，但注释有两处事实错误（必须修）

逐个错误码核对（状态、码、`retryable` 三要素）：

| 码 | 状态 | 后端 `retryable` | 契约依据 | 作者处置 | 判定 |
| --- | --- | --- | --- | --- | --- |
| `job_outcome_unknown` | 503 | true（`job_errors.py:93`、`:129-134`） | `speech-openapi.yaml:840-842` 保留原键与原请求、重放或查询原 Job | unknown-outcome | 对 |
| `job_capacity_exhausted` | 429 | true | `:823-824` 写 0 行、`Retry-After: 1`（`job_errors.py:135-139`） | retryable | 对 |
| `job_admission_contended` | 503 | true | 并发准入变化，可重试 | retryable | 对 |
| `job_unavailable` | 503 | **false** | `job_errors.py:113`；`ServiceUnavailable`（`:2897`）写明「retry only when `retryable` is true」 | retryable | 行为安全，**命名/文案与协议冲突**（见第 2 条） |
| `asset_inspection_busy` | 503 | true（`asset_errors.py:_RETRYABLE`） | `:2682-2695` 唯一「同一请求稍后可能成功」的 Asset 503；但同段又说「issue a new upload instead of replaying the original transport request」 | retryable | 方向对，**文案只说「按原键重试」**（见第 7 条） |
| `voice_draft_outcome_unknown` | 409（**同码也出现在 503**） | 409 为 false | `VoiceDraftCreationConflict`（`:2945-2977`）：keyless 提交可能已提交；503 变体（`:1657-1670`）：带原键、用原键重试 | unknown-outcome | **判断成立**，但属本客户端不可达面（见下） |
| `pronunciation_outcome_unknown` | 409 | false | `PronunciationSetConflict`（`:3172-3206`）：keyless 提交可能已提交 | unknown-outcome | **判断成立**，同上 |

对最反直觉的一点（409 `*_outcome_unknown`）：

- 判断**正确**。契约确实把这两个码挂在 409，并写明「提交可能已提交，重提是新意图、可能产生第二份」；只看状态码会把它们当 terminal 清掉未知态，作者的「同时看状态与码」有真实依据。
- 但同一码 `voice_draft_outcome_unknown` 在 voice-design 路由上**同时**存在 409（keyless）与 503（带键）两种语义；作者的表只显式列了 `[503, job_outcome_unknown]`，靠 `code.endsWith("unknown")` 兜住两个状态。分类结果对两者都安全（都不会清 unknown），但**派生动作不同**：409 是 keyless、正确动作是「先对账，重提是新意图」，503 才是「按原键重放」。本客户端所有提交恒带幂等键、且创建路由不会返回这两个码，所以当前不可达；`canReplay: true` 与「按原幂等键重放」文案只在本客户端恒带键的前提下成立。注释没有写出这个不变量。
- 两处注释事实错误（必须修）：
  1. `rejectionDisposition` 注释称「A 4xx contract refusal is the only class that proves nothing was written for this key」。**不成立**：`409 execution_cleanup_pending` 证明存在一个终态 Job；`409 idempotency_conflict` 证明该 key 已有另一个意图的 Job。二者都是 4xx。
  2. terminal 分支注释称「the protocol declares no Job row for it (e.g. … 409 `idempotency_conflict`)」。**不成立**：`idempotency_conflict` 的契约原文是「The key identifies another Job intent」（`speech-openapi.yaml:805`；`job_errors.py:12-16`），即该 key 下有 Job 行，只是不属于本次意图。「本次意图没写」成立，「该 key 没写」不成立。
  3. 代码注释引用 `job_errors.py:136-140` 作为 retryable 集合，实际行号是 **129-134**（PR 正文同样写错）。

### 2. 有没有把 terminal 误判成 unknown 或反之 —— 通过，但发现一个契约声明面的缺口（必须修）

用真实 `FileTaskSession` 逐码验证（探针 50 例全绿，摘录）：

| 场景 | 期望 | 实测 |
| --- | --- | --- |
| 创建 `422 job_text_asset_unsupported` | terminal：`unknown=false, canReplay=false, recovery=undefined` | 一致 |
| 创建 `409 idempotency_conflict` | terminal | 一致 |
| 创建 `409 execution_cleanup_pending` | 作者归 terminal | **实测 terminal：清 unknown**（见下） |
| 创建 `503 job_outcome_unknown` | unknown：`unknown=true, canReplay=true, recovery=detail` | 一致 |
| 创建 `500 internal_error` | unknown（未列 5xx 兜底） | 一致 |
| 创建 `429 job_capacity_exhausted` | retryable：`unknown=false, canReplay=true` | 一致 |
| 创建 `503 job_unavailable` | retryable：`unknown=false, canReplay=true`，notice 含「按原键重试」 | 一致 |
| 创建 `503 job_admission_contended` | retryable | 一致 |
| 上传 `503 asset_inspection_busy` | retryable，随后同键重放成功 | 一致 |

两个特别点名项：

- **`409 execution_cleanup_pending`（create 路径）**：作者说取消路径仍按 terminal。取消路径的 terminal 是**对的**（Job 身份已知，该 409 证明取消请求被确定性拒绝，清 unknown 合理）。但分类函数只有 `(status, code)`，没有操作上下文：`execution_cleanup_pending` 的语义是「Job 已终态、原执行未确认停止」，**证明存在一个 Job**。契约里创建路由的 409 复用了通用 `Conflict` 组件（`:2860-2868`），其描述明确列出「cleanup pending」，所以**契约层面创建路由也可以返回它**。当前后端只在 cancel/retry 处理器发出该码（`jobs.py:607`、`:632`），本客户端也不调用 retry，故当前不可达、不是活 bug；但这是「分类只靠状态+码」的固有缺口：同一个码在 create 应保 unknown、在 cancel 应 terminal。必须至少把这一依赖写成显式注释（或给分类函数加操作上下文），否则契约/代理一旦变化就会重演「清掉未知态」。
- **`503 job_unavailable`（非 retryable）**：作者「只保留意图、由用户显式按原键重试」的区分**行为上成立**。后端把存储的 `rejected` 结果与 `JobRejectedError`/`PublicJobUnavailableError` 都映射为 503 `job_unavailable`（`jobs.py:507`、`public_jobs.py:1501`），本次意图未写入；即使某条 raise 点发生在提交后读回阶段，**同键重放也只会返回原 Job、不会二次执行**，所以保留 `canReplay` 是安全且唯一的对账方式。问题在**命名与文案**：协议对该码 `retryable: false`，`ServiceUnavailable` 组件原文是「retry only when `retryable` is true」，而处置名也叫 `retryable`、notice 写「请稍后按原键重试」。同名的两个 `retryable` 含义不同（协议重试许可 vs 意图保留），应在命名或注释里拆开。

### 3. 「不出现可新建」是否真成立 —— 通过

独立核对了所有用 `unknown` 做判断的地方：

- `file-task-policy.ts:canFinishTask`：`!state.unknown` 是首项，unknown 恒为 false 才能通过。
- `FileTaskPanel.tsx`：「准备新任务」按钮 = `canFinishTask(state)`；「继续创建原任务」= `asset ready && !job && !state.unknown`；导入、取消、告警也都以 `!unknown` 为前提。
- `FileTaskHost.tsx:onNew`：先调 `host.clearCompleted()`，后者在 `!canFinishTask` 时 `throw new Error("Task is not confirmed complete")`，生产入口唯一的新建路径被同一守卫拦截。
- 探针实测：unknown-outcome 快照 `canFinishTask === false`，且 `asset ready && !job && !unknown === false`。
- 未发现生产 UI 的其他新建入口（`dev/TtsTaskDemo.tsx` 的 scenario 切换是开发演示，不属于生产装配）。

结论成立；唯一绕开方式是用户在浏览器里手改/清空 sessionStorage，不属于 UI 路径。

### 4. `recovery` 是否真的保留 —— 通过，但作者声称的这条性质零测试覆盖（必须修）

构造「先一次未知（带 recovery）→ 又一次未知」：

```text
attempt0 TypeError（丢响应）        → unknown=true, recovery=undefined, canReplay=true
attempt1 503 job_outcome_unknown    → recovery=job_outcome_unknown
attempt2 500 internal_error         → recovery 仍为 job_outcome_unknown（未被覆盖成空）  ✅
```

另外两种也通过：

- 前一次 503 `job_outcome_unknown` → 后一次 `429`：retryable 分支不触碰 recovery，保留原 detail。
- 前一次 503（无 nexus）→ 后一次 503（带 `nexus.operation_id`）：recovery 保留旧 detail，`#persist({operationId})` 落盘 `op-demo`；`canReplay` 变 false。

但作者新增的 12 例里**没有一例**覆盖 `#view.recovery ?? detail` 的保留语义（现有用例都是「一次未知」）。这条是作者在 PR 正文明确声称的性质，应补回归测试。附带一个非阻塞的 UI 小问题：后一次未知带 operationId 时，`state.recovery` 仍是旧 detail（无 nexus），`state.operation` 又未读取，面板「原执行」字段在 `refresh()` 前显示「无」，与 notice「已取得原执行身份」不一致。

### 5. 既有保护未弱化 —— 通过

逐条核对（`git show 8d858e68^:…` 对比 + head 源码）：

- `operation_in_progress`/`response_not_replayable` 分支：原文未动，仍在三类之前，置 `unknown=true, canReplay=false, recovery=detail`，并持久化 `operationId`。既有测试 `file-task-session.test.ts` 的 `recover-operation` fixture 覆盖。
- `SpeechUploadRecoveryError`（`asset_upload_in_progress` / `asset_upload_unknown` / `asset_metadata_unknown`）：原文未动；`upload-recovery.test.ts` 覆盖。
- `OriginalTextUnavailableError`：原文未动；`tts-task.test.ts:517-535` 用 5 种读回故障覆盖，断言 `unknown=true, canReplay=true`、`creates` 不增。
- 「原上传未确认」守卫：从「最后 else 内」提为独立 else-if，位置仍在三个新类之前。**语义不变的证明**：按 (mutation, error 类型) 穷举，旧 `else` 能到达的场景与新 else-if 链完全一致；守卫条件逐字未变；旧分支的 `return` 改由 else-if 结构承担。另有现成测试咬住顺序：`FileTaskHost.test.tsx:197-259`（fresh + restored 两种「未知上传后再遇 413」）断言 unknown 不被清、无「准备新任务」——若把守卫排到 terminal 之后，该用例会红。
- 唯一的行为差异就是本 PR 要改的部分：未被前三类接住的 mutation 契约错误，从「一律清空」改为按三分类处置。

### 6. 反例是否咬住 —— 通过（10 failed | 2 passed 独立复现）

把 `rejectionDisposition` 恒改为 `terminal`（等价回退到 `instanceof` 一刀切）后：

```text
 Test Files  1 failed (1)
      Tests  10 failed | 2 passed (12)
```

逐条红因（作者测试文件行号）：

| # | 用例 | 首个失败断言 | 红因 |
| --- | --- | --- | --- |
| 1 | 503 job_outcome_unknown 保留未知态 | `:99 expect(snapshot.unknown).toBe(true)` | 未知状态被清掉 |
| 2 | 503 后按原键重放 | `:119 expect(unknown).toBe(true)` | 同上 |
| 3 | 未列出 5xx | `:136 toMatchObject({unknown:true, canReplay:true})` | 同上 |
| 4 | 409 `*_outcome_unknown` | `:149` | 同上 |
| 5 | 带 operationId 的 503 | `:160 toMatchObject({unknown:true, canReplay:false})` | 同上 |
| 6 | 首次 429 | `:180 expect(canReplay).toBe(true)` | **意图被作废**（此例 unknown 本应 false，红因不是 unknown） |
| 7 | 未知后再遇 429 | `:207 expect(refused.unknown).toBe(true)` | 未知状态被清掉 |
| 8-10 | 503 job_unavailable / job_admission_contended / asset_inspection_busy | `:233` | 未知状态被清掉 |

结论：10 例全部红在状态字段，**无连带失败**；9 例红在「unknown 被清」，第 6 例（首次 429）红在「canReplay 被置 false」，属同一刀切下的另一种正确症状。2 个通过项正是两个 terminal 用例（422、413）。作者 PR 正文「首条红因即 `expect(snapshot.unknown).toBe(true)` 收到 false」成立（首条红确为 #1）。

### 7. 「未被精确分类的失败面」是否完整 —— 无法逐条比对；三条点名项已核，另有遗漏

作者的 7 条清单**没有出现在 PR #692 的正文、提交信息、PR/issue 评论或仓库文档中**（我检索了 `gh pr view 692`、`git show -s --format=%B`、`gh pr view 692 --comments`、`gh issue view 691 --comments`、`docs/`），因此无法逐条核对完整性；我按任务点名的三条独立核验，并另外枚举。

点名的三条，均成立：

1. **未列出 5xx 一律判 unknown**：成立，方向安全；文案不精确（例如上传路径的 `asset_service_unavailable`/`asset_inspection_unavailable` 实际发生在 app 处理前或配置缺口，notice 仍说「原提交可能已执行…读取原执行」）。
2. **`Retry-After` 未透传**：成立。`SpeechResponseError` 只保留 status + body，没有 header；`web/workbench` 全文没有读取 `Retry-After` 或 `retry_after_ms`。
3. **协议违约组合**：成立。探针实测 `422 + job_capacity_exhausted` → terminal（清 unknown）。状态与码的绑定是契约事实，客户端信任状态码，因此协议违约会走错方向。

我另外找到、作者清单若未包含则应补的失败面：

- **`error.retryable` / `X-Should-Retry` 完全未消费**：分类器硬编码了 5 条码表，协议自己的 `retryable` 布尔与 `X-Should-Retry` 都被忽略。后端新增/调整可重试码时客户端会静默漂移（`asset_inspection_busy` 之外还有 `asset_ingress_full` 等靠 429 兜底，恰好安全）。这是结构性遗漏。
- **`409 execution_cleanup_pending`（create 路由）**：见第 2 条，契约 409 组件声明了它，当前分类为 terminal。
- **terminal 上传拒绝的「可新建」承诺不成立**：`#uploadInput` 的 `uploadRejected` 白名单只有首次 `413 asset_too_large` 与 `422 asset_empty`；`422 invalid_asset_audio` / `asset_audio_too_long` / `asset_text_too_long` / `asset_digest_mismatch` 等确定性上传拒绝会清 unknown 但 `uploadRejected` 未置位，`canFinishTask` 仍 false → 用户既不能重放也不能新建（我探针实测 3 个码）。这是既有缺口，不是本 PR 引入，但「terminal 后新意图安全」在 UI 上并未兑现。
- **terminal 创建拒绝同样没有新意图入口**：`canFinishTask` 需要 `uploadRejected` 或终态 Job，创建 4xx 后两者都不满足；作者注释已承认「Abandoning that spent record still needs UI work outside this task」。
- **取消路径 409 terminal 与持久记录不一致**：`#cancelIntent` 先 `#persist({pending:"cancel"})`，terminal 分支不清 pending；实测取消遇 409 后内存 `unknown=false`，重载后 `unknown=true, canReplay=true`（durable pending cancel 复活）。既有行为，非本 PR 引入。
- **上传路径的 unknown-outcome 文案**：分支是 create/upload 共用的，上传 5xx 会提示「读取原执行」，应说「原素材/原上传」。

### 8. 新绿/新红与边界输入 —— 通过；两例新测试属合成面，一例是既有保护

边界输入（纯函数，探针全绿）：

| 输入 | 结果 | 说明 |
| --- | --- | --- |
| `(0, "")`、`(0, job_outcome_unknown)` | unknown-outcome | 非 4xx/5xx 落到保守兜底，安全 |
| `(200, …)`、`(399, …)`、`(600, …)` | unknown-outcome | 同上 |
| `(418, "teapot")` | terminal | 未知 4xx 清 unknown；契约内 4xx 均有「未写入」或「确定性拒绝」依据，但见第 1 条全称判断问题 |
| `(408, "asset_receive_timeout")` | terminal | 契约 `AssetReceiveTimeout` 明确「expired before reservation」，安全 |
| `(409, "not_unknown")` | unknown-outcome | `endsWith("unknown")` 命中，按名字算误判但**方向安全**（不清 unknown） |
| `(409, "unknown")` | unknown-outcome | 同上 |
| `(409, "NOT_UNKNOWN")` | terminal | `endsWith` 区分大小写；协议码全小写，不可达 |
| `(503, "job_outcome_unkown")` | unknown-outcome | 5xx 兜底，安全 |
| `(422, "job_capacity_exhausted")` | terminal | 协议违约组合，作者已列 |

新绿（通过但证明力弱的用例）：

- 「带 operationId 的 503 job_outcome_unknown 只查询」：**合成组合**。后端 `job_errors.py` 构造错误体时不带 `nexus`；`shared/errors.schema.json` 只要求 `operation_in_progress`/`response_not_replayable` 携带 nexus，而这两个码当前后端也未发出。该分支是防御性代码，生产不可达。
- 「409 voice_draft_outcome_unknown」：创建路由不会返回该码（属 voice-design 路由），且真实 409 是 keyless 语义；测试验证的是分类规则，不是本客户端可达协议面。
- 「首次 413 仍允许新建」：在恒 terminal 变异下**仍然通过**，它咬的是既有 `#uploadInput` 白名单，不是本 PR 的新分类。
- 缺失覆盖：recovery 保留（第 4 条）、上传未确认守卫与三分类的交叉（现有 `FileTaskHost.test.tsx:197` 间接覆盖）、取消路径 409、上传路径 5xx 文案、`execution_cleanup_pending`。

新红：无（763 全绿）。

## 必须修清单

1. **修正 `rejectionDisposition` 与 terminal 分支的注释事实错误**：删除「4xx 拒绝证明该 key 什么都没写」的全称判断；改写 `409 idempotency_conflict` 的「协议声明无 Job 行」；把 `job_errors.py:136-140` 改为 `129-134`。本 issue 的核心就是「注释说 A、代码做 B」，新注释不能再留同类问题。
2. **写明 `409 execution_cleanup_pending` 的处置依赖**：它的语义证明存在终态 Job，创建路由的契约 409 组件也声明了它；当前只在 cancel/retry 发出，所以取消路径 terminal 正确。必须在注释里显式写出「依赖后端只在 cancel/retry 发出该码」，或给分类函数补操作上下文（create → 保 unknown，cancel → terminal）。
3. **补 recovery 保留的回归测试**：「前一次未知带 recovery → 后一次未知（无 nexus / 带 operationId）」两例，锁住 `#view.recovery ?? detail` 与 `#persist({operationId})` 的组合；这是作者 PR 正文明确声称、当前零覆盖的性质。
4. **拆开两个 `retryable` 的语义**：`job_unavailable` 的协议 `retryable` 是 false、契约要求「retry only when retryable is true」，而处置名与 notice 都写成「可重试」。建议改名（如 `intent-kept`/`replayable`）或至少在注释与 notice 里区分「协议重试许可」与「保留原意图、按原键对账」。

非阻塞建议（可后续）：

- unknown-outcome 带 operationId 时，面板「原执行」字段在 `refresh()` 前显示「无」，可同步展示 `recovery`/`operationId`。
- 上传 5xx 的 unknown-outcome 文案改为「原素材/原上传」。
- 考虑消费 `error.retryable`/`X-Should-Retry` 作为分类器的交叉校验，减少硬编码码表漂移。
- 取消路径 terminal 后清理持久 `pending:"cancel"`，避免重载复活 unknown（既有问题）。
- 把「确定性上传拒绝后无新意图入口」列为后续 UI 工作（既有问题）。

## 独立复现的反例（原始输出）

恒 terminal 变异：

```text
     × keeps one unknown outcome and its recovery identity for 503 job_outcome_unknown
     × replays the original key after 503 job_outcome_unknown instead of starting over
     × treats an unlisted 5xx create failure as an unknown outcome, never a spent intent
     × never spends a 4xx intent whose code marks the outcome unknown
     × queries an identified original execution instead of replaying it after 503 job_outcome_unknown
     × keeps the original intent retryable for a first 429 job_capacity_exhausted
     × never clears an earlier unknown outcome when a replay is refused by 429 job_capacity_exhausted
     × retains the intent and the unknown state for 503 job_unavailable
     × retains the intent and the unknown state for 503 job_admission_contended
     × retains the intent and the unknown state for 503 asset_inspection_busy
     ✓ keeps the existing terminal behavior for a deterministic 422 rejection
     ✓ keeps an explicit new-intent path for a first deterministic upload rejection

 Test Files  1 failed (1)
      Tests  10 failed | 2 passed (12)
```

基线（e58e16e7）行为：

```text
$ npx vitest run src/file-workflow/review-695-base-probe.test.ts
 ✓ baseline clears the unknown state for 503 job_outcome_unknown
 ✓ baseline clears the unknown state for 429 job_capacity_exhausted
 ✓ baseline clears the unknown state for 500 internal_error
      Tests  3 passed (3)
```

head 独立探针：

```text
$ npx vitest run src/file-workflow/review-695-probe.test.ts
      Tests  50 passed (50)
```

## 只能依赖作者陈述的点

- **「7 条未被精确分类的失败面」清单本身**：不在 PR #692 的任何产物中（正文/提交/评论/仓库文档），无法逐条比对完整性；第 7 条按任务点名的三条 + 我的独立枚举给出结论。
- **`job_unavailable` 的「存储拒绝、未报告 Job 事实」**：我核对了 `_submission_result` 的 rejected→`job_unavailable` 映射与 `jobs.py:507` 的兜底，但未逐条证明所有 `PublicJobUnavailableError` raise 点都在提交前。该处置不依赖这一点（同键重放即使原提交已提交也只返回原 Job），所以不影响结论。
- **作者对「上传守卫语义不变」的自述**：我通过分支顺序穷举 + 既有 UI 用例独立复核，结论一致；不依赖作者陈述。

## 时间线（本地时间，2026-11-14）

- 03:40 `git fetch` + 建 worktree `/private/tmp/sn695`（review/695-unknown @ 8d858e68）；确认主检出停在 `docs/tracker-batch12`。
- 03:41 `pnpm install --frozen-lockfile --offline`（复用 store）。
- 03:42 `cd web/workbench && pnpm test`：62 files / 763 tests 全绿。
- 03:42 `pnpm typecheck`：四个包全 Done。
- 03:45 `pnpm lint`、`pnpm format:check` 全绿；建基线 worktree `/private/tmp/sn695-base`（e58e16e7）。
- 03:46 基线 `pnpm test`：61 files / 751 tests，确认 +12。
- 03:46–03:48 独立探针 50 例（6+ 码、边界输入、recovery 保留、取消路径、UI 新建入口）全绿；基线探针 3 例证明修前清空。
- 03:47 恒 terminal 变异复现 10 failed | 2 passed，逐条记录红因行号；变异只在 `/private/tmp/sn695-mut`。
- 03:50 `uv run --no-project ruff format --check .`：1814 files already formatted；`git status` 干净，`uv.lock` 未变。
- 03:5x 撰写本报告；复核后提交 `docs/reviews/2026-11-13-workbench-unknown-disposition.md`。
