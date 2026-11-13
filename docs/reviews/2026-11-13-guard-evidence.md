# 验收入口结构守卫「证据收集与类级挂起」的非作者审查（#677 / #572 第五轮）

- **审查对象**：`4cfdfe77`（`fix: 收口结构守卫的证据收集与类级挂起 (#572) (#677)`），已在 `main`（基线 `origin/main` head 即此提交）。
- **基线核验**：`git rev-parse origin/main` → `4cfdfe776b16776a6e515ab6048414ea8cb6732c`；`git cat-file -t 4cfdfe77` → `commit`。主检出停在陈旧分支 `docs/tracker-batch12`（`a7508635`），所有事实取自 `origin/main` / 新 worktree。
- **独立 worktree**：`/private/tmp/sn679`，分支 `review/679-guard-evidence`（`git worktree add /private/tmp/sn679 -b review/679-guard-evidence origin/main`）。变异实验在同级目录 `/private/tmp/sn679-mut`（每例前 `rsync -a --delete --exclude .git` 从 `sn679` 复原），另用 `git archive 4cfdfe77~1` 解到 `/private/tmp/sn679base` 作用例数对照。**未 push 任何变异。**
- **审查者**：非作者。前四轮报告 [structure-guard](2026-11-13-structure-guard.md)（第三轮）、[guard-residuals](2026-11-13-guard-residuals.md)（第四轮）是本轮的验收基线。
- **本片改动范围**：5 个文件，+517/−133。`ci-entry-guard.py`（+399/−…）、`tests/ci/test_ci_entry_guard.py`（+212/−…）、`ci.yml`（+10/−6，**全在注释块内**）、`ci-validation.md`（+8/−8）、`reference-acceptance.md`（+21/−…）。产品代码零改动。
- **产品代码与 CI 零改动**：本报告是唯一新增文件；`uv.lock` 提交前复原。

---

## 一、结论

**有条件通过。**

**第四轮点名的三条必改，两条改到位、一条只修了字面形态。** `_skip_marks` 确实从「只看方法」扩到「方法 + 类 + 同文件基类 + `setUp`/`setUpClass`」，`_find_test` 确实从 `glob("*.py")` 收紧到 `test*.py` + `test` 前缀 + 同文件继承链，删掉了「赋给 `_` 后不再使用」。四类新查询（同名重复定义、字面量恒真断言、`if <字面量真>:` 里的 `return`、`try/except` 吞 `AssertionError`）我每条都独立造了变异，**全部判红且错误信息指向正确位置**。

**但本轮新增的 `GUARD_PRELUDE` 正则有一个「文档与实现直接矛盾」的假红**：`ci-validation.md:35` 与守卫自己的错误信息都明写 `: ` 是允许的环境前置（`reference-acceptance.md:123` 用「`set`/`export`/`cd` 之类的环境前置」概括），而代码的正则**永远匹配不到以 `:` 开头的行**（`\b` 在 `:` 与空格之间没有词边界）。这是本轮为了修 `set -euo pipefail` 假红而新写的代码，属于「修一个假红、引入一个新的、而且写在文档里当成了已修」。

**而且第四轮那句最强的话只收窄了一半。** `reference-acceptance.md` 的「每次 CI 都会真的判红」确实改写成了「会被收集、会真的执行、断言不会被跳过或吞掉」，方向对；但**同一个句子在 `ci-validation.md:41` 原样保留着**（`这条证据会真的判红`）。本轮修掉的 skip 形态之外，还有两条常见写法能让证据被真的跳过而全绿（类体里 `test_x = unittest.skip(...)(test_x)`、`setUp` 调用的模块级助手里的 `skipTest`），所以这句仍然强于实测。

**没有发现守卫规则被放宽。** `SELFTEST_CASES` 仍 20（未增未减）、`PINNED_COUNTS` 六个值全未动、`WRITE_TOKENS`/`READ_ARGUMENTS`/`PROBE_CASES` 未动、`ci.yml` 的改动逐行确认全在注释块，没有任何 `run:` 或判定逻辑变化。

**必须处理的三项**（都不动架构）：

1. 修 `GUARD_PRELUDE` 对 `:` 的匹配（`^(set|export|cd|umask|unset|:)(\s|$)` 之类），否则「`: ` 允许」是一句无法兑现的话。
2. 把「断言不会被跳过或吞掉」/「这条证据会真的判红」再收一档，或补上类体 `skip()` 再赋值与 `setUp` → 助手函数里的 `skipTest` 两种形态（第四轮已经点过「给这个类加个 setUp 跳过条件」是常见重构动作，本轮只堵住了直接写法）。
3. 条件跳过（`skipIf(sys.platform == 'win32')`、模块常量、`skipUnless(not FLAG)`）**是**假红，这是**已披露**的设计取舍（`ci-validation.md:33` 明写「条件不是字面量布尔值则按无法确认会运行算红」），但需要在假红清单里给一条出路（挪到非证据用例 / 改成字面量），否则第一次有人给证据用例加平台守卫就会被 required check 卡住且看不出原因。

---

## 二、逐条清单

### 1. 四类新查询是否真生效：**全部生效，消息位置正确**

独立复现（每例 `rsync` 复原后单改证据用例 `test_bucket_names_follow_the_init_script_rules`，它同时是 `step_buckets:start` / `initialize` / `verify` 三条登记的唯一证据）：

| ID | 变异 | `sites` | 消息位置 |
| --- | --- | --- | --- |
| A | 证据 **类**上 `@unittest.skip("parked")` | **红** | 指 `test_reference_acceptance.py:1105`（类行）+ 点名「类 `BucketAndGateParsingTest` 上的 `unittest.skip('parked')`（无条件跳过）」 |
| B | 证据类 `setUp` 里 `self.skipTest("fixture unavailable")` | **红** | 指 `:1104`（方法行）+ 点名「`BucketAndGateParsingTest.setUp` 里的 `self.skipTest('fixture unavailable')`」 |
| C | **同文件基类** `EvidenceBase.setUp` 里 `skipTest` | **红** | 指 `:1109` + 点名「`EvidenceBase.setUp` 里的 `self.skipTest(...)`」 |
| D | 方法 `@unittest.expectedFailure` | **红** | 指方法行 + 「方法上的 `unittest.expectedFailure`（断言失败也会被计为通过）」 |
| E | 同类同名方法定义两次（断言版在前、`return` 版在后） | **红** | 「在 `BucketAndGateParsingTest` 里定义了 2 次（行 1107、1117）；守卫读到第一个、运行时用最后一个」 |
| F | 断言换成 `self.assertTrue(True)` | **红** | 「的断言恒真（`self.assertTrue(True)`），永远不会失败」 |
| G | 正文首行 `if True: return` | **红** | 「在前面 `if True:` 早退，剩下的正文是死代码」 |
| H | `try: self.assertEqual(...) except AssertionError: pass` | **红** | 「把断言包在会吞 `AssertionError` 的 try/except 里（`try:`）」 |

三类「类级/基类」查询的实现确实走了继承链：`_class_ancestry` 用同模块类名 BFS 回溯，`_evidence_skip_problems` 对链上每个类看装饰器、对链上每个 `setUp`/`setUpClass` 看 `skipTest`/`raise SkipTest`（`ci-entry-guard.py:991`）。

**位置精度**：A 的 `where` 用 `klass.lineno`、方法级用 `node.lineno`，都命中真实行；B 例外——上报行是证据方法行、不是 `setUp` 行，靠消息里的 `` `类.setUp` `` 补足定位。可选改进，不是缺陷。

### 2. `_find_test` 收紧有没有放过真证据：**没有放过；8 条登记全部满足新约束**

逐条解析当前登记（直接 import 守卫模块跑 `_find_test`）：

| 站点 | 证据 | 解析结果 |
| --- | --- | --- |
| `step_migrate:start` | `test_behind_migration_stops_before_roles_buckets_and_stack` | `test_reference_acceptance.py:1011 StopOnFailureTest`（1 个定义） |
| `step_migrate:migrate` | `test_migrate_step_builds_before_running` | `:975 BuildFlagTest` |
| `step_roles:provision_argv()` | `test_roles_step_creates_once_then_reports_idempotent` | `:486 IdempotencyTest` |
| `step_buckets:start` / `initialize` / `verify` | `test_bucket_names_follow_the_init_script_rules` | `:1104 BucketAndGateParsingTest` |
| `step_gate:argv` | `probe:gate_verdict-fail` | 非 `test:` 分支（`probe` 侧校验） |
| `step_stack:ctx.compose(...)` | `test_up_exit_nonzero_with_healthy_ps_is_red` | `:635 StackExitCodeTest` |

7 个唯一证据名**全部**以 `test` 开头、全部落在 `tests/ci/test_reference_acceptance.py`（`test*.py`）、所在类直接或经同文件基类接 `unittest.TestCase`。收紧没有放过任何现有证据，也没有制造现存假红。

**放进非 `test*.py` 会怎样**：变红，且消息里带原因 —— 我把同名断言 `TestCase` 放进 `tests/ci/helper_evidence.py` 并掏空真证据，`sites` 红，消息为 `指向不存在（或不在 `TestCase` 类里、不在 test*.py 模块里）的 tests/ci 用例 …`（**D-c**）。这正是第四轮 E10 的关闭证据。

**新引入的假红面**：`_is_test_case` 只在**同文件**里回溯基类，`class X(ExternalBase)`（基类从别的模块 import）会判红（**L2**）。文档已明写「从别的模块导入的基类不认识」，属已披露；但消息把「不存在 / 不在 `TestCase` 类里 / 不在 `test*.py` 模块里」三种原因压成一句，L2 这种情形读消息无法定位到「是基类跨模块」。建议消息里补一句「基类跨模块时守卫认不出」。

### 3. 新引入的假红：**4 类，其中 1 类未披露且与文档矛盾**

| ID | 正确写法 | 结果 | 是否已披露 |
| --- | --- | --- | --- |
| **W3** | 守卫步骤前置 `: "${VAR:?missing}"` 保护语句 | **红**：「前面还有会先执行的命令 `: "${SPEECHNEXUS_ENV:?missing}"`；只有 `set`/`export`/`cd`/`umask`/`: ` 这类环境前置可以出现在守卫命令之前」 | **未披露，且与文档/消息自身矛盾** |
| **P3** | `@unittest.skipIf(sys.platform == "win32", ...)` 加在证据用例 | **红**：`skip 条件不是字面量，无法确认这个用例会运行` | 已披露（`ci-validation.md:33`） |
| **P4** | `@unittest.skipUnless(sys.platform != "win32", ...)` | **红**，同上 | 已披露 |
| **P5** | `@unittest.skipIf(SKIP_ON_WINDOWS, ...)`（模块常量 `SKIP_ON_WINDOWS = False`） | **红**，同上 | 已披露 |
| **P6** | `@unittest.skipUnless(not SKIP_ON_WINDOWS, ...)` | **红**，同上 | 已披露 |
| **L3** | `setUp` 里 `if sys.platform == "win32": self.skipTest(...)` | **红**（`setUp` 里的 `self.skipTest`） | 已披露（无条件判红） |
| **L2** | 证据类继承别的模块的 `TestCase` 基类 | **红** | 已披露 |

平台条件跳过这一类**确实是假红**：在 Linux CI 上 `sys.platform == "win32"` 恒 False，用例必然执行，属正确写法被拦。但它是**作者明确写进文档的取舍**（`ci-validation.md:33`：`skipIf(False)` 这类恒不触发的不算；条件不是字面量布尔值则按「无法确认会运行」算红），并且实现了 `skipIf(False)`→绿（**P1**）、`skipUnless(True)`→绿（**P2**）。所以判定是「已披露的假红，可接受但需给出口」：目前成本为零（7 条登记证据无一使用条件跳过），一旦有人给证据用例加平台守卫，required check 会红且不能绕过。建议在两篇文档的同一段补一句出路（「平台条件跳过不属于证据用例；把条件跳过放到非证据用例，或把该缺口登记到 `probe` 侧」）。

**`:` 这一条不是取舍，是 bug。** `GUARD_PRELUDE = re.compile(r"^(set|export|cd|umask|unset|:)\b")`（`ci-entry-guard.py:1461`）在 `:` 后面要求 `\b`，而 `:` 与后续空格都是非单词字符，永远没有边界：

```text
'set -euo pipefail' -> True
'export A=b'         -> True
'cd x'               -> True
'umask 022'          -> True
'unset FOO'          -> True
': ok'               -> False     # 文档与错误信息都声称允许
': "${X:?}"'         -> False
```

两处明写 `: `（`ci-validation.md:35` 的 `wiring` 段、守卫自己的错误信息；`reference-acceptance.md:123` 用「之类的环境前置」概括），实测一次都不成立。**这是本轮引入的假红 + 文档-实现矛盾，且是「修一个假红（`set -euo pipefail`）时新写的代码」。** 顺带：`unset` 在代码里允许、文档里没列（方向相反的小不一致）。

**已修好的假红**（逐条实测确认）：

| 变异 | 结果 |
| --- | --- |
| 守卫步骤前置 `set -euo pipefail` | **绿**（W1，第四轮假红 #1 已消） |
| 前置 `set -e` + `cd` + `export` 三行 | **绿**（W2） |
| 前置 `umask 022` | **绿**（W4） |
| 证据正文调用业务对象 `.skip(...)` | **绿**（X2，第四轮假红 #3 已消；`_skips_here` 只认 `*.skipTest`） |
| `@unittest.skipIf(False, ...)` / `skipUnless(True, ...)` | **绿**（P1/P2，第四轮假红 #2 已消） |
| 合法重构：抽出同文件基类、`setUp` 做正常初始化 | **绿**（L1） |

**未变、仍存在的旧假红**（上一轮已列，本轮未处理）：`read` argv 用 f-string、`read` 用未登记只读助手、入口 `STEPS` 重排使 `log` 红、`probe` 硬签名演进。

### 4. `expectedFailure` 的处理是否过严：**不过严**

`tests/ci` 全量 21 个 Python 模块（其中 18 个 `test*.py`）里，`expectedFailure` 只出现在两处：`test_ci_entry_guard.py:151`（在 `EVIDENCE_FIXTURE` **字符串**里，不是真用例）与 `:280`（断言文本）。**没有任何真 `TestCase` 使用 `expectedFailure`**，也没有任何 `skipIf`/`skipUnless`/`skipTest` 用在真用例上（其余命中全在同一个字符串 fixture 里）。所以把它们判红在本仓库当前形态下不产生假红。反方向也成立：真加一个 `@unittest.expectedFailure` 的证据用例，运行时报 `OK (expected failures=1)`，确实等于断言失效，判红是正确取舍。

### 5. 端到端盖章仍绿：**仍是绿的，但披露已经补上，性质从「未披露的假绿」变成「已披露的边界」**

我重做了第四轮的 R3′：在入口 `step_gate` 的非 dry-run 路径插入

```python
removed = ctx.mutate(("docker", "compose", "rm", "-f", "api", "gateway"))
print(removed.ok)
```

登记 `step_gate:('docker', 'compose', 'rm', '-f', 'api', 'gateway')`，判定写 `` `step_gate` ``（入口真实存在的函数名），证据写 `test:test_the_contract_passes`（`test_ci_entry_guard.py:692`，真 `TestCase` 方法、会被收集、执行、断言非恒真、无 skip），`PINNED_COUNTS["MUTATE_SITES"]` 8→9 + `tables --write`：

```text
tables --write rc 0 tables: 已写入 docs/operations/reference-acceptance.md
sites:  rc=0 sites: OK (sites: 9 registered)
probe:  rc=0 probe: OK (probe: 9 cases)
wiring: rc=0 wiring: OK (wiring: 4 guard steps)
tests/ci rc 0  Ran 287 tests in 6.863s  OK
```

所以「一条真实破坏性写命令 + 一个存在的无关证据 + 一个存在的判定指针 + 一行 PINNED_COUNTS + 一条生成命令」仍然能把四守卫和 `tests/ci` 全部走绿。**但本轮不再把这写成「不是橡皮章」的强承诺**：`ci-validation.md:41` 的看不见清单新增「证据与被登记站点不相关（只校验它会被收集、会执行、断言不是字面量恒真也不会被吞）」，`reference-acceptance.md:138-139` 把「每次 CI 都会真的判红」改写成「会被收集、会真的执行、断言不会被跳过或吞掉」并紧接一句「不是「它覆盖了这个站点的语义」——证据的相关性…仍由评审和真实 apply 运行负责」。**按「宁可宣称得少」的标准，这条改写是有效的**：它把「相关性」明确划出界外，而 R3′ 恰好落在划出的界外。

**剩下没兑现的是同一句话的另一半**，见条目 6。

### 6. 文档与实现一致性：**主体一致，「看不见」清单逐项成立，但有两处未收窄**

`ci-validation.md:41` 的「看不见」清单我逐项做了实现探针，**11 项全部真实**（这是本轮文档最扎实的部分）：

| 清单项 | 探针 | 结果 |
| --- | --- | --- |
| 判定条件写反仍读 `.ok` | `if result.ok: raise`（I1） | 绿 |
| 读 `.ok` 只打日志不中断 | `print(result.ok)`（I2） | 绿 |
| `ctx.read` 里 `psql -c "DELETE …"` | 加入 `ctx.read(("psql","-c","DELETE …"))`（I3） | 绿 |
| 换接收者名/别名/包函数丢返回值 | `other = ctx; other.mutate(...)`（I4） | 绿 |
| `getattr` 反射拿 `mutate` | `m = getattr(ctx,"mutate")`（I5） | 绿 |
| `subprocess`/`os.system` | `subprocess.run([...])`（I6） | 绿 |
| 证据与站点不相关 | R3′（条目 5） | 绿（已披露） |
| `return` 藏在非字面量条件里 | `if sys.platform == "win32": return`（P9） | 绿 |
| 断言恒真但形态不在单子里 | `self.assertIn('a','abc')`（P10） | 绿 |
| 判定列描述属实与否 | 只校验反引号名字存在（第四轮 J5/J6，代码未变） | 绿 |
| `*` 展开/构造助手按名字认 | 代码未变（第四轮 S6/S7） | 绿 |

`ci-validation.md:33` 的 `sites` 段逐句核对：`test*.py` + `test` 前缀 + 同文件继承链 ✓；同名重复定义 ✓（E）；方法/类/同文件基类 skip 与 `expectedFailure` ✓（A/C/D）；条件为真的 `skipIf`/`skipUnless`、非字面量算红 ✓（P1–P6）；`setUp`/`setUpClass`/正文 `skipTest` ✓（B/C/L3）；开头 `return`/字面量恒真分支早退 ✓（G）；无断言/字面量恒真断言 ✓（F）；吞 `AssertionError` 的 `try/except` ✓（H）。**这一段与实现的形态清单是逐条对得上的**，本轮最难得。

两处未做到一致：

1. **`wiring` 段（`ci-validation.md:35`）与 `reference-acceptance.md:123` 的「`set`/`export`/`cd` 之类的环境前置」在实现里不存在 `:` 这一项**（条目 3 的 W3），错误信息自己也在列这个不成立的允许项。
2. **「这条证据会真的判红」在 `ci-validation.md:41` 原样保留**（本轮 diff 未触碰该句）。反例两种，都实测全绿：

| ID | 形态 | `sites` | 运行时 |
| --- | --- | --- | --- |
| **P7** | 类体里 `test_x = unittest.skip("reassigned")(test_x)`（不用装饰器） | 绿 | `OK (skipped=1)` |
| **P8** | `setUp` 调模块级助手 `_require_platform(self)`，由助手 `self.skipTest(...)` | 绿 | `OK (skipped=1)` |

P8 尤其值得注意：第四轮说的「给这个类加个 setUp 跳过条件」正是最常见重构动作，本轮堵住了**直接**写法，但把判断抽进 helper（很自然的重构）就又漏了。同理，`X1`（同文件里更早一个同名 `TestCase` 诱饵 + 真证据掏空）也全绿且 `tests/ci` 288 例 OK —— 第四轮的 E10（跨模块诱饵）关闭了，同模块更早的诱饵仍然成立。

顺带记录两处准确的小改写：`ci.yml` 的注释块从「not skipped」改成了枚举式「not skipped at method, class or fixture level, not an `expectedFailure`, not defined twice」，与实现一致；`reference-acceptance.md:120` 把 `_` 语义改成「赋给 `_` 即红、不看之后有没有再读」，与实现一致（全仓 `后不再使用` 只剩历史审查报告）。

### 7. 门禁与成本：**都在，数字都对**

- **仍在 required checks**：`Gate: required checks for this change` 的 `needs` 含 `reference_acceptance`（`ci.yml:732`）；`gh api repos/hrygo/SpeechNexus/branches/main/protection` 仍是三个名字 `["Gate: required checks for this change","Web: lint, types, tests, build and accessibility","Compose: configuration validation"]`，未变。`ci.yml` 本片的 6 处改动全在注释块内，`run:`/`if:`/`needs:` 一行未动（逐 hunk 核对）。
- **`tests/ci` 272 → 287**：`git archive 4cfdfe77~1` 解出的树跑 `Ran 272 tests`，当前树 `Ran 287 tests`，**+15 属实**。
- **job 时长**：`gh run view 43441176316` → `Deploy: reference acceptance entry (dry-run execution) in 11s`，与作者陈述一致；同 run 全绿。
- **守卫自身成本**：本机 Python 3.14.9，`sites` 0.50–0.52 s、`probe` 0.10 s、`wiring` 0.04 s，合计 **0.64 s**，`ci.yml` 注释「entry point plus the four structural guards measured under 2 s」仍成立。但 `sites` 从上一版 0.23 s 涨到 0.50 s，原因是 `audit_evidence` 对 8 行各调一次 `_find_test`，每次都重新 `ast.parse` 全部 18 个 `test*.py`（`test_reference_acceptance.py` 在 `sorted()` 里靠后，代价最高）。不作阻塞项，但一次 memo 就能省掉一半。

### 8. 对 `tests/ci` 自身的影响：**没变脆，有一个改进、一处仍靠测试而非语料**

- **规模与耗时**：272 例 5.83 s → 287 例 6.26 s（本机）。`tests/ci` 跑在 `changes` 作业（`timeout-minutes: 3`），余量充足。
- **无行号/文件名耦合**：新用例全走 tempdir 里的 pytest 风格 fixture（`EVIDENCE_FIXTURE` / `DECOY_FIXTURE`）与直接调 `GUARD.audit_evidence()`，没有断言任何仓库行号；断言的是消息子串（`会被跳过或不算失败`、`test*.py 模块里`、`定义了 2 次` 等），属消息契约，改消息时该红，合理。
- **重写真实 workflow 的两例**（`test_a_shell_prelude_before_the_guard_command_is_green` / `test_a_real_command_before_the_guard_command_is_red`）用精确字符串替换 + `assertNotEqual` 兜底：一旦 workflow 的缩进或命令文本变化，`assertNotEqual` 立刻红而不会静默退化成空转。这个写法比裸 `replace` 好。
- **第四轮「陈旧拷贝」问题已修**：`test_a_shrunk_table_without_touching_the_pin_is_red` 的 `shrunk` 字典改成 `len(GUARD.MUTATE_SITES) - 1` 等从活体推导，`"READ_ARGUMENTS": 3` 那处陈旧值消失。这是本轮的一个实在改进。
- **一处仍需注意**：**新增的证据规则不在自检语料里**。`SELFTEST_CASES` 20 条全部是 `mutate`/`read` 形态（`vacuity-empty-module`、`discarded-result`、`read-starred-*` 等），没有一条覆盖 skip/`expectedFailure`/重复定义/恒真断言/吞断言。实测：删掉 `_skip_decorator_reason` 的 `expectedfailure` 分支后，`sites` 仍 `OK (sites: 8 registered)`（selftest 全绿），只有 `tests/ci` 1 例红。所以「把规则改松会先弄红自检」这句对证据规则不成立 —— 第二道锁在 `tests/ci`（另一个作业），而不在守卫自身的语料里。`ci-validation.md:37` 段末那句「所以把规则改松会先弄红自检」的括号里列的确实只有 mutate/read 形态，字面上不算撒谎，但 `reference-acceptance.md:141` 把同一句紧接在本轮新增的证据规则枚举之后，读者会以为证据规则也被语料锚住了。

---

## 三、假红清单（正确改动会被拦）

| # | 正确改动 | 结果 | 状态 |
| --- | --- | --- | --- |
| 1 | 守卫步骤前置 `: "${VAR:?}"` 保护语句 | `wiring` 红，消息还自称 `: ` 允许 | **本轮新增，未披露，与 `ci-validation.md:35` 和错误信息自身矛盾** |
| 2 | 证据用例加 `@unittest.skipIf(sys.platform == "win32", …)`（Linux CI 必然执行） | `sites` 红 | 本轮新增，已披露 |
| 3 | 证据用例加 `@unittest.skipUnless(sys.platform != "win32", …)` | `sites` 红 | 本轮新增，已披露 |
| 4 | 证据用例加 `@unittest.skipIf(SKIP_ON_WINDOWS, …)`（模块常量） | `sites` 红 | 本轮新增，已披露 |
| 5 | `setUp` 里 `if sys.platform == "win32": self.skipTest(...)` | `sites` 红 | 本轮新增（随类级查询一起），已披露 |
| 6 | 证据类继承别的模块的 `TestCase` 基类 | `sites` 红，且消息只给「不存在 / 不在 TestCase 类里 / 不在 test*.py 模块里」模糊原因 | 本轮新增，已披露 |
| 7 | `@unittest.skipIf(0, …)`（`0` 是真值假、但 `isinstance(0, bool)` 为 False） | `sites` 红（实测） | 细节：只认 `True`/`False` 两个字面量 |
| 8 | 守卫步骤写成 `python3 -u scripts/dev-env/ci-entry-guard.py sites` / `uv run python3 …` | `wiring` 红（`不是直接执行的命令`） | 设计如此，消息可读 |
| 9 | 合法新增 `read` argv 用 f-string | `sites` 红 | 旧问题，未处理 |
| 10 | 合法新增项目内只读助手 `ctx.read(new_ps_argv(ctx))` | `sites` 红 | 旧问题，未处理 |
| 11 | 入口 `STEPS` 重排 | `log` 红（`sites`/`probe`/`wiring` 绿） | 设计如此 |
| 12 | `probe` 硬签名演进（新增必填位置参数） | `probe` 红，消息点名同步登记 | 可接受 |
| 13 | 守卫步骤前置任意条件块 `if [ -z "$X" ]; then exit 1; fi` | `wiring` 红 | 与文档一致（只允许环境前置），但比 `:` 的宣称窄 |

**已消的旧假红**：`set -euo pipefail`（W1）、`@unittest.skipIf(False)`（P1）、业务对象 `.skip()`（X2）、`compose.env.example` 加行、私有目录改名。

---

## 四、橡皮章残余

按「盖一次章要付多少代价」排序：

1. **新增写命令 + 无关证据 + 无关判定指针**：代价 = 一行 `SiteRow` + `PINNED_COUNTS` 改一个数 + `tables --write`。四守卫 + 287 例全绿（R3′ 完整跑通）。**与上一轮同价**，但本轮已把「相关性不可见」写进覆盖边界，所以这是**已披露的边界**而非未兑现的承诺。
2. **同名诱饵放在更早的位置**：把同名断言 `TestCase` 加到**同文件更早的类**里（或 `tests/ci/test_aa_*.py`，`sorted()` 排前），再掏空真证据 → `sites` 绿、`tests/ci` 288 例 OK（D-a / X1）。代价 = 一个 3 行类。第四轮的跨模块诱饵（E10）已关闭，同名解析优先级仍敞着；`_find_test` 取 `sorted(glob)` 中第一个命中的类，而证据列只有一个裸方法名，没有模块/类限定。
3. **证据整类/整方法挂起的新写法**：类体 `test_x = unittest.skip(...)(test_x)`（P7）、`setUp` → 模块级 helper 里的 `skipTest`（P8）。各一行，全绿，且运行时确实 skip。**这是「断言不会被跳过」未能兑现的两种形态。**
4. **`if <非字面量>: return`、`assertIn('a','abc')` 这类恒真断言**：已在「看不见」清单显式举例（P9/P10 实测绿）。
5. **「判定在哪」写任意入口真实名字**：`` `Context` ``、`` `argv` `` 仍绿（第四轮 J5/J6，代码未变），已披露为「只校验存在」。
6. **证据规则改松不会被自检语料挡住**：`SELFTEST_CASES` 20 条不覆盖证据规则；删掉 `expectedFailure` 检查后 `sites` 仍绿，只有 `tests/ci` 红。同一 PR 里连 `tests/ci` 的断言一起删仍然全绿（这是 `tests/ci` 作为第二道锁的固有弱点，上一轮已列）。
7. **行数锚点 + `tables --write` 缩表/改词**：`WRITE_TOKENS` 去词、`MUTATE_SITES` 换行不改数，三守卫全绿（上一轮 4′/R2′，本片未动这块），作者已在 `ci-validation.md:41` 写明取舍。

**守得住的部分（本轮逐条试过，全红）**：裸语句丢结果、`_ =` 丢结果、`read` 藏变量、`read` 未登记 `*` 展开、`read` 带写词、`read` 用未登记助手、命令消失、命令新增未登记、`probe` 集合不一致、`probe` 判定函数被掏空、`wiring` 换 `echo`、`wiring` 前面插真实命令、文档锚点不一致、自检语料失真、**证据方法/类/同文件基类 skip、`expectedFailure`、同名重复定义、`if True: return`、`assertTrue(True)` 系、吞断言、非 `test*.py` 诱饵**（本轮新增的四类全部落在这一栏）。

---

## 五、我独立复现的变异

全部在 `/private/tmp/sn679-mut`（同级目录，非 worktree 内），每例前从 `/private/tmp/sn679` `rsync -a --delete` 复原；涉及登记表的例在改常量后跑 `tables --write`。**未 push 任何变异。**

### 5.1 四类新查询（条目 1）

| ID | 变异 | `sites` | 消息位置 |
| --- | --- | --- | --- |
| A | 证据类 `@unittest.skip` | **红** | 类行 + 「类 `BucketAndGateParsingTest` 上」 |
| B | 证据类 `setUp` 里 `skipTest` | **红** | 方法行 + 「`…setUp` 里的 `self.skipTest(...)`」 |
| C | 同文件基类 `setUp` 里 `skipTest` | **红** | 方法行 + 「`EvidenceBase.setUp` 里的 …」 |
| D | 方法 `@unittest.expectedFailure` | **红** | 方法行 |
| E | 同名方法定义两次 | **红** | 「定义了 2 次（行 1107、1117）」 |
| F | `assertTrue(True)` | **红** | 方法行 |
| G | `if True: return` | **红** | 「在前面 `if True:` 早退」 |
| H | `try/except AssertionError: pass` | **红** | 「会吞 `AssertionError` 的 try/except」 |

### 5.2 `_find_test` / 基类（条目 2、4）

| ID | 变异 | 结果 |
| --- | --- | --- |
| — | 逐行解析 8 条登记的 `_find_test` 结果 | 7 个唯一名全部命中 `test*.py` 的 `TestCase` 类，无一条被收紧打掉 |
| D-a | 同名断言诱饵放进 `tests/ci/test_aa_decoy.py` + 真证据掏空 | `sites` **绿**、`tests/ci` 288 例 **OK**（残余） |
| D-b | 同诱饵放进 `test_zz_decoy.py`（排在真模块之后） | `sites` **红**（真证据无断言） |
| D-c | 同名断言诱饵放进 `tests/ci/helper_evidence.py`（不被收集）+ 真证据掏空 | `sites` **红**（`test*.py 模块里`） |
| X1 | 同文件里更早一个同名 `TestCase` 诱饵 + 真证据掏空 | `sites` **绿**、`tests/ci` 288 例 **OK**（残余） |
| L1 | 合法重构：抽同文件基类 + 有内容的 `setUp` | **绿** |
| L2 | 证据类继承别的模块的基类 | **红**（消息未点明跨模块基类） |
| — | `rg` 全仓 `expectedFailure`/`skipIf`/`skipUnless`/`skipTest` | `tests/ci` 真用例零使用，仅出现在 fixture 字符串里 → 判红无假红 |

### 5.3 假红与预置跳过（条目 3）

| ID | 变异 | 结果 |
| --- | --- | --- |
| P1 | `@unittest.skipIf(False, …)` | **绿** |
| P2 | `@unittest.skipUnless(True, …)` | **绿** |
| P3 | `@unittest.skipIf(sys.platform == "win32", …)` | **红（假红）** |
| P4 | `@unittest.skipUnless(sys.platform != "win32", …)` | **红（假红）** |
| P5 | `@unittest.skipIf(SKIP_ON_WINDOWS, …)`（模块常量 `False`） | **红（假红）** |
| P6 | `@unittest.skipUnless(not SKIP_ON_WINDOWS, …)` | **红（假红）** |
| P7 | 类体 `test_x = unittest.skip(...)(test_x)` | **绿**，运行时 `OK (skipped=1)`（残余） |
| P8 | `setUp` → 模块级 helper 里的 `skipTest` | **绿**，运行时 `OK (skipped=1)`（残余） |
| P9 | `if sys.platform == "win32": return` | **绿**（已披露） |
| P10 | `self.assertIn('a', 'abc')` | **绿**（已披露） |
| L3 | `setUp` 里平台条件 `self.skipTest` | **红**（假红，已披露） |
| X2 | 正文调业务对象 `.skip(...)` | **绿**（第四轮假红已消） |
| W1 | 前置 `set -euo pipefail` | **绿**（第四轮假红已消） |
| W2 | 前置 `set -e` + `cd` + `export` | **绿** |
| **W3** | 前置 `: "${SPEECHNEXUS_ENV:?missing}"` | **红（假红，未披露，与 `ci-validation.md:35` 及错误信息自身矛盾）** |
| W4 | 前置 `umask 022` | **绿** |
| W5 | 前置 `echo "guard start"` | **红**（符合设计） |
| W6 | 前置 `if [ -z "$X" ]; then exit 1; fi` | **红**（符合设计） |
| W7 | `python3 -u scripts/dev-env/ci-entry-guard.py sites` | **红**（消息可读） |
| W8 | `uv run python3 scripts/dev-env/ci-entry-guard.py sites` | **红**（消息可读） |

### 5.4 看不见清单与端到端（条目 5、6）

| ID | 变异 | 结果 |
| --- | --- | --- |
| I1 | `if result.ok: raise`（反转判定） | **绿**（清单成立） |
| I2 | `print(result.ok)` 不中断 | **绿**（清单成立） |
| I3 | `ctx.read(("psql","-c","DELETE …"))` | **绿**（清单成立） |
| I4 | `other = ctx; other.mutate(("docker","compose","rm","-f","api","gateway"))` | **绿**（清单成立） |
| I5 | `getattr(ctx,"mutate")(...)` | **绿**（清单成立） |
| I6 | `subprocess.run(["docker","compose","rm","-f","api"])` | **绿**（清单成立） |
| R3′ | 入口加真实 `docker compose rm -f api gateway` + 无关判定/证据 + `PINNED_COUNTS` 8→9 + `tables --write` | 四守卫 **绿**（`sites: 9 registered`）、`tests/ci` 287 例 **OK** |
| — | 删掉 `_skip_decorator_reason` 的 `expectedfailure` 分支（把规则改松） | `sites` **绿**（自检语料不覆盖证据规则）、`tests/ci` **1 例红** |

### 5.5 成本与门禁

| 项 | 实测 |
| --- | --- |
| `sites` / `probe` / `wiring` | 0.50–0.52 s / 0.10 s / 0.04 s（合计 0.64 s，`< 2 s` 成立） |
| `tests/ci` | 287 例 6.26 s（父提交 272 例 5.83 s） |
| 真实 CI | `gh run view 43441176316`：`reference_acceptance` **11 s**，全 run 绿 |
| 受保护状态名 | 三个，未变；`Gate` 的 `needs` 含 `reference_acceptance`（`ci.yml:732`） |

---

## 六、这条守卫现在到底守住了什么（给非作者看）

一句话：**它现在能保证「登记在册的每条写命令还在、每条证据用例都真的会被 `unittest` 收进来、真的会跑、断言不是一眼就假的那种」，不能保证「这条证据和它守护的那条命令有任何关系」。**

具体说：它把入口 `reference-acceptance.py` 里每个 `<ctx>.mutate(...)` 与六张登记表对表，任何一条命令消失或新增而没登记，红；`ctx.read(...)` 的 argv 必须能静态看出是只读的，藏变量、用未登记的助手、带写词，红；四条守卫子命令必须在 workflow 里被真实执行（`echo` 冒充、前面先跑别的命令，红）。证据这一列本轮被显著加固：指名的用例必须落在 `tests/ci/test*.py`、方法名 `test` 开头、所在类沿同文件基类能追到 `TestCase`（跨模块基类认不出，红）；同一个类里同名方法定义两次红；方法、类、同文件基类上的 `skip`/`expectedFailure`、`setUp`/`setUpClass` 里的直接 `skipTest`、开头或字面量恒真分支里的 `return`、没有任何断言、断言是守卫认得出的字面量恒真形态、断言被 `try/except AssertionError` 吞掉，全红。

它**仍然看不见**：判定条件写反但还读 `.ok`、把结果读出来只打日志、经 `psql -c` 间接写库、换接收者名或包进自定义函数丢掉 `ctx.mutate` 的返回值、反射拿 `mutate`、`subprocess`/`os.system`——以及**最要紧的一条：证据和站点的相关性**。所以「登记」到现在仍然不是「证明」，它是一张**形式完整的清单**：改代码就能让守卫红，但想盖章也只需要一行登记、一个存在的判定函数名、一个存在的测试方法名、改一个数字、跑一条生成命令——四守卫和全部 287 例测试/ci 都会绿。**这个守卫的价值在于「悄悄消失会被拦」，不在于「偷偷加上会被拦」，也不在于「登记的内容是真的」。** 本轮把这句话写进了文档，这是它比上一轮诚实的地方；剩下的问题是同一句话在 `ci-validation.md:41` 还留着强版本，以及 `: ` 那个允许项根本没实现。

---

## 七、只能依赖作者陈述的点

1. **真实 apply 运行**：本机没有真实的 PostgreSQL/MinIO/Docker apply 环境，「语义由 `tests/ci` 的假 Runner 用例与真实 apply 运行负责」这条分工，我只核到「分工写在文档里、`tests/ci` 确实在跑」，没有验证 apply 路径的实际覆盖。
2. **`log` 子命令**：需要真实 dry-run 日志（`--log` 参数），我的 R3′ 与其余探针都没有产生真实日志；我核到的是「`log` 的输入未被本片改动影响」以及「入口 `STEPS` 重排会让 `log` 红」这一分支。
3. **GitHub Actions 上 `setup-uv` / `docker compose config` 的行为**：我只读了真实 run 的结果与耗时，没有在 Actions 上替换 workflow 复现变异；变异全部在本机 Python 3.14.9 + 本地文件副本上完成。
4. **第四轮列出的旧代码路径**：`_skip_marks` 旧版只看 `node.decorator_list`、`_find_test` 旧版用 `glob("*.py")` 等，我引用的是第四轮报告与本轮 diff，没有把 `54bf6162` 的守卫拿来逐条重跑（只用它做了用例数对照）。
5. **`<name>.example.json` 对非 JSON 挂载的命名**：`ci.yml:1016` 是 `stem = name.removesuffix(".json")`，所以 `silero_vad.onnx` 会要求 `silero_vad.onnx.example.json`；文档新加的括注「非 JSON 挂载同样按 `<name>.example.json` 命名」与代码一致，但我没有真的提交一个非 JSON 挂载来验证整条链路。这一条读代码即可确认，属于低风险的未实测项。

---

## 八、时间线

| 时点 | 动作 | 结果 |
| --- | --- | --- |
| T0 | 核对 `origin/main`；`git worktree add /private/tmp/sn679 -b review/679-guard-evidence origin/main` | HEAD = `4cfdfe77`，工作区干净 |
| T1 | 读第三、四轮审查报告（282 + 441 行）与本轮全量 diff（5 文件 +517/−133） | 定位三类改动：证据查询扩展、`_find_test` 收紧、`wiring` 前置放宽 |
| T2 | 跑基线：`sites`/`probe`/`wiring`；`tests/ci`；`git archive 4cfdfe77~1` 对照 | 全绿；0.50/0.10/0.04 s；287 例 6.26 s；父提交 272 例 |
| T3 | 条目 1：A–H 八组变异 | 全部红，消息位置正确 |
| T4 | 条目 2：解析 8 条登记的证据解析结果；D-a/D-b/D-c/L1/L2 | 现有登记全满足；跨模块诱饵关、同名更早诱饵仍开；跨模块基类红 |
| T5 | 条目 3：P1–P10、L3、X2、W1–W8 假红探针 | 平台条件跳过 4 例全红（已披露）；P7/P8 残余；**W3 `:` 假红且与文档矛盾** |
| T6 | 条目 4：`rg` 全仓 `expectedFailure`/`skip*` | `tests/ci` 真用例零使用 → 无假红 |
| T7 | 条目 5：R3′ 端到端盖章 | 四守卫 + 287 例全绿（已披露边界内） |
| T8 | 条目 6：I1–I6 看不见清单 + CI 注释 + `_` 措辞核对 | 清单 11 项全部成立；`后不再使用` 已删；`: ` 不一致；`这条证据会真的判红` 仍在 |
| T9 | 条目 7、8：`gh` 只读核对门禁/时长；守卫计时；自检语料覆盖探针 | 门禁与 11 s 属实；`sites` 0.50 s；证据规则不在自检语料里 |
| T10 | 写本报告、`uv run --no-project ruff format`、提交、开 PR | — |
