# 审查产物边界与变异计量两条约束的非作者审查（PR #735）

> 过程记录文档：保留时间线、独立复现的命令与输出。结论面向合并决定；未修改被审的两份文档与任何产品代码/测试，变异全部施加在临时副本并已还原。
> 审查对象：PR #735 `docs/736-review-hygiene`，head `154abc3ab4ed294c041d05ca8a88740a71e4bfc1`（下称 head），基线 `origin/main` = `1ef3c61319d20a90d114a04f57115acf83a0d755`（下称 main）。`git diff --name-status origin/main 154abc3a` 恰为 2 文件：`docs/agent-verification.md`（+2）、`docs/operations/agent-delivery-sop.md`（+1/−1）。
> 审查分支 `review/737-review-hygiene` 基于 `origin/main` 拉出（`git worktree add /private/tmp/sn737 -b review/737-review-hygiene origin/main`），提交前 `git diff origin/main --stat` 只含本报告。被审内容用 `git show 154abc3a:<path>` 与 detached 快照 `/private/tmp/sn737-head` 读取，审查分支上未检出被审改动。

## 结论

**两条约束的方向和落点都成立，值得固化；但两份文档各自给出的「自查命令」按其字面执行会漏判或误判，必须修；「≠ 各单点之和」这条数字声明在仓库内没有可核对的证据。**

- **规则一**（`docs/operations/agent-delivery-sop.md` 第 4 步）：事故为真——审查 PR 的 squash `f88279dc` 除报告外确实带进了 3 个实现文件，而 `94258c1b` 从未进过 main。但 `git diff origin/main --stat` **不能区分「携带实现」与「main 前进」**：作者拿来演示的 40 文件（我逐字复现）主体是 main 前进的反向 diff，真正的携带实现在三点口径下只有 3 个文件；该命令还完全看不见未跟踪文件。**按当前形态拦不住事故**（MF-1）。
- **规则二**（`docs/agent-verification.md`「证据与交付」）：`git diff -U0 | grep -c '^@@'` **不可靠**——文档自己引的那次复合变异（`web/workbench/src/file-workflow/task-journal.ts:211`，三个门写在同一行）在 `-U0` 下只有 1 个 hunk，按文档口径会被判成「单点」；反过来，一处语义变异加其配套声明就是 2 个 hunk。**该自查手段必须换口径**（MF-2）。
- **数字声明**我逐项复现：基线 86 passed、M1 8 failed、M2 5 failed、复合 12 failed，`12 = 8 + 5 − 1`，交集恰为 `SiteInventoryTests::test_the_real_entry_matches_its_registry`。但文档给出的两条引证**恰好都可加**（`13+4=17`、`2+2=4`），仓库内没有记录 `8+5≠12` 那组实验，「≠ 和」的举例与引证对不上（MF-3）。

**判定：需返修后合并。** 两处修正都是文档级、每处不超过 2 行。

## 八条逐条判定

### 1. 规则一是否真能拦住事故 —— **规范成立，自查命令不成立**

**事故核实（成立）。** `f88279dc`（`review: COMMIT 分类修复的非作者审查 (#722)`，2026-11-14 08:58:56）的提交内容是 4 个文件：报告 + `version_ports.py` + `postgres_versions.py` + `test_workbench_postgres_cleanup.py`；且 `git branch -a --contains 94258c1b` 为空——被审 head 自身从未进入 main，实现是通过审查 PR 进主干的。实现 PR 随后在 09:20:16 以 `4ad6cad7` 合并，但内容是**改过的修订版**（相对被审 head 为 `5+/18−`），即「进入主干的那份实现不是被审的那份」在历史上确实发生过一次，只是最终被 22 分钟后的修订版覆盖。

**对照复现（数字全对，但解释错位）。** 在 head `94258c1b` 上建分支并只加一个报告文件：

```console
$ git diff origin/main --stat | tail -1
 40 files changed, 221 insertions(+), 5869 deletions(-)     # 与作者/PR 正文逐字一致
$ git diff origin/main...HEAD --stat | tail -1
 4 files changed, 155 insertions(+), 3 deletions(-)          # 报告 + 3 个被审实现文件
$ git diff --name-only 7e09b4db 94258c1b | wc -l
       3                                                    # 该 PR 的实现只有 3 个文件
```

即 **40 文件这一输出并不指向「携带实现」，而是指向「分支基线落后于 origin/main」**：`public_jobs.py`、`contracts/speech-openapi.yaml`、`file-task-session.ts` 等 37 个文件来自 main 上晚于被审 base 的 PR（#724、#729、#732 等），`git diff origin/main` 把它们算成了「分支的改动」。真正的违规信号是三点口径的 3 个实现文件。

**两个方向的反例（实测）：**

| 场景 | `git diff origin/main --stat` | 判定 |
| --- | --- | --- |
| 从被审 head 拉 + 加报告（作者演示） | 40 files, 221+/5869− | 混入 main 前进噪声 |
| 从**较旧 main**（`bff7f75a`，落后 2 个提交）拉 + 只加报告 | 22 files, 96+/1345− | **合规分支被误报** |
| 从 origin/main 拉 + 把被审实现 `git checkout 94258c1b -- <paths>` | 3 files（2 实现 + 报告） | 被正确看见 |
| 从 origin/main 拉 + 把被审**新**文件落成未跟踪文件 | **空输出、退出码 0** | **完全漏判** |

最后一行正是文档自己的取证方式带来的风险：文档要求「被审内容用 `git show <被审 head>:<path>` 读取」，读者顺手把内容重定向到工作区就得到未跟踪文件；此时 `git diff origin/main --stat` 干净，`git add -A` 后实现即随报告进入审查 PR——正是本条要防的事故。`git status --porcelain` 能看到它（`?? tests/unit/backend/zz_copied_impl.py`），但文档没要求。

**结论：规则文本覆盖了「分支从哪拉」这个机制，但没有覆盖「怎么证明自己没带实现」。** 判据要换成三点口径 + 未跟踪检查（MF-1）。

### 2. 规则二的可执行性 —— **自查手段不可靠，必须修**

被测命令 `git diff -U0 | grep -c '^@@'` 数的是**文本 hunk**，规则要的是**语义施加处**，两者不等价（实测）：

| 情形 | `-U0` hunk 数 | 说明 |
| --- | --- | --- |
| 同一文件两处改动、**相邻行** | **1** | 相邻改动并成一个 hunk |
| 同一文件两处改动、中间隔 1 行 | 2 | |
| 同一文件两处改动、中间隔 2 行 | 2 | |
| 跨文件各 1 处 | 2 | 这个方向是对的 |
| 变异已 `git add`，原样命令 | **0** | 命令漏了 `HEAD`，只数未暂存 |
| 同一命令改用 `git diff -U0 HEAD` | 2 | |

**最要命的一条：文档自己引的复合变异会被判成单点。** 被审 head `0d9e5b61` 的 `web/workbench/src/file-workflow/task-journal.ts:211` 把三个门写在同一行：

```ts
if (row.settled !== "terminal" || row.operationId !== null || !shape) return invalid();
```

| 变异 | hunk |
| --- | --- |
| 仅去 `!shape`（单点，2 红） | 1 |
| 仅去 `operationId !== null`（单点，2 红） | 1 |
| 两处都去（= 文档引的复合，4 红） | **1** |

反向误差同样存在：把 `WRITE_TOKENS` 去掉 `"rm"` 并同步更新声明计数（一处语义变异 + 其配套声明）实测 **2 hunks**，仍是一处变异、仍是 8 failed。另有一个极端例子说明「hunk 数」与「变异强度」无关：给守卫脚本加一行语法错误 = 1 个 hunk，结果是 `1 error during collection` 而非 N 条失败用例。

**结论：`>1 hunk` 只能证明「至少两处」，`=1 hunk` 什么也不能证明。** 必须把命令降级为下界，并补 `HEAD`、未跟踪与人工确认（MF-2）。

### 3. 「复合失败数 ≠ 各单点之和」—— **数字全对，解释只对本例成立，证据链缺**

按作者 PR 正文的实验（`scripts/dev-env/ci-entry-guard.py` + `tests/ci/test_ci_entry_guard.py`）逐项复现，**全部一致**：

| 变异 | 施加处/hunk | 结果 | 与作者 |
| --- | --- | --- | --- |
| 基线 | — | `86 passed, 53 subtests` | 一致 |
| M1：`WRITE_TOKENS` 去 `"rm"` | 1/1 | **8 failed, 80 passed** | 一致 |
| M2：`_write_words` 不再标记写命令词 | 1/1 | **5 failed, 84 passed** | 一致 |
| M1+M2 复合 | 2/2 | **12 failed, 79 passed** | 一致 |

`8 + 5 = 13 ≠ 12`；排序后 `复合失败集合 == 单点失败集合的并集`（逐行 diff 为空），交集恰为 1 条 `SiteInventoryTests::test_the_real_entry_matches_its_registry`。所以「重叠覆盖使复合数偏小」对这组实验是成立的，`12` 也确实不能当 `WRITE_TOKENS` 的强度（那会高估 50%，其中 5 红与该守卫无关）。

**但作为一般解释不完备**，至少还有三种机制会让「复合 ≠ 和」，文档没提：

1. **遮蔽/短路**：一个变异使另一变异对应的用例不再进入被咬的代码路径，复合时那些用例转绿——减小的原因不是「同一条用例被数两次」，而是「并集本身没出现」；
2. **计数单位/中断**：变异导致模块加载失败或采集中断时只记 1 个 `error` 而非 N 条失败（上面的语法错误例子），数字与覆盖面脱钩；
3. **涌现与不稳定**：复合触发两处单点都不触发的第三条守卫（文档的「无关守卫的红」只覆盖了「无关」，没覆盖「两处交互才可达」），以及顺序/flaky 造成的抖动。

**证据链问题（更实际）**：文档括注举的两例恰好**都可加**——terminal-exit 是 `13 + 4 = 17`，cancel-settled 拆分是 `2 + 2 = 4`。读者照着算会得到相等，反而怀疑「不等于各单点之和」这句。仓库内没有任何文档记录 `8+5≠12` 这组实验（grep `12 failed`/`8 failed` 均无；PR 正文有，但那不是持久产物）。要么把这组实验落进 `docs/validation/` 或本目录并引用，要么把命题改写为可核对的形态：「复合失败数的上界是各单点之和；只有失败用例互不相交时才相等」（MF-3）。

### 4. 落点 —— **恰当**

- 规则一落在 `docs/operations/agent-delivery-sop.md` 第 4 步：该步正是审查派发处（「审查安排由承接会话在派工时预定」），也是「固定受检候选」所在段，规则是其自然延伸；`AGENTS.md` 的路由表把「跨会话接续 / 多 Agent 交付」指向 SOP，reviewer 会被派到这里。
- 规则二落在 `docs/agent-verification.md`「证据与交付」：该节管的就是「报告什么、怎么计量」，`CONTRIBUTING.md` 与 `AGENTS.md` 都把验证指南指向它。
- `CONTRIBUTING.md` 确实没有审查章节（只有第 79 行工作区收尾与第 83 行 PR 描述要求），不存在「第二份来源」，不重复是对的。
- 这条规则并非新造：现存 19 份审查报告已按「从 `origin/main` 拉」执行（如 `2026-11-14-matrix-refresh.md:22`、`2026-11-13-guard-platform.md:5`），只有 4 份用了旧做法（含被引的 `2026-11-13-commit-classification.md:272`）。把多数实践写成规范合理。

**残余风险**：只读 `CONTRIBUTING.md` 的人看不到这两条（但它本来也不讲审查）；真正的缺口是没有机械守卫——仓库已有 `scripts/dev-env/` 系列结构守卫与 CI，一条「审查 PR 的改动路径 ⊆ `docs/reviews/`」的检查就能把规则一变成硬约束（N-1）。

### 5. 终态语态 —— **合格**

逐句核对两段新增文字：

- SOP 第 4 步：`审查 PR 只承载审查产物：审查分支基于 origin/main 拉出，提交报告前用 … 确认改动只含报告文件；被审内容用 git show <被审 head>:<path> 或 detached 快照读取，不在审查分支上检出被审改动。`——规范语态，无时间标记。
- 紧随其后的 `审查分支从被审 head 拉出会把实现带进审查 PR，squash 合并后实现以审查标题进入主干，…（实证：…）`——是**一般现在时的因果说明**，不是「某日发现…于是改为…」的流水账，与同文档其它步骤的写法一致，可接受。
- verification 段：前半是规范性祈使句，末尾括注指向 `docs/reviews/` 的实证记录，符合「规范在正文、过程在 reviews」的分工。
- 未发现「本次/此前/原计划/改为」等过程词汇混入规范正文。唯一可挑的是括注把「记录了什么数字」写进规范正文，句子长且数字与正文命题不完全对应（见第 6 条、MF-3），属可读性而非语态问题。

### 6. 实证引用 —— **事件与数字都对，两处精度问题**

| 引用 | 核对结果 |
| --- | --- |
| `2026-11-13-commit-classification.md`（规则一） | **指向正确事件、但不是事故本身的记录。** 该报告第 5 行与第 272 行写明审查 worktree 建在被审 head `94258c1b` 上，这是事故的**前提**；「带进实现、以审查标题进主干」只能从 git 历史 `f88279dc` 看到（3 个实现文件 + 报告）。建议括注直接补 squash SHA。 |
| `terminal-exit`（17 vs 13/4） | **准确。** 该报告第 18 行「但它是复合变异：单一变异分别为 13 / 4」，第 99–110 行给出 `17 = 13 + 4` 的完整表；`17 failed / 247 passed` 与 `13 failed / 251 passed`、`4 failed / 260 passed` 全部对得上。唯一精度问题：该报告第 270 行明确「作者没有给出变异细节…不能断定作者用的就是这一对」，而规范写的是「系**两个**变异同时施加」——「必然是复合」有据，`13/4` 是审查者的复现重构，「恰好两个」是推断。 |
| `cancel-settled`（4 红 / 纯形状 0 红 / 各 2 红） | **准确。** 第 118–127 行给出「仅去形状门 2 红 / 仅去 identified-execution 门 2 红 / 复合 4 红 / 纯 `pending !== null` 0 红」，第 223 行复核 `4 failed` 与作者一致。括注里「记「形状门放宽 → 4 红」」是把被审 PR 的说法压成短语，语义无损。 |

**关键问题不在「引错」而在「证据支撑不了那句话」**：这两条恰好都可加（17=13+4、4=2+2），无法支撑「≠ 各单点之和」（MF-3）。

### 7. 有无与既有条文冲突 —— **未发现**

- 全仓没有「审查分支建在被审 head」的**现行**指令；该做法只出现在历史审查报告的时间线里（4 份），按 `AGENTS.md`「历史计划、审查和失败记录保留原时点事实」，它们不构成冲突，只是不能再被当作模板。
- 与 `docs/operations/agent-delivery-sop.md` 第 4 步既有「固定受检候选」、第 35 行「候选 = 实际 base、目标分支、候选 SHA」相容：审查分支的基线是 main，受检候选仍是 head。
- 与「工作区生命周期与收尾」相容：审查 worktree 从 main 拉、被审内容用 detached 快照，正好符合该节「独立验收确需新 checkout 时…」与「只读 subagent 优先读已有候选」。
- 与 `docs/validation/2026-11-14-*.md` 的变异表方向一致（逐行单点，未发现未标注的复合行）。
- 作者的冲突检查结论成立。

### 8. `check-doc-refs` 归因 —— **46/2 复现，未新增；但该检查对本次新增内容是空证明**

```console
# head 154abc3a
SUMMARY refs=731 AMBIGUOUS=46 EXTERNAL=1 MISSING=2 OK=682   RESULT FAIL
# origin/main（不含该 PR）
SUMMARY refs=731 AMBIGUOUS=46 EXTERNAL=1 MISSING=2 OK=682   RESULT FAIL
$ diff <(head 输出) <(base 输出) | wc -l
       0
```

作者实测 46 与本卡所写 28 不符的是**卡**，不是作者：基线确为 `AMBIGUOUS=46 MISSING=2`，且两份输出逐行相同（2 条 MISSING 均在 `docs/reviews/2026-11-13-workbench-tts-probes.md:48`、`:110` 指向不存在的 `orphan_probes.py`，属既有问题）。**但要注意**：`refs=731` 两侧相同，说明该脚本根本没解析本次新增的三条 markdown 链接——它只校验 `path:line` 形式。所以「0 新增」是真的，却也是空证明；真正的验证是手工解析，我做了，三条链接全部命中（见证据 E5）。

## 必须修清单

| 编号 | 位置 | 问题 | 修法 |
| --- | --- | --- | --- |
| **MF-1** | `docs/operations/agent-delivery-sop.md:43` | `git diff origin/main --stat` 两点口径把「main 前进」与「携带实现」混为一谈（作者自己的 40 文件演示就是前者），且完全看不见未跟踪文件 | 改为三点口径 + 未跟踪检查：`git diff origin/main...HEAD --stat`（或 `git diff $(git merge-base origin/main HEAD) --stat`）确认改动只含报告文件，并用 `git status --porcelain` 确认没有未跟踪的实现文件；示例不要用「40 文件」这种同时能被 main 前进触发的形态 |
| **MF-2** | `docs/agent-verification.md:69` | `git diff -U0 \| grep -c '^@@'` 把文档自己引的复合变异（三条件同一行）判成 1 个 hunk；相邻行改动并 hunk；漏 `HEAD` 时暂存变异计 0 | 降级为下界并补全：「hunk 数 >1 一定复合；=1 不能证明单点，必须人工确认 diff 里只有一处语义改动」；命令改为 `git diff -U0 HEAD`；补 `git status --porcelain` 覆盖未跟踪；建议同时记录变异锚点（文件:行 + 表达式）以便复核 |
| **MF-3** | `docs/agent-verification.md:69` | 「复合失败数…也不等于各单点之和」的两条引证恰好都可加（`13+4=17`、`2+2=4`），仓库内没有 `8+5≠12` 的持久记录；一般解释漏了遮蔽、计数单位与涌现 | 二选一：把 `ci-entry-guard` 的 8/5/12 实验落一份持久记录（`docs/validation/` 或本目录）并在括注引用；或改写为「上界是各单点之和，只有失败用例互不相交时才相等」，并把解释补成「重叠 / 遮蔽 / 计数单位 / 交互」四类 |
| **MF-4** | `docs/operations/agent-delivery-sop.md:43` | 引证指向的报告只间接记录事故（分支建在 head 上），事故本身要读 git 历史；且「系两个变异同时施加」超出 `2026-11-13-terminal-exit.md` 的结论（该报告只说必然复合） | 括注补 squash `f88279dc`；「两个」改为「必然是复合（单点 13 / 4）」 |

## 非阻断建议

- **N-1（机械守卫）**：加一条 CI 检查——审查 PR 的改动路径必须 ⊆ `docs/reviews/`。仓库已有 `scripts/dev-env/` 守卫脚本与 `docs_format` 文档车道，改动成本低；有了它，规则一不再依赖自觉。
- **N-2（发现入口）**：`CONTRIBUTING.md` 增加一行指向 SOP 第 4 步与验证指南的交叉引用即可，不必复制条文。
- **N-3（可读性）**：SOP 第 4 步新增句与 verification 括注都偏长（单句 60+ 字、括注套两层），可拆成「规范 + 判据」两句，判据用列表。
- **N-4（历史模板风险）**：4 份历史审查报告的时间线记录了从被审 head 建 worktree 的命令，未来读者可能照抄。可在 SOP 该步加半句「不以既有报告的时间线命令为依据」，避免规范与样例互斥。

## 独立复现的证据（命令与输出）

审查环境：`/private/tmp/sn737`（审查分支，基于 `origin/main` `1ef3c613`）、`/private/tmp/sn737-head`（detached `154abc3a`）、`/private/tmp/sn737-probe`、`/private/tmp/sn737-carry`（临时副本，变异均已 `git checkout -- .` 还原）。ruff 用主检出 pinned `0.17.2`，pytest 用主检出 `.venv`（Python 3.14.9，pytest 9.1.1）。

### E1 规则一的四种拉法与两个反例

```console
# A 从被审 head 拉 + 只加报告
$ git diff origin/main --stat | tail -1
 40 files changed, 221 insertions(+), 5869 deletions(-)
$ git diff origin/main...HEAD --stat | tail -1
 4 files changed, 155 insertions(+), 3 deletions(-)
$ git diff --name-only 7e09b4db 94258c1b | wc -l
3

# B 从 origin/main 拉 + 只加报告
 docs/reviews/2026-11-14-demo4.md | 1 +
 1 file changed, 1 insertion(+)

# C 合规但从较旧 main(bff7f75a) 拉 + 只加报告（误报）
 22 files changed, 96 insertions(+), 1345 deletions(-)

# D 从 origin/main 拉 + 带入被审实现（漏判以外的正确拦截）
 .../workbench/application/version_ports.py | 23 +++++-----------------
 docs/reviews/2026-11-14-demo-carry.md | 1 +
 .../backend/test_workbench_postgres_cleanup.py | 12 +----------
 3 files changed, 7 insertions(+), 29 deletions(-)

# E 未跟踪实现文件（漏判）
$ git diff origin/main --stat; echo "rc=$?"
rc=0
$ git status --porcelain
?? tests/unit/backend/zz_copied_impl.py
```

### E2 hunk 计数实验

```console
同一文件、改动行 2 与 3（相邻）        hunks=1
同一文件、中间隔 1 行                 hunks=2
同一文件、中间隔 2 行                 hunks=2
跨文件各 1 处                         hunks=2
已 git add，git diff -U0              hunks=0     # 漏 HEAD
已 git add，git diff -U0 HEAD         hunks=2

# 真实被审代码 web/workbench/src/file-workflow/task-journal.ts:211
仅去 !shape                hunks=1
仅去 operationId !== null  hunks=1
两处都去（复合）           hunks=1     # 文档口径判成「单点」

# 一处语义变异 + 配套声明（WRITE_TOKENS 去 rm，计数 7→6）
hunks=2  → 8 failed, 80 passed        # 仍是一处变异
# 一处变异造成模块加载失败
hunks=1  → 1 error during collection  # 计数单位脱钩
```

### E3 单点 vs 复合（作者实验的独立复现）

```console
$ python -m pytest tests/ci/test_ci_entry_guard.py -q
86 passed, 53 subtests passed                    # 基线
M1（去 rm）        8 failed, 80 passed           # hunk=1
M2（不再标记写词）  5 failed, 84 passed           # hunk=1
M1+M2 复合        12 failed, 79 passed           # hunk=2
8 + 5 = 13 ≠ 12 ；复合失败集合 == 单点并集；交集 1 条：
SiteInventoryTests::test_the_real_entry_matches_its_registry
```

### E4 引用与 git 历史核对

```console
$ git show --name-status --format='' f88279dc
M backend/src/speechnexus_backend/workbench/application/version_ports.py
M backend/src/speechnexus_backend/workbench/infrastructure/postgres_versions.py
A docs/reviews/2026-11-13-commit-classification.md
M tests/unit/backend/test_workbench_postgres_cleanup.py
$ git branch -a --contains 94258c1b            # 空：被审 head 从未进 main
$ git diff --stat f88279dc 94258c1b -- <3 个实现文件>   # 空：携带内容与被审 head 逐字节相同
$ git log -1 --format='%ci %s' 4ad6cad7
2026-11-14 09:20:16 +0800 fix: … (#718) (#720)          # 修订版，晚于审查 PR 22 分钟
$ git diff --stat 4ad6cad7 94258c1b -- .../version_ports.py
 1 file changed, 5 insertions(+), 18 deletions(-)        # 进主干的那份 ≠ 被审的那份

terminal-exit：17 = 13 + 4（报告第 18/99–110 行）；作者变异定义未公开（第 270 行）
cancel-settled：形状门 2 / identified-execution 门 2 / 复合 4 / 纯形状 0（报告第 118–127、223 行）
```

### E5 门禁与文档检查（head 快照）

```console
$ ruff format --check .
1840 files already formatted
$ ruff check .
All checks passed!
$ python3 scripts/dev-env/check-doc-refs.py ; echo rc=$?
SUMMARY refs=731 AMBIGUOUS=46 EXTERNAL=1 MISSING=2 OK=682
RESULT FAIL   rc=1                     # 基线即 FAIL，与 origin/main 输出逐行相同（0 新增）
$ git diff --check origin/main 154abc3a   # 无空白错误

# 三条新增链接手工解析
OK docs/operations/agent-delivery-sop.md -> ../reviews/2026-11-13-commit-classification.md
OK docs/agent-verification.md -> reviews/2026-11-13-terminal-exit.md
OK docs/agent-verification.md -> reviews/2026-11-14-cancel-settled.md
```

## 只能依赖作者陈述的点

- **PR 正文里「从被审 head 拉 → 40 文件」的取数时点**：数字我按当前 `origin/main` 完整复现（40 / 221+ / 5869−），但那份输出里哪几行是「main 前进」、哪几行是「被审实现与 main 修订版之差」只能由三点口径分离；作者的意图（演示「一眼看见违规」）无法从截断的输出里判断。
- **M2 的确切改法**：PR 正文只写「`_write_words` 不再标记写命令词」，我按「把 `flagged.append(word)` 换成 `pass`」复现到 5 failed；作者是否同一改法无法确认，但数字与失败集合一致。
- **`17 failed` 与 `4 红` 的原始变异定义**：两份审查报告都写明作者未公开变异细节，`13/4`、`2/2` 是审查者的重构；本报告只核对这些重构数字与文档引用一致。
- **本卡所写 `AMBIGUOUS=28`**：与仓库实测 46 不符，我没有找到 28 的来源；按作者实测与我的复现，46 是当前真值。

## 时间线

- 12:34 `git fetch`；建 `/private/tmp/sn737`（`review/737-review-hygiene` @ `origin/main` `1ef3c613`）与 detached 快照 `/private/tmp/sn737-head`（@ `154abc3a`）；确认 PR 范围 2 文件 +3/−1。
- 12:36 读被审两处原文（SOP 第 4 步、verification「证据与交付」）与三份被引审查报告；`git show 154abc3a:<path>` 读取，未检出。
- 12:38 复现规则一对照：from-head 40 文件（与作者逐字一致）、from-main 1 文件；三点口径 4 文件。
- 12:39 补两个反例：较旧 main 的合规分支 22 文件（误报）、未跟踪实现文件零输出（漏判）、从 main 拉但 checkout 被审实现 3 文件（正确拦截）。
- 12:40 hunk 实验：相邻/隔行/跨文件/暂存；真实 `task-journal.ts:211` 三变体全 1 hunk；一处变异+配套声明 2 hunk；语法错误 1 hunk/1 error。
- 12:42 `ci-entry-guard` 单点/复合复现（86/8/5/12，交集 1 条，并集逐行相同）。
- 12:43 `ruff format --check .`、`ruff check .`、`check-doc-refs`（head 与 base 逐行相同）、三条新链接手工解析。
- 12:44 核对引用与 git 历史（`f88279dc`、`4ad6cad7`、`94258c1b` 不在 main），确认「进主干的那份 ≠ 被审的那份」。
- 12:46 写本报告；临时副本变异全部还原。
