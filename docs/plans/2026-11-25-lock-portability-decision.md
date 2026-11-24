# 27 处 advisory 锁的可移植决策

本文定下产品运行时里 27 处 `pg_advisory_xact_lock(hashtextextended(...))` 的去向：每一处用哪种
可移植机制承载，哪一处必须保留锁，按什么顺序做。判据是**正确性**：锁的每个参与者必须切换到
同一个载体，顺序与持有时长不变；不是「哪种改动最小」。

核验时间：2026-11-24（Asia/Shanghai）。所有关于当前行为的陈述都来自当次读到的源码，行号即版本。

---

## 1. 计数口径

### 1.1 27 的边界

27 = 产品运行时代码中形如

```sql
SELECT pg_advisory_xact_lock(hashtextextended('<域>:<schema>:<…>', 0))
```

的构造点，跨 9 个文件、2 个进程（backend、execution-gateway）、6 个键命名空间。

### 1.2 与 portability 基线的对账

`scripts/dev-env/portable-sql-baseline.json` 记 `advisory-lock` **32 条 / 24 文件**（仓库基线
总 196 条）。两者不是同一个数，差异逐条归位：

| 项 | 条数 | 依据 |
| --- | ---: | --- |
| 基线 `advisory-lock` 总数 | 32 | `portable-sql-baseline.json` |
| 其中整数键 (`pg_advisory_xact_lock(?)`) | 6 | `job_cancellation_fence.py:76`、`cancellation_fence_sql.py:24`、`workbench/infrastructure/postgres_deletions.py:436`、`postgres_meetings.py:152`、`postgres_minutes_tasks.py:294`、`postgres_versions.py:760` |
| 其中 `hashtextextended` 构造 | 26 | 32 − 6 |
| 门禁扫不到的 1 处 | 1 | `speech/infrastructure/migrations/env.py:21` |
| **本文口径** | **27** | 26 + 1 |

`env.py` 不进基线是明文规则，不是遗漏：`scripts/dev-env/check-portable-sql.py:79-83` 把整个
alembic 迁移树登记为 `MIGRATION_INPUT_PREFIXES`，注释写明「整个 alembic 迁移树是**迁移输入**,
不是产品运行时 SQL……受限方言约束的是应用运行时的 DML」。该文件自身在
`migrations/env.py:17-18` 也会拒绝非 PostgreSQL 连接。

因此本文的 27 里，**26 处是门禁在管的对象，1 处（迁移互斥）按既有规则不在可移植范围内**。
做完全部建议后，基线 `advisory-lock` 从 32 降到 6，剩下的 6 条全部是整数键族
（§6.4）。

### 1.3 两条「已存在原语」的核实结果

任务下达时的两条前提，与当前树不符，按代码更正：

**（一）`packages/portable-sql/src/speechnexus_portable_sql/keyed_lock.py` 不存在。**
`packages/portable-sql/src/speechnexus_portable_sql/` 下无此文件（现有模块：`budget`、
`claims`、`clock`、`contract`、`credit`、`logical_schema`、`manifest`、`markers`、`paging`、
`pool`、`psycopg_driver`、`psycopg_pool_driver`、`pymysql_driver`、`schema`、`subset`、
`transaction`、`types`、`usage`、`vendor`）。全仓也搜不到 `keyed_lock` / `lock_key` /
`bucket_collision` 字样。

当前真实存在的行级协调原语有两个，都不是通用键锁：

- `insert_if_absent()`（`packages/portable-sql/src/speechnexus_portable_sql/transaction.py:129-162`）：
  受限方言下的「唯一约束 + 保存点 + 冲突即返回 False」，按 `sqlstate == '23505'` 或 MySQL
  errno `1062` 判定（`transaction.py:102-116`）。
- `coordination_claim_buckets`（`claim_bucket_schema.py:29-35`，alembic `0066`）：256 个**播种**
  行，`bucket_no` 主键、`version` CAS。运行时只拿到 `SELECT` 与
  `UPDATE (claim_owner_id, lease_until, version)`，**没有 INSERT/DELETE**
  （`claim_bucket_schema.py:5-9,23`）。它无法为任意业务键建行，所以当不了键锁表。

**（二）`public_jobs` 的提交键锁没有被删除。** `_lock_submission_key()`
（`public_jobs_postgres.py:1292-1300`）仍在，且有四个活的调用点：

| 调用点 | 事务性质 | 作用 |
| --- | --- | --- |
| `:651` | 写（`create`） | 同键创建先串行化，再查原结果 |
| `:787` | 写（`create_revision`） | 同上 |
| `:1081` | **`SET TRANSACTION READ ONLY`**（`:1079`） | 读侧等写者提交，避免「刚创建就查不到」 |
| — | — | `_read(submission_key=…)` 的唯一使用者是 `find_by_key`（`:550-564`） |

只有唯一约束、没有这处锁时，写侧可以改成 insert-first；**读侧不能**——只读事务无法先插一行。
这一点决定了它不能像其他幂等键那样被唯一约束取代（§4.3）。

---

## 2. 行业立场

### 2.1 advisory lock 的定位

PostgreSQL 官方文档对 advisory lock 的定位是「应用程序自定义语义的锁」，用于「对 MVCC 模型
来说别扭的加锁策略」，并明确它与表无关、由应用自己保证用法正确
（<https://www.postgresql.org/docs/current/explicit-locking.html> §13.3.5，2026-11-24 核验）。
同一节还写明事务级锁「在事务结束时自动释放」、会话级锁不遵守事务语义。

同一节对「用表里的一个标记」这个替代方案给了代价判断，原文：

> While a flag stored in a table could be used for the same purpose, advisory locks are faster,
> avoid table bloat, and are automatically cleaned up by the server at the end of the session.

这句话是本文所有取舍的成本基线：**把键锁落到表行上，是用「更慢、会胀表、要自己清理」换
「跨引擎可执行」**。这一点在 §5 的代价栏里逐项落地。

函数语义见
<https://www.postgresql.org/docs/current/functions-admin.html> §9.29.10（Table 9.110，同日核验）：
`pg_advisory_xact_lock` 取排他事务级锁，等待；无显式释放。

### 2.2 五种可移植替代与代价

| 方案 | 机制 | 代价 | 何时正确 |
| --- | --- | --- | --- |
| 唯一约束 + 冲突重试 | 先 `INSERT`；撞唯一键则回滚到保存点后按业务键查回 | 需要一条真唯一键；每次冲突一个保存点；PG 会把整个事务标记为中止，必须用保存点（`transaction.py:79-86`） | 被保护的动作**就是**「插入这个键的那一行」 |
| 天然行 `SELECT … FOR UPDATE` | 锁住业务本来就要读写的那一行 | 行必须**已经存在**；不存在时两引擎都不锁任何东西 | 所有参与者都能保证该行存在 |
| 专用锁表 | `INSERT` 键行（不存在则插），再 `SELECT … FOR UPDATE` | 一表、一写入授权、每键一行、多一次往返；键无界时要有回收策略 | 键没有天然行，或参与者里有「行还不存在」的一方 |
| 乐观版本 CAS | 条件 `UPDATE … WHERE version=?`，以 `rowcount` 判成败 | 冲突时调用方必须能重试或转成业务冲突 | 读后写的判定可以压进写语句的谓词里 |
| `ON CONFLICT` / `ON DUPLICATE KEY` | 引擎内 upsert | **本仓禁用**：`subset.py:58-59` 禁止，替代写法是「先 INSERT,失败回滚后按业务唯一键查询」 | 不适用 |

前四种的引擎依据：

- **唯一约束本身就是并发串行点。** PostgreSQL `INSERT` 文档：
  「INSERT into tables that lack unique indexes will not be blocked by concurrent activity.
  Tables with unique indexes might block if concurrent sessions perform actions that lock or modify
  rows matching the unique index values being inserted」
  （<https://www.postgresql.org/docs/current/sql-insert.html>，2026-11-24 核验）。这就是
  「先 INSERT 顶替先加锁」在 READ COMMITTED 下成立的原因：后到者等前一个事务结束，再拿到
  唯一冲突。
- **READ COMMITTED 下等待者看到的是已提交版本。** 同一引擎的事务隔离文档：被阻塞的写入方
  「wait for the first updating transaction to commit or roll back」，提交后按更新后的行重新
  判定（<https://www.postgresql.org/docs/current/transaction-iso.html>，2026-11-24 核验）。
- **InnoDB 的锁是索引记录锁。** `SELECT … FOR UPDATE` 锁的是索引记录；只有在**唯一索引上做
  唯一值查找**时才不需要间隙锁，其余情况会锁间隙；`READ COMMITTED` 下间隙锁被关闭（外键与
  重复键检查除外）（<https://dev.mysql.com/doc/refman/8.4/en/innodb-locking.html>，2026-11-24
  核验）。锁读还要求关闭 autocommit
  （<https://dev.mysql.com/doc/refman/8.4/en/innodb-locking-reads.html>，同日核验）。
- **MySQL 的 upsert 语义不够精确，且已被本仓禁用。**
  <https://dev.mysql.com/doc/refman/8.4/en/insert-on-duplicate.html>（同日核验）明确「有多个
  唯一索引时应尽量避免 `ON DUPLICATE KEY UPDATE`」，因为只更新匹配到的其中一行。
- **MySQL 的 `GET_LOCK` 不是事务级的**：不在提交/回滚时释放，只有显式 `RELEASE_LOCK` 或会话
  结束才释放；名字上限 64 字符；服务端全局命名空间，任何客户端都能占用同名锁；死锁时报
  `ER_USER_LOCK_DEADLOCK` 且**不回滚事务**
  （<https://dev.mysql.com/doc/refman/8.4/en/locking-functions.html>，同日核验）。事务语义与
  `pg_advisory_xact_lock` 不对齐，因此本仓 `subset.py:72` 也把它列为禁止构造。

### 2.3 本仓已经冻结的替代口径

不必重新发明：共同子集的唯一定义点已经给出了两个合法替代——

```python
# packages/portable-sql/src/speechnexus_portable_sql/subset.py:71-72
(_rule("advisory-lock", r"\bPG_ADVISORY\w*", "单行 SELECT ... FOR UPDATE 或 bucket CAS"),)
(_rule("advisory-lock", r"\bGET_LOCK\s*\(", "单行 SELECT ... FOR UPDATE 或 bucket CAS"),)
```

以及 upsert 的替代（`subset.py:58-59`：「先 INSERT,失败回滚后按业务唯一键查询」）。
`docs/architecture/database-portability.md:103`（该行须含 `单行、显式事务内的`） 把「单行、显式事务内的 `SELECT ... FOR UPDATE`」
列入允许语句；`:326` 写「权威判断只来自条件写入的 `rowcount`、唯一约束和显式行锁」。

**因此本文不含新增 profile、不含新增 portable 类型。** 所有建议只用到：单行 `INSERT`、
单行 `SELECT … FOR UPDATE`、条件 `UPDATE` + `rowcount`、唯一约束——全部在允许清单内。

### 2.4 一处需要更正的既有结论

`docs/plans/2026-11-21-portable-sql-delivery.md:1429`（该行须含 `#1096 的「锁后探针」模式`） 记的是「受限方言里没有『可移植的跨事务
串行化原语』替代 `pg_advisory_xact_lock`」。这句话只在**不新增对象**的前提下成立。准确表述：

> 不新增表时，没有可移植的跨事务串行化原语；新增一张键锁表后，「单行 INSERT + 单行
> `SELECT … FOR UPDATE`」就是那个原语，且两个引擎都在允许清单内。

同一文档 `:1430` 也预判了这条路（「例如用 bucket 行锁 + 持久 grant」）。本文把它收敛成一个
具体决定：§5。

### 2.5 同一条铁律

PostgreSQL 文档对死锁的建议是「确保所有应用以一致的顺序在多对象上加锁」
（`explicit-locking.html` §13.3）。对本文更关键的是它的推论，也是这个项目已经踩过的那条：

> **一把逻辑锁不能有两个载体。** 参与者里只要有一方换了载体，锁就不再互斥，而且**不会报错**
> ——两边都正常提交，只是不再互相等待。

`docs/components/execution-gateway.md:101`（该行须含 `先取同一 job 的 canonical 行锁`） 与 `docs/components/execution-gateway.md:103`（该行须含 `网关角色只需要该行的`） 把同一条规则写成了契约：「先取同一 job 的互斥
事务级 advisory lock……backend 受理事务必须取同一键的同一把锁。这样受理与提交只能有一方先
提交。」`docs/components/synchronous-speech.md:20`（该行须含 `新准入与声音退役使用同一 voice catalog 事务锁`） 同样把
`binding → voice → resource pool → workspace quota → host → runtime instance head → instance →
runtime intent → operation/attempt/lease` 的固定顺序写成事实。

---

## 3. 逐站清单

分类维度是**这把锁是什么**，不是它在哪个文件。27 处分属 6 个键命名空间。

### 3.1 N1 — 目录发布身份屏障，16 处

键构造统一为（`publications_sql.py:56-58`）：

```sql
SELECT pg_advisory_xact_lock(hashtextextended(
'speechnexus:catalog:' || current_schema() || ':' || ? || ':' || ?, 0))
```

`?` 为 `(kind, object_id)`；binding 与 voice 两族在别处写死同一前缀
（`binding_disable_barrier.py:22-24`、`admission_postgres.py:205-207`），因此它们**是同一把锁**，
不是三把。这一节 16 个站点共享同一个键空间，必须整体迁移。

| # | 位置 | 键 | 串行化的对象 | 天然行是否存在 | 分类 |
| --- | --- | --- | --- | --- | --- |
| 1 | `publications_sql.py:56` | `(kind, object_id)` | 发布追加：查重 → head CAS → 插 `speech_publications` → 推进 head | 有：`speech_publication_heads`（`publication_lifecycle_schema.py:30-41`） | 状态互斥 |
| 2 | `publications_sql.py:236` | 同上 | 退役：读当前 → 插墓碑 → 删 head | 同上 | 状态互斥 |
| 3 | `publications_postgres.py:204` | 同上 | 绑定发布的外层校验（证据、容量清单、revision 文档） | 同上 | 状态互斥 |
| 4 | `binding_disable_barrier.py:22` | `binding:<binding_id>` | `lock_binding()`：disable 屏障，被 backend 多处与 gateway 共用 | 有 | 状态互斥（跨进程） |
| 5 | `builtin_voice_publication.py:144` | `binding:<binding_id>` | 内置声音发布前锁绑定，确认 head 三元组仍是该 revision | 有 | 状态互斥 |
| 6 | `builtin_voice_publication.py:149` | `voice:<voice_id>` | 发布 voice：在锁内复查撤销与 head | 有 | 状态互斥 |
| 7 | `builtin_voice_revocation.py:68` | `voice:<voice_id>` | 撤销：查已有撤销 → 插入 | 有（已发布的 voice） | 状态互斥（屏障的另一侧） |
| 8 | `builtin_voice_source_approval.py:130` | `voice:<voice_id>` | 来源批准插入，与撤销共锁 | **无**（voice 尚未发布） | 跨写者屏障 |
| 9 | `admission_postgres.py:205` | `voice:<voice_id>` | 新 TTS 准入 vs voice 发布/退役 | 有（准入要求已发布） | 状态互斥（读快照） |
| 10 | `qualification_admission_postgres.py:287` | `voice:<voice_id>` | 探针准入，先 `lock_binding(:285)` 再 voice | 有 | 状态互斥（读快照） |
| 11 | `qualification_probe_authorizer.py:136` | `binding:<binding_id>` | 探针授权签发前的绑定复核 | 有 | 状态互斥（读快照） |
| 12 | `qualification_probe_authorizer.py:141` | `voice:<voice_id>` | 同上，voice 侧 | **无**（授权先于发布） | 跨写者屏障 |
| 13 | `qualification_probe_approval.py:160` | `binding:<binding_id>` | 探针批准写入前的绑定复核 | 有 | 状态互斥（读快照） |
| 14 | `qualification_probe_approval.py:165` | `voice:<voice_id>` | 同上，voice 侧 | **无** | 跨写者屏障 |
| 15 | `operator_attested_qualification.py:180` | `voice:<voice_id>` | 操作员证明写入前，先 `lock_binding(:178)` 再 voice | **无** | 跨写者屏障 |
| 16 | `execution-gateway/src/speechnexus_gateway/infrastructure/binding_disable_guard.py:31` | `binding:<binding_id>` | 网关提交意图创建 vs 精确 disable；**跨进程的另一侧** | 有 | 状态互斥（跨进程） |

关键注释原文：

- `binding_disable_barrier.py:19` — `"""Hold until transaction end, before resource, operation or dispatch locks."""`
  （锁序里的第一位）
- `binding_disable_barrier.py:29` — `"""Call under lock_binding; a retired but not disabled revision remains usable."""`
  （`check_binding` 的读之所以有意义，全因为它在锁内）
- `builtin_voice_publication.py:226` — `# Recheck revocations after acquiring the same voice lock as revocation writers.`
- `builtin_voice_revocation.py:1` — `"""Explicit, append-only revocation using the admission voice barrier."""`
- `builtin_voice_source_approval.py:123` — `# ID lock makes cross-voice collisions serialize; revocation takes only voice.`
- `admission_postgres.py:209` — `# A duplicate admission can commit while the voice lock is awaited.`
- `qualification_admission_postgres.py:291` — `# Recheck single-use and the original after waiting for the common locks.`
- `publications_sql.py:233-234` — `# Publication and retirement paths take this same transaction-scoped lock, so the current-revision check cannot be split by a newer publication.`

**这一族不能拆。** 站点 8/12/14/15 的天然行不存在（voice 尚未发布），站点 1/2/3 的天然行是
head 行且站点 2 会 **删掉** 它。任何「有行就锁行、没行就锁表」的混合方案，都会让同一键上的
参与者用不同载体，直接落进 §2.5 的陷阱。

### 3.2 N2 — 生命周期幂等键，1 处

`binding_lifecycle_postgres.py:375-378`：

```sql
SELECT pg_advisory_xact_lock(hashtextextended(
'speechnexus:catalog:' || current_schema() || ':idempotency:' || ?, 0))
```

| # | 位置 | 键 | 串行化的对象 | 天然行 | 分类 |
| --- | --- | --- | --- | --- | --- |
| 17 | `binding_lifecycle_postgres.py:375` | `key_digest` | `_replay()`：等可能未提交的同键写者，再按摘要读回生命周期事件 | **有**：`speech_publication_lifecycle_events` 上已有 `CREATE UNIQUE INDEX … (idempotency_key_digest) WHERE … IS NOT NULL`（`publication_lifecycle_schema.py:93-95`） | 幂等键等待 |

### 3.3 N3 — 幂等键族，6 处

| # | 位置 | 键构造 | 串行化的对象 | 唯一约束 | 分类 |
| --- | --- | --- | --- | --- | --- |
| 18 | `publications_postgres.py:313` | `publish-intent:<schema>:<key_digest>` | 绑定发布意图重放 | `speech_binding_publish_intents.key_digest` PK（`0024_binding_publish_intents.py:23-24`） | 幂等键等待 |
| 19 | `builtin_voice_publication.py:65` | `builtin-voice-intent:<schema>:<key_digest>` | 内置声音发布意图重放 | `speech_builtin_voice_publish_intents.key_digest` PK（`0029_builtin_voice_qualification.py:62-64`） | 幂等键等待 |
| 20 | `workspaces_postgres.py:249` | `workspace:<schema>:idempotency:<key_digest>` | 工作区创建/更新重放 | `speech_workspace_commands.key_digest` PK（`workspace_schema.py:22`） | 幂等键等待 |
| 21 | `public_jobs_postgres.py:1297` | `public-job-key:<schema>:<json([ws,key])>` | 同键公开 Job 创建 + 拒绝协调 + **只读查询** | `speech_jobs UNIQUE (workspace_id, idempotency_key_digest)`（`public_job_schema.py:57`） | 幂等键等待（**读写双面**） |
| 22 | `public_jobs_postgres.py:1181` | `public-job-cancel-key:<schema>:<json([ws,key])>` | 取消回执的键级串行化 | `speech_job_cancellations UNIQUE (workspace_id, idempotency_key_digest)`（`public_job_cancellation_schema.py:29`） | 幂等键等待 |
| 23 | `voice_publications_postgres.py:180` | `voice-publication:<schema>:<draft_id>` | 声音发布：草稿状态、意图、证据 | 草稿行 `speech_voice_drafts.draft_id` PK（`voice_draft_schema.py:15`），且**下一句就 `FOR UPDATE`**（`:184`） | 状态互斥（已有行锁） |

注释原文：

- `workspaces_postgres.py:246-247` — `# The lock bucket only orders equal keys; the complete digest is the PK, so a bucket collision adds contention and can never alias a replay.`
- `public_jobs_postgres.py:1293-1295` — `# Serialize all keyed public creates and rejection reconciliation, including keyless-binding requests. Never acquire this lock under a catalogue lock. Canonical tuple encoding prevents ambiguous workspace/key concatenation.`
- `public_jobs_postgres.py:1178-1179` — `# Cancellation keys are scoped by workspace and never share the create namespace. Acquire this lock before any receipt row write.`

### 3.4 N4 — TTS 取消/准入屏障，2 处

`tts_settlement_sql.py:48-50` 与 `tts_settlement_postgres.py:70-72`，键同为
`tts-cancel:<schema>:<operation_id>`：

| # | 位置 | 串行化的对象 | 天然行 | 分类 |
| --- | --- | --- | --- | --- |
| 24 | `tts_settlement_sql.py:48` | 准入：读取消原因 → 插结算根并把原因**快照**进 `abort_reason` | **无**（无取消时行不存在） | 取消-准入屏障 |
| 25 | `tts_settlement_postgres.py:70` | 取消：插取消行 → 回填未结算根的 `abort_reason` | 同上 | 取消-准入屏障（另一侧） |

`tts_settlement_postgres.py:61` — `"""Persist the original operation even when admission has not committed yet."""`

这两处必须成对看，因为 `due()`（`tts_settlement_postgres.py:34-43`）**只从结算行读
`abort_reason`**，从不回读 `speech_tts_cancellation`。原因一旦没在插入时快照进去，后面没有任何
补救路径。

### 3.5 N5 — 来源批准身份，1 处

| # | 位置 | 键 | 串行化的对象 | 天然行 | 分类 |
| --- | --- | --- | --- | --- | --- |
| 26 | `builtin_voice_source_approval.py:125` | `source-approval:<schema>:<source_approval_id>` | 同一 `source_approval_id` 的重复/冲突写入；并固定「ID 锁 → voice 锁」的次序 | 有：`speech_builtin_voice_source_approvals.source_approval_id`（主键） | 幂等键等待 |

### 3.6 N6 — 迁移互斥，1 处

| # | 位置 | 键 | 串行化的对象 | 天然行 | 分类 |
| --- | --- | --- | --- | --- | --- |
| 27 | `migrations/env.py:21` | `migrations:<schema>` | 同一 schema 上并发跑 alembic | 无（但只在 PG 上运行） | 部署期互斥 |

`migrations/env.py:1` — `"""Alembic entry point with a database-scoped, transaction-held migration mutex."""`

---

## 4. 按类推荐

判据（按优先级，前一条满足就不看后一条）：

1. **参与者能否保证同一行存在。** 能 → 天然行锁；不能 → 不许用天然行。
2. **被保护的动作是不是「插入这个键的那一行」。** 是 → 唯一约束 + insert-first，锁该删。
3. **见证者是否需要「读到空」这件事本身可信。** 需要 → 必须有锁，唯一约束帮不上。
4. **以上都不成立 → 键锁表。**

### 4.1 N1（16 处）→ 键锁表

**不能用天然行。** `speech_publication_heads` 是唯一的候选行，但两类参与者拿不到它：

- 站点 8、12、14、15 在 voice **发布之前**运行（来源批准、探针授权、操作员证明），此时 head 行
  不存在；
- 站点 2 的语义就是**删除** head 行（`publications_sql.py:269-278`）。

`SELECT … FOR UPDATE` 在行不存在时两引擎都**不锁任何东西**（PG 返回空集；InnoDB 在
`READ COMMITTED` 下不取间隙锁，见 §2.2），所以「有行锁行」在这里不是「弱一点」，是**静默失效**。

**也不能一分为二。** 把「发布态」放 head 行、「批准态」放 `speech_builtin_voice_approvals` 行，
会让发布路径与撤销路径不再互相等待——正是 `builtin_voice_publication.py:226` 那句
「Recheck revocations after acquiring the same voice lock as revocation writers」所依赖的东西。

**结论：16 处整体迁到一张键锁表，键的语义（`<kind>:<object_id>`，含 `current_schema()`）与持有
时长（事务级）逐位保持不变。** 这是一次替换而不是十六次改造：迁移点是「键构造 + 一条
`SELECT pg_advisory_xact_lock(...)`」这一对形态，16 处完全同构。

表与助手的最小形状（属部署期 DDL，非运行时 DDL，符合 `subset.py:117-125`）：

```
coordination_lock_keys(
    lock_key    VARCHAR(512) 主键      -- 'speechnexus:catalog:<schema>:binding:<id>'
    locked_at_us BIGINT      非空      -- 回收策略的唯一依据
)
```

拿锁两步，两步都在允许清单内：

1. `INSERT INTO coordination_lock_keys (lock_key, locked_at_us) VALUES (?, ?)`，用
   `insert_if_absent()` 包（`transaction.py:129`），已存在则返回 False，事务保持可用；
2. `SELECT lock_key FROM coordination_lock_keys WHERE lock_key = ? FOR UPDATE`。

授权：每个参与角色需要 `SELECT, INSERT`；`DELETE` 只给回收者（§5.3 的代价项）。

### 4.2 N2（1 处）、N3 写侧（5 处：18/19/20/22/23）、N5（1 处）→ 删除

共 7 处，判据 2 全部满足：

| 站点 | 现在为什么需要锁 | 删掉之后谁保证 |
| --- | --- | --- |
| N2 `binding_lifecycle:375` | 等可能未提交的同键写者 | 唯一索引 `publication_lifecycle_schema.py:93-95` + insert-first |
| 18 `publications_postgres:313` | 意图重放 | `key_digest` PK |
| 19 `builtin_voice_publication:65` | 意图重放 | `key_digest` PK |
| 20 `workspaces:249` | 命令重放 | `key_digest` PK |
| 22 `public_jobs:1181` | 取消回执的键级唯一性 | `UNIQUE (workspace_id, idempotency_key_digest)` |
| 23 `voice_publications:180` | **与下一句重复** | `:184` 的 `SELECT … FROM speech_voice_drafts WHERE draft_id=? FOR UPDATE` |
| 26 `builtin_voice_source_approval:125` | 同 ID 重复写入 | `source_approval_id` 主键 |

统一改法（与本仓 `insert_if_absent` 的既有用法一致，例如 `tts_settlement_sql.py:69-74`）：
先 `INSERT`，`False` 表示别人先到，再按业务键查回原行并比摘要；摘要不同 → 现有的
`idempotency_conflict` / `intent_conflict` 错误码。**不需要新错误码，也不需要改调用方。**

站点 23 是 7 处里唯一零风险的：锁的下一句已经把同一行锁住了，删掉它连行为都不变。

站点 26 有一处顺序要求：删除 ID 锁后，voice 锁（`:130`）仍在，且必须是插入之后的**第一个**锁；
`builtin_voice_source_approval.py:177-179` 的「INSERT 可能等锁，锁后要重新判过期」逻辑不变。

N2 需要一条**独立的部署期 DDL 变更**，不属于锁迁移本身：`publication_lifecycle_schema.py:93`
的唯一索引带 `WHERE idempotency_key_digest IS NOT NULL`，MySQL 没有部分索引。改成列上的普通唯一
索引即可（两引擎的唯一索引都允许多个 NULL），语义等价。

### 4.3 N3 站点 21（`public-job-key`）→ 键锁表

判据 3 命中：**它有一个只读消费者**。`find_by_key`（`public_jobs_postgres.py:550-564`）经由
`_read(submission_key=…)` 在 `SET TRANSACTION READ ONLY`（`:1079`）里取同一把锁，目的是让
「刚创建、正在提交」的 Job 不被读成不存在。只读事务无法先插一行，唯一约束在这条路径上是空的。

**并且不能只删一半。** 删掉 `:651/:787` 而保留 `:1081`，读侧那把锁就再没有对手，等于一个看着
像屏障、实际不设防的锁——这正是本项目已经踩过的那类事故（§2.5）。要么两侧都留（迁入键锁表），
要么两侧都删（并明确接受「创建提交期间查询可能返回 404」）。本文取前者：`201` 之后立刻
`GET` 拿到 404 不是可接受的客户端语义。

### 4.4 N4（2 处）→ 键锁表

判据 1 与 2 都不满足：`speech_tts_cancellation` 行在没有取消时不存在，而动作不是「插入这个键
的那一行」（两侧插的是不同的表）。判据 3 命中：`due()` 只读结算行的 `abort_reason`。

**记录一个被否决的更优方案**：把 `due()` 改成
`COALESCE(settlement.abort_reason, cancellation.reason)`（`operation_id` 在结算表里是列，
`0031_tts_settlement.py:20`），原因在读取时派生，两把锁一起消失，且这类「原因丢失」
的 bug 整类消失。否决理由：结算根的职责就是「准入当时看到了什么」的不可变快照；改成派生值会
让它随取消表变化，破坏该表的证据语义。这一条是设计取舍，不是实现细节，故不并入本次迁移。

### 4.5 N6（1 处）→ 保留

`migrations/env.py:21` 不在可移植范围内（§1.2），且 `env.py:17-18` 本身拒绝非 PostgreSQL 连接。
不改。

---

## 5. 必须保留锁的站点

这一节的共同结构是：**锁的一侧换载体、另一侧不换，两边都会正常提交，只是不再互相等待。**

### 5.1 N1 的 16 处全部保留（载体换成键锁表行）

具体到会坏什么：

- **跨进程。** `execution-gateway/src/speechnexus_gateway/infrastructure/binding_disable_guard.py:31` 是网关进程里唯一的那一侧，
  其后 `check_binding`（`:38-48`）读退役记录、再决定是否写提交意图。backend 的
  `binding_disable_barrier.py:22` 是另一侧。删掉任一侧，`docs/components/execution-gateway.md:103`（该行须含 `网关角色只需要该行的`）
  的「受理与提交只能有一方先提交」不再成立，而**没有任何测试会红**——上一条同类判断在
  `docs/reviews/2026-11-13-quarantine-release.md:198`（该行须含 `锁序反转`） 已被实测证伪过一次：把锁序反转，42 个
  用例全绿。
- **发布 vs 撤销。** `builtin_voice_publication.py:226-240` 在拿到 voice 锁后重查撤销；
  `builtin_voice_revocation.py:68` 是那个「另一个写者」。删掉撤销侧，重查看不到正在提交的撤销，
  发布可以在被撤销的批准上提交；删掉发布侧，撤销的回填顺序失去保证。两侧同一把锁是这个重查
  唯一的意义来源。
- **准入 vs 退役。** `admission_postgres.py:205-217` 在锁后**故意重读一次原身份**
  （`:209` 的注释）。删掉锁，这次重读退化成一次普通查询，重复准入可以在等待期间提交并被漏掉。
- **锁序。** `binding → voice`（`builtin_voice_source_approval.py:124-133`、
  `operator_attested_qualification.py:178-183`、`qualification_admission_postgres.py:285-290`）
  必须整体保留；换载体时顺序不变，只换执行语句。

### 5.2 N4 的 2 处保留

两侧插不同的表，行不存在时没有可锁对象。删掉任一侧的具体后果可由代码读出：
`due()` 只读 `speech_tts_settlement.abort_reason`（`tts_settlement_postgres.py:34-43`），
所以「取消先提交、准入后插根」这一交错下，结算根会永久保留 `abort_reason = NULL`，该 TTS 不会
因为这次取消被中止，且没有任何后续路径会修正它。

### 5.3 N3 站点 21 保留

见 §4.3：读侧是只读事务。

### 5.4 N6 保留

见 §4.5。

### 5.5 键锁表的代价，必须一并接受

| 代价 | 具体量 |
| --- | --- |
| 每键一行、无界增长 | `tts-cancel:<operation_id>` 每个 TTS 操作一行；`catalog:…` 每个 binding/voice 身份一行。需要按 `locked_at_us` 的回收策略 |
| 多一个写入授权 | 每个参与角色要 `INSERT`（当前它们只有既有业务表的列级授权） |
| 多一次往返 | 拿锁从 1 条语句变 2 条 |
| 表膨胀与 vacuum | PG 文档明确指出 advisory lock「avoid table bloat」；这是为可移植性付出的明确对价 |
| 键长度 | 键含 `current_schema()`，`VARCHAR(512)` 足够；MySQL 的 `GET_LOCK` 64 字符上限与本方案无关 |

回收是硬要求，不是可选项：`DELETE` 只给回收者，且必须在锁表里保留「行还在但没人持锁」这一
合法状态（`SELECT … FOR UPDATE` 的语义只依赖行存在，不依赖 `locked_at_us`）。

---

## 6. 按 ROI 排序

排序依据：删掉的锁数 ÷ 需要新引入的东西。前四步不新增任何表。

### 6.1 前五步

| 序 | 动作 | 站点数 | 为什么排这里 |
| --- | ---: | ---: | --- |
| 1 | 删 `voice_publications_postgres.py:180` | 1 | 下一句已经 `FOR UPDATE` 同一行（`:184`）；零行为变化，零新增对象 |
| 2 | 删 N3 写侧五处（18/19/20/22/23） | 5 | 唯一键全部已存在并逐条核过；一套改法（insert-first + 读回）覆盖 5 处 |
| 3 | 删 N2 与 N5（2 处） | 2 | N5 靠主键；N2 靠已有唯一索引，只需把部分索引改成普通唯一索引 |
| 4 | N1 十六处迁入键锁表 | 16 | 一次建表 + 一个助手 + 16 处同构替换；这是「不逐一迁移」的兑现点 |
| 5 | N4 两处 + N3 站点 21 迁入同一张表 | 3 | 复用第 4 步的表与助手，边际成本接近零 |

### 6.2 可删除（7 处，最高 ROI）

`voice_publications_postgres.py:180`、`publications_postgres.py:313`、
`builtin_voice_publication.py:65`、`workspaces_postgres.py:249`、`public_jobs_postgres.py:1181`、
`binding_lifecycle_postgres.py:375`、`builtin_voice_source_approval.py:125`。

### 6.3 迁入键锁表（19 处）

N1 的 16 处 + `public_jobs_postgres.py:1297` + `tts_settlement_sql.py:48` +
`tts_settlement_postgres.py:70`。

### 6.4 不动（1 处 + 6 处域外）

`migrations/env.py:21`：按 `check-portable-sql.py:78-83` 的明文规则不在运行时子集内。

整数键族 6 处（§1.2）不在本文的 27 之内，但它们与 N4 同构（跨进程、无天然行），且
`job_cancellation_fence_lock_key()` 的整数键已在
`packages/contracts/src/speechnexus_contracts/cancellation_fence.py` 冻结成跨进程契约。同一张
键锁表可以承载它们（键写成 `speechnexus:job-cancel-fence:<schema>:<int>`），但那要同时改
`docs/components/execution-gateway.md:101`（该行须含 `先取同一 job 的 canonical 行锁`） 描述的冻结键与两侧进程，属独立决定。做完 §6.1
之后 `advisory-lock` 基线 32 → 6，剩下的正是这 6 条。

### 6.5 一句范围提醒

不建议按文件顺序迁移。N1 的 16 处若分两批合入，中间那一版里同一把锁会短暂存在两个载体——
参与者不互相等待，且不报错。**N1 必须单个 PR 内整体切换**；其余各类相互独立，可分批。

---

## 7. 未能核实的项

以下结论依赖数据库行为或本机无法取得的运行态，本文只做到静态读码，**没有起库实测**：

1. **InnoDB 对「未提交事务已删除的行」执行 `SELECT … FOR UPDATE` 的精确行为**（期望：阻塞到
   对方提交，然后返回空集）。这决定 §4.1 里「head 行被删时屏障是否仍然成立」的推理。PG 侧由
   `explicit-locking.html` §13.3 的「SELECT FOR UPDATE … or no row, if the row was deleted」
   支持；MySQL 侧未在文档中找到同句表述，也未实测。
2. **键锁表在 InnoDB `REPEATABLE READ` 下 `INSERT` 与 `SELECT … FOR UPDATE` 的间隙/插入意向锁
   是否会在高并发同键下互锁。** 文档只说 `READ COMMITTED` 下间隙锁关闭；本仓连接隔离级别未在
   本文核对。
3. **N1 的键里含 `current_schema()`**：若两个参与者以不同 `search_path` 连接同一库，键会不同而
   锁失效。部署 DSN 与角色默认 schema 未逐一核对。这是现值就存在的风险，不是迁移引入的。
4. **各参与角色是否都已具备键锁表的 `INSERT` 权限**：`claim_bucket_schema.py:5-9` 显示本仓倾向于
   列级精确授权，实际授权清单（`builtin_voice_grants.py`、`coordination_grants.py`、
   `admission_grants.py`、`gateway_query_grants.py`）未逐个核对到「谁拿得到 INSERT」。
5. **N1 各键的实际基数**（多少 binding、多少 voice）。只从管理面推断为「数十」，未统计。
6. **`tts-cancel` 键的增长率**：每个 TTS 操作一行，实际 QPS 与保留窗口未取数，回收策略的
   `locked_at_us` 阈值只能定为「由后续切片实测后确定」。
7. **删除 7 处锁之后，是否仍有测试依赖它们的阻塞行为**：`tests/integration/test_tts_durable_pg.py:189`、
   `tests/integration/test_builtin_voice_qualification.py:239`、`test_binding_publication_cli.py:459` 等直接内联了
   advisory lock 语句，迁移后需要同步改写；逐条影响未评估。
8. **任务下达时提到的「此前曾几乎删掉一把跨进程锁的一侧并静默破坏排序保证」的具体事故记录**：
   在 `docs/reviews/` 与 `docs/plans/` 中未检索到该事故的书面复盘。本文引用的机制证据是当前代码
   注释（§3.1）与两份组件文档（§2.5），不是那份复盘。

## 8. 一句话结论

7 处删除（唯一键已就位），19 处迁到一张键锁表（一把逻辑锁、一个载体、一次切换），1 处按既有
规则不动；N1 的 16 处必须在同一个 PR 内整体切换，因为它是 backend 与 gateway 共享的一把锁，
只改一侧不会报错，只会不再互斥。

---

## 附录：本切片落地时的核对结果（2026-11-24）

本文正文是 2026-11-24 早间的判读。落地时逐条核对代码，以下八条与当时的树已经不符，按代码
更正；正文其余结论（N1 必须整体迁、N4 必须保留、站点 21 的只读消费者）在当次核对中仍成立。

1. **`keyed_lock` 模块已存在。** §1.3（一）说
   `packages/portable-sql/src/speechnexus_portable_sql/keyed_lock.py` 不存在；现在它存在，
   并且 `coordination_keyed_locks` 已经建表（`0067`）。§4.1 提出的键锁表不是待建方案，
   它已经落地，绑定禁用栅栏两侧也已同迁。
2. **站点 21 与站点 22 已删除。** `public_jobs_postgres` 里两处 `pg_advisory_xact_lock` 已经
   不在：`#1193` 把提交与取消的键锁交给唯一约束裁决，站点 21 的只读消费者改由写者本就持有的
   binding 锁兜住。§4.3「站点 21 保留」与 §6.3「迁移 19 处」因此比现树多算了两处。
3. **整数键族 6 处已不是 6 处。** `job_cancellation_fence_lock_key()` 已不存在，
   `cancellation_fence.py` 现在只导出表名并说明两侧在 canonical Job 行上排序；§1.2 与 §6.4
   里「整数键 6 条、基线做完全部之后剩 6 条」的前提已经失效。
4. **§6.4 的行号引用已越界。** 它把冻结键指向那个文件的第 21 行，而该文件现在只有 17 行。
5. **站点 22 的编号位置已变。** 取消回执的键锁删除后，`public_jobs_postgres.py:1181` 附近是
   取消目标的行锁，不再是键锁。
6. **基线数字已变。** §1.2 记 `advisory-lock` 32 条、总基线 196；本切片起算时是 148，落地后
   是 146（见 `scripts/dev-env/portable-sql-baseline.json`）。
7. **七处里只有两处在这一片被删。** 「删掉之后仍有保证」的判据是：把兜底的那个唯一约束去掉，
   现有用例必须变红。按此判据，只有两处成立：

   - **站点 23**（`voice_publications_postgres.py`）：摘掉 `_SELECT_DRAFT` 后面的
     `FOR UPDATE`，`tests/integration/test_voice_publications_pg.py` 的
     `test_concurrent_publication_has_one_winner` 由 `[200, 201]` 变成 `[(201, ''), (409,
     'VoicePublicationUnavailableError')]`，红。
   - **站点 20**（`workspaces_postgres.py`）：把 `speech_workspace_commands.key_digest` 的
     `PRIMARY KEY` 降级为 `NOT NULL`，`tests/integration/test_workspace_writes.py` 新增的
     `test_two_concurrent_same_key_creates_overlap_and_the_key_decides` 得到两个身份，红。
     该文件原有的 `test_two_concurrent_same_key_requests_serialise_to_one_identity` 挡不住这一
     注入：两个写者各用不同 workspace_id 时，兜底的是 `speech_workspaces` 的主键，且在安静机器
     上第一个写者先提交，所谓并发只是先后。

   其余四处**没有删**，因为去掉各自依赖的唯一约束后，现有用例全绿：

   - **站点 18**（`publications_postgres.py`，依赖 `speech_binding_publish_intents.key_digest`
     主键）：`tests/integration/test_binding_publication_cli.py` 整个模块在去掉主键后仍 20 条
     全过。缺的用例是「两个不同 binding、同一个键、事务真重叠」。
   - **站点 19**（`builtin_voice_publication.py`，依赖
     `speech_builtin_voice_publish_intents.key_digest` 主键）：
     `tests/integration/test_builtin_voice_qualification.py` 在去掉主键后仍 2 条全过。缺的
     用例是「两个不同 voice、同一个键、事务真重叠」。
   - **站点 N2**（`binding_lifecycle_postgres.py`，依赖
     `speech_publication_lifecycle_events.idempotency_key_digest` 唯一索引）：把该索引改成非
     唯一（`0068` 的 `CREATE UNIQUE INDEX` 降级）后，
     `tests/integration/test_binding_lifecycle_pg.py` 与
     `tests/integration/test_binding_lifecycle_concurrency_pg.py` 仍 23 条全过。同 binding
     的并发写者本来就被 `lock_binding` 串行化，`_replay` 看到的是已提交事件，唯一索引只对
     「跨 binding 复用同一个键」是承重的，而现有用例没有重叠的跨 binding 场景。相应地 `0068`
     迁移与把偏索引改成普通唯一索引的 DDL 变更在这一片**没有落地**。
   - **站点 26**（`builtin_voice_source_approval.py`，依赖
     `speech_builtin_voice_source_approvals.source_approval_id` 主键）：去掉主键（并摘掉
     `speech_builtin_voice_source_revocations` 上那条依赖它的外键）后，
     `tests/integration/test_builtin_voice_source_approval_pg.py` 仍全过。缺的用例是「两个不同
     voice、同一个 source_approval_id、事务真重叠」。

   这四处的咨询锁与顺序要求都保持原样：站点 26 的 voice 锁仍是第一条锁，
   `builtin_voice_source_approval.py:177` 起的「INSERT 可能等锁、锁后重判过期」逻辑未动。
8. **三处被引用的行文本已经变了。** §2.4 引的
   `docs/plans/2026-11-21-portable-sql-delivery.md:1429`（该行须含 `#1096 的「锁后探针」模式`）
   现在说的是锁后探针，不再是「没有可移植的跨事务串行化原语」；§2.5 与 §5.1 引的执行网关
   契约现在写的是「先取同一 job 的 canonical 行锁」（`coordination_jobs.job_id`），不再是
   advisory lock。上面七处引用按**当前**行文本加了断言，所以行号漂移会红；但正文引用的
   那段原文只有第 8 条里的三处不再是当前事实。
