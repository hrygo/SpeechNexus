# 18 个协调端口与消费者证据快照

本报告完成 [A01 #20](https://github.com/hrygo/SpeechNexus/issues/20) 的
`A-COORD-EVIDENCE-01` 取证卡，关联 [A03 #22](https://github.com/hrygo/SpeechNexus/issues/22)。
核验日期：2026-10-26（Asia/Shanghai）；冻结源码为
`7dc276a4cfa38e8f433def05b0cbed25dcec8d5d`，即 [PR #169](https://github.com/hrygo/SpeechNexus/pull/169)
合入后的 main。后续提交不自动继承此报告的运行结论。

结论：18 端口均可定位责任和实现入口，关键共享 SQL/领域行为有隔离 PG/InnoDB
证据；完整 Scheduler/Worker 消费、全部统一结果边界的双库对照和真实执行联合验收仍缺。
本卡完成证据整理，不完成 A01/A03、总控 AC 或任何 Gate。

## 事实来源与证据等级

- 唯一所有权源：[机器矩阵](../../contracts/coordination-ownership.json)；请求与结果形状：
  [协调契约](../../contracts/internal/coordination-transactions.schema.json)。不在本报告复制字段默认值或写权限。
- A 的生产代码根：[speech](../../backend/src/speechnexus_backend/speech/)；B 的生产代码根：
  [gateway](../../execution-gateway/src/speechnexus_gateway/)。下表的相对模块名均从对应代码根解析。
- 正式产品数据库是 PostgreSQL；InnoDB 经 [测试驱动](../../tests/support/gateway_database.py)
  和 `tests/support/` 其他适配层执行同源 SQL/领域规则，不是产品 MySQL adapter。
- **协议边界**指实际调用 `validate_coordination` 并返回 `TransactionResult` 的接口；
  **持久化**指真实数据库事务；**业务装配**指正式组合根及调用循环。
  三者分别取证。脚本引擎、注入 readiness、合成停止、测试直接 SQL 预置不算真实执行资格。

## 18 端口映射

表中测试名称位于 [集成目录](../../tests/integration/)。模块内既有直接 SQL/adapter
断言，也有协议边界断言；模块通过不代表每个情形都经过统一结果封装。具体数量和限制见后文。

| 端口 / owner | 协议边界 → application | 正式存储入口 | 本版证据与实际消费者边界 |
| --- | --- | --- | --- |
| acquire_owner / A | `interfaces/owner_transactions.py:OwnerTransactionBoundary` → `OwnerService.acquire` | `infrastructure/owners_postgres.py:PostgresOwnerTransactions.acquire_owner` | `test_production_owners.py`；完整权威快照有断言，Scheduler 装配待接 |
| renew_owner / A | 同边界 → `OwnerService.renew` | 同 adapter 的 `renew_owner` | 同模块；锁后数据库取时、版本/过期拒绝，持续续约循环待接 |
| activate_owner / A | 同边界 → `OwnerService.activate` | 同 adapter 的 `activate_owner` | `test_owner_activation_quarantine.py`；A 隔离记账/阈值/回滚有双库证据，生产屏障与接管循环待联验 |
| admit_recipe / A | `interfaces/admission_transactions.py:AdmissionTransactionBoundary` → `AdmissionService.admit` | `infrastructure/admission_postgres.py:PostgresAdmissionTransactions.admit_recipe` | `test_production_admission.py`；完整阶段、Job/outbox 原子性和原结果快照；权威配方/业务策略调用方待接 |
| create_job / A | `interfaces/job_transactions.py:JobTransactionBoundary` → `CreateJobService.create/recover` | `infrastructure/jobs_postgres.py:PostgresJobTransactions.create_job/recover_create_job` | `test_production_jobs.py`、`test_job_recovery_unknown.py`；协调 JobFact，不是公开 SpeechJob |
| claim_job / A | `interfaces/claim_transactions.py:ClaimTransactionBoundary` → `ClaimService.claim` | `infrastructure/claims_postgres.py:PostgresClaimTransactions.claim_job` | `test_production_claims.py`；两连接竞争/过期版本/原结果查询，业务 Worker 循环待接 |
| quarantine_lease / A | `interfaces/lease_transactions.py:LeaseTransactionBoundary` → `LeaseService.quarantine` | `infrastructure/leases_postgres.py:PostgresLeaseTransactions.quarantine_lease` | `test_production_leases.py`；消费原生 B unknown 事实且保留总占用，真实 reconciler 待接 |
| release_lease / A | 同边界 → `LeaseService.release` | 同 adapter 的 `release_lease` | `test_lease_release.py`；停止/未外发事实、并发最后 stage 仅释放一次，真实停止来源和业务结算待联验 |
| create_runtime_intent / A | `interfaces/runtime_transactions.py:RuntimeTransactionBoundary` → `RuntimeService.create` | `infrastructure/runtime_postgres.py:PostgresRuntimeTransactions.create_runtime_intent` | `test_production_runtime_intents.py`；固定完整计划、预算/outbox/私有投递账本，实际批准来源待接 |
| observe_runtime / A | 同边界 → `RuntimeService.observe` | 同 adapter 的 `observe_runtime` | `test_runtime_observations.py`；原观察/资格来源匹配、CAS、回滚与预算，可信生产 readiness 和观察消费待接 |
| seal_barrier / B | `interfaces/barrier_transactions.py:GatewayBarrierBoundary` → `GatewayIngress.seal` | `infrastructure/submit_postgres.py:PostgresSubmissionLedger.seal_barrier` | `test_gateway_barrier_transactions.py` 统一结果仅 PG；`test_gateway_barrier.py` 共享行为双库。正式 HTTP 经 `GatewayBarrierService`，不经此统一结果边界 |
| create_submit / B | `interfaces/submit_transactions.py:ExecutionFactBoundary` → `SubmitExecutionService.submit` | 同 ledger 的 `add` → `submit_sql.create_submit_in_transaction` | `test_gateway_fact_transactions.py` 统一结果仅 PG；准入联验直接 ledger 双库。正式 HTTP 已装配 SubmitExecutionService，真实模型执行另验 |
| mark_execution_unknown / B | 同边界 → ledger | 同 ledger 的 `mark_execution_unknown` | 同 PG 统一结果模块；`test_gateway_recovery.py` 双库。abort 路径可调用 ledger，不代表 A 完整恢复循环已接 |
| record_stop / B | 同边界 → `SubmitExecutionService.record_stop` | 同 ledger 的 `record_stop` | PG 统一结果与 `test_gateway_stop_evidence.py` 双库；abort/recovery 代码有正式调用，合成停止证据不证明物理停止 |
| record_execution_observation / B | `interfaces/observation_transactions.py:ExecutionObservationBoundary` → `ExecutionObservationService` | 同 ledger 的 `record_execution_observation` | `test_gateway_observation.py` 的双库测试实际消费统一结果边界；原生提交服务也调用 ledger，可信观察与实际执行链仍分验 |
| redact_execution_command / B | `interfaces/command_redaction_transactions.py:ExecutionCommandRedactionBoundary` → `ExecutionCommandRedactionService` | 同 ledger 的 `redact_execution_command` | `test_gateway_command_redaction.py` 双库；授权器及 A 已释放行由测试预置。无正式删除 coordinator 装配，不开放 HTTP |
| append_interval / B | `interfaces/output_interval_transactions.py:OutputIntervalBoundary` → `OutputIntervalService` | 同 ledger 的 `append_interval` | `test_gateway_output_intervals.py` 双库含统一结果/查询；正式 meter 与 A 业务结算消费者待接 |
| query_outcome / fact-owner | 按下表分派原身份 | 各 owner 的只读查询 | 7 个 target 是同一端口的分支，不再额外算 7 个端口；不得读取失败后发起替代执行 |

### 正式调用方的核对方法

冻结树中，A 的上述六类 application service 和六类 boundary 尚无生产组合根构造调用；
不能由测试创建实例推断 Scheduler/Worker 已实现。可复核搜索：

```bash
rg -n 'OwnerService\(|AdmissionService\(|CreateJobService\(|ClaimService\(|LeaseService\(|RuntimeService\(' backend/src
rg -n 'TransactionBoundary\(' backend/src
```

无匹配时 `rg` 返回 1。此结论限定上述正式类型；不否认已有独立资产、配置或其他服务。
[runtime_delivery_worker.py](../../backend/src/speechnexus_backend/runtime_delivery_worker.py)
已有实际 CLI 装配，但 [RuntimeDeliveryService](../../backend/src/speechnexus_backend/speech/application/runtime_delivery.py)
负责私有投递 claim/发送前持久标记/原身份查询/ack；ack 不是 `observe_runtime` 或预算释放的替代入口。

B 的 [app.py](../../execution-gateway/src/speechnexus_gateway/app.py) 已装配屏障、执行查询、drain、
恢复 worker；显式配置文件执行 runtime 时装配 SubmitExecutionService/abort。
[HTTP 路由](../../execution-gateway/src/speechnexus_gateway/interfaces/http/) 返回各内部 HTTP DTO，
不等于使用全部原生 `TransactionResult` wrapper。三个补充服务 Observation/OutputInterval/Redaction
在冻结生产树中未找到组合根构造；不能将其测试注入算正式 meter/删除协调器消费。

## 7 类原身份查询与完整 value

“完整”按契约中相应 Snapshot 定义判断，不把事实摘要扩写为完整业务对象。
没有事实的 unknown 也不能伪造 value。各边界的 outcome/error/recovery 仍由源契约约束。

| target / owner | 返回 value 及读取入口 | 本版验证 / 剩余条件 |
| --- | --- | --- |
| pool / A | OwnerSnapshot：authority、expires_at_us、owner_state、version；`OwnerTransactionBoundary.query_outcome` | `test_wire_boundary_returns_a_real_owner_snapshot`；完整 owner 结构及当前权威读取，生产续约/接管消费者待接 |
| attempt / A | AdmissionSnapshot：identity、lease_ids、version、全部 reservations 的 stage/slots/physical；`AdmissionTransactionBoundary.query_outcome` | `test_frozen_boundary_returns_only_original_admission_projection`；原意图恢复，不按新策略重算 |
| job / A | ClaimSnapshot：job_id、worker_id、claim_version、expires_at_us；`ClaimTransactionBoundary.query_outcome` | `test_claim_and_read_only_current_authority_do_not_release_or_dispatch`；这是当前 claim 查询。创建回执丢失另走 `recover_create_job`，不能以缺 claim 判断创建未提交 |
| submit / B | ExecutionSnapshot：`ExecutionQueryService.query_outcome` → ledger，`ExecutionSnapshot.response/status/staging_response` 分别投影 | 原生读取/HTTP status/staging 已有持久化测试；未找到将 `query_outcome(target=submit)` 统一请求封装成 `TransactionResult` 的正式 boundary。此差异仍需 B/A 对齐，不能仅凭 HTTP DTO 声称原生 envelope 齐全 |
| lease / A | LeaseSnapshot：lease_id、lease_state、cleanup_state、version；`LeaseTransactionBoundary.query_outcome` | `test_isolation_is_atomic_preserves_budgets_and_queries_never_write`、释放模块；不包含完整六维预算账本，真实结算消费者仍待 |
| interval / B | FactSnapshot：kind、fact_id、request_digest；`OutputIntervalBoundary.query_outcome` | `test_intervals_are_canonical_durable_facts_and_diagnostics_leave_generated_usage_alone`；完整区间在 owner 账本，摘要不携带 PCM 或替代用量结算 |
| runtime_intent / A | RuntimeSnapshot：无观察时 runtime=null、原 request/intent_state/recorded_at_us/updated_at_us/version；有观察时原 runtime 观察；`RuntimeTransactionBoundary.query_outcome` | `test_runtime_observations.py` 与 codec 单测覆盖观察映射/身份；未观察与已观察的查询消费须按不同契约分支，生产观察/readiness 接线仍待 |

create_job、seal_barrier、redact_execution_command、append_interval 的成功 value 按契约使用
FactSnapshot；不是缺字段后补造公开 Job、屏障全状态或删除许可。ExecutionSnapshot 的
私有持久命令正文也不进入公开回执。

## 当前组合的真实持久化证据

[合后 CI 40208749970](https://github.com/hrygo/SpeechNexus/actions/runs/40208749970)
精确 source 为上述 `7dc276a`，14 项作业成功。Python job `121215300240`：6259 passed、
184 subtests passed、coverage 85.51%；这是整个作业集合，不是 18 端口各有 6259 个案例。
[数据库聚合作业](https://github.com/hrygo/SpeechNexus/actions/runs/40208749970/job/121216489119)
核对 6 分片完整集合：1637 collected、1615 passed、22 skipped，最长分片执行 179.61s。

本次下载各 ZIP 后逐一对比 GitHub artifact SHA256，使用冻结版
[integration-shards.py](../../scripts/dev-env/integration-shards.py) 再校验 source、完整集合摘要、
分片不交叠/无遗漏、实际执行与 outcome；全部通过。该操作只读既有报告，没有重启数据库或重跑实验。

| 分片 | artifact ID | ZIP SHA256 |
| --- | --- | --- |
| 0 | 12604407971 | `d962dad7abc059b36e27378912fefa84d5799565ca987885beb61abcccbc05c4` |
| 1 | 12604838563 | `286e7ad191af665340be667e85d8ddd57f181e51416d799b12448fff4d819be8` |
| 2 | 12605206345 | `c134bdece8d5ea362dc7533467604b78f395d967dc505657d8effb4b41542e58` |
| 3 | 12604723641 | `42ab8d1852bf47aeb6a0f1f894052265ba352dbe606381c239e684c030c98172` |
| 4 | 12604658788 | `4e6966f11e33c14b96c042a99d7b494aec2acc0e0384e8d16f91c3d7ec297960` |
| 5 | 12604573933 | `f1e574b6cee7ac8087ae945d8d31c118cf0597f7067ab96a82d9239d94be84a3` |

恢复 artifact 中的 `integration-shard-*.json` 到一个空目录后可复核：

```bash
python3 scripts/dev-env/integration-shards.py check --count 6 \
  --head 7dc276a4cfa38e8f433def05b0cbed25dcec8d5d --reports-dir <报告目录>
```

以下计数从六份报告的 `outcomes` 按模块及参数中的完整 dialect token 汇总，均为 passed。
每行是模块级证据，不是该端口所有失败窗口的完成声明；PG 多出的案例包含驱动/DDL 专用检查。

| 测试模块（tests/integration/） | PG | InnoDB | 证明边界 |
| --- | ---: | ---: | --- |
| test_production_jobs.py | 26 | 23 | Job/outbox、原结果、权限、边界快照 |
| test_production_owners.py | 13 | 10 | owner 事务/权威读取；屏障来源含合成 fixture |
| test_owner_activation_quarantine.py | 5 | 5 | 激活前隔离记账及失败回滚 |
| test_production_claims.py | 10 | 9 | 行锁竞争/过期/旧版本及只读查询 |
| test_production_admission.py | 37 | 35 | 完整阶段原子准入及跨 A/B ledger 联验 |
| test_admission_recovery_pressure.py | 1 | 1 | 已提交丢回执后读取受压仍保持 unknown |
| test_job_recovery_unknown.py | 7 | 3 | 创建原身份查询失败不变为写重试 |
| test_production_leases.py | 22 | 22 | unknown 隔离且总占用不下降 |
| test_lease_release.py | 31 | 30 | 原事实裁决、竞争、回滚与一次释放 |
| test_production_runtime_intents.py | 29 | 28 | 固定计划/峰值/outbox/同事务预算 |
| test_runtime_observations.py | 27 | 25 | 观察/资格来源、锁后取时、CAS/回滚 |
| test_gateway_barrier.py | 12 | 11 | 共享屏障行为，非所有原生结果封装 |
| test_gateway_barrier_transactions.py | 6 | 0 | 原生统一结果仅 PG，无 MySQL 跳过项 |
| test_gateway_fact_transactions.py | 5 | 0 | create_submit/unknown/stop 原生统一结果仅 PG |
| test_gateway_observation.py | 10 | 8 | 原观察与统一结果、身份/版本守卫 |
| test_gateway_stop_evidence.py | 28 | 26 | 合成可信停止事实的持久裁决，不证明引擎停止 |
| test_gateway_output_intervals.py | 23 | 22 | 区间事实/重叠锁/幂等与原生查询 |
| test_gateway_command_redaction.py | 12 | 11 | 清除/tombstone 原子性，授权与 A 释放含合成预置 |

22 个 skip 全部是 `test_gateway_submit_ledger.py::*[mysql]`；本模块另有 22 个 PG passed。
校验器仅允许该明确集合。其他模块的双库通过不能把这些 skip 改写为通过；
同样，两个原生结果模块只收集 PG，虽无 skip，也不证明双库 envelope 一致。
历史 `test_coordination_transactions.py` 的 39+39 原型和旧 schema 检查另列，不能冒充生产 adapter。

### 能证伪的关键场景索引

| 不变量 | 实际断言入口 | 限制 |
| --- | --- | --- |
| 同一准入全部成功或全回滚 | `test_production_admission.py::test_complete_recipe_rolls_back_every_record_and_projection` | 受信命令/计划由 fixture 提供，业务配额范围仍限已实现账本 |
| 最后预算跨池竞争 | 同模块 `test_different_pools_compete_for_the_same_host_last_increment` | 同进程独立数据库连接，不是两台 Scheduler 主机 |
| 提交成功丢回执、查询受压仍原身份 | `test_admission_recovery_pressure.py`、`test_job_recovery_unknown.py` | 真实提交/驱动故障注入，不是物理网络分区/HA 演练 |
| unknown 保留总占用、激活失败回滚 | `test_owner_activation_quarantine.py` | 合成 B 事实不签发实际停机证明 |
| A/B 同库准入到原执行事实再释放 | `test_production_admission.py::test_native_admission_to_gateway_fact_and_last_stage_release` | 两库均直接调用 ledger，不等于全部 B 统一结果 wrapper；命令/观察/停止是合成数据 |
| 不释放其他 lease、最后 stage 一次结算 | `test_lease_release.py::test_concurrent_last_stages_return_attempt_quota_exactly_once` 及 `test_every_dimension_returns_only_the_target_lease_charge` | 不覆盖尚未实现的业务用量结算 |
| 锁后数据库取时、旧 owner/CAS 拒绝 | owners/claims/runtime 模块中的 lock-wait 案例 | 实际等待已断言；不承诺物理集群/长期公平性 |
| runtime 原意图只投一次、未知仅查原节点 | `test_runtime_delivery.py`、`test_runtime_delivery_commit_loss.py`、`test_runtime_delivery_tls.py` | 私有 transport 账本/测试 TLS；不替代观察准入和真实 GPU 资格 |

## 剩余验收与下一最小单元

| 缺口 | owner / 关联 | 解除条件 |
| --- | --- | --- |
| A 正式策略、批准计划/readiness 与业务 Scheduler/Worker | A01/A02/A03：#20/#21/#22 | 权威事实 provider、实际组合根调用，原命令/版本在同一事务重检；不能从示例默认值构造批准事实 |
| B submit 查询统一 envelope、PG-only wrapper 的 InnoDB 对照 | B01/B04 与 A00/A01：#28/#31/#19/#20 | B 核对当前 native/HTTP 消费需求，补对应封装或明确同源映射及双库正反例；A 不跨队改实现 |
| 区间事实的业务配额/结算消费 | A03 + B 的 meter：#22/#31 | A 真实应用读取 B 账本并在原事务结算，unknown 不释放、最后阶段只退一次，不复制 PCM 账本 |
| 可信停止与物理多机/多 worker 联验 | A03/A07 + B：#22/#26/#28/#29 | 固定组合版本、实际引擎/设备、停止与资源释放双证据、故障与并发结果；由 B 安排硬件 |
| C 公开 Job/媒体消费 | A00/A03 与 C 原 owner | A 公开业务接口和 C 同版实际路由/浏览器证据；不以 SDK TCP fixture 代替，不编辑 C issue |

本卡后的 A 内部最小前置应继续 #21 已规划的权威配置/发布事实链：先确认当前已实现的
发布/CAS 与批准来源，再接一个固定资源版本的真实应用消费者；验收需覆盖原结果优先、
事务内重检、冲突及提交未知。具体切片须先读取 #21 最新状态，不由本报告宣布新接口或功能。
其余证据缺口按上表分 owner，不能为了凑齐 18 个绿色格子跨队制造事实。

本次只增加报告及索引，没有协议、生成产物、CI、锁、迁移或权限变化；回退仅撤销文档提交。
#155 的平台限制及已消费的一次实验保持原状态，不运行安全策略实验、生产迁移或硬件操作。
