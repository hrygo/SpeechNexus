# 交付矩阵同步的非作者审查（PR #965）

审查对象：PR #965（`docs/tracker-sync-1117`，head `f03833bd`，父 `4f4440bb`）。审查分支
`review/tracker-sync` 自 `origin/main` `4f4440bb` 新建，本报告是该分支唯一改动。我不是作者，
未改被审文件；反例与重渲染只读或只写 `/tmp` 副本，被审工作树 `/tmp/sn965-subject` 收尾
`git status --porcelain` 为空。

**行号口径：** 下文 `file:line` 按被审 head `f03833bd` 计。GitHub 实时状态与门禁为
2026-11-17T21:20–21:30+08:00 的读数；其余时间为各命令自身的记录时间。

## 结论

**能合。没有阻断项。**

本 PR 是纯文档的状态同步：4 个文件、+91/−33，全部在 `docs/` 下；快照是采集时刻的真实事实
（快照里的 open 集合与 GitHub 实时 open 集合逐号相同，23/23），矩阵用快照自身 `fetched_at`
离线重渲染逐字节一致（退出码 0、`git diff --exit-code` 无输出），manifest 只加了 `885`/`957`
两个号，组件文档两处计数与代码的四列授权一致；文档门禁 `PASS`、`terminal problems=0`、
`RESULT OK`，CI 的「Docs: delivery matrix freshness」已绿。

唯一值得写下来的是 §9 的 N1：这次状态刷新让 O-ROLE 行的一处旧事实矛盾显形——同一行既写
`#572：closed / completed；验收见需求证据`，又写「真实端到端……仍未跑通」「真实部署端到端
仍未做」。这不是本 PR 引入的数据错误（issue 状态是真的，事实文字按授权范围一字未动），
但需要在同一收尾批次里把 #572 的关闭证据落库并改掉那句话；不构成阻塞。

## 1. diff 口径

```
$ git -C /tmp/sn965-subject diff 4f4440bb..HEAD --stat
 docs/components/production-jobs.md     |  7 +--
 docs/plans/delivery-issues.json        |  7 ++-
 docs/plans/delivery-tracking.md        | 20 ++++----
 docs/plans/github-issues-snapshot.json | 90 +++++++++++++++++++++++++++-------
 4 files changed, 91 insertions(+), 33 deletions(-)
```

`git diff --name-only` 只有这四个路径，没有代码、契约、迁移、发布说明、判定表或运行配置。
四个文件的改动逐段读过：快照是新增/删除/状态变更的整条记录，矩阵是渲染产物，manifest 是
两处 `issues` 数组，组件文档是两处计数措辞；没有夹带其它编辑。

## 2. 快照真实性

| 项 | 前 | 后 | 判定 |
| --- | --- | --- | --- |
| `fetched_at` | `2026-11-14T17:17:14+08:00` | `2026-11-17T21:18:47+08:00` | 新，与提交时间 `21:19:38+08:00` 同分钟级 |
| 记录数 | 78（open 20 + closed 58） | 82（open 23 + closed 59） | 增加 4 条，符合采集口径 |
| 新增 | — | `819`/`845`/`852`/`862`/`863`/`885`/`941`/`957`/`959` | 9 条，全部 live `OPEN` |
| 消失 | `740`/`750`/`754`/`755`/`757` | — | 5 条，live 全部 `CLOSED`，且均未被 manifest 跟踪 |
| 状态变化 | `#572` `open` | `#572` `closed` / `completed` | 与 live 一致 |
| `#885`/`#957` 父项 | `null` | `…/issues/337` | 与 live `parent=337` 一致 |

实测核对（只读）：

```
$ gh issue view 572 --json state,stateReason,parent
572 CLOSED COMPLETED parent 342
$ gh issue view 885 --json parent
885 OPEN parent 337
$ gh issue view 957 --json parent
957 OPEN parent 337
```

最强的一条时效证据是 open 集合的逐号比对，而不是用单个号抽查：

```
$ gh issue list --state open --limit 400 --json number   # live open = 23
snapshot open = 23；live-only = []；snapshot-only = []
```

即快照里的每条 open 记录都与实时一致，且实时没有快照漏采的 open issue。结构上 82 条记录的
键集完全相同（`number/html_url/repository_url/title/state/state_reason/labels/parent_issue_url`），
号序严格升序、无重复，与脚本 `_issues()` 写出的形状一致（`scripts/dev-env/refresh-implementation-tracker.py`
的 `load_issues`：全部 open 分页 + manifest 受跟踪号补采）。

PR 正文的两条口径说明也成立：`#953`/`#954`/`#829` live 已关闭且不在 manifest 受跟踪集合，
因此既不进快照也不进矩阵；这不是漏采，而是脚本口径。

## 3. 矩阵只由快照驱动（CI 同命令重渲染）

```
$ SNAPSHOT_AT=$(python3 -c 'import json;print(json.load(open("docs/plans/github-issues-snapshot.json",encoding="utf-8"))["fetched_at"])')
$ echo "$SNAPSHOT_AT"
2026-11-17T21:18:47+08:00
$ python3 scripts/dev-env/refresh-implementation-tracker.py \
    --snapshot docs/plans/github-issues-snapshot.json --snapshot-at "$SNAPSHOT_AT" \
    --output docs/plans/delivery-tracking.md
Refreshed 65 requirements: docs/plans/delivery-tracking.md
$ git diff --exit-code -- docs/plans/delivery-tracking.md
$ echo $?
0
```

渲染后 `git status --porcelain` 为空，没有残留写回。命令与 `.github/workflows/ci.yml:196-205`
的 `docs_matrix` 步骤逐字同源（同一脚本、同一 `--snapshot-at` 算法、同一 `git diff --exit-code`
目标）；CI 上该 job 在本 head 上是 `pass`（18 s）。因此矩阵没有手工编辑的成分。

表头是快照时刻而不是「实时读取」：

```
GitHub 来源：GitHub 快照；快照时间：2026-11-17T21:18:47+08:00。
```

矩阵 diff 的实质内容也与渲染一致：表头时刻、`O-ROLE` 实施列 `#572` 由 `open；状态标签待核实`
变为 `closed / completed；验收见需求证据`、`F-ASR` 实施列 `未建卡` → `#957`、`F-TTS` 实施列
`#406` 后并列 `#885`，以及「未登记的 open issue」删 5 增 7（现 8 条：`#696` + 7 张新卡）。

## 4. manifest 登记

`docs/plans/delivery-issues.json` 的 diff 只有两处数组：

| 需求 | 前 | 后 |
| --- | --- | --- |
| `F-ASR`（`:147`） | `[]` | `[957]` |
| `F-TTS`（`:185`） | `[406]` | `[406, 885]` |

受跟踪实施 issue 总数 61 → 63，与新增两个号一致；其余需求的 `issues` 数组、`facts_as_of`
（`2026-11-03`）、`epics`、`program` 全部未动。按要求逐个查过误挂：

```
817/845/852/862/863/941/959  in manifest: 全部 False
953/954/829                  in manifest: 全部 False
```

矩阵侧 `F-TTS`/`F-ASR` 的「实施卡」列确实出现 `#885`/`#957`（§3 的 diff），且 `#885` 与
已关闭的 `#406` 并列、`#957` 取代「未建卡」，与 manifest 的关联方向一致（一个实施 issue 只
登记一个需求）。

## 5. 组件文档的计数笔误

代码是四列授权：`cancellation_debt_reader_grants`（`backend/src/speechnexus_backend/speech/infrastructure/job_cancellation_fence_schema.py:107`）
对账本给出

```
GRANT SELECT (job_id, settled_at_us, last_outcome, next_check_at_us)
```

同一函数 docstring 也写明 `last_outcome`/`next_check_at_us` 是调度状态而非请求内容。改动前
`docs/components/production-jobs.md:406-408` 已把账本列列为这四列，而 `:410` 的正文说「账本
那两列」、`:416` 说「旧的四列授权（账本只有 `job_id/settled_at_us`）」——同一段自相矛盾。

改后：`:411` 明确「账本的这两列（`last_outcome`/`next_check_at_us`）」，`:417` 改为「旧的两列
授权」。语义与代码一致，`docs/releases/mvp-0.1.0.md:348` 的「旧的账本两列授权会让该探针整体
降级」也因此与组件文档同口径；文件内已无「四列授权」的残留表述，`:421` 的「这两列」在上下文中
指代明确。改动只涉及计数措辞，没有改变授权集合、查询或升级步骤。

## 6. 范围与门禁

范围：4 个文件全在 `docs/`（`docs/plans` 三件 + `docs/components` 一件），未碰代码、契约、
迁移、发布说明与判定表；被审分支相对 `4f4440bb` 只有一个提交 `f03833bd`。

门禁实跑（被审工作树内，`UV_NO_CONFIG=1`）：

| 命令 | 结果 |
| --- | --- |
| `python3 scripts/dev-env/check-doc-refs.py` | `SUMMARY refs=2088 …` / `SCOPE terminal problems=0 new=0 accepted=0` / `RESULT PASS` |
| `python3 scripts/dev-env/check-doc-statements.py` | `RESULT OK`（6 条路由 + 13 条能力声明） |
| CI `Docs: delivery matrix freshness` | `pass`（18 s，run 44091732424） |
| CI `Docs: path:line references` / `Route: scope and review boundary` / `Compose: configuration validation` / `Deploy: reference acceptance entry (dry run)` | 均 `pass` |

`check-doc-refs` 的 record 侧计数（`docs/reviews` AMBIGUOUS=132 / MISSING=24 / OUT_OF_RANGE=3，
`docs/validation` AMBIGUOUS=1，合计 problems=160）是基线里已记录的过程记录残量，`terminal` 为 0，
与 PR 正文的说明一致。

## 7. 远端写核对

PR 正文声称只做了一次可回退的 issue 结构修正，把 `#885`/`#957` 挂到 `#337`。只读核对结果：

```
$ gh issue view 337 --json state,labels,body,subIssuesSummary
OPEN；labels = [type:epic, status:planned]；body 长度 900；subIssuesSummary = {completed:15, total:17}
$ gh issue view 885 --json parent
parent = #337（OPEN）
```

- 两条命令的参数在 `gh 2.103.2` 上存在：`gh issue edit --help` 列出 `--add-sub-issue number`
  与 `--remove-sub-issue number`，回退命令的语法成立。
- 结果与记载一致：`#337` 的子项总数 17、完成 15，新增的两个号各自由 `#337` 作父项；`#337`
  自身仍是 `OPEN`、标签未变、正文长度 900。
- 「未挂载时渲染直接失败」这条前提可以离线复现，因此远端写不是可选项：把 `957` 加进临时
  manifest、把临时快照里 `957.parent_issue_url` 置 `null` 后渲染：

```
ValueError: Implementation #957 has wrong native parent
exit=1
```

  对应 `scripts/dev-env/delivery_tracker.py:233` 的硬校验。用真实快照与真实 manifest 渲染则
  `Refreshed 65 requirements`、退出码 0（§3），说明两条父链现在都满足校验。

## 8. 未验证项

1. **`#337` 正文是否逐字未变无法证明**：GitHub 不提供 issue 编辑历史。已核对当前状态与
   PR 记载一致（`OPEN`、`[type:epic, status:planned]`、正文 900 字符、子项 17/15），但「操作
   前后字节相同」只能作为作者自述采信。
2. **远端命令的实际执行过程不可复现**：只读审查只能验证 flag 存在、结果状态与失败前提；
   没有独立日志证明「一次执行、未回退」。
3. **CI 未全绿**：审查时 `mergeStateStatus=BLOCKED`，`Python: tests/lint`、`Web`、12 个
   `Integration` 分片仍 `pending`（本 PR 无代码改动，但未等它们完成）。合并前以平台实际结果为准。
4. **快照是否由 `--refresh-snapshot` 直接产出未验证**：只核验了内容与实时一致、键集/号序/格式
   与脚本输出一致；手工拼装同样的内容无法从产物上区分。
5. **未做矩阵的语义 MECE 审查**，也未核验 `#819`/`#845`/`#852`/`#862`/`#863`/`#941`/`#959`
   七张新卡的需求归属（本 PR 按授权不动它们）。

## 9. 非阻断建议

**N1（中，建议同一收尾批次处理）：`O-ROLE` 行自相矛盾。** `docs/plans/delivery-tracking.md:74`
现在同时写着 `#572：closed / completed；验收见需求证据`，以及「真实端到端（完整 up、Compose
迁移、minio-init、engine-register、worker 启动与命名空间、三个 HTTP 调用）仍未跑通」「真实
部署端到端仍未做」。`#572` 于 `2026-11-17T10:14:00Z` 以 `COMPLETED` 关闭，关闭评论记录的是
`DEL-537` 的真实参考部署窗口（`POST /v1/nexus/jobs` 201、`pending → running → succeeded`、
artifact 92 B、lease released、引擎侧真实 200），但该证据只在 issue 评论里，仓库内没有对应
记录。建议：把该窗口按既有格式落库（`docs/plans/mvp-release-readiness.md` 或该行「验证」列），
并把那一句改成与证据同宽；若决定证据不入库，则应把「验收见需求证据」改成指向 issue 评论的
可追溯说法。表头「任务关闭不自动完成需求」只解释了需求未完成，解释不了这一行的事实句。

**N2（低）：两张新卡没有 `status:*` 标签。** `#885`/`#957` live `labels=[]`，矩阵因此显示
`open；状态标签待核实`；仓库约定活动跟踪项保持唯一状态标签。加标签属于远端写，超出本 PR
授权，建议在下一轮同步时补上，或明确这两张卡暂不进状态跟踪。

**N3（低）：七张新 open 卡仍未登记。** 矩阵把 `#819`/`#845`/`#852`/`#862`/`#863`/`#941`/`#959`
列进「未登记的 open issue」，每条都写「父 Epic 未核实」。实时核对：连同 `#696` 在内的这 8 条
都没有原生父项，所以该措辞是准确的。其中 `#862`/`#863` 是 `#845` 的拆分片，下一轮规划时
按「一个实施 issue 只登记一个需求」归类即可。

**N4（低，仅记录）：快照滞后是设计内行为。** 快照与实时 open 集合当前完全一致，但任何新
issue 或状态变化都要等下一次刷新；CI 只比对「快照 ↔ 渲染」，不比对远端最新状态。PR 正文
已声明这一点，无需改动。