---
title: 执行网关：提交、入口屏障与停止事务
status: active
version: "1.2.0"
date: 2026-10-23
---

# 执行网关：提交、入口屏障与停止事务

网关的第一个生产事务是 `create_submit`：先持久化唯一 intent，再向外发引擎请求；重放沿用原
intent，不能再次外发。PostgreSQL adapter 已按[应用事务方案与契约](coordination-transactions.md)
的锁序、摘要比较和未知结果规则实现，使用[生产协调存储基线](production-coordination-storage.md)
冻结的表与网关最小权限角色。`record_stop` 已原子保存停止证据与执行终态；
`GatewayIngress` 已将完整外发段与入口屏障串行，`seal_barrier` 已保存实际封锁后的最低 token。
输出计量、HTTP 提交/屏障入口与真实引擎装配仍待后续单元。本页区分已实现、已验证与未交付。

## 1. 代码位置与依赖

| 职责 | 位置 | 约束 |
| --- | --- | --- |
| 请求身份、状态/清理矩阵、停止证据裁决、租约校验规则 | `speechnexus_gateway.domain.submission` | 纯值对象与函数；不导入 SQL、驱动或生成 DTO |
| 提交/停止应用服务、重放与隔离对账 | `speechnexus_gateway.application.submit_execution` | 通过 domain 端口访问外部系统，不自己发 SQL |
| 入口排序、屏障请求与证明 | `speechnexus_gateway.application.ingress`、`domain.fencing` | 同一个 guard 覆盖落 intent、完整外发握手与结果保存；网络期间不占数据库事务 |
| 端口定义（`SubmissionLedger`、外发、观察） | `speechnexus_gateway.domain.ports` | 实现放 infrastructure 或测试 fake |
| SQL 语句、锁序、列映射与错误分类 | `speechnexus_gateway.infrastructure.{submit_sql,stop_sql,barrier_sql,submit_postgres,postgres_pool}` | 一次调用一个短事务；不复制预算或证据策略 |
| 同宿主网关进程独占 | `speechnexus_gateway.infrastructure.process_ownership` | Linux/POSIX 文件锁；同身份的候选进程必须共用宿主锁目录 |
| 组合根与运行入口 | `speechnexus_gateway.app`、`main` | 当前只装配运营健康检查；提交路由属于后续接口单元 |

网关是与 speech backend 独立的部署，因此不导入 backend 包；两边重复的只是已评审的池策略
（Read Committed、UTC、显式 `search_path` 且 `pg_temp` 置后、有界等待队列、导入时不连接）。

## 2. `create_submit` 事务

一次 `add(intent, request_document=…)` 就是一个 `create_submit`：

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
`version_conflict`（`query_current_authority`）。

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
   其他终态必须没有错误码。无外发事实不能伪装停止；未知隔离态须先协调，或以已验证中止闭合。
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
屏障；共享目录、凭据、HTTP 入口尚未接入部署，不能据此宣称已验证跨节点集群或宿主故障接管。

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

## 7. 未交付与已知缺口

- `mark_execution_unknown`、`record_execution_observation`、`redact_execution_command`、
  `append_interval` 及自身 `query_outcome` 的完整具名端口仍未交付；
  `seal_barrier` 的应用入口与持久事务已有，完整事务响应封装、HTTP 屏障/提交/中止路由、
  实际引擎 consumer 仍未交付。组合根尚未装配协调池与进程独占 lease。
- PostgreSQL 已验证并发停止、真实 pool 行锁等待与回滚；InnoDB 使用相同 SQL/领域规则的
  测试 adapter 验证迁移意图，不形成产品 MySQL 支持。数据库连接配置、时钟与异常分类仍在 adapter 内。
- 这里的证据是最小合成事实，仅证明受控事务与存储规则。真实引擎/控制器如何提供可信双证据、
  跨节点屏障、实际 abort、物理 GPU 停止及 A 的预算释放联调仍需单独取证。
- 提交端完整 deadline/运行资格校验仍需后续接入，当前没有公开执行入口，不可据本事务称为生产执行链路就绪。

## 8. 验证

```sh
# 需要同时设置隔离 PG 与 MySQL DSN；缺一不可，跳过不算通过。
SPEECHNEXUS_TEST_DATABASE_URL=… SPEECHNEXUS_TEST_MYSQL_URL=… \
  uv run pytest -m integration tests/integration/test_gateway_submit_ledger.py \
    tests/integration/test_gateway_stop_evidence.py tests/integration/test_gateway_barrier.py -q
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
