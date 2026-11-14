---
title: PR #749 非作者审查：审查路径门禁的比较端点（#747）
status: active
---

# PR #749（`ci/747-boundary-merge-base`）非作者审查记录

被审对象：PR #749《ci: 审查路径门禁改用 merge-base，避免主干前进触发误报 (#747)》，
head `9d6b08c2485d377917ad553464806771c9e7ae44`，单 commit，相对 `origin/main`
（审查开始时 `0b79b633`，merge-base 为 `cdf60a9e`，即该 PR 事件记录的 `base.sha`）
`+328 / −39`，4 文件。

审查方式：独立 worktree `/private/tmp/sn750`，**基于 `origin/main`** 拉
`review/750-boundary-fix`；被审内容在 detached 快照 `/private/tmp/sn750-pr`
（`git worktree add --detach` 到 `9d6b08c2`）内读取与实跑，不在被审 head 上开分支。
审查只读：未改被审脚本、门禁、测试或产品代码；变异一律在 `/tmp/sn750-mut*`、
`/tmp/fx-*`、`/tmp/sn750-repro*` 的副本内进行，未 push 任何变异。
`tests/ci` 的全量跑动在快照内执行，未污染本报告分支。

本报告的处境同样是被审门禁的自然实验：标题 `review:` + 分支 `review/` + 仅改
`docs/reviews/*.md` → 应判 `applicable: true, ok: true`；而它同时是「PR 打开后主干
又合入了一份审查报告」的活样本（本 PR 的事件 `base.sha=cdf60a9e`，主干已到
`0b79b633`，其间 #748 合入了 `docs/reviews/2026-11-14-render-unit-editor-bounds.md`），
即 #747 那条假阳性的**当前仍可复现的真实输入**。

## 结论

**可以合并，附两条必须修。** 作者的四个核心论断我逐条独立复核，**全部成立**，
其中最关键的两条（`pull_request` 检出的是合并提交、`merge-base(base.sha, HEAD)` 就是
`base.sha`）不是纸面推理：前者由真实 `refs/pull/749/merge` 提交对象与 #741 的 CI 日志
反证，后者由真实历史 SHA 直算。修法本身是对的：比较合并提交与其第一父提交，得到的
正是本 PR 叠加到基分支上的改动；失败关闭没有退化；三条信号在 merge 形态下语义仍然正确；
旧/新对照在真实 #741 元数据上**逐字节**复现（sha256 相同）。

两条必须修都不影响当前 CI 主路径的判定正确性，但一条会在特定正常操作下**重新引入本 PR
要修的同一类假阳性**（回退方向错），另一条是**父提交「角色」未被校验**——`--head-sha`
今天实际承担的比「确认第二父」更多，文档把这一点写轻了。

另外必须说清楚一个方向问题：作者称「形态判定退回三点不是误绿」，这个判断**在 CI 路径上
成立**（`--head-sha` 一定传，角色倒置会被拒 → 退回三点 → 红），**但在没有 `--head-sha`
的本地路径上不成立**：我构造出「主题角色倒置 + 缺 `--head-sha`」的可执行输入，未声明
的 PR 明明同时带 `docs/reviews/` 与实现，门禁**判绿**（实证见 B5 第 3 条）。因为它今天
不可达（GitHub 的主题格式是 `<head> into <base>`，我已用真实合并引用核实），我没有把它
放进必须修，只要求把文档那一句补全、并顺手把父角色校验改成有序比对。

### 本卡两个核心问题的答案

**检出形态与 merge-base 断言：都成立，而且我拿到了比作者更硬的证据。**
`changes` 作业的 `actions/checkout` 只传 `persist-credentials: false` 与
`fetch-depth: 1`，**没有 `ref:`**，所以默认取 `github.ref`；`pull_request` 事件的
`github.ref` 为 `refs/pull/<n>/merge`，`HEAD` 是 GitHub 合成的合并提交。这一点不能只看
文档：我拉了真实 `refs/pull/749/merge`（`82072bf7`），其提交对象两个父提交是
`cdf60a9e`（第一父，即事件 `base.sha`）与 `9d6b08c2`（第二父，即 PR head），主题逐字为
`Merge 9d6b08c2485d377917ad553464806771c9e7ae44 into cdf60a9efc169434aa1122020ce6d47a7b7ac9a3`，
committer 是 `GitHub <noreply@github.com>`。再加一条反证：#741 的旧代码在 CI 里输出
31 条路径，而 `git diff d58127c0 c7d924bf`（若 HEAD 其实是 PR head）只有 9 条——31 只可能
来自合并树。`merge-base` 断言我在真实 #741 形态上直算：`merge-base(d58127c0, 合并提交)`
输出就是 `d58127c0`，且两点与三点**都**是 31 条路径，逐条同集合（见证据 2）。

**缺 `--head-sha` 时会不会静默用错端点：不会退回两点口径，这一点作者说得对。**
形态判定完全来自提交对象本身（父提交数 + 主题正则 + 父集合），与 `--head-sha` 无关；
我把 `--head-sha` 去掉，在真实 #741 形态上实跑，仍走
`compared 4514b65b1e10..4966343692ec (merge ref against its first parent)` 并判绿——
与传入时一致（证据 3）。但它的作用不止「确认第二父」：`--head-sha` 是**父提交角色
倒置的唯一守卫**，缺它时主题只看「两个 SHA 是否恰好是这两个父提交」，不看谁在第一、
谁在第二；角色一旦倒置，比较端点会变成 `head..merge`（= 主干那些路径），在我构造的
输入上直接**误绿**。这条路径今天不可达，但它说明「缺 `--head-sha` 也无害」这个更强的
读法不成立。

## 十二条逐条判定

| # | 项 | 判定 |
| --- | --- | --- |
| A1 | `pull_request` 的 checkout 默认 ref / `ci.yml` 该 job 有无 `ref:` | **成立**（独立实测） |
| A2 | `merge-base(base.sha, merge-head) == base.sha` | **成立**（真实 SHA 直算） |
| A3 | 比较合并提交与第一父 ≡ 本 PR 自有改动 | **成立**（含 rebase 场景推演） |
| B4 | 主题格式变更退回三点的风险方向 | **成立但需限定**（CI 路径不误绿，本地缺参路径可误绿） |
| B5 | 缺 `--head-sha` 的行为 | **不退回两点（对）；但它是角色倒置的唯一守卫（文档写轻）** |
| C6 | 标题+分支都改名、本 PR 自己混实现仍红 | **成立**（实跑 + 变异） |
| C7 | 失败关闭未退化、文案可区分 | **成立**（浅检出 + 不可达 origin 实测） |
| C8 | 本 PR 自己混实现仍红 | **成立**（实跑） |
| D9 | 旧/新对照复跑 | **成立**（旧输出与 CI 日志 sha256 相同） |
| D10 | 341 → 347，钉子与前提断言 | **成立**（前提断言确实存在） |
| E11 | 改动范围、`uv.lock`、未碰产品/契约 | **成立** |
| E12 | `ci.yml` 只传参不改门禁条件 | **成立** |

### A. 核心论断

**A1（成立）。** `ci.yml:27-32` 的检出块只有两项：

```yaml
      - name: Check out repository
        uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1
        with:
          persist-credentials: false
          fetch-depth: 1
```

没有 `ref:`/`repository:`，因此取默认 `github.ref`。三条独立证据把它钉死为合并提交：
（a）真实 `refs/pull/749/merge`（`82072bf7`）的提交对象主题与双父如上；
（b）若 HEAD 是 PR head，旧代码在 #741 上会打印 9 条路径而不是 31 条；
（c）本 PR 自己的最新运行日志里，边界步骤的 `env` 出现了 `HEAD_SHA`、命令行出现了
`--head-sha`——这两个只存在于 PR 侧的 `ci.yml`，说明 `pull_request` 运行用的工作流来自
合并引用一侧（顺带说明 `ci-validation.md` 里「`pull_request` 取的是基线分支的工作流」
这句既有表述与观测矛盾，见建议 3）。

**A2（成立）。** 我按 #741 的真实元数据重建合并提交（`base.sha=d58127c0`、第一父
`4514b65b`、第二父 `c7d924bf`），直算：

```text
$ git merge-base d58127c02eac437913b051be77eec973115a1489 HEAD
d58127c02eac437913b051be77eec973115a1489
$ git diff --name-only --no-renames d58127c02eac437913b051be77eec973115a1489 HEAD | wc -l
31
$ git diff --name-only --no-renames d58127c02eac437913b051be77eec973115a1489...HEAD | wc -l
31
$ git diff --name-only --no-renames HEAD^1 HEAD | wc -l
9
```

`merge-base` 输出与 `base.sha` 完全相同，两点/三点同为 31，第一父只剩 9——作者的
「三点在合并引用形态下是 no-op」由此成立。附带一个我自己的结论：因为 `base.sha` 是
`4514b65b` 的祖先（我在 `origin/main` 历史里核过 `--is-ancestor`），这个等价对任何
「PR 打开后主干未改写」的 PR 都成立；它不依赖 #741 这一例。

**A3（成立）。** 合并引用的树 = 把 PR 改动合到第一父的树上，因此
`diff(第一父, 合并提交)` 的路径集恰是「本 PR 相对基分支头新增/改动的路径」：主干已进
第一父的路径两侧相同、自动退出；PR 自己碰过的路径进入集合。关于本卡点名的反例
（期间主干合入 + PR head 又 rebase 过，第一父 ≠ `base.sha`）：**仍然正确**，因为比较端点
不依赖 `base.sha`——我读代码确认 merge 分支在 `comparison()` 里 `return` 于
`_ensure_commit(base)` 之前，`base` 只做 40 位十六进制格式校验（`comparison()` 第 405–422 行）；
第一父是合并引用构建时的基分支头，两者都指向「现基线」，diff 只反映 PR 的净贡献。
我另外用可控 fixture 验证了主干路径确实被排除（C6/C8 的 `violations` 只有
`backend/pr.py`，主干的 `docs/reviews/main-line.md` 与 `backend/y.py` 没有出现）。
唯一需要记住的是语义边界：它给的是「本 PR 叠加到当前基线后的净差异」，不是「作者在
自己分支上敲了什么」——如果期间主干改过同一文件，merge 结果会带上合流内容，这与门禁
要守的东西（本 PR 是否把实现带进审查 PR）方向一致。

### B. 形态判定

**B4（成立但需限定）。** 若 GitHub 改了合并提交主题，`_merge_ref` 返回 `None`，退回
`merge-base(base.sha, HEAD)..HEAD`；在合并引用检出下这就是两点口径，于是
**任何 `base.sha` 落后于主干的 PR 重新变红**，即 #747 的假阳性原样回来。方向上是
**误红而非误绿**，作者的判断在 CI 路径成立：主题不匹配（不属于「角色倒置」而是
「完全不匹配」）时，`--head-sha` 一定传、`head_sha != merged` 会先被拒，两重保险都在。
理由是：绿灯的代价是事故类问题（#738），红只需要作者改一行（或维护者 re-run）——本仓库
的取舍在 #739 已定，我不反对。严重的部分不是方向而是**可发现性**：这条回退只改一行
`compared ... (merge base of base.sha and HEAD)` 标签，不告警。建议在退回时多打一行
`::warning`（见建议 2）。

**B5（对，但不完整）。** 实测三条（证据 3）：

1. 缺 `--head-sha`、真实 #741 形态 → 仍判 `merge ref against its first parent`、绿，
   **没有**退回两点口径。作者这句成立。
2. `--head-sha` 与实际第二父不一致（我传 `4514b65b` 模拟事件与检出不一致）→
   `_merge_ref` 返回 `None` → 退回三点，标签变成
   `compared d58127c02eac..4966343692ec (merge base of base.sha and HEAD)`，
   31 条路径、28 条 violations、红。**这正是在合并引用检出下退回两点口径**，也正是本 PR
   要消灭的那类假阳性；触发条件是「事件里的 head 与检出到的合并提交第二父不一致」，
   现实入口是 re-run 一个旧运行（refs 在运行开始时重新抓取，事件 payload 仍是旧
   `head.sha`）或 push 与 checkout 之间的竞态。修法三行（见必须修 1）。
3. 主题角色倒置（`Merge <base> into <head>`）+ 缺 `--head-sha` → 父集合校验挡不住
   （它只比集合，不比集合顺序），端点变成 `head..merge` → 未声明但真混实现的 PR
   **误绿**（我构造的 fixture 上 `exit=0`、`applicable:false`、`ok:true`）。
   同一输入给上 `--head-sha` 则退回三点、红。所以 `--head-sha` 今天不只「确认第二父」，
   它还是角色倒置的唯一守卫；而 CI 一定传，故这条是外部格式假设的兜底，不是现网缺陷。

### C. 不能放宽

**C6（成立）。** fixture：`main` 先有基提交，`pr` 分支加 `docs/reviews/own.md` 与
`backend/pr.py`，`main` 期间再合入 `docs/reviews/main-line.md` 与 `backend/y.py`，最后
`git merge --no-ff` 造出主题为 `Merge <pr> into <main>` 的合并提交。标题与分支**都改名**
（`test: renamed` / `test/renamed`）后实跑：`::error title=Path outside docs/reviews/::backend/pr.py`，
`exit=1`，`violations` 只有 `backend/pr.py`。变异 M2（把 `structure` 恒置 `False`）→
`test_a_renamed_title_and_branch_are_still_caught_by_the_structure` 与
`test_structure_is_a_third_independent_signal` 两条用例红（证据 6），说明结构信号在
merge 形态下仍是独立守卫、且被测试钉住。

**C7（成立）。** 用真实 GitHub 合并引用做浅检出（`fetch --depth=1`，第一父本地不存在，
`rev-parse HEAD^1` 直接失败，但 `cat-file commit` 仍能看到 `parent` 行——`_parents()` 的
注释成立），`origin` 指向不可达地址后实跑：

```text
::error title=Review PR boundary unverifiable::The exact change set of this pull request is unavailable;
a declared review PR cannot be verified (fetching cdf60a9efc169434aa1122020ce6d47a7b7ac9a3 from
origin failed after 2 attempts: exit 128: fatal: unable to access ... Failed to connect ...)
{"applicable": true, "ok": false, ...}   exit=1
```

文案与真的越界（`::error title=Path outside docs/reviews/::`、`::error title=Review PR has
no report::`）可以区分；未声明 PR 在同一条件下仍是 `applicable:false, ok:true`（文档
里那条残余缺口没有变化，不是本次放宽）。

**C8（成立）。** 同一 merge 形态下本 PR 自己同时改 `docs/reviews/own.md` 与
`backend/pr.py`、标题声明 `review:` → `exit=1`，`violations=["backend/pr.py"]`、
`reports=["docs/reviews/own.md"]`（fixture 与作者新增用例
`test_a_violation_on_the_pr_itself_is_red_with_main_ahead` 同向）。

### D. 证据

**D9（成立，且有超出作者范围的新证据）。** 我按 #741 的真实三 SHA 用
`git merge-tree --write-tree` + `git commit-tree`（双父 + GitHub 主题）重建合并提交，
旧代码（`57037a41` 版本）在同一 clone 上的输出与 CI 运行 43564561839 的 JSON 行
**逐字节相同**：

```text
$ shasum -a 256 /tmp/old-out.txt /tmp/ci-json.txt
04900a92416cbfefb755a2d2168eadfc5e85a7c3083dbba554cacca4d9b04b98  /tmp/old-out.txt
04900a92416cbfefb755a2d2168eadfc5e85a7c3083dbba554cacca4d9b04b98  /tmp/ci-json.txt
```

新代码在同一输入上：`{"applicable": false, "ok": true, "signals": [], ...}`
+ `review-pr-boundary: compared 4514b65b1e10..4966343692ec (merge ref against its first parent)`，
`exit=0`。

**D10（成立）。** `python3 -m unittest discover -s tests/ci`：基线 `0b79b633` **341**、
被审 head **347**（+6，与提交信息一致，无删除、无跳过）。新增 6 条即 `MergeBaseTests`
的六条。钉子用例 `test_main_moving_after_the_pr_opened_keeps_a_declared_review_green`
里确实写了前提断言（`tests/ci/test_review_pr_boundary.py:432-438`）：

```text
432:        self.assertEqual(self.git("merge-base", self.base, "HEAD"), self.base)
433:        two_point = self.git("diff", "--name-only", "--no-renames", self.base, "HEAD").splitlines()
434:        self.assertIn(self.MAIN_MOVED_REPORT, two_point)
435:        self.assertIn(self.MAIN_MOVED_IMPL, two_point)
```

即「三点单独无效」这个反直觉事实被写进了测试，而不是只出现在注释里——这一点作者做得比
要求好。变异 M1（把端点换回两点）→ 5 条用例红；M2（删结构信号）→ 2 条用例红（证据 6）。

### E. 范围与记录

**E11（成立）。** `git diff origin/main...9d6b08c2 --stat` 只有 4 个文件、`+328 / −39`：
`scripts/dev-env/check-review-pr-boundary.py`、`tests/ci/test_review_pr_boundary.py`、
`docs/validation/ci-validation.md`、`.github/workflows/ci.yml`；`uv.lock` 0 命中；
`backend/`、`contracts/`、`packages/`、`web/` 全部 0 命中——未碰产品代码与契约。

**E12（成立）。** `ci.yml` 只加了两处：步骤 `env` 增加
`HEAD_SHA: ${{ github.event.pull_request.head.sha }}`，命令行增加 `--head-sha="$HEAD_SHA"`，
其余是注释与文档；步骤名、位置、没有 `if:`、没有 `continue-on-error`、没有改 `Gate`
依赖。传参本身不改变门禁条件：`--head-sha` 只参与「第二父是否与事件一致」的判定，缺省
不影响形态识别（B5 实测）。

## 必须修清单

1. **`head_sha` 不一致时不要退回三点。** `_merge_ref()` 第 381–385 行在
   `head_sha != merged` 时返回 `None`，`comparison()` 随即退回
   `merge-base(base.sha, HEAD)..HEAD`——在合并引用检出下就是两点口径（B5 第 2 条实测：
   28 条假 violations、红）。现实入口是 re-run 旧运行（运行开始时重新抓取 refs，事件
   payload 仍是旧 `head.sha`）或 push/checkout 竞态。不论哪种，检出到的合并提交与第一父
   的差异仍是「本次要门禁的内容」，退回更差的端点没有收益。最小改法：形状（父提交数 +
   主题 + 第二父是 PR 侧提交）成立时**始终**按第一父比较，`head_sha` 不一致只打
   `::warning`（或干脆删掉这层判断）；补一条用例（第二父与 `--head-sha` 不同仍绿）。
   若维护者认为 re-run 属「过期事件」、红是刻意的失败关闭，则在文档里写明这条回退，
   不要让它只以标签变化的形式存在。
2. **父提交「角色」没校验，`ci-validation.md` 把 `--head-sha` 写轻了。** 第 379 行用
   `{merged, onto} != set(parents)` 只比集合；`Comparison(onto, head, ...)` 假定
   `onto` 是第一父，但代码没有验证顺序（我核过当前真实格式确实是 `<head> into <base>`，
   `onto == parents[0]`，所以今天不触发）。建议改成有序比对
   （`parents != [merged, onto]` → `None`），把假想的格式倒置压成失败关闭的红而不是
   静默误绿（B5 第 3 条有可执行反例）；同时把文档「缺 `--head-sha` 也不会静默退回
   合并引用的两点口径」补一句：它同时是父角色倒置的唯一守卫，缺省时形态只由主题与
   父集合认定。

## 建议（非必须）

1. **成功抓取第一父提交这条生产常态路径没有测试。** `fetch-depth: 1` 下第一父提交必然
   本地不存在，`_ensure_commit(onto)` → `_fetch_commit` 是每次运行都走的路；现有用例只
   覆盖「抓不到」的失败分支（`test_a_declared_review_that_cannot_read_its_change_set_is_red`）。
   我用真实远端验证了成功路径（证据 4：浅检出 + 真实 GitHub origin → 自动抓第一父 →
   `compared cdf60a9efc16..82072bf7f234`、4 条自身路径），但测试里没有它。配方很便宜：
   在 `GitFixture` 里 `git clone --bare` 一个 origin，`git fetch --depth=1 origin <merge>`
   造出「第一父不在本地」的状态，再跑一次断言绿。同批可补「缺 `--head-sha`」与
   「`--head-sha` 不一致」两条（与必须修 1 合并）。
2. **形态回退时打一行 `::warning`**：`_merge_ref` 返回 `None` 的原因（父数不对 / 主题
   不匹配 / 第二父不符）现在只体现为标签变化，运维看到红无法一眼分辨「门禁判对了」
   还是「形态没认出来」。这是 B4 那条外部假设的可发现性补丁，不动判定。
3. **顺手订正两处文档/注释**：`tests/ci/test_review_pr_boundary.py:504` 附近的注释
   `keeps a declared review RGB-red` 是笔误（应为 red）；
   `docs/validation/ci-validation.md`「调度证据等级」段里「`pull_request` 取的是基线分支
   的工作流」与本 PR 自己的运行日志矛盾（该运行的 `env` 有 `HEAD_SHA`、命令行有
   `--head-sha`，只可能来自 PR 侧 `ci.yml`）。两处都不是本 PR 引入的判定缺陷，但都在
   本 PR 的文档主线附近，顺手改比留下一句错的事实更好。

## 独立复现的证据

### 1. A1 的 checkout 配置（`ci.yml:27-32`，`changes` 作业）

```text
      - name: Check out repository
        uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1
        with:
          persist-credentials: false
          fetch-depth: 1
```

无 `ref:`。真实合并引用的提交对象：

```text
$ git fetch --no-tags origin refs/pull/749/merge   # -> 82072bf7f234795c67e0720099ed5dbdc7da1fb3
$ git log -1 --format='subject=[%s] parents=[%p]' 82072bf7...
subject=[Merge 9d6b08c2485d377917ad553464806771c9e7ae44 into cdf60a9efc169434aa1122020ce6d47a7b7ac9a3]
parents=[cdf60a9e 9d6b08c2]
```

### 2. A2 的 merge-base 断言自算（真实 #741 元数据重建）

```text
$ git merge-base d58127c02eac437913b051be77eec973115a1489 HEAD
d58127c02eac437913b051be77eec973115a1489
two-point base..HEAD:    31
three-point base...HEAD: 31
first-parent..HEAD:      9
```

### 3. B5 的缺 `--head-sha` / 不一致 / 角色倒置实测

```text
# 缺 --head-sha（真实 #741 形态）
review-pr-boundary: compared 4514b65b1e10..4966343692ec (merge ref against its first parent)
exit=0

# --head-sha 与实际第二父不一致
review-pr-boundary: compared d58127c02eac..4966343692ec (merge base of base.sha and HEAD)
{"applicable": true, "ok": false, ... 28 violations ...}   exit=1

# 主题角色倒置 + 缺 --head-sha（fixture /tmp/fx-f2）
correct endpoint (first-parent..merge): backend/pr.py docs/reviews/own.md
reversed endpoint (second-parent..merge): backend/y.py
{"applicable": false, "ok": true, ...}   exit=0        # 误绿：PR 真混了实现
```

### 4. 真实浅检出下抓取第一父提交（生产常态路径）

```text
$ git init; git remote add origin https://github.com/hrygo/SpeechNexus.git
$ git fetch --depth=1 origin refs/pull/749/merge; git checkout --detach FETCH_HEAD
HEAD=82072bf7f234795c67e0720099ed5dbdc7da1fb3
$ git cat-file -e <first-parent>^{commit}   # fatal: Not a valid object name（CI 同款）
$ python3 check-review-pr-boundary.py --base=cdf60a9e --head-sha=9d6b08c2 --title="review: probe" ...
{"applicable": true, "ok": false, ... "reports": [], "reason": "Review PR reports no file under docs/reviews/ (4 path(s) outside)."}
review-pr-boundary: compared cdf60a9efc16..82072bf7f234 (merge ref against its first parent)
```

第一父被抓取成功、diff 成功、且我故意声明 `review:` 后得到「没有报告」这条红——
同时证明该 PR 的真实自有改动只有 4 条路径，与 `git diff origin/main...9d6b08c2` 一致。

### 5. C7/C8 实测（失败关闭 + 真违规）

```text
# C7：真实合并引用浅检出 + origin 不可达 + 声明 review:
::error title=Review PR boundary unverifiable::The exact change set of this pull request is unavailable; ...
{"applicable": true, "ok": false, ...}   exit=1

# C8：merge 形态下本 PR 自己混实现
::error title=Path outside docs/reviews/::backend/pr.py
{"violations": ["backend/pr.py"], "reports": ["docs/reviews/own.md"]}   exit=1
```

### 6. D9 旧/新对照复跑（真实 #741 元数据）

```text
# 旧代码（57037a41）
{"applicable": true, "ok": false, "signals": ["docs/reviews mixed with other paths"],
 "violations": [28 条], "reports": [3 份 docs/reviews/2026-11-14-*.md],
 "reason": "Review PR changes 28 path(s) outside docs/reviews/."}   exit=1
# 与 CI 日志 JSON 行 sha256 相同（04900a92...，见正文 D9）

# 新代码（9d6b08c2）同一 clone、同一 HEAD
{"applicable": false, "ok": true, "signals": [], "violations": [], "reports": [], ...}
review-pr-boundary: compared 4514b65b1e10..4966343692ec (merge ref against its first parent)
```

### 7. D10 用例数与钉子断言

```text
$ python3 -m unittest discover -s tests/ci     # 基线 origin/main（0b79b633）
Ran 341 tests ... OK
$ python3 -m unittest discover -s tests/ci     # 被审 head（9d6b08c2）
Ran 347 tests ... OK
```

变异（在 `/tmp/sn750-mut`、`/tmp/sn750-mut2` 的 detached 副本内，未 push）：

```text
M1 端点改回两点      -> FAILED (failures=5)
  test_a_head_checkout_uses_the_signed_off_merge_base
  test_a_violation_on_the_pr_itself_is_red_with_main_ahead
  test_main_moving_after_the_pr_opened_keeps_a_declared_review_green
  test_main_moving_after_the_pr_opened_keeps_an_undeclared_pr_not_applicable
  test_the_merge_ref_reading_is_its_first_parent
M2 删结构信号        -> FAILED (failures=2)
  test_a_renamed_title_and_branch_are_still_caught_by_the_structure
  test_structure_is_a_third_independent_signal
```

### 8. 静态检查（在被审 head 快照内，`uv sync --locked --all-packages --all-groups` 之后）

```text
$ ruff check .                    -> All checks passed!
$ ruff format --check .           -> 1847 files already formatted
$ uv run mypy --platform linux    -> Success: no issues found in 1452 source files
$ uv run lint-imports             -> Contracts: 9 kept, 0 broken.
```

### 9. 本 PR 的 `gh pr checks` 与该步骤日志行

`gh pr checks 749`：21 个结论全部 `pass` 或按范围 `skipping`（`Docs: markdown code block formatting`
为 `skipping`，由范围路由决定），含 `Scope routing and CI / design-token checks`、
`Gate: required checks for this change`。

运行 43567960143（job 132365854633）的边界步骤：

```text
env: EVENT_NAME: pull_request / BASE_SHA: cdf60a9efc16... / HEAD_SHA: 9d6b08c2485d...
run: python3 scripts/dev-env/check-review-pr-boundary.py \
       --event="$EVENT_NAME" --base="$BASE_SHA" --head-sha="$HEAD_SHA" ...
{"applicable": false, "ok": true, "signals": [], "violations": [], "reports": [], ...}
review-pr-boundary: compared cdf60a9efc16..82072bf7f234 (merge ref against its first parent)
```

### 10. 本 PR 自身在「主干已前进」下的旧/新对照（新绿）

用 PR #749 自己的 head `9d6b08c2`、其事件的 `base.sha=cdf60a9e`、当前主干 `0b79b633`
重建合并引用后复算（这正是 #747 那条假阳性的当前可复现输入）：

```text
two-point paths: 5  (含 #748 合入的 docs/reviews/2026-11-14-render-unit-editor-bounds.md)
first-parent paths: 4 (仅本 PR 自己的 4 个文件)

# 旧代码
{"applicable": true, "ok": false,
 "signals": ["docs/reviews mixed with other paths"],
 "violations": [".github/workflows/ci.yml", "docs/validation/ci-validation.md",
                "scripts/dev-env/check-review-pr-boundary.py", "tests/ci/test_review_pr_boundary.py"],
 "reports": ["docs/reviews/2026-11-14-render-unit-editor-bounds.md"]}   exit=1
# 新代码
{"applicable": false, "ok": true, "signals": [], ...}
review-pr-boundary: compared 0b79b633cfb5..fee607b1ffdc (merge ref against its first parent)   exit=0
```

也就是说：**这个 PR 修好的正是它自己现在的红**——旧代码在该 PR 的当前输入上会因 #748
合入的报告而红，新代码绿。这也是 #747 验收标准里「不再需要手动 Update branch」的直接
证据（除本 PR 的运行日志之外的第二份）。

## 新绿

- 主干前进（含 `docs/reviews/` 报告）不再进入比较集：`MergeBaseTests` 六条用例 + 我在
  真实 #741 元数据与 PR #749 自身输入上的复算。
- 钉住「三点在合并引用下无效」的反直觉前提（`test_review_pr_boundary.py:432`），使这条
  事实不会随重构静默丢失。
- 比较端点写进 step summary（`- Compared: <a>..<b> (<label>)`），红灯可直接归因、无需
  复跑。

## 过程记录

| 时间 | 动作 |
| --- | --- |
| 14:58 | `git fetch`，从 `origin/main`（`0b79b633`）建独立 worktree `/private/tmp/sn750` + 分支 `review/750-boundary-fix` |
| 15:00 | 读被审 diff（4 文件）；建 detached 快照 `/private/tmp/sn750-pr`（`9d6b08c2`） |
| 15:01 | `gh pr view 749`：base `cdf60a9e`、head `9d6b08c2`、main 已前进到 `0b79b633` → 本 PR 自身即为假阳性输入 |
| 15:02 | `tests/ci`：基线 341 / head 347 |
| 15:03 | 核实 `ci.yml` 的 checkout 无 `ref:`；拉真实 `refs/pull/749/merge`（`82072bf7`）核主题与双父；发现 #741 的 merge ref 已被回收 |
| 15:04 | 用 `merge-tree`+`commit-tree` 重建 #741 合并提交，旧代码输出与 CI 日志 sha256 相同；`merge-base` 与两点/三点同为 31、第一父 9 |
| 15:06 | B5：缺 `--head-sha` 仍走第一父（绿）；`--head-sha` 不一致退回三点（红）；构造角色倒置 + 缺参 → 误绿 |
| 15:07 | C6/C8 fixture：改名与声明两态都红且只点名本 PR 路径；C7 用真实合并引用浅检出 + 不可达 origin 验失败关闭 |
| 15:08 | 真实浅检出 + 真实 GitHub origin 验「抓第一父」成功路径；`/tmp/fx-f2` 验角色倒置误绿 |
| 15:09 | 变异 M1（两点）5 红、M2（删结构信号）2 红 |
| 15:10 | `uv sync`、`ruff check`、`ruff format --check`、`mypy --platform linux`、`lint-imports` 全绿 |
| 15:11 | `gh pr checks 749` 全 pass；取运行 43567960143 的边界步骤日志（`compared cdf60a9efc16..82072bf7f234`）；PR #749 自身输入上旧红/新绿复算 |
| 15:12 | 写本报告，`uv run --no-project ruff format` + `ruff format --check .`；`git diff origin/main...HEAD` 仅含本报告；提交并 push `review/750-boundary-fix` |

## 只能依赖作者陈述的点

1. **#741 的原始合并提交对象**已不可得：我确认 `git fetch origin refs/pull/741/merge`
   失败（PR 合并后该 ref 被回收），因此「旧输出与 CI 日志逐字节相同」这条，我是用
   `git merge-tree --write-tree` 重建的合并提交复现的——结果 sha256 与日志一致，所以
   **结论独立成立**，但严格说不是「同一个提交对象」。
2. **作者自称列了 4 条未验证项**，但 PR #749 正文只有「反例」与「验证」两节、issue #747
   下无评论，我找不到该清单，无法逐条核对；本报告改为自列未验证项（见下）。
3. **GitHub 合并提交主题/父序的稳定性**属外部假设，无法用仓库状态证明；我只核实了
   今天（2026-11-14）真实 `refs/pull/749/merge` 的形态，以及 #741 日志与旧代码输出
   反推出的形态一致。
4. **「docs 路由下该步骤真的执行」仍无日志观测**：本 PR 的路由是 `full`（日志里
   `{"mode": "full"...}`），不是 docs；该结论仍是结构推断，与本 PR 无关但未因本 PR
   变成实测。
5. **`fetch-depth: 1` 下抓取第一父提交的 CI 实际耗时**作者只写了「与路由抓取 `base.sha`
   同一机制」，我没有在 GitHub runner 上量过墙钟时间；本地与真实远端的成功抓取是
   实测的，耗时按同机制推断。
