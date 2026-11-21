---
title: 单一可移植 SQL 数据路径与实时预算
status: proposed
date: 2026-11-21
---

# ADR 0016 单一可移植 SQL 数据路径与实时预算

## 背景

产品要求数据库只使用 PostgreSQL、MySQL、Oracle、OceanBase 等共同具备的能力。应用层不得
维护多套数据库适配器，也不得在业务代码中按品牌选择 SQL；迁移数据库时，运行时只替换
ODBC 数据源和底层数据库驱动。

上一版设计仍允许“中立端口 + 多 adapter + 品牌快路径”。它可以在当前 PostgreSQL 上保持高性能，
但无法证明业务代码只换驱动即可迁移，也无法阻止 PostgreSQL 语法、异常码、upsert、
`SKIP LOCKED` 和数据库通知继续扩散。本决定改为单一 SQL 路径。

本决定区分三件事：

1. **运行时可移植性。** 同一应用代码、同一 SQL、同一 schema 只通过 DSN 与底层 ODBC 驱动切换数据库。
2. **数据库初始化。** 新数据库按同一份可移植 DDL 建立 schema。
3. **存量数据迁移。** 跨库复制、校验、切换与回退仍是一次性部署作业，不能被“换驱动”掩盖。

当前实现仍是 PostgreSQL 专用路径，未达到本决定。公开实时链路也尚未完成生产装配与真实联验。

## 决定

1. **只有一条 SQL 数据路径。** 产品代码不包含 `if database == ...`、数据库方言分支、
   品牌 SQL 模板或按品牌加载的 repository/adapter。应用依赖统一 ODBC DB-API，运行时只切换
   DSN、ODBC driver manager 配置和厂商 ODBC driver。
2. **冻结最小 SQL 子集。** 只允许 `SELECT`、`INSERT`、`UPDATE`、`DELETE`、基础 `JOIN`、
   `GROUP BY`、`ORDER BY`、`CASE`、`COALESCE`、`COUNT`、`SUM` 及静态谓词。
   禁止 upsert、`RETURNING`、`MERGE`、`SKIP LOCKED`、数据库通知、advisory lock、CTE、窗口函数、
   存储过程、触发器、数据库函数、动态 SQL、`LIMIT/OFFSET` 和品牌类型。
3. **使用 ODBC 统一参数与事务接口。** 应用 SQL 只使用 ODBC `?` 参数标记；连接关闭 autocommit，
   每条用例显式提交或回滚。厂商驱动必须满足同一结果类型、Unicode、时间、`rowcount` 和事务契约。
   **不得使用 ORM 或 SQLAlchemy 作为运行时方言适配层。**
4. **schema 只有一份。** 表、列、主键、唯一键、外键、普通索引和迁移版本使用共同 DDL 子集。
   标识符保持 ASCII 小写、无引号、长度不超过 30；运行时不执行 DDL。
5. **应用生成身份。** 新 Job、Operation、lease、grant、event 和区间 ID 由应用生成 ULID；
   `VARCHAR(128)` 保留现有 ASCII 身份容量。禁止自增列、sequence、数据库 UUID 函数、
   `RETURNING` 或 `LAST_INSERT_ID`。
6. **数据类型收敛。** 使用 `VARCHAR(n)`、`CHAR(n)`、`DECIMAL(38,0)`、`TIMESTAMP(6)`；
   布尔用 `CHAR(1)` 的 `Y/N`，金额 / 配额 / 样本坐标使用十进制整数。禁止 `BOOLEAN`、`TEXT`、
   `CLOB`、`BLOB`、`JSON`、`JSONB`、数组、枚举、区间类型和品牌 UUID。音频正文与大数据留在对象存储。
   所有变长文本在应用层按 UTF-8 字节数限制为不超过声明长度，使 Oracle 的字节语义与 MySQL 的
   字符语义收敛到同一更严格上限；标识与摘要使用 ASCII 规范形式。
7. **时间和锁显式化。** 数据库实例与会话统一 UTC。需要等待行锁的决定在取得锁后，通过独立、
   只读的短事务执行 `SELECT CURRENT_TIMESTAMP FROM schema_meta WHERE meta_id = ?`，把新鲜 UTC
   `datetime` 绑定到当前写事务并只用于本次决定；不依赖各库 `CURRENT_TIMESTAMP` 的事务快照语义。
   `schema_meta` 行只提供共同 `FROM` 锚点与 schema revision，不持久化数据库时间。需要串行化预算或
   区间时按固定顺序逐行执行 `SELECT ... FOR UPDATE`；高竞争 Job/outbox 使用 claim bucket 租约，
   不依赖 `SKIP LOCKED` 或数据库通知。
8. **失败语义不读品牌错误码。** CAS 以影响行数为准；插入冲突统一回滚后按业务唯一键查询原身份；
   提交调用抛错或结果不可确认一律视为 `commit_unknown`，只按原身份查询，不重试新身份。事务明确
   不依赖数据库默认隔离级别；只是在未提交异常后、确认原身份不存在且 deadline 尚存时，才允许用
   完全相同业务键重放，不依据 SQLSTATE 做透明重试。
9. **实时语音使用持久 grant 与本地 credit。** 会话准入先持久预留预算；网关只在已提交 grant 内
   发放本地 credit，低水位异步续租。数据库短抖时消费现有 credit，grant 耗尽后显式背压或结束。
10. **最终用量精确。** ASR 按 accepted canonical samples，TTS 输入按首次冻结 codepoint，
    TTS 输出按实际 generated PCM samples；边界区间半开、可重放且只结算一次。运行水位与交付展示
    可以近似，持久授权不能超发。
11. **四类数据库是同一资格的多个实例。** PostgreSQL、MySQL、Oracle、OceanBase 的固定目标版本
    运行同一 SQL、同一 DDL 和同一 conformance suite；任一目标跳过即未取得可移植资格。

## 运行时替换边界

```text
业务用例 / SQL
      |
      v
ODBC DB-API（唯一接口，? 参数）
      |
ODBC driver manager（不变）
      |
厂商 ODBC driver（替换对象）
      |
目标数据库（同一 schema）
```

允许替换：ODBC driver、DSN、连接参数、数据库连接串和部署实例。

不允许替换：业务代码、SQL 文本、schema、字段语义、事务结构、错误处理分支、
测试 oracle 或公开协议。

## 共同 SQL 子集

| 类别 | 允许 | 禁止 |
| --- | --- | --- |
| DML | 单行 `INSERT`、按 PK / 唯一键 `SELECT`、条件 `UPDATE`、条件 `DELETE` | upsert、`MERGE`、`RETURNING`、多行 `VALUES (...), (...)` |
| 查询 | 基础 `JOIN`、`WHERE`、`GROUP BY`、`ORDER BY`、`COUNT`、`SUM`、`CASE`、`COALESCE` | `LIMIT/OFFSET`、`NULLS FIRST/LAST`、递归 CTE、窗口函数、`FILTER`、品牌 JSON 操作符 |
| 并发 | 单行 `SELECT ... FOR UPDATE`、version/owner/epoch CAS、claim bucket 租约 | `SKIP LOCKED`、`NOWAIT`、advisory lock、品牌隔离级别语法、锁表 |
| 时间 | 锁后独立短事务执行一次 `SELECT CURRENT_TIMESTAMP FROM schema_meta WHERE meta_id=?`、`TIMESTAMP(6)`、应用传参的 UTC `datetime` | 依赖写事务内 `CURRENT_TIMESTAMP` 快照、锁前旧时间、`clock_timestamp()`、`NOW()`、`SYSDATE`、`SYSTIMESTAMP` |
| 类型 | `VARCHAR`、`CHAR`、`DECIMAL(38,0)`、`TIMESTAMP(6)` | `BOOLEAN`、`TEXT`、`CLOB`、`BLOB`、`JSON*`、数组、枚举、区间类型、UUID 类型 |
| 身份 | 应用 ULID、SHA-256 十六进制、业务唯一键 | sequence、identity、自增、数据库生成 UUID |
| 迁移 | 单一 SQL DDL、显式 schema revision、离线数据迁移 | ORM 自动建表、运行期 DDL、品牌迁移 DSL |

## 数据模型原则

- **协调记录与内容分离。** 数据库只保存身份、状态、版本、租约、预算、计数、摘要和对象引用；
  音频、转写正文、完整命令和大 JSON 放对象存储。
- **一表一权威。** 每个状态、配额和区间只有一个写入路径；后台对账不修改权威事实。
- **单行计数器。** workspace 配额与 host / instance 预算在各自权威行维护上限、held、consumed /
  quarantine 与 `version`，在同一事务中按固定顺序锁定并更新；不通过 `SUM` 推导实时可用量。
- **半开区间。** PCM / codepoint / sample 区间使用 `start_units < end_units`，存储为
  `DECIMAL(38,0)`；锁 stream head 后做重叠查询并追加不可变事实。
- **空字符串。** Oracle 将空字符串视为 NULL，所有文本字段统一规定“缺失用 NULL，禁止写入空串”；
  应用在写库前拒绝空串，四库测试验证同一语义。
- **排序。** 不使用可空排序列；分页采用 `(sort_key, id)` keyset 条件和 `fetchmany(page_size + 1)`。

## Job 与 outbox

不使用 `SKIP LOCKED`，以 `coordination_claim_buckets` 提供有限的逻辑并发：

1. worker 读取一个 bucket 的 `version`、owner 和 lease。
2. 用条件 `UPDATE` 竞争 bucket：`WHERE bucket_no=? AND version=? AND (owner IS NULL OR lease_until<=?)`。
3. `rowcount=1` 的 worker 在 bucket 内按 `due_at, job_id` 选择 Job，再以 Job version CAS 认领。
4. bucket 与 Job 分离 lease；bucket 过期不会撤回仍在有效 Job lease 内的执行，结果写入仍校验
   Job owner/version/epoch。
5. outbox 使用相同 bucket 模式；通知、Redis 或本地事件循环只作可选唤醒，持久轮询负责完成。

这样无需数据库跳跃锁、通知或存储过程，且每个 bucket 的候选扫描和锁范围有界。

## 提交未知与错误

应用不读取数据库错误码或 SQLSTATE。统一规则：

- 单行 CAS 以 `rowcount` 判定成功或冲突。
- `INSERT` 未提交即失败：回滚后按业务唯一键查询；命中且摘要相同返回原身份，命中但摘要不同报冲突，
  未命中才允许同一幂等键重试。
- `commit()` 调用期间连接断开、超时或异常：事务状态记为 `commit_unknown`；只查询原身份，
  不新建执行、不提前释放预算、不自动换副本。
- 任何品牌异常只进入日志和驱动诊断，不进入业务分支。

## 与既有决定的关系

- 扩展 [ADR 0006](0006-cluster-coordination-and-storage-portability.md)：保留单一事务权威、
  受控应用事务、不采用 etcd 和 fencing；把“PostgreSQL 唯一正式适配器”改为过渡事实。
- 保留 [ADR 0005](0005-complete-execution-and-data-lifecycle-contracts.md) 的提交未知、
  停止证据、区间账本和一次结算要求。
- 现有 PostgreSQL 专用 SQL、Alembic/ORM 方言路径和双库测试原型是迁移输入，不是目标架构。

## 替代方案

| 方案 | 结论 | 原因 |
| --- | --- | --- |
| 中立端口 + 每品牌 adapter | 拒绝 | 应用层无品牌分支，但维护多份语义实现，无法证明只换驱动 |
| ORM 自动生成 SQL | 拒绝 | ORM 会根据方言生成不同 SQL，仍存在隐式适配层 |
| 手写最低公分母 SQL | 采用 | 限制语义换来确定性、可审计性和真正的单路径 |
| 统一 ODBC driver | 采用 | 统一参数、事务、结果和异常边界；运行时只替换 driver 与 DSN |
| 继续使用 PostgreSQL 专有特性 | 拒绝 | 违反迁移只换驱动的目标 |

## 影响

- 失去 `SKIP LOCKED`、upsert、`RETURNING`、数据库通知、JSONB 和范围约束等便利；需要通过
  claim bucket、应用生成 ID、回滚后原身份查询和应用层区间检查补偿。
- 数据库写入模式更保守，候选扫描需要 bucket 调度和轮询；换来的是应用单 SQL 路径和可证明的
  驱动替换。
- 迁移工作集中到 schema、ODBC 驱动资格和数据搬迁，不再分散到业务层方言分支。
- 目标数据库必须提供固定版本的 ODBC driver；如果目标环境没有合格 ODBC driver，该数据库不能
  声明支持，而不是退回品牌专属适配层。

## 验收与未验证项

接受本决定前，设计必须冻结 SQL 子集、DDL、驱动契约、claim bucket、实时 grant / credit、
提交未知规则和四库测试矩阵。至少验证：

- 同一 schema 在 PostgreSQL、MySQL、Oracle、OceanBase MySQL/Oracle 模式建立成功。
- 同一 SQL 绑定 `?` 参数并在目标 ODBC driver 下返回可转换的统一类型。
- 并发最后预算不超发、同幂等键只产生一个 Job、旧 version / epoch 更新 0 行。
- 通知丢失仍推进；commit unknown 只查原身份。
- 实时 grant 内短抖继续；grant 耗尽后显式背压；重复区间不重复结算。

当前未验证：四库实际运行、ODBC 驱动一致性、存量数据迁移、目标负载、主备切换、备份恢复和
真实实时 ASR/TTS 链路。设计通过不等于迁移或生产资格完成。

## 依据

以下资料在 2026-11-21 核验，用于确认共同接口与锁定/时间能力；具体版本在实现资格时锁定。

- [PEP 249 DB-API 2.0](https://peps.python.org/pep-0249/)：参数风格由数据库接口声明，统一驱动需要
  明确选择 `qmark` 并验证。
- [Microsoft ODBC 参数标记](https://learn.microsoft.com/en-us/sql/odbc/reference/syntax/sqlprepare-function?view=sql-server-ver17)：
  ODBC 使用 `?` 参数标记。
- [MySQL Locking Reads](https://dev.mysql.com/doc/refman/8.4/en/innodb-locking-reads.html) 与
  [Oracle `SELECT FOR UPDATE`](https://docs.oracle.com/en/database/oracle/oracle-database/26/zzpre/defining-controlling-transactions.html)：
  行锁必须在显式事务中使用并在提交/回滚时释放。
- [OceanBase `SELECT FOR UPDATE`](https://www.oceanbase.com/docs/enterprise-oceanbase-database-cn-10000000000377026)：
  MySQL/Oracle 模式均支持行锁查询。
- [PostgreSQL `CURRENT_TIMESTAMP`](https://www.postgresql.org/docs/current/functions-datetime.html)：
  该函数在 PostgreSQL 取事务开始时间，证明锁等待后不能把写事务内结果当作新鲜时刻；设计改用
  锁后的独立短探针。
