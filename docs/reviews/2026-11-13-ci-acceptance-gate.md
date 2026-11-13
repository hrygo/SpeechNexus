# 参考部署验收入口接入合并门禁的非作者审查（#655 / #572）

- **审查对象**：`7d575298`（`ci: 在 CI 中真实执行参考部署验收入口 (#572) (#655)`），已合并；`git show --format="" --name-only` 只有四个文件：`.github/workflows/ci.yml`、`docs/validation/ci-validation.md`、`tests/ci/test_node_controller_image_ci.py`、`tests/contract/test_ci_workflow.py`（+361/−6，其中 `ci.yml` +298/−6）。
- **基线**：`origin/main` head `fd05f88b`（受命值，建 worktree 时实测一致）。事实一律取自 `git show origin/main:<path>` 与独立 worktree，未读主检出（主检出停在 `a7508635`，且 `uv.lock` 有未提交改动）。
- **独立 worktree**：`/private/tmp/sn669`，分支 `review/669-ci-acceptance-gate`（`git worktree add /private/tmp/sn669 -b review/669-ci-acceptance-gate origin/main`）。变异实验另用 `/private/tmp/sn669-mut`（`review/669-mut`），实验后 `git checkout -- .` 复原，未 push。
- **审查者**：非作者。入口脚本 `scripts/dev-env/reference-acceptance.py` 与上一轮审查报告 [2026-11-13-reference-acceptance-rework.md](2026-11-13-reference-acceptance-rework.md) 是本轮判断「约定 vs 偶然措辞」的参照。
- **审查方式**：静态逐行审查 + 本地按 YAML 原样执行该 job 的五个 `run` 块（0.68 s，全绿）+ 20 组变异/探针（9 组假红候选、5 组假绿候选、4 组 AST 宽容度探针、1 组 B1 纯回归、1 组真值表互锁）+ 直接执行门禁脚本跑 4 个范围 × 5 个状态 + 读真实 CI run 的逐作业时长 + 分支保护只读查询 + 两个真值表测试文件实跑（`tests/ci` 183 项、`tests/contract` 249 项）。
- **产品代码与 CI 零改动**：本片只新增本报告。`git status --porcelain` 在提交前只有 `docs/reviews/2026-11-13-ci-acceptance-gate.md`；`uv.lock` 未改（实验用的 `uv run --no-project` 不触碰项目锁）；`deploy/compose.env` 与 `deploy/.runtime/private/` 是 job 自己生成、已被 `.gitignore:4`/`:67` 忽略。

---

## 结论

**有条件通过。**

门禁接线这一半是**做对了**，不是「显示绿色」的接线：

- `needs`、`ACCEPTANCE_RESULT`、四种范围期望值与 `deployment` **完全同构**（docs/web 期望 `skipped`，no-db/full 期望 `success`）；我直接执行门禁脚本复现了 4 个范围的通过态，以及 `no-db/full + skipped`、`web + success`、`docs + cancelled`、`docs + 空值` 四种应红态（§4）。
- 该 job 确实能把失败/跳过变成**合并阻断**：门禁作业名 `Gate: required checks for this change` 就是分支保护的必需检查（只读 API 实测 `required_status_checks.contexts` 含该名）；`reference_acceptance` 一旦在 no-db/full 上失败、取消或跳过，门禁脚本直接 `exit 1`。
- 增量成本与作者陈述相符：真实 run `43362530051` 里该 job **11 s**（03:59:44→03:59:55），关键路径由 Integration 分片决定（最后一个 04:10:12 结束，门禁 04:11:03 才跑），该 job **不在关键路径**上。本地五个 `run` 块合计 0.68 s（entry 0.40 s），与注释「entry + 两条控制 under 1 s locally」一致。
- 负对照不是摆设：篡改环境后 entry 以 `exit=10`、`FAIL step=tools` 退出，job 变红；纯 B1 回归（删掉 `step_stack` 的 `started.ok` 判定）被 AST 扫描抓成 `7/8`。

但「入口已被完整守住」**不成立**，而且这一片自己引入了一个**新的假红面**和一个**实现与文档不符的 AST 扫描**：

1. **AST 扫描会把正确的等价写法判红**（无登记通道）：`if applied.code:`（退出码真值判定，语义完全正确）、把 `.code` 先绑到局部再比较、把结果交给用**关键字实参**调用的辅助函数——三种都红，且失败信息只说「never judges」，不告诉作者哪种写法被接受（§1b、F3/F4/F5）。
2. **AST 扫描同时漏判**：带注解的赋值 `applied: CommandResult = ctx.mutate(...)`、属性/多元赋值、把结果当实参传给「读别的参数 `.ok`」的辅助函数，全都不算 site，于是**未被判定也绿**。文档却写「结果被丢弃或赋值后未判定即失败」，对这三种写法不成立（§3、G2/G3）。
3. **`DECLARED_PENDING` 是按人类可读中文句子做精确匹配**：把那条延后说明改得更清楚（不改语义）就红（F1）。这是把措辞升格成合并门禁契约。
4. **合成输入把 example 文件与 compose 的具体细节钉死**：新增一条 gateway file lane 红（F6）、把 asr 例子里的 VAD 文件名升级红（F7）、把 artifact 桶从 `EXTRA_REQUIRED` 放宽成 compose 默认值红（F8）。三者都是「改对了也会被拦」，且需要同步改 `ci.yml` 才放行。
5. **`ci.yml` 的注释越界**：`so corrupting the entry itself fails a PR` 与实测矛盾——只破坏 apply 路径（`migration_verify` 恒返回 `None`）时该 job 全绿（G4）。文档（`ci-validation.md`）的覆盖边界写得很诚实，反而是作业注释这句更强。

建议的收口（都不要求回滚本片）：

1. AST 扫描把「对目标的 `.ok`/`.code` 做真值判断或比较」都算判定（含 `if result.code:`、`if result.code: raise`、`match result.code`），并接受位置**或**关键字实参调用的辅助函数、接受 `AnnAssign`/属性/多元赋值（把 site 数出来而不是跳过）；不然就把未识别的形式降级为**警告**，只对「结果被丢弃」保持红。
2. 延后项登记改为「前缀/ID 匹配」或把机器可读的 deferral id 打进日志，别再拿整句中文字符串当键；至少把登记位置（`ci.yml` 第 961 行常量）写进失败信息。
3. 把 `order=`、`[i/7] <ident> —`、`待输入: ` 三条被钉住的格式写进 `docs/operations/reference-acceptance.md` 的「机器可读输出」段，明确它们是契约；否则未来入口仅重排首行就会红且文档无法解释。
4. 改掉 `ci.yml` 里「corrupting the entry itself fails a PR」这句，或补一句「apply 路径由 `tests/ci` 假 Runner 用例与真实运行负责」（后者本来就写对了，只是注释没跟上）。

---

## 时间线

| 时点 | 动作 | 结果 |
| --- | --- | --- |
| T0 | 建独立 worktree `/private/tmp/sn669`（`origin/main`=`fd05f88b`） | 与主检出、既有 `/private/tmp/sn667` 等隔离 |
| T1 | 读 `7d575298` 全量 diff 与四个文件现状；`git log 7d575298..origin/main` 核对基线漂移 | 入口脚本未被本片、也未被基线漂移改动；`deploy/` 只有 `compose.env.example` 在基线 `fd05f88b` 改了 +5/−2（DSN 覆盖表返修）。在 `origin/main` 上按原样复跑该 job **仍全绿**，说明合成输入对这次改动不敏感 |
| T2 | 读入口脚本关键路径（`require`/`run_steps`/`step_*`/`compose_config`/`extends`/`EXTRA_REQUIRED`） | 枚举 dry-run 实际执行与只计划的内容、`待输入` 的两个产生点 |
| T3 | 用 PyYAML 解析 `ci.yml` 的 `reference_acceptance.steps`，本地按 `bash -euo pipefail` 原样跑五个 `run` 块 | 全绿；`DRY-RUN PASS steps=7 pending=1`；`mutating call sites judged: 8/8`；负对照 `exit=10 step=tools`；合计 0.68 s |
| T4 | 直接执行门禁脚本 4 范围 × 应红态 | 4 个通过态、5 个应红态全部符合预期 |
| T5 | 实跑两个真值表：`tests/ci`（183 项，含新 2 项）、`tests/contract`（249 项，含新 1 项） | 全绿；再做 web 期望值变异，`tests/ci` 绿而 `tests/contract` 7 项红 → 两处**不同构** |
| T6 | 9 组假红变异 + 4 组 AST 宽容度探针 + 5 组假绿变异 + 1 组 B1 纯回归（`sn669-mut`） | 全部按预判命中，见 §5/§6/§8 |
| T7 | 读真实 CI：PR run `43362530051` 的逐作业/逐 step 时间；分支保护只读 API | job 11 s、不在关键路径；门禁名在必需检查列表 |
| T8 | 写本报告；复原变异 worktree；核对 `git status` | 只留本报告 |

---

## 逐条清单结论与证据

### 1. 假红风险（本审查重点）

**结论：存在四类假红，其中 AST 扫描的等价写法拒绝与延后项文案精确匹配是新引入的真假红；合成输入与 `EXTRA_REQUIRED` 的假红属于「改对必须同改 `ci.yml`」的登记式耦合。**

**(a) `DECLARED_PENDING` 子集断言**

- 规则：`ci.yml:961` 的元组里逐条列出允许出现的 `待输入` 文本；`ci.yml:968-994` 从日志抽 `待输入: ` 行，逐条做**字符串包含/相等**判断（`reason not in DECLARED_PENDING`），并核对 `pending=N` 与行数一致。
- 合法新增一个延后输入会不会红：**会**。入口里只有两处能产生 `待输入`：`Context.require`（脚本 `:263-271`，dry-run 下把失败转成 pending）和 `step_stack` 的 `stack_private_inputs` 问题（`:1250-1252`）。任何一处新增都会立刻出现未声明行 → 红。登记方式是**在 `ci.yml` 里加一行完全一致的中文字符串**——不是接口、不是配置文件、不是文档。
- 现有唯一一条声明是 `gateway 未 healthy；job-dispatch-worker 的 network_mode: service:gateway 需要它先运行`。这是**人写的说明句**：改标点、改「未」为「尚未」、补充原因，都会红（F1 实测）。也就是说，入口的一句文案优化足以拦住正确合并，而作者从失败信息里只能看到「register each in DECLARED_PENDING」，看不到该常量在哪个文件。
- 另一面：`pending` 计数只校验「声明集合是超集」，不校验该 pending 是否**应该**出现；把一条本应失败的条件登记成 pending 就能整轮变绿（见 §2 G7）。这是设计选择，但登记成本极低、门禁语义会被悄悄放宽。

**(b) AST 扫描（`Static sweep: every mutating command is judged`）对等价重构的宽容度**

判红清单（实测，见 §8）：
- `if applied.code:`——**退出码真值判定，语义等价且正确**，判红（F3）。
- `code = applied.code` 后再 `if code != 0:`——正确，判红（F4）。
- 辅助函数读 `.ok`，但调用处用关键字实参 `judge_result(result=checked)`——正确，判红（F5）。
- 方法形式辅助函数（`self._check(result)`）、跨模块辅助函数、`match result.code:` 同样不在接受集内（推断，扫描只认模块内 `ast.Name` 调用与 `ast.Compare`）。
- 任何把结果先转存别的名字再判断的写法（别名）也是红。
- 放过清单（实测）：`assert applied.ok`、`if applied.code > 0:`（`Compare` 命中）、变量重命名后仍读 `.ok`、内联 `if not ctx.mutate(...).ok:`（不计数也不报错）。

**评价：这是「以实现细节为门禁」的典型。** 接受集是「读 `.ok` / `Compare` 里读 `.code` / 交给模块内任意读某参数 `.ok` 的函数」，既窄（正确写法被拒）又宽（错误写法被放行），见 §3。

**(c) 合成私有输入假设**

`ci.yml:892-924` 写死了一份私有输入清单，隐含以下 `deploy/` 细节：

| 假设 | 合法变化时的后果 |
| --- | --- |
| `deploy/compose.env.example` 的 `SPEECHNEXUS_PRIVATE_CONFIG_DIR=./.runtime/private` | 改目录名/改绝对路径 → 合成文件写错地方，`stack` 转 pending → 红 |
| gateway 只有两条 `SPEECHNEXUS_GATEWAY_FILE_*_CONFIG_FILE`（asr、tts），路径都在 `/run/speechnexus-private/` 下 | 新增第三条 lane（如 voice-clone）→ 「缺少 gateway lane 配置」pending → 红（F6） |
| `deploy/private/` 里恰好 `gateway-file-asr`、`gateway-file-tts`、`transcription-recipe` 三个 example 文件名 | 重命名/新增 lane example → `cp` 或 pending → 红 |
| asr example 的 `vad_model_file` 指向 `silero_vad.onnx`，且脚本把合成模型写到该名字 | 升级模型文件名（`silero_vad_v6.onnx`）→「vad_model_file 不存在」pending → 红（F7） |
| asr example 的 `objects.result_bucket` = `speechnexus-results`，与 example env 的 `SPEECHNEXUS_EXECUTION_RESULT_BUCKET` 一致 | 任一侧改名 → pending → 红 |
| 合成 VAD 是文本文件（`ci.yml:917-918`） | 未来在 dry-run 里真解析 ONNX 头/结构 → 红 |
| `SPEECHNEXUS_OBJECT_STORAGE_BUCKET` / `SPEECHNEXUS_TRANSCRIPTION_INPUT_BUCKET` 必须显式非空（`EXTRA_REQUIRED`，脚本 `:80-87`） | 放宽成 compose `:-` 默认（把该键移出 `EXTRA_REQUIRED`）→ 负对照红：「the entry accepted a blank …」（F8） |

这些都是**正确的产品改动**与 CI 的对偶耦合：改对本身没问题，但必须在同一 PR 里改 `ci.yml`，而 `ci.yml` 不在这些改动的自然审查范围里。

**(d) runner 环境假设**

- `docker version --format {{.Server.Version}}`（脚本 `:866`）：需要**运行中的 docker daemon**，不是只装 CLI。`ubuntu-24.04` runner 满足（真实 run 成功，本地 Docker 29.9.3 也满足）；本地是 compose v5.7，runner 是 v2.x，脚本只要求 `v>=2`。
- `docker compose config --quiet` + 两次 `docker compose config --profile job-dispatch --format json`（`step_tools`、`step_stack`、`step_gate` 各一次，共三次渲染，与注释一致）不需要 daemon。
- `docker compose ps -a --format json`（`:763`）需要 daemon；`step_gate` 的 `service_healthy` 把它包成 `False` 并落到**已声明**的 pending，所以即使这里坏了也不会额外红（但会掩盖错误，见 §2 G-8）。
- `python3` 是**系统解释器**：`ubuntu-24.04` 是 3.12，恰好等于 `MINIMUM_PYTHON=(3,12)`；job 不跑 `uv sync`，入口的 dry-run 路径必须保持 **stdlib-only**。这条没有写在任何文档里。
- 不拉任何镜像、不访问网络（`config`/`ps`/`version` 都是本地），所以没有 registry 抖动风险；`timeout-minutes: 5` 只兜住挂死的 daemon。

### 2. 假绿风险

**结论：job 显示的 success 只等于「dry-run 的 7 个步骤都只计划、前置检查/派生值成立、判定存在」，不等于入口被守住。作者在文档里把边界写清楚了，但作业注释与提交信息的表述更强。**

逐条核它声称的覆盖与不覆盖：

| # | 显示 success 但入口可能坏的情形 | 证据 |
| --- | --- | --- |
| G1 | **判定存在但没用**：保留 `.ok` 读取、清空守卫体（`if started.ok: pass`）→ 扫描绿、job 绿 | 实测（§8 G1），注释与文档已承认这一条 |
| G2 | **带注解的赋值整条逃逸**：`applied: CommandResult = ctx.mutate(...)` 且不再判定 → 不算 site，`sites` 减 1，扫描仍报 7/7、job 绿 | 实测（G2）；这与文档「赋值后未判定即失败」矛盾 |
| G3 | **辅助函数白名单过宽**：`def annotate(label, other): if other.ok: ...`，调用 `annotate(started)`（`started` 落在 `label` 位）→ 判为「已判定」，job 绿 | 实测（G3） |
| G4 | **apply 路径完全不执行**：把 `migration_verify` 改成恒 `return None`（迁移落后也报 PASS）→ job 全绿 | 实测（G4）。`tests/ci` 的假 Runner 用例只覆盖已有行为的重放，**新增**的 apply 语义没有对应用例时无人发现 |
| G5 | **`ctx.read` 可以执行写**：dry-run 的「不写」只靠 `ctx.mutate` 自己拒绝，AST 扫描只看 `ctx.mutate`；在 `step_tools` 里加一条 `ctx.read(("sh","-c","echo proof > /tmp/…"))` → dry-run 真的写了，job 仍绿 | 实测（G5，文件确实被创建） |
| G6 | **负对照只覆盖一类腐化**：它证明的是 `run_steps` 的**通用**失败传播（同一个 `except StepFailureError` 给所有步骤），不是每个步骤自己的判定有牙；某步骤把异常吞掉并返回结论时不会被这条控制发现 | 读代码 + 实测负对照只有 `FAIL step=tools` |
| G7 | **登记即放行**：新增一个在 CI 下不成立的 `require` 条件，只要在 `DECLARED_PENDING` 里登记，job 就绿，而且最终行仍写 `DRY-RUN PASS` | 读 `require`/`run_steps` + F1 的反向操作 |
| G8 | **daemon 异常被降级成已声明 pending**：`service_healthy` 捕获 `StepFailureError` 返回 `False`，于是 `docker compose ps` 失败与「gateway 没起」在日志里不可区分；前者会被当作后者放行 | 读 `:762-793` |
| G9 | **`ctx.read` 的语义**：AST 扫描只检查 `ctx.mutate`，不检查是否有写命令被塞进 `ctx.read`，也不检查是否出现别的执行原语（`subprocess` 直调）。扫描的正确性建立在一个未被检查的约定上 | 读 `Context.read`/`Runner` |

**门禁接线本身没有假绿**：门禁脚本对 `failure`/`cancelled`/空值一律红，对 docs/web 只接受 `skipped`（§4 实测），`needs` 缺失/改名会让工作流校验失败而不是静默放行。真正的风险在「job 绿 ≠ 入口守住」，而这一条在 `docs/validation/ci-validation.md` 的覆盖边界段是**写对了**的（见 §7）。

### 3. AST 扫描的正确性

**结论：作为「B1 回归的文本层兜底」它够用（纯丢弃必红，实测 7/8），但它的接受规则既窄又宽，且实现与文档不一致。**

**(a) 「判定存在」的规则**

1. `ast.Expr` 且直接是 `ctx.mutate(...)` → 结果被丢弃 → 记问题。
2. `ast.Assign` 且只有一个 `ast.Name` 目标、值直接是 `ctx.mutate(...)` → 在该函数**整棵 AST**里找任一：`目标.ok` 属性读取；包含 `目标.code` 的 `ast.Compare`；或调用一个「模块内、某参数被读 `.ok`」的函数且该调用把目标作为**位置实参**。
3. 其它形态（注解赋值、属性赋值、多元赋值、`return ctx.mutate(...)`、作为实参传递、内联 `ctx.mutate(...).ok`、`self.x = ...`）**既不计数也不检查**。

**(b) 能否被「读了 `.ok` 但没用」绕过：能**，且这是作者自己承认的边界（G1 实测）。除此之外还有作者没提到的绕过：G2（`AnnAssign`）、G3（白名单参数错位）、以及「把结果交给 `print(r.ok)` 只为打日志」。

**(c) 是否会漏掉 `ctx.read` 返回值的写操作：会，而且比「漏掉」更重——dry-run 里它真的会执行。** `step_tools` 里插入一条 `ctx.read(("sh","-c","echo proof > /tmp/…"))`，本地复跑：文件被创建、`DRY-RUN PASS` 照旧、扫描 8/8 绿（G5）。`ctx.read` 与 `ctx.mutate` 的区别只是命名约定，没有任何静态或运行时检查把写命令挡在 `read` 外面；`ctx.compose(...)` 拼出来的 argv 也一样可以由 `read` 执行。所以「`--dry-run` 不变更数据库/对象存储/角色」这句文档承诺，在 CI 里有赖于作者不变的手写纪律。

**(d) `gate_verdict` 这类白名单是否过宽：过宽。** 辅助函数识别是**函数级**的：只要函数体里任何一个参数被读 `.ok`，整个函数名进 `ok_helpers`；调用侧只要把目标放在**任意位置实参**里就算判定。于是：

- `gate_verdict(result)` 这个真实用例是对的；
- `annotate(label, other)`（读 `other.ok`）把目标放在 `label` 位也叫「判定」（G3 实测绿）；
- 函数自己的 `.ok` 读取在死分支里、或在判断别的参数，都算数；
- 关键字实参、`*args` 展开、方法调用、跨模块辅助函数反而**不算**（F5 实测红）。

**(e) 实现细节上的其它问题**

- `nodes = list(ast.walk(function))` 会包含嵌套函数，嵌套函数里的 `ctx.mutate` 会被外层和内层各算一次（当前脚本没有嵌套，故 8/8 恰好正确），并且外层同名变量的 `.ok` 读取可以「冒充」内层的判定。
- 计数不可信：`mutating call sites judged: N/N` 的 `N` 只统计被识别的两种形态；`AnnAssign` 等形态连 site 都不算，所以「N/N」不能当覆盖率看（G2 是 7/7）。
- `is_mutate` 只认字面 `ctx.mutate`（`ast.Name` 为 `ctx`），`context.mutate`、包装函数、`getattr` 全部逃逸。
- 扫描只读一个文件；入口若把写判定抽到 `scripts/dev-env/` 的另一个模块，扫描看不见。
- 顺带：如果入口出现语法错误，扫描步骤以 traceback 失败（exit 1，无结构化 `::error::`），归因不如其它步骤清晰。

### 4. 门禁接线的完整性

**结论：与 `deployment` 完全同构，`skipped` 在 docs/web 下被正确允许，四种范围没有漏项。**

证据：

- `ci.yml:710-716` 在门禁环境里加了 `ACCEPTANCE_RESULT`；`:690-701` 的 `needs` 加了 `reference_acceptance`；`:735-748` 四个 `case` 分支加 `acceptance=`，取值与 `deployment` 逐行相同（docs/web `skipped`，no-db/full `success`）；`:751-756` 的与条件与错误行都加了该变量。
- 作业条件：`reference_acceptance` 的 `if`（`:873`）与 `deployment`（`:824`）逐字相同（`full || no-db`），`needs: [changes]` 也相同；doc 注释与 `timeout-minutes: 5`（`:879`）不改变语义。
- 直接执行门禁脚本（`bash -euo pipefail`，环境同 `steps[0].env`）的结果：

| 输入 | 退出码 | 末行 |
| --- | --- | --- |
| docs（含 acceptance=skipped） | 0 | `Verified scope=docs; …` |
| web（含 acceptance=skipped） | 0 | `Verified scope=web; …` |
| no-db（含 acceptance=success） | 0 | `Verified scope=no-db; …` |
| full（含 acceptance=success） | 0 | `Verified scope=full; …` |
| no-db + acceptance=skipped | 1 | `::error::Scope=no-db; … acceptance=skipped` |
| full + acceptance=skipped | 1 | `::error::Scope=full; … acceptance=skipped` |
| web + acceptance=success | 1 | `::error::Scope=web; … acceptance=success` |
| docs + acceptance=cancelled | 1 | `::error::Scope=docs; … acceptance=cancelled` |
| docs + acceptance=""（缺失） | 1 | `::error::Scope=docs; … acceptance=` |

- 范围组合是否漏掉：`scripts/dev-env/ci-routing.py` 的 `Mode` 只有 `docs/web/no-db/full` 四种，不存在第五种。本轮实测 `classify()`：`.github/workflows/ci.yml`、`scripts/dev-env/reference-acceptance.py`、`deploy/compose.yaml`、`deploy/compose.env.example`、`deploy/private/*.example.json`、`tests/ci/…` 全部 → `full`（即该 job 会跑）；`docs/**/*.md` → `docs`，`web/<app>/src/*.tsx` → `web`（该 job 跳过且门禁期望 skipped）。**「只有 workflow 文件变更」会路由到 full，不会漏跑。**
- `skipped` 的语义：门禁脚本把 docs/web 下的 skipped 当作「不适用」而不是「证据缺失」，与既有 lane 一致；`main` push / `workflow_dispatch` 一律 full，会跑该 job。
- 受保护状态名未变（三个），只读 API 实测 `required_status_checks.contexts` = `["Gate: required checks for this change", "Web: …", "Compose: configuration validation"]`，`strict: false`。该 job 的失败/跳过只会通过门禁作业放大成红，不会新增受保护名。

### 5. 真值表互锁

**结论：两处不是「断言同一件事」——`tests/contract` 是更强的真值表，`tests/ci` 是它的子集（缺 web 范围）。两处都跑在 CI 里，所以现实覆盖率没有缺口，但单看 `tests/ci` 会漏。**

- `tests/contract/test_ci_workflow.py`：断言门禁作业的 `needs` 集合、`steps[0].env` 全字典、四个范围的 `_EXPECTED`（含 `ACCEPTANCE_RESULT`），并新增按范围参数化的用例，其期望值 `"skipped" if mode in ("docs","web") else "success"` 是**独立于 `_EXPECTED` 的第二份硬编码**——改门禁时两份都要改，形成三元互锁（脚本、`_EXPECTED`、硬编码映射）。
- `tests/ci/test_node_controller_image_ci.py`：只覆盖 `docs`（接受 skipped、其余状态红）与 `full`/`no-db`（接受 success、skipped/failure/cancelled/空值红）；**没有 web 用例**，也不断言 `needs`/`env` 字典。
- 实测互锁方向：把 `ci.yml` 的 web 分支改成 `acceptance=success`（模拟接错），`tests/ci` 12 项**全绿**，`tests/contract` **7 项红**（含新用例的 `[success-web]`/`[skipped-web]`）。反向（门禁完全不看 `ACCEPTANCE_RESULT`）两边都红。
- 两个文件都从同一份 YAML 抽同一段脚本执行，不是独立实现，因此它们只能发现「期望值不一致」，发现不了共同的假设错误（例如都以为 `needs` 的语义就是 GitHub 的语义）。

### 6. 增量成本

**结论：作者陈述的「11–12 s、并行、不在关键路径」成立；实测 11 s。**

真实 run `43362530051`（PR head `c3141f21`，`success`）：

| 作业 | 开始 | 结束 | 结论 |
| --- | --- | --- | --- |
| Deploy: reference acceptance entry (dry-run execution) | 03:59:44 | 03:59:55 | success（**11 s**） |
| Integration 2（最后结束的分片） | 03:59:44 | 04:10:12 | success |
| Gate: six-shard integration evidence and merged coverage | 04:10:14 | 04:11:01 | success |
| Gate: required checks for this change | 04:11:03 | 04:11:07 | success |

该 job 内部步骤（GitHub 记录）：Set up job 1 s、checkout 3 s、setup-uv 1 s、prepare/execute/verify/sweep/negative-control 各 ≤1 s。它 `needs: [changes]`，与 python_checks、integration_shards、web 同波次启动，因此**不增加关键路径**——门禁本来就要等分片，等该 job 不需要额外时间。

本地等价计时（`/tmp/run_acceptance_job.py`，按 `ci.yml` 原样解析执行五个 `run` 块，跳过两个 action step）：

| 步骤 | 墙钟 |
| --- | --- |
| Prepare（cp + 合成私有输入） | 0.03 s |
| Execute（entry `--dry-run`） | 0.40 s |
| Verify（日志断言） | 0.02 s |
| Static sweep（AST） | 0.05 s |
| Negative control | 0.17 s |
| **合计** | **0.68 s** |

与注释「entry point plus both controls measured under 1 s locally」一致；`| entry wall clock | 1s |` 的 summary 是 `$SECONDS` 取整，实际 0.40 s。代价是每个 full/no-db 的 run 多一个 hosted runner 作业，量级可接受。

### 7. 文档

**结论：`docs/validation/ci-validation.md` 的「本 job 覆盖什么/不覆盖什么」足以让运维与维护者判断边界；但它没有说明哪些输出字符串已被钉成契约，也没有写「新增步骤/新增 lane 时要同步改哪里」。危险措辞不在文档，而在 `ci.yml` 的作业注释。**

- `docs/validation/ci-validation.md:28、30、32`（本片新增）三段：门禁映射、两条不依赖执行的守护、覆盖边界。第三段明确写「**不是一次参考部署**」「只存在于 apply 路径的语义，本作业只能靠 AST 扫描确认『有判定』」「任何把『入口有 CI 作业』当作『入口已被完整守住』的推论都不成立」——这句正是本次审查要验证的命题，文档给出了正确回答，**没有**让读者误以为 apply 分支被覆盖。
- `docs/validation/ci-validation.md:57` 表格行描述作业内容与「不启动服务或执行写步骤」，与实测一致。
- 缺口：
  1. 没写「`order=` 首行、`[i/7] <ident> —`、`待输入: ` 前缀与内容都已被作业精确匹配」——维护者改文案时不知道会红；
  2. 没写「新增步骤必须同时改 `ci.yml:961-979` 里的 `DECLARED_PENDING`、步序列表、`order=` 与 `steps=7` 断言」；
  3. 没写「合成私有输入清单在 `ci.yml:915-922`，新增 lane/改 VAD 文件名要同步」；
  4. 没写「作业用系统 python3、不 `uv sync`，入口 dry-run 路径必须保持 stdlib-only」。
- 越界措辞：`ci.yml:852-853` 的注释 `This job runs the real process the way an operator does, so corrupting the entry itself fails a PR.` 与 G4 实测矛盾（破坏 apply 路径仍绿）。`docs/validation/ci-validation.md` 本身没有这句话的问题；建议只在注释上收敛，不要把文档改得更弱。
- `docs/operations/reference-acceptance.md` 没有因为本片更新。它是入口的运维主文档，把 `DRY-RUN PASS steps=7` / `PASS steps=7` 写成「结论」，但没有 `order=` 首行的样例，也没有「CI 会把这几行当契约」的说明。本片不动它不算错误（本片没改入口），但上面第 1 条缺口最好落在它里面。

### 8. 对 `reference-acceptance.py` 的影响

**结论：本片没有改入口脚本（实测），但把入口的六处输出/说明文字提升成了合并门禁契约，其中至少三处（`order=` 首行、步骤首行格式、延后说明正文）是偶然措辞。**

- 未改入口：`git show --format="" --name-only 7d575298` 不含该脚本；`git diff --stat 7d575298^ 7d575298 -- scripts/dev-env/reference-acceptance.py` 为空；`git log --oneline 7d575298..origin/main -- scripts/dev-env/reference-acceptance.py` 无输出（基线漂移未触及它）。
- 依赖的入口输出，逐个定性：

| 被钉住的字符串 | 出处 | 定性 |
| --- | --- | --- |
| `reference acceptance: DRY-RUN PASS steps=7` | 脚本 `:1509`；`docs/operations/reference-acceptance.md:56` 写「`--dry-run` 的结论是 `DRY-RUN PASS steps=7`」 | **文档化的机器可读结论**，算稳定契约 |
| `steps=7`（步数） | 同上；apply 侧 `PASS steps=7` 还被 `tests/ci/test_reference_acceptance.py:927` 断言 | 准契约：加步骤本来就要求同步文档与测试，CI 只是又多一处 |
| `order=tools, migrate, roles, buckets, stack, gate, health` | 脚本 `:1595`；**只有 review 文档提到，`docs/operations` 里没有这行样例** | **偶然措辞升格**：首行重排/加字段即红，文档无法解释 |
| `[1/7] tools — …` 步骤首行格式（`^\[\d+/7\] (\w+) —`） | 脚本 `:1474`；文档只写步骤表与顺序 | **偶然措辞升格**：改分隔符/编号样式即红 |
| `待输入: ` 前缀 | 脚本 `:269`；`ci-validation.md` 本轮首次把它写成契约 | 半契约：前缀可用，但**内容**（整句中文）也被精确匹配 |
| 那条延后说明的正文 | 脚本 `:1168` | 纯人类措辞，却按整句精确匹配（F1） |

因此：「未来入口改文案会让门禁假红」不是假设，是本片引入的确定行为，且改文案的人从 `docs/operations` 或入口自身都看不到这条约束。

---

## 假红清单（最重要）

| ID | 正确改动 | 后果 | 实测 |
| --- | --- | --- | --- |
| FR1 | 把 gate 步的延后说明写得更清楚（语义不变） | 门禁红：`undeclared deferred input(s), register each in DECLARED_PENDING: <新文案>` | F1 |
| FR2 | 新增一个合法的延后输入（新 `require` 或新的静态私有输入检查） | 红，直到在 `ci.yml:961` 常量里加一行**逐字相同**的字符串 | F6、F7 的机制 |
| FR3 | 新增/重排入口步骤（并同步文档与 `tests/ci`） | 红：`step sequence … != […]`、`header order line …`、`missing the dry-run PASS verdict` 三条同时报 | F2 |
| FR4 | 用退出码真值判定 `if applied.code:`（等价且正确） | AST 扫描红：`never judges applied.ok / applied.code` | F3 |
| FR5 | 先把 `.code` 绑到局部再比较 | 同上红 | F4 |
| FR6 | 把结果交给用**关键字**实参调用的辅助函数 | 同上红 | F5 |
| FR7 | 把判定抽到别的模块的辅助函数（`from … import judge`，合法重构） | 红：`never judges checked.ok / checked.code`（扫描只认模块内既有 `ok_helpers`） | 实测（新建 `scripts/dev-env/_review_judge.py` 后扫描红） |
| FR8 | 新增第三条 gateway file lane（compose + example 都改好） | 红：`缺少 gateway lane 配置: …/gateway-file-voice-clone.json` 未声明 | F6 |
| FR9 | 升级 VAD 模型文件名（asr example 指向新文件） | 红：`vad_model_file 不存在` 未声明 | F7 |
| FR10 | 让 artifact 桶回落到 compose 的 `:-` 默认（移出 `EXTRA_REQUIRED`） | 红：负对照 `the entry accepted a blank SPEECHNEXUS_OBJECT_STORAGE_BUCKET` | F8 |
| FR11 | 仅重排首行/改结论措辞（`DRY-RUN PASS` → `DRY-RUN PASSED`） | 红：`missing the dry-run PASS verdict` | F9 |
| FR12 | 入口 dry-run 路径引入一个项目依赖（如用 sqlalchemy 解析迁移） | 红：job 用系统 python3、不 `uv sync`，`ModuleNotFoundError` | 读 `ci.yml:870-891`（job 只 `setup-uv`，不 `uv sync`；本地系统 python 也缺项目依赖，机制可推） |

FR4–FR6 是**真假红**：它们拦的是语义完全正确的代码，且没有「登记一下就放行」的通道，作者只能改回扫描喜欢的形状。FR1/FR3/FR11 是「用实现细节当契约」，可以在同一 PR 改 `ci.yml` 放行，但归因不指向 `ci.yml`。FR8–FR10 是「改对必须同改 CI」的对偶耦合。

---

## 假绿清单

| ID | 损坏方式 | 该 job 的表现 | 实测 |
| --- | --- | --- | --- |
| FG1 | 保留 `.ok` 读取但清空守卫体 | 绿（作者已声明的边界） | G1 |
| FG2 | `applied: CommandResult = ctx.mutate(...)` 且不再判定 | 绿；扫描仍报 `7/7` | G2 |
| FG3 | 结果交给「读另一个参数 `.ok`」的辅助函数（参数错位） | 绿；扫描认为已判定 | G3 |
| FG4 | 破坏 apply 路径判定（`migration_verify` 恒 `None`） | 全绿（dry-run 不执行 apply 路径） | G4 |
| FG5 | 通过 `ctx.read` 执行写命令 | 绿；dry-run 真的写了（本地 `/tmp` 文件被创建） | G5 |
| FG6 | 某步骤吞掉自己的失败并返回结论 | 绿；负对照只证明通用失败传播，不证明各步判定 | 读 `run_steps` + 负对照实测 |
| FG7 | 把本应失败的前置条件登记成 pending | 绿，且最终行仍写 `DRY-RUN PASS` | 读 `require`（F1 的反向） |
| FG8 | `docker compose ps` 失败 | 降级成已声明的 pending，与「gateway 未起」不可区分 | 读 `:762-793` |
| FG9 | B1 的「读退出码但读错/判错」 | 扫描只保证「读了 `result.ok`」，不保证判定正确；`tests/ci:636` 的假 Runner 用例只对已写下的行为有断言，PR 新增/改动的 apply 语义若没有对应用例，本 job 也发现不了 | 读代码 + `tests/ci` 636/671 行 |

`FG4`/`FG9` 的缓解措施在文档里写对了：`tests/ci` 的假 Runner 用例与真实 apply 运行负责 apply 语义；本片只负责「有判定」。真正的问题是 `ci.yml` 注释把这条说成了「corrupting the entry itself fails a PR」。

---

## AST 扫描的评价

- **它解决了一个真实问题**：B1 的纯回归（`started = ctx.mutate(...)` 后不判）在 dry-run 下完全不可见，而扫描能抓住（实测 `7/8`、`step_stack:1269 never judges`）。
- **它不能承担「门禁」这个重量**：接受集是「形状」而非「语义」，于是同一份正确代码换个等价写法就红（FR4–FR6），同一份错误代码换个赋值写法就绿（FG2–FG3）。一个既误杀又放行的检查，在**合并阻断**位置上会同时制造摩擦和虚假安全感。
- **它的正确性依赖未声明的约定**：`ctx.read` 必须只读、写只能走 `ctx.mutate`、入口不把判定抽到别的模块。这三条都没有被检查，也没有写进文档（第一条最危险，因为 dry-run 会真的执行写）。
- **改进方向（按性价比）**：
  1. 判定识别改为「目标的 `.ok`/`.code` 出现在 `if`/`while`/`assert`/`match`/`or`/`and`/`not`/`Compare` 任一判定位，或作为实参（位置或关键字）交给同模块读该形参 `.ok` 的函数」；按**形参名**而不是「函数内任一参数」匹配辅助函数；
  2. `Assign`/`AnnAssign`/`AugAssign`/属性目标/`return`/实参传递都算 site，**看不出来就报出来**（宁可多问，别静默跳过）；
  3. 把「`ctx.read` 只读」纳入检查（例如文档化白名单命令，或至少把 `ctx.read` 的 `compose run/up`、`sh -c` 之类判红）；
  4. 失败信息带「接受写法清单 + `ci.yml` 路径/行号」。

---

## 我独立复现的反例（原始记录）

所有变异只在 `/private/tmp/sn669-mut` 上做，跑完 `git checkout -- .` 复原，未 push。执行器 `/tmp/run_acceptance_job.py` 直接解析 `ci.yml` 的 `reference_acceptance.steps`，按 `bash -euo pipefail` 顺序执行五个 `run` 块（action step 跳过）。

### 基线（未变异）

```text
[3] Prepare …: exit=0 wall=0.03s
[4] Execute …: exit=0 wall=0.40s
[5] Verify every step really ran …: exit=0 wall=0.02s
[6] Static sweep …: exit=0 wall=0.05s   mutating call sites judged: 8/8
[7] Reject a corrupted environment …: exit=0 wall=0.17s   negative control: exit=10 step=tools
TOTAL run steps wall clock: 0.68s
```

### F1 延后说明改文案

```text
-> exit=1 (RED)
::error::undeclared deferred input(s), register each in DECLARED_PENDING:
  gateway 尚未 healthy；job-dispatch-worker 通过 network_mode: service:gateway 复用其网络，需先运行
```

### F2 新增第八步

```text
-> exit=1 (RED)
::error::step sequence [] != ['tools','migrate','roles','buckets','stack','gate','health']
::error::header order line does not name the documented step order
::error::missing the dry-run PASS verdict
```

（`sequence []` 是因为首行成了 `[1/8]`，正则 `^\[\d+/7\]` 不再匹配——这也顺带说明 verify 步骤的解析对步数很敏感。）

### F3/F4/F5 AST 等价写法

```text
F3 `if applied.code:`                        -> exit=1  ::error::step_migrate:940 applied = ctx.mutate(...) never judges applied.ok / applied.code
F4 `code = applied.code; if code != 0:`      -> exit=1  同上
F5 `judge_result(result=checked)`（关键字实参）-> exit=1  ::error::step_buckets:1071 … never judges checked.ok / checked.code
```

宽容度探针（同一扫描）：

```text
变量重命名后仍读 .ok                       -> exit=0 GREEN
assert applied.ok                          -> exit=0 GREEN
if applied.code > 0:（Compare）            -> exit=0 GREEN
内联 if not ctx.mutate(...).ok:            -> exit=0 GREEN（且不计数）
```

### F6 新增第三条 gateway lane

```text
-> exit=1 ::error::undeclared deferred input(s) … 缺少 gateway lane 配置:
   /private/tmp/sn669-mut/deploy/.runtime/private/gateway-file-voice-clone.json
```

### F7 VAD 文件名升级

```text
-> exit=1 ::error::undeclared deferred input(s) … gateway-file-asr.json 的 vad_model_file 不存在:
   /private/tmp/sn669-mut/deploy/.runtime/private/silero_vad_v6.onnx
```

### F8 把 artifact 桶移出 `EXTRA_REQUIRED`

```text
-> exit=1 ::error::the entry accepted a blank SPEECHNEXUS_OBJECT_STORAGE_BUCKET (exit 0)
```

### F9 结论行改措辞

```text
-> exit=1 ::error::missing the dry-run PASS verdict
```

### G1 保留 `.ok`、清空守卫（buckets 启动）

```text
-> exit=0 GREEN
```

### G2 注解赋值且不再判定

```text
applied: CommandResult = ctx.mutate(migrate)   # 旧守卫删除
-> exit=0 GREEN   （sweep 输出仍为 7/7，site 静默少了一个）
```

### G3 白名单参数错位

```python
def annotate(label: str, other: CommandResult) -> None:
    if other.ok:
        print(label)
...
started = ctx.mutate(start, env=ctx.child_env)
annotate(started)          # started 落在 label 位，other 无人传
```
```text
-> exit=0 GREEN
```

### G4 破坏 apply 路径判定

```python
def migration_verify(head, database_version) -> str | None:
    return None          # 迁移落后也报 PASS
```
```text
-> exit=0 GREEN（dry-run 不调用该函数）
```

### G5 用 `ctx.read` 执行写

```python
ctx.read(("sh", "-c", "echo proof > /tmp/acceptance-dryrun-write.txt"))
```
```text
-> exit=0 GREEN    side effect written during dry-run: True
```

### B1 纯回归（删掉 `step_stack` 的退出码判定）

```text
-> exit=1  ::error::step_stack:1269 started = ctx.mutate(...) never judges started.ok / started.code
   mutating call sites judged: 7/8
```

### 真值表互锁方向

把 `ci.yml` 的 web 分支改成 `acceptance=success`：

```text
tests/ci（unittest discover -s tests/ci -p test_node_controller_image_ci.py） -> Ran 12 tests, OK   （绿）
tests/contract（pytest tests/contract/test_ci_workflow.py）                  -> 7 failed, 242 passed
   含 test_final_gate_requires_the_reference_acceptance_entry[success-web]
   含 test_final_gate_requires_the_reference_acceptance_entry[skipped-web]
```

### 测试实跑

```text
python3 -m unittest discover -s tests/ci        -> Ran 183 tests … OK（含新 2 项）
pytest tests/contract/test_ci_workflow.py       -> 249 passed（含新 1 项）
```

（`tests/contract` 在本机需要 `pytest/pyyaml/psycopg/pytest-asyncio`；用 `uv run --no-project --with …` 跑的，未改 `uv.lock`。）

---

## 只能依赖作者陈述 / 未验证的点

1. **未来入口演进意图**：我无法验证「`order=` 首行与 `[i/7] —` 格式会被长期保持不动」。这决定了 FR11 的实际发生率；文档没有把它们写成契约，所以按「偶然措辞」处理。
2. **分支保护的实际策略细节**（`strict:false`、是否需要 PR 审核等）我只做了只读查询；无法验证仓库管理员是否会为新增作业再加一条独立必需检查（作者说不会，工作流也确实没加）。
3. **`tests/ci` 的假 Runner 用例对 apply 语义的覆盖完整度**：我只确认了 `test_reference_acceptance.py:636/671` 覆盖 B1（`compose up` 退出码）；该文件其余 38 项的覆盖面未逐条核（超出本片范围）。
4. **GitHub hosted runner 的 docker/compose/python 版本**：我依据真实 run 成功与本地等价（Docker 29.9.3 / compose v5.7 / python 3.14）推断，未在 `ubuntu-24.04` 上复现；`docker version`、`config`、`ps` 三个命令的实际行为以真实 run 成功为间接证据。
5. **作者注释「The entry point plus both controls measured under 1 s locally」**：本地 0.68 s 支持该说法；但这是本机 mac 的数据，不代表 runner。
6. **`DECLARED_PENDING` 是否会被作者当作「每次新增延后项的例行编辑」**：文档这么写、实现也这么要求；我无法验证运维是否知道该常量在 `ci.yml:961`（失败信息未给位置）。

---

## 复现命令

```bash
# 0) 独立 worktree（本报告所在）
git worktree add /private/tmp/sn669 -b review/669-ci-acceptance-gate origin/main

# 1) 按 ci.yml 原样执行该 job 的五个 run 块（跳过 action step），并计时
python3 /tmp/run_acceptance_job.py /private/tmp/sn669

# 2) 基线测试
cd /private/tmp/sn669 && python3 -m unittest discover -s tests/ci
cd /private/tmp && uv run --no-project --with pytest --with pytest-asyncio --with pyyaml \
  --with 'psycopg[binary]' python -m pytest /private/tmp/sn669/tests/contract/test_ci_workflow.py -q

# 3) 变异实验（在 review/669-mut 上改一行 → 跑 job 的 run 块 → 复原）
python3 /tmp/mutate_acceptance.py            # 9 组假红 + 5 组假绿

# 4) 门禁脚本直跑（4 范围 × 应红态）
#    见 §4 表格；脚本取自 ci.yml jobs.python.steps[0].run

# 5) 真 CI 时长与必需检查（只读）
gh api repos/hrygo/SpeechNexus/actions/runs/43362530051/jobs
gh api repos/hrygo/SpeechNexus/branches/main/protection
```

（`/tmp/run_acceptance_job.py` 与 `/tmp/mutate_acceptance.py` 是本轮临时脚本，未入库：前者用 PyYAML 解析 `ci.yml` 的 `reference_acceptance.steps`、按 `bash -euo pipefail` 顺序执行 `run` 块并把 `RUNNER_TEMP`/`GITHUB_STEP_SUMMARY` 指到临时目录；后者对每个实验做「字符串替换 → 跑前者 → `git checkout -- .` 复原」。复现时按这两条逻辑重写即可。）

---

## 附：本片与上一轮审查的关系

上一轮（`8281c2bb` 返修）确认了 B1 在 apply 路径已修、由 `tests/ci` 的假 Runner 用例守着。本片没有重新改入口，而是把「入口有没有被真正执行」补进 CI，并加了两条不依赖执行的守护。于是本轮的判断标准是**这道新门槛本身**：它挡住了什么（dry-run 可见的前置/派生/顺序/延后项/判定存在性）、它挡住了正确的改动吗（§1/§5 假红清单）、它漏了什么（§2 与「假绿清单」）、以及它有没有让后来者误以为 apply 分支已被覆盖（§7：文档没有，`ci.yml` 注释有）。结论见开头。
