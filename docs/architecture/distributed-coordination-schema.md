---
title: 分布式协调 Schema：受控应用事务草案
status: active
version: "1.0.0"
date: 2026-10-23
---

# 分布式协调 Schema：受控应用事务草案

[ADR 0006](../decisions/0006-cluster-coordination-and-storage-portability.md) 的目标 DDL
位于 [`0001_cluster_coordination_draft.sql`](../../migrations/0001_cluster_coordination_draft.sql)。
它使用普通表、主键/唯一键、外键、行内 CHECK 和普通索引；没有业务函数、触发器、
扩展、容量验证队列、部分唯一索引或 GiST 区间约束。跨表业务裁决由受控应用事务承担。

SQL 是**新建 namespace 的设计草案和测试输入**，不是已部署业务迁移，也不是旧草案的
原地升级。它位于产品 Python 包之外，不随应用 wheel 发布。迁移角色执行 DDL；
运行角色只获最小 DML 权限。

[首个生产 Job/outbox 单元](production-jobs.md)使用包内独立 Alembic 迁移，冻结初始
事件身份并以最低角色运行；不把本文件的 15 表草案作为生产迁移直接应用。

程序的端口、原子步骤、锁序、错误/重放/未知结果、预算公式与后续编码交接详见
[受控协调事务方案](coordination-transactions.md)；机器定义见
[事务契约](../../contracts/internal/coordination-transactions.schema.json)。

## 表与所有权

| 表 | 写入所有者 | 职责 |
| --- | --- | --- |
| `resource_pools` | Speech Scheduler | 池状态、当前 owner、单调 fencing token、到期时间和 version；合并旧 pool/owner 两张权威行 |
| `workspace_quotas` | Speech Scheduler | 完整 attempt 的并发 quota，held/quarantine 均占限额；不是完整计费表 |
| `host_nodes` | Speech Scheduler | 六维父物理预算、held/quarantine 投影、拓扑与隔离阈值 |
| `engine_instances` | Speech Scheduler | 按 `(instance_id,generation)` 保留历史；不可变配置/peak/steady/per-slot 与可变运行投影 |
| `runtime_instance_heads` | Speech Scheduler | 当前 generation、generation high water、prepare/stop 当前意图指针；普通 PK/FK 串行化 |
| `runtime_instance_intents` | Speech Scheduler | 节点内幂等请求摘要与规范请求、状态、停止观察；控制器通过协议报告，不共写底座表 |
| `admission_attempts` | Speech Scheduler | 完整配方、binding/manifest/policy、阶段集合、规范请求摘要、quota 清理投影 |
| `admission_leases` | Speech Scheduler | 每阶段身份、六维增量、槽位、token、期限、清理与释放事实 |
| `gateway_fencing_barriers` | Gateway | 实际封锁入口后保存的最低 token 与证据摘要；不是数据库写入即可完成物理 fencing |
| `submit_intents` | Gateway | 唯一 lease/stage 提交，完整规范请求及摘要、执行/清理状态、外发关联、冻结输出边界与水位；非 PCM 阶段不伪造流，正文按受控删除清除而最小事实保留 |
| `execution_stop_evidence` | Gateway，只追加 | 已核验的停止与资源释放事实；独立于 Scheduler 的预算释放事务 |
| `output_stream_heads` | Gateway | 按 operation/attempt/output stream 锁定；同一输出流的不同 stage/submit 共享重叠保护 |
| `output_sample_intervals` | Gateway，只追加 | PCM 样本区间账本；重叠由锁定流权威行后的应用检查拒绝 |
| `coordination_jobs` | 所属 Speech 任务模块 | 持久 Job 身份、认领 owner/version/deadline；尚无生产 Job 业务实现 |
| `coordination_outbox` | 同上 | 与 Job 同事务写入；通知丢失时仍可轮询 |

新增表使配方、停止事实、当前指针和 Job/outbox 的职责显式，目标不是最少表数。
核心 FK 继续约束 instance/host/pool/gateway、attempt/lease、lease/submit、submit/evidence
与 Job/outbox 关系；binding/policy/qualification 的完整身份仍需与既有不可变资源存储
在同一应用事务中核对。TEXT 仅保存已校验的规范文档，不依赖数据库 JSON 查询。

## 预算与历史保护

host 的六维为 unified physical memory、device memory、host memory、CPU decode、
PCM buffer 和 scratch disk。每维行内 CHECK 使用
`held <= allocatable - quarantined`，避免检查表达式先加法溢出。held 包含当前 peak/steady
实例基线和全部活动增量，quarantine 包含未知实例/执行的占用。应用同步保证这些投影
与全部权威事实一致，后台对账不能替代同步更新。带宽、对象大小、时长等其他配额遵循
既有领域契约，尚未在这份协调 DDL 中具体化。

运行角色无 DELETE、TRUNCATE 或 DDL 权限；不可变身份和预算列不获 UPDATE，追加事实
不获 UPDATE。配额上限、policy revision、拓扑摘要、隔离阈值和 allocatable 容量只由
控制面写入，运行角色不获这些列的 UPDATE，也不能读取其他所有者的规范请求正文
（只获身份/状态列级 SELECT）。可变运行投影的终态和单调性由应用事务维护，数据库不再独立拒绝所有
绕过应用的跨表违例。[权限工具](../../scripts/dev-env/coordination-grants.py) 输出双库
对应授权语句；现有继承/全局权限须另核对，运行角色不能是 owner 或超级用户。
规范请求正文允许在受控删除事务中单向清除；必须同时写删除 tombstone，不能清除
协调身份、原摘要、fencing 或停止证据，也不能在执行/资源清理前先删除正文。

## 验证与历史证据

[共享行为测试](../../tests/integration/test_coordination_transactions.py) 在 PostgreSQL 和
隔离 MySQL/InnoDB 上应用同一逻辑 DDL、运行同一期望，验证受控事务行为。
MySQL 仅将文档 TEXT 列映射为 LONGTEXT，避免合法长命令截断；没有业务规则分叉。
它是测试/工具原型，不代表生产 Scheduler 或数据库完整替代适配器已交付。
真实多主机、引擎、主备切换和备份恢复保持强制验收要求。

修复后的旧 SQL 完整保存在
[`legacy_postgresql.sql`](../../tests/fixtures/coordination/legacy_postgresql.sql)，其
[旧设计](legacy-postgresql-coordination.md)和
[53 项 PostgreSQL 回归](../../tests/integration/test_cluster_coordination_schema.py)继续独立运行，
保留直接 SQL 拦截、触发器来源校验、权限与历史证据；不把旧负例改为跳过或 fake。
历史规则到新执行位置的逐项映射见应用方案第 8 节。

上述保留是迁移对照期安排，退出条件见 [ADR 0007](../decisions/0007-legacy-coordination-retirement.md)。
有效业务不变量迁入新路径并通过相关验收后，移除旧门禁并删除废弃原型文件；
历史证据通过 Git 追溯，不形成永久 CI 负担。

[资源治理](runtime-governance.md) · [整体技术方案](technical-blueprint.md) · [重构计划](../plans/2026-10-23-cluster-coordination-refactor.md)
