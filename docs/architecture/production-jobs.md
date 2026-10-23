# 首个生产 Job/outbox 事务

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

后续 `0002_coordination` 增加其余 13 表的[生产存储与最小权限基线](production-coordination-storage.md)，
不改写本单元的 Job/outbox 身份或授权；已有迁移命令升级整个包内链到 head。

这里的 Job 是协调元数据，不是完整公开 Job 的业务命令/结果存储。公共路由、
完整配方准入、claim、outbox 投递、Scheduler/Worker 和真实引擎仍待对应单元。
不得把此单元解释为文件转写/合成闭环已完成。

## 原子性、身份与恢复

`job_id`、`(operation_id, attempt_id)` 和初始事件身份由唯一约束保护。
Job 行冻结初始 event ID/digest；outbox 后续可追加其他事件，不重定义创建身份。
初次创建在同一事务插入 Job/outbox，提交确认后才返回 committed。
原 job/operation/attempt/event 两个摘要全部相同才 replay；任一不同均 conflict。
`request_digest` 来自调用方完成默认值解析/版本固定后的业务命令，不对包含摘要
自身的包装再做散列。本单元不假装能从摘要重建尚未实现的完整业务 payload。

重放读取不可变事实；不存在的新键通过唯一索引处理竞争。失去唯一键竞争后，
整个失败事务回滚，再只读查询胜者；不会重复投递或外发执行。未来 `admit_recipe`
必须在其现有完整配方事务内调用 `create_in_transaction`，不能先调用独立
`create_job` 端口再提交第二个准入事务。

仅 PG SQLSTATE `40P01`/`40001` 的已确认终止事务可最多重试三次；抖动发生在
回滚并归还连接后。池队列、连接数、语句等待与重试预算有界；这不是已验证的
任意网络黑洞硬截止时间。连接中断、提交确认丢失返回 unknown/query_original。

`recover_create_job` 是同一进程内 Job 端口的只读恢复 helper，返回既有 create_job
结果，不新增 HTTP 路由或机器协议 operation。Job 或 outbox 缺失不能靠补写修复：
尚无可见 Job 时仍 unknown；已有 Job 却缺少对应 outbox 时进入 reconcile。
恢复只读，既不补建 Job，也不授予重新执行权限。
`query_outcome(job)` 保持既有 ClaimSnapshot 映射；其未认领 worker 表达与完整
查询行为不在本单元伪造实现。

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
数据库级 PUBLIC TEMP 与其他继承授权按[存储/角色基线](production-coordination-storage.md)
单独核对；共享池显式将 `pg_temp` 置于末尾，防止同名临时表覆盖权威表。

## 证据与保护范围

```sh
uv run pytest tests/unit/test_production_jobs.py -q
# 先配置指向项目隔离实例的两个测试 DSN。
make coordination-test
make integration-test
```

`test_production_jobs.py` 在真实 PG adapter 与 InnoDB 共享 SQL 上执行同组创建、
完整身份冲突、outbox/约束失败回滚、最低权限和恢复测试。竞争测试先观测实际
`pg_blocking_pids` / InnoDB lock wait，再释放阻塞提交；不靠 sleep 推断发生竞争。
MySQL 仅将 PG `COLLATE "C"` 映射为 `utf8mb4_0900_bin`；不进入产品依赖。

PG 专属故障测试终止真实会话验证提交前未知；已提交后通过测试连接类模拟丢失
回执，证明不自动重写且只读恢复原事实。这不等于真实网络丢包、HA/故障切换或
多主机资格。运行角色的负例要求权限错误，不能以其他 SQL 错误冒充拒绝授权。

ADR 0007 退出复核：本单元迁移 Job/outbox 原子性与幂等不变量。预算、fencing、
停止证据、生命周期和其他旧规则尚未全部迁移，旧机制退出条件未满足；本次保留
旧门禁，不将首个生产端口通过当成旧原型整体退役依据。
