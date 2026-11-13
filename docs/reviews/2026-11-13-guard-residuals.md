# 验收入口结构守卫「残留返修」的非作者审查（#672 / #572 第四轮）

- **审查对象**：`54bf6162`（`fix: 收口验收入口结构守卫的残留承诺与橡皮章路径 (#572) (#672)`），已在 `main`。
- **基线核验**：建 worktree 前主检出的 `origin/main` 停在 `97cc9ea2`；`git fetch origin` 后 `origin/main` 前进到 `54bf6162`，与本轮基线一致（`git rev-parse origin/main` → `54bf6162e4…`）。
- **独立 worktree**：`/private/tmp/sn676`，分支 `review/676-guard-residuals`（`git worktree add /private/tmp/sn676 -b review/676-guard-residuals origin/main`）。变异实验在**同级**目录 `/private/tmp/sn676-mut`（每例前 `rsync -a --delete --exclude .git` 从 `sn676` 复原），**未 push 任何变异**；另有 `/private/tmp/sn676base`（`git archive 54bf6162~1`）用于上一提交的用例计数对照。
- **审查者**：非作者。上一轮报告 [2026-11-13-structure-guard.md](2026-11-13-structure-guard.md)（合并于 `eace178e`）是本轮的验收基线。
- **本片改动范围**：6 个文件，+488/−70。`ci.yml`（+29/−24，全在注释块与「Prepare the acceptance environment」一步内）、`ci-entry-guard.py`（+196/−24）、`tests/ci/test_ci_entry_guard.py`（+114/−1）、`tests/ci/test_ci_private_inputs.py`（+119/−0，新增）、`reference-acceptance.md`（+26/−17）、`ci-validation.md`（+4/−4）。产品代码零改动。
- **产品代码与 CI 零改动**：本报告是唯一新增文件。

---

## 结论

**有条件通过。**

**四条点名项里，三条改到位、一条只堵住了它字面列出的形态。** 未披露的门禁松动已恢复为硬失败，并且有行为级回归测试（我把它改回 `::warning::` + 合成 `{}`，`tests/ci` 立刻 2 例红）；`_ = ctx.mutate(...)` 与未登记的 `*argv` 展开现在判红；边界举例改对了方向。

**但第四条的文档表述跑在了实现前面，而这正是本轮要警惕的两件事。** 本轮新增的两句承诺——「证据列必须指向一个**会被 `unittest` 收集**的 `TestCase` 方法」「该用例**不带 skip 装饰器/`skipTest`**」——实测都有反例，而且是**整条 CI 全绿**的反例：

| 形态 | `sites`/`probe`/`wiring` | 完整 `tests/ci`（247 例） |
| --- | --- | --- |
| 证据类加**类级** `@unittest.skip` | **绿** | **绿**（`OK (skipped=1)`） |
| 证据类 `setUp` 里 `self.skipTest()` | **绿** | 绿 |
| 证据方法定义两次（断言版在前，`return` 版在后——运行的是后者） | **绿** | 绿 |
| 在 `tests/ci/error_codes.py`（`unittest discover` 不收）里放一个同名断言 `TestCase`，真证据掏空 | **绿** | **绿**（`OK`） |
| 证据方法加 `@unittest.expectedFailure` 且断言故意失败 | **绿** | 绿（`OK (expected failures=1)`） |

同一类问题还出现在「判定在哪」列：校验只要求反引号里是入口**真实存在**的任意定义，写 `` `Context` ``（一个与闸门无关的类）或 `` `argv` ``（函数内局部变量）都是绿。文档在覆盖边界里承认「不证明描述属实」，所以这一条算**已披露的弱校验**；但上面五条不属于「相关性不可见」——它们直接违背了「会被收集」「不带 skip」「每次 CI 都会真的判红」这三句。

**没有发现守卫规则被放宽。** 逐条核对：`SELFTEST_CASES` 只增不减（17→20）、`PINNED_COUNTS` 只有 `READ_ARGUMENTS` 3→4 与 `SELFTEST_CASES` 17→20（都是跟着新增走）、`tests/ci` 只删了一行断言文本（`不存在的 tests/ci 用例` → `不是 `TestCase` 方法`，两个分支仍都红）。唯一一处「松」是**叙事松**：把没做到的写成做到了。

**建议的最低必改**（都不重设计架构）：

1. `_skip_marks`（`ci-entry-guard.py:895`）把 `_find_test` 返回的**类节点**一起看：类上的 `@skip*` 装饰器、类体内 `setUp`/`setUpClass` 里对 `skipTest`/`SkipTest` 的调用，与现有方法级检查同等判红。
2. `_find_test`（`ci-entry-guard.py:866`）只匹配 `unittest discover` 真会收的文件（`test*.py`），或把文档的「会被 `unittest` 收集」改成「tests/ci 下任意模块里的 `TestCase` 方法」。
3. `ci-validation.md:33` 与 `reference-acceptance.md:134-136` 的「赋给 `_` 后不再使用」删掉「后不再使用」：实现是**只要赋给 `_` 就红**，之后再读 `_` 反而变成假红。

**不阻塞但应记入覆盖边界**：类级 skip / `setUp` skip / 重复定义 / `expectedFailure` / `if True: return` / 非 `test*.py` 模块里的诱饵——「证据是不是真证据」仍然只到「有断言且不是方法级 skip」这一层。

---

## 时间线

| 时点 | 动作 | 结果 |
| --- | --- | --- |
| T0 | `git fetch origin` | 主检出基线的 `origin/main` 从 `97cc9ea2` 前进到 `54bf6162`；`git cat-file -t 54bf6162` → `commit` |
| T1 | `git worktree add /private/tmp/sn676 -b review/676-guard-residuals origin/main` | HEAD = `54bf6162`，工作区干净 |
| T2 | 读全量 diff（6 文件）与上一轮报告 | 定位四类改动：私有输入失败恢复、`_ =`/`*` 展开、边界举例、证据/判定列校验 |
| T3 | 跑基线 `sites`/`probe`/`wiring`、`tests/ci`、`tests/contract` | 全绿：`sites` 0.23 s、`probe` 0.11 s、`wiring` 0.04 s；`tests/ci` 247 例 4.85 s；`tests/contract/test_ci_workflow.py` 249 例 1.56 s |
| T4 | 上一提交用例数对照（`git archive 54bf6162~1`） | 233 例 → 247 例，作者「233 → 247」属实 |
| T5 | 条目 1 复现：抽取 `ci.yml` 的 inline Python，在沙箱里跑四种场景 | 基线绿、缺示例红、新增无示例挂载红、无私有挂载红 |
| T6 | 条目 1 反向：把 `raise SystemExit` 改回 `::warning::` + `{}` | `tests/ci` 2 例红；改成「静默合成、不带 warning 字样」仍 1 例红（行为级断言） |
| T7 | 条目 2 探针：20 余种 `mutate`/`read` 形态直调 `audit_sources` | `_ =` 红、未登记 `*argv` 红、`*ctx.compose_argv` 绿；另有 6 种包起来的丢弃仍绿 |
| T8 | 条目 3 探针：正确/反转 `.ok`、`.code`、只打日志、删整段守卫 | 五例全绿，与文档新表述一致 |
| T9 | 条目 4 变异 E1–E12（掏空 / skip / `expectedFailure` / 类级 / `setUp` / 重复定义 / 诱饵模块 / 吞断言） | 方法级三种红；类级、`setUp`、重复定义、诱饵模块、`expectedFailure`、`if True: return` 六种**全绿**（含完整 `tests/ci`） |
| T10 | 「判定在哪」探针 J1–J7 + 重复行端到端 | 空/无声明/不存在→红；`` `Context` ``/`` `argv` ``→绿；重复行→红 |
| T11 | 橡皮章端到端 R3′：入口新增 `docker compose rm -f api gateway` + 登记（判定写 `` `step_gate` ``、证据写 `test:test_the_contract_passes`）+ `tables --write` | **四守卫 + 完整 `tests/ci` 全绿** |
| T12 | 假红探针：`set -euo pipefail`、f-string argv、`skipIf(False)`、`.skip()` 调用、步骤重排、软/硬签名、目录改名、`compose.env.example` 加行 | 见「假红清单」 |
| T13 | 互锁复现：`web` 分支真值表变异（`checks=skipped; coordination=success`） | `tests/ci` 1 例红、`tests/contract` 7 例红 |
| T14 | 只读核对真实 CI | PR #672 全绿；`Deploy: reference acceptance entry` 10 s（守卫步合计 ~1 s）；`python` 门禁 `needs` 含 `reference_acceptance`；三个受保护状态名未变 |
| T15 | 写本报告、`ruff` 校验、提交、开 PR | — |

---

## 一、四条逐条判定

### 1. 未披露的门禁松动：**改到位**

改动：`ci.yml` 的「Prepare the acceptance environment」把 `::warning::` + 合成 `{}` 恢复为

```python
missing.append(f"compose 挂载 {name}，但缺少 {example.as_posix()} 示例")
...
raise SystemExit("; ".join(missing))
```

不再写任何占位文件。新增 `tests/ci/test_ci_private_inputs.py`（5 例）把这层钉住。

**我独立复现（抽 `ci.yml` 里的 inline Python 到沙箱跑，不是跑作者的测试）**：

| 场景 | 退出码 | 消息 |
| --- | --- | --- |
| 基线（3 个挂载全有示例） | 0 | `private inputs: [… 4 files]` |
| 删 `deploy/private/gateway-file-asr.example.json` | **1** | `compose 挂载 gateway-file-asr.json，但缺少 deploy/private/gateway-file-asr.example.json 示例` |
| `compose.yaml` 新增一个 `orphan.json` 挂载、不交示例 | **1** | `compose 挂载 orphan.json，但缺少 deploy/private/orphan.example.json 示例` |
| compose 里删掉所有 `/run/speechnexus-private` 挂载 | **1** | `deploy/compose.yaml 没有挂载任何 /run/speechnexus-private 文件…` |
| 缺示例但 exit 0 | 不可能 | 已无写入占位的分支 |

**反悔测试是否真的挡得住（这是上轮点名「修一处松一处」的核心）**：

| 变异 | 结果 |
| --- | --- |
| 原样恢复上一版的 `::warning::` + 写 `{}` 占位 | `tests/ci` **2 例红**（`test_a_mount_without_a_committed_example_fails_the_job`、`test_a_placeholder_is_never_synthesized`） |
| 静默合成占位（不打印 `::warning::`，只 `print("note: …")` 后 `continue`） | **1 例红**（行为级 `returncode != 0` 断言命中） |

第二种是我特意挑出来试探「是不是只靠字符串匹配 `::warning::` 才红」的，答案是**不是**——主断言是退出码，这个设计对。仓库内三个挂载与示例文件一一对应，未引入假红。

### 2. 三处正面承诺：**改到位（另有两处残余，见 §2 与 §7）**

**(a) `_ = ctx.mutate(...)`。** `audit_sources`（`ci-entry-guard.py:694-712`）新增 `elif isinstance(parent, ast.Assign) and any(target.id == "_")`。实测：

| 形态 | 结果 |
| --- | --- |
| `_ = ctx.mutate(('docker','compose','up','-d'))` | **红**（消息：`结果被赋给 `_` 后丢弃…`） |
| `_ = x = ctx.mutate(...)`（链式赋值） | **红** |
| `(ctx.mutate(...),)` / `[ctx.mutate(...)]` 裸表达式 | 绿（父节点是 Tuple/List，不是 `ast.Expr`） |
| `ctx.mutate(...) if cond else None` / `... or None` | 绿 |
| `(_r := ctx.mutate(...))` | 绿 |

后四种都是「结果仍被丢弃」，但都是不自然写法；真实代码里被丢结果的两条路径（裸语句、`_ =`）都已判红。**判断：(a) 改到位**，残余属于文字层面的边界列举（见 §2）。

**(b) 星号展开。** `_argv_tokens`（`:605`）的 `ast.Starred` 分支改为「只接受登记为 `spread` 的名字」，并在 `READ_ARGUMENTS` 新增一行 `compose_argv`。实测（直接调 `audit_sources`，`baseline=()`，排掉「基线站点消失」的干扰）：

| 形态 | 结果 |
| --- | --- |
| `ctx.read((*argv,))`（`argv` 是本地元组） | **红**：`无法静态确定（*argv）` |
| `ctx.read(*argv)`（星号作为唯一实参） | **红** |
| `ctx.read((*(('sh','-c','rm')),))`（展开一个字面量元组） | **红** |
| `ctx.read((*ctx.extra_argv, 'ps'))` | **红** |
| `ctx.read((*ctx.compose_argv, 'compose', 'down'))` | **红**：`read 携带写命令（down）` |
| `ctx.read((*ctx.compose_argv, 'rm', '-f', 'api'))` | **红**：`read 携带写命令（rm）` |
| `ctx.read((*ctx.compose_argv, 'ps'))` | 绿（合法形态） |
| `ctx.read((*ctx.compose_argv, str(ctx.x)))` | 绿（`str` 是已登记的词包装） |
| `ctx.read((*local_compose_argv,))`，其中 `local_compose_argv = ('sh','-c','rm -rf /')` | **绿** |
| `ctx.read((*attacker.compose_argv, 'ps'))` | **绿** |

最后两行是本条唯一实质残余：`_spread_name`（`:635`）只取**名字**（`Name.id` / `Attribute.attr`），不看接收者、也不看本地是否有同名定义。也就是说，任意函数内局部变量或任意对象的同名属性绰号 `compose_argv` 都会被当成「登记的只读段」。这与既有 `psql_argv`/`provision_argv` **builder** 行的信任级别相同（builder 也只看名字，不看函数体），所以**不是本轮引入的新洞**；但文档把它称为「登记的只读段」，读者会以为守卫看过这个段的内容。**判断：(b) 字面要求改到位，名字级信任是残留。**

**(c) 「登记不是橡皮章」的表述。** 两篇文档都重写为：校验到的是「这条证据每次 CI 都会真的判红」，不是「覆盖了该站点的语义」，并显式把「证据的相关性」列入看不见清单。这个收窄方向是对的，也与我上一轮的建议一致。但「每次 CI 都会真的判红」这句是全轮**最强的一句**，而它恰好被 §4 的五条变异推翻。**判断：(c) 收窄到位，但新句子仍然强于实测**（这是本轮唯一的「修了但引入新问题」，归入条目 4）。

### 3. 边界举例写反：**改到位**

`ci-validation.md:41` 现在写：「判定条件写反方向却仍读 `.ok`／`.code`（正确写法是 `if not started.ok: raise`，改写成 `if started.ok: raise` 仍绿）」。实测四种：

| 形态 | 结果 |
| --- | --- |
| `if not started.ok: raise`（入口里的正确写法） | 绿 |
| `if started.ok: raise`（写反方向） | 绿 |
| `if started.code == 0: raise` | 绿 |
| 读出 `.ok` 只 `print`，不中断 | 绿 |

举例方向与「正确/反例」的措辞都对上了。这是上一轮报告里「读者会把正确写法当反例」的直接修复，**核实通过**。

### 4. 橡皮章路径：**修了但引入新问题（关键发现）**

已堵住的（我逐条重放上一轮的 R1/skip/R3/dup）：

| 形态 | 结果 |
| --- | --- |
| 证据方法首行插 `return`（R1 原样） | **红**：`在前面 `return` 早退，剩下的正文是死代码` |
| 证据方法整体替换为 `return` | **红**：`正文没有任何断言` |
| 方法级 `@unittest.skip` | **红**：`被 skip 掉了：unittest.skip('parked')` |
| 方法体内 `self.skipTest(...)` | **红** |
| 证据名不存在 / 不是 `TestCase` 方法 | **红** |
| 同一站点重复登记两行（判定互相矛盾） | **红**：`登记了多次` |
| 「判定在哪」无反引号（「谁也没判」） | **红**：`没有反引号指向入口定义的名字` |
| 「判定在哪」写了不存在的定义 | **红**：`没有定义它` |

**仍绿且能盖章的（全部在完整 247 例 `tests/ci` 下复验过）**：

| 编号 | 形态 | 为什么绿 |
| --- | --- | --- |
| E7 | `@unittest.skip("parked")` 加在 **TestCase 类**上 | `_skip_marks` 只看 `node.decorator_list`（方法自己的装饰器），不看类 |
| E8 | 类里加 `def setUp(self): self.skipTest("parked")` | 同上，只扫证据方法的方法体 |
| E9 | 同名方法定义两次：断言版在前、`return` 版在后 | `_find_test` 返回 `node.body` 里**第一个**同名成员；Python 运行时用**最后一个** |
| E10 | 在 `tests/ci/error_codes.py` 追加 `class DecoyEvidence(unittest.TestCase)` 并实现同名断言方法，真证据掏空为 `return` | `_find_test` 用 `glob("*.py")`，包含 `unittest discover` 不收的辅助模块；`sorted()` 下 `error_codes.py` 排在前，先被找到 |
| E5 | 证据方法加 `@unittest.expectedFailure`，断言故意写错 | 装饰器名不含 `skip`；运行时 `OK (expected failures=1)` |
| E2 | 正文改成 `if True:` / `return` / 一句断言 | 早退检查只认**直接**写在 `body[:-1]` 里的 `Return`/`Raise`，包进 `If` 就看不见；断言检查只问「有没有」，`assertTrue(True)` 也算 |
| E6 | 断言包在 `try/except AssertionError: pass` 里 | `_assertion_line` 只问有无 |

**这四条里 E7/E8/E10 最值得警惕，因为它们不是刻意作弊**：重构时「先把这个测试类整体挂起」「给这个类加个 setUp 跳过条件」「把测试夹具抽到辅助模块」都是常见动作，而每一个都会让「证据」悄悄变成 no-op 并保持全绿。

**印证：端到端橡皮章 R3′ 仍然盖得下去。** 我在入口 `step_gate` 里加一条真正的破坏性写命令，并按「标准动作」登记：

```python
removed = ctx.mutate(("docker", "compose", "rm", "-f", "api", "gateway"))
print(removed.ok)
```

```python
(
    SiteRow(
        "step_gate:('docker', 'compose', 'rm', '-f', 'api', 'gateway')",
        "`step_gate` 判",
        "test:test_the_contract_passes",
    ),
)
```

（`PINNED_COUNTS["MUTATE_SITES"] 8→9` + `tables --write`）

结果：`sites: OK (sites: 9 registered)` / `probe: OK` / `wiring: OK` / `tests/ci` **`OK`（247 例）**。`test_the_contract_passes` 是守卫自己的自检用例，就在 `tests/ci/test_ci_entry_guard.py:532`，现成的、会被收集的、有断言的、未 skip 的——完全满足本轮的每一条新增校验。**「登记不是橡皮章」的距离仍然是：一个存在的用例名 + 一个入口里存在的定义名。**

顺带记录：这条新写命令在 `tests/ci` 里没被捉住，是因为 `step_gate` 的 `ctx.mutate` 在 dry-run 下不可达（假 Runner 走 dry-run 分支）。所以「新增站点」这条路径上，`tests/ci` 不构成第二道锁。

---

## 二、「修一处松一处」清单

**结论：没有发现守卫规则被放宽。** 逐项核对：

| 检查点 | 结论 |
| --- | --- |
| `SELFTEST_CASES` 是否删过用例 | 没有。17→20，只增 3 条（`read-starred-unregistered`、`read-starred-registered`、`discarded-to-underscore`），diff 里没有删除行 |
| `PINNED_COUNTS` 是否偷偷调低 | 没有。`READ_ARGUMENTS` 3→4、`SELFTEST_CASES` 17→20，均与新增行数一致；`MUTATE_SITES` 仍 8、`WRITE_TOKENS` 仍 7、`PROBE_CASES` 仍 9 |
| `READ_ARGUMENTS` 新增 `compose_argv` 是否让 builder 检查变松 | 没有。顶层实参是 `Call` 时仍要求 kind == `builder`；`spread` 只在 `ast.Starred` 分支生效。`ctx.read(ctx.compose_argv)`（顶层的属性）仍红，`*psql_argv`（用 `*` 展开 builder 名）仍红 |
| `_ =` 的新检查是否让裸语句检查失效 | 没有。`if isinstance(parent, ast.Expr)` 分支原样保留，新分支是并列的 `elif` |
| `_find_test` 改成 AST 扫描是否放宽 | 反倒收紧：上一版用 `f"def {name}(" in text` 子串搜索，连模块级函数都算「存在」；现在要求是 `TestCase` 子类的方法 |
| `tests/ci` 是否弱化了其它断言 | 只删了一行：断言从「不存在的 tests/ci 用例」改成「不是 TestCase 方法」。因为两个分支现在共用同一句消息，这条断言不再区分「名字不存在」和「不是 TestCase 方法」——但两者都仍红，不构成放宽 |
| `ci.yml` 除私有输入外是否还有别的放松 | 没有。diff 的 3 个 hunk 全在注释块与那一步的 inline Python 内；routing、gate 真值表、job `needs` 未动 |

**找到的「松」不在规则里，在叙事里 —— 3 处把没做到写成做到：**

1. `ci-validation.md:33`、`reference-acceptance.md:134`、`ci.yml:859` 都说证据是「**会被 `unittest` 收集**的 `TestCase` 方法」。实测 E10：`tests/ci/error_codes.py` 里的同名 `TestCase` 会被认作证据，而 `unittest discover -s tests/ci`（默认 pattern `test*.py`）**不会**收集它。`_find_test` 用的是 `glob("*.py")`（`ci-entry-guard.py:866`）。
2. 两篇文档都说该用例「**不带 skip 装饰器/`skipTest`**」、覆盖边界说「未被跳过」。实测 E7（类级 `@unittest.skip`）、E8（`setUp` 里 `skipTest`）：**都被跳过，都判绿**。`_skip_marks`（`:895`）只读方法自身的 `decorator_list` 与方法体的语句。
3. 上两条的直接后果：`reference-acceptance.md:136` 的「**校验到的是「这条证据每次 CI 都会真的判红」**」在 E5（`expectedFailure`）、E7、E8、E9、E10 五种形态下都不成立。

**另有两处小项（不影响判定力，但会误导后来者）：**

- `ci-validation.md:33` 与 `reference-acceptance.md:120` 写「结果被丢弃（裸表达式语句，以及**赋给 `_` 后不再使用**）」。实现是**只要赋给 `_` 就红**：`_ = ctx.mutate(...)` 之后 `if not _.ok:` 仍然红。措辞让读者以为「用了就行」，实测是假红。
- `tests/ci/test_ci_entry_guard.py:80-91` 的 `test_a_shrunk_table_without_touching_the_pin_is_red` 里 `shrunk` 字典仍写 `"READ_ARGUMENTS": 3`（实际 4）。该用例不会因此变空转（`assertIn("MUTATE_SITES", problems[0])` 仍要求 `MUTATE_SITES` 是第一条问题），但一旦 `PINNED_COUNTS` 的迭代顺序变化，它就会变成 vacuous。作者改了常量却没同步这处陈旧拷贝。
- `check_judgements` 命中重复行后 `continue`，跳过该行的指针校验。重复行本身已红，实际影响为零。

---

## 三、假红清单（正确改动会被拦）

| # | 正确改动 | 结果 | 备注 |
| --- | --- | --- | --- |
| 1 | 守卫步骤 `run` 第一行加 `set -euo pipefail` | **红**（`wiring`），消息称「被 `echo` 或别的命令包住」 | 上一轮就报过，本轮未修、未写进覆盖边界；消息仍然指向错误原因 |
| 2 | 证据方法加 `@unittest.skipIf(False, …)`（在 Linux 上照跑） | **红**：`被 skip 掉了` | 本轮新增的假红：`skip` 子串匹配不看条件 |
| 3 | 证据正文里调用某个业务对象的 `.skip(...)` 方法 | **红**：`被 skip 掉了` | 本轮新增：`_skip_marks` 对方法体做 `ast.walk`，任何 `.skip` 调用都算 |
| 4 | 合法新增的 `read` argv 里带 f-string（端口、过滤名拼接） | **红**：`无法静态确定` | 上一轮已报，未变 |
| 5 | 合法新增的项目内只读助手 `ctx.read(new_ps_argv(ctx))` | **红**：`未登记的助手` | 必须同时改 `READ_ARGUMENTS` + `doc`（`tables --write`） |
| 6 | `_ = ctx.mutate(...)` 之后继续读 `_` | **红** | 新检查的过度触发，实际几乎不会发生（`_` 的语义就是丢弃） |
| 7 | 入口 `STEPS` 重排 | `sites`/`probe`/`wiring` 绿、`log` **红** | 设计如此（`EXPECTED_STEP_ORDER` 是刻意的第二份副本），一次改动要同步 entry + guard + 文档三处 |
| 8 | `probe` 登记函数的软签名演进（追加 kw-only 默认参数） | **绿** | ✓ 不误报 |
| 8′ | 硬签名演进（新增必填位置参数） | 红，消息点名「签名变化时同步 probe 用例、PROBE_CASES 与文档表」 | 可接受 |
| 9 | 私有输入目录改名（`SPEECHNEXUS_PRIVATE_CONFIG_DIR=…/private2`） | **绿**（`sites`/`probe`/`wiring` + `test_ci_private_inputs.py` 5 例全过） | ✓ 上一轮修掉的硬编码没有回归 |
| 10 | `deploy/compose.env.example` 追加无害行 | **绿** | ✓ 只有 `SPEECHNEXUS_PRIVATE_CONFIG_DIR` 一行被读 |
| 11 | 新增一个**非 JSON** 私有挂载（如 `silero_vad.onnx`） | 需要名为 `silero_vad.onnx.example.json` 的文件 | 派生规则是 `name.removesuffix(".json") + ".example.json"`，对二进制挂载得出的示例名很别扭；文档写 `<name>.example.json` 也没说清 `name` 含不含扩展名 |
| 12 | 删除整个 `wiring` 步骤 | 四守卫「绿」（剩下三步照跑）；`tests/ci` 1 例红 | 与上一轮 C3 相同：守卫不会发现自己消失，第二道锁仍只在 `tests/ci` 里 |

---

## 四、橡皮章残余（重新枚举）

按「盖一次章要付多少代价」排序：

1. **新增写命令 + 无关证据 + 无关判定指针**：代价 = 一行登记 + 改 `PINNED_COUNTS` + `tables --write`。四守卫 + 247 例全绿（§1.4 的 R3′ 完整跑通）。**这是当前最容易的盖章路径。**
2. **证据整类挂起**：类级 `@unittest.skip` 或 `setUp` 里 `skipTest`。代价 = 一行。全绿。且是常见重构动作。
3. **诱饵模块**：把同名断言 `TestCase` 放到 `tests/ci` 下任意非 `test*.py` 模块（`error_codes.py`、`configuration_prerequisites.py` 都已存在），真证据掏空。代价 = 追加 5 行。全绿。
4. **同名方法定义两次**：断言版在前撑过守卫，`return` 版在后被运行时使用。全绿。
5. **`@unittest.expectedFailure`**：断言的语义完全失效，运行时报 `OK (expected failures=1)`。全绿。
6. **`if True: return` / `assertTrue(True)` / 断言包进 `try…except`**：断言存在性检查的绕过。
7. **「判定在哪」写任意入口真实名字**：`` `Context` ``、`` `argv` ``（函数内局部变量）都绿。已在覆盖边界里承认为「不证明描述属实」。
8. **行数锚点 + `tables --write` 缩表/改词**：`WRITE_TOKENS` 去掉 `create`、`MUTATE_SITES` 换一行不改数——三守卫全绿。作者已在 `ci-validation.md:41` 明确写出这条取舍，**属于已披露**。

**未变的部分**（守得住的）：命令消失、命令新增未登记、裸语句丢结果、`_ =` 丢结果、`read` 藏变量、`read` 用未登记 `*`、`read` 带写词、`read` 用未登记助手、`probe` 用例集合不一致、`probe` 判定函数被掏空、`wiring` 把守卫命令换成 `echo`、文档锚点不一致、自检语料失真——这些我都单独试过，全部红。

---

## 五、我独立复现的变异

全部在 `/private/tmp/sn676-mut`（同级目录，非 worktree 内），每例前从 `/private/tmp/sn676` `rsync -a --delete` 复原；涉及登记表的例在改常量后跑 `tables --write` 同步文档块。**未 push 任何变异。**

### 5.1 条目 1（私有输入门禁）

| ID | 变异 | 结果 |
| --- | --- | --- |
| P1 | 沙箱跑 inline Python，缺一个示例 | 退出 1，消息点名示例路径 |
| P2 | 沙箱跑 inline Python，新增无示例挂载 | 退出 1 |
| P3 | 沙箱跑 inline Python，compose 无私有挂载 | 退出 1 |
| L1 | 恢复上一版 `::warning::` + 合成 `{}` | `tests/ci` 2 例红 |
| L2 | 静默合成占位（无 `::warning::` 字样） | `tests/ci` 1 例红（退出码断言） |

### 5.2 条目 2（`_ =` 与 `*` 展开）

| ID | 形态 | 结果 |
| --- | --- | --- |
| D1 | `_ = ctx.mutate(...)` | **红** |
| D2 | `_ = x = ctx.mutate(...)` | **红** |
| D3 | `[ctx.mutate(...)]` / `(ctx.mutate(...),)` | 绿 |
| D4 | `ctx.mutate(...) if c else None` / `… or None` | 绿 |
| D5 | `(_r := ctx.mutate(...))` | 绿 |
| S1 | `ctx.read((*argv,))`（本地元组） | **红** |
| S2 | `ctx.read(*argv)` | **红** |
| S3 | `ctx.read((*(('sh','-c','rm')),))` | **红** |
| S4 | `ctx.read((*ctx.compose_argv, 'compose', 'down'))` | **红**（写词 down） |
| S5 | `ctx.read((*ctx.compose_argv, 'ps'))` | 绿 |
| S6 | `ctx.read((*local_compose_argv,))`，本地同名变量含 `rm -rf /` | **绿（残余）** |
| S7 | `ctx.read((*attacker.compose_argv, 'ps'))` | **绿（残余）** |

### 5.3 条目 3（边界举例）

| ID | 形态 | 结果 |
| --- | --- | --- |
| T1 | `if not started.ok: raise`（正确写法） | 绿 |
| T2 | `if started.ok: raise`（反转） | 绿 |
| T3 | `if started.code == 0: raise` | 绿 |
| T4 | 只 `print(started.ok)`，不中断 | 绿 |
| T5 | 删掉整段 `.ok` 守卫，只留赋值 | 绿 |

### 5.4 条目 4（橡皮章）

| ID | 变异 | `sites` | 完整 `tests/ci` |
| --- | --- | --- | --- |
| E1 | 证据首行插 `return`（R1 原样） | **红**（`死代码`） | — |
| E1′ | 证据整体替换为 `return` | **红**（`没有任何断言`） | — |
| E2 | `if True:` / `return` / 一句断言 | 绿 | — |
| E3 | 整体换成 `self.assertTrue(True)` | 绿 | — |
| E4 | 方法级 `@unittest.skip` | **红** | — |
| E5 | `@unittest.expectedFailure` + 故意失败的断言 | 绿 | 绿（`expected failures=1`） |
| E6 | 断言包进 `try/except AssertionError: pass` | 绿 | — |
| E7 | **类级** `@unittest.skip` | 绿 | **绿（`skipped=1`）** |
| E8 | `setUp` 里 `self.skipTest` | 绿 | 绿 |
| E9 | 同名方法定义两次（`return` 版在后） | 绿 | 绿 |
| E10 | `tests/ci/error_codes.py` 塞同名断言 `TestCase`，真证据掏空 | 绿 | **绿（`OK`）** |
| J1 | 判定列「谁也没判」（无反引号） | **红** | — |
| J2 | 判定列写不存在的 `gate_verdict_removed` | **红** | — |
| J4 | 判定列空串 | **红** | — |
| J5 | 判定列写 `` `argv` ``（局部变量） | 绿 | — |
| J6 | 判定列写 `` `Context` ``（无关类） | 绿 | — |
| DUP | 同一站点两行、判定矛盾 + `tables --write` | **红**（`登记了多次`） | — |
| R3′ | 新增 `docker compose rm -f api gateway` + 无关判定/证据 + `tables --write` | **绿（9 registered）** | **绿（247 例）** |
| R2′ | 把 `step_buckets` 的核对命令换成 `ctx.mutate(("true",))` 并同步登记 | 绿 | **红（3 例）**（假 Runner 覆盖到的站点会被 `tests/ci` 兜住） |
| 4′ | `WRITE_TOKENS` 去 `create` + `PINNED_COUNTS` + `tables --write` | 绿 | 绿 |

### 5.5 假红与互锁

| ID | 变异 | 结果 |
| --- | --- | --- |
| F1 | 守卫步骤前置 `set -euo pipefail` | `wiring` **红**（消息误指 `echo`） |
| F2 | `@unittest.skipIf(False, …)` 加在证据方法 | `sites` **红** |
| F3 | 证据体调用业务对象 `.skip()` | `sites` **红** |
| F4 | `read` argv 用 f-string | `sites` **红** |
| F5 | 私有目录改名 | 绿 |
| F6 | `compose.env.example` 加行 | 绿 |
| F7 | 入口 `STEPS` 把 `tools` 移到最后 | `log` **红**（顺序不符，消息可执行） |
| F8 | `gate_verdict` 追加 kw-only 默认参数 | `probe` 绿 |
| F8′ | `gate_verdict` 追加必填位置参数 | `probe` **红**（点名签名不匹配） |
| W1 | 删掉整个 `wiring` 步骤 | 三守卫绿、`tests/ci` **红**（1 例） |
| X1 | `web` 分支真值表 `checks=skipped; coordination=success` | `tests/ci` **1 红**、`tests/contract` **7 红** |

---

## 六、文档与实现逐句核对

| 位置 | 表述 | 判定 |
| --- | --- | --- |
| `ci-validation.md:27` | 「新增私有挂载必须同时提交 `deploy/private/<name>.example.json`，缺示例时准备步骤直接失败——…所以这里没有降级通道」 | **成立**（P1–P3、L1–L2） |
| `ci-validation.md:33` | 「结果被丢弃（裸表达式语句，以及赋给 `_` 后不再使用）」 | **前半成立**；「后不再使用」与实际相反（D1 之后的读取也红） |
| `ci-validation.md:33` | 「`read` 的 argv … 或 `*` 展开的登记只读段」 | **按名字成立**；S6/S7 说明「只读」未经内容校验 |
| `ci-validation.md:33` | 「「判定在哪」列必须用反引号点出入口里真实存在的定义，改名或删函数即红」 | **成立**（J1/J2/J4 红）；J5/J6 说明只到「存在」 |
| `ci-validation.md:33` | 「「证据」列必须指向一个**会被 `unittest` 收集**的 `TestCase` 方法」 | **不成立**（E10：`glob("*.py")` 收进未收集模块） |
| `ci-validation.md:33` | 「带 skip 装饰器或 `skipTest` …都使作业红」 | **仅方法级成立**（E7/E8 类级与 `setUp` 全绿） |
| `ci-validation.md:33` | 「正文开头就 `return`/`raise` 让后文成死代码」 | **仅直接语句成立**（E2 包进 `If` 即绿） |
| `ci-validation.md:33` | 「正文没有任何断言」 | **成立**，但 `assertTrue(True)` 也算（已被下一条披露） |
| `ci-validation.md:34` | 「`wiring` …把调用换成仍含命令字符串的 `echo` 会红」 | **成立**（D1 类） |
| `ci-validation.md:41` | 举例方向（`if not started.ok: raise` 是正确写法，`if started.ok: raise` 仍绿） | **成立**（T1–T2） |
| `ci-validation.md:41` | 「换接收者名、别名或包进自定义函数再丢弃返回的 `ctx.mutate`」在图不见清单里 | **新增、成立**（上一轮漏项已补） |
| `ci-validation.md:41` | 「证据与被登记站点不相关（只校验它会被收集、有断言、未被跳过）」 | 方向对，但「会被收集 / 未被跳过」这两把尺子本身失效（E7/E10） |
| `ci-validation.md:41` | 「行数锚点挡的是「不知情的缩水」…取舍留给评审」 | **诚实且成立**（4′、R2′ 全绿） |
| `ci-validation.md:41` | 「不证明「判定在哪」那句描述属实」 | **成立**（J5/J6） |
| `reference-acceptance.md:119-121` | 「结果不得被丢弃——裸语句和 `_ =` 都算…`*` 展开的登记只读段」 | 同 `ci-validation.md:33` |
| `reference-acceptance.md:132-136` | 「登记不是橡皮章…该用例不带 skip 装饰器/`skipTest`…校验到的是「这条证据每次 CI 都会真的判红」」 | **最强的一句，且被 E5/E7/E8/E9/E10 推翻**；需修 |
| `ci.yml:859` | 「evidence pointer to a **collected** `TestCase` method that still asserts and is **not skipped**」 | 同上，需修 |
| `ci.yml:878-879` | 「each row must name, in backticks, a definition the entry really has, so a stale or empty "who judges this" cell is red」 | **成立**（J1/J2/J4 红） |
| `ci.yml:885-888` | 「A private mount with no committed example fails the preparation step」 | **成立**（P1–P3） |
| 两篇文档 | 覆盖边界里的 7 项「看不见」 | 上一轮列的 7 项逐项核对**仍然全部成立**，本轮补进「换接收者名/别名/包装」「证据不相关」「行数锚点同改动」三项——都是真的 |

**总评：文档不再有「形状检查说成语义检查」的新增误导**（`does NOT prove a condition is correct` 那段仍准确），但本轮在证据列上新增的三句「会被收集 / 不带 skip / 每次都会真的判红」确实强于实现。这是本轮唯一实质性的文档-实现冲突。

---

## 七、门禁与成本

- **仍在门禁内**：`Gate: required checks for this change` 的 `needs` 含 `reference_acceptance`（`ci.yml:699`，`tests/contract/test_ci_workflow.py:226` 断言）。三个受保护状态名未变（`gh api repos/hrygo/SpeechNexus/branches/main/protection` → `["Gate: required checks for this change", "Web: lint, types, tests, build and accessibility", "Compose: configuration validation"]`）。
- **真实运行**（PR #672 checks）：`Deploy: reference acceptance entry (dry-run execution)` **pass，10 s**（job 内步进 12:58:40→12:58:45，四条守卫步各 0–1 s）。与本轮新增的 `tests/ci` 用例同量级，不是成本问题。
- **守卫本身**（本机 Python 3.14）：`sites` 0.23 s（上一版 0.07 s，多出来的部分是新 `_find_test` 每次 parse `tests/ci` 里的 Python 文件）、`probe` 0.11 s、`wiring` 0.04 s，合计 **0.38 s**。`ci.yml` 注释「entry point plus the four structural guards measured under 2 s」**成立**。
- **新增用例的代价**：`tests/ci` 233 → 247 例（4.85 s），跑在 `changes` 作业（`timeout-minutes: 3`），每个 PR（含 docs 范围）都跑，不落在关键路径上。
- 私有输入门禁从警告改回失败，没有增加运行时间（少写几个占位文件反而更短）。

---

## 八、互锁：`tests/ci` 与 `tests/contract` 的 web 真值表

本 PR **未触碰** routing、gate 真值表与 `needs`（`git show HEAD --name-only` 只有 6 个文件，均不含路由逻辑）。同一变异（`web` 分支改成 `checks=skipped; coordination=success`）在**本轮树**上的实测：

- `tests/ci`：**1 失败**（`test_node_controller_image_ci.TestFinalGate.test_web_scope_expects_python_and_web_success_and_skips_the_rest`）。
- `tests/contract`（`uv run --no-project --with pytest --with pytest-asyncio --with pyyaml --with psycopg … pytest tests/contract/test_ci_workflow.py -q`）：**7 failed, 242 passed**，基线为 **249 passed**。

与上一轮的 1 / 7 完全一致，互锁没有退化。两边仍不是「同构」而是「在当前变异集合上都红」：`tests/ci` 是整表 + 5 个单点错值，`tests/contract` 是 `_STATES × lane` 全参数化，覆盖密度不同。

---

## 九、对自己诚实度

**做对的：** 覆盖边界里「换接收者名、别名或包进自定义函数」「证据与被登记站点不相关」「行数锚点同一次改动仍可行」三项补得到位，且都是真的；「不证明「判定在哪」那句描述属实」这句尤其诚实；提交信息把「证据与被登记站点的相关性、行数锚点 + `tables --write` 的整表缩水仍不可见」写进 `ci.yml` 注释，没有假装解决。

**漏的（8 类，其中前 4 类与本轮新增的正面承诺直接冲突）：**

| 漏项 | 为什么重要 |
| --- | --- |
| 类级 `@unittest.skip` / `setUp` 里 `skipTest` | 与「该用例不带 skip 装饰器/`skipTest`」直接冲突；常见重构动作（E7/E8） |
| `tests/ci` 下非 `test*.py` 模块里的同名 `TestCase` | 与「会被 `unittest` 收集」直接冲突（E10） |
| 同名方法定义两次 | 守卫读第一个，运行时用最后一个（E9） |
| `@unittest.expectedFailure` | 断言语义整体失效，运行时报「通过」（E5） |
| `if True: return`、`assertTrue(True)`、断言包进 `try/except` | 「掏空」的其它写法（E2/E3/E6） |
| `_ =` 之外的丢弃包装（列表/元组字面量、条件表达式、海象） | 与「结果被丢弃」的直觉不符（D3–D5） |
| `*` 展开按名字信任（本地同名变量、任意对象同名属性） | 「登记的只读段」不含内容校验（S6/S7） |
| `wiring` 自身消失只由 `tests/ci` 守护（而 `tests/ci` 可被同 PR 掏空） | 上一轮已列，本轮未变（W1） |

另外提交信息的「五条反例」实际列了六项（缺示例、`_ =`、`(*argv,)`、证据首行 `return`、`@unittest.skip`、判定列掏空）——数量口误，逐项我都复现为红，不是虚假陈述。

---

## 十、只能依赖作者陈述的点

1. **真实 apply 运行**：本机没有真实的 PostgreSQL/MinIO/Docker apply 环境，`--dry-run` 之外的路径、以及「语义由 `tests/ci` 的假 Runner 用例负责」这条分工，我只核到「分工写在文档里、`tests/ci` 确实在跑」，没有验证 apply 路径的实际覆盖。
2. **`log` 子命令对真实 dry-run 日志的判定**：需要跑出真实日志才能验；我用「入口 `STEPS` 重排」验到了 `log` 的顺序比对分支（红且消息可执行），但 `结论:` / `待输入:` / `pending=` 的具体匹配没有独立产生的真实日志可跑。
3. **上一版的具体代码路径**：`_ =` 旧版只认裸语句、`*argv` 旧版静默跳过等，我引用的是本轮 diff 与上一轮报告，没有把 `54bf6162~1` 的守卫拿来逐条重跑（只用它做了用例计数对照：233 例）。
4. **GitHub Actions 上 `setup-uv` / `docker compose config` 的行为**：我只读到了 job 的真实结果与耗时，没有在 Actions 上替换 workflow 复现变异。
5. **`silero_vad.onnx` 这类非 JSON 私有挂载的未来形态**：当前 compose 只挂 3 个 `.json`，非 JSON 挂载的示例命名规则（`<name>.example.json`）是否合用属于推断，没有实际提交过这样的挂载。

---

## 十一、建议

**必改（都不动架构，1–2 小时量级）：**

1. `_skip_marks` 同时检查证据**所在类**上的 `@skip*` 装饰器，以及该类 `setUp`/`setUpClass` 里对 `skipTest`/`SkipTest` 的调用。
2. `_find_test` 只匹配 `unittest discover` 真会收集的文件（`test*.py`），或把两篇文档 + `ci.yml` 注释里的「会被 `unittest` 收集」改成「`tests/ci` 下任意模块里、可被 `unittest` 收集的 `TestCase` 方法」。
3. 删掉「赋给 `_` 后不再使用」里的「后不再使用」（实现是只要赋给 `_` 就红）。

**建议（不阻塞，记入覆盖边界即可）：**

4. 把「类级 skip / `setUp` skip / 重复定义 / `expectedFailure` / 非 `test*.py` 模块」列入「看不见」清单；或把证据契约换成 `test:<用例名>#<站点 key>` 让相关性可校验。
5. 修 `wiring` 对 `set -euo pipefail` 的假红与那句误导消息（上一轮已提，仍未做）。
6. `test_a_shrunk_table_without_touching_the_pin_is_red` 里陈旧的 `"READ_ARGUMENTS": 3` 改为 `len(GUARD.READ_ARGUMENTS) - 1`，避免下次改登记时再脱节。
7. 非 JSON 私有挂载的示例命名规则在文档里写清（当前 `name.removesuffix('.json')` 对 `x.onnx` 会要求 `x.onnx.example.json`）。
