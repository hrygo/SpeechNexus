---
title: PR #739 非作者审查：审查类 PR 路径门禁（#738）
status: active
---

# PR #739（`ci/738-review-pr-guard`）非作者审查记录

被审对象：PR #739《ci: 审查类 PR 的改动路径必须有机器门禁 (#738)》，
head `fd835594573982eef016ae215c1b5ee2f4d57453`，单 commit，相对 `origin/main`
（`d58127c027`，merge-base 即该提交，分支落后 0）`+760 / −2`，4 文件。

审查方式：独立 worktree `/private/tmp/sn741`、**基于 `origin/main`** 拉分支
（`review/741-review-guard`），被审内容在 detached 快照 `/private/tmp/sn741-pr`
（`git worktree add --detach` 到 `fd835594`）内读取与实跑，不在被审 head 上开分支。
审查只读：未改被审脚本、门禁或测试；变异一律在 `/private/tmp/sn741-mut`、
`/tmp/mut-*` 的副本内进行，未 push 任何变异。

本报告的处境本身就是被审门禁的一个自然实验：标题 `review:` + 分支 `review/` +
仅改 `docs/reviews/*.md`，因此应判 `applicable: true, ok: true` 并绿；这条 PR 同时是
「docs 路由下 `changes` 作业是否真的执行该步骤」的第一份直接证据（见 B5）。

## 结论

**可以合并，附两条必须修的文档事实修正。**

门禁的判据与边界方向正确、实现干净、测试有承重力：三条信号相互独立且逐条钉住，
边界常量被测试钉住，改装法/放宽边界/删除检查三种变异都会让测试与实跑同时翻面。
独立复核后作者的四项核心测量**全部成立**（33 个审查 PR、32 个只碰一个报告文件、
0 个碰过 `docs/validation/`、648 个变更集只命中事故一次且历史误报 0），
其中「648」比我在 PR 描述里读到的口径更精确（排除 80 个 merge 与 1 个空提交后的
非 merge 变更集数，逐字吻合）。

但有两处**事实性表述不成立或不一致**，都落在文档层，不影响门禁行为，却恰好是本 PR
赖以立身的「测量准确」这条主线上的瑕疵：

1. 「`changes` 是**唯一**没有任何 `if:` 范围过滤、每个 PR 都执行的林道」不成立——
   `delivery_tracking` 同样没有 job 级 `if:`，同样每个 PR 都跑（实测，见 B5）。
2. 门禁把边界收紧到 `docs/reviews/`，而 SOP 第 4 步只写「只应列出**报告文件**」；
   一个按 SOP 字面把报告写到 `docs/validation/` 的声明审查 PR 会被判红。

两项各一行的修正即可，不动逻辑。

另有一处**证据等级**问题（不是缺陷）：本卡要求核实「docs 路由下这个步骤是否真的执行」，
而现有材料只能证明「docs 路由到达 `changes`」与「该步骤没有 `if:`」，**没有一份日志
同时包含 docs 模式与 guard 输出**——#736 早于该步骤、#739 是 full 路由、本报告的 #742
是 docs 路由但基线 `ci.yml` 还没有这个步骤。结论仍是「会执行」（结构上不可能不执行），
但它是推断而非实测，建议把补证点写进文档，否则下一轮审查还会在同一处停下。

### 本卡两个核心问题的答案

**误伤真实率：历史 0，未来只剩「审查 PR 顺手改了 `docs/reviews/` 之外的东西」这一类。**
33 个已合并审查 PR 里没有第二个例外；`docs/validation/` 补记 0 次、`docs/api` 在仓库里
根本不存在、`docs/reviews/` 没有需要同步更新的索引文件——三个「最像误伤」的场景
在历史上都不存在。方向上这不是历史误报率，而是**设计取舍**：只要审查 PR 里出现任何
非 `docs/reviews/` 路径就红，哪怕那是审查过程中必要的订正；解药是把那部分拆到
另一个 PR（错误信息里已明确给出这条出口）。

**能否靠改标题绕过：不能，只改标题不够。** 实测三种改写：
只把标题从 `review:` 改成 `docs:` → 仍红（分支信号）；
标题与分支都改掉、报告留在 `docs/reviews/` → **仍红**（结构信号单独生效）；
只有把标题、分支、报告位置**三者同时**去掉才会绿，而这正是作者已写入
`docs/validation/ci-validation.md` 的残余缺口。结构信号是这条门禁的真正兜底，不是装饰。

## 十二条逐条判定

### A. 判据是否会误伤 / 漏报

| # | 判定 | 结论 |
| --- | --- | --- |
| A1 | 测量是否可信 | **成立，数字逐项吻合** |
| A2 | 误伤风险 | **历史 0；剩余风险是设计取舍而非测量错误** |
| A3 | 漏报路径 | **作者声明的缺口为真；另有一处无害缺口未写进文档** |
| A4 | 结构信号真实误报率 | **648 变更集、命中 1、误报 0，独立复现一致** |

**A1（成立）。** 独立口径：`gh pr list --state all --limit 1000` 取全量元数据，
按标题前缀与分支前缀各自分类，再用 `gh pr view --json files` 逐 PR 取改动路径。

- 当前全量 **518** 个 PR（作者写 517，差值就是 #739 自己，当时尚未创建）。
- 标题前缀 `review:` / `review：` 命中 **33**；分支前缀 `review/` / `docs/review` 命中 **33**；
  两个集合**完全相同**，无单侧命中。与作者「两者集合完全相同」逐字一致。
- 33 个全是已合并（`state: MERGED`），无未合并的审查 PR。
- 33 个里 **32 个只碰且仅碰一个 `docs/reviews/*.md`**；唯一例外正是 #722：
  `docs/reviews/2026-11-13-commit-classification.md` +
  `.../application/version_ports.py` + `.../infrastructure/postgres_versions.py` +
  `tests/unit/backend/test_workbench_postgres_cleanup.py`，即「报告 + 3 条实现/测试路径」，
  与作者描述一致。
- 没有任何审查 PR 同时改两个 `docs/reviews/` 文件（因此「审查时顺手补旧报告」也不存在，
  且即便存在也在边界内，不会误伤）。

**A2（历史 0，剩余风险是取舍）。** 逐条核过作者点名的三类「最像误伤」：

- `docs/validation/` 补记：33 个审查 PR 中 **0** 个碰过；`git log` 侧 34 个触及
  `docs/reviews/` 的提交里，除事故提交外**其余 33 个的其它路径数均为 0**。作者的
  「0 个」成立，不开口子的决定有据。
- 审查报告同时改 `docs/reviews/` 的其它文件：**0** 个（每个审查 PR 恰好 1 个报告）；
  且这类改动本来就在边界内，不构成误伤。
- 审查过程中必要的 `docs/api` 订正：**该目录不存在**，场景为空集。
- 还需补充一个作者没提的向量：`docs/reviews/` 是否有必须同步更新的索引/README？
  实测该目录只有 33 个按日期命名的 `.md`，**没有索引文件**，所以「加报告必须同时改索引」
  这类误伤不存在。

所以历史误伤率是 **0/33**。真正需要承认的是：这条门禁**故意**不接受「审查 PR 里带一点
必要的非报告改动」，它要求把那部分拆走；错误信息也明确写了这条出口
（"or move the non-review paths into their own pull request"）。这是可辩护的边界，
但应当按 A2 的结论写清楚：**它不是「已证明不会误伤」，而是「历史上没发生过、且发生后
的处置是拆 PR」**。

**A3（缺口为真，另有一处无害缺口未记录）。** 作者声明的缺口，我逐条做了真值验证：

- 标题与分支都改名 + 报告不写进 `docs/reviews/` → **确实没有任何信号**，判
  `applicable: false`。我用两个变体确认：报告写到 `docs/validation/my-review.md`、
  以及写到形近目录 `docs/review/r.md`（单数，不匹配 `docs/review` 分支前缀也不匹配
  `docs/reviews/` 路径根），两者都绿。缺口描述准确。
- 「实现恰好也在 `docs/` 下」：只要报告在 `docs/reviews/` 且实现是 `docs/` 任意其它
  路径（试过 `docs/architecture/spec.md`），**结构信号照常触发、判红**，不是缺口。
- 「携带 `scripts/` 下的小改」：试过 `scripts/dev-env/tiny.py`，**判红**；试过
  `.github/workflows/ci.yml`，**判红**。不是缺口。
- **作者未记录的第三处缺口**：把「实现」也放进 `docs/reviews/` 内（例如同时新增
  `docs/reviews/r.md` 与 `docs/reviews/impl.py`），标题分支都改名 → 全绿。
  这条实质上无害（`docs/reviews/` 下没有任何文件被导入或执行），但
  `docs/validation/ci-validation.md` 的「覆盖边界」只列了两处残余缺口，此处应补一句
  「边界是路径前缀，不区分文件类型」；否则读者会以为 `docs/reviews/` 内只可能有报告。

**A4（成立）。** 独立按提交遍历 `main`（不只是 33 个审查 PR），对每个变更集判断
「同时含 `docs/reviews/` 与其它路径」，与脚本同样的 `--no-renames` 口径：

- **729** 个提交全长（其中 80 个 merge、**1 个空提交**）；排除 merge 与空提交后为
  **648** 个非空非 merge 变更集——**与作者写的 648 逐字吻合**，说明作者用的是
  「有实际路径改动的变更集」而非「提交数」。
- 结构信号命中 **1** 次：`f88279dcec9e` 《review: COMMIT 分类修复的非作者审查 (#722)》，
  即事故 squash；**历史误报 0**。
- 补充：放宽到「只要碰 `docs/reviews/`」共命中 **34** 个提交，其中 33 个的其它路径数为 0；
  即作者「放宽到触到 `docs/reviews/` 就命中的也是同一唯一提交」的敏感性结论成立。

初看我把 merge 提交也算进去得到 729，一度与 648 不符；定位后确认是 merge 提交
（80 个）与空提交（1 个）的口径差，作者数字更精确，我的初算更粗。

### B. 门禁本身可靠吗

| # | 判定 | 结论 |
| --- | --- | --- |
| B5 | 挂载点是否正确 | **结论正确，但「唯一」的前提事实错误；docs 下执行是推断而非实测** |
| B6 | 失败关闭是否安全 | **取舍成立；无重试，有明确错误信息** |
| B7 | 能否被标题改掉绕过 | **不能（只改标题无效）；需三者同时去掉** |

**B5（挂载正确，「唯一」不成立）。** 三件事分开核：

- `changes` 作业**没有 job 级 `if:`**，该新增步骤也**没有 step 级 `if:`**（直接读 YAML 确认）；
  实测全仓 11 个作业有 `if:`，`changes` 与 `delivery_tracking` 无。
- **docs 路由确实会执行 `changes` 的步骤**：#736（标题 `review:`、分支
  `review/737-review-hygiene`、docs 范围 PR）的 `changes` 作业日志输出
  `{"mode": "docs", "reason": "Only prose or skill documents/metadata changed; ..."}`，
  证明 docs 模式下 `changes` 照常跑完全部步骤。
- **但「该步骤在 docs 路由下执行」至今没有被任何一份日志观测到**，这是本卡
  「必须实跑并贴输出」的一处落空，作者的两种材料都补不上：#736 合并在本 PR 之前，
  那时 `changes` 里还没有这个步骤；#739 自己确实跑了这个步骤，但它路由到
  `{"mode": "full"}`（改 `ci.yml` → full），不是 docs。我试着自己补这份证据也补不上：
  本报告的 PR #742 是 docs 范围、标题与分支都是审查形态，但 `pull_request` 事件取的是
  基线分支的 `ci.yml`，而 `main` 上还没有这个步骤，所以 #742 的 `changes` 作业日志里
  `check-review-pr-boundary` 出现 **0** 次，步骤列表止于 `ci-routing.py`。

  也就是说 B5 的链条是「三条结构事实」而非「一次观测」：`changes` 无 job 级 `if:`、
  docs 路由能到达 `changes`（#736 实证）、该步骤无 step 级 `if:`。这已经足够证明它会在
  docs 路由下执行（若不然 #736 的 `changes` 也会被跳过），但**证据等级是推断，不是实测**。
  这条证据只能在 #739 合并后由「第一个合入的审查 PR」的日志补上；建议把这条补证义务
  写进 `docs/validation/ci-validation.md`，否则下一轮审查还会在同一处停下。
- `delivery_tracking` **同样没有 job 级 `if:`**，同样每个 PR 都跑。所以
  「唯一没有任何 `if:` 范围过滤、每个 PR 都执行的林道」这句在 `ci.yml` 注释与
  `docs/validation/ci-validation.md:53` 两处都不成立。挂载结论不受影响——
  `changes` 仍是更好的宿主（它已经在路由前算了精确 diff、失败信息归属清晰），
  但「唯一」这个词把一个可选事实写成了排他事实。

**B6（取舍成立，无重试）。** 实测已声明审查 PR 在 base 取不到时的行为：
`{"applicable": true, "ok": false, "signals": ["title prefix", "branch prefix"],
"reason": "The exact base-to-HEAD diff is unavailable; a declared review PR cannot be
verified (the exact Git diff is unavailable: 128: fatal: ...)"}`，退出码 1；
同一故障下未声明 PR 判 `applicable: false` 并绿。分层与作者说明一致。

失败关闭的代价评估：确实存在「一次网络抖动让所有已声明审查 PR 变红」的路径，
而且脚本**没有任何重试**（`changed_paths` 里只有一次 `git fetch --no-tags --depth=1`，
60s 超时），`changes` 作业本身 `timeout-minutes: 3`。缓解因素有三条：
(a) 审查 PR 是 docs 范围，重跑成本极低（#739 的 `changes` 作业 21s）；
(b) 错误信息带 git 的 `returncode: stderr` 首行，能区分「网络故障」与「真的越界」；
(c) 反向选择（失败开放）正是事故那一条。
所以这个取舍可以接受，但**建议加一次带退避的重试**，把「抖动变红」压成「抖动变红一次」。
这不是必须修——它是可运维性优化，不是正确性问题。

另外记录一个真实的健壮性观察：因为 `changed_paths()` 在信号判定**之前**无条件执行，
一次取 diff 失败会让「本 PR 是不是审查 PR」这件事也失效，此时若 PR 未声明就静默判
`applicable: false`。作者对此是知情的（文档里写了第二处残余缺口），而且路由在同一
条件下本来就回退全量，所以不构成新的风险面；但这也意味着**「本 PR 实跑绿」不能单独
证明 diff 取到了**——必须看日志里的 reason 字符串。我核了 #739 的实际日志：
`review-pr-boundary: No review signal: the title is not review-prefixed, the branch is
not review/ or docs/review, and no changed path mixes docs/reviews/ with another path.`
这是 `paths is not None` 分支的同一条文案，**说明 #739 上 diff 确实取到了**，
不是「取不到 + 未声明」的假绿。这一点作者没有点明，但结论成立。

**B7（不能，只改标题无效）。** 用真实 git fixture（base→含报告与 `backend/src/x/v.py`、
`tests/unit/t.py` 的提交）跑三种改写：

```bash
# 原始：标题 review: + 分支 review/722
exit=1  {"applicable": true, "ok": false,
         "signals": ["title prefix", "branch prefix", "docs/reviews mixed with other paths"],
         "violations": ["backend/src/x/v.py", "tests/unit/t.py"], "reports": ["docs/reviews/r.md"]}

# 只改标题为 docs:，分支保持 review/722
exit=1  {"applicable": true, "ok": false, "signals": ["branch prefix", "docs/reviews mixed with other paths"],
         "violations": ["backend/src/x/v.py", "tests/unit/t.py"]}

# 标题改成 docs: 且分支改成 chore/722，报告仍在 docs/reviews/
exit=1  {"applicable": true, "ok": false, "signals": ["docs/reviews mixed with other paths"],
         "violations": ["backend/src/x/v.py", "tests/unit/t.py"], "reports": ["docs/reviews/r.md"]}

# 再把报告移出 docs/reviews/（写到 docs/validation/my-review.md）
exit=0  {"applicable": false, "ok": true, "signals": [], "violations": [], "reports": []}
```

结论：**改标题只能骗过标题信号，结构信号照旧兜住**；要绕过必须同时放弃「标题像审查」
「分支像审查」「报告落在 `docs/reviews/`」三件事。第三种写法之所以能绕过，恰恰因为
它连「这是审查 PR」的痕迹都不留了——那时门禁没有可依赖的判据，这是作者已经写明的
残余缺口，不是隐藏后门。

### C. 反例与测试

| # | 判定 | 结论 |
| --- | --- | --- |
| C8 | 独立复现四条反例 | **前两条已独立复现，另两条功能复现** |
| C9 | 24 条测试断言什么 | **断言判据逻辑，不是 exit 0；结构信号被单独钉住** |
| C10 | `tests/ci` 例数 | **311 → 335，24 条新增，逐条核对** |

**C8（复现成立）。** 在 `/private/tmp/sn741-mut` 的独立 git fixture 上，对脚本副本做两处
单点变异，同一违规变更集（`review:` 标题 + 报告 + 2 条实现路径）：

```bash
# 变异①：删掉违规判定分支（judge 的 ok=False 返回改成绿）
exit=0  {"applicable": true, "ok": true, "signals": [...三条全中...], "violations": []}

# 变异②：REVIEW_ROOT = ""（边界放宽为任意路径）
exit=0  {"applicable": true, "ok": true, "signals": ["title prefix", "branch prefix"],
         "reports": ["backend/src/x/v.py", "docs/reviews/r.md", "tests/unit/t.py"]}

# 对照：未变异
exit=1
```

「去掉检查」在 CI 层同样可复现且**会被测试当场抓住**：把整个 guard 步骤块从 `ci.yml`
删掉后，`tests/ci` 出现 5 failures + 1 error（`test_the_guard_receives_the_pull_request_identity_and_the_exact_base`
等接线测试），而 `tests/ci` 就在 `changes` 作业的第一步跑——也就是说「悄悄摘掉门禁」
本身会让同一条林道红。这比反例本身更有价值，作者没有强调。

另两条（普通实现 PR 不适用、`docs/validation` 记录型 PR 不适用）用真实 diff 复现：
实现 PR（`fix:` + `backend/`、`tests/`，无报告）→ `applicable: false, ok: true`；
记录型 PR（`docs:` + `docs/validation/` + `docs/plans/`）→ `applicable: false, ok: true`。

**C9（是判据断言，结构信号被单独钉住）。** 24 条测试里绝大多数断言的是
`signals` 元组、`violations`/`reports` 元组、`reason` 文本、stderr 的
`::error title=Path outside docs/reviews/::<path>` 注解与 step summary 渲染，
不是只断言退出码。关键的一条确实存在：

```text
test_structure_is_a_third_independent_signal
    verdict = verdict_for(title="chore: 整理文档", head_ref="chore/727", paths=INCIDENT_PATHS)
    assertEqual(verdict.signals, ("docs/reviews mixed with other paths",))
    assertFalse(verdict.ok)
```

标题与分支都不像审查 PR、只有结构信号命中 → 必须红，正是本卡要求的那条。承重力我用
三处变异验证：

| 变异 | `tests/ci` 结果 |
| --- | --- |
| `structure=False`（去掉结构信号） | 335 ran，**1 failure**（就是那条） |
| `REVIEW_ROOT = "docs/"`（放宽边界） | 335 ran，**3 failures** |
| `branch=False` + `structure=False`（只剩标题信号） | 335 ran，**3 failures** |

也就是：结构信号被删、边界被放宽、信号被削弱成只剩标题，三种退化都会让测试红，
测试不是记录实现，而是钉住契约。

唯一可加强的点：结构信号只在**单元层**（`detect_signals` + `judge`）被单独验证，
7 条真实 git fixture 测试全部用 `--title review:` 走声明路径。真实的
`changed_paths → 结构信号 → 红` 这条端到端串联没有被 fixture 覆盖（虽然各段分别有覆盖）。
建议补一条 fixture：标题分支都改成 `chore/`，改动仍含 `docs/reviews/` + 源码，断言退出码 1。

**C10（311 → 335，24 条）。** 在 `origin/main` 上跑 `python3 -m unittest discover -s tests/ci`
得 `Ran 311 tests`；在 `fd835594` 快照上得 `Ran 335 tests in 6.705s / OK`。
新文件自身 `Ran 24 tests ... OK`，我逐条列出并计数：
SignalTests 4 + BoundaryTests 6 + GitFixtureTests 7 + WiringTests 6 + SelfTestTests 1 = **24**，
与 311+24=335 吻合。

### D. 边界与记录

| # | 判定 | 结论 |
| --- | --- | --- |
| D11 | 不许 `--admin` 绕过 | **成立，且 `enforce_admins=true`** |
| D12 | 范围与残余缺口文档化 | **范围干净、`uv.lock` 干净；残余缺口如实但少一处** |

**D11（成立）。** `python` 作业（显示名 `Gate: required checks for this change`）的
`needs` 含 `changes`，`if: always()`，门禁步骤内
`if [[ "$ROUTING_RESULT" != success ]]; then echo "::error::CI routing=$ROUTING_RESULT"; exit 1; fi`。
分支保护实测：必需检查 `contexts` 包含 `Gate: required checks for this change`，
`enforce_admins.enabled = true`——即管理员同样不能绕过必需检查。所以
「`changes` 红 → Gate 红 → 不可 `--admin` 合并」这条链闭合。

**D12（干净，文档如实但少一处）。** 精确范围：

```text
M .github/workflows/ci.yml                      (+21)
M docs/validation/ci-validation.md              (+12 / −2)
A scripts/dev-env/check-review-pr-boundary.py   (+349)
A tests/ci/test_review_pr_boundary.py           (+378)
```

即 `.github/workflows/ci.yml`、`scripts/dev-env/`、`tests/ci/`、`docs/validation/ci-validation.md`
四处的并集，**无越界**；`git diff origin/main fd835594 -- uv.lock` 为空，
`uv.lock` 未被改动。

残余缺口文档化：`docs/validation/ci-validation.md` 明确写了「不比对 blob 哈希、不验证
报告内容/链接/行号」「`push` 到 `main` 的历史不再判」「两处残余缺口（标题分支都改名且
报告不在 `docs/reviews/`；未声明 PR 在 diff 不可读时无法评估结构信号）」——如实、具体、
没有把「路径门禁」说成「内容门禁」。缺的是 A3 里那第三处（`docs/reviews/` 内可放任意
文件类型），以及 B5 那句不成立的「唯一」。

## 必须修清单

按「改的人会不会被误导」排序，两条都是文档层，不动逻辑、不动测试。

1. **删掉或改写「唯一」这处事实错误。** 位置：`.github/workflows/ci.yml` 的
   `changes` 作业注释（"This is the one lane that runs for every scope"）与
   `docs/validation/ci-validation.md:53`（"这是唯一没有任何 `if:` 范围过滤、每个 PR
   都执行的林道"）。事实：`delivery_tracking` 同样没有 job 级 `if:`，同样每个 PR 都跑。
   建议改成「两条不过滤的作业之一；只有 `changes` 在路由之前就已持有精确 diff，
   且其失败直接归因到路由」，保留原有结论、去掉排他词。
   理由：本 PR 的说服力全部建立在「测量说得出处」上，一处可被一行 YAML 反驳的排他
   断言会削弱同一段落里其它四个正确数字的可信度。

2. **把 SOP 第 4 步与门禁边界对齐。** 门禁强制「全部路径落在 `docs/reviews/`」，
   而 `docs/operations/agent-delivery-sop.md` 第 4 步只要求「只应列出**报告文件**」。
   一个照 SOP 字面把报告写到 `docs/validation/xxx.md` 的**已声明**审查 PR 会被判红
   （`test_a_review_without_a_report_is_red` 就是这条行为的钉子），
   而它按流程文档自查会得出「我没有违规」。
   最小改法：在第 4 步那句里把「报告文件」写成「`docs/reviews/` 下的报告文件」，
   或在 `ci-validation.md` 明写「门禁比 SOP 字面更严：边界固定为 `docs/reviews/`」。
   理由：机器门禁的正当性来自它执行的是生效的流程契约；两者不一致时，被拦者有理。

## 建议（非必须）

- **把「docs 路由下执行」的补证义务写进文档。** 现有材料只能证明「docs 路由到达
  `changes`」（#736）与「该步骤无 `if:`」，没有一份日志观测到两者结合；#739 是 full
  路由，本报告的 PR #742 是 docs 路由但基线 `ci.yml` 还没有这个步骤（实测日志
  `check-review-pr-boundary` 出现 0 次）。建议在 `docs/validation/ci-validation.md`
  记明这条是推断，并把「#739 合并后第一个审查 PR 的日志」指定为确认点。若不做，
  下一轮审查会在同一处无法结案。
- **base fetch 加一次带退避的重试。** 现在只有一次 `git fetch --depth=1`（60s 超时），
  一次抖动就让全部已声明审查 PR 变红。重试一次可把「抖动即红」降为「抖动重试一次」，
  与「失败关闭」的初衷不冲突。
- **补一条结构信号的端到端 fixture 测试。** 现在结构信号的独立验证只在单元层
  （`test_structure_is_a_third_independent_signal`），7 条真实 git fixture 全走声明路径。
- **把「已声明审查 PR 的空 diff 判红」写进文档。** 脚本 docstring 说边界是「所有改动
  路径落在 `docs/reviews/`」，空集是空洞满足；实现额外要求至少一个报告文件
  （实测空 diff 的已声明审查 PR 判红，reason 为 "reports no file under docs/reviews/"），
  这更严也更合理，但应当写明，否则读者会按 docstring 推断空 PR 不适用。
- **残余缺口补第三处。** 「实现」若也被放进 `docs/reviews/`（如 `docs/reviews/impl.py`），
  标题分支都改名后全绿；实质无害（该目录下没有文件被导入或执行），但
  「覆盖边界」段应承认「边界是路径前缀，不区分文件类型」。

## 独立复现的证据

环境：`origin/main = d58127c0`，被审 head `fd835594`，macOS，`python3 3.14.9`，
`uv 0.13.5`。

### 1. A1 独立统计（与作者对比）

```bash
gh pr list --state all --limit 1000 --json number,title,headRefName,state,mergedAt
# total PRs: 518        （作者写 517；差值是 #739 自身，当时未创建）
# title hits: 33        （review: / review： 前缀）
# branch hits: 33       （review/ 或 docs/review 前缀）
# identical sets? True
# title-only: []   branch-only: []

# 对 33 个逐 PR 取 gh pr view --json files：
# 32-claim: 只碰一个 docs/reviews 文件且别无其它 = 32
# non-clean (1): #722 review: COMMIT 分类修复的非作者审查
#     [...version_ports.py, ...postgres_versions.py,
#      docs/reviews/2026-11-13-commit-classification.md,
#      tests/unit/backend/test_workbench_postgres_cleanup.py]
# any review PR touching docs/validation/: []
# any review PR with paths outside docs/reviews/: [722]
# states: ['MERGED']   file-count distribution: [1, 4]
```

结论：33 / 32 / 唯一例外 #722 / `docs/validation` 0 —— **四项全部与作者一致**。

### 2. A4 独立误报扫描

```bash
git log --no-merges --name-only --no-renames --format=@@%H origin/main
# non-merge commits: 649
# non-merge with >=1 changed path: 648      <- 作者写的 648，逐字吻合
# non-merge empty commits: 1

# structural-signal hits (docs/reviews/ mixed with other paths): 1
#   f88279dcec9e review: COMMIT 分类修复的非作者审查 (#722)
#     other   .../workbench/application/version_ports.py
#     other   .../workbench/infrastructure/postgres_versions.py
#     REVIEW  docs/reviews/2026-11-13-commit-classification.md
#     other   tests/unit/backend/test_workbench_postgres_cleanup.py
# commits touching docs/reviews/ at all: 34  (33 个 other=0 + 事故)
```

### 3. B7 绕过实测

见 B7 节的四段真实 diff 输出：只改标题仍红；标题+分支都改、报告留在
`docs/reviews/` 仍红（结构信号单独生效）；三者同时去掉才绿。

### 4. C8 至少两条反例

见 C8 节的变异①（删违规判定分支 → 绿）、变异②（`REVIEW_ROOT=""` → 绿）、
对照（原样 → 红）；另加 CI 层「删掉步骤 → `tests/ci` 5 failures + 1 error」。

### 5. C9 结构信号测试核实

`test_structure_is_a_third_independent_signal` 存在且断言
`signals == ("docs/reviews mixed with other paths",)` 且 `ok is False`；
`structure=False` 变异使 335 条中恰好 1 条失败，承重力确认。

### 6. `python3 -m unittest discover -s tests/ci`

```text
# origin/main (d58127c0)
Ran 311 tests in 5.646s
OK

# 被审 head (fd835594)
Ran 335 tests in 6.705s
OK

# 仅新文件
Ran 24 tests in 0.748s
OK
```

### 7. lint / 类型 / 边界

```text
# 审查分支（origin/main + 本报告）
ruff check .                 -> All checks passed!
ruff format --check .        -> 1841 files already formatted
mypy --platform linux        -> Success: no issues found in 1449 source files
lint-imports                 -> Contracts: 9 kept, 0 broken.

# 被审 head 快照
ruff check .                 -> All checks passed!
ruff format --check .        -> 1843 files already formatted
ruff format --check <新两文件> -> 2 files already formatted
mypy --platform linux        -> Success: no issues found in 1451 source files  （+2 = 新脚本与新测试）
lint-imports                 -> Contracts: 9 kept, 0 broken.
```

说明：`lint-imports` 直接复用主工作区 venv 时会报 `Could not find package
'speechnexus_asset_inspector'`——这是把外部 venv 指向快照目录的环境副作用，不是 PR
缺陷；把快照自身的 6 个 `src` 目录放进 `PYTHONPATH` 后 9 条契约全 kept。

### 8. 本 PR 的 `gh pr checks`（作者说「实跑绿」的核实）

```text
$ gh pr checks 739
Scope routing and CI / design-token checks                          pass  21s
Gate: required checks for this change                               pass   4s
Python: lint, types and contract compatibility                       pass  1m54s
Python: tests, coverage and package build                            pass  6m18s
Docs: markdown code block formatting                                 skipping
...（其余 16 项均 pass）
```

`changes` 作业里 guard 步骤的实际日志（run 43560061968 / job 132341526739）：

```text
Run python3 scripts/dev-env/check-review-pr-boundary.py \
  --event="$EVENT_NAME" --base="$BASE_SHA" \
  --title="$PR_TITLE" --head-ref="$PR_HEAD_REF" \
  --summary="$GITHUB_STEP_SUMMARY"
review-pr-boundary: No review signal: the title is not review-prefixed, the branch is
not review/ or docs/review, and no changed path mixes docs/reviews/ with another path.
```

这条 reason 属于 `paths is not None` 分支，**证明 #739 上精确 diff 确实算出来了**，
不是「取不到 diff + 未声明」的假绿；也证明该步骤在真实 PR 上确实被执行到。

docs 路由的实证取 #736（job 132336374448）：

```text
{"mode": "docs", "reason": "Only prose or skill documents/metadata changed; heavy checks are out of scope."}
```

证明 docs 范围下 `changes` 作业照常执行步骤；但 #736 合并早于本 PR，`changes` 里尚无
本步骤，所以它只到「docs 路由到达 `changes`」为止。「该步骤在 docs 路由下执行」至今
没有日志观测，本报告的 PR #742 也补不上——它是 docs 路由，但基线 `ci.yml` 还没有这个
步骤（见 B5 与「新绿」）。

### 9. 分支保护

```text
required_status_checks.contexts: ['Gate: required checks for this change',
                                  'Web: lint, types, tests, build and accessibility',
                                  'Compose: configuration validation']
enforce_admins.enabled: true
```

## 新绿

本报告的 PR [\#742](https://github.com/hrygo/SpeechNexus/pull/742) 是「声明 + 合规」的
真实样本：标题 `review:`、分支 `review/741-review-guard`、改动只有本文件。它验证了两件事，
**也止步于两件事**：

```text
$ gh pr checks 742
Scope routing and CI / design-token checks                 pass  20s
Docs: markdown code block formatting                       pass  12s
Docs: delivery matrix freshness                            pass   5s
Gate: required checks for this change                      pass   4s
Node Controller image evidence / scope                     pass   7s
Python: tests, coverage and package build                  skipping
Web: lint, types, tests, build and accessibility           skipping
（合计 15 项：5 pass / 1 无关项、其余 skipping）

# changes 作业日志
{"mode": "docs", "reason": "Only prose or skill documents/metadata changed; heavy checks are out of scope."}
```

1. **docs 路由 + 声明审查形态的 PR 能一路绿**：`changes`（20s）、`docs_format`、
   `delivery_tracking`、最终 `Gate` 全绿，说明「审查 PR 都是 docs 范围」这条前提与
   「docs 路由下必需检查集合」都自洽。
2. **但 guard 步骤在这份日志里根本不存在**：`grep -c check-review-pr-boundary` 为 **0**，
   步骤列表止于 `ci-routing.py`。原因是 `pull_request` 事件按基线分支的 `ci.yml` 执行，
   而 `main` 上还没有这个步骤。

所以「docs 路由下该步骤执行」**在本 PR 上仍然没有直接证据**，B5 只能结到结构推断。
真实的新绿（`applicable: true, ok: true` 的端到端）要等 #739 合并后，由第一个合入的
审查 PR 的 `changes` 日志给出；这是本卡唯一未能实跑贴出的输出，已写进 B5 与建议清单。

## 过程记录

| 时间 | 动作 |
| --- | --- |
| 13:21 | `git fetch --all --prune`；`git worktree add /private/tmp/sn741 -b review/741-review-guard origin/main`；确认 `git diff origin/main --stat` 为空、HEAD 即 `d58127c0` |
| 13:22 | `git worktree add --detach /private/tmp/sn741-pr fd835594` 建 detached 快照；读 `check-review-pr-boundary.py`（349 行）与 `test_review_pr_boundary.py`（378 行）、`ci.yml` diff |
| 13:23 | `tests/ci`：origin/main 311 OK、fd835594 335 OK（`python3 3.14.9`，无需安装依赖） |
| 13:24 | A1：`gh pr list` 全量 518 个 PR，标题/分支各命中 33、集合相同；逐 PR `gh pr view --json files` 得 32/33 与唯一例外 #722 |
| 13:25 | A4：遍历 main 全量提交，648 个非空非 merge 变更集、结构信号命中 1（`f88279dc`）、误报 0；同时发现作者「648」的精确口径 |
| 13:26 | 核 `docs/api` 不存在、`docs/reviews/` 无索引文件 → A2 的三个误伤向量里两个是空集 |
| 13:27 | 建 `/private/tmp/sn741-mut` 独立 fixture（6 个场景提交），跑 B7 四段改写与 C8 两条反例 |
| 13:28 | 变异 `structure=False` / `REVIEW_ROOT="docs/"` / 只剩标题信号，跑 `tests/ci` 得 1、3、3 failures；另把 guard 步骤整块从 `ci.yml` 删掉，得 5 failures + 1 error |
| 13:29 | A3 补充缺口：`docs/reviews/` + `scripts/`、`docs/architecture/`、`.github/workflows/` 全红；`docs/reviews/` 内放 `.py` 全绿 |
| 13:30 | ruff check / format（分支 + 快照全量 + 新两文件） |
| 13:31 | `mypy --platform linux`（1449 / 1451）；`lint-imports` 先因外部 venv 缺包失败，补 `PYTHONPATH` 后 9 kept 0 broken |
| 13:32 | 读 `gh pr checks 739`、`gh run view --job` 取 guard 步骤日志与 #736 的 docs 路由日志 |
| 13:33 | 核分支保护 `enforce_admins=true` 与必需检查含 `Gate: required checks for this change` |
| 13:34 | 核 B5：全部 job 级 `if:` 列表 → 发现 `delivery_tracking` 同样无 `if:`，「唯一」不成立 |
| 13:35 | 核 D12 精确文件范围与 `uv.lock` 干净；核 SOP 第 4 步原文，发现与门禁边界措辞不一致 |
| 13:36 | 写本报告，`uv run --no-project ruff format`（不变）、`ruff format --check .`（1842 文件）通过 |
| 13:37 | 提交并 push `review/741-review-guard`；`git diff origin/main --stat` 仅含本报告；开 PR #742 |
| 13:39 | 取 #742 的 `changes` 作业日志（job 132347962189）：`{"mode": "docs"}`、步骤列表无 guard（`check-review-pr-boundary` 出现 0 次）→ B5 的 docs 观测缺口确认，改写本报告 B5、建议与新绿三处 |

## 只能依赖作者陈述的点

以下无法从本仓库当前状态独立验证，报告按「作者陈述」处理，未当作已证事实：

1. **517 这个总数**只能间接吻合（现存 518 = 517 + #739 自身）；无法证明作者当时读到的
   就是 517，只能证明它自洽。
2. **`/pulls?state=all` 6 页**这个取数方式本身：我改用 `gh pr list --limit 1000` 取同一
   数据源，结论一致，但不能证明作者当时用的是 REST 分页。
3. **事故的两个 squash SHA `f88279dc` 与 `4ad6cad7`**：`f88279dc` 在 main 历史里可核
   （就是唯一结构命中），内容与其描述一致；`4ad6cad7` 我只能核到它是「另一份修订版」
   这一叙述在 `docs/reviews/2026-11-13-commit-classification.md` 与后续提交里的复述，
   没有独立比对其与 `f88279dc` 的 blob 差异，因此「进主干的那份 ≠ 被审的那份」按作者
   陈述采信。
4. **「blob 哈希已核」**：作者称三文件与被审 head 逐字节相同，我没有重算 blob 哈希
   （被审 head 已合并，其树与 `f88279dc` 的实现路径需要另行定位），按作者陈述采信。
5. **四种情形的「作者实跑」**：我用自己的 fixture 复现了四类判定与两条反例，结论一致，
   但那是我的复现，不是复现作者的命令序列。
6. **「docs 路由下该步骤执行」**：这是**结构推断**而非观测。三条结构事实成立
   （`changes` 无 job 级 `if:`、docs 路由能到达 `changes`、该步骤无 step 级 `if:`），
   但没有一份日志同时包含「docs 模式」与「guard 步骤输出」；#736 早于该步骤、#739 是
   full 路由、本报告的 #742 是 docs 路由但基线 `ci.yml` 无此步骤。该推断风险很低
   （若 docs 会跳过 `changes`，#736 的 `changes` 作业本身也会被跳过），但按证据等级
   如实登记为推断。
