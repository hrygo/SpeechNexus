# 生产协调存储基线

本单元属于 [A01 #20](https://github.com/hrygo/SpeechNexus/issues/20)，向 B01 提供
具名的 lease/instance/submit FK 与权限基线，不替代各所有者的生产事务 adapter。
PostgreSQL 是唯一产品数据库；InnoDB 只验证相同基础约束与权限意图。

## 迁移链与交接

包内 Alembic head 为 `0002_coordination`，依赖 `0001_jobs`。新 schema 依次建表；
已有 `0001_jobs` schema 只增加剩余 13 表和 4 个查询索引，不改写 Job/outbox 行、
初始事件身份、约束或既有授权。版本表、整份新 DDL 与 schema 级迁移互斥同事务提交。
重复升级为 no-op；失败全部回滚；破坏性 downgrade 拒绝执行。

| 写入所有者           | 本轮新增表                                                                                                                                                             |
| -------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| A / Speech Scheduler | `resource_pools`、`workspace_quotas`、`host_nodes`、`engine_instances`、`runtime_instance_heads`、`runtime_instance_intents`、`admission_attempts`、`admission_leases` |
| B / Gateway          | `gateway_fencing_barriers`、`submit_intents`、`output_stream_heads`、`execution_stop_evidence`、`output_sample_intervals`                                              |

所有权与 [18 端口矩阵](../../contracts/coordination-ownership.json)一致。A 维护迁移顺序；
B 实现 gateway 的窄事务端口和 adapter，不能用 Job adapter 或独立双写替代完整事务。
控制器通过 runtime 协议回报，不直接共写这些表。B/C 接入前应固定合入 SHA、迁移 head、
契约版本和边界包；这些表没有公开业务 HTTP 路由。

`coordination_schema.COORDINATION_DDL` 是从 ADR 0006 设计草案逐项复核后冻结的包内
存储定义：使用 PG `C` 比较标识，保留基础范围、预算不超额、唯一身份与组合 FK，补充
必填时间非负约束。产品不会读取根目录 draft SQL 或测试原型。变更这些冻结语句应使用
后续迁移，不直接编辑历史定义。

## 安装与角色

继续使用[已有 owner 专用迁移命令](production-jobs.md#迁移与最低权限)：

```sh
uv run speechnexus-job-migrate --schema speech
```

该既有命令现在升级整个包内迁移链到 head。部署注入独立
`SPEECHNEXUS_MIGRATION_DATABASE_URL`；应用启动和运行角色不执行 DDL。
安装 wheel 时迁移环境需 backend 的 `migrations` extra。

部署创建两个独立、非 owner、无继承/全局提权的运行角色，由 migration owner
审阅并安装 `coordination_grants.coordination_runtime_grants` 返回的 SQL，例如：

```python
from speechnexus_backend.speech.infrastructure.coordination_grants import (
    coordination_runtime_grants,
)

statements = coordination_runtime_grants(
    "speech", speech_role="speech_runtime", gateway_role="gateway_runtime"
)
```

此函数只生成 SQL，不创建身份、不连接数据库、不自动授予权限。现有 Job 创建者
`job_runtime_grants` 保持原先更窄的权限。迁移不会自动扩大任何旧角色权限。

- 运行角色无 schema CREATE、持久表 DDL、版本表权限、DELETE 或 TRUNCATE。
- A 更新所属运行投影；池/配额/节点的控制面初始配置由独立部署/控制面身份写入，
  运行角色无这三表的 INSERT，也无配额上限、策略版本、拓扑、隔离阈值或容量配置 UPDATE。
- B 更新所属运行投影；停止证据与样本区间只允许 B SELECT/INSERT，无 UPDATE/DELETE。
  身份、规范摘要、冻结输出边界不获 UPDATE。正文清除列是 B 的显式 tombstone 例外。
- 跨所有权只为行锁授 `UPDATE(version)`；A 不能读取 B 的规范请求正文，B 不能读取
  A 的 runtime 请求/停止文档。必要身份/状态使用列级 SELECT，不能用 SELECT * 绕过。

这些授权必须在实际非 owner 连接验证；GRANT 列表不证明已有继承权限安全。
PG 默认数据库级 PUBLIC TEMP 权限不由 schema GRANT 函数撤销，应在部署权限核对中
单独确认。共享连接池将 `pg_temp` 显式放在路径末尾；回归验证同名临时表不能覆盖
权威 owner 表的读取/写入。
根目录 `coordination-grants.py` 仍服务于草案/测试，不作为这份生产迁移的安装入口。

## 保护范围与证据

基础约束保护 instance/host/pool/gateway、attempt/lease、lease/submit、submit/interval
和 stream 的核心关系；正文或 tombstone 必须完整，流边界与计数有行内约束。
PostgreSQL 迁移保留长规范正文；InnoDB 测试仅映射 `C` 到 binary collation、TEXT 到
LONGTEXT，验证大于 64 KiB 的合成正文不会截断。它不形成产品 MySQL 支持承诺。

跨表预算投影一致、generation/fencing 单调、实际封锁入口、完整配方原子准入、停止后
释放、正文清理先决条件与 sample 重叠裁决，仍由相应受控应用事务实现。基础 FK/CHECK
和列权限不能独立拒绝所有绕过应用的 SQL。测试中的 admin 合成行不证明这些业务路径
已经实现，更不证明真实 GPU、调度、网络故障或多主机资格。

```sh
# 设置两个项目隔离测试 DSN；缺一不可，跳过不算通过。
make coordination-test
make integration-test
```

`test_coordination_foundation.py` 的同组约束/最小角色行为在 PG/InnoDB 执行；PG 使用
实际包内迁移。升级故障测试在新表创建中途制造冲突，验证 DDL/版本全部回滚、既有
Job/outbox 与授权保留，再重复升级。两个独立迁移进程先观测真实 advisory lock wait，
释放后串行完成；这证明迁移互斥，不等于 HA 或恢复资格。

ADR 0007 复核：本轮只迁入存储和权限基础，其他旧业务不变量仍未全部进入生产端口，
保留旧必跑门禁。B 的 PG adapter、A 的准入/owner/runtime/claim 等后续实现与联合验收
继续单独取证；不因本迁移合入关闭 A01 或 Gate。
