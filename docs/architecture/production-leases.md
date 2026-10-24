# 生产租约隔离事务

本单元属于 [A01 #20](https://github.com/hrygo/SpeechNexus/issues/20)。提供独立
`quarantine_lease` 与 `query_outcome(lease)`，消费 B 的持久执行事实，不写 gateway 表，
不执行容器、网络或停止操作。完整配方准入、lease 释放、实际 scheduler 装配与
真实 GPU/多主机资格不包含在本单元。

## 入口与所有权

- `speech.domain.leases`：不可变请求、租约快照、六维资源/槽位/attempt 账目和纯隔离决策。
- `speech.application.leases`：`LeaseTransactions` / `LeaseService` 窄端口。
- `speech.infrastructure.leases_postgres.PostgresLeaseTransactions`：显式有界
  `SpeechPool`，默认共享三秒请求预算、最多三次已确认回滚重试。
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
事务核验足够停止/释放事实后才可能归还占用，本端口始终不写 budget_released_at_us。
版本耗尽、负投影、已超容量或身份漂移拒绝并回滚，不能用负账目抵消未知执行。
调用方收到 reconciliation_required 后须停止依赖该账目的新准入并交给协调流程；
本单元没有实现自动对账或 scheduler 控制循环。

所有更新带原 version 条件；任一步失败整笔回滚。事务内没有 sleep、外部 I/O 或新执行。
连接/提交确认不明返回 unknown/query_original，不透明重试；deadlock/serialization
仅在确认整笔回滚且连接归还后有限重试。租约查询使用只读事务。

## 存储、验证与保护边界

复用现有 `0006_runtime_observation` 及最小 A/B 角色，无新增迁移、DDL 或授权扩张。
共享 SQL 在隔离 InnoDB 上仅作行为 oracle，不形成产品 MySQL 支持承诺。

针对性测试覆盖同键竞争、跨池 stage 共享 attempt quota、两种内存架构的全部资源维度、
阈值、首观察保留、活动/终态拒绝、锁后数据库时间、最新 gateway 事实、版本耗尽、
中途失败回滚，以及原生 B mark_execution_unknown → A quarantine 的数据库接线。
提交确认丢失的 adapter 故障模型只验证分类策略，不声称确定远端实际提交状态。
这些事实均不替代真实引擎停止、内容网络边界或集群资格。

```sh
uv run pytest tests/unit/test_production_leases.py -q
# 两个 DSN 均须指向本项目隔离实例；缺一不可，跳过不计通过。
uv run pytest tests/integration/test_production_leases.py -q
make coordination-test
make integration-test
```

ADR 0007 复核：本单元迁入了旧隔离不变量；完整准入、释放及其联合行为尚未全部
迁入生产端口，因此继续保留旧必跑门禁，不提前删除原型或声称整体退出完成。
