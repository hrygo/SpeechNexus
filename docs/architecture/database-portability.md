---
title: 单一可移植 SQL 数据路径与实时预算设计
status: active
version: "0.2.0"
date: 2026-11-21
---

# 单一可移植 SQL 数据路径与实时预算设计

本文冻结 PostgreSQL、MySQL、Oracle、OceanBase 共同支持的最小 SQL、数据模型、事务模式与驱动契约，
作为 [ADR 0016](../decisions/0016-database-portability-core.md)的实施设计。目标不是维护多套按品牌
分叉的数据访问实现，而是让同一应用产物、同一 SQL、同一 schema 只通过 DSN 和底层 ODBC 驱动完成
数据库替换。

本文规定目标架构。当前 PostgreSQL 专用实现仍含 `FOR UPDATE`、`SKIP LOCKED`、upsert、
`RETURNING`、advisory lock、数据库通知、品牌时间和 SQLSTATE 分类；迁移完成前不得宣称目标已经达到。

## 1. 可移植性定义

### 1.1 必须成立的运行时条件

1. 应用代码不导入厂商数据库 SDK，不按数据库品牌分支，不加载品牌 repository。
2. 同一份 SQL 文本在四类数据库上执行；只使用 ODBC `?` 参数标记。
3. 数据库连接统一关闭 autocommit，由用例显式 `commit()` / `rollback()`。
4. 同一份 schema / DDL 在所有目标数据库建立；运行时 schema revision 相同。
5. 结果类型在公共 DB-API 边界统一为 `str`、`Decimal`、`datetime`、`None` 等稳定值。
6. 迁移数据库只替换 ODBC driver、DSN、连接配置和数据库实例。应用文件、SQL 与 schema 不变。

### 1.2 不等于运行时替换的部分

- 新数据库仍须执行一次 schema bootstrap。
- 存量数据仍需一次性复制、校验、切换与回退；这一过程可能使用独立运维工具。
- 数据库实例必须按共同前提配置：UTC、Unicode 编码、字符长度语义、足够的 ODBC driver 版本与最小权限。

把数据搬迁也声称成“只换驱动”是不真实的。可验收的承诺是：**应用运行时和 SQL 不因数据库品牌变化。**

## 2. 总体结构

```mermaid
flowchart LR
    App[业务用例 / 静态 SQL]
    DBAPI[统一 ODBC DB-API]
    Manager[ODBC driver manager]
    Driver[厂商 ODBC driver]
    DB[(目标关系数据库)]

    App --> DBAPI
    DBAPI --> Manager
    Manager --> Driver
    Driver --> DB

    Obj[(对象存储)]
    App -. 身份 / 摘要 / 对象引用 .-> Obj
```

应用不定义“PostgreSQL adapter / MySQL adapter”。唯一的数据访问入口是共同 ODBC DB-API。
`pyodbc` / `aioodbc` 一类统一 ODBC 接口可以保留；它们不属于业务 adapter，也不得生成不同方言 SQL。

## 3. 驱动契约

厂商 ODBC driver 由统一 DB-API 使用，必须满足以下契约：

| 维度 | 契约 | 验证 |
| --- | --- | --- |
| 参数 | SQL 使用 ODBC `?` 参数；绑定 Python `str`、`Decimal`、`datetime`、`None` | 同一 SQL 四库执行；`%s` / `:1` / `$1` 被 guard 拒绝 |
| 事务 | autocommit 关闭；`commit()` 成功才表示提交，异常/断连视为 unknown | 提交未知探针 |
| 行数 | DML 后 `rowcount` 表示实际影响行数；0 为 CAS 失败 | 并发 CAS 测试 |
| 十进制 | `DECIMAL` 不经过 float；应用统一转 `Decimal` | 精确计数、预算与样本区间 |
| 时间 | 独立短时间探针执行 `SELECT CURRENT_TIMESTAMP`，统一转换为 UTC `datetime` 后作为参数传入事务；不依赖各库函数的事务时间语义 | 跨库时间 round-trip、锁等待后新鲜度 |
| 文本 | UTF-8 字符串原样往返；所有 `VARCHAR(n)` 在应用层按 UTF-8 字节数限制为不超过 `n`；Oracle 空串问题由应用禁止空串规避 | 多语言文本、边界长度、NULL、空串反例 |
| 异常 | 业务不读取 SQLSTATE / vendor error code | 注入品牌错误字符串仍不改变行为 |
| 结果 | 列名、顺序和 NULL 形状由静态 SQL 固定 | 四库结果 oracle 逐行一致 |

任何 driver 不满足契约，只能标记“不支持”，不能添加业务适配分支绕开。

## 4. 共同 SQL 子集

### 4.1 允许的语句

- 单行 `INSERT INTO ... VALUES (...)`。
- 按主键 / 唯一键读取，带固定列清单的 `SELECT`。
- 基础 `JOIN`、静态 `WHERE`、`GROUP BY`、确定性 `ORDER BY`。
- 条件 `UPDATE` / `DELETE`，以 `rowcount` 判定 CAS。
- `CASE`、`COALESCE`、`COUNT`、`SUM` 等四库共有标量或聚合表达式。
- 单行、显式事务内的 `SELECT ... FOR UPDATE`。

### 4.2 禁止的语句与能力

| 禁止项 | 替代方案 |
| --- | --- |
| `ON CONFLICT` / `ON DUPLICATE KEY` / `MERGE` | 先 `INSERT`；失败回滚后按唯一键查询原身份 |
| `RETURNING` / `LAST_INSERT_ID` / sequence / identity | 应用生成 ULID；提交后按 ID 查询 |
| `SKIP LOCKED` / `NOWAIT` / advisory lock / `LOCK TABLE` | claim bucket + 单行 CAS；预算 / stream head 使用 `FOR UPDATE` |
| 数据库通知 / job 调度函数 / 触发器 | 持久 outbox + 轮询 + bucket |
| `LIMIT` / `OFFSET` | keyset 条件 + `cursor.fetchmany(page_size + 1)` |
| 动态 `IN (?, ?, ...)` 扩展 | 固定小集合，或逐身份读取；列表查询走分页与 join |
| CTE / 窗口函数 / `FILTER` / 递归查询 | 预计算列、单行 head 与普通索引查询 |
| JSON / JSONB / 数组 / 区间类型 / 枚举 / `BOOLEAN` | 列式短字符串、`CHAR(1)`、`DECIMAL`；大对象放对象存储 |
| `TEXT` / `CLOB` / `BLOB` | 对象存储 + `VARCHAR` 引用 / 摘要 |
| 存储过程、函数、触发器、运行期 DDL | 应用事务 + 部署期 SQL migration |

### 4.3 参数与 SQL 文本规则

- 只允许 ODBC qmark 参数 `?`；禁止字符串插值和手工 SQL 转义。
- 表、列、索引名使用 ASCII 小写、无引号、长度不超过 30；避免保留字。
- SQL 文本来自常量模块或经审查的查询构造器；品牌分支在静态检查中直接失败。
- `SELECT` 不只做 `SELECT 1`：Oracle 需要 `FROM`，健康检查统一读取 `schema_meta` 的单行。
- 动态排序字段不得来自用户输入；分页、过滤和投影集合固定。
- 权威数据库时间通过独立短探针读取：
  `SELECT CURRENT_TIMESTAMP FROM schema_meta WHERE meta_id = ?`。该行只提供共同 `FROM` 锚点和 schema
  revision，不持久化时间。需要等待行锁的路径在取得全部锁后运行探针，再把返回的 UTC `datetime`
  绑定到当前事务；同一业务决定只使用该值。不得依赖 `CURRENT_TIMESTAMP` 在写事务内的事务开始语义。

## 5. 数据类型

| 语义 | 唯一类型 | 约定 |
| --- | --- | --- |
| ID | `VARCHAR(128)` | 新身份由应用生成 ULID；保留现有短 ASCII 身份，禁止为空 |
| 短标识 / 枚举 | `VARCHAR(64)` | ASCII 小写，禁止空串 |
| 摘要 | `CHAR(64)` | SHA-256 小写十六进制 |
| 状态 / 布尔 | `CHAR(1)` | 固定单字符；布尔 `Y/N` |
| 数量 / 预算 / 样本坐标 | `DECIMAL(38,0)` | 不用 float；Python `Decimal` |
| 时间 | `TIMESTAMP(6)` | 统一 UTC，NULL 表示未设置 |
| 对象引用 | `VARCHAR(1024)` | ASCII 规范引用；不进入唯一索引 |
| 用户可见短文本 | `VARCHAR(255)` / `VARCHAR(2000)` | 数据库 UTF-8；应用按 UTF-8 字节数限长；缺失用 NULL，空串在入口拒绝 |

禁止依赖数据库默认值、数据库时区、数据库 UUID、数据库序列和自动更新时间。所有值由应用显式写入。

## 6. canonical schema

### 6.1 命名与约束

- schema 只有一份，按 revision 一次性建立；运行角色无 DDL 权限。
- 表名与现有 owner 表保持一致，使用 ASCII 小写且无品牌前缀；主键列名稳定，外键仅使用普通
  `FOREIGN KEY`，不使用级联删除。
- 唯一键承担幂等身份；业务多列不变量由应用事务维护，不依赖部分索引或数据库函数。
- 索引只使用普通 B-tree、固定列顺序，不使用表达式索引、部分索引、GiST 或覆盖索引语法。

### 6.2 核心表

| 表 | 职责 | 关键唯一键 / 索引 |
| --- | --- | --- |
| `schema_meta` | schema revision、独立数据库时间探针所需的单行 `FROM` 锚点 | `meta_id` PK |
| `coordination_jobs` | Job 身份、状态、version、bucket、claim owner/lease | `job_id` PK；`(operation_id,attempt_id)` 唯一 |
| `coordination_outbox` | SSE / outbox 事件、待投递状态 | `event_id` PK；`(job_id,event_digest)` 唯一 |
| `coordination_claim_buckets` | 有限逻辑并发、bucket owner/lease/version | `bucket_no` PK |
| `admission_leases` | 执行 / 引擎 / 资源租约与 fencing | `lease_id` PK；`(operation_id,attempt_id,stage_id)` 唯一 |
| `workspace_quotas` | workspace 并发上限、held / quarantine / version | `workspace_id` PK |
| `admission_budget_grants` | 实时会话持久 grant、epoch、续租序号 | `grant_id` PK；`(session_id,epoch,renew_seq,meter_kind)` 唯一 |
| `usage_intervals` | 输入 / 输出不可变计量区间 | `interval_id` PK；`(stream_id,meter_kind,start_units,end_units)` 唯一 |
| `output_stream_heads` | 每个逻辑输出流的串行化 head / version | `(operation_id,attempt_id,output_stream_id)` PK |

### 6.3 设计 DDL 片段

以下 DDL 只使用共同类型与基础约束，作为 schema 冻结输入；实际迁移需在四库资格中执行，
当前文件不代表已经验证。

```sql
CREATE TABLE schema_meta (
    meta_id VARCHAR(32) NOT NULL,
    schema_rev VARCHAR(64) NOT NULL,
    PRIMARY KEY (meta_id)
);

CREATE TABLE coordination_jobs (
    job_id VARCHAR(128) NOT NULL,
    operation_id VARCHAR(128) NOT NULL,
    attempt_id VARCHAR(128) NOT NULL,
    request_digest CHAR(64) NOT NULL,
    bucket_no DECIMAL(10,0) NOT NULL,
    job_state CHAR(1) NOT NULL,
    version DECIMAL(38,0) NOT NULL,
    claim_owner_id VARCHAR(128),
    claim_until TIMESTAMP(6),
    due_at TIMESTAMP(6) NOT NULL,
    created_at TIMESTAMP(6) NOT NULL,
    updated_at TIMESTAMP(6) NOT NULL,
    PRIMARY KEY (job_id),
    UNIQUE (operation_id, attempt_id)
);

CREATE INDEX coordination_jobs_ready_idx
    ON coordination_jobs (bucket_no, job_state, due_at, job_id);

CREATE TABLE coordination_claim_buckets (
    bucket_no DECIMAL(10,0) NOT NULL,
    version DECIMAL(38,0) NOT NULL,
    claim_owner_id VARCHAR(128),
    lease_until TIMESTAMP(6),
    PRIMARY KEY (bucket_no)
);

CREATE TABLE workspace_quotas (
    workspace_id VARCHAR(128) NOT NULL,
    max_attempts DECIMAL(38,0) NOT NULL,
    held_attempts DECIMAL(38,0) NOT NULL,
    quarantined_attempts DECIMAL(38,0) NOT NULL,
    version DECIMAL(38,0) NOT NULL,
    updated_at TIMESTAMP(6) NOT NULL,
    PRIMARY KEY (workspace_id)
);

CREATE TABLE admission_budget_grants (
    grant_id VARCHAR(128) NOT NULL,
    session_id VARCHAR(128) NOT NULL,
    lease_id VARCHAR(128) NOT NULL,
    owner_id VARCHAR(128) NOT NULL,
    epoch DECIMAL(38,0) NOT NULL,
    renew_seq DECIMAL(38,0) NOT NULL,
    meter_kind VARCHAR(32) NOT NULL,
    granted_amount DECIMAL(38,0) NOT NULL,
    consumed_amount DECIMAL(38,0) NOT NULL,
    returned_amount DECIMAL(38,0) NOT NULL,
    version DECIMAL(38,0) NOT NULL,
    expires_at TIMESTAMP(6) NOT NULL,
    created_at TIMESTAMP(6) NOT NULL,
    PRIMARY KEY (grant_id),
    UNIQUE (session_id, epoch, renew_seq, meter_kind)
);

CREATE TABLE output_stream_heads (
    operation_id VARCHAR(128) NOT NULL,
    attempt_id VARCHAR(128) NOT NULL,
    output_stream_id VARCHAR(128) NOT NULL,
    version DECIMAL(38,0) NOT NULL,
    max_end_units DECIMAL(38,0) NOT NULL,
    updated_at TIMESTAMP(6) NOT NULL,
    PRIMARY KEY (operation_id, attempt_id, output_stream_id)
);

CREATE TABLE usage_intervals (
    interval_id VARCHAR(128) NOT NULL,
    session_id VARCHAR(128) NOT NULL,
    attempt_id VARCHAR(128) NOT NULL,
    stream_id VARCHAR(128) NOT NULL,
    meter_kind VARCHAR(32) NOT NULL,
    start_units DECIMAL(38,0) NOT NULL,
    end_units DECIMAL(38,0) NOT NULL,
    fact_hash CHAR(64) NOT NULL,
    PRIMARY KEY (interval_id),
    UNIQUE (stream_id, meter_kind, start_units, end_units)
);

CREATE INDEX usage_intervals_stream_idx
    ON usage_intervals (stream_id, start_units, end_units);
```

真实 schema 还包含 Operation、asset、artifact、binding、policy 等表，但只允许复用以上类型、
命名和约束规则。完整命令、正文、音频和大 JSON 只保存对象引用与摘要。

## 7. 事务模式

### 7.1 幂等插入

不在 SQL 中做“存在则更新”。业务使用确定性唯一键：

1. 应用先生成 ULID 和 `request_digest`。
2. `SELECT` 唯一键；已存在且摘要一致，返回原结果；摘要不同，冲突。
3. 不存在则执行单行 `INSERT`。
4. 任一未提交异常：尝试 `rollback` 后在新连接上按唯一键查询；不读取品牌错误码。
5. `commit()` 异常：返回 `commit_unknown`，后续只按原身份查询。

事务不依赖数据库默认隔离级别或品牌隔离语法。权威判断只来自条件写入的 `rowcount`、唯一约束和
显式行锁。未执行 `commit()` 前出现异常时，整笔事务按回滚处理；只有确认原身份不存在、仍满足
deadline 且使用完全相同业务键时才允许重新执行，不做基于品牌 error code 的透明重试。

取得可能需要等待的权威行锁后，数据访问层通过一条独立、只读的短事务读取当前数据库时间，
再回到仍持有锁的写事务完成资格复核。探针必须连接同一权威主库并立即用于当前决定；不能在不同
数据库品牌间假定 `CURRENT_TIMESTAMP` 的事务快照语义相同。探针失败、超时或可能落到副本时，
当前写事务回滚，不授予资源、不释放预算，也不回退到应用时钟。

### 7.2 配额与预算准入

完整配方在同一事务中按固定锁序锁定需要变更的配额与资源权威行。workspace 并发额度示例：

```text
  SELECT max_attempts, held_attempts, quarantined_attempts, version
    FROM workspace_quotas
   WHERE workspace_id = ?
     FOR UPDATE
  校验剩余额度并计算新值
  UPDATE workspace_quotas
     SET held_attempts = ?, version = ?, updated_at = ?
   WHERE workspace_id = ? AND version = ?
```

随后按同一固定顺序锁 host/instance 预算行，并在同一事务插入 Job/outbox、lease 和 grant。
任何一步影响行数不符或约束失败，整个事务回滚。不在事务内执行推理、HTTP、对象上传或长编码。

### 7.3 Job / outbox claim bucket

不使用 `SKIP LOCKED`。固定数量 bucket 将竞争范围从全表缩小到单个 bucket：

```text
1. SELECT version, claim_owner_id, lease_until FROM coordination_claim_buckets WHERE bucket_no = ?
2. UPDATE coordination_claim_buckets SET version = ?, claim_owner_id = ?, lease_until = ?
    WHERE bucket_no = ? AND version = ?
      AND (claim_owner_id IS NULL OR lease_until <= ?)
3. rowcount = 0 时放弃并按退避重试
4. bucket 持有者读取该 bucket 的 ready Job，按 due_at, job_id 取第一个
5. CAS 更新 Job version、state、claim_owner、claim_until；rowcount = 1 才执行
6. 结果发布再次校验 Job owner/version/epoch；旧 owner 只能失败或隔离
```

bucket lease 只限制 dispatcher 进入；Job lease 单独控制执行归属。这样无需数据库跳跃锁、
通知或全表长期锁，也能保持多 worker 唯一胜者。

### 7.4 outbox 与事件流

- outbox 行与 Job 状态变更同事务写入，保存最小事件字段和对象引用。
- outbox 不依赖全局事件序号；应用生成 ULID `event_id`，并按 `(created_at,event_id)` 做稳定分页。
- 投递 worker 通过 bucket 获取待办；通知缺失时持久轮询继续推进。
- 重复投递按 `event_id` 与 `(job_id,event_digest)` 唯一键去重。

### 7.5 分页与批量读取

- 不使用 `LIMIT/OFFSET`。按 `(created_at, id)` 加 keyset 谓词，`ORDER BY` 使用相同方向和列。
- 多取一条判断 `has_more`；返回的整页对象按固定顺序读取。
- 禁止运行时拼接可变 `IN` 列表；需要批量关联时使用规范 join 或按身份逐项读取，并限制 fan-out。

### 7.6 区间追加

1. `SELECT version FROM output_stream_heads
   WHERE operation_id=? AND attempt_id=? AND output_stream_id=? FOR UPDATE`。
2. 在应用层或同事务查询 `start_units < ? AND end_units > ?`；发现同 meter 重叠则拒绝。
3. 先执行同一区间幂等查询；精确重放返回原区间。
4. 插入 `usage_intervals`，更新 output stream head version 和 `max_end_units`，提交。

不使用 GiST、范围约束、触发器或数据库函数。

## 8. 实时预算 grant 与本地 credit

### 8.1 数据路径

```mermaid
sequenceDiagram
    participant C as 客户端
    participant G as 实时网关
    participant L as 本地 credit
    participant DB as 统一 SQL 数据路径

    G->>DB: 锁配额 / 租约权威行
    G->>DB: 独立短事务执行 CURRENT_TIMESTAMP 探针
    DB-->>G: 已验证 grant / version / expiry
    G->>L: 只发放已提交额度
    C->>G: PCM / 文本
    G->>L: 本地扣减，不逐帧查库
    L-->>G: 低水位
    G->>DB: CAS renew_seq / epoch / version
    DB-->>G: 新持久额度
    G->>L: 提交成功后才增加 credit
    G->>DB: segment / utterance 边界 usage
    C-->>G: close / cancel / disconnect
    G->>DB: 原身份结算与一次回收
```

### 8.2 不变量

- 对每个 grant，`persisted_consumed + locally_unsettled_consumed + returned + outstanding_credit
  <= granted_amount`。
- `outstanding_credit >= 0`，`locally_unsettled_consumed >= 0`；结算只把本地已消费量迁移到持久量，
  不重复增加总消费。
- 每次新增本地 credit 之前，对应 grant / 续租已成功提交。
- 旧 epoch / version / owner 的续租或 usage 更新 `rowcount=0`。
- 数据库不可用短于 credit 余量时继续；超过余量时背压或有限 drain。
- 提交未知时保留本地和持久预算，不按应用估算退款。

### 8.3 计量

| 流 | 单位 | 边界 |
| --- | --- | --- |
| ASR 输入 | accepted canonical samples | `[start,end)`，按 segment / utterance 聚合 |
| TTS 输入 | 首次冻结正文 Unicode scalar values | commit / finish 后写一次 |
| TTS 输出 | generated canonical PCM samples | 每个 attempt 的真实生成区间 |

credit 不需要每帧绝对精确，但持久授权不能超发，最终 usage interval 必须精确、可重放且只结算一次。
delivered / received / played 只作诊断，不替代生成或接受量。

## 9. 四库边界条件

| 边界 | 统一规则 |
| --- | --- |
| Oracle 空字符串等于 NULL | 入口禁止 `""`；缺失统一 NULL；四库反例测试 |
| 时间类型 / 时区 | 实例和会话 UTC、`TIMESTAMP(6)`；锁后从同一主库的独立短事务读取 `CURRENT_TIMESTAMP`，driver 输出统一 UTC |
| MySQL 默认隔离级别 | 不依赖普通一致性读做权威判断；多行决策使用 `FOR UPDATE` 或 bucket CAS |
| OceanBase 双模式 | MySQL 与 Oracle 模式都作为独立目标运行同一 schema / SQL / oracle |
| ODBC 参数 | 只允许 `?`；不使用 Python driver 的 `%s` / `:name` / `$1` 风格 |
| 分页 | keyset + cursor fetch；不用 `LIMIT/OFFSET` |
| 数值 | `Decimal`，禁止 float；所有预算与样本坐标用 `DECIMAL(38,0)` |
| 大对象 | 不进入数据库；对象存储保存正文、音频、完整命令和事件大 payload |
| Unicode | 数据库按 UTF-8 / Unicode 配置；所有 `VARCHAR(n)` 按 UTF-8 字节数限长，避免 Oracle 字节与 MySQL 字符长度口径分叉 |

## 10. schema 与数据迁移

### 10.1 schema 迁移

- schema 变更由单一版本化 SQL 目录管理，部署角色执行，应用账号只有 DML 权限。
- expand / migrate / switch / contract 分开执行；运行时不检查后自动建表。
- 任何 DDL 必须在四个目标数据库执行；不能通过 PostgreSQL 专用 DDL 后再声称可移植。
- 迁移脚本可以重复检查 schema revision，但不得依赖品牌 upsert、database function 或 DDL 事务。

### 10.2 数据搬迁

数据搬迁不属于运行时驱动替换，具体要求：

- 从源库按业务身份导出，写目标库时使用同一 canonical schema 与同一应用写入路径。
- 切换前完成行数、摘要、预算、租约、outbox、区间和终态的逐项对账。
- 双写不作为默认方案；若临时使用，必须有单一胜出权威或明确的 fencing，不能产生双账本。
- 回退保留源库、源快照、导出摘要与切换窗口；数据搬迁成功不等于实时 / 物理集群资格完成。

## 11. 测试与资格

### 11.1 同一测试程序与数据

每个目标运行同一组 SQL 与行为 oracle：

- PostgreSQL 固定版本。
- MySQL 固定 8.4 LTS 或项目实施时冻结的兼容版本。
- Oracle 固定版本（如 19c 或项目目标版本）。
- OceanBase MySQL 模式。
- OceanBase Oracle 模式。

数据库版本、ODBC driver 版本、Unicode / UTC / 字符长度配置和 DDL revision 均写入资格报告。

### 11.2 必测反例

- 通过 application artifact 注入 PostgreSQL 导入、SQLSTATE、品牌 SQL 或 `%s` 参数，static guard 变红。
- 把 `?` 改成另一个 driver 的参数风格，DB-API conformance 变红。
- 在 SQL 中加入 `SKIP LOCKED`、`RETURNING`、`JSONB` 或 `LIMIT`，SQL subset guard 变红。
- 移除 Job version / epoch 条件，两个 worker 的“唯一赢家”测试变红。
- 在加本地 credit 前跳过 grant 提交，grant 上限测试变红。
- 允许空字符串写入，Oracle 语义测试变红。
- 锁等待后用写事务内 `CURRENT_TIMESTAMP` 或应用时钟替代独立新鲜探针，时钟偏移 / 锁等待探针变红。
- 同一测试程序只替换 DSN / ODBC driver，应用文件摘要保持不变。

### 11.3 资格边界

四库单元和合成集成通过，不等于：

- 真实引擎、真实音频、真实 GPU 或浏览器联验；
- 主备切换、备份恢复、网络分区、数据库故障窗口；
- 大规模数据搬迁或在线切换；
- 性能、容量、GC、锁等待和 P95/P99 达标。

## 12. 性能与高 ROI

| 优先级 | 方案 | 成本 | 收益 |
| --- | --- | --- | --- |
| P0 | SQL 子集、canonical schema、ODBC 契约与静态 guard | 中 | 一次性消除业务层方言分叉 |
| P0 | 应用 ULID、插入原身份查询、commit unknown | 低到中 | 兼容四库并保留可靠恢复 |
| P0 | claim bucket + 单行 CAS + outbox 轮询 | 中 | 替代 `SKIP LOCKED` 和通知，正确性可移植 |
| P0 | 实时 grant、本地 credit 与边界 usage | 中 | 数据库短抖不穿透音频热路径，预算不超发 |
| P1 | 四库同 SQL conformance harness | 中到高 | 把口头可移植变成可验收能力 |
| P1 | 普通索引与 keyset 分页优化 | 低 | 弥补最低公分母查询的不足 |
| P2 | 数据搬迁工具与切换演练 | 高 | 解决实际迁移，但不改变运行时 SQL |

先做前三项。新增品牌专用数据路径不再符合目标；接入其他数据库的方式是安装对应 ODBC driver、
执行相同 schema，并运行同一测试 oracle。

## 13. 当前到目标的迁移

现有 PostgreSQL 路径必须分步收敛：

1. 清点并冻结所有 SQL 语句，建立允许 / 禁止清单和静态扫描。
2. 将应用生成 ULID、单行插入、rowcount CAS、回滚后原身份查询写入公共数据访问模块。
3. 把 Pool/Job/lease/outbox/usage 的 PG 专用 SQL 替换为共同 SQL；用 claim bucket 替代
   `SKIP LOCKED`，用应用区间检查替代 GiST / 触发器。
4. 将品牌异常、SQLSTATE、数据库时间、连接池和迁移工具移出业务层。
5. 建立同一 schema 和 ODBC driver 的四库测试环境。
6. 完成真实业务消费者的同 SQL 验证后才允许切换或补充目标数据库。

迁移期间，旧 PostgreSQL 路径和新共同路径不能同时独占写入同一权威表；切换必须有 schema
revision、租约 / fencing 与回退证据。

## 14. 依据与核验

以下资料于 2026-11-21 核验；具体版本和实施资格仍需在目标环境锁定：

- [PEP 249](https://peps.python.org/pep-0249/)：DB-API 的 `paramstyle` 由实现定义，因此产品层需要
  固定统一接口，而不是假定厂商 Python driver 原生相同。
- [Microsoft ODBC 参数标记](https://learn.microsoft.com/en-us/sql/odbc/reference/syntax/sqlprepare-function?view=sql-server-ver17)：
  ODBC SQL 参数使用 `?`，适合作为统一 SQL 边界。
- [MySQL locking reads](https://dev.mysql.com/doc/refman/8.4/en/innodb-locking-reads.html)：显式事务中的
  `FOR UPDATE` 行锁可作为预算与 stream head 的单行串行化机制。
- [Oracle 事务控制](https://docs.oracle.com/en/database/oracle/oracle-database/26/zzpre/defining-controlling-transactions.html)：
  `SELECT FOR UPDATE OF` 锁定活动行，锁在事务结束时释放。
- [OceanBase SELECT FOR UPDATE](https://www.oceanbase.com/docs/enterprise-oceanbase-database-cn-10000000000377026)：
  MySQL / Oracle 模式均提供行锁查询。
- [PostgreSQL 日期时间函数](https://www.postgresql.org/docs/current/functions-datetime.html)：
  `CURRENT_TIMESTAMP` 在 PostgreSQL 取事务开始时间，说明锁等待后不能直接复用写事务内的该值；
  因此统一时间探针必须使用锁后的独立短事务。
