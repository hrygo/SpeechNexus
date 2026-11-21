---
title: 可移植数据契约、受限数据库方言与实时预算
status: proposed
date: 2026-11-21
---

# ADR 0016 可移植数据契约、受限数据库方言与实时预算

## 背景

本项目首发数据库明确为 PostgreSQL。产品保持同一应用产物可迁移到 PostgreSQL、MySQL、Oracle、
OceanBase 等固定目标版本的能力，但非 PG 目标只在明确激活时取得资格。领域和应用
不得维护多套数据库适配器或按品牌选择 SQL；同一 canonical 逻辑 schema、statement contract、事务
边界、锁序和恢复语义必须跨库成立。迁移数据库可同时替换 ODBC 数据源、底层驱动、目标 DDL 制品和
显式 SQL profile 选择，但不得修改业务软件设计。

上一版设计仍允许“中立端口 + 多 adapter + 品牌快路径”。它可以在当前 PostgreSQL 上保持高性能，
但无法证明业务代码只换驱动即可迁移，也无法阻止 PostgreSQL 语法、异常码、upsert、
`SKIP LOCKED` 和数据库通知继续扩散。本决定改为单一应用数据契约与受限数据库方言。

本决定区分四件事：

1. **应用运行时可移植性。** 同一应用产物、statement contract 和事务语义通过统一 ODBC 边界运行。
2. **受限数据库级差异。** 允许少量等价物理 DDL，以及共同 SQL 无法执行时登记的静态 DML profile；不得改变业务语义。
3. **数据库初始化。** 新数据库按同一 canonical 逻辑 schema 的目标库 DDL profile 建立 schema。
4. **存量数据迁移。** 跨库复制、校验、切换与回退仍是一次性部署作业，不能被“换驱动”掩盖。

当前实现仍是 PostgreSQL 专用路径，未达到本决定。公开实时链路也尚未完成生产装配与真实联验。

## 决定

1. **只有一个应用数据契约。** 领域、应用和业务用例不包含 `if database == ...`、数据库方言分支、
   品牌 SQL 模板、profile 身份或按品牌加载的 repository/adapter。应用依赖统一 ODBC DB-API；
   静态 profile catalog 仅存在于 `infrastructure`，由部署显式选择。
2. **冻结共同 SQL 语义子集。** 业务 statement 默认只表达 `SELECT`、`INSERT`、`UPDATE`、`DELETE`、基础 `JOIN`、
   `GROUP BY`、`ORDER BY`、`CASE`、`COALESCE`、`COUNT`、`SUM` 及静态谓词。
   禁止 upsert、`RETURNING`、`MERGE`、`SKIP LOCKED`、数据库通知、advisory lock、CTE、窗口函数、
   存储过程、触发器、数据库函数、动态 SQL、`LIMIT/OFFSET` 和品牌类型。受限 DML profile 不得借方言名义引入这些能力。
3. **使用 ODBC 统一参数与事务接口。** 应用 SQL 只使用 ODBC `?` 参数标记；连接关闭 autocommit，
   每条用例显式提交或回滚。厂商驱动必须满足同一结果类型、Unicode、时间、`rowcount` 和事务契约。
   **不得使用 ORM 或 SQLAlchemy 作为运行时方言适配层。**
4. **schema 只有一个 canonical 逻辑定义。** 表、列、主键、唯一键、外键、普通索引、NULL 性和迁移版本
   使用同一逻辑 fingerprint；物理 DDL profile 可调整等价类型拼写、索引 / 约束语法和部署属性。
   标识符保持 ASCII 小写、无引号、长度不超过 30；运行时不执行 DDL。
5. **应用生成身份。** 新 Job、Operation、lease、grant、event 和区间 ID 由应用生成 ULID；
   `VARCHAR(128)` 保留现有 ASCII 身份容量。禁止自增列、sequence、数据库 UUID 函数、
   `RETURNING` 或 `LAST_INSERT_ID`。
6. **数据类型收敛。** canonical 语义使用 `VARCHAR(n)`、`CHAR(n)`、`DECIMAL(38,0)` 和 UTC 时间；
   布尔用 `CHAR(1)` 的 `Y/N`，金额 / 配额 / 样本坐标使用十进制整数。禁止 `BOOLEAN`、`TEXT`、
   `CLOB`、`BLOB`、`JSON`、`JSONB`、数组、枚举、区间类型和品牌 UUID。音频正文与大数据留在对象存储。
   所有变长文本在应用层按 UTF-8 字节数限制为不超过声明长度，使 Oracle 的字节语义与 MySQL 的
   字符语义收敛到同一更严格上限；标识与摘要使用 ASCII 规范形式。
7. **时间和锁显式化。** 数据库实例与会话统一 UTC。需要数据库时间的决定先从专用池预取得探针连接，
   再开启写事务并完成全部潜在阻塞的锁 / 唯一键 / FK 写入；随后在同一权威主库读取
   `CURRENT_TIMESTAMP, schema_rev, schema_fingerprint, authority_epoch`，回到写事务做最终资格复核。
   探针与写事务共享硬 deadline，失败即回滚；不得持有写锁等待共享池里的第二连接。
   `schema_meta` 只提供共同 `FROM` 锚点、schema 身份和权威代次，不持久化数据库时间。需要串行化预算或
   区间时按固定顺序逐行执行 `SELECT ... FOR UPDATE`；高竞争 Job/outbox 使用 claim bucket 租约，
   不依赖 `SKIP LOCKED` 或数据库通知。
8. **失败语义不读品牌错误码。** CAS 以影响行数为准；插入冲突统一回滚后按业务唯一键查询原身份；
   提交调用抛错或结果不可确认一律视为 `commit_unknown`，只按原身份查询，不重试新身份。事务明确
   不依赖数据库默认隔离级别；只是在未提交异常后、确认原身份不存在且 deadline 尚存时，才允许用
   完全相同业务键重放，不依据 SQLSTATE 做透明重试。
9. **实时语音使用持久 grant 与本地 credit。** 会话准入先持久预留预算；网关只在已提交 grant 内
   发放本地 credit，低水位异步续租。数据库短抖时消费现有 credit，grant 耗尽后显式背压或结束。
   本地 credit 不要求逐帧持久化，但 ASR accepted、TTS 冻结输入和 TTS generated PCM 必须各自留下
   崩溃后可读取的恢复证据；缺失时保留 grant 并 quarantine。
10. **最终用量精确。** ASR 按 accepted canonical samples，TTS 输入按首次冻结 codepoint，
    TTS 输出按实际 generated PCM samples；边界区间半开、可重放且只结算一次。运行水位与交付展示
    可以近似，持久授权不能超发。进程内计数、投递量或播放进度不能单独作为最终用量证据。
11. **首发 PG，其他目标是后续 profile。** 首发 PostgreSQL 运行同一应用产物、statement manifest、
    逻辑 schema fingerprint 和完整 behavior oracle；物理 DDL 与已登记 SQL profile 必须通过 PG 资格。
    MySQL、Oracle、OceanBase 保持可扩展，但只有在各自完成同一 contract / oracle、切换回退、恢复和
    目标负载资格后才声明支持；任何未登记或改变业务不变量的差异都使该目标资格失败。

## 运行时替换边界

```text
业务用例
   |
   v
canonical statement contract（参数 / 结果 / 事务语义）
   |
   v
infrastructure 静态 SQL profile（默认共同 SQL，少量已登记 variant）
   |
   v
ODBC DB-API（唯一接口，? 参数）
      |
ODBC driver manager（不变）
      |
厂商 ODBC driver（替换对象）
      |
目标数据库（同一 canonical 逻辑 schema）
```

允许替换：ODBC driver、DSN、连接参数、数据库连接串、部署实例、物理 DDL 制品和显式 SQL profile 选择。

不允许替换：领域 / 应用代码、statement ID、参数 / 结果形状、canonical 逻辑 schema 语义、事务边界、
锁序、CAS / 错误 / 恢复语义、测试 oracle 或公开协议。

## 共同 SQL 子集

| 类别 | 允许 | 禁止 |
| --- | --- | --- |
| DML | 单行 `INSERT`、按 PK / 唯一键 `SELECT`、条件 `UPDATE`、条件 `DELETE`；运行时 variant 仅在已登记 profile 中 | upsert、`MERGE`、`RETURNING`、多行 `VALUES (...), (...)`、未登记 variant |
| 查询 | 基础 `JOIN`、`WHERE`、`GROUP BY`、`ORDER BY`、`COUNT`、`SUM`、`CASE`、`COALESCE` | `LIMIT/OFFSET`、`NULLS FIRST/LAST`、递归 CTE、窗口函数、`FILTER`、品牌 JSON 操作符 |
| 并发 | 单行 `SELECT ... FOR UPDATE`、version/owner/epoch CAS、claim bucket 租约 | `SKIP LOCKED`、`NOWAIT`、advisory lock、品牌隔离级别语法、锁表 |
| 时间 | 预取得专用连接；完成潜在阻塞写入后在权威主库读取 `CURRENT_TIMESTAMP, schema_rev, schema_fingerprint, authority_epoch`；应用传参 UTC `datetime` | 持写锁等待探针连接、依赖写事务内 `CURRENT_TIMESTAMP` 快照、锁前旧时间、应用时钟、`clock_timestamp()`、`NOW()`、`SYSDATE`、`SYSTIMESTAMP` |
| 类型 | canonical 语义为 `VARCHAR`、`CHAR`、`DECIMAL(38,0)`、UTC 时间；物理类型差异必须逐 profile 证明等价 | `BOOLEAN`、`TEXT`、`CLOB`、`BLOB`、`JSON*`、数组、枚举、区间类型、UUID 类型 |
| 身份 | 应用 ULID、SHA-256 十六进制、业务唯一键 | sequence、identity、自增、数据库生成 UUID |
| 迁移 | canonical 逻辑 schema、目标 DDL profile、显式 schema revision / fingerprint、离线数据迁移 | 改变逻辑约束的物理 DDL、ORM 自动建表、运行期 DDL、品牌迁移 DSL |

## 数据模型原则

- **协调记录与内容分离。** 数据库只保存身份、状态、版本、租约、预算、计数、摘要和对象引用；
  音频、转写正文、完整命令和大 JSON 放对象存储。
- **一表一权威。** 每个状态、配额和区间只有一个写入路径；后台对账不修改权威事实。
- **单行计数器。** workspace 配额与 host / instance 预算在各自权威行维护上限、held、consumed /
  quarantine 与 `version`，在同一事务中按固定顺序锁定并更新；不通过 `SUM` 推导实时可用量。
- **半开区间。** PCM / codepoint / sample 区间使用 `start_units < end_units`，存储为
  `DECIMAL(38,0)`；锁 stream head 后，先做同身份 / 同区间 / 同摘要的精确重放查询，再做锁定读取的
  部分重叠检查，最后追加不可变事实。锁前快照和唯一键不能代替该流程。
- **空字符串。** Oracle 将空字符串视为 NULL，所有文本字段统一规定“缺失用 NULL，禁止写入空串”；
  应用在写库前拒绝空串。PostgreSQL 首发验证该语义，其他目标激活时验证同一行为。
- **排序。** 不使用可空排序列；分页采用 `(sort_key, id)` keyset 条件和 `fetchmany(page_size + 1)`。

## Job 与 outbox

不使用 `SKIP LOCKED`，以 `coordination_claim_buckets` 提供有限的逻辑并发：

1. worker 读取一个 bucket 的 `version`、owner 和 lease。
2. 用条件 `UPDATE` 竞争 bucket：`WHERE bucket_no=? AND version=? AND (owner IS NULL OR lease_until<=?)`。
3. `rowcount=1` 的 worker 在 bucket 内按 `due_at, job_id` 选择候选，再在同一事务复核 bucket
   owner/version/lease 与 Job / outbox 当前版本后 CAS 认领。
4. bucket 与 Job 分离 lease；bucket 过期不会撤回仍在有效 Job lease 内的执行，结果写入仍校验
   Job owner/version/epoch。
5. outbox 使用相同 bucket 模式；通知、Redis 或本地事件循环只作可选唤醒，持久轮询负责完成。

这样无需数据库跳跃锁、通知或存储过程，且每个 bucket 的候选扫描和锁范围有界。

公共 Job SSE 使用每 Job 连续 `event_seq`：锁定 `coordination_jobs` 后从 `last_event_seq + 1` 生成，
事件与 head 同事务提交，`Last-Event-ID` 只按该序号续读。`coordination_outbox.event_id` 只做投递去重，
不能用 `(created_at,event_id)` 定义公共事件顺序，也不引入全局 sequence。

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
| 中立端口 + 每品牌 adapter | 拒绝 | 维护多份业务语义实现，无法证明应用契约可替换 |
| ORM 自动生成 SQL | 拒绝 | ORM 会根据方言生成不同 SQL，仍存在隐式适配层 |
| 所有目标使用字面相同 SQL / DDL | 不采用 | 会把无业务意义的物理差异误判为不支持，不能提高语义保证 |
| canonical 逻辑契约 + 受限静态 profile | 采用 | 允许少量方言，同时把语义、事务和恢复差异挡在业务之外 |
| 统一 ODBC driver | 采用 | 统一参数、事务、结果和异常边界；部署替换 driver、DSN 与 profile |
| 继续使用 PostgreSQL 专有特性 | 拒绝 | 违反迁移只换驱动的目标 |

## 影响

- 失去 `SKIP LOCKED`、upsert、`RETURNING`、数据库通知、JSONB 和范围约束等便利；需要通过
  claim bucket、应用生成 ID、回滚后原身份查询和应用层区间检查补偿。
- 数据库写入模式更保守，候选扫描需要 bucket 调度和轮询；换来的是单一应用数据契约和可证明的
  目标库行为等价性。
- 少量静态 SQL profile 与物理 DDL profile 需要登记和验证，但不得进入领域 / 应用或按品牌加载业务实现。
- 迁移工作集中到逻辑 schema、物理 DDL、ODBC 驱动资格、行为 oracle 和数据搬迁，不再分散到业务层方言分支。
- 目标数据库必须提供固定版本的 ODBC driver；如果目标环境没有合格 ODBC driver，该数据库不能
  声明支持，而不是退回品牌专属适配层。

## 验收与未验证项

接受本决定前，设计必须冻结 SQL 语义子集、canonical 逻辑 schema / fingerprint、PostgreSQL 首发物理
DDL / SQL profile、驱动契约、claim bucket、实时 grant / credit 崩溃证据、提交未知规则和测试矩阵。
至少验证：

- 首发 PostgreSQL 建立 canonical schema fingerprint，并通过真实业务消费者、并发、故障和故障恢复测试。
- 同一 statement manifest 在目标 profile / ODBC driver 下保持参数形状、结果、事务、锁序和错误语义。
- 并发最后预算不超发、同幂等键只产生一个 Job、旧 version / epoch 更新 0 行。
- 通知丢失仍推进；commit unknown 只查原身份；SSE 在逆序提交下仍按每 Job `event_seq` 无漏事件。
- 实时 grant 内短抖继续；grant 耗尽后显式背压；三类 meter 的崩溃证据缺失时进入 quarantine；
  精确重放返回原区间、部分重叠拒绝且不重复结算。
- 固定模型、拓扑、并发和数据库故障窗口下，报告会话准入、首轮 grant、低水位续租、首包、credit 饥饿、
  结算与双工 / batch 竞争的 P50/P95/P99/P99.9；数据库贡献与端到端总数分开记录。

当前未验证：非 PG 目标实际运行、四库 ODBC 驱动一致性、存量数据迁移、目标负载、主备切换、备份恢复、
实时语音 SLO / 故障资格和真实实时 ASR/TTS 链路。设计通过不等于迁移或生产资格完成。

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
