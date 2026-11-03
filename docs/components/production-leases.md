# 生产租约隔离与释放事务

本单元属于 [A01 #20](https://github.com/hrygo/SpeechNexus/issues/20)。提供独立
`quarantine_lease`、`release_lease` 与 `query_outcome(lease)`，消费 B 的持久执行事实，不写 gateway 表，
不调用引擎、容器控制或外部 HTTP。完整配方准入、实际 scheduler 装配与
真实 GPU/多主机资格不包含在本单元。

## 入口与所有权

- `speech.domain.leases`：不可变请求、租约快照、六维资源/槽位/attempt 账目和纯隔离决策。
- `speech.domain.lease_release`：停止/未外发事实裁决、version 前提与最后 stage 的配额结算。
- `speech.application.leases`：`LeaseTransactions` / `LeaseService` 窄端口。
- `speech.infrastructure.leases_postgres.PostgresLeaseTransactions`：显式有界
  `SpeechPool`，默认共享三秒请求预算、最多三次事务尝试（含首次；仅已确认回滚可重试）。
- `speech.interfaces.lease_transactions.LeaseTransactionBoundary`：复用原协调
  `1.0.0` 契约；新增生成包 `quarantine_lease` 校验入口，没有改变 wire 源定义。

```python
from speechnexus_backend.speech.application.leases import LeaseService
from speechnexus_backend.speech.infrastructure.leases_postgres import PostgresLeaseTransactions
from speechnexus_backend.speech.interfaces.lease_transactions import LeaseTransactionBoundary

# pool 由可信组合根显式提供；本片段不会连接数据库或注册 HTTP 路由。
boundary = LeaseTransactionBoundary(LeaseService(PostgresLeaseTransactions(pool)))
```

调用 `quarantine_lease({lease_id, reason_code, observed_at_us})`；查询使用
`query_outcome({"target": {"kind": "lease", "lease_id": original_id}})`。
返回原 `LeaseSnapshot` 的 state、cleanup 和字符串 version，不暴露预算或私有命令。
lease 查询没有请求摘要身份，带 `request_digest` 或其他 target 的调用在该窄边界拒绝。
不存在的原租约返回 unknown，只能继续查询原身份或协调，不能据此新建执行。

## 事务与账目规则

未加锁读取只定位父键。随后按 pool → workspace quota → host → instance/generation
→ attempt → lease → submit 顺序锁定全部事实；锁后重核对引用及冻结资源，并在最后
一次等待后读取数据库时间。单条 lease 不需要修改或重新选择 runtime head。

首次隔离必须已有对应 submit 的 `unknown_quarantined / quarantined` 事实；缺少 submit、
活动或终态执行均不能授权隔离。deadline 过期、取消或断线不是独立证据。
六维 host 资源与实例槽位从 held 搬到 quarantine，held+quarantine 不减少，
也不改变 runtime 基线归属。attempt 第一次隔离才搬一次并发 quota；同 attempt
其他 stage 后续隔离不会重复搬配额。任一物理维度的 quarantine 严格超过配置阈值时，
active host 转 quarantined；已有非 active 管理状态保持。没有自动解除 host 隔离。

同一原因重放保留第一次观察时间、原因及版本，不再次搬移。即使 gateway 后续得到
停止事实，重放也不重写历史；换原因返回 idempotency_conflict。只有独立 release
事务核验足够停止/释放事实后才可能归还占用，隔离端口始终不写 budget_released_at_us。
版本耗尽、负投影、已超容量或身份漂移拒绝并回滚，不能用负账目抵消未知执行。
调用方收到 reconciliation_required 后须停止依赖该账目的新准入并交给协调流程；
本单元没有实现自动对账或 scheduler 控制循环。

所有更新带原 version 条件；任一步失败整笔回滚。事务内没有 sleep、外部 I/O 或新执行。
连接/提交确认不明返回 unknown/query_original，不透明重试；deadlock/serialization
仅在确认整笔回滚且连接归还后有限重试。租约查询使用只读事务。

## 释放与最后阶段结算

`release_lease({"lease_id": original_id, "expected_version": "2"})` 只接受原 ID 与
十进制字符串 revision，不接受调用方提供停止标志、预算或其他事实。只有未释放的
当前 version 可结算；冲突返回 version_conflict/query_original，已释放则按原
version/时间只读重放，即使请求仍带旧 version 也不能再次扣减。

释放沿用相同父锁序。持有 submit 锁后，读取 B 只追加、不可删除的停止证据，不对
无 UPDATE 权限的证据表加 FOR UPDATE。已提交执行须 terminal + cleanup complete，
停止与资源释放均已确认且观察时间不晚于锁后 DB 时间；成功必须是 terminal response。
quarantined lease 只接受 controller/operator 停止事实对应的 aborted，不能靠 timeout、
迟到成功或失败/未外发声明解除隔离。

B 原生 record_execution_observation 写入的 failed/not_required 是另一条权威事实：
该端口已核对无 dispatch、request ID、staging、生成样本与输出区间，并禁止从 unknown
降级到该状态。A 消费这个持久事实，不读取 B 的私有请求正文，也不另造停止证据。
从未创建 submit 的 lease 可在 lease 锁下关闭；B 新 submit 必须锁同一 pool/attempt/lease
并核对未释放状态，因此提交与释放最多一方成功。本边界不证明 B 的真实观察提供者资格。

attempt 锁串行化 A 的准入/隔离/释放。释放前核对冻结 required_stage_ids 与实际全部
lease 的 stage 集合完全一致、无缺失或重复，再读取其他 lease 的已释放事实；无需倒序
锁其他 host/pool 的兄弟 lease。只扣当前 lease 的 held 或 quarantine 六维增量及槽位，
最后一个 lease 才从 attempt 原 quota_state 归还一次并发 quota。首次隔离的 quota
不会因某个已隔离 stage 先结束而提前返还。host 不自动恢复 active。

release 只记当前 lease 的 budget_released_at_us，不改变 gateway 历史或
engine_instances 的基线预算类型/内存。实例基线即使已有单次执行停止事实，也须由
独立 runtime 停止流程核对退出、资源释放、全部 lease 和执行事实后才能归还。

## 存储、验证与保护边界

隔离与释放复用既有协调表和最小 A/B 角色。当前释放实现对 B 的
`failed/not_required` 事实还要求迁移 `0028_execution_release_guards` 创建的
`execution_release_guards` 视图，以及 `release_guard_grants` 声明的视图列读取权限；
缺少迁移、权限或对应行时该路径拒绝释放；数据库错误通常映射为 `reconciliation_required`，
缺行映射为 `not_found`，连接状态不明仍可能返回 unknown。该视图只暴露
无外发元数据的结构性佐证，不单独构成停止证据。授权须由部署方显式配置，不由应用启动执行。
共享 SQL 在隔离 InnoDB 上仅作行为 oracle，不形成产品 MySQL 支持承诺。

针对性测试定义覆盖同键竞争、跨池 stage 共享 attempt quota、两种内存架构的全部资源维度、
阈值、首观察保留、活动/终态拒绝、锁后数据库时间、最新 gateway 事实、版本耗尽、
中途失败回滚，以及原生 B mark_execution_unknown → A quarantine 的数据库接线。
释放额外覆盖真实跨池 last-stage 行锁竞争、B 新 submit 与 A release 的互斥、
原生 B stop/failed-not_required → A release、未释放兄弟 lease 及 runtime 基线保留。
提交确认丢失的 adapter 故障模型只验证分类策略，不声称确定远端实际提交状态。
PG 专用回归另在实际提交后丢弃确认，验证 unknown、原 ID 只读恢复及无重复扣减；
运行中的未知结果仍须查询，不能从超时推定提交或回滚。
测试定义不证明当前环境已通过；这些范围均不替代真实引擎停止、内容网络边界或集群资格。

```sh
uv run pytest tests/unit/test_production_leases.py -q
uv run pytest tests/unit/test_lease_release.py -q
# 两个 DSN 均须指向本项目隔离实例；integration-test 还需要隔离 MinIO endpoint 和测试密钥。
# 配置见 tests/integration/README.md；缺少前置条件或跳过不计通过。
uv run pytest tests/integration/test_production_leases.py -q
uv run pytest tests/integration/test_lease_release.py -q
make coordination-test
make integration-test
```

ADR 0007 复核：本单元交付时迁入了隔离/释放不变量，旧必跑门禁仍保留。此后
[完整配方准入事务](production-admission.md)及部分同步业务装配已实现；这不等于
所有联合行为、旧路径退出条件或真实部署资格均已验收。
