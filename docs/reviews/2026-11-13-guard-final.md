# 验收入口结构守卫「收口」的非作者审查（#682 / #572 第七轮，切片最后一轮）

- **审查对象**：`9c3b991a`（`ci: 收口结构守卫的平台常量谓词与 `:` 前置锚 (#572) (#682)`），已合并（PR #682 的 squash 结果，`mergedAt` 2026-11-13T16:49:52Z）。基线 `origin/main` head 即此提交。
- **基线核验**：`git log --oneline -1 origin/main` → `9c3b991a ci: 收口结构守卫的平台常量谓词与 `:` 前置锚 (#572) (#682)`；`git show --stat HEAD` 与 PR #682 的 `files` 一致。主检出仍在陈旧分支 `docs/tracker-batch12`（`a7508635`，工作区有 `M uv.lock`），所有事实取自 `origin/main` 与独立 worktree。
- **独立 worktree**：`/private/tmp/sn684`，分支 `review/684-guard-final`（`git worktree add /private/tmp/sn684 -b review/684-guard-final origin/main`）。变异在同级目录 `/private/tmp/sn684-mut`（每轮前 `rsync -a --delete --exclude .git` 从 `sn684` 复原），成本隔离用 `/private/tmp/sn684-iso`，父提交对照用 `git archive 9c3b991a~1` 解到 `/private/tmp/sn684base`。**未 push 任何变异。**
- **审查者**：非作者。前六轮报告 [structure-guard](2026-11-13-structure-guard.md)（第三轮）、[guard-residuals](2026-11-13-guard-residuals.md)（第四轮）、[guard-evidence](2026-11-13-guard-evidence.md)（第五轮）、[guard-platform](2026-11-13-guard-platform.md)（第六轮）是本轮的验收基线。**本轮目标不同**：不是找「这轮漏了什么」，而是判断「能不能停」。
- **本片改动范围**：4 个文件，+435/−67。`scripts/dev-env/ci-entry-guard.py` +235/−59、`tests/ci/test_ci_entry_guard.py` +187/−0、[reference-acceptance.md](../operations/reference-acceptance.md) +10/−5、[ci-validation.md](../validation/ci-validation.md) +3/−3。**`ci.yml` 未改**，入口脚本 `reference-acceptance.py` 未改，产品代码零改动。
- **产品代码与 CI 零改动**：本报告是唯一新增文件；`uv.lock` 提交前复原。

---

## 一、结论

**有条件通过（收尾补丁级，不需要第八轮审查）。**

**这一轮四处改动都做到了它宣称的事，而且是六轮里第一次「锚比宣称更硬」。** 六条回滚必红我逐条独立复现，**失败用例数与作者陈述完全吻合**（14 / 1 / 8 / 1 / 1 / 1，见第三节第 2 条）；六张登记表、`PINNED_COUNTS`、`CI_PLATFORM_STRINGS` 与父提交**逐字段相等**，`render_tables()` 逐字节相同（4183 字符）、`tables --write` 幂等；`ci.yml` 与入口脚本一行未动；`tests/ci` 294 → 307 属实（3.12 与 3.14 各跑一遍全绿）。

**「修假红带新绿」的规律还在，但性质退化了。** 前四轮的规律是「修一处，带出一个行为级新绿」；这一轮修完的四处，我找到的新绿 **3 条实测 + 1 条理论**，其中只有 1 条属于「守卫绿、运行时真跳过」，而且要求的代码形态是给布尔开关写 PEP 695 类型别名（`type FLAG = int`）——**不是任何正常人会写的代码**。另外两条是：`runs-on` 写成块状列表时 `check_platform_runner` 静默跳过该作业；`: \` 行续接把下一行（守卫命令本身）吞进 no-op 且四守卫 + 307 例全绿。三条都不是「可信度高的日常写法」，与前六轮那些 `for`/`|=`/`setUp` 形态不在同一量级。

**15 项「看不见」清单是真的，而且没有一项写弱。** 我逐项造了变异，**15/15 全部为绿**（确实看不见）；父提交的 12 项**逐字未改**（只是重新编号），新增的 3 项（运行期 `globals()`/`exec`/下标改写、环境前置按「行号关键字 + 禁用字符」认、平台条件按写死 Linux 判定）**全部属实**。**没有发现「其实抓得住却写进清单」的项**，说明文档没有把能力往低里写。

**这一轮真正需要处理的是三处「枚举漏项/拼写口径」，不是行为级松动：**

1. **`type X = …`（PEP 695，3.12+）不在常量谓词的 12 类绑定形态里** → `FLAG = False` + `type FLAG = int` + `@skipIf(FLAG)` 实测 **`sites` 绿、运行时 `OK (skipped=1)`**。这是本轮唯一一条「真实跳过被判绿」，但要求用类型别名覆盖布尔开关，概率≈0（mypy 也会先报）。修法 2 行，或写进枚举。
2. **`check_platform_runner` 会静默跳过块状列表形式的 `runs-on`**：把任一作业改成
   `runs-on:` + 下一行 `- macos-14`（合法 YAML、GitHub 接受）→ **GREEN**，而这正是本轮第 3 项要防的「新增非 Linux runner 即红」。行内列表 `[macos-14]` 反而判红——**两种等价拼写的判定方向相反**。
3. **`: \`（行续接）被当成无害前置**：`wiring` 报 `OK (wiring: 4 guard steps)`，而真实 shell 把下一行的守卫命令并进 `:`，`sites` 守卫**根本没执行**，`tests/ci` 307 例仍全绿。机制被新增第 13 项披露（「禁用字符表以外的 shell 构造仍可能被当成前置」），但后果（守卫自身可以被静默吞掉）没有写出来。

**判断：能停。** 剩下未披露的项全部是「低概率 + 低影响」，没有一条是「日常写法就会让真实跳过被判绿」。三处收尾补丁（2 行代码 + 4 段文档，见第四节）可以在同一个收尾 PR 里做完，**不需要再开一轮返修与审查**——因为它们的失败模式都已被独立复现、修法唯一、且都能被 `tests/ci` 一条用例锚住，不存在「再修出一个新绿」的探索空间。

---

## 二、时间线

| 时点 | 动作 | 结果 |
| --- | --- | --- |
| T0 | 核 `origin/main` = `9c3b991a`；`git worktree add /private/tmp/sn684 -b review/684-guard-final origin/main` | HEAD 正确，工作区干净 |
| T1 | 读四份历史报告（282 + 441 + 342 + 395 行）与本轮全量 diff（4 文件 +435/−67） | 定位四类改动：常量绑定形态、`:` 前置字符表、平台 runner 对照、模块级证据重绑 + 缓存摘要 |
| T2 | 跑基线：`sites`/`probe`/`wiring`/`log`；`tests/ci`（3.14 与 3.12）；父提交对照 | 全绿；`sites` 0.22/0.22/0.24 s、`probe` 0.09、`wiring` 0.03、`log` 0.03；307 例 5.5 s；父提交 294 例 5.05 s |
| T3 | 常量谓词矩阵：30 种绑定形态直调 `_module_constants`/`_module_binding_forms` | 12 类全部生效；**`type X = …` 漏项** |
| T4 | 端到端：合成 `tests/ci` fixture + `audit_evidence()` + 真跑 `unittest` | `for`/`|=`/解包三条**现在红**（修复生效）；`type` 别名形态**绿 + `OK (skipped=1)`** |
| T5 | `:` 前置 32 例矩阵（`{ }`/`!`/`eval`/`command`/`builtin`/`time`/`xargs`/`. file`/`>|`/`<>`/`<<<`/`$((`/`${X:=$(…)}`/注释/引号值） | 24 例红、4 例绿；**`: \` 绿（新绿）**；注释/引号里的算符造成假红 |
| T6 | `check_platform_runner` 10 种 YAML 拼写 | 行内列表/引号/尾注释/`${{ matrix.os }}` 全红；**块状列表 `- macos-14` 绿（新绿）**；缺 `runs-on` 与 reusable job 不参与核对 |
| T7 | 独立复现六条回滚必红（(a)–(f)） | **14 / 1 / 8 / 1 / 1 / 1，与作者陈述逐条吻合** |
| T8 | 15 项「看不见」清单逐项造变异 | **15/15 全部为绿**（确实看不见） |
| T9 | 父提交清单比对 | 12 项逐字未改，+3 项属实，无一项被写弱 |
| T10 | 门禁对齐：六张表 + `PINNED_COUNTS` + `render_tables()` | 逐字段相等、4183 字符、`tables --write` 幂等 |
| T11 | 成本归因：`sites` 增长是摘要还是常量谓词 | **不是 blake2b**（≈0.7 ms），是 `_module_constants` × 7 次（≈17 ms） |
| T12 | `gh` 只读核对 job 时长与步骤 | job 13 s；父提交 3a8043ce 11 s、cb1d1960 9 s；四条守卫步各 ≤1 s |
| T13 | 3.12 与 3.14 各跑 `tests/ci` 与四守卫 | 均全绿，无宿主/版本差异 |
| T14 | 写本报告、`ruff format --check .`（1811 文件）、提交、开 PR | — |

---

## 三、六条逐条判定

### 1. 「修假红带新绿」的规律是否还在：**还在，但从「行为级新绿」退化为「枚举漏项 + 拼写变体」**

四处改动逐处找「新放行的形态」，实测结果：

| 改动 | 我找新绿的探针 | 结果 |
| --- | --- | --- |
| ① 常量谓词 12 类绑定 | `for` / `|=` / 解包 / `with … as` / `except … as` / 推导式 / 海象 / `import … as` / `def` / `class` / `global` / `nonlocal` / `del` / 三种 `match` 捕获 / `async for` / `async with` / `except*` / `(a as F, b as C)` | **12 类全部生效**（无一放行）；**PEP 695 `type X = …` 漏项**（新绿 G2） |
| ① 附带 | `from os import *`（在常量赋值之后） | 仍被判为可证明（新绿 G4，理论形态） |
| ② `:` 前置字符表 | `{ cmd; }`、`! cmd`、`eval`、`command`、`builtin`、`time`、`xargs`、`. file`、`printf` 重定向、`>|`、`<>`、`<<<`、`$((…))`、`${X:=$(…)}`、`set -- $(…)`、`export FOO=<(…)`、`cd … && …` | **全部红**（正确的方向）；**`: \` 行续接绿**（新绿 G3）；`unset -v $X` 绿（无害） |
| ③ `check_platform_runner` | 行内 `macos-14`、`${{ matrix.os }}`、reusable workflow、无 `runs-on` | 行内/表达式/reusable 均红或按披露跳过；**块状列表 `- macos-14` 绿**（新绿 G1） |
| ④ 模块级证据重绑 + 缓存摘要 | `Cls.test_x = unittest.skip(...)(Cls.test_x)`、`setattr`/`delattr`/`del`、函数体内重绑、跨模块重绑 | 前四种**红**（收紧）；跨模块重绑仍绿（未披露，但概率≈0，见第四节） |

**规律还在，但新绿的「可利用性」显著下降。** 前六轮的典型新绿是「日常重构就会踩到」的形态（类级 skip、`setUp` 里的 `skipTest`、`for` 目标标志位）；本轮的三条要求写 `type FLAG = int`、把 `runs-on` 拆成块状列表、或在守卫命令前写一行 `: \`——都不是任何人在正常改动里会写的东西。**唯一仍属「真实跳过被判绿」性质的是 G2，代价是它同时不现实。**

### 2. 新增锚是否真咬住：**咬住了，六条全部复现，且数字与作者陈述逐条吻合（这是六轮里最硬的一次）**

按 PR #682 正文的六条「回滚实验」，我在 `/private/tmp/sn684-mut` 里逐条把代码改回父提交语义后跑完整 `tests/ci`：

| ID | 回滚内容 | 作者陈述 | 我的实测 | 失败用例 |
| --- | --- | --- | --- | --- |
| (a) | `_module_constants` 换回只数 `Assign`/`AnnAssign` | 14 例红 | **14 例红** | 3 条新用例 + `test_only_a_single_module_level_assignment_is_a_provable_constant` 的 11 个 subTest |
| (b) | `GUARD_PRELUDE` 分隔符 `(\s|$)` → `\b` | 1 例红 | **1 例红** | `test_a_colon_prelude_with_a_parameter_expansion_is_green` |
| (c) | 去掉 `PRELUDE_SHELL_OPERATORS` 字符限制 | 8 例红 | **8 例红** | 5 个用例 + `test_every_writing_colon_prelude_is_red` 的 6 个 subTest |
| (d) | 去掉模块顶层重绑扫描 | 1 例红 | **1 例红** | `test_a_module_level_rebinding_of_the_evidence_is_red` |
| (e) | 缓存 key 换回 `(path, mtime_ns, size)` | 1 例红 | **1 例红** | `test_a_rewrite_with_a_restored_stamp_is_reparsed` |
| (f) | `run_wiring` 去掉 `check_platform_runner` | 1 例红 | **1 例红** | `test_a_job_on_another_runner_is_red` |

复原后 `Ran 307 tests … OK`。**六轮以来第一次「回滚 → 红」全部可复现且数字不虚报。** 上一轮点名的「`: ` 修复零锚点」这条**确实关闭了**：现在回滚分隔符会红 1 例，回滚字符表会红 8 例。

**但要区分「锚住了哪一半」**：六条锚钉的是「谓词/字符表/摘要/对照存在」，不是「覆盖完整」。G1/G2/G3 三条新绿都落在**已存在锚的谓词之外**——把 `type` 加进 `_binding_sites` 不会让任何现有用例变红，把块状列表加进 `check_platform_runner` 也不会。也就是说：**能回滚就红，但不能「不完整」就红。** 这是结构守卫的常态，不是本轮缺陷，但它决定了补丁必须在文档里写成「枚举」而不是「规则」。

### 3. 假红是否又增多：**新增 7 条（全部是假想形态），旧假红一条未消；macOS 与 3.12/3.14 无差异**

| # | 正确写法 | 结果 | 归属 |
| --- | --- | --- | --- |
| 1 | `runs-on: [ubuntu-24.04]`（行内列表，与单值等价） | **红** | 本轮新增（`check_platform_runner` 只认单行标量） |
| 2 | `runs-on: "ubuntu-24.04"`（加引号） | **红** | 本轮新增 |
| 3 | `runs-on: ubuntu-24.04 # pinned`（尾注释） | **红** | 本轮新增 |
| 4 | `runs-on: ${{ matrix.os }}`（矩阵全 ubuntu） | **红** | 本轮新增；方向安全，但矩阵一出现就红 |
| 5 | 前置行 `set -e  # always pipe > null`（注释里有 `>`） | **红** | 本轮新增：字符表对整行做子串匹配，注释/引号里的算符也算 |
| 6 | 前置行 `export FORMAT="a|b"` / `umask 022 2>/dev/null` | **红** | 同上 |
| 7 | `F: bool`（仅注解）+ 模块级 `F = False` | **红**（常量不可证明） | 本轮新增：仅注解的 `AnnAssign` 被算作一次绑定 |
| 8 | `def f(): global F`（只声明不重绑） | **红**（常量不可证明） | 同上，`global` 声明即视为第二次绑定 |

旧假红**一条未消**（第六轮已列，本轮未动）：`skipIf(sys.platform not in (…))`、`skipIf(sys.platform in (…))`、`sys.platform.lower() == …`、`platform.machine() == …`、`skipIf(0)`、类属性常量、`read` argv 用 f-string、`read` 用未登记只读助手、入口 `STEPS` 重排使 `log` 红、`probe` 硬签名演进。第 1–4 条与第 5–6 条是本轮唯一新增的假红面，**全部需要先写出一个当前仓库里不存在的拼写**才会触发。

**宿主与版本差异：无。** 本机 macOS 14（`sys.platform == "darwin"`）：

| 项 | 3.14.9 | 3.12 |
| --- | --- | --- |
| `tests/ci` | `Ran 307 tests … OK`（5.51 s） | `Ran 307 tests … OK`（5.49 s） |
| `sites` / `probe` / `wiring` | OK / OK / OK | OK / OK / OK |

没有出现「本机能过、CI 才红」或反向的项。守卫的平台判定本来就不读宿主 `sys.platform`（`grep` 全仓无运行时读取），所以本机与 CI 的**判定**一致；`tests/ci` 里唯一与文件系统相关的用例是缓存摘要那条（`os.utime` + `st_mtime_ns`），Linux ext4 与 APFS 都是纳秒精度，两边都过。

### 4. 成本：**三个数字都对得上；但 `sites` 增长的原因不是 blake2b 摘要**

| 项 | 作者陈述 | 我的实测 |
| --- | --- | --- |
| `sites` | 0.22–0.24 s | **0.22 / 0.22 / 0.24 s**（3 次）；父提交 `9c3b991a~1` **0.18 s**（3 次）→ 增长 ≈ +0.04 s |
| `probe` / `wiring` / `log` | 0.093 / 0.047 / 0.061 s | **0.09 / 0.03 / 0.03 s**（差异在解释器启动抖动内） |
| `tests/ci` | 307 例 | **307 例**（3.14 与 3.12 均全绿）；父提交 **294 例** → +13 属实 |
| job | 14 s | **13 s**（run `43466661825`，16:50:27→16:50:40）；父提交 `3a8043ce`（run `43459586733`）**11 s**，再前一版 `cb1d1960`（run `43455335297`）**9 s** |

**「上版 9 s」的对照对象要并一句上下文**：9 s 是两版之前（第六轮 squash），紧邻的上一版是 11 s。**增长不在守卫**：四条守卫步（wiring/log/sites/probe）在真实 run 里各 ≤1 s，与父提交同值；13 s 与 11 s 的差全部落在第 6 步「Execute the reference acceptance entry in dry-run mode」（1 s → 4 s），而**那个入口脚本本片一行未改**——这是 `docker compose config` 的冷热抖动，不是本片引入的。作者「14 s」与实测 13 s 属测量时点差异。

**`sites` 从 0.19 涨到 0.23 的原因，不是「blake2b 摘要 vs 时间戳」。** 我在同进程里对 `run_sites` 做三次对照（各 5 次取中位数，隔离变量）：

| 组合 | 中位耗时 | 差值 |
| --- | --- | --- |
| 现状（新常量谓词 + blake2b key） | 172.2 ms | — |
| 只把 key 换回 `(mtimen, size)` | 171.5 ms | **≈ −0.7 ms（blake2b 的代价可忽略）** |
| 再把 `_module_constants` 换回父提交版本 | 155.3 ms | **≈ −17 ms（常量谓词是主因）** |

原因是 `_module_constants` 现在对**每一行登记的证据**都做一次整模块 `ast.walk`（实测 **7 次/次 `sites` 运行**），而证据模块 `test_reference_acceptance.py` 有 1200+ 行：单次从 1.86 ms 涨到 4.44 ms。摘要本身只读 18 个模块共 294 KB（0.42 ms/次全扫）。**结论：0.04 s 的增长合理，但成因描述要改；如果要省，一次按 `id(tree)` 记忆化就能拿回 17 ms，与摘要无关。**

### 5. `ci-validation.md` 与实现逐字一致：**15 项全部真·看不见，12 项旧项逐字未改，3 项新项全部属实；两处口径需要收紧**

**项数核实**：父提交 12 项 → 当前 15 项（按 `；`/`。` 切分逐项比对）。旧 12 项**逐字未改**（只在中间插了 3 项，编号后移），**没有任何一项被改写或删除**——这是六轮里唯一一次「看不见清单只增不改」。

**逐项造变异（每项都实测为绿，即确实看不见）**：

| # | 清单项 | 我造的变异 | `sites`/`wiring` | 运行时 |
| --- | --- | --- | --- | --- |
| 1 | 判定条件写反仍读 `.ok` | `if started.ok:`（入口 `step_migrate`） | **绿** | — |
| 2 | 读 `.ok` 只打日志 | `print(started.ok)` + `if False:` | **绿** | — |
| 3 | `ctx.read` 里 `psql -c "DELETE …"` | 加一行 `ctx.read(("psql","-c","DELETE FROM bucket"))` | **绿** | — |
| 4 | 换接收者名/别名/包函数 | `_other = ctx; _other.mutate(("docker","compose","rm","-f","api","gateway"))` | **绿** | — |
| 5 | `getattr`/反射 | `getattr(ctx,"mutate")((…"rm","-f","api","gateway"))` | **绿** | — |
| 6 | `bit=` 以外执行原语 | `subprocess.run(["docker","compose","rm","-f","api","gateway"])` | **绿** | — |
| 7 | 判定语义作任何改动 | `if not started.ok and False:`（判定永不触发） | **绿** | — |
| 8 | 证据与站点不相关 | 指向同模块另一个「存在、有断言、无 skip、会执行」的用例 | **绿** | 执行 |
| 9 | `skipTest` 藏在 `setUp` 调用的模块级助手里 | `def setUp(self): _require_env(self)` + 模块级助手 `case.skipTest(...)` | **绿** | `OK (skipped=1)` |
| 10 | 运行期 `globals()`/`exec`/下标改写 | `FLAG = False` + `globals()["FLAG"] = True` / `exec("FLAG = True")` + `@skipIf(FLAG)` | **绿** | `OK (skipped=1)` |
| 11 | `return` 藏非字面量条件 / 恒真断言不在单子里 | `if sys.platform != "win32": return` 后的断言（CI 上是死代码）；`self.assertIn('a','abc')` | **绿** | 死代码 / 恒真 |
| 12 | `*` 展开与构造助手按名字认 | 函数内 `compose_argv = ("sh","-c","rm -rf /")` + `ctx.read((*compose_argv,))` | **绿** | — |
| 13 | 环境前置按「行号关键字 + 禁用字符」认 | 守卫命令前插一行 `: \` | **绿**（`wiring: OK`） | 守卫**未执行** |
| 14 | 平台条件按写死 Linux、只核对 `runs-on` 字面量 | 作业改成块状列表 `runs-on:` + `- macos-14` | **绿** | 该作业实际在 macOS 上 |
| 15 | 「判定在哪」只校验名字存在 | 判定列写 `` `Context` `` + `tables --write` | **绿** | — |

**两处口径需要收紧（都是「写强了」而不是「写弱了」）：**

1. **第 14 项**说「只核对 `ci.yml` 里每个作业的 `runs-on` 字面量等于 `CI_RUNNER_IMAGE`」。实际是「**只核对写成单行标量的 `runs-on`**」——块状列表形式的作业**不进核对集合**（G1）。同一句里「每个作业」这四个字对块状列表形态不成立。可用的更准确写法：加一句「`runs-on` 写成块状列表（`runs-on:` 换行 `- 镜像`）的作业不参与核对」。
2. **`type` 别名不在 `sites` 段的枚举里**（[ci-validation.md:33](../validation/ci-validation.md#L33) 明列 12 类绑定形态并以「都是第二次绑定，出现即不可证明、判红」收尾，读起来是完备枚举）。PEP 695 的 `type X = …` 同样是一次静态绑定。要么补进代码（2 行），要么补进枚举。

**没有发现「其实抓得住却写进清单」的项**——15 项全部为绿，说明文档没有低报能力。这一点比前六轮都干净。

### 6. 能不能停：**能停（条件是一次收尾补丁，不是一轮返修）**

判断依据严格按你给的规则过一遍：

| 判据 | 结论 |
| --- | --- |
| 剩余问题里有没有「真实跳过被判绿」？ | **有一条（G2：`type` 别名）**，但要求的代码形态是给布尔开关写类型别名——不是任何正常改动会写出的东西，mypy 也会先报。**它不构成「日常写法就会漏」的风险。** |
| 有没有「真实跳过被判绿」的**可信形态**？ | **没有**。12 类绑定形态覆盖了全部现实写法（`for`/`|=`/解包/`with as`/`except as`/推导式/海象/`import`/`def`/`class`/`global`/`del`/`match`），我逐条试过，全红。 |
| 剩余项是否都属于已披露边界？ | 大部分是（15 项清单 + 3 项新增）。未披露的 4 条：`type` 别名、块状列表 `runs-on`、跨模块证据重绑、`: \` 的后果——**全部低概率、低影响**。 |
| 继续修的边际收益 vs 引入新绿的风险 | **边际收益已低于风险。** 三处补丁都是「加一个分支/一行枚举」，失败模式唯一且能锚住；但如果按「再开一轮返修」的方式做，就还要再走一遍「修一处松一处」的轮次，而这轮的实测已经证明**每次新增出口都会带来新的拼写变体**（`runs-on` 就是活例子：行内列表真红、块状列表假绿）。 |
| 继续修会不会把已绿的锚弄红？ | 不会：六条回滚锚都能在补丁后保留（补 `type` 加一条用例；补块状列表把现有 `macos-14` 用例延伸成两种拼写）。 |

**所以：能停。** 条件是第四节那三处收尾补丁，在**同一个**收尾 PR 里完成，**不再启动第八轮「返修 + 独立审查」流程**。理由一句话：剩下的每一条都已经有确定的修法、确定的锚、确定的影响面；它们属于「把枚举补全」，不属于「再发现一个未知出口」。

---

## 四、必须修清单（按严重度）

### A. 必须修（3 项，合计约 10 行代码 + 1 条用例，可在收尾 PR 内完成）

1. **（低-中）`check_platform_runner` 对块状列表 `runs-on` 静默跳过**（[ci-entry-guard.py:1858](../../scripts/dev-env/ci-entry-guard.py#L1858)）。现在 `_RUNS_ON = ^    runs-on: (.+)$` 只匹配单行标量；把任一作业写成
   ```yaml
       runs-on:
         - macos-14
   ```
   时该作业**不进 `runners`**，其余作业正常 → 整体 GREEN。这是本轮第 3 项宣称要防的形态，且与行内列表 `[macos-14]` 的判定方向相反。修法：`runs-on:` 后为空时，把紧随的 `      - <值>` 行也收进候选值（并对 `[a, b]`、引号、尾注释做归一）。锚：把 `test_a_job_on_another_runner_is_red` 延伸成两种拼写。

2. **（低）PEP 695 `type X = …` 不在常量绑定形态里**（[ci-entry-guard.py:1127](../../scripts/dev-env/ci-entry-guard.py#L1127)）。实测 `FLAG = False` + `type FLAG = int` + `@skipIf(FLAG)` → `sites` 绿、运行时 `OK (skipped=1)`。修法：`_binding_sites` 加 `ast.TypeAlias` 分支（`("type-alias", node.name.id)`），并在 `test_only_a_single_module_level_assignment_is_a_provable_constant` 的 `bindings` 里加 `"type F = int"`。**这一条是「真实跳过被判绿」，即便概率≈0，也按必须修处理**——修它是 2 行，改文档也是 2 行，但代码修法不会留下解释成本。

3. **（低）`: \` 行续接让守卫命令本身被吞掉，且四守卫 + 307 例全绿**（[ci-entry-guard.py:1842](../../scripts/dev-env/ci-entry-guard.py#L1842)）。修法二选一：把 `\` 加进 `PRELUDE_SHELL_OPERATORS`；或在 `run_wiring` 里对「前置行以 `\` 结尾」直接判红（因为那意味着下一行不是独立命令）。前者一行，建议前者。锚：`test_a_command_substitution_in_another_prelude_is_red` 同类加一例。

### B. 写进边界即可（4 项，用户已明确这类算「可停」）

4. **`runs-on` 的其它拼写假红**（行内列表、引号、尾注释、`${{ matrix.os }}`）：不要逐个兼容，直接在 `ci-validation.md:35` 的 `wiring` 段写「只认单行无引号无注释的标量形式」。（若做了 A1 的归一化，这一条自动消掉。）
5. **`: ` 仍被称作「环境前置」**：[ci-validation.md:35](../validation/ci-validation.md#L35)、[reference-acceptance.md:123](../operations/reference-acceptance.md#L123) 与守卫错误消息仍把 `:` 列在「这类环境前置」里，下一句才澄清它是 no-op 内建。PR 正文声称「文档不再称 `:` 为『环境前置』」——**与文档实际内容不符**（澄清加了，但没有改称谓）。属无害，但收尾时一起改成「`set`/`export`/… 这类环境前置，外加 `:` 这个可带重定向的 no-op」更清楚。
6. **前缀行字符表对整行做子串匹配**：注释或引号里的 `>`/`|` 也会判红（`set -e  # always pipe > null`、`export FMT="a|b"`）。一行文档即可（「字符表是对整行的子串匹配，注释与引号里的算符同样计」）。
7. **跨模块证据重绑仍绿**：模块 A 里 `Cls.test_x = …`、证据在模块 B 时不检查。与已披露的「证据相关性不可见」同族，补一句到「看不见」清单即可。

> 另有两项**不属于本片、但建议顺手记一笔**：`_module_constants` 被每行证据各调一次（7 次/`sites`），按 `id(tree)` 记忆化能省 17 ms；`CI_RUNNER_IMAGE` 换成 `ubuntu-24.04-arm` 这类两段名时 `_RUNNER_IMAGE_PLATFORM[...]` 会在 import 期 `KeyError`（硬失败，可接受，但错误信息不友好）。

---

## 五、这轮找到的新绿（逐条）

| ID | 形态 | `sites`/`wiring` | 运行时 | 性质 | 已披露？ |
| --- | --- | --- | --- | --- | --- |
| **G1** | 作业 `runs-on:` + 换行 `- macos-14`（块状列表） | **GREEN** | 该作业实际在 macOS 上跑 | 本轮第 3 项要防的形态，静默放过 | **未披露**（第 14 项说「每个作业」，但只覆盖单行标量） |
| **G2** | `FLAG = False` + `type FLAG = int` + `@skipIf(FLAG)` | **GREEN** | `OK (skipped=1)` | 真实跳过被判绿（同一类缺陷的新成员） | **未披露**（12 类枚举漏了 PEP 695） |
| **G3** | 守卫命令前插一行 `: \` | **GREEN**（`wiring: 4 guard steps`） | 守卫**根本没执行**；`tests/ci` 307 例仍 OK | 守卫自身被静默吞掉 | 机制已披露（第 13 项），**后果未披露** |
| **G4** | `FLAG = False` 之后 `from os import *` | GREEN（判为可证明） | 只有在被导入模块导出同名 `FLAG` 时才会真跳过 | 理论形态 | **未披露**，建议只在文档写一句 |

**对照组（正确形态仍然绿）**：`FLAG = False` + `@skipIf(FLAG)` → 绿且真运行 ✓；`SKIP_ON_WINDOWS = False` + `@skipIf(SKIP_ON_WINDOWS)` → 绿 ✓；`: "${VAR:?missing}"` → 绿 ✓；`set -euo pipefail` / `umask 022` / `unset FOO` / `cd /tmp` → 绿 ✓。

---

## 六、假红清单（正确改动会被拦）

1. `runs-on: [ubuntu-24.04]`（行内列表，与单值等价）→ `wiring` 红，消息称「`runs-on` 是 `[ubuntu-24.04]`」。**本轮新增。**
2. `runs-on: "ubuntu-24.04"` → 红。**本轮新增。**
3. `runs-on: ubuntu-24.04 # pinned` → 红。**本轮新增。**
4. `runs-on: ${{ matrix.os }}`（矩阵全 ubuntu）→ 红。**本轮新增**；方向安全，但一旦引入矩阵就必红。
5. 前置行 `set -e  # always pipe > null` / `export FMT="a|b"` / `umask 022 2>/dev/null` → 红（字符表对整行做子串匹配）。**本轮新增。**
6. `F: bool`（仅注解）+ `F = False` → 常量不可证明 → 红。**本轮新增**（仅注解的 `AnnAssign` 被记作一次绑定）。
7. `def f(): global F`（只声明、不重绑）→ 常量不可证明 → 红。**本轮新增。**
8. `@unittest.skipIf(sys.platform not in ("win32","cygwin"), …)` → 红。**旧假红，未消，实用性最高**（「非 Windows」最自然写法）。
9. `@unittest.skipIf(sys.platform in ("win32",), …)` / `sys.platform > "a"` → 红。旧。
10. `@unittest.skipIf(sys.platform.lower() == "win32", …)` / `platform.machine() == "x86_64"` → 红。旧（只认三种探针 + 四种运算）。
11. 类属性常量 `class T: SKIP = sys.platform == "win32"` + `@skipIf(SKIP)` → 红。旧（只认模块级常量）。
12. `@unittest.skipIf(0, …)` → 红。旧（只认 `True`/`False` 字面量）。
13. `read` argv 用 f-string / `read` 用未登记只读助手 → 红。旧。
14. 入口 `STEPS` 重排 → `log` 红（`sites`/`probe`/`wiring` 绿）。旧，设计如此。
15. `probe` 硬签名演进（新增必填位置参数）→ `probe` 红。旧，消息可执行。

**已消的旧假红**：`: "${VAR:?}"`（第五轮 W3）、`skipIf(sys.platform == "win32")` 系四条（第六轮）、类体 `skip()` 重绑（第六轮）、`set -euo pipefail`（第五轮）、`@unittest.skipIf(False)`、业务对象 `.skip()`。
**本轮无一条旧假红被消除**；新增的 7 条全部需要先写出一个当前仓库里不存在的拼写。

---

## 七、我独立复现的变异

全部在 `/private/tmp/sn684-mut`（同级目录，非 worktree 内），每轮前 `rsync -a --delete --exclude .git` 从 `/private/tmp/sn684` 复原；涉及登记表的例在改常量后跑 `tables --write`。**未 push 任何变异。** 探针脚本：`probe_const.py`、`probe_const2.py`（直调 `_module_constants`/`_module_binding_forms`）、`probe_e2e.py`（合成 fixture + `audit_evidence()` + 真跑 `unittest`）、`probe_prelude.py`（32 例前置矩阵）、`probe_runner.py`（10 种 `runs-on` 拼写）、`probe_entry.py` / `probe_entry2.py`（入口改写 + 登记表改写）、`probe_evidence.py`（证据级 8 例）、`probe_rollback.py`（六条回滚锚）、`probe_falsered.py`（条件装饰器假红）。

### 7.1 常量绑定形态（第三节第 1 条）

| ID | 形态 | 期望 | 实测 |
| --- | --- | --- | --- |
| C1 | `for F in (True,): pass` + `@skipIf(F)` | 红 | **红**（端到端，运行时 `OK (skipped=1)`）✓ |
| C2 | `F \|= True` | 红 | **红** ✓ |
| C3 | `F, _ = True, True` | 红 | **红** ✓ |
| C4 | `with … as F` / `async with … as F` / `(a as F, b as C)` | 红 | **红** ✓ |
| C5 | `except … as F` / `except* … as F` | 红 | **红** ✓ |
| C6 | 推导式目标 / `(F := True)` | 红 | **红** ✓ |
| C7 | `import os as F` / `def F` / `class F` | 红 | **红** ✓ |
| C8 | `global F` / `nonlocal F` / `del F` / `del F` 后重绑 | 红 | **红** ✓ |
| C9 | `match` 捕获（`case F` / `case [*F]` / `case {**F}` / `case [F]` / `case int(F)`） | 红 | **红** ✓ |
| C10 | `async for F in x` | 红 | **红** ✓ |
| C11 | **`type F = int`（PEP 695，3.12+）** | 红 | **绿（G2）** ✗ |
| C12 | `from os import *`（赋值之后） | 红 | **绿（G4，理论）** ✗ |
| C13 | 对照：`F = False` 单次绑定 / `F: bool = False` | 绿 | **绿** ✓ |

### 7.2 `:` 前置矩阵（32 例，只列关键）

| ID | 前置行 | 期望 | 实测 |
| --- | --- | --- | --- |
| P1 | `set -euo pipefail` / `umask 022` / `unset FOO` / `cd /tmp` / `: "${VAR:?missing}"` / `:` / `: 'quote'` | 绿 | **绿** ✓ |
| P2 | `: > file` / `: >> file` / `: ; rm -rf …` / `: && curl … \| sh` / `: $(rm …)` / `` : `rm …` `` | 红 | **红** ✓（第六轮 5 条已关） |
| P3 | `export FOO=$(…)` / `export FOO=<(…)` / `cd /tmp && rm …` | 红 | **红** ✓（第六轮「本轮之前就绿」已关） |
| P4 | `{ rm -rf x; }` / `! false` / `eval …` / `command …` / `builtin …` / `time …` / `xargs …` / `. /tmp/x.sh` / `printf x > f` / `: >\|` / `: <>` / `: <<<` / `set -- $(…)` / `${X:=$(…)}` | 红 | **红** ✓（字符表覆盖了全部这些写法） |
| P5 | **`: \`（行续接）** | 红 | **绿（G3）** ✗ |
| P6 | `unset -v $X` | — | 绿（无害）✓ |
| P7 | `set -e  # always pipe > null` / `umask 022 2>/dev/null` / `export FOO=$((1))` | 绿 | **红（假红）** |

### 7.3 `check_platform_runner`（10 种拼写）

| ID | 写法 | 期望 | 实测 |
| --- | --- | --- | --- |
| R1 | 现状 12 个作业全 `ubuntu-24.04` | 绿 | **绿** ✓ |
| R2 | 行内 `runs-on: macos-14` | 红 | **红** ✓ |
| R3 | 行内列表 `[macos-14]` / `[ubuntu-24.04]` | 红 / 绿 | **红 / 红（假红）** |
| R4 | **块状列表 `runs-on:` + `- macos-14`** | 红 | **绿（G1）** ✗ |
| R5 | `"ubuntu-24.04"` / `ubuntu-24.04 # pinned` / `${{ matrix.os }}` | 绿 / 绿 / — | **红 / 红 / 红（假红）** |
| R6 | 某个作业完全没有 `runs-on` | — | 该作业不参与核对（GREEN） |
| R7 | 新增 reusable workflow 调用作业（`uses:`） | — | GREEN（第 14 项已披露；真实仓库 `ci.yml:717` 就是这种，被调用 workflow 里确有 `ubuntu-24.04-arm`） |

### 7.4 六条回滚锚

见第三节第 2 条表格（14 / 1 / 8 / 1 / 1 / 1，复原后 307 例 OK）。

### 7.5 15 项「看不见」清单

见第三节第 5 条表格（15/15 全部为绿）。

### 7.6 其它

| ID | 探针 | 结果 |
| --- | --- | --- |
| M1 | 六张表 + `PINNED_COUNTS` + `CI_PLATFORM_STRINGS` 与父提交逐字段比对 | **全部相等** |
| M2 | `render_tables()` 与父提交比对 / `tables --write` 幂等 | 4183 字符逐字节相同 / `git status` 干净 |
| M3 | `tests/ci` 3.12 与 3.14 | 307 例全绿（5.49 s / 5.51 s） |
| M4 | 父提交 `tests/ci` | 294 例（+13 属实） |
| M5 | `sites` 成本归因（同进程三次对照） | blake2b ≈0.7 ms、常量谓词 ≈17 ms、`_module_constants` 7 次/运行 |
| M6 | 真实 run `43466661825` 步骤时长 | 守卫步各 ≤1 s；+4 s 落在未改动的入口 dry-run 步 |
| M7 | `ruff format --check .` | 1811 files already formatted |

---

## 八、只能依赖作者陈述的点

1. **真实 apply 运行**：本机没有真实的 PostgreSQL/MinIO/Docker apply 环境，「语义由 `tests/ci` 的假 Runner 用例与真实 apply 运行负责」这条分工，我只核到「写在文档里、`tests/ci` 确实在跑」，没有验证 apply 路径的实际覆盖。
2. **GitHub Actions 上 `setup-uv` / `docker compose config` 的行为**：我只读了真实 run 的结果与耗时，没有在 Actions 上替换 workflow 复现变异；全部变异在本机 Python 3.14.9 / 3.12 + 文件副本上完成。
3. **`log` 子命令对真实 dry-run 日志的判定**：需要 `--log` 的真实日志；我核到的是「`log` 输入未被本片改动影响」以及「入口 `STEPS` 重排会让 `log` 红」。
4. **作者的「上版 9 s」**：我测到的是两版之前的 `cb1d1960` 为 9 s、紧邻上一版 `3a8043ce` 为 11 s；9 s 与 11 s 都是真的，差别是排队/缓存抖动。
5. **`sites` 增长的归因**：作者未在 PR 正文给出归因，「blake2b vs 时间戳」是审查任务里的假设；我实测否定它，但**没有**作者的原始测量记录可对照。
6. **未来加 macOS runner 的后果**：我没有真的加一个 macOS 作业验证平台条件会错；G1/第 14 项的三条判断是读代码 + 本机 `sys.platform == "darwin"` 运行时行为推出来的。

---

## 九、一句话给非作者看

**这一轮把上一轮点名的六条全部修实了，锚也第一次比宣称更硬（六条回滚必红逐条复现，数字全对），15 项「看不见」清单逐项都是真的、旧 12 项一字未改。** 剩下的三条新绿（`type` 别名、块状列表 `runs-on`、`: \` 行续接）都是「拼写变体/枚举漏项」，不是「日常写法就会漏」的行为级缺陷；其中只有 `type` 别名算「真实跳过被判绿」，但它要求给布尔开关写类型别名，概率≈0。**结论：有条件通过——补上 `type` 别名（2 行）、块状列表 `runs-on`（约 6 行）、`: \`（1 行）三处，外加四条边界说明，在同一个收尾 PR 内完成即可，不需要再开一轮返修审查。这个切片可以停了。**
