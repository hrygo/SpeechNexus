# 隔离租约运维释放的非作者审查（#635 / #646）

- **审查对象**：`60b70d57`（`feat: 承接隔离租约运维释放端点 (#635) (#637)`），父提交 `9fe27b19`，基线 `origin/main` head `60b70d57`。
- **审查分支**：`review/646-quarantine-release`，独立 worktree `/private/tmp/sn646`（`git worktree add /private/tmp/sn646 -b review/646-quarantine-release origin/main`）。
- **审查者**：非作者。作者是 `60b70d57` 的提交者；本审查未读取作者的推理过程，只读代码、契约、设计和 issue/PR 文字，其余结论由本机实测得出。
- **审查日期**：2026-11-13。
- **审查方式**：只读静态审查 + 隔离 PostgreSQL 16.15 容器实跑 + 删守卫反例复现。**未改动产品代码**；临时改动用 `git checkout --` 逐个还原，`git status` 在每次实验后为 0 dirty。

---

## 结论

**有条件通过。**

八条清单全部给出结论：`release()` 单份实现、单事务、锁序与文档一致、三处投影在任何失败点都整笔回滚、两种竞争顺序结论确定、权限面逐列精确等式未被绕过、错误映射没有把实现违约包装成「可重试的 503」。**未发现阻塞性代码缺陷**。

但本轮独立验证推翻了设计文末两条测试强度声明（`§9.3` 反例 #1、#9 的「删掉就变红」不可复现），并确认实现与设计在步骤 5 的锁形态上互相矛盾；同时该端点在生产制品中没有任何可执行的启用路径。这些不阻塞既有代码运行，阻塞「这一片可被下一位维护者安全地继续修改」和「已交付」这一验收表述。四条必须修复项见[缺陷清单](#缺陷清单)，全部是文档/装配面，不需要改事务、不需要改 schema。

---

## 时间线

| 时点 | 动作 | 结果 |
| --- | --- | --- |
| T0 | `git worktree add /private/tmp/sn646 -b review/646-quarantine-release origin/main` | worktree 建在 `60b70d57`，与主工作区、`sn644`、`sn645` 隔离 |
| T1 | `docker run ... postgres:16.15-alpine`，`127.0.0.1:55446` | 隔离实例，未复用宿主机或其他审查容器的库 |
| T2 | `uv sync --all-packages` | 首次 `uv run` 只装了 dev 依赖，`speechnexus_contracts` 不可导入；显式 sync 后可用。`uv.lock` 未变 |
| T3 | `pytest tests/integration/test_quarantine_release.py -q` | **42 passed in 8.02s**（基线绿，确认作者声明的用例数） |
| T4 | `log_statement=all` 抓 SQL 顺序 | 得到释放事务的真实语句序列（见 §1） |
| T5 | 逐条删守卫实验（6 组，见 §7） | 3 组红、3 组仍绿，与作者的声明部分不符 |
| T6 | 库内故障注入（账本 INSERT 失败） | 三处投影与证据全量回滚，同键重试成功 |
| T7 | 权限面探针（schema 内 sequence 授权） | 启动期逐列等式**不报错**，见 §6 |
| T8 | `tests/contract` 与单测回归 | 249 passed |
| T9 | 写报告、提交、推分支、开 PR | 见文末 |

---

## 环境与复现命令

```bash
git worktree add /private/tmp/sn646 -b review/646-quarantine-release origin/main
docker run -d --name sn646-pg -e POSTGRES_PASSWORD=sn646 -e POSTGRES_USER=sn646 \
  -e POSTGRES_DB=sn646 -p 127.0.0.1:55446:5432 postgres:16.15-alpine
cd /private/tmp/sn646
uv sync --all-packages
export SPEECHNEXUS_TEST_DATABASE_URL='postgresql://sn646:sn646@127.0.0.1:55446/sn646'
uv run pytest tests/integration/test_quarantine_release.py -q          # 42 passed
uv run pytest tests/contract/test_admin_http_bindings.py tests/contract/test_operational_contract.py \
  tests/contract/test_p0_public_contracts.py tests/contract/test_profile_http_bindings.py \
  tests/unit/backend/test_quarantine_release_domain.py \
  tests/unit/backend/test_quarantine_release_http.py -q                # 249 passed
uv run python scripts/dev-env/check-route-conformance.py               # OK
python3 scripts/dev-env/check-doc-refs.py                              # RESULT PASS
```

容器与临时探针在收尾时删除；`uv.lock` 在提交前 `git checkout --`。

---

## 逐条清单结论

### 1. 锁序：与文档声明的全局顺序完全一致

**结论：通过。** 实测语句序（`log_statement=all`，PID 237，`test_release_settles_every_projection_once`）：

```text
BEGIN ISOLATION LEVEL READ COMMITTED
SET TRANSACTION READ ONLY                                   # ← 第 1 级（level-1）只读事务
  SELECT ... FROM admission_quarantine_releases WHERE lease_id = $1
COMMIT
BEGIN ISOLATION LEVEL READ COMMITTED
  SELECT * FROM admission_leases WHERE lease_id = $1                    # 无锁 hint
  SELECT * FROM admission_attempts WHERE operation_id = $1 AND ...      # 无锁 hint
  SELECT pool_id FROM resource_pools        ... FOR UPDATE    -- 1 resource_pools
  SELECT * FROM workspace_quotas            ... FOR UPDATE    -- 2 workspace_quotas
  SELECT * FROM host_nodes                  ... FOR UPDATE    -- 3 host_nodes
  SELECT * FROM engine_instances            ... FOR UPDATE    -- 4 engine_instances
  SELECT * FROM admission_attempts          ... FOR UPDATE    -- 5 admission_attempts
  SELECT * FROM admission_leases            ... FOR UPDATE    -- 6 admission_leases
  SELECT ... FROM admission_quarantine_releases WHERE lease_id = $1     # 主键普通 SELECT，不取锁
  SELECT ... FROM submit_intents ... FOR UPDATE               -- 7 submit_intents
  SELECT clock_timestamp()
  INSERT INTO execution_stop_evidence (...)
  UPDATE submit_intents SET execution_state='aborted', ...               # CAS
  -- 以下进入 release_in_transaction：重复取 1–6 的行锁（本事务已持有，不等待）
  SELECT * FROM submit_intents ...                                      # 非锁定重读
  SELECT ... FROM execution_stop_evidence
  SELECT clock_timestamp()
  SELECT * FROM admission_leases WHERE operation_id = $1 AND attempt_id = $2
  UPDATE workspace_quotas / host_nodes / engine_instances / admission_attempts / admission_leases
  INSERT INTO admission_quarantine_releases (...)
COMMIT
```

对照 `docs/components/production-quarantine-release.md:233-243` 声明的顺序
`resource_pools → workspace_quotas → host_nodes → engine_instances → admission_attempts → admission_leases → submit_intents`：
实际取锁顺序逐位相同，**没有任何分支提前取后面的锁**。三个细节值得记录：

- 账本的二级读（`quarantine_release_sql.py:316`）落在租约锁之后、`submit_intents` 锁之前，取的是**普通 SELECT**，不参与锁序。与文档字面（`production-quarantine-release.md:267` 写的是 `SELECT ... FOR UPDATE`）不一致，见[缺陷清单 B1](#b1-设计与实现在步骤-5-的锁形态上互相矛盾)。
- 同一表内排序满足「按字典序」约束：`submit_intents ... ORDER BY submit_intent_id`（`:92`）、siblings `ORDER BY lease_id`（`lease_release_sql.py:161-166`）。
- 锁序确实是跨 owner 共享的：网关 `submit_sql.py:614-625` 取 `resource_pools → admission_leases → submit_intents`。因此把 `submit_intents` 提到 `resource_pools` 之前是真会与网关成环的，不只是理论问题——但这条目前没有测试守护，见 §7 的 `#9` 实验。

### 2. 事务边界：确实只有一个写事务，且没有事务外写

**结论：通过。** 证据：

- 写事务只有一处 `BEGIN`（上表）。第 1 级是 `SET TRANSACTION READ ONLY` 的独立事务，PostgreSQL 保证它写不了。
- 事务外的计算只有 `prepare_evidence()`（`quarantine_release_sql.py` 之外的纯函数：RFC 8785 规范摘要、键摘要），不含数据库访问；写事务内所有判定值（hint、锁后重读、`clock_timestamp()`）都在同一事务内取得。第 1 级的结果只被用作「有终态答案就直接返回」的快路径，**不参与第 2 级的任何写判定**——第 2 级在锁后重读账本并重做整套判定（`quarantine_release_sql.py:316-339`）。
- 提交点是连接上下文退出的 `Connection.__exit__`。`psycopg_pool` 的 `ConnectionPool.connection()` 明确「Apply the normal connection context behaviour (commit/rollback the transaction in case of success/error)」（实读 `psycopg_pool/pool.py` 源码确认），因此：
  - `_DeadlineCursor.execute` 抛 `_BudgetExpiredError`（预算耗尽）与 `commit()` 第 123-124 行的同名校验都发生在 `with` 块内 → **确认回滚** → 映射为可重试的 `unavailable` 是诚实的；
  - COMMIT 本身失败 → `OperationalError` → `QuarantineUnknownError`（`quarantine_release_postgres.py:139-140`），不会被当成「未提交」重放。
- 重试只对数据库已确认回滚的 `DeadlockDetected`/`SerializationFailure` 生效（`:133`），且这条 `except` 排在 `psycopg.OperationalError` **之前**——顺序是对的：psycopg3 里 `TransactionRollback` 系（含 40P01/40001）继承自 `OperationalError`，一旦把两个 `except` 写反，死锁就会被误判成 `unknown` 而永不重试。这里没写反。
- 「已提交被误判为未提交导致重复释放」不成立：即使适配器真的返回 `unknown`，同键重试命中账本（`test_lost_commit_acknowledgement_is_unknown_until_queried`），不同键同证据命中分支 D 只读重放，租约行锁 + 账本主键再兜一次。

### 3. 错误映射：没有把实现违约包装成可重试的 503，但有一条把存储故障说成业务冲突

**结论：通过（带 2 条非阻塞意见）。** 逐个 5xx/异常出口追到触发条件：

| 出口 | 位置 | 触发条件 | retryable | 判断 |
| --- | --- | --- | --- | --- |
| 503 `quarantine_release_unavailable` | `quarantine_release_postgres.py:137-138`、`:143` | `PoolClosed`/`PoolTimeout`/`TooManyRequests`/`_BudgetExpiredError`；重试次数耗尽 | `true` | 事务未开始或已确认回滚，诚实 |
| 503 `quarantine_release_unavailable` | `interfaces/http/quarantine_releases.py:171-172` | `service is None`（DSN 未配置 / lifespan 未打开） | `true` | 无副作用；但永久的装配缺失被报成可重试，见 §3-注 2 |
| 503 `quarantine_release_unavailable` | `quarantine_release_postgres.py:95-98` | level-1 只读事务的池/连接错误 | `true` | 只读，无副作用，诚实 |
| 503 `quarantine_release_unknown` | `quarantine_release_postgres.py:139-140` | `psycopg.OperationalError`/`InterfaceError`（连接中断、COMMIT 确认丢失、语句被取消） | `false` | 结果不明，保守正确 |
| 503 `quarantine_release_unknown` | `interfaces/http/quarantine_releases.py:149-151` | 服务层显式 unknown | `false` | 正确 |
| 503 `quarantine_release_unknown` | `interfaces/http/quarantine_releases.py:152-153`（`except Exception`） | **任意未预期异常**，包括 `_wire()`/`validate_response()` 的实现自身违约 | `false` | 不是「可重试 503」，方向安全，但把自身违约说成「结果不明」，见 §3-注 1 |
| 409 `quarantine_release_conflict` | `quarantine_release_postgres.py:130-132` | `LeaseRejectedError("reconciliation_required")`、`not_found` 以外的全部内部不一致 | `false` | 违约没有伪装成 5xx |
| 409 `quarantine_release_conflict` | `quarantine_release_postgres.py:99-100`、`:141-142` | `psycopg.Error`/`ValueError`/`TypeError`/`KeyError` | `false` | 见 §3-注 3 |
| 400/404/409 | `quarantine_release_errors.py:10-46` + `interfaces/http/quarantine_releases.py:123-130` | 输入、未命中、证据冲突 | `false` | 与契约 `contracts/admin-openapi.yaml` 的映射一致；`test_error_catalogue_covers_every_mapped_code` 守住码表 |

关键点：**本轮没有找到「实现自身违约 → 可重试 503」的路径**。所有内部不变式（冻结字段不一致、`rowcount != 1`、版本耗尽、账本行不一致）都走 `LeaseRejectedError`/`ValueError` → 409，`retryable` 恒为 `false`；两个 503 里只有真正「可能重试成功」的那一个标了 `true`。`retryable` 的唯一来源是 `quarantine_release_errors.py:53` 的 `code == "quarantine_release_unavailable"`，没有第二条捷径。

三条注记（都不阻塞）：

1. `except Exception → unknown`（`interfaces/http/quarantine_releases.py:152-153`）会把响应校验失败、`_wire()` 的编程错误也说成「事务结果不明」。方向是安全的（客户端会先去查，不会盲重），但违反了设计 §7.2 对 `unknown` 的定义（「事务结果不明（提交确认丢失、连接中断）」）。建议至少把 `ContractValidationError` 从这条分支里分出来，或在该分支加一条结构化日志标识「实现违约」。
2. DSN 未配置时返回 `unavailable` + `retryable: true`，运维脚本会无限轮询一个永远不会自愈的装配缺失。与仓库既有可选端点的做法一致，不算本片引入，但值得在 `docs/operations/` 里点明「持续 503 先查 `SPEECHNEXUS_QUARANTINE_RELEASE_DATABASE_URL` 是否配置」。
3. **存储故障被说成业务冲突**：`lookup()` 的 `except psycopg.Error`（`:99-100`）与 `commit()` 的同名分支（`:141-142`）把 `UndefinedTable`、`InsufficientPrivilege`、`QueryCanceled` 等全部映射成 `409 quarantine_release_conflict`，而契约与运行手册把 409 定义为「证据不匹配 / 租约未隔离 → 换证据或转人工」（`docs/operations/reconciliation-recovery.md:224`）。启动期逐列校验把「表缺失 / 授权偏差」挡在启动前，所以可达性低；但授权在运行期被回收、表被 `CASCADE` 掉之后，运维会收到一个语义上完全错误的 409。这与本阶段定性的缺陷模式（把 bug 伪装成别的结论）同族，只是伪装方向不是 outage 而是 conflict。

### 4. 容量守恒：`release()` 只有一份实现，最后一个失败点也全量回滚

**结论：通过。** 逐项：

- **复用是否唯一**：是。`quarantine_release_sql.py:344-346` 调 `release_in_transaction(...)`，后者调 `domain/lease_release.py:105` 的 `release()`。全仓库没有第二份容量算术；本片新增的只有步骤 6–9（前置条件、时钟、证据、意图 CAS）和步骤 12（账本）。
- **三处投影**：`workspace_quotas`/`host_nodes`/`engine_instances` 在 `lease_release_sql.py:213-252` 更新，`admission_attempts`/`admission_leases` 在 `:253-265` 更新，每条都过 `_update()`，`rowcount != 1` 抛 `reconciliation_required`（`leases_sql.py:73-76`）。证据 INSERT 在投影之前（`:342`），账本 INSERT 在投影之后（`:349`），三者同事务。

**我做的失败注入**（未改产品代码，只加测试侧触发器）：在 `admission_quarantine_releases` 上加一个 `BEFORE INSERT ... RAISE EXCEPTION` 的触发器，让**最后一步**（账本 INSERT，此时停止证据已写、意图已 CAS 关闭、三处投影已 UPDATE）失败：

```text
结果：QuarantineReleaseRejectedError('quarantine_release_conflict')
     db.projections() == 注入前快照        # 含所有 version 列
     count(admission_quarantine_releases) == 0
     count(execution_stop_evidence) == 0
     移除触发器后同一请求重放 → idempotent_replay=False, 账本 1 行
```

即：**最坏的失败点也不存在「扣了容量但没释放」或「释放了但证据没落」**。补充约束：账本表上的第二个外键 `FOREIGN KEY (lease_id, operation_id, attempt_id) REFERENCES admission_leases (lease_id, operation_id, attempt_id)`（`quarantine_release_schema.py:49-50`，目标唯一约束在 `coordination_schema.py:226`）在 INSERT 时对父行取 `FOR KEY SHARE`，与租约的 `FOR UPDATE` 冲突，因此**即便是绕过应用直接写账本，也必须排在租约行锁之后**——这比文档说的「主键是第二道防线」还多一层。

### 5. 幂等与竞争：两种顺序结论确定；步骤 5 的普通 SELECT 替代是安全的

**结论：通过。**

- **两种顺序都确定**：`test_operator_first_makes_the_consumer_replay`、`test_consumer_first_makes_the_operator_evidence_inapplicable`、`test_evidence_split_release_still_replays_read_only`、`test_operator_and_consumer_racing_the_same_lease_never_double_charge` 均绿。消费者先释放时，运维请求落到分支 F（`quarantine_release_sql.py:106-147`）逐字段比对已存释放事实；`evidence_kind` 是常量 `operator_verified_instance_terminated`（`domain:26`），机器证据的种类必然不同，所以**恒为 409**，与设计 §6.2 一致。
- **步骤 5 的 `SELECT ... FOR UPDATE` → 普通 SELECT 是否安全：安全**，理由三条，均已核实：
  1. **账本只有一个写入者**：`grep` 全仓 `INSERT INTO admission_quarantine_releases` 只命中 `quarantine_release_sql.py:199`，而该语句在步骤 12 执行，此时事务已持有 `admission_leases` 行的 `FOR UPDATE`（`:291-295`）。同租约的两个写事务在第 6 位串行化，后者重读时必然看到前者已提交的行（READ COMMITTED 逐语句取新快照），因此不会出现「两个写者都不看账本就写」。
  2. **主键是第二道防线**：`lease_id` 是主键（`quarantine_release_schema.py:25`），并发插入的第二个会拿到 `UniqueViolation`，在 `quarantine_release_postgres.py:128-129` 映射成 409，不会产生第二条释放记录。
  3. **`FOR UPDATE` 本来也做不到**：账本表对释放角色只有 `SELECT + INSERT`（`quarantine_release_schema.py:68-102`，`UPDATE_COLUMNS` 无该表），而 PostgreSQL 的 `SELECT ... FOR UPDATE` 需要至少一列 `UPDATE` 权限。想要 `FOR UPDATE` 就得授予 `UPDATE`，那会直接破掉 append-only 的授权前提。**实现选择正确，错的是设计文档仍写着 `FOR UPDATE`**（见 B1）。
- 一个附带发现：`_branch_f` 返回的 `ReleaseLedger` 是用**请求**里的四元组构造的（`:137-147`），不是从库里回读的。这在本轮不构成伪造，因为它刚用 `evidence_matches_stored` 逐字段证明四个字段与库内行相等；但这是一处「信任链靠前一行断言维持」的写法，建议改成用 `stop[...]` 的库值构造，免得将来有人删掉那行比对。

### 6. 权限面：逐列精确等式有效，两处覆盖盲区

**结论：通过（带 2 条非阻塞意见）。**

- **能否被 owner / 超级用户绕过**：不能。`_require_non_owner`（`quarantine_release_runtime.py:67-85`）同时拒绝 `rolsuper`、schema `CREATE`、数据库 owner 成员、schema 内任一 relation owner 成员；`_verify_no_role_switch`（复用 `binding_disable_worker.py:200-235`）拒绝任何 `SET`/`MEMBER WITH ADMIN OPTION` 可达角色与全部 `pg_` 前缀预定义角色（含 `pg_read_all_data`）。`test_runtime_rejects_a_privileged_identity` 用迁移 owner 直接验证拒绝。
- **PostgreSQL 默认权限**：`ALTER DEFAULT PRIVILEGES` 或 `PUBLIC` 授予只会**增加**实际权限，而 `_require_minimum_surface` 是 `bool(rd) != can_read` 的**双向等式**（`:117-124`），多给即拒。`test_one_extra_column_grant_fails_startup` 实测有效；我把等式放宽成「只要有」后该用例立刻红（§7 实验 5）。
- **`refuse_*` 系列有没有 `and not ...` 把该拦的放过**：逐个读了 `startup_privileges.py`（本片未改该文件）。四个函数的短路条件都是 `if row is None or row["held"]: raise`，方向一致地 fail-closed；`refuse_foreign_relation_privileges` 用 `has_table_privilege(..., 'A,B,...')`（PostgreSQL 语义是「任一」）**并** `has_any_column_privilege(...)`，因此表级和列级两种越权都能命中。未发现漏判写法。

两处覆盖盲区（都不影响释放事务的不变式）：

- **schema 内的 sequence 不在等式覆盖面内**。`_require_minimum_surface` 的列枚举只取 `relkind IN ('r','p','v','m')`（`:108`），`refuse_foreign_relation_privileges` 取 `('r','p','v','m','f')`，两者都漏掉 `'S'`（序列）。**实测**：给释放角色额外 `GRANT USAGE, SELECT ON SEQUENCE "<schema>"."<seq>"`，`verify_quarantine_release_role` **不报错**（探针用例通过）。函数 `EXECUTE` 同理未覆盖。由于释放事务不需要任何序列，这不构成可利用的越权，但设计 §8.2「任一列多给或少给都终止启动」的边界应写明是「表/视图/物化视图/外表」。
- **只检查配置 schema 内的关系**：`public` 或其他 schema 上的散授权不会被发现。影响面小，建议在设计里写清「边界是 schema + 声明表清单」。

### 7. 测试强度：3 条反例独立复现成功，另有 3 组「删了也不红」

作者在 #635 的交付评论里只写了「另如实记录 4 个守卫单独移除不改变对外结论」，**没有列出这 4 个守卫的名字**（PR #637 正文亦无），所以下面的集合是我自己实测出来的，不保证与作者的那 4 个一一对应。

**独立复现成功的反例（先删、再跑、确认真红）：**

| # | 删掉的守卫 | 定向结果 | 红在哪 |
| --- | --- | --- | --- |
| A | `domain/quarantine_release.py:288-289` 的 `observed_at` 上下界 | 3 failed | `test_observed_at_bounds_are_server_decided[recorded_minus_one/dispatched_minus_one/future]`，`assert 'quarantine_release_conflict' == 'invalid'`（`tests/integration/test_quarantine_release.py:681`）；单测 `test_observed_at_bounds_use_recording_dispatch_and_server_time` 同红 |
| B | `quarantine_release_sql.py:316-318` 的第 2 级账本重读 | 1 failed | `test_concurrent_same_key_and_different_evidence_has_one_winner`，`['committed','quarantine_release_conflict'] != ['committed','idempotency_conflict']`（`:1128`）——并发落败方掉到分支 F，返回了错误的 409 码 |
| C | 迁移里的 append-only 触发器 DDL（`0052_quarantine_releases.py:upgrade`） | 4 failed | `test_appended_ledger_row_cannot_be_rewritten`、`test_disabled_append_only_trigger_fails_startup`、`test_release_role_matches_the_declared_surface`、`test_assembled_app_releases_through_its_own_runtime` |
| D | 启动期逐列等式放宽成「只要有哪些」（`quarantine_release_runtime.py:117-124`） | 1 failed | `test_one_extra_column_grant_fails_startup`：`DID NOT RAISE ValueError` |
| E | `require_releasable` 的意图状态前提（`domain:272-273`） | 42 passed | **不红**，见下 |
| F | `_close_intent` 的 `execution_state/cleanup_state/version` CAS（`:178-180`） | 42 passed | **不红**，见下 |
| G | 锁后冻结字段重核对（`:310-315`） | 42 passed | **不红** |
| H | `result.outcome != "committed"` 守卫（`:347-348`） | 42 passed | **不红** |
| I | 锁序反转：把 `submit_intents` 的 `FOR UPDATE` 提到 `resource_pools` 之前 | 42 passed ×2 | **不红**，见下 |

**关于「删了也不红」的判断：**

- **E/F 是一对互为兜底的纵深防御，不是死代码。** 单独删任一条都 42 绿：删 F 时 `require_releasable` 在写之前就挡住非 `unknown_quarantined` 意图；删 E 时 `_close_intent` 的 CAS（`rowcount != 1`）兜住。**但两条一起删就红**：`test_lease_or_intent_outside_quarantine_conflicts[executing]` 报 `DID NOT RAISE QuarantineReleaseRejectedError`——也就是会把仍在 `executing` 的意图关闭并释放容量。所以这对守卫是真正承重的，判决：**合理纵深防御**。这也意味着设计 §9.3 反例 #1 的表述（「删掉步骤 9 的 CAS → 用例 8 变红」）单独不成立，必须写成「同时删掉领域前提与 CAS」。
- **G/H 是纯冗余，判决：可接受的冗余，但掩盖了一个真实缺口。** 冻结字段在准入之后没有任何写入路径，`hint` 与锁后行不可能不同；`release_in_transaction` 在同一事务里已把租约读成 `quarantined`，`outcome` 只可能是 `committed`。这两处删掉后没有任何可观测差异，说明「锁后重核对」这条设计约束目前**没有测试能证明其存在**——它只在设计文档和 reviewer 的脑子里。
- **I 是本次最重要的覆盖缺口。** 设计 §9.3 反例 #9 声称「把 `submit_intents` 的写提到 `resource_pools` 之前 → 用例 20 变红」，实测两轮全绿：用例 20（`test_concurrent_sibling_stages_do_not_double_return_quota`）里两个事务锁的是**不同**的 `submit_intents` 行，反转后各自先锁自己的意图行、再争 pool，不存在环。真正会成环的对象是网关（`submit_sql.py:614-625` 取 `resource_pools → admission_leases → submit_intents`），而测试套件里没有任何用例跨这两条路径。也就是说：**代码的锁序目前是正确的，但删掉它不会红**。这类缺陷不会在 CI 里暴露，只会在生产并发下变成死锁。判决：**这不是「合理纵深防御」，是一个不被观测的关键不变量**，应补一条用例（见[建议改进](#建议改进不阻塞)）。

### 8. 断言强度：总体强，两处名不副实

**结论：通过（带 2 条非阻塞意见）。**

做得好的地方（逐条核过断言体）：

- `test_release_settles_every_projection_once`（`:409-457`）对三处投影**同时断言取值和 version**，对证据行断言条数与 `reason`/`evidence_kind`，对账本断言行数——不是「存在即过」。
- `test_same_key_same_evidence_replays_without_a_write` 用 `replay.ledger == first.ledger`（dataclass 全字段相等）+ `projections() == snapshot`（含 version）——是值比较，不是存在性比较。
- 所有「零写入」用例都用 `db.projections()`（六张表的显式列投影，含 version）做前后快照，`test_ledger_refuses_incoherent_evidence_rows` / `test_appended_ledger_row_cannot_be_rewritten` 用 `pytest.raises(psycopg.errors.CheckViolation)` 精确到 SQLSTATE 类。
- 权限负例同时覆盖「多给一列」和「少给一列」（`:809-855`），不是只测一个方向。

两处与名字不符：

1. **`test_operator_first_makes_the_consumer_replay`（`:565`）断言了 `budget_released_at` 与投影不变，但没有断言消费者真的走 `replayed` 分支**。用例名里的「replay」只由投影不变间接支持；把消费者改成「重算一次但不改投影」也能通过。应补 `assert result.outcome == "replayed"`（`LeaseResult.outcome` 已可用）。
2. **`test_evidence_split_release_still_replays_read_only`（`:577`）只断言 `idempotent_replay is True` 和零写入，没有断言回放返回的 `budget_released_at` / `released_submit_intent_ids` 与已存释放事实相等**——分支 F 的全部价值就在「返回原事实」，而这条没有校验取值。

另外 `test_lost_commit_acknowledgement_is_unknown_until_queried`（`:1022`）是**服务级 fake**（在真正 commit 之后由包装层抛 `QuarantineUnknownError`），不是真实的「COMMIT 已发出、确认丢失」。它证明的是「unknown 之后按原键查询能拿到重放」这一业务结论，没有证明适配器把「确认丢失」分类成 unknown。适配器那条分支与 `test_terminated_backend_leaves_no_partial_release` 覆盖的 `except OperationalError` 是同一处代码，因此间接覆盖，但设计 §9.2 #17 的表述应降级为「服务级模拟」。

---

## 缺陷清单

### 必须修复（阻塞）

> 四条都不需要改事务、schema 或权限。它们阻塞的是「这一片可被安全继续修改」和「已交付」的验收表述，而不是当前代码的运行。

#### B1. 设计与实现在步骤 5 的锁形态上互相矛盾

- **位置**：`docs/components/production-quarantine-release.md:267`（步骤 5 写 `SELECT ... FOR UPDATE`）vs `backend/src/speechnexus_backend/speech/infrastructure/quarantine_release_sql.py:69-83,316`（普通 `SELECT`）。
- **核实**：查看权限声明 `quarantine_release_schema.py:107-133`（账本表无 `UPDATE`），可以确认作者在 #635 评论里的说法成立——**同一个设计文档既要求 `FOR UPDATE` 又要求账本表只有 `SELECT+INSERT`，两者在 PostgreSQL 上不可能同时成立**。
- **为什么阻塞**：协调写路径的锁语义以设计文档为准。下一位维护者要么按文档去加 `FOR UPDATE`（然后被迫授予 `UPDATE`，破掉 append-only），要么相信代码而不知道文档过期。这不是笔误，是一处未闭合的决策。
- **最小改法**：把步骤 5 改成「持有租约行锁后普通 `SELECT` 重读」，并补一句「`FOR UPDATE` 需要账本 `UPDATE` 权限，与 append-only 最小授权互斥；串行化点由第 6 位的租约行锁提供，主键与外键是第二、三道防线」。

#### B2. 设计 §9.3 的两条反例声明与实测不符

- **位置**：`docs/components/production-quarantine-release.md:632`（反例 #1：删 CAS → 用例 8/18/19 红）、`:640`（反例 #9：锁序反转 → 用例 20 红）。
- **实测**：删 CAS 后 42 passed（§7 实验 F）；锁序反转后 42 passed ×2（§7 实验 I）。
- **为什么阻塞**：这两条是「反例先红后绿」这一验收证据的组成部分。一条不可复现的反例声明比没有声明更危险——它让 review 与 CI 都以为该不变量有守护。
- **最小改法**：反例 #1 改为「同时删除领域前提与 CAS 才红」，并写明单独任一条不可观测；反例 #9 改为「当前无用例覆盖，锁序由 review 守护」，同时补一条覆盖用例（见 S1）。

#### B3. 释放角色在生产制品里没有启用路径

- **位置**：`quarantine_release_grants()`（`quarantine_release_schema.py:136-158`）除测试外**无任何调用点**；`deploy/compose.env.example` 里没有 `SPEECHNEXUS_QUARANTINE_RELEASE_DATABASE_URL`（其余 20 个专用 DSN 都在，均为 `KEY=` 空值形态）；`docs/operations/compose-minimal.md` 只更新了迁移 head，没有说这个端点如何启用。
- **核实**：`grep -rn quarantine_release_grants` 仅命中 schema 定义与 `tests/integration/test_quarantine_release.py`；`grep SPEECHNEXUS_QUARANTINE_RELEASE_DATABASE_URL deploy/ docs/` 无命中。
- **为什么阻塞**：设计把「角色 + 独立 DSN + 列级授权」定义为本端点可用性的四条不变量之一（设计开头），而实现卡没有交付任何可执行的安装路径。加上启动期校验是 fail-closed，实际部署里这个端点只会永远返回 `503 quarantine_release_unavailable`，「已交付」在这一项上不成立。
- **最小改法**：在 `deploy/compose.env.example` 增加空值 DSN 条目，并在 `docs/operations/compose-minimal.md` 写清「迁移 owner 调用 `quarantine_release_grants(schema, role)`、创建 LOGIN、配置 DSN」三步，以及未配置时的 503 语义。

#### B4. `docs/operations/reconciliation-recovery.md:338-339` 在同一文件内自相矛盾

- **位置**：同一文件的表格行（`:224`）已改为「已装配」，`§5.1`（`:240-242`）已改为「由 admin `quarantine-releases` 承接」，但 `:338-339` 仍写「只声明了 quarantine release 契约；**生产实现和 `quarantine_release_*` 列当前缺失**」。
- **核实**：`git diff 60b70d57^ 60b70d57 -- docs/operations/reconciliation-recovery.md` 只动了描述表格行和 §5.1，这一行未动。
- **为什么阻塞**：这是运行手册，运维会按它判断「该端点不可用、只能升级」。同一文件两种相反结论，等于把 B3 的缺口再放大一次。
- **最小改法**：删掉该条或改为「已装配，见上表；未配 DSN 时返回 503 `quarantine_release_unavailable`」。

### 建议改进（不阻塞）

| # | 项 | 依据 | 建议 |
| --- | --- | --- | --- |
| S1 | 锁序没有守护用例 | §7 实验 I：反转后 42 绿 | 加一条用例：一个会话按网关顺序持 `resource_pools` 行锁，另一个会话发起释放，断言释放会话**先**等待 pool 而不是先拿 `submit_intents`（可用 `pg_stat_activity.wait_event_type='Lock'` 与 `_wait_for_lock_wait` 现有手法）；死锁方向用两个会话加 `statement_timeout` 判红 |
| S2 | 存储/授权故障被映射成业务 409 | §3-注 3 | 把 `UndefinedTable`/`InsufficientPrivilege`/`QueryCanceled` 从 409 分支里分出，映射为 503（`unavailable` 或新码），避免运维把基础设施故障读成「证据冲突」 |
| S3 | 预算耗尽的两种终局码不一致 | §3 表：语句内超时 → `unknown(retryable=false)`，语句间超时 → `unavailable(retryable=true)`，服务端状态相同 | 统一成一种结论，或在设计 §7.2 注明「两者都安全，`retryable` 只是提示」 |
| S4 | 启动期逐列等式不覆盖序列/函数 | §6 探针：sequence 授权不报错 | 设计 §8.2 把边界写成「schema 内的表/视图/物化视图/外表」，或把 `'S'` 也纳入枚举 |
| S5 | `host_nodes` 的 12 个维度列在授权清单里是**硬编码**，未取自 `coordination_grants.DIMENSIONS` | `quarantine_release_schema.py:110-124` vs `coordination_grants.py:13-20` | 改从 `DIMENSIONS` 派生。现状：将来给 `DIMENSIONS` 加一维，`release_in_transaction` 的 `UPDATE host_nodes` 会多写一列 → `InsufficientPrivilege` → 被 `except psycopg.Error` 吞成 409，而**启动期等式仍会通过**（角色确实没有该列），故障因此是静默的 |
| S6 | `_branch_f` 用请求值构造回放账本 | §5 附带发现 | 改用库内 `stop[...]` 值构造，减少对前一行断言的隐式依赖 |
| S7 | 两处断言与用例名不符 | §8 | `test_operator_first_makes_the_consumer_replay` 补 `result.outcome == "replayed"`；`test_evidence_split_release_still_replays_read_only` 补对 `budget_released_at` / 意图列表的取值断言 |
| S8 | 「4 个不可观测守卫」未具名 | #635 交付评论 | 在 issue 或设计 §9.3 下列出具体守卫与实测方式，让后来者能区分「冗余」与「未被观测」 |
| S9 | InnoDB 无对应 oracle | 设计 §11 已声明 PG-only（`~`、`jsonb_typeof`、plpgsql 触发器） | 保持声明即可，但应在 `docs/architecture/coordination-transactions.md` 的端口清单里标出「本端口无 InnoDB 等价实现」，避免双库门禁被读成已覆盖 |

---

## 我独立验证的反例（原始记录）

全部在 `/private/tmp/sn646`，每次实验前 `cp` 备份、实验后 `git checkout --`，`git status` 归零后再进行下一组。

### A. `observed_at` 上下界（对应设计反例 #4）

```bash
python3 - <<'EOF'   # 把 require_observed_at 的 if 判据整段删成 return
backend/src/speechnexus_backend/speech/domain/quarantine_release.py:288-289
EOF
uv run pytest tests/integration/test_quarantine_release.py -q
```

```text
E  AssertionError: assert 'quarantine_release_conflict' == 'invalid'
tests/integration/test_quarantine_release.py:681
FAILED ...[recorded_minus_one-invalid]
FAILED ...[dispatched_minus_one-invalid]
FAILED ...[future-invalid]
3 failed, 39 passed
```

单测同红：`test_observed_at_bounds_use_recording_dispatch_and_server_time` → `DID NOT RAISE`。

### B. 第 2 级账本重读（对应设计反例 #2/#3 的并发面）

```bash
# 删除 quarantine_release_sql.py:316-318 的
#   ledger = _decide(read_ledger(cursor, evidence.lease_id), evidence)
#   if ledger is not None: return ledger
```

```text
E  AssertionError: assert ['committed','quarantine_release_conflict'] == ['committed','idempotency_conflict']
tests/integration/test_quarantine_release.py:1128
FAILED test_concurrent_same_key_and_different_evidence_has_one_winner
1 failed, 41 passed
```

说明：这条守卫是**可观测的**，但它守的是 409 的**码**（`idempotency_conflict` vs `quarantine_release_conflict`），不是容量。

### C. append-only 触发器（对应设计反例 #6）

```bash
# 0052_quarantine_releases.py 的 upgrade(): 去掉 QUARANTINE_RELEASE_TRIGGER_DDL 循环
```

```text
FAILED test_release_role_matches_the_declared_surface
FAILED test_disabled_append_only_trigger_fails_startup
FAILED test_appended_ledger_row_cannot_be_rewritten
FAILED test_assembled_app_releases_through_its_own_runtime
4 failed, 38 passed
```

### D. 启动期逐列精确等式（对应设计反例 #8）

```bash
# 把 quarantine_release_runtime.py:117-124 的双向等式改成「只要声明需要的都在」
```

```text
E  Failed: DID NOT RAISE ValueError
tests/integration/test_quarantine_release.py:824
FAILED test_one_extra_column_grant_fails_startup
1 failed, 41 passed
```

### E/F. 意图状态前提与 CAS 成对（设计反例 #1 的实测修正）

```text
删 domain:272-273（require_releasable 的意图态前提）        → 42 passed
删 sql:178-180（_close_intent 的 CAS 判据）                 → 42 passed
两条一起删                                                → 1 failed
  Failed: DID NOT RAISE QuarantineReleaseRejectedError
  FAILED test_lease_or_intent_outside_quarantine_conflicts[executing]
```

### I. 锁序反转（设计反例 #9 的实测修正）

```bash
# 把 intents = _intents(cursor, evidence.lease_id, locked=True)
# 从 lease 锁之后整体移到 resource_pools 锁之前
uv run pytest tests/integration/test_quarantine_release.py -q   # 连跑两轮
```

```text
42 passed in 7.74s
42 passed in 7.75s
```

### 库内故障注入（不涉及产品代码改动）

在账本表挂 `BEFORE INSERT ... RAISE EXCEPTION` 触发器，让步骤 12 失败：`quarantine_release_conflict`、`projections() == snapshot`、账本 0 行、证据 0 行；移除触发器后同请求重放成功且只入账一次（详见 §4）。

### 权限探针（不涉及产品代码改动）

在目标 schema 内建序列并 `GRANT USAGE, SELECT ON SEQUENCE ... TO <release role>`，`verify_quarantine_release_role` **不报错**（详见 §6）。

---

## 未验证项（只能依赖作者陈述或 CI）

| 项 | 原因 | 影响 |
| --- | --- | --- |
| `generate-contract-bindings.py --check`（契约生成物漂移） | 独立 worktree 没有 `node_modules`，`normalize-typescript-headers.mjs` 报 `Cannot find module 'openapi-typescript'`；未在审查中执行 `pnpm install` | 我用 `tests/contract` 的 249 条相关用例（含 `test_profile_http_bindings` 的 version 与 bindings 断言）间接核对，未发现漂移，但没有跑生成器本身 |
| `0.1.0.dev10 → dev11` 的 HTTP 结构变更检查 | 依赖 `--base origin/main` 的基线脚本与生成环境 | 变更本身是纯追加（补 `503` + `released_submit_intent_ids.maxItems: 1`），我逐行读了 diff，未见破坏性字段改动 |
| 真实部署角色上的 `GRANT` 安装 | 本机没有部署库；测试用临时角色 | 与设计卡的未验证项一致，本片**没有**关闭它；这正是 B3 |
| 真实 COMMIT 确认丢失 | 只能靠 `pg_terminate_backend`（事务中途）与服务级 fake | 适配器的 `OperationalError → unknown` 分类在终止场景下被覆盖；「COMMIT 已发出、确认丢失」仍是模拟 |
| 真实网关进程对 `aborted/complete` 的后续读取 | 需要跑网关 | 设计 §1.3 的「迟到引擎回执被版本 CAS 拒绝」是代码推论，未在真实网关进程上实测；本片也没有跨 owner 的并发用例 |
| InnoDB 行为 | 设计 §11 明确 PG-only | 双库协调门禁不覆盖本端点 |
| 真实多机/HA 下的死锁概率 | 单容器、单机 | 锁序分析为静态 + 单实例并发用例 |
| `observed_at` 在真实运维时钟偏差下的 400 占比 | 无真实运维流量 | 设计 §11 已声明未测 |

---

## 收尾

- 隔离容器 `sn646-pg` 与临时探针文件（`tests/integration/test_zz_*.py`）已删除；`uv.lock` 已 `git checkout --`。
- 产品代码零改动：`git diff --stat` 只有本报告。
- 本报告是过程记录，保留时间线、复现命令与实测输出；不作为终态设计文档。
