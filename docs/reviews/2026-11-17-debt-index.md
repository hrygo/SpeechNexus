# 就绪债务查询索引的非作者审查（PR #923）

审查对象：`perf/debt-probe-queries`，head `220b4770`（`docs: 参考部署 runbook 迁移 head 跟进 0056 (#919)`，
父 `baa65cdb`），PR #923。审查分支 `review/923-debt-index` 从 `origin/main`（`3b1bbf5e`）新建，本报告是该分支
唯一改动。

**行号口径：** 下文所有 `file:line` 按被审查 head `220b4770` 计。head 比 `origin/main` 多出 `0056` 迁移与`readiness.py` 的两段注释，因此 `readiness.py` 的 `_DEBT` / `_STOP_EVIDENCE_DEBT` 在 head 上是 `:388` / `:431`、在 `origin/main` 上是 `:382` / `:421`；`0056_debt_probe_live_state_index.py` 与两个新用例文件只存在于 head。

审查在独立 worktree `/private/tmp/sn926` 完成。隔离环境：PostgreSQL 16.16，
`SPEECHNEXUS_TEST_DATABASE_URL=postgresql://hrygo:synthetic_pg_pass@127.0.0.1:55480/sn892`；所有 `uv` 命令带
`UV_NO_CONFIG=1`。规模实验各自新建临时 schema（`sn_debt_*`/`sn_var_*`/`sn_scale_*`/`sn_s2_*`/`sn_cx_*`/
`sn_lock_*`/`sn_dir_*`/`sn_perm_*`），每个都在 `finally` 里 `DROP SCHEMA … CASCADE`，实验后 `pg_namespace`
只剩审查前就存在的 `sn_review_empty`；权限实验用的临时角色也已 `DROP ROLE`。被审查工作树里为反例做的
改动全部还原，`git status --porcelain` 为空。

**结论：能合。** 索引定义与两条查询的谓词确实匹配（`EXPLAIN ANALYZE` 证据见 §1），迁移单 head、可逆、
SHARE 锁窗口 47.5 ms，读取诊断身份不需要任何新授权（§4），`#917` 立的探针语义一字未动（§5），反例换回
作者描述的形态可以复现红（§7）。没有发现阻塞合并的缺陷，§8 的九项都是「可接受但需记录」：其中三项是文案/
阈值表述精度（迁移 docstring 的 `ORDER BY` 说法在测试自己用的数据规模上不成立、失效阈值写成单一数字而非
区间+触发条件、超时后果被描述成就绪失败而实际是探针变盲），其余是测试覆盖面与运维注记。

---

## 0. diff 口径：两点会把 `#922` 的文档改写算成本 PR 的删除

按要求用两点 `git diff origin/main 220b4770`，得到 9 文件 +349/−176；三点
`git diff origin/main...220b4770 --stat` 是 5 文件 +349/−1。差额的四个文档不是本 PR 的改动，而是 `origin/main`
在本 PR 的 fork 点之后前进的结果：

| 口径 | 结果 |
| --- | --- |
| `git merge-base origin/main 220b4770` | `c83ac803` |
| `git diff c83ac803 origin/main --name-only` | `deploy/private/README.md`、`docs/operations/async-job-runbook.md`、`docs/operations/builtin-voice-qualification.md`、`docs/operations/compose-minimal-from-zero.md`、`docs/operations/compose-minimal.md`（PR #922, `3b1bbf5e`） |
| `git diff c83ac803 220b4770 --name-only` | `0056_debt_probe_live_state_index.py`、`readiness.py`、`docs/operations/compose-minimal.md`、`test_debt_probe_index_migration_pg.py`、`test_debt_probe_plans_pg.py` |

所以两点口径里的 −176 有 −175 是 `#922` 对四份运维文档的重写被反向计数，只有 −1 属于本 PR（runbook 里
`0055` → `0056` 那一行）。**合并不会删除任何文档**：`git merge-tree --write-tree origin/main 220b4770`
退出码 0、无冲突，说明两处对 `compose-minimal.md` 的改动不重叠（`origin/main:490` 仍是
`仓库当前迁移链的 head 为 \`0055_operator_attestation\``，本 PR 把它改成 `0056_debt_probe_live_state_index`，
而 `#922` 改的是同一份文档的其它段落）。

## 1. 索引与两条查询的实际匹配（含 `ORDER BY` 方向）

实际落库的索引定义（`pg_indexes.indexdef`，1M Job 表上跑完 `0056` 后读出，不是抄迁移文案）：

```
CREATE INDEX speech_jobs_live_state_scan ON <schema>.speech_jobs USING btree
    (state, updated_at_us, job_id)
    WHERE ((state)::text = ANY ((ARRAY['running'::character varying,
                                        'cancelling'::character varying])::text[]))
```

读到的三条相关 SQL 是（`backend/src/speechnexus_backend/speech/infrastructure/readiness.py`）：

| SQL | 位置 | 状态谓词 | 排序列 | 谓词蕴含索引谓词 |
| --- | --- | --- | --- | --- |
| `_DEBT` | `:388` | `j.state = 'cancelling'` | `ORDER BY f.accepted_at_us, f.job_id, l.lease_id`（`:410`，表 `speech_job_cancellation_fences`） | 是 |
| `_JOB_LEVEL_DEBT` | `:369` | `j.state = 'cancelling'` | `ORDER BY f.accepted_at_us, f.job_id`（`:378`） | 是 |
| `_STOP_EVIDENCE_DEBT` | `:431` | `j.state = 'running'` | `ORDER BY j.updated_at_us, j.job_id`（`:462`） | 是 |

`_JOB_LEVEL_DEBT` 不是死代码：`readiness.py:228` 在读取身份缺租约/执行表授权时降级走它，它同样用上了
新索引（`Index Only Scan`），降级路径也跟着受益。

**计划证据（每次 `ANALYZE`，取三次最小值，1M 行级 `speech_jobs`/`admission_leases`/`submit_intents` 等
真实 DDL 上跑原 SQL，非改写版）：**

| 形态 | `_STOP_EVIDENCE_DEBT` | `_DEBT` |
| --- | --- | --- |
| 有索引，held=64 | 0.08 ms，`Index Only Scan`，**无 Sort** | 0.11 ms，`Index Only Scan`，Sort(30) |
| 有索引，held=2 000 | 3.00–3.33 ms，`Bitmap Index Scan`，**有 Sort** | 0.12 ms，`Index Scan`，Sort(30) |
| 有索引，held=20 000 | 39.1–44.1 ms，`Index Only Scan`，**无 Sort** | 0.14 ms，`Index Only Scan`，Sort(30) |
| 有索引，held=50 000 | 110.9–116.9 ms，`Index Only Scan`，**无 Sort** | 0.12 ms |
| 索引删掉，held=20 000 | 58.6 ms，`Seq Scan on speech_jobs` | 28.4 ms，`Seq Scan on speech_job_cancellation_fences` |

谓词和列序都对得上：`state` 是等值前缀，`updated_at_us`、`job_id` 是紧随其后的两个排序键，正好覆盖
`ORDER BY j.updated_at_us, j.job_id`；`_DEBT` 的 `ORDER BY` 首选列在围栏表上，所以索引只负责 `state` 谓词，
计划改成从 `j` 起头用索引、再按主键连围栏，这正是把围栏全表扫换成「只读活集」的那一步——代价随「活集」
（也就是债务本身）而不是随围栏历史走，见下方与 §7 的对照。

### 1.1 `ORDER BY` 方向：没有影响，但混向会掉到 `Incremental Sort`

两条查询的排序键都是同向 ASC，btree 正向扫即可。用同样的索引形状在独立表上验三组（`state`,
`updated_at_us`, `job_id` 全 ASC 的部分索引，`WHERE state='running'`，`LIMIT 96`）：

| `ORDER BY` | 计划 |
| --- | --- |
| `updated_at_us, job_id`（ASC, ASC，即现状） | `Index Only Scan`，无 Sort |
| `updated_at_us DESC, job_id DESC`（同向降序） | `Index Only Scan`，无 Sort（退化为反向扫） |
| `updated_at_us DESC, job_id ASC`（混向） | `Incremental Sort` + `Index Only Scan` |

结论：默认 ASC 索引对现状和「整条反过来」都免排序；只有混向排序键会退化到增量排序。现状不受影响，
但将来若把排序改成混向，免排序就不成立了，界面上仍受 `LIMIT 96` + 活集规模约束。

### 1.2 迁移 docstring 的 `ORDER BY` 说法过于绝对（文案级）

`0056_debt_probe_live_state_index.py:27-29` 写的是

> ``updated_at_us`` follows ``state`` so ``stop_evidence_debt``'s ``ORDER BY updated_at_us, job_id`` can read
> a range of live rows in its own order instead of sorting them, and ``job_id`` closes the ordering key.

这只在活集大到规划器选有序 `Index Only Scan` 时成立。在**本 PR 自己的测试数据规模上（`_HELD = 2_000`，
`test_debt_probe_plans_pg.py:50`）不成立**：计划是 `Bitmap Index Scan` + `Sort`（3.00–3.33 ms 里有
2.96 ms 在 Sort 上）。held=64 同理。held ≥ 20 000 时才变成无 Sort 的有序索引扫。差别不影响结论
（两种计划的代价都随活集走，不随历史走），但 docstring 是永久记录，措辞应加一句限定
（例如「once the planner picks the ordered scan — measured from ~20 000 held leases」）。

## 2. `state` 取值的字面量漂移风险

`speech_jobs.state` 是 `VARCHAR(16) COLLATE "C"` 加 `CHECK` 约束，**不是 PG 枚举**
（`public_job_schema.py:44-46`）。同一组取值在仓库里有四处独立副本：domain 的 `JobState` Literal 与
`JOB_STATES` 元组（`domain/public_jobs.py:20-31`）、表的 `CHECK`、`0034` 迁移里的 `_NEW_STATES`/`_OLD_STATES`、
以及 `0056` 索引谓词里写死的 `'running', 'cancelling'`。

**已被钉住的部分：**

- 索引 DDL 字面量钉在 `test_debt_probe_index_migration_pg.py:39-42`（精确匹配
  `speech_jobs USING btree (state, updated_at_us, job_id)` 与 `'running'`/`'cancelling'`/`WHERE`）。
- 漂移实际发生时会红：若 `running` 改名而索引谓词没跟着重建，`state='<新名>'` 不再蕴含
  `state IN ('running','cancelling')`，索引不可用 → `test_debt_probe_plans_pg.py:219`
  的 `speech_jobs must not be sequentially scanned` 立刻失败。所以「查询常量改了、索引没改」这一半是新测试
  兜住的。

**没被钉住的部分（真实、但很小）：** 若将来**新增**一个活状态（例如 `draining`），索引谓词不会自动包含它，
也不会有人去看——现有两条查询都不过滤它，因此没有任何用例会红。同一文件对同类漂移是有先例的：
`_SQL_VERIFIED_STOP_EVIDENCE`（`readiness.py:423`）从 `VERIFIED_STOP_EVIDENCE_KINDS` 派生，注释明说
「a second literal here would drift, and drift in this direction is silent」。索引谓词做不到从常量派生
（DDL 是静态的），但测试可以从 `JOB_STATES` 派生断言「活状态集合 ⊆ 索引谓词集合」。这是建议，不是本 PR 的
缺陷。

## 3. 迁移 `0056` 的安全性

**单事务、`CONCURRENTLY` 确实非法。** `migrations/env.py:19` 是 `with connection.begin():`（另有
`:21` 的 `pg_advisory_xact_lock`），`CREATE INDEX CONCURRENTLY` 不允许在事务块内执行。作者的理由成立。

**锁与窗口（1 000 000 Job + 200 000 围栏 + 2 030 活行的临时 schema 上实测，不是推算）：**

- `CREATE INDEX` 在持有事务里耗时 **47.5 ms**，与作者自述的 37–131 ms 相符（迁移 docstring 写 37–49 ms）。
- `pg_locks` 显示 `speech_jobs` 上 `mode=ShareLock, granted=true`。`SHARE` 与 `ROW EXCLUSIVE` 冲突，
  所以**阻塞写入、不阻塞读取**——用一个并发线程实测：事务未提交前那条 `INSERT` 一直不返回，提交后立即完成
  （`blocked while lock held: True`）。
- 写阻塞窗口 = 整个迁移事务（`0056` 里只有这一条语句），即上面的 47.5 ms。表每翻一倍这个数近似线性增长，
  10M Job 量级约 0.5 s；当前规模可以忽略，但值得在 runbook 里点名一次。
- `0056` 用普通 `CREATE INDEX` 而非 `CONCURRENTLY`，与 `0048_voice_evidence_scan_index` 的先例一致
  （同样是 `CREATE INDEX` + 部分谓词 + 同一事务内的迁移运行器）。

**可逆性：真的可逆。** 用 alembic 走 `head → 0055_operator_attestation → head`：downgrade 后
`alembic_version = 0055_operator_attestation` 且索引不存在（`to_regclass(...) IS NULL` 为真）；再 upgrade 后
`alembic_version = 0056_debt_probe_live_state_index` 且 `indexdef` 与首次逐字相同。这条路径还是**在有 1M 行
存量数据时**走的，也就是说「在已 populating 的大表上加索引/回滚」两个方向都验过。新增的
`test_debt_probe_index_migration_pg.py` 覆盖的正是这个序列，head 上实测 `1 passed`。

**head 唯一性。** 解析 `versions/` 的 `revision`/`down_revision` 依赖图：55 个版本、**唯一 head
`0056_debt_probe_live_state_index`**，`down_revision = 0055_operator_attestation`，且 0055 只被 0056 引用，
没有分支、没有第二 head，`alembic_version` 仍是单行。

**与 runbook 的 head 断言一致，而且是机器钉住的。**
`tests/ci/test_compose_minimal_role_provisioning.py:156` 的
`test_the_runbook_states_the_current_migration_head` 从版本图算出 head，要求
`docs/operations/compose-minimal.md` 含 ``仓库当前迁移链的 head 为 `<head>` ``；本 PR 把 `:490` 改成了
`0056_debt_probe_live_state_index`，head 上实测 `1 passed`。`DEL-537` 报告里的 `0055` 是该报告写作时点的事实，
不冲突。`deploy/job-startup-gate.py` 用同一张版本图算镜像内 head 与库内 `alembic_version` 比对，所以
`0056` 不需要改闸门——但**旧 worker 镜像对 `0056` 库会拒绝启动**，这是既有迁移约束（发布说明需带），
不是本 PR 引入的。

**一处小注记：** `upgrade()` 用的是不带 `IF NOT EXISTS` 的 `CREATE INDEX`。若某个部署自己手工预建过同名
索引（性能排查时很常见的做法），迁移会失败——失败即整链回滚、状态一致，安全但需要运维知道该先确认。

## 4. 权限面

两个探针读的是 `SPEECHNEXUS_READINESS_CANCELLATION_DSN` 指向的专用只读身份，由
`cancellation_debt_reader_grants`（`job_cancellation_fence_schema.py:107-147`）授权，共 7 条语句，**全部是
列级授权**，其中 `speech_jobs` 只有
`GRANT SELECT (job_id, state, updated_at_us)`（`:135`）。该文件不在本 PR 的 diff 里。

实测（不是推理）：新建一个临时角色只施加这 7 条授权，然后**以该角色**连库跑两条原 SQL：

| 检查 | 结果 |
| --- | --- |
| `has_table_privilege(role, speech_jobs, 'SELECT')` | `false` |
| `has_any_column_privilege(role, speech_jobs, 'SELECT')` | `true` |
| 该角色 `EXPLAIN (ANALYZE)` `_DEBT` | OK，0.216 ms，`Index Only Scan on speech_jobs using speech_jobs_live_state_scan` |
| 该角色 `EXPLAIN (ANALYZE)` `_STOP_EVIDENCE_DEBT` | OK，3.228 ms，`Bitmap Index Scan using speech_jobs_live_state_scan` |
| 该角色读 `pg_indexes` | 可读 |

所以「索引无需 GRANT，`*_grants` 不动」成立：PostgreSQL 对索引不设权限，部分索引谓词由执行器内部求值、
不触发列权限检查；本 PR 也没有引入 `EXPLAIN` 权限需求或对 `pg_stat*` 的依赖。列级授权的
`(job_id, state, updated_at_us)` 恰好覆盖新计划需要的全部列（索引扫甚至只需要索引本身）。

## 5. 探针语义未变

`readiness.py` 的 diff 是 **+10 / −0**（`git diff --numstat origin/main 220b4770`），十个新行全是注释
（`_DEBT` 之上 5 行、`_STOP_EVIDENCE_DEBT` 之上 5 行），**没有任何删除行**
（`git diff … | grep -c '^-[^-]'` = 0）。逐项核对：

- 三条 SQL 文本未改（`_DEBT`、`_JOB_LEVEL_DEBT`、`_STOP_EVIDENCE_DEBT`）。
- detail 文案与上限未改：`_DETAIL_MAX_CHARS = 512`（`readiness.py:80`）、`_bounded_detail`、
  `CANCELLATION_DEBT_PREFIX` / `STOP_EVIDENCE_DEBT_PREFIX`、两个 `_describe_*` 的句式全未触碰。
- 探针永不失败的既有性质（`#917` 立的）仍成立：`check()` 的超时/异常分支未改，
  `tests/integration/test_cancel_failure_slot_pg.py::test_the_debt_probe_never_fails_a_readiness_report`
  在 head 上实测 `1 passed`。
- 迁移文件只新增一个 `CREATE INDEX`，没有 DDL 之外的副作用（不改列、不改 `CHECK`、不动授权）。

唯一「越界」的是新加的注释本身：它们把 §1.2 那个过强的机制描述又复述了一遍
（`readiness.py:382-387` 与 `:427-430`）。这是同一个文案问题，不是行为问题。

## 6. 失效阈值可信度：方向对，数字偏乐观，且「后果形态」需要改

**实测斜率**（100 000 Job 表，`settled = 50 000`–`100 000` 历史取消，held 变化，索引在位）：

| held | `_STOP_EVIDENCE_DEBT` | µs/持有租约 |
| --- | --- | --- |
| 64 | 0.08 ms | 1.23 |
| 2 000 | 3.00–3.22 ms | 1.61 |
| 20 000 | 39.1–44.1 ms | 1.96 |
| 50 000 | 110.9–116.9 ms | 2.22 |

区间斜率：64→2 000 是 **1.62 µs/条**，2 000→20 000 是 **1.99**，20 000→50 000 是 **2.39**。所以

1. 作者「≈1.2–2 µs/条」在低端对、高端偏乐观：最高已到 2.4 µs/条，严格增（超线性），不是「正比」。
   迁移 docstring 里「0.5 ms / 34 ms / 101 ms 成正比」也应按实测写成「约 1.2–2.4 µs/条，随规模缓慢上升」。
2. 2.4 s 探针预算（`readiness_timeout_seconds` 默认 3.0 s × `_PROBE_BUDGET_FRACTION` 0.8，
   `readiness.py:76`）按实测斜率在 **≈1.0M 持有租约**处吃满；按作者取的 2 µs 才是 1.2M。
   所以「≈120–200 万」落在乐观一侧 0–2 倍，且是跨 20–40 倍的线性外推（合成均匀数据），
   上界不可当作事实。
3. **后果形态要改。** 超时不是「就绪红」：探针自己的 `asyncio.timeout` 先触发，`check()` 捕获后返回
   `cancellation_debt: unavailable (timeout)`，`ProbeStatus` 仍是 `OK`——`/readyz` 保持绿。也就是说越过预算的
   真实损失是**在最该看清单的时候把清单换成 `unavailable (timeout)`**，不是把 API 拉出服务。这个差别决定了
   阈值应该定在「还能看见」而不是「还没超时」。

**建议写法（区间 + 触发条件，替代单一数字）：** 「残余代价 ≈1.2–2.4 µs/条持有租约（100k Job 表实测，
随规模缓慢上升）。复查触发：持有租约 ≥ 5 万（实测 111 ms，占 2.4 s 预算 4.6%），或探针 p99 > 预算的 10%
（≈240 ms，≈10 万条），或 `readiness_timeout_seconds` 调到 1 s 以下（预算按 0.8 倍同比缩放）。」
作者选的「≥5 万」这个触发点本身很保守、可接受；该删的是「120–200 万吃满预算」这个不可核验的绝对值，
或至少标注它是外推。

## 7. 反例重做

### 7.1 反例一：把索引从 `0056` 里去掉

改动：`0056_debt_probe_live_state_index.py` 的 `upgrade()` 换成 `pass`（其余不动），跑两个新用例。

| 侧 | 结果 |
| --- | --- |
| 有索引（原样） | `2 passed in 1.47s` |
| 去掉索引 | `2 failed in 1.44s` |

两侧的失败原文（去掉后）：

- `tests/integration/test_debt_probe_plans_pg.py:202`：
  `AssertionError: speech_jobs must not be sequentially scanned: ['Seq Scan']`（由 `:219` 触发）
- `tests/integration/test_debt_probe_index_migration_pg.py:36`：`assert None is not None`

作者自述的反例成立。**一处覆盖缺口：** 计划用例在第一条断言（`:219`，`_STOP_EVIDENCE_DEBT` 不得顺序扫
`speech_jobs`）就中止了，`:225`/`:226` 那两条「不得顺序扫围栏表/对账表」在负例方向**从未被执行到**。我单独
补了这条负例：删掉索引后 `_DEBT` 的计划确实是 `Seq Scan on speech_job_cancellation_fences`，代价
**6.19 / 31.73 / 67.57 ms**（settled = 20k / 100k / 400k），索引在位时是 **0.12 / 0.12 / 0.13 ms**。
这就是「随上线时长无界增长」那一条的真实度量：线性随历史涨，加了索引后压成常数。

### 7.2 反例二：列序换成 `(updated_at_us, state, job_id)`

只改 `CREATE INDEX` 的列序，跑同样两个用例：

| 用例 | 结果 |
| --- | --- |
| `test_debt_probe_plans_pg.py` | **1 passed** —— **计划用例分辨不出列序** |
| `test_debt_probe_index_migration_pg.py:39` | **1 failed** —— `assert 'speech_jobs USING btree (state, updated_at_us, job_id)' in definition` 失败，实际是 `(updated_at_us, state, job_id)` |

所以「列序是否 load-bearing」的答案是分层的：

- **对「免 Seq Scan」不是 load-bearing。** 反序后两条查询仍是
  `Index Only Scan using speech_jobs_live_state_scan`、仍然没有 `Seq Scan`，计划用例的断言全过
  （held=20 000：stop 38.5 vs 44.1 ms，cancel 0.24 vs 0.14 ms，甚至不比正序差——因为部分索引只装活行，
  反序退化成「扫完活集」，量级仍是 O(债务)）。
- **对「两个探针代价互不耦合」是 load-bearing。** 正序把 `state` 当等值前缀，`_DEBT` 直接定位到
  `cancelling` 那几十行，`cancellation_debt` 的代价与 `running` 积压无关；反序必须先走遍全部活行。
  held=20 000 / cancelling=30 时是 0.24 vs 0.14 ms，差距随 `running` 积压放大。作者选正序是对的。
- 这条性质目前**只被 DDL 字符串相等钉住**（`test_debt_probe_index_migration_pg.py:39`），而不是被行为断言
  钉住；一旦有人为了「优化」把列序换掉，红的是那条字符串断言，失败信息不解释为什么列序重要。

### 7.3 反例三（补充）：部分谓词只留 `state='running'`

`WHERE state = 'running'`（丢掉 `cancelling`）时，`_DEBT` **完全失去索引**：0.12 ms → 6.47 ms，计划回到
`Seq Scan on speech_job_cancellation_fences`。所以「两个状态写进同一条部分谓词」是 load-bearing 的，
这条会红。

## 8. 必须修 / 可接受但需记录

**必须修（阻塞合并）：无。**

**可接受但需记录（建议在合并前后处理，都不阻塞）：**

1. **迁移 docstring 的 `ORDER BY` 机制描述过强**（`0056_…py:27-29`，并在 `readiness.py:382-387`、
   `:427-430` 复述）。在测试自己用的规模（held=2 000、held=64）上计划是 `Bitmap Index Scan` + `Sort`，
   并不是「不再排序」；held ≥ 20 000 才是无 Sort 的有序扫。建议加规模限定，或改成「当规划器选有序扫时」。
2. **失效阈值应写成区间 + 触发条件。** 实测 1.2→2.4 µs/条且严格增，`1.2–2M` 是跨 20–40 倍的外推且偏乐观
   （按 2.4 µs/条是 ≈1.0M）。建议按 §6 的写法替换，保留「≥5 万持有租约」这个触发点。
3. **超时后果的表述要改。** 越过预算时 `/readyz` 仍是绿、detail 变成 `unavailable (timeout)`
   （`readiness.py:255-266` 的 `_failure_category` + `check()` 的 `except`），所以这是「探针变盲」而不是
   「就绪失败」。阈值和 runbook 的告警口径都应按前者描述。
4. **`state` 活状态集合缺一条派生断言。** domain 常量与索引谓词各写一份；新增第三个活状态时静默失效
   （`running` 改名会被 `test_debt_probe_plans_pg.py:219` 兜住，新增不受兜）。建议测试从 `JOB_STATES` 派生
   断言「活状态 ⊆ 索引谓词」。
5. **计划用例的负例可达性。** 删掉索引后 `:219` 就中止，`:225`/`:226` 的围栏断言在负例方向不可观测
   （见 §7.1）。建议把两组断言拆成两个用例。
6. **列序只被 DDL 字符串钉住**（§7.2）。要么给这条断言补一行「为什么」，要么补一条行为断言把
   「`_DEBT` 不随 `running` 积压增长」钉住。
7. **`CREATE INDEX` 无 `IF NOT EXISTS`。** 手工预建过同名索引的部署会让迁移失败（失败即整链回滚，安全）。
   建议 runbook 加一句排查：「迁移报 `relation … already exists` 先确认是否手工建过同名索引」。
8. **SHARE 写阻塞窗口随表线性增长。** 1M Job 实测 47.5 ms（阻塞写、不阻塞读）；当前可忽略，规模上去后
   应在 runbook 点名。
9. **旧 worker 镜像对 `0056` 库拒绝启动**（`deploy/job-startup-gate.py` 比对镜像内 head）。属既有迁移约束，
   发布说明需带；本 PR 不引入新风险。

## 9. 复核记录

| 检查 | 命令 / 方式 | 结果 |
| --- | --- | --- |
| 环境 | `UV_NO_CONFIG=1 pnpm install --frozen-lockfile`；`UV_NO_CONFIG=1 uv sync --locked --all-packages --all-groups` | 均退出码 0 |
| 新增用例 | `pytest tests/integration/test_debt_probe_plans_pg.py tests/integration/test_debt_probe_index_migration_pg.py -q` | **2 passed**（1.47 s / 1.42 s） |
| 探针永不失败 | `pytest tests/integration/test_cancel_failure_slot_pg.py::test_the_debt_probe_never_fails_a_readiness_report -q` | 1 passed（9.73 s） |
| runbook head 断言 | `pytest tests/ci/test_compose_minimal_role_provisioning.py::…::test_the_runbook_states_the_current_migration_head -q` | 1 passed |
| 迁移单 head | 解析 `versions/*.py` 的 `revision`/`down_revision` | 55 版本，唯一 head `0056_debt_probe_live_state_index` |
| 索引定义（落库） | `SELECT indexdef FROM pg_indexes` | `(state, updated_at_us, job_id) WHERE (state)::text = ANY (ARRAY['running','cancelling']::text[])` |
| 计划匹配 | `EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)` 跑原 SQL，多种 held 规模 | 见 §1 表 |
| 方向 | 同形状部分索引 + `ORDER BY` 三态 | ASC/ASC 与 DESC/DESC 免排序，混向 `Incremental Sort` |
| 无界增长（负例） | 删索引后 `_DEBT` 在 settled=20k/100k/400k | 6.19 / 31.73 / 67.57 ms（有索引：0.12 / 0.12 / 0.13 ms） |
| 迁移锁 | 1M Job 大表、事务内 `CREATE INDEX` + `pg_locks` + 并发写线程 | 47.5 ms；`ShareLock`；写入阻塞至提交 |
| 可逆性 | alembic `head → 0055 → head`（库内有 1M 行存量） | 版本号与 `indexdef` 均逐字还原 |
| 权限面 | 临时角色 + 恰好 7 条 `cancellation_debt_reader_grants`，以该角色跑两条 SQL | 表级 SELECT 无、列级有；两条 SQL 均用上新索引；无新授权需求 |
| 权限面清理 | `DROP ROLE`、`DROP SCHEMA … CASCADE` | 临时 schema/角色均已移除 |
| 反例一 | 索引从迁移里去掉 | `2 passed` → `2 failed`（见 §7.1） |
| 反例二 | 列序换 `(updated_at_us, state, job_id)` | 计划用例 1 passed / 迁移用例 1 failed（见 §7.2） |
| 反例三 | 部分谓词只留 `state='running'` | `_DEBT` 失去索引，回到围栏 Seq Scan，6.47 ms |
| 篡改还原 | `git status --porcelain` | 空 |
| 合并可行性 | `git merge-tree --write-tree origin/main 220b4770` | 退出码 0，无冲突 |
