# PR #720 非作者审查：COMMIT 分类修复（服务端 SQLSTATE 不被清理额度截断）

> 过程记录文档：保留审查时间线、逐条探针与原始输出，不做返修。审查者非作者。
> 审查对象：PR #720 `fix/718-commit-classification`，head `94258c1b264f1fbd28531b2bcb97e89c95f84ed4`，base `7e09b4db`（= 审查时点 `origin/main`），修 #718。
> 工作区：独立 worktree `/private/tmp/sn721`（分支 `review/721-commit-classification` @ `94258c1b`，审查结束时 HEAD 未动）；对照树 `/private/tmp/sn721-pre`（detached @ `7e09b4db`，只在其上放"新测试 + 旧源码"的变异体）；全部注入、探针与 pytest 插件写在仓库外的 `/private/tmp/review721`，未 push。
> 环境：macOS arm64、uv 0.13.5、CPython 3.14.9、PostgreSQL 18.6（Homebrew）。隔离集群为仅 Unix socket、`listen_addresses=''`、`speechnexus.disposable_workbench_probe='on'`、port 55439、db `speechnexus_workbench_test`、role `workbench_test_owner`，与 `tests/integration/test_workbench_versions_postgres.py` fixture 的身份断言（`("speechnexus_workbench_test","workbench_test_owner","","on")`）逐字一致。
> 主检出停在陈旧分支 `docs/tracker-batch12`（`a7508635`），本报告所有源码结论取自上述两个 worktree。

## 结论

**定性成立，修法方向正确且安全，可以合并。** 十条清单里 8 条判定「成立」，2 条判定「成立但不完整」（A2 的可达路径枚举、B4 的外部取消），没有把「确定性拒绝」误判成「结果未知」的新风险，也没有把「真正的未知」误判成确定的新风险。

必须修只有一条，且是**文档级、非阻断合并**：`version_ports.py:30-35` 新写的 `commit_unknown` 定义比现有实现更窄，与该异常类型的另一个生产者（`postgres_content_writes.py:107,143,184` 出于业务状态主动报 `commit_unknown=True`）以及「提交在飞时被取消」这条路径都不吻合。这条定义是本次修复新加的规范性文字，写窄了会让下游误读，建议同批收口。

| # | 清单项 | 判定 | 一句话证据 |
| --- | --- | --- | --- |
| A1 | 故障注入与独立复现 | **成立** | 我自己写触发器注入：0.5 s → `23503`/`False`；1.5 s（修复前）→ `None`/`True` + `__context__=TimeoutError("workbench transaction exit deadline")`，耗时 1.005 s 正好卡在 1 s 清理额度 |
| A2 | `sqlstate=None + commit_unknown=True` 可达路径是否只有三条 | **成立但不完整** | 找到了第四条（提交在飞时被外部取消 → `CancelledError` 直接逃逸，不产生本异常）与第五条（`committing` 早于 COMMIT 发出即置位 → 保守假 unknown）；两者都不产生本签名，故对 CI 红的定性无影响 |
| B3 | 提交等待是否变成无界 / 预算耗尽的行为 / 调用方是否重试 | **成立** | 上界 = 剩余事务预算（默认 10 s，已扣 connect≤5 s 与业务体）；11 s 延迟实测 10.002 s 返回 `None`/`True` 不挂死；应用层无自动重试，`version_ports.py:28-29` 明文禁止 |
| B4 | 取消传播的分类 | **成立但对一种取消不安全（非新增）** | 内部预算取消→unknown（正确）；外部取消→`CancelledError`，与「提交前取消」异常类型相同、无法区分，且本修复把该窗口从 ≤1 s 放大到 ≤10 s；安全性由幂等回执兜住（实测取消后 `mutation_receipts` 已落盘，重放可取回原结论） |
| B5 | `cleanup_timeout` 是收窄而非删除 | **成立** | 全仓仅 3 处使用点（构造器、校验、`_exit_transaction` 的默认额度），回滚/清理路径仍用它；`_exit_transaction` 全仓唯一实现，其它 workbench 存储（含 `PostgresDeletionUnitOfWork`）复用同一 UoW，无旁路 |
| B6 | 反向风险：晚到答案被误当「确定中止」 | **成立** | `23*` 整类 + `40001`/`40P01` 恰是「服务器在 COMMIT 上能给出且必然回滚」的码；`40003`/`57P01`/`08006`/`57014`/`55P03` 仍归 unknown，我逐码实测 |
| C7 | 既有断言未被放宽 | **成立** | 改动 3 文件 `+152/-3`，集成用例 0 行改动；`assert error.value.sqlstate == "23503"` 与 `assert not error.value.commit_unknown` 原样 |
| C8 | 新单测咬住修复 | **成立（但仅 1/3 是回归护栏）** | 回退变异后第一条即红在 `assert None == '23503'`（CI 同一句），另两条在旧源码上也通过 |
| C9 | 三条互斥情形实跑 | **成立** | 晚到 `23503`→`23503`/`False`；提交期间连接断开→`57P01`/`True`；预算耗尽→`None`/`True`；三者互斥 |
| D | 改动范围、`uv.lock`、CI | **成立** | 3 文件全在 `workbench/` + 该单测；`uv.lock` 未入 diff；head 上 20 项检查全绿 |

## 1. A1：定性成立（我自己注入，不看作者的注入）

我先写了一个与作者无关的注入：`DEFERRABLE INITIALLY DEFERRED` 约束触发器里 `pg_sleep`，再触发一个真实的延迟外键违反，让服务器在 COMMIT 上**确定地回答** `23503`。探针 `/private/tmp/review721/a1_probe.py` 建全新 schema + runtime role，走真实 `WorkbenchVersions.create_project`。

修复前（`/private/tmp/sn721-pre` @ `7e09b4db`）：

```text
=== PRE-FIX delay=0.5 ===
{"delay":0.5,"result":"VersionStoreError","sqlstate":"23503","commit_unknown":false,
 "context_chain":["psycopg.errors.ForeignKeyViolation: ..."],"elapsed_s":0.506}
=== PRE-FIX delay=1.5 ===
{"delay":1.5,"result":"VersionStoreError","sqlstate":null,"commit_unknown":true,
 "message_has_23503":false,
 "context_chain":["builtins.TimeoutError: workbench transaction exit deadline"],"elapsed_s":1.005}
```

修复后（`/private/tmp/sn721` @ `94258c1b`，同一探针、同一集群）：

```text
{"delay":0.5,...,"sqlstate":"23503","commit_unknown":false,"elapsed_s":0.507}
{"delay":1.5,...,"sqlstate":"23503","commit_unknown":false,"elapsed_s":1.508}
{"delay":2.5,...,"sqlstate":"23503","commit_unknown":false,"elapsed_s":2.508}
```

三点值得记下：`elapsed_s=1.005` 精确等于 1 s 清理额度，说明截断点是 `cleanup_timeout` 而不是事务预算；`__context__` 就是 `_exit_transaction` 那条 `TimeoutError("workbench transaction exit deadline")`（`postgres_versions.py:239`），不是传输层；修复后 1.5 s 与 2.5 s 都被采纳为确定拒绝。

**更强的证据：不改任何仓库测试**。我写了一个 pytest 插件 `/private/tmp/review721/slowreply.py`，只把 psycopg `transaction()` 的**提交退出**延迟 1.5 s（回滚退出不延迟），这正是"COMMIT 的答案在 1 s 清理额度内不可见"。用它在**原封不动的集成用例**上：

```text
=== PRE-FIX: real CI test with a 1.5s late COMMIT answer ===
FAILED tests/integration/test_workbench_versions_postgres.py::test_database_failure_rolls_back_header_revision_and_receipt
>       assert error.value.sqlstate == "23503"
E       AssertionError: assert None == '23503'
E        +  where None = VersionStoreError('workbench store commit uncertain').sqlstate
tests/integration/test_workbench_versions_postgres.py:367: AssertionError
1 failed, 35 deselected in 1.11s

=== POST-FIX: same test, same 1.5s late COMMIT answer ===
1 passed, 35 deselected in 1.59s
```

即 issue #718 里那条 CI 红（`AssertionError: assert None == '23503'`、`VersionStoreError('workbench store commit uncertain')`）在同一文件同一行被确定性复现，并在此修复下转绿。

在**无延迟**下它并不 flaky：修复后 30/30 通过（`1 passed, 35 deselected in 0.10s`），修复前 20/20 通过。这与作者「修复前 0/100 失败」的说法一致——红需要"服务端答复晚于 1 s"这个条件，不是随机竞态。

## 2. A2：定性正确，但"只有三条路径"不完整

作者把 `sqlstate=None + commit_unknown=True` 的成因归为三条（`_exit_transaction` 的 TimeoutError、外层 10 s 预算的 TimeoutError、传输层丢包）。就**这个签名**而言，在 `PostgresVersionUnitOfWork.transaction()` 里我确认只有这三类（`:303-313`：`committing` 为真且 SQLSTATE 不属"已证中止"）。但审查要求找第四条，确实存在，只是不产生该签名：

- **第四条（外部取消）**：`committing=True` 后，调用方 `task.cancel()` 落在 `await asyncio.wait(...)` 上 → `CancelledError` 不在 `except (psycopg.Error, TimeoutError)` 里，也不是外层 `asyncio.timeout` 的自我取消 → 原样逃逸出 `transaction()`。此时 COMMIT 的答案同样被丢弃、结果同样不可判定，但调用方拿到的是 `CancelledError`，既没有 `sqlstate` 也没有 `commit_unknown`。实测见第 4 节：取消落在 COMMIT 上时**数据真的提交了**，调用方只看到 `CancelledError`。
- **第五条（保守假 unknown）**：`:292` 先 `committing = True`，`:296-298` 才发起等待。若外层预算恰在这一空档到期（或 `budget.when()` 已过期使 `allowance=0.0` 立即 `TimeoutError`），COMMIT 可能压根没发出，却仍报 unknown。方向保守（不会把已提交说成失败），但"unknown"比事实更悲观一层。

两条都不改变 #718 的定性：CI 红是"服务器已答复却被丢弃"，属于作者归纳的第一、二类。但"可达路径只有三条"这句话在报告/后续 issue 里不应原样引用。

## 3. B3：提交等待的上界、耗尽行为与重试

**上界不是新增超时，而是外层事务预算的剩余额度。** `budget = asyncio.timeout(self._timeout)`（`:252`）、`deadline=budget.when()`（`:297`）、`allowance = max(0.0, deadline - loop.time())`（`:228`）。生产装配（`backend/src/speechnexus_backend/workbench/runtime.py:556`）只传 `schema` 与 `max_connections`，因此 `transaction_timeout` 取默认 **10 s**（`:186`），`cleanup_timeout` 取默认 1 s（`:189`）。关键推论：**10 s 是从进入预算开始的整笔事务，connect（`connect_timeout=5`）、`SET ROLE NONE`、三条 `set_config`、业务体都吃这份额度**，所以提交可等的真实额度是"剩下多少"。

**预算耗尽会返回，不会挂死。** 11 s 延迟对 10 s 预算：

```text
{"case":"B3: server answer outlives the 10 s transaction budget","result":"VersionStoreError",
 "sqlstate":null,"commit_unknown":true,"context":["TimeoutError: ","CancelledError: "],
 "elapsed_s":10.002,"rows_after":[0,0]}
```

`rows_after=[0,0]` 说明这次确实没有落库，`10.002 s` 说明它按时返回。与作者陈述的 `elapsed=10.002s` 一致。

**"剩余额度"这个边界的实测**（业务体活跃 1.8 s、预算 2.2 s、剩余 0.4 s、同一个真实 `23503` 只改答复延迟）：

```text
{"body_active_s":1.8,"budget_s":2.2,"leftover_allowance_s":0.4,"commit_reply_delay_s":0.3,
 "result":"VersionStoreError","sqlstate":"23503","commit_unknown":false,"elapsed_s":2.11}
{"body_active_s":1.8,"budget_s":2.2,"leftover_allowance_s":0.4,"commit_reply_delay_s":0.6,
 "result":"VersionStoreError","sqlstate":null,"commit_unknown":true,"elapsed_s":2.201}
```

答复在剩余额度内 → 采纳；超出 → unknown 且在预算点（2.201 s）截断。**所以这个修复把"1 s 硬截断"换成了"剩余预算"，并没有引入无界等待**；但业务体越慢，提交可等的额度越小，这是需要注意的语义（也是正确语义：整笔事务受 10 s 约束）。

**调用方不会自动重试。** 应用层（`workbench/application/versions.py`）没有重试循环；`version_ports.py:28-29` 写明 `Consumers must not infer rollback from it or retry a mutation automatically`；接口层把 `commit_unknown` 映射为 503 `outcome_unknown`（如 `interfaces/http_management.py:228`）。恢复路径是"保留原 scoped key 重查"，不是重发。

## 4. B4：取消传播——内部预算取消正确，外部取消逃逸

三种取消，真实 PG + 真实 COMMIT 在飞（探针 `/private/tmp/review721/probes.py`；"提交中"由 `_exit_transaction` 被进入的瞬间放行）：

```text
{"case":"B4: cancel before COMMIT (body in progress)","outcome":"CancelledError",
 "elapsed_s":0.003,"rows_after":[0,0],"project_visible":false}

{"case":"B4: cancel during COMMIT (server refuse)","result":"CancelledError",
 "elapsed_s":0.207,"cancel_to_return_s":0.0,"rows_after":[0,0]}

{"case":"B4: cancel during COMMIT (server commit)","result":"CancelledError",
 "elapsed_s":0.205,"cancel_to_return_s":0.0,"rows_after":[1,1]}
```

- **内部预算取消**（`asyncio.timeout` 自己取消）：被 `asyncio.timeout.__aexit__` 转成 `TimeoutError`，落进 `except (psycopg.Error, TimeoutError)`，`committing=True`、`state=None` → `commit_unknown=True`。**正确归为 unknown**，见第 3 节的 `10.002 s` 那条。
- **外部取消**：`CancelledError` 逃逸，「提交前」与「提交中」两种情形**在异常类型上完全相同**，调用方无法从异常区分"确定没提交"与"可能已提交"。第三条实测更直接：`rows_after=[1,1]` —— 事务真的提交了，而调用方只看到 `CancelledError`。

这条在修复前就存在（是 `BaseException` 分支的设计选择），不是本 PR 引入；但本 PR 把"COMMIT 在飞"的窗口从 **≤1 s 放大到 ≤10 s**，同一缺陷的可达概率随之放大。安全性目前靠幂等回执兜住：取消后 `mutation_receipts` 已随同一事务落盘（`rows_after=[1,1]`），客户端拿原 key 重放会命中回执而不是二次写入（该恢复路径已有用例 `test_lost_commit_confirmation_is_unknown_and_does_not_auto_retry` 与 `test_exception_after_commit_can_reconcile_original_key` 覆盖）。所以不是安全洞，但**它是 `commit_unknown` 之外的第二个「结果未知」出口，而文档没提它**（见必须修）。

## 5. B5：`cleanup_timeout` 是被收窄，不是被删除

全仓使用点（`grep -rn "cleanup_timeout"`）：

| 位置 | 作用 | 修复后是否仍生效 |
| --- | --- | --- |
| `postgres_versions.py:189` | 构造参数默认 1 s | 是 |
| `postgres_versions.py:196` | 校验 `0 < cleanup_timeout <= 5` | 是（用例 `cleanup_timeout=0/6` 仍被拒） |
| `postgres_versions.py:226` | `allowance = self._cleanup_timeout`（无 deadline 时的默认） | 是——**回滚/清理路径仍用它** |
| `postgres_versions.py:228` | 有 deadline 时改用剩余预算 | 仅提交路径 |

`_exit_transaction` 有两个调用点：错误路径（`:286`，无 deadline，额度 = 1 s）与提交路径（`:296`，带 deadline）。`asyncio.wait(..., timeout=allowance)` 的"等待但不取消、先关私有 socket 再取消"这套清理语义完全保留，`connection.pgconn.finish()` 仍在 `finally` 里。没有别处依赖它表达"提交结论超时"：`grep "transaction exit deadline"` 全仓只有这一处，`grep "asyncio.wait("` 在 backend 里的其它四处都不是 COMMIT 路径（分别是 speech monitor、minutes 轮询、MinIO 对象写、导出媒体校验）。`PostgresDeletionUnitOfWork`（`postgres_deletions.py:169`）与其它 workbench 存储都复用同一个 `PostgresVersionUnitOfWork`，不存在没被修到的旁路存储。

## 6. B6：反向风险——晚到的答案可不可信

这次修复第一次让"晚到的答案"被采纳，所以要问：会不会把一个"服务端其实失败、但答案本身不可信"的情况误判成确定？

**先看分类条件**（`:303-313`）：`proven_abort = state is not None and (state.startswith("23") or state in {"40001","40P01"})`，`unknown = committing and not proven_abort`。

- **`23*` 整类**：类 23 = 完整性约束违反。它出现在 COMMIT 上只有一种方式——延迟约束检查在提交处理时失败，此时服务器**不会**写下提交记录并返回错误，事务被回滚。所以"COMMIT 收到 `23503`"⇒"未提交"，是协议级保证，不是对错误码的乐观猜测。类内 `23505`（延迟唯一约束）、`23P01`（排他约束）同一语义，没有例外成员。真实外键/唯一/排他在延迟提交时都走这条路径，实测表里 `23503`/`23505` 都落 `commit_unknown=False`。
- **`40*` 只收 `40001`/`40P01`**：核对成立，`40003`（`statement_completion_unknown`）**不在白名单**，仍归 unknown。这正是"类 40 不能整类收"的理由，代码注释（`:305-309`）与 `tests/integration/test_workbench_versions_postgres.py:607` 的 `statement_unknown` 参数化用例一致。
- 我逐码实测"晚 1.5 s 的答案"（服务端事实上已提交，只把答复延后并伪造错误，用于观察分类表本身）：

```text
23503  sqlstate=23503  commit_unknown=False
23505  sqlstate=23505  commit_unknown=False
40001  sqlstate=40001  commit_unknown=False
40P01  sqlstate=40P01  commit_unknown=False
40003  sqlstate=40003  commit_unknown=True     <- 仍未知，符合声明
57P01  sqlstate=57P01  commit_unknown=True
08006  sqlstate=null   commit_unknown=True
57014  sqlstate=57014  commit_unknown=True
55P03  sqlstate=55P03  commit_unknown=True
```

这张表的"已提交 + 23503"组合在真实协议里不可达（已提交的服务器不会回 `23503`），它只证明**分类逻辑只信任 SQLSTATE、不看时间**。协议保证在上一条已经论证。剩余几种保守取舍（`57014` 查询取消、`55P03` 取锁失败若发生在 COMMIT 上）仍归 unknown，方向安全：它们**可能**发生在提交之后，宁可 unknown。注意 `55P03` 在 `test_lock_timeout_is_bounded_known_abort` 里是 `commit_unknown=False`，那是主体语句抛的（`committing=False`），与本条不矛盾。

**没有把真正的未知误判成确定：** 唯一的非 SQLSTATE 判定是"超时/被切断"，两者都归 unknown。

## 7. C7：既有断言未被放宽

```text
backend/src/speechnexus_backend/workbench/application/version_ports.py   |  6 +0
backend/src/speechnexus_backend/workbench/infrastructure/postgres_versions.py | 20 +3
tests/unit/backend/test_workbench_postgres_cleanup.py                     | 126 +0
```

单测文件是纯追加（`@@ -130,3 +130,129 @@`，原有 130 行一字未动）；`tests/integration/test_workbench_versions_postgres.py` **0 行改动**，`:367-368` 的 `assert error.value.sqlstate == "23503"` 与 `assert not error.value.commit_unknown` 原样保留，没有出现 `in (None, "23503")` 一类的放宽。`git diff --name-only` 只有上述 3 个文件。

## 8. C8：回退变异（新测试 vs 旧源码）

在 `/private/tmp/sn721-pre`（`7e09b4db` 的旧源码）里放入 PR 的新测试文件后运行，其余不动：

```text
....F..                                                                  [100%]
__ test_server_sqlstate_arriving_after_the_cleanup_allowance_is_a_known_abort __
>       assert error.value.sqlstate == "23503"
E       AssertionError: assert None == '23503'
E        +  where None = VersionStoreError('workbench store commit uncertain').sqlstate
tests/unit/backend/test_workbench_postgres_cleanup.py:211: AssertionError
1 failed, 6 passed in 0.77s
```

第一条即红，且红在 `assert None == '23503'` 与 `'workbench store commit uncertain'` —— 与 CI 同一句。

**但要如实说：3 条新测试里只有第 1 条是回归护栏。** 第 2 条（`test_connection_lost_during_commit_stays_unknown_without_a_sqlstate`）与第 3 条（`test_commit_reply_beyond_the_transaction_budget_is_unknown_not_abort`）在**旧源码上也通过**：前者旧代码在 0.02 s 清理额度截断后同样得到 `unknown/None`（答案被丢弃，恰好也是它要断言的结果），后者旧代码同样被外层预算截断。它们是对的边界护栏（防止以后把 unknown 改宽），但不构成"这条测试能抓住本次缺陷"的证据。要让它俩也咬住，第 2 条需要能区分"连接断开"与"服务端答复被截断"，而当前构造（同一 fake 退出里抛 `OperationalError`）区分不了。

新测试在修复后全绿：`tests/unit/backend/test_workbench_postgres_cleanup.py` = **7 passed**。三处 `def` 行号与我核对的一致（`:199`/`:216`/`:233`）。

## 9. C9：三条互斥情形，各自自跑

| 情形 | 我的探针 | sqlstate | commit_unknown | 耗时 |
| --- | --- | --- | --- | --- |
| 服务端晚返回确定的 `23503`（延迟 1.5 s） | 真实 PG + 延迟外键，`a1_probe.py` | `23503` | `False` | 1.508 s |
| 提交期间连接/后端断开 | 提交触发器睡眠时由旁路连接 `pg_terminate_backend` | `57P01` | `True` | 0.024 s（`rows_after=[0,0]`） |
| COMMIT 在飞、事务预算耗尽（延迟 11 s） | 真实 PG，默认 10 s 预算 | `None` | `True` | 10.002 s（`rows_after=[0,0]`） |

```text
{"case":"C9: connection/backend lost while COMMIT in flight","result":"VersionStoreError",
 "sqlstate":"57P01","commit_unknown":true,
 "context":["AdminShutdown: terminating connection due to administrator command ..."],"elapsed_s":0.024}
```

三者互斥且方向正确：已知拒绝带码、未知不带码。第三条是"服务器从没给出结论"，落地行为与作者陈述一致。

## 10. D：范围、`uv.lock`、CI

- 范围：3 文件全在 `backend/src/speechnexus_backend/workbench/`（product）+ 对应的单元测试；`version_ports.py` 只加 docstring，无签名变化。未触碰契约、迁移、Web、其它模块。`PostgresVersionUnitOfWork` 是 workbench 唯一 UoW，因此改动覆盖全部 workbench PG 存储，没有"只修一个 store"的缺口。
- `uv.lock`：不在 diff 里（我本地为跑测试产生的 lock/`.venv` 未提交；审查树 `git status` 干净）。
- CI（head `94258c1b`）：`gh pr checks 720` 20 项全部 `pass`，含 `Python: tests, coverage and package build`、`Python: lint, types and contract compatibility`、六个 `Integration N`、`Gate: required checks for this change`；`Docs: markdown code block formatting` 与 Node Controller 镜像项为 `skipping`（路由未命中）。

本机复跑（`/private/tmp/sn721`，head `94258c1b`，隔离 PG 18.6）：

| 命令 | 结果 |
| --- | --- |
| `uv run pytest tests/integration/test_workbench_versions_postgres.py -q` | **36 passed in 1.22s**（与作者一致） |
| `uv run pytest tests/integration/test_workbench_versions_postgres.py -q --cov --cov-branch` | **36 passed**，但整轮 exit 1：全仓 80% 覆盖率门槛在"只跑一个模块"下必然不达标（`Total coverage: 1.82%`）。用例本身全绿，覆盖率结论不可用 |
| `uv run pytest tests/unit/backend -q` | **3043 passed in 39.91s**（与作者一致） |
| `uv run ruff check .` | `All checks passed!` |
| `uv run ruff format --check .` | `1827 files already formatted` |
| `uv run mypy --platform linux` | `Success: no issues found in 1446 source files` |
| `uv run lint-imports` | `Contracts: 9 kept, 0 broken.` |

## `commit_unknown` 的边界现在是什么、有没有新绿

| 情形 | 修复前 | 修复后 | 新绿 |
| --- | --- | --- | --- |
| 服务端在预算内答复错误（`23*` / `40001` / `40P01`），延迟 < 1 s | 带码 / `False` | 带码 / `False` | 无变化 |
| **同上，但答复在 1 s 之后、剩余预算之内** | **`None` / `True`（丢弃答案）** | **带码 / `False`** | **是（#718 的 CI 红就是这一类）** |
| 服务端答复晚于剩余预算 | `None` / `True` | `None` / `True` | 否 |
| 答复 SQLSTATE 不属已证中止（`40003`/`57P01`/`08006`/`57014`/`55P03`…） | `None` 或原码 / `True` | 同左 | 否（保守保留） |
| 提交期间连接/后端断开 | `None` / `True` | 同左 | 否 |
| 提交在飞时被**内部**预算取消 | `None` / `True` | `None` / `True` | 否 |
| 提交在飞时被**外部**取消 | `CancelledError`（无分类） | `CancelledError`（无分类，窗口 1 s→10 s） | 否（更易触发） |
| 业务体把预算吃掉、`allowance`→0 | 1 s 额度内仍可能采纳 | 立即截断为 `True` | 否（更保守） |

一句话：**边界从"1 秒是否能收到答复"改成"事务预算内是否能收到答复"，只新增一种绿——服务器已给出确定拒绝但答复迟到（1 s 至剩余预算之间）时不再谎报未知。** 其余未知一律保持未知，没有新增"把未知说成确定"的通道。真正仍会丢结论的窗口有两个：预算耗尽（不可避免）与外部取消（可避免，未修）。

## 必须修清单

1. **（文档级，非阻断）`version_ports.py:30-35` 的 `commit_unknown` 定义写窄了。** 现在的文字是"did the store's answer leave the outcome undecidable? It is True only when the server never determined the transaction inside its own budget"。两处不吻合：
   - 同一异常类型的其它生产者出于**业务状态**主动报 `commit_unknown=True`，与"服务器没在预算内判定"无关：`postgres_content_writes.py:107`（导出任务状态不是 running）、`:143`（写意图 owner 已存在，`writing`/`unknown`）、`:184`（CAS 抢 token 失败）。这些是"本请求确定没提交，但前一次写的结果未知"，是有意的保守报法。
   - 「提交在飞时被外部取消」这条路径**根本不产生 `commit_unknown`**，而新定义读起来像"`commit_unknown` 覆盖所有未知"。

   建议改成"本适配器在下列情形置 True：传输丢失、shutdown、COMMIT 在飞时预算耗尽、取消；`False` 表示服务器在预算内给出了可归纳为既知失败的答案"这类列举式表述，并注明 `postgres_content_writes` 的保守复用。属文档收口，可与本 PR 同批微小改动，也可另开。

2. **（建议，非阻断）** 把第 2 条新测改成能区分"连接断开"与"服务端答复被截断"的构造（例如让假退出先返回一个可观察的延迟答复、再在另一次运行里直接抛 `OperationalError`），否则它只是边界护栏，抓不到本次缺陷——我已在第 8 节证明它在旧源码上通过。

3. **（观察）** 提交等待占用的连接槽位从 ≤1 s 放大到 ≤10 s（`max_connections` 默认 8，semaphore 在预算内获取）。正常提交是毫秒级，影响可忽略；若生产出现系统性慢提交，会表现为排队等待者各自耗完 10 s 预算后得到 `store_unavailable`（`committing=False`，方向正确）。建议在容量/告警侧留意"提交耗时长"而不是靠这个修复兜。

没有发现需要阻断合并的产品缺陷。

## 不能本机复跑 / 只能依赖作者陈述的点

- **CI 上原始那次红的完整日志**：我只核到 issue #718 引用的断言文本与修复前的确定性复现（同一行同一句），没有翻到 `43527451664` 那次的完整 job 日志。
- **作者"修复前 0/100 失败"的 100 次循环**：我没有复刻这个次数；我在无延迟下跑的是修复前 20/20、修复后 30/30 全绿，结论方向一致（红需要延迟条件，不是随机）。因此"CI 现场到底为什么慢了 1 s 以上"（宿主机负载、MinIO/浏览器并行）我无法验证，只能确认"慢于 1 s 就必然红"这条因果。
- **作者的 `--cov --cov-branch` 口径**：作者说"36 passed，带 `--cov --cov-branch` 同"。我跑出 36 passed 但 exit 1（单模块跑全仓覆盖率门槛必然失败）。若作者是在完整门禁 job 里看的，则口径不同；单模块下这条不能算通过。
- **`tests/integration -k workbench` 的 35 failed / 60 errors 与基线一致**：我没有跑这套（需要 MinIO 与浏览器产物），只跑了 versions 这一个模块。
- **第 9 节第二条"连接断开"**：我用 `pg_terminate_backend` 制造，得到 `57P01`；这与作者/集成用例里 monkeypatch 伪造 `OperationalError`(sqlstate=None) 的现场不同，两者都归 unknown，但码不同，报告里已分别标注。

## 时间线（本地时间 Asia/Shanghai，2026-11-14）

- 08:24 `git fetch`；主检出确认停在 `docs/tracker-batch12`（`a7508635`）。
- 08:25 `git worktree add /private/tmp/sn721 -b review/721-commit-classification 94258c1b`；读 PR 的三文件 diff。
- 08:26 建 `/private/tmp/sn721-pre`（detached `7e09b4db`）作对照；读 `postgres_versions.py` 全文、`version_ports.py`、两条相关集成用例。
- 08:27 手工建隔离 PG 18.6 集群（仅 Unix socket、`listen_addresses=''`、探针开关 `on`、port 55439），身份四元组与 fixture 逐字一致；`36 passed in 1.22s`。
- 08:28 `uv run pytest tests/unit/backend -q` = 3043 passed；`--cov --cov-branch` 单模块跑出 36 passed + 覆盖率门槛失败。
- 08:29 写 `/private/tmp/review721/a1_probe.py`（自造延迟触发器 + 真实延迟外键），修复前 0.5 s→`23503/False`、1.5 s→`None/True` + `TimeoutError("workbench transaction exit deadline")`，1.005 s 截断。
- 08:30 修复后同探针：0.5/1.5/2.5 s 全部 `23503/False`。写 `probes.py`。
- 08:31 B3：11 s 对 10 s 预算 → `None/True`、10.002 s、`rows_after=[0,0]`。
- 08:32 B4：取消前 / 取消中（服务器拒绝）/ 取消中（服务器提交）三跑；写 `b3b.py`（被 `idle_in_transaction_session_timeout` 干扰，改 `b3c.py`）。
- 08:33 B3c：1.8 s 活跃体 + 2.2 s 预算，剩余 0.4 s 内 0.3 s 答复被采纳、0.6 s 答复被截断。
- 08:34 B6 九码矩阵；B5 `cleanup_timeout` 使用点审计；确认 `_exit_transaction` 全仓唯一、`PostgresDeletionUnitOfWork` 复用。
- 08:35 C9 三条；C8 在 `/private/tmp/sn721-pre` 放入新测试 → `1 failed, 6 passed`，红在 `assert None == '23503'`。
- 08:36 写 pytest 插件 `slowreply.py`，用**未改动的**集成用例复现 CI 红（修复前 `tests/integration/test_workbench_versions_postgres.py:367`），修复后同跑转绿；整模块带 1.5 s 延迟时修复前 4 红、修复后 3 红（余下 3 条是并发/极小预算用例，两侧都红，属注入工具的副作用，非本 PR 引入）。
- 08:37 无延迟 flakiness：修复后 30/30、修复前 20/20 全绿。
- 08:38 ruff check / format、mypy、lint-imports 全绿；`gh pr checks 720` 20 项全绿（head `94258c1b`）。
- 08:39 撰写本报告；`python3 scripts/dev-env/check-doc-refs.py` 自检引用、`uv run --no-project ruff format` 与 `ruff format --check .` 复跑。
