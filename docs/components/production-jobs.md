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

这里的 Job 是协调元数据，不是完整公开 Job 的业务命令/结果存储。后续单元已有公共路由、准入事务、派发器、生产 planner 和结果发布组件；真实引擎、GPU 与部署资格仍分别验收。不得把协调事务或单次同步合成证据解释为完整文件转写/异步合成闭环。

## 公开 Job 列表

`GET /v1/nexus/jobs`（`listJobs`）读取已提交的公开事实，按 `(created_at, id)` 升序。
省略 `workspace_id` 与 `createJob` 一样选择 `workspace-default`；显式值只过滤该 workspace，
合法但没有记录的 workspace 返回空列表，不将该过滤器用作身份或权限边界。
`status`/`kind` 按生成契约枚举校验，`queued` 对应内部 `pending`。
契约有效但当前生产者不产生的 `retry_wait` / voice 系列 kind 返回空列表。
`limit` 默认 50、有效范围 1–100；0、负数、超限或非整数字面量返回
400 `invalid_job_request`，不截断或静默忽略。

`next_cursor` 是版本化、确定性编码的最后一行 `(created_at_us, job_id)` 和过滤摘要。
它绑定 workspace/status/kind；继续分页须保持过滤条件，可调整 limit。
服务使用严格大于最后一行的 keyset 谓词并多读一行判断 `has_more`，末页游标为 null。
数据不变时同游标同页；页面是实时读取，跨页不承诺数据库快照隔离。
合法空页仍为 200；未装配或存储不可用为 503 `job_unavailable`。
每行复用 `getJob` 的生成契约校验与已提交制品投影。

列表页依赖 `speech_jobs_workspace_created_id (workspace_id, created_at_us, job_id)`，
`speech_jobs` 只有这一个 workspace 列表索引。迁移 `0042_drop_superseded_job_index`
删除被它覆盖的 `speech_jobs_workspace_created (workspace_id, created_at_us DESC)`：
旧索引的列是 keyset 索引的前缀，仓库内没有按 `created_at_us DESC` 排序的读路径，
保留它只会让每次写入多维护一份近重复索引。其余读取 `speech_jobs` 的路径——单 Job、
幂等键查找、取消锁目标、voice draft 关联与事件进度触发器——按主键或
`(workspace_id, idempotency_key_digest)` 唯一索引访问；派发认领以 `coordination_jobs` 为驱动，
对 `speech_jobs` 按 state/kind 过滤走顺序扫描（排序在内存完成）。这些路径都不依赖旧索引。
`0042` 的降级按原定义重建旧索引；两项迁移都只改索引，不改历史 Job、授权、
协调写事务或恢复语义。

隔离 PG 验证使用 `tests/integration/test_public_job_list_pg.py`：
仅读取 `SPEECHNEXUS_TEST_DATABASE_URL`，缺失即失败；随机 schema 在 finally 清理。
`uv run pytest tests/integration/test_public_job_list_pg.py -q -s` 会打印实际适配器 SQL 的
首屏/续页 `EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)`，不关闭 seqscan；用例同时断言
`speech_jobs` 只被一次 `Index Scan` 读取、方向为 `Forward`、计划无 `Sort`，
续页的 keyset 谓词出现在 `Index Cond`。
未执行该用例时不能宣称索引已被目标环境计划采用。

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
公共提交在同一事务写入 `speech_jobs`、事件、提交文档、权威公开 intent
`speech_job_intents`，以及通过原子准入产生的权威 `admission_operations`、attempt、
request、lease 和 `coordination_jobs`/`coordination_outbox`；`request_document` 是本次
准入写下的完整业务命令，不使用摘要或当前默认值重建请求。voice 生成类仍写非派发
副本并占用 operation 锚点，见[生产准入存储](production-admission-storage.md)。

授权分两段安装：`job_runtime_grants` 覆盖从 `0001_jobs` 起就存在的表，可在历史版本沙箱上直接应用；派发器额外需要的 `admission_operations` INSERT、`speech_job_intents` 的 SELECT/INSERT、`speech_artifacts` 的 SELECT/INSERT，以及 cancellation not-submitted 表的 INSERT 由 `job_dispatch_grants` 一并给出，**必须在升级到 head 之后**再执行——前者要到 `0007_admission_commands`、intent 表要到 `0046_public_job_intents`、后者要到 `0020_artifacts` 才存在，提前应用会让授权失败或掩盖升级顺序问题。Artifact 写入只授予显式列 INSERT 与读取既有行的 SELECT，不授予 UPDATE/DELETE 或 DDL；intent 表保持 append-only，同样不授予 UPDATE/DELETE。

派发器按 `FOR UPDATE OF c SKIP LOCKED` 批量领取到期的协调行，写入 owner、
`claim_version` 和租约；进程崩溃后租约到期可重新领取。一次公开 Job 对应一个由
Job ID 确定性派生的 `submit_intent_id`，领取者先查询原身份，只有 `not_found`
才尝试以同一身份提交；已经观察到执行后再查到 `not_found` 则保持 unknown。原身份去重不单独证明引擎只执行一次：网关接收与崩溃恢复仍须按提交屏障及固定命令取证。`unknown`、不可解析的回执、身份或命令摘要
不一致都返回 unknown 并保留 `running`，不当作可重试失败。
## 生产 planner、领取过滤与装配

`speech/infrastructure/stored_command_planner.py` 只在只读事务中重放准入已提交的事实：
`admission_operations.request_document` 是本次准入写下的完整业务命令，
`admission_requests` 保存 attempt/stage/lease/fencing/deadline 信封，
`admission_attempts` 与 `admission_leases` 提供绑定、策略和冻结资源。planner 先核对
命令与信封各自的摘要，再对 `gateway_request` 执行冻结契约的
`parse("SubmitExecutionRequest", ...)`，逐项核对 operation/attempt/stage/lease/
workspace/pool/host/gateway/instance/generation/binding/policy/fencing/deadline 与
准入行一致，并要求租约处于可提交状态。它不读取当前绑定版本、不补默认值、不改变
`submit_intent_id`；任何不一致都以协议错误停止，不按摘要或新默认值重建请求。

`PostgresDispatchQueue` 以 `require_admission=True` 装配时，只领取同一事务写下了
完整命令、attempt 和可用 lease 的 Job；租约必须仍是 `reserved`/`executing` 且
`cleanup_state='pending'`，已释放的租约不会再被领取。voice 生成 Job 仍有 operation 锚点但无
attempt/lease，因此不会被该通道领取，也不会对既有锚点再次调用
`admit_in_transaction`；voice 的独立 lanes 继续按显式 kind 领取。
`SPEECHNEXUS_JOB_DISPATCH_KINDS` 选择本进程的 kind 列表（默认 `asr,tts`，逗号分隔、
去重且必须属于可派发并集）。

`PostgresDispatchQueue.require_admission` 的默认值是 `False`，因为这个类也服务
voice 生成 lanes 与历史调用方，它们需要领取仅含 operation 锚点的行；生产 Job runtime
是唯一把它设为 `True` 的装配点，并在启动时断言该过滤确实生效，回退即拒绝启动。

`build_dispatch_dependencies` 装配真实 planner 与结果发布器并共享一个连接池；
对象存储 readiness、planner 构造或任何前置失败都包装为
`DispatchDependenciesUnavailableError`，worker 在领取任何租约之前以退出码 2 结束。
planner 与发布器均存在时才进入领取与提交。

派发器对已知提交的状态文档仍只在请求的 `command_digest` 和响应值都非 `null` 时比较
该摘要；响应缺少摘要不会被这一步拒绝，不能把现有检查当作完整的原命令一致性证明。

成功执行读取原回执后写引用型 `result_json`；网关终态失败冻结错误码。公共 Job
的 `status_version` 由条件 UPDATE 递增，终态与协调行关闭在同一事务。批次、租约、
轮询、提交超时、任务期限和领取次数均有显式上限。

asr/tts 的终态结算在这一事务之后走现有 `LeaseService.release`：成功需要网关 terminal
response、cleanup complete 和停止证据；失败沿 `failed/not_required` 或网关终态事实。
释放被拒绝或结果未知时租约保持占用并留给协调，不回写已提交的公共终态。
单次失败、超时或断线都不是释放证据。

终态提交与释放之间存在崩溃/失败窗口，本片不做自动重试。留下的持久事实是租约本身：
公共 Job 已 `succeeded`/`failed` 而 `admission_leases.lease_state` 仍非 `released`、
`budget_released_at_us IS NULL`，释放失败时 worker 也向 stderr 输出一条只含 job_id 的
warning（stdout 的批次 JSON 字段保持不变）。可用以下只读查询发现：

```sql
SELECT l.lease_id, l.operation_id, l.lease_state, l.deadline_at_us
FROM admission_leases AS l
CROSS JOIN (SELECT CAST(EXTRACT(EPOCH FROM clock_timestamp()) * 1000000 AS BIGINT) AS now_us)
    AS clock
WHERE l.lease_state <> 'released'
  AND l.operation_id LIKE 'job-%'
  AND l.deadline_at_us <= clock.now_us - 60000000
ORDER BY l.lease_id;
```

该查询与 preflight 的公开分支同条件，不带状态白名单：`pending`/`running`/`cancelling`
等任何已过冻结期限、仍被持有的公开租约都会出现；未过期限的活跃 Job 不会误报。
preflight 另要求无 settlement 根，因此它拒绝启动的那一行必然在这个结果里。

取消结算同样归还容量：`settle_cancelled` 提交 `cancelled` 后按原 `lease_id` 走同一
`LeaseService.release`（幂等）。释放被拒绝、结果未知或存储不可用时，该次结算返回
未完成，对账账本按退避重试同一个已 `cancelled` 的 Job，直到释放成功或 `max_attempts`
用尽；期间租约保持 held，上面的查询会列出该行。

冻结期限过后再留 60 秒结算窗口，仍被持有且无 settlement 根时，preflight 拒绝 API
启动，因此这一窗口最迟在期限后被运营侧发现；在此之前由该查询或日志人工巡检。
处理路径是按原 `lease_id` 调用既有 `LeaseService.release`（先 `query_lease` 核对
原身份），不得新建执行或改写公共终态；自动重试与扫描器归 [#528](https://github.com/hrygo/SpeechNexus/issues/528)。

网关长期不可用时，`max_attempts` 用尽后 Job 会停在 `running`：派发器不会把可能仍在
执行的 Job 写成 failed，本片也不主动结算这类终止态。其恢复依赖
[#528](https://github.com/hrygo/SpeechNexus/issues/528) 的时间超时结算与对账，运维侧
应先按该 issue 的范围处理，不能手工改成 failed 来释放容量。

过期提交使用准入冻结的 `deadline_at`，而不是进程自己估算的 Job 期限：先以稳定
`submit_intent_id` 查询网关，只有网关明确返回 404 且冻结期限已过，才写持久
`execution_deadline_expired` 终态并随后释放容量。仅期限到达、网关不可用或结果
未知时保持 `running` / unknown，不写终态也不释放。该 404 只证明该稳定身份没有
执行，不能推广为任意先前外发的停止证明。接管后的历史行若发现存储命令与准入
不符，也只有网关对该身份返回 404 时才可落 `failed`；网关显示执行存在或不可读时
保持 unknown，并不得写入取消的 not-submitted 事实。dispatcher 超时的持久结算与
对账归 [#528](https://github.com/hrygo/SpeechNexus/issues/528)。

派发器只通过 `speech/infrastructure/execution_gateway_adapter.py` 的
`ExecutionGatewayAdapter` 使用执行网关；适配器不持有 HTTP 实现，而是委托唯一
`speech/infrastructure/gateway_execution_client.py` 中的
`GatewayExecutionClient`。连接失败、超时、5xx 和不可解析响应保持“结果未知”
（分别进入 `DispatchUnavailableError` / `DispatchProtocolError`）；只有通过冻结错误
契约校验的 4xx 语义拒绝经 `DispatchRejectedError`（`DispatchProtocolError` 子类）进入
终态失败，缺少合法错误码的 4xx 仍是 `DispatchProtocolError` / `unknown`。查询或回执
的 404 仍是 `not_found`，408/429 保持 `unknown`。不得把 `unknown` 映射成 `failed`。
进入网关阶段之前，若持久请求缺失、摘要不符或 planner 生成不合法提交，代码会先走
`job_request_unavailable` 或 `execution_result_invalid` 分支；对 `running` 的历史 Job
必须先向网关确认该 `submit_intent_id` 没有执行（404）才能落 `failed`，否则保持
unknown 且不写 not-submitted 事实。这些错误不能作为执行停止或预算释放证据。语音生成
Job 还会在 `mark_running`、外发与发布之前校验存储请求的 `kind` 和 `draft_target` 是否
满足冻结契约；缺少匹配 `kind` 的历史行在确认无执行后失败，不会提交执行、发布孤儿
Artifact 或长期停留在 `running`。

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

派发器在已领取 Job 后观察到受理时不得再提交；它只在能证明本次派发从未越过提交点时
才于 `speech_job_cancellation_not_submitted` 幂等写入 "已观察受理但未提交" 的持久事实：
领取时 Job 仍为 `pending`（本进程才把它标成 `running`），或网关对固定 identity 明确
返回 `control_execution_not_found`，或网关屏障拒绝本次提交。首次查询已经返回执行状态、
查询不可读、提交应答丢失这三种情况都可能隐藏一次真实提交，一律不得写成 not-submitted。
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
停止证据；只有与派发器的 not-submitted 观察同时存在时，404 才被当作无执行事实；
空 body 的 router/proxy 404（不含 `control_execution_not_found`）也不是该事实，
按不可用重试。

结算提交 `cancelled` 后，组合根以原 `lease_id` 调 `LeaseService.release`（`PostgresLeaseSettler`
解析唯一租约并重放版本冲突）。释放成功才把本轮记为 `settled`；被拒绝、未知或存储异常
时返回未完成，账本按退避重试该行（Job 已是 `cancelled` 时重试只做释放），`max_attempts`
用尽后停止重试并保留 held 租约，由上面的只读查询与 preflight 暴露。取消受理本身仍不
直接释放容量：只有完成上述无执行或停止证据后，释放才发生。

迁移 `0035_job_cancellation_fences` 的 `down_revision` 是
`0034_public_job_cancellations`，形成单一 head；down 在 reconcile 账本、not-submitted
观察或围栏仍有行时拒绝删除，要求先保留或导出证据。`public_job_grants` 为 API Job writer
授予围栏 SELECT/INSERT 与 `speech_artifacts` 的 SELECT；`job_dispatch_grants` 的取消围栏部分只授予派发器对 not-submitted 表的 INSERT，其 Job 写入部分另含 `admission_operations` INSERT 与 `speech_job_intents` 的 SELECT/INSERT；
worker 角色使用 `public_job_grants` 与 `cancellation_reconciliation_grants`（围栏列 SELECT、
对账账本 SELECT/INSERT/列级 UPDATE、not-submitted SELECT）。派发器的终态结算与取消对账
的释放都需要 `coordination_runtime_grants` 给出的 `admission_leases`/`admission_attempts`/
`workspace_quotas`/`host_nodes`/`engine_instances` 读取与版本列 UPDATE、
`submit_intents` 的受限列 SELECT、`execution_stop_evidence` SELECT 和
`execution_release_guards` 视图列 SELECT；两者使用同一 runtime 角色时只安装一次。gateway 写角色在
`coordination_runtime_grants` 之外还须安装 `gateway_cancellation_fence_grants` 以获得围栏
`job_id` 列 SELECT（提交只判断存在性），独立执行读取器使用同一函数。

接线已经落地：

1. `PostgresPublicJobs.cancel` 在写入 `cancelling`、事件和 receipt 后，于同一事务调用
   `write_cancellation_fence`；它是该事务最后一个会等待锁的写入，之后不再取
   `admission_leases`/`runtime_instance*` 行锁。
2. `job_cancellation_runtime.py` 把 `PublicJobs`、PG 对账账本、`PostgresLeaseSettler`、
   `GatewayCancellationConfirmer` 和 `CancellationReconciliationWorker` 装配在一起；
   `settle_cancelled` 走 `PublicJobs` 的 `status_version` 条件更新并随后归还租约，活跃
   执行通过 `JobCancellationService` + `GatewayJobCancellationStopper` 注入一次有界停止
   尝试。
3. 派发器在提交点前观察到 Job 已是 `cancelling` 时写入 not-submitted 观察并返回
   `claim_lost`；收到 `gateway_barrier_fenced` 且 Job 仍是 `cancelling` 时返回 `skipped`，
   不调用 `_fail`。两种情况都由对账 worker 以网关“没有执行”事实结算为 `cancelled`。
   Job 未取消时同一错误码仍按普通终态失败处理。

当前边界完成派发器、真实 PG 领取/终态存储、生产 planner、唯一网关客户端（派发器经
适配器接入）、worker 入口/进程角色及 `create_app` 之外的组合根。`build_dispatch_dependencies`
装配真实 planner 与结果发布器；任何前置失败仍在领取前以退出码 2 结束，不会用 fake
或空实现硬跑。`GET /v1/nexus/artifacts/{id}` 的读路径与 Job 路径的制品引用已由发布器
写入。该组合仍不能代替真实网关、真实引擎、GPU 或对象存储的发布资格，也不能据此宣称
部署中的公开异步链已经完成端到端验收。

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

## 重试与事件流

`POST /v1/nexus/jobs/{job_id}/retries` 以新 Job 承接终态任务，不复活旧任务。可重试状态
为 `failed`（排除 `cancellation_unconfirmed`）、`cancelled` 与 `expired`；其余状态返回
`409 job_retry_not_allowed`，`cancellation_unconfirmed` 返回 `409 execution_cleanup_pending`。
新 Job 复制父任务已提交的规范请求文档与摘要（顶层字段，不包一层），重新解析模型绑定、
功能策略与目录等系统默认值，并把实际使用的配方写入自身 `effective_configuration`；
父任务行不被更新，其终态事实与状态守卫保持原样。请求意图已被内容删除时返回
`409 job_retry_unavailable`，不用当前默认值重建。

血缘落在 `speech_jobs.retry_of_job_id`：自引用外键、`ON DELETE RESTRICT`、`CHECK`
不等于自身、索引 `speech_jobs_retry_of`，替换后的状态守卫禁止改写该列。公开 `Job`
形状不变（契约对象附加属性关闭），血缘只在持久层可查。同一幂等键重放返回原新 Job，
不产生第二次业务执行；同一键换父任务、或与创建入口的同一键交叉使用返回
`409 idempotency_conflict`。迁移 `0037_public_job_retries`（后接
`0038_voice_catalogue_reader`、`0039_public_job_progress`）依次叠加；`0037` down 在存在
重试血缘行时拒绝删除列，并先恢复 `0034` 的状态守卫；`0039` down 在存在进度事实时拒绝
删除进度列。该拒绝把 0039 的可回滚窗口限定在首个进度事实提交之前：进度事实是执行证据，
不删除也不重算，所以生产流量一旦写下过一条进度行，回退到 0038 就只能走回滚前备份恢复
或向前修复，不能原地 downgrade。

`GET /v1/nexus/jobs/{job_id}/events` 只读地回放持久事件：按
`speech_job_events.event_seq` 升序，线路 `id` 为 `event_seq + 1`（契约
`EventSequence` 从 1 起），`data` 为契约 `JobEvent`。无 `Last-Event-ID` 时从最早事件读起；
续读以 `Last-Event-ID - 1` 为游标，非法值返回 `400 invalid_job_request`，未知 Job 返回
`404 job_not_found`。事件行与对应状态变更在同一事务提交，因此终态事件只出现一次；
读到终态且当前页为空后流关闭，已消费的终态不会重放。契约只为历史过期声明
`410 event_history_expired`；当前实现没有事件保留或过期策略，因此永不返回该状态，客户端仍须
按契约在收到 `410` 时改读当前 Job 状态。

### 进度语义

进度是持久的观测事实，不是按配置推算的百分比。唯一事实来源是执行网关返回的
`ExecutionStatusResponse.output_watermark.generated_pcm_samples`：它取自网关持久的
output interval 账本，只随实际生成音频单调前进；契约规定毫秒数为
`floor(generated_pcm_samples * 1000 / sample_rate_hz)`。派发器只在 TTS Job 上、且该
毫秒值严格大于本次已上报值时，向 `speech_job_events` 追加一行
`kind='progress'`、`state='running'`、`progress_unit='audio_ms'` 的事实；同值重复轮询、
乱序回退或 Job 已离开 `running` 都不写。当前 file execution 在登记结果时一次性追加完整
generated 区间，因此这条路径通常只产生一条终态前的进度事实；执行侧若以增量区间推进
watermark，则每次前进都会产生一条新事实。ASR 在终态前没有已转写段数或其他可持久化
进度事实，因此只产生生命周期事件，不发送 `job.progress`，也不估算进度。

`Job` 的 `progress` 取该 Job 最新一条进度事实；尚无事实时使用准入时冻结的单位
`audio_ms` 和 `completed=0`。本次迁移前已受理的 Job 保留其已冻结的进度单位；若该单位
不是 `audio_ms`，写入路径不会用另一单位的事实覆盖它，因此该 Job 不产生进度事件。
`total` 为空：请求文本长度是输入规模而非完成总量，网关只给出生成上限而非期望时长，
二者都不能充当事实分母。事件行仍以 Job 内连续的
`event_seq` 排序，SSE 线路 `id` 继续是 `event_seq + 1`，重启后按同一游标重放而不重算或
重发同值。数据库守卫拒绝终态 Job 的进度行、重复或回退值以及不连续序号；终态事件之后的
进度行无法写入。迁移 `0039_public_job_progress` 为事件表增加 `kind`、`progress_unit`、
`progress_completed`、`progress_total` 与部分索引，不新建第二套状态机。`kind` 以
`DEFAULT 'lifecycle'` 加入：升级时既有事件行一律回填为 `lifecycle`，进度列保持 NULL，
`event_seq` 不变；只有升级后新追加的进度行写 `kind='progress'`。进度行不改写
`speech_jobs` 的状态、`status_version` 或 `updated_at`；Job 的 `updated_at` 仍是最近一次
生命周期变更的时间。派发器启动探针在选择事件列时包含这些进度列，未应用迁移时拒绝启动。

成功 Job 的终态事件与 `Job` 投影都要求结果引用的 `speech_artifacts` ready 行存在且
`receipt_id`、类型匹配；缺失时返回 `503 job_unavailable`，不伪造成功。该错误声明
`retryable: false`：Artifact 可能已过期或删除，重复读取不会改变结论，客户端应改读当前 Job
状态而不是原地重试。因此 API Job writer 角色需要 `speech_artifacts` 的 SELECT，
`job_runtime` 启动探针也把它列入必需表。

### 心跳、背压与断线语义

空闲流按固定间隔发送 SSE 注释帧 `: keepalive`。它是注释而不是事件：不带 `id` 或
`data`，不写事件表，不改变线路 `id` 和事件顺序。间隔由
`SPEECHNEXUS_JOB_EVENT_HEARTBEAT_SECONDS` 配置，默认 15 秒，取值 `(0, 60]` 秒；
只有距上次心跳达到间隔时才插入，事件帧照常优先发送，心跳不会出现在某个事件帧内部。
等待按剩余期限计算：轮询间隔与心跳间隔不整除时（例如 0.5 秒轮询、0.51 秒心跳），心跳在
约 0.51 秒而不是下一个完整轮询周期后发出，读取耗时不会把间隔拖后整整一个轮询周期。

流是分页拉取式的：每次从事件表读取至多 `_EVENT_BATCH`（100）条已提交事件，本页帧全部
写出后才读取下一页。因此单连接在进程内至多持有一页投影帧（≤100 帧）加一帧在途。
单帧大小由事件数据推出，而不是由请求体推出：最大的帧是 `job.succeeded`，其 `result` 来自
`speech_jobs.result_json`，该列有 `OCTET_LENGTH BETWEEN 2 AND 8192` 的 DB CHECK；
`artifacts` 至多一个，其投影字段同样受列约束（`artifact_id`、`media_type`、`receipt_id`
≤128 字节，`sha256` 71 字节，`expires_at` 至多 30 字节），连同事件信封合计不到 10 KiB。
于是每连接在进程内
的事件帧缓冲上限约为 `100 × 9 KiB + 一帧在途 ≈ 0.9 MiB`，并非按请求体上限推算。这是
**每连接**预算：并发连接各自持有一份，服务端不设连接数上限，总量随并发连接线性增长，
由部署侧的连接数与内存预算约束。uvicorn 传输层另有 64 KiB 写缓冲高水位。
慢客户端不会造成无界缓冲：若一帧在 `SPEECHNEXUS_JOB_EVENT_SEND_TIMEOUT_SECONDS`（默认
10 秒，取值 `(0, 60]` 秒）内无法写出，连接被判为慢客户端，服务端关闭事件源，然后中止
套接字（不等待写缓冲排空，否则不读的客户端会让连接一直占着文件描述符；uvicorn 下由应用
外层中间件捕获该请求的传输并在超时后中止，没有该能力的服务器退化为普通关闭）。源关闭
自身受 5 秒独立上限约束：超时只停止等待，随后的中止与告警照常执行，不会被挂起的源
无限期推迟（前提是源遵守取消语义：`asyncio.timeout` 只取消一次，吞掉取消后继续等待的源
仍会让请求任务停在关闭步骤）。该捕获要求 `RequestIdMiddleware` 保持用户中间件中最外层：
装配时若不成立（例如后来新增了包装 `receive` 的 ingress 中间件），应用创建即报错，不会
静默失去中止能力。同时记录一条结构化 WARNING `job_event_stream_backpressure`，字段只有
`event`、`reason=slow_client`、`send_timeout_seconds`、`close_timed_out`（源关闭是否
因超过该上限
被放弃：`true` 表示关闭未在限期内返回，连接仍会中止）与 `connection_reset`（本次是否
真的中止了连接：只有 `abort()` 成功才为 `true`，未装配 hook 或中止抛错时为 `false`，
此时套接字交给服务器的优雅关闭），不含 Job、请求或客户端身份。超时覆盖该流的每一次
ASGI send，包括终态的空 body：末帧与数据帧共用同一写缓冲，同样可能永久等待。被丢弃的
未发送帧不影响持久化事实：
客户端以最后收到的 `id` 续读即可补齐。关闭连接不改变任何事件行或 Job 状态，也不表示任务
终止。

代理与负载均衡：心跳默认 15 秒，代理的空闲读超时必须显著大于该值，建议
`proxy_read_timeout` 不低于 60 秒（至少为心跳间隔的两倍）；调小心跳时同步调整。
`text/event-stream` 必须关闭响应缓冲（如 nginx `proxy_buffering off`，或由代理注入
`X-Accel-Buffering: no`），否则事件会被攒批，心跳也会失去作用。代理层的读超时只会
表现为流被断开，恢复规则见下。

断线语义：流关闭（终态、慢客户端或网络中断）都不表示任务结束。客户端以最后收到的
`id` 作为 `Last-Event-ID` 重连，服务端严格从其后回放，因此不重复、不跳过。终态事件
已消费后再重连得到空流并立即关闭，客户端应改读
`GET /v1/nexus/jobs/{job_id}` 确认当前状态。心跳注释没有 `id`，不能作为续读游标。

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
| `SPEECHNEXUS_JOB_DISPATCH_KINDS` | 本进程领取的 kind 列表，默认 `asr,tts`；逗号分隔、去重且属于可派发并集 |
| `SPEECHNEXUS_OBJECT_STORAGE_ENDPOINT` | 版本化对象存储 origin，不允许路径、凭据或 query |
| `SPEECHNEXUS_OBJECT_STORAGE_BUCKET` | Artifact 桶；必须启用版本化 |
| `SPEECHNEXUS_EXECUTION_RESULT_BUCKET` | 网关 staging 回执所在桶；按回执版本读取 |
| `SPEECHNEXUS_OBJECT_STORAGE_ACCESS_KEY` / `SPEECHNEXUS_OBJECT_STORAGE_SECRET_KEY` | 对象存储凭据，只从部署注入 |

安装 backend wheel 后以该独立入口启动；`--once` 最多做一轮有界领取，可用于获准环境的
部署冒烟。planner 或对象存储未就绪时，进程在领取前以退出码 2 结束：

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
uv run pytest tests/unit/backend/test_public_job_event_stream.py -q
# 配置隔离 PG DSN 后，真实 uvicorn + httpx 连接覆盖分页、心跳、断线与续读：
uv run pytest tests/integration/test_public_job_retry_sse_pg.py tests/integration/test_public_job_event_stream_tcp.py -q
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
