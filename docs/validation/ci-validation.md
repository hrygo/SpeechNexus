---
title: CI 验证范围与维护
status: active
---

# CI 验证范围与维护

CI 使用[工作流](../../.github/workflows/ci.yml)和[标准库路由](../../scripts/dev-env/ci-routing.py)选择验证范围。未知路径保守运行全量，不维护易遗漏的业务测试白名单。

| 触发或变更 | 范围 |
| --- | --- |
| PR 仅修改根 `README.md` / `README.en.md` / `AGENTS.md`、`docs/` 内 Markdown、`.agents/skills/<skill>/` 内 Markdown / JSON / YAML | 路由回归、审查 PR 路径边界、Design Token 静态门禁、Markdown 代码块格式检查（独立作业 `docs_format`）、差异检查、矩阵新鲜度检查（仅矩阵、关联源、快照或生成器变化时重渲染）、Node Controller 镜像范围检查与其门禁、最终门禁；镜像构建按独立范围判定，通常跳过。不安装项目依赖、不启动业务测试服务。差异检查以取得准确基线为前提；否则回退全量 |
| PR 仅修改已知 Web 源码/静态资产，可混合上述文档 | Python 静态/契约检查、覆盖率数据采集、完整 Web 检查，以及 Web 能力道 `Integration 4`/`Integration 5`（真实后端 + PostgreSQL/MySQL/MinIO + 真实浏览器）；采集作业仍使用私有 PostgreSQL 和 MinIO，DB-only 分片 0–3、覆盖率判定与部署检查不适用 |
| PR 仅修改已知独立引擎目录或明确列出的引擎单元测试，可混合上述 Web/文档 | `no-db`：保留 Python 静态/契约检查、覆盖率数据采集、完整 Web、构建和部署检查；采集作业仍使用私有 PostgreSQL 和 MinIO，六个双数据库集成分片、覆盖率判定及其汇总不适用 |
| CI、脚本、测试、依赖、配置、契约、迁移、部署、其他路径或空/不确定差异 | 完整 Python/Web/部署检查，六个 PG/InnoDB/MinIO 分片及证据汇总 |
| `main` push、手动运行及未知事件 | 始终全量 |

精确白名单以路由脚本为准。新增应用、资源类型或目录默认全量；缩窄范围前先证明其不依赖被省略的验证，并添加回归。

Skill 的说明、参考、合成评测数据及展示元数据不参与产品构建，复用文档轻量路径。技能内的 Python、Shell、JavaScript 等脚本和未知文件仍走全量；混合业务代码时按业务范围选择，其他 `.agents/` 配置不在白名单内。轻量路径不执行 skill 行为评测，也不证明 JSON/YAML 内容或引用有效，维护者须按变更核对。`main` push 仍按既有约定全量验证。

独立引擎目录白名单为 `deploy/engines/` 下的 `cpu-vad`、`file-asr-audio`、`vllm-asr-lifecycle`；测试白名单为 `test_native_asr_scope.py`、`test_worker_fence.py`（均在 `tests/unit/gateway/`）。集成测试会导入其他部分单元测试工具，因此不按整个 `tests/unit/` 跳过数据库。后端/网关业务实现、共享测试工具、契约、迁移、共享依赖和 CI 配置仍触发全量；任意混合变更取更严格的范围。主干 push 与手动运行继续全量。引擎若接入数据库集成测试，须同步收紧路由并补回归。

## 必需检查与证据

三个受保护检查为 `Gate: required checks for this change`、`Web: lint, types, tests, build and accessibility`、`Compose: configuration validation`。它们分别替代原 `Python quality and tests`、`Web quality, tests, and build`、`Deployment configuration`；变更显示名称时必须同步 `main` 的 required status checks，并保持 GitHub Actions app 绑定及其他保护策略。迁移时先确认新名称在当前提交实际通过，再替换保护列表，不将缺失检查视为通过。

验收入口作业 `reference_acceptance` 由 `Gate: required checks for this change` 按与 `deployment` 相同的范围映射要求：docs/web 范围期望 skipped，no-db/full 范围期望 success，失败、取消或该跑却跳过都使门禁失败。它不新增受保护状态名，三个受保护检查保持不变。它真实执行 `scripts/dev-env/reference-acceptance.py --dry-run`：工具与版本、从 `deploy/compose.yaml` 派生的 `${KEY:?}` 键与入口额外必需键、compose 渲染、三条互不相同的 Job DSN 身份、从迁移目录派生的 head、桶名和私有输入扫描都由脚本本身计算；合成私有输入按 `deploy/compose.yaml` 挂载的文件名与 lane 配置里的 `vad_model_file` 派生，新增 lane 或改模型文件名不需要改工作流；新增私有挂载必须同时提交 `deploy/private/<name>.example.json`（非 JSON 挂载同样按 `<name>.example.json` 命名，当前只挂 3 个 `.json`），缺示例时准备步骤直接失败——合成的空 JSON 只会让私有输入扫描凭「文件可读」假通过，所以这里没有降级通道。

作业另有四条不依赖写步骤的守卫，实现在 `scripts/dev-env/ci-entry-guard.py`，只做**结构完整性**检查，不做语义校验：

- `log` 校验[机器可读输出契约](../operations/reference-acceptance.md#机器可读输出ci-契约)：步骤顺序、逐条结论、`pending=N` 与 `待输入` 行数、dry-run 不得声明 PASS；延后项按稳定词登记（`DECLARED_DEFERRALS`，匹配「必须同时出现的词」并排除别的条件词），新增延后项必须一行登记，未登记即红。
- `sites` 把入口里的每条 `<ctx>.mutate(...)`（含 `ctx.runner.mutate`）与 `MUTATE_SITES` 清单逐条比对：命令消失、命令新增、结果被丢弃（裸表达式语句，或赋给 `_`——赋值即红，不看之后有没有再读 `_`）、`read` 的 argv 既不是字面量也不是 `READ_ARGUMENTS` 里登记的 argv 构造助手或 `*` 展开的登记只读段、`read` argv 里出现写词（`run/up/start/down/restart/rm/create`；`uv run` 是唯一允许配对；`sh`/`bash`/`python` 等紧跟 `-c` 一律判红）都使作业红。清单的「判定在哪」列必须用反引号点出入口里真实存在的定义，改名或删函数即红；「证据」列必须指向一个会被 `unittest` 收集的 `TestCase` 方法（`tests/ci` 下 `test*.py` 模块、方法名以 `test` 开头、所在类沿同文件基类回溯能追到 `TestCase`，从别的模块导入的基类不认识）或 probe 用例。以下都红：名字不存在、不在被收集的模块里，或同名方法在多个 `TestCase` 里出现（证据列只写裸方法名，守卫不按 `glob` 顺序挑；同名多处必须改成 `test:<模块>::<类>::<方法名>`，或给方法改名）；同一个类里同名方法定义多次（守卫读第一个、运行时用最后一个）；类体里赋值重新绑定证据方法名（`test_x = unittest.skip(...)(test_x)`）；方法或类（含同文件基类）上带 `@unittest.skip`/`@expectedFailure`，或条件为真的 `skipIf`/`skipUnless`（`skipIf(False)` 这类恒不触发的不算；条件不是字面量布尔值则按「无法确认会运行」算红，例外是能在 CI 平台 Linux 上算出值的平台条件：`sys.platform`／`platform.system()`／`os.name` 的 `==`/`!=`/`startswith`/`endswith`、它们的 `not`/`and`/`or` 组合，以及绑定到这类表达式的模块级常量（该名字在整个模块里必须只被绑定一次，且那次是模块级简单赋值；`for` 目标、`|=` 这类增量赋值、元组/列表解包、`with … as`、`except … as`、推导式目标、海象、`import … as`、`def`/`class`、`type` 别名（PEP 695）、`global`/`nonlocal` 声明和 `del` 都是第二次绑定，出现即不可证明、判红）——算出仍会运行则绿，算出会跳过则红）；`setUp`/`setUpClass`（含同文件基类）或正文里调 `skipTest`／`raise SkipTest`（写在 `if sys.platform == "win32":` 这类可证明在 Linux 上不进入的分支里的不算）；正文开头就 `return`/`raise`，或写在字面量恒真分支里让后文成死代码；正文没有任何断言；断言是守卫认得出的字面量恒真形态（`assert True`、`assertTrue(True)`、`assertFalse(False)`、`assertEqual`/`assertIs` 两边同一字面量、`assertNotEqual`/`assertIsNot` 两边不同字面量、`assertIsNone(None)`、`assertIsNotNone(字面量)`）；断言包在会吞 `AssertionError` 的 `try/except` 里。
- `probe` 用必然失败的输入调用 `PROBE_CASES` 里登记的纯判定函数；用例集合与实现集合不一致、入口函数改名/签名变化都会点名到「更新登记表与文档」，而不是误报「判定被掏空」。
- `wiring` 要求四条子命令各自是所在步骤的第一条被执行的命令（只允许 `set`/`export`/`cd`/`umask`/`unset` 这类环境前置，外加 `:` 这个可带重定向的 no-op；认法是「行首关键字 + 字符黑名单」，不解析 shell），因此把调用换成仍含命令字符串的 `echo`、或在它前面先跑别的命令都会红。前缀行还必须不含 `>`、`<`、`|`、`;`、`&`、`$(`、反引号和续行反斜杠，而且这是对**整行**做子串匹配，注释和引号里的这些字符同样计入：`:` 是内建 no-op，但 `: > file` 会立刻创建或截断文件、`: $(rm …)` 会执行命令、`: \` 会把下一行（也就是守卫命令）并进 no-op，带这些字符的行按「会先执行的命令」判红；同样的限制覆盖既有的 `set`/`export`/`cd`/`umask`/`unset`。`wiring` 还要求 `ci.yml` 每个作业的 `runs-on` 等于 `CI_RUNNER_IMAGE`（当前 `ubuntu-24.04`），标量、行内列表、块状列表三种拼写都按其中的标量元素逐个比对；其它拼写（加引号、带尾注释、`${{ matrix.os }}` 这类表达式）不做归一，按字面量比对即红。因为平台条件的三个判定值正是从它派生出的 `linux`/`Linux`/`posix`，换了 runner 而平台条件不动即红。

六张登记表（站点、只读 argv 助手、写词、probe 用例、延后项、自检语料）逐行镜像在[参考验收入口](../operations/reference-acceptance.md)的锚点块里，守卫每次运行都比较两侧：删一行登记、缩小写词集合或删掉一个自检用例，都必须在同一次改动里改文档，否则红。守卫每次运行还会跑自检语料（反空转、丢弃结果与 `_ =`、未登记站点、`echo`/`sh -c` 写形态、`read` 藏变量与 `*` 展开等），所以把规则改松会先弄红自检。

**覆盖边界**：迁移、角色、桶、栈、闸门与健康检查在 dry-run 下只被计划、不被执行。因此本作业证明的是入口的前置检查、派生值、步骤顺序、延后项契约和写/读边界的存在性，**不是一次参考部署**。

四条守卫都只看结构，下面这些看不见：判定条件写反方向却仍读 `.ok`／`.code`（正确写法是 `if not started.ok: raise`，改写成 `if started.ok: raise` 仍绿）；把 `.ok` 读出来只打日志、随后仍不中断；`ctx.read` 里用 `psql -c "DELETE …"` 间接写库；换接收者名、别名或包进自定义函数再丢弃返回的 `ctx.mutate`；`getattr`/反射拿到的 `mutate`；`bit=` 以外的执行原语（`subprocess`、`os.system`）；对已登记站点的判定语义作任何改动；证据与被登记站点不相关（只校验它会被收集、不会被守卫认出的挂起形态停掉、断言不是字面量恒真也不会被吞）；`skipTest` 藏在 `setUp` 调用的模块级助手里（守卫只看 `setUp`/`setUpClass` 自身的语句）；运行期用 `globals()`/`exec` 或下标改写模块常量、证据方法名（守卫只数静态绑定，只扫模块体与类体里的属性赋值、`setattr`/`delattr` 与 `del`）；绑定形态按名字在整个模块里收集，因此函数体或类体里的 `type` 别名也会让同名模块常量失去可证明性，这是宁可误红的取舍；`return` 藏在非字面量条件里，或断言恒真但形态不在上面那张单子里（例如 `assertIn('a', 'abc')`）；`*` 展开与 `read` 构造助手按**名字**认，同名不同语义的本地元组骗得过；环境前置不是 shell 解析的结果（只按「行首关键字 + 字符黑名单」认前缀行，不解析 shell），黑名单以外的 shell 构造仍可能被当成前置；平台条件按写死的 Linux 判定，`wiring` 只核对 `ci.yml` 里每个作业 `runs-on`（标量、行内列表、块状列表）的标量元素等于 `CI_RUNNER_IMAGE`，既不核对镜像实际报告的平台字符串，也不读别的 workflow（含 reusable workflow）里的 runner；「判定在哪」那句描述本身属实与否（只校验反引号里的名字在入口里存在）。登记表只证明「这条命令还在、这条证据会被收集且不会被守卫认出的挂起形态停掉、这一行的判定位置确实存在」，不证明证据覆盖了该站点的语义，也不证明「判定在哪」那句描述属实——这仍由 `tests/ci` 的假 Runner 用例与真实 apply 运行负责。行数锚点挡的是「不知情的缩水」：同一次改动里改 `PINNED_COUNTS` 再跑 `tables --write` 仍可行，取舍留给评审。所以「入口有 CI 作业」不等于「入口已被完整守住」，只是「入口的结构不会在无人知晓的情况下消失」。

作业级条件让不适用项明确显示 skipped；最终 CI 门禁用 `always()` 执行，核对路由成功和各项实际结果。非法/缺失路由、失败、取消或本应执行却跳过均失败。不适用不表示业务验证已通过。

正文范围的 Markdown 代码块格式由独立作业 `docs_format` 把关。Ruff 的格式化器会解析并重排 Markdown 内标记为 `python` 的代码块，而 `docs` 范围不执行 `python_contracts`，`uv run ruff format --check .` 因而在这条路径上从未运行：PR #659、#661 是单文件 `docs/reviews/` 新增（路由输出 `{"mode": "docs"}`），CI 显示 lint 作业 skipping 后正常合并，随后 `main` 的 push 运行在 `Check Python formatting with Ruff` 上变红（`docs/reviews/2026-11-13-ci-acceptance-gate.md:399`、`docs/reviews/2026-11-13-reference-acceptance-rework.md:399`），并让 #662 的同类红被误判为它自身的问题（#673）。该作业只检出仓库并用 `uvx --from "ruff==${RUFF_VERSION}"` 安装锁定版本（与 `uv.lock` 的 `ruff` 版本一致，`tests/ci/test_ci_ruff_pin.py` 双向核对这一处钉住的版本），不安装项目依赖；命令与 `python_contracts` 的格式步骤逐字相同（`ruff format --check .`），所以检查的文件集合不会比全量道窄，也不存在第二份会与路由白名单漂移的路径清单。范围映射：`docs` 期望 success，`web`／`no-db`／`full` 期望 skipped——这三种范围已由 `python_contracts` 执行同一命令，重复运行只增加固定开销；`Gate: required checks for this change` 按此映射核对，`docs` 上失败、取消或意外跳过都使门禁失败。

**覆盖边界**：这只覆盖 Ruff 能解析的代码块，即标记为 `python`（或 `py`）的围栏；标记为 `text`、`bash`、`yaml`、`json`、`sql` 的围栏从不解析，其中的错误命令、失效路径、非法 JSON／YAML 仍可通过。`ruff check` 不检查 Markdown，因此代码块内的 lint 级问题（未使用变量、导入顺序、命名）也不在范围内。文档结构、链接与 `路径:行号` 引用、双语 README 一致性、`.agents/skills/` 内 JSON／YAML 的有效性同样无人检查：`scripts/dev-env/check-doc-refs.py` 仍是未接入 CI 的本地报告工具。格式检查只保证「能解析且排版一致」，不证明代码块内容可执行、输出正确或与实现同步，仍需人工审查。

审查 PR 的改动路径由 `changes` 作业里的 `scripts/dev-env/check-review-pr-boundary.py` 把控。第 4 步「按风险审查与验证」只给出两条作者自律命令（`git status --porcelain`、`git diff origin/main...HEAD --stat`），CI 对它们从不响应：曾有审查 PR 从被审 head 拉出，报告之外携带了与被审字节相同的实现，squash `f88279dc` 以 `review:` 标题进主干，而被审实现最终又以另一份修订版 `4ad6cad7` 合并——被审代码与进主干的代码不是同一份（#738）。

判据是三条相互独立、命中任意一条即为审查 PR 的信号：标题以 `review:`（或全角 `review：`）开头；head 分支以 `review/` 或 `docs/review` 开头；结构上同时改动 `docs/reviews/` 与其它路径。边界为改动路径必须全部落在 `docs/reviews/` 下（与 SOP 第 4 步同一边界，该步已写明「`docs/reviews/` 下的报告文件」），依据是既有数据：33 个已合并的审查 PR 中 32 个只碰一个 `docs/reviews/*.md`，唯一例外即上述事故；没有任何审查 PR 改过 `docs/validation/`，因此不为它开口子。`REVIEW_ROOT` 由 `tests/ci/test_review_pr_boundary.py` 钉住，放宽边界必须同时改测试而不是只改常量。结构信号在 `main` 的 648 个非空非 merge 变更集上只命中事故那一次提交（全长 729 个提交含 80 个 merge 与 1 个空提交，不计入），历史误报为零。

比较端点由检出形态决定。`pull_request` 事件的 `actions/checkout` 默认检出 `refs/pull/<n>/merge`，`HEAD` 是 GitHub 生成的合并提交：主题为 `Merge <head> into <base>`，第一父提交是构建合并引用时的基分支头，第二父提交是 PR head。门禁比较该合并提交与它的第一父提交，得到的正是本 PR 叠加到基分支上的改动；主干在此期间合进的内容已包含在第一父提交里，不进入比较集。`github.event.pull_request.base.sha` 停在该 PR 最近一次事件，主干一动它就落后，`git diff base.sha..HEAD` 会把主干期间的提交算成本 PR 的改动，而三点也无效：`base.sha` 是合并提交第一父提交的祖先，`merge-base(base.sha, HEAD)` 就是 `base.sha` 本身。实测（2026-11-14，运行 43564561839，PR #741）：`base.sha=d58127c0`、合并提交第一父 `4514b65b`、第二父 `c7d924bf`，两点口径得到 31 条路径——三份 `docs/reviews/2026-11-14-*.md` 来自主干，另 28 条是主干合入的契约、工作流、脚本与测试——结构信号据此把标题为 `test:` 的 PR 判成「审查 PR 携带实现」；改按第一父提交比较后，同一输入只剩该 PR 自己的 9 条路径。检出被固定到 `head.sha`（本地运行或显式 `ref`）时没有合并提交，改用三点：`merge-base(base.sha, HEAD)..HEAD`，该 `merge-base` 即分叉点。形态由提交本身判定，且父提交**按顺序**核对：第一父必须是 `into` 之后的基分支头，第二父才是 `Merge` 之后的 head。只比集合不够——主题把两个角色写反时集合仍然相等，比较起点会落到本 PR 的 head 上，恰好看不到它自己携带的实现（审查在 #751 里构造出这条可执行输入：未声明、真混了实现的 PR 被判绿），所以有序比对是父角色倒置的唯一守卫。`--head-sha` 只用于发现事件与检出不一致（re-run 读到旧 payload、push 与检出竞态），不一致时打 `::warning title=Review PR boundary comparison::` 并**仍按第一父比较**：此时退回 `merge-base(base.sha, HEAD)` 恰好就是合并引用下的两点口径，会把一次正常重跑变成假红。`changes` 以 `fetch-depth: 1` 检出，本地只有合并提交本身，第一父提交按 SHA 抓取一次（与路由抓取 `base.sha` 同一机制，失败退避重试一次），取不到即按失败关闭处理。

归属与调度：该检查留在 `changes` 作业内。`changes` 与 `delivery_tracking` 是两条没有作业级 `if:`、每个 PR 都执行的作业（其余作业按范围或依赖过滤）；选 `changes` 的真实理由是它在本步骤之前已经检出仓库并算出路由用的精确差异，边界检查因此不需要第二次检出，红灯也直接归因到路由；第一父提交在 `fetch-depth: 1` 下按 SHA 抓取一次，与路由抓取 `base.sha` 是同一机制。审查 PR 恰好都是 `docs` 范围 PR，放进按范围过滤的作业就会恰好在它该跑的地方被跳过。步骤排在路由之后，`changes` 失败使 `Gate: required checks for this change` 因 `ROUTING_RESULT != success` 变红，因此这条门禁自身就受合并门禁约束，不需要也不支持 `--admin` 绕过。

调度证据等级：**已实测**。此前登记的缺口是「docs 路由下本步骤真的执行」没有日志观测，审查 PR #751 的运行 43570172238、43570366503 补上了它：`changes` 先输出 `{"mode": "docs", "reason": "Only prose or skill documents/metadata changed; heavy checks are out of scope."}`，紧接着本步骤输出 `{"applicable": true, "ok": true, "signals": ["title prefix", "branch prefix"], "violations": [], "reports": ["docs/reviews/2026-11-14-boundary-merge-base.md"], "reason": "Review PR changes only docs/reviews/ (1 report file(s))."}` 与 `review-pr-boundary: Review PR changes only docs/reviews/ (1 report file(s)).`，作业成功。**限定**：这两次运行执行的是**基线版**脚本与 `ci.yml`（分支基于当时的主干，未含 #749 的改动），因此它们证明的是**调度与执行**——docs 路由确实到达该作业、步骤无 `if:` 的判断成立——不是新比较端点在 docs 路由下的行为。工作流定义取自 PR 的合并提交（含 head 的 `ci.yml` 改动），不是基线分支的版本：#749 的运行 43567960143 里该步骤命令带 `--head-sha="$HEAD_SHA"`、环境含 `HEAD_SHA: 9d6b08c2485d377917ad553464806771c9e7ae44`，而基线 `cdf60a9e` 的 `ci.yml` 没有这个参数。

失败方向：标题或分支已声明为审查 PR 时，比较端点取不到即失败关闭（红）——抓不到合并提交的第一父提交，或三点口径下算不出 `merge-base`，都按失败关闭处理，因为绿灯的代价正是上面那类事故，而修复动作完全在作者手里；抓取比较端点失败会带固定退避重试一次，仍失败时错误写成 `::error title=Review PR boundary unverifiable::` 并保留 git 的退出码与 stderr 首行，可从文案区分「网络/取不到」与「确实越界」（后者是逐条 `Path outside docs/reviews/`），不会把两种红混成一种；未声明的 PR 在 diff 取不到时按不适用处理，不把 Git／网络故障变成全仓库阻塞，路由在同样条件下本就回退全量。红灯会列出每个越界路径，并给出取码方式：`git show <被审 head>:<path>` 输出到管道或仓库外文件（如 `/tmp/<name>`），不得在被审 head 上检出或把其工作树抄回仓库。

边界是前缀而非类型：`docs/reviews/` 内的任意文件类型都算边界内，因此把「实现」也放进该目录（例如 `docs/reviews/impl.py`）不触发门禁——该目录下没有文件被导入或执行，实质无害，但这是第三条残余缺口。

已声明的审查 PR 若精确 diff 为空（或只有 `docs/reviews/` 之外、且一条报告都没有），同样判红，reason 为「reports no file under docs/reviews/」：边界「全部路径落在 `docs/reviews/` 下」对空集是空洞成立，实现额外要求至少一条 `docs/reviews/` 路径，这比 docstring 字面更严也更合理。成功路径的文案可区分 diff 是否取到：`No review signal: ...` 表示 diff 已取到并判过，`No review signal, and the exact diff is unavailable ...` 表示未取到、结构信号没有评估。

**覆盖边界**：这条检查只看路径，不比对 blob 哈希，因此证明不了「报告评审的就是被审 head」，也不验证报告内容、链接或 `路径:行号` 引用。`push` 到 `main` 的历史不再判（事件没有 PR 元数据），事故类问题必须在 PR 阶段拦住。比较口径只覆盖被审改动：基分支前进、其它 PR 合入的内容不进入比较集，因此这里的红灯只由本 PR 自身的路径造成。残余缺口有三处：标题与分支都被改名、且报告没有写进 `docs/reviews/` 的 PR 不构成任何信号；未声明 PR 在 diff 不可读时同样无法评估结构信号；边界只认路径前缀，不区分文件类型（见上）。

矩阵新鲜度作业 `delivery_tracking` 每次运行都检出仓库，但只有本次差异触及 `docs/plans/delivery-tracking.md`、`docs/plans/delivery-issues.json`、`docs/plans/github-issues-snapshot.json`、`scripts/dev-env/refresh-implementation-tracker.py` 或 `scripts/dev-env/delivery_tracker.py` 时才执行重渲染；无关 PR 的该步骤 skipped，作业仍成功，不阻塞合并。重渲染只读提交的快照，并以快照的 `fetched_at` 固定表头时间，随后 `git diff --exit-code` 拒绝手改或陈旧矩阵。基线不可取得或差异计算失败时保守执行，不以跳过掩盖；该作业不查询 GitHub，因此不证明快照覆盖未来 issue 变化。

Node Controller 镜像验证由 CI 以同提交的 [reusable workflow](../../.github/workflows/node-controller-image.yml) 调用，不再单独订阅 PR、push 或手动事件。PR 根据准确 base SHA 与 checkout 的差异选择；控制器源码、镜像文件、构建上下文忽略规则、根锁文件和 workspace 元数据，以及镜像路由或门禁文件变更触发镜像验证。使用 NUL 分隔并关闭重命名检测，确保移出受监控目录的旧路径也被检查；空差异保守执行，base 无效或不可获取时失败。`main` push、手动及未知事件始终执行。fork PR 仍使用调用工作流的事件与 checkout，公开仓库取 base，不传私有凭据。

镜像 workflow 在 amd64 和 arm64 的 GitHub 托管 runner 分别以空缓存构建并运行镜像、哈希拒绝和合成 CA 检查。其内部 `always()` 门禁要求 scope 成功且输出只能为 `true` 或 `false`：前者要求整个矩阵成功，后者要求矩阵 skipped。调用方的既有 `Gate: required checks for this change` 再要求该 workflow 成功并提供合法输出；缺失、失败、取消或不适用范围以外的跳过都不能放行。镜像验证不新增分支保护状态名，也不证明真实部署 CA 或物理节点已通过验证。

显示名称区分执行、证据验收和最终合并门禁；内部 job ID、依赖、命令、范围与超时不随改名改变：

| job ID | Actions 显示名称 | 实际职责 |
| --- | --- | --- |
| `changes` | `Scope routing and CI / design-token checks` | 检查路由边界、审查 PR 改动路径边界与 Design Token，按准确差异选择范围 |
| `python_checks` | `Python: tests, coverage and package build` | Python 单元、契约、CI、Workbench PG/Runtime 及选定 Speech PG 测试，导出两 worker 覆盖率数据集（不判定）、媒体工具链及 Python 包构建 |
| `python_contracts` | `Python: lint, types and contract compatibility` | Ruff、mypy、模块边界、生成漂移、协议兼容与 schema 校验 |
| `integration_shards` | `Integration 0–3: PG/MySQL/MinIO`；`Integration 4–5: Web + PG/MySQL/MinIO` | 六个真实隔离服务分片；混合集合中的浏览器与 Node HTTP 消费者只分配到后两片，普通 DB 用例按估算成本分配到全部六片，具体工具需求按各片实际用例计算。`full` 展开六片；`web` 只展开 4、5 两片（`excluded_shards` 排除 0–3），`docs`/`no-db` 跳过整个作业 |
| `coordination` | `Gate: six-shard integration evidence and merged coverage` | 验收六片成功及同提交、完整互斥集合、实际执行证据；下载六份分片覆盖数据与采集作业数据集，合并后按 80% 判定一次，不在此节点重新执行测试。仍只在 `full` 范围执行 |
| `python_checks_gate` | `Gate: Python execution and static checks` | 要求 Python 执行与静态/契约两个作业均成功，不执行测试 |
| `node_controller_image` | `Node Controller image evidence` | 调用镜像 scope、双架构构建与内部证据门禁，按输入差异决定是否构建 |
| `docs_format` | `Docs: markdown code block formatting` | 以与 `uv.lock` 一致的锁定 Ruff 复跑 `ruff format --check .`（含 Markdown 内 Python 代码块）；只在 `docs` 范围执行，只装 Ruff 自身，不安装项目依赖 |
| `delivery_tracking` | `Docs: delivery matrix freshness` | 从提交快照离线重渲染矩阵并比对；仅矩阵、关联源、快照或生成器变化时执行渲染 |
| `python` | `Gate: required checks for this change` | 按已验证范围核对全部适用结果，阻止失败、取消与意外跳过 |
| `web` | `Web: lint, types, tests, build and accessibility` | 格式、lint、TypeScript、SDK 契约、workspace 测试和构建、真实浏览器无障碍与对比度 |
| `deployment` | `Compose: configuration validation` | 执行 `docker compose config --quiet`，不启动服务或发布部署 |
| `reference_acceptance` | `Deploy: reference acceptance entry (dry-run execution)` | 真实执行 `scripts/dev-env/reference-acceptance.py --dry-run`：工具版本、派生环境键、compose 渲染、三条 Job DSN 身份、迁移 head、桶名与派生出的私有输入；`ci-entry-guard.py` 的 `log`/`sites`/`probe`/`wiring` 四条结构守卫（输出契约与延后项登记、写命令清单与 `ctx.read` 只读边界、纯判定函数探针、作业接线），并比对文档锚点块里的登记表与自检语料；另做一条损坏环境的负对照，不启动服务或执行写步骤 |

完整范围要求覆盖率、契约生成漂移及六个真实双数据库分片；契约兼容和结构变更的基线比较仅在 PR 执行，`main` push 与手动全量运行不会执行这些 PR 专属步骤。比较步骤获取当时的目标分支头作为基线，与路由使用的事件准确 base SHA 是不同依据。集成汇总继续核对同一提交、完整收集、互斥并集和实际执行结果，不以数量或仅收集代替证据。唯一允许的跳过是 `tests/integration/test_gateway_submit_ledger.py` 中以 `[mysql]` 结尾的既有 PostgreSQL 专属产品适配器参数；这不构成 MySQL 协调路径通过的证据，其他必需用例跳过会使汇总失败。

Python 测试作业通过 `python-coverage.py run --workers 2` 在同一 runner 内并发两个 `workbench-pg-probe.py --full-coverage` 进程，运行配置的非集成测试目录、脚本显式列出的 Workbench PG／Runtime 文件，以及发布证据、绑定生命周期和停用意图三个 Speech PG 测试文件；该作业只测量并导出数据，不做覆盖率判定。Workbench 文件按 `WORKBENCH_PG_TESTS` 和 `WORKBENCH_RUNTIME_TESTS` 清单选择，新增文件不会自动进入此作业；标记为 `integration` 的用例由独立分片收集和执行；未带该标记的用例不因此自动进入分片，真实模型资格测试见下文。各进程独占临时 PG、Unix socket 与 coverage 文件；Speech PG 测试在 full coverage 模式使用同一 worker 的私有 socket，不连接 runner 的系统数据库。集成分片仍使用显式配置的双库地址。作业仍只准备一次依赖及隔离 MinIO。父进程先准备四个 fixture 共用的 `speechnexus-workbench-saved` 测试桶，避免两个进程首次创建竞态；其对象按随机 project 前缀隔离，其余桶按文件独占或随机命名。测试按完整收集的文件分组，文件内用例不拆开，较大文件先分到当前用例数较少的一组；这是确定性的数量均衡，不是耗时均衡。

每片保留原完整覆盖源与 branch 配置，只在片内使用 `--cov-fail-under=0` 采集局部数据，并固定导出到 `coverage-<shard>.data`。父进程必须确认两片执行成功、私有 PG 已停止、同一 SHA/配置、相同完整 collection、精确分配且互斥并集覆盖全集、每个结果实际通过及覆盖数据摘要一致，然后复制各片原始数据并按 worker 归档为 `workbench-pg-coverage-parts/coverage-<worker>.data`，连同汇总以 `workbench-pg-full-coverage` 上传；覆盖率不足不由该作业判定，因此数据照常保留。若采集步骤已运行且失败、作业未取消，工作流尝试上传已生成的日志及分片／生命周期／汇总报告；步骤未运行、作业取消或上传失败时不能保证留存。测试或执行证据校验失败不会生成合格的覆盖数据集。入口拒绝任何非空 `PYTEST_ADDOPTS`，即使其选项不缩小集合；完整集合以 pytest 收集时各 worker 观察到的结果为准，同一插件或配置在两片共同遗漏用例时，同集与分配校验不能独立发现，仍须审查测试根目录、pytest 配置和插件变更。直接使用 `scripts/dev-env/workbench-pg-probe.py` 的 `--full-coverage` 模式是单次 pytest、只覆盖非集成范围的本地检查，仍沿用项目 `fail_under = 80`：只被集成测试执行的新增代码会让它失败，而 CI 的合并门禁通过。本机要按门禁口径复核时使用上文的 `combine` 路径，或在该模式下自行加 `--cov-fail-under=0` 只看数据。`--coverage` 仍是独立的 C 层 90% 检查。两种模式都须安装锁定依赖并提供 `--pg-bin`，运行前置条件见[验证指南](../agent-verification.md)，具体参数可用 `uv run python scripts/dev-env/workbench-pg-probe.py --help` 查看。

集成分片在安装 Python 依赖后，通过 `uv run python scripts/dev-env/integration-shards.py requirements --index 0 --count 6` 等命令实际收集完整 integration nodeid（包括参数化身份），以执行时相同的资源分组与成本加权分片算法输出对应分片的 `node_contracts` 与 `browser` 需求；工作流分别传入各分片的 index。`*_browser.py` 与两个合成浏览器探针模块声明浏览器依赖；回归核对当前所有 Node 浏览器探针消费者均被分类。收集错误或设置了非空 `PYTEST_ADDOPTS` 均会失败，即使该选项本身不缩小选择，也不能输出“无需工具”放行。该步骤只收集，实际执行和六份报告的互斥并集核验仍独立必需。预检记录 pytest 最终收集结果，未单独校验所有插件对收集的影响；汇总比较各片报告的相同全集，也不能独立发现所有分片共同遗漏的用例。须保持受控依赖与 pytest 配置，并审查收集范围的变更。

完整集合同时含普通用例与 Web 工具消费者时，仍保留四条 DB-only 道和 Web 能力道：浏览器与显式 Node 契约用例只落在 Web 能力道，普通 DB 用例按估算成本分配到全部六片。每片的目标是等量估算成本，而不是等量用例数：一个 Web 用案例按 `WEB_CASE_WEIGHT = 5` 折算（2026-11-06 两次完整运行中两类用例的实测单例耗时比为 3.4–6.4 倍，类均值为 4.6–5.5 倍，取 5），Web 能力道因此少收普通用例。分配使用 nodeid 的 SHA-256 排序、最大余数法配额和确定性取整；六片各自从完整收集独立算出同一结果，不依赖共享状态或历史制品。Web 用例在 Web 能力道间按 hash 顺序轮转，使各道用例数最多相差 1。一类用例单独存在时，仍按 nodeid 的 SHA-256 取模使用全部分片。通用入口仍接受 1–16 片，混合类别为 Web 工具保留 `max(1, count // 3)` 片；单片执行整个集合。预检、执行和报告检查使用同一个 `partition`，工具需求仍根据实际选中用例计算，不能在工作流里按编号跳过工具或业务测试。

该分组的目的不是把 Web 用例移入 DB 道，而是让 Web 能力道承担普通 DB 工作，从而使六个分片的实测 pytest 执行时间接近。Web 能力道另需安装 Web 依赖、构建入口并安装 Chromium，这部分固定开销不计入 `elapsed_seconds`；划分只均衡实测执行时间，不试图抵消该开销，也不改变 job 预算。

有 `browser` 需求的分片安装完整锁定 Web 依赖、构建两个实际入口，并安装相同版本 Chromium headless shell 及系统依赖后运行测试；只有 Node 契约消费者的分片保留锁定生产依赖安装；数据库分片不安装浏览器。需求随真实 nodeid 分区计算，不在 workflow 中固定某几个分片号。汇总 `check` 子命令与 `tests/ci` 轻量路由回归继续仅依赖 Python 标准库。

浏览器准备复用[独立脚本](../../scripts/dev-env/prepare-ci-browser.py)：先从锁定 Playwright CLI 的依赖模拟结果取得缺失/升级包，再向 APT 请求 URI/大小/SHA256 清单。仅在这条清单命令中选择 SHA256 输出，避免 `--print-uris` 的历史 MD5 默认值；不改系统或实际安装配置。归档通过 APT 自带的 `apt-helper download-file` 最多四个并发下载，同时传入强摘要和 `Checksum-FileSize`，让原生获取阶段约束已认证大小；不新增下载器依赖或覆盖传输政策。写入完整 APT 缓存前仍独立核对 SHA256/SHA512 与大小，MD5-only、错误大小、错误强摘要及不匹配的已有缓存均拒绝。不覆盖未知缓存内容。

随后仍执行原 `playwright install --with-deps --only-shell chromium`。信任链依赖 APT 认证的索引与上述写前强校验，不能假定安装器会重新校验完整缓存：APT 2.8 的[现有归档路径](https://github.com/Debian/apt/blob/2.8.1/apt-pkg/acquire-item.cc#L3494-L3507)可能只按大小复用。未知或无法得到强摘要的计划直接失败，不降级到弱摘要或强制信任。

预取不更改软件源、APT/网络安全配置或安装器。仅对 HTTP/HTTPS 的 `Timeout`、`Pipeline-Depth`、`No-Cache`，直接 HTTP 的 `User-Agent-Non-Interactive` 及无相关控制项的情形开放；HTTPS 同名项、代理、自定义 User-Agent、地址族、速率/队列/hash、`apt-get` 或 `apt-helper` 的 Acquire 专用配置、替代配置、HTTP/HTTPS 传输命名空间中的未知或嵌套键以及不可读取配置，触发原安装器路径。该检查不是对所有 APT 配置项的穷举审计。APT 2.8.1 的[原生实现](https://github.com/Debian/apt/blob/2.8.1/methods/http.cc)按既有配置与 systemd unit 语境选择非交互标记；apt-helper 使用同一 acquire HTTP method，不改写配置值、header 或上下文。

预取策略诊断输出固定分类、退出码与错误类型；对直接字段仅允许 1–64 字符、字母开头且只含 ASCII 字母/数字/下划线/连字符的字段名。空字段另分类，其他文本只给分类/数量；该诊断不输出配置值、代理凭据、嵌套主机名、URL 或原始 stderr。原生 APT 下载与安装子进程沿用其自身标准输出和错误输出，不能把上述过滤保证扩展到它们的日志。识别出字段名不自动授予预取资格，必须先核对原生语义。未知计划、下载或校验失败会阻止安装，临时预取目录自动清理。真实 runner 的预取行为及收益须以当次运行证据验证。

分片验收应区分取消、未开始测试与断言失败；回退脚本接线可恢复原官方安装路径。

每个集成分片还检查 PostgreSQL 16 工具，通过 `workbench-pg-probe.py --integration-shard` 启动独立的私有 Unix socket PG，再把 Workbench DSN 传给原 `integration-shards.py run`。双数据库与 MinIO 环境仍指向该作业既有隔离测试服务，不用私有 Workbench DSN 替换协调数据库地址。原分片报告路径、完整 collection、实际执行与意外跳过拒绝规则保持不变；驱动器把分片覆盖数据路径交给 `integration-shards.py run`，生命周期报告、分片报告与该片覆盖数据一并上传，分别保存，不替代分片执行报告。仅有单个分片作业成功不能替代汇总验收。

真实内网模型资格与可复现集成分别验收。`qualification` 标记的三条纪要用例调用配置的真实模型，不进入普通 `integration` 分片，也不计作 CI 通过；它们使用测试装配的工作台运行时与隔离数据库，不能单独证明目标生产部署或替代协调事务的确定性门禁。`make workbench-llm-qualification` 要求显式的私有 Workbench PG DSN、获准内网 LLM endpoint/model，并构建真实 Web 入口后选择这三项；缺少上述配置时入口直接失败。该命令没有禁止 `PYTEST_ADDOPTS` 等外部选项缩小选择，验收时须确认三项均实际通过，不能将跳过或部分通过当成资格证据。普通 CI 的分片完整性仍拒绝任何必需用例跳过，阶段 AC 仍须另附这三项真实模型与版本证据。

不在整个 workflow 的 `on` 中设置 `paths` / `paths-ignore`：被整体过滤的必需检查可能长期 Pending。[GitHub 路径过滤说明](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax#onpushpull_requestpull_request_targetpathspaths-ignore)、[作业条件与 skipped 语义](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/control-jobs-with-conditions)是维护入口；核验日期为 2026-10-25。

汇总使用 runner 自带 GitHub CLI 和标准库脚本 `download-integration-evidence.py`：只列当前仓库/运行，最多一页 100 个制品且拒绝截断；以 `integration-shard-` 开头的制品必须恰为六个预期名称，其他名称的制品可以同时存在；元数据响应上限为 1 MiB，制品最多三个并发下载进程。每次请求限 15 秒，进程失败或超时最多尝试三次，每次使用独立临时文件，完整性校验失败不放行；令牌仅经环境传入。先核对未过期、所属运行、压缩大小与 API 提供的 SHA-256，再只读 ZIP 内的三份固定成员——该片报告、PG 生命周期报告与 `coverage-<shard>.data`——不提取任意路径；单 ZIP 接受上限 16 MiB，单 JSON 保持原集成报告的 8 MiB 上限，覆盖数据上限 32 MiB；下载先写临时文件，进程退出后才检查实际大小，因此这些上限不是传输期间的磁盘写入硬限制。`workbench-pg-full-coverage` 制品只按白名单提取汇总 JSON 与两份 `coverage-<worker>.data`，名称、数量、大小或摘要不符即失败。输出目录必须事先不存在，全部下载及归档校验成功后才开始发布报告；既有同提交、完整互斥集合与逐项执行检查继续独立执行。仅汇总作业有 `actions: read`，全局仍仅 `contents: read`，超时为十分钟（含锁定依赖安装与合并判定）；不以 CLI 下载成功替代业务证据验收。依据：[官方 artifact REST API](https://docs.github.com/en/rest/actions/artifacts)、[GitHub CLI API](https://cli.github.com/manual/gh_api)，核验日期 2026-10-27。

历史性能分析和运行故障记录见[归档](../archive/validation/2026-10-ci-validation-history.md)。

## Web 范围执行 Web 能力道

`web` 范围此前只跑 Python 检查与 `web` 作业：`integration_shards` 的作业条件是 `mode == 'full'`，而路由把 `web/` 改动判为 `web`。因此纯前端改动从不执行唯一在真实浏览器里驱动工作台产物、真实后端、真实 PostgreSQL/MySQL 与 MinIO 的分片（4/5，#757）。#745 只改 `web/workbench/src/**`，自己的 PR 全绿，主干合并后才在 `Integration 4` 因 Playwright strict mode violation 变红；修复 PR #756 的 PR 运行里该分片同样 skipping，作者只能手动 `workflow_dispatch` 才拿到证据。另一层（`tests/web/*.behavior.spec.mjs`，在 `web` 作业里跑 preview 构建并用 `page.route` 注入应答）覆盖的是同一产物在**替身**后端下的交互，不覆盖真实后端与真实数据库那条链；两层能力不重叠，而其中一层在 `web` 路由下不执行。

采用 **只跑 Web 能力道**：`web` 路由保留六片中的 4、5 两片。`ci-routing.py` 按 `mode` 输出 `excluded_shards`（`web` 为 0–3 四行，其余范围为 `[]`），工作流把它用作 `strategy.matrix.exclude`；六片仍静态声明在 `[.github/workflows/ci.yml](../../.github/workflows/ci.yml)` 里，`full` 不排除任何片，也不改变矩阵只声明六片的既有事实。分片算法、`--index` 与 `--count 6` 全未改动，所以 `web` 路由在 4、5 两片执行的 nodeid 集与 `full` 路由在同一两片执行的**完全相同**——不是另一个子集，也没有新增筛选逻辑；`tests/ci/test_ci_routing.py` 用真实分区器交叉核对「Web 能力道恰好是所有浏览器/Node 契约消费者被选中的那些片」。

被排除的方案与理由：

- **新增独立「Web 集成」作业**：要重复服务容器、锁定 Python/Web 依赖、构建与 Chromium 安装，还要另写一套「只跑驱动浏览器/产物的用例」选择逻辑，与六片分区、分片证据和覆盖数据脱钩，两套选择必然漂移。现有作业已经把这些前置串好，重排分片只是少展开四片。
- **把 `web` 判成 `full`**：作业数、runner 分钟与 PR 墙钟按 `full` 增长，其中四片 DB-only 分片、部署检查与参考验收入口对 `web/` 改动没有信号；这是用全跑掩盖范围判断，也正是「不得把 full 变成默认」要避免的。
- **只跑 `test_workbench_real_runtime_*`**：admin、删除、会议、合成探针等浏览器与 Node 契约消费者同样只在 4/5 两片执行，缩到单一文件名前缀会把同类缺口留在别的模块；分层筛选还需要在分片器之外再写一条选择路径。

### 实测代价

`web` 路由新增 **2 个作业**（`Integration 4`、`Integration 5`），其余作业集合不变。分片作业墙钟实测区间为 6 分 42 秒 – 10 分 40 秒（`Integration 4`：run `43575396153` 481 s、`43573021394` 581 s、`43577755898` 623 s、`43577692527` 640 s；`Integration 5`：同四次运行 409 s / 549 s / 402 s / 571 s；每片 392 例，pytest 执行约 6 分钟，其余为锁定依赖安装、Web 构建与 Chromium 准备）。这些作业与 `python_checks`、`web` 同波次启动，因此 PR 墙钟增量是 `max(分片 4, 现有关键路径) − 现有关键路径`。改造前 `web` 路由的关键路径是 `python_checks`（run `43572881930`：544 s 墙钟中 511 s 是该作业，runner 分钟 14.6）；改造后同一内容（只多一个 `web/` 无关改动）的实跑 run `43577755898` 墙钟 **651 s（10 分 51 秒）**、runner 分钟 **29.5**，关键路径变为 `Integration 4`。即 **PR 墙钟 +107 秒（约 +1.8 分钟）**、**runner 分钟 +14.9 分钟**、**作业行数 15 → 16**。

| 路由 | 作业行数 | 实际执行作业 | 分片作业 | 实测墙钟 | runner 分钟 | 覆盖真实浏览器链 |
| --- | --- | --- | --- | --- | --- | --- |
| `web`（改造前，run `43572881930`） | 15 | 9 | 0（六片整体 skipping） | 9 分 04 秒 | 14.6 | 否 |
| `web`（改造后，本方案；run `43577755898`） | 16 | 11 | 2（4、5 片） | 10 分 51 秒 | 29.5 | 是 |
| `full`（run `43577692527`：本改动自身的 PR 运行） | 21 | 20 | 6 | 11 分 53 秒 | 67.9 | 是 |

「把 `web` 判成 `full`」的代价即最后一行：相对改造前多 6 个作业行、11 个实际执行作业、约 53 runner 分钟，相对本方案仍多 5 个作业行与约 38 runner 分钟；而其中四片 DB-only 分片、部署检查与参考验收入口对纯 Web 改动不产生信号。

### 语义与门禁

- `coordination`（`Gate: six-shard integration evidence and merged coverage`）仍是 `mode == 'full'` 专属：条件、依赖、六片必然成功、同提交完整互斥集合与实际执行证据、以及合并后的 `fail_under=80` 判定全部不变；`web`/`no-db`/`docs` 下它继续 skipped。本次移动的是**分片作业的展开范围**，不是六片证据门禁的语义。
- 最终门禁 `Gate: required checks for this change` 新增一条范围映射：`integration_shards` 结果按范围核对——`docs`/`no-db` 期望 `skipped`，`web`/`full` 期望 `success`。这是收紧：改造前 `web` 路由对该作业没有任何要求，分片失败或被跳过都不会阻止合并；现在 `web` 上分片失败、取消或意外跳过都会让受保护检查变红。`docs`/`no-db` 的期望未变（仍不适用），`full` 上的期望与 `coordination` 重合而不冲突。
- Web 能力道上新增一条失败关闭守卫 `Require the Web lanes to select actual Web consumers`：`web` 路由下若该片**既没有**选中浏览器消费者（`steps.integration_tools.outputs.browser`）**也没有**选中 Node HTTP 契约消费者（同一步骤的 `node_contracts`），作业直接失败，避免「跑了但没跑 Web 用例」再次以绿呈现。两路都读 `requirements` 的真实输出，未出现、为空或不是字面 `true` 一律按未选中处理（fail closed）；任意一路为真即通过，因为单个分片可以只承载其中一类。该步骤只在 `web` 路由执行，`full` 路由的步骤与断言不变；判据（`tests/contract/test_ci_workflow.py`）与判定结论（`tests/ci/test_ci_routing.py::WebLaneGuardTests` 用真实 `requirements` 向量执行该步骤脚本）都被钉住。
- 该守卫只证明「这一片确实在跑 Web 用例」，不证明两类都被选中、也不证明线上集合被完整覆盖：`tests/ci/test_ci_routing.py::IntegrationLaneTests` 用真实分区器（fixture 集合，不是线上 2754 例）钉住每个 Web 能力道都同时选中浏览器与 Node 契约消费者，并逐元素比对确认该 fixture 的全部 Web 用例都落在这些道上；线上集合的覆盖证据仍是 `full` 路由的分片报告。
- `web` 路由下两片仍产出 `coverage-<shard>.data`，但**没有消费者判断**：它只作为失败证据随分片制品留存，`coordination` 不下载、不合并、不按阈值判定，覆盖率仍只在 `full` 的单一判定点合并判定（`web`/`no-db` 不判定，既有缺口不变，见下文维护事项）。守卫注释与本节都不得把它读成「覆盖率达到 80%」的证据。

### 复现与验证

```sh
# 路由输出：pure-Web 改动应为 web，并排除 0–3 片
python3 scripts/dev-env/ci-routing.py --event pull_request --base <sha> --output /tmp/route
cat /tmp/route    # mode=web / excluded_shards=[{"shard": 0}, ..., {"shard": 3}]
# 门禁与矩阵形状的结构回归（不安装项目依赖）
python3 -m unittest discover -s tests/ci -v
```

三条反例与一次同改动的 `web` 路由实跑（都在 PR 阶段完成，运行记录长期可用；承载它们的一次性分支已删除）：

- 同改动只加一个 `web/` 注释后推成 `web` 路由：run `43577755898` 全绿，`Integration 4`/`Integration 5` 实际执行（`browser=true` 守卫通过），最终门禁 success。
- **条件改回 `mode == 'full'`**：基线工作流（`26fd8b39`）加回 #745 的 `aria-label`，`web` 路由 run `43577811710` 全绿、`integration_shards` 整体 skipping —— 证明红灯来自展开条件，而不是别的东西。
- **`web` 判成 `full`**：同一 `web` 改动按 `full` 判定的作业集就是上表最后一行的实测值（21 行 / 20 执行 / 67.9 runner 分钟），相对本方案多 5 个作业行与约 38 runner 分钟。
- **守卫的判据向量**：`tests/ci/test_ci_routing.py::WebLaneGuardTests` 直接执行工作流里该步骤的脚本，输入取自真实 `requirements` 输出——只有 Node 契约消费者（`browser=false, node_contracts=true`，浏览器分类器失效时的真实分区结果）与只有浏览器消费者都通过；两类都没有（DB-only 分片）与 output 缺失都失败关闭，并把实际取值写进错误文案。
- **复现 #745**：在 `web` 路由 head 上把 `aria-label="新单元文本 字数"` 加回去，run `43577800599` 里 `web` 作业 green、`Integration 4` 失败于 `workbench-chapter-create-probe.mjs` 的 Playwright strict mode violation，最终门禁 failure。这是本改动要堵的那一层：组件侧守卫（#756）不存在时，只有真实后端 + 真实数据库 + 真实浏览器那条链能看见它。

## 覆盖率门禁、采集边界与结构性余量

覆盖率数据在 `python_checks` 作业采集、在 `coordination` 汇总作业判定一次。判定对象是 `[tool.coverage.run].source` 列出的全部生产包，参数为 `branch = true`、`[tool.coverage.report].fail_under = 80`、`skip_covered = true`、`show_missing = true`。数据来源是全量路由实际执行的两类覆盖数据：`python-coverage.py run --workers 2` 导出的两份 `coverage-<worker>.data`，覆盖上文列出的四个测试集合（`tests/unit`、`tests/contract`、`tests/ci`、`tests/integration/test_probe_inventory.py`）加 `WORKBENCH_PG_TESTS`、`WORKBENCH_RUNTIME_TESTS` 与 `SPEECH_PG_COVERAGE_TESTS`；以及六个集成分片各自导出的 `coverage-<shard>.data`，覆盖 `tests/integration` 下其余用例。汇总用 `python-coverage.py combine` 合并八份数据，再以 `coverage report --rcfile pyproject.toml` 判定。采集作业与分片都不判定，同一门槛只在一处生效。

汇总作业对每份数据校验名称、存在性、大小、SHA-256 与 branch 数据，并要求采集作业结果为成功、六片结果均为成功、数据摘要与分片报告一致；任一份缺失、为空、被替换或采集作业未成功都直接失败，不会退化为「只用采集作业的数据判定通过」。合并结果写入 `merged.json`（数据份数、合并百分比、判定结果），同时以 `merged-python-coverage` 制品留存。

覆盖率判定只存在于执行六个分片的全量路由（`full`）。`web` 与 `no-db` 路由仍采集数据但不判定，这是「判定与采集同源」的直接结果，作为已知覆盖缺口记入下文维护事项。

判定函数是 `coverage.results.should_fail_under`，语义为 `round(total, 0) < fail_under`：**79.5%–79.99% 通过、低于 79.5% 失败**，失败输出 `Coverage failure: total of N is less than fail-under=80`。`total` 是语句与分支的加权覆盖率 `(已覆盖语句 + 已覆盖分支) / (语句 + 分支)`，不是语句覆盖率。

### 合并前后的实测依据

门禁的分母始终是 `source` 下全部可执行语句与分支，其中一部分只能由 `tests/integration` 执行。判定只在采集作业数据上运行时，那部分代码恒以「未覆盖」计入分母——这是把判定迁到合并数据的直接原因。2026-11-13 在 `97c70504` 上用 [ci.yml](../../.github/workflows/ci.yml) 相同脚本实测合并前作业范围，并用同一提交的八份数据（两份 worker 数据 + 六份分片数据）实测合并结果：

| 观测 | 数值 |
| --- | --- |
| 语句 / 分支 | 48315 / 13922 |
| 已覆盖语句 / 已覆盖分支 | 40050 / 9570 |
| `percent_covered` | **79.7275%**（显示 80%，通过） |
| 判定单元 | 49620 / 62237 = 79.7275% |
| 距 79.5% 的余量 | **142 个判定单元（0.23 个百分点）** |
| 使门禁失败的新增未覆盖判定单元 | **179 个** |
| 集成独占语句（197 个文件） | **4021（占 48315 的 8.3%）** |
| 集成独占分支边 | **1380（占 13922 的 9.9%）** |
| 作业范围实测可达上限 | 79.7275%，即门禁要求维持可达上限的 99.7% |
| 并入非浏览器集成覆盖后 | 已覆盖 44071 语句 + 10950 分支 → **88.4056%**，余量 5543 单元，可再容纳约 **6972** 个未覆盖判定单元 |

集成独占语句并非零散分布，而是集中在基础设施与装配边界：

| 区域 | 语句 | 文件数 |
| --- | --- | --- |
| 基础设施适配层（Postgres / SQL） | 2683 | 129 |
| 其他后端模块（worker、校验、发布编排） | 627 | 18 |
| 运行时装配根（`*_runtime.py`） | 427 | 14 |
| 应用服务 | 117 | 14 |
| HTTP 接口 | 104 | 11 |
| 领域层 | 63 | 11 |

按包划分：`backend` 3661 语句 / 166 文件、`execution-gateway` 351 / 27、`node-controller` 6 / 3、`packages` 3 / 1。单个文件集成独占比例最高的样例：`speech/infrastructure/runtime_sql.py` 161/211、`qualification_probe_approval.py` 103/124、`speech/infrastructure/workspaces_postgres.py` 98/152、`speech/infrastructure/public_job_admission.py` 192/380、`public_jobs_postgres.py` 100/706。

### 新增代码的分布

近 10 个改到 production Python 的已合并 PR，其改动文件的当前语句量与其中仍只被集成测试命中的比例：

| commit | PR | production 文件 | 语句 | 集成独占 | 占比 |
| --- | --- | --- | --- | --- | --- |
| `9ee2ca04` | #623 | 9 | 1027 | 25 | 2.4% |
| `ae3c8857` | #621 | 10 | 1188 | 187 | 15.7% |
| `9d480881` | #619 | 5 | 649 | 15 | 2.3% |
| `228ea403` | #618 | 13 | 1216 | 108 | 8.9% |
| `65598969` | #617 | 2 | 202 | 0 | 0.0% |
| `7a3b61a0` | #615 | 1 | 110 | 15 | 13.6% |
| `8c023545` | #614 | 4 | 159 | 0 | 0.0% |
| `7b26e0b6` | #612 | 8 | 554 | 0 | 0.0% |
| `27c945d0` | #611 | 4 | 426 | 15 | 3.5% |
| `e06a11e1` | #609 | 2 | 143 | 0 | 0.0% |
| 合计 | — | 58 | 5674 | 365 | 6.4% |

这张表是**补测之后**的快照：`#605` 序列中的 #621 之后紧跟 #622（109 行单元测试）、#626（149 行 CI 测试）、#627（294 行单元测试），这些补测把对应文件的集成独占比例压了下来。在功能提交自身的 head 上，比例更高——两片的失败就发生在补测之前。新增代码的类型分布与全量集成独占一致：集中在 Postgres/SQL 适配层与 `*_runtime.py` 装配根。

合并判定消除了这个结构性错配：集成用例本来就在六个分片里执行，其覆盖数据并入后，4021 语句与 1380 分支边由「恒未覆盖」变为按实际执行计入，TOTAL 从 79.7275% 变为 88.4056%，余量从 142 提升到 5543 个判定单元，可再容纳约 6972 个新增未覆盖判定单元。这不改变分母、不新增测试执行时间，也不放松 `fail_under`；只被集成测试执行的新增代码不再消耗余量，但仍须满足既有的单元测试政策（见下文方案对比中的 B 项）。

### 方案对比

代价以本次实测为准（macOS 18 核；PG 16.15 / MySQL 8.4 / MinIO 容器；2621 个非浏览器集成用例分三个并行进程执行，三个进程各自耗时 737 / 757 / 781 秒）。浏览器用例（40 例，14 个 `*_browser.py` 模块与 2 个合成浏览器探针模块）需要 Web 构建，未纳入测量；另有 10 个 Node/TS 消费者用例因未构建 Web 产物在本机失败。两者都只会让集成独占量偏小，因此表内 4021 是下界。

| 方案 | 对 4021 语句 / 1380 分支边的影响 | CI 壁钟增量 | 维护成本 | 会不会引入新盲区 |
| --- | --- | --- | --- | --- |
| **A1** 分片导出覆盖数据，在汇总证据里合并判定（**已采用**） | 计入 4021 + 1380，TOTAL 变 88.41%，余量 5543 单元 | 测试 0 分钟（分片已执行）；新增 6 份覆盖数据制品（每份约 1–2 MB）与一次 `combine`（十秒级）；汇总作业超时由 2 分钟上调到 10 分钟（含安装与判定） | 中：分片内启用 `--cov` 并落盘，`download-integration-evidence.py` 增列数据文件，覆盖率判定从 `python_checks` 迁到协调汇总 | 无（分母不变）；判定只在执行分片的全量路由存在 |
| **A2** 覆盖率作业内直接跑集成分片 | 同上 | +约 1100 秒（2621 例按 0.85 秒/例摊到两个 worker，各约 1310 例并行）；10 分钟作业预算需提到 25 分钟以上；另需在覆盖率作业增加 MySQL 服务 | 高：作业预算、服务、`python-coverage.py` 的 2 worker 上限一起改 | 无 |
| **B** 政策化：新增集成边界模块必须自带单元覆盖 | 4021 不变；新增 N 个集成独占判定单元时须新覆盖 0.795×(62237+N) − 49620，即 N=500/1000/2000 时须覆盖 51%/65%/72% | 0 | 每片新增测试代码；最近三片为补余量已写 552 行专用单测（#627 294 行、#622 109 行、#626 149 行），最终余量为约 140 个判定单元 | 浅测试风险：只覆盖判定单元、不约束断言强度 |
| **C1** 仅排除 3 个 100% 集成独占文件（36 语句） | 79.7275% → 79.7736% | 0 | 低 | 小但无效，数量级不足以解决问题 |
| **C2** 排除 58 个集成独占 ≥50% 的文件（3538 语句 + 1072 分支） | 79.7275% → 84.5715%（余量 3676 单元） | 0 | 低（`omit` 一项） | 大：7.4% 判定单元永久脱离覆盖约束 |
| **C3** 排除全部 197 个含集成独占语句的文件（18298 语句） | 79.7275% → 89.6185% | 0 | 低 | 极大：37.9% 判定单元退出约束，是「让数字好看」而非修复 |
| **D** 下调 `fail_under` | 79.0 时余量 453 单元（2.5 倍）；不改变结构 | 0 | 极低 | 门禁变松，同类漂移被掩盖而不是暴露 |

### 采用方案与维护事项

采用 **A1 为结构性修复、B 为并行政策**，并把 C、D 排除在修复手段之外：

- **A1 是结构性修复**：集成用例本来就在六个分片里执行，让它们顺手产出覆盖数据并在汇总证据中合并，分母与采集范围重新对齐，实测 TOTAL 从 79.7275% 变为 88.4056%，余量从 142 提升到 5543 个判定单元。它不新增测试执行时间，也不引入新的盲区。
- **B 是政策补充而非替代**：可单元验证的行为仍应写单元测试；只用 B 时，每片新增约 51%–72% 的单元覆盖才能维持阈值，成本随时长上升且容易产出为覆盖率而写的浅测试。
- **C 会让盲区不可见**：C2 已经要隐去 3538 语句 + 1072 分支。任何排除方案都必须同时发布被排除清单（文件 + 语句数），不允许只报总数；本章评估的 C1/C2 清单见下文。
- **D 被排除**：把 80 改成 79 只是把「新增 179 个未覆盖判定单元即失败」换成「新增 453 个即失败」，下一块集成专属代码仍会撞线，同时降低了对既有回归的敏感度；这是把门禁改松来消除失败，不解决分母与采集范围错配。

维护事项：

- `web` 与 `no-db` 路由只采集数据、不判定覆盖率。若要让这两条路由也受覆盖率约束，需要在不执行分片的前提下单独设计判定位置，并避免与全量路由重复判定。
- 分片覆盖数据使分片作业的制品多出一份（约 1–2 MB）并把汇总作业超时提到 10 分钟；分片作业自身的 30 分钟预算不变。
- 覆盖数据只在分片实际执行时产生；`--collect-only` 预检不产生数据，其报告不构成覆盖证据。

实施位置：

| 文件 | 当前职责 |
| --- | --- |
| [.github/workflows/ci.yml](../../.github/workflows/ci.yml) | 分片步骤导出 `coverage-<shard>.data` 并随分片制品上传；`coordination` 下载两类数据、校验、合并并判定；超时 10 分钟 |
| [scripts/dev-env/integration-shards.py](../../scripts/dev-env/integration-shards.py) | `run` 子命令以 `--cov --cov-fail-under=0 --cov-report=` 采集，固定 `COVERAGE_FILE`，在报告里记录数据文件名与 SHA-256；`check` 校验执行证据与数据摘要 |
| [scripts/dev-env/download-integration-evidence.py](../../scripts/dev-env/download-integration-evidence.py) | 分片制品按三份固定成员下载；`workbench-pg-full-coverage` 按白名单提取汇总与两份 worker 数据；名称、数量、大小、摘要不符即失败 |
| [scripts/dev-env/python-coverage.py](../../scripts/dev-env/python-coverage.py) | `run` 只测量并导出两 worker 数据与 `workbench-pg-full-coverage`；`combine` 校验八份数据、合并、按 `pyproject.toml` 判定，并写 `merged.json` |
| [pyproject.toml](../../pyproject.toml) | **不改**：`fail_under = 80`、`source`、`branch` 均维持现状 |

这属于门禁治理变更（改变判定所在作业与必要检查的证据构成）；`ci.yml` 的判定位置变化须与本节、`main` 的 required status checks 说明保持同步。

### 复现测量

两条命令即可复现（本机需可用的隔离 PostgreSQL、MinIO、MySQL 与 `SPEECHNEXUS_TEST_*` 变量；采集范围还需 `--pg-bin` 指向含 `initdb`/`pg_ctl` 的目录）：

```sh
# 1) 采集作业范围：两 worker 数据写到 /tmp/cov/job-parts，汇总在 /tmp/cov/job.json
uv run python scripts/dev-env/python-coverage.py run --pg-bin <pg-bin> --workers 2 --output /tmp/cov/job.json

# 2) 合并判定：--job-parts 与 --integration-dir 指向两类数据目录，输出 merged.json 与合并数据
uv run python scripts/dev-env/python-coverage.py combine \
  --job-summary /tmp/cov/job.json --job-parts /tmp/cov/job-parts \
  --integration-dir /tmp/cov/integration-shards --count 6 --output /tmp/cov/merged
#    集成数据按 coverage-<shard>.data 命名；缺任一份、为空或摘要不符即失败，不会退化为作业范围数据判定。
```

集成分片数据由 `integration-shards.py run --index <i> --count 6 --coverage-data <目录>/coverage-<i>.data --report <目录>/integration-shard-<i>.data` 产生（`check` 子命令校验报告与数据一致）。没有可用分片数据时，`combine` 直接失败；只想复现作业范围量级可对 `/tmp/cov/job-parts` 自行 `coverage combine` 后 `coverage report`，但这不是门禁路径。

`coverage-delta.py` 是一次性只读脚本（不进入仓库），用于比较作业范围与集成范围的语句集合：先把每份数据复制成临时目录下的 `.coverage.*`，用 `Coverage(..., config_file=pyproject.toml).combine()` 合并，再对每个文件取 `analysis2()` 的语句集合做差集。

`coverage-delta.py` 是只读的一次性脚本（不进入仓库）：先把每份数据复制成临时目录下的 `.coverage.*`，用 `Coverage(..., config_file=pyproject.toml).combine()` 合并，再对每个文件取 `analysis2()` 的语句集合做差集，输出集成独占语句总数与逐文件条数。

测量是确定性的：同一子集（5 个模块、155 例）连续两次运行耗时 95.26 秒与 88.32 秒，两次的已覆盖语句集合在 708 个文件、11150 行上**完全一致（差异 0 行）**；覆盖数据只由实际执行的语句决定，与用例顺序、分片方式和并行度无关，分三组并行与单进程一次运行得到同一并集。可能让结果偏小的情况只有用例跳过与环境相关失败：本机缺 `SPEECHNEXUS_WORKBENCH_TEST_DSN` 时 210 例跳过，另有 10 个 Node/TS 消费者用例因未构建 Web 产物失败，因此 4021 是集成独占量的下界。

### 排除方案的盲区清单

下列清单对应方案 C2（排除集成独占 ≥50% 的 58 个文件，按语句数降序）。这些语句在排除后不再受任何覆盖率约束：共 3538 语句、1072 分支，占判定单元总量的 7.4%。C1 的 3 个文件（`profile_lifecycle_intents_postgres.py` 22 语句、`qualification_admission_grants.py` 8 语句、`profile_lifecycle_intents.py` 6 语句）是它的子集；C3 的范围是此表的超集（另有 139 个文件只含少量集成独占语句）。

| 文件 | 语句 | 其中集成独占 |
| --- | --- | --- |
| `backend/src/speechnexus_backend/speech/infrastructure/public_job_admission.py` | 380 | 192 |
| `backend/src/speechnexus_backend/speech/infrastructure/runtime_sql.py` | 211 | 161 |
| `backend/src/speechnexus_backend/speech/infrastructure/workspaces_postgres.py` | 152 | 98 |
| `backend/src/speechnexus_backend/tts_settlement_worker.py` | 138 | 76 |
| `backend/src/speechnexus_backend/qualification_probe_approval.py` | 124 | 103 |
| `backend/src/speechnexus_backend/speech/infrastructure/pronunciation_drafts_sql.py` | 118 | 83 |
| `backend/src/speechnexus_backend/speech/infrastructure/runtime_delivery_sql.py` | 114 | 81 |
| `backend/src/speechnexus_backend/speech/infrastructure/admission_sql.py` | 110 | 82 |
| `backend/src/speechnexus_backend/speech/infrastructure/qualification_admission_postgres.py` | 104 | 94 |
| `backend/src/speechnexus_backend/workspace_writer_runtime.py` | 101 | 58 |
| `backend/src/speechnexus_backend/builtin_voice_publication.py` | 97 | 82 |
| `backend/src/speechnexus_backend/profile_runtime.py` | 92 | 64 |
| `backend/src/speechnexus_backend/speech/infrastructure/runtime_observation_sql.py` | 84 | 55 |
| `backend/src/speechnexus_backend/binding_list_runtime.py` | 83 | 53 |
| `backend/src/speechnexus_backend/resource_pool_runtime.py` | 81 | 52 |
| `backend/src/speechnexus_backend/speech/infrastructure/qualified_speech_facts_postgres.py` | 78 | 42 |
| `backend/src/speechnexus_backend/speech/infrastructure/lease_release_sql.py` | 76 | 61 |
| `backend/src/speechnexus_backend/speech/infrastructure/job_cancellation_reconciliation_postgres.py` | 72 | 37 |
| `backend/src/speechnexus_backend/speech/infrastructure/pronunciation_drafts_postgres.py` | 72 | 43 |
| `backend/src/speechnexus_backend/qualification_probe_execution.py` | 66 | 58 |
| `backend/src/speechnexus_backend/speech/infrastructure/leases_sql.py` | 63 | 41 |
| `backend/src/speechnexus_backend/speech/interfaces/http/resource_pools.py` | 60 | 30 |
| `backend/src/speechnexus_backend/speech/infrastructure/binding_defaults_sql.py` | 57 | 40 |
| `backend/src/speechnexus_backend/speech/infrastructure/binding_summaries_postgres.py` | 56 | 29 |
| `backend/src/speechnexus_backend/speech/infrastructure/owners_sql.py` | 55 | 36 |
| `backend/src/speechnexus_backend/speech/infrastructure/tts_settlement_postgres.py` | 54 | 31 |
| `backend/src/speechnexus_backend/speech/infrastructure/voice_catalogue_postgres.py` | 53 | 38 |
| `backend/src/speechnexus_backend/artifact_runtime.py` | 51 | 30 |
| `execution-gateway/src/speechnexus_gateway/infrastructure/barrier_sql.py` | 49 | 35 |
| `backend/src/speechnexus_backend/builtin_voice_source_approval.py` | 46 | 28 |
| `backend/src/speechnexus_backend/speech/infrastructure/voice_retirements_postgres.py` | 46 | 23 |
| `execution-gateway/src/speechnexus_gateway/infrastructure/output_interval_sql.py` | 46 | 34 |
| `backend/src/speechnexus_backend/speech/infrastructure/migrations/versions/0023_publication_lifecycle.py` | 40 | 22 |
| `backend/src/speechnexus_backend/speech/interfaces/admission_transactions.py` | 36 | 20 |
| `backend/src/speechnexus_backend/speech/infrastructure/binding_defaults_postgres.py` | 32 | 16 |
| `backend/src/speechnexus_backend/builtin_voice_revocation.py` | 31 | 29 |
| `backend/src/speechnexus_backend/speech/infrastructure/migrations/versions/0022_catalog_revision_state.py` | 31 | 16 |
| `backend/src/speechnexus_backend/speech/infrastructure/resource_pools_postgres.py` | 31 | 16 |
| `execution-gateway/src/speechnexus_gateway/infrastructure/recovery_sql.py` | 30 | 16 |
| `execution-gateway/src/speechnexus_gateway/infrastructure/command_redaction_sql.py` | 29 | 18 |
| `execution-gateway/src/speechnexus_gateway/infrastructure/stop_sql.py` | 29 | 17 |
| `backend/src/speechnexus_backend/speech/infrastructure/tts_settlement_sql.py` | 24 | 14 |
| `backend/src/speechnexus_backend/speech/infrastructure/migrations/versions/0011_binding_parameter_defaults.py` | 22 | 11 |
| `backend/src/speechnexus_backend/speech/infrastructure/profile_lifecycle_intents_postgres.py` | 22 | 22 |
| `execution-gateway/src/speechnexus_gateway/infrastructure/drain_sql.py` | 21 | 11 |
| `backend/src/speechnexus_backend/speech/infrastructure/public_job_admission_grants.py` | 20 | 11 |
| `execution-gateway/src/speechnexus_gateway/infrastructure/binding_disable_guard.py` | 20 | 11 |
| `execution-gateway/src/speechnexus_gateway/infrastructure/abort_recovery_sql.py` | 19 | 11 |
| `execution-gateway/src/speechnexus_gateway/infrastructure/abort_authority.py` | 18 | 13 |
| `execution-gateway/src/speechnexus_gateway/infrastructure/observation_sql.py` | 18 | 11 |
| `backend/src/speechnexus_backend/speech/infrastructure/builtin_voice_grants.py` | 13 | 8 |
| `backend/src/speechnexus_backend/speech/infrastructure/runtime_delivery_schema.py` | 13 | 8 |
| `backend/src/speechnexus_backend/speech/infrastructure/asset_schema.py` | 12 | 9 |
| `backend/src/speechnexus_backend/speech/infrastructure/pronunciation_draft_schema.py` | 10 | 6 |
| `backend/src/speechnexus_backend/speech/infrastructure/qualification_admission_grants.py` | 8 | 8 |
| `backend/src/speechnexus_backend/speech/infrastructure/profile_draft_schema.py` | 7 | 4 |
| `backend/src/speechnexus_backend/speech/infrastructure/tts_settlement_schema.py` | 7 | 4 |
| `backend/src/speechnexus_backend/speech/application/profile_lifecycle_intents.py` | 6 | 6 |

这张表本身就是排除方案的成本：58 个基础设施文件连同其未被覆盖的分支一起退出门禁视野，门禁显示的 85% 不再覆盖它们。

## 集成分片耗时与超时

`integration_shards` 的作业超时为 30 分钟。超时是作业预算：改变它不改变用例选择、断言、`fail-fast`、分片算法或证据验收，也不放松任何检查。该预算与两级超时对 `web` 路由的 4、5 两片同样适用：`web` 只改变展开哪些片，不改变片的执行内容、内层上限或收尾预留。

分片执行有两级超时，二者必须成对调整：

| 层级 | 位置 | 当前值 | 行为 |
| --- | --- | --- | --- |
| job 预算 | `.github/workflows/ci.yml` 的 `integration_shards.timeout-minutes` | 1800 秒（30 分钟） | runner 侧硬上限；到点直接取消作业，后续 `always()` 步骤、分片摘要与证据上传都不再执行 |
| 探针内部上限 | `scripts/dev-env/workbench-pg-probe.py` 的 `INTEGRATION_SHARD_PYTEST_TIMEOUT_SECONDS` | 1500 秒（25 分钟） | 从分片运行器（pytest）启动时开始计时；到点由探针结束子进程、打印诊断信息并保留日志，随后照常停库和写生命周期报告 |

内部上限由 job 预算减去 300 秒派生（`INTEGRATION_JOB_RESERVE_SECONDS`）。差值不是余数，而是显式留给探针之外的工作：探针启动前的容器初始化、checkout、依赖安装与 MinIO，Web 分片还包括 pnpm 依赖、构建与 Chromium；探针超时后的停库、写生命周期报告与本地证据落盘也共用同一预算。2026-11-06 run `42251268385` 的 Integration 2 attempt 1（job `128006854118`）：作业 16:19:39 开始，探针 16:21:17 启动，准备耗 98 秒；1080 秒内部截止在 16:39:17，超时消息 16:39:20.76，作业 16:39:26 完成。从内部截止到作业完成约 9 秒，到 runner 硬上限 16:39:39 约 19 秒；旧 120 秒差值扣掉 98 秒准备后已无完整收尾余量，收尾几乎被 runner 抢占。300 秒覆盖按分钟计的 Web 依赖安装与构建，也给收尾抖动留出余量。

为什么是 1800/1500：同 run attempt 2 在相同 commit、相同分片上成功，作业 760 秒、pytest 670.98 秒；其余 DB 分片两轮约 462–544 秒，Web 分片约 87–200 秒。attempt 1 在 1080 秒时进度 84%（12%→84%，全部通过、无 F），但同 commit 重跑约 671 秒完成，说明这次 21 分钟是单次 runner 慢尝试/stall，不是分片已结构性增长到 21 分钟；把 84% 线性外推为 1286 秒与重跑事实矛盾，不能作为预算依据。分片耗时仍会随用例数增长，且单次慢尝试已超过 18 分钟；1500 秒内部上限高于该慢尝试（1084 秒）约 416 秒，1800 秒 job 再留 300 秒准备/收尾，覆盖高方差尾部。Integration 2 常态约 12.7 分钟，新增预算只由慢尝试或挂起分片使用。

取舍：提高 job 上限会增加最坏情况下的 runner 占用（失败分片最长约 27 分钟），但不改变常态耗时、用例选择或断言；降低内部上限只会让仍在推进的分片更早被误杀，无法覆盖高方差尾部。`--full-coverage` 模式不在此预算内，仍按其 10 分钟 Python 作业使用 600 秒；该模式内部上限等于 job 上限、没有预留（既有不自洽），诊断会明确打印 `no reserve`，本次不改其行为。

超时路径必须可诊断。分片运行器把实时位置写入 `progress-<report stem>.json`（`selected`、`completed`、`passed`、`failed`、`skipped`、最后开始与最后完成的测试名、已运行秒数）；该名字不被 `integration-shard-*.json` 证据 glob 匹配，`check` 也只接受 `integration-shard-<n>.json`，避免把旁车当报告。该文件只用于解释超时，写入失败不会改变门禁。探针把子进程已产生的部分输出先写入 `<cluster>/pytest.log`，超时后向 stderr 打印：已运行秒数与内部上限、已完成/选中总数（无进度文件时从 pytest 进度行估算；`[ 0%]` 不再触发除零）、通过/失败/跳过计数、最后开始与最后完成的测试名、phase、与 job 上限的差值及预留用途。建议由证据推出：存在 F/E 或 failed>0 时提示先修失败；有完成数增长且无失败标记时才建议确认增长后提高预算/拆分；否则提示 collection 可能仍在运行或卡住。生命周期报告同时记录 `pytest_timeout_seconds`、`pytest_job_timeout_seconds` 与超时诊断 `pytest_timeout`。超时不等于跳过：子进程照常终止、作业失败、选择集与断言不变，变化只在于失败时可定位到具体进度与测试。

| 分片 | 整作业墙钟时间 | 说明 |
| --- | --- | --- |
| 0–3（PG/MySQL/MinIO） | 6.2–7.3 分钟 | 依赖安装、私有 PG、双数据库与 MinIO 启动都计入；各片约 413 例 |
| 4–5（Web + PG/MySQL/MinIO） | 7.9–8.5 分钟 | 另装 Web 依赖、构建入口与 Chromium，并承担约 288 个普通 DB 用例 + 25 个 Web 用例 |

2026-11-07 run `42272812867`（#507，PR head `d66d9f90`，分片报告记录的 checkout merge 为 `53180f84`）实测：pytest 执行 296.2 / 352.2 / 346.9 / 345.5 / 380.9 / 359.7 秒，六片用例数 414 / 413 / 413 / 413 / 313 / 313；DB 类内 0.88–1.05x、Web 类内 0.97–1.03x，跨六片 0.85–1.10x。整作业墙钟 374 / 436 / 419 / 417 / 510 / 475 秒，最长 8.5 分钟。调整前同 collection 的 run `42265079348` 为 349.4 / 337.1 / 379.2 / 480.6 / 103.5 / 88.8 秒，最长作业 9.45 分钟；issue #507 所引 run 的最慢分片为 671.1 秒。4/5 片不再在约 2 分钟内空闲，但 Web 固定准备仍计入其墙钟。

基线取自 2026-11-06 完整运行 `42175653086`、`42169960755` 中各作业 `started_at`→`completed_at`。PR #498 的 Integration 2 在 `42251268385` 的 attempt 1 因单次慢尝试触发 1080 秒内部上限（job 1187 秒），同 commit attempt 2 以 job 760 秒 / pytest 671 秒通过；其余 DB 分片两轮约 462–544 秒，Web 分片约 87–200 秒。因此 Integration 2 常态约 12.7 分钟，单次慢尝试 >18 分钟，重跑可恢复；本次增预算是覆盖高方差尾部，不是认定分片已结构性增长到 21 分钟。旧上限 10 分钟相对最慢分片只剩约 40 秒余量：同日 `42172973696`（#441）的 Integration 3 在 10 分 17 秒被作业超时取消，汇总作业因此得到 `shards=cancelled` 并失败，同一提交重跑即可通过；issue #452 记录的三例（#441、#445、#448）同属这一模式。取消是 runner 侧终止作业，与用例断言失败不同，不按失败用例处理。

| 观测点 | 位置 | 回答的问题 |
| --- | --- | --- |
| 分片作业墙钟时间 | 各分片 job summary 的 `Job wall clock` | 安装、构建、服务启动等作业整体是否变慢 |
| pytest 执行时间 | 各分片 job summary 的 `pytest execution`，以及制品 `integration-shard-<n>.json` 的 `elapsed_seconds` | 某片的用例集是否独胖 |
| 分片用例数 | `integration-shards.py check` 输出 | 六片用例数是否均衡；按 DB/Web 资源类内均值比较，某片超过同类均值 1.25 倍时打印 warning，不改变门禁结论 |
| 分片耗时 | `integration-shards.py check` 输出与 `integration-shard-<n>.json` 的 `elapsed_seconds` | 某片是否超过 15 分钟预警线或同类均值 1.25 倍；只告警，不改变门禁结论 |
| 六片横向对比 | 各分片 job summary 的 `Job wall clock`、`pytest execution`、`Selected cases` | 六片相对耗时、最慢片与用例数 |

`integration-shards.py check` 逐片打印用例数、`elapsed_seconds`、相对同类均值倍数；每片运行结束时也打印自己的用例数与耗时。用例数超过同类均值 1.25 倍、耗时超过 15 分钟或同类均值 1.25 倍时打印 warning，不失败。各分片 job summary 的指标表由 workflow 的 `Publish integration partition timing` 提供（每片的 Job wall clock、pytest execution、Selected cases）；check 只把 warning 与 skipped 写入汇总 summary，避免两份重复表。

两个摘要步骤只写 `$GITHUB_STEP_SUMMARY`，不参与任何结论；作业结论仍由分片执行与证据校验决定。被超时取消的作业不会执行 `always()` 步骤，因此超时运行没有分片摘要，只能从未上传的制品与 `cancelled` 结论识别。

规则：某片常态耗时（近期多次运行的典型值，不是单次抖动）超过 15 分钟时，必须拆分或重排分片——提高 `count`、调整矩阵或按实际成本重新分区——并使上限至少高于最慢分片常态值 5 分钟；不得继续抬高上限来代替拆分。15 分钟是预警线，30 分钟是当前余量而非目标值。内部上限不得成为更早的隐性上限：任何分片常态耗时接近 1500 秒时按同一规则拆分，而不是把内部常数改回更小值。本次 #502 只修复超时诊断与预算自洽，未改 6 分片划分；#507 随后在不改预算的前提下重排 6 分片：普通 DB 用例分配到全部六片，Web 用例仍只落在 Web 能力道，同 collection 实测最慢分片从 480.6 秒降到 380.9 秒；单次 stall 不作为拆分依据，拆分依据是近期多次运行的常态耗时。若后续确认某片常态接近 15 分钟，再独立做分片重排。调整 job 超时、探针内部上限或观测步骤时，同步更新本节、推导常数与基线（核验日期：2026-11-07）。

## 路由维护

- 对比事件的准确 base SHA 与实际 checkout 的合并结果；路径用 NUL 分隔，不按换行拆分，也不使用 API 文件列表截断后的子集。
- 重命名同时考虑旧/新路径。将 Python 文件移入文档目录或删除源码仍须全量。
- 基线不可用时回退全量；路由或轻量回归失败时阻止合并。不要用 `continue-on-error` 或缺失输出兜底放行。
- 轻量入口使用 runner 的系统 Python 3.12+，只依赖标准库；Ruff 对该脚本及 `tests/ci/` 固定 `py312`，产品代码继续遵循项目 Python 版本。

从仓库根目录验证变更：

```sh
python3 -m unittest discover -s tests/ci -v
uv run pytest tests/ci tests/contract/test_ci_workflow.py tests/unit/test_integration_shards.py -q --no-cov
```

需要完整复核时可在 Actions 手动运行 CI；本机完整检查仍按 [AGENTS.md](../../AGENTS.md) 的宿主和隔离服务前置条件执行。

## 文档引用检查（非门禁）

[check-doc-refs.py](../../scripts/dev-env/check-doc-refs.py) 是本地报告工具，检查 `docs/` 内 `路径:行号` 引用是否存在、越界或同名文件歧义；发现问题时打印 `RESULT FAIL` 并以退出码 1 结束。当前 CI 工作流未调用该脚本，它不属于必需检查、作业门禁或路由范围，失败不阻止合并。需要时手工运行 `python3 scripts/dev-env/check-doc-refs.py`；`--show` 会同时打印被引用行原文，便于核对内容是否与文档描述一致。

## 公开语音路由一致性门禁

`python_contracts` 作业（`Python: lint, types and contract compatibility`）在契约兼容步骤之后、Ruff/mypy 之前执行
`make contracts-route-conformance`，对应 [check-route-conformance.py](../../scripts/dev-env/check-route-conformance.py)。
该步骤无 `if`、无 `continue-on-error`、不设置 `CONTRACT_BASE_REF`，在 `full`、`web`、`no-db` 三种路由范围都运行。

检查用合成 `Settings` 装配 `create_app()`（不启动 lifespan、不连库、不联网），把实际注册的
`(method, path, operationId)` 与当前 `contracts/speech-openapi.yaml` 对账，并静态提取每条已注册路由的响应状态码。
新增未注册的契约操作、已注册但契约未声明的路由、未声明的可达状态码、operationId 路由形状不一致、
405 响应缺少公开错误信封，以及过期的显式基线都会以非零退出码失败。无法静态判定的表达式只输出 `UNDETERMINED`，不猜造、不判失败；
ASGI ingress 状态码、框架 404/500、FastAPI 参数校验产生的 422、跨模块 helper 返回的响应与真实执行内容不在静态集合内；endpoint 源码无法定位则直接失败。

每条声明 405 的操作还会在该路径上发起一个进程内 ASGI 请求，方法由 #473 的 `methods_for_path` 从该路径未注册的
方法中选出，要求返回包含 `code/type/message/param/request_id/retryable` 的公开错误信封，而不是框架裸
`{"detail": "Method Not Allowed"}`。还要求非空 `Allow` 响应头、非空字符串 `code`、`retryable=False`、
`request_id` 回显探针发送的 `X-Request-ID`，且响应体不携带顶层 `detail`。
因此「路由已注册」不再等于「405 已统一为信封」，新增路由或专用处理器漏掉这些要求会直接失败。

缺少探测证据时，两层的可观察结果不同：

- **`evaluate()` / 报告层**：直接调用 `evaluate()`，若 `method_not_allowed_envelopes` 为 `None`
  或缺少某个操作的 key，会为缺少证据的操作产生 `method_not_allowed_envelope_missing` finding。
  将返回的报告交给 `format_report()`，对应行打印 `405-envelope=FAILED`，不输出 OK 尾巴。
  `evaluate()` 本身只返回报告，不决定进程退出码。
- **CI / CLI 入口层**：CI 的 `make contracts-route-conformance` 调用脚本入口，先执行 `build_report()`。
  探针返回 `None`、空映射或缺 key 时，`require_envelope_coverage()` 在调用 `evaluate()` 前抛出
  `RuntimeError`；脚本退出码为 **2**，stderr 输出 `route conformance check could not run:`，
  诊断包含 `refusing to report declared 405s as verified`。stdout 为空，不打印文本或 JSON 报告，
  因而 CI 日志中没有 `405-envelope=FAILED` 行或 OK 尾巴，门禁步骤失败。

单测分别覆盖两层，并断言探测集合等于声明 405 的集合；partial mapping 用例只去掉 `getJob` 的 key，
直接调用 `evaluate()`，确认 finding 精确且仅指向 `getJob`。

探针注入只启用 ingress 的资产与 workspace 服务替身；任何业务方法调用立即失败，不连接服务。
未配置服务的部署仍可能在路由匹配前返回 503，这不属于探针通过的证据。
当前匹配器只支持普通 `{parameter}` 段；检测到 Starlette converter（如 `{id:int}`、`{rest:path}`）、
懒加载 `include_router` 的非空 prefix，或任何不是「无子路由 StaticFiles」的 `Mount`（含裸 ASGI 应用，
它的 `.routes` 为空却接受前缀下所有路径）时，门禁在发送请求前显式报错（退出码 2），不按简化模型探测，
也不修改注册路由。`Mount` 先于子路由匹配且接受任意方法，`methods_for_path` 不下降进去，探针会选中
子路由已注册的方法并真正调用其 endpoint；因此门禁只放行无子路由的 `StaticFiles`，其它挂载一律拒绝，
`Host` 同样拒绝。探测窗口内另有 dispatch 计数守护：普通 Starlette `Route.handle`（方法被接受后才计入，
405 不计）、`Mount.handle`（未建模的挂载应用）与每个可达 FastAPI ASGI handler（直接路由与懒加载 include，
覆盖直接调用 `dependant.call` 的生成器 endpoint）均被计数，必须为 0；一旦有请求到达 handler，门禁在接收
探测结果前以 `endpoint was invoked during probe` 失败（退出码 2）。安装由所有活动 guard 共享，最后一个
退出时才恢复。
已展平为完整路径的普通路由可直接检查。

已知未实现操作与已知未声明状态码保留在脚本内的显式基线中，逐条带原因；实现或契约变更后必须同步删除条目，
否则 `stale_*` 检查失败。未实现操作另有行为要求：门禁解析中央占位表与占位错误映射，要求基线、占位条目、
注册模块与静态可观察状态（`{405, 503}`）一致，占位错误映射恰好为 `{operation_not_implemented: 503}`，
因此「注册了路由」不再等于「已实现」。基线与审计结论的对应关系见
[公开语音面契约一致性审计](../operations/contract-conformance-audit.md#10-可重复检查与-ci-门禁)。

本机验证：

```sh
make contracts-route-conformance
uv run pytest tests/unit/test_route_conformance.py tests/contract/test_ci_workflow.py -q --no-cov
```

## Design Token 门禁

`changes` 作业用 `.nvmrc` 固定的 Node 执行独立回归与全 Web 静态扫描，不安装 pnpm 依赖；适用于所有路由，失败会阻断最终必需检查。Web 作业的 `pnpm lint` 继续执行相同校验器。

| 范围 | 当前检查 |
| --- | --- |
| `tokens.json` | 必须可解析且包含有效 token；支持 `color` / `dimension` / `fontFamily` / `number` / `shadow`。primitive 颜色与 CSS 同名声明值一致（规范化空白与小数）；其余 token 使用单个 `var(--name)`。未知格式、缺失值及 DTCG 别名明确失败 |
| TypeScript 导出 | 所有字面量 CSS 引用必须已注册，包括 primitive；元数据文字不能充当注册记录 |
| Web 消费者 | 扫描 TS/TSX/JS/JSX/MTS/CTS/MJS/CJS、CSS、HTML、SVG；检查带空白、换行、嵌套 fallback 的 `var()` 引用。项目策略要求 fallback 引用也已声明；同一 CSS 规则块内拒绝循环别名 |
| 颜色 | 拦截 hex、RGB/HSL/HWB/Lab/LCH/OKLab/OKLCH/`color()` 字面量及颜色属性中的命名色。允许 `transparent` / `currentColor`；fragment href 只豁免其自身，不能放过同一行的颜色 |
| 例外 | 共享样式仅在 custom property 值中允许原始颜色；[静态资源例外](../../web/shared/design-token-exceptions.json) 按精确路径、SHA-256 和理由固定。当前为两个启动 HTML 元数据与四个独立品牌 SVG；内容改变须审查，不能批量刷新哈希掩盖新增违规。例外仍检查引用与外链 |

独立验证（均无需安装依赖）：

```sh
node --test tests/ci/design-tokens.test.mjs
node scripts/dev-env/validate-design-tokens.mjs
```

生产扫描排除 `.test.*` / `.spec.*` / `__tests__` 和构建/依赖目录。规则是静态字面量门禁，不能证明动态拼接、CSS 转义、浏览器级联/跨规则循环、视觉效果、对比度或所有出站请求正确。当前注册表是 CSS 字符串项目格式，**不是完整 DTCG 2025.10 交换格式**；不使用标准合规表述替代实际验证。

依据（2026-10-25）：[CSS Variables 的引用与 fallback 语义](https://www.w3.org/TR/css-variables-1/)、[DTCG 2025.10 格式与别名要求](https://www.designtokens.org/tr/2025.10/format/)。本门禁对未声明 fallback 的限制是项目策略，比浏览器 fallback 语义更严格。

## Web 无障碍与颜色对比度

Web／全量路由在现有受保护的 Web 作业中，构建后运行 `pnpm test:a11y`。使用锁定的 `@axe-core/playwright` 与现有 Playwright，只安装 Chromium headless shell；两个预览服务绑定 `127.0.0.1`，严格检查端口，不复用其他服务，不启动后端或模型。浏览器请求仅允许这两个回环端口。文档路由不安装浏览器。

| 覆盖 | 门禁 |
| --- | --- |
| 两个实际构建入口 | 系统浅/深色、显式浅色覆盖深色系统、显式深色覆盖浅色系统，各测 320px／1280px；页面运行 axe 的 `wcag2a`、`wcag2aa`、`wcag21a`、`wcag21aa`、`wcag22aa` 和 `best-practice` 标签规则，所有 violation 失败 |
| 主题语义文字 | 每个页面情景检查 21 对前景／背景：主、次、弱化与强调文字 × 四种表面，三个能力状态，以及强调／警示色对。透明值在明确的 page 底色上合成，普通文本以 4.5:1 检查；每个探针必须产生明确通过证据，不允许 incomplete 或缺失 Token |
| 键盘与焦点 | 首次 Tab 到可见跳转链接，Enter 后主内容真正获得焦点；焦点环可见、至少 2px，对三个基础表面达到 3:1。该项目策略不等于完整证明 WCAG 焦点区域／遮挡成功条件 |
| 用户偏好 | 两入口分别在浅/深色下验证增强对比度、强制颜色和减少动效；状态动效最长 5 秒，减少动效时无无限循环；强制颜色测 UA 实际调整后的颜色 |
| 门禁自证 | 故意注入无名称按钮、低对比度文字、失效跳转目标、隐藏焦点环、局部坏色带及玻璃合成回归，证明对应保护实际失败；亮度公式以已知色对和 4.5 临界值回归 |

axe 的 `color-contrast` incomplete 中，背景渐变、背景图像或文字过短的情况转入[栅格门禁](../../tests/web/raster-contrast.mjs)，不再使用文字位置或前景 Token 白名单。Chromium 在当前 viewport 按真实滚动位置截图，覆盖实际渐变、半透明背景及单层 `backdrop-filter`；DOM Range 定位每条文字行框，检查其中**每个 CSS 像素**的最差对比度，不只取中心点或渐变端点。小文本要求 4.5:1；24px 以上，或至少 18⅔px 且字重 ≥700 的大文本要求 3:1；判断使用未舍入比值。

测量时只临时隐藏选中文字节点的墨迹，保留 `currentColor`、背景、边框与玻璃合成；校验文字几何和原有绘制属性没有变化，异常时也恢复 DOM 和滚动。可能影响同一玻璃背景的文字分开截图，避免隐藏后方文字改变被测底色。每情景最多 512 个文字行框、12 张 viewport 截图，超限失败而非截断。PNG 用现有浏览器解码，不新增图像库、依赖或 CI 作业。报告增加逐行最差背景、比值、阈值、采样数及截图批次，失败额外附背景截图。

该方法限定于固定 Chromium、DPR=1、sRGB 8-bit 的现有静态情景。行框包含字间空白，比逐字形检查保守；通过只覆盖这些实际渲染像素，不能外推到所有尺寸、DPR、浏览器或动态帧。组透明度、可测的半透明前景、前景 filter/mix-blend/mask/clip、伪元素改变文字或背景、生成文字、动态绘制、嵌套玻璃及与文字交叠的非祖先玻璃层等未建模情况，明确失败并要求补充实现或针对性复核，**不保留自动放行例外**。其他种类的 axe incomplete 仍失败。依据：[W3C F83 最不利背景检查](https://www.w3.org/WAI/WCAG22/Techniques/failures/F83)，核验日期 2026-10-25。

无障碍检查步骤实际成功或失败且作业未取消时，上传 `web-accessibility-evidence`，保留 7 天；包含 HTML/JSON、逐情景 axe 与语义色对证据，桌面浅/深色截图，失败时额外保留截图和 trace。若此前步骤失败导致该检查未运行，或作业取消，不保证有此制品。报告仅含合成页面。维护测试时同时维护其覆盖声明，不能使用全局关闭规则、自动重试或既有 violation 基线掩盖回归。

本机复现：

```sh
pnpm install --frozen-lockfile
pnpm build
pnpm exec playwright install --only-shell chromium
pnpm test:a11y
```

Linux CI 使用 `--with-deps` 安装浏览器系统依赖。本机须有对应浏览器前置条件；失败不改用 jsdom 伪装浏览器通过。自动门禁覆盖当前工程外壳，不能代替屏幕阅读器、人工键盘走查、缩放/复杂遮挡、所有非文本图形或后续业务流程验收。规范核验日期为 2026-10-25：[WCAG 2.2](https://www.w3.org/TR/WCAG22/)、[文本对比度](https://www.w3.org/WAI/WCAG22/Understanding/contrast-minimum.html)、[Playwright 自动与人工测试边界](https://playwright.dev/docs/accessibility-testing)。
