---
title: "配额副作用快照测试（PR #710 / issue #709）的非作者审查"
status: active
version: "0.1.0"
date: 2026-11-14
---

# 配额副作用快照测试的非作者审查（PR #710 / #709）

- **审查对象**：`62564255a23fc097fc9ebbd871049bf7ad0df7b0`（`test: 把 workspace_quotas 纳入拒绝路径的副作用快照 (#709)`），PR #710（分支 `test/709-quota-snapshot`，base `main`），修 issue #709。改动面 1 文件、+131/−38，只动 `tests/integration/test_public_job_admission_http_pg.py`。
- **基线**：审查开始时 `origin/main` = `7147f306`，被审 head `62564255` 尚未合入；交付分支改基于最新 `origin/main`（`a4bc09f7`），只新增本报告，不把待审测试带进审查 PR。
- **工作区**：独立 worktree `/private/tmp/sn713`，按任务指定 `git worktree add /private/tmp/sn713 -b review/713-quota 62564255` 建立后读取被审内容。主检出停在 `docs/tracker-batch12`（`a7508635`）且 ` M uv.lock`，本报告未读取其工作树，产品与测试事实全部取自 worktree 或 `git show 62564255:<path>`。
- **审查者**：非作者。交付分支只新增本报告；产品代码与测试零改动。变异实验在另一个临时 worktree `/private/tmp/sn713-mut`（同 commit，detached）进行，每次实验后 `git checkout -- backend/src/` 还原，未 push 任何变异；探针与变异脚本只存在于该临时目录。
- **隔离环境**：本机 PostgreSQL 18.6 的一次性专用库 `sn713_review`（审查结束 `dropdb`）+ 既有项目隔离 MySQL 容器 `speechnexus-coordination-mysql`（`127.0.0.1:55434`，MySQL 8.4.8，与 CI 同主次版本；只连接，未启停）。DSN 只经 `SPEECHNEXUS_TEST_DATABASE_URL` / `SPEECHNEXUS_TEST_MYSQL_URL` 环境变量传入。本文件不需要 MinIO 与浏览器前置，因此未验证对象存储与 Web 分片。
- **审查方式**：静态审查（`workspace_quotas`/`speech_workspaces` DDL、准入 SQL、最小权限授予、拒绝异常路径）+ 本机实跑目标文件与相邻公开 Job 套件 + 7 组产品侧变异 + 1 组判别探针 + CI shard artifact 独立下载复核。
- **报告文件名**：沿用任务指定的 `2026-11-13`；实际执行在 2026-11-14（+08:00）。

> **事实分层**：标「实跑」的都有本机命令输出；只做代码推导或只能采信作者转述的，在末节单列。

---

## 结论

**有条件通过：核心目标达成，但 `#709` 的盲区只堵住了 `workspace_quotas` 这一层，同一事务里另一半预算记账仍未进快照。**

作者声称的四件事我都能复现：`workspace_quotas` 的 5 个非主键列全部进快照；拒绝路径比较整快照相等；成功路径断言"配额变了"；反例 1（拒绝路径偷 `held_attempts` 更新）与反例 2（成功路径不更新配额）都真的能变红。`workspace_quotas` 的列覆盖是**完整**的。

但我实测出两条**假绿**，都是 `#709` 那个盲区的同类：

1. **节点/实例预算泄漏完全看不见（严重）**：准入在同一个事务里还就地更新 `host_nodes.held_*` 与 `engine_instances.held_slots`（`admission_sql.py:323`、`admission_sql.py:333`），而准入角色对这两张表的这些列**有 UPDATE 权限**（`public_job_admission_grants.py:88-89`）。我在拒绝路径上提交一次 `host_nodes` 的 `held_unified_physical_memory_bytes + 1`，实测 `held` 确实从 15 涨到 16、`version` 3→4，而 `_snapshot()` 判定相等，目标文件仍然 **5 passed**。
2. **`_expect_reservation()` 只看目标 workspace 那一行（中等）**：它只比较被准入的 workspace 行与配额表行数（`test_public_job_admission_http_pg.py:188-193`）。我在成功路径上额外给"没被准入的另一个 workspace" `held_attempts + 1`，实测 `workspace-test` 1→2，目标文件仍然 **5 passed**。

`speech_workspaces` 的列采集也不完整：`display_name` 是可写列（`workspaces_postgres.py:297`）却没进快照。不过准入角色对该表**没有任何 UPDATE 授权**，所以这条目前只是纵深防御缺口，不是可被产品代码触发的泄漏（详见第一节）。

必须修清单见第四节。不修 1 的话，issue #709 想防的"只改计数的拒绝泄漏"在节点/实例预算上原样存在。

---

## 时间线

| 时点 | 动作 | 结果 |
| --- | --- | --- |
| T0 | `git fetch`、`git worktree add /private/tmp/sn713 -b review/713-quota 62564255`、读 `tests/integration/AGENTS.md` | head 只有 1 个提交，`git diff --name-status 7147f306..HEAD` 只有目标测试文件 |
| T1 | 读迁移与 schema：`migrations/0001_cluster_coordination_draft.sql`、`asset_schema.py` | `workspace_quotas` 6 列（PK + 5）；`speech_workspaces` 6 列（PK + 5，其中 `display_name` 可写） |
| T2 | 读准入实现：`public_job_admission.py`、`admission_sql.py`、`public_job_admission_grants.py` | 配额 CAS 在 `admission_sql.py:314`；同事务还写 `host_nodes:323`、`engine_instances:333`；角色 UPDATE 授权正好覆盖这些列 |
| T3 | 建隔离库（本机 PG 18.6）+ 复用隔离 MySQL 8.4.8，跑目标文件 | **5 passed**（作者基线一致） |
| T4 | 跑相邻公开 Job 套件（原子准入 / public jobs http / list / retry sse / pin http） | **64 passed**（作者基线一致） |
| T5 | CI artifact 独立下载：run `43518162133` 六个 `integration-shard-*` | 本文件 5 个 nodeid 分布在分片 1/2/3，全部 `passed`，`executed=true`；`source_head=a1ea5397…` 是 PR 的 merge ref |
| T6 | 变异 M1（拒绝路径偷 `held_attempts+1`）、M2（只改 `policy_revision`） | 都红，失败点是整快照相等，diff 落在配额值上 |
| T7 | 变异 M3（成功路径不占配额）、M4（`held_attempts` +2）、M4b（`version` 不前进） | 各 4 红 1 绿，失败点都是 `_expect_reservation()` 的值比较 |
| T8 | 变异 M5（拒绝路径偷 `host_nodes` 预算）、M6（成功路径顺手占别的 workspace） | 都 **5 passed** —— 假绿，见第三节 |
| T9 | 静态检查：`ruff check`、`ruff format --check`、`mypy --platform linux`、`lint-imports`；`_counts` 与范围核对 | 全绿；`_counts` 与 `backend/src`、`uv.lock` 零改动 |

---

## 一、核心问题：快照的列够不够

### 1.1 `workspace_quotas`：够

`migrations/0001_cluster_coordination_draft.sql:21-29` 定义 6 列：`workspace_id`（PK）、`max_attempts`、`held_attempts`、`quarantined_attempts`、`policy_revision`、`version`。快照采集 5 个非主键列，主键进 key（`test_public_job_admission_http_pg.py:122-128`、`:135-142`）。没有遗漏列。表级 CHECK `held_attempts <= max_attempts - quarantined_attempts` 的三个变量也都在快照里。

准入对这张表的就地写只有一处：`admission_sql.py:314` 的 `held_attempts = %s, version = version + 1`，且执行该写的角色只被授予 `held_attempts`、`version` 两列的 UPDATE（`public_job_admission_grants.py:87`）。也就是说：**这张表上拒绝路径唯一可能偷写的两列，恰好都被采到了**。这一点作者的说法准确。

### 1.2 `speech_workspaces`：不全，但当前被授权边界兜住

`asset_schema.py:6-13` 定义 6 列：`workspace_id`（PK）、`display_name`、`workspace_state`、`metadata_revision`、`policy_revision`、`created_at_us`。快照采集 `workspace_state`、`metadata_revision`、`policy_revision` + 行数（`test_public_job_admission_http_pg.py:132`）。

- **漏了 `display_name`**，而它是可写列：全仓库对 `speech_workspaces` 只有一条 UPDATE，`workspaces_postgres.py:297-300` 会改 `display_name`、`workspace_state`、`metadata_revision`。作者说"该表只有 `workspace_state`/`metadata_revision`/`policy_revision`"这一步**不准确**。
- `created_at_us` 不可变，漏掉可以接受。
- `policy_revision` 在本仓库**没有任何写路径**（准入只读它，`public_job_admission.py:163`），采它属于无害的纵深防御。

不过这条缺口目前**不可被产品代码触发**：准入角色对 `speech_workspaces` 只出现在 `_READ_TABLES`（`public_job_admission_grants.py:19`），没有任何列级 UPDATE 授权。所以它是"采集不完整"，不是"已经存在可见泄漏"。

### 1.3 真正的洞：`host_nodes` / `engine_instances` 的就地预算计数

准入是一次完整配方预留：配额之外还在同一事务里写节点与实例预算。

- `admission_sql.py:323`：`UPDATE host_nodes SET held_<dimension> …, version = version + 1`
- `admission_sql.py:333`：`UPDATE engine_instances SET held_slots = %s, version = version + 1`
- 授权：`public_job_admission_grants.py:88-89` 明确把 `host_nodes` 的 6 个 `held_*` 与 `version`、`engine_instances` 的 `held_slots` 与 `version` 授予准入角色。

这两张表**既不在十表行数里，也不在新增的两表快照里**。判别探针实测（实跑，无变异，成功路径）：

```console
PROBE_HOST_BEFORE [{'host_node_id': 'host-test', 'held_unified_physical_memory_bytes': 15,
                    'held_cpu_decode_millicores': 110, 'held_host_pcm_buffer_bytes': 6,
                    'held_scratch_disk_bytes': 27, 'version': 3}]
PROBE_HOST_AFTER  [{'host_node_id': 'host-test', 'held_unified_physical_memory_bytes': 18,
                    'held_cpu_decode_millicores': 130, 'held_host_pcm_buffer_bytes': 8,
                    'held_scratch_disk_bytes': 32, 'version': 4}]
PROBE_INSTANCE_BEFORE [{'instance_id': 'instance-new', 'held_slots': 0, 'version': 2},
                       {'instance_id': 'instance-test', 'held_slots': 1, 'version': 1}]
PROBE_INSTANCE_AFTER  [{'instance_id': 'instance-new', 'held_slots': 1, 'version': 3},
                       {'instance_id': 'instance-test', 'held_slots': 1, 'version': 1}]
PROBE_HOST_MOVED True
PROBE_INSTANCE_MOVED True
```

即：成功路径确实在就地移动节点与实例的占用计数（`held_unified_physical_memory_bytes` 15→18，`held_slots` 0→1），而这 4 组数字**一个都没进快照**。`_expect_reservation()` 只用 `workspace_quotas` 的 `held_attempts`/`version` +1 作代理，所以"预算记账整体失效"或"拒绝路径泄漏节点预算"都仍然不可见。

**结论一句话：`workspace_quotas` 的列够；"跨层副作用快照"整体不够——准入真正预留的另一半（节点/实例预算）没有采。**

---

## 二、九条逐条判定

| # | 判据 | 判定 | 依据 |
| --- | --- | --- | --- |
| 1 | 采集的列是否够 | **部分通过** | `workspace_quotas` 5/5 完整；但同事务的 `host_nodes.held_*`、`engine_instances.held_slots` 未采且准入角色有写权限 → 实测假绿（M5） |
| 2 | `speech_workspaces` 采得对不对 | **部分通过** | 漏可写列 `display_name`；`policy_revision` 无写路径属冗余；当前无跨层泄漏可能（角色无 UPDATE 授权） |
| 3 | 反例 1 是否真咬住 | **通过** | 实跑 M1 红，失败断言是 `assert _snapshot(db) == before`，diff 为 `held_attempts: 2 != 1`；自造 M2（只改 `policy_revision`）同样红 |
| 4 | 反例 2 是否真咬住 | **通过** | 实跑 M3 4 红 1 绿，红的断言是 `_expect_reservation()` 的配额行比较，`held_attempts: 0 != 1`，非连带失败 |
| 5 | 有没有假绿（变了但变错了） | **通过（针对配额列）** | M4（`held_attempts` +2）= 4 红；M4b（`version` 不前进）= 4 红；都是精确值比较，不是"只要变了就算过" |
| 6 | `_counts()` 未被改动 | **通过** | `git diff 7147f306..62564255` 里没有 `test_public_job_atomic_admission_pg.py`；`_counts` 仍在 `test_public_job_atomic_admission_pg.py:253`，仍只数行 |
| 7 | 范围 | **通过** | 只有 1 个文件变动；`backend/src` diff 为空；`uv.lock` diff 为空；分支只有 1 个提交 |
| 8 | CI 执行证据真实性 | **通过（已独立下载复核）** | run `43518162133` 的六个 `integration-shard-*`：`executed=true`、`exit_code=0`，本文件 5 nodeid 全 `passed`；gate 日志“2753 collected, 2731 passed, 22 skipped, source a1ea5397…” |
| 9 | 用例间顺序耦合 | **通过** | 每个用例 `_seed()` 后重取基线，fixture 每用例新建随机 schema；逐 nodeid 单跑 5×1 passed，倒序 5 passed |

### 逐条补充说明

**第 3 条：作者把反例 1 的红色归给 `test_text_above_the_plan_ceiling…`，我没能复现到那个 nodeid，但检测能力成立。** 我把泄漏放在准入拒绝的统一出口（`public_jobs_postgres.py:597`，事务已回滚后另开一个已提交事务），红的是 `test_create_without_a_provisioned_workspace_quota_is_refused_without_rows`。原因可查：4097 字符那条路径不是 `PublicJobAdmissionRejectedError`，而是 `public_job_admission.py:678` 构造 `SpeechPlan` 抛出的**裸 `ValueError`**（`domain/speech_synthesis.py:90-92`），它由连接上下文管理器回滚（`public_jobs_postgres.py:972-986`）后冒泡到 `interfaces/http/jobs.py:508` 映射成 400，因此不经过我插入的拒绝出口。也就是说：那条路径今天**不存在可提交的写入窗口**，作者的归属要么插在别的注入点，要么只是记错 nodeid；不管哪种，`assert _snapshot(db) == after_limit` 这一断言形式本身是有牙的（M1/M2 已证），且它与另外两条拒绝用例的断言是同一个式子。

**第 4 条：作者的"4 红"我复现为 4 红，但要看失败原因不是连带。** 我的第一版 M3 直接把 `SET` 子句改成 `SET version = version`，留下 3 个绑定参数配 2 个占位符 → `psycopg` 报错被上层映射成 503，4 红但红在 `assert created.status_code == 202`，这是**伪证**（错在参数绑定而不是配额没更新）。改成"占位符自洽、`held_attempts` 原地不动、`version` 仍前进"的干净变异后，4 条的失败点才都落在 `_expect_reservation()` 的配额行比较，项目符号形如 `{'held_attempts': 0} != {'held_attempts': 1}`，与作者转述的数值一致。

**第 5 条：这里"有牙"仅限配额列。** 精确值比较（不是 `!=`）能挡住 `+2` 与 `version` 不变；但 M6 证明它只看**被准入的那一行**，另一个 workspace 被多占一次不会被发现。

**第 8 条：artifact 里的 `source_head` 与 PR head 不一致是正常的，不是伪造。** `source_head() = git rev-parse HEAD`（`scripts/dev-env/python-coverage.py:103-104`），`pull_request` 事件 checkout 的是 merge ref。`git cat-file -p a1ea5397` 显示其第二个 parent 正是 `62564255`，第一个 parent 是当时的 base `7147f306`，即 `a1ea5397 = Merge 62564255 into 7147f306`。gate 日志同时给出 2753/2731/22 的合并结论，六分片证据自洽。

---

## 三、我独立复现的反例（实跑）

所有变异只作用于临时 worktree `/private/tmp/sn713-mut`，跑完 `git checkout -- backend/src/` 还原；未 push。

| 编号 | 变异 | 期望 | 实测 | 失败断言 |
| --- | --- | --- | --- | --- |
| M1 | 拒绝出口提交 `UPDATE workspace_quotas SET held_attempts = held_attempts + 1` | 红 | **1 failed, 4 passed** | `assert _snapshot(db) == before`；`held_attempts: 2 != 1` |
| M2 | 拒绝出口提交只改 `policy_revision` 的 UPDATE（同时把该列加入角色授权） | 红 | **1 failed, 4 passed** | 同上；`policy_revision: 'policy-test-leak' != 'policy-test'` |
| M3 | 成功路径不占配额（`held_attempts` 原地不动、`version` 仍 +1） | 4 红 | **4 failed, 1 passed** | `_expect_reservation()`；`{'held_attempts': 0} != {'held_attempts': 1}` |
| M4 | 成功路径多占一个（`held_attempts = %s + 1`） | 4 红 | **4 failed, 1 passed** | `_expect_reservation()` 精确值比较 |
| M4b | 成功路径 `version` 不前进（`held_attempts` 正确） | 4 红 | **4 failed, 1 passed** | `_expect_reservation()` 精确值比较 |
| M5 | 拒绝出口提交 `host_nodes` 预算 `+1`（角色本来就有该列授权） | 应红 | **5 passed（假绿）** | 无断言失败；探针显示 `held_unified 15→16`、`version 3→4`，`_snapshot()` 仍相等 |
| M6 | 成功路径额外给"别的 workspace" `held_attempts + 1` | 应红 | **5 passed（假绿）** | 无断言失败；探针显示 `workspace-test` 1→2、`workspace-default` 0→1 |

M1 失败输出摘录（`assert _snapshot(db) == before` 是配额值，不是别的）：

```text
>           assert _snapshot(db) == before
E           AssertionError: assert {'rows': {'sp...vision': 1}}}} == {'rows': {'sp...vision': 1}}}}
E             Differing items:
E             {'workspace_quotas': {'count': 1, 'rows': {'workspace-test': {'max_attempts': 2,
E               'held_attempts': 2, ...}}}} != {'workspace_quotas': {'count': 1, 'rows':
E               {'workspace-test': {'max_attempts': 2, 'held_attempts': 1, ...}}}}
```

M2 失败输出摘录（更小的改动，只有 1 个字符串列）：

```text
E             {'workspace_quotas': {'count': 1, 'rows': {'workspace-test': {...,
E               'policy_revision': 'policy-test-leak', ...}}}} != {'workspace_quotas':
E               {'count': 1, 'rows': {'workspace-test': {..., 'policy_revision': 'policy-test', ...}}}}
```

M4（`+2`）失败输出摘录：

```text
>       assert after["workspace_quotas"]["rows"][workspace_id] == {
            **previous,
            "held_attempts": previous["held_attempts"] + 1,
            "version": previous["version"] + 1,
        }
E       AssertionError: assert {'max_attempt...cy-test', ...} == {'max_attempt...cy-test', ...}
```

M5（假绿）探针输出摘录：

```console
# 变异：拒绝路径提交 host_nodes 预算 +1；目标文件仍然是 5 passed
PROBE_REFUSAL_SNAPSHOT_EQUAL True
PROBE_REFUSAL_HOST_BEFORE [{'host_node_id': 'host-test', 'held_unified_physical_memory_bytes': 15, ..., 'version': 3}]
PROBE_REFUSAL_HOST_AFTER  [{'host_node_id': 'host-test', 'held_unified_physical_memory_bytes': 16, ..., 'version': 4}]
PROBE_REFUSAL_HOST_MOVED True
```

M6（假绿）探针输出摘录：

```console
PROBE_OTHER_WS_BEFORE [{'workspace_id': 'workspace-default', 'held_attempts': 0, 'version': 1},
                       {'workspace_id': 'workspace-test', 'held_attempts': 1, 'version': 1}]
PROBE_OTHER_WS_AFTER  [{'workspace_id': 'workspace-default', 'held_attempts': 1, 'version': 2},
                       {'workspace_id': 'workspace-test', 'held_attempts': 2, 'version': 1}]
# 目标文件：5 passed
```

---

## 四、必须修清单

### M5 级（必须修，否则 #709 的盲区仍在）

1. **把节点与实例预算记进快照**。准入在同事务就地更新 `host_nodes` 的 6 个 `held_*` 与 `version`（`admission_sql.py:323`）、`engine_instances.held_slots` 与 `version`（`admission_sql.py:333`），而准入角色对这些列有 UPDATE 授权（`public_job_admission_grants.py:88-89`）。建议 `_snapshot()` 增加两项：按 `host_node_id` 采集 `held_* + version`，按 `(instance_id, instance_generation)` 采集 `held_slots + version`；`_expect_reservation()` 同步断言成功路径的预算 +预留量。理由不是"覆盖更全"，而是**它们与 `held_attempts` 是完全同一类可被拒绝路径泄漏的就地计数，且实测泄漏后本文件全绿**。

### M6 级（必须修）

2. **`_expect_reservation()` 要断言"其它配额行没变"**。现在只比较 `rows[workspace_id]` 与配额行数（`test_public_job_admission_http_pg.py:188-193`）。最小改法：`after["workspace_quotas"]["rows"] == {**before["workspace_quotas"]["rows"], workspace_id: {**previous, "held_attempts": previous["held_attempts"] + 1, "version": previous["version"] + 1}}`。用例 3 结尾同时存在 `workspace-test` 与 `workspace-default` 两行，这个洞是可达的（M6 实测）。

### P2（建议，不阻塞）

3. **`speech_workspaces` 补 `display_name`**（`WORKSPACE_COLUMNS` 增加该列）。它是唯一未被采集的可写列；今天靠"准入角色对该表无 UPDATE 授权"兜住，补上是零成本的纵深防御。
4. **把"快照不覆盖什么"写成显式边界**。模块 docstring（`test_public_job_admission_http_pg.py:20-31`）现在读起来像"跨层副作用已全覆盖"，实际只覆盖十表行数 + 两表指定列。建议一句"节点/实例预算计数不在本快照内（见 #NNN）"，避免下一个人再次以为这里能证伪预算泄漏——这正是 #709 的成因。

---

## 五、本机复跑能力

能复跑，且与作者基线逐项对齐。

| 命令 | 结果 |
| --- | --- |
| `uv run pytest tests/integration/test_public_job_admission_http_pg.py -q` | **5 passed** |
| 相邻公开 Job 套件（原子准入 / public jobs http / list / retry sse / pin http） | **64 passed** |
| 目标文件 5 个 nodeid 各自单跑 | 5 × `1 passed` |
| 目标文件倒序跑 | **5 passed** |
| `uv run ruff check .` | `All checks passed!` |
| `uv run ruff format --check .` | `1820 files already formatted` |
| `uv run mypy --platform linux` | `Success: no issues found in 1444 source files` |
| `uv run lint-imports` | `Contracts: 9 kept, 0 broken.` |

环境差异与限制：

- **本机 PostgreSQL 18.6，CI 与作者都是 16.15**。本文件不依赖 16/18 差异特性，但"只在本机 18.6 绿"不等于"CI 16.15 绿"；CI 已给出该 head 自身证据（第 8 条），两边结论一致。
- **测试管理员用的是本机 superuser 角色**（`DROP OWNED BY` 在 16+ 需要成员关系，非 superuser 管理员在 teardown 会因权限失败）。这会影响 schema/角色的创建清理，不影响用例断言的授权语义：准入连接仍以脚本新建的最小权限角色运行。
- **未跑 MinIO / 浏览器分片**：本文件不需要；六分片 artifact 已覆盖其余前置。
- **MySQL 容器是既有的**（`speechnexus-coordination-mysql`，8.4.8，与 CI 8.4.8 同版本），只连接未启停。本文件是 `parametrize("database", ["postgresql"])`，但 fixture 要求双 DSN，故 MySQL 仅用于满足门禁前置，不产生本文件的断言证据。

---

## 六、只能依赖作者陈述或未实测的点

- **反例 1 归属到 `test_text_above_the_plan_ceiling…` 这个 nodeid**。我复现的红色落在用例 3；按代码路径推导，用例 2 的 400 是事务内裸 `ValueError` 回滚产物，不存在可提交窗口。作者的注入点未公开，只能按转述记录。
- **作者使用的"隔离 PG 16.15 容器"**：我只用本机 PG 18.6 复跑，未复刻其容器环境。
- **PR 描述里"相邻公开 Job 套件 64 passed"的文件集合**：我按 PR 描述点名的五套件跑，得到同样的 64 passed，但作者未列出完整 nodeid 清单，属于口径对齐而非逐条核对。
- **六分片之外的 `workbench-pg-full-coverage` 等 artifact**：本文件不涉及，未逐项核对。

---

## 附录 A：关键代码位置（均以被审 head `62564255` 为准）

- 快照列定义：`tests/integration/test_public_job_admission_http_pg.py:122-132`
- `_table_state` / `_snapshot`：`test_public_job_admission_http_pg.py:135-158`
- `_expect_reservation`：`test_public_job_admission_http_pg.py:176-195`
- 配额 DDL：`migrations/0001_cluster_coordination_draft.sql:21-29`
- 工作区 DDL：`backend/src/speechnexus_backend/speech/infrastructure/asset_schema.py:6-13`
- 准入就地写：`backend/src/speechnexus_backend/speech/infrastructure/admission_sql.py:314`、`:323`、`:333`
- 准入角色授权：`backend/src/speechnexus_backend/speech/infrastructure/public_job_admission_grants.py:86-90`
- 拒绝回滚出口：`backend/src/speechnexus_backend/speech/infrastructure/public_jobs_postgres.py:597`、`:972-986`、`:988`
- 计划上限抛点：`backend/src/speechnexus_backend/speech/infrastructure/public_job_admission.py:678`、`backend/src/speechnexus_backend/speech/domain/speech_synthesis.py:90-92`
- 工作区改名写路径：`backend/src/speechnexus_backend/speech/infrastructure/workspaces_postgres.py:297-300`
- 共享行数 helper：`tests/integration/test_public_job_atomic_admission_pg.py:253`

## 附录 B：变异清单（只在临时 worktree，未提交、未 push）

| 编号 | 文件 | 改动要点 |
| --- | --- | --- |
| M1 | `public_jobs_postgres.py` | 拒绝出口用准入池另开事务 `held_attempts + 1` 并提交 |
| M2 | `public_job_admission_grants.py` + `public_jobs_postgres.py` | 授权加 `policy_revision`，拒绝出口只改 `policy_revision` |
| M3 | `admission_sql.py` | 配额 `SET held_attempts = held_attempts, version = version + 1`（保持占位符自洽） |
| M4 | `admission_sql.py` | 配额 `SET held_attempts = %s + 1` |
| M4b | `admission_sql.py` | 配额 `SET held_attempts = %s, version = version` |
| M5 | `public_jobs_postgres.py` | 拒绝出口 `host_nodes.held_unified_physical_memory_bytes + 1, version + 1` 并提交 |
| M6 | `admission_sql.py` | 成功路径额外 `UPDATE workspace_quotas … WHERE workspace_id <> %s` |

每次变异后 `git checkout -- backend/src/` 还原并复核 `git status`；`/private/tmp/sn713-mut` 与探针文件在审查结束时删除。
