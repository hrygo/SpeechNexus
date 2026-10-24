---
title: 执行网关：提交、屏障与未知执行恢复
status: active
version: "1.10.0"
date: 2026-10-24
---

# 执行网关：提交、屏障与未知执行恢复

网关的第一个生产事务是 `create_submit`：先持久化唯一 intent，再向外发引擎请求；重放沿用原
intent，不能再次外发。PostgreSQL adapter 已按[应用事务方案与契约](coordination-transactions.md)
的锁序、摘要比较和未知结果规则实现，使用[生产协调存储基线](production-coordination-storage.md)
冻结的表与网关最小权限角色。`record_stop` 已原子保存停止证据与执行终态；
`GatewayIngress` 已将完整外发段与入口屏障串行，`seal_barrier` 已保存实际封锁后的最低 token。
`mark_execution_unknown`、`record_execution_observation`、受信命令正文清理
与自身 submit/interval 分支的 `query_outcome`、`append_interval` 已实现；
认证 HTTP 屏障、只读执行查询与提交/中止路由已接入生产组合根，但提交与中止要真正外发引擎，
仍需在组合根注入引擎 adapter。真实计量采集器与 `drain` 仍待后续单元。本页区分已实现、已验证与未交付。

语音处理侧另有 [CPU VAD](cpu-vad.md)、[有界文件媒体](file-asr-audio.md)和
[原生文件识别](file-asr-recognition.md)：已实现 VAD 门禁、源样本时钟与真实 vLLM 调用，
通过固定模型/授权录音探针。它们尚未装配公开实时入口或持久 submit dispatch；
不改变本页的提交与停止证据、预算和屏障语义。

## 1. 代码位置与依赖

| 职责 | 位置 | 约束 |
| --- | --- | --- |
| 请求身份、状态/清理矩阵、停止证据裁决、租约校验规则 | `speechnexus_gateway.domain.submission` | 纯值对象与函数；不导入 SQL、驱动或生成 DTO |
| 提交/停止应用服务、重放与隔离对账 | `speechnexus_gateway.application.submit_execution` | 通过 domain 端口访问外部系统，不自己发 SQL |
| 未知转换与只读执行快照 | `domain.recovery`、`application.query_execution` | 独立 recovery/query 端口；查询不调用引擎、不触发对账或状态转换 |
| 观察裁决与受信事务边界 | `domain.observation`、`application.observe_execution`、`interfaces.observation_transactions` | 完整身份、版本与状态检查；生成契约校验与完整 TransactionResult，不新增 HTTP 路由 |
| 提交/停止/未知的受信事务结果 | `interfaces.submit_transactions`、`interfaces.execution_payloads` | 与 HTTP 提交共用一份冻结请求翻译；重复事务报 `replayed`，终态不被降级，仍不释放预算 |
| 命令正文清理与许可端口 | `domain.command_redaction`、`application.redact_execution_command`、`interfaces.command_redaction_transactions` | 必须显式接入可信删除协调器；仅清正文并原子留 tombstone，不提供默认许可或 HTTP 入口 |
| 输出区间追加与原事实查询 | `domain.output_intervals`、`application.output_intervals`、`infrastructure.output_interval_sql`、`interfaces.output_interval_transactions` | 跨阶段/资源池按逻辑流串行；权威用量是区间长度，不是最大水位；受信原生入口，无 HTTP 计量路由 |
| 入口排序、屏障请求与证明 | `speechnexus_gateway.application.ingress`、`domain.fencing` | 同一个 guard 覆盖落 intent、完整外发握手与结果保存；网络期间不占数据库事务 |
| 屏障控制用例与 HTTP 边界 | `application.barrier_control`、`interfaces.http.{barrier_control,control_schemas,control_errors,control_security}` | 专用组件凭据；契约生成类型、离线校验和同源 OpenAPI；不伪造引擎服务 |
| 端口定义（`SubmissionLedger`、外发、观察） | `speechnexus_gateway.domain.ports` | 实现放 infrastructure 或测试 fake |
| SQL 语句、锁序、列映射与错误分类 | `speechnexus_gateway.infrastructure.{submit_sql,stop_sql,barrier_sql,recovery_sql,submit_postgres,postgres_pool}` | 一次调用一个短事务；不复制预算或证据策略 |
| 同宿主网关进程独占 | `speechnexus_gateway.infrastructure.process_ownership` | Linux/POSIX 文件锁；同身份的候选进程必须共用宿主锁目录 |
| 组合根与运行入口 | `speechnexus_gateway.app`、`main` | 显式配置后在 lifespan 中取得宿主进程锁并打开数据库池；默认只装配运营健康检查 |

网关是与 speech backend 独立的部署，因此不导入 backend 包；两边重复的只是已评审的池策略
（Read Committed、UTC、显式 `search_path` 且 `pg_temp` 置后、有界等待队列、导入时不连接）。

## 2. `create_submit` 事务

一次 `add(intent, request_document=…)` 就是一个 `create_submit`：

入口只接受 version=1 的新 submitting/pending intent：尚无外发、错误、输出或停止事实，
创建与更新时间一致。不能通过首次 INSERT 直接导入 executing、终态或 not_required 结论。

1. 按全局顺序取行锁：`resource_pools` → `admission_attempts` → `admission_leases`，每条语句
   都通过普通 `SELECT … FOR UPDATE` 命中一行，否则按未知身份拒绝。
   不依赖无变化 UPDATE 的影响行数，避免 PG 与 InnoDB 的 rowcount 差异。
2. 锁后重新读取租约/attempt 权威事实，逐字段核对身份：lease/operation/attempt/stage/workspace/
   pool/host/gateway/instance/binding/policy 不一致为 `invalid_execution_request`，generation 不一致为
   `instance_generation_mismatch`，token 不一致为 `stale_fencing_token`。
3. 租约必须处于 `reserved` 或 `executing`：`stopping`/`released`/`quarantined` 一律
   `lease_not_active`，不因请求已排队而放行。
4. 已封屏障 `gateway_fencing_barriers.min_fencing_token` 高于本次 token 时
   `gateway_barrier_fenced`。屏障行不存在时不假装入口已封锁；屏障与新任期由 `seal_barrier`
   和 `activate_owner` 负责。
5. 写入一行 `submit_intents`（含规范正文与完整请求摘要），需要输出流时再写
   `output_stream_heads`。同一 (operation, attempt, stream) 已有流权威行时只校验并复用其
   采样率，不重复建行，也不按 submit ID 分开计量。
6. 提交。已存在的 intent 或已占用的租约让 INSERT 冲突，adapter 报
   `idempotency_conflict`（`query_original`），绝不把旧记录当作本次创建成功返回，调用方必须
   查询原身份；重放始终不再外发。

`get` 用 `submit_intents ⋈ admission_leases ⋈ admission_attempts` 重建完整身份，行数不等于 1、
枚举或上限无法重新解释时按 `reconciliation_required` 报告，不猜测。`save` 是带版本条件的写入：
只有 `WHERE submit_intent_id = … AND version = 读取值` 命中一行才成功，否则
`version_conflict`（`query_current_authority`）。该写入助手只供受控事务内部复用；
提交服务与 adapter 不再提供通用 `save` 入口，外发结果与迟到观察都经第 9 节专用端口。

未知结果只对数据库已确认回滚的 deadlock/serialization 冲突重试，最多三次、抖动在连接归还后
发生、共享同一 deadline。连接断开、提交确认丢失或 `InterfaceError` 报 `outcome_unknown`
（`query_original`），不自动重发；池未发放连接或预算耗尽且未提交时报
`reconciliation_required`（`retry_transaction`）。每条语句按剩余预算设置 `statement_timeout`，
commit 前预算耗尽即回滚。事务内不做推理、HTTP、容器控制、对象写入和 sleep。

## 3. 存储映射

| `submit_intents` 列 | 来源或规则 |
| --- | --- |
| `submit_intent_id`、`lease_id`、`operation_id`、`attempt_id`、`command_digest`、`voice_revision_id`、`deadline_at_us` | 请求身份；`deadline_at` 以 UTC Unix 微秒存储，无时区或负值拒绝 |
| `request_digest`、`request_document` | RFC 8785 规范化的完整有效请求（`execution_request` + `output_stream_id`）及其 SHA-256 |
| `recorded_at_us`、`updated_at_us`、`version` | 应用服务首次记录时间；此后每次状态变化加一 |
| `execution_state`、`cleanup_state`、`engine_request_id`、`dispatched_at_us`、`error_code` | 状态矩阵与 `save` 条件写入的列 |
| `output_stream_id`、`sample_rate_hz`、`hard_output_ceiling_samples` | 只有发出 PCM 的阶段才有；上限为 `ceil(max_output_audio_ms × sample_rate_hz / 1000)`，反解要求整毫秒，否则拒绝改写冻结预算 |
| `terminated_at_us` | `record_stop` 保存证据的观察时间；`updated_at_us` 采用等待行锁后的数据库时间 |
| `staging_output_object_key`、`generated_pcm_samples`、正文清除列 | 发布、计量、删除仍由后续端口负责 |

规范正文只在创建时写入、不在读取投影中返回，避免把私有业务内容带进查询路径；它保留在
部署网络内，不写日志或公开投影。摘要比较针对**完整有效请求**，因此只改 deadline、token 或
输出流而不改命令的同 ID 重放同样按 `submit_intent_conflict` 拒绝。

## 4. 权限边界

运行使用非 owner 的 gateway 角色，只持有 `coordination_runtime_grants` 生成的清单：
`submit_intents`、`output_stream_heads`、`gateway_fencing_barriers`、`execution_stop_evidence`、
`output_sample_intervals` 的 SELECT/INSERT，`submit_intents` 与屏障的运行投影列 UPDATE，以及为
行锁所需的 `UPDATE (version)`。角色没有表级 UPDATE、DELETE、TRUNCATE 或 DDL；身份、摘要、
冻结输出边界不在 UPDATE 清单内；正文清除与 tombstone 列是删除端口的显式例外。集成测试对 `command_digest`、`output_stream_id`、
`DELETE`、`TRUNCATE` 逐条断言被拒绝。

## 5. `record_stop` 事务

`SubmissionLedger.record_stop(RecordStop)` 消费冻结的证据、终态和错误码；应用服务不再通过
独立的 `get`/`save` 来关闭执行。`RecordStop` 是 domain 值对象，fake 与 SQL 共用同一份
转换/重放规则。事务没有引擎调用，也不创建数据库函数、触发器或新表。

1. 只读定位 pool，按 `resource_pools` → `submit_intents` 取普通行锁，再重读完整执行；
   核对引用仍属于该 pool，与 owner 接管及其他受控写入者串行。
2. 按 evidence ID 和 submit ID 查询原证据。相同证据的**全部字段**（含 reason、观察时间）及
   相同终态/错误码返回原事实，不增加版本、不改时间。异请求同身份或跨执行复用证据 ID 报
   `idempotency_conflict`；全局唯一键还能拒绝不同池上的并发抢占。
3. 无原证据时，纯规则核对：四类受信证据、停止与资源释放均为 true、观察时间未早于外发且
   未晚于锁后的数据库时间。成功只接受 `engine_terminal_response`；失败必须有稳定错误码，
   其他终态必须没有错误码。缺失外发时间的 unknown 只允许凭整实例终止双证据中止，详见第 8 节；
   engine ack、执行中或成功仍须有真实外发事实。未知隔离态须先协调，或以已验证中止闭合。
   已有终态不能反转，version 耗尽拒绝写入。
4. 同事务 INSERT `execution_stop_evidence`，按原 version 更新 `submit_intents` 的终态、清理状态、
   错误、终止与更新时间、version。两步任一步失败都回滚；不修改 A 的 lease、quota、节点预算或槽位。
   Scheduler 后续独立事务读取这些证据，裁决何时归还预算。
5. 提交确认丢失报告 `outcome_unknown/query_original`，不自动重写；调用方可读原 submit，
   或以完全相同的停止请求重放，恢复已经提交的原事实。

所有 `cleanup_state=complete` 的写入都经该端口，包括成功完成。普通 `save` 在 pool/submit
锁下核对 version 和原始外发关联，拒绝绕过停止证据的关闭、终态复活和已发送执行改成
`not_required`。读取已清理终态却没有证据行时返回 `reconciliation_required`，不从成功状态
补造证据。状态查询只报告成功/失败而没有资源释放事实时，执行保留隔离与占用；迟到的
executing 观察可以对账并保存版本，但不触发再次外发。

## 6. 入口屏障与 `seal_barrier`

只把大 token 写入数据库不足以封住引擎入口：旧进程可能已经提交 intent，暂停在真正外发之前。
新 owner 若此时收到成功屏障并接管，旧进程恢复后仍可能发出旧请求。因此提交服务必须显式接收
一个 `GatewayIngress`，同 gateway/node 下所有提交与封锁共用它，不允许无 guard 构造服务。

### 6.1 本机外发排序

- 每个 pool 一把普通进程内锁，覆盖 `add` 提交 → adapter 外发 → `save` 提交。等待锁后再次查询
  原 intent，避免并发同身份再次外发；旧身份仍可重放查询并对账。
- 封锁先提高本地最低 token，再等已有外发段退出。等待默认 3 秒，可配置且最多 30 秒；
  超时报告 `reconciliation_required/retry_transaction`，不返回 `barrier_sealed=true`。
  新的低 token 请求不会延长等待；数据库失败或提交确认未知都保留本地最低 token。
- 拿到入口锁以后才保存屏障，数据库事务内没有引擎 I/O。事务返回成功才生成封锁确认。
  已进入引擎的执行仍由 inventory 列出；封入口不表示这些执行已经停止，更不归还预算。
- 该锁串行的是短提交握手；真实 adapter 必须有界，不能把整段长推理放进握手。吞吐与真实
  引擎超时尚未取得资格证据。

### 6.2 进程替换与集群边界

线程锁不能阻止另一个进程顶替暂停的旧进程。`ProcessIngressOwnership` 因此按 node/gateway
身份取得宿主本地 `flock`，不以 TTL 到期抢占。一个 owner 只能绑定一个入口 guard；身份只读，
fork 后不能使用或关闭父进程 lease。退出时等待仍在使用 lease 的外发/封锁段结束，再关闭 fd。

装配与部署必须满足以下条件，否则不能声明该入口具备屏障保证：

1. 同一 node/gateway 的所有候选进程、候选容器挂载**同一个宿主本地锁目录**；不能各用容器私有目录，
   不能删除或替换正在使用的锁文件。目录与引擎凭据只对受信网关进程开放。
2. node 与引擎凭据绑定该宿主；其他宿主使用独立 gateway 身份、凭据和本机 guard。
   本地文件锁不是全局协调权威，不代替数据库的 owner、租约或预算。
3. 新进程只能在旧进程真正释放锁后取得同身份；进程存活但暂停时替换失败并保持隔离。
   宿主丢失不表示 GPU 已停止，跨宿主接管仍需停止与资源释放证据。
4. 重启丢失本地 floor 时，`create_submit` 仍在 pool 锁下读取持久屏障，旧 token 在外发之前被拒绝。
   启动与恢复不能通过绕过 guard、改 gateway 身份或手工降低屏障恢复服务。

这些约束允许各节点独立外发，没有引入单例全局网关。当前仅测试了同宿主进程排他和数据库
屏障与认证 HTTP；共享目录和凭据尚未接入 Compose 部署，不能据此宣称已验证跨节点集群或宿主故障接管。

### 6.3 屏障事务与重放

`BarrierLedger.seal_barrier(IngressFence)` 消费入口已排空外发段的事实：

1. 按 pool → gateway/pool 屏障行取普通行锁，锁后取数据库时间，核对当前未过期 owner ID/token。
   有活动引擎实例时，gateway 对应 node 也必须一致。
2. 保存单调递增的最低 token；同 token/同证明重放返回同版本，较低 token 拒绝，
   同 token/异证明报 `idempotency_conflict/query_current_authority`。不新建表、函数、触发器或预算权威。
3. 在 pool 锁保护下按固定顺序枚举该入口的 `submitting`、`executing`/`stopping` 与
   `unknown_quarantined` intent。此处普通 JOIN 读取，不在屏障行之后倒序锁 lease。
4. 证明摘要固定覆盖 gateway/node/pool/new owner/minimum/sealed。inventory 与 `enforced_at`
   是本次确认时的最新观察，不进入不可变证明摘要；同 token 重放可返回更新后的 inventory，
   不改持久屏障版本。A 激活时重读权威执行状态，不能把空 inventory 当作独立停止证明。
5. 提交确认丢失报告 `outcome_unknown/query_original`；不自动重做未知写入、不重新外发。
   完全相同的封锁请求可重新排空入口并恢复持久证明；本地 floor 始终保留。

当前只支持 `abort_in_flight=false` 的封入口与枚举。`true` 明确返回
`execution_cleanup_pending`，因为真实 abort adapter 尚未交付；不确认已完成中止。
事务只更新 B 的屏障表，不修改 A 的 lease/quota/节点预算。A 的激活事务已经通过合成执行
联调：活动执行存在时拒绝，可信停止事实保存后才可激活，激活本身不退预算。

## 7. 认证 HTTP 控制与运行装配

已实现 `POST /internal/v1/gateways/{gateway_id}/fencing-barrier`，operation ID 为
`enforceGatewayFencingBarrier`；只读 `GET /internal/v1/executions/{submit_intent_id}`
（`getExecution`）；`POST /internal/v1/executions/submit`（`submitExecution`）与
`POST /internal/v1/executions/{submit_intent_id}/abort`（`abortExecution`）。
drain 没有占位路由。

提交路由先用冻结请求校验 command 与 `command_digest` 一致，再按 `create_submit` 落唯一 intent，
之后才外发一次；重放返回原记录并标记 `idempotent_replay=true`，同一 intent 换命令为
`submit_intent_conflict`，摘要与正文不符为 `invalid_execution_request`。冻结请求只带输出上限与
采样率、不带输出流名，因此采样率非空时由网关按该 attempt 的身份派生唯一逻辑流名
（`{operation_id}.{attempt_id}`，超出契约长度时保留前缀并附摘要），使同一 attempt 的各阶段共用一条流。
中止路由发起有界中止，只在 adapter 亲自核对到“已停止且资源已释放”时才记 `aborted`/`complete`；
传输超时、断连、找不到或无从核对一律记 `unknown_quarantined`/`cancellation_unconfirmed` 并继续占用。
已终态的 intent 直接返回记录快照，不再询问引擎；属于其他 gateway/node 的执行为 404
`control_execution_not_found`，尝试与记录不符为 422，token 不符为 409。
未注入引擎 adapter 时，两条路由都返回 503 `control_store_unavailable`（`retry_transaction`）
且不写任何行，避免把“本网关没有 adapter”读成已提交或已停止。

组件凭据独立于公开服务密钥和 controller 凭据；健康端点不把“控制面可用”解释为引擎或模型就绪。
传输 TLS、凭据分发与出站网络隔离仍由部署实现，本次 HTTP 测试不能证明这些资格。

默认只开放运营端点。以下设置必须成组显式提供，缺失其中一项、相对锁目录或沿用默认身份会在
配置阶段失败；不能通过临时随机目录启动同身份实例：

| 环境变量（均以 `SPEECHNEXUS_GATEWAY_` 开头） | 规则 |
| --- | --- |
| `NODE_ID`、`GATEWAY_ID` | 显式身份，符合冻结 Identifier；同身份候选实例遵守第 6.2 节宿主约束 |
| `COMPONENT_TOKEN` | 部署签发的非空专用 bearer secret；不写入响应、文档、日志或 Git |
| `INGRESS_LOCK_DIRECTORY` | 宿主本地绝对路径；候选容器挂载同一个目录；这里只保存进程锁，不传递业务内容 |
| `CONTROL_DATABASE_URL` | 已有必填项；使用迁移生成的非 owner gateway 角色，不使用管理角色 |
| `CONTROL_SCHEMA` | 默认 `speech`；隔离测试可指定已迁移的独立 schema |
| `CONTROL_TIMEOUT_SECONDS` | 默认 3 秒，最大 30 秒；入口等待和数据库事务分别有界 |

构造 app 不打开数据库池或取得进程锁。lifespan 启动后才装配一个 `GatewayIngress`、
`PostgresSubmissionLedger`、屏障服务与查询服务；同身份旧进程仍持锁时启动失败。
屏障/查询与提交/中止服务复用同一个 ingress 与 ledger，两组注入都必须各自成对提供，
且注入控制服务要求显式控制配置。关闭时先停止提供控制服务，
等待仍在使用进程 lease 的段退出，再释放锁与数据库池。启用后 `/readyz` 增加 `gateway_ingress`
检查；未启动或已关闭的控制服务不能给出屏障确认。

HTTP 请求/响应使用 `speechnexus_contracts.execution` 的生成类型和离线 schema。原始输入在
Pydantic 转换前验证，字符串 token、伪布尔、越界整数和未知字段不能被转换后放行；服务生成的
OpenAPI 同样取自契约 schema，保留范围、枚举、额外字段限制及错误恢复条件。
验证错误只返回固定描述，不序列化 input/context。已声明的成功和失败响应带 `X-Request-ID`。

内部执行控制契约为 `0.4.0.dev0`，包含屏障、执行查询与控制错误的 Python/TypeScript consumer 入口，
并重新生成绑定。引擎执行错误枚举保持原义；以下控制面错误不写入执行的 `error_code`：

| HTTP/code | retryable | recovery | 调用方行为 |
| --- | --- | --- | --- |
| 503 / `control_store_unavailable` | true | `retry_transaction` | 暂时无法排空入口或取得事务；可重试同一屏障请求，不得激活 owner |
| 503 / `control_outcome_unknown` | false | `query_original` | 普通自动重试停止；按原身份恢复。屏障可用完全相同 gateway/pool/owner/token 请求重新确认原最低值；这不重新提交任何引擎工作 |
| 503 / `control_reconciliation_required` | false | `reconcile` | 保留封锁与占用，协调权威状态，不确认接管 |
| 409 / `control_state_conflict` | false | `query_current_authority` | 读取当前 owner/屏障，不能以另一个证明覆盖原身份 |
| 404 / `control_execution_not_found` | false | `none` | 当前 gateway/node 没有原身份；不据此证明没有外发，更不能直接退预算或创建新执行 |

新增控制错误的恢复字段与 retryable 组合由 schema 约束。现有执行错误格式继续可读，增加的
控制码与可选 recovery 要求调用方更新至本版生成绑定；旧严格错误消费者不能假定可直接兼容。
401 表示凭据无效；422 表示字段或目标 gateway 不合法；409 保留 stale token、未过期 owner
不匹配和未完成 abort 等明确拒绝。错误响应不是成功屏障，不能据此释放预算。

## 8. 未知执行与只读恢复

进程可能在提交 intent 之后、记录外发结果之前崩溃。此时 `dispatched_at` 和
`engine_request_id` 仍为空，不能把“没有记录”解释为“没有执行”。

`ExecutionRecoveryLedger.mark_execution_unknown` 在 pool → submit 普通行锁后读取数据库时间，
调用共享领域规则，保存一个 unknown/quarantined 版本，保留全部身份、摘要、输出投影与既有外发事实。
观察时间不能晚于数据库时间或早于原事实；终态不能复活。同 submit/同原因重放返回原版本，
不刷新时间；异原因报 `idempotency_conflict/query_original`。观察时间只裁决新事实是否陈旧，
不是新建一份观察历史。本事务不改 A 的租约、quota、槽位或节点预算。

缺失外发时间时，只允许 `unknown_quarantined → aborted`，且必须有
`instance_terminated_by_controller` 或 `operator_verified_instance_terminated` 的停止、资源释放双证据。
观察时间不得早于 intent 创建；operator 仍须说明原因。`record_stop` 原子保存证据与终态，
保留空外发时间和空请求 ID，不补造一次 engine dispatch。engine abort/terminal ack 不适用这个分支。
证据生产者必须可信覆盖原 instance/generation 的整个生命周期；这些 schema 和合成测试不代替
第 6 节入口封锁、代际凭据及真实实例终止保证。

`ExecutionQueryPort.query_outcome(submit_intent_id)` 返回原 `ExecutionSnapshot`，空身份返回 None；
快照的 `response()` 对应协调契约 submit 分支的 `kind=execution`。认证 GET 仅返回其 execution
投影，只接受当前 gateway/node 的记录。它不提交、观察、重试引擎，也不协调执行。
存储读取失败为 503，不能转为 404。查询不读取命令正文，不返回内部 staging key；
仅有 staging key 不足以生成完整制品引用。输出水位读取既有 `generated_pcm_samples` 持久投影，
追加事务已由第 11 节实现；水位不代表完整区间用量或真实计量采集器取得资格。unknown 的 wire marker 为
`unconfirmed_timeout`，不创建假停止证据行。
已有生成样本的记录不能被解释成 `failed/not_required`；读取这种矛盾事实要求
`reconciliation_required/reconcile`，不能对外给出“从未外发、无需清理”的结论。

0.4 的兼容变化是空外发时间的整实例中止分支，以及新的 404 控制码。
`SubmitExecutionResponse.stop_evidence_kind` 通常可选，但上述中止重放必须携带；
旧严格消费者会拒绝这一状态组合，生产者和消费者必须同步升级生成绑定与正负例。
公开语音契约不变，也不改写已有执行记录。

## 9. `record_execution_observation`

提交服务已将外发后的状态保存和迟到 executing 对账迁到本端口。事务外取得引擎观察，
事务内按 pool → submit 普通行锁重读原事实，再取数据库时间并执行共享领域裁决；
没有引擎、HTTP、对象写入或预算修改。数据库与 fake 共用 `RecordExecutionObservation`，
没有新增表、存储过程、触发器或专门的观察历史。

- 完整冻结身份必须相等，包括原 generation、token、binding/policy/voice revision、
  command digest、deadline 与输出边界。采样数与输出流只能沿用持久投影，不能借观察修改计量；
  staging 发布也不属于此端口。数据库读取仅把 staging key 转为“已有 staging”事实，不暴露路径。
- 必须命中 `expected_version`。改变事实只增加一个版本，以锁后的数据库时间更新；
  同版本的相同事实即使观察时间较新也不写心跳版本。旧版本返回
  `version_conflict/query_current_authority`，不能通过语义相似绕过版本守卫。
- 观察时间不得早于已记录事实或晚于数据库时间；真实外发时间必须在 intent 创建与观察之间。
  已有 request ID、外发时间不可改写。迟到 executing 不能撤销 stopping，终态不能复活；
  普通状态响应不能伪装停止与释放证据，成功、中止及有清理的失败仍经 `record_stop`。
- `failed/not_required` 只从 submitting 产生，并同时要求无外发时间、无 engine request ID、
  无持久 staging、无生成样本及任何已记输出区间。unknown 不能据此解除隔离。该事实允许 A 后续裁决，
  本事务不归还 A 的任何预算。
- 提交确认丢失返回 unknown/query_original，不自动重写。先读取原 submit；重发旧版本会冲突。
  `query_outcome` 给出当前事实，不据此声称某个更早的请求已提交或回滚。

受信 `ExecutionObservationBoundary` 消费已有协调机器契约，验证后转换成独立领域对象；
返回完整、经同源校验的 `TransactionResult` 与 `ExecutionSnapshot`。音频观察须携带与原记录
匹配的完整 output watermark，非音频使用 null；该要求用于冻结关系核对，不产生区间计量。
边界拒绝非空 staging artifact 发布输入。观察服务与提交服务均核对本 gateway/node，
不能凭另一个节点的记录调用本节点引擎。没有新增 HTTP 观察路由或自动恢复循环。

`EngineObservation` 可携带可信的原外发时间。崩溃记录没有外发时间、引擎又未提供实际时间时，
即使 status=executing 也继续 unknown；不能用“本次查询时间”补造 dispatch。
已有真实外发时间时仍可对账原请求，始终不再次 dispatch。
新增 Python 协调入口与 Python/TypeScript 正负例复用原 `0.4.0.dev0` 状态协议，不改变公开语音协议。

## 10. `redact_execution_command`

本端口已实现原生领域/应用/PG 事务与受信契约边界；它不实现完整删除计划、依赖确认、
删除账本重放或对象删除。入口必须注入 `ExecutionCommandDeletionAuthorizer`：
由服务端删除协调器先核验权威删除任务、确认范围和固定依赖，并在已接受的持久删除范围内
阻断新引用/执行，才签发绑定完整执行身份及确切请求的 `AuthorizedCommandRedaction`。
该值对象不是密码学凭据，不能从客户端 DTO 直接构造来替代授权。
仓库未提供默认 authorizer，组合根不装配本端口，HTTP 未暴露此操作；
A 的真实删除协调器仍待交付。测试使用明确命名的合成 authorizer。

授权检查在数据库事务外完成，不在事务中等待外部服务。随后按 pool → lease → submit
普通行锁重读完整执行身份及租约状态，锁后取数据库时间：

1. 只有 terminal + cleanup complete/not_required，且 A 已将原 lease 置为 released，才可清除正文。
   停止证据不能代替 lease release，released 也不能代替执行清理。B 不归还或改写任何预算。
2. 首次清理必须命中 `expected_version`，并确认尚无 tombstone。只将 `request_document`
   置空，同事务写 `command_redacted_at_us`、删除 Job/证据摘要、更新时间和一个新版本。
   完整执行身份、原 request/command digest、输出边界、计量、停止证据与历史保留。
3. 同删除 Job/证据摘要重放返回原 tombstone，先识别已提交事实，不因旧版本拒绝原重放，
   不重写时间/版本。不同 Job 或证据摘要返回 idempotency_conflict/query_original；
   尚未清理的版本冲突返回 version_conflict/query_current_authority。
4. 提交确认丢失仅报告 unknown/query_original，adapter 不自动重写。原执行查询给出当前
   执行状态及版本，不投影私有正文或删除 tombstone，也不能独自证明哪次清理已提交。
   协调器查询后可用原许可作显式幂等确认；只有匹配 tombstone 的重放才确认原删除事实。
   已清理执行的提交重放仍只返回原执行，不能再次 dispatch。

机器结果沿用既有完整 `TransactionResult` + `FactSnapshot`：
`fact_id` 为原 submit identity，`request_digest` 为原规范执行请求摘要，保留执行追溯；
不把它改成 ExecutionSnapshot，也不扩展字段发布内部删除许可。原生结果额外携带匹配的
不可变 tombstone 供受信协调使用。观察与清理边界复用统一错误/恢复映射，
分别产生契约要求的具名快照。没有新增表、迁移、触发器、存储过程或预算权威。

## 11. `append_interval` 与原区间查询

输出区间账本由 B 唯一写入，A 读取生成事实作业务额度和结算，不再创建一份 PCM 账本。
`OutputIntervalBoundary` 消费既有协调契约，事务前只读定位并核对本 gateway/node；
事务内重核对完整冻结执行身份，按 pool → submit → output_stream_heads 普通行锁，
在整条 `(operation, attempt, output_stream, meter_kind)` 上检查全部重叠，再追加不可变区间。
不同 submit/stage，乃至不同 pool 的同一逻辑流都共用这行流权威锁。
这里没有进程内全局协调、GiST、触发器、存储过程或新表/迁移。

- 区间为 canonical PCM 样本坐标 `[sample_start,sample_end)`，不是毫秒或传输包字节。
  范围必须正长、非负且不超过 JSON safe integer；采样率、流身份及硬输出上限必须匹配原执行。
  同 ID 的完整请求相同才可重放；新 ID 即使范围完全相同也按重叠拒绝。相邻、空隙和乱序合法。
- generated 追加与该 submit 的 `generated_pcm_samples=max(old,end)`、一个新版本和锁后数据库时间
  同事务提交；任一步失败整体回滚。水位只表示已见最大位置：`[0,100)` 与 `[200,300)` 的
  水位为 300，实际生成消耗为 200 样本。A 必须按 generated 区间长度求和，不能按水位或各 stage
  水位相加结算。不同 attempt 实际重新生成仍是消耗。
- delivered/played 独立记录诊断，不推进生成水位，不回退已生成消耗，也不代替停止/释放证据。
  任意已记输出区间都会阻止后续声称“从未外发、not_required”，即使生成区间因乱序尚未到达。
  该存在性事实从账本按原流键读取，不新增一份标量权威或公开字段；首次 INSERT 也不能预填它。
- 迟到的生成事实可在已有完整停止证据的终态补记，不复活执行、不改停止证据或归还预算。
  已确认从未外发的失败不能接受新的输出事实；返回 execution_cleanup_pending/reconcile。
  越硬上限或逻辑流重叠也要求协调，不能伪造停止或改写旧区间来让计量继续。

`append_interval` 和 `query_outcome(target.kind=interval)` 返回完整 `TransactionResult/FactSnapshot`；
`fact_id` 是原 interval ID，摘要是完整规范 append 请求的 SHA-256，可由不可变区间与冻结流采样率重建。
区间没有独立版本，结果不编造 version。查询可核对请求摘要，只读原身份，不锁行、不调用引擎，
不修改区间、执行版本或预算。读不到本 gateway/node 的原事实时返回既有
unknown/outcome_unknown/query_original，不能推断早前写入已回滚或授权重新生成；
数据库读取失败也不能冒充“没有事实”。提交确认丢失时 adapter 不自动重复写入。

Python/TS 共用请求、完整结果及未知恢复样例；JSON Schema 接受的整数数值在验证后映射为 domain int，
wire 输入保持原样。输入中的布尔、数字字符串、额外权威字段仍按源 schema 拒绝。
此处只提供受信原生事务，不开放浏览器/HTTP 计量入口，也不把任意客户端播放报告当作可信生成事实。
真实引擎输出、网络交付/播放采集、A 的用量消费与资格负载仍需后续联调。

按 ADR 0007 对旧计量用例作本分项映射：

| 旧检查 | 当前执行位置与证据 | 边界变化 |
| --- | --- | --- |
| `test_output_intervals_are_non_overlapping_and_identity_bound` 的重叠、相邻、流/执行/采样率、硬上限、水位与历史保护 | `OutputIntervalRecord`、流锁事务；`test_gateway_output_intervals.py`、`test_output_intervals.py`，配合观察与首次创建守卫测试 | 仍保留业务不变量；同 meter 全流串行，运行角色禁止历史 UPDATE/DELETE |
| 同用例中 raw SQL 触发器/GiST 抛错、管理员不能直改水位等机制断言 | ADR 0006 的受控事务与最小列权限替代；观察端口拒绝改写计量，只有专用追加端口推进水位 | 不要求数据库独立拦截所有绕过应用的跨表违例，不复制旧触发器 |
| `test_output_ceiling_cannot_be_introduced_after_generated_output` | 上限在 create_submit 冻结；观察/计量均核对完整原身份，不能迟引入/改上限，native/fake 与双库正负例 | 冻结早于输出，旧 nullable→set 的管理员 DML 路径不再是产品入口 |

本表仅完成输出分项映射；其他旧用例仍需逐项分类与验收，退出条件尚未全部满足，旧门禁继续过渡保留。

## 12. 提交、停止与未知的具名结果边界

`ExecutionFactBoundary`（`interfaces.submit_transactions`）是这三个事务的受信进程内入口：
它不开 HTTP 路由、不授予外发权限，调用方拿到冻结的完整 `TransactionResult` —— outcome、
稳定错误码、恢复提示、版本与 `ExecutionSnapshot` —— 而不是领域异常。它与认证 HTTP 提交共用
`interfaces.execution_payloads` 里同一份冻结请求翻译，因此两条入口拒绝同样的值；命令中的
`output_stream_id` 由命令显式给出（HTTP 入口没有该字段时按 attempt 身份派生），采样率非空时必须存在。

重复同一事务报 `replayed` 并返回原事实，不改写时间与版本；同一 intent 换命令、已终态执行被降级为
unknown、对不存在的执行报事实，分别是 `submit_intent_conflict`、`execution_cleanup_pending`
（`reconcile`）与 `invalid_execution_request`。版本只在新事实写入时前进，所以“版本未变”就是原重放。
停止事实仍只追加、仍不释放任何预算，未知转换仍保留全部身份与占用。

`seal_barrier` 的具名入口仍待补一个只读屏障事实端口：同一屏障命令的重放不能靠「再封锁一次」取得
原事实，因为 owner 已被替换时再次封锁会因权限过期被拒，而不是返回原事实。

## 13. 未交付与已知缺口

- 提交、停止、未知、观察、清理与区间事务都已返回完整具名 TransactionResult，`seal_barrier`
  的具名入口仍待补只读屏障事实端口；真实计量采集器、A 用量消费与资格验证仍待接入；
  命令正文清理事务已有，真实删除协调器与许可签发仍待接入；
  unknown 事务和 submit 查询分支已有，自动恢复循环仍待装配。
  `seal_barrier` 的应用入口、持久事务与认证 HTTP 已有，提交与中止路由也已服务，
  但完整事务响应封装、`drain` 路由与真实引擎 dispatch/abort adapter 仍未交付；
  在没有 adapter 的组合根里两条引擎路由只返回可重试的不可用，不产生任何执行事实。
  Compose 未配置控制凭据和共享宿主锁目录，仍只开放运营端点。
- PostgreSQL 已验证并发停止、真实 pool 行锁等待与回滚；InnoDB 使用相同 SQL/领域规则的
  测试 adapter 验证迁移意图，不形成产品 MySQL 支持。数据库连接配置、时钟与异常分类仍在 adapter 内。
- 这里的证据是最小合成事实，仅证明受控事务与存储规则。真实引擎/控制器如何提供可信双证据、
  跨节点屏障、实际 abort、物理 GPU 停止及 A 的预算释放联调仍需单独取证。
- 提交入口已服务并通过真实 PG 集成测试，但完整 deadline/运行资格校验与实际引擎外发仍需接入；
  没有引擎 adapter 时入口只会拒绝，因此不能据此称为生产执行链路就绪。

## 14. 验证

```sh
# 需要同时设置隔离 PG 与 MySQL DSN；缺一不可，跳过不算通过。
SPEECHNEXUS_TEST_DATABASE_URL=… SPEECHNEXUS_TEST_MYSQL_URL=… \
  uv run pytest -m integration tests/integration/test_gateway_submit_ledger.py \
    tests/integration/test_gateway_submit_http.py \
    tests/integration/test_gateway_fact_transactions.py \
    tests/integration/test_gateway_stop_evidence.py tests/integration/test_gateway_barrier.py \
    tests/integration/test_gateway_recovery.py tests/integration/test_gateway_observation.py \
    tests/integration/test_gateway_command_redaction.py \
    tests/integration/test_gateway_output_intervals.py \
    tests/integration/test_gateway_barrier_http.py -q
uv run pytest -m "not integration" -q
uv run ruff format --check . && uv run ruff check . && uv run mypy && uv run lint-imports
```

`test_gateway_submit_ledger.py` 在真实 PostgreSQL 上、以非 owner 的 gateway 角色运行：创建后
重放不产生第二行也不再外发；新进程（新池）读回同一 intent；`save` 版本守卫与陈旧写入失败；
封屏障/非活动租约/未知租约/流采样率冲突各自拒绝；两 stage 共享一条流权威行；无音频阶段不建流、
无上限；管理员改坏 `sample_rate_hz` 后读取报 `reconciliation_required`；同一连接上建同名临时表后，无限定名的读取
仍解析到权威表；另一个连接持有池行时提交事务真实等待该行锁，释放后才提交。InnoDB 分支按设计跳过（adapter 是 PostgreSQL 专用），不构成
MySQL 支持承诺。


`test_gateway_stop_evidence.py` 的共享场景在 PG/InnoDB 两库运行：四类证据、成功/失败矩阵、
完整重放冲突、不完整/未来证据拒绝、证据 ID 不可跨执行复用、未知执行中止、终态不可复活、
伪造无证据终态读取拒绝、并发相同/冲突请求与故障注入整体回滚，且逐项核对 A 的预算投影未改变。
PG 另验证真正的 pool 行锁等待，以及提交成功后注入确认丢失再查询/重放恢复，不自动二次写入。
这些测试没有加载模型或触碰同机共享数据库。

`test_ingress.py` 验证落 intent 后暂停外发时不提前确认屏障、超时/数据库失败后 floor 不倒退、
旧身份仍能重放、不同进程不能抢占同身份、退出等待最后一段外发、一个 owner 不能绑定多个 guard。
`test_gateway_barrier.py` 在 PG/InnoDB 上验证最低 token、稳定证明与最新 inventory、重启拒绝旧
token、owner/token/期限/node 核对、单调版本、回滚，以及 A 激活等待停止事实。PG 另注入屏障
提交确认丢失，证明原事实可恢复且不产生第二个版本；暂停外发时没有 idle-in-transaction。

`test_barrier_http.py` 验证认证、原始字段拒绝、目标身份、未支持的 abort、正在外发时不确认、
四类存储失败及恢复提示。`test_gateway_control_contract.py` 比较服务实际 OpenAPI 与冻结约束，
并验证源 schema 与生成消费者一致。consumer profiles 的正负例同时由 Python/TypeScript 运行。
`test_gateway_recovery.py` 在 PG/InnoDB 验证 unknown、原身份查询、无外发时间的实例中止、
并发单版本、冲突与未来观察拒绝、写入失败回滚，逐项核对 A 预算未改变；PG 另覆盖提交确认丢失恢复。
共享 `execution-recovery.json` 在源 schema、Python 与 TypeScript 中验证新增协调入口的正负例。
`test_gateway_barrier_http.py` 用真实回环 TCP 与生产 lifespan、非 owner PG 角色验证 readiness、
凭据、持久屏障、重放、owner 变更、pending/unknown/closed 查询、无版本或预算副作用和同身份候选进程启动拒绝，
退出后同时清除两个控制服务并可重新取得锁。
这条测试没有注入 fake service/readiness，也没有真实模型执行，不证明真实引擎或跨宿主资格。

`test_gateway_observation.py` 在 PG/InnoDB 验证观察版本与完整结果、确认未外发失败、staging/采样
保护、并发单版本、失败回滚及 generation/水位冲突，核对 A 预算和停止历史未改变。
PG 另验证真实 pool 锁等待后的取时与提交确认丢失后的只读恢复。单位测试覆盖完整身份、
时间/状态矩阵、不可倒退与不补造外发时间。原有提交、停止、屏障回归均经新窄端口继续验证。

按 ADR 0007 复核：本单元补齐观察路径，但遗留用例尚未逐项分类并映射全部有效不变量，
退出条件未齐；本轮不删除旧 PG 原型或停用其门禁，也不把旧机制通过当作新事务证据。

`test_gateway_command_redaction.py` 在 PG/InnoDB 使用同一产品 SQL/领域规则与最小 gateway 角色，
验证正文/tombstone 原子性、A 的 release 与执行清理双条件、版本/身份/重放冲突、
竞争单版本与约束失败回滚。两库均观察实际 lease 行锁等待，提交 A 的合成 release 后，
清理须重读 released 并使用锁后时间；PG 另验证真实提交后丢确认及只读查询恢复。
测试核对其他 submit 字段、停止历史、输出区间和 A 预算不变。
许可和 A release 仅为合成 fixture，不是完整删除协调器或 A 生产释放的联合资格。
按 ADR 0007 复核，新增正文清理路径仍不足以完成全部旧用例分类映射，旧门禁继续过渡保留。

`test_gateway_output_intervals.py` 在 PG/InnoDB 验证 generated/delivered/played、完整重放和原区间查询、
相邻/空隙/乱序、流硬上限、冻结身份及清理冲突、区间 INSERT 后投影失败的整体回滚、最小角色历史保护，
以及停止/正文清理后仍可查询原用量事实。两个不同 pool/stage 的写者均实际等待同一输出流行锁，
释放后重核对相邻或重叠；同 ID 并发只提交一次。PG 另有真实提交后丢确认的只读恢复。
只有合成 PCM 坐标，没有实际语音、网络交付/播放或计费结算资格。
