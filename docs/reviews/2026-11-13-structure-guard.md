# 参考验收入口结构完整性守卫的非作者审查（#572 / #668）

- **审查对象**：`ba99602e`（`ci: 把验收入口守卫收窄为结构完整性检查并消除假红假绿 (#572) (#668)`），已合并（PR #668 的 squash 结果，基线 `origin/main` head 即此提交）。
- **基线核验**：建 worktree 时主检出的 `origin/main` 还停在 `4b5c8418`，`git fetch origin` 后为 `ba99602e`，`git reset --hard origin/main` 后核对一致。`git cat-file -t ba99602e` → `commit`。
- **独立 worktree**：`/private/tmp/sn673`，分支 `review/673-structure-guard`（`git worktree add /private/tmp/sn673 -b review/673-structure-guard origin/main`）。变异实验另用 `/private/tmp/sn673-mut`（逐次 `rsync --delete` 从 `sn673` 复原），**未 push 任何变异**。
- **审查者**：非作者。前两轮报告 [2026-11-13-ci-acceptance-gate.md](2026-11-13-ci-acceptance-gate.md)、[2026-11-13-ci-guard-script.md](2026-11-13-ci-guard-script.md) 是本轮的对照基线。
- **审查方式**：读全量 diff（`ci.yml` +80/−35、`ci-entry-guard.py` 1355 行重写、`tests/ci/test_ci_entry_guard.py` 497 行重写、`tests/ci/test_node_controller_image_ci.py` +31、两份文档）；本机实跑四条子命令；在 `sn673-mut` 里做约三十组变异（复原后逐条记录红/绿与消息）；直调守卫函数做形态探针；用 `gh` 只读核对真实 CI 运行时长、作业结果与分支保护；用 `uv run --no-project` 临时装 pytest 跑 `tests/contract` 对照。
- **产品代码与 CI 零改动**：本片只新增本报告。`uv.lock` 未被 `uv` 改写（提交前 `git checkout -- uv.lock` 兜底）。

---

## 结论

**有条件通过。**

**定位收窄这件事是真做了。**上一轮被点名的三处语义级宣称——`ACCEPTED_JUDGEMENTS` 接受集、`REGISTERED_UNAUDITED`「无法判定的登记」、`ci.yml` 里的 `corrupting the entry itself fails a PR`——在代码与两份现行文档里**全部消失**（全仓 `rg` 只剩两份历史审查报告里的引用，那是历史记录，应保留）。`ci.yml:849-871` 换成了四项正面清单加一句明确的否定（"It does NOT prove a condition is correct"），文档的覆盖边界段也按结构口径重写，并且**"看不见"的清单本身是真的**：我逐项实测，被声明为"看不见"的形态确实绿。

**但「收窄」只完成了大约三分之二。**残留的是三类：

1. **三处正面承诺与实测冲突，其中两处是新的假绿**（不是"没承诺"，而是"承诺了却没做到"）：
   - `ci-validation.md:33` 说「结果被丢弃…都使作业红」，但只有**裸表达式语句**丢弃会红；`_ = ctx.mutate(verify)` 实测**绿**（H10）。
   - 同处与 `reference-acceptance.md:119-120` 说「`read` 的 argv 不是字面量也不是登记过的构造助手…都使作业红」，但 `ctx.read((*argv,))`（星号展开一个变量元组）实测**绿**（H1）——`_argv_tokens` 对 `*Name`/`*Attribute` 静默跳过（`ci-entry-guard.py:581-583`），整条 argv 与写词检查一起被绕过。
   - `reference-acceptance.md:132` 写「登记不是橡皮章：…证据列必须指向存在的 `tests/ci` 用例或 probe 用例」，实际只校验**名字存在**：正文可掏空、可 `@unittest.skip`、可指向无关用例；「判定在哪」这一列**完全不校验**（我写「谁也没判」仍绿，R3）。「不是橡皮章」这句强于实际。

2. **边界的举例写反了**：`ci-validation.md:41` 把 `if not started.ok: raise` 当作「语义反了也绿」的例子——那是**入口里的正确写法**（`reference-acceptance.py:938`、`1059`、`1062` 等十余处都是这个形态）。反转形态是 `if started.ok: raise`（M3 实测绿）。这句会让读者把正确写法当成反例，属于"自述边界不准确"。

3. **同一 PR 里夹了一处未披露的门禁收紧变松**：`ci.yml` 的「Prepare the acceptance environment」把「compose 有新私有挂载但没有 committed 示例」从 `raise SystemExit` 改成 `::warning::` + 合成 `{}` 占位。这条改变的是 job 的信号，不是守卫的结构，提交信息没提，`tests/` 也没有覆盖。详见附录。

**没有发现需要回滚的问题。** 建议的必改项：

1. 修上面三处过强表述（把「结果被丢弃」「read 的 argv 一律红」「不是橡皮章」改成各自实际覆盖的形态），并把 §41 的举例改成 `if started.ok: raise`。
2. 披露或回退附录里的门禁松动：至少把"缺示例 → 合成占位"写进 `ci-validation.md` 的覆盖边界，并考虑让新挂载缺示例重新变红（当前形态让 `stack` 步的私有输入扫描在空 JSON 上"通过")。
3. 把 §8 里漏掉的 7 类补进自述清单（尤其 `_ =` 与星号展开，它们与正面承诺直接冲突）。
4. 若要让「登记不是橡皮章」名副其实，最小改动是给证据加"相关性"约束（例如 `test:<用例名>#<站点 key>` 或要求证据函数的 docstring/断言里出现站点 key），这一条不是本轮的阻塞项。

---

## 时间线

| 时点 | 动作 | 结果 |
| --- | --- | --- |
| T0 | `git fetch`；建 `/private/tmp/sn673`（`origin/main` 先为 `4b5c8418`，fetch 后 `ba99602e`）与 `/private/tmp/sn673-mut` | 与主检出（陈旧分支）隔离 |
| T1 | 读全量 diff | 定位四条子命令、六张登记表、行数锚点、文档锚点块、web 真值表用例 |
| T2 | 读 `ci-entry-guard.py` 1355 行 + `tests/ci/test_ci_entry_guard.py` 497 行 + 两份文档 | 枚举出接受集边界与所有"静默跳过"分支 |
| T3 | 跑基线：`sites`/`probe`/`wiring` | 全绿，0.07/0.08/0.03 s；`tests/ci` 227 例 3.5 s 全绿 |
| T4 | 第二轮问题复现（M1–M5） | 关键字实参绿；`if not started:` 红且消息点名 `__bool__`；反转 `.ok` 绿；`read` 变量红并给登记行；`uv run docker compose run` 红 |
| T5 | 四类「删守卫」独立复现（D1–D4） | 四类都红；但 D3 配合改锚点后全绿（C1） |
| T6 | 结构洞探针（H1/H2/H5/H10 + B1 回归 + `_=`） | 全部绿，其中 H1/H10 与正文承诺冲突 |
| T7 | 橡皮章实验（R1/R2/R3/dup/skip） | 掏空正文、缩表+改锚点、无关证据、重复矛盾行、`@unittest.skip` 全绿；伪造证据名红 |
| T8 | 假红探针（C4/C5/C6/C7/C8） | `set -euo pipefail`、f-string argv、新只读助手会红；probe 软签名演进绿、硬签名红且消息可用 |
| T9 | 互锁验证（web 分支变异） | tests/ci 1 失败、contract 7 失败；换回 `origin/main~1` 的 tests/ci 对同一变异 12 例全绿（复现"此前只有 contract 会红"） |
| T10 | 只读核对真实 CI | 作业 success、12.0 s；三个受保护状态名未变 |
| T11 | 写报告、`ruff format`、提交、开 PR | — |

---

## 逐条清单

### 1. 文档与注释有无残留的语义级承诺

**结论：三处「换皮重述」+ 一处举例写反；被点名的旧表述确实全删。**

- `ACCEPTED_JUDGEMENTS`、`REGISTERED_UNAUDITED`、`corrupting the entry itself fails a PR`：代码与两份现行文档零残留（全仓 `rg` 只命中 `docs/reviews/2026-11-13-ci-acceptance-gate.md` 与 `docs/reviews/2026-11-13-ci-guard-script.md`，属历史记录）。**核实通过。**
- `ci.yml:849-871`：四项正面清单 + "It does NOT prove a condition is correct… an inverted `if` that still reads `.ok` is invisible to an AST"。这句是准确的（M3 实测绿）。
- `ci-validation.md:33`（新版）：「命令消失、命令新增、**结果被丢弃**、`read` 的 argv 不是字面量也不是登记过的构造助手…**都使作业红**」。三处与实测不符：
  - 结果被丢弃：只有 `ast.Expr` 父节点的裸调用会红（`ci-entry-guard.py:651-654`）；`_ = ctx.mutate(verify)` 的父节点是 `ast.Assign`，不红（H10 实测绿）。
  - `read` argv：`ctx.read((*argv,))` 不是字面量也不是构造助手，却绿（H1 实测）。
- `reference-acceptance.md:119-120`：「`ctx.read(...)` 的 argv 必须可静态确定且不含写命令」。星号展开时 argv 根本不是"可变但已知"，而是**未知**，仍绿。
- `reference-acceptance.md:132`：「登记不是橡皮章：`MUTATE_SITES` 的「证据」列必须指向存在的 `tests/ci` 用例或 probe 用例，否则红」。这句把"存在"写成了"是证据"，实际见 §4。
- `ci-validation.md:41`：举例写反（见结论 2）。
- **换皮重述的判定**：是。旧的「接受集 / 未审计登记」消失了，但 `MUTATE_SITES` 的「判定在哪」列（`ci-entry-guard.py:65` `judgement` 字段）承接了同一个语义问题，只是从"已被接受"换了说法。而这一列**没有任何校验代码**：R3 里我把新站点的 judgement 写成「谁也没判」，`sites` 绿。表头与文档措辞会让读者以为这一列是被审计的。

### 2. 第二轮的问题是否真消

| 形态 | 期望 | 实测 | 证据 |
| --- | --- | --- | --- |
| `judge(result=applied)`（关键字实参） | 绿（不再检查条件） | **绿** | M1；`tests/ci/test_ci_entry_guard.py:195-205` 明确断言它绿 |
| `if not started:` | 红，且说明 `CommandResult` 无 `__bool__` | **红**，消息：``条件直接用了 `started` 的真值；`started` 是 `CommandResult`（无 `__bool__`），该条件恒真/恒假`` | M2 |
| `if started is not None:` | 红 | **红**，消息把条件写成 ``用 `started is None` 判定``（措辞与源码相反，小瑕疵） | M9 |
| `if started.ok: raise`（反转） | 绿 | **绿** | M3，与作者自述一致 |
| `ctx.read(argv_var)` | 红并给登记行 | **红**，两条 `::error`，第二条为 `登记处: scripts/dev-env/ci-entry-guard.py:110 的 READ_ARGUMENTS` | M4 |
| `uv run docker compose run` | 红 | **红**：``read` 携带写命令（run）`` | M5 |

`.ok`/`.code` 形态与"按形参名读 `.ok` 的助手"现在都绿（`if r.code != 0:`、`judge_by_code(r)` 直调 `audit_sources` 实测绿），上一轮的等价写法假红确实消了。`probe` 与 `Context` 构造解耦也成立：软签名演进（追加 kw-only 默认参数）绿，硬签名（新增必填位置参数）红且消息可执行。

**边界是否写清**：「形状检查不是分支检查」这条写清了（`ci.yml:856-871` + `ci-validation.md:41` 的意图），但举例写反（结论 2），且未提及"删掉整段 `.ok` 守卫、结果仍被赋值"也绿——这是该作业存在的原始理由（B1），我只能从严的「对已登记站点的判定语义作任何改动」里间接读出来。

### 3. 四类「删掉守卫」+ 还有哪些删法仍绿

| 类 | 变异 | 结果 |
| --- | --- | --- |
| ① 换 `echo` | `ci.yml` 的 `sites` 步骤 `run` 改成 `echo "python3 … sites"` | **红**：``步骤 `Check the mutating surface…` 的 `sites` 被 `echo` 或别的命令包住，不是该步骤的第一条命令`` |
| ② 删 `bucket_names` probe 行 | 只删 `PROBE_CASES` 一行 | **红**，三重叠加：锚点不一致 + 行数锚点 + `probe 实现不在 PROBE_CASES 里登记` |
| ②' 同上 + 实现 + 锚点 + 文档 | 四处一起改 | **红**，但抓它的是 `tests/ci/test_ci_entry_guard.py::test_an_emptied_bucket_names_probe_is_red`（手写用例钉住 bucket_names 而已，不是结构性不变量） |
| ③ 删反空转自检 | 只删 `SELFTEST_CASES` 一行 | **红**（锚点 + 行数锚点） |
| ③' 同上 + 行数锚点 + `tables --write` | 三处一起改 | **全绿**（含 `tests/ci` 227 例）——C1 |
| ④ 写词表缩成 `{"run"}` | 只改 `WRITE_TOKENS` + 行数锚点 + 生成 | **红**：`自检用例 read-literal-write 期望红，实际绿` |
| ④' 部分收缩（只删 `create`）+ 锚点 + 生成 | — | **全绿**——自检语料只覆盖 `down`/`run`/`sh -c`/`python -c`，没有覆盖 `rm`/`restart`/`create` |

所以「咬住」的准确说法是：**在不配合改锚点的前提下咬住**。行数锚点在同一个模块里，`tables --write` 一条命令就把文档块重新生成——"必须同一次改动里改文档"的摩擦实际是**改一个数字 + 跑一条命令**。

**本轮新发现的、仍绿的删法/写法**（全部实测）：

| 编号 | 形态 | 结果 | 是否与正文承诺冲突 |
| --- | --- | --- | --- |
| B1 | 删掉整段 `if not started.ok: raise`，只留赋值 | 绿 | 无（"判定语义"已声明看不见），但它是作业的原始理由，值得点名 |
| H10 | `_ = ctx.mutate(verify)`（结果丢弃的常见惯用法） | 绿 | **冲突**：正文说"结果被丢弃"红 |
| H1 | `ctx.read((*argv,))`（星号展开变量元组） | 绿 | **冲突**：正文说 read argv 不可静态确定即红 |
| H2 | `other = ctx; other.mutate(('docker','compose','rm','-f','api'))` | 绿 | 清单里只说"包进自定义函数再丢弃返回"，未覆盖换接收者名 |
| H5 | `def _apply(recv, argv): return recv.mutate(argv)` + 两次不同命令调用 | 绿 | 同上（整个包装体与调用点均不可见） |
| R2 | 删一条写命令 + 删登记行 + 改行数锚点 + `tables --write` | 绿 | 文档只说"必须改文档"，未说文档块是一条命令的输出 |
| R3 | 新增写命令 + 登记（judgement="谁也没判"）+ 证据指向无关存在的用例 | 绿 | **冲突**：正文说"不是橡皮章" |
| dup | 同一站点登记两次、判定互相矛盾、证据不同 | 绿 | **冲突**：行数锚点被设计成"阻止缩水"，对矛盾项无效 |
| R1 | 证据用例首行插 `return`（正文成死代码） | 绿 | **冲突**：正文说证据必须"指向真实证据" |
| skip | 证据用例加 `@unittest.skip` | 绿 | 同上 |

### 4. 登记表是否是橡皮章（本次重点）

**(a) 证据用例是否真的在被跑：是。** `changes` 作业（`ci.yml:35`）用 `python3 -m unittest discover -s tests/ci -v` 跑 227 例（本机 3.5 s，`timeout-minutes: 3`）。8 条 `MUTATE_SITES` 指向的 5 个 `test:` 目标全部是 `tests/ci/test_reference_acceptance.py` 里的 `TestCase` 方法（`BuildFlagTest:978`、`StackExitCodeTest:663`、`IdempotencyTest:494`、`StopOnFailureTest:1014`、`BucketAndGateParsingTest:1107`），`unittest` 会收集，不是 pytest 风格的模块级函数。这一层没有"存在即可"的问题。

**(b) 掏空正文但保留函数名：绿。实测。** 把 `test_bucket_names_follow_the_init_script_rules` 的首行插入 `return`（后续 `with tempfile…` 全成死代码）：

```
--- guard:
sites: OK (sites: 8 registered)
--- run the emptied test file:
Ran 39 tests in 0.056s
OK
```

影响面：`step_buckets:start` / `initialize` / `verify` 三条登记共用这一个用例（`ci-entry-guard.py:85-99`），掏空它等于同时抹掉 8 条登记里 3 条的唯一证据。`_find_test` 只做 `def {name}(` 子串搜索（`ci-entry-guard.py:808-812`），连被 `if False:` 包住的定义、被 `@unittest.skip` 修饰的定义都算"存在"——`@unittest.skip` 实测也是绿（`OK (skipped=1)`）。

**(c) 行数锚点能否阻止"静默缩水"与"登记矛盾项"：都不能单独阻止。**

- 缩水：删 `step_gate:argv` 的写命令 + 删对应登记行 + `PINNED_COUNTS["MUTATE_SITES"]` 8→7 + `tables --write` → **四条子命令全绿**（R2）。改动量：一处代码、一行常量、一条命令。
- 矛盾项：同一 key 登记两次、第二行写「同一条站点又登记一次，判定写的是「谁也没判」，两边矛盾」、证据改指 `probe:gate_verdict-fail`，行数锚点 8→9 + 生成 → **绿**（`sites: OK (sites: 9 registered)`）。原因：`check_counts` 比 `len()`，而 `audit_sources` 用 dict 折叠（`ci-entry-guard.py:690-703`），两侧不对照，重复行无人发现。
- 部分收缩：`WRITE_TOKENS` 去掉 `create`（自检语料不覆盖该词）+ 锚点 + 生成 → **绿**。

**(d) 新增一条无证据的登记会红吗：会。** 这是我实测里**唯一完全站得住**的一层：登记 `test:test_that_does_not_exist` 同时触发「指向不存在的 tests/ci 用例」+「登记的写命令消失了」（因为站点不存在）+ 登记处提示行。**但相关性不校验**：把证据指向任一个存在的用例（我用 `test_the_contract_passes`）就绿（R3）。

**风险评估：不是纯橡皮章，但仍可盖章。** 三个真实约束——集合完整性（少一条写命令红）、登记↔实现双向一致（probe 两侧集合比大小）、证据名存在。盖一次章的成本是：改代码 → 改一行数字 → 跑一条生成命令。文档用「不是橡皮章」把这道摩擦描述成一道闸，读起来比实际强。最容易被滥用的方向恰好是**"先写实现再补登记"**：新写一个 `ctx.mutate(('docker','compose','rm','-f','api'))`，登记时 judgement 随便写、证据挂到附近任何一个存在的测试上，四守卫全绿——我用 R3 完整跑通了这个流程。

### 5. 结构检查自身的假红

| 编号 | 正确改动 | 结果 | 说明 |
| --- | --- | --- | --- |
| C4 | 在守卫步骤的 `run` 第一行加 `set -euo pipefail`（常见加固） | **红**，且消息误称「被 `echo` 或别的命令包住」 | 只有"第一步就是 `python3 scripts/dev-env/ci-entry-guard.py <cmd>`"才通过 |
| C5 | 合法新增 `ctx.read(("docker","compose","ls", f"--filter=name={ctx.project_name}"))` | **红**（`read` 的 argv 无法静态确定） | f-string 一律算不可确定 |
| C6 | 合法新增 `ctx.read(compose_ps_argv(ctx))`（项目内新的只读助手） | **红**（未登记的助手） | 必须同时改 `READ_ARGUMENTS` + 生成文档块 |
| C7 | 入口新增/重排步骤 | `sites`/`probe`/`wiring` 绿，`log` 红 | 设计如此（`EXPECTED_STEP_ORDER` 是刻意的第二份副本），但一次改动要动 entry + guard + 文档三处 |
| C8 | 已登记判定函数的软签名演进（追加 kw-only 默认参数） | **绿**（不误报） | ✓ |
| C8' | 硬签名演进（新增必填位置参数） | 红，消息点名「签名变化时同步 probe 用例、PROBE_CASES 与文档表」 | 可接受的成本 |
| — | 私有输入目录改名 | **绿**：`ci.yml` 已从 `compose.env.example` 派生目录（修掉了旧的硬编码假红）✓ | 但**键名**改名会静默回落 `./.runtime/private`（`ci.yml:949-952`），不报错——是静默点而非假红 |
| — | `compose.env.example` 里新增/删除行 | 目录派生只看 `SPEECHNEXUS_PRIVATE_CONFIG_DIR` 一行，其余变化无影响 | ✓ |

另外两处措辞级瑕疵：`is not None` 的消息写成 `is None`；`_hint` 用 `line.startswith(needle + ":")` 找行号（`ci-entry-guard.py:344-351`），若常量改成 `NAME = (` 形式会退化为 `:0`。

### 6. 成本与门禁

- **仍在门禁内**：`Gate: required checks for this change` 的 `needs` 含 `reference_acceptance`（`tests/contract/test_ci_workflow.py:220-241` 断言），full/no-db 期望 `success`、docs/web 期望 `skipped`（`ci.yml:739-747`）。三个受保护状态名未变（`gh api repos/hrygo/SpeechNexus/branches/main/protection` → `["Gate: required checks for this change", "Web: lint, types, tests, build and accessibility", "Compose: configuration validation"]`）。✓
- **真实运行时长**（`gh run view 43424070063`）：`Deploy: reference acceptance entry (dry-run execution)` = success，**12.0 s**（`Set up job` 1 s、`Check out` 3 s、`Set up uv` 2 s、`Execute … dry-run` 1 s、四条守卫步与负对照各 0.0 s、post 1 s）。对照：`4b5c8418` 的同一作业 9.0 s，`dd0fe3d6` 11.0 s。**「仍 9 s 级」应修正为 12 s 级**，+3 s 与抖动同量级（`setup-uv`/`checkout` 波动），不是守卫引入的。
- **守卫本身**：本机 Python 3.14 三次测量 `sites` 0.06–0.07 s、`probe` 0.08 s、`wiring` 0.03 s，合计 **< 0.2 s**（上一版 `sites`+`probe` 0.12 s）。「入口 + 四条守卫 measured under 2 s」成立。✓
- `tests/ci` 新增用例的代价：227 例 3.5 s，跑在 `changes` 作业（`timeout-minutes: 3`），每个 PR（含 docs 范围）都跑，不落在关键路径上。✓

### 7. 互锁：`tests/ci` 与 `tests/contract` 是否同构

用 web 分支变异（`checks=skipped; coordination=success`，其余保持 `_EXPECTED["web"]`）：

- `tests/ci`：**1 失败**（`test_web_scope_expects_python_and_web_success_and_skips_the_rest`）。
- `tests/contract`（`uv run --no-project --with pytest … pytest tests/contract/test_ci_workflow.py`）：**7 失败**（`test_protected_status_never_passes_missing_evidence[success-skipped-web]`、`[skipped-success-web]`、web 的 `WEB_RESULT`/`DEPLOYMENT_RESULT`/`ACCEPTANCE_RESULT` 单点、delivery、routing）。
- **复现旧缺口**：把 `tests/ci/test_node_controller_image_ci.py` 换回 `origin/main~1` 版本、保留同一变异的 `ci.yml` → `Ran 12 tests … OK`。作者"此前 web 分支变异只有后者会红"的陈述**实测成立**，新增用例确实补上了这个缺口。✓

严格说两边不是"同构"：`tests/ci` 是"整张表 + 5 个单点错值"，`tests/contract` 是全参数化组合（`_STATES × lane`）。在当前变异集合上两边都会红，但覆盖密度不同，不宜写成"同构"。

### 8. 对自己诚实度

作者列出的"看不见"清单（`ci-validation.md:41`）实际是 **7 项 + 一句总括**（任务描述说的"8 类"在文本里找不到第 8 项）。逐项核实：**列的 7 项全部真实**（反转判定、只打日志、`psql -c` 写库、包装 `mutate`、反射、`subprocess`/`os.system`、对已登记站点的语义改动），我用 M3/C9 等实测了可验证的部分，没有发现"列了但其实能抓"的反向错误。

**但漏了 7 类**，其中两类与正文正面承诺直接冲突：

| 漏项 | 为什么重要 |
| --- | --- |
| `_ = ctx.mutate(...)` 丢弃结果 | 正文说"结果被丢弃"红（H10 绿） |
| `ctx.read((*argv,))` 星号展开 | 正文说 read argv 不可静态确定红（H1 绿） |
| 换接收者名/别名/内部包装函数 | 只写了"包进自定义函数再丢弃返回"，实际整个包装体与调用点的命令都不可见（H2/H5 绿） |
| 证据用例正文被掏空或被 skip | 总括说"登记指向一个真实证据"，没定义"真实"（R1/skip 绿） |
| 同一改动里改行数锚点 + `tables --write` | 文档只说"必须改文档"，没说文档块是一条命令的输出（R2/C1 绿） |
| `judgement`/「判定在哪」列不校验 | 这列读起来像被审计的语义字段（R3 绿） |
| `wiring` 自身的接线只由 `tests/ci` 守护 | 删掉整个 `wiring` 步骤 → `reference_acceptance` 作业全绿（剩下三条守卫照跑），只有 `changes` 里的 `tests/ci` 会红；而 `tests/ci` 可以在**同一个 PR** 里按 R1 的手法掏空 |

---

## 假红清单（正确改动会被拦）

1. **守卫步骤 `run` 第一行加 `set -euo pipefail`** → `wiring` 红，且消息错误地指向 `echo`（C4）。建议：允许在守卫命令前出现 `set`/`export`/`cd` 之类的前置语句，或把判定改成"守卫命令是该脚本里第一条被**执行**的命令"。
2. **合法新增的 `read` argv 用 f-string**（路径过滤、端口拼接）→ 红（C5）。这类改动只能改成字面量或登记构造助手；文档没给"普通 f-string 怎么办"的出路。
3. **合法新增的项目内只读助手** `ctx.read(compose_ps_argv(ctx))` → 红（C6）。必须同时改 `READ_ARGUMENTS` 与文档块（一条生成命令）。
4. **入口新增/重排步骤** → `log` 红（设计如此），一次改动要同步 entry + `EXPECTED_STEP_ORDER` + 文档 §步骤顺序 三处；没有"这次是合法重排"的通道。
5. **`probe` 的硬签名演进**（新增必填位置参数）→ 红（成本可接受，消息可用）。
6. 措辞级：`is not None` 的报错把条件写成 `is None`；`set -euo pipefail` 触发的消息说"被 `echo` 包住"。

---

## 假绿清单（破坏后仍绿）

| 编号 | 破坏 | 影响 |
| --- | --- | --- |
| B1 | 删掉整段 `if not started.ok: raise`，保留赋值 | 这是该作业存在的原始理由（旧 B1 回归） |
| H10 | `_ = ctx.mutate(verify)` | 结果丢弃被当成"有接收者" |
| H1 | `ctx.read((*argv,))`，argv 含 `sh -c "rm -rf /"` | read 边界整体失守（dry-run 会真执行） |
| H2 | `other = ctx; other.mutate((…'rm','-f','api'))` | 新写命令完全不入清单 |
| H5 | `def _apply(recv, argv): return recv.mutate(argv)` + 任意命令调用 | 同上 |
| C9 | `healthy = started.ok; print(...)` 后不中断 | 已声明看不见（诚实） |
| R2 | 删写命令 + 删登记 + 改行数锚点 + 生成 | 整表静默缩水 |
| dup | 重复站点 + 互相矛盾的 judgement + 不同证据 | 行数锚点只数长度 |
| R1 / skip | 证据用例正文掏空 / `@unittest.skip` | 证据形同虚设 |
| R3 | 新增写命令 + 证据指向无关存在的用例 | 登记=盖章 |
| C1 | 删反空转自检 + 改行数锚点 + 生成 | 反空转只剩 `main` 路径的 `require_sites=True` |
| ④' | `WRITE_TOKENS` 删 `create`（自检不覆盖） | 部分词失效而不报警 |

---

## 我独立复现的变异（全部在 `/private/tmp/sn673-mut`，未 push）

命令：每例先 `rsync -a --delete --exclude .git /private/tmp/sn673/ /private/tmp/sn673-mut/`，再改文，再跑 `sites`/`probe`/`wiring`（必要时跑 `tests/ci`）；涉及登记表的例在改完常量后跑 `tables --write` 同步文档块。

| ID | 变异 | 结果 |
| --- | --- | --- |
| M1 | `judge_result(result=started)` 关键字实参 | 绿（符合收窄后的定位） |
| M2 | `if not started:` | 红，消息含 `CommandResult（无 __bool__）` |
| M3 | `if started.ok: raise`（反转） | 绿（已声明） |
| M4 | `ctx.read(argv)`（变量 argv，`docker compose down`） | 红 + 登记处提示行 |
| M5 | `ctx.read(("uv","run","docker","compose","run","-d","api"))` | 红（`read` 携带写命令 run） |
| M9 | `if started is not None:` | 红（措辞写成 `is None`） |
| D1 | `sites` 步骤换 `echo` | 红（`wiring`） |
| D2 | 删 `bucket_names` 探针行 | 红（三重） |
| D3 | 删反空转自检 | 红（锚点+行数） |
| D4 | `WRITE_TOKENS={"run"}` | 红（自检 `read-literal-write`） |
| C1 | D3 + 行数锚点 + 生成 | **绿**（含 227 例 tests/ci） |
| C2 | 删探针行 + 实现 + 锚点 + 生成 | 红（`test_an_emptied_bucket_names_probe_is_red`） |
| C3 | 删整个 `wiring` 步骤（name + 注释 + run 块） | 四守卫绿（剩下三步照跑）；`tests/ci` **红**（`test_the_checked_in_workflow_runs_every_guard_as_the_first_command`）。第二道锁有效，但只存在于 `tests/ci` |
| C4 | 守卫步骤前加 `set -euo pipefail` | 红（假红） |
| C5 | 新 read 用 f-string | 红（假红） |
| C6 | 新 read 用未登记助手 | 红（假红） |
| C8/C8' | probe 软/硬签名演进 | 绿 / 红（消息可用） |
| H1 | `ctx.read((*argv,))` | **绿** |
| H2 | 别名接收者 + `rm -f api` | **绿** |
| H5 | 包装函数 + 两条不同命令 | **绿** |
| H10 | `_ = ctx.mutate(verify)` | **绿** |
| B1 | 删整段 `.ok` 守卫 | **绿** |
| R1 | 证据用例首行 `return` | **绿**（guard + 39 例测试 OK） |
| skip | 证据用例 `@unittest.skip` | **绿**（`OK (skipped=1)`） |
| R2 | 删写命令 + 删登记 + 锚点 + 生成 | **绿** |
| R3 | 新增写命令 + judgement「谁也没判」+ 无关证据 | **绿** |
| dup | 重复站点 + 矛盾判定 + 锚点 + 生成 | **绿** |
| ④' | `WRITE_TOKENS` 删 `create` + 锚点 + 生成 | **绿** |
| — | 伪造证据名 `test:test_that_does_not_exist` | 红（唯一"新增无证据登记"的有效拦截） |

**C3 的备注**：删掉整个 `wiring` 步骤后，`sites`/`probe`/`log` 照跑照绿——**守卫不会自己发现自己消失了**，唯一的第二道锁是 `tests/ci` 的 `test_the_checked_in_workflow_runs_every_guard_as_the_first_command`（实测红）。这条锁是有效的，但它是纯文本用例：按 R1 的手法（首行插 `return` 或 `@unittest.skip`）可以在**同一个 PR** 里把它变成 no-op，而 `sites`/`probe`/`wiring` 都不会察觉。所以 `wiring` 的自举是"守卫 + 一条可被同 PR 掏空的用例"，不是一条硬约束。

---

## 只能依赖作者陈述的点

1. **上一版的具体代码路径**：`judge(result=applied)` 死代码、`if not started:` 因无 `__bool__` 当真值等，我引用的是上一轮审查报告与提交信息，没有把 `origin/main~1` 的守卫拿来逐条重跑（只跑了 `origin/main~1` 的 `sites`/`probe` 计时用于成本对照）。
2. **"9 s 级"**：作者原话无独立来源；我用 `gh run view` 的实测（12.0 s / 前一次 9.0 s）替代。
3. **真实 apply 运行与 `tests/ci` 假 Runner 用例"负责语义"的分工**：本机没有真实 PostgreSQL/MinIO/Docker apply 环境，我只核到"这段分工写在文档里、且 `tests/ci` 确实在跑"，没有验证 apply 路径的实际覆盖。
4. **`log` 的延后项匹配（`DECLARED_DEFERRALS`）**：需要真实 dry-run 日志，我用的是 `tests/ci` 的契约样例，没有独立产生 `待输入:` 行来验证"一条延后项匹配多个登记"的歧义分支。
5. **web 真值表在 GitHub 上的实际效果**：我用本地 `bash -euo pipefail` 跑 gate 脚本同款逻辑（`tests/ci`/`tests/contract` 都是这样跑的），没有在 Actions 上替换 workflow 验证。

---

## 附录：同一 PR 里未披露的门禁松动

`ci.yml`（`Prepare the acceptance environment` 步骤）有三处与守卫无关的改动，其中两处是修假红的正向改动，一处是**收紧变松**：

- ✓ 私有目录改为从 `deploy/compose.env.example` 的 `SPEECHNEXUS_PRIVATE_CONFIG_DIR` 派生（此前硬编码 `deploy/.runtime/private`，改目录就假红）。
- ✓ 派生挂载名时跳过 `deploy/compose.yaml` 的注释行（此前注释里提到私有路径会误判成挂载）。
- ✗ **缺 committed 示例时不再失败**：此前 `raise SystemExit("; ".join(missing))`，现在合成 `{}` / 空文件并打 `::warning::`。后果：新增一个 compose 私有挂载但不交 `deploy/private/<name>.example.json` 时，作业从红变绿，而 `stack` 步的私有输入扫描面对 `{}` 只能"有文件可读"，`vad_model_file` 与 digest 检查被绕过——这恰好是 `ci.yml` 自己的覆盖段声明要覆盖的内容（"the private input scan (JSON parse, result-bucket agreement, VAD digest)"）。
- 该步骤的 inline Python 在 `tests/` 里没有覆盖（`rg "占位|SPEECHNEXUS_PRIVATE_CONFIG_DIR" tests/` 只命中无关 fixture）。

建议：要么恢复"缺示例即失败"，要么在 `ci-validation.md` 的覆盖边界里写明"缺示例时合成占位、VAD digest 不覆盖"，并让这条降级走显式登记（与本片其它"不可判定 → 登记"的处理保持一致）。
