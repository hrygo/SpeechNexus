# PR #815 非作者审查：flake 复现 harness 与 ACK 租约窗口调整

- 审查对象：PR #815 `test/813-flake-harness`，head `90495fafdd90281d252d68a207a7a2e81cdd5471`（自报修 #813）
- 审查分支：`review/817-flake-harness`，基于 `origin/main` = `575a7506`（`docs: 记录 PR #814 规则文件路由判据的非作者审查 (#816)`）
- 运行检出：`/private/tmp/sn817-pr`（detached HEAD = `90495faf`，只用于运行与变异，审查结束时删除；本审查分支的 `git diff origin/main...HEAD` 只含本报告）
- 运行环境：macOS arm64、Python 3.14.9、PostgreSQL 18.6（本审查自建一次性集群 `127.0.0.1:55441`）、MySQL 8.4.8（项目受管容器 `127.0.0.1:55434`，`log_bin=1`）
- 审查时间：2026-11-15 09:07–09:47（Asia/Shanghai）
- 被审内容一律用 `git show 90495faf:<path>` 读取；本文所有数字均为本机实测，标注为「作者自报」的除外

## 结论

**不通过（阻塞）。**

1. **阻塞**：PR 没有把相邻用例的租约窗口改到它自己声明的值。head 上是 `LEASE_WINDOW_US = 1000000`（1 s，与 `origin/main` 的内联 `lease_us=1000000` 等价），而 PR 正文、`tests/integration/AGENTS.md` 新增小节、代码注释三处都写 2.5 s。结果是 `--condition latency-proxy` 用它自带的默认目标跑出来就是红的：`1 failed, 3 passed`，失败句正是 #813 要消灭的 `Failed: ACK did not encounter the real outbox row lock`。即 #813 的第二件事在 head 上没有交付。
2. **必须修**：即使把值改成 2.5 s，上半侧余量不足。无代理的普通运行里 PG 侧 ACK 阻塞 2.501 s，而 `PostgresRuntimeDelivery` 的整笔操作预算是 3 s，只剩 0.498 s（17%）给锁后复核与两次 UPDATE + 提交。PR 注释把上界只归给 MySQL 的 `innodb_lock_wait_timeout=3`，并称「阻塞等待约 1 s」，两处都与实测不符。
3. **必须修**：`PostgresSlowCommits` 的「`pg_reload_conf` 后读回确认」是空转的：同一连接上 reload 生效前 `SHOW` 仍返回旧值，读到旧值即等于 `_original_*` 时立刻判成功。它无法区分「已复原」与「还没生效」，而正是被污染的状态下最容易假通过。
4. **必须修**：`latency-proxy` 这个条件没有任何实测证据。启动打印的「measured, not assumed」对它是假的——只打印 `chunk_delay_ms` 与改写后的回环 DSN，没有一条往返延迟测量；代理若静默不延迟，输出一模一样且用例会「稳定绿」。#813 的整个目的就是防止这种「没生效的条件被当成证据」。
5. 其余部分（harness 入口、条件生效守卫、常规/失败/异常路径清理、不默认开启、例数与静态检查）核查通过；M1/M2 变异证明放宽到 2.5 s 后断言未松，作者「零放宽」的结论成立。

## 审查过程（时间线）

1. `git fetch` 后核对远端：`refs/heads/test/813-flake-harness` 与 `refs/pull/815/head` 都是 `90495faf`；`/private/tmp/sn814`（作者工作区）工作树干净、同一 commit。
2. 建审查 worktree（基于 `origin/main`）与运行 worktree（detached `90495faf`），读 `tests/integration/AGENTS.md`（含新增小节）、`flake_harness.py`、`flake_conditions.py`、被改用例与产品侧 `runtime_delivery_sql.py` / `runtime_delivery_postgres.py`。
3. 起一次性 PG 集群 + 复用项目受管 MySQL，先在无条件下跑基线（`2 passed in 6.81s`）。
4. 在 head 原样跑 `--condition latency-proxy`（默认目标）→ 红，见 §A.0。
5. 在运行 worktree 里临时给用例加计时探针、把 `LEASE_WINDOW_US` 改 2.5 s，量建立耗时、锁检出、阻塞时长与 ACK 墙钟（§A.2/A.3），并跑默认目标（`4 passed in 43.33s`）。
6. 单点变异 M1、M2 各跑一次（§A.4）。
7. 跑 `--condition commit-delay` 实测生效与常态清理（§B.6），再用 `kill -9` 复现强杀残留（§B.6）。
8. 用变异 `_MINIMUM_EFFECT_RATIO=5.0` 验证生效守卫（§C.8）；打印 `latency-proxy` 的证据输出（§C.8）。
9. `pytest tests/integration --collect-only`、`gh pr checks 815`、`gh pr diff 815 --name-only`，并在 PR 树上跑 `ruff` / `mypy --platform linux` / `lint-imports`（§D）。
10. 复原所有变异（`git status --porcelain` 为空）、删运行 worktree、停并删除临时 PG 集群、确认 MySQL `binlog_group_commit_sync_delay=0` 与项目 PG 参数/`postgresql.auto.conf`/临时 schema 均无残留（§E）。

## A. 相邻用例的窗口调整

### A.0 前置事实：head 上并不存在 2.5 s

```
$ git grep -n "LEASE_WINDOW_US = " 90495faf -- tests/integration/test_runtime_delivery.py
67:LEASE_WINDOW_US = 1000000

$ git ls-remote origin 'refs/pull/815/head' 'refs/heads/test/813-flake-harness'
90495faf…  refs/pull/815/head
90495faf…  refs/heads/test/813-flake-harness
```

同一份文件里，紧接常量的注释写的是「2.5 s stays above the measured establishment time…」，PR 正文写「`LEASE_WINDOW_US = 2500000`」，`AGENTS.md` 表格写「租约窗口 `LEASE_WINDOW_US` = 2.5 s」。三处文档/注释与代码不一致，且**代码侧是未修的那一侧**。作者工作区 `/private/tmp/sn814` 干净、同为 `90495faf`，不存在「本地已改未推」的情形。

head 上用 harness 自带默认目标跑文档里那条命令：

```
$ SPEECHNEXUS_TEST_DATABASE_URL=… SPEECHNEXUS_TEST_MYSQL_URL=… \
    uv run python -m tests.integration.flake_harness --condition latency-proxy
E   Failed: ACK did not encounter the real outbox row lock
FAILED tests/integration/test_runtime_delivery.py::test_ack_rechecks_expiration_after_the_actual_outbox_row_lock[mysql]
1 failed, 3 passed in 42.85s
```

即：`flake_harness.py` 模块文档「Both are expected green under every condition」与 `AGENTS.md` 表格里该条件的「预期」行，在当前 head 上都不成立。这是本审查最重的一条。

另外要注意方向性：head 把观测预算从 `origin/main` 的硬编码 2 s 改成 `LEASE_WINDOW_US/1e6`（= 1 s），单看这一处是**收紧**而非放宽；`origin/main` 与 head 的租约值相同。因此 head 对 #813 第二件事是「没修」，不是「修错了」。

### A.1 「1 s 是限制应成功路径的观察窗口」这个分类是否成立——成立

用例要证明的语义（读产品代码，不是读声明）：ACK 事务先在 `_owned()` 读自己的 ledger 行（此处租约必须**仍在有效期内**，否则直接 `stale`、根本不请求 outbox 锁），然后在 outbox 行上等锁，**拿到锁之后**再复核一次租约，过期就返回 `stale` 且不写 `delivered_at_us`：

- `runtime_delivery_sql.py:120-137` `_owned()`：`claim_expires_at_us <= now_us` 即返回 `None` → `acknowledge()` 立刻 `stale`；
- `runtime_delivery_sql.py:243-255`：outbox `FOR UPDATE` → `now_us = clock()` → `claim_expires_at_us <= now_us` 返回 `stale`（这才是本用例的被测分支）；
- 断言：`outcome == "stale"`、`delivered_at_us IS NULL`、`delivery_phase == "unknown"`、`physical() == before`。

租约值只决定「`_owned()` 看到的是活租约还是死租约」，不改变任何断言语义，也不改变要触发的分支。作者把它归为「限制应成功路径的观察窗口」与代码结构一致：1 s 在 10 ms/chunk 代理下让建立阶段（`claim`+`start`+blocker 就绪）就吃掉整个租约，ACK 到不了自己的观察点。判定：**分类论证成立**。

### A.2 「到期是否仍确定性发生」——是，且与窗口尺寸无关

到期不是靠运气踩时钟，而是用例显式等 DB 时钟：

```python
while delivery.runtime.now() <= ticket.claim_expires_at_us:
    time.sleep(0.01)
```

`before` 在提交 ACK 前取，ACK 在 `ThreadPoolExecutor` 里阻塞于 blocker 持有的 outbox 行锁，主线程用**数据库时钟**（PG `clock_timestamp()` / MySQL `CURRENT_TIMESTAMP(6)`）等到 `claim_expires_at_us` 之后才 `blocker.rollback()`。所以「阻塞期间到期」由构造保证，窗口大小只改变等的时长。实测（2.5 s + 代理）三次都稳定到期，且阻塞时长可预测（下表）。

判定：**成立**。把窗口从 1 s 放宽到 2.5 s 不会让「到期」更容易或更难发生，只让 ACK 在自己的 3 s 预算里多等 1.5 s——后者是 A.3 的问题。

### A.3 双侧余量——下半侧够，上半侧不够（必须修）

实测（`--condition latency-proxy`，10 ms/chunk，`LEASE_WINDOW_US=2500000`，连跑 3 次；单位 ms）：

| 引擎 | claim | start | 建立到 ACK 提交 | ACK 提交后检出锁 | 自 ACK 提交起阻塞 | ACK 墙钟 | 结果 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| PG | 504–518 | 280–288 | 1127–1135 | 269–287 | 1800–1828 | 1894–1922 | stale |
| MySQL | 434–453 | 394–397 | 1280–1297 | 371–405 | 1613–1631 | 1688–1707 | stale |

同一个探针在**无任何条件**下（2.5 s 窗口）跑一次：

| 引擎 | 建立到 ACK 提交 | 自 ACK 提交起阻塞 | ACK 墙钟 |
| --- | --- | --- | --- |
| PG | 11 | 2501 | 2502 |
| MySQL | 498 | 2030 | 2032 |

- **下半侧（窗口 > 建立耗时）**：最坏 1297 ms（MySQL+代理）对 2500 ms，余量 1.20 s，建立耗时还能涨约 1.9 倍才破；且这 1.2–1.3 s 几乎全是确定性的 `time.sleep` 累加（三次重复波动 ≤17 ms），不受负载影响。**够**。
- **上半侧（阻塞 < 超时）**：真正的上界不是作者写的 `innodb_lock_wait_timeout=3`，而是两条更窄的约束：
  - PG：`PostgresRuntimeDelivery(timeout_seconds=3)` 给整笔 ACK 3 s 单调预算，`_DeadlineCursor.execute` 每条语句再设 `statement_timeout=剩余`。无代理时 ACK 墙钟 2502 ms，**只剩 498 ms（17%）**给锁后 `clock()` 复核 + 两条 UPDATE + 提交，全部在同一预算内；
  - MySQL：连接上 `SET SESSION innodb_lock_wait_timeout = 3`（`tests/support/job_database.py:51`），无代理阻塞 2030 ms，剩 0.97 s（32%）。
- 也就是说：2.5 s 把**不受负载影响**的一侧留了 1.2 s，把**受负载影响**的一侧只留 0.5 s；在 6 分片并发的 CI 上这两条锁后写语句若抖到 0.5 s 以上，失败会变成 `DeliveryUnavailableError` 而不是 `stale`。这与「不要变成下一颗 flake」的目标相反。
- 作者注释的算术不准确：实测阻塞是 1.6–1.8 s（代理）/ 2.0–2.5 s（无代理），不是「near 1 s」；上界的主约束是产品自身 3 s ACK 预算，不只是 MySQL 的 3 s。

判定：**余量不足，必须修**（建议窗口收到 ~2.0 s：下半侧仍有 0.7–0.87 s 余量，上半侧回到 1.0 s，或直接用「最小硬上界 − 余量」导出窗口并把两个 3 s 都写进注释）。

### A.4 M1/M2 变异：放宽到 2.5 s 后仍然咬得住——成立，但 M2 的机制描述要修正

两次变异都在运行 worktree 里单点施加、单点运行、跑完 `git checkout --` 复原（`git status --porcelain` 为空，未 push 任何变异），都是 2.5 s 版本 + 10 ms/chunk 代理：

| 变异 | 结果 | 失败句 |
| --- | --- | --- |
| M1：删除 ACK 的 post-lock 租约复核（`runtime_delivery_sql.py:253-255`） | `2 failed in 22.74s` | `AssertionError: assert 'committed' == 'stale'`（两引擎 `outcome=committed`） |
| M2：去掉 ACK outbox 查询的 `FOR UPDATE` | `2 failed in 22.73s` | 同上 `assert 'committed' == 'stale'` |

判定：**「零放宽」成立**——放宽到 2.5 s 后，删掉被测分支或去掉真实锁仍会红。

需要修正 PR 正文对 M2 的解释：M2 红**不是**被收紧后的锁检出咬住的。去掉 `FOR UPDATE` 后 ACK 在那条 `SELECT` 上不再等锁，会先过复核、再在随后的 `UPDATE coordination_outbox …` 上等同一行锁；用例的检出循环照样能看到「本 blocker 阻塞、对象是 `coordination_outbox` 记录锁」，所以检出不报警，是 `outcome == "stale"` 这条断言把它咬住。正文「同样红（复核早于真实锁等待）」的因果应改成「锁等待被推迟到 UPDATE，检出不区分 SELECT/UPDATE，靠结果断言咬住」。

## B. harness 可用性与清理

### B.5 一条命令能否跑通——能跑通，但默认目标在 head 上是红的

`uv run python -m tests.integration.flake_harness --help`、`--condition {latency-proxy,commit-delay,both}` 均正常；`tests/integration/` 无 `__init__.py` 但按命名空间子包可 `-m` 导入，实际运行成功。缺 DSN 时退出码 2 并点名变量（代码 `flake_harness.py:main`）。作者的退出码说明里「条件未生效也退 2」不准确，见 C.8。

问题不在入口，在默认目标（§A.0）：同一命令在 head 上 `1 failed, 3 passed`。把值改成 2.5 s 后同一条命令 `4 passed in 43.33s`（与作者自报 ~44.7 s 一致），所以 A.0 的修复能直接让这条命令回到「一条命令可重放」。

### B.6 清理是否彻底——常态与失败路径彻底，强杀残留的说明不完整，「读回确认」空转

常态退出（`--condition commit-delay`，默认目标，`2 passed in 42.59s`）后实测：

```
PG : commit_delay=0  commit_siblings=5  source=default
     postgresql.auto.conf 只剩两行注释
     pg_namespace 中 sn_commit_probe_* / sn_flake_* 计数 = 0
MySQL: @@GLOBAL.binlog_group_commit_sync_delay = 0；无 sn_commit_probe_* schema
无残留进程
```

生效证据也确实被打印（`PostgresSlowCommits` / `MySQLSlowCommits` 的 `evidence`）：

```json
{"condition":"postgres-commit-delay","baseline_commit_median_ms":0.09,"slow_commit_median_ms":102.39,…}
{"condition":"mysql-binlog-group-commit-delay","baseline_commit_median_ms":0.85,"slow_commit_median_ms":220.35,…}
```

失败与异常路径同样会收尾：pytest 用例失败后仍打印 `flake harness cleanup: conditions restored, background load stopped` 且参数复原；`PostgresSlowCommits.__enter__` 在 `_require_effect` 失败时走 `_cleanup(raise_on_failure=False)`，实测值守卫失败后 PG 仍回到 0/5。

强制终止后（对真实 harness `kill -9`，再单独处理其 pytest 子进程）实测残留：

```
PG : show commit_delay → 100000, commit_siblings → 1
     postgresql.auto.conf → commit_delay = '100000' / commit_siblings = '1'
     遗留 schema sn_flake_commit_a80669007b41
MySQL: @@GLOBAL.binlog_group_commit_sync_delay = 200000
pytest 子进程仍在跑
```

`AGENTS.md`「按同一顺序手工复原并在 `SHOW` … 上核对」对**参数**是准确的（`ALTER SYSTEM RESET` + `pg_reload_conf` 能复原，MySQL 重启或 `SET GLOBAL` 能复原）；但漏了两件同样会被下一个人踩到的事：`sn_flake_commit_<hex>` 与 `sn_commit_probe_<hex>` 临时 schema 会永久留在测试库里，被强杀的 pytest 子进程会成为孤儿并继续在慢提交下跑。这两点应补进清理小节。

**「读回确认」是空转的**（必须修）。`_cleanup` 在 `SELECT pg_reload_conf()` 之后立刻 `_wait_for(lambda: SHOW commit_delay == _original_delay)`，但同一会话在 reload 生效前读到的仍是旧值，实测：

```
original = 100000
same-session reads right after reload: [(0.1, 100000), (0.2, 100000), (0.7, 0), (0.8, 0)]
```

第一次探测（+0.1 ms）读到 100000 == `_original_delay` → `_wait_for` 立即判成功。于是当运行前参数已被污染（正是最需要它报警的场景），这个「确认」必然通过——它证明不了 RESET 生效。端到端复现：先把 `commit_delay` 置 100000 模拟残留，再跑 harness，退出码 0、打印「conditions restored」，而读回那一步什么也没验证。MySQL 侧 `SET GLOBAL` 是同步生效后读回，不受此影响。修法：reload 后先等到 `pg_settings.setting` 真的变化（或比较 `pending_restart`/轮询到首次变化）再断言最终值。

### B.7 不默认开启——成立

- `tests/conftest.py`、`tests/integration/` 下无任何 `flake_conditions` / `flake_harness` 导入（只有 `test_runtime_delivery.py:61` 的注释提到名字）；无 marker 自动应用。
- `make integration-test` 仍是 `./scripts/dev-env/mysql-service.sh` + MinIO 服务 + `uv run pytest -m integration tests/integration -q`，与 harness 无关。
- `pytest tests/integration --collect-only -q` → `2761 tests collected in 13.62s`，与基线一致（未减少，也未新增）。

## C. harness 会不会被误用成「绿色证据」

### C.8 生效守卫——慢提交侧成立且实测会失败，`latency-proxy` 侧没有守卫（必须修）

慢提交侧（`PostgresSlowCommits._require_effect` / `MySQLSlowCommits._require_effect`，阈值 `_MINIMUM_EFFECT_RATIO = 0.5`）用真实提交中位数与配置延迟比较，不达标即抛。用一个单点变异（`_MINIMUM_EFFECT_RATIO = 5.0`）验证它确实会失败、而不是静默绿：

```
harness_exit=1
RuntimeError: PostgreSQL commit_delay is not in effect: measured median 101.96 ms < 500.00 ms;
commit_delay needs at least commit_siblings concurrently committing backends
```

且失败后 PG 参数仍被复原（0/5）。判定：**守卫接线正确、失败可见**。两处小瑕疵：模块文档说「条件未生效也退 2」，实际是异常回溯 + 退出码 1；`__enter__` 失败时用 `_cleanup(raise_on_failure=False)`，读回不达标不会额外提示（可接受，因为主异常已在）。

`latency-proxy` 侧则完全没有对应物。实测启动输出只有配置与改写后的 DSN：

```json
{"condition":"tcp-latency-proxy","chunk_delay_ms":10.0,
 "postgres_via":"postgresql://…@127.0.0.1:55651/speechnexus_test",
 "mysql_via":"mysql://…@127.0.0.1:55652/"}
```

没有一条「经代理的往返/提交耗时」测量，也没有「延迟未生效即失败」。于是：代理正常但字节没被延迟（例如将来改了 `_forward` 的 sleep 位置、或 DSN 改写只对其中一侧生效、或驱动走了别的路径）时，输出与现在完全一样，用例会「稳定绿」，而 harness 的标题还写着 `conditions (measured, not assumed)`。这正是 #813 想消灭的「用没生效的条件证明用例稳定」。修法：像提交侧一样加一条测量——经代理 DSN 做一次基线/条件对比的往返或 `measure_commit_latency_ms`，中位增量不到 `chunk_delay_ms × 往返次数下界` 的一半就失败。

## D. 文档与例数

### D.9 AGENTS.md 小节

- **终态语态**：成立。全节陈述现行入口、前置、用法、清理、边界，没有演进流水账或「原计划…现改为…」。
- **命令可直接复制**：成立（`--condition latency-proxy` 那一行原样可跑，见 B.5；`--commit-delay-us` 覆盖两引擎、PG >100000 启动即被 `ValueError` 拒绝也与代码一致）。
- **前置条件准确**：两个 DSN（缺任一退 2）✓；PG 需超级用户（`ALTER SYSTEM` + `pg_reload_conf`）✓——`scripts/dev-env/bootstrap.sh` 用 `initdb --username=speechnexus` 建本地实例，该角色就是集群超级用户，普通双库门禁的默认 DSN 满足这一条；MySQL 需 `log_bin=ON`，受管容器 8.4.8 实测 `@@GLOBAL.log_bin=1` ✓；`./scripts/dev-env/mysql-service.sh` 与 `127.0.0.1:55434` 一致 ✓；链接 `../operations/local-dual-database.md#临时-postgresql-实例` 存在且该节确为一次性 `postgres:16.15` 容器（55436）✓。
- **「证明什么/不证明什么」**：覆盖了「代理只延迟字节、不模拟宕机、不模拟慢磁盘/fsync」，还补了 HA / 跨主机时钟偏移 / 租约设计本身正确性 ✓。缺一句「提高 `--chunk-delay-ms` 会让租约用例红，那是窗口耦合失效、不是产品缺陷」——在 A.3 的窗口与 10 ms/chunk 强耦合之后，这句有必要。
- **必须修的不一致**：表格 `latency-proxy` 行的「预期」写「`LEASE_WINDOW_US` = 2.5 s」，与 head 的 1 s 冲突；文档在描述未提交的行为（与 §A.0 同一处修复）。

### D.10 例数与静态检查

- `pytest tests/integration --collect-only -q`：`2761 tests collected in 13.62s` ✓ 与作者自报一致。
- 在 PR 树上（`/private/tmp/sn817-pr`，detached `90495faf`）独立复跑：`ruff format --check .` → `1871 files already formatted`；`ruff check .` → `All checks passed!`；`lint-imports` → `Contracts: 9 kept, 0 broken.`；`mypy --platform linux` → `Success: no issues found in 1459 source files`。四项与作者自报数字一致。
- `gh pr checks 815`：24 项无 fail。六分片 `Integration 0..5` 全 pass（5m40s / 7m30s / 5m40s / 9m36s / 9m26s / 6m44s），`Python: lint, types and contract compatibility` pass（1m51s），`Python: tests, coverage and package build` pass（6m20s），`Gate: six-shard integration evidence and merged coverage` pass（46s），另 Web / Docs / Compose / Deploy / Node Controller / Scope 各项 pass；两项 skipping（node-controller 矩阵、docs markdown 格式）。
- `gh pr diff 815 --name-only`：`tests/integration/AGENTS.md`、`tests/integration/flake_harness.py`、`tests/integration/test_runtime_delivery.py`、`tests/support/flake_conditions.py`；`git diff --stat origin/main...90495faf` = 4 files, +817 −8（与 PR 的 additions/deletions 一致）。**产品代码不在本 PR diff 内**，符合作者声明。

## 必须修清单

1. **【阻塞】把 `LEASE_WINDOW_US` 真正改成 2500000 并重推 head**，同时确认重推后 `--condition latency-proxy` 默认目标 `4 passed`。当前 head 的 1 s 让 harness 自带默认目标红（`1 failed, 3 passed`，`Failed: ACK did not encounter the real outbox row lock`），且与 PR 正文/AGENTS.md/注释三处 2.5 s 的表述冲突；#813 第二件事在 head 上未交付。
2. **【阻塞】修双侧余量（上半侧）**：2.5 s 在无代理运行里占掉 PG ACK 3 s 操作预算的 2.502 s（余 0.498 s，17%），MySQL 占 2.03 s / 3 s（32%）。要么把窗口收到 ~2.0 s（下半侧仍有 0.7–0.87 s、上半侧回到 1.0 s），要么按「最窄硬上界 − 余量」导出窗口；并把注释里的实测值写实（阻塞 1.6–1.8 s @ 代理 / 2.0–2.5 s @ 无代理，不是「约 1 s」），上界要写成「PG 3 s ACK 预算 + MySQL 3 s `innodb_lock_wait_timeout`」两条。
3. **【阻塞】修 `PostgresSlowCommits` 的读回确认**：`pg_reload_conf()` 之后同会话 `SHOW` 仍返回旧值，`_wait_for` 会立即误判成功（实测 +0.1 ms 读到旧值）。必须等到参数真的变化后再断言终值，否则这条「确认」在参数被污染时反而必然通过。
4. **【阻塞】给 `latency-proxy` 加实测守卫**：现在只打印配置，没有测量；代理不生效时仍是绿的。加一条经代理 DSN 的往返/提交中位测量，达不到预期增量即失败，让 `conditions (measured, not assumed)` 对三个条件都成立。
5. **【必须】文档与代码一致**：`AGENTS.md` 表格的 2.5 s、模块 docstring 的「条件未生效也退 2」（实际退 1）、强杀残留说明（补 `sn_flake_commit_*` / `sn_commit_probe_*` 临时 schema 与孤儿 pytest 子进程两件、并说明「被强杀后重跑前先手工复原，否则读回确认会继续空转」）。
6. **【必须】修正 PR 正文对 M2 的因果**：M2 红是被 `outcome == "stale"` 咬住的，收紧后的锁检出并不区分「FOR UPDATE 前的等待」与「UPDATE 上的等待」；结论不变，但机制描述要改，否则下一个人会以为检出循环能单独覆盖这个变异。
7. **【建议】把窗口与 `--chunk-delay-ms` 的耦合写进 `AGENTS.md`**：租约用例的成立前提是默认 10 ms/chunk；调大 chunk delay 后它变红是窗口失效，不是产品缺陷。

## 只能依赖作者陈述、本审查未独立验证的点

- 作者的重复次数证据：`latency-proxy` 5/5、`commit-delay` 3/3、常规 5/5、整文件 `test_runtime_delivery.py` 24/24。本审查只做了：2.5 s 下租约用例 3/3、默认目标 1 次、`commit-delay` 2 次、无条件下 1 次。
- `#775` 的历史：本审查没有回溯 #775 的会话记录，只核了「`origin/main` 上此前确无这套入口」这一可核事实（PR 新增四个文件）。
- M3（「租约回退 1 s + 代理 → 稳定红」）：没有单独造这个变异，因为 head 本身就是 1 s，我直接用 head 复现了红（§A.0），等价性由本报告给出。
- CI 绿是 `gh pr checks 815` 的平台结果，未在本机重跑六分片。
- 强杀残留的「手工复原后能否完全恢复」：本审查实测了参数与 `auto.conf` 可以复原、schema 需另外 DROP；没有验证「下次运行是否会因为读回确认空转而静默通过」以外的二次污染路径。

## E. 本审查自身的环境复原（不留运行中的条件）

- 运行 worktree `/private/tmp/sn817-pr` 已 `git worktree remove`；所有变异已 `git checkout --` 复原，`git status --porcelain` 为空，未 push 任何变异。
- 一次性 PG 集群 `/private/tmp/sn817-pg`（`127.0.0.1:55441`）已 `pg_ctl stop` 并删除数据目录。
- MySQL 受管容器 `speechnexus-coordination-mysql` 上 `binlog_group_commit_sync_delay` 已回 0，无 `sn_commit_probe_*` schema 残留。
- 强杀实验遗留的 `sn_flake_commit_a80669007b41` 已 DROP CASCADE；`pg_namespace` 中 `sn_flake_*` / `sn_commit_probe_*` 计数为 0，`postgresql.auto.conf` 已无 `commit_delay` / `commit_siblings`，`SHOW` 为 0 / 5（default）。
- 未触碰项目 `127.0.0.1:55432` 实例、未改产品代码、未 push main。
