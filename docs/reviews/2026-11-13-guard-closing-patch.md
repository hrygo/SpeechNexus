# 结构守卫收尾补丁的非作者定点核对（#684 / #572 收口）

- **核对对象**：`6b6e2dd4`（`ci: 收口结构守卫的块状 runs-on、PEP 695 TypeAlias 与续行前置 (#572) (#684)`），已合并；基线 `origin/main` head 即此提交，前一提交为合入本轮审查报告的 `9d915e9c`。
- **范围**：3 个文件，+113/−19（`ci-entry-guard.py` +66/−16、`tests/ci/test_ci_entry_guard.py` +44/−0、[ci-validation.md](../validation/ci-validation.md) +3/−3）。**`ci.yml` 与入口脚本未改**，`tests/ci` 307 → 310。
- **独立 worktree**：`/private/tmp/sn684b`，分支 `review/684-closing-patch`（`git worktree add ... origin/main`）。变异在同级目录 `/private/tmp/sn684b-mut`（每例前 `rsync -a --delete --exclude .git` 复原），**未 push 任何变异**。
- **核对方式**：只做定点核对，不开新一轮全套审查。四项逐条独立复现；其余以「本轮审查报告判定的三项必须修」为唯一验收口径。

---

## 结论

**四项全部成立。** 三条回滚必红复现为 **3 / 1 / 1**，且红的用例与理由都是对应的那一条；G3 最严重的洞真堵住（且不是碰巧）；没有找到行为级新绿；`runs-on: [ubuntu-24.04]` 的翻转是**修对了，不是放水**。

**切片可以收尾。** 唯一残留是 `runs-on: []` 这条不可利用的空列表不一致，建议顺手一行，不构成再开一轮的理由。

---

## 四项逐条判定

### 1. 三条回滚必红是否成立：**成立（3 / 1 / 1，与作者陈述完全一致）**

| ID | 回滚内容 | 作者陈述 | 我的实测 | 红的用例 |
| --- | --- | --- | --- | --- |
| ① | `check_platform_runner` 退回只认标量 `runs-on` | `failures=3` | **3** | 全部是 `test_every_runs_on_spelling_gets_the_same_verdict` 的三个 subTest |
| ② | 删掉 `ast.TypeAlias` 分支 | `failures=1` | **1** | `test_a_type_alias_is_not_a_provable_constant` |
| ③ | 从 `PRELUDE_SHELL_OPERATORS` 删掉 `\` | `failures=1` | **1** | `test_a_line_continuation_prelude_is_red` |

**确认红的是对应断言而不是连带红**（逐条查了失败消息与行为）：

- ① 三个 subTest 分别是：`[ubuntu-24.04]` 期望绿实得红（正是被翻转的那条）；块状 `- ubuntu-24.04` 因测试用 `str.replace` 把 12 个作业全换成块状、旧解析器读不到任何 `runs-on` 而红；块状 `- macos-14` 的 `assertIn("macos-14")` 因消息退化成「没有任何 `runs-on`」而红。三条都在同一条用例里、都是「块状列表必须按元素读」的直接推论，**没有别的用例被带红**。
- ② 行为层复核：`_module_constants(ast.parse("F = False\ntype F = int\n"))` 由 `{}` 变 `{'F': False}`——红的正是「别名不算第二次绑定」这条断言。
- ③ 行为层复核：`sites` 前插 `: \` 由红转绿——红的正是「续行前置」这条断言。

复原后 `Ran 310 tests … OK`（3.14.9 与 3.12 各跑一遍，均 310 例全绿）。

### 2. G3 这个最严重的洞是否真堵住：**堵住了，且红的是「守卫会不执行」**

| 前置行 | `wiring` | 说明 |
| --- | --- | --- |
| `: \`（续行） | **RED** | 消息点名 `: \`，理由是「`sites` 前面还有会先执行的命令」，并写明「续行会把下一行并进这一行」 |
| `set -euo pipefail \` | **RED** | 同一个洞的同型写法也堵住 |
| `:`（裸） | GREEN | 合法 no-op 未被误伤 |
| `set -euo pipefail` | GREEN | 合法前置未被误伤 |

**「不是碰巧」的证据**：红的对象就是那条前置行本身（消息回显 `: \`），同一步内其它前置形态保持绿；且 `bash -c` 实测 `: \` + 下一行 → 输出为空、`rc=0`，即下一行的守卫命令确实被并进 no-op 不执行。

**`log` 步骤自身的 `\` 续行没被误伤**（`ci.yml:1082`）：真实工作流 `wiring: OK (wiring: 4 guard steps)`。机制上黑名单只在 `lines[:position]`（守卫命令**之前**的行）上生效，`log` 的续行在守卫调用行本身之后；镜像探针（把 `sites` 调用行也拆成 `\` + 续行，形状与 `log` 完全一致）仍为 GREEN。**`log: OK` 本身本机未能端到端复现**——本地缺 CI「Prepare the acceptance environment」的产物，dry-run 产出 `pending=2` 而非 1，`log` 因 `DECLARED_DEFERRALS` 内容不符而红；该报错是**内容级**的，反过来证明调用行被正常解析。这一点记为依赖作者陈述。

### 3. 有没有新绿：**没有行为级新绿；一条不可利用的不一致**

| 探针 | 结果 | 判定 |
| --- | --- | --- |
| 块状 `runs-on:` + `- ubuntu-24.04` 与 `- macos-14` 混排 | **RED**（点名 `macos-14`） | 收紧正确 |
| 行内 `[ubuntu-24.04, macos-14]` | **RED** | 收紧正确 |
| `runs-on:` 空值 | **RED**（消息回显空镜像） | 新收紧（此前静默跳过） |
| 带 `uses:` 的作业（无 `runs-on`） | **GREEN**，不报「缺失」 | 与作者一致，属已披露边界 |
| `runs-on: ['ubuntu-24.04']` / 尾注释 / `${{ matrix.os }}` | RED | 已披露（文档明写不归一、按字面量判红） |
| **`runs-on: []`（空行内列表）** | **GREEN** | **唯一新绿**：`_runs_on_elements("[]")` 返回空表 → 该作业不进核对集合，与「空块状 → 记空串 → 红」不一致。GitHub 会拒绝没有 runner 标签的作业，实际不可利用 |
| `type F = …` 在函数体/类体内 | 常量不可证明（**假红**） | 过严，与既有的 `global` 声明同族，低影响 |
| 前置行含引号内反斜杠（`export WINPATH='C:\x'`） | **RED** | 文档已明写「对整行子串匹配，引号里的也算」，属已披露 |

其余三项修复的归一化没有放过别的形态：`_runs_on_elements` 只拆 `[...]`，flow mapping / 折叠标量 / 表达式一律按字面量判红；`TypeAlias` 分支只影响同名绑定，无关别名仍判可证明；`\` 只加进黑名单，既有 7 个算符一个没删。

### 4. `runs-on: [ubuntu-24.04]` 由红转绿：**修对了，不是放水**

- **谓词没变**：仍然是「作业命名的**每一个** runner 镜像都必须等于 `CI_RUNNER_IMAGE`」。变的是把行内列表解析成元素、而不是拿整串 `"[ubuntu-24.04]"` 与 `"ubuntu-24.04"` 比——旧行为是字符串比较的假红（本轮审查报告已把它列为假红第 1 条）。
- **等价性成立**：`[ubuntu-24.04]` 命名的镜像集与标量 `ubuntu-24.04` 完全相同，因此必须同判；否则同一语义两种拼写方向相反，正是这次要修的病。
- **没有放水的路径**：`[ubuntu-24.04, macos-14]` 红、`['ubuntu-24.04']` 红、`${{ matrix.os }}` 红、`[ubuntu-24.04, ${{ matrix.arch }}]` 红。唯一例外是 `runs-on: []`（空集）被跳过，见第 3 项。

---

## 其它核对（门禁对齐）

- 八张表（`MUTATE_SITES` / `READ_ARGUMENTS` / `WRITE_TOKENS` / `PROBE_CASES` / `DECLARED_DEFERRALS` / `SELFTEST_CASES` / `PINNED_COUNTS` / `CI_PLATFORM_STRINGS`）与上一提交**逐字段相等**；`render_tables()` 逐字节相同（4183 字符）；`tables --write` 幂等。
- `tests/ci` 的 diff **只增不删**（+44/−0），没有弱化任何既有断言。
- 「看不见」清单仍 15 项：第 13 项改成「环境前置不是 shell 解析的结果（只按行首关键字 + 字符黑名单）」，第 14 项改成「三种拼写的标量元素逐个比对」——**我上一轮判定的两条口径问题都已收紧，且没有删项**。四条覆盖边界（三种拼写、引号/尾注释/表达式仍按字面量判红、`:` 只是字符黑名单、字符表对整行子串匹配）都已写入。
- 仍未写入（我上一轮只列为可选）：跨模块证据重绑仍绿。

## 新绿

**无行为级新绿。** 唯一一条：`runs-on: []` 不进入核对集合（GREEN），实际不可利用；建议 `_runs_on_elements` 返回空表时记一个空串，与空块状形态对齐。

## 只能依赖作者陈述的点

1. **`log: OK`**：本机缺 CI 的 prepare 产物（私有挂载、vad 模型摘要），dry-run `pending=2` 而非 1，`log` 因 `DECLARED_DEFERRALS` 内容不符而红。调用行被正常解析这点已由报错层级与 `wiring: OK` 佐证。
2. **GitHub Actions 上 `docker compose config` 的真实输出**：未在 Actions 上替换 workflow 复现。
3. 真实 apply 路径的语义覆盖：与本轮审查一致，不在结构守卫的证明范围内。

---

## 切片可否收尾：**可以**

一句话：三项必须修全部落地且各有独立回滚锚（3 / 1 / 1），唯一行为翻转是把「同一语义两种拼写」统一成同一判定而不是放宽谓词，剩余项只有一条不可利用的 `runs-on: []` 与两条低影响过严，**已无继续返修的边际收益**。
