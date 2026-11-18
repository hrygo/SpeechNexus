---
title: 同步转写的租约结算
status: active
version: "1.0.0"
date: 2026-11-18
---

# 同步转写的租约结算

同步转写（`POST /v1/audio/transcriptions`）的容量由 API 进程内的**独立 ASR 结算运行时**归还。该运行时只做一件事：把网关已经持久化的执行终态交给唯一的 `LeaseService.release`
规则，由那条规则在加锁事务里判定并归还本租约的增量。它自己不判定终态、不隔离、不发 abort/drain、不写执行事实。每次同步转写准入在**同一个事务**里写一条持久结算根（`speech_asr_settlement`），因此进程崩溃、网关应答丢失或客户端断连之后，结算仍可重放。

本文定义目标行为与判据。当前 `backend/src` 没有同步转写的结算运行时，`docs/operations/compose-minimal.md` 仍把「成功后不自动释放」写成已知限制（[#974](https://github.com/hrygo/SpeechNexus/issues/974)）；落地顺序与提交单元见[实施计划](../plans/2026-11-18-sync-transcription-settlement.md)。

## 1. 问题与既有事实

`DEL-540` 在参考部署形态上取得同步转写真实 `200`，同轮测得租约没有结算：

| 观察项 | 实测值 | 应有值（成功结算后） |
| --- | --- | --- |
| `admission_leases.lease_state` | `reserved` | `released` |
| `admission_leases.cleanup_state` | `pending` | `complete` |
| `admission_leases.budget_released_at_us` | `NULL` | 释放事务的数据库时间 |
| `engine_instances.held_slots` | `1`（`max_slots=1`） | `0` |

`cleanup_state=pending` 本身不是缺陷：它正是 `LeaseService.release` 的输入形态，释放事务把它改成
`complete`（`backend/src/speechnexus_backend/speech/infrastructure/lease_release_sql.py:262`）。缺的是**调用者**，不是证据：

| 事实 | 谁已经写了 | 出处 |
| --- | --- | --- |
| 执行以成功终态收尾，并写入 `engine_terminal_response` 停止与释放双证据 | 执行网关的 file worker | `execution-gateway/src/speechnexus_gateway/infrastructure/file_execution_worker.py:380` → `execution-gateway/src/speechnexus_gateway/application/execution_completion.py:55` → `execution-gateway/src/speechnexus_gateway/infrastructure/stop_sql.py:62` |
| 释放只扣本租约增量、CAS 更新 host/instance/attempt/quota/lease | 仓库已有的释放事务 | `backend/src/speechnexus_backend/speech/infrastructure/lease_release_sql.py:30` |
| 释放的授权判据 | 仓库已有的领域规则 | `backend/src/speechnexus_backend/speech/domain/lease_release.py:121` |
| 同步转写的准入装配 | `build_transcription`，没有任何结算运行时 | `backend/src/speechnexus_backend/transcription_runtime.py:56` |

同步转写的操作 id 在 `speech_jobs` 里没有对应行：准入事务写的是 `coordination_jobs` / `coordination_outbox`
（`backend/src/speechnexus_backend/speech/infrastructure/admission_sql.py:337`、`backend/src/speechnexus_backend/speech/infrastructure/jobs_sql.py:70`），
其 `job_id` 是 `job-…`，而租约的 `operation_id` 是 `operation-…`（`backend/src/speechnexus_backend/speech/infrastructure/transcription_admission.py:292`）。
因此运维出口 `python -m speechnexus_backend.completed_release_cli` 的识别快照按
`speech_jobs.job_id = admission_leases.operation_id` 关联（`backend/src/speechnexus_backend/speech/infrastructure/quarantine_release_postgres.py:117`），
读到的 `job_state`、围栏、对账账本三列全为空，工具在 `_require_accepted_cancellation` 处以
`completed_release_not_accepted_cancellation` 拒绝（`backend/src/speechnexus_backend/speech/domain/completed_release.py:292`）。
这与实测一致：该出口不接受同步转写，不是用法问题。

## 2. 由谁发起释放

### 2.1 三个候选与逐项对比

| | A. 新增 ASR 结算 worker（照 TTS 形态） | B. 扩展 `completed_releases` | C. API 请求路径内联释放 |
| --- | --- | --- | --- |
| 释放身份 | 专用受限非 owner 身份（沿用 `sn_speech_settlement`），启动时逐列核对边界 | 操作员身份 `sn_quarantine_release`，按次人工执行 | API 运行身份 `sn_api` |
| 权限后果 | 不扩大 API 身份；root 写入只需新表的列级 `INSERT`/`SELECT` | 保留原出口，不新增权限面 | 把释放决定交给宽运行身份；若坚持边界自检，就要在请求路径再开一个受限池 |
| 可重放性 | root 是持久工作项，at-least-once 扫描；释放版本 CAS；崩溃后重放 | 证词摘要 + 账本追加；重放依赖操作员重新提交同一证词 | 无持久工作项；200 之后进程崩溃/断连即永久泄漏 |
| 未知结局 | 网关不可用 → defer，保持占用；恢复后同一 root 重放 | 需要操作员重新取 drain 并重算证词；无法自动重试 | 响应已经发出，进程内没有第二次机会 |
| 与 TTS 规则一致性 | 同一条 `LeaseService.release`、同一 CAS、同一 fail-closed 装配口径 | 是另一条证词规则，明确不吞并完成判据 | 同一条规则，但触发点与 TTS 不同，且没有持久触发记录 |

**选定 A。** 同步转写的结算与同步 TTS 是同一类问题：准入写租约，执行方在网关侧留下终态与停止证据，缺一个把终态送回释放规则的持久触发者。A 复用 TTS 已经验证过的全部形状：准入事务内写 root、受限身份、独立 DSN、扫描 + defer、启动 fail-closed。新增的只有扫描对象与 root 表。

### 2.2 被否掉的 B：为什么不能扩展现有 `completed_releases`

1. **它的前置与本形状矛盾。** 这个出口在代码里要求「已受理取消且仍在进行中」：`completed_release_not_accepted_cancellation`（无围栏）、`completed_release_already_settled`（对账已结算）、`completed_release_not_cancelling`（Job 不再 `cancelling`）三条拒绝各自对应一个事实（`backend/src/speechnexus_backend/speech/domain/completed_release.py:292`）。同步转写没有 `speech_jobs` 行、没有取消围栏、操作 id 是 `operation-…`；要接受它，必须删掉这三条判据中的至少一条。那三条正是该出口存在的理由：它只关闭「取消已受理、执行却已完成」这一种债。
2. **它是操作员证词，不是自动结算。** 每次成功转写都要人工取一次网关 drain、写一份 `completed-release-evidence/v1` 证词、再执行一次 CLI。drain 是对**实例代次**「可以停止」的断言，对健康实例反复 drain 会误伤同实例上仍在服务的其它执行；把它变成每条成功请求的常规路径，等于用停服证据换容量。
3. **一条规则会接受两种互不覆盖的授权。** `completed_releases.py` 的模块注释把边界写死：未知执行的隔离释放与已完成执行的证词释放是「two services over one ledger, never one rule that accepts both」。要接受同步转写，`require_completed_releasable` 就必须同时接受「公开 Job 的已受理取消 + drain」和「同步转写没有取消 + drain」两种形状，而证词摘要与重放只对应其中一种。这不是扩展现有出口，而是把它变成两条规则共用一扇门。

### 2.3 被否掉的 C：为什么不能内联释放

1. **没有持久工作项。** 释放必须发生在网关成功之后；如果进程在响应写出后、释放提交前退出，或客户端断连导致请求协程被取消，就没有任何记录会再次尝试。`#974` 的形态正是「网关已经成功、A 侧没有归还」，内联释放把同一个窗口从「没有调用者」换成「调用者退出后没有重试」，并没有消除它。
2. **把结算事务放进请求尾。** 释放要对 `resource_pools`、`workspace_quotas`、`host_nodes`、`engine_instances` 逐行 `FOR UPDATE`
   （`backend/src/speechnexus_backend/speech/infrastructure/lease_release_sql.py:45`），这些正是准入事务的热行；在请求路径里执行会把每个成功转写与同池准入串行化，并让客户端响应等待结算提交。
3. **权限面不成立。** API 运行身份是宽身份；TTS 的结算刻意使用受限非 owner 身份，并在启动时逐列核对授权边界（`backend/src/speechnexus_backend/tts_settlement_worker.py:90`）。内联释放要么让宽身份做结算决定、失去这层自检，要么在请求路径再开受限池，连接预算与延迟都变差。
4. **与 TTS 不一致。** 同步 TTS 的成功结算不在请求路径里，而在独立运行时；两条同步入口用两种触发模型，会把「成功即释放」变成两条需要分别验证的规则。

### 2.4 权限结论

API 运行身份**不新增**释放授权。它只需要在准入事务里写 settlement root 的列级 `INSERT`（以及插入后精确回读的列级 `SELECT`），这部分与 TTS 的 `speech_tts_settlement` 写入授权同形（`backend/src/speechnexus_backend/speech/infrastructure/tts_settlement_schema.py:6`）。真正的释放动作在受限身份下执行，其授权来自既有的 `disable_settlement_grants`（`backend/src/speechnexus_backend/speech/infrastructure/binding_disable_targets_schema.py:84`）。

## 3. 持久结算根

### 3.1 表

```text
speech_asr_settlement(
  lease_id          VARCHAR(128) PRIMARY KEY REFERENCES admission_leases(lease_id),
  operation_id      VARCHAR(128) NOT NULL,
  attempt_id        VARCHAR(128) NOT NULL,
  gateway_id        VARCHAR(128) NOT NULL,
  host_node_id      VARCHAR(128) NOT NULL,
  next_check_at_us  BIGINT NOT NULL DEFAULT 0 CHECK (next_check_at_us >= 0),
  settled_at_us     BIGINT CHECK (settled_at_us > 0),
  FOREIGN KEY (operation_id, attempt_id) REFERENCES admission_attempts(operation_id, attempt_id)
)
-- 到期扫描：CREATE INDEX ... ON speech_asr_settlement (next_check_at_us, lease_id)
--           WHERE settled_at_us IS NULL
```

root 只回答「这条租约由谁负责结算」，不是证据：它不含执行终态、停止证据、命令正文或命令摘要。
释放事务每次都按加锁后的 `admission_leases`、`submit_intents`、`execution_stop_evidence`、
`engine_instances` 重读事实；root 的字段不被当作任何授权依据。因此 root 可以安全地只存路由与身份，
不会出现「root 说有停止证据」这种需要信任的副本。

### 3.2 写入点

root 在**准入事务内**写入，与 `admission_operations`、`admission_attempts`、`admission_requests`、
`admission_leases`、配额/节点/实例计数、Job/outbox 同一次提交（`backend/src/speechnexus_backend/speech/infrastructure/admission_sql.py:244`）。
写入通过 `PostgresAdmissionTransactions._record_settlement` 的 ASR 子类完成；插入后在同一事务内回读整行并逐列比对，
不一致即 `reconciliation_required` 并回滚整个准入（TTS 的 `insert_tts_settlement` 用同一做法，
`backend/src/speechnexus_backend/speech/infrastructure/tts_settlement_sql.py:73`）。判据：

- **A-ROOT-1**：一次提交的准入必然留下且只留下一条与租约同身份的 root；缺 root 的已持有租约只能来自
  迁移前历史或写入缺陷，启动 preflight 拒绝。
- **A-ROOT-2**：root 冲突（同一 `lease_id` 已存在但 `operation_id`/`attempt_id`/路由不同）必须让准入失败，
  不得 `DO NOTHING` 后继续。

### 3.3 历史租约

迁移为所有**仍被持有**（`admission_leases.lease_state <> 'released'`）的同步 ASR 租约补 root：
按 `admission_operations.request_document::jsonb->>'kind' = 'asr_file'` 与 `operation_id LIKE 'operation-%'`
识别，`next_check_at_us = 0`。部署后第一次扫描就会处理 `#974` 残留的那条已成功租约；不需要人工 SQL。
迁移不回填已释放租约，也不为公开 Job（`job-…`）或资格探针（`builtin_voice_qualification_probe`）写 root。

### 3.4 路由

root 存 `gateway_id`/`host_node_id`，来自准入阶段冻结的 stage（`backend/src/speechnexus_backend/speech/infrastructure/transcription_admission.py:226`）。
worker 只扫描自己 `(gateway_id, host_node_id)` 的 root，因为它的组件凭据只能查询自己那个网关。
路由不再匹配任何已配置 worker 的 root 是运维债：不会被静默结算，也不会被另一个网关的 worker 误结算。

## 4. 释放的前置证明

### 4.1 原则

**worker 没有自己的释放判据。** 它只做两件事：读网关的终态（决定「现在是否值得尝试」），以及带着
租约版本调用 `LeaseService.release`。真正的证明全部发生在释放事务的锁内，由既有规则给出
（`backend/src/speechnexus_backend/speech/domain/lease_release.py:121`）。这条原则保证：新增的触发者
不能绕过任何一条已有证据要求，也不能把「读到成功」当成「可以归还」。

### 4.2 必须同时成立的事实

| # | 事实 | 谁在什么锁下证明 | 缺一条时 |
| --- | --- | --- | --- |
| 1 | root 未 `settled`、到期、路由与本 worker 匹配 | 扫描查询（只读） | 不扫描；保持占用 |
| 2 | 租约存在且 `lease_state='reserved'` | 释放事务对 `admission_leases` 加 `FOR UPDATE` | 租约已 `released`：直接 `mark_settled`，不再释放；租约不存在：defer 并报债，不标记 |
| 3 | 冻结身份一致：解锁读到的租约与锁定后的租约在 `lease_id`/`operation_id`/`attempt_id`/`stage_id`/`pool_id`/`host_node_id`/`instance_id`/`instance_generation`/`gateway_id`/`fencing_token`/`reserved_*` 上逐列相等；实例的 host/pool/gateway 与租约相等；attempt 的 workspace 与解锁读相等 | 释放事务先读 hint，再按 pool→quota→host→instance→attempt→lease 加锁并比对 | `reconciliation_required`，事务回滚，root 保持未结算 |
| 4 | 该租约的提交意图恰好 0 或 1 条，且租约/操作/尝试三元组一致 | 释放事务锁 `admission_leases` 后读 `submit_intents ... FOR UPDATE` | 多于 1 条 → `reconciliation_required`；0 条时释放允许 |
| 5 | 执行终态：`succeeded`/`failed`/`aborted` 且 `cleanup_state='complete'`；或 `failed` + `not_required` 且网关结构证明未派发 | 领域规则 `release()` | `execution_cleanup_pending`，保持占用 |
| 6 | 停止证据恰好一行，`execution_stopped=1`、`resources_released=1`、`observed_at_us <= now`；`succeeded` 的证据种类必须是 `engine_terminal_response` | 释放事务读 `execution_stop_evidence`，`now` 取最后一把锁之后的数据库时间 | 拒绝并保持占用；不补造证据、不放宽种类 |
| 7 | attempt 的全部 stage 租约都已释放，才结算 attempt 配额 | 释放事务锁 attempt 后读同 attempt 的兄弟租约 | 未全部释放则只扣本租约增量，配额留到最后一个租约 |
| 8 | 实例代次存在：`engine_instances (instance_id, instance_generation)` 行在，且 host/pool/gateway 与租约一致 | 释放事务对该行 `FOR UPDATE` | `reconciliation_required`，保持占用 |
| 9 | 五处版本 CAS 全部命中：host、instance、attempt、quota、lease | 释放事务的条件 `UPDATE ... AND version = %s` | 任一处未命中整体回滚，不产生部分释放 |

第 4 条的 0 条分支是既有规则的一部分，不是本机制的新授权：租约锁在 `create_submit` 之前或之后取得，
所以「没有提交意图」等于「没有任何已提交的网关执行」，而不是「有执行但没读到」。

同步转写是单 stage（`backend/src/speechnexus_backend/speech/infrastructure/transcription_admission.py:226`），
所以第 7 条在本形状里等价于「本次释放同时结算 attempt 配额」：`other_leases_released` 对空集合为真。
这不是特例分支，而是既有规则在单 stage 上的结果。

### 4.3 网关查询的地位

worker 在调用释放前查询一次网关执行状态（`GatewayExecutionClient.query(submit_intent_id)`）。
`QueryTtsTarget` 式的完整 target 不需要：root 只提供租约，`submit_intent_id` 从本租约唯一的
`submit_intents` 行读取（结算身份已持有该列的 `SELECT`）。没有提交意图时没有可查询的原身份，
直接调用释放；释放规则允许该分支，并会在租约已隔离时拒绝。查询结果只用于选择动作：

- `succeeded`/`failed`/`aborted`：尝试释放；释放规则按证据裁决，拒绝即 defer。
- `unknown_quarantined`：不是终态，不尝试释放（§5）。
- 缺失、超时、连接失败、`not_found`：defer，保持占用。**缺失不是停止证明。**

### 4.4 不变量

- **A-REL-1**：没有一条释放能绕过 `release()` 的任一判据；worker 不直接更新 `admission_leases`。
- **A-REL-2**：任何一次拒绝都保持 `lease_state='reserved'` 与全部计数不变；不存在部分释放或「先扣后补」。
- **A-REL-3**：`budget_released_at_us` 只由释放事务写入，且不早于停止证据的 `observed_at_us`。
- **A-REL-4**：`settled_at_us` 只在对应租约已经是 `released` 时写入；反向不成立（已释放不等于已标记）。
- **A-REL-5**：网关的未知、缺失、拒答不改变任何账目。

## 5. 未知与失败形状

| 形状 | 归谁管 | 本机制的动作 | 可见面 |
| --- | --- | --- | --- |
| `unknown_quarantined`（执行未知） | 隔离 + 隔离释放：`LeaseService.quarantine` 把租约移入隔离桶，`QuarantineReleaseService` 的 `operator_verified_instance_terminated` 证词才能解除 | 不隔离、不释放、不写执行事实；root 保持未结算 | 管理诊断 `asr_settlement_debt` 的 `unknown` 类（§9.2）；同步转写目前没有隔离推进者，这是本设计范围外的缺口 |
| `execution_cleanup_pending`（终态缺停止/清理证据） | 网关停止证据修复（`#338`） | defer，保持占用 | 同上；detail 点名执行状态 |
| `failed`（执行失败） | 失败结果路径拥有「失败意味着什么」；本机制不新增分支 | 只把已持久终态交给共享 `release()`：`not_required` 且证明确未派发时可释放，否则拒绝并 defer | 同上 |
| `aborted`（执行中止） | 取消路径拥有「中止意味着什么」 | 同上：只有停止与释放双证据齐全时由 `release()` 放行 | 同上 |
| `succeeded` 但无 `engine_terminal_response` | `#338` | 拒绝并 defer，不拿操作员证词替代 | 同上 |
| 已 `released` 但 root 未标记 | 本机制 | 下一轮直接 `mark_settled`，不再触账 | 无债 |

「不得碰」在本设计里落成一条可检查的约束：**本机制没有任何按这些状态名决定授权分支的代码路径。**
它不新增对 `failed`/`aborted` 的授权、不调用 `quarantine`、不调用 abort/drain、不创建新执行；
状态名只用于「是否尝试调用释放」这一处终态判断（`succeeded`/`failed`/`aborted` 是终态，
`unknown_quarantined` 不是），授权仍由唯一的释放规则给出。`unknown_quarantined` 不进入释放尝试，
也就不会走到隔离释放的证据要求上。

## 6. 幂等与并发

### 6.1 同一租约被 worker 与运维工具同时处理

两条路径最终都调用 `release_in_transaction`，用同一个租约版本做 CAS：

- worker 先提交：租约变 `released`。运维工具的 `completed_release_in_transaction` 读到已释放后进入
  `_branch_f`，要求存储的停止证据与操作员证词四字段一致（`backend/src/speechnexus_backend/speech/infrastructure/quarantine_release_sql.py:116`）。
  操作员证词的 `evidence_kind` 是 `operator_verified_instance_terminated`，与存储的
  `engine_terminal_response` 不同，因此得到 `quarantine_release_conflict`。这是正确结果：第二次、不同的证词
  不能记成原始释放的依据。
- 运维工具先提交：租约已 `released`。worker 下一轮读到 `released`，直接 `mark_settled`，不再触账。

两种顺序都只产生一次释放，账目只减一次。

### 6.2 崩溃与重放

- 提交前崩溃：租约与 root 都没变，下一轮重放同一 root。
- 提交后、`mark_settled` 前崩溃：租约已 `released`、root 未标记；下一轮读租约 → `mark_settled`。
- `mark_settled` 的 SQL 只更新 `settled_at_us`，条件 `WHERE lease_id=%s AND settled_at_us IS NULL`，
  重复执行不改变时间（TTS 同形，`backend/src/speechnexus_backend/speech/infrastructure/tts_settlement_postgres.py:109`）。
- 释放结果为 `unknown`（提交确认丢失）：先重读租约；已释放则按完成处理，未释放则 defer，下一轮用新版本重试。
  不重复外发、不新建租约。

### 6.3 多个 worker / 多副本

`due()` 是只读查询，不加行锁，也不认领 root。两个进程取到同一条 root 时：都调用 `release()`，版本 CAS
只让一个提交；另一个读到 `version_conflict` 或 `replayed`，重读租约后按已释放收口。路由过滤把竞争限制在
同一 `(gateway_id, host_node_id)` 内，但不承担正确性。重复扫描的代价是有界的网关查询，不是重复扣账。

### 6.4 结算周期与预算时钟

| 量 | 取值 | 判据 |
| --- | --- | --- |
| 扫描周期 | `SPEECHNEXUS_ASR_SETTLEMENT_POLL_SECONDS`，默认 1，范围 0.1–300 | 非正或越界拒绝启动 |
| 批大小 | `SPEECHNEXUS_ASR_SETTLEMENT_BATCH_SIZE`，默认 32，范围 1–128 | 越界拒绝启动 |
| `next_check_at_us` / `settled_at_us` | 数据库 `clock_timestamp()` | 不使用进程时间；defer 量等于一个扫描周期 |
| `budget_released_at_us` | 释放事务提交前的数据库时间 | 规则要求不早于停止证据 `observed_at_us` |
| `deadline_at_us` | 准入时冻结，租约上的原值 | 只作运维债判据（过了 deadline 仍被持有 = 债）；**不参与释放授权**：迟到的成功仍必须归还容量 |

结算周期是**容量可见性的上界**，不是授权的一部分：一个成功执行从网关落终态到 `held_slots` 下降，
最坏多等一个扫描周期加一次释放事务。这个上界只在 worker 运行且 preflight 通过时成立；worker 没启动时
管理诊断的 `asr_settlement_debt` 与 preflight 负责把「没有推进者」暴露出来，而不是让容量静默消失。

## 7. 配置与权限面

### 7.1 身份与 DSN

沿用同步 TTS 的结算身份与 DSN：

| 项 | 值 |
| --- | --- |
| 环境键 | `SPEECHNEXUS_SPEECH_SETTLEMENT_DATABASE_URL`（容器内即此名） |
| 角色 | `sn_speech_settlement`：非 owner、无 `SET ROLE`、无 `CREATEROLE`/`SUPERUSER`/`BYPASSRLS`，PostgreSQL 16+ |
| 复用理由 | `disable_settlement_grants` 已经给出**通用**的释放边界（对任何 admission lease 的释放列级授权）。新增第二个角色只会复制同一套通用释放授权，扩大而不是缩小 blast radius；root 表只增加工作项生命周期，不增加释放能力 |
| 不放宽的部分 | 两个 worker 的启动自检都逐列比对「允许的额外表」；共用同一角色后，两边的允许集合都是 `speech_tts_settlement`/`speech_tts_cancellation`/`speech_asr_settlement` 三张表，任何额外的表级/列级权限仍拒绝启动 |
| 残留边界 | TTS 进程因此也能写 ASR root 的 `settled_at_us`。该写不归还容量，且被 §7.3 的「标记不撒谎」检查与债探针捕获；要求两个 worker 互相不可写就要第二个角色/DSN 与两条 provisioning 路径，本设计不采用 |

### 7.2 Grants

| 声明 | 作用对象 | 内容 |
| --- | --- | --- |
| `asr_settlement_writer_grants(schema, "sn_api")` | 准入身份 | 新表列级 `SELECT` 与 `INSERT`：`lease_id, operation_id, attempt_id, gateway_id, host_node_id, next_check_at_us, settled_at_us` |
| `asr_settlement_worker_grants(schema, role)` | 结算身份 | 新表 `SELECT` 与列级 `UPDATE (next_check_at_us, settled_at_us)` |
| `disable_settlement_grants(schema, role)` | 结算身份 | 既有释放边界（`resource_pools`、`workspace_quotas`、`host_nodes`、`engine_instances`、`admission_attempts`、`admission_leases`、`submit_intents`、`execution_stop_evidence`、release guard 视图） |

没有 `DELETE`、`TRUNCATE`、`REFERENCES`、DDL 或 schema 级 `CREATE`；root 行不删除，历史随释放保留。

### 7.3 启动自检（preflight）

按 TTS 的三段式：

1. **可读性**：root 表存在。缺失时抛命名错误并给出迁移命令（对照
   `backend/src/speechnexus_backend/speech/infrastructure/tts_settlement_preflight.py:93`），不让驱动的
   `UndefinedTable` 变成重启循环的 traceback。
2. **容量有主**：每个仍被持有的同步 ASR 租约都必须有 root。缺 root 说明准入写入缺陷或历史未回填，
   拒绝启动，由运维按原身份对账后再起。现有 `require_complete_tts_roots` 的两个分支只覆盖同步
   TTS/资格探针与公开 Job（`job-…`）；同步 ASR 是 `operation-…` 且 `kind='asr_file'`，两个分支都不匹配，
   所以升级前的部署能带着 `#974` 的租约启动。新检查补的正是这个缺口，并复用同一个
   `SettlementPreflightUnavailableError` 与迁移命令。
3. **标记不撒谎**：任何 `settled_at_us IS NOT NULL` 的 root，其租约必须已经是 `released`。反向不检查
   （已释放未标记是正常中间态，worker 会补标记）。这条拒绝「标记成功但容量仍被持有」的实现缺陷。

此外，worker 在打开自己的连接池后核对身份：两个池必须是同一数据库、同一 schema、**不同角色**；
结算角色不能有 `SET ROLE`、角色管理或预定义角色权限（沿用 `_verify_no_role_switch` 与
`_verify_pool_pair` 的判据，`backend/src/speechnexus_backend/binding_disable_worker.py:200`、`:362`）；
逐列权限核对改用 TTS worker 的同一函数，允许集合为三张结算表（§7.1）。
preflight 的只读检查跑在准入身份上（需要读 `admission_operations.request_document` 识别 `asr_file`），
worker 再用准入身份重跑一次，确保两个池指向同一套事实。

### 7.4 参考部署

不需要新服务、新容器或新 profile：worker 在 API 进程内启动，与同步 TTS 的结算运行时同形
（`backend/src/speechnexus_backend/speech_runtime.py:88`）。需要的变更只有：

- 迁移新建 `speech_asr_settlement`；
- 参考部署的两条 provisioning 路径（fresh install 与已有部署升级）都给 `sn_speech_settlement`
  追加 `asr_settlement_worker_grants`，并给 `sn_api` 追加 `asr_settlement_writer_grants`；
- `deploy/README.md` 里 “The `sn_speech_settlement` role and its two grants” 的表述改为三个；
- `SPEECHNEXUS_SPEECH_SETTLEMENT_DATABASE_URL` 已在 `deploy/compose.yaml` 中转发，不新增键。

### 7.5 漏配时的启动语义

没有单独的 `ENABLED` 开关：装配条件与结算条件是同一个结算 DSN。不存在「转写可用但结算停止」的配置，
因为那正是 `#974` 要消灭的可达状态。

| 状态 | 结果 |
| --- | --- |
| `SPEECHNEXUS_SPEECH_SETTLEMENT_DATABASE_URL` 未设置或为空 | 同步转写不装配：`POST /v1/audio/transcriptions` 返回 `503`，启动日志点名该键；API 其余路由照常启动 |
| DSN 已配置但表缺、权限不符、身份过宽、preflight 拒绝 | API 启动失败，错误只含键名/类别，不含 DSN 或凭据 |
| 准入身份缺 `INSERT` 授权 | 准入事务整体回滚，请求失败（不产生「有租约无 root」的半成品）；修复是重放 provisioning 授权 |

这与 TTS 的既有口径逐条相同：缺配置时特性关闭而不是带病启动；配置了但起不来时整体拒绝启动
（`backend/src/speechnexus_backend/speech_runtime.py:67`、`:117`）。

## 8. 可撤销性

### 8.1 怎么停

唯一安全的停法是让**准入与结算一起停**：把结算 DSN 置空/失效，或直接停 API 容器。前者使
`build_transcription` 返回 `None`，转写路由 `503`，worker 不启动；后者同时停掉两者。
不存在「保持转写、只停扫描」的开关：那会恢复 `#974` 的泄漏形态，而没有任何推进者。停服务不删除
root、租约或证据；恢复 DSN 后按未结算 root 继续。

### 8.2 已释放的预算不能重算

已提交的释放不能撤销，原因是可核对的，不是策略选择：

- 释放只做**增量扣减**：host/instance 的 held 向量、attempt/quota 计数按本租约的 `reserved_*` 减一次，
  没有任何地方保存扣减前的快照；`admission_leases` 行只留 `budget_released_at_us`。
- 状态机不接受 `released → reserved`：仓库没有任何把 `lease_state` 写回 `reserved` 的代码路径，
  任何写回都会绕过版本 CAS 与计数投影。
- 重放同一释放不是补偿：`release_already_recorded` 只校验并返回原事实，不再触账；
  对已释放租约调用 `release()` 得到 `replayed`，不会把容量再加回去。
- 因此「误释放」的可恢复动作只有**向前**：容量已经可用，重新准入即可；如果误释放源于错误的停止证据，
  那是网关证据缺陷，按 `#338` 的路径处理受影响的实例代次，而不是在账本上做反向分录。
  需要反向分录的场景应作为新的、带独立账本的设计另立，不并入本机制。

### 8.3 上线后的观察窗

参考部署升级后，先只跑一次 `--once`（不启动周期扫描），核对 `#974` 残留租约在**没有人工 SQL**的情况下
被结算：`lease_state='released'`、`cleanup_state='complete'`、`budget_released_at_us` 非空、
`engine_instances.held_slots=0`，且第二次同步转写返回 `200`。这一步通过后再启用周期扫描。
`--once` 是受控窗口，不是常规运行方式。

## 9. 验收与断言

### 9.1 本地（隔离 PostgreSQL，合成数据）

| 断言 | 用例形态 |
| --- | --- |
| A-ROOT-1/2 | 准入提交后 root 与租约同身份；同一租约冲突 root 使准入回滚，无租约残留 |
| 成功结算 | 成功终态 + `engine_terminal_response` → 一轮扫描后租约 `released`、五处计数各减一次、root `settled` |
| 无证据不释放 | `succeeded` 但缺停止证据 → 零释放、零计数变化、root 未结算、defer |
| 未知不碰 | `unknown_quarantined` → 不隔离、不释放、不标记 |
| 迟到成功 | 过了 `deadline_at_us` 才成功 → 同一轮释放（授权不看 deadline） |
| 已释放先到 | 先由运维/其它写者释放 → worker 只 `mark_settled`，释放调用次数为 0 |
| 崩溃重放 | 提交前崩溃 → 重放；提交后崩溃 → 只补标记 |
| 双 worker | 同一 root 被两次扫描 → 只提交一次，另一个按已释放收口 |
| 历史回填 | 迁移前持有的成功租约 → 迁移后一轮扫描结算；第二轮回填不产生第二行 |
| 重复 `--once` | 同一成功租约连跑两次 → `budget_released_at_us` 与计数都不再变化 |
| 启动拒绝 | 缺表/缺 root/settled-but-held/身份过宽/同角色 → 启动失败且错误不含凭据 |

### 9.2 参考部署（真机）

1. 升级后 `--once` 结算 `#974` 残留租约（§8.3 的四项读数 + `workspace_quotas.held_attempts` 回到 0 +
   第二次 `200`）。
2. 启用周期后，再跑一次成功转写：一个扫描周期内 `held_slots` 回到 0，无需任何命令。
3. 把结算 DSN 置空重启：转写 `503`，`held_slots` 不再增长；恢复后继续结算。
4. 杀 worker（停 API 再起）：重启后未结算 root 被继续处理，已结算 root 不被重复扣账。
5. 并发：同一实例上连续两次成功转写，第二次不被容量拒绝。

### 9.3 可见面（管理诊断）

`/v1/nexus/admin/diagnostics` 的 `asr_settlement_debt` 报告四类计数与有界明细：

| 类别 | 判据 | 含义 |
| --- | --- | --- |
| `unknown` | root 未 settled、租约仍被持有、`deadline_at_us` 已过、网关终态是 `unknown_quarantined` | 等隔离/隔离释放，本机制不碰 |
| `cleanup_pending` | 同上，但执行终态缺停止/清理证据 | 等网关停止证据修复 |
| `route_mismatch` | root 的 `(gateway_id, host_node_id)` 不是任何已配置 worker 的路由 | 没有推进者；运维债 |
| `settled_but_held` | root `settled_at_us` 非空而租约仍非 `released` | 实现缺陷；应恒为 0 |

该 detail 始终 `ok`，不进入 `/readyz` 的失败判据：`/readyz` 只反映依赖（数据库、对象存储等）是否可用，
债是「依赖可用但没有推进者」的运维事实，应由诊断暴露并告警，而不是把 API 判成不健康。

### 9.4 不在本机制内

- 不发 abort/drain：执行停在非终态时，容量保持占用，归停止/取消路径。
- 不推进 `unknown_quarantined` 的隔离：同步转写目前没有隔离推进者，这是独立缺口。
- 不做多节点 HA、跨网关迁移或引擎代次晋升；这些场景下 root 的 route 失配按运维债处理。

## 10. 与 TTS 的差异清单

| 项 | 同步 TTS | 同步 ASR |
| --- | --- | --- |
| root 表 | `speech_tts_settlement` + 取消表 | `speech_asr_settlement`（无取消表） |
| root 目标 | 完整 gateway target（查询/abort 需要） | 租约身份 + 路由；`submit_intent_id` 从 `submit_intents` 读 |
| 触发 | 终态、超时 abort、取消 | 仅终态；不 abort |
| 与租约的界面 | `LeaseService.release` | 同一条 `LeaseService.release` |
| 身份 | `sn_speech_settlement` | 同一个身份（新增一张表的列级授权） |
| 失败形状 | 隔离 + 隔离释放 | 同左，但同步 ASR 的隔离推进者不在本设计内 |

两条链路共享释放规则、版本 CAS、受限身份与启动口径；差异只在「谁触发」和「root 里放什么」。