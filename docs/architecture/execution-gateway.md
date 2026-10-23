---
title: 执行网关：提交事务与持久意图
status: active
version: "1.0.0"
date: 2026-10-24
---

# 执行网关：提交事务与持久意图

网关的第一个生产事务是 `create_submit`：先持久化唯一 intent，再向外发引擎请求；重放只读原
intent，不能再次外发。PostgreSQL adapter 已按[应用事务方案与契约](coordination-transactions.md)
的锁序、摘要比较和未知结果规则实现，使用[生产协调存储基线](production-coordination-storage.md)
冻结的表与网关最小权限角色。停止证据、输出计量、屏障和网关 HTTP 提交入口仍待后续单元，
本页区分已实现、已验证与未交付。

## 1. 代码位置与依赖

| 职责 | 位置 | 约束 |
| --- | --- | --- |
| 请求身份、状态/清理矩阵、停止证据裁决、租约校验规则 | `speechnexus_gateway.domain.submission` | 纯值对象与函数；不导入 SQL、驱动或生成 DTO |
| 提交/停止应用服务、重放与隔离对账 | `speechnexus_gateway.application.submit_execution` | 只依赖 domain 端口，不自己发 SQL |
| 端口定义（`SubmissionLedger`、外发、观察） | `speechnexus_gateway.domain.ports` | 实现放 infrastructure 或测试 fake |
| SQL 语句、锁序、列映射与错误分类 | `speechnexus_gateway.infrastructure.{submit_sql,submit_postgres,postgres_pool}` | 一次调用一个短事务；不复制预算或证据策略 |
| 组合根与运行入口 | `speechnexus_gateway.app`、`main` | 当前只装配运营健康检查；提交路由属于后续接口单元 |

网关是与 speech backend 独立的部署，因此不导入 backend 包；两边重复的只是已评审的池策略
（Read Committed、UTC、显式 `search_path` 且 `pg_temp` 置后、有界等待队列、导入时不连接）。

## 2. `create_submit` 事务

一次 `add(intent, request_document=…)` 就是一个 `create_submit`：

1. 按全局顺序取行锁：`resource_pools` → `admission_attempts` → `admission_leases`，每条语句
   都必须命中一行，否则按未知身份拒绝；未释放行锁前不做任何写入。
2. 锁后重新读取租约/attempt 权威事实，逐字段核对身份：lease/operation/attempt/stage/workspace/
   pool/host/gateway/instance/binding/policy 不一致为 `invalid_execution_request`，generation 不一致为
   `instance_generation_mismatch`，token 不一致为 `stale_fencing_token`。
3. 租约必须处于 `reserved` 或 `executing`：`stopping`/`released`/`quarantined` 一律
   `lease_not_active`，不因请求已排队而放行。
4. 已封屏障 `gateway_fencing_barriers.enforced_min_fencing_token` 高于本次 token 时
   `gateway_barrier_fenced`。屏障行不存在时不假装入口已封锁；屏障与新任期由 `seal_barrier`
   和 `activate_owner` 负责。
5. 写入一行 `submit_intents`（含规范正文与完整请求摘要），需要输出流时再写
   `output_stream_heads`。同一 (operation, attempt, stream) 已有流权威行时只校验并复用其
   采样率，不重复建行，也不按 submit ID 分开计量。
6. 提交。已存在的 intent 或已占用的租约让 INSERT 冲突，adapter 报
   `idempotency_conflict`（`query_original`），绝不把旧记录当作本次创建成功返回，调用方必须
   查询原身份再决定是否外发。

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
| `submit_intent_id`、`lease_id`、`operation_id`、`attempt_id`、`command_digest`、`voice_revision_id`、`deadline_at_us` | 请求身份；`deadline_at` 以 UTC Unix 微秒存储，非 UTC 或负值拒绝 |
| `request_digest`、`request_document` | RFC 8785 规范化的完整有效请求（`execution_request` + `output_stream_id`）及其 SHA-256 |
| `recorded_at_us`、`updated_at_us`、`version` | 应用服务首次记录时间；此后每次状态变化加一 |
| `execution_state`、`cleanup_state`、`engine_request_id`、`dispatched_at_us`、`error_code` | 状态矩阵与 `save` 条件写入的列 |
| `output_stream_id`、`sample_rate_hz`、`hard_output_ceiling_samples` | 只有发出 PCM 的阶段才有；上限为 `ceil(max_output_audio_ms × sample_rate_hz / 1000)`，反解要求整毫秒，否则拒绝改写冻结预算 |
| `staging_output_object_key`、`terminated_at_us`、`generated_pcm_samples`、正文清除列 | 本单元不写；计量、终止与删除墓碑由 `append_interval`、`redact_execution_command` 端口负责 |

规范正文只在创建时写入、不在读取投影中返回，避免把私有业务内容带进查询路径；它保留在
部署网络内，不写日志或公开投影。摘要比较针对**完整有效请求**，因此只改 deadline、token 或
输出流而不改命令的同 ID 重放同样按 `submit_intent_conflict` 拒绝。

## 4. 权限边界

运行使用非 owner 的 gateway 角色，只持有 `coordination_runtime_grants` 生成的清单：
`submit_intents`、`output_stream_heads`、`gateway_fencing_barriers`、`execution_stop_evidence`、
`output_sample_intervals` 的 SELECT/INSERT，`submit_intents` 与屏障的运行投影列 UPDATE，以及为
行锁所需的 `UPDATE (version)`。角色没有表级 UPDATE、DELETE、TRUNCATE 或 DDL；身份、摘要、
正文和冻结输出边界不在 UPDATE 清单内。集成测试对 `command_digest`、`output_stream_id`、
`DELETE`、`TRUNCATE` 逐条断言被拒绝。

## 5. 未交付与已知缺口

- `record_stop` 端口未实现。已验证停止是"追加证据行 + 关闭执行"的一对事实；本单元的 `save`
  只写 `submit_intents`，遇到 `engine_abort_ack`、`instance_terminated_by_controller`、
  `operator_verified_instance_terminated` 会直接拒绝，不写一个没有证据行支撑的终态。因此生产存储上
  尚不能关闭执行，未知执行继续占预算。
- `mark_execution_unknown`、`record_execution_observation`、`redact_execution_command`、
  `append_interval`、`seal_barrier` 仍未实现；网关没有 HTTP 提交/中止路由，也没有 worker
  消费这些端口。组合根尚未装配该池（无消费者），接口单元接入时再按本节边界打开。
- 只验证单网关、单节点、真实 PostgreSQL。真实引擎容器、多网关并发、跨节点屏障收敛、
  真实 deadlock 重试与 MySQL adapter 均未验证；InnoDB 只验证共享 schema 与约束意图。
- 到期拒绝没有契约错误码，本单元不为它发明一个：deadline 作为请求身份参与摘要与恢复，
  逾期由租约 TTL 与 abort 路径处理。

## 6. 验证

```sh
# 需要同时设置隔离 PG 与 MySQL DSN；缺一不可，跳过不算通过。
SPEECHNEXUS_TEST_DATABASE_URL=… SPEECHNEXUS_TEST_MYSQL_URL=… \
  uv run pytest -m integration tests/integration/test_gateway_submit_ledger.py -q
uv run pytest -m "not integration" -q
uv run ruff format --check . && uv run ruff check . && uv run mypy && uv run lint-imports
```

`test_gateway_submit_ledger.py` 在真实 PostgreSQL 上、以非 owner 的 gateway 角色运行：创建后
重放不产生第二行也不再外发；新进程（新池）读回同一 intent；`save` 版本守卫与陈旧写入失败；
封屏障/非活动租约/未知租约/流采样率冲突各自拒绝；两 stage 共享一条流权威行；无音频阶段不建流、
无上限；管理员改坏 `sample_rate_hz` 后读取报 `reconciliation_required`；另一个连接持有池行时提交
事务真实等待该行锁，释放后才提交。InnoDB 分支按设计跳过（adapter 是 PostgreSQL 专用），不构成
MySQL 支持承诺。
