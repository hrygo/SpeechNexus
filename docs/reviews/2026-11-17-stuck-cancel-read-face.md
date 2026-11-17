# 取消卡住读面区分的非作者审查（PR #951 / issue #852）

审查对象：PR #951 `fix/stuck-cancel-capacity`。
第一轮 head `4ed1f379d98608ca8b44abd95bef16cfae8574e4`；第二轮（返修第 1 轮）head
`f89c78a6b812d05cbbb62b2658c108fb5d56d062`（父 `4ed1f379`），第二轮增量复核见 §8。
第一轮比对基线 `origin/main` = `5ffb4eb7`，`git merge-base origin/main HEAD` = `94a76674`；三点 diff
共 5 个文件（`backend/src/speechnexus_backend/speech/infrastructure/readiness.py`、
`docs/components/production-jobs.md`、`docs/operations/async-job-runbook.md`、
`tests/integration/test_cancel_failure_slot_pg.py`、`tests/integration/test_completed_release_pg.py`），
**没有 `deploy/**` 改动**。审查人不是作者，未修改被审代码，也未合并任何 PR。

审查 worktree：`/tmp/sn951-review`（分支 `review/951-stuck-cancel-read-face`，自 `origin/main`
新建）。被审 worktree：`/tmp/sn951-subject`（detached HEAD = 被审 head）。数据库：独立
PG 容器 `sn951-review-pg`（`127.0.0.1:55481`，`sn951`），每例自建 schema/role 并清理；
未使用其它审查者的 `sn892-pg`。运行方式：
`UV_NO_CONFIG=1 uv sync --locked --all-packages --all-groups`，测试 DSN 为
`SPEECHNEXUS_TEST_DATABASE_URL=postgresql://…@127.0.0.1:55481/sn951`。

## 结论

**可合（第二轮，as-of `f89c78a6`）。** 第一轮（`4ed1f379`）的阻断项 B1/B2 已在第二轮闭合：
读面按同一账本时刻报出 `never claimed` / `never claimable (exhausted)` / `due now` 三类，
按 Job 去重且共用同一段 CASE；文档删除了「worker 在跑」等反推；参考窗口统一为 `exhausted`
形状并有用例；混合计数、去重、降级、截断、只读与领取后消失都有断言，M1–M5 变异均红在点名
用例上。第二轮只剩非阻断建议（§8.5），没有发现新的行为缺陷。

下面 §1–§7 保留第一轮的事实与证据，作为返修依据；§8 是第二轮增量复核。第一轮结论为
**不能合（as-of `4ed1f379`）**，当时的两个阻断项是：

- **B1（语义与文档）**：`never claimed` 编码的是「**历史上从未**领取」，不是「**当前**没有
  推进者」。首次领取后、以及账本已是 `exhausted`（永久不再领取）的行，读面都不再报这个
  标记；而 runbook 把「有阻塞明细」直接写成「worker 在跑」、把「跑一轮 `--once` 后标记
  消失」写成「推进者已经在工作」。按 runbook 自己的一轮操作就会得到「没有 worker、但读面
  看起来像有 worker」的形态——正是 #852 要消灭的静默。
- **B2（断言强度）**：把计数改成恒值 `1`、把 `_JOB_LEVEL_DEBT` 的账本指示位翻成
  `IS NOT NULL`，新增/更新的用例**全绿**。没有多受理/混合受理的计数断言，降级路径
  （`jobs: lease-unread`）的 `never claimed` 也没有任何断言。

B1 既需要改文档，也需要（或明确不做并写清）补一个可观察的「当前没有推进者 / 永久不可再
领取」信号；B2 是纯测试补齐。两项修完可以复审，不需要重做容量或状态机。

## 1. 第一轮阻断项（均已在第二轮闭合）

### B1（第一轮，已闭合）`never claimed` 是「历史从未领取」，文档却把它当「当前有没有推进者」

涉及代码与文档：

- `backend/src/speechnexus_backend/speech/infrastructure/readiness.py:338`：判定位来自
  `row[7]`，即 SQL 的 `r.job_id IS NULL`；`:386` 把它按 `len(unclaimed)` 写进括号。
- `backend/src/speechnexus_backend/speech/infrastructure/job_cancellation_reconciliation_postgres.py:56`：
  `_CLAIM` 在首次领取时 `INSERT … ON CONFLICT DO UPDATE`，所以「账本无行」确实等于
  「从未领取」；但 `:64` 的 `last_outcome IS DISTINCT FROM 'exhausted'` 意味着
  `exhausted` 行**永远不会再被领取**。
- `docs/operations/async-job-runbook.md:1296`：「有阻塞明细（…）：worker 在跑，被外部事实
  挡住」。
- `docs/operations/async-job-runbook.md:1304`：「一轮之后重读 detail：`never claimed`
  消失说明推进者已经在工作」。
- `docs/components/production-jobs.md:366`：「回答的是「这份部署有没有推进者」」。

复现 1（runbook 自己推荐的一轮 `--once`，跑完进程即退出）：用测试 harness 起参考形状
（`cancelling` + `reserved` + `executing/pending`），围栏已超过确认窗口（生产里就是卡住的
旧围栏），只调用一次 `JobCancellationReconciler.run_once`（等价于 `--once` 的一轮），
然后立刻读探针：

```
ONESHOT batch : 1 0 1
ONESHOT probe : cancellation_debt: 1 accepted cancellation(s) still hold capacity over
                1 held lease(s) (held leases: execution-unknown=1): job-523f… ->
                lease-test-stage-main(lease=reserved, execution=executing,
                subclass=execution-unknown)
ONESHOT ledger: {'attempts': 1, 'last_outcome': 'stop_unconfirmed', 'settled_at_us': None}
ONESHOT job   : cancelling held: 1
```

这一轮之后**没有任何 worker 进程在跑**（`--once` 已退出），但 detail 里已经既没有
`never claimed`、也没有任何「没有推进者」的信号；按 `docs/operations/async-job-runbook.md:1296`
的读法，运维会把这一行当成「worker 在跑，被外部事实挡住」。

复现 2（账本已是 `exhausted`，即 #852 复盘记录的形状）：在参考形状上写入
`attempts=8, last_outcome='exhausted', settled_at_us=NULL`（历史窗口留下的行），再读探针并
跑一轮对账：

```
BEFORE ledger row : cancellation_debt: 1 … (held leases: execution-unknown=1;
                    never claimed: 1): job-5847… -> lease-…(lease=reserved,
                    execution=unknown_quarantined, subclass=execution-unknown)
AFTER  ledger row : cancellation_debt: 1 … (held leases: execution-unknown=1):
                    job-5847… -> lease-…(lease=reserved,
                    execution=unknown_quarantined, subclass=execution-unknown)
run_once now_us=real-now -> claimed: 0 settled: 0
job state: cancelling lease: reserved held_slots: 1
```

`claimed=0` 是设计使然（`last_outcome='exhausted'` 被 `_CLAIM` 排除），但读面与文档都没有
说明这一点：detail 看起来与「worker 会重试的阻塞行」完全一样，runbook 的两条陈述
（worker 在跑 / 跑一轮就会推进）对这类行都是假的，而且再跑多少轮也不会推进。仓库自己的
记录说参考窗口就是这一类：`docs/reviews/2026-11-16-cancel-failure-slot-release.md:138`
记录 `ledger last_outcome=exhausted settled_at_us=NULL`，`:149` 记录 `restart claimed=0`；
`tests/integration/test_cancel_failure_slot_pg.py:5` 起的模块 docstring 也写着
"the reconciliation worker recorded `exhausted` in its private ledger, stopped claiming the
row"。而本 PR 新增测试的 docstring（`tests/integration/test_cancel_failure_slot_pg.py:722`）
却写 "The reference window had a fence and no reconciliation-ledger row"，PR #951 正文也承认
"未在真实部署窗口重放：真实行 … 的原始账本不在本机"。**同一个文件里两个 docstring 对参考
窗口的事实相反，PR 正文又把最关键的前提标为未重放**——这是本 PR 的立论证据问题，不是措辞
问题：如果真实窗口的账本有 `exhausted` 行，`never claimed` 根本不会出现，这次改动就答不了
#852 记录的那个「静默」。

为什么阻断：这不是「再多写一句文档」的边角。PR 的写集与标题都把「读面区分没有推进者 /
被外部事实挡住」当作已交付的能力；实际交付的是「是否曾经领取过」。在 runbook 推荐的
`--once` 运维方式下，一次领取就会把「没有推进者」这个信号永久消掉（至少在这条受理上），
而 `exhausted` 行从一开始就不会有这个信号。运维照着新文档做，会在最需要判断「还有没有
人在推进」的时候得到相反的暗示。

最小修法（任一组合，按作者对 #852 范围的决定）：

- 文档收窄：把 `never claimed` 明确为「截至本次读取账本仍无行（= 从未领取）」；删除
  「worker 在跑」「推进者已经在工作」这两处反推，改成「是否有进程在跑，读面不可见；
  本 detail 只能证明曾经领取过」。
- 或补可观察信号（更彻底，且不需要新索引）：两条查询已经 join 了账本，可以多取
  `r.last_outcome`、`r.next_check_at_us`，在括号里报
  `never claimable (exhausted): N` 与/或 `overdue: N`（`settled_at_us IS NULL`、
  `last_outcome IS DISTINCT FROM 'exhausted'`、`next_check_at_us <= now - grace`），
  分别对应「永久不可再领取」与「到期很久没人领取」。这两种都是当前账面事实，能把
  `--once` 退出后和 `exhausted` 行重新点名。
- 证据面：给出真实窗口的账本查询结果（或把结论改成「本次修的是 no-worker 部署形状，
  不声称覆盖历史 exhausted 窗口」），并让两个 docstring/PR 正文的参考窗口陈述一致。

### B2（第一轮，已闭合）新增断言对「计数恒值」和「降级分支」不敏感

涉及：

- `backend/src/speechnexus_backend/speech/infrastructure/readiness.py:386`（计数）与
  `:391`（`_JOB_LEVEL_DEBT` 的指示位列）。
- `tests/integration/test_cancel_failure_slot_pg.py:717`（新增用例）、
  `tests/integration/test_cancel_failure_slot_pg.py:815`（Job 级降级用例，未更新）。
- `tests/integration/test_completed_release_pg.py:714`、`tests/integration/test_cancel_failure_slot_pg.py:964`。

变异方法：在 `/tmp/sn951-subject` 内对 `readiness.py` 做单点替换，然后跑 4 条相关用例
（新增 `never claimed` 用例、`test_the_optional_reader_identity_reports_the_full_detail`、
`test_a_role_without_the_coordination_grants_still_names_the_job`、
`test_completed_release_pg.py::test_the_label_routes_this_shape_to_the_exit_that_accepts_it`），
跑完用备份还原。

```
# M3：计数改成恒值 1
scopes.append(f"{UNCLAIMED_SCOPE}: {len(unclaimed)}")
  -> scopes.append(f"{UNCLAIMED_SCOPE}: 1")
....

4 passed in 2.27s

# M5：Job 级查询的账本指示位翻转
SELECT j.job_id, NULL, NULL, NULL, NULL, NULL, NULL, r.job_id IS NULL
  -> … r.job_id IS NOT NULL
....
4 passed in 2.30s
```

两个变异都有行为差异，但没有任何用例变红。直接调用 `_describe_debt` 的对照（同一组两条
未领取受理）：

```
baseline: … (held leases: execution-unknown=2; never claimed: 2): job-a -> …; job-b -> …
mutant  : … (held leases: execution-unknown=2; never claimed: 1): job-a -> …; job-b -> …
```

Job 级降级路径的对照（`coordination=False`，读不到租约/执行表）：

```
baseline: cancellation_debt: 1 … (jobs: lease-unread=1; never claimed: 1): job-… -> lease-unread [lease detail unavailable: permission]
mutant  : cancellation_debt: 1 … (jobs: lease-unread=1): job-… -> lease-unread [lease detail unavailable: permission]
```

为什么阻断：PR 的核心承诺是「读面必须说出没有推进者」；现在**把计数写死**或**让降级路径
永远不报这个标记**，测试仍然全绿。`docs/components/production-jobs.md:362` 的降级形态
示例本身也没有更新成带 `never claimed` 的实际输出，测试与文档一起漏掉了这条路径。

最小修法：

1. 加一条多受理用例：两个 Job（或同一 Job 两租约 + 另一个 Job），一个已领取、一个未领取，
   断言 `never claimed: 1`（并保留 `2 accepted cancellation(s)`），再补一条两个都未领取、
   断言 `never claimed: 2`；这会让 M3 红。
2. 在 `test_a_role_without_the_coordination_grants_still_names_the_job` 里断言
   `(jobs: lease-unread=1; never claimed: 1)`（或至少 `never claimed: 1`），这会让 M5 红。
3. 已领取阶段的负断言建议同时断言一条正向的阻塞明细（Job → 租约、`subclass=…`），
   避免「把整句 detail 删空」也能让 `"never claimed" not in claimed` 通过。

## 2. 语义逐分支核对表

行号按被审 head `4ed1f379`。判定位为 SQL 第 8 列 `r.job_id IS NULL`；`_DEBT` 与
`_JOB_LEVEL_DEBT` 都只返回 `state='cancelling'`、围栏早于 grace（默认 900 s）、
账本未 `settled`（或无行）的受理。

| # | 形状 | 期望 | 实测 | 结论 |
|---|---|---|---|---|
| 1 | 围栏存在、账本无行、Job `cancelling`、租约 `reserved` | 报 `never claimed: N` | `… (held leases: execution-unknown=1; never claimed: 1)` | ✅ 正确（新增用例 + 复现 2 的 BEFORE） |
| 2 | 同一 Job 多租约、账本无行 | 按受理计 1，不按租约计 | `over 2 held lease(s) (held leases: execution-unknown=2; never claimed: 1)` | ✅ 计数去重正确；⚠ 单位混在「按租约计」的括号里（见 N3） |
| 3 | 账本有行、`pending`/`stop_unconfirmed`，Job 仍 `cancelling` | 不再报 `never claimed` | `… (held leases: execution-unknown=1)` | ✅ 按「曾经领取」语义正确；❌ 文档把它当「worker 在跑」（B1） |
| 4 | 第 3 种，但 worker 是 `--once` 已退出 | 应能看出「当前没有推进者」 | 与第 3 种完全相同，无任何提示 | ❌ B1；runbook 的推荐操作直接落在这里 |
| 5 | 账本行 `last_outcome='exhausted'`、`settled_at_us=NULL`、Job 仍 `cancelling` | 应说明「永久不可再领取」 | 与阻塞行相同；`run_once` 实测 `claimed=0` | ❌ B1；仓库复盘记录的参考窗口形状 |
| 6 | 账本 `settled` | 不计入债 | `WHERE (r.job_id IS NULL OR r.settled_at_us IS NULL)` 排除 | ✅ 与 `jobs.settle_cancelled` 用例一致 |
| 7 | Job 已离开 `cancelling`、租约仍 `reserved` | 轮询读面看不到，钻取查询能点名 | 探针返回 `None`；把文档第二条 SQL 原样在迁移后的 schema 上执行，返回该 Job/租约行 | ✅ 盲区已钉住；文档 SQL 列名/过滤实测有效 |
| 8 | 读不到租约/执行表（权限降级） | 仍报 Job 级 + `never claimed` | `(jobs: lease-unread=1; never claimed: 1)` | ⚠ 代码正确，用例/文档示例漏了后半段（B2/N1） |
| 9 | 未配 `SPEECHNEXUS_READINESS_CANCELLATION_DSN`（默认部署） | `unavailable (not configured)` | `test_the_reference_api_identity…` 断言相同 | ✅ 代码/既有用例；❌ 新文档说默认部署「只会出现这个形态」（N2） |
| 10 | 对账 `enabled=false`、对账启用但 worker 未运行、账本无行 | 三者读面相同（探针不读部署配置） | 都是 `never claimed: N` | ✅ 可隔离测试、不依赖部署配置；⚠ 也意味着读面分不清「没启用」与「启用了但没跑」（B1 的语义边界） |
| 11 | 96 行 × 超长 id，detail 超 512 字符 | `never claimed` 不被截断 | 输出长度 461、`kept scope: True`、`truncated: True` | ✅ 文档「不会被 detail 截断」成立 |
| 12 | 探针读取前后 | 只读、容量账不变 | `unchanged: True`；`held_slots=1, quarantined_slots=0, lease=reserved` | ✅ 没有误改租约/预算/槽位 |

容量语义专门核对：`git diff origin/main...HEAD` 在 `readiness.py` 只新增 SELECT 列和
detail 字符串，没有触达 `LeaseService`、`PublicJobs` 或配额/槽位计算；上面的快照
（第 12 行）与 `test_cancel_failure_slot_pg.py` 的 `held_slots/quarantined_slots` 断言
都保持原值。**不构成容量语义变更。**

## 3. 变异红/绿证据

命令（在 `/tmp/sn951-subject`，独立 DSN；每条变异后 `cp` 还原，收尾
`git status --porcelain` 为空）：

```bash
UV_NO_CONFIG=1 SPEECHNEXUS_TEST_DATABASE_URL='postgresql://…@127.0.0.1:55481/sn951' \
  uv run pytest \
  tests/integration/test_cancel_failure_slot_pg.py::test_the_detail_says_when_no_worker_has_ever_claimed_the_acceptance \
  tests/integration/test_cancel_failure_slot_pg.py::test_the_optional_reader_identity_reports_the_full_detail \
  tests/integration/test_cancel_failure_slot_pg.py::test_a_role_without_the_coordination_grants_still_names_the_job \
  tests/integration/test_completed_release_pg.py::test_the_label_routes_this_shape_to_the_exit_that_accepts_it \
  -q
```

| ID | 变异（单点） | 结果 | 判定 |
|---|---|---|---|
| M1 | `if unclaimed:` → `if unclaimed and False:`（不追加 scope） | `3 failed, 1 passed`（新增用例、full detail、completed release 红；Job 级用例绿） | ✅ 有用例捕获 |
| M2 | `row[7] is True` → `row[7] is False`（所有行都算未领取） | `3 failed, 1 passed`（已领取阶段的负断言红） | ✅ 有用例捕获 |
| M3 | `{len(unclaimed)}` → `1`（计数恒值） | `4 passed` | ❌ **B2** |
| M4 | `_DEBT` 的 `r.job_id IS NULL` → `IS NOT NULL` | `3 failed, 1 passed` | ✅ 有用例捕获 |
| M5 | `_JOB_LEVEL_DEBT` 的 `r.job_id IS NULL` → `IS NOT NULL` | `4 passed` | ❌ **B2** |
| M6 | `_CLAIM` 的 `LEAST(attempts + 1, 64)` → `attempts + 1` | `1 failed`（`test_lease_attempts_saturate_at_64_and_stay_claimable`） | ✅ 新回归用例捕获 |

M6 的失败被 `ReconciliationStoreUnavailableError` 泛化（CHECK 被 `psycopg.Error` 包成
「storage unavailable」），能红但不易读；见 N5。M1/M2/M4 都没能红 `test_a_role_without_…`，
因为该用例没有任何 `never claimed` 断言——这是 B2 的第二半。

新增用例本身不是空壳：已领取阶段断言了 `never claimed` 不存在，且用
`box.rows()["ledger"][0]["last_outcome"] == "stop_unconfirmed"` 证明 worker 真领取过；
`test_a_job_already_left_cancelling_is_documented_as_a_blind_spot` 对
`probe.check() is None` 与租约仍 `reserved` 有正反两侧断言；M6 证明 attempts 用例确实
钉住了 `LEAST` 回归。缺的是 B2 列出的多受理/降级两侧。

## 4. 文档核查

除 B1/B2 外，新增文档与代码的逐条核对：

| 文档陈述 | 代码/实测 | 结论 |
|---|---|---|
| `never claimed=N` 是计数括号、不被 detail 截断（`docs/components/production-jobs.md:372`） | 96 行超长 id 时保留 `never claimed: 96`，整体 461 字符且带 truncated | ✅ |
| 默认 900 s grace 大于确认窗口 + 退避（`docs/components/production-jobs.md` 同节） | `backend/src/speechnexus_backend/speech/infrastructure/readiness.py:167` 默认 `grace_seconds=900`；`confirm_timeout=300`、`max_backoff=300` | ✅ |
| 有界出入口的 300 s/3600 s、退避上限 300 s（`docs/components/production-jobs.md:497` 起表格） | `JobCancellationSettings` 默认/上限与 `JobCancellationReconciler` 的 `max_backoff_seconds=300` 一致 | ✅ 数值 |
| 第二条钻取 SQL（Job 已离开 `cancelling`） | 在真实迁移 schema 上原样执行成功，返回 `job_state=cancelled`、`lease=reserved`、`cleanup=pending`、`execution=unknown_quarantined` 的行 | ✅ 可执行 |
| 文档锚点 `#隔离不了的时候可见面与处置`（`docs/operations/async-job-runbook.md:1313`） | 标题 `#### 隔离不了的时候：可见面与处置` 存在（`docs/components/production-jobs.md:334`） | ✅ |
| `never claimed=N` 的书写（`docs/components/production-jobs.md:361`、`:366`、`:372`；`docs/operations/async-job-runbook.md:1283`、`:1294`） | 代码实际输出 `never claimed: N`（冒号） | ❌ N1：运维按 `=N` grep 会落空 |
| 「默认部署…只会出现这个形态」（`docs/components/production-jobs.md:369`） | 同表 `:363` 与既有用例：未配专用 DSN 时是 `unavailable (not configured)` | ❌ N2：默认部署没有 reader DSN，根本看不到这个形态 |
| 降级形态示例 `(jobs: lease-unread=X)`（`docs/components/production-jobs.md:362`） | 实际输出 `(jobs: lease-unread=1; never claimed: 1)` | ❌ N1：示例与代码不一致 |
| `never claimed` 在括号里「按租约计」的计数规则（`docs/components/production-jobs.md:374`-`376`） | 它按 Job 计；同一 Job 两租约实测 `over 2 held lease(s) … never claimed: 1` | ⚠ N3：单位混用，容易被读成「1 条租约」 |

另外确认：

- 第 7 行的盲区陈述与测试一致：`test_a_job_already_left_cancelling_is_documented_as_a_blind_spot`
  钉住了 `probe.check() is None` 与租约仍 `reserved`；文档第二条查询是有效读面。
- 没有把「可观察到」写成「自动收口」：新增的「有界出入口」表格明确写「有界指每一步有上界，
  不是一定会自动完成」，与 #888/#906 的既定语义一致；唯一越界的是 B1 的「worker 在跑 /
  已经在工作」两处。
- 没有把实现违约兜成 503：探针仍永远 `ok`，新增内容只进 detail；`test_the_debt_probe_never_fails_a_readiness_report`
  与 `test_the_reference_api_identity_keeps_readyz_ok_and_says_why` 均通过。

## 5. 未验证项

- **真实部署窗口未重放**：本机没有真实行 `job-6bb932069e464c8088825b572496d6b8` 的原始
  账本；PR 正文也承认这一点。`never claimed` 是否真的覆盖了 #852 的参考窗口，取决于该行
  是否有账本行（仓库旧复盘说 `exhausted`，PR 说无行，见 B1）。这是本轮最关键的未验证事实。
- **真实引擎/GPU**：全部用 fake gateway 文档与停止事实，没有真实 `unknown_quarantined`
  引擎停止、`/abort`、`/drain` 或隔离释放证词；测试只证明状态机/读面，不证明物理释放。
- **生产身份**：只在测试里用 `cancellation_debt_reader_grants` 生成的角色验证；没有在真实
  `sn_api`/独立 reader 身份上执行（`test_the_reference_api_identity…` 只覆盖 `sn_api`
  的 `unavailable (permission)` 形态）。
- **`enabled=false` 的端到端 worker 行为**：没有起真实 worker 进程配 `enabled=false`；
  探针不读这个开关，读面上与「启用但未领取」不可区分（B1 的语义边界）。
- **生产 grace（900 s）**：测试统一用 `grace_seconds=0.001`；900 s 宽限下的实际 detail
  延迟没有端到端测过（判定谓词相同，属于时序边界）。
- **MySQL/InnoDB 与 MinIO**：未运行；本改动是 PG 专用的只读查询，双库协调路径不涉及，
  但这不构成双库验收。
- **完整集成套件**：只跑了相关文件（见 §7），没有跑全量 `make integration-test`（缺
  MinIO/MySQL 配置）。
- **`deploy/**` 装配**：确认 diff 未改；默认 compose 仍无取消对账服务。这是有意留边界，
  不是本 PR 的验证项。

## 6. 非阻断建议

1. **N1（文档字面）**：把 4 处 `never claimed=N` 改成 `never claimed: N`，并把
   `docs/components/production-jobs.md:362` 的降级示例补成
   `(jobs: lease-unread=X; never claimed: N)`；否则运维的 grep/告警规则对不上实际 detail。
2. **N2（默认部署陈述）**：`docs/components/production-jobs.md:369` 的「默认部署只会出现
   这个形态」应改为「配置了 `SPEECHNEXUS_READINESS_CANCELLATION_DSN`、但未运行 worker 的
   部署会出现这个形态；未配 DSN 时是上一行的 `unavailable (not configured)`」。
3. **N3（单位）**：`never claimed` 数的是 Job，却放在「按租约计」的括号里；建议写成
   `never claimed jobs: N`，或移到括号外的独立分句。实测多租约 Job 会输出
   `over 2 held lease(s) … never claimed: 1`，容易被读成租约数。
4. **N4（文档 SQL 漂移）**：第二条钻取查询目前只有人工验证，没有用例原样执行它；建议把
   文档里的 SQL 提成一个测试常量（或在测试里跑同一段 SQL），防止列名/过滤条件随 schema
   漂移。现有测试用的是简化查询，不能代替文档 SQL 的可执行性。
5. **N5（M6 的失败可读性）**：`attempts+1` 变异被 `psycopg.Error` 包成
   `ReconciliationStoreUnavailableError`；如果能在 store 层保留 CHECK/约束名，回归失败会
   更容易定位。非本 PR 引入，但新用例正好踩到。
6. **N6（宽限未写进 runbook）**：§13.1 没提生产 grace 900 s——刚受理的取消在 15 分钟内
   不会出现在 detail 里；components 文档写了默认值，runbook 的三步判断可以补一句，
   避免运维在窗口内误判「没有债」。
7. **N7（计数上界）**：`never claimed` 的计数受 `LIMIT 96` 与本条 detail 的采样约束；
   「不会被截断」成立，但「计数是总量」不成立。若告警要按数量分级，建议在文档里点明
   这是有界样本。
8. **正向**：无需为 `enabled=false` 单独造一条测试路径——判定本来就不读部署配置，
   这是本改动的优点（可在隔离环境测试）；只要 B1 把「读面能证明什么」写准即可。

## 7. 复核环境与收尾

- 第一轮被审 head 上相关测试：`tests/integration/test_cancel_failure_slot_pg.py` **18 passed**，
  加上 `tests/integration/test_completed_release_pg.py` 共 **34 passed**；
  `tests/integration/test_debt_probe_plans_pg.py` **1 passed**。
- `ruff check` / `ruff format --check`（被改的 3 个 Python 文件）通过。
- 全部变异在 `/tmp/sn951-subject` 内完成并逐条还原；收尾 `git status --porcelain` 为空。
- 审查报告 worktree 收尾 `git status --porcelain` 为空（提交后）。
- 文档门禁：`UV_NO_CONFIG=1 uv run python scripts/dev-env/check-doc-refs.py` →
  `RESULT PASS`、`SCOPE terminal problems=0`；本文件属 `docs/reviews` 过程记录，其记录项
  按门禁设计只计数、不参与终态门禁。

## 8. 第二轮复核（`f89c78a6`）

### 8.1 范围与实跑

返修第 1 轮 head `f89c78a6b812d05cbbb62b2658c108fb5d56d062`（父 `4ed1f379`）。
`git diff 4ed1f379..f89c78a6` 共 6 个文件、+311/−67：`readiness.py`、`job_cancellation_fence_schema.py`、
两份文档、两份测试；**仍无 `deploy/**` 代码改动**。新增代码用一段共享的
`_SCHEDULE_STATE_SQL`（`backend/src/speechnexus_backend/speech/infrastructure/readiness.py:120`）
和每查询一次的数据库时钟 `_NOW_CLOCK`（同文件 `:111`），完整查询与 Job 级降级查询都引用它
（同文件 `:432`、`:456`）。

在 `/tmp/sn951-subject`（第二轮 detach 到 `f89c78a6`）与独立 PG `127.0.0.1:55481` 实跑：

```bash
UV_NO_CONFIG=1 SPEECHNEXUS_TEST_DATABASE_URL='postgresql://…@127.0.0.1:55481/sn951' \
  uv run pytest tests/integration/test_cancel_failure_slot_pg.py \
                 tests/integration/test_completed_release_pg.py -q
# 39 passed in 18.03s（cancel 23 + completed 16）

… pytest tests/integration/test_debt_probe_plans_pg.py -q
# 1 passed

… pytest tests/integration/test_stop_evidence_debt_pg.py -q
# 6 passed

uv run ruff check <4 changed .py files>        # All checks passed!
uv run ruff format --check <4 changed .py files>  # already formatted
uv run pytest tests/unit -q                     # 7513 passed, 3 skipped
```

第一轮报告里的 18/34 是本轮之前的数字；第二轮两个受影响文件合计 39 passed（另加 plan 1、
stop-evidence 6）。审查人未修改被审代码；全部变异在 `/tmp/sn951-subject` 内逐条施加并以
备份 `cmp` 还原，收尾 `git status --porcelain` 为空。

### 8.2 B1 闭合

**判定：闭合。** 三条事实现在都由同一段 CASE 在**同一数据库时刻**算出，且完整读面与 Job 级
降级读面共用该 CASE：

- `never claimed`：`r.job_id IS NULL`，即账本无行；首次领取后消失，文档明确它只描述历史，
  不再反推「现在有没有进程」（`docs/components/production-jobs.md:360` 起的表、
  `docs/operations/async-job-runbook.md:1285` 起的 §13.1 都已删掉第一轮点名的
  「worker 在跑」「推进者已经在工作」两处）。
- `never claimable (exhausted)`：`settled_at_us IS NULL AND last_outcome = 'exhausted'`；
  与 `_CLAIM` 的 `last_outcome IS DISTINCT FROM 'exhausted'` 同义，重跑不会领取。
- `due now`：未 settled、非 exhausted、`next_check_at_us <= now`；`now` 与宽限过滤共用
  `_NOW_CLOCK`，不使用进程时钟（同文件 `:111`）。

实跑混合部署（主 Job 两条租约、无账本；另 1 无账本、2 exhausted、2 due）：

```
cancellation_debt: 6 accepted cancellation(s) still hold capacity over 2 held lease(s)
(held leases: execution-unknown=2; jobs: no-held-lease=5;
 never claimed: 2; never claimable (exhausted): 2; due now: 2): …
```

主 Job 的两条租约只计 1 个 `never claimed`，三类合计 6，与 6 个受理一一对应；没有重复、
没有同一 Job 落两类。Job 级降级路径也实测三种齐全：

```
(jobs: lease-unread=1; never claimed: 1)
(jobs: lease-unread=1; never claimable (exhausted): 1)
(jobs: lease-unread=1; due now: 1)
```

参考窗口证据已统一：`tests/integration/test_cancel_failure_slot_pg.py:791`
（`test_the_reference_window_shape_is_never_claimable_again`）插入 `attempts=8`、
`last_outcome='exhausted'`、`settled_at_us=NULL`，断言 detail 只有
`never claimable (exhausted): 1`，并断言 `run_once` 的 `(claimed, settled) == (0, 0)`；
模块 docstring 与本 PR 新增测试 docstring 都改为「参考窗口是 exhausted 行」，不再与
`docs/reviews/2026-11-16-cancel-failure-slot-release.md:138` 的旧复盘冲突。B1 的语义与
证据面都闭合。

### 8.3 B2 闭合与变异

**判定：第一轮点名的 B2 项闭合；另发现一条非阻断测试缺口（M6）。** 新用例覆盖：

- 混合计数 `2 never claimed + 2 exhausted + 2 due`，同时证明按 Job 去重
  （`tests/integration/test_cancel_failure_slot_pg.py:850`）；
- `never claimed` 领取后消失，且领取后仍保留完整阻塞明细（同文件 `:751`）；
- 参考窗口 `exhausted` 不再被领取（同文件 `:791`）；
- `due now` 的正例（同文件 `:821`）；
- Job 级降级路径的 `never claimed`（同文件 `:973`）；
- 只读快照不变（同文件 `:876`）；
- 40 个长 id、detail ≤ 512、`never claimed: 40` 保留（同文件 `:892`）。

本次独立施加的变异（完整 `test_cancel_failure_slot_pg.py`，`-q --tb=line`）：

| 变异 | 实测 | 红在 |
|---|---|---|
| M1 计数恒值 `1` | `2 failed, 21 passed` | mixed、truncation |
| M2 删 exhausted 分支 | `2 failed, 21 passed` | reference-window、mixed |
| M3 删 due 分支 | `2 failed, 21 passed` | due-now、mixed |
| M4 `IS NULL` → `IS NOT NULL`（共享首支） | `7 failed, 16 passed`（作者记 8，其选择集未复核） | never-claimed、reference、due、mixed、truncation、Job 级、full-detail |
| M5 把 Job 级查询的 `schedule_state` 换成常量 `'scheduled'` | `1 failed, 22 passed` | `test_a_role_without_the_coordination_grants_still_names_the_job` |

M5 的原文说「只翻 Job 级 CASE 首支」，但当前 head 的两个查询共用同一段 CASE，没有可单独
翻转的 Job 级首支；用等价的「Job 级常量」变异复现，红色仍落在点名的降级用例上，说明该
路径的标记确实有断言。

**M6（本轮新发现，非阻断）**：把 `WHEN r.next_check_at_us <= now_clock.now_us THEN 'due'`
改成 `WHEN r.next_check_at_us IS NOT NULL THEN 'due'`，整套 23 条仍 **`23 passed`**。原因是
没有一条用例使用「`next_check_at_us` 在未来」的受理：现有 due 用例都用 `0`（远在过去）。
直接场景实测当前实现是正确的：

```
SCHEDULED detail: … (held leases: execution-unknown=1): …   # 无 due now
CLAIM before due: 0
CLAIM at/after due: 1
```

即未来 `next_check_at_us` 不会被报 `due now`，到点后 `_CLAIM` 才会领取；缺的是一条把
「未来不是 due」钉住的负例。建议补一条（`next_check_at_us = now + 1h`，断言 detail 不含
`due now`，并断言到期前 `claimed=0`、到期后 `claimed=1`），即可让 M6 变红。

### 8.4 授权变更：必需、最小、升级影响

`cancellation_debt_reader_grants` 在
`backend/src/speechnexus_backend/speech/infrastructure/job_cancellation_fence_schema.py:140`
把账本授权从 `job_id, settled_at_us` 扩为 `job_id, settled_at_us, last_outcome, next_check_at_us`。
核对结论：

- **CASE 必需**：共享 CASE 只读账本的这四列；`_DEBT`/`_JOB_LEVEL_DEBT` 均不读 `attempts` 或
  `updated_at_us`。
- **最小且只读**：`information_schema.column_privileges` 实测只有上述 4 列（无表级授权）；
  以该角色执行 `SELECT attempts` 与 `UPDATE … SET attempts=99` 均 `InsufficientPrivilege`。
  新增两列是调度状态，不含请求内容；没有新增关系或写权限。
- **升级影响**：只影响重跑授权函数，不需要迁移。以旧授权（仅 `job_id, settled_at_us`）实跑，
  `_DEBT` 与 `_JOB_LEVEL_DEBT` 都读不到新列，探针整体变成
  `cancellation_debt: unavailable (permission)`；`/readyz` 仍 `ready=True`、`status=ok`，
  同族 `stop_evidence_debt` 仍可执行（返回 `None`，不因这两列降级）。这与
  `docs/components/production-jobs.md:412`、`:419` 的升级说明一致，升级动作写明了
  「以 owner 重跑 `cancellation_debt_reader_grants`」。

**非阻断文档缺口**：`deploy/README.md:988` 与
`docs/operations/compose-minimal.md:1223` 仍把漏授权的自查信号写成
`cancellation_debt: … (jobs: lease-unread=1) …`；本次实测旧授权下是
`unavailable (permission)`，不是 Job 级降级形态。`docs/releases/mvp-0.1.0.md:161` 也仍写
「`cancellation_debt` 不受影响」，与本轮新增列的实际降级相反。`docs/components/production-jobs.md:416`
的「旧的四列授权（账本只有 `job_id/settled_at_us`）」措辞也自相矛盾（旧账本授权是两列）。
建议同步这三处；组件文档是主文档且已正确，故不阻断，但部署自检/发布说明应跟上。

### 8.5 非阻断残留

1. **M6 测试缺口**（§8.3）：未来 `next_check_at_us` 未被负例钉住；建议补一条。
2. **时钟源不同**：读面用数据库 `clock_timestamp()`（`backend/src/speechnexus_backend/speech/infrastructure/readiness.py:111`），worker 的领取
   用进程时钟 `int(time.time() * 1_000_000)`（`backend/src/speechnexus_backend/job_cancellation_runtime.py:445`）。
   过滤字段（settled/exhausted/`next_check <= now`）逐条一致，单机/NTP 同步下等价；时钟偏差时
   `due now` 可能相对 worker 提前或滞后。文档已注明 `now` 取数据库时钟，但
   `docs/components/production-jobs.md:368` 的「与 worker 的领取谓词逐条一致」宜收窄为
   「过滤条件一致」或补一句时钟口径。
3. **部署自检文档**（§8.4）：`deploy/README.md:988`、`docs/operations/compose-minimal.md:1223`、
   `docs/releases/mvp-0.1.0.md:161` 与实测降级信号不一致；组件文档
   `docs/components/production-jobs.md:416` 的「旧的四列授权」措辞需改为「旧账本两列授权」。
4. **`scheduled` 窗口未命名**：`next_check_at_us` 在未来的行不打印任何标记；文档
   （`docs/components/production-jobs.md:378` 起）说无 worker 时读面「在 never claimed 与
   due now 之间变化」，实际中间还有一段 ≤ 退避上限（默认 300 s）的无标记窗口。这不是错误
   （`due now` 只在到期后出现），但建议在文档里点明这个有界窗口。
5. **M5 的表述**：PR 正文的「只翻 Job 级 CASE 首支」在当前 head 上不可直接施加（两查询共享
   CASE）；这不影响闭合结论，但返修记录宜改成可复现的等价变异。

### 8.6 第二轮结论

**可合（第二轮）。** B1/B2 已闭合，授权变更必需、最小、只读，旧授权降级如实且不失败
`/readyz`。上面 5 条均为非阻断建议，其中 M6 是唯一一条能让测试继续变绿的变异，建议在合并
前或紧随其后补上；不构成新的行为缺陷，也不改变本轮结论。
