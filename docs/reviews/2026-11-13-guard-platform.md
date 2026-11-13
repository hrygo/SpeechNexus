# 验收入口结构守卫「平台条件求值」的非作者审查（#680 / #572 第六轮）

- **审查对象**：`cb1d1960`（`fix: 收口结构守卫的 wiring 前置、平台守卫出口与证据唯一定位 (#572) (#680)`），已合并，基线 `origin/main` head 即此提交。
- **基线核验**：建 worktree 前主检出的 `origin/main` 停在 `7acca3b1`；`git fetch origin` 后 `origin/main` 前进到 `cb1d1960`（`git rev-parse origin/main` → `cb1d196013137546724a3fa994a4ffebbcb542d2`），与本轮基线一致。主检出仍在陈旧分支 `docs/tracker-batch12`（`a7508635`，工作区有 `M uv.lock`），所有事实取自 `origin/main` 与独立 worktree。
- **独立 worktree**：`/private/tmp/sn682`，分支 `review/682-guard-platform`（`git worktree add /private/tmp/sn682 -b review/682-guard-platform origin/main`）。变异实验在同级目录 `/private/tmp/sn682-mut`（每例前 `rsync -a --delete --exclude .git` 从 `sn682` 复原），另用 `git archive cb1d1960~1` 解到 `/private/tmp/sn682base` 作成本与用例数对照。**未 push 任何变异。**
- **审查者**：非作者。前三轮报告 [structure-guard](2026-11-13-structure-guard.md)（第三轮）、[guard-residuals](2026-11-13-guard-residuals.md)（第四轮）、[guard-evidence](2026-11-13-guard-evidence.md)（第五轮）是本轮的验收基线，重点核对「每轮修了什么、每次留下什么」。
- **本片改动范围**：4 个文件，+435/−61（`git show --numstat`）。`scripts/dev-env/ci-entry-guard.py` +301/−48、`tests/ci/test_ci_entry_guard.py` +116/−1、[reference-acceptance.md](../operations/reference-acceptance.md) +15/−9、[ci-validation.md](../validation/ci-validation.md) +3/−3。**`ci.yml` 未改**（`git show --stat` 不含该文件），产品代码零改动。
- **产品代码与 CI 零改动**：本报告是唯一新增文件；`uv.lock` 提交前复原。

---

## 一、结论

**有条件通过。**

**第五轮点名的三条必改，三条都做了，而且两条做对了。** `:` 前置的假红确实修掉（`GUARD_PRELUDE` 的 `\b` → `(\s|$)`，[ci-entry-guard.py:1713](../../scripts/dev-env/ci-entry-guard.py#L1713)）；`_find_tests` 返回全部命中后，第五轮的「同名诱饵放在更早位置」残余（X1/D-a）**实测关闭**；类体 `test_x = unittest.skip(...)(test_x)` 判红；`ci-validation.md:41` 的强承诺确实收窄，且「看不见」清单从 11 项变 12 项——**第 8 项改措辞、第 9 项全新，两处都属实**（我逐字比对了父提交的同一行）。

**但这一轮第一次给「跳过」开了绿灯，而这个绿灯的证明谓词是不完整的。** 新增的平台求值器要证明「这个用例在 Linux CI 上会运行」，它依赖一个「模块顶层只赋值一次的常量」谓词（[ci-entry-guard.py:1103](../../scripts/dev-env/ci-entry-guard.py#L1103)）。该谓词只数 `Assign`/`AnnAssign`，**不数 `for` 目标、`|=` 这类 AugAssign、以及元组/列表解包**。我据此造出的 4 条变异**全部是「运行时真跳过、守卫判绿」**，其中一条是 `setUp` 里的 `if AUG_FLAG: self.skipTest(...)`（守卫绿，`unittest` 输出 `OK (skipped=1)`）。这是六轮以来第一次出现「新增的绿表示一个真实的跳过」，性质比前几轮的叙事松动更硬。

**并且两条「修假红」的新代码各带一个新口子，且都没有被测试锚住：**

1. **`:` 前缀放行真实写操作**：`: > file`（创建/截断）、`: >> file`、`: ; rm -rf …`、`: && curl … | sh`、`: $(rm …)` 全部绿。`: > file` 是真写，被当成「环境前置」放行——审前重点怀疑项，**成立**。同时：把 `GUARD_PRELUDE` 还原成 `\b`（即回滚本轮唯一的 bug 修复）后，`tests/ci` **294 例全绿、`wiring` 也绿**——这条修复没有任何测试锚。
2. **`CI_PLATFORM = "linux"` 与 runner 无耦合**：今天 12 个 job 全是 `ubuntu-24.04`（常量正确），但没有任何用例或代码把两者绑起来；`reference_acceptance` 与跑 `tests/ci` 的 `changes` 作业的 runner 都没被钉住。若以后加 macOS job，`skipIf(sys.platform != "linux")`、`skipUnless(sys.platform == "linux")`、`skipIf(sys.platform == "darwin")` 三种自然写法会在新平台上真跳过而守卫判绿。

**没有发现登记表被放宽。** `PINNED_COUNTS` 六个值、`SELFTEST_CASES` 20 条、`MUTATE_SITES` 8 行、`READ_ARGUMENTS` 4 行、`WRITE_TOKENS` 7 词、`PROBE_CASES` 9 例全部未动；[reference-acceptance.md](../operations/reference-acceptance.md) 的锚点块**逐字节相同**（4185 字符，60 行）；`tests/ci` 的 diff 只删了一行（fixture 里的 `FLAG = False`，改成 `FLAG = len(["a"]) > 1`，是新求值器下保持该用例有效的必要适配，不是弱化断言）。

**必须处理的三项**（都不动架构，一见第四节）：补「常量只绑定一次」的完整绑定形态；给 `:` 前置补测试并把写操作挡掉；把 `CI_PLATFORM` 与 `ci.yml` 的 `runs-on` 绑起来或写进覆盖边界。

---

## 二、时间线

| 时点 | 动作 | 结果 |
| --- | --- | --- |
| T0 | `git fetch origin --prune` | `origin/main` 从 `7acca3b1` 前进到 `cb1d1960`；`git cat-file` 确认是 commit |
| T1 | `git worktree add /private/tmp/sn682 -b review/682-guard-platform origin/main` | HEAD = `cb1d1960`，工作区干净 |
| T2 | 读第三、四、五轮报告（282 + 441 + 342 行）与本轮全量 diff（4 文件 +435/−61） | 定位四类改动：`:` 正则、平台求值器、`_find_tests` 全命中 + 类体重绑、记忆化 |
| T3 | 跑基线：`sites`/`probe`/`wiring`/`log`；`tests/ci`；`git archive cb1d1960~1` 对照成本与用例数 | 全绿；0.19/0.11/0.03/0.03 s；294 例 5.16 s；父提交 `sites` 0.44 s、287 例 |
| T4 | 求值器矩阵：13 个条件 + 32 个装饰器形态直调 `_ci_condition_value`/`_skip_decorator_reason` | 五条「Linux 上真跳过」全红；`and`/`or` 短路语义正确；发现常量谓词漏计 |
| T5 | 端到端：合成 `tests/ci` fixture + `audit_evidence()` + 真跑 `unittest` | 常量三形态真跳过全绿；`setUp` 形态真跳过绿 |
| T6 | `_find_tests`：8 条真实登记解析；同名/限定/错模块/错类/错方法/畸形定位串 | 8 条全唯一定位；同名红；限定绿；四种写错全红 |
| T7 | 证据重绑一行位移：类体（红）vs 模块级属性赋值（绿 + 真跳过） | 残余确认 |
| T8 | `wiring` 前置 16 例 | `: "${VAR:?}"` 绿（假红已修）；`::`/`:: foo`/`:ok` 红；`: > file` 与 4 种 `:` 复合形态**绿** |
| T9 | `ci-validation.md:41` 12 项逐项造变异 | 12 项全部成立（绿）；第 8 项措辞与第 9 项新增核实为真 |
| T10 | 记忆化：同秒改写 vs 同尺寸+保留 mtime；缓存无淘汰 | 同秒改写重解析 ✓；保留 mtime 读到陈旧 AST ✗ |
| T11 | 锚点探针：回滚 `:` 修复 / 关掉平台识别 / 放松「赋值一次」谓词 | 全绿 / 6 例红 / 1 例红 |
| T12 | `gh` 只读核对 job 时长与真实 run；`ruff format` 校验 | 见第九节 |
| T13 | 写本报告、`ruff format`、提交、开 PR | — |

---

## 三、九条逐条判定

### 1. 求值器把 `sys.platform` 硬算成 `"linux"`：**方向正确，但与 runner 没有耦合（必改 4）**

**(a) CI runner 是不是真的 linux：是。** `.github/workflows/ci.yml` 的 12 个 `runs-on` **全部**是 `ubuntu-24.04`（`grep -n runs-on` → 12 命中，全部同值）；`reference_acceptance` 在 `ci.yml:941`，跑 `tests/ci` 的 `changes` 作业在 `ci.yml:23`，都是同一 runner。`CI_PLATFORM_STRINGS` 三个值与 ubuntu 的真实值一致：`sys.platform == "linux"`、`platform.system() == "Linux"`、`os.name == "posix"`。

**(b) 本机 macOS 上跑守卫，结论是否与 CI 一致：一致，而且是构造上的一致。** 守卫**不读宿主的 `sys.platform`**（`grep sys\.platform` 只命中 `CI_PLATFORM_STRINGS` 的字符串表与两处消息文案，没有任何运行时读取），所以判定与宿主无关。实测对照：

| 形状 | 本机 macOS 运行时 | 守卫判定 |
| --- | --- | --- |
| `skipIf(sys.platform == "linux")` | 不跳过（darwin） | **红**（按 Linux 判会跳过）✓ |
| `skipIf(not sys.platform == "win32")` | 跳过 | **红** ✓ |
| `skipIf(sys.platform.startswith("lin"))` | 不跳过 | **红** ✓ |
| `skipIf("linux" == sys.platform)` | 不跳过 | **红** ✓ |
| `skipUnless(sys.platform != "linux")` | 不跳过 | **红** ✓ |

同一个 fixture 在本机跑 `unittest -v` 是 `Ran 5 tests … OK (skipped=1)`，而守卫把五条全判红——这正是设计意图：**判的是 CI 平台，不是本机**。这条取舍本身是对的，第五轮的四条假红（P3–P6）就是它修掉的。

**(c) 如果 CI 将来加 macOS job，这个常量会不会让守卫静默放过一个真实跳过：会。** 三种自然写法在 Linux 上判定为「会运行」（绿），在 macOS 上却真跳过：

| 形状 | Linux 上 | macOS 上 | 守卫 |
| --- | --- | --- | --- |
| `skipIf(sys.platform != "linux")` | False → 运行 | True → 跳过 | **绿（错）** |
| `skipUnless(sys.platform == "linux")` | True → 运行 | False → 跳过 | **绿（错）** |
| `skipIf(sys.platform == "darwin")` | False → 运行 | True → 跳过 | **绿（错）** |

**耦合强度为零**：`CI_PLATFORM` 只出现在守卫自己（[ci-entry-guard.py:1022](../../scripts/dev-env/ci-entry-guard.py#L1022)），`tests/ci` 没有任何用例把它与 `ci.yml` 的 runner 对照；`tests/ci/test_ci_inspection_retirement.py:49` 钉了 `python_checks` 一个 job 的 `runs-on: ubuntu-24.04`，但**没有**覆盖 `reference_acceptance` 与 `changes`。也就是说把 `ci.yml:941` 改成 `macos-14` 之后，`tests/ci` 294 例、四条守卫**全绿**，而守卫继续假设 linux。这是「假设被写死」的典型形态：**判定用的是一个常量，而不是一个被校验过的事实**。

### 2. 求值器会不会反而放过真跳过：**五条全部判红，这一层通过；但同一条路径上的常量分支会放过（见第 3 条）**

逐个实测（合成 `tests/ci` fixture + `audit_evidence()`，`probe:key` 单行登记）：

| 变异 | 期望 | 实测 | 守卫消息（首行截断） |
| --- | --- | --- | --- |
| `skipIf(sys.platform == "linux")` | 红 | **红** | `…（在 CI 平台（linux）上条件为 True，该用例不会运行）` |
| `skipIf(not sys.platform == "win32")` | 红 | **红** | 同上 |
| `skipIf(sys.platform.startswith("lin"))` | 红 | **红** | 同上 |
| `skipIf("linux" == sys.platform)` | 红 | **红** | 同上（反向比较分支也走通） |
| `skipUnless(sys.platform != "linux")` | 红 | **红** | `…条件为 False，该用例不会运行` |
| `skipIf(platform.system() == "Linux")` / `skipIf(os.name == "posix")` | 红 | **红** | 同上 |
| `skipUnless(sys.platform == "linux")` | 绿 | 绿 ✓ | — |
| `skipIf(sys.platform == "win32")` / `skipUnless(sys.platform != "win32")` | 绿 | 绿 ✓ | 第五轮假红已消 |
| `skipIf(not sys.platform.startswith("win"))` | 红 | **红** | ✓ |
| `skipIf(sys.platform == "win32" and unknown)` | 绿（短路为 False） | 绿 ✓ | 布尔代数正确 |
| `skipIf(sys.platform == "win32" or True)` | 红 | **红** | ✓ |
| `skipIf(sys.platform in {"linux"})` / `sys.platform > "a"` | 无法判定 | **红** | 保持旧语义 |

`and`/`or` 的三值逻辑我逐分支验过，与 Python 短路语义一致（`False and unknown → False`、`unknown and False → False`、`unknown or True → True`、`True and unknown → 未知`），**没有发现「算出 True 其实不会跳过」或反向的错误**。

### 3. 常量绑定的可证明性：**部分成立 —— 三种赋值形态漏计，造出 4 条真跳过全绿（必改 1）**

作者的规则是「只在模块顶层赋值一次」（[ci-entry-guard.py:1103](../../scripts/dev-env/ci-entry-guard.py#L1103) 的 docstring 写得更明确：*A name assigned anywhere else (a `global` rebind in `setUpModule`, a local with the same name) is left out*）。实测：

| 形态 | 期望 | 实测 |
| --- | --- | --- |
| `global` 重绑（`global F; F = True`） | 拒绝 | **拒绝（红）** ✓ |
| 函数内重绑（局部同名） | 拒绝 | **拒绝（红）** ✓ |
| `del F` 后重绑 | 拒绝 | **拒绝（红）** ✓（重绑计数为 2） |
| 条件赋值（`if X: F = False`） | 拒绝 | **拒绝（红）** ✓ |
| **`for F in (True,): pass`** | 拒绝 | **当作常量 False（绿）** ✗ |
| **`F \|= True`（AugAssign）** | 拒绝 | **当作常量 False（绿）** ✗ |
| **`F, _x = True, True`（解包赋值）** | 拒绝 | **当作常量 False（绿）** ✗ |

原因是 `_module_constants` 的计数只遍历 `ast.Assign`/`ast.AnnAssign` 的**直接 `Name` 目标**：`For.target`、`AugAssign.target`、`Assign.targets` 里的 `Tuple`/`List` 都不计数。于是「赋值一次」被误判成立。

**端到端后果（守卫绿、运行时真跳过）**：

| 变异 | `sites` | 运行时 |
| --- | --- | --- |
| 模块级 `for FOR_FLAG in (True,): pass` 后 `@skipIf(FOR_FLAG)` | **绿** | 跳过 |
| 模块级 `AUG_FLAG \|= True` 后 `@skipIf(AUG_FLAG)` | **绿** | 跳过 |
| 模块级 `TUP_FLAG, _ = True, True` 后 `@skipIf(TUP_FLAG)` | **绿** | 跳过 |
| `AVAILABLE = False` + 函数内 `global AVAILABLE; AVAILABLE \|= True` + 模块级调用 + `setUp` 里 `if AVAILABLE: self.skipTest(...)` | **绿** | `OK (skipped=1)` |
| 对照：未被重绑的 `OK_FLAG = False` + `@skipIf(OK_FLAG)` | 绿 | 运行 ✓（正确） |

第四条是最接近现实的形态（特性探测标志 + `setUp` 守卫），**也是本轮唯一一条「守卫明确说会运行、实际被跳过」的证据**。注释里那句 *assigned anywhere else is left out* 与实现不符，需要一并修文案。

### 4. `_find_tests` 返回全部命中：**成立，第五轮的 X1/D-a 残余关闭；新增一条「限定到诱饵」的残余（必改 5）**

**(a) 8 条登记仍唯一定位：是。** `sites: OK (sites: 8 registered)`；逐行解析（直接 import 守卫）7 个唯一证据名全部 `1 match`：`StopOnFailureTest:1014`、`BuildFlagTest:978`、`IdempotencyTest:494`、`BucketAndGateParsingTest:1107`（3 行共用）、`StackExitCodeTest:663`，`step_gate:argv` 走 `probe:` 分支。

**(b) 同名方法出现在两处 → 红。** 同一模块里两个 `TestCase` 各定义 `test_dup_name` → `…在 2 个 TestCase 里同名出现（tests/ci/…:5、…:10）；守卫不按 glob 顺序挑，证据必须唯一定位`。**第五轮的两个残余都关了**：诱饵在**更早**的类（X1）→ 红；诱饵在**更后**的类（D-a 的同类）→ 红。`sorted()` 顺序不再参与判定。

**(c) 限定出口可用，且指向真实存在的那个。** `test:test_zz_probe_dup.py::SecondTest::test_dup_name` → 绿；把真实 3 行 bucket 证据改写成 `test:test_reference_acceptance.py::BucketAndGateParsingTest::test_bucket_names_follow_the_init_script_rules` 并 `tables --write` → `sites: OK (sites: 8 registered)` ✓（说明出口在真实登记上可用，不是纸面功能）。

**(d) 限定串写错全部红。** 类名错 → `指向不存在…`；模块名错 → 同上；方法名错 → 同上；`test:模块::类`（两段）→ `定位写法无法解析`；四段 → 同上；空段 → 同上。

**(e) 新残余：限定到诱饵。** 在真实证据所在模块里加一个同名断言类，把真实证据掏空，再把登记限定到**诱饵类** → `sites: OK (sites: 8 registered)`。比第五轮好的一点是：诱饵类名现在必须显式写进登记表，评审看得见；坏的一点是盖章成本没变（一个 3 行类 + 改 1 行登记 + `tables --write`）。这落在已披露的「证据与被登记站点不相关」边界内，但值得记进橡皮章残余。

### 5. `:` 前置修复：**假红确实修掉，但放行了真实写操作（必改 2、3）**

`GUARD_PRELUDE = re.compile(r"^(set|export|cd|umask|unset|:)(\s|$)")`（[ci-entry-guard.py:1713](../../scripts/dev-env/ci-entry-guard.py#L1713)）。16 例实测（每例只改 `ci.yml` 的 `sites` 步骤，插一行前置）：

| 前置行 | 期望 | 实测 |
| --- | --- | --- |
| `set -euo pipefail` / `umask 022` / `unset FOO` | 绿 | **绿** ✓（既有允许项无回归） |
| `: "${VAR:?missing}"` | 绿 | **绿** ✓（第五轮 W3 假红关闭） |
| `:`（单独一行） | 绿 | 绿 ✓ |
| **`: > /tmp/x`（创建并截断文件）** | 红 | **绿** ✗ |
| **`: >> /tmp/x`（追加）** | 红 | **绿** ✗ |
| **`: ; rm -rf /tmp/x`** | 红 | **绿** ✗ |
| **`: && curl https://… \| sh`**（命令链） | 红 | **绿** ✗ |
| **`: $(rm -rf /tmp/x)`（命令替换）** | 红 | **绿** ✗ |
| `::` / `:: foo` | 红 | **红** ✓（`::` 没被误判为无害前置） |
| `:ok`（无空格） | 红 | **红** ✓ |
| `echo hi`（对照） | 红 | 红 ✓ |
| `export FOO=$(rm -rf /tmp/x)` / `cd /tmp && rm -f x` | 红 | **绿**（**本轮之前就绿**） |

`: > file` 确实写：`rm -f /tmp/guard-write-probe; : > /tmp/guard-write-probe` → 0 字节文件；对已有内容的文件执行 `: > file` → 截断为 0 字节。所以「`: ` 只算环境前置」这句话在带重定向时是假的。

**定性**：这不是全新类别，而是既有「行前缀信任」的延伸——`export`/`cd` 在本轮之前就能带 `$(…)`、`&&`，而文档从没提过。但本轮**第一次**把 `:` 这个纯 no-op 内建加进允许列表，等于给任何 `: ` 开头的行（含重定向、命令替换、命令链）开了绿灯，同时 `ci-validation.md:35`、守卫自己的报错文案、`reference-acceptance.md:123` 都把它称为「环境前置」。**小结：修假红的方向对，代价是一个可被执行的新绿，而且这条修复没有测试锚**（见下）。

**修复没有测试锚**：把 `GUARD_PRELUDE` 改回 `\b`（即回滚本轮唯一的 bug 修复）后，`tests/ci` **294 例全绿**、`wiring` 也绿（`ci.yml` 里没有 `: ` 前置，所以只有人工写进去时才触发）。第五轮点名的这条假红可以在同一 PR 里被静默回滚而无人察觉。

### 6. `ci-validation.md:41` 与实现逐字一致：**12 项全部成立；第 8 项改措辞、第 9 项新增，两处都属实**

**项数核实**：父提交 11 项 → 当前 12 项（按 `；` 切分，逐项文本比对）；被移除的旧项只有一条（`证据与被登记站点不相关（…只校验它会被收集、会执行…`），换成第 8 项的新措辞；第 9 项 `skipTest` 藏在 `setUp` 调用的模块级助手里是全新增。**作者的陈述与我逐字比对的结论一致。**

**逐项造变异（每项都实测为绿，即确实「看不见」）**：

| # | 清单项 | 变异 | 实测 |
| --- | --- | --- | --- |
| 1 | 判定条件写反仍读 `.ok` | `if started.ok: raise` | **绿** |
| 2 | 读 `.ok` 只打日志 | 删掉 `if not started.ok` 分支，改 `print(started.ok)` | **绿** |
| 3 | `ctx.read` 里 `psql -c "DELETE …"` | 加一行 `ctx.read(("psql","-c","DELETE FROM …"))` | **绿** |
| 4 | 换接收者名/别名/包函数丢返回值 | `other = ctx; other.mutate(("docker","compose","rm","-f","api","gateway"))` | **绿** |
| 5 | `getattr`/反射拿 `mutate` | `run_any = getattr(ctx,"mutate"); run_any((…"rm","-f","api"))` | **绿** |
| 6 | `bit=` 以外的执行原语 | `subprocess.run(["docker","compose","rm","-f","api","gateway"])` | **绿** |
| 7 | 对已登记站点的判定语义作任何改动 | `if not started.ok and False: raise`（判定永远不触发） | **绿** |
| 8 | 证据与站点不相关（新措辞） | 把 `step_migrate:start` 的证据改成 `test:test_the_contract_passes`（存在、被收集、有断言、无 skip）+ `tables --write` | **绿** |
| 9 | （新）`setUp` 调用的模块级助手里 `skipTest` | `def setUp(self): _require_env(self)` + 模块级 `_require_env` 里 `case.skipTest(...)` | **绿** |
| 10 | `return` 藏非字面量条件 / 恒真断言形态不在单子里 | 证据体首行 `if sys.platform == "win32": return`；以及首行 `self.assertIn("a","abc")` | 两者均 **绿** |
| 11 | `*` 展开与构造助手按**名字**认 | 入口加 `compose_argv = ("sh","-c","rm -rf /")` + `ctx.read((*compose_argv,))` | **绿** |
| 12 | 「判定在哪」描述属实与否 | 判定列改成 `` `Context` ``（入口真实存在但无关）+ `tables --write` | **绿** |

**第 8 项与第 9 项的具体成立性**：第 8 项把「会执行」换成「不会被守卫认出的挂起形态停掉」——这是**变弱且变准**：我实测的模块级重绑、`setUp` 助手两种「守卫认不出」的挂起形态都仍绿，所以新措辞成立；第 9 项加了括号「守卫只看 `setUp`/`setUpClass` 自身的语句」，与实现（`_reachable_skip_marks` 只扫这两个方法体）**逐字吻合**。

### 7. 是否又「修一处松一处」：**登记表与文档锚点零松动，但新增出口本身带来两处行为级松动**

| 检查点 | 结论 |
| --- | --- |
| `PINNED_COUNTS` 六个值 | **未动**（`MUTATE_SITES` 8、`READ_ARGUMENTS` 4、`WRITE_TOKENS` 7、`PROBE_CASES` 9、`DECLARED_DEFERRALS` 1、`SELFTEST_CASES` 20） |
| `SELFTEST_CASES` 是否删例 | 未删未增（仍 20）；diff 里该常量无改动 |
| `MUTATE_SITES` / `READ_ARGUMENTS` / `WRITE_TOKENS` / `PROBE_CASES` | 未动（diff 里只有消息字符串行，无 `SiteRow(`/`ProbeRow(` 增删） |
| 文档锚点块 | **逐字节相同**（4185 字符、60 行）；`tables --write` 幂等（`git status` 干净） |
| `ci.yml` | 未改（不在 `git show --stat` 的 4 个文件里） |
| `tests/ci` 是否弱化断言 | 只删一行 fixture 常量（`FLAG = False` → `FLAG = len(["a"]) > 1`）。这是**必要适配**：新求值器会把 `FLAG = False` 证明为 False，`UnknownFixtureTest.setUp` 的 `if FLAG: skipTest` 就变成正确绿，那条用例会失效（我按新逻辑复算确认过）。不是弱化 |
| **行为级松动一** | 平台/常量求值器第一次允许「跳过」判绿，而谓词不完整 → 4 条真跳过绿（第 3 条） |
| **行为级松动二** | `:` 前缀把重定向/命令替换/命令链一并放行（第 5 条） |

**判断：规则文本与登记表没有松，但「修假红」的新代码各自带了一个可执行的新绿，且两个都没有测试覆盖其边界形态。** 前五轮的「修一处松一处」都是「叙事松于实现」；这一轮第一次是**行为松于宣称**，性质更值得盯。

### 8. 假红与记忆化：**7 条假红（1 条实用性强）；记忆化在同尺寸+保留 mtime 时会读到陈旧 AST**

**(a) 新增假红**（正确写法被拦）：

| # | 写法 | 结果 | 备注 |
| --- | --- | --- | --- |
| 1 | `@unittest.skipIf(sys.platform not in ("win32","cygwin"), …)` | **红** | `In`/`NotIn` 不求值。这是「非 Windows」最自然的写法，实用性最高 |
| 2 | `sys.platform in {"win32"}（→ 本应绿）` / `sys.platform > "a"` | 红 | 同上（无法判定 → 红） |
| 3 | `@unittest.skipIf(sys.platform.lower() == "win32", …)` | 红 | 方法链不认 |
| 4 | `@unittest.skipIf(platform.machine() == "x86_64", …)` | 红 | 只认 `platform.system()` |
| 5 | 类属性常量：`class T: SKIP = sys.platform == "win32"` + `@skipIf(SKIP)` | 红 | 只认模块级常量（类体作用域不认）；安全方向但自然 |
| 6 | 模块级 `platform` 被同名对象遮蔽 → `platform.system()` 按文本当作真 `platform` | 可能误判 | 本例把「其实会运行」判成「会跳过」（红） |
| 7 | `@unittest.skipIf(0, …)` | 红 | 只认 `True`/`False` 字面量（第五轮已披露） |

旧假红仍存在：`read` argv 用 f-string、`read` 用未登记只读助手、入口 `STEPS` 重排使 `log` 红、`probe` 硬签名演进、守卫步骤前置任意真实语句（`echo`/`docker compose config`）。

**已消的旧假红**：`: ` 前置（W3）、`skipIf(sys.platform == "win32")` 系四条（P3–P6）、类体 `skip()` 重绑（P7）——前者是本轮修的，后两者是本轮修的。

**(b) 记忆化 `(路径, mtime_ns, size)`**：

| 场景 | 结果 |
| --- | --- |
| 同一秒内改写（正常写文件，mtime_ns 变） | **重解析** ✓（`st_mtime_ns` 是纳秒精度，「同一秒」不是问题；实测同秒内两次写，改名立刻可见） |
| **同尺寸 + 恢复原 `mtime_ns`**（`os.utime`、`cp -p`、`rsync -a`、tar 解包都能造成） | **读到陈旧 AST** ✗（实测：把 `test_memo` 改成同长度 `test_mem0` 并复原 mtime，`_find_tests` 仍返回 `test_memo`） |
| 文件删除 / 新增 | 不受影响（glob 每个 key 都重新列举，删掉的不会命中缓存，新文件重新解析） |
| 缓存淘汰 | **无**。`_MODULE_INDEX_CACHE` 是进程级 dict，永不清理；`tests/ci` 每个用例一个 tempdir，294 例下来会留下上百条 AST 缓存 |

**结论**：对守卫这个「单次 CLI 进程、读 checkout 里的静态文件」场景，**不是现实风险**；对 `tests/ci`（每例独立 tempdir）也不是。危险在两类未来情形：① 有人写「同一路径、同尺寸」改写后再审一次（现在会静默读到旧 AST）；② 长驻进程调用。代码注释那句 *The key carries the file stamp so a rewritten fixture in a temp directory is re-parsed instead of served stale* 在「mtime 会变」的前提下成立，但**没提 mtime 可被复原，也没提缓存无淘汰**。建议把 key 换成内容摘要（`hashlib.blake2b(path.read_bytes())`），或在注释里写明这个前提。

### 9. 成本：**三个数字都对，`job 19 s` 要并上一句上下文**

| 项 | 作者陈述 | 我的实测 |
| --- | --- | --- |
| `sites` | 0.19 s（0.48 → 0.19） | **0.19–0.20 s**（3 次：0.20/0.20/0.19）；父提交同一命令 **0.44 s**（3 次）→ 提速属实 |
| `tests/ci` | 294 例 | **`Ran 294 tests in 5.16 s`**，全绿；父提交 **287 例**（+7） |
| `probe`/`wiring`/`log` | — | 0.11 / 0.03 / 0.03 s；四条合计 **≈0.36 s**，`ci.yml` 注释的 *measured under 2 s* 成立 |
| job | 19 s | **PR head（`f47b3c12`，run `43452222991`）19 s**；squash 后 `cb1d1960`（run `43455335297`）**9 s**（step 08→13）；父提交 `4cfdfe77`（run `43443137285`）11 s。19 s 与 9 s 都是真的，差别是排队/缓存抖动，不是守卫引入的 |

`ruff format --check .` 与 `uv run --no-project ruff format` 见交付说明；`uv.lock` 提交前复原。

---

## 四、必须修清单（按严重度）

1. **（高）补全「常量只绑定一次」的证明谓词**（[ci-entry-guard.py:1103](../../scripts/dev-env/ci-entry-guard.py#L1103)）。现在只数 `Assign`/`AnnAssign` 的直接 `Name` 目标，`for` 目标、`AugAssign`（`|=`/`+=`）、`F, _x = …` 解包都不计数，导致它们被当成可证明常量；实测 4 条「守卫绿、运行时真跳过」，含 `setUp` 里 `if AUG_FLAG: self.skipTest(...)` 的形态。修法：一次性收集该名字的**全部**绑定形态（Assign 目标递归 `Tuple`/`List`、`AnnAssign`、`AugAssign`、`For`/`AsyncFor` 目标、`With` 的 `optional_vars`、`except … as`、推导式目标、海象、`import … as`、`def`/`class`、`global`/`nonlocal` 声明、`Delete`），要求「恰好一次，且是模块级简单 `Assign`」；顺带修正 docstring 里 *assigned anywhere else is left out* 的过强表述。补 3 条 `tests/ci` 用例（for / augassign / 解包）。

2. **（高）给 `:` 前置补测试，并挡住写操作**。把 `GUARD_PRELUDE` 还原成 `\b` 后 `tests/ci` 294 例全绿、`wiring` 也绿 —— 第五轮唯一的 bug 修复零锚点。至少要两条用例：`: "${VAR:?}"` 绿、`: > file` 红。同时给前缀行加一条「不含 `>`/`>>`/`|`/`;`/`&`/`$(`/反引号」的限制，否则「只允许环境前置」这句话在 `: > file` 上继续是假的（同样的限制其实也该用在既有的 `export`/`cd` 上）。

3. **（中）把 `CI_PLATFORM` 与 runner 绑起来，或明确写进覆盖边界**。今天 12 个 job 全 `ubuntu-24.04`，常量正确；但 `reference_acceptance`（`ci.yml:941`）与 `changes`（`ci.yml:23`）的 runner 没有任何用例钉住，改 runner 不会红。最小改动：守卫从 `ci.yml` 读 `reference_acceptance` 的 `runs-on` 并与 `CI_PLATFORM_STRINGS` 对照（不一致即红），或在 `tests/ci` 加一条「所有 job 的 `runs-on` 都是 ubuntu-24.04」的断言。至少要在 `ci-validation.md:41` 的看不见清单里加一句「平台条件按写死的 Linux 判定；新增非 Linux runner 后平台条件判定失效」。

4. **（中）在覆盖边界里写明 `:` 前缀的真实语义**：`ci-validation.md:35`、`reference-acceptance.md:123` 与守卫报错文案都称 `: ` 为「环境前置」，实际是「任何以 `: ` 开头的行」。与第 2 条一起改，改完再收窄措辞（或改成「只允许无重定向的 `set`/`export`/…」）。

5. **（低）给「证据重绑」补模块级形态**（[ci-entry-guard.py:1224](../../scripts/dev-env/ci-entry-guard.py#L1224)）。类体里 `test_x = unittest.skip(...)(test_x)` 已红，但同一句下移一行到模块级（`Cls.test_x = unittest.skip(...)(Cls.test_x)`）就绿，运行时 `OK (skipped=1)`。文档写的是「类体里赋值」，字面成立，但这条修复的实际宽度只有一行。修法：扫模块级对 `Name.attr == 证据名` 的 `Assign`/`AnnAssign`/`setattr`/`del`。

6. **（低）记忆化 key 用内容摘要，或写明「mtime 可被复原」的前提**（[ci-entry-guard.py:949](../../scripts/dev-env/ci-entry-guard.py#L949)）。同尺寸 + 复原 `mtime_ns` 会读到陈旧 AST（实测）；缓存无淘汰。对当前 CLI 场景不是现实风险，但注释的稳妥性说法强于实现。

---

## 五、假红清单（正确改动会被拦）

1. `@unittest.skipIf(sys.platform not in ("win32","cygwin"), …)` → `sites` 红；**「非 Windows」最自然的写法，实用性最高**。出路：改 `==`/`or` 链或 `startswith`，或（更好）把 `In`/`NotIn` 对常量元组也加入求值器。
2. `@unittest.skipIf(sys.platform in ("win32",), …)`（本应绿）→ 红。
3. `sys.platform.lower() == "win32"` / `platform.machine() == …` → 红（只认三种探针 + 四种运算）。
4. 类属性常量 + `@skipIf(类属性)` → 红（只认模块级常量；类体作用域不认）。
5. `platform` 在模块里被同名对象遮蔽时会按文本错判（本例把会运行判成会跳过）。
6. 模块级条件赋值形式的常量（`if X: FORCE = False`）→ 红（无法证明，安全方向但自然写法）。
7. `@unittest.skipIf(0, …)` → 红（只认 `True`/`False` 字面量，第五轮已披露）。
8. 旧有未变：`read` argv 用 f-string、`read` 用未登记只读助手、入口 `STEPS` 重排（`log` 红）、`probe` 硬签名演进、守卫步骤前置任意真实语句（`echo`/`docker compose config`，设计如此）。

**已消的旧假红**：`: "${VAR:?}"`（W3，本轮）、`skipIf(sys.platform == "win32")` 四条（P3–P6，本轮）、类体 `skip()` 重绑（P7，本轮）、`set -euo pipefail`（上轮）、`@unittest.skipIf(False)`（上轮）、业务对象 `.skip()`（上轮）、`@unittest.expectedFailure` 之外的证据形态…（上轮）。

---

## 六、橡皮章残余（重新枚举，按盖章成本排序）

1. **新增写命令 + 无关证据 + 无关判定指针 + `PINNED_COUNTS` 改一个数 + `tables --write`**：四守卫 + 294 例全绿（第五轮 R3′ 同款，本轮未变）。已披露的边界。
2. **限定到诱饵类**：`test:<模块>::<诱饵类>::<同名方法名>`，诱饵 3 行、有断言；真实证据掏空。`sites: OK`。**本轮新形态**（第五轮靠 glob 顺序，现在要在登记里显式写诱饵类——可读性略好，成本不变）。
3. **模块级重绑证据方法名**：`Cls.test_x = unittest.skip(...)(Cls.test_x)` 一行，`sites` 绿、运行时 `OK (skipped=1)`。**本轮新形态**（类体形态已红）。
4. **`setUp` → 模块级 helper 里的 `skipTest`**：`sites` 绿、运行时跳过。**已披露**（新的第 9 项）。
5. **平台常量三形态**（`for` 目标 / `|=` / 解包重绑）：`sites` 绿、运行时跳过。**未披露**（必改 1）。
6. **「判定在哪」写任意入口真实名字**（`` `Context` ``）：绿；已披露为「只校验存在」。
7. **行数锚点 + `tables --write` 缩表/改词**：绿；已披露。
8. **证据规则改松不会被自检语料挡住**：`SELFTEST_CASES` 20 条全是 mutate/read 形态；证据规则由 `tests/ci` 锚住（本轮新规则**是**被锚住的：关掉平台识别 → 6 例红；放松「赋值一次」谓词 → 1 例红）。但 `:` 前置这条**没有**任何用例（第 5 条）。

---

## 七、我独立复现的变异

全部在 `/private/tmp/sn682-mut`（同级目录，非 worktree 内），每例前从 `/private/tmp/sn682` `rsync -a --delete` 复原；涉及登记表的例在改常量后跑 `tables --write`。**未 push 任何变异。** 探针脚本 `/private/tmp/sn682-mut/probe.py`（直调 `_ci_condition_value`/`_skip_decorator_reason`/`_module_constants`）、`probe2.py`（合成 `tests/ci` fixture + `audit_evidence()` + 真跑 `unittest`）、`probe3.sh`（`wiring` 前置）、`probe4.sh`（12 项清单）、`probe5.sh`（证据定位）、`probe6.py`（假红与绑定形态）。

### 7.1 求值器（条目 1、2）

| ID | 形态 | 结果 |
| --- | --- | --- |
| E1 | `skipIf(sys.platform == "linux")` | **红**（`在 CI 平台（linux）上条件为 True`） |
| E2 | `skipIf(not sys.platform == "win32")` | **红** |
| E3 | `skipIf(sys.platform.startswith("lin"))` | **红** |
| E4 | `skipIf("linux" == sys.platform)` | **红**（反向比较） |
| E5 | `skipUnless(sys.platform != "linux")` | **红** |
| E6 | `skipIf(platform.system() == "Linux")` / `skipIf(os.name == "posix")` | **红** |
| E7 | `skipUnless(sys.platform == "linux")` / `skipIf(sys.platform == "win32")` | 绿 ✓ |
| E8 | `skipIf(not sys.platform.startswith("win"))` | **红** |
| E9 | 五条真跳过 fixture 在本机 `unittest` 跑 | `Ran 5 tests … OK (skipped=1)`（本机只跳 1 条，守卫按 CI 判 5 红） |

### 7.2 常量绑定（条目 3）

| ID | 形态 | 结果 |
| --- | --- | --- |
| C1 | `global F; F = True` 重绑 | 拒绝（红）✓ |
| C2 | 函数内同名局部重绑 | 拒绝（红）✓ |
| C3 | `del F` 后重绑 | 拒绝（红）✓ |
| C4 | `if X: F = False` 条件赋值 | 拒绝（红）✓ |
| C5 | `for F in (True,): pass` + `@skipIf(F)` | **绿（真跳过）** ✗ |
| C6 | `F \|= True` + `@skipIf(F)` | **绿（真跳过）** ✗ |
| C7 | `F, _ = True, True` + `@skipIf(F)` | **绿（真跳过）** ✗ |
| C8 | `AVAILABLE = False` + `global AVAILABLE; AVAILABLE \|= True` + `setUp` 里 `if AVAILABLE: skipTest` | **绿**，运行时 `OK (skipped=1)` ✗ |

### 7.3 证据定位（条目 4）

| ID | 形态 | 结果 |
| --- | --- | --- |
| F1 | 逐行解析 8 条登记 | 全部 `1 match`；`sites: OK (sites: 8 registered)` |
| F2 | 同名方法在两个 `TestCase`（同模块） | **红**（`在 2 个 TestCase 里同名出现`） |
| F3 | 诱饵在**更早**的类（第五轮 X1 原样）+ 真证据掏空 | **红**（X1 关闭） |
| F4 | 诱饵在**更后**的类 + 真证据掏空 | **红**（D-a 关闭） |
| F5 | 限定到 `SecondTest::test_dup_name` | 绿 ✓ |
| F6 | 限定到真实 `BucketAndGateParsingTest::test_bucket_names_…`（3 行 + `tables --write`） | `sites: OK (sites: 8 registered)` ✓ |
| F7 | 类名错 / 模块名错 / 方法名错 / 两段 / 四段 / 空段 | 全 **红** |
| F8 | 限定到**诱饵类**（真证据掏空） | **绿**（残余） |
| F9 | 类体 `test_x = unittest.skip(...)(test_x)` | **红**（本轮修复） |
| F10 | 模块级 `Cls.test_x = unittest.skip(...)(Cls.test_x)` | **绿**，运行时 `OK (skipped=1)`（残余） |
| F11 | `setUp` → 模块级 helper 的 `skipTest` | **绿**（已披露为新第 9 项） |

### 7.4 `wiring` 前置（条目 5）

见第五节表格（16 例）：`: "${VAR:?}"` 绿（修复生效）；`::`/`:: foo`/`:ok` 红；`: > file`、`: >> file`、`: ; rm`、`: && curl|sh`、`: $(rm …)` 全绿（新松动）；`export FOO=$(rm …)`、`cd /tmp && rm` 绿（本轮之前就绿）。

**反悔测试**：`GUARD_PRELUDE` 改回 `\b` → `tests/ci` **294 例全绿**、`wiring` 绿（修复无锚）。

### 7.5 12 项清单、记忆化、成本

见第三、六节；另：

| ID | 探针 | 结果 |
| --- | --- | --- |
| M1 | 同秒改写（mtime_ns 变） | 重解析 ✓ |
| M2 | 同尺寸 + `os.utime` 复原 mtime | **陈旧 AST**（改名不可见）✗ |
| A1 | 关掉平台识别（`_platform_string` 恒 None） | `tests/ci` **6 例红**（新规则被锚住）✓ |
| A2 | 放松「只赋值一次」谓词 | `tests/ci` **1 例红**（只覆盖普通 `Assign` 重绑）✓ |
| A3 | 回滚 `:` 修复 | **全绿**（无锚）✗ |

### 7.6 门禁与真实 CI（只读）

- `gate` 作业的 `needs` 与三个受保护状态名本轮未变（`ci.yml` 未被本片触碰，`git show --stat` 只有 4 个文件）。
- 真实 run：`43455335297`（`cb1d1960`）全绿；`reference_acceptance` 作业 9 s；PR head `43452222991`（`f47b3c12`）19 s；父提交 `43443137285` 11 s。

---

## 八、这轮新增的求值器：净收益还是净风险？

**净收益，但必须补上第 1、2、3 条必改，否则收益会被它自己带来的假绿吃掉。**

**收益是确定的：**

1. 它把第五轮 P3–P6 四条**假红**（`skipIf(sys.platform == "win32")` 系列在 Linux 上永远不触发）从 required check 里消掉——这是本仓库第一次让「给证据用例加平台守卫」这件事不再撞墙。
2. 判定**与宿主无关**：守卫不读宿主 `sys.platform`，所以本机 macOS 与 CI 的结论一致（实测本机运行时只跳 1/5，守卫判 5/5 红）。这比「读本机平台」的实现强一档。
3. 布尔代数（`not`/`and`/`or` 三值）与 Python 短路语义一致，我没有找到「算出 True 其实不跳过」的方向性错误。

**风险也是真的，而且都落在「新增的绿」一侧：**

1. 这是六轮里**第一个允许「跳过」判绿的出口**，而它的证明谓词（只数 `Assign`）不完整 → 我已经造出 4 条「守卫绿、运行时真跳过」。前五轮的所有假绿都是「看不见某个改动」，这一条是「看见了，但算错了」。
2. `CI_PLATFORM` 是**写死的假设**，与 runner 零耦合；加 macOS job 后三种自然写法会静默失效。
3. 它的两条「修假红」都没有覆盖边界形态的测试：`: ` 修了却没测（回滚全绿），平台常量只测了普通 `Assign` 重绑（`for`/`|=`/解包没测）。

**量化对比**：收益 = 消掉 4 条真实会卡住正确改动的假红（第五轮实测）；风险 = 新增 2 类可执行假绿（常量三形态、`: ` 重定向）+ 1 个未耦合假设 + 2 处无锚修复。假红每撞一次就是 required check 拦下一个正确 PR，代价高但可见；假绿不可见，代价是守卫的意义被稀释。**所以结论是「保留求值器、立刻补谓词与测试」，不是「回滚」**——回滚会把第五轮那四条假红请回来，而假红的可见伤害更大。

---

## 九、只能依赖作者陈述的点

1. **真实 apply 运行**：本机没有真实 PostgreSQL/MinIO/Docker apply 环境，「语义由 `tests/ci` 的假 Runner 用例与真实 apply 运行负责」这条分工，我只核到「写在文档里、`tests/ci` 确实在跑」，没有验证 apply 路径的实际覆盖。
2. **`log` 子命令对真实 dry-run 日志的判定**：需要 `--log` 的真实日志；我核到的是 `log` 输入未被本片改动影响、以及 `wiring` 会要求四条子命令都真实执行。
3. **上一版的具体代码路径**：`_skip_decorator_reason` 旧版「非字面量一律红」、`_find_test` 旧版「返回第一个命中」等，我引用的是第五轮报告与本轮 diff，没有把 `cb1d1960~1` 的守卫逐条重跑（只用它做了 `sites` 计时 0.44 s 与 `tests/ci` 287 例的对照）。
4. **GitHub Actions 上 `setup-uv`/`docker compose config` 的行为**：只读了真实 run 的结果与耗时，没有在 Actions 上替换 workflow 复现变异；变异全部在本机 Python 3.14.9 + 文件副本上完成。
5. **`CI_PLATFORM` 在新 runner 上的行为**：我没有真的加一个 macOS job 验证；第 1 节 (c) 的三条「会静默放过」是读代码 + 本机 macOS 运行时行为推出来的（本机 `sys.platform == "darwin"`，`skipIf(sys.platform == "darwin")` 确实跳过）。
6. **作者声称的 `sites` 0.48 s**：我测到的是父提交 0.44 s、当前 0.19 s；0.48 与我量级一致，属测量差异，未视为不实陈述。

---

## 十、一句话给非作者看

**这一轮修的三件事都真修了：`:` 假红、平台条件假红、同名证据定位。** 但两处「修假红」各自带了一个新的绿：`: ` 前缀把 `: > file` 这类真实写操作放行（而且这条修复没有测试，回滚无人察觉），平台求值器把「模块顶层只赋值一次」的常量当成可证明，而 `for` 目标、`|=`、解包赋值都不计数——我用它造出了 4 条「守卫绿、运行时真跳过」，这是六轮以来第一次出现「新增的绿表示一个真实跳过」。求值器值得留（它消掉的四条假红是真的），但谓词与测试必须补；`CI_PLATFORM = "linux"` 今天是对的，但它是一个**没有被校验的假设**，不是事实。
