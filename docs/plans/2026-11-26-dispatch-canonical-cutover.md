---
title: 派发认领改接 canonical：delta、锁序与分阶段切换
status: active
version: "1.0.0"
date: 2026-11-26
scope: "#1102 / 2026-11-21-portable-sql-delivery.md §8.75 §8.76 §8.77"
---

# 派发认领改接 canonical：delta、锁序与分阶段切换

本文只裁决一件事：**派发链路上还剩哪些列、哪些语句、哪些事实仍在草案形状上，按什么顺序切到
`migrations/0003_portable_claim_buckets.sql` 定义的 canonical 形状，以及每一步怎么被证伪。**
不含代码改动，不含新 portable 类型，不含新 profile 条目。

## 0. 结论

**前提修正（先说，因为它改变了整份计划的形状）。** 交付计划 §8.75 把剩余工作写成
「`claims` 的语句全落在 `0003` 的 canonical 表上，而生产派发仍在草案表的 epoch 微秒列上」。
这句话在本树**前半句成立、后半句不成立**：

- 认领路径已经是 canonical。`PostgresDispatchQueue._claim` 调 `claim_batch`
  （`backend/src/speechnexus_backend/speech/infrastructure/job_dispatch_postgres.py:314-323`），
  桶租约、`job_state='claimed'` CAS、`claim_expires_at` 全在
  `packages/portable-sql/src/speechnexus_portable_sql/claims.py` 的 17 条语句里。
- 该文件对 `claim_expires_at_us` 的**可执行引用为 0**（唯一一处是模块 docstring
  `job_dispatch_postgres.py:8` 的说明文字）：

  ```bash
  grep -n "claim_expires_at_us" backend/src/speechnexus_backend/speech/infrastructure/job_dispatch_postgres.py
  # 8:``claim_expires_at``; the draft ``claim_expires_at_us`` epoch column is no longer written
  ```

- 仍在 epoch 列上的只剩**第二认领口** `claims_sql.py`（2 条语句，3 行引用），而它**没有生产装配**：
  `ClaimService` / `ClaimTransactionBoundary` 只被 `tests/unit/test_production_claims.py` 与
  `tests/integration/test_production_claims.py` 引用，`backend/src` 内无路由、无装配点：

  ```bash
  grep -rn "ClaimService\|ClaimTransactionBoundary" --include=*.py . | grep -v "\.venv"
  # 只有 application/claims.py、interfaces/claim_transactions.py 两个定义文件 + 两个测试文件
  ```

**所以 #1102 剩下的不是「把 `_claim()` 换过去」，而是三件事**：事件侧切 canonical、收敛第二认领口、
退役草案列。决策如下。

| 决策 | 内容 | 依据 |
| --- | --- | --- |
| **D1** | 事件侧（`job_events` / `coordination_outbox.delivered_at`）落到 alembic 树之前，**不做任何事件读翻转**。canonical 事件对象在生产 schema 里根本不存在（§7-B1），先翻转等于把 SSE 与 outbox 投递同时打成 42P01 | `migrations/0003:38-54` 有表；`backend/src/.../migrations/versions/` 全树 0 处 `CREATE TABLE job_events`、0 处 `delivered_at`（不含 `_us`） |
| **D2** | 事件侧走「先双写 → 再翻读 → 最后删列」，删除是**单向**步骤，且必须在翻读后经过一个观察窗 | 草案事件表带 DB 侧不变量（触发器），canonical 侧没有（§3.4） |
| **D3** | 第二认领口**不迁移，直接收敛**：它没有生产写入者，为死代码再实现一遍 canonical 语义等于制造第二个定义点 | §0 第三条命令 |
| **D4** | `claim_expires_at` 保持可空。`NULL` 就是「从未认领」的语义，不是缺失；不得为「和草案列形状一致」而给它 `NOT NULL DEFAULT` | 实测 A（§3.1） |

**不做的事**：不新增 manifest 语句（§8.76.2 的四象限已在 `packages/portable-sql/src/speechnexus_portable_sql/claims.py:438-443` 落地）、不引入新的
portable 类型、不改 `BUCKETS`（256）、不改 `CLAIM_ROUND_START_BUCKET`（固定 0，理由是桶行锁跨整个
轮询导致交错起点必然死锁，`job_dispatch_postgres.py:74-85`）。

---

## 1. 今天的派发链路，逐语句

一次 `claim_due()` 是一笔事务，由 `pool.connection()` 的上下文管理器在成功退出时提交
（`psycopg_pool 3.3.3`：池的 `with conn:` 包裹，成功退出即提交；同时把每条连接的会话时区钉成 UTC，
`backend/src/speechnexus_backend/speech/infrastructure/postgres_pool.py:36`）。语句按发生顺序：

| # | 语句 | 表 / 列 | 出处 |
| --- | --- | --- | --- |
| 1 | `SELECT set_config('statement_timeout', ?, true)` | 会话 | `job_dispatch_postgres.py:306-309` |
| 2 | `SELECT clock_timestamp() AS now` | 会话时钟 → `BIGINT` 微秒 | `clock.py:81-91`，调用点 `job_dispatch_postgres.py:312` |
| 3 | `SELECT version, claim_owner_id, lease_until FROM coordination_claim_buckets WHERE bucket_no = ? FOR UPDATE` | canonical 桶行 | `packages/portable-sql/src/speechnexus_portable_sql/claims.py:106-109` ← `claim_batch:719` |
| 4 | `UPDATE coordination_claim_buckets SET claim_owner_id = ?, lease_until = ?, version = ? WHERE bucket_no = ? AND version = ? AND (claim_owner_id IS NULL OR lease_until <= ?)` | 桶租约 CAS | `packages/portable-sql/src/speechnexus_portable_sql/claims.py:100-105` ← `:729-736` |
| 5 | 同一句 `bucket.fence` 再读一次，`require_fence` 复核 owner / 未过期 / 版本 | 桶行不变量断言 | `packages/portable-sql/src/speechnexus_portable_sql/claims.py:743-748`、`require_fence:417-434` |
| 6 | `UPDATE coordination_jobs SET job_state = ?, claim_owner_id = ?, claim_version = claim_version + 1 WHERE bucket_no = ? AND job_state = ? AND claim_expires_at <= ?` | 桶内死租约重领 | `packages/portable-sql/src/speechnexus_portable_sql/claims.py:150-154` ← `:796` |
| 7 | `SELECT c.job_id, c.claim_version, c.due_at FROM coordination_jobs AS c JOIN speech_jobs AS j ON j.job_id = c.job_id WHERE c.bucket_no = ? AND c.job_state = ? AND c.due_at <= ? AND (c.due_at > ? OR (c.due_at = ? AND c.job_id > ?)) AND j.kind IN (?) AND EXISTS (…admission…) ORDER BY c.due_at, c.job_id` | canonical + 草案 `speech_jobs.kind`（lane）+ 准入锚点 | `packages/portable-sql/src/speechnexus_portable_sql/claims.py:130-138` ← `:800-809` |
| 8 | `UPDATE coordination_jobs SET job_state = ?, claim_owner_id = ?, claim_expires_at = ?, claim_version = ? WHERE job_id = ? AND claim_version = ? AND job_state = ?` | Job CAS（每条候选一次） | `packages/portable-sql/src/speechnexus_portable_sql/claims.py:146-149` ← `:813-819` |
| 9 | `UPDATE coordination_claim_buckets SET claim_owner_id = ?, lease_until = ?, version = ? WHERE bucket_no = ? AND version = ? AND claim_owner_id = ?` | 归还桶（`finally`） | `packages/portable-sql/src/speechnexus_portable_sql/claims.py:110-113` ← `:762-769` |
| 10 | `SELECT j.job_id, j.workspace_id, j.kind, j.state, j.status_version, j.created_at_us, COALESCE(a.request_digest, j.request_digest), a.request_document FROM speech_jobs AS j LEFT JOIN admission_operations AS a … WHERE j.job_id IN (?) ORDER BY j.job_id` | 认领结果的 Job 投影 | `packages/portable-sql/src/speechnexus_portable_sql/claims.py:139-145` ← `job_dispatch_postgres.py:324-327` |

第 3–9 步对**整圈 256 个桶**重复，直到 `limit` 满足或扫满。生产档位是
`lane + admission` 同时为真（`require_admission=True`、`kinds` 非空，
`job_dispatch_runtime.py:412-421`），即只走 `job.candidates.lane_admitted` 一条候选语句。

认领之后 worker 侧还有四条语句（同一个 `PostgresDispatchStateStore`）：

| 方法 | 语句 | 出处 |
| --- | --- | --- |
| `_mark_running` | `UPDATE speech_jobs SET state='running', status_version = status_version + 1, updated_at_us = ? WHERE job_id = ? AND status_version = ? AND state = 'pending'`，再 `INSERT INTO speech_job_events (job_id, event_seq, state, occurred_at_us) VALUES (?, ?, ?, ?)` | `:424-434`，序号来自 `SELECT COALESCE(MAX(event_seq), -1) + 1`（`:156-163`） |
| `_renew` | `UPDATE coordination_jobs SET claim_expires_at = ? WHERE job_id = ? AND claim_owner_id = ? AND claim_version = ? AND job_state = ?` | `:463-474` |
| `_record_progress` | `SELECT job_id FROM coordination_jobs WHERE … claim_expires_at > ? FOR UPDATE` → `SELECT j.state, s.progress_unit FROM speech_jobs j JOIN speech_job_submissions s … FOR UPDATE OF j` → `SELECT COALESCE(MAX(event_seq),-1), MAX(CASE WHEN kind='progress' …)` → `INSERT INTO speech_job_events (… kind, progress_unit, progress_completed, progress_total)` | `:503-546` |
| `_finish` | canonical 守卫 `SELECT … FOR UPDATE` → `UPDATE speech_jobs SET state = ? …` → `INSERT INTO speech_job_events` → `UPDATE coordination_jobs SET job_state = ? WHERE … claim_version = ? AND claim_expires_at > ?` | `:606-651` |

**判读：** 租约（谁拥有、到什么时候）已经在 canonical 列上；**事实**（这一轮发生了什么）仍只在草案
`speech_job_events`。`coordination_jobs.last_event_seq`、`job_events`、`coordination_outbox` 在生产
派发里**没有任何写入者**。这正是 #1102 的剩余范围。

---

## 2. canonical 路径期待什么

### 2.1 语句清单

`claims.STATEMENTS` 共 **17** 条（命令见 §8），分五组：

| 组 | 语句 id | 作用 | 出处 |
| --- | --- | --- | --- |
| 桶（3） | `bucket.acquire` / `bucket.fence` / `bucket.release` | 租约 CAS、`FOR UPDATE` 复核、归还 | `packages/portable-sql/src/speechnexus_portable_sql/claims.py:100-113` |
| 候选（4） | `job.candidates` / `.lane` / `.admitted` / `.lane_admitted` | lane 与准入锚点是两个独立开关，四象限各一条 | `packages/portable-sql/src/speechnexus_portable_sql/claims.py:114-138`、选择点 `438-443` |
| Job（3） | `job.claimed_view` / `job.claim` / `job.reclaim` | 结果投影、状态+版本 CAS、死租约重领 | `packages/portable-sql/src/speechnexus_portable_sql/claims.py:139-154` |
| 事件（4） | `job.event_head` / `job.event_head_update` / `job_event.insert` / `job_event.read` | 事件头 CAS、事件行、续读 | `packages/portable-sql/src/speechnexus_portable_sql/claims.py:155-168` |
| outbox（3） | `outbox.insert` / `outbox.undelivered` / `outbox.mark_delivered` | 投递与去重 | `packages/portable-sql/src/speechnexus_portable_sql/claims.py:169-180` |

派发只用到其中 7 条：`bucket.fence`、`bucket.acquire`、`bucket.release`、`job.reclaim`、
`job.candidates.lane_admitted`、`job.claim`、`job.claimed_view`。

### 2.2 必需列

| 表 | 必需列（canonical） | 出处 |
| --- | --- | --- |
| `coordination_jobs` | `job_id`、`operation_id`、`attempt_id`、`request_digest`、`bucket_no NOT NULL`、`due_at NOT NULL`、`job_state`、`claim_owner_id`（可空）、`claim_expires_at`（可空）、`claim_version NOT NULL`、`last_event_seq NOT NULL`；`UNIQUE (operation_id, attempt_id)` | `migrations/0003:14-28` |
| `coordination_claim_buckets` | `bucket_no`、`claim_owner_id`、`lease_until`、`version NOT NULL` | `migrations/0003:30-36` |
| `job_events` | `job_id`、`event_seq`、`event_digest`、`payload_ref`（可空）；`PK (job_id, event_seq)`；`FK → coordination_jobs` | `migrations/0003:38-45` |
| `coordination_outbox` | `event_id`、`job_id`、`event_digest`、`delivered_at`（可空）；`FK → coordination_jobs` | `migrations/0003:47-54` |

### 2.3 `bucket_no` 与 `due_at` 的契约

- **`bucket_no` 是 `job_id` 的纯函数**：`sha256(job_id)` 前 8 字节大端 mod `BUCKETS`（256），
  `Decimal` 返回（`packages/portable-sql/src/speechnexus_portable_sql/claims.py:270-277`、`BUCKETS=256` 在 `:66`）。定义点只有这一个；0064/0065 的
  回填也调它（`0064:67-83`、`0065:122-140`），SQL 里不重写哈希。
- **轮询起点固定 0**，不用 `start_bucket_for(owner_id, 分钟)`：因为 `claim_batch` 把整圈放在一笔事务
  里，桶行锁持有到提交，两个起点不同的派发者必然在环上相交报 `40P01`
  （`job_dispatch_postgres.py:74-85`）。`start_bucket_for` 在生产**无调用者**
  （`grep -rn "start_bucket_for" backend/src execution-gateway/src` 只命中注释）。
- **`due_at` 是 `TIMESTAMP(6)` 的 naive UTC**，来源冻结为草案 `speech_jobs.created_at_us`
  （`migrations/canonical_source_map.json`；换算见 `0064:59-65`）。候选谓词是
  `due_at <= ?` 加 keyset `(due_at, job_id)`（`packages/portable-sql/src/speechnexus_portable_sql/claims.py:74-78`），不是 `LIMIT/OFFSET`。

### 2.4 `event_seq` / `event_digest`

- 序号来自**权威头**而不是当前投影：`job.event_head` 取 `last_event_seq, claim_version` 并
  `FOR UPDATE`，取 `+1`，然后 `job_event.insert` 与 `job.event_head_update`
  （`packages/portable-sql/src/speechnexus_portable_sql/claims.py:967-1013`）。这让「先取时间、后提交」的两笔事务不会换序。
- 摘要是**唯一算法**：字段集合与顺序固定在 `EVENT_DIGEST_FIELDS`（`packages/portable-sql/src/speechnexus_portable_sql/claims.py:889-899`），时间折成
  UTC 微秒定长 `%Y-%m-%dT%H:%M:%S.%fZ`（`_canonical_time:902-906`），JSON 用 `sort_keys` + 紧凑
  分隔符 + `ensure_ascii=False`，前缀 `sha256:`（`:951-964`）。
- `payload_ref` 可空；历史事件搬迁**只写 NULL**，非空引用直接拒绝而不是丢掉
  （`packages/portable-sql/src/speechnexus_portable_sql/claims.py:1053-1060`）。这条口径来自 §8.75.1 的产品决定仍未作出的现状。

### 2.5 一次认领怎么返回结果

`BatchClaim` 只有三个字段——`job_id`、`claim_version`（CAS 之后的新版本）、`expires_at`
（本次认领写入的 `claim_expires_at`）（`packages/portable-sql/src/speechnexus_portable_sql/claims.py:210-221`）。其余列按 `job_id` 集合用
`claimed_jobs()` 读一次（`packages/portable-sql/src/speechnexus_portable_sql/claims.py:836-864`），由 `_claim_job()` 组装成 `ClaimedJob`：

- `lease_expires_at_us = to_epoch_us(claim.expires_at)`，**不是**用 `lease_ms` 重算
  （`job_dispatch_postgres.py:195-219`）——否则 `_renew`/`_finish` 的守卫会整体偏移；
- `request_document` 在视图里可空，`DispatchJob` 要历史空串形状，所以显式 `or ""`（`:213-216`）；
- 顺序还原成旧语句的 `created_at_us, job_id`（`:340-342`）。

---

## 3. 精确 delta

16 行，覆盖「已经是 canonical 的」与「还没切的」两类，因为退役草案列时必须知道哪些列**没有**
写者。行数由命令可复现（§8）。

| # | 语句 / 位置 | 草案列 | canonical 列 | 换算规则 | 状态 |
| --- | --- | --- | --- | --- | --- |
| D1 | `bucket.fence`/`acquire`/`release`（`packages/portable-sql/src/speechnexus_portable_sql/claims.py:100-113`） | 草案无桶表 | `coordination_claim_buckets.*` | 桶号 `bucket_for(job_id)`；256 行由 0066 播种 | 已切 |
| D2 | `job.reclaim`（`packages/portable-sql/src/speechnexus_portable_sql/claims.py:150-154`） | 无对应语句；到期写在认领谓词里（`claims_sql.py:63`） | `job_state='pending'` + `claim_version = claim_version + 1` | 重领必须涨版本；不涨则旧 owner 的版本 CAS 仍能成功 | 已切 |
| D3 | `job.claim`（`packages/portable-sql/src/speechnexus_portable_sql/claims.py:146-149`） | `claim_expires_at_us BIGINT`；`job_state` 不动 | `claim_expires_at TIMESTAMP(6)`；`job_state='claimed'` | epoch µs → naive UTC；`0` → `NULL`；状态必须由 `pending` 推到 `claimed` | 已切 |
| D4 | `job.candidates.lane_admitted`（`packages/portable-sql/src/speechnexus_portable_sql/claims.py:130-138`） | `claim_expires_at_us <= now`（到期即重领） | `job_state = ?` + `due_at <= ?` + keyset | 到期语义移到 D2；候选只回答「哪些 Job 到期可派」 | 已切 |
| D5 | `job.claimed_view`（`packages/portable-sql/src/speechnexus_portable_sql/claims.py:139-145`） | `speech_jobs.kind/state/status_version/created_at_us` | 同（草案 Job 投影仍是权威） | 无换算；`request_document` 可空，`None → ""` | 已切 |
| D6 | `claims.read_claim`（`claims_sql.py:27-31`） | `claim_expires_at_us AS expires_at_us` | `claim_expires_at` | 读回微秒需 `to_epoch_us`（naive 按 UTC，`clock.py:60-66`） | 待收敛 |
| D7 | `claims.claim_in_transaction`（`claims_sql.py:60-65`） | `claim_expires_at_us = ?` 且谓词 `claim_expires_at_us <= ?` | `claim_expires_at = ?`，`job_state='claimed'` | 直换即坏：`NULL <= ?` 求值为 NULL，首次认领永远失败（实测 A） | 待收敛 |
| D8 | Job 写入（`job_writer_columns.py:24-38`） | 无 `bucket_no`、`due_at` | `bucket_no`、`due_at` | `bucket_for(job_id)`；`due_at` 来源 `speech_jobs.created_at_us` | 已切 |
| D9 | `_mark_running` 事件追加（`job_dispatch_postgres.py:432-434`） | `speech_job_events(event_seq = MAX+1, state, occurred_at_us BIGINT)` | `job_events(event_seq = head+1, event_digest, payload_ref)` + `coordination_jobs.last_event_seq` | 序号取头；`occurred_at` epoch → naive UTC | 待切 |
| D10 | `_record_progress` 进度事件（`job_dispatch_postgres.py:540-546`） | 同上 + `kind/progress_unit/progress_completed/progress_total` | `job_events` 无进度列，值只能进摘要 | 读者必须从摘要重建进度，不能读列 | 待切 |
| D11 | 事件摘要（`packages/portable-sql/src/speechnexus_portable_sql/claims.py:909-964`） | 草案表无摘要列 | `event_digest VARCHAR(71)` | `sha256:` + 规范 JSON，字段顺序固定 | 待切 |
| D12 | `outbox.insert`/`mark_delivered`（`packages/portable-sql/src/speechnexus_portable_sql/claims.py:169-180`） | `coordination_outbox.delivered_at_us BIGINT` | `delivered_at TIMESTAMP(6)` | `0`/`NULL` → `NULL`；epoch → naive UTC；列在 alembic 树不存在（§7-B1） | 待切 |
| D13 | 事件头 | `MAX(speech_job_events.event_seq)`（`job_dispatch_postgres.py:156-160`） | `coordination_jobs.last_event_seq`（带 `claim_version` CAS，`packages/portable-sql/src/speechnexus_portable_sql/claims.py:155-161`） | 回填 = MAX（`0065:90-100`）；写 = +1 且在 `FOR UPDATE` 下 | 待切 |
| D14 | 进度不变量 | DB 触发器 `speech_job_event_progress_guard`（`public_job_progress_schema.py:41-79`） | 无对应触发器 | 四条断言（running、unit 一致、序号接续、严格递增）必须搬到应用层 | 待决 |
| D15 | `claim_version` 类型 | `BIGINT DEFAULT 0`（`job_schema.py:30`） | `DECIMAL(38,0) NOT NULL`（`0003:24`） | 直传，只经 `Decimal(str(...))`，不经 float | 已切 |
| D16 | 草案独有列 | `initial_event_id`（`NOT NULL UNIQUE`）、`initial_event_digest`（`job_schema.py:25-26`） | `0003` 无对应列 | 收敛方向未定：canonical 形状没有「初始事件」这个概念 | 未处理 |

### 3.1 可空性：`0` 与 `NULL` 不是同一个值

草案用 `0` 表示「未领取」，canonical 用 `NULL`（`packages/portable-sql/src/speechnexus_portable_sql/claims.py:280-300`）。这在**等值**比较下等价，
在**范围**比较下不等价。真库实测（临时库，跑完即删，命令见 §8 探针 A）：

```text
draft  claim_expires_at_us = 0  <= now_us  -> 1
canon  claim_expires_at IS NULL <= now     -> 0
canon  谓词求值为 NULL(不是 false)        -> True
```

后果分两面：把 `claims_sql.claim_in_transaction` 的谓词按字面换列，**首次认领永远失败**
（三值逻辑里 `NULL <= ?` 是 unknown，`rowcount=0`），而且失败形态是「认领不到」而不是异常——最安静
的那种坏。反过来，如果把 `NULL` 当成 `0` 用 `coalesce` 兜成纪元，`claim_expires_at` 就会带上一个
假事实（「1970 年到期」），后续 `job.reclaim` 会把它当死租约重领，版本连涨。
**结论：`claim_expires_at` 保持可空（D4），不得为形状一致加 `NOT NULL DEFAULT`。**

### 3.2 时区：naive UTC 是契约，不是风格

canonical 时刻列是 `TIMESTAMP(6)`（无时区）。把 aware 值交给驱动，PostgreSQL 会按**会话时区**折墙上
时间，且读回没有任何报错。实测（探针 B，会话 `Asia/Shanghai`）：

```text
直接赋 timestamptz (session +08:00)         -> 2026-11-23 20:00:00
AT TIME ZONE 'UTC' (canonical 换算)         -> 2026-11-23 12:00:00
```

现有防线有三层，切列时必须全部保留：`utc_from_epoch_us` 返回 naive UTC（`packages/portable-sql/src/speechnexus_portable_sql/claims.py:285-300`）、
迁移里显式 `AT TIME ZONE 'UTC'`（`0064:50-56`、`0065:74-78`）、生产池把每条连接 `SET TIME ZONE
'UTC'`（`backend/src/speechnexus_backend/speech/infrastructure/postgres_pool.py:36`）。第三层是**兜底而非契约**：池外的代码（脚本、第二个驱动、
`test_canonical_job_conversion_pg.py` 刻意用非 UTC 会话）不经过它。

### 3.3 `claim_version` 是 fence，不是计数器

草案里「过期」本身就是 fence：旧 owner 的 CAS 带 `claim_expires_at_us <= now`，过期必然失败。
canonical 里旧 owner 的 CAS 带 `claim_version = ?` 与 `job_state='claimed'`——
**若不涨版本，重领之后旧 owner 用原版本仍可能写成功**（`packages/portable-sql/src/speechnexus_portable_sql/claims.py:580-588`）。
所以 `job.reclaim` 的 `claim_version + 1` 是语义的一部分，不是顺手；把它当「版本字段总该加一」
抄进另一条语句、或从重领语句里删掉，都会让两个 owner 同时持有同一个 Job，而且症状要等到下一次
`_finish` 才出现（`DispatchClaimLostError` 变成成功）。

派生风险：`_finish` 关闭后**保留** `claim_expires_at` 不清空（`job_dispatch_postgres.py:585-588`），
所以「终态行仍带一个过去的到期时间」是正常状态。任何「`claim_expires_at < now` 即可重领」的
判定都必须先看 `job_state`，否则会把已完成的 Job 重新打开。

### 3.4 去重：序号主键、摘要、outbox 三件不同的事

- **序号**：草案 `speech_job_events` 主键 `(job_id, event_seq)`（`public_job_schema.py:59-69`），
  序号由应用 `MAX+1` 算出（`job_dispatch_postgres.py:149-163`）；canonical 也以
  `(job_id, event_seq)` 为主键（`0003:43`），但序号由头 CAS 生成。**换列不会自动换掉
  「`MAX+1` 与头 CAS 不一致」这件事**：只要还有一个写者用 `MAX+1`，`last_event_seq` 就会被绕过。
- **摘要**：canonical 多一列 `event_digest`，它覆盖 `kind/progress_*`（`EVENT_DIGEST_FIELDS`），
  而草案把这些当独立列存。D10 的换算因此**不可逆**：从 canonical 行读不出原始进度列，只能靠摘要
  相等判断「是不是同一条事实」。
- **outbox**：`outbox.insert` 没有 `ON CONFLICT`（`packages/portable-sql/src/speechnexus_portable_sql/claims.py:169-172`），`event_id` 是
  `new_identity()` 生成的随机 ULID（`types.py:81-90`，`packages/portable-sql/src/speechnexus_portable_sql/claims.py:1002`）。所以**同一条事实重试一次
  就会写第二行 outbox**，两行 `event_digest` 相同、`event_id` 不同。去重责任因此落在消费者的摘要
  比对上，不在主键上。这一条在切事件侧之前必须写进判据，否则「至少一次投递」会变成「至少一次重复
  投递且无标记」。

---

## 4. 锁序

### 4.1 声明与代码位置

固定顺序是 **`coordination_claim_buckets` → `coordination_jobs` → `coordination_outbox`**
（`packages/portable-sql/src/speechnexus_portable_sql/claims.py:11-12`，模块 docstring；`job_dispatch_postgres.py:12-18`）。代码里的落点：

| 顺序 | 证据 |
| --- | --- |
| 桶行先 | `claim_batch` 每个桶先 `read_fence`（`FOR UPDATE`，`packages/portable-sql/src/speechnexus_portable_sql/claims.py:106-109`）再 `acquire`，最后在 `finally` 里 `release`（`:762-769`）；整笔事务内桶行不可能再变 |
| 再 Job 行 | 桶持有时才 `reclaim_expired` → `job.claim`（`:796-826`）；state store 的 `_record_progress`/`_finish` 都是先 canonical Job `FOR UPDATE`（`:503-514`、`:606-611`）再 `speech_jobs`（`:517-522`、`:615`） |
| outbox 最后 | 只有 `append_event` 在同事务尾部 `insert_outbox`（`packages/portable-sql/src/speechnexus_portable_sql/claims.py:1003-1010`），而它先锁 `job.event_head`（`:985`） |

`_finish` 的顺序容易被误读：它先取 canonical Job 行锁（`:606`），再改 `speech_jobs`（`:615`），
再写事件（`:633`），最后**关闭**同一个 canonical 行（`:636`）——第二次 UPDATE 不再取新锁，
所以仍是 Job → public → event，没有反向。

### 4.2 证明这个顺序需要什么样的真库证据

现有并发用例只断言**结果**（`threading.Barrier` + 结果集合），不断言**谁被谁挡住**：
`test_claim_batch_pg.py:266-317`（同一桶只有一个赢家）、`:321-357`（活租约让另一个 owner 跳过）、
`:421-469`（Job CAS 竞争）。**「谁阻塞谁」在本仓还没有一条派发侧判据。** 观测法已有范式：
`test_public_job_draft_lock_pg.py:173-175` 与 `tests/integration/test_workbench_keyed_lock_pg.py`
用 `pg_stat_activity` 的 `wait_event_type='Lock'`。

我在这台机器的临时库上实测了这条观测链，确认它**能给出可断言的形状**（探针 C，跑完即删）：

```text
waiter: (54269, 'Lock', 'transactionid', [54268])
holder: (54268, 'idle in transaction',
         'SELECT bucket_no FROM coordination_claim_buckets WHERE bucket_no=0 FOR UPDATE',
         'coordination_claim_buckets:RowShareLock,coordination_claim_buckets_pkey:RowShareLock')
second: blocked
```

三个可断言的字段，且缺一不可：

1. waiter 的 `pg_blocking_pids(pid)` 必须等于 holder 的 pid（行的身份）；
2. waiter 的 `wait_event='transactionid'`——**行锁不产生 relation 级等待项**，所以
   「`wait_event_type='Lock'` 且 relation = 桶表」这种断言永远写不出来，只能靠 holder 侧反查；
3. holder 的 `pg_locks`（`locktype='relation'`）必须只含 `coordination_claim_buckets`，
   **不含 `coordination_jobs`**——这一条才是「桶锁先于 Job 锁」的正面证据，而不是「它也在等」。
   配合 holder 的 `state='idle in transaction'` 与 `query` 文本，可以钉住「它停在桶语句上」。

**因此需要新增三条判据**（真库、两个连接、非 owner 角色）：

| 判据 | 构造 | 红在哪 |
| --- | --- | --- |
| 桶锁先于 Job 锁 | 会话 A `BEGIN; SELECT … FROM coordination_claim_buckets WHERE bucket_no=0 FOR UPDATE`，会话 B 跑 `claim_batch` | B 阻塞且 `pg_blocking_pids(B)={A}`；A 的 relation 锁集合不含 `coordination_jobs`；否则「顺序」没有被观测到 |
| 反向路径不存在 | 会话 A 持 `coordination_jobs` 行锁（`_finish` 的守卫语句），会话 B 跑 `claim_batch` | B 必须阻塞在 **Job 行**而不是先拿到桶；若 B 先拿到桶再等 Job，说明有路径把顺序倒过来了 |
| 无环 | 两笔事务交叉（A 拿桶等 Job、B 拿 Job 等桶） | 必须出现 `40P01`，且只能是人工构造出来的——正常路径下不可能构造成功 |

第三条是**反例判据**（证明环不可达），不是通过判据；把它当绿灯会误导。

---

## 5. 分阶段切换

硬约束：**任一时刻不得有两个写者写两种形状。** 「双写」只允许发生在「同一事实的两种表示」上
（草案事件行 + canonical 事件行），不允许同一列有两个写者。事件侧必须先有对象才有写者，所以
S0 是纯加法 DDL。

| 阶段 | 类型 | 动作 | 注入（必须变红） |
| --- | --- | --- | --- |
| **S0** 事件侧加法 | 加法 | 新修订建 canonical `job_events`、给 `coordination_outbox` 加 `delivered_at`、按列授 `SELECT/INSERT` 与 `UPDATE (delivered_at)`。无写者、无读者 | 把 `delivered_at` 从 DDL 删掉 → `outbox.mark_delivered` 必须报 42703；若测试仍绿，说明没有语句覆盖它 |
| **S1** 事件双写 | 加法（写两份表示，读旧的） | `_mark_running`/`_record_progress`/`_finish` 在同一事务里额外 `append_event` 并推 `last_event_seq`；读路径不动。事件序号仍以草案 `MAX+1` 为准，canonical 侧做影子 | 删掉 canonical 事件 INSERT → 双写对账（逐 Job 条数 + digest 逐条相等，`portable-reconcile.py:_event_gaps` 同口径）必须红；仍绿说明对账读的是同一侧 |
| **S1b** started 纳入双写 | 加法（补齐 S1 漏掉的那条迁移） | `_mark_running` 在同一事务里追加 started 的 canonical 行并推 `last_event_seq`；先取 canonical 事件头锁再改公开行，保持 §4.1 的锁序。`claim_version` 不推进。没有 `coordination_jobs` 行的 Job 由 `_append_canonical_event` 的父行检查**拒绝**（类型化错误、整笔回滚），不跳过 | 删掉 started 的 canonical 追加 → 「完整生命周期 canonical 序号 1..N 无缺口」必须红（实测：`canonical has 2 event(s) [1, 2], the draft side covers 3 [1, 2, 3]`）；把追加挪到独立事务 → started 的原子性用例必须红（实测：`{'state': 'running', 'status_version': 2} != {'state': 'pending', 'status_version': 1}`）；去掉父行检查 → 没有父行的 Job 必须撞外键（实测 `ForeignKeyViolation` 23503 `job_events_job_id_fkey`） |
| **S2** 事件读翻转 | 翻读 | SSE 与列表改读 canonical `job_events`；草案事件表退为影子，只写不读。`last_event_seq` 成为唯一事件头 | 把 canonical 事件的 `occurred_at` 写成会话本地时间（去掉 `utc_from_epoch_us`/`AT TIME ZONE 'UTC'`）→ 摘要与顺序断言必须红（§3.2 实测 B 是这条注入的固定靶） |
| **S3** 收敛第二认领口 | 删（收敛，不迁移） | `claims_sql.claim_in_transaction` 与 `read_claim` 改为调 `claim_batch`/`claimed_jobs`，或整体删除并让 `ClaimTransactionBoundary` 拒绝服务。二者只能选一个 | 保留 `claim_expires_at_us <= ?` 谓词但把列换成 `claim_expires_at` → 「首认领」用例必须红（§3.1 实测 A）；若仍绿，说明用例种子里 `claim_expires_at` 非空，没有覆盖未认领态 |
| **S4** 收紧约束与授权面 | 紧 | 只收紧写入者能保证的列：`bucket_no`/`due_at` 的 `NOT NULL`（0065 有意留给写入切换，`0065:26-36` 第 3 条）；同一提交从 `coordination_grants.SPEECH_UPDATES["coordination_jobs"]` 与 `job_runtime._JOB_UPDATE_SURFACE` 里删掉 `claim_expires_at_us` | 让写入者不再提供 `bucket_no` → 必须 23502；把 `claim_expires_at_us` 从授予面删掉但启动闸门仍要求它 → `RuntimeStartupError` 必须红（`job_runtime.py:283`、`:740`） |
| **S5** 退役草案列 | 删（单向） | 删 `coordination_jobs.claim_expires_at_us` 与 `coordination_outbox.delivered_at_us`；删 `claims_sql` 两条语句；删 `job_schema.JOB_DDL` 里的 epoch 列与 `provision-job-roles.WRITER_BASELINE_UPDATE`、`check-async-job-prereqs`、`coordination-grants.py` 的第二份清单 | 删列但保留一处列清单 → 启动闸门必须报未声明列；删列后 0064/0065 的重跑必须报 42703（这证明「重跑迁移」不再是合法回退路径，见 §6） |
| **S6** 回退演练 | 验证 | 按 §6 的顺序在临时 schema 上跑一遍：S5 之后只能走备份恢复，S4 之前可以走「读回切」 | 在 S2 之后执行 0065 的 `downgrade`（`UPDATE … SET last_event_seq = 0`，`0065:185`）→ 事件头必须归零；若事件读已经翻转，这条 downgrade 就是毁事实，演练必须红 |

三条与顺序有关的硬事实：

- **S1 之前不能翻读。** `job_events` 在生产 schema 里不存在（§7-B1），翻读的直接后果是 SSE 42P01。
- **S4 与 S5 不能合并。** 授权面收缩（S4）是可回退的；删列（S5）不是。把两者放一个提交，
  等于让回退必须改代码。
- **S3 必须在 S4 之前。** 否则「授权面里已经没有 `claim_expires_at_us`」与「还有一条语句在写它」
  会同时为真，启动闸门与应用写入互相矛盾，症状是 42501 而不是测试红。

---

## 6. 回退

回退点不是一个，而是三个；越过 S5 之后没有回退点。

| 回退到 | 仍必须可读 | 不能做的 | 证据 |
| --- | --- | --- | --- |
| S2 之后（事件已翻读） | canonical `job_events` 行、`last_event_seq`、草案 `speech_job_events` | 不能走 alembic `downgrade`：`0065` 的 downgrade 把 `last_event_seq` 归零（`0065:185`），而它当初安全的理由是「这一列没有任何读取者」（`0065:53-55`）——S2 之后这个前提不成立 | 回退＝读路径回切 + 保留列；对账仍按 `portable-reconcile.py` 的逐字段口径 |
| S1 之后（已双写） | 草案 `speech_job_events`（旧读路径的唯一来源）、草案 `claim_expires_at_us` | 不能「重跑搬迁」：`adopt_historical_events` 对已有事件的 Job 直接拒绝（`packages/portable-sql/src/speechnexus_portable_sql/claims.py:1051-1052`），一次性回填不是修补手段 | 回退后重跑必须**被拒绝**，这正是「没有半成品」的形状 |
| S4 之前（约束未收紧） | `claim_expires_at`（canonical 权威）、`bucket_no`/`due_at`（可空） | 不能把 canonical 租约擦成 NULL：`0065` 的回填是 fill-if-null（`0065:75-78`），就是为了不擦在飞租约 | `0065:102-118` 的四条对账语句在每次 upgrade 时由 `0065:160-174` 调用 |

**「没有丢事实」的判据（按强度递增）：**

1. `0064:84-109` 与 `0065:160-174` 的提交前对账：漏桶、草案已领取但 canonical 未收到、事件头与
   `MAX` 不一致、`due_at` 缺失——任一不为 0 就抛错并随迁移事务回滚。
2. `scripts/dev-env/portable-reconcile.py` 逐字段比对并以退出码 1 报告（`:8-18` 的口径表与退出码、
   `:92-135` 的字段清单）。它比的是值，不是行数。
3. 演练 `test_portable_switchover_rehearsal_pg.py:198-231`：撤掉 canonical 侧全部表→重跑→
   `stdout` 与第一次**逐字节相同**，且草案侧行数不变（`:221-223`）。
4. `test_canonical_backfill_rollback_pg.py:189-231`：提交后行可读，重复搬迁被拒——
   「可读」与「不可重复」两条一起才排除「搬了一半又补了一遍」。

**回退时要一起回退的东西（容易漏）：** `coordination_grants.py:63-65` 的
`coordination_jobs` 列清单（当前同时含 `claim_expires_at` 与 `claim_expires_at_us`）、
`job_runtime.py:265-271` 的允许面、`scripts/dev-env/provision-job-roles.py:84` 与
`check-async-job-prereqs.py:377` 的第二份清单。这四处是同一份声明面的四份副本（§8.77 前置 4 已记
过一次），任一处与表结构不一致都会在**启动**时以 `RuntimeStartupError` 失败，而不是在写入时。

---

## 7. 未验证

以下条目**不是**推断，而是我在这棵树里无法证实的事实。每条都给出验证方式。

**A. 生产库当前状态。** 所有结论都建立在代码与迁移文件上；我没有连接任何生产库，也没有设
`SPEECHNEXUS_TEST_DATABASE_URL`（`env | grep SPEECHNEXUS_TEST_DATABASE_URL` 为空）。
验证：`alembic current` + `\d+ coordination_jobs`（看 `bucket_no`/`due_at`/`claim_expires_at`/
`claim_expires_at_us` 是否并存）+ `SELECT COUNT(*) FROM coordination_claim_buckets`（应为 256）。

**B1. canonical 事件对象在 alembic 树里不存在。** 这是代码事实（0 处 `CREATE TABLE job_events`、
0 处 `delivered_at`），不是运行时事实。若某个部署手工建过，结论要改。
验证：`grep -rn "job_events\|delivered_at" backend/src/.../migrations/versions/*.py | grep -v speech_job_events`
——当前输出为空。

**B2. `portable-reconcile.py` 在生产 schema 上跑不起来。** 它读 `due_at_us`（`:103`），而生产
`coordination_jobs` 没有这一列（`0064` 的 docstring 明说来源列不存在）。演练用的是测试自带
`draft_jobs` 表（`test_portable_switchover_rehearsal_pg.py:37-46`），所以演练全绿**不等于**生产库
能对账。验证：对生产 schema 跑一次 `--dsn`，期望 42703 而不是「字段不一致」。

**C. 派发侧锁序没有真库判据。** §4.2 的三条判据是我在临时库上验证的**观测手法**，不是仓库里的
用例；仓库当前 0 条派发侧「谁阻塞谁」断言。验证：把 §4.2 的三条写进
`tests/integration/test_dispatch_canonical_claim_pg.py`（该文件已有 5 条 oracle，
`:414/:440/:481/:508/:538`），用非 owner 角色与两个真实连接跑。

**D. `due_at` 是否恒等于 `submitted_at_us`。** 来源清单把 `due_at` 冻结为
`speech_jobs.created_at_us`，但「`created_at_us` 与 `submitted_at_us` 相同」这一点在代码里没有
断言（§8.77 前置 2 明确说「不能假定」）。验证：一次 `SELECT COUNT(*) FROM speech_jobs j JOIN
speech_job_submissions s USING (job_id) WHERE j.created_at_us <> s.submitted_at_us`。

**E. `ClaimService` 确实没有生产装配。** 依据是 `grep` 只命中定义与测试。若某些部署脚本或外部
调用方通过它认领，S3 就是行为变更而不是清理。验证：在部署侧 `grep -rn ClaimTransactionBoundary`
与入口清单，并查 `tests/integration/test_production_claims.py` 的调用方式是否被运维脚本复用。

**F. 三条测试 oracle 未在本轮执行。** `test_dispatch_canonical_claim_pg.py`、`test_claim_batch_pg.py`、
`test_portable_switchover_rehearsal_pg.py` 的通过状态我没有观测，本文不据此宣称任何「已验证」。
验证：设 `SPEECHNEXUS_TEST_DATABASE_URL` 后按 §8 的命令跑。

**G. `coordination_jobs` 两张形状的收敛方向未定。** canonical 0003 要求 `bucket_no/due_at`
`NOT NULL` 且没有 `initial_event_id`；生产 alembic 表是草案形状 + 0064 加法列（`bucket_no` 仍可空，
`initial_event_id` 仍 `NOT NULL UNIQUE`）。D16 的收敛需要产品/架构决定「初始事件」在 canonical
形状里如何表达，本文不替它决定。

---

## 8. 复现命令

数值与实测都出自下面这些命令（仓库根目录执行）。

```bash
# canonical 语句条数 = 17
python3 - <<'PY'
import re, pathlib
src = pathlib.Path("packages/portable-sql/src/speechnexus_portable_sql/claims.py").read_text()
block = src.split("STATEMENTS = {", 1)[1].split("\n}", 1)[0]
print(len(re.findall(r'^    "([a-z_.]+)":', block, re.M)))
PY

# 派发侧 epoch 列引用 = 1 行(且只在 docstring 里) / 可执行引用 = 0
grep -n "claim_expires_at_us" backend/src/speechnexus_backend/speech/infrastructure/job_dispatch_postgres.py

# 第二认领口仍在线上的语句 = 2(3 行)
grep -n "claim_expires_at_us" backend/src/speechnexus_backend/speech/infrastructure/claims_sql.py

# 第二认领口无生产装配
grep -rn "ClaimService\|ClaimTransactionBoundary" --include=*.py . | grep -v "\.venv"

# canonical 事件对象在 alembic 树里缺失(输出为空)
grep -rn "job_events\|delivered_at" backend/src/speechnexus_backend/speech/infrastructure/migrations/versions/*.py \
  | grep -v speech_job_events

# 本文 delta 表行数 = 16
grep -c '^| D[0-9]' docs/plans/2026-11-26-dispatch-canonical-cutover.md

# 已存在的派发 oracle 数 = 5
grep -c '^def test_' tests/integration/test_dispatch_canonical_claim_pg.py

# 锁序/时区/空值三处实测:把 §9.1 与 §9.2 各存成一个临时文件,再
.venv/bin/python <9.1 的临时文件>              # A 空值语义、B 时区折叠
.venv/bin/python <9.2 的临时文件>              # C 谁阻塞谁(pg_blocking_pids + holder 关系锁)

# 已存在的判据(需 SPEECHNEXUS_TEST_DATABASE_URL,本轮未执行)
uv run pytest -q tests/integration/test_dispatch_canonical_claim_pg.py \
  tests/integration/test_claim_batch_pg.py tests/integration/test_portable_switchover_rehearsal_pg.py
```

探针脚本只建临时库、跑完 `DROP DATABASE … WITH (FORCE)`，不写仓库、不碰既有 schema；A/B 的输出与
C 的输出已逐字抄在 §3.1、§3.2、§4.2。

---

## 9. 附录：探针源码

两段脚本在临时库上跑，结束前 `DROP DATABASE … WITH (FORCE)`；不写仓库、不碰既有 schema。
§3.1、§3.2、§4.2 的输出就是它们逐字打印的。

### 9.1 空值语义与时区折叠（探针 A/B）

```python
import psycopg

ADMIN, DB = "dbname=postgres", "sn_cutover_probe_20261126"
a = psycopg.connect(ADMIN, autocommit=True)
a.execute(f"DROP DATABASE IF EXISTS {DB}")
a.execute(f"CREATE DATABASE {DB}")
a.close()
c = psycopg.connect(f"dbname={DB}", autocommit=False)
c.execute("""
CREATE TABLE draft_shape (claim_expires_at_us BIGINT NOT NULL, job_state VARCHAR(24) NOT NULL);
CREATE TABLE coordination_jobs (job_id VARCHAR(128) PRIMARY KEY, claim_expires_at TIMESTAMP(6));
INSERT INTO draft_shape VALUES (0, 'pending');
INSERT INTO coordination_jobs VALUES ('job-1', NULL);
""")
c.commit()

print("== A. 0 与 NULL 在认领谓词下的差别 ==")
print(
    "draft  claim_expires_at_us = 0  <= now_us  ->",
    c.execute(
        "SELECT count(*) FROM draft_shape WHERE claim_expires_at_us <= 9999999999999999"
    ).fetchone()[0],
)
print(
    "canon  claim_expires_at IS NULL <= now     ->",
    c.execute(
        "SELECT count(*) FROM coordination_jobs WHERE claim_expires_at <= TIMESTAMP '2026-11-23 00:00:00'"
    ).fetchone()[0],
)
print(
    "canon  谓词求值为 NULL(不是 false)        ->",
    c.execute("SELECT (NULL::timestamp <= TIMESTAMP '2026-11-23 00:00:00') IS NULL").fetchone()[0],
)

print("== B. aware 值写进 TIMESTAMP(6) 的会话时区折叠 ==")
c.execute("SET TIME ZONE 'Asia/Shanghai'")
print(
    "直接赋 timestamptz (session +08:00)         ->",
    c.execute("SELECT ('2026-11-23 12:00:00+00'::timestamptz)::timestamp(6)").fetchone()[0],
)
print(
    "AT TIME ZONE 'UTC' (canonical 换算)         ->",
    c.execute(
        "SELECT ('2026-11-23 12:00:00+00'::timestamptz AT TIME ZONE 'UTC')::timestamp(6)"
    ).fetchone()[0],
)
c.rollback()
c.close()
a = psycopg.connect(ADMIN, autocommit=True)
a.execute(f"DROP DATABASE {DB} WITH (FORCE)")
a.close()
```

### 9.2 谁阻塞谁（探针 C）

```python
import psycopg, time, threading

ADMIN, DB = "dbname=postgres", "sn_cutover_probe_lock3"
a = psycopg.connect(ADMIN, autocommit=True)
a.execute(f"DROP DATABASE IF EXISTS {DB}")
a.execute(f"CREATE DATABASE {DB}")
a.close()


def conn():
    return psycopg.connect(f"dbname={DB}", autocommit=False)


c = conn()
c.execute("""
CREATE TABLE coordination_claim_buckets (bucket_no DECIMAL(38,0) PRIMARY KEY,
  claim_owner_id VARCHAR(128), lease_until TIMESTAMP(6), version DECIMAL(38,0) NOT NULL);
CREATE TABLE coordination_jobs (job_id VARCHAR(128) PRIMARY KEY,
  bucket_no DECIMAL(38,0) NOT NULL, due_at TIMESTAMP(6) NOT NULL, job_state VARCHAR(24) NOT NULL);
INSERT INTO coordination_claim_buckets VALUES (0, NULL, NULL, 0);
INSERT INTO coordination_jobs VALUES ('job-1', 0, TIMESTAMP '2026-11-23 00:00:00', 'pending');
""")
c.commit()

# 持有者只锁桶行,不碰 Job 行 —— 这正是「桶锁先于 Job 锁」要观测的形状
hold = conn()
hold.execute("SELECT bucket_no FROM coordination_claim_buckets WHERE bucket_no = 0 FOR UPDATE")
hold_pid = hold.info.backend_pid
st = {}


def second():
    s = conn()
    st["pid"] = s.info.backend_pid
    s.execute("SET lock_timeout = '3s'")
    try:
        s.execute("SELECT bucket_no FROM coordination_claim_buckets WHERE bucket_no = 0 FOR UPDATE")
        st["r"] = "acquired"
    except psycopg.errors.LockNotAvailable:
        st["r"] = "blocked"
    s.rollback()
    s.close()


t = threading.Thread(target=second)
t.start()
obs = conn()
for _ in range(40):
    rows = obs.execute("""
      SELECT a.pid, a.wait_event_type, a.wait_event, pg_blocking_pids(a.pid)
      FROM pg_stat_activity a WHERE a.datname = current_database() AND a.wait_event_type = 'Lock'
    """).fetchall()
    if rows:
        break
    time.sleep(0.05)
print("waiter:", rows)
print(
    "holder:",
    obs.execute(
        """
  SELECT pid, state, left(query, 80) AS last_query,
    (SELECT string_agg(DISTINCT c.relname || ':' || l.mode, ',')
       FROM pg_locks l JOIN pg_class c ON c.oid = l.relation
      WHERE l.pid = a.pid AND l.locktype = 'relation') AS relation_locks
  FROM pg_stat_activity a WHERE pid = %s
""",
        (hold_pid,),
    ).fetchone(),
)
t.join()
print("second:", st.get("r"))
hold.rollback()
hold.close()
obs.close()
a = psycopg.connect(ADMIN, autocommit=True)
a.execute(f"DROP DATABASE {DB} WITH (FORCE)")
a.close()
```

实测断言的三件事：`pg_blocking_pids(waiter) == {holder}`；`wait_event='transactionid'`（行锁没有
relation 级等待项）；holder 的 `locktype='relation'` 集合只含 `coordination_claim_buckets`。
