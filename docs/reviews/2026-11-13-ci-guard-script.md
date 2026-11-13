# 参考验收入口守卫脚本化的非作者审查（#572 / #663）

- **审查对象**：`923157c7`（`ci: 参考验收入口守卫改由脚本实现并消除假红假绿 (#572) (#663)`），已合并（2026-11-13T11:20:24Z）。PR #663 的 head 是 `18688839`，合并为 squash 后的 `923157c7`。
- **基线**：`origin/main` head `dd0fe3d6`（受命值，建 worktree 时实测一致）。该提交之后只多了同轮 #664 的文件（`tests/ci/configuration_prerequisites.py` 与 `tests/ci/test_configuration_prerequisites.py`），`scripts/dev-env/reference-acceptance.py`、`.github/workflows/ci.yml`、两份文档在 `923157c7..origin/main` 之间**零漂移**（`git diff --stat` 只有这两个新文件）。
- **独立 worktree**：`/private/tmp/sn671`，分支 `review/671-ci-guard-script`（`git worktree add /private/tmp/sn671 -b review/671-ci-guard-script origin/main`）。变异实验另用 `/private/tmp/sn671-mut`（`review/671-mut`），每个实验前 `git checkout -- .` 并清掉 job 自己生成的 `deploy/compose.env`、`deploy/.runtime/`，未 push 任何变异。
- **审查者**：非作者。上一轮审查报告 [2026-11-13-ci-acceptance-gate.md](2026-11-13-ci-acceptance-gate.md) 的 M1/M2、FR1–FR12、FG1–FG9 是本轮的对照基线。
- **审查方式**：读 `923157c7` 全量 diff（`ci.yml` +270/−…、`scripts/dev-env/ci-entry-guard.py` 818 行新增、`tests/ci/test_ci_entry_guard.py` 461 行新增、两份文档、`pyproject.toml` 一行）+ 在本机用脚本按 `ci.yml` 原样执行该 job 的六个 `run` 块（基线 0.82 s 全绿）+ 三十余组变异（M1/M2 八组、三条旧假红七组、新守卫自身十二组、其余为接线/解析器/副作用探针）+ 直调守卫函数做形态探针 + 两处真值表实跑 + 只读查询真实 CI 与分支保护。
- **产品代码与 CI 零改动**：本片只新增本报告。实验全部落在 `sn671-mut`，实验后复原。

---

## 结论

**有条件通过。**

方向是对的：上一轮点名的三组问题里，M1 修好，四种等价写法修好三种，三条旧假红全部收口（其中格式类改成了写明契约），九类假绿里 FG2/FG3/FG4 已消除、FG5 的字面量形态已消除。但「假红假绿已消除」这句话**不成立**：一个被文档和提交信息明确宣称已接受的写法（关键字实参交给判定助手）**仍然是红的**，而且补上的 33 例单测里对应用例**是因为别的原因变绿的**（测的是机制 A，不是它宣称的机制 B）。新脚本自己还引入了与上一轮同类的新缺陷，其中两条比上一轮更危险：

1. **`sites` 把「读结果对象的真值」当成判定**。`if not started:` / `if applied is not None:` 这类写法全绿，而 `CommandResult` 是没有 `__bool__` 的 frozen dataclass，`not started` 恒为假——等于把守卫体整体清空，CI 照样绿。旧的内联 AST 扫描在这两个形态上是红的，也就是说这是**本片新引入的假绿**。
2. **「关键字实参」这条接受规则有两处死代码**。`_handed_to_judging_helper` 与 `_site_judged_by_call` 都要求实参的父节点是 `ast.Call`，但关键字实参的父节点是 `ast.keyword`，两个 `for keyword in parent.keywords` 分支永远不会命中。上一轮 F5 那个形态（`checked = ctx.mutate(...)` 之后 `judge_result(result=checked)`）**仍然红**；`ci-validation.md:30` 与 `ACCEPTED_JUDGEMENTS` 却说它已接受。
3. **守卫本身可以被「删掉」而 CI 全绿**：把三条 `ci-entry-guard.py` 调用改成仍含命令字符串的 `echo`，job 绿、33 例守卫测试也绿；删掉 `_probe_checks` 里未被测试钉住的那一项再把对应判定函数掏空，job 与测试同样绿（实测，见 §9 D1/D2）；删掉反空转检查（D3）、把 `ctx.read` 写词表收缩成只剩 `run`（D4）也都全绿。

所以结论是：**M1 修好了；假红修好了四条中的三条；九类假绿只修好了其中四类；新脚本自身有一处新的假红回归、两处新的语义级假绿、若干静默跳过与「删守卫仍绿」通道**。剩下的是收口而不是回滚。建议的必改项按性价比排：

1. 修关键字实参：`_handed_to_judging_helper` 与 `_site_judged_by_call` 要处理 `ast.keyword`（父节点是 keyword 时按 `keyword.arg` 匹配），并把 `tests/ci/test_ci_entry_guard.py` 的相应用例改成真实的 F5 形态（先绑局部、语句级调用），否则测试继续假装覆盖。
2. `sites` 只接受「`.ok`/`.code` 出现在判定位，或作为实参交给按形参名读该参数的助手」，不接受裸对象真值：`if applied:`、`if not started:`、`if applied is not None:` 应当红，或至少在 `ACCEPTED_JUDGEMENTS` 里写明这是被接受的（现在两处都没写，静默放宽）。
3. 用与 `REGISTERED_UNAUDITED` 同样的方式把 `_probe_checks` 的**集合**钉住（或让 `tests/ci` 断言 probe 覆盖的函数名清单），否则「删一项 probe → 掏空对应判定函数」是静默的；`sites` 同理，建议登记期望的站点 key 集合，让「少了一个写命令」变红，而不只是「剩下的都被判定了」。
4. `log` 的解析器放宽到文档写的契约：`order=` 现在只接受 `[a-z_, ]+`，步骤 id 带数字（如 `db2`）会报「缺少机器可读首行」，而文档只写了「id 顺序」；`mode=` 也写死 `dry-run`，与文档表格里的 `mode=<dry-run|apply>` 不一致。
5. `ctx.read` 只读边界的口径写清楚或收紧：`ctx.read(argv_var)` 完全看不见（实测写文件仍执行）；`uv` 出现在同一次调用里会豁免所有 `run`（`uv run docker compose run` 绿）；`python -c` 不在拒绝集内。

---

## 时间线

| 时点 | 动作 | 结果 |
| --- | --- | --- |
| T0 | 建 `/private/tmp/sn671`（`origin/main`=`dd0fe3d6`）与 `/private/tmp/sn671-mut` | 与主检出（停在陈旧分支）、其他审查 worktree 隔离 |
| T1 | 读 `923157c7` 全量 diff；核 `923157c7..origin/main` 漂移 | 入口脚本未被本片也未被 #664 改动；`ci.yml`/文档无漂移 |
| T2 | 读 `ci-entry-guard.py` 818 行、`tests/ci/test_ci_entry_guard.py` 461 行、两份文档 diff、job 的六个 `run` 块 | 枚举出三条守卫的接受集、三张登记表、17 处可能的形状/语义错位 |
| T3 | 按 `ci.yml` 原样跑基线 job（`/tmp/sn671_run_job.py`） | 六步全绿，合计 0.82 s；`sites 8/8`、`probe 9/9`、负对照 `exit=10 step=tools` |
| T4 | M1 + M2 八组变异 | M1 红；`if applied.code:`/局部 `.code`/跨模块助手 绿；**关键字实参红**；注解赋值/参数错位/`read` 写 红 |
| T5 | 三条旧假红 7 组变异（FR1/FR2/FR8/FR9/FR10/FR11/FR12） | 除 FR2（登记式）与 FR12（系统 python）外全部绿 |
| T6 | 新守卫自身 12 组变异（N1–N11、D1–D5） | 6 组「正确改动被接受或错误改动被放过」、4 组「删守卫仍绿」、3 组耦合型假红 |
| T7 | 直调 `audit_sites`/`check_log` 做形态与解析器探针 | 关键字实参两处死代码、真值接受、`uv` 豁免、`order=` 字符集、apply-mode 首行 |
| T8 | 真值表互锁：`tests/ci`（221 项）与 `tests/contract`（249 项）基线 + web 分支变异 | `tests/ci` 12 项全绿、`tests/contract` 7 项红 → 仍不同构 |
| T9 | 真实 CI 与分支保护只读查询（`gh`） | PR run `43369860393`：该 job 9 s、六个 step 各 ≤1 s、不在关键路径；必需检查仍是三条，该 job 经门禁间接必需 |
| T10 | 写本报告；`ruff format` + `format --check` | 见文末复现命令 |

---

## 逐条清单结论与证据

### 1. 上一轮的三组问题

#### 1a. M1：掏空 apply 路径判定（`migration_verify` 恒 `None`）

**结论：修好了。**

把 `migration_verify` 整体替换成恒 `return None`（语义：迁移落后也报 PASS），job 红：

```text
=== M1 | migration_verify 恒返回 None（掏空 apply 路径判定）
    expect=RED actual=RED job_wall=0.87s
        ::error::probe migration_verify: 迁移落后必须判失败
        ::error::该判定函数被掏空或判据反转（入口: .../scripts/dev-env/reference-acceptance.py）；修好后重跑该步骤。
```

机制是 `probe` 在 dry-run 之外真正导入入口，用「必须失败」的输入调用 `migration_verify`（`ci-entry-guard.py:501-507`）。这是本片**实质性的新证据**：上一轮 G4 靠的是 `tests/ci` 假 Runner 用例，现在 job 自己就能看见这条腐化。

边界：`probe` 只钉住九项检查/六个函数（`migration_verify`、`gate_verdict` 的四种输入、`env_gaps`、`parse_dsn`+`dsn_identity`、`migration_heads`、`bucket_names`）。**没有被 probe 或测试钉住的判定函数仍然可以被静默掏空**（§9 D1/D2 实测：删掉 `bucket_names` 那项 probe 再把 `bucket_names` 改成 `return []`，job 与 33 例测试都绿）。

#### 1b. M2 假红：四种等价正确写法

**结论：修好三条，关键字实参这一条没修好。**

| 写法 | 预期 | 实测 | 证据 |
| --- | --- | --- | --- |
| `if applied.code:`（退出码真值判定） | 绿 | **绿** | M2a，job exit=0 |
| `returncode = applied.code` 后 `if returncode != 0:` | 绿 | **绿** | M2b |
| 跨模块判定助手（`from review_judge_helper import judge_command` + `judge_command(applied)`） | 绿 | **绿** | M2d（`_ModuleIndex._scan_repo` 真去仓库里找 `def judge_command(`） |
| 关键字实参 `judge_applied(result=applied)`（语句级调用） | 绿 | **红** | M2c：`::error::step_migrate:945 assignment 未被判定（applied）` |

关键字实参为什么红：`applied` 是赋值形态，判定走 `_judged_name` → `_handed_to_judging_helper`；后者第一条就是 `if not isinstance(parent, ast.Call): return False`（`ci-entry-guard.py:277-279`），而关键字实参的父节点是 `ast.keyword`。直接调用 `_handed_to_judging_helper` 的探针输出：

```text
name applied parent keyword handed: False in_judgement: False
```

`_site_judged_by_call` 里那个 `for keyword in parent.keywords`（`:333-337`）同样是死代码：只有「站点本身是某个 Call 的位置实参」时 `parent` 才是 `Call`，而关键字实参的父节点永远不是 `Call`。三种形态直测：

```text
1 bare keyword handoff: RED  step:6 assignment 未被判定（applied）
2 inline keyword arg:   RED  step:6 other 未被判定（父节点 keyword 不是可识别形态）
3 keyword handoff inside if: GREEN
4 positional handoff bare:   GREEN
5 inline positional arg:     GREEN
```

上一轮 F5 的原始形态就是第 1 种（`checked = ctx.mutate(verify)` 之后 `judge_result(result=checked)`），所以 **F5 没有被消除**。

更麻烦的是**测试在替它作证**。`test_keyword_argument_reaches_a_judging_helper` 写的是：

```python
def judge(*, result):
    return not result.ok


def step(ctx):
    applied = ctx.mutate(("docker", "compose", "up", "-d"))
    if not judge(result=applied):
        raise RuntimeError("failed")
```

它绿是因为 `applied` 这个 `Name` 位于 `if` 的条件里，`_in_judgement` 沿 `Call → If.test` 爬升就返回 True——**与被调函数的形参名毫无关系**。把助手改成完全不看结果也能绿：

```text
helper does not judge at all, called in if: GREEN
helper does not judge at all, bare call:    RED
```

也就是说这个用例同时是「假红用例其实没测到」和「条件位里的任意实参都算判定」的两个证据。

#### 1c. M2 假绿：三种应红写法

**结论：注解赋值与参数错位修好了；`ctx.read` 里执行写命令只修好了字面量形态。**

| 破坏方式 | 预期 | 实测 | 证据 |
| --- | --- | --- | --- |
| `applied: CommandResult = ctx.mutate(migrate)` 且不再判定 | 红 | **红** | M2e：`step_migrate:940 annotated assignment 未被判定（applied）`（旧扫描里它连站点都不算） |
| 结果交给读另一个参数 `.ok` 的助手（参数错位） | 红 | **红** | M2f：`note_job(applied, started)` → `step_migrate:945 assignment 未被判定（applied）`（`_ok_parameters` 现在按形参名匹配，旧扫描是函数级白名单） |
| `ctx.read` 里执行写命令 | 红 | **红** | M2g：`step_tools:866 ctx.read(...) 携带写命令（sh -c）` |

但三个附带事实要一起记：

1. **写已经发生了，守卫只是事后判红。** job 里 `Execute` 步先跑入口，`read` 真的执行；`sites` 在下一步才判。实测加一条 `ctx.read(("sh", "-c", "echo proof > /tmp/sn671-dryrun-write.txt"))` 后文件存在，job 随后变红（`file_written=True` + `携带写命令`）。作为合并门禁这可以接受，但不能当成运行时防护。
2. **只认字面量。** 把 argv 放进变量再 `ctx.read(shoot)`：`file_written=True`、job **绿**。`_read_violations` 只扫 `ast.Constant` 字符串，变量/`psql_argv` 这类间接形态全看不见。
3. **`uv` 豁免过宽。** 只要同一次调用的字符串里出现 `uv`，`run` 就从写词表里删掉（`:394-396`），于是 `ctx.read(("uv", "run", "docker", "compose", "run", "-d", "api"))` 是绿的（实测）。

#### 1d. 三条旧假红逐条核对

**(i) `DECLARED_PENDING` 整句匹配 → 已消除。** FR1 实测：把网关那条延后说明从「未 healthy；…需要它先运行」改成「尚未 healthy（需先启动）；…复用其网络」，job **绿**。匹配改成「一条登记的全部稳定词都出现」（`ci-entry-guard.py:62-64`、`:707-738`），并且失败信息里带登记位置（`scripts/dev-env/ci-entry-guard.py:62 的表 DECLARED_DEFERRALS`）与登记行模板。

残留（换了个方向）：词的集合**过宽**。实测一条新延后项「gateway 不 healthy 的新原因：镜像未构建、模型未下载」同时包含 `gateway` 与 `healthy`，会被现有 slug 吞掉而不报「未登记」。FR2（真正的新延后项）仍然要一行登记，这是设计选择，且这次位置可解释。

**(ii) 入口输出格式当契约 → 已消除「悄悄钉死」，改成「写明是契约」。** `docs/operations/reference-acceptance.md:95-121` 新增「机器可读输出（CI 契约）」一节，把首行、`[i/N] <ident> —`、`结论:`、`待输入:`、`步骤记录`、结论行六类写成表格，并写「改格式必须同时改本节的表格、守卫脚本与作业断言」。实测 FR11（`DRY-RUN PASS` → `DRY-RUN PASSED`）**绿**（上一轮是红），因为 `_VERDICT` 只锚前缀。改 `reference acceptance:` 前缀或 `结论:` 标签仍然红，但这次文档说清楚了。

残留：`order=` 的字符集比文档窄（§2e），`mode=` 写死 `dry-run` 与文档表格的 `<dry-run|apply>` 不一致。

**(iii) 合成私有输入的派生 vs 硬编码 → 部分消除。** 现在从 `deploy/compose.yaml` 的 `/run/speechnexus-private/<name>` 反推文件名、从 lane 配置的 `vad_model_file` 派生合成模型与摘要（`ci.yml:899-960`），实测：

- FR8 新增第三条 gateway file lane（compose + 已有 example）：**绿**（上一轮 F6 红）。
- FR9 把 asr example 的 VAD 从 `silero_vad.onnx` 改名 `silero_vad_v6.onnx`：**绿**（上一轮 F7 红）。
- FR10 把 `SPEECHNEXUS_OBJECT_STORAGE_BUCKET` 移出 `EXTRA_REQUIRED`：**绿**（负对照改用 compose 派生键 `POSTGRES_PASSWORD`，上一轮 F8 红）。

残留三处仍会假红（实测，见 §9）：目录仍硬编码 `deploy/.runtime/private`（改 `compose.env.example` 的 `SPEECHNEXUS_PRIVATE_CONFIG_DIR` 到别的名字 → 「私有配置目录不存在」未登记，红）；正则扫**原始文本含注释**（compose 注释里写一句 `/run/speechnexus-private/notes.md` 就会要求 `deploy/private/notes.md.example.json`，红）；派生步骤假设每个挂载文件都有同名 `.example.json`（新增一个非 JSON 的私有挂载，比如 TLS 私钥，prepare 步 `exit=1`，stderr 只有 `compose 挂载 tls.key，但缺少 deploy/private/tls.key.example.json 示例`，不是 `::error::`，还会让后续步骤在半个私有目录上产生误导性失败）。

### 2. 新脚本守卫自身的质量（本轮重点）

**结论：三条守卫的职责划分、失败信息、登记通道、文档边界都比上一版强；但它有与上一版同类的缺陷——只查形状不查语义、白名单过宽、静默跳过、对合法形态过严，并且新增了一条比上一版更钝的假绿（对象真值当判定）。**

**(a) 只查形状不查语义（新引入）**

`_judged_name` 接受「别名出现在判定位」（`ci-entry-guard.py:258-272`），而「判定位」只看父链是不是 `if/while/assert/IfExp` 的 test、`Match.subject`。于是：

```text
if not applied:            GREEN（CommandResult 无 __bool__，not applied 恒假）
if applied is not None:    GREEN（CommandResult 恒非 None，判定恒真）
```

这两种写法把守卫体整体架空，`sites` 报 `1 judged`、job 绿。上一版的扫描要求 `.ok` 属性读取或含 `.code` 的 `Compare`，这两个形态是**红的**——所以这是本片引入的新假绿，而且比上一轮 FG3 更隐蔽（那至少读了一个真实参数）。

**(b) 假红：一处真回归 + 三处耦合**

- 关键字实参（§1b）：真回归，且文档/提交信息都宣称已接受。
- 助手按 `.code` 判定（`judge_by_code(result)` 里 `if result.code != 0: raise`）：**红**（N5）。`_ok_parameters` 只认 `.ok`，等价正确写法被拒；`REGISTERED_UNAUDITED` 可以登记放行，但那是「先被拦下再登记」，不是接受集里写明的形态。
- `probe` 与入口签名强耦合：`Context(...)` 用七个关键字构造、`CommandResult` 用四个位置实参构造（`:510`、`:552-560`）。实测给 `Context` 加一个必需字段并同步改入口自己的构造（合法重构）→ `::error::probe dsn_identity: TypeError: Context.__init__() missing 1 required positional argument: 'workspace'`，归因却写「该判定函数被掏空或判据反转」。
- `probe` 与函数名强耦合：把 `env_gaps` 重命名成 `environment_gaps`（全仓库同步改名）→ `::error::probe env_gaps: AttributeError: module 'reference_acceptance_under_test' has no attribute 'env_gaps'`，同样误归因。

**(c) 白名单过宽**

- 跨模块助手按**裸函数名**在仓库里找第一个「读某形参 `.ok`」的同名 `def`（`:178-201`），不解析 import。实测：入口 `from ci_review_helpers import publish`（`publish` 只 `print(result.tail(2))`，不判定），仓库另一个无关文件里有 `def publish(item): if not item.ok: ...` → job **绿**（N9）。同名 `check`/`judge`/`publish` 在真实仓库里并不罕见。
- 助手只要读 `.ok` 就算判定，不要求它在判定位：`def surface(result): print(f"ok={result.ok}")` + `surface(applied)` → **绿**（N2）。这与上一轮 FG9 同类，只是从「模块内函数级」缩到「形参名级」。
- 延后项词表过宽（§1d(i)）。
- `uv` 豁免（§1c(3)）。

**(d) 静默跳过**

- `sites` 只认「`<裸名>.mutate(...)`」；`ctx.runner.mutate(...)`、`getattr(ctx, "mutate")(...)`、`self.ctx.mutate(...)` 一律不计数也不报错。实测把 gate 步的 `ctx.mutate(argv, env=environment)` 改成 `ctx.runner.mutate(...)`，job **绿**（站点静默从 8 掉到 7；`require_sites` 只在**一个站点都没有**时才报）。
- `ctx.read(argv_var)` 看不见（§1c(2)）。
- 整个站点被删也看不见：删掉 buckets 步的 `initialized = ctx.mutate(initialize)` 与后续用到它的判定，job **绿**，只是摘要从 `8 judged` 变成 `7 judged`（实测）。守卫证明的是「**剩下的**站点都被判定了」，没有任何「期望站点集合」。
- `REGISTERED_UNAUDITED` 的 key 是 `f"{function.name}:{ast.unparse(node)[:60]}"`，站点一改参数 key 就变，旧登记只在 stderr 打 `::warning::`（不失败）。

**(e) 解析器对合法形态过严**

- `_HEADER` 的 `order=([a-z_, ]+)`（`:641`）不接受数字/大写：步骤 id 叫 `db2` 的合法日志被报「缺少机器可读首行 `steps=<N> order=<csv> …`」，而同一条日志的 `[2/2] db2 —` 能被 `_STEP_LINE` 正常识别（直调 `check_log` 实测）。
- `mode=` 写死 `dry-run`，而文档表格写 `mode=<dry-run|apply>`：把一份合法的 `mode=apply` 首行喂给 `log` 子命令，报「缺少机器可读首行」。
- `log` 只按字面量匹配中文标签（`结论:`/`待输入:`/`步骤记录:`/`reference acceptance:`），这些现在至少已经在文档里写成契约，算「有意收紧」。

**(f) 它自己有没有「删掉守卫仍绿」的情形：有，四类实测**

1. **把三条调用改成 echo**：`run: |` 块里 `python3 … ci-entry-guard.py log/sites/probe` 换成 `echo "ci-entry-guard.py log 已停用"` 等 → job **绿**、`tests/ci/test_ci_entry_guard.py` 33 例 **全绿**。接线测试只断言 job 文本里出现命令字符串（`JOB_TEXT` 是 `ci.yml` 里 `reference_acceptance:` 之后的整段文本），注释/echo 里的字符串照样满足。
2. **删 probe 一行 + 掏空对应判定函数**：删 `("bucket_names", bucket_names_derives),`，再把 `bucket_names` 改成 `return []` → job **绿**、33 例 **全绿**（D1+D2）。单删 probe 一行也绿（D1）。`ProbeTests` 只钉 `migration_verify` 一项。
3. **删反空转检查**：去掉 `if not seen and require_sites:` 那段（`:466-467`）→ job 绿、测试绿（D3）。之后扫描即使完全空转也没人报。
4. **收缩 `ctx.read` 写词表**：`_COMPOSE_WRITE_TOKENS` 改成只剩 `{"run"}` → job 绿、测试绿（D4）；此时 `ctx.read(("docker", "compose", "down"))` 被接受（探针实测 GREEN）。测试只覆盖 `compose run` 与 `sh -c` 两种。

相对地，D5（把判定位收缩成只剩 `if`）被测试抓红（`assert applied.ok` 有用例），说明这 33 例对**已写下的**检测分支有效，只是对「删除/收缩登记表」无效。

### 3. 假红清单

| ID | 正确改动 | 后果 | 本轮实测 |
| --- | --- | --- | --- |
| FR1 | 把网关延后说明写得更清楚（语义不变） | **已消除**：绿 | 实测绿 |
| FR2 | 新增一条合法延后输入 | 仍需一行登记（设计选择），但登记位置/行模板在失败信息里 | 实测红，信息可操作 |
| FR3 | 新增/重排入口步骤（同步文档与 `tests/ci`） | 仍红（`EXPECTED_STEP_ORDER` + `steps=N`），但现在是**写明的契约** | 读代码 + `docs/operations/reference-acceptance.md:110-113` |
| FR4 | `if applied.code:` | **已消除**：绿 | 实测绿 |
| FR5 | 先把 `.code` 绑局部再比较 | **已消除**：绿 | 实测绿 |
| FR6 | 结果交给**关键字**实参调用的判定助手 | **仍在**：红（上一轮 F5 原形） | 实测红；`ci-validation.md:30` 与 `ACCEPTED_JUDGEMENTS` 宣称已接受 |
| FR7 | 判定抽到别的模块的助手 | 位置实参形态**已消除**：绿 | 实测绿（`_scan_repo`） |
| FR8 | 新增第三条 gateway file lane | **已消除**：绿 | 实测绿 |
| FR9 | 升级 VAD 模型文件名 | **已消除**：绿 | 实测绿 |
| FR10 | artifact 桶回落到 compose `:-` 默认 | **已消除**：绿 | 实测绿（负对照改用 `POSTGRES_PASSWORD`） |
| FR11 | 只改结论行措辞（`DRY-RUN PASS` → `DRY-RUN PASSED`） | **已消除**：绿 | 实测绿 |
| FR12 | 入口 dry-run 路径引入项目依赖 | 仍红（job 用系统 `python3`、不 `uv sync`） | 实测红（`execute` exit=1，随后 `log`/`probe` 报 `ModuleNotFoundError`） |
| **NR1** | 助手按 `.code` 判定（`judge_by_code(result)`） | **新假红**：`assignment 未被判定` | N5 实测红 |
| **NR2** | `probe` 覆盖的判定函数改名 / `Context` 新增必需字段（合法重构） | **新假红**：`AttributeError`/`TypeError`，归因为「判定函数被掏空」 | NR4、N11 实测红 |
| **NR3** | 步骤 id 含数字（如 `db2`）或 `mode=apply` 的合法首行 | **新假红**：`缺少机器可读首行` | `check_log` 直测 |
| **NR4** | compose 新增非 JSON 的私有挂载（如 TLS 私钥） | **新假红**：prepare 步 `exit=1`，stderr 非 `::error::`，后续步骤连锁误导 | 实测 |
| **NR5** | compose 注释里提到 `/run/speechnexus-private/<name>` | **新假红**：prepare 步要求一个不存在的 example | 实测 |
| **NR6** | 把 `SPEECHNEXUS_PRIVATE_CONFIG_DIR` 改到别的目录名（compose 与环境文件同步改） | **仍在**（上一轮 §1(c) 第 1 行）：派生目录硬编码 `deploy/.runtime/private` | 实测红 |

### 4. 假绿清单

| ID | 损坏方式 | 该 job 的表现 | 本轮证据 |
| --- | --- | --- | --- |
| FG1 | 保留 `.ok` 读取、清空守卫体 | **仍绿** | 实测：`if not started.ok:` 后只 `print` → job 绿、33 例绿 |
| FG2 | `applied: CommandResult = ctx.mutate(...)` 不判定 | **已消除**：红 | M2e |
| FG3 | 结果交给读另一个参数 `.ok` 的助手 | **已消除**：红 | M2f |
| FG4 | `migration_verify` 恒 `None` | **已消除**：红 | M1 |
| FG4b | **其它**判定函数被掏空（未被 probe/测试钉住的） | **仍绿** | D2：`bucket_names` 恒 `[]` + 删其 probe → job/测试全绿 |
| FG5 | `ctx.read` 执行写命令（字面量 `compose run/up`、`sh -c`） | **已消除**：红（但写已发生） | M2g + `file_written=True` |
| FG5b | `ctx.read(argv_var)` 执行写命令 | **仍绿**（新形态） | 实测：`file_written=True`、job 绿 |
| FG6 | 某步骤吞掉自己的失败并返回结论 | **仍绿**（负对照只证明 `tools` 步的通用传播） | 读 `run_steps` + 负对照只有 `FAIL step=tools` |
| FG7 | 把本应失败的前置条件登记成 pending | **仍绿（设计使然）**：在 `DECLARED_DEFERRALS` 加一行即绿，最终行仍 `DRY-RUN PASS` | 实测：加 `require(False, …)` + 一行登记 → 绿；不登记 → 红 |
| FG8 | `docker compose ps` 失败 | 降级成已声明 pending，与「gateway 未起」不可区分（入口未改） | 读 `service_healthy` |
| FG9 | B1 的「读退出码但读错/判错」 | **仍绿**，且本片新增两个更钝的形态 | 见下 |
| **NFG1** | `if not started:` / `if applied is not None:`（对象真值，恒真/恒假） | **新假绿**：绿 | N1 + 形态探针 |
| **NFG2** | 判定助手只 `print(result.ok)`，不判定 | **新假绿**：绿 | N2 |
| **NFG3** | `ctx.runner.mutate(...)`（接收者非裸名） | **新假绿**：绿，站点静默消失 | N4 |
| **NFG4** | 跨模块同名函数碰撞（导入的助手不判定，仓库另一同名函数读 `.ok`） | **新假绿**：绿 | N9 |
| **NFG5** | 删除整个 `ctx.mutate` 写命令与判定 | **新假绿**：绿，摘要 8→7 | 实测 |
| **NFG6** | 新延后项文本里恰好含已登记的词（`gateway`+`healthy`） | **新假绿**：绿 | 探针实测匹配到现有 slug |
| **NFG7** | `ctx.read(("uv", "run", "docker", "compose", "run", …))` | **新假绿**：绿（`uv` 豁免吃掉 `run`） | 探针实测 |
| **NFG8** | 删/停用三条守卫调用（改写为 echo） | **新假绿**：job 绿 + 守卫测试绿 | 实测 |

### 5. 门禁接线与成本

**结论：接线没退化，成本与上一轮同量级，仍在关键路径外。**

- 接线：`reference_acceptance` 仍在 `python`（`Gate: required checks for this change`）的 `needs` 里（`ci.yml:699`），`ACCEPTANCE_RESULT` 仍在门禁环境里（`:715`），四范围期望值未变（docs/web `skipped`、no-db/full `success`，`:735-755`）。分支保护只读 API 实测 `required_status_checks.contexts` 仍是三条（`Gate: required checks for this change`、`Web: …`、`Compose: configuration validation`），`strict: false`；本 job 不新增受保护状态名，经门禁间接必需。
- 真实 CI（PR run `43369860393`，head `18688839`，success）：该 job `05:07:13 → 05:07:22` = **9 s**；六个 step 里 Prepare/Execute/Log/Sites/Probe/Negative 各 ≤1 s（`log`/`sites`/`probe` 各 1 s 精度内）；最后一个 Integration 分片 `05:17:46` 结束、门禁 `05:18:33` 才开始 → 该 job **不在关键路径**。
- 本地等价（`/tmp/sn671_run_job.py`，按 `ci.yml` 原样跑六个 `run` 块）：Prepare 0.04 s、Execute 0.43 s、`log` 0.06 s、`sites` 0.06 s、`probe` 0.06 s、负对照 0.18 s，合计 **0.82 s**。与注释「entry point plus all four guards measured under 2 s locally」（`ci.yml:882`）一致。
- 负对照仍有效：空 `POSTGRES_PASSWORD` → `exit=10`、`FAIL step=tools`。

### 6. 真值表互锁

**结论：仍不同构，与上一轮相同——`tests/ci` 是 `tests/contract` 的子集，缺 web 范围。**

- `tests/contract/test_ci_workflow.py:261-270` 对四个范围 × 五种状态参数化，期望值是独立于 `_EXPECTED` 的第二份硬编码（`"skipped" if mode in ("docs","web") else "success"`）。
- `tests/ci/test_node_controller_image_ci.py` 仍然只有：`MODE=docs` 的 acceptance 真值表（`:121-139`）与 full/no-db 的「success 绿、其余红」（`:141-…`）；**没有 web 用例**，也不断言 `needs`/`env` 字典。
- 实测（在 `sn671-mut` 把 `ci.yml` 的 web 分支 `acceptance=skipped` 改成 `success`）：`tests/ci` 12 项 **OK**；`tests/contract` **7 failed / 242 passed**（含 `[success-web]`、`[skipped-web]`）。两处都跑在 CI 里，所以现实覆盖没缺口，但单看 `tests/ci` 会漏。
- 基线实跑：`tests/ci` 221 项 OK（含新 33 项）；`tests/contract/test_ci_workflow.py` 249 passed（含 `ACCEPTANCE_RESULT` 相关用例）。

### 7. 文档

**结论：比上一轮诚实得多，契约写明了；但有一处与实现不符，一处覆盖边界清单不完整，一处措辞与实现相反。**

- `docs/operations/reference-acceptance.md:95-121` 新增「机器可读输出（CI 契约）」：六类行的格式/约束、步骤顺序、延后项登记通道，并明确「改格式必须同时改本节的表格、守卫脚本与作业断言」。这一节正是上一轮缺口 1/2 的收口，**做对了**。
- `docs/validation/ci-validation.md:30` 描述 `sites` 接受「注解赋值、先绑局部、**位置/关键字实参**交给按形参名读取 `.ok` 的助手」——**关键字实参这一半不成立**（§1b）。这是本片最实质的文档失真：读者会以为 F5 已被接受。
- `ci-validation.md:32` 的覆盖边界列了三条「看不见」，方向对，但不完整：少了「裸对象真值被当成判定」（NFG1）、「接收者非裸名的 `mutate` 被静默跳过」（NFG3）、「`ctx.read` 的 argv 来自变量」（FG5b）、「登记表/探针表被删改」（D1–D4）。其中第一条不是「看不见」而是「被接受」，性质不同。
- `ci-validation.md:32` 还写「把 `ctx.mutate` 包进自定义函数再丢弃返回看不见」——**同模块内是能看见的**（实测 wrapper 的 `return ctx.mutate(...)` 与「赋值后不判」都红）；真正看不见的是把 `mutate` 搬到另一个模块（`sites` 只扫入口文件）。措辞应改为「跨模块」。
- `ci.yml:850-874` 的作业注释已收敛成「corrupting the entry point's derivations, step order, deferred-input contract, pure judgement functions, or its read-only boundary fails a PR」+ 覆盖边界段，与实测基本一致（每个列举项都有对应实测红），不再有上一轮「corrupting the entry itself」那种越界表述。**这一条修好了。**

### 8. 跨片一致性（#664）

**结论：没有重复或冲突的职责，只有一处轻度维护耦合。**

- `tests/ci/configuration_prerequisites.py` 是运行时配置键的机器可读扫描表（`Row`：keys/prerequisite/behaviour/disposition/evidence），`tests/ci/test_configuration_prerequisites.py` 是四类元守护（库存、证据、静默行处置、文档同步）。它不引用 `ci.yml`、`reference-acceptance.py`、`ci-entry-guard.py` 或 `deploy/private/`（grep 无命中），与入口守卫的职责（入口脚本的派生/顺序/判定/输出契约）**不重叠**。
- 唯一交集是键名：`configuration_prerequisites.py:110-116` 的 `TRANSCRIPTION_KEYS` 与入口的 `EXTRA_REQUIRED`/`bucket_names` 都提到 `SPEECHNEXUS_TRANSCRIPTION_INPUT_BUCKET` 一类键。语义不同（前者是「配了不生效」的运行时配对，后者是「CI 合成环境必须显式」的准入键），不冲突，但改名要同时改两处。
- 一个可借鉴的点：#664 的元守护会把「模块表」与「运维文档表格」逐行对齐（`test_the_operator_table_matches_the_module`），而本片的 `DECLARED_DEFERRALS` / `REGISTERED_UNAUDITED` / `_probe_checks` 三张表没有任何文档或测试锚定其**集合**——这正是 §2f 里「删一行仍绿」的根因。
- #664 对本 job 的影响：只改了 `deploy/compose.env.example`（新增/改写注释与键说明）。实测基线 job 在 `origin/main` 上仍绿，`DECLARED_DEFERRALS` 的 `gateway`+`healthy` 仍能匹配，无漂移。

---

## 我独立复现的变异（原始记录）

除特别说明外，均在 `/private/tmp/sn671-mut` 上做，每个实验前 `git checkout -- .` 并清掉 `deploy/compose.env`、`deploy/.runtime/`；执行器 `/tmp/sn671_run_job.py` 直接解析 `ci.yml` 的 `reference_acceptance.steps`，按 `bash -euo pipefail` 顺序跑六个 `run` 块（跳过 checkout/setup-uv 两个 action step）。

### 基线

```text
[2] Prepare the acceptance environment from the committed example: exit=0 wall=0.04s
[3] Execute the reference acceptance entry in dry-run mode: exit=0 wall=0.43s
[4] Verify the documented output contract and declared deferred inputs: exit=0 wall=0.06s
    log: OK (deferrals: 1)
[5] Audit judgement sites and the read-only boundary: exit=0 wall=0.06s
    sites: OK (mutating sites: 8 judged, 0 registered, 8 total)
[6] Probe the entry point's pure judgement functions: exit=0 wall=0.06s
    probe: OK (probe checks: 9/9 passed)
[7] Reject a corrupted environment instead of passing quietly: exit=0 wall=0.18s
    negative control: exit=10 step=tools
TOTAL run steps wall clock: 0.82s
```

### M1 / M2

```text
M1  migration_verify 恒 None            -> RED   ::error::probe migration_verify: 迁移落后必须判失败
M2a if applied.code:                    -> GREEN
M2b returncode = applied.code; if …     -> GREEN
M2c judge_applied(result=applied)       -> RED   ::error::step_migrate:945 assignment 未被判定（applied）
M2d 跨模块 judge_command(applied)       -> GREEN
M2e applied: CommandResult = ctx.mutate -> RED   ::error::step_migrate:940 annotated assignment 未被判定（applied）
M2f note_job(applied, started)          -> RED   ::error::step_migrate:945 assignment 未被判定（applied）
M2g ctx.read(("sh","-c","echo … > /tmp"))-> RED  ::error::step_tools:866 ctx.read(...) 携带写命令（sh -c）
```

`M2g` 的副作用：`/tmp/sn671-dryrun-write.txt` 在 job 变红前已被创建（`file_written=True`）。

### 关键字实参（形态直调 `audit_sites`）

```text
1 bare keyword handoff: RED  step:6 assignment 未被判定（applied）
2 inline keyword arg:   RED  step:6 other 未被判定（父节点 keyword 不是可识别形态）
3 keyword handoff inside if: GREEN
4 positional handoff bare:   GREEN
5 inline positional arg:     GREEN
helper does not judge at all, called in if: GREEN      ← 33 例里的 keyword 用例就是这样绿的
helper does not judge at all, bare call:    RED
```

### 三条旧假红

```text
FR1  延后说明改文案                    -> GREEN
FR2  新增未登记延后项                  -> RED（信息含登记位置与登记行）
FR8  新增第三条 gateway file lane      -> GREEN
FR9  VAD 改名 silero_vad_v6.onnx       -> GREEN
FR10 桶移出 EXTRA_REQUIRED             -> GREEN
FR11 DRY-RUN PASS → DRY-RUN PASSED     -> GREEN
FR12 入口 import psycopg               -> RED（execute exit=1；log/probe 报 ModuleNotFoundError）
```

### 新守卫自身

```text
N1  if not started:                    -> GREEN（sites 1 judged）
N2  surface(result) 只 print .ok        -> GREEN
N3  ctx.read(shoot)（变量 argv）        -> GREEN，/tmp/sn671-write2.txt 已写
N4  ctx.runner.mutate(...)              -> GREEN（站点静默消失）
N5  judge_by_code(result)（读 .code）    -> RED   相等价正确写法被判红
N9  跨模块同名 publish 碰撞             -> GREEN
N11 Context 新增必需字段                -> RED   ::error::probe dsn_identity: TypeError: Context.__init__() missing 1 required positional argument: 'workspace'
NR4 判定函数改名 env_gaps               -> RED   ::error::probe env_gaps: AttributeError: … has no attribute 'env_gaps'
D1  删 probe 的 bucket_names 一行        -> job GREEN / 33 例 OK
D2  D1 + bucket_names 恒 []             -> job GREEN / 33 例 OK
D3  删反空转检查                        -> job GREEN / 33 例 OK
D4  写词表只剩 {"run"}                  -> job GREEN / 33 例 OK；随后 ctx.read(("docker","compose","down")) GREEN
D5  判定位只剩 if                       -> 测试 RED（assert 有用例），job 不受影响
G-1 三条守卫调用改成 echo               -> job GREEN / 33 例 OK
G-2 删掉 buckets 步的 initialize 写命令 -> job GREEN（摘要 8→7）
```

### 真值表与解析器

```text
web 分支 acceptance=success  -> tests/ci 12 项 OK；tests/contract 7 failed / 242 passed
check_log(order 含 db2)      -> RED 缺少机器可读首行
check_log(mode=apply 首行)    -> RED 缺少机器可读首行
新延后项含 gateway+healthy    -> 被现有 slug 吞掉（matched）
ctx.read(("uv","run","docker","compose","run",…)) -> GREEN
```

### 真实 CI（只读）

```text
PR #663 head 18688839 / run 43369860393（success）
Deploy: reference acceptance entry (dry-run execution) 05:07:13 → 05:07:22（9 s）
  Prepare 0:00 / Execute 0:01 / log+sites+probe+negative 各 ≤1 s
最后 Integration 分片 05:17:46 结束；Gate: required checks 05:18:33 → 05:18:35
分支保护 required contexts = ["Gate: required checks for this change",
  "Web: lint, types, tests, build and accessibility", "Compose: configuration validation"]，strict=false
```

---

## 只能依赖作者陈述 / 未验证的点

1. **测试与 lint 的通过态**：我核过真实 PR run `43369860393` 的 “Python: lint, types and contract compatibility” 为 success，并本地用 `ruff format --check` / `ruff check` 核了两个新文件与三份文档；但 `mypy --strict` 对 `scripts/dev-env/ci-entry-guard.py` 的具体通过态没有在本机复跑（本机跑需要 `uv sync`，会动 `uv.lock`），依据是真实 CI 绿。
2. **`ubuntu-24.04` 上的 docker/compose/python 版本**：本机 python 3.14 / Docker 29.x / compose v5.7；`pyproject.toml` 已把该脚本标成 `py312`，真实 run 绿是间接证据。
3. **`tests/ci/test_ci_entry_guard.py` 的其余断言在 runner 上的行为**：本机 3.14 全绿；`JOB_TEXT` 的文本切片方式在 runner 上一致（文件不变）。
4. **作者对未来步骤/延后项演进的意图**：`EXPECTED_STEP_ORDER` 是刻意第二份副本，我按「写明就是契约」接受，未验证运维是否知道要同步三处。
5. **`REGISTERED_UNAUDITED` 的实际使用频率**：当前是空表，无法判断登记通道会不会变成「遇红就登记」的橡皮章。
6. **`probe` 的九项断言是否是入口「守住的全部」**：我只能证明九项；入口里其它判定函数（`service_healthy`、`compose_services`、`roles_in_database`、`stack_private_inputs` 等）没有 probe，也没有「这些函数不需要 probe」的登记，作者意图未知。

---

## 复现命令

```bash
# 0) 独立 worktree 与变异 worktree
git worktree add /private/tmp/sn671 -b review/671-ci-guard-script origin/main
git worktree add /private/tmp/sn671-mut -b review/671-mut origin/main

# 1) 按 ci.yml 原样跑该 job 的六个 run 块（跳过 action step）并计时
python3 /tmp/sn671_run_job.py /private/tmp/sn671

# 2) 基线测试
cd /private/tmp/sn671 && python3 -m unittest discover -s tests/ci
cd /tmp && uv run --no-project --with pytest --with pytest-asyncio --with pyyaml \
  --with 'psycopg[binary]' python -m pytest /private/tmp/sn671/tests/contract/test_ci_workflow.py -q

# 3) 变异：入口/R 组 + 守卫自身 D 组
python3 /tmp/sn671_mutate.py            # M1、M2、FR、N 组
python3 /tmp/sn671_guard_mutate.py      # D1–D5
python3 /tmp/sn671_recheck.py           # FR8/FR9/FR10 + 非 JSON 私有挂载（每轮先清 deploy/.runtime）

# 4) 真实 CI 与分支保护（只读）
gh api repos/hrygo/SpeechNexus/actions/runs/43369860393/jobs --paginate
gh api repos/hrygo/SpeechNexus/branches/main/protection

# 5) 报告格式（本片只新增本报告）
uv run --no-project --with ruff ruff format docs/reviews/2026-11-13-ci-guard-script.md
uv run --no-project --with ruff ruff format --check docs/reviews/
```

（`/tmp/sn671_run_job.py`、`/tmp/sn671_mutate.py`、`/tmp/sn671_guard_mutate.py`、`/tmp/sn671_recheck.py` 是本轮临时脚本，未入库：第一个用 PyYAML 解析 `ci.yml` 的 `reference_acceptance.steps`、按 `bash -euo pipefail` 顺序执行 `run` 块并把 `RUNNER_TEMP`/`GITHUB_STEP_SUMMARY` 指到临时目录；后三个做「改一处 → 跑第一个 → 复原」，并可选跑 `tests/ci/test_ci_entry_guard.py`。）
