---
title: 可移植 SQL 十一族 rule 的行业实践与处置判决
status: active
date: 2026-11-25
scope: scripts/dev-env/check-portable-sql.py + packages/portable-sql/src/speechnexus_portable_sql/subset.py
---

# 可移植 SQL 十一族 rule 的行业实践与处置判决

基线里 11 个 rule 族，各族**业界最佳实践是什么**、我们**现在改 / 激活时改 / 永久接受**。判决口径是 ROI，不是清零。

首发数据库只有 PostgreSQL；MySQL/InnoDB 只验证关键协调路径；Oracle/OceanBase 未激活，其资格**不做**。

核对时间 2026-11-25（Asia/Shanghai）。抓取到的 `postgresql.org/docs/current` 当日指向 **PostgreSQL 19**（[文档索引](https://www.postgresql.org/docs/)，2026-11-25 核对，Online Version 第一行 `19 / Current`）。MySQL 引 **8.4 LTS**，并对 innovation 线 **26.7** 做了对照。

## 1. 一页结论表

计数取自 `scripts/dev-env/portable-sql-baseline.json`，并经守卫实跑复核（2026-11-25 实跑：`findings=127 rules=11 files=47`）。**「条数」与「文件数」是两个数**：任务书表格里的 24/17/15/9… 是文件数，条数是 43/28/23/12…，不可互换。

| 族 | 条数 | 文件 | 判决 | 一句话理由 |
| --- | ---: | ---: | --- | --- |
| `sql-string-building` | 43 | 24 | **现在改**（分档，§2） | 问题不是「拼了字符串」，而是**片段注入**把禁用构造带进 SQL 且守卫看不见 |
| `vendor-json` | 28 | 17 | **激活时改** | 阻塞点在**列类型**（`jsonb` 列），是 schema 级改动；现在只动 `?` 操作符 |
| `vendor-type` | 23 | 15 | **现在改** 20 / **永久接受** 3 | 20 条是 `CAST(… AS text)` 与 `CAST(? AS timestamptz)`；3 条是本地 SQLite |
| `paging-limit` | 12 | 9 | **现在改** | `FETCH FIRST` 与 `LIMIT` 各缺一个大目标，**没有**单一可移植写法 |
| `advisory-lock` | 4 | 4 | **现在改** | 3 处写侧幂等键（唯一约束即可删锁），1 处读侧重放（用已有键锁表行） |
| `cte` | 3 | 3 | **永久接受** + 规则收窄 | 不是「CTE 不可移植」，而是 `pg_catalog.pg_roles` 权限内省 |
| `vendor-function` | 3 | 2 | **现在改** | `EXTRACT(EPOCH …)`、`array_agg` 都能挪到应用侧，与 `vendor-time` 同批 |
| `vendor-time` | 3 | 2 | **现在改** | 正确替代**已经在树里**（`CAST(? AS BIGINT)`），剩下的是删旧路径 |
| `session-tuning` | 2 | 1 | **永久接受** | 不是会话调优，是「我在哪个库/是否副本」；无标准等价 |
| `pyformat` | 1 | 1 | **现在改** | 与 `vendor-time` 同一处 readiness，`%(name)s`→`?` 是机械替换 |
| `runtime-ddl` | 5 | 1 | **永久接受** | 节点本地 SQLite 文件，不是激活目标，也不是协调权威 |
| **合计** | **127** | **47** | 现在改 **86** / 激活时改 **28** / 永久接受 **13** | — |

分布可算：现在改 = 43+20+12+4+3+3+1 = **86**；激活时改 = **28**；永久接受 = 3+3+2+5 = **13**。

**基线之外还有 417 条**：`--profiles` 把 59 个已登记的 `*_schema.py` 物理 DDL 制品拆出去（实跑 `profile-ddl=417`）。它不是本任务的处置对象；但只看 127 会以为「债只剩一百多条」，那是把大头当成零头。

## 2. `sql-string-building`（43 条 / 24 文件）

**在做什么.** 判据是「出现以语句关键字开头的 f-string」（`check-portable-sql.py:259-266`），命中后不区分拼的是什么。实际三类：

- **标识符来自命名常量**（多数）：`deletion_plans_postgres.py:581` `SELECT {_PLAN_COLUMNS}, intent_digest FROM {DELETION_PLANS} WHERE workspace_id = ? …`；`postgres_exports.py:194` `SELECT {_COLUMNS} FROM {s}.export_jobs WHERE id = ?{suffix}`。
- **SQL 片段注入**（少数，风险最高）：`asr_settlement_debt.py:17` 定义 `_NOW = "CAST(? AS BIGINT)"`，在 `:115`、`:126` 以 `f"…<=({_NOW})…"` 注入；`backend/src/speechnexus_backend/speech/infrastructure/readiness.py:683` 的 `_JOB_LEVEL_DEBT` 同时注入 `{_SCHEDULE_STATE_SQL}`、`{_RETRY_COLUMNS_SQL}` 与 pyformat 占位符。
- **部署期 GRANT/REVOKE**：`asr_settlement_schema.py:10`、`tts_settlement_schema.py:10`、`qualified_speech_facts_postgres.py:48`。

守卫对第二类几乎无检测力，因为值层规则只跑在**含语句关键字的那一段**上：`job_cancellation_fence.py:134` 的片段藏着 `ON CONFLICT (job_id) DO NOTHING`，该段无关键字 → 只判 `sql-string-building`，不判 `upsert`；`voice_catalogue_postgres.py:76` 的 `document->'capabilities'->'languages' ? %s` 同时躲过 `vendor-json` 与 `format`，且 PG 的 `?` 存在性操作符与 ODBC `?` 标记**同形**。

**业界怎么做.** 参数只能绑**值**：PEP 249 定义 parameters「will be bound to variables in the operation」，变量即值位置（[PEP 249](https://peps.python.org/pep-0249/)，Final，2026-11-25 核对）。动态标识符只有两条路，后者优先：① **重构为固定语句**（本仓 `database-portability.md` §4.3 已写）；② **白名单 + 厂商引用函数**——PG `format('%I')`/`quote_ident`（[String Functions §9.4](https://www.postgresql.org/docs/current/functions-string.html)，PG 19，2026-11-25 核对）、Oracle `DBMS_ASSERT.ENQUOTE_NAME`（[DBMS_ASSERT](https://docs.oracle.com/en/database/oracle/oracle-database/19/arpls/DBMS_ASSERT.html)，19c，2026-11-25 核对）。**没有**跨库统一的标识符引用函数——这本身说明拼标识符不该是运行时形态。

**最优解：现在改，按三档分治.**（b）片段注入优先：判据是**守卫可见性**——一个 `{_NOW}` 能让 `clock_timestamp()`、一个片段能让 `ON CONFLICT` 进入业务 SQL 而基线不涨。改法是降级为同一函数内几条完整静态语句。（a）标识符：收益不在可移植性（常量无注入面），在**让规则能判**；按调用频次与是否跨进程排序，一次一个模块。（c）GRANT/REVOKE：并入 `scripts/dev-env/portable-sql-profiles.json` 的 `deploy` 字段（当前只登记 6 个 `*_grants.py`，`*_schema.py` 里的 GRANT 落在登记之外），从「业务 DML 债」变成「已声明部署制品」——既有唯一登记点，不是新定义点。

**代价与判据.**（b）约 6–8 处、每处拆成两条静态语句；（a）机械但涉及 24 个文件，分批；（c）只改清单 + 已存在的 CI 复算用例。**注入即红**：① 在 f-string **片段**里写 `ON CONFLICT`/`SKIP LOCKED`/`clock_timestamp()`，对应规则必须红（今天绿，这正是要补的判据）；② `%s` 放进片段，`format` 必须红（今天绿，见 §14.1）。

**解除条件.**（b）归零 → 片段注入消失；（a）归零 → 每模块只剩完整静态语句；（c）迁入 `deploy` → 不进业务基线。三者独立。

## 3. `vendor-json`（28 条 / 17 文件）

**在做什么.** 三种东西被一个规则名盖住：

1. **写 `jsonb`**：`builtin_voice_publication.py:256` `VALUES (?,?,?,?,?,CAST(? AS jsonb),?)`，目标列在迁移里就是 `document JSONB NOT NULL`（`migrations/versions/0029_builtin_voice_qualification.py:55`；`voice_publication_schema.py:85` 同）。
2. **读字段**：`qualification_provenance.py:52` `a.facts->>'instance_id'`；`qualification_admission_postgres.py:121/143` `a.intent->>'database'=current_database()`。
3. **把 JSON 当关系结构展开**：`postgres_versions.py:506` `… jsonb_array_elements(l.deleted) e WHERE e->>1 = ? …`（workbench 7 条同形状），列定义见 `deletion_schema.py:118` `deleted jsonb NOT NULL CHECK (jsonb_typeof(deleted)='array' …)`。

另有一条只是「借操作符」：`deletion_plans_postgres.py:468` 对 **TEXT 列** `request_document` 先 `CAST(… AS jsonb)` 再 `->`；`asr_settlement_preflight.py:24`、`asr_settlement_sql.py:69`、`tts_settlement_postgres.py:36` 同形（列本身是 `TEXT`，见 `coordination_schema.py:168`、`admission_command_schema.py:12`）。

**业界怎么做.** `json`/`jsonb` 是 PG 自家类型，只有 `jsonb` 支持包含/存在运算与 GIN（[JSON Types](https://www.postgresql.org/docs/current/datatype-json.html)，PG 19，2026-11-25 核对）；MySQL 是 `JSON` 列 + `JSON_EXTRACT`/`->`/`->>`/`JSON_VALUE`/`JSON_TABLE`（[JSON Function Reference](https://dev.mysql.com/doc/refman/8.4/en/json-function-reference.html)，8.4，2026-11-25 核对）。标准侧有 **SQL/JSON** 函数族，PG 文档把 `jsonpath` 称作 SQL/JSON path language、并把若干行为标为「PostgreSQL extension of the SQL/JSON standard」——即**标准化的是函数与路径语言，不是某家的二进制存储类型**。因此跨库最佳实践是 `VARCHAR`/`TEXT` + 应用侧序列化，JSON 只作「列里的不透明字符串」；需要按内部结构判定的地方**列化**。把 JSON 当第二张表用（`jsonb_array_elements` 展开后 join/`NOT EXISTS`）是最不可移植的用法。

独立于可移植性的硬冲突：PG 的 `?` 是 jsonb 存在性操作符（同页 Table「String exists as object key」），与本仓 `?` 参数标记同形，`voice_catalogue_postgres.py:76` 两者同句——这不是风格问题，是 `placeholder_count()` 会被操作符喂错。

**最优解：激活时改.** 理由不是难，而是**改在列类型上**：`document JSONB`/`deleted jsonb` 要变 `VARCHAR` + 应用侧编解码，涉及 profile DDL、迁移与全部 `->>` 站点。PG 单库下这件事**没有收益**（jsonb 的 GIN 与包含运算正是 PG 的优势），收益只在目标库激活时兑现。现在只动一条：删掉 `?` 存在性操作符与「仅为借操作符」的 `CAST(… AS jsonb)`，改成无歧义函数——否则参数标记契约在本仓内部自相矛盾。真正的架构债是 7 条 workbench 站点：`deleted` 应是一张子表（一行一个 `(ledger_id, resource_kind, resource_id)`），`NOT EXISTS` 变普通 join；这是唯一「JSON 干了关系表活」的地方，也是激活时第一项。

**代价与判据.** 子表化 = 一次迁移 + 回填 + 7 条语句；列类型切换 = profile DDL + 全部读写点；两者都在「激活某目标库」批次里付。**注入即红**：① `deleted` 放两个目标，`NOT EXISTS` 与子表版本逐行一致（两版并存才算验证过）；② 语句里同时出现 `?` 操作符与 `?` 参数时 `placeholder_count(sql) != len(params)` 必须失败——这条**现在**就能写，会立刻抓到 `:76`。

**解除条件.** 业务表无 `jsonb` 列 + JSON 只作 `VARCHAR` 应用侧串 + 无 JSON 展开参与关系判定。三者任一不成立即未解除。

## 4. `vendor-type`（23 条 / 15 文件）

**在做什么.** 任务书假设这族是 `UUID`/`JSONB`/`BYTEA`/`TIMESTAMPTZ`/epoch `BIGINT` 混合。**实际不是**：23 条里 `UUID`、`JSONB`、`BYTEA` 各 **0 条**。

| 形状 | 条数 | 例子 |
| --- | ---: | --- |
| `CAST(… AS text)` / 身份读 | 17 | `builtin_voice_publication.py:86` `SELECT current_database() AS db, current_schema() AS schema, CAST(installation_id AS text) …` |
| `CAST(? AS timestamptz)` | 2 | `builtin_voice_source_approval.py:155/177` `SELECT CAST(? AS timestamptz) > ? AS live` |
| `text[]`/`ARRAY` | 1 | `resource_pools_postgres.py:28` `COALESCE(CAST(aggregate.node_ids AS text[]), CAST(ARRAY[] AS text[]))` |
| SQLite `TEXT` 列 | 3 | `node-controller/src/speechnexus_node_controller/infrastructure/sqlite_intent_store.py:51`、`:57`、`:75` |

（`model_catalog_runtime.py:242` 的 `regprocedure`、`asr_settlement_debt.py:121` 的 `CAST(NULL AS text)` 混在前两类里。）

**业界怎么做.** 标准里裸 `timestamp` 就是 `timestamp without time zone`，PG 遵守；`timestamptz` 是 PG 缩写扩展；带时区值「按 `TimeZone` 转换后以 UTC 存」且不保留原时区（[Date/Time Types](https://www.postgresql.org/docs/current/datatype-datetime.html)，PG 19，2026-11-25 核对）。MySQL 是另一套：`TIMESTAMP` 写入按会话时区转 UTC、读出转回，`DATETIME` 不转，且 `TIMESTAMP` 上界 **2038-01-19**（[DATE/DATETIME/TIMESTAMP](https://dev.mysql.com/doc/refman/8.4/en/datetime.html)，8.4，2026-11-25 核对）。所以「epoch 微秒存 `BIGINT` 更可移植」的答案是**是，且不是妥协而是唯一解**：`BIGINT` 没有时区语义、没有 2038 上界、没有「会话时区决定存取」的隐式转换；代价是时间比较/排序必须用整数建谓词，DB 侧 `date_trunc`/`INTERVAL` 不可用，靠索引范围扫描承担。`TEXT` 不是标准类型名（标准是 `CHARACTER VARYING`/`VARCHAR`）——17 条多数命中的真实原因是**非标准拼写**，不是「text 不可移植」。

**最优解：现在改 20，永久接受 3.** 17 条 `AS text`：① 优先去掉 cast（另一端本就是 `VARCHAR`，cast 是遗留）；② 否则改 `CAST(… AS VARCHAR(128))`（对齐 canonical schema §5）；③ 若只为让驱动拿到 str，那是驱动层职责，不该写在语句里。2 条 `timestamptz` 优先级最高：把参数转成 `timestamptz` 后比较结果由**会话时区**决定，`+08:00` 会话把 `12:00Z` 读成 `20:00` 就是这么来的；替代是比 `_us` 整数，或比 `TIMESTAMP(6)` 且参数以 naive UTC `datetime` 绑定。`array_agg` 改返回明细行 + 应用侧聚合（控制面、行数受实例数约束）。SQLite 3 条见 §11。

**代价与判据.** 17 条半天量级机械改动；`timestamptz` 2 条改比较形状 + 补时区用例；`array_agg` 改返回形状。**注入即红**：会话语时区设 `+08:00`，断言「写入后读回的 epoch µs 等于原始 UTC 时刻」——今天红在 `:155`，改完必须在 `+08:00` 与 `-05:00` **两会话时区下都绿**（单一时区不算验证）。

**解除条件.** 无 `UUID/JSONB/BYTEA/timestamptz` 出现在业务 DML；`VARCHAR` 用标准拼写；时间只用整数 epoch µs 或 `TIMESTAMP(6)`+naive UTC。

## 5. `paging-limit`（12 条 / 9 文件）

**在做什么.** 12 条里 **10 条不是分页**，是「取一行」：无 `ORDER BY` 的 `LIMIT 1`（`admission_sql.py:231/235`）、`LATERAL … ORDER BY … LIMIT 1` 取 head（`asr_settlement_debt.py:107`、`postgres_render_units.py:215/268`）、`UNION ALL … LIMIT 1`（`postgres_versions.py:167`）、`LIMIT 2` 当**单例断言**（`publications_postgres.py:150` `CROSS JOIN (SELECT … LIMIT 2) i`，读到 2 行说明单例行被破坏）。剩下 2 条真分页：`assets_postgres.py:38/44` `… ORDER BY … LIMIT ?`。另有 `abort_recovery_sql.py:40` `ORDER BY … LIMIT ? FOR UPDATE`（批量认领 + 行锁）。

**业界怎么做.** 先破一个误解：**`FETCH FIRST` 不是可移植单一口径。** PG 两种都支持，并明说 `LIMIT`/`OFFSET` 是「PostgreSQL-specific syntax, also used by MySQL」，SQL:2008 引入 `OFFSET … FETCH`，标准要求 count/start 是「a literal constant, a parameter, or a variable name」（[SELECT](https://www.postgresql.org/docs/current/sql-select.html)，PG 19，2026-11-25 核对）。MySQL **没有** `FETCH FIRST`：8.4 与 26.7 的语法块都只有 `[LIMIT {[offset,] row_count | row_count OFFSET offset}]`，且参数必须是「nonnegative integer constants」，例外只有 prepared statement 的 `?` 与存储程序变量（[8.4](https://dev.mysql.com/doc/refman/8.4/en/select.html) / [26.7 SELECT](https://dev.mysql.com/doc/refman/26.7/en/select.html)，2026-11-25 核对）。Oracle 12c+ 有 `row_limiting_clause`，示例即 `FETCH FIRST 5 ROWS ONLY`、`OFFSET 5 ROWS FETCH NEXT 5 ROWS ONLY`（[SELECT §row_limiting_clause](https://docs.oracle.com/en/database/oracle/oracle-database/19/sqlrf/SELECT.html)，19c，2026-11-25 核对）。**结论：`FETCH FIRST` 在 MySQL 不成立，`LIMIT` 在 Oracle 不成立，`LIMIT ?` 还要 MySQL 的 prepared statement；没有一种写法是四库共集。**

再破一个：即便语法统一，**结果也不可移植**。PG 文档明说要用「an `ORDER BY` clause that constrains the result rows into a unique order. Otherwise you will get an unpredictable subset」，且重复执行可能返回不同子集（同页）。`admission_sql.py:231/235` 正是无 `ORDER BY` 的 `LIMIT 1`——**这是正确性缺陷，不只是可移植性缺陷**。与方言无关的答案是 **keyset（seek）分页**：`(sort_key, id)` 谓词 + 同向 `ORDER BY` + `fetchmany(page_size + 1)` 判 `has_more`。本仓已固化：`packages/portable-sql/src/speechnexus_portable_sql/paging.py:19-26` 的 `keyset_page()`（**已实现**；调用点覆盖属**未验证**）。

**最优解：现在改全族，但替代物不同.** 10 条「取一行」不是分页问题而是「唯一键读取」问题：有唯一键的用唯一键谓词（不需要 `LIMIT`）；`LATERAL … LIMIT 1` 改 `ORDER BY` + `fetchmany(1)` 或把 head 列化；`LIMIT 2` 单例断言改 `COUNT(*)` 或读出来判 `len(rows)==1`（这个技巧很聪明，但它是**用方言表达不变量**）。2 条 `LIMIT ?` → keyset（`reconcile_polled_at_us, expires_at_us, asset_id` 三元组排序键形状现成）。`abort_recovery_sql.py:40` 单独看：`LIMIT` + 行锁的批量认领，可移植形态是「keyset 读候选 id → 逐条 `FOR UPDATE` 复核」或 bucket CAS，是 12 条里唯一要改事务形状的。

**明确不建议**：把 `LIMIT n` 统一换成 `FETCH FIRST` 再宣称「符合 SQL:2008」——按上节在 MySQL 直接语法错误。「标准里写着」不等于「目标库支持」。

**代价与判据.** 10 条语句级小改（半天到一天）；2 条真分页加 keyset 谓词 + 越界用例（一天）；`abort_recovery` 改事务形状（最大）。**注入即红**：① 页边界反复插入/删除同序键值，断言「翻完所有页恰覆盖全集且无重复」（今天 `LIMIT ?` 版本应失败）；② 对无 `ORDER BY` 的 `LIMIT 1` 注入两行同键候选，断言结果稳定可解释（今天不稳定）。

**解除条件.** 业务 DML 无 `LIMIT`/`OFFSET`/`FETCH FIRST`；单行读取靠唯一键或确定性 `ORDER BY`；多行读取走 keyset。

## 6. `advisory-lock`（4 条 / 4 文件）

> 本族已有逐站决策文档 [`2026-11-25-lock-portability-decision.md`](2026-11-25-lock-portability-decision.md)（27 处）。本节只回答剩下 4 条，不重开 27 处的账。

**在做什么.** 27 处已迁到 `packages/portable-sql/src/speechnexus_portable_sql/keyed_lock.py` + `coordination_keyed_locks`（migration `0067_coordination_keyed_locks`，桶化行锁），剩 4 条是**幂等键**族：

| # | 位置 | 键 | 形态 |
| --- | --- | --- | --- |
| 1 | `builtin_voice_publication.py:67` | `builtin-voice-intent:<schema>:<key_digest>` | 写侧：查 `…publish_intents.key_digest`，无则插 |
| 2 | `publications_postgres.py:310` | `publish-intent:<schema>:<key_digest>` | 写侧：查 `…binding_publish_intents.key_digest`，无则写 after |
| 3 | `builtin_voice_source_approval.py:126` | `source-approval:<schema>:<source_approval_id>` | 写侧：查 `…source_approvals`，无则插 |
| 4 | `binding_lifecycle_postgres.py:375` | `catalog:…:idempotency:<key_digest>` | **读侧**：`_replay()` 写事务内先 `SELECT … WHERE idempotency_key_digest=?` |

**业界怎么做（决策规则）.** PG 文档定位 advisory lock 为「application-defined meanings … the system does not enforce their use」，并给出对比：「While a flag stored in a table could be used for the same purpose, advisory locks are faster, avoid table bloat, and are automatically cleaned up by the server at the end of the session」（[Explicit Locking §13.3.5](https://www.postgresql.org/docs/current/explicit-locking.html)，PG 19，2026-11-25 核对）。MySQL 的对等物**语义不等价**：`GET_LOCK()` 的锁「are not released when transactions commit or roll back」，只在 `RELEASE_LOCK()`/会话结束时释放；名字上限 64 字符；全局命名空间，非协作客户端也能占名；死锁报 `ER_USER_LOCK_DEADLOCK` 且「does not cause transactions to roll back」（[Locking Functions](https://dev.mysql.com/doc/refman/8.4/en/locking-functions.html)，8.4，2026-11-25 核对）。

**因此规则只有一条**：互斥必须落在**能被事务自动释放**的载体上。四问，前一个成立就不看后面：

1. **被保护的动作就是「插入这个键的那一行」吗？** 是 → 唯一约束 + insert-first（撞唯一键回滚到保存点后按业务键读回）。锁该**删**，不是替换。
2. **参与者能否保证同一行一定存在？** 能 → 天然行 `FOR UPDATE`；**不能** → 不许用天然行（行不存在时两引擎都不锁任何东西，得到的是静默失效）。
3. **是否有人需要「读到空」这件事本身可信（读侧重放）？** 是 → 必须有载体，唯一约束帮不上只读事务。
4. 以上都不成立 → **键锁表**一行 + `SELECT … FOR UPDATE`。

**最优解：4 条都现在改，命中既有原语.** #1–#3 第 1 问成立 → 删锁，改用 `transaction.py` 已有的 `insert_if_absent()`（唯一约束 + 保存点，**已实现**），把「先锁后查」换成「先插后查」。#4 第 3 问成立 → 换载体：它运行在写事务里、此前已持 binding 行锁（`:221` `self._lock(cursor, command.binding_id)`），动手前做一次 10 分钟核实——**若 key 与 binding 1:1**，行锁已承担串行化，advisory lock 冗余可直接删；**若同键可跨 binding**，必须落到 `coordination_keyed_locks` 一行（`keyed_lock.py` 已有 `acquire_in` 与键文本函数，**已实现**）。四条都不需要新表、新 profile 或品牌分支。

**代价与判据.** #1–#3 各一次换调用形状 + 一条并发用例；#4 先核实再定。**注入即红**：① 两笔同键并发，断言「恰一笔执行、另一笔 replay、都不死锁」；② **载体单点测试（最关键）**：把一侧载体换回 advisory lock 或天然行、另一侧不动，必须**失败**——载体分裂的症状不是死锁，而是两边都正常提交、只是不再互斥，只有专门用例能抓到；③ 删掉 ID 锁后 `builtin_voice_source_approval` 的「ID 锁 → voice 锁」次序仍须单调（`catalog_keyed_lock.py` 模块文档把它写成契约）。

**解除条件.** 4 条不再出现 `pg_advisory_xact_lock`；同一逻辑键在所有参与者上**只有一个载体**；`subset.py:185-186` 的规则保留（防回流闸门，不随债务清零删除）。

## 7. `cte`（3 条 / 3 文件）

**在做什么.** 三条**全部**是 `WITH RECURSIVE` + **PG 系统目录**：`model_catalog_runtime.py:140`（`pg_catalog.pg_roles` / `pg_auth_members` 递归求角色可达）、`workbench/runtime.py:293`（同形状，以 `current_user` 为根）、`reader_authority.py:240`（同形状，判断登录角色可达）。它们不是「业务查询顺手用 CTE」，而是**「这个连接到底以什么权限在跑」的授权探测**。

**业界怎么做.** `WITH [RECURSIVE]` 三库**都有**：PG（[SELECT §WITH Clause](https://www.postgresql.org/docs/current/sql-select.html)，PG 19，2026-11-25 核对）、MySQL 8.0 起（[WITH](https://dev.mysql.com/doc/refman/8.4/en/with.html)，8.4，2026-11-25 核对）、Oracle recursive subquery factoring（[SELECT §subquery_factoring_clause](https://docs.oracle.com/en/database/oracle/oracle-database/19/sqlrf/SELECT.html)，19c，2026-11-25 核对）。但可移植的**形状**很窄：Oracle 原文「You must combine the recursive member with the anchor member using the UNION ALL set operator」（同页 Recursive Subquery Factoring；锚点成员自身可用 `UNION`/`INTERSECT`/`MINUS`）——我们三条都在递归成员上用 `UNION`，**在 Oracle 上不成立**。更关键：`pg_catalog.pg_roles`/`pg_auth_members`/`pg_has_role` 只有 PG 有；标准里的权限是 `INFORMATION_SCHEMA` 静态授权视图，没有「角色可达性递归」。业界不存在跨库读取角色继承图的 SQL。

**最优解：永久接受 3 条 + 收窄规则.** 两件事性质不同：

1. **这 3 条语句**属**授权探测**（`database-portability.md` §3「身份/异常」一行的实现手段），天然每库一份。处置是作为**已声明的静态方言制品**（与 `deploy`/`paths` 同一登记点 `portable-sql-profiles.json`）或驱动/部署期能力，而不是改写成「可移植 CTE」——不存在的不是 CTE 语法，是目录表。**注意这是「设计存在」而非「已经支持」**：今天没有任何目标库的等价实现，也没有登记。
2. **规则过宽**：`subset.py:189-190` 把 `WITH x AS (` 与 `WITH RECURSIVE` 一律判禁。按上节，**普通非递归 CTE 是共同子集**，把它算债会让基线虚高、诱导「为删计数而拆 CTE」的负收益动作。收窄只能在**唯一定义点** `subset.py` 做，判据从「用了 CTE」改成「递归 + 依赖品牌目录」；`MATERIALIZED`/`NOT MATERIALIZED` 是 PG 对标准的扩展（PG 文档「Nonstandard Clauses」明列），必须继续禁。收窄后这 3 条会从 `cte` 变成无规则命中或需重新归类，属可预期的 `FIXED`/`STALE-EXCEPTION`，必须在同一次 PR 内连同 `portable-sql-baseline.json`、`portable-sql-exceptions.json` 一起处理干净。

**代价与判据.** 登记 3 条（小）；改 `subset.py` 一条规则 + 重生成基线 + 清 exceptions（中，一次完成）。**注入即红**：普通 `WITH x AS (SELECT …)` 不再因 `cte` 红；`WITH RECURSIVE` + `pg_auth_members` 仍必须红；`WITH x AS MATERIALIZED (…)` 必须红。

**解除条件.** 业务 DML 不含品牌目录递归；`cte` 判据是「构造」而非「语法」；3 条探测已登记且**未**被任何 DML 路径执行。

## 8. `vendor-function`（3）与 `vendor-time`（3）

两族共 6 条、3 个文件，是同一件事：**用 SQL 算「现在」和时间差**。

**在做什么.** `backend/src/speechnexus_backend/speech/infrastructure/readiness.py:147`/`:423` `CAST(EXTRACT(EPOCH FROM clock_timestamp()) * 1000000 AS BIGINT)`（同时命中两族）；`postgres_versions.py:499` `SELECT clock_timestamp() AS now`；`resource_pools_postgres.py:28` `array_agg(...)`。选 `clock_timestamp()` 而非 `now()` 的原因正是它给**语句执行时**的墙钟，而 `CURRENT_TIMESTAMP` 给**事务开始时间**——本仓要的是前者（锁后读「新鲜现在」）。

**业界怎么做.** `CURRENT_TIMESTAMP`/`CURRENT_DATE`/`CURRENT_TIME` 是标准；`now()` 是 PG 对 `CURRENT_TIMESTAMP` 的等价拼写，`clock_timestamp()`/`statement_timestamp()`/`transaction_timestamp()` 是 PG 扩展（[Date/Time Types §8.5.1.4 函数清单](https://www.postgresql.org/docs/current/datatype-datetime.html)，PG 19，2026-11-25 核对）。`EXTRACT` 是标准，但 `EPOCH` 字段不是。其它引擎是**别的机制**（MySQL 用 `SYSDATE()`/`NOW()` 区分），没有跨库一致的「语句墙钟」函数名。**所以可移植的核心不是某个时间函数，而是「时间只有一个来源」**：取一次「现在」，作为**参数**传进所有语句——这正是本仓 `database-portability.md` §4.3 已写成的契约。与 naive-UTC 契约的关系要说死：一旦让数据库把时间转成 epoch，`EXTRACT(EPOCH FROM …)` 的语义就绑在**会话时区与类型**上（`timestamptz` 按 `TimeZone` 转换、`timestamp` 不转；MySQL `TIMESTAMP` 又按会话时区双向转换），**同一表达式在三库对同一时刻给三个结果**，这正是 `12:00Z → 20:00` 的成因。正确做法：数据库只交出**时刻**（`CURRENT_TIMESTAMP`），epoch 换算在应用侧，两端都显式 UTC。

**最优解：现在改 6 条，全表 ROI 最高的一批.** 依据在代码：**正确形状已在树里**——`asr_settlement_debt.py:17` `_NOW = "CAST(? AS BIGINT)"`、`artifacts_postgres.py:177` `_NOW_US = "CAST(? AS BIGINT)"`、`backend/src/speechnexus_backend/speech/infrastructure/readiness.py:786` `CROSS JOIN (SELECT CAST(%(now_us)s AS BIGINT) AS now_us) AS now_clock`。也就是说 `backend/src/speechnexus_backend/speech/infrastructure/readiness.py:147/423` 与 `postgres_versions.py:499` 是**旧路径残留**，不是「没设计」：删 SQL 内时钟，`now_us` 一律由 `ClockReader`/探针给出后以 `?` 绑定；`array_agg` 改应用侧聚合。隐藏面必同时处理：`qualification_source_evidence.py:41` 的 `_NOW_US = "CAST(floor(extract(epoch FROM clock_timestamp())*1000000) AS bigint)"` **不在基线里**（纯片段常量，`SQL_SHAPE`（`check-portable-sql.py:120-124`）不把它当 SQL），不改它则「清零」是假象。

**代价与判据.** 3 文件约 6 处，全是「删时钟 + 传参」；readiness 还要把 `%(now_us)s`/`%(grace_us)s`/`%(limit)s` 换 `?`（与 §10 同批，同一批语句）。**注入即红**：① `+08:00` 与 `-05:00` 两会话时区下时间断言结果必须**相同**（今天红在 `clock_timestamp()`/`timestamptz` 路径）；② 写事务内制造一次唯一键等待后再取「现在」，断言最终 `now_us` **晚于**等待开始时刻（证明不是事务开始时间，替换后仍须成立）；③ 任一业务 SQL 无 `EXTRACT`/`clock_timestamp`/`now()`，且片段常量也已消失（今天这条注入是绿的）。

**解除条件.** SQL 内无时钟函数与 `EXTRACT(EPOCH …)`；「现在」由探针 + 参数提供且**单点**；epoch 换算只在应用侧一处发生，naive-UTC 契约有两时区用例。

## 9. `session-tuning`（2 条 / 1 文件）

**在做什么.** 两条都在 `execution-gateway/…/reader_authority.py`：`:113` 读 `current_database()`、`current_setting('search_path')`、`system_identifier`、`pg_postmaster_start_time()`、`inet_server_addr()`/`inet_server_port()`、`pg_is_in_recovery()`；`:227` 读 `current_setting('server_version_num')`、`SESSION_USER`/`CURRENT_USER`。命中 `session-tuning` 的直接原因是**输出别名 `AS search_path`**（未加引号，规则 `\bSEARCH_PATH\b` 命中），不是 `SET` 语句。它读的是「我这个连接落在哪个库/schema/是不是备库/版本多少」——**读者权威校验**，不是会话调优。

**业界怎么做.** 这些函数全部 PG 专有，`pg_is_in_recovery()` 更是复制拓扑特有。标准 SQL 只有 `CURRENT_USER`/`SESSION_USER`/`CURRENT_CATALOG`/`CURRENT_SCHEMA` 这一层，**没有**「我连的是不是主库」「system identifier 是多少」的标准表达；MySQL 侧是 `DATABASE()`、`@@server_uuid`、`@@read_only` 另一套名字与语义。会话超时也不一致（MySQL 的会话级执行超时 `max_execution_time` 官方明确作用于 `SELECT`：[WITH 页 §Limiting CTE Recursion](https://dev.mysql.com/doc/refman/8.4/en/with.html)，8.4，2026-11-25 核对）。**结论：超时与身份由应用层/deadline 承担，SQL 里只留读取。**

**最优解：永久接受，归入方言层.** 这两条语句的存在目的就是「证明我在正确的库上」，必须问目标库自己；改写成跨库语句不可能，删掉则失去护栏。正确形态是**每目标一份已登记的静态制品**，激活 MySQL/Oracle 时补对应制品并登记——不是运行时按品牌分支，也不是业务层分支。顺带记录：`assets_postgres.py:63` 一类「每条语句前 `SELECT set_config('statement_timeout', ?, true)`」有 20 余处（`binding_runtime.py:136`、`engine_registration.py:855` 等），属会话调优但守卫看不见——引号内 `'statement_timeout'` 被 `_mask_literals` 掩掉（实测 `find_forbidden("SELECT set_config('statement_timeout', ?, true)")` 返回空表）。它不产生基线条目，但它是「基线数字 ≠ 真实面」的第二个证据（第一个是 §8 的片段常量）。

**代价与判据.** 登记（小）。若将来要求禁运行时 `set_config`，那是独立改造（连接建立时设定 + 部署配置），不混进本族。**注入即红**：把 `:113` 别名改成 `AS current_search_path`，`session-tuning` 命中应消失——这实测能证明该族命中的是别名而非 SET，也就证明规则名与内容不符。

**解除条件.** 只有当「每个激活目标都有已登记的权威探针制品，且无业务 DML 读品牌目录/会话状态」时不再算债。它不会因改写消失。

## 10. `pyformat`（1 条 / 1 文件）

**在做什么.** `readiness.py` 的 debt 模板 5 条语句用 psycopg pyformat：`%(grace_us)s`、`%(limit)s`、`%(debt_class)s`、`%(now_us)s`、`%(window_us)s`，以 `cursor.execute(_DEBT, {"grace_us": …, "limit": …})`（`:378`）绑字典。守卫只报 1 条（`:783`），其余漏判（`%(name)s` 落在 f-string 片段里，片段不含语句关键字）。

**业界怎么做.** PEP 249 定义五种 paramstyle（`qmark`/`numeric`/`named`/`format`/`pyformat`），并说明「Variables are specified in a database-specific notation」（[PEP 249](https://peps.python.org/pep-0249/)，2026-11-25 核对）：`%(name)s` 是**合法 DB-API 风格，但不是 ODBC 的**；ODBC 标记是 `?`，本仓驱动契约也把「只允许 `?`」写成硬约束。参数风格属**驱动适配层**，不属 SQL 文本——业务 SQL 出现 pyformat 就是把驱动细节写进语句，换驱动必须改 SQL。

**最优解：现在改.** 与 §8 同批：5 条模板 `%(name)s` 全改 `?`，参数改**定长 tuple**、顺序写死。附带收益：`backend/src/speechnexus_backend/speech/infrastructure/readiness.py:895` 的 `"(" + ", ".join(f"%({name})s" for name in _VERIFIED_STOP_PARAMETERS) + ")"` 一并消失——它本质是 `dynamic-placeholder-list` 变体，而现有规则只抓 `["?"] * len(rows)`（`check-portable-sql.py:246-257`）。

**代价与判据.** 5 条语句 + 1 处占位符生成，全在一个文件；唯一代价是参数顺序写死后的可读性。**注入即红**：把 `?` 改回 `%(x)s` 且放在含关键字的那一段，守卫必须红；今天 `--selftest` 只有 `format` 的注入用例，**没有 `pyformat` 的**，需补一条。

**解除条件.** 业务 SQL 中无 `%(name)s`/`%s`/`:name`/`$1`；参数元数在源码里可数。

## 11. `runtime-ddl`（5 条 / 1 文件）

**在做什么.** 5 条全在 `node-controller/src/speechnexus_node_controller/infrastructure/sqlite_intent_store.py:51`、`:57`、`:67`、`:71`、`:75`：**节点本地 SQLite 文件**的建表/建索引，用 `CREATE TABLE IF NOT EXISTS` 在启动时执行，列类型是 SQLite 的 `TEXT`/`INTEGER`。模块文档写明「This store is deliberately not a second coordination authority … the record is a local file rather than a shared database dependency」（`:1-8`）。

**业界怎么做.** 运行时 DDL 的行业立场是「不要」，理由是**锁**而非规范：PG 多数 `ALTER TABLE` 形式取 `ACCESS EXCLUSIVE`，而只有它会阻塞普通 `SELECT`（[Explicit Locking §13.3](https://www.postgresql.org/docs/current/explicit-locking.html)，PG 19，2026-11-25 核对）。这也是本仓把 schema 变更收进 alembic、运行角色无 DDL 权限的原因。**但这条论证不适用 SQLite 本地文件**：单进程、单文件、启动时执行、失败即拒绝启动，不存在并发读者被阻塞。

**最优解：永久接受.** 判据不是「它是方言」，而是**它不在可移植面上**：SQLite 不是四个激活目标之一，这个库也不是产品数据库，它是节点重启后恢复自己事实的本地文件。按四库共同子集判红它，等于要求「本地文件格式也要能被 Oracle 执行」。边界要划清：**接受的载体是「这个模块是本地存储的唯一实现点」，不是「runtime-ddl 规则被豁免」**——规则必须留着，因为 `backend/src` 下的 `*_schema.py` 是迁移期制品（已由 `portable-sql-profiles.json` 的 `paths` 登记，并由 `tests/ci/test_declared_ddl_is_migration_only.py` 复算「没有非迁移消费者」），业务模块新出现 `CREATE/ALTER/DROP` 必须继续红。

**代价与判据.** 零。可选加固：把「`sqlite3` 只允许出现在该模块」写成导入边界检查（与 `vendor-sdk` 同族），使「本地存储只有一个实现点」可机检。**注入即红**：在 `backend/src` 任一业务模块注入 `CREATE TABLE`，`scan_python` 的 `runtime-ddl` 必须红。

**解除条件.** 不解除。条件只有一条：**不扩散**——本地存储实现点不增加，`node-controller` 不出现第二个数据库。

## 12. 不建议做的事

1. **把 `LIMIT n` 统一改 `FETCH FIRST n ROWS ONLY` 并宣称符合 SQL:2008。** MySQL 8.4 与 26.7 语法里没有该子句（§5），改了是把 PG/Oracle 写法伪装成可移植，等 MySQL 激活时一次性爆出来。可移植的是 keyset + `fetchmany`。
2. **为把 127 降到 0 去动 `profile-ddl` 的 417 条。** 那是 59 个已登记物理 DDL 制品，消费者只有 alembic 迁移树和 `provision-roles.py`，物理 DDL 本就允许方言。动它等于取消 profile 概念：数字好看，可移植性不变。
3. **为 `cte` 三条重写 SQL。** 不存在的不是 CTE 语法（三库都有）而是 `pg_catalog.pg_roles`；重写只是把「读品牌目录」搬到别处，还引入 `UNION`→`UNION ALL` 的语义变化（Oracle 要求递归成员 `UNION ALL`）。正解是登记 + 收窄规则（§7）。
4. **给 SQLite 本地存储新增 profile。** 「注册 `sqlite-local` profile」看似最省事，但会把「本地文件」写进「部署期可替换的产品数据库」的概念里，还要登记 `targets`/`evidence` 并进资格矩阵——为一个不在激活范围的本地文件付这份登记成本是负收益。接受在**基线**（带卡与理由）比接受在**profile 注册表**更准确。
5. **让业务层按品牌选 SQL（哪怕一处 `if database == 'mysql'`）。** 守卫已禁 `brand-branch`/`brand-literal`；本文补原因：载体分裂的症状不是报错，而是**静默不再互斥**——MySQL `GET_LOCK` 不在提交时释放，混用两种载体时两边都「成功」。方言差异只能出现在已声明的静态制品里。
6. **给「时间」「JSON 编解码」「参数标记」各开新 helper 模块。** 三者本仓已有唯一定义点：时间在 `clock.py`、参数标记在 `subset.py` 的 `PARAMETER_MARKERS`；JSON 编解码若需要，只能落在既有 `contract.py`/`types.py`。再开 `json_utils.py` 就是第二个定义点，违反 `AGENTS.md` 的「一个不变量一个定义点」。

补充一条**流程**建议（非技术判决）：本族清单不要再从文件名数推。`sql-string-building` 是 **43 条 / 24 文件**，任务书表格里的 24 是文件数；按印象估数会让这一族排期整体失真（`AGENTS.md`「收益与进度先清点再报数」已记录同类返工）。

## 13. 解除条件汇总

| 族 | 让它不再算债的条件 | 判据（注入即红） |
| --- | --- | --- |
| `sql-string-building` | `(b)` 片段注入归零；`(a)` 每模块只剩完整静态语句；`(c)` 迁入 `deploy` 登记 | 片段里注入 `ON CONFLICT`；f-string 片段里出现 `%s` |
| `vendor-json` | 无 `jsonb` 列；JSON 只作 `VARCHAR` 应用侧串；无 JSON 展开参与关系判定；无 `?` 操作符 | `?` 操作符与 `?` 参数并存；`deleted` 数组与子表结果不一致 |
| `vendor-type` | 无 `UUID/JSONB/BYTEA/timestamptz`；`VARCHAR` 标准拼写；时间只用 epoch µs 或 `TIMESTAMP(6)`+naive UTC | `+08:00` 与 `-05:00` 两会话时区断言结果不同 |
| `paging-limit` | 无 `LIMIT/OFFSET/FETCH FIRST`；单行读取靠唯一键或确定性 `ORDER BY` | 页边界增删后翻页重复或漏行 |
| `advisory-lock` | 4 条无 `pg_advisory_xact_lock`；同一逻辑键只有一个载体 | 一侧换载体后并发用例不再互斥（**必须失败**） |
| `cte` | 业务 DML 不读品牌目录；判据改为「递归 + 品牌目录/`MATERIALIZED`」；3 条已登记 | 普通 `WITH` 仍红（说明规则没真收窄） |
| `vendor-function` | SQL 内无 `EXTRACT(EPOCH …)`/聚合；聚合在应用侧 | 任一业务 SQL 出现 `EXTRACT` |
| `vendor-time` | SQL 内无时钟函数；「现在」由探针 + 参数提供且单点 | 写事务内等待后再取「现在」取到事务开始时间 |
| `session-tuning` | 每激活目标都有已登记的权威探针制品；无业务 DML 读会话/目录状态 | 探针缺失时仍宣称「在正确库上」 |
| `pyformat` | 无 `%(name)s`/`%s`/`:name`/`$1`；元数在源码里可数 | 占位符按运行期长度生成 |
| `runtime-ddl` | 不扩散：本地存储实现点不增加 | `backend/src` 业务模块出现 `CREATE/ALTER/DROP` |

## 14. 未验证

1. **SQL 标准是否已标准化 JSON 数据类型**（SQL:2016 标准化了 SQL/JSON 函数族；SQL:2023 是否引入 JSON 类型）——**未核实**。可核实的只是 PG 文档把 `jsonpath` 称作 SQL/JSON path language、并把若干行为标为「extension of the SQL/JSON standard」（§3 链接），足以支撑「函数族有标准、存储类型没有跨库一致形态」，不足以断言类型标准化的版本。
2. **`JSON_TABLE`/`JSON_VALUE` 在 PG 的引入版本**（16/17/18 的具体界限）——**未核实**；本文不依赖版本界限。
3. **ODBC 规范「标识符不可参数化」的原文**——未直接核实；§2 的论证用 PEP 249 的绑定定义 + PG `%I`/Oracle `DBMS_ASSERT` 两个厂商引用函数的存在，属间接依据。
4. **PG `INSERT` 文档「唯一索引使并发插入互相阻塞」的原文**——本次未重抓，结论引自 [`2026-11-25-lock-portability-decision.md`](2026-11-25-lock-portability-decision.md) §2.2 所引条款（该文档记 2026-11-24 核验）。§6 的决策规则不依赖该条也成立（依赖行锁/唯一约束的事务语义）。
5. **MySQL 引入 CTE 的确切小版本**（8.0.1？）——未核实；本文只断言「MySQL 8.0 起有 `WITH [RECURSIVE]`」，依据是 8.4 手册 §15.2.20 的存在与内容。
6. **SQLite 本地存储在可预见期内不会成为第 5 个激活目标**——这是产品判断而非文档结论。§11 的「永久接受」以此为条件，需产品侧确认一次。
7. **`resource_pools` 的 `array_agg` 改应用侧聚合后的实例规模**——未实测。若单池节点数很大，移除 `array_agg` 会增加行数，需一次容量确认才能断言「代价可忽略」。
8. **OceanBase（MySQL/Oracle 两模式）对 `FETCH FIRST`/`JSON_TABLE`/`WITH RECURSIVE` 的支持**——未核实，且按既定范围**不做**资格；任何依赖其行为的结论都不成立。
9. **`binding_lifecycle_postgres.py:375` 的键是否与 binding 1:1**——§6 已写成「动手前 10 分钟的核实项」，本次未读完全部调用链，不断言「锁可删」或「必须换键锁表」。
10. **`keyset_page()` 是否已被每个分页调用点使用**——`paging.py` 实现已读（已实现），调用点覆盖未逐一核对（未验证）。

### 14.1 顺带实测到的守卫盲区（不是推断）

三条都用实际调用验证过，属「已实现但覆盖不足」，供后续收窄判据使用：

1. **f-string 片段化**：值层规则只跑在**含语句关键字的那一段**（`check-portable-sql.py:259-266`、`:279-281`），所以 `voice_catalogue_postgres.py:76` 的 `?` 操作符与 `%s`、`job_cancellation_fence.py:134` 的 `ON CONFLICT`、`readiness.py` 的 `LIMIT %(limit)s` 都不产生对应族命中。
2. **片段常量不是语句**：`SQL_SHAPE` 要求出现语句关键字，因此 `qualification_source_evidence.py:41` 的 `CAST(floor(extract(epoch FROM clock_timestamp())*1000000) AS bigint)` 完全不被扫描（实测直接对该串调 `find_forbidden` 会返回 `vendor-time`+`vendor-function`，但守卫从不把它当 SQL）。
3. **引号内掩码**：`_mask_literals` 把 `'statement_timeout'`、`'search_path'` 一起掩掉，所以 `SELECT set_config('statement_timeout', ?, true)` 实测 `find_forbidden` 返回空表（全仓 20 余处同形）；`reader_authority.py` 的 `session-tuning` 命中其实来自**别名 `AS search_path`**，规则名与命中内容不符。

这三条不改变各族判决，但改变「基线数字代表多少真实面」的解读：**127 是「规则能看见的债」，不是「可移植债的全集」。**
