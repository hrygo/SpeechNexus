---
title: "配额副作用快照测试返修（PR #710 返修，head fae1c417）的定点复核"
status: active
version: "0.1.0"
date: 2026-11-14
---

# 配额副作用快照测试返修的定点复核（PR #710 / `62564255..fae1c417`）

- **复核对象**：`fae1c4177dfa8d0efcd82e0e50970de602c224ea`（`test: 拒绝路径快照补齐 host_nodes 预算与 engine_instances 槽位 (#709)`），相对上一轮被审 head `62564255`：+138/−37，仍只动 `tests/integration/test_public_job_admission_http_pg.py`。
- **上一轮结论**：报告已合入 `4ea3f208`（`docs/reviews/2026-11-13-quota-snapshot-tests.md`），要求 P0（节点/实例预算进快照）、P1（其它 workspace 配额行未变）、P2（`display_name`、docstring 边界）。
- **基线**：复核开始时 `origin/main` = `7e09b4db`（#701 已合入，改过同一测试文件）；返修分支未 rebase，仍基于 `7147f306`。
- **工作区**：`/private/tmp/sn713d`（`fae1c417`）；变异在 `/private/tmp/sn713m`（同 commit，detached），每次实验后 `git checkout -- backend/src/`，未 push 任何变异。
- **隔离环境**：本机 PostgreSQL 18.6 一次性库 `sn713_review` + 既有隔离 MySQL 8.4.8 容器（只连接，未启停）。DSN 只经环境变量传入。
- **方法**：只做定点复核：读 delta、按授权面核列、**独立复现上一轮 M5/M6**、自造 7 组针对性变异（含 3 条 `_expect_single_row_moved` 假绿试探）、核 docstring 与实现、复核 shard 3 超时定性、试合并。
- **报告日期**：沿用任务链的 `2026-11-13` 命名；实际执行 2026-11-14（+08:00）。

> 事实分层：标「实跑」的都有本机输出；仅推导的（如宿主节点身份盲区在单节点 fixture 下不可达）单列说明。

---

## 结论

**P0/P1 都真的堵住了，抖动定性成立，但合并前必须 rebase（2 处冲突，其中 1 处是语义冲突），docstring 还有 2 处与实现不一致，并漏声明一个身份盲区。**

- P0：上一轮 M5（拒绝路径偷 `host_nodes` 预算）现在**红**，diff 精确落在 `host_nodes`；我自造的「只改一个 `held_<dimension>`」也红。`HOST_COLUMNS`（6 个 `held_*` + `version`）与 `INSTANCE_COLUMNS`（`held_slots` + `version`）与准入角色的 `_UPDATE_COLUMNS` 授权面**逐字相符**，论证成立。
- P1：上一轮 M6 现在**红**，命中「a workspace other than the admitted one had its quota row changed」；两行非目标 workspace 同时被改也红。
- `_expect_single_row_moved`：`version` 不前进、槽位 +2 都红；但**不钉身份**——把槽位记到「不是本次准入的实例」上，5 个用例里 **4 个仍然绿**（当前 fixture 有 2 个实例行，这条盲区是活的）。
- docstring：边界声明方向正确、比上一版诚实得多，但有两处事实不符：`host_nodes` 根本没有 `peak_*`/`steady_*` 族（那是 `engine_instances` 的列）；「五个表 + 十份计数」实为**四个表** + 十份计数。
- shard 3 超时定性成立：日志写明 `timed out after 1500.3 s`、`60% — completed 293 of 492 (289 passed, 0 failed, 4 skipped)`，且本文件在该分片只有 1 个用例、排在第 318/492 位——挂起发生在它之前的 `test_profile_http.py` 用例上。属位置+归属推断，根因未定位。
- **合并会冲突**：试合并 `fae1c417` 到 `7e09b4db` 得 2 处冲突，其中第 2 处是语义冲突（main 已把 4097 字符的拒绝改成 422 `unsupported_parameter`，PR 仍断言 400 `invalid_job_request`）。

---

## 五项逐条

| # | 判据 | 判定 | 一句证据 |
| --- | --- | --- | --- |
| 1 | P0 是否堵住 M5 | **成立** | 实跑拒绝路径提交 `host_nodes` 预算 `+1,version+1` → 1 failed，失败断言 `assert _snapshot(db) == before`，diff 为 `held_unified_physical_memory_bytes: 16 != 15`；自造「只改 `held_host_pcm_buffer_bytes`」同样红 |
| 2 | 列清单是否够 | **成立（附边界说明）** | `HOST_COLUMNS`/`INSTANCE_COLUMNS` 与 `public_job_admission_grants.py:88-89` 的授权面逐字相符；create 请求的写全在准入池，读连接是 `SET TRANSACTION READ ONLY`（`public_jobs_postgres.py:940`），故"授权面=写面"成立 |
| 3 | P1 是否堵住 M6 | **成立** | 成功路径多改另一个 workspace → 1 failed，红在 P1 断言；自造两行非目标 workspace 同时被改 → 也被同一条断言抓住 |
| 4 | `_expect_single_row_moved` 是否假绿 | **部分成立** | 方向 (a) 错节点/错实例：**不钉身份**，错实例时 4/5 绿（新绿）；方向 (b) `version` 不变：红；维度错配同总量：绿，但已在 docstring 声明 |
| 5 | docstring 边界是否如实 | **基本成立，2 处不实** | 声明与实现的覆盖项一致；但 `peak_*`/`steady_*` 族不在 `host_nodes`（属 `engine_instances`），"five tables" 实为 4 个表 |

---

## 我实跑的变异（全部在临时 worktree，跑完还原）

| 编号 | 变异 | 期望 | 实测 | 失败断言/说明 |
| --- | --- | --- | --- | --- |
| M5 | 拒绝出口提交 `host_nodes` 全预算 `+1, version+1` | 红 | **1 failed, 4 passed** | `assert _snapshot(db) == before`；`held_unified…: 16 != 15` |
| M5b | 拒绝出口只加 `held_host_pcm_buffer_bytes + 1` | 红 | **1 failed, 4 passed** | 同上（`host_nodes` 分组不同）；单维最小泄漏也可见 |
| M6 | 成功路径额外给非目标 workspace `held_attempts + 1` | 红 | **1 failed, 4 passed** | P1：`a workspace other than the admitted one had its quota row changed` |
| M6b | 成功路径同时改**两行**非目标配额 | 红 | **探针：CAUGHT** | 同一 P1 断言；三行配额（1 目标 + 2 非目标）逐个比较 |
| M4b | `host_nodes` 的 `version` 不前进（held 正确） | 红 | **4 failed, 1 passed** | `host budget: host-test version 3 -> 3` |
| M4d | 实例槽位 `held_slots = %s + 1` | 红 | **4 failed, 1 passed** | `unexpected instance slot movement: {'held_slots': 2}` |
| M4c | 两个维度错配、总量正确（`unified +1`、`cpu -1`） | ？ | **5 passed** | 只要求"至少一个计数增长"，已在 docstring 声明 |
| M4e2 | 槽位记到**不是本次准入的实例**（`held_slots + 1`） | 应红 | **4 passed, 1 failed** | **假绿**：唯一失败是重放用例第二次创建 503，与身份无关 |
| M4a | 预算记到**另一台 host**、准入节点不动 | 应红 | **探针：FALSE GREEN** | 需先插一行备用 host；`host-test` 停在 15/3，`host-spare` 15→18、3→4，`_expect_reservation` 通过 |

M4a 探针输出摘录：

```console
PROBE_WRONG_NODE_HOSTS_BEFORE {'count': 2, 'rows': {'host-test': {... 'held_unified_physical_memory_bytes': 15, 'version': 3},
                                                    'host-spare': {... 'held_unified_physical_memory_bytes': 15, 'version': 3}}}
PROBE_WRONG_NODE_HOSTS_AFTER  {'count': 2, 'rows': {'host-test': {... 'held_unified_physical_memory_bytes': 15, 'version': 3},
                                                    'host-spare': {... 'held_unified_physical_memory_bytes': 18, 'version': 4}}}
PROBE_WRONG_NODE_FALSE_GREEN True
```

M6b 探针输出摘录：

```console
PROBE_TWO_ROWS_AFTER  {'count': 3, 'rows': {'workspace-default': {... 'held_attempts': 1, 'version': 2},
                                            'workspace-test': {... 'held_attempts': 2, 'version': 1},
                                            'workspace-a': {... 'held_attempts': 1, 'version': 1}}}
PROBE_TWO_ROWS_CAUGHT a workspace other than the admitted one had its quota row changed
```

---

## 关于第 2 条：列清单论证是否成立

- `HOST_COLUMNS = (held_<6 个 DIMENSIONS>, version)`（`tests/integration/test_public_job_admission_http_pg.py:173`）与 `INSTANCE_COLUMNS = (held_slots, version)`（`:177`），正好等于 `public_job_admission_grants.py:88-89` 里准入角色的 `_UPDATE_COLUMNS`。`DIMENSIONS` 也是从 `coordination_grants.py:13` 导入，不是手抄。
- **授权面之外有没有别的路径在拒绝时写**：本请求的写入全部走 `admission_pool`（`public_jobs_postgres.py:965-967` 是 `create` 唯一的事务入口）；同一个 store 的 `_pool` 连接只用于读，且显式 `SET TRANSACTION READ ONLY`（`public_jobs_postgres.py:940`），提交不了写。更宽的 `coordination_grants.SPEECH_UPDATES` 确实给了 `host_nodes.node_state`、`quarantined_*`、`engine_instances.instance_state` 等列（`coordination_grants.py:24-28`），但那是 lease release / 观察 / 注册路径的角色，不参与 create。测试装配里 `_app` 把 `job_writer_database_url` 指向管理员 DSN，但 `jobs` 被注入后 `JobRuntime` 不会构建（`backend/src/speechnexus_backend/app.py:505-508`），该池不参与 create。**因此"授权面=本请求写面"成立。**
- **`engine_instances` 只有两列是否够**：对本请求够（准入只写 `held_slots`/`version`）。该表还有 `quarantined_slots`、`instance_state`、`peak_*`/`steady_*`/`per_slot_*`，它们在别的请求路径被写，不在本模块范围；docstring 已用"only `held_slots` / `version` are"声明。
- **`host_nodes` 的 `allocatable_*`/`quarantined_*`**：声明为"不比较"是对的（`coordination_schema.py` 的 host_nodes 只有 `allocatable_*`、`held_*`、`quarantined_*` 三族 + 元数据列），且拒绝路径确实不写它们（准入只赋 `held_*`）。**但 `peak_*`/`steady_*` 是 `engine_instances` 的列**，docstring 把它们和 `allocatable_*`/`quarantined_*` 一起算作 `host_nodes` 的"未比较族"，是事实错误（不影响覆盖率结论，只影响声明准确性）。

---

## 关于第 5 条：docstring 与实现一致性

**做到的都声明了**：十份行数、`workspace_quotas` 5 列、`speech_workspaces` 4 列（含新增 `display_name`）、`host_nodes` 6 个 `held_*` + `version`、`engine_instances` 2 列；明确排除 `resource_pools`、`runtime_instance_heads`/`intents`、`submit_intents`、派发/执行账本（`submit_intents` 是真实存在的表）。失败语义、用例 4 的 262144 绑定、#690/#702/#703 的归属都保留。

**两处声明不实**（都是措辞，不是覆盖率）：

1. `tests/integration/test_public_job_admission_http_pg.py:49`：把 `peak_*`、`steady_*` 归到 `host_nodes`；实际这两族只在 `engine_instances`。建议改成"`host_nodes` 的 `allocatable_*`/`quarantined_*` 与 `engine_instances` 的 `quarantined_slots`/`instance_state`/`peak_*`/`steady_*`/`per_slot_*` 不比较"。
2. 同处 `:44`："the five tables and ten counts" —— 快照实际是**四个表** + 十份行数（`workspace_quotas`、`speech_workspaces`、`host_nodes`、`engine_instances`）。

**未声明的身份盲区**：`_expect_reservation` 断言"恰好一行移动、`version` +1、计数只增不减、至少一个增长"，但不钉"哪一行"。实例维度今天就有两个实例行，把槽位记错实例时 4/5 绿（M4e2）；宿主维度当前 seed 只有一台节点，所以 M4a 需要先插一行备用节点才可复现。这不是本次返修引入的问题（上一轮同样没钉），但 docstring 说"some host budget held"时容易被读成已覆盖，建议在边界里加一句"success 断言钉的是'恰好一行移动'，不钉是哪一台节点/哪一个实例"。

---

## 关于 shard 3 超时定性

复核 run `43523610879`（attempt 2 全绿）。attempt 1 的 `Integration 3` 失败，日志原文：

```text
probe subprocess timed out after 1500.3 s (limit 1500 s); pytest log: .runtime/team-c/pg-qapx3b7c/pytest.log
pytest progress: 60% — completed 293 of 492 selected cases (289 passed, 0 failed, 4 skipped); 199 not completed
timeout budget: probe limit 1500 s inside job limit 1800 s; 300 s (16%) beyond the probe ceiling ...
```

分片顺序（attempt 2 同名 artifact，`selected` 分区确定）核到：`selected[293]` = `test_profile_http.py::test_real_create_history_original_replay_conflicts_and_lifecycle[postgresql]`，`selected[317]`（第 318/492 位）= 本文件的 `test_create_without_a_provisioned_workspace_quota_is_refused_without_rows`。即挂起发生在与本 diff 无关的 profile 用例上，本文件在该分片唯一的用例还没开始；重跑同分片 492 例 255 s 全绿。

**定性成立**（位置 + 文件归属 + 同分片重跑对照），但**根因未定位**：只证明"卡住的不是本改动"，不证明"与本改动无关"的因果——这是归纳，不是证明。作者据此把它当抖动处理，在"该分片 60% 前全是无关用例、0 failed"的前提下可以接受。

attempt 1 的 artifact 已被重跑覆盖（下载返回 128 字节错误体），失败分片的 823 字节 artifact 无法再取；本结论依赖日志。

---

## 合并冲突

试合并（`7e09b4db` ← `fae1c417`）：`CONFLICT (content)`，同一文件 **2 处**冲突。

1. 模块 docstring：main（#701 后）的"Each case asserts … row deltas …"段落 vs PR 的"write surface"段落，两者都重写了同一段。
2. **语义冲突**（必须人工决策）：4097 字符用例 —— main 断言 `422 unsupported_parameter` 且用 `_counts(db) == after_limit`，PR 断言 `400 invalid_job_request` 且用 `_snapshot(db) == after_limit`。正确解法是取 main 的状态码/错误码 + PR 的 `_snapshot`，并顺手更新 PR 新写的边界文字（4097 现在由契约层拒绝、不进准入事务）。

其余改动（helpers、`_expect_reservation`）自动合并。**结论：合并前必须 rebase；不是机械冲突。**

---

## 可否合并

**先 rebase 解决那 2 处冲突（第 2 处按 #690 的 422 语义收敛），并把 docstring 两处措辞改正，即可合并。** P0/P1 的实质保护经独立复现已成立；剩下的身份盲区建议随同一次改动补一句声明，不阻塞。

## 附：本机复跑

- `uv run pytest tests/integration/test_public_job_admission_http_pg.py -q`（`fae1c417`）：**5 passed**
- 变异矩阵与探针输出见上表；原始日志留在 `/private/tmp/sn713-evidence-delta/`（仓库外，临时目录删除后仍可查）

## 附：代码位置（均以 `fae1c417` 为准）

- 快照结构：`tests/integration/test_public_job_admission_http_pg.py:186-220`
- 列清单：`tests/integration/test_public_job_admission_http_pg.py:162-179`
- `_expect_single_row_moved`：`tests/integration/test_public_job_admission_http_pg.py:233-260`
- `_expect_reservation`（含 P1 断言）：`tests/integration/test_public_job_admission_http_pg.py:263-302`
- 准入授权面：`backend/src/speechnexus_backend/speech/infrastructure/public_job_admission_grants.py:86-90`
- 就地写：`backend/src/speechnexus_backend/speech/infrastructure/admission_sql.py:314`、`:321-330`、`:332-339`
- 只读读连接：`backend/src/speechnexus_backend/speech/infrastructure/public_jobs_postgres.py:940`
