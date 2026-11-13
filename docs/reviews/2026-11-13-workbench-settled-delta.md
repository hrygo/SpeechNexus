# PR #708 返修定点复核（`settled` 白名单结算）

> 过程记录文档：只做四项定点复核，不重跑全套审查。
> 审查对象：PR #708 返修 head `ffbc6e4217b65b664507e862757dc051cfeb40b8`（`317c957d..ffbc6e42`），base `7147f306` = `origin/main`（返修时点）。
> 上一轮报告（已合入 `431dce18`）：`docs/reviews/2026-11-13-terminal-exit.md`。实际复核发生在 2026-11-14 06:44–07:0x（本地时间，Asia/Shanghai）。
> 环境：系统 node v26.12.1、pnpm 12.14.0；探针与变异只在 `/private/tmp/sn709-r2-probe`、`-mut` 临时副本，未进入任何提交。

## 结论

**可以合并。** 四项复核全部成立；返修把 M1 的「时间序」缺口按白名单方式补齐，且白名单逐码对着后端实现核过**没有不安全成员**。仅留两条非阻塞项（白名单保守漏码使部分确定性 422 仍无出口；M2 形状下执行身份不落盘）。

| # | 项 | 判定 | 一句话证据 |
| --- | --- | --- | --- |
| 1 | M1 白名单安全性（a） | **成立** | 五个成员码的「原键下无本意图 Job」证明链在后端实现里逐段可核（`submit` 重放+重查、store 事务内 key 锁先于准入） |
| 1b | M1 白名单完整性（b） | **成立（保守，但有代价）** | 漏掉的 `model_not_published`/`profile_not_published`/`asset_not_ready` 其实也在重查块内、可安全结算；漏掉导致这些确定性 422 仍退回「无出口」 |
| 2 | M1 两条回归 + 旁路反例 | **成立** | 两条正是「页内」与「重载后」，且旁路变异精确复现 **3 failed / 283 passed**（红的三条即 M1 两条 + body 级拒绝） |
| 3 | M2 + fixture 改动性质 | **成立（非迁就实现）** | 旧 fixture 把 `recover-operation` 的 `nexus.operation_id` 复制到普通 422，属**不真实**信封；新 fixture 只给契约强制携带 nexus 的两个码显式传 `op-demo`，并新增「带 nexus 的拒绝不结算」用例 |
| 4 | M3 数字 | **成立** | 被审 head `317c957d` 上单点 13/251、4/260 与我上一轮独立实测逐字一致 |
| 另 | 「settled 再 save 仍携带」自述诚实性 | **成立** | 对 settled 记录调用全部公开会话动作，`setItem` 计数 0、网络请求 0；`save()` 丢弃字段抛 `changed` |

门禁（返修 head）：`pnpm test` **808 passed / 62 files**（上一轮 786，+22）；`src/file-workflow` 子集 **286**（247+17 那套口径，+22）；`typecheck` exit 0；`lint`「ESLint: passed」；`format:check`「All matched files use Prettier code style!」；我的 35 例 delta 探针全绿。

## 1. M1 白名单：逐码核（安全性全部所在）

结算谓词现在是 `settlesReplayedIntent`（`file-task-session.ts:134-150`）+ `!nexus.operation_id` + 原来的 create/无 Job/无执行身份。白名单两段：`409 idempotency_conflict`；`422 job_text_asset_unsupported | voice_not_published | job_binding_* | model_binding_*`。

**(a) 名单里有没有「其实不保证原键下没有 Job」的码 —— 没有。** 证明链（对着实现，不是对着注释）：

1. `PublicJobs.submit`（`application/public_jobs.py:578-649`）先 `_canonical_intent` → `_find_original`（原键重放）→ `_submit_new`；`_submit_new` 抛出的 `UnknownWorkspaceError`/`DisabledWorkspaceError`/`ModelNotPublishedError`/`ModelBindingError`/`VoiceNotPublishedError`/`ProfileNotPublishedError`/`PronunciationNotFoundError`/`AssetNotReadyError`/`VoiceDraft*` 会被 `except` 块**再查一次原键**后才继续抛（`:624-648`）。因此 `job_binding_*`/`model_binding_*`/`voice_not_published` 的传播意味着两次同键同摘要查找都落空。
2. store 侧 `create`（`infrastructure/public_jobs_postgres.py:513-562`）在**同一事务内**先 `_lock_submission_key` → `_existing_by_key` → `_replay_or_conflict`，**然后**才拿 binding 锁、走准入。`_replay_digest`（`:1426-1433`）只在 `request_digest` 与 `retry_of_job_id` 完全一致时返回 `replayed`，否则 `conflict`。所以 `idempotency_conflict` 的含义是「该键属于**另一个**摘要的意图」，本意图在该键下没有 Job（同摘要会重放）；并发同键写入被 key 锁串行化，`UniqueViolation` 首次重试、二次返回 `unknown`（`:605-610`），不会伪装成 conflict。
3. `job_binding_incomplete` / `job_text_asset_unsupported` 来自 `public_job_admission.py`（`:123,311,344,674`），经 `PublicJobAdmissionRejectedError` 在同事务内被 `create` 转成 `JobCreateResult("rejected")`（`:588-592`）——同样发生在 `1.` 的 key 检查之后。
4. 该安全性依赖一个客户端不变量：同一条持久记录重建出的 canonical body 恒定（v1 固定；v2 文本从不可变 asset 读回、voice/profile revision 固定），否则 `idempotency_conflict` 的推理不成立。这条今天成立，值得写进注释（可后续）。

**(b) 漏码 —— 存在，属保守但把 #707 的「无出口」留在了这几个码上。** 下列码也只在重查块内产生（`ModelNotPublishedError`/`ProfileNotPublishedError`/`AssetNotReadyError` 都在 `submit` 的重查元组里，且 raise 点位于 `_prepare_job` 内 `public_jobs.py:783,801,805,837,822,833`），因此**本可安全结算**却不在白名单：

| 漏码 | 可达性（本客户端） | 实际后果 |
| --- | --- | --- |
| `422 model_not_published` | 可达（非 binding 路径按 `model` 解析） | 确定性拒绝后 `unknown` 恒真、`canFinishTask` 恒假 → 回到 #707 的「既不能放弃也不能新建」 |
| `422 profile_not_published` | 可达（TTS profile revision） | 同上 |
| `422 asset_not_ready` | 可达（上传后素材状态在提交时变化） | 同上 |
| `422 pronunciation_not_found` | 不可达（本客户端 TTS body 无 pronunciation） | — |

`workspace_not_found`/`workspace_disabled`/`job_kind_not_supported`/`unsupported_parameter`/`invalid_job_request` 被排除是**对的**：这些码另有重放检查**之前**的产生点（`submit:596-598` 的 workspace 类型检查、`:586-594` 的 kind 检查、HTTP `_create` 的 body 解析），无法区分来源。作者的 16 组谓词表把这些列 false 与实现一致。

建议（非阻塞）：把 `model_not_published`、`profile_not_published`、`asset_not_ready` 加入白名单（同一条重查块的证据已经足够），即可让这些确定性拒绝也拿到显式放弃出口；不加入也不会重复执行，只是用户被卡住。

## 2. M1 两条回归与旁路反例

要求的正是「`503 unknown → 重放 → 401`」在**页内**与**重载后**都不结算。实测两条都在，且比要求更全：

- `keeps an earlier unknown outcome unknown when a replay is refused before the key check`（页内 + 页内重载断言）与 `keeps a reloaded unknown outcome unknown when its replay is refused before the key check`（先重载再重放）。断言项与我上一轮探针一致：`unknown:true, canReplay:true`、`rejected` 空、notice 含「不能证明原提交未执行」、`canFinishTask:false`、`createKeys` 长度 2 且去重后为 1（同键重放）、存储无 `settled`。
- 我的 delta 探针把这类码扩到 6 个（401/403/404/405/400/413），页内与重载后全部保持 unknown；白名单 5 个正例（409 conflict、422 三个 + model_binding_retired）在重载后仍结算且二次重载仍 `rejected`。
- **旁路变异独立复现**：把 `settles` 里的 `&& settlesReplayedIntent(contractError)` 去掉（谓词本身不动）→ `src/file-workflow` **3 failed / 283 passed (286)**，红的正是上述两条 + `keeps a body-level refusal unknown on a first attempt`。与作者数字一致。
- 顺带复现 M2 守卫的变异（去掉 `!nexus.operation_id`）→ **1 failed / 285 passed**，红的正是 `never settles a refusal that names an identified execution`。也与作者数字一致。

## 3. M2 与 fixture 改动性质（重点）

M2 代码：`settles` 增加 `!contractError.detail.error.nexus?.operation_id`，只有该条件为真才写 `settled`，且不满足时进入「保留 unknown + 恢复信封 + 重放句柄」分支。我的探针：`409 idempotency_conflict` + `nexus.operation_id: op-demo` → 页内 `unknown:true, canReplay:true`、`recovery` 保留 `op-demo`、存储无 `settled`、`canFinishTask:false`。成立。

fixture 改动的**性质判断：这是把不真实的 fixture 改回真实，不是迁就实现。**

- 旧 fixture（`tts-task.test.ts::failure`、`dev/demo-transport.ts::error`）克隆 `recover-operation` 示例后只覆盖 `code`/`message`，于是**每一个**错误信封都带着 `nexus.operation_id: "op-demo"`。按 `contracts/shared/errors.schema.json`，`nexus` 只在 `operation_in_progress` / `response_not_replayable` 时被强制；把执行身份挂到普通 422 上在协议里不存在。旧断言「422 × interaction 后结算」实际上是在断言一个**协议不会发出的组合**，属于假绿。
- 新 fixture 按 status 构造普通信封，并给 `operation_in_progress` 的调用点显式传 `op-demo`（`demo-transport.ts:77`、`tts-task.test.ts:501`），即保留契约强制的身份，去掉伪造的身份。
- 覆盖面没有被削弱、反而增加：`job_text_asset_unsupported` × 2 仍断言结算（真仍成立）；新增 `never settles a refusal that names an identified execution` 专门钉住「带身份不结算」；`settlesReplayedIntent` 有 16 组表驱动边界。仓库其余用例（含 `upload-recovery.test.ts` 对 `recover-operation` 的直接使用）全绿，说明没有别的测试靠这个伪造字段。

## 4. M3 数字

在被审 head `317c957d` 上（`src/file-workflow`，264 例）我上一轮独立实测：classifier 恒 terminal = **13 failed / 251 passed**；`spent` 恒 true = **4 failed / 260 passed**；两者复合 = **17 failed / 247 passed**。作者 PR 正文的三行与这三个数字逐字一致，且明确标注「17/247 是复合结果、不能当作单个修复的守卫强度」。成立。

## 新绿 / 非阻塞项

1. **白名单漏码仍然无出口**（见 1b）：`model_not_published` / `profile_not_published` / `asset_not_ready` 是确定性、可在重查块内证明的拒绝，却仍落回 `unknown` + `canFinishTask:false`。方向安全（不重复执行、保留原键可重放），但 #707 的原始症状在这三个码上仍在。建议补白名单。
2. **M2 形状下执行身份不落盘**：`terminal` 分类但带 `nexus.operation_id` 时，该身份只进 `view.recovery`，记录仍是 `pending:"create"` 无 `operationId`。实测重载后 `recovery` 变 `undefined`（`unknown:true, canReplay:true`，安全方向），但用户丢掉已识别的执行句柄。可按 unknown-outcome 分支同样 `#persist({ operationId })`，或注明该信封形状契约不可达（只有两个 recovery 码携带 nexus）。
3. 「settled 再 save 仍携带」用例的自述**诚实**：我枚举了 `#persist` 的全部调用点（`#acceptJob`、`#submit`、unknown-outcome、operation_in_progress、cursor 写入、`save()` 守卫），它们都需要 job/operation 身份或一次新提交，settled 记录都不具备；再用「对 settled 快照调用全部公开动作」的探针实测 `setItem` 0 次、网络 0 次。所以「settle 之后目前没有会话写入路径，这条守的是未来写入的携带语义」是准确描述，不是掩饰；`save()` 的 `changed` 守卫仍是未来写入路径的兜底。

## 可否合并

**可以合并**（一句话：M1 白名单逐码在后端实现上可证安全、两条回归与两个变异数字均独立复现、M2 与 fixture 改动是修真实而非迁就实现；余下两条为白名单保守漏码与执行身份落盘的可选加固，不阻塞）。
