# 生产 Job/outbox 与 worker 认领

本单元属于 [A01 #20](https://github.com/hrygo/SpeechNexus/issues/20)。
生产代码独立于 `tests/support/coordination.py`；PG 是唯一产品数据库，
MySQL 测试 adapter 仅复用显式 SQL 与领域裁决验证 InnoDB 行为。

## 已实现边界

- `speech/domain/jobs.py`：不可变创建身份、严格 ASCII 标识/摘要和重放裁决。
- `speech/application/jobs.py`：`JobTransactions` 窄端口与创建/恢复用例。
- `speech/infrastructure/jobs_postgres.py`：有界 PG 池、Read Committed、语句等待、
  事务重试和未知提交分类；`jobs_sql.py` 不拥有连接或 commit。
- `speech/interfaces/job_transactions.py`：进程内输入/输出边界，经独立契约包中的
  `create_job` 与 `TransactionResult` 离线闭包校验，返回完整 `FactSnapshot`。
- Alembic `0001_jobs`：在独立 speech schema 中创建协调 Job 和 outbox；
  迁移仅由独立命令执行，不在 API/worker 启动时自动建表。

后续 `0002_coordination` 增加其余 13 表的[生产存储与最小权限基线](../operations/production-coordination-storage.md)，
不改写本单元的 Job/outbox 身份或授权；已有迁移命令升级整个包内链到 head。

这里的 Job 是协调元数据，不是完整公开 Job 的业务命令/结果存储。后续单元已有公共路由、准入事务、派发器和结果发布组件；公开异步 Job 的生产 planner 仍未装配，见下文。不得把协调事务或单次同步合成证据解释为完整文件转写/异步合成闭环。

## 原子性、身份与恢复

`job_id`、`(operation_id, attempt_id)` 和初始事件身份由唯一约束保护。
Job 行冻结初始 event ID/digest；outbox 后续可追加其他事件，不重定义创建身份。
初次创建在同一事务插入 Job/outbox，提交确认后才返回 committed。
原 job/operation/attempt/event 两个摘要全部相同才 replay；任一不同均 conflict。
`request_digest` 来自调用方完成默认值解析/版本固定后的业务命令，不对包含摘要
自身的包装再做散列。本单元不能从摘要重建完整业务 payload；原业务命令由
[准入命令存储](production-admission-storage.md)及各已接入业务用例单独保存和恢复。

重放读取不可变事实；不存在的新键通过唯一索引处理竞争。失去唯一键竞争后，
整个失败事务回滚，再只读查询胜者；不会重复投递或外发执行。`admit_recipe`
必须在其完整配方事务内调用 `create_in_transaction`，不能先调用独立
`create_job` 端口再提交第二个准入事务。

仅 PG SQLSTATE `40P01`/`40001` 的已确认终止事务可重试；最多三次尝试包含首次，抖动发生在
回滚并归还连接后。池队列、连接数、语句等待与重试预算有界；这不是已验证的
任意网络黑洞硬截止时间。连接中断、提交确认丢失返回 unknown/query_original。

`recover_create_job` 是同一进程内 Job 端口的只读恢复 helper，返回既有 create_job
结果，不新增 HTTP 路由或机器协议 operation。Job 或 outbox 缺失不能靠补写修复：
尚无可见 Job 时仍 unknown；已有 Job 却缺少对应 outbox 时进入 reconcile。
恢复只读，既不补建 Job，也不授予重新执行权限。
读取原结果时，连接池关闭/容量不足、checkout后deadline耗尽或数据库查询错误均保持
unknown/query_original；读取失败不证明原创建被拒绝，也不授予重做事务。原生查询超时/
取消已有同一语义。已确认未开始的新写入重试及已读到的账本损坏reconcile规则不变。
实际PG/InnoDB回归先提交并丢弃回执，再使读取失败，最后恢复原identity/fact并核对
完整Job/outbox不变；它不证明完整业务额度、调度或公开Job已交付。
`query_outcome(job)` 由下面的独立 claim 边界返回当前 `ClaimSnapshot`；
它不代替不可变创建身份的 `recover_create_job`。

## worker 认领与当前权威查询

`speech/domain/claims.py` 和 `application/claims.py` 定义独立领域值与窄端口，
`claims_sql.py` 在一个短事务中锁定候选 Job，再取数据库时钟，核对
`pending`、预期 `claim_version` 与到期时间，最后条件更新 owner、version、deadline。
即使同一个 worker，也不能在当前期限内用重复请求延长认领；到期后须携带当前
版本再认领，版本增加且旧版本失效。版本与微秒时间受 JSON 安全整数上限保护。
本单元复用已有表和 speech 最小列授权，不修改已发布的迁移或扩大 gateway 权限。
只安装创建专用 `job_runtime_grants` 的旧角色没有认领 UPDATE 权限，部署须按
[协调角色基线](../operations/production-coordination-storage.md)审阅并配置 speech 角色。

`PostgresClaimTransactions` 使用共享 RC/UTC 池；默认总事务预算 3 秒，最多三次
已确认回滚的 deadlock/serialization 重试。每条 SQL 重新设置剩余等待预算；
池等待耗尽时不开始写入。连接中断或提交确认丢失只返回
`unknown / query_current_authority`，不透明重发认领。语句/池预算不是任意网络黑洞
的硬实时保证，网络故障切换仍待交付资格。

`ClaimTransactionBoundary` 离线验证 `claim_job`、`query_outcome` 和完整
`TransactionResult`；只处理 job 查询目标，不发布公开 Job HTTP 路由。
查询使用只读事务，不锁行、不续期、不改预算或 outbox。未认领的行返回
`unknown / query_current_authority`，不存在的 Job 返回 `not_found` 错误；已有 owner 时返回当前快照，包括过期或后来 worker
的投影。快照不证明旧认领未提交，也不单独授予执行或发布权限。
未来 worker 必须在实际改变 Job/发布制品的事务中核对当前 owner、version 和期限；
该认领单元当时尚未接入这些写入端口；后续派发与发布进展见下文，不能仅凭认领端口宣称完整 worker fencing 已完成。
重新认领不允许重新提交已有引擎执行，必须查询原 submit 身份与持久证据。
认领、TTL 和查询都不改变 GPU 租约、停止证据或容量账本。

## 公开 Job 派发器（DEL-96 第一步）

`speech/domain/job_dispatch.py`、`speech/application/job_dispatcher.py` 与
`speech/infrastructure/job_dispatch_postgres.py` 组成公开 Job 的派发边界。
公共提交在同一事务写入 `speech_jobs`、事件、提交文档，以及私有
`admission_operations` 原命令和 `coordination_jobs`/`coordination_outbox` 身份；
不使用摘要或当前默认值重建请求。

授权分两段安装：`job_runtime_grants` 覆盖从 `0001_jobs` 起就存在的表，可在历史版本沙箱上直接应用；派发器额外需要的 `admission_operations` INSERT、`speech_artifacts` 的 SELECT/INSERT，以及 cancellation not-submitted 表的 INSERT 由 `job_dispatch_grants` 一并给出，**必须在升级到 head 之后**再执行——前者要到 `0007_admission_commands`、后者要到 `0020_artifacts` 才存在，提前应用会让授权失败或掩盖升级顺序问题。Artifact 写入只授予显式列 INSERT 与读取既有行的 SELECT，不授予 UPDATE/DELETE 或 DDL。

派发器按 `FOR UPDATE OF c SKIP LOCKED` 批量领取到期的协调行，写入 owner、
`claim_version` 和租约；进程崩溃后租约到期可重新领取。一次公开 Job 对应一个由
Job ID 确定性派生的 `submit_intent_id`，领取者先查询原身份，只有 `not_found`
才尝试以同一身份提交；已经观察到执行后再查到 `not_found` 则保持 unknown。原身份去重不单独证明引擎只执行一次：网关接收与崩溃恢复仍须按提交屏障及固定命令取证。`unknown`、不可解析的回执、身份或命令摘要
不一致都返回 unknown 并保留 `running`，不当作可重试失败。
当前派发器对状态文档仅在请求的 `command_digest` 和响应值都非 `null` 时比较该摘要；
响应缺少摘要不会被这一步拒绝。接入生产 planner 前须单独补齐并验收完整响应身份和摘要
校验，不能把现有检查当作完整的机器契约或原命令一致性证明。

成功执行读取原回执后写引用型 `result_json`；网关终态失败冻结错误码。公共 Job
的 `status_version` 由条件 UPDATE 递增，终态与协调行关闭在同一事务。批次、租约、
轮询、提交超时、任务期限和领取次数均有显式上限。任务期限到达但执行可能仍在运行时
返回 unknown，不用 `failed` 假装算力已停止；当前代码在首次查询返回 `not_found` 且任务已过期时写 `job_timeout`。该查询结果本身不能证明任何先前未知外发不存在；生产 planner 尚未装配，接入前必须核对提交屏障与超时竞争，不能将这一分支视为已验证的停止或释放证据。

派发器只通过 `speech/infrastructure/execution_gateway_adapter.py` 的
`ExecutionGatewayAdapter` 使用执行网关；适配器不持有 HTTP 实现，而是委托唯一
`speech/infrastructure/gateway_execution_client.py` 中的
`GatewayExecutionClient`。连接失败、超时、5xx 和不可解析响应保持“结果未知”
（分别进入 `DispatchUnavailableError` / `DispatchProtocolError`）；只有通过冻结错误
契约校验的 4xx 语义拒绝经 `DispatchRejectedError`（`DispatchProtocolError` 子类）进入
终态失败，缺少合法错误码的 4xx 仍是 `DispatchProtocolError` / `unknown`。查询或回执
的 404 仍是 `not_found`，408/429 保持 `unknown`。不得把 `unknown` 映射成 `failed`。
进入网关阶段之前，若持久请求缺失、摘要不符或 planner 生成不合法提交，代码会走
`job_request_unavailable` 或 `execution_result_invalid` 失败分支；这与外发后无法解释
网关响应保持 unknown 的分支不同，也不能作为执行停止或预算释放证据。

## 取消受理、提交裁决与对账

取消受理与执行提交之间没有跨服务事务，因此用一张只读围栏投影裁决。
`speech_job_cancellations` 仍是唯一的受理事实；`speech_job_cancellation_fences` 是
它的同事务投影，只含 `job_id`、`request_id` 和受理时间，避免 gateway 角色读取
operation/attempt/摘要列。投影与 receipt 在同一 `PostgresPublicJobs.cancel` 事务写入，
迁移 `0035` 会为既有 accepted receipt 回填。围栏本身不是停止证据，也不释放租约或预算。
`write_cancellation_fence` 取
`job_cancellation_fence_lock_key(job_id)` 的互斥事务级 advisory lock 后
`INSERT … ON CONFLICT DO NOTHING` 并回读，先提交者胜出，重放返回原受理时间与
request ID，不覆盖他人事实。

网关在创建任何执行之前读同一主键：围栏存在时只写
`failed / not_required / gateway_barrier_fenced` 的终态 `submit_intents`，不写输出流头、
不派发引擎，并以既有的 `gateway_barrier_fenced` 拒绝；该错误码与普通屏障拒绝共用，
但只有取消围栏路径同时落下无外发终态 intent。围栏不存在时按原流程创建；
执行创建后才受理的取消不改写 intent，继续由 abort/stop-evidence 路径推进。

派发器在已领取 Job 后观察到受理时不得再提交；它在
`speech_job_cancellation_not_submitted` 幂等写入 "已观察受理但未提交" 的持久事实。
该行只证明派发器没有越过提交点，不能单独结算；对账仍要查询固定 submit intent。
只有该行存在且网关对该 intent 返回 404 时，二者才共同构成 "没有执行" 证据。

`speech_job_cancellation_reconciliations` 是 backend 私有重试账本：每个围栏一行，
记录 `attempts`、`next_check_at_us`、`last_outcome` 和 `settled_at_us`。worker 以短事务
`INSERT … ON CONFLICT DO UPDATE … WHERE next_check_at_us <= now` 领取到期行，
因此同一行不会被两个 worker 同时领取；处理在事务外进行，结果再按 job 条件写回。
退避从 1 秒起翻倍并封顶，`max_attempts` 用尽后写 `exhausted`，不再无限重试；
进程重启从账本恢复，已 `settled` 的行不再领取。`CancellationReconciliationWorker` 按有界 interval 驱动 `run_once`；默认只领取受理
超过 5 秒的围栏，避免与受理侧的即时停止重复竞争。未注入 `on_error` 时存储异常向上
抛出，注入后记录并继续下一轮，便于组合根显式决定告警与退出策略。

只有两类事实允许推进 `cancelled`：网关回报上述“没有执行”终态，或既有停止路径
验证到 `aborted`、`cleanup_state=complete` 且证据在 `engine_abort_ack`、
`engine_terminal_response`、`instance_terminated_by_controller`、
`operator_verified_instance_terminated` 白名单内。执行仍活跃、已 `succeeded`、
传输失败或证据不全都不结算，分别有界重试或进入终止未确认。单独的 404 从不是
停止证据；只有与派发器的 not-submitted 观察同时存在时，404 才被当作无执行事实。

迁移 `0035_job_cancellation_fences` 的 `down_revision` 是
`0034_public_job_cancellations`，形成单一 head；down 在 reconcile 账本、not-submitted
观察或围栏仍有行时拒绝删除，要求先保留或导出证据。`public_job_grants` 为 API Job writer
授予围栏 SELECT/INSERT；`job_dispatch_grants` 只授予派发器对 not-submitted 表的 INSERT；
worker 角色使用 `public_job_grants` 与 `cancellation_reconciliation_grants`（围栏列 SELECT、
对账账本 SELECT/INSERT/列级 UPDATE、not-submitted SELECT）；gateway 写角色在
`coordination_runtime_grants` 之外还须安装 `gateway_cancellation_fence_grants` 以获得围栏列
SELECT，独立执行读取器使用同一函数。

接线已经落地：

1. `PostgresPublicJobs.cancel` 在写入 `cancelling`、事件和 receipt 后，于同一事务调用
   `write_cancellation_fence`；它是该事务最后一个会等待锁的写入，之后不再取
   `admission_leases`/`runtime_instance*` 行锁。
2. `job_cancellation_runtime.py` 把 `PublicJobs`、PG 对账账本、
   `GatewayCancellationConfirmer` 和 `CancellationReconciliationWorker` 装配在一起；
   `settle_cancelled` 走 `PublicJobs` 的 `status_version` 条件更新，活跃执行通过
   `JobCancellationService` + `GatewayJobCancellationStopper` 注入一次有界停止尝试。
3. 派发器在提交点前观察到 Job 已是 `cancelling` 时写入 not-submitted 观察并返回
   `claim_lost`；收到 `gateway_barrier_fenced` 且 Job 仍是 `cancelling` 时返回 `skipped`，
   不调用 `_fail`。两种情况都由对账 worker 以网关“没有执行”事实结算为 `cancelled`。
   Job 未取消时同一错误码仍按普通终态失败处理。

当前边界完成派发器、真实 PG 领取/终态存储、唯一网关客户端（派发器经适配器接入）、
worker 入口/进程角色及 `create_app` 之外的组合根。生产 `SubmitExecutionRequest` planner 仍未装配（结果发布已实现，见下节）；`build_dispatch_dependencies` 因 planner 未装配显式抛错，进程以
退出码 2 结束且不领取租约，不会用 fake 或空实现硬跑。`GET /v1/nexus/artifacts/{id}` 的读路径已可用，但需 planner 装配后才会由 Job 路径产生制品。因此本步不能宣称公开异步链已在部署中闭环，也不能代替
真实网关、真实引擎或对象存储的发布资格。

### 取消对账 worker 与开关

`job_cancellation_worker.py` 是独立进程入口，默认关闭：
`SPEECHNEXUS_JOB_CANCELLATION_RECONCILIATION_ENABLED=false` 时只打印关闭状态，
不创建连接池、不读取网关；设置为 `true` 时才要求数据库 URL/schema、网关 URL 和
credential 齐全。间隔、批量、最大尝试、最小受理年龄、租约和事务超时分别由
`SPEECHNEXUS_JOB_CANCELLATION_RECONCILIATION_{INTERVAL_SECONDS,BATCH_SIZE,MAX_ATTEMPTS,MIN_AGE_SECONDS,LEASE_SECONDS,TRANSACTION_TIMEOUT_SECONDS}`
控制，均有上界。`--once` 只跑一轮，便于部署探针；常驻模式由 SIGINT/SIGTERM 停止。

```bash
SPEECHNEXUS_JOB_CANCELLATION_RECONCILIATION_ENABLED=true SPEECHNEXUS_DATABASE_URL=postgresql:///speech SPEECHNEXUS_DATABASE_SCHEMA=speech SPEECHNEXUS_EXECUTION_GATEWAY_BASE_URL=http://gateway:8010 SPEECHNEXUS_EXECUTION_GATEWAY_COMPONENT_CREDENTIAL=... python -m speechnexus_backend.job_cancellation_worker
```

worker 与派发器共享同一条 gateway 契约和停止服务；它不会创建新 execution，也不会把
单独的 404、传输失败或未验证的 abort 当作停止。worker 角色需要 `public_job_grants`
（读取 Job/submission、cancellation receipt 并条件更新 `state/status_version`）与
`cancellation_reconciliation_grants`（围栏列 SELECT、账本读写、not-submitted SELECT）；
两者都必须在升级到 `0035` 之后应用。启动探针同时检查 `speech_jobs`、
`speech_job_submissions`、`speech_job_cancellations`、围栏、对账账本和 not-submitted 表，
任一缺失即拒绝启动。

### 结果发布语义（DEL-100）

发布以 `job_id + kind` 确定性派生 `artifact_id`，以 `job_id` 确定性派生 `receipt_id`，
对象键为 `private/artifacts/{workspace_id}/{artifact_id}`。发布先查询既有 Artifact 行；
命中时校验所属 workspace、类型、状态与 receipt 后返回原 `artifact_id`/`receipt_id`，
不会重新读取 staging 或重写对象。没有既有行时，读取版本固定的 staging 对象并核对长度/SHA-256，
再写入版本化 Artifact 桶并回读校验，最后才在短事务中提交 `speech_artifacts` ready 行；
对象 I/O 不在该事务内。DB 提交失败时对象可保留；
重试以同一对象键与内容复用既有版本，再提交同一行。对象已写入但 DB 未提交不返回成功。

### worker 入口与运行配置（DEL-98）

独立入口为 `speechnexus-job-dispatch-worker`，组合根在 `job_dispatch_runtime.py`，
不进入 `create_app` 的请求期装配。部署需显式提供下列变量；缺任意一项时
`JobDispatchSettings` 在连接数据库或网关前拒绝启动，进程不领取租约：

| 变量 | 含义 |
| --- | --- |
| `SPEECHNEXUS_PROCESS_ROLE` | 固定为 `job-worker` |
| `SPEECHNEXUS_NODE_ID` / `SPEECHNEXUS_INSTANCE_ID` | 显式 worker 身份；组合为 claim owner |
| `SPEECHNEXUS_DATABASE_URL` / `SPEECHNEXUS_DATABASE_SCHEMA` | 权威 PostgreSQL 与非 owner 运行角色 |
| `SPEECHNEXUS_EXECUTION_GATEWAY_BASE_URL` | 网关 origin，不允许路径、凭据或 query |
| `SPEECHNEXUS_EXECUTION_GATEWAY_COMPONENT_CREDENTIAL` | 网关组件凭据 |
| `SPEECHNEXUS_EXECUTION_GATEWAY_TIMEOUT_SECONDS` | 单次网关调用上限，有限数 `(0, 60]` 秒 |
| `SPEECHNEXUS_JOB_DISPATCH_BATCH_SIZE` | 单轮领取上限 1–32 |
| `SPEECHNEXUS_JOB_DISPATCH_LEASE_MS` | 领取租约 1–60 秒；不得用超长租约替代续租 |
| `SPEECHNEXUS_JOB_DISPATCH_POLL_SECONDS` | 执行轮询间隔，有限数 `[0.01, 30]` 秒 |
| `SPEECHNEXUS_JOB_DISPATCH_JOB_TIMEOUT_SECONDS` | 单 Job 总上限，有限数 `[0.01, 3600]` 秒 |
| `SPEECHNEXUS_JOB_DISPATCH_MAX_ATTEMPTS` | 最大领取版本 1–20 |
| `SPEECHNEXUS_JOB_DISPATCH_INTERVAL_SECONDS` | 空批次后的下一轮等待，有限数 `[0.1, 300]` 秒 |
| `SPEECHNEXUS_JOB_DISPATCH_STORAGE_TIMEOUT_SECONDS` | 领取、续租、终态事务的上限，有限数 `(0, 30]` 秒 |
| `SPEECHNEXUS_OBJECT_STORAGE_ENDPOINT` | 版本化对象存储 origin，不允许路径、凭据或 query |
| `SPEECHNEXUS_OBJECT_STORAGE_BUCKET` | Artifact 桶；必须启用版本化 |
| `SPEECHNEXUS_EXECUTION_RESULT_BUCKET` | 网关 staging 回执所在桶；按回执版本读取 |
| `SPEECHNEXUS_OBJECT_STORAGE_ACCESS_KEY` / `SPEECHNEXUS_OBJECT_STORAGE_SECRET_KEY` | 对象存储凭据，只从部署注入 |

安装 backend wheel 后以该独立入口启动；planner 装配完成后，`--once` 最多做一轮有界领取，
可用于获准环境的部署冒烟。当前组合根因 planner 缺失而在领取前退出，不能用它验证领取：

```sh
export SPEECHNEXUS_PROCESS_ROLE=job-worker
# 其余变量按上表显式注入，数据库与网关凭据从部署环境读取。
uv run speechnexus-job-dispatch-worker
```

每轮向 stdout 输出一行固定字段的 JSON：`event=job_dispatch_batch`、`claimed`、
`succeeded`、`failed`、`unknown`、`skipped`、`deferred`、`lease_reclaimed`、
`lease_lost`。只包含计数，不包含原命令、请求 ID、端点或凭据。

### 续租、接管与优雅停止

每次网关查询（包括查询后的下一步处理前）都以 `claim_owner_id`、`claim_version`
和 `pending` 协调行状态为条件续租。返回 0 行表示租约已被其他 worker 接管：本 worker
立即以 `claim_lost` 结束该 Job，不再 `finish`，也不再调用结果发布。任务超时仍按原
语义返回 `unknown`，不会把仍可能运行的引擎写成 failed。

终态写入先锁协调行，再在同一个事务中核对 owner、`claim_version`、租约未过期、公共
Job 的 `status_version` 和协调行 `pending`；只有公共状态、事件和协调关闭三者都更新
成功才提交，任一失败整笔回滚。旧 worker 在续租失败或被接管后无法写终态。续租延长
租约，但 Job 总期限不延长，避免用超长租约掩盖崩溃 worker。

SIGTERM/SIGINT 只设置停止标志：停止后不会开始下一轮领取；派发器在部分步骤前检查该标志，
但检查与外发之间存在竞态，不能保证收到信号后绝无新的提交。若轮询期间观察到停止标志，
本轮以 `deferred` 结束，不写终态也不发布，保留当前租约直到到期。下一 worker
在到期后按 `running` 原 Job 和同一 `submit_intent_id` 接手，不创建第二个执行。

## 迁移与最低权限

在锁定开发工具环境中执行：

```sh
uv sync --locked --all-packages --all-groups
# 由部署注入独立 DDL 身份；不使用非 owner 运行角色 DSN。
uv run speechnexus-job-migrate --schema speech
```

命令要求 `SPEECHNEXUS_MIGRATION_DATABASE_URL`；支持 `postgresql://` 或
`postgresql+psycopg://`。安装 wheel 的迁移环境需带 backend 的 `migrations` extra。
同一数据库/schema 的并发迁移以事务持有的 PG advisory lock 串行化；版本记录和
DDL 同事务提交。默认应用镜像不安装 Alembic/SQLAlchemy，运行角色没有持久表 DDL。
不支持破坏性 downgrade；回退应用或按明确恢复计划处理，不能删除幂等/执行证据。

部署先创建不继承管理员权限、非 owner 的运行角色，再以迁移 owner 审阅/安装
`job_schema.job_runtime_grants("speech", role)` 输出的 SQL。仅授予 schema USAGE、
Job/outbox SELECT 和创建所需列的 INSERT；没有 UPDATE、DELETE、持久表 DDL 或对版本表
的权限。已有角色的继承/全局授权需另核对，不能把 GRANT 列表当作有效权限证明。
池通过显式 schema 配置定位这些表，不接收客户端命名空间。
数据库级 PUBLIC TEMP 与其他继承授权按[存储/角色基线](../operations/production-coordination-storage.md)
单独核对；共享池显式将 `pg_temp` 置于末尾，防止同名临时表覆盖权威表。

## 证据与保护范围

```sh
uv run pytest tests/unit/test_production_jobs.py -q
uv run pytest tests/unit/test_production_claims.py -q
uv run pytest tests/unit/backend/test_result_publication.py -q
# 配置隔离 PG DSN 与 MinIO endpoint/keys 后：
uv run pytest tests/integration/test_result_publication_real_storage.py -q
# 先配置指向项目隔离实例的两个测试 DSN；integration-test 还需要隔离 MinIO endpoint 和测试密钥。
# 完整配置见 tests/integration/README.md。
make coordination-test
make integration-test
```

集成测试 [`tests/integration/test_production_jobs.py`](../../tests/integration/test_production_jobs.py)
在真实 PG adapter 与 InnoDB 共享 SQL 上执行同组创建、
完整身份冲突、outbox/约束失败回滚、最低权限和恢复测试。竞争测试先观测实际
`pg_blocking_pids` / InnoDB lock wait，再释放阻塞提交；不靠 sleep 推断发生竞争。
MySQL 仅将 PG `COLLATE "C"` 映射为 `utf8mb4_0900_bin`；不进入产品依赖。

集成测试 [`tests/integration/test_production_claims.py`](../../tests/integration/test_production_claims.py)
将同一生产 SQL/领域裁决用于 PG adapter 和 InnoDB
驱动 oracle，覆盖真实双 worker 行锁竞争、锁等待后重新取时、期限/版本冲突、
终态拒绝、溢出与 gateway 认领权限负例。PG 专属测试在实际提交成功后注入确认丢失，
证明只读恢复当前事实且没有第二次写入。合成既有协调行仅用于检查预算不变，
不代表配方准入、真实引擎或多主机 worker 已通过资格。

PG 专属故障测试终止真实会话验证提交前未知；已提交后通过测试连接类模拟丢失
回执，证明不自动重写且只读恢复原事实。这不等于真实网络丢包、HA/故障切换或
多主机资格。运行角色的负例要求权限错误，不能以其他 SQL 错误冒充拒绝授权。

ADR 0007 退出复核：本单元迁移 Job/outbox 原子性与幂等不变量。预算、fencing、
停止证据、生命周期和其他旧规则尚未全部迁移，旧机制退出条件未满足；本次保留
旧门禁，不将首个生产端口通过当成旧原型整体退役依据。
