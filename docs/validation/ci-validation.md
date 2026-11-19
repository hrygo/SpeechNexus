---
title: CI 验证范围与维护
status: active
---

# CI 验证范围与维护

CI 使用[工作流](../../.github/workflows/ci.yml)和[标准库路由](../../scripts/dev-env/ci-routing.py)选择验证范围。未知路径保守运行全量，不维护易遗漏的业务测试白名单。

| 触发或变更 | 范围 |
| --- | --- |
| PR 仅修改任意层级的 `AGENTS.md` / `README*`（含 `web/AGENTS.md`、`tests/integration/AGENTS.md`、各级 `README.md`，例外见下）、`docs/` 内 Markdown、`.agents/skills/<skill>/` 内 Markdown / JSON / YAML | 路由回归、审查 PR 路径边界、Design Token 与契约消费者绑定静态门禁（以上合并为一个不筛范围、不安装依赖的 `guards` 作业）、Markdown 代码块格式检查（独立作业 `docs_format`）、`路径:行号` 引用门禁（独立作业 `docs_refs`）、差异检查、矩阵新鲜度检查（仅矩阵、关联源、快照或生成器变化时重渲染）、Node Controller 镜像范围检查与其门禁、最终门禁；镜像构建按独立范围判定，通常跳过。不安装项目依赖、不启动业务测试服务。差异检查以取得准确基线为前提；否则回退全量 |
| PR 仅修改已知 Web 源码/静态资产，可混合上述文档 | Python 静态/契约检查、覆盖率数据采集、完整 Web 检查，以及 Web 能力道 `Integration 8`–`Integration 11`（真实后端 + PostgreSQL/MySQL/MinIO + 真实浏览器）；采集作业仍使用私有 PostgreSQL 和 MinIO，DB-only 分片 0–7、覆盖率判定与部署检查不适用 |
| PR 仅修改已知独立引擎目录或明确列出的引擎单元测试，可混合上述 Web/文档 | `no-db`：保留 Python 静态/契约检查、覆盖率数据采集、完整 Web、构建和部署检查；采集作业仍使用私有 PostgreSQL 和 MinIO，十二个双数据库集成分片、覆盖率判定及其汇总不适用 |
| CI、脚本、测试、依赖、配置、契约、迁移、部署、其他路径或空/不确定差异 | 完整 Python/Web/部署检查，十二个 PG/InnoDB/MinIO 分片及证据汇总 |
| `main` push、手动运行及未知事件 | 始终全量 |

精确白名单以路由脚本为准。新增应用、资源类型或目录默认全量；缩窄范围前先证明其不依赖被省略的验证，并添加回归。

规则文件与 README 按内容归类，不按位置：任意层级的 `AGENTS.md`、`README*` 与根 `AGENTS.md` 走同一 `docs` 车道（#812：`web/AGENTS.md` 曾因既不在 `docs/` 也没有 `web/<app>/src` 三段结构而被判为 `full`，每改一次子目录规则文件都要跑满矩阵）。三个位置的文件仍是构建或校验输入，保持原车道：

- 被镜像 `COPY` 的包 README：`asset-inspector/`、`backend/`、`execution-gateway/`、`node-controller/`、`packages/contracts/`、`packages/media/`。`deploy/images/python.Dockerfile`、`deploy/images/node-controller.Dockerfile` 与 `asset-inspector/Dockerfile` 把它们复制进包元数据，镜像工作流也把它们声明为构建输入，内容变化因此进入产物，仍走 `full`。
- `web/<app>/public/` 内的说明文件：Vite 把 `public/` 原样复制进构建产物（`dist`），文件本身是产品资产，仍走 `full`。
- `deploy/engines/file-voice-clone/README.md`、`deploy/engines/file-voice-design/README.md`：`tests/unit/gateway/test_engine_language_semantics.py` 直接读取并断言其措辞，`docs` 车道不执行 pytest，仍走 `full`。

其余说明文件不被任何构建或检查读取：Vite 只跟随导入（全仓库没有对 `.md` 的 `?raw`／`import.meta.glob`／`new URL` 导入，并由 `tests/ci` 的回归守住），`tsc` 与 ESLint 只处理 TS/TSX，Prettier 的 `format:check` glob 不含 `*.md`；`deploy/images/web.Dockerfile` 的 `COPY web web` 会把 `web/AGENTS.md` 的字节带进 builder 层，但最终阶段只发布 `dist`。这类文件包括源码树内的 `web/workbench/src/AGENTS.md`。`deploy/README.md` 虽被 `tests/ci` 的 unittest 守卫读取，但无范围过滤的 `guards` 作业本来就执行 `python -m unittest discover -s tests/ci`（跑在 `.python-version` 钉住的解释器上），守卫在轻量路径上继续运行。

例外集合由 `tests/ci/test_ci_routing.py::ProseRoutingTests` 双向核对：期望车道从三条独立规则派生（Dockerfile 字面量 `COPY`、单测读者、`web/<app>/public/`），再与 `classify` 和 `_prose_route` 逐条比对，新增例外而不补规则（或反之）都会变红。派生只认字面量 `COPY <path>.md`：目录形式复制（如 `COPY web web`、`COPY packages/contracts packages/contracts`）、`ADD` 与 glob 不在派生内，新增这类消费者须人工登记到 `_IMAGE_COPY_PROSE`。仅 `AGENTS.md` 与 `README*.md` 按内容归类，`agents.md`、`AGENTS.MD`、`Readme.md`、`README.rst`、`README` 等命名变体仍走 `full`（fail-closed，不会漏跑验证）。

Skill 的说明、参考、合成评测数据及展示元数据不参与产品构建，复用文档轻量路径。技能内的 Python、Shell、JavaScript 等脚本和未知文件仍走全量；混合业务代码时按业务范围选择，其他 `.agents/` 配置不在白名单内。轻量路径不执行 skill 行为评测，也不证明 JSON/YAML 内容或引用有效，维护者须按变更核对。`main` push 仍按既有约定全量验证。

独立引擎目录白名单为 `deploy/engines/` 下的 `cpu-vad`、`file-asr-audio`、`vllm-asr-lifecycle`；测试白名单为 `test_native_asr_scope.py`、`test_worker_fence.py`（均在 `tests/unit/gateway/`）。集成测试会导入其他部分单元测试工具，因此不按整个 `tests/unit/` 跳过数据库。后端/网关业务实现、共享测试工具、契约、迁移、共享依赖和 CI 配置仍触发全量；任意混合变更取更严格的范围。主干 push 与手动运行继续全量。引擎若接入数据库集成测试，须同步收紧路由并补回归。

## 必需检查与证据

三个受保护检查为 `Gate: required checks for this change`、`Web: lint, types, tests, build and accessibility`、`Compose: configuration validation`。它们分别替代原 `Python quality and tests`、`Web quality, tests, and build`、`Deployment configuration`；变更显示名称时必须同步 `main` 的 required status checks，并保持 GitHub Actions app 绑定及其他保护策略。迁移时先确认新名称在当前提交实际通过，再替换保护列表，不将缺失检查视为通过。

验收入口作业 `reference_acceptance` 由 `Gate: required checks for this change` 按与 `compose` 相同的范围映射要求：docs/web 范围期望 skipped，no-db/full 范围期望 success，失败、取消或该跑却跳过都使门禁失败。它不新增受保护状态名，三个受保护检查保持不变。它真实执行 `scripts/dev-env/reference-acceptance.py --dry-run`：工具与版本、从 `deploy/compose.yaml` 派生的 `${KEY:?}` 键与入口额外必需键、compose 渲染、三条互不相同的 Job DSN 身份、从迁移目录派生的 head、桶名和私有输入扫描都由脚本本身计算；合成私有输入按 `deploy/compose.yaml` 挂载的文件名与 lane 配置里的 `vad_model_file` 派生，新增 lane 或改模型文件名不需要改工作流；新增私有挂载必须同时提交 `deploy/private/<name>.example.json`（非 JSON 挂载同样按 `<name>.example.json` 命名，当前只挂 3 个 `.json`），缺示例时准备步骤直接失败——合成的空 JSON 只会让私有输入扫描凭「文件可读」假通过，所以这里没有降级通道。

作业另有四条不依赖写步骤的守卫，实现在 `scripts/dev-env/ci-entry-guard.py`，只做**结构完整性**检查，不做语义校验：

- `log` 校验[机器可读输出契约](../operations/reference-acceptance.md#机器可读输出ci-契约)：步骤顺序、逐条结论、`pending=N` 与 `待输入` 行数、dry-run 不得声明 PASS；延后项按稳定词登记（`DECLARED_DEFERRALS`，匹配「必须同时出现的词」并排除别的条件词），新增延后项必须一行登记，未登记即红。
- `sites` 把入口里的每条 `<ctx>.mutate(...)`（含 `ctx.runner.mutate`）与 `MUTATE_SITES` 清单逐条比对：命令消失、命令新增、结果被丢弃（裸表达式语句，或赋给 `_`——赋值即红，不看之后有没有再读 `_`）、`read` 的 argv 既不是字面量也不是 `READ_ARGUMENTS` 里登记的 argv 构造助手或 `*` 展开的登记只读段、`read` argv 里出现写词（`run/up/start/down/restart/rm/create`；`uv run` 是唯一允许配对；`sh`/`bash`/`python` 等紧跟 `-c` 一律判红）都使作业红。清单的「判定在哪」列必须用反引号点出入口里真实存在的定义，改名或删函数即红；「证据」列必须指向一个会被 `unittest` 收集的 `TestCase` 方法（`tests/ci` 下 `test*.py` 模块、方法名以 `test` 开头、所在类沿同文件基类回溯能追到 `TestCase`，从别的模块导入的基类不认识）或 probe 用例。以下都红：名字不存在、不在被收集的模块里，或同名方法在多个 `TestCase` 里出现（证据列只写裸方法名，守卫不按 `glob` 顺序挑；同名多处必须改成 `test:<模块>::<类>::<方法名>`，或给方法改名）；同一个类里同名方法定义多次（守卫读第一个、运行时用最后一个）；类体里赋值重新绑定证据方法名（`test_x = unittest.skip(...)(test_x)`）；方法或类（含同文件基类）上带 `@unittest.skip`/`@expectedFailure`，或条件为真的 `skipIf`/`skipUnless`（`skipIf(False)` 这类恒不触发的不算；条件不是字面量布尔值则按「无法确认会运行」算红，例外是能在 CI 平台 Linux 上算出值的平台条件：`sys.platform`／`platform.system()`／`os.name` 的 `==`/`!=`/`startswith`/`endswith`、它们的 `not`/`and`/`or` 组合，以及绑定到这类表达式的模块级常量（该名字在整个模块里必须只被绑定一次，且那次是模块级简单赋值；`for` 目标、`|=` 这类增量赋值、元组/列表解包、`with … as`、`except … as`、推导式目标、海象、`import … as`、`def`/`class`、`type` 别名（PEP 695）、`global`/`nonlocal` 声明和 `del` 都是第二次绑定，出现即不可证明、判红）——算出仍会运行则绿，算出会跳过则红）；`setUp`/`setUpClass`（含同文件基类）或正文里调 `skipTest`／`raise SkipTest`（写在 `if sys.platform == "win32":` 这类可证明在 Linux 上不进入的分支里的不算）；正文开头就 `return`/`raise`，或写在字面量恒真分支里让后文成死代码；正文没有任何断言；断言是守卫认得出的字面量恒真形态（`assert True`、`assertTrue(True)`、`assertFalse(False)`、`assertEqual`/`assertIs` 两边同一字面量、`assertNotEqual`/`assertIsNot` 两边不同字面量、`assertIsNone(None)`、`assertIsNotNone(字面量)`）；断言包在会吞 `AssertionError` 的 `try/except` 里。
- `probe` 用必然失败的输入调用 `PROBE_CASES` 里登记的纯判定函数；用例集合与实现集合不一致、入口函数改名/签名变化都会点名到「更新登记表与文档」，而不是误报「判定被掏空」。
- `wiring` 要求四条子命令各自是所在步骤的第一条被执行的命令（只允许 `set`/`export`/`cd`/`umask`/`unset` 这类环境前置，外加 `:` 这个可带重定向的 no-op；认法是「行首关键字 + 字符黑名单」，不解析 shell），因此把调用换成仍含命令字符串的 `echo`、或在它前面先跑别的命令都会红。前缀行还必须不含 `>`、`<`、`|`、`;`、`&`、`$(`、反引号和续行反斜杠，而且这是对**整行**做子串匹配，注释和引号里的这些字符同样计入：`:` 是内建 no-op，但 `: > file` 会立刻创建或截断文件、`: $(rm …)` 会执行命令、`: \` 会把下一行（也就是守卫命令）并进 no-op，带这些字符的行按「会先执行的命令」判红；同样的限制覆盖既有的 `set`/`export`/`cd`/`umask`/`unset`。`wiring` 还要求 `ci.yml` 每个作业的 `runs-on` 等于 `CI_RUNNER_IMAGE`（当前 `ubuntu-24.04`），标量、行内列表、块状列表三种拼写都按其中的标量元素逐个比对；其它拼写（加引号、带尾注释、`${{ matrix.os }}` 这类表达式）不做归一，按字面量比对即红。因为平台条件的三个判定值正是从它派生出的 `linux`/`Linux`/`posix`，换了 runner 而平台条件不动即红。

六张登记表（站点、只读 argv 助手、写词、probe 用例、延后项、自检语料）逐行镜像在[参考验收入口](../operations/reference-acceptance.md)的锚点块里，守卫每次运行都比较两侧：删一行登记、缩小写词集合或删掉一个自检用例，都必须在同一次改动里改文档，否则红。守卫每次运行还会跑自检语料（反空转、丢弃结果与 `_ =`、未登记站点、`echo`/`sh -c` 写形态、`read` 藏变量与 `*` 展开等），所以把规则改松会先弄红自检。

**覆盖边界**：迁移、角色、桶、栈、闸门与健康检查在 dry-run 下只被计划、不被执行。因此本作业证明的是入口的前置检查、派生值、步骤顺序、延后项契约和写/读边界的存在性，**不是一次参考部署**。

四条守卫都只看结构，下面这些看不见：判定条件写反方向却仍读 `.ok`／`.code`（正确写法是 `if not started.ok: raise`，改写成 `if started.ok: raise` 仍绿）；把 `.ok` 读出来只打日志、随后仍不中断；`ctx.read` 里用 `psql -c "DELETE …"` 间接写库；换接收者名、别名或包进自定义函数再丢弃返回的 `ctx.mutate`；`getattr`/反射拿到的 `mutate`；`bit=` 以外的执行原语（`subprocess`、`os.system`）；对已登记站点的判定语义作任何改动；证据与被登记站点不相关（只校验它会被收集、不会被守卫认出的挂起形态停掉、断言不是字面量恒真也不会被吞）；`skipTest` 藏在 `setUp` 调用的模块级助手里（守卫只看 `setUp`/`setUpClass` 自身的语句）；运行期用 `globals()`/`exec` 或下标改写模块常量、证据方法名（守卫只数静态绑定，只扫模块体与类体里的属性赋值、`setattr`/`delattr` 与 `del`）；绑定形态按名字在整个模块里收集，因此函数体或类体里的 `type` 别名也会让同名模块常量失去可证明性，这是宁可误红的取舍；`return` 藏在非字面量条件里，或断言恒真但形态不在上面那张单子里（例如 `assertIn('a', 'abc')`）；`*` 展开与 `read` 构造助手按**名字**认，同名不同语义的本地元组骗得过；环境前置不是 shell 解析的结果（只按「行首关键字 + 字符黑名单」认前缀行，不解析 shell），黑名单以外的 shell 构造仍可能被当成前置；平台条件按写死的 Linux 判定，`wiring` 只核对 `ci.yml` 里每个作业 `runs-on`（标量、行内列表、块状列表）的标量元素等于 `CI_RUNNER_IMAGE`，既不核对镜像实际报告的平台字符串，也不读别的 workflow（含 reusable workflow）里的 runner；「判定在哪」那句描述本身属实与否（只校验反引号里的名字在入口里存在）。登记表只证明「这条命令还在、这条证据会被收集且不会被守卫认出的挂起形态停掉、这一行的判定位置确实存在」，不证明证据覆盖了该站点的语义，也不证明「判定在哪」那句描述属实——这仍由 `tests/ci` 的假 Runner 用例与真实 apply 运行负责。行数锚点挡的是「不知情的缩水」：同一次改动里改 `PINNED_COUNTS` 再跑 `tables --write` 仍可行，取舍留给评审。所以「入口有 CI 作业」不等于「入口已被完整守住」，只是「入口的结构不会在无人知晓的情况下消失」。

作业级条件让不适用项明确显示 skipped；最终 CI 门禁用 `always()` 执行，核对路由成功和各项实际结果。非法/缺失路由、失败、取消或本应执行却跳过均失败。不适用不表示业务验证已通过。

正文范围的 Markdown 代码块格式由独立作业 `docs_format` 把关。Ruff 的格式化器会解析并重排 Markdown 内标记为 `python` 的代码块，而 `docs` 范围不执行 `python_static`，`uv run ruff format --check .` 因而在这条路径上从未运行：PR #659、#661 是单文件 `docs/reviews/` 新增（路由输出 `{"mode": "docs"}`），CI 显示 lint 作业 skipping 后正常合并，随后 `main` 的 push 运行在 `Check Python formatting with Ruff` 上变红（`docs/reviews/2026-11-13-ci-acceptance-gate.md:8`（该行须含 `本片只新增本报告`）、`docs/reviews/2026-11-13-reference-acceptance-rework.md:548`（该行须含 `新增文件仅`）），并让 #662 的同类红被误判为它自身的问题（#673）。该作业只检出仓库并用 `uvx --from "ruff==${RUFF_VERSION}"` 安装锁定版本（与 `uv.lock` 的 `ruff` 版本一致，`tests/ci/test_ci_ruff_pin.py` 双向核对这一处钉住的版本），不安装项目依赖；命令与 `python_static` 的格式步骤逐字相同（`ruff format --check .`），所以检查的文件集合不会比全量道窄，也不存在第二份会与路由白名单漂移的路径清单。范围映射：`docs` 期望 success，`web`／`no-db`／`full` 期望 skipped——这三种范围已由 `python_static` 执行同一命令，重复运行只增加固定开销；`Gate: required checks for this change` 按此映射核对，`docs` 上失败、取消或意外跳过都使门禁失败。

**覆盖边界**：这只覆盖 Ruff 能解析的代码块，即标记为 `python`（或 `py`）的围栏；标记为 `text`、`bash`、`yaml`、`json`、`sql` 的围栏从不解析，其中的错误命令、失效路径、非法 JSON／YAML 仍可通过。`ruff check` 不检查 Markdown，因此代码块内的 lint 级问题（未使用变量、导入顺序、命名）也不在范围内。文档结构、普通 Markdown 链接、双语 README 一致性、`.agents/skills/` 内 JSON／YAML 的有效性同样无人检查；`路径:行号` 引用由独立作业 `docs_refs` 把关（见[文档引用门禁](#文档引用门禁)），它的判据不覆盖上述项目，也不证明引用目标行的内容与文档描述相符。格式检查只保证「能解析且排版一致」，不证明代码块内容可执行、输出正确或与实现同步，仍需人工审查。

审查 PR 的改动路径由 `routing` 作业里的 `scripts/dev-env/check-review-pr-boundary.py` 把控。第 4 步「按风险审查与验证」只给出两条作者自律命令（`git status --porcelain`、`git diff origin/main...HEAD --stat`），CI 对它们从不响应：曾有审查 PR 从被审 head 拉出，报告之外携带了与被审字节相同的实现，squash `f88279dc` 以 `review:` 标题进主干，而被审实现最终又以另一份修订版 `4ad6cad7` 合并——被审代码与进主干的代码不是同一份（#738）。

判据是三条相互独立、命中任意一条即为审查 PR 的信号：标题以 `review:`（或全角 `review：`）开头；head 分支以 `review/` 或 `docs/review` 开头；结构上同时改动 `docs/reviews/` 与其它路径。边界为改动路径必须全部落在 `docs/reviews/` 下（与 SOP 第 4 步同一边界，该步已写明「`docs/reviews/` 下的报告文件」），依据是既有数据：33 个已合并的审查 PR 中 32 个只碰一个 `docs/reviews/*.md`，唯一例外即上述事故；没有任何审查 PR 改过 `docs/validation/`，因此不为它开口子。`REVIEW_ROOT` 由 `tests/ci/test_review_pr_boundary.py` 钉住，放宽边界必须同时改测试而不是只改常量。结构信号在 `main` 的 648 个非空非 merge 变更集上只命中事故那一次提交（全长 729 个提交含 80 个 merge 与 1 个空提交，不计入），历史误报为零。

比较端点由检出形态决定。`pull_request` 事件的 `actions/checkout` 默认检出 `refs/pull/<n>/merge`，`HEAD` 是 GitHub 生成的合并提交：主题为 `Merge <head> into <base>`，第一父提交是构建合并引用时的基分支头，第二父提交是 PR head。门禁比较该合并提交与它的第一父提交，得到的正是本 PR 叠加到基分支上的改动；主干在此期间合进的内容已包含在第一父提交里，不进入比较集。`github.event.pull_request.base.sha` 停在该 PR 最近一次事件，主干一动它就落后，`git diff base.sha..HEAD` 会把主干期间的提交算成本 PR 的改动，而三点也无效：`base.sha` 是合并提交第一父提交的祖先，`merge-base(base.sha, HEAD)` 就是 `base.sha` 本身。实测（2026-11-14，运行 43564561839，PR #741）：`base.sha=d58127c0`、合并提交第一父 `4514b65b`、第二父 `c7d924bf`，两点口径得到 31 条路径——三份 `docs/reviews/2026-11-14-*.md` 来自主干，另 28 条是主干合入的契约、工作流、脚本与测试——结构信号据此把标题为 `test:` 的 PR 判成「审查 PR 携带实现」；改按第一父提交比较后，同一输入只剩该 PR 自己的 9 条路径。检出被固定到 `head.sha`（本地运行或显式 `ref`）时没有合并提交，改用三点：`merge-base(base.sha, HEAD)..HEAD`，该 `merge-base` 即分叉点。形态由提交本身判定，且父提交**按顺序**核对：第一父必须是 `into` 之后的基分支头，第二父才是 `Merge` 之后的 head。只比集合不够——主题把两个角色写反时集合仍然相等，比较起点会落到本 PR 的 head 上，恰好看不到它自己携带的实现（审查在 #751 里构造出这条可执行输入：未声明、真混了实现的 PR 被判绿），所以有序比对是父角色倒置的唯一守卫。`--head-sha` 只用于发现事件与检出不一致（re-run 读到旧 payload、push 与检出竞态），不一致时打 `::warning title=Review PR boundary comparison::` 并**仍按第一父比较**：此时退回 `merge-base(base.sha, HEAD)` 恰好就是合并引用下的两点口径，会把一次正常重跑变成假红。`routing` 以 `fetch-depth: 1` 检出，本地只有合并提交本身，第一父提交按 SHA 抓取一次（与路由抓取 `base.sha` 同一机制，失败退避重试一次），取不到即按失败关闭处理。

归属与调度：该检查留在 `routing` 作业内。`routing`、`docs_matrix` 与 `docs_refs` 是没有作业级 `if:`、每个 PR 都执行的作业（其余作业按范围或依赖过滤）；选 `routing` 的真实理由是它在本步骤之前已经检出仓库并算出路由用的精确差异，边界检查因此不需要第二次检出，红灯也直接归因到路由；第一父提交在 `fetch-depth: 1` 下按 SHA 抓取一次，与路由抓取 `base.sha` 是同一机制。审查 PR 恰好都是 `docs` 范围 PR，放进按范围过滤的作业就会恰好在它该跑的地方被跳过。步骤排在路由之后，`routing` 失败使 `Gate: required checks for this change` 因 `ROUTING_RESULT != success` 变红，因此这条门禁自身就受合并门禁约束，不需要也不支持 `--admin` 绕过。

调度证据等级：**已实测**。此前登记的缺口是「docs 路由下本步骤真的执行」没有日志观测，审查 PR #751 的运行 43570172238、43570366503 补上了它：`routing` 先输出 `{"mode": "docs", "reason": "Only prose or skill documents/metadata changed; heavy checks are out of scope."}`，紧接着本步骤输出 `{"applicable": true, "ok": true, "signals": ["title prefix", "branch prefix"], "violations": [], "reports": ["docs/reviews/2026-11-14-boundary-merge-base.md"], "reason": "Review PR changes only docs/reviews/ (1 report file(s))."}` 与 `review-pr-boundary: Review PR changes only docs/reviews/ (1 report file(s)).`，作业成功。**限定**：这两次运行执行的是**基线版**脚本与 `ci.yml`（分支基于当时的主干，未含 #749 的改动），因此它们证明的是**调度与执行**——docs 路由确实到达该作业、步骤无 `if:` 的判断成立——不是新比较端点在 docs 路由下的行为。工作流定义取自 PR 的合并提交（含 head 的 `ci.yml` 改动），不是基线分支的版本：#749 的运行 43567960143 里该步骤命令带 `--head-sha="$HEAD_SHA"`、环境含 `HEAD_SHA: 9d6b08c2485d377917ad553464806771c9e7ae44`，而基线 `cdf60a9e` 的 `ci.yml` 没有这个参数。

失败方向：标题或分支已声明为审查 PR 时，比较端点取不到即失败关闭（红）——抓不到合并提交的第一父提交，或三点口径下算不出 `merge-base`，都按失败关闭处理，因为绿灯的代价正是上面那类事故，而修复动作完全在作者手里；抓取比较端点失败会带固定退避重试一次，仍失败时错误写成 `::error title=Review PR boundary unverifiable::` 并保留 git 的退出码与 stderr 首行，可从文案区分「网络/取不到」与「确实越界」（后者是逐条 `Path outside docs/reviews/`），不会把两种红混成一种；未声明的 PR 在 diff 取不到时按不适用处理，不把 Git／网络故障变成全仓库阻塞，路由在同样条件下本就回退全量。红灯会列出每个越界路径，并给出取码方式：`git show <被审 head>:<path>` 输出到管道或仓库外文件（如 `/tmp/<name>`），不得在被审 head 上检出或把其工作树抄回仓库。

边界是前缀而非类型：`docs/reviews/` 内的任意文件类型都算边界内，因此把「实现」也放进该目录（例如 `docs/reviews/impl.py`）不触发门禁——该目录下没有文件被导入或执行，实质无害，但这是第三条残余缺口。

已声明的审查 PR 若精确 diff 为空（或只有 `docs/reviews/` 之外、且一条报告都没有），同样判红，reason 为「reports no file under docs/reviews/」：边界「全部路径落在 `docs/reviews/` 下」对空集是空洞成立，实现额外要求至少一条 `docs/reviews/` 路径，这比 docstring 字面更严也更合理。成功路径的文案可区分 diff 是否取到：`No review signal: ...` 表示 diff 已取到并判过，`No review signal, and the exact diff is unavailable ...` 表示未取到、结构信号没有评估。

**覆盖边界**：这条检查只看路径，不比对 blob 哈希，因此证明不了「报告评审的就是被审 head」，也不验证报告内容、链接或 `路径:行号` 引用。`push` 到 `main` 的历史不再判（事件没有 PR 元数据），事故类问题必须在 PR 阶段拦住。比较口径只覆盖被审改动：基分支前进、其它 PR 合入的内容不进入比较集，因此这里的红灯只由本 PR 自身的路径造成。残余缺口有三处：标题与分支都被改名、且报告没有写进 `docs/reviews/` 的 PR 不构成任何信号；未声明 PR 在 diff 不可读时同样无法评估结构信号；边界只认路径前缀，不区分文件类型（见上）。

矩阵新鲜度作业 `docs_matrix` 每次运行都检出仓库，但只有本次差异触及 `docs/plans/delivery-tracking.md`、`docs/plans/delivery-issues.json`、`docs/plans/github-issues-snapshot.json`、`scripts/dev-env/refresh-implementation-tracker.py` 或 `scripts/dev-env/delivery_tracker.py` 时才执行重渲染；无关 PR 的该步骤 skipped，作业仍成功，不阻塞合并。重渲染只读提交的快照，并以快照的 `fetched_at` 固定表头时间，随后 `git diff --exit-code` 拒绝手改或陈旧矩阵。基线不可取得或差异计算失败时保守执行，不以跳过掩盖；该作业不查询 GitHub，因此不证明快照覆盖未来 issue 变化。

Node Controller 镜像验证由 CI 以同提交的 [reusable workflow](../../.github/workflows/node-controller-image.yml) 调用，不再单独订阅 PR、push 或手动事件。PR 根据准确 base SHA 与 checkout 的差异选择；控制器源码、镜像文件、构建上下文忽略规则、根锁文件和 workspace 元数据，以及镜像路由或门禁文件变更触发镜像验证。使用 NUL 分隔并关闭重命名检测，确保移出受监控目录的旧路径也被检查；空差异保守执行，base 无效或不可获取时失败。`main` push、手动及未知事件始终执行。fork PR 仍使用调用工作流的事件与 checkout，公开仓库取 base，不传私有凭据。

镜像 workflow 在 amd64 和 arm64 的 GitHub 托管 runner 分别以空缓存构建并运行镜像、哈希拒绝和合成 CA 检查。其内部 `always()` 门禁要求 scope 成功且输出只能为 `true` 或 `false`：前者要求整个矩阵成功，后者要求矩阵 skipped。调用方的既有 `Gate: required checks for this change` 再要求该 workflow 成功并提供合法输出；缺失、失败、取消或不适用范围以外的跳过都不能放行。镜像验证不新增分支保护状态名，也不证明真实部署 CA 或物理节点已通过验证。

作业名按「领域: 实际动作」命名，显示名称区分执行、证据验收和最终合并门禁；三个受保护状态名是唯一例外，改动它们必须同步分支保护。作业 ID 与显示名一一对应，依赖、命令、范围与超时不随改名改变：

| job ID | Actions 显示名称 | 实际职责 |
| --- | --- | --- |
| `routing` | `Route: scope and review boundary` | 按准确差异选择范围，并审查 PR 改动路径边界；不安装项目依赖、不跑任何测试，因此它在 checkout 后十余秒内发布 `mode`/`excluded_shards`，所有重作业随即启动 |
| `guards` | `Guards: routing boundaries, design tokens and contract consumers` | 不筛范围的轻量守卫：`tests/ci` 路由回归、Design Token 校验、生成契约消费者绑定。只装 `.python-version` 钉住的解释器与 Node，不安装项目依赖：这套守卫要解析产品源码，必须跑在产品声明的解释器上——runner 的系统 `python3` 是 3.12，读不了产品已在用的 PEP 758 `except A, B:`，会把「解释器过旧」误报成「产品语法错」（#1001）。结果被最终门禁无条件要求；与 `routing` 并行，因此不再把重作业挡在约 25 秒的串行前缀之后 |
| `python_tests`（矩阵两腿） | `Python: tests, coverage and build (part a|b)` | Python 单元、契约、CI 测试，导出四 worker 覆盖率数据集（不判定）、媒体工具链及 Python 包构建；集成标记的 Workbench/Speech PG 模块由十二个分片执行并采集覆盖数据，此作业不重复执行 |
| `python_static` | `Python: lint, types and contract compatibility` | Ruff、mypy、模块边界、生成漂移、协议兼容与 schema 校验 |
| `integration_plan` | `Integration plan: one collection selects every partition's tools` | 只在展开分片的路由（`full`/`web`）执行：一次 pytest 完整收集发布十二片的 `browser`/`node_contracts` 工具需求，供各分片读取；分片自身的执行与证据校验不依赖它 |
| `integration_shards` | `Integration 0/12`–`7/12: PG/MySQL/MinIO`；`Integration 8/12`–`11/12: Web + PG/MySQL/MinIO` | 十二个真实隔离服务分片，`max-parallel: 12` 同时启动；混合集合中的浏览器与 Node HTTP 消费者只分配到后四片（8–11），普通 DB 用例按估算成本分配到全部十二片，工具需求读取 `integration_plan` 的发布结果并 fail-closed 校验。`full` 展开十二片；`web` 只展开 8–11 四片（`excluded_shards` 排除 0–7），`docs`/`no-db` 跳过整个作业。每片内部再用 pytest-xdist `-n 2 --dist loadfile` 并发两个 worker |
| `integration_gate` | `Gate: integration partitions and merged coverage` | 验收十二片成功及同提交、完整互斥集合、实际执行证据；下载十二份分片覆盖数据与采集作业数据集，合并后按 80% 判定一次，不在此节点重新执行测试。仍只在 `full` 范围执行 |
| `python_gate` | `Gate: Python tests and static checks` | 要求 Python 执行与静态/契约两个作业均成功，不执行测试 |
| `controller_image` | `Node Controller: image evidence` | 调用镜像 scope、双架构构建与内部证据门禁，按输入差异决定是否构建 |
| `docs_format` | `Docs: markdown code block formatting` | 以与 `uv.lock` 一致的锁定 Ruff 复跑 `ruff format --check .`（含 Markdown 内 Python 代码块）；只在 `docs` 范围执行，只装 Ruff 自身，不安装项目依赖 |
| `docs_matrix` | `Docs: delivery matrix freshness` | 从提交快照离线重渲染矩阵并比对；仅矩阵、关联源、快照或生成器变化时执行渲染 |
| `docs_refs` | `Docs: path:line references` | 校验 `docs/` 内 `路径:行号` 引用；终态文档全量门禁、过程记录只计数（按基线报增量）；并在同一次检出上跑未发布变更清单门禁（`docs/releases/unreleased.md` 必须点名新迁移与新授权函数）。两条都无范围过滤，只用系统 Python 与标准库；`fetch-depth: 0` 供标签解析 |
| `required_checks` | `Gate: required checks for this change` | 按已验证范围核对全部适用结果，阻止失败、取消与意外跳过 |
| `web` | `Web: lint, types, tests, build and accessibility` | 格式、lint、TypeScript、SDK 契约、workspace 测试和构建、Web 镜像构建上下文完整性、真实浏览器无障碍与对比度 |
| `compose` | `Compose: configuration validation` | 执行 `docker compose config --quiet`，不启动服务或发布部署 |
| `reference_acceptance` | `Deploy: reference acceptance entry (dry run)` | 真实执行 `scripts/dev-env/reference-acceptance.py --dry-run`：工具版本、派生环境键、compose 渲染、三条 Job DSN 身份、迁移 head、桶名与派生出的私有输入；`ci-entry-guard.py` 的 `log`/`sites`/`probe`/`wiring` 四条结构守卫（输出契约与延后项登记、写命令清单与 `ctx.read` 只读边界、纯判定函数探针、作业接线），并比对文档锚点块里的登记表与自检语料；另做一条损坏环境的负对照，不启动服务或执行写步骤 |

完整范围要求覆盖率、契约生成漂移及十二个真实双数据库分片；契约兼容和结构变更的基线比较仅在 PR 执行，`main` push 与手动全量运行不会执行这些 PR 专属步骤。比较步骤获取当时的目标分支头作为基线，与路由使用的事件准确 base SHA 是不同依据。集成汇总继续核对同一提交、完整收集、互斥并集和实际执行结果，不以数量或仅收集代替证据。唯一允许的跳过是 `tests/integration/test_gateway_submit_ledger.py` 中以 `[mysql]` 结尾的既有 PostgreSQL 专属产品适配器参数；这不构成 MySQL 协调路径通过的证据，其他必需用例跳过会使汇总失败。



拆分后的首次绿色运行（2026-11-16，run 43813742783）：两腿分别为 245 s 与 183 s，十二个分片全部通过（146–272 s，启动跨 +48…+117 s），端到端 402 s；门禁 41 s。此前单作业测量稳定占用 285–451 s，是最长作业；现在关键路径回到分片尾部与门禁。同期观察：三 worker 片内并行会让 `test_startup_authority_561.py` 在共享集群上触发PostgreSQL `tuple concurrently updated`，因此片内并行为 2。
Python 测试作业是两个矩阵腿：每腿执行 `python-coverage.py run --workers 4 --partitions 8 --index-base {0|4}`，在 runner 内并发四个 `workbench-pg-probe.py --full-coverage` 进程，只运行非集成测试目录（`tests/unit`、`tests/contract`、`tests/ci` 与 `tests/integration/test_probe_inventory.py`）；该作业只测量并导出数据，不做覆盖率判定。集成标记的 39 个 Workbench／Runtime／Speech PG 模块由十二个分片执行，其覆盖数据在同一门禁里合并，因此在测量作业里再跑一遍只增加时长、不增加被覆盖的代码行（2026-11-16 之后不再重复）。标记为 `integration` 的用例由独立分片收集和执行；未带该标记的用例不因此自动进入分片，真实模型资格测试见下文。各进程独占临时 PG、Unix socket 与 coverage 文件；Speech PG 测试在 full coverage 模式使用同一 worker 的私有 socket，不连接 runner 的系统数据库。集成分片仍使用显式配置的双库地址。作业仍只准备一次依赖及隔离 MinIO。父进程先准备四个 fixture 共用的 `speechnexus-workbench-saved` 测试桶，避免四个进程首次创建竞态；其对象按随机 project 前缀隔离，其余桶按文件独占或随机命名。测试按完整收集的文件分组，文件内用例不拆开，较大文件先分到当前用例数较少的一组；这是确定性的数量均衡，不是耗时均衡。

每片保留原完整覆盖源与 branch 配置，只在片内使用 `--cov-fail-under=0` 采集局部数据，并固定导出到 `coverage-<shard>.data`。父进程必须确认四片执行成功、私有 PG 已停止、同一 SHA/配置、相同完整 collection、精确分配且互斥并集覆盖全集、每个结果实际通过及覆盖数据摘要一致，然后复制各片原始数据并按 worker 归档为 `workbench-pg-coverage-parts/coverage-<worker>.data`，连同汇总以 `workbench-pg-full-coverage` 上传；覆盖率不足不由该作业判定，因此数据照常保留。若采集步骤已运行且失败、作业未取消，工作流尝试上传已生成的日志及分片／生命周期／汇总报告；步骤未运行、作业取消或上传失败时不能保证留存。测试或执行证据校验失败不会生成合格的覆盖数据集。入口拒绝任何非空 `PYTEST_ADDOPTS`，即使其选项不缩小集合；完整集合以 pytest 收集时各 worker 观察到的结果为准，同一插件或配置在四片共同遗漏用例时，同集与分配校验不能独立发现，仍须审查测试根目录、pytest 配置和插件变更。直接使用 `scripts/dev-env/workbench-pg-probe.py` 的 `--full-coverage` 模式是单次 pytest、只覆盖非集成范围的本地检查，仍沿用项目 `fail_under = 80`：只被集成测试执行的新增代码会让它失败，而 CI 的合并门禁通过。本机要按门禁口径复核时使用上文的 `combine` 路径，或在该模式下自行加 `--cov-fail-under=0` 只看数据。`--coverage` 仍是独立的 C 层 90% 检查。两种模式都须安装锁定依赖并提供 `--pg-bin`，运行前置条件见[验证指南](../agent-verification.md)，具体参数可用 `uv run python scripts/dev-env/workbench-pg-probe.py --help` 查看。

分片不再各自收集：`integration_plan` 作业在安装锁定依赖后用 `uv run python scripts/dev-env/integration-shards.py plan --count 12` 实际收集完整 integration nodeid（包括参数化身份），一次性为十二片发布 `browser_<i>` 与 `node_contracts_<i>` 布尔需求；此前每片在测试执行前串行重复一次完整收集，实测 10–14 s/片（2026-11-16 的十二片拓扑下为 12 片 × 10–14 s ≈ 120–170 runner 秒）。`*_browser.py` 与两个合成浏览器探针模块声明浏览器依赖；回归核对当前所有 Node 浏览器探针消费者均被分类。收集错误或设置了非空 `PYTEST_ADDOPTS` 均会失败，即使该选项本身不缩小选择，也不能输出“无需工具”放行。各分片在读取需求前先校验本片的两个取值严格为 `true`/`false`，缺失或畸形取值在安装任何 Web/Node 依赖之前直接红灯。计划作业失败或被跳过时，分片随之跳过、两级门禁照常变红（fail closed）。证据保真不变：实际执行和报告的互斥并集核验仍从每片自身 collection 独立重算，plan 与实际收集的分歧无法通过门禁。预检记录 pytest 最终收集结果，未单独校验所有插件对收集的影响；汇总比较各片报告的相同全集，也不能独立发现所有分片共同遗漏的用例。须保持受控依赖与 pytest 配置，并审查收集范围的变更。

完整集合同时含普通用例与 Web 工具消费者时，仍保留八条 DB-only 道和 Web 能力道：浏览器与显式 Node 契约用例只落在 Web 能力道，普通 DB 用例按估算成本分配到全部十二片。每片的目标是等量估算成本，而不是等量用例数：一个 Web 用案例按 `WEB_CASE_WEIGHT = 5` 折算（2026-11-06 两次完整运行中两类用例的实测单例耗时比为 3.4–6.4 倍，类均值为 4.6–5.5 倍，取 5），Web 能力道因此少收普通用例。分配使用 nodeid 的 SHA-256 排序、最大余数法配额和确定性取整；十二片各自从完整收集独立算出同一结果，不依赖共享状态或历史制品。Web 用例在 Web 能力道间按 hash 顺序轮转，使各道用例数最多相差 1。一类用例单独存在时，仍按 nodeid 的 SHA-256 取模使用全部分片。通用入口仍接受 1–16 片，混合类别为 Web 工具保留 `max(1, count // 3)` 片（十二片时为 8–11 四片）；单片执行整个集合。预检、执行和报告检查使用同一个 `partition`，工具需求仍根据实际选中用例计算，不能在工作流里按编号跳过工具或业务测试。

片内并发：每片以显式 nodeid 调用 pytest，并加 `-n 2 --dist loadfile` 让两个 worker 分担用例。分片器先在同进程内解析一次完整 inventory，再把自己那份 nodeid 作为参数交给 pytest；xdist 下 collection 由 worker 完成，控制端等到调度之后才 deselect 会让每个 worker 跑完整套件，因此选择必须体现在命令行上。`loadfile` 保证同文件用例（含模块级 fixture 与 TRUNCATE 复位）留在同一 worker。`tests/integration/AGENTS.md` 要求每个用例使用唯一 schema/数据库名，这是片内并发的前提。`--workers` 由 `workbench-pg-probe.py --shard-workers` 透传，默认 2、上限 4。共享固定桶名的模块改用 `tests/support/minio_bucket.py::ensure_bucket`：`bucket_exists` + `make_bucket` 是 check-then-act，两个 worker 同时启动模块 fixture 时后者会拿到 `BucketAlreadyOwnedByYou`，该结果按成功处理，其它 S3 错误照常上抛。对象仍按随机 project 前缀隔离，因此同桶并发不改变断言。

该分组的目的不是把 Web 用例移入 DB 道，而是让 Web 能力道承担普通 DB 工作，从而使十二个分片的实测 pytest 执行时间接近。Web 能力道另需安装 Web 依赖、构建入口并安装 Chromium，这部分固定开销不计入 `elapsed_seconds`；划分只均衡实测执行时间，不试图抵消该开销，也不改变 job 预算。

有 `browser` 需求的分片安装完整锁定 Web 依赖、构建两个实际入口，并安装相同版本 Chromium headless shell 及系统依赖后运行测试；只有 Node 契约消费者的分片保留锁定生产依赖安装；数据库分片不安装浏览器。需求随真实 nodeid 分区计算，不在 workflow 中固定某几个分片号。汇总 `check` 子命令与 `tests/ci` 轻量路由回归继续仅依赖 Python 标准库。

浏览器准备复用[独立脚本](../../scripts/dev-env/prepare-ci-browser.py)：先从锁定 Playwright CLI 的依赖模拟结果取得缺失/升级包，再向 APT 请求 URI/大小/SHA256 清单。仅在这条清单命令中选择 SHA256 输出，避免 `--print-uris` 的历史 MD5 默认值；不改系统或实际安装配置。归档通过 APT 自带的 `apt-helper download-file` 最多四个并发下载，同时传入强摘要和 `Checksum-FileSize`，让原生获取阶段约束已认证大小；不新增下载器依赖或覆盖传输政策。写入完整 APT 缓存前仍独立核对 SHA256/SHA512 与大小，MD5-only、错误大小、错误强摘要及不匹配的已有缓存均拒绝。不覆盖未知缓存内容。

随后仍执行原 `playwright install --with-deps --only-shell chromium`。信任链依赖 APT 认证的索引与上述写前强校验，不能假定安装器会重新校验完整缓存：APT 2.8 的[现有归档路径](https://github.com/Debian/apt/blob/2.8.1/apt-pkg/acquire-item.cc#L3494-L3507)可能只按大小复用。未知或无法得到强摘要的计划直接失败，不降级到弱摘要或强制信任。

预取不更改软件源、APT/网络安全配置或安装器。仅对 HTTP/HTTPS 的 `Timeout`、`Pipeline-Depth`、`No-Cache`，直接 HTTP 的 `User-Agent-Non-Interactive` 及无相关控制项的情形开放；HTTPS 同名项、代理、自定义 User-Agent、地址族、速率/队列/hash、`apt-get` 或 `apt-helper` 的 Acquire 专用配置、替代配置、HTTP/HTTPS 传输命名空间中的未知或嵌套键以及不可读取配置，触发原安装器路径。该检查不是对所有 APT 配置项的穷举审计。APT 2.8.1 的[原生实现](https://github.com/Debian/apt/blob/2.8.1/methods/http.cc)按既有配置与 systemd unit 语境选择非交互标记；apt-helper 使用同一 acquire HTTP method，不改写配置值、header 或上下文。

预取策略诊断输出固定分类、退出码与错误类型；对直接字段仅允许 1–64 字符、字母开头且只含 ASCII 字母/数字/下划线/连字符的字段名。空字段另分类，其他文本只给分类/数量；该诊断不输出配置值、代理凭据、嵌套主机名、URL 或原始 stderr。原生 APT 下载与安装子进程沿用其自身标准输出和错误输出，不能把上述过滤保证扩展到它们的日志。识别出字段名不自动授予预取资格，必须先核对原生语义。未知计划、下载或校验失败会阻止安装，临时预取目录自动清理。真实 runner 的预取行为及收益须以当次运行证据验证。

分片验收应区分取消、未开始测试与断言失败；回退脚本接线可恢复原官方安装路径。

每个集成分片还检查 PostgreSQL 16 工具，通过 `workbench-pg-probe.py --integration-shard` 启动独立的私有 Unix socket PG，再把 Workbench DSN 传给原 `integration-shards.py run`。双数据库与 MinIO 环境仍指向该作业既有隔离测试服务，不用私有 Workbench DSN 替换协调数据库地址。原分片报告路径、完整 collection、实际执行与意外跳过拒绝规则保持不变；驱动器把分片覆盖数据路径交给 `integration-shards.py run`，生命周期报告、分片报告与该片覆盖数据一并上传，分别保存，不替代分片执行报告。仅有单个分片作业成功不能替代汇总验收。

真实内网模型资格与可复现集成分别验收。`qualification` 标记的三条纪要用例调用配置的真实模型，不进入普通 `integration` 分片，也不计作 CI 通过；它们使用测试装配的工作台运行时与隔离数据库，不能单独证明目标生产部署或替代协调事务的确定性门禁。`make workbench-llm-qualification` 要求显式的私有 Workbench PG DSN、获准内网 LLM endpoint/model，并构建真实 Web 入口后选择这三项；缺少上述配置时入口直接失败。该命令没有禁止 `PYTEST_ADDOPTS` 等外部选项缩小选择，验收时须确认三项均实际通过，不能将跳过或部分通过当成资格证据。普通 CI 的分片完整性仍拒绝任何必需用例跳过，阶段 AC 仍须另附这三项真实模型与版本证据。

不在整个 workflow 的 `on` 中设置 `paths` / `paths-ignore`：被整体过滤的必需检查可能长期 Pending。[GitHub 路径过滤说明](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax#onpushpull_requestpull_request_targetpathspaths-ignore)、[作业条件与 skipped 语义](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/control-jobs-with-conditions)是维护入口；核验日期为 2026-10-25。

汇总使用 runner 自带 GitHub CLI 和标准库脚本 `download-integration-evidence.py`：只列当前仓库/运行，最多一页 100 个制品且拒绝截断；以 `integration-shard-` 开头的制品必须恰为八个预期名称，其他名称的制品可以同时存在；元数据响应上限为 1 MiB，制品最多三个并发下载进程。每次请求限 15 秒，进程失败或超时最多尝试三次，每次使用独立临时文件，完整性校验失败不放行；令牌仅经环境传入。先核对未过期、所属运行、压缩大小与 API 提供的 SHA-256，再只读 ZIP 内的三份固定成员——该片报告、PG 生命周期报告与 `coverage-<shard>.data`——不提取任意路径；单 ZIP 接受上限 16 MiB，单 JSON 保持原集成报告的 8 MiB 上限，覆盖数据上限 32 MiB；下载先写临时文件，进程退出后才检查实际大小，因此这些上限不是传输期间的磁盘写入硬限制。`workbench-pg-full-coverage` 制品只按白名单提取汇总 JSON 与两份 `coverage-<worker>.data`，名称、数量、大小或摘要不符即失败。输出目录必须事先不存在，全部下载及归档校验成功后才开始发布报告；既有同提交、完整互斥集合与逐项执行检查继续独立执行。仅汇总作业有 `actions: read`，全局仍仅 `contents: read`，超时为十分钟（含锁定依赖安装与合并判定）；不以 CLI 下载成功替代业务证据验收。依据：[官方 artifact REST API](https://docs.github.com/en/rest/actions/artifacts)、[GitHub CLI API](https://cli.github.com/manual/gh_api)，核验日期 2026-10-27。

历史性能分析和运行故障记录见[归档](../archive/validation/2026-10-ci-validation-history.md)。

## Web 范围执行 Web 能力道

`web` 范围此前只跑 Python 检查与 `web` 作业：`integration_shards` 的作业条件是 `mode == 'full'`，而路由把 `web/` 改动判为 `web`。因此纯前端改动从不执行唯一在真实浏览器里驱动工作台产物、真实后端、真实 PostgreSQL/MySQL 与 MinIO 的分片（4/5，#757）。#745 只改 `web/workbench/src/**`，自己的 PR 全绿，主干合并后才在 `Integration 4` 因 Playwright strict mode violation 变红；修复 PR #756 的 PR 运行里该分片同样 skipping，作者只能手动 `workflow_dispatch` 才拿到证据。另一层（`tests/web/*.behavior.spec.mjs`，在 `web` 作业里跑 preview 构建并用 `page.route` 注入应答）覆盖的是同一产物在**替身**后端下的交互，不覆盖真实后端与真实数据库那条链；两层能力不重叠，而其中一层在 `web` 路由下不执行。

采用 **只跑 Web 能力道**：`web` 路由保留八片中的 6、7 两片。`ci-routing.py` 按 `mode` 输出 `excluded_shards`（`web` 为 0–5 六行，其余范围为 `[]`），工作流把它用作 `strategy.matrix.exclude`；八片仍静态声明在 `[.github/workflows/ci.yml](../../.github/workflows/ci.yml)` 里，`full` 不排除任何片，也不改变矩阵只声明八片的既有事实。分片算法、`--index` 与 `--count 8` 全未改动，所以 `web` 路由在 6、7 两片执行的 nodeid 集与 `full` 路由在同一两片执行的**完全相同**——不是另一个子集，也没有新增筛选逻辑；`tests/ci/test_ci_routing.py` 用真实分区器交叉核对「Web 能力道恰好是所有浏览器/Node 契约消费者被选中的那些片」。

被排除的方案与理由：

- **新增独立「Web 集成」作业**：要重复服务容器、锁定 Python/Web 依赖、构建与 Chromium 安装，还要另写一套「只跑驱动浏览器/产物的用例」选择逻辑，与六片分区、分片证据和覆盖数据脱钩，两套选择必然漂移。现有作业已经把这些前置串好，重排分片只是少展开四片。
- **把 `web` 判成 `full`**：作业数、runner 分钟与 PR 墙钟按 `full` 增长，其中四片 DB-only 分片、部署检查与参考验收入口对 `web/` 改动没有信号；这是用全跑掩盖范围判断，也正是「不得把 full 变成默认」要避免的。
- **只跑 `test_workbench_real_runtime_*`**：admin、删除、会议、合成探针等浏览器与 Node 契约消费者同样只在 4/5 两片执行，缩到单一文件名前缀会把同类缺口留在别的模块；分层筛选还需要在分片器之外再写一条选择路径。

### 实测代价

`web` 路由新增 **2 个作业**（`Integration 4`、`Integration 5`），其余作业集合不变。分片作业墙钟实测区间为 6 分 42 秒 – 10 分 40 秒（`Integration 4`：run `43575396153` 481 s、`43573021394` 581 s、`43577755898` 623 s、`43577692527` 640 s；`Integration 5`：同四次运行 409 s / 549 s / 402 s / 571 s；每片 392 例，pytest 执行约 6 分钟，其余为锁定依赖安装、Web 构建与 Chromium 准备）。这些作业与 `python_tests`、`web` 同波次启动，因此 PR 墙钟增量是 `max(分片 4, 现有关键路径) − 现有关键路径`。改造前 `web` 路由的关键路径是 `python_tests`（run `43572881930`：544 s 墙钟中 511 s 是该作业，runner 分钟 14.6）；改造后同一内容（只多一个 `web/` 无关改动）的实跑 run `43577755898` 墙钟 **651 s（10 分 51 秒）**、runner 分钟 **29.5**，关键路径变为 `Integration 4`。即 **PR 墙钟 +107 秒（约 +1.8 分钟）**、**runner 分钟 +14.9 分钟**、**作业行数 15 → 16**。

| 路由 | 作业行数 | 实际执行作业 | 分片作业 | 实测墙钟 | runner 分钟 | 覆盖真实浏览器链 |
| --- | --- | --- | --- | --- | --- | --- |
| `web`（改造前，run `43572881930`） | 15 | 9 | 0（六片整体 skipping） | 9 分 04 秒 | 14.6 | 否 |
| `web`（改造后，本方案；run `43577755898`） | 16 | 11 | 2（4、5 片） | 10 分 51 秒 | 29.5 | 是 |
| `full`（run `43577692527`：本改动自身的 PR 运行） | 21 | 20 | 6 | 11 分 53 秒 | 67.9 | 是 |

「把 `web` 判成 `full`」的代价即最后一行：相对改造前多 6 个作业行、11 个实际执行作业、约 53 runner 分钟，相对本方案仍多 5 个作业行与约 38 runner 分钟；而其中四片 DB-only 分片、部署检查与参考验收入口对纯 Web 改动不产生信号。

### 语义与门禁

- `integration_gate`（`Gate: integration partitions and merged coverage`）仍是 `mode == 'full'` 专属：条件、依赖、八片必然成功、同提交完整互斥集合与实际执行证据、以及合并后的 `fail_under=80` 判定全部不变；`web`/`no-db`/`docs` 下它继续 skipped。本次移动的是**分片作业的展开范围**，不是八片证据门禁的语义。
- 最终门禁 `Gate: required checks for this change` 新增一条范围映射：`integration_shards` 结果按范围核对——`docs`/`no-db` 期望 `skipped`，`web`/`full` 期望 `success`。这是收紧：改造前 `web` 路由对该作业没有任何要求，分片失败或被跳过都不会阻止合并；现在 `web` 上分片失败、取消或意外跳过都会让受保护检查变红。`docs`/`no-db` 的期望未变（仍不适用），`full` 上的期望与 `integration_gate` 重合而不冲突。
- Web 能力道上新增一条失败关闭守卫 `Require the Web lanes to select actual Web consumers`：`web` 路由下若该片**既没有**选中浏览器消费者（`steps.integration_tools.outputs.browser`）**也没有**选中 Node HTTP 契约消费者（同一步骤的 `node_contracts`），作业直接失败，避免「跑了但没跑 Web 用例」再次以绿呈现。两路都读 `requirements` 的真实输出，未出现、为空或不是字面 `true` 一律按未选中处理（fail closed）；任意一路为真即通过，因为单个分片可以只承载其中一类。该步骤只在 `web` 路由执行，`full` 路由的步骤与断言不变；判据（`tests/contract/test_ci_workflow.py`）与判定结论（`tests/ci/test_ci_routing.py::WebLaneGuardTests` 用真实 `requirements` 向量执行该步骤脚本）都被钉住。
- 该守卫只证明「这一片确实在跑 Web 用例」，不证明两类都被选中、也不证明线上集合被完整覆盖：`tests/ci/test_ci_routing.py::IntegrationLaneTests` 用真实分区器（fixture 集合，不是线上 2754 例）钉住每个 Web 能力道都同时选中浏览器与 Node 契约消费者，并逐元素比对确认该 fixture 的全部 Web 用例都落在这些道上；线上集合的覆盖证据仍是 `full` 路由的分片报告。
- `web` 路由下两片仍产出 `coverage-<shard>.data`，但**没有消费者判断**：它只作为失败证据随分片制品留存，`integration_gate` 不下载、不合并、不按阈值判定，覆盖率仍只在 `full` 的单一判定点合并判定（`web`/`no-db` 不判定，既有缺口不变，见下文维护事项）。守卫注释与本节都不得把它读成「覆盖率达到 80%」的证据。

### 复现与验证

```sh
# 路由输出：pure-Web 改动应为 web，并排除 0–3 片
python3 scripts/dev-env/ci-routing.py --event pull_request --base <sha> --output /tmp/route
cat /tmp/route    # mode=web / excluded_shards=[{"shard": 0}, ..., {"shard": 3}]
# 门禁与矩阵形状的结构回归（不安装项目依赖；用 `.python-version` 的 3.14）
python -m unittest discover -s tests/ci -v
```

三条反例与一次同改动的 `web` 路由实跑（都在 PR 阶段完成，运行记录长期可用；承载它们的一次性分支已删除）：

- 同改动只加一个 `web/` 注释后推成 `web` 路由：run `43577755898` 全绿，`Integration 4`/`Integration 5` 实际执行（`browser=true` 守卫通过），最终门禁 success。
- **条件改回 `mode == 'full'`**：基线工作流（`26fd8b39`）加回 #745 的 `aria-label`，`web` 路由 run `43577811710` 全绿、`integration_shards` 整体 skipping —— 证明红灯来自展开条件，而不是别的东西。
- **`web` 判成 `full`**：同一 `web` 改动按 `full` 判定的作业集就是上表最后一行的实测值（21 行 / 20 执行 / 67.9 runner 分钟），相对本方案多 5 个作业行与约 38 runner 分钟。
- **守卫的判据向量**：`tests/ci/test_ci_routing.py::WebLaneGuardTests` 直接执行工作流里该步骤的脚本，输入取自真实 `plan` 输出——只有 Node 契约消费者（`browser=false, node_contracts=true`，浏览器分类器失效时的真实分区结果）与只有浏览器消费者都通过；两类都没有（DB-only 分片）与 output 缺失都失败关闭，并把实际取值写进错误文案。
- **复现 #745**：在 `web` 路由 head 上把 `aria-label="新单元文本 字数"` 加回去，run `43577800599` 里 `web` 作业 green、`Integration 4` 失败于 `workbench-chapter-create-probe.mjs` 的 Playwright strict mode violation，最终门禁 failure。这是本改动要堵的那一层：组件侧守卫（#756）不存在时，只有真实后端 + 真实数据库 + 真实浏览器那条链能看见它。

## 覆盖率门禁、采集边界与结构性余量

覆盖率数据在 `python_tests` 作业采集、在 `integration_gate` 汇总作业判定一次。判定对象是 `[tool.coverage.run].source` 列出的全部生产包，参数为 `branch = true`、`[tool.coverage.report].fail_under = 80`、`skip_covered = true`、`show_missing = true`。数据来源是全量路由实际执行的两类覆盖数据：`python-coverage.py run --workers 4` 导出的四份 `coverage-<worker>.data`，覆盖上文列出的四个测试集合（`tests/unit`、`tests/contract`、`tests/ci`、`tests/integration/test_probe_inventory.py`）加 `WORKBENCH_PG_TESTS`、`WORKBENCH_RUNTIME_TESTS` 与 `SPEECH_PG_COVERAGE_TESTS`；以及十二个集成分片各自导出的 `coverage-<shard>.data`，覆盖 `tests/integration` 下其余用例。汇总用 `python-coverage.py combine` 合并十六份数据（四份 Python worker 加十二份分片），再以 `coverage report --rcfile pyproject.toml` 判定。采集作业与分片都不判定，同一门槛只在一处生效。

汇总作业对每份数据校验名称、存在性、大小、SHA-256 与 branch 数据，并要求采集作业结果为成功、十二片结果均为成功、数据摘要与分片报告一致；任一份缺失、为空、被替换或采集作业未成功都直接失败，不会退化为「只用采集作业的数据判定通过」。合并结果写入 `merged.json`（数据份数、合并百分比、判定结果），同时以 `merged-python-coverage` 制品留存。

测量作业对每条被选用例要求结果为 `passed`，唯一的例外是 `scripts/dev-env/python-coverage.py` 的 `EXPECTED_SKIPS` 里显式登记的 nodeid：这类用例在收集期恒定存在、在无法执行它的宿主上逐条报 `skipped` 并带原因（`tests/ci/test_build_sequences_without_minio.py` 的 计划层与负控制层），库内其余任何跳过一律判失败。名单是双向约束：未登记的跳过失败，登记了却已从收集里消失也失败（`tests/unit/test_python_coverage.py` 用真实 `--collect-only` 钉住后者），所以名单不会腐烂成「这批用例其实一直没跑」。

覆盖率判定只存在于执行十二个分片的全量路由（`full`）。`web` 与 `no-db` 路由仍采集数据但不判定，这是「判定与采集同源」的直接结果，作为已知覆盖缺口记入下文维护事项。

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
| **A1** 分片导出覆盖数据，在汇总证据里合并判定（**已采用**） | 计入 4021 + 1380，TOTAL 变 88.41%，余量 5543 单元 | 测试 0 分钟（分片已执行）；新增 6 份覆盖数据制品（每份约 1–2 MB）与一次 `combine`（十秒级）；汇总作业超时由 2 分钟上调到 10 分钟（含安装与判定） | 中：分片内启用 `--cov` 并落盘，`download-integration-evidence.py` 增列数据文件，覆盖率判定从 `python_tests` 迁到协调汇总 | 无（分母不变）；判定只在执行分片的全量路由存在 |
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
| [.github/workflows/ci.yml](../../.github/workflows/ci.yml) | 分片步骤导出 `coverage-<shard>.data` 并随分片制品上传；`integration_gate` 下载两类数据、校验、合并并判定；超时 10 分钟 |
| [scripts/dev-env/integration-shards.py](../../scripts/dev-env/integration-shards.py) | `run` 子命令以 `--cov --cov-fail-under=0 --cov-report=` 采集，固定 `COVERAGE_FILE`，在报告里记录数据文件名与 SHA-256；`check` 校验执行证据与数据摘要 |
| [scripts/dev-env/download-integration-evidence.py](../../scripts/dev-env/download-integration-evidence.py) | 分片制品按三份固定成员下载；`workbench-pg-full-coverage` 按白名单提取汇总与各 worker 数据（当前四份）；名称、数量、大小、摘要不符即失败 |
| [scripts/dev-env/python-coverage.py](../../scripts/dev-env/python-coverage.py) | `run` 只测量并导出四 worker 数据与 `workbench-pg-full-coverage`；`combine` 校验十六份数据（四份 worker 加十二份分片）、合并、按 `pyproject.toml` 判定，并写 `merged.json` |
| [pyproject.toml](../../pyproject.toml) | **不改**：`fail_under = 80`、`source`、`branch` 均维持现状 |

这属于门禁治理变更（改变判定所在作业与必要检查的证据构成）；`ci.yml` 的判定位置变化须与本节、`main` 的 required status checks 说明保持同步。

### 复现测量

两条命令即可复现（本机需可用的隔离 PostgreSQL、MinIO、MySQL 与 `SPEECHNEXUS_TEST_*` 变量；采集范围还需 `--pg-bin` 指向含 `initdb`/`pg_ctl` 的目录）：

```sh
# 1) 采集作业范围：四 worker 数据写到 /tmp/cov/job-parts，汇总在 /tmp/cov/job.json
uv run python scripts/dev-env/python-coverage.py run --pg-bin <pg-bin> --workers 4 --output /tmp/cov/job.json

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

## 调度完整性与预算实测（2026-11-15）

**并发取消只用于 PR。** `concurrency.cancel-in-progress` 改为 `${{ github.event_name == 'pull_request' }}`：过期的 PR 运行仍有价值——只有最新提交才是合并候选，取消它们能直接省下整轮 runner 分钟。`main` 的 push 不同，它是**唯一**验证 squash 后实际落在主干上那个提交的运行；被下一次合并取消后，该提交就没有合并后证据。2026-11-15 统计最近 20 次 `main` push，有 3 次（15%）被下一次合并这样取消，主干验证未完成。现在 push、手动运行与未知事件不再互相取消；PR 仍按分支取消过期运行。分组键不变，也不新增并发组。

**Python 测试作业预算 10 → 15 分钟。** 预算不是目标耗时。2026-11-15 两次完整运行的 `python_tests` 墙钟为 471 s 与 502 s（7:51 / 8:22），其中两 worker 覆盖率测量占 413 s；旧 10 分钟预算只剩 1:38–2:09 余量，一次慢 runner 尝试就会把整个作业取消，而不是让测试失败或被诊断。按项目既有规则「上限至少高于常态慢值 5 分钟」，取 8:22 + 5 min = 15 min。这不改变用例选择、断言、`fail-fast`、分片算法或证据验收，也不放松任何检查；只有挂起才会触及新的上限。

**内层探针必须同步。** `python-coverage.py run --workers 2` 会以 `--full-coverage` 调用 `workbench-pg-probe.py`，其内层 pytest 上限由 `COVERAGE_JOB_TIMEOUT_SECONDS` 派生。若该常量仍是 600 s，内层会在 600 s 主动结束 pytest 并打印超时诊断，新增的 15 分钟余量根本到不了慢 runner。因此它同步为 `15 * 60`，`FULL_COVERAGE_PYTEST_TIMEOUT_SECONDS` 仍等于 job 上限（既有「无预留」语义不变，诊断仍打印 `no reserve`）。`tests/contract/test_ci_workflow.py::test_python_coverage_budget_matches_the_workflow_job_timeout` 把 `python_tests.timeout-minutes`、该常量与两个派生函数钉在一起，任一侧单独改动都会变红；`test_integration_shard_budget_matches_the_workflow_job_timeout` 对 30 分钟分片预算做同样的事。集成分片的 30 / 25 分钟两级超时不变。

## CI 效率实测基线与优化优先级（2026-11-15）

**先定义效率，再谈优化。** 本节的效率指三个可度量量：合并等待时间（run 的 wall clock）、有效结论率（成功 / 被取消 / 失败）、runner 分钟消耗。上一节的并发取消、15 分钟预算与探针同步解决的是**验证完整性与抗误杀**，不是效率：抬高预算甚至会增加失败作业的最坏占用。把可靠性收益表述为「更快」是不成立的。

**度量方式。** `scripts/dev-env/ci-timing-report.py` 只读地经 `gh` 查询 Actions API，输出 wall clock、关键路径、每个作业的中位耗时 / 结束偏移 / 固定开销 / 净工作时间，以及并行因子；它不写仓库或远端，也不判定门禁是否通过。用法：`python3 scripts/dev-env/ci-timing-report.py --limit 40`，或 `--run <id>` 复现单次运行。`tests/unit/test_ci_timing_report.py` 钉住时间解析、固定开销分类、关键路径判定与报告规则。以下数据取自 2026-11-15 最近的 19 个 full 路由成功样本与 60 次运行；作业名沿用当时显示名（对应本次改名后的同一作业）。

**关键路径：wall clock 中位数 683 s（642–870 s），runner 约 3895 s，并行因子 5.70x。**

| 作业 | 中位耗时 | 中位结束偏移 | 固定开销 |
| --- | --- | --- | --- |
| `Integration 4/6`（Web） | 566 s | 593 s | 89 s |
| `Integration 3/6` | 542 s | 569 s | 56 s |
| `Integration 1/6` | 530 s | 556 s | 55 s |
| `Integration 2/6` | 525 s | 552 s | 54 s |
| `Python: tests, coverage and build` | 480 s | 504 s | 28 s |
| `Integration 5/6`（Web） | 449 s | 476 s | 85 s |
| `Integration 0/6` | 429 s | 458 s | 55 s |
| `Web` / `Python static` | 208 s / 101 s | 236 s / 128 s | 36 s / 28 s |
| 六分片证据门禁 / 最终门禁 | 40 s / 3 s | 678 s / 683 s | 23 s / 0 s |

结论一：**只有集成分片与两级门禁在关键路径上**。缩短 `python_tests`、`web`、`python_static` 或镜像作业不会缩短合并等待时间，只节省 runner 秒；把它们当作首要提速对象是无效优化。结论二：关键路径**在不同 run 间漂移**——单次 run `43670911505` 的最慢分片是 `0/6`（587 s），而中位数样本里最慢的是 `4/6`；静态地针对某个编号分片优化不成立。

**分片均衡已到静态启发式的极限。** 按 `integration-shards.py` 的 `WEB_CASE_WEIGHT = 5` 计算，六片加权用例数为 493 / 493 / 492 / 492 / 487 / 497（差 2%，几乎完美），但分离固定开销后的净工作时间差异达 40%：

| 分片 | 用例数 | 浏览器用例 | 净工作 | 固定开销 | 合计 |
| --- | --- | --- | --- | --- | --- |
| 0 | 493 | 0 | 354 s | 77 s | 431 s |
| 1 | 493 | 0 | 424 s | 79 s | 503 s |
| 2 | 492 | 0 | 439 s | 78 s | 517 s |
| 3 | 492 | 0 | 458 s | 79 s | 537 s |
| 4 | 392 | 19 | 466 s | 122 s | 588 s |
| 5 | 392 | 21 | 333 s | 121 s | 454 s |

按用例数或固定权重分区无法反映用例真实耗时：慢用例被哈希顺序随机聚到 3 / 4 片，Web 分片还额外承担约 43 s 浏览器与 Web 构建成本。**但 21 个样本的逐片方差分解推翻了「静态重分区可显著提速」的假设**：六个分片的均值只差 45 s（368–413 s），而单个分片在 run 间的标准差达 92–128 s（CV 0.24–0.32），单次 run 内最快与最慢片差距 224–269 s；固定开销则很稳定（均值 94 s，同类分片内标准差仅 4–8 s）。关键路径因此由 **run 间随机波动**决定，而不是分片固有负载。静态重分区最多消除那 45 s（约 6% 理论上限），且会被 ±110 s 噪声淹没；缓存固定开销同样无效。本节初稿曾据单次 median 快照推断「duration-aware 分区可省 106 s / 15%」，该推断不成立，此处更正。

在此数据下，能确定性压缩关键路径的是**提高并行度**（拆成更多片，降低每片用例数与绝对波动），而不是调大 `WEB_CASE_WEIGHT` 或缓存浏览器。**本轮已把分片数由 6 提到 8**：`ci-routing.py` 的 `INTEGRATION_SHARD_COUNT`、`ci.yml` 的矩阵 `[0..7]` / `max-parallel` / 三处 `--count` / `--shard-count`、证据下载与覆盖率合并参数，以及显示名 `Integration <n>/8:` 同步更新；Web 能力道由 4、5 变为 6、7，`web` 路由排除 0–5。用真实 2754 条 inventory 复核：八片加权用例数 364–374（差 3%），union 完整无重复，每片用例数由约 459 降到约 344（−25%）。`tests/ci/test_ci_routing.py::test_router_and_workflow_agree_on_the_partition_count` 钉住路由常量与工作流参数一致。

代价是固定开销按片数线性增加（每片约 94 s，八片比六片约多 188 runner 秒/次）：**这是用成本换速度，wall clock 下降而 runner 分钟上升**。初稿曾据「每片用例数 −25%」估计 wall clock 降到 550–580 s（−15~20%）；首个实测样本 594 s 一度让人以为该估计偏乐观，但累计三个样本后中位 547 s 恰落在估计区间下沿——**用单样本否定一个估计，和用单样本确认一个收益，同样不严谨**。度量脚本同时兼容 6 / 8 两种拓扑并输出 `shard counts`，正是为了这个前后对比。更精细的按用例耗时分区仍缺少数据源：现有 evidence 只有分片级耗时，需先采集每用例耗时并版本化。

### 改造后 8 分片实测（2026-11-15 样本快照，n=3）

用同一脚本的 `--run` / `--limit` 两套入口以相同算法计算。基线重算取最近 60 次运行中的 33 个 full 路由成功样本，得 wall clock 中位数 681 s（635–1078 s）、runner 3859 s、并行因子 5.67x，与本节 21 样本口径的 683 s / 3895 s / 5.70x 一致。改造后取 PR #831 的三次 full 路由运行。**本节是截至该时点的样本快照**，后续运行请用 `--run` 自行采样，不必回写本节。

| run | 整体结论 | wall clock | runner 秒数 | 并行因子 |
| --- | --- | --- | --- | --- |
| `43678543533` | failure（mypy 注解） | 594 s | 4167 s | 7.02x |
| `43679506586` | success | 545 s | 3881 s | 7.12x |
| `43680380262` | success | 547 s | 4192 s | 7.66x |
| **8 分片中位** | — | **547 s** | **4167 s** | 7.12x |
| 6 分片基线中位（33 样本） | success | 681 s | 3859 s | 5.67x |
| **变化** | — | **−134 s / −19.7%** | **+308 s / +8.0%** | +1.45x |

三次 wall clock（594 / 545 / 547 s）都低于 6 分片样本最小值 635 s，方向明确；但 n=3 与基线 33 样本不是同一量级，**不给置信区间，也不宣称稳定收益**。runner 代价 +22~+333 s（中位 +308 s / +8.0%），与初稿按固定开销推算的 +188 s 同一量级；分片变多后每片净工作时间缩短的抵消并不稳定，不宜把任一单次结果写成定论。

第一次运行整体结论为 failure，原因是新增的两个度量文件在严格 mypy 下缺泛型注解（本地全量验证漏跑 mypy，已补 `dict[str, Any]` / `cast` 修复），与分片拓扑无关；门禁作业仍按 `always()` 执行，因此 wall clock 可同口径计算。**该轮是一次「无效结论」，应计入有效结论率**，不能只记作提速样本。后两次运行八个分片与全部 26 项检查 success。

**噪声主导的结论在 8 分片下再次成立。** 三轮分片耗时（`0/8`…`7/8`）：

| run | `0/8` | `1/8` | `2/8` | `3/8` | `4/8` | `5/8` | `6/8` | `7/8` | 最慢片 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `43678543533` | 264 | 451 | 448 | 264 | 438 | 433 | 520 | 465 | `6/8` |
| `43679506586` | 265 | 421 | 428 | 467 | 265 | 267 | 468 | 447 | `6/8` |
| `43680380262` | 428 | 463 | 424 | 477 | 343 | 466 | 350 | 401 | `3/8` |

`3/8` 由 264 → 467 → 477 s、`4/8` 由 438 → 265 → 343 s，快片身份在 run 间轮换；同一轮内 `0/8` 与 `1/8` 用例数同为 370 条、净工作却是 192 s 与 385 s。前两轮最慢片都是 Web 能力道 `6/8`，曾提示 Web lane 有系统性负担，但第三轮 `6/8` 仅 350 s、最慢片变成 `3/8`，**该提示已被第三个样本证伪**。按用例数分区是弱代理，但静态地针对编号分片或 Web 道重分配同样无效；若要调整 `WEB_CASE_WEIGHT`，须先采集每用例耗时并做多样本方差分解。

**依赖源单点已解耦（2026-11-15）。** `uv.lock` 曾把 841 个 wheel URL 与 109 条 registry 钉在 `pypi.tuna.tsinghua.edu.cn`——那是开发者本机 `~/.config/uv/uv.toml` 的镜像配置，在生成 lock 时被带入主干，导致所有 CI runner 依赖单一镜像。PR #831 的 run `43674410931` 中 `Integration 3/6` 因该镜像对 `click==8.5.0` 返回 403，安装 3 秒后失败、整片约 9 分钟白跑。现将 URL 切回官方 `files.pythonhosted.org` / `pypi.org`（包版本与 sha256 全部不变，官方 CDN 对同一路径实测 HTTP 200，显式指定官方源后 `uv lock --check` 与 `uv sync --locked --dry-run` 均通过），并由 `tests/ci/test_dependency_source.py` 钉住「lock 中不得出现非官方源」。四个 `Install locked dependencies` 步骤（后随 `integration_plan` 作业增为五处）统一改为 `bash scripts/dev-env/uv-sync-retry.sh`：只对 403/429/5xx、下载失败、超时与连接重置重试最多 3 次，lock 漂移或非网络错误立即失败，避免用重试掩盖真实缺陷。该脚本支持 `UV_SYNC_ATTEMPTS` / `UV_SYNC_RETRY_DELAY` 覆盖，测试用假 `uv` 验证成功、瞬态与非瞬态三条路径。

**有效结论率是比几十秒 wall clock 更大的损耗。** 60 次运行中 45 成功、7 取消、5 失败、3 进行中；失败率 8.3%，且约 72% 的运行展开 full 路由（21 或 20 个作业，每次约 65 runner 分钟）。一次依赖安装失败会让整片约 9 分钟白跑，并使本次 PR 不产出有效结论。

| 优化项 | 预期收益 | 依据 | 约束 / 状态 |
| --- | --- | --- | --- |
| 依赖源解耦 + 瞬态重试 | 消除 403 类整片白跑，提升有效结论率 | 上述失败日志 | **已实施**：官方源 + 仅网络类错误有限重试，5 项合约测试钉住 |
| 提高并行度（6 → 8 片） | 实测中位 −134 s（−19.7%），**仅 3 样本，待更多样本确认** | 每片用例数 −25%，绝对波动随之下降；三次 wall clock 均低于 6 分片最小值 | **已实施**；runner 秒数中位 +308 s / +8.0%，与按固定开销推算的 +188 s 同一量级，属成本换速度 |
| 消除每分片重复 collection | 每片串行前缀 −10~14 s；runner 净变化约 −78 s/次（−98 s 重复收集 + 新计划作业 +~20 s） | run `43681245311` 八片 requirements 步骤实测合计 98 s；partition 对同一提交确定性 | **已实施**：新增 `integration_plan` 作业单次收集并发布八片工具需求；证据校验仍从每片自身 collection 重算，fail closed 不变。改造后首个成功样本 `43686099430`（墙钟 586 s、runner 4239 s）落在此前 8 分片样本噪声带内（545–594 s / 3881–4192 s），结构性验证成立（8×requirements 98 s → 校验步骤 ≈0 s + plan 作业 20 s），但单样本不区分收益与噪声，须继续用 `--run` 采样 |
| 按历史耗时确定性加权分区 | ≤45 s（≤6%），易被噪声淹没 | 分片均值差 45 s vs run 间标准差 110 s | **不推荐**；静态分区无法消除 run 间波动 |
| 缓存 Web 分片浏览器 / 依赖 | 关键路径约 −43 s（6%） | 121 s vs 78 s 固定开销 | 固定开销本身稳定，缓存只降均值不降波动，**不优先** |
| 收窄 full 路由判据 | 显著减少 runner 分钟 | 72% 运行走 full | 直接影响验证完整性，须与路由安全边界一并审查，**待决策** |
| 缩短 `python_tests` / `web` / 静态检查 | 对 wall clock 零收益 | 均不在关键路径 | 只节省 runner 秒，**不优先** |

**本节口径**：除「改造后 8 分片实测」小节外，上面所有墙钟、分片耗时与方差数据都取自**改造前 6 分片**的 21 个样本，作为基线保留，不因本轮改动重写；该小节是 n=3 的改造后样本快照，样本量与基线不可等同看待。文中历史章节（#507、#757、2026-11-07 run、`4/5` 两片、「六个分片」等）同样记录原时点事实；当前行为规范以本节与作业表为准。

**本轮结论**：依赖源与重试改善的是**有效结论率**（但 mypy 漏检造成的返工说明本地验证必须覆盖全部 CI 门禁）；提高并行度针对 **wall clock**，代价是 **runner 分钟上升**；消除重复 collection 同时改善 wall clock 与 runner 秒（每片少付一次收集、全局只付一次）。两者都不是免费提速。后续任何「提速」主张都必须说明优化的是三个量中的哪一个，并给出改造前后同口径、**样本量可比**的数据；只报「作业更快」而不报关键路径变化，或用单次运行外推稳定收益，都不构成效率结论。

## 端到端 5 分钟目标与十二片拓扑（2026-11-16）

目标是把 `full` 路由的端到端墙钟（workflow 启动到 `Gate: required checks for this change` 完成）从约 8:49 压到 5 分钟以内，并且只用真实执行时间的缩减：不改用例选择、断言、`fail_under`、证据校验或 `required_checks` 语义。

### 基线与关键路径

取 run `43781129694`（`full` 路由，全部检查 success）作为同口径基线：总墙钟 529 s（8:49）。

| 作业 | 墙钟 | 说明 |
| --- | --- | --- |
| `routing` | 31 s | checkout 3 s、`tests/ci` 11 s、setup-node 4 s、Token 与契约消费者约 8 s、路由判定 1 s |
| `integration_plan` | 26 s | 一次完整收集并发布工具需求 |
| 分片（最慢片） | 435 s | 起点 +65 s；容器初始化 34 s、每片步骤约 20 s、pytest 最长 377 s |
| `integration_gate` | 22 s | +502 s 开始，下载 8 份证据并合并覆盖率 |
| `python_tests` | 353 s | 覆盖率步骤 306 s，不在关键路径 |
| `web` / `python_static` | 230 s / 115 s | 不在关键路径 |
| `required_checks` | 3 s | 收尾 |

关键路径由分片作业决定：分片之前是 routing（约 10 s）、plan（约 26 s）与排队，实测 +65 s 起步；分片之后是 25 s 证据汇总。本轮把 routing 里的守卫挪走，前缀由「31 s routing + 26 s plan」变成「10 s routing + 26 s plan」，约省 20 s。plan 仍是分片的前置依赖（分片要用它发布的 `browser_<i>`/`node_contracts_<i>` 决定装不装浏览器），因此没有把它从 `needs` 里去掉：去掉后每片要自己多付 10–14 s 收集，净收益只剩 10 s 左右，却多花 12 × 12 s 的 runner 时间。

同一分片在不同运行中的 pytest 耗时相差 2–2.4 倍（分片 0 的六个样本为 190、192、299、359、463、469 s），而用例构成只解释约 ±20%（按类内均值归一化后 0.84–1.31x）。**端到端时间的主要方差来自 runner 主机，不是分区偏斜。**因此「5 分钟」只能按常态（中位数）表述，尾部仍可能超出；把单次样本当作稳定收益，或把一次慢运行当作分区失败，都不成立。

### 本轮改动

1. **十二片 + 片内两 worker。** `INTEGRATION_SHARD_COUNT` 由 8 改为 12，矩阵 `[0..11]`、`max-parallel: 12`；`integration_plan` 发布 12 组 `browser_<i>`/`node_contracts_<i>`，`plan`、证据下载、`check`、覆盖率 `combine` 与探测器的 `--shard-count` 五处参数同步。每片用显式 nodeid 调用 pytest 并加 `-n 2 --dist loadfile`，普通分片用例数由约 371 降到约 248；`web` 路由仍只展开 8–11 四片。
2. **路由与守卫拆分。** 原先挂在 `routing` 里的 `tests/ci` 回归、Design Token、契约消费者绑定迁到并行且不筛范围的 `guards` 作业，`routing` 只保留 checkout 与范围判定；`guards` 结果被最终门禁无条件要求（意外 `skipped` 也算失败）。
3. **覆盖率采集 2 → 4 worker。** `python-coverage.py` 的 `PYTHON_COVERAGE_WORKERS_MAX`、CLI `choices`、分片元数据校验与 `workbench-pg-probe.py` 的 `coverage_count` 上限一并放宽到 4；四份 worker 数据加十二份分片数据共十六份合并判定。
5. **按成本而非用例数分区。** 分片器用 `HEAVY_CASE_WEIGHT = 3` 给需要真实 PostgreSQL/MySQL 或 Workbench 运行时的模块加权（`_pg`/`_postgres`/`_mysql`/`_migration_pg` 文件、`workbench_runtime`、`test_gateway_submit_ledger`），再按每道目标的成本切连续区间，而不是先算用例配额。2026-11-16 用同一次完整 inventory（2769 例、其中 928 例属于重模块）对比：旧算法八条 DB 道的估算成本为 388–438（跨度 1.42x），新算法八条全部为 402（1.00x），四条 Web 道 309–345 升到 350–359，再加各自约 12 例五倍权重的浏览器用例后仍在 410–419；整体成本跨度 1.42x → 1.15x。用例数跨度基本不变（1.27x → 1.26x），因为均衡目标已经从数量换成成本。
   首次运行（run 43821357681，2026-11-16）全绿：十二片 143–282 s、启动跨 +51…+53 s、端到端 370 s。各道估算成本已经相等，而实测跨度仍有 1.97x（lane 4 的 143 s 与 lane 3 的 267 s 同为 DB 道、成本同为 402），印证了本节前面的判断：剩余方差来自每片独占的 runner 主机，继续调分区没有收益，要压尾部只能压每片固定成本。
   对象存储启动随后改为后台执行、只在需要时等待就绪（run 43823979448 全绿，377 s）：实测只有带 Node 步骤的 Web 道真正重叠——lane 8 的等待降为 0 s，代价被 42 s 的 Node／pnpm／打包／Chromium 步骤吸收；DB 道没有可重叠的步骤，等待仍是 9–11 s。这项收益是 4 条 Web 道各约 9–11 s，不是每条道 9 s；Web 道因此成为新的尾部（270–285 s）。
   Web 道那 42 s 里最大的一块是 `Install Chromium`（22 s），但它不是浏览器下载：缓存 `~/.cache/ms-playwright` 后（run 43830012103 的冷跑与重跑对比），缓存恢复只花 2–4 s，而 `Install Chromium` 仍是 15–31 s——时间花在`playwright install --with-deps` 的 APT 系统依赖上，缓存浏览器包省不到它。该缓存已回退：它只增加 2–4 s 恢复时间。要让这 22 s 消失，得让 runner 自带这些系统库（预置镜像或更大 runner 的基础镜像），不是工作流能改的。
4. **并发预算。** 同时运行的长作业为 12 分片 + `python_tests` + `python_static` + `web` + `controller_image` 的两个构建作业，加上接近完成的 docs/compose/reference 作业，峰值约 19，低于账号的 20 个并发作业上限；16 片会到 22，因此停在 12。

### 实测与预期

本机按 CI 口径验证（macOS，PG 16.15 / MySQL 8.4 / MinIO 容器；本机核数与宿主 runner 不同，耗时不可直接比较）：分片 0/12 选中 248 例，`-n 2 --dist loadfile` 下 88.9 s 完成（含一次完整 inventory 收集 2769 条），`coverage-0.data` 摘要写入报告，`selected` 与 `outcomes` 各 248 项；分片 2/12 走完整探针路径（私有 workbench PG + 双 worker）245 passed / 3 skipped（均为允许的 `[mysql]` 参数）、75.6 s。`tests/ci` 455 项、`tests/unit/test_integration_shards.py` 59 项、`tests/unit/test_workbench_pg_probe_driver.py` 41 项、`tests/contract/test_ci_workflow.py` 298 项全部通过。片内并行单独实测：7 个数据库模块 106 例，串行 83.4 s、`-n 2` 49.9 s（1.67x），结果集合一致。

按基线分解推算的常态值：分片起点由 +65 s 提前到约 +45 s，容器初始化 34 s 与步骤约 20 s 不变，pytest 约 248 例 ÷ 2 worker × 每例约 0.9–1.1 s ≈ 110–140 s，收尾约 15 s，单分片作业耗时约 3:00–3:30（0:45 启动，3:44–4:14 完成）；加 `integration_gate` 22 s 与 `required_checks` 3 s，端到端约 4:10–4:40。5 分钟是常态上限，不是保证。覆盖率作业由 306 s 降到约 200–260 s（四 worker，非线性），仍在关键路径之外。

这些是推算与单机实测，不是 GitHub 宿主上的样本：真实收益须在同一工作流参数下用多次 `full` 运行核对，并把每次的分片 `elapsed_seconds`、作业墙钟与总墙钟一起看。分片报告的 `elapsed_seconds` 已包含新的 inventory 收集，与旧口径可直接比较「每片付出多少」。

### 同步点

改动其中任一侧都必须同时改另一侧，`tests/ci` 与 `tests/contract` 会拦下不一致：

- `INTEGRATION_SHARD_COUNT`（`ci-routing.py`）↔ 矩阵 `shard`、`max-parallel`、显示名、`integration_plan` 的 12 组输出、`plan`/下载/`check`/`combine` 的 `--count` 与探测器的 `--shard-count`；`tests/ci/test_ci_routing.py` 钉住常量与工作流参数，`tests/unit/test_integration_shards.py` 与 `tests/contract/test_ci_workflow.py` 钉住矩阵与计数。
- `WEB_CASE_WEIGHT`/`HEAVY_CASE_WEIGHT`（`integration-shards.py`）↔ 分区的成本模型 ↔ `integration_plan` 发布的每片用例数与工具需求；改权重会改所有分片的 `selected`，`check` 按同一模型重算，两边不一致即失败。
- `SHARD_WORKERS_DEFAULT`/`SHARD_WORKERS_MAX`（`integration-shards.py`）↔ `workbench-pg-probe.py` 的 `INTEGRATION_SHARD_WORKERS` 与 `--shard-workers` ↔ 工作流的 `--shard-workers 2`。
- `PYTHON_COVERAGE_PARTITIONS_MAX`（`python-coverage.py`）与 `PYTHON_PARTITION_MAX`（`workbench-pg-probe.py`）↔ 工作流 `python_tests` 矩阵的 `--partitions 8` 与两腿的 `--index-base` ↔ 门禁 `combine` 的两次 `--job-summary/--job-parts` ↔ 证据下载器的两次 `--coverage-artifact`。两侧分片数满足 `sum(workers) == partitions` 且索引互不重叠，合并仍按一份数据集判定。
- `PYTHON_COVERAGE_WORKERS_MAX`（`python-coverage.py`）↔ CLI `choices` ↔ `workbench-pg-probe.py` 的 `coverage_count` 上限 ↔ `download-integration-evidence.py` 的 `COVERAGE_WORKERS_MAX` 与门禁的 `--coverage-workers` ↔ 工作流的 `--workers 4`；`tests/ci/test_ci_inspection_retirement.py` 与 `tests/contract/test_ci_workflow.py` 分别钉住工作流里的 worker 数与下载参数。
- `guards` 作业 ↔ `required_checks` 的 `needs`、`GUARDS_RESULT` 环境变量与无条件校验；`tests/contract/test_ci_workflow.py` 与 `tests/ci/test_node_controller_image_ci.py` 各有一组真值表。
- 分片内并发的前提是唯一 schema/数据库名（`tests/integration/AGENTS.md`）与固定桶名的竞态处理（`tests/support/minio_bucket.py`）；新增共用固定名资源的模块必须走同一约定，否则只能在该片降为单 worker。

## 作业命名规范

作业名按「**领域: 实际动作**」组织，让运行列表里每一项都能看出它做了什么、红灯该找谁，不必展开日志：

| 前缀 | 含义 | 作业 |
| --- | --- | --- |
| `Route:` | 决定本次验证范围，不执行业务测试 | `routing` |
| `Python:` | Python 执行或静态检查 | `python_tests`、`python_static` |
| `Integration <n>/12:` | 十二个分片之一，`/12` 明示总数；8–11 四片带真实浏览器 | `integration_shards` |
| `Guards:` | 不筛范围的依赖无关守卫 | `guards` |
| `Gate:` | 只验收证据、不重跑测试 | `integration_gate`、`python_gate`、`required_checks` |
| `Docs:` | 文档车道的轻量检查 | `docs_format`、`docs_matrix`、`docs_refs` |
| `Web:` / `Compose:` / `Deploy:` / `Node Controller:` / `Image:` | 对应领域的执行与证据 | `web`、`compose`、`reference_acceptance`、`controller_image` 及两个镜像 reusable workflow |

三个受保护状态名（`Gate: required checks for this change`、`Web: lint, types, tests, build and accessibility`、`Compose: configuration validation`）**保持不变**，它们是分支保护的必需检查，改名必须同步保护列表；其余显示名与内部 job ID 一一对应，本次一并改为可辨识的名称。镜像 reusable workflow 里原先没有 `name:` 的 `scope` 作业原先在列表中只显示为 YAML key `scope`，看不出它决定了「本次是否需要构建」，现补为 `Image: required for this change`；两个镜像 workflow 的 `image` 与 `gate` 也统一为 `Image:` / `Gate:` 前缀并写明是哪个镜像。内部依赖、`needs`、命令、范围映射与超时不随改名改变。

## 集成分片耗时与超时

`integration_shards` 的作业超时为 30 分钟。超时是作业预算：改变它不改变用例选择、断言、`fail-fast`、分片算法或证据验收，也不放松任何检查。该预算与两级超时对 `web` 路由的 8–11 四片同样适用：`web` 只改变展开哪些片，不改变片的执行内容、内层上限或收尾预留。

分片执行有两级超时，二者必须成对调整：

| 层级 | 位置 | 当前值 | 行为 |
| --- | --- | --- | --- |
| job 预算 | `.github/workflows/ci.yml` 的 `integration_shards.timeout-minutes` | 1800 秒（30 分钟） | runner 侧硬上限；到点直接取消作业，后续 `always()` 步骤、分片摘要与证据上传都不再执行 |
| 探针内部上限 | `scripts/dev-env/workbench-pg-probe.py` 的 `INTEGRATION_SHARD_PYTEST_TIMEOUT_SECONDS` | 1500 秒（25 分钟） | 从分片运行器（pytest）启动时开始计时；到点由探针结束子进程、打印诊断信息并保留日志，随后照常停库和写生命周期报告 |

内部上限由 job 预算减去 300 秒派生（`INTEGRATION_JOB_RESERVE_SECONDS`）。差值不是余数，而是显式留给探针之外的工作：探针启动前的容器初始化、checkout、依赖安装与 MinIO，Web 分片还包括 pnpm 依赖、构建与 Chromium；探针超时后的停库、写生命周期报告与本地证据落盘也共用同一预算。2026-11-06 run `42251268385` 的 Integration 2 attempt 1（job `128006854118`）：作业 16:19:39 开始，探针 16:21:17 启动，准备耗 98 秒；1080 秒内部截止在 16:39:17，超时消息 16:39:20.76，作业 16:39:26 完成。从内部截止到作业完成约 9 秒，到 runner 硬上限 16:39:39 约 19 秒；旧 120 秒差值扣掉 98 秒准备后已无完整收尾余量，收尾几乎被 runner 抢占。300 秒覆盖按分钟计的 Web 依赖安装与构建，也给收尾抖动留出余量。

为什么是 1800/1500：同 run attempt 2 在相同 commit、相同分片上成功，作业 760 秒、pytest 670.98 秒；其余 DB 分片两轮约 462–544 秒，Web 分片约 87–200 秒。attempt 1 在 1080 秒时进度 84%（12%→84%，全部通过、无 F），但同 commit 重跑约 671 秒完成，说明这次 21 分钟是单次 runner 慢尝试/stall，不是分片已结构性增长到 21 分钟；把 84% 线性外推为 1286 秒与重跑事实矛盾，不能作为预算依据。分片耗时仍会随用例数增长，且单次慢尝试已超过 18 分钟；1500 秒内部上限高于该慢尝试（1084 秒）约 416 秒，1800 秒 job 再留 300 秒准备/收尾，覆盖高方差尾部。Integration 2 常态约 12.7 分钟，新增预算只由慢尝试或挂起分片使用。

取舍：提高 job 上限会增加最坏情况下的 runner 占用（失败分片最长约 27 分钟），但不改变常态耗时、用例选择或断言；降低内部上限只会让仍在推进的分片更早被误杀，无法覆盖高方差尾部。`--full-coverage` 模式不在此预算内，按 `python_tests` 的 15 分钟 Python 作业使用 900 秒（见下节）；该模式内部上限等于 job 上限、没有预留（既有不自洽），诊断会明确打印 `no reserve`，本次不改其行为。

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
| 分片用例数 | `integration-shards.py check` 输出 | 十二片用例数是否均衡；按 DB/Web 资源类内均值比较，某片超过同类均值 1.25 倍时打印 warning，不改变门禁结论 |
| 分片耗时 | `integration-shards.py check` 输出与 `integration-shard-<n>.json` 的 `elapsed_seconds` | 某片是否超过 15 分钟预警线或同类均值 1.25 倍；只告警，不改变门禁结论 |
| 十二片横向对比 | 各分片 job summary 的 `Job wall clock`、`pytest execution`、`Selected cases` | 十二片相对耗时、最慢片与用例数 |

`integration-shards.py check` 逐片打印用例数、`elapsed_seconds`、相对同类均值倍数；每片运行结束时也打印自己的用例数与耗时。用例数超过同类均值 1.25 倍、耗时超过 15 分钟或同类均值 1.25 倍时打印 warning，不失败。各分片 job summary 的指标表由 workflow 的 `Publish integration partition timing` 提供（每片的 Job wall clock、pytest execution、Selected cases）；check 只把 warning 与 skipped 写入汇总 summary，避免两份重复表。

两个摘要步骤只写 `$GITHUB_STEP_SUMMARY`，不参与任何结论；作业结论仍由分片执行与证据校验决定。被超时取消的作业不会执行 `always()` 步骤，因此超时运行没有分片摘要，只能从未上传的制品与 `cancelled` 结论识别。

规则：某片常态耗时（近期多次运行的典型值，不是单次抖动）超过 15 分钟时，必须拆分或重排分片——提高 `count`、调整矩阵或按实际成本重新分区——并使上限至少高于最慢分片常态值 5 分钟；不得继续抬高上限来代替拆分。15 分钟是预警线，30 分钟是当前余量而非目标值。内部上限不得成为更早的隐性上限：任何分片常态耗时接近 1500 秒时按同一规则拆分，而不是把内部常数改回更小值。本次 #502 只修复超时诊断与预算自洽，未改 6 分片划分；#507 随后在不改预算的前提下重排 6 分片：普通 DB 用例分配到全部六片，Web 用例仍只落在 Web 能力道，同 collection 实测最慢分片从 480.6 秒降到 380.9 秒；单次 stall 不作为拆分依据，拆分依据是近期多次运行的常态耗时。若后续确认某片常态接近 15 分钟，再独立做分片重排。调整 job 超时、探针内部上限或观测步骤时，同步更新本节、推导常数与基线（核验日期：2026-11-07）。

## 路由维护

- 对比事件的准确 base SHA 与实际 checkout 的合并结果；路径用 NUL 分隔，不按换行拆分，也不使用 API 文件列表截断后的子集。
- 重命名同时考虑旧/新路径。将 Python 文件移入文档目录或删除源码仍须全量。
- 基线不可用时回退全量；路由或轻量回归失败时阻止合并。不要用 `continue-on-error` 或缺失输出兜底放行。
- 轻量入口使用 runner 的系统 Python 3.12+，只依赖标准库；`guards` 是例外，它把 `tests/ci` 跑在 `.python-version` 钉住的解释器上（当前 3.14），因为这些守卫要解析产品源码，而系统 3.12 读不了 PEP 758。Ruff 对轻量入口及 `tests/ci/` 仍固定 `py312`（守卫自身保持 3.12 可解析），产品代码继续遵循项目 Python 版本。

从仓库根目录验证变更：

```sh
# `python` 必须是 `.python-version` 的 3.14：更旧的解释器会跳过语法判定而不是判红
python -m unittest discover -s tests/ci -v
uv run pytest tests/ci tests/contract/test_ci_workflow.py tests/unit/test_integration_shards.py -q --no-cov
```

需要完整复核时可在 Actions 手动运行 CI；本机完整检查仍按 [AGENTS.md](../../AGENTS.md) 的宿主和隔离服务前置条件执行。

## 文档引用门禁

[check-doc-refs.py](../../scripts/dev-env/check-doc-refs.py) 校验 `docs/` 与 `deploy/` 内 `路径:行号` 引用的目标是否存在、行号是否越界，以及指向 Markdown 的引用所带的内容断言是否仍在目标行附近。判据五种，都是机械的：路径在仓库里不存在（`MISSING`）、行号超过文件总行数（`OUT_OF_RANGE`）、只写裸文件名且仓库里同名文件不止一个（`AMBIGUOUS`）、断言片段不在目标行 ±15 行内（`DRIFT`）、指向 Markdown 的终态引用没写断言（`UNASSERTED`）。行内标注「外部依赖」的引用不计入问题。

### 终态文档与过程记录

| 类别 | 范围 | 处置 |
| --- | --- | --- |
| 终态文档 | `docs/` 下除过程记录外的全部 Markdown（`architecture/`、`operations/`、`components/`、`design/`、`decisions/`、`plans/`、`prd.md`、各级 `README.md`、`validation/` 下的非 dated 文件等）与 `deploy/README.md` | 全量门禁，一条问题即红 |
| 过程记录 | `docs/reviews/**`、`docs/archive/**`、`docs/validation/YYYY-MM-DD-*.md`、`docs/audits/YYYY-MM-DD-*.md` | 只计数，不门禁；按基线报增量 |

依据是 [AGENTS.md](../../AGENTS.md)「文档终态与事实化」对两者的区分。过程记录按约定保留原时点事实：审查报告引用的是当时的代码行、临时 worktree 绝对路径（`/private/tmp/sn772/...`）和从未提交的探针文件，这些引用失效本身就是证据，改动它们等于篡改证据。而长期 `RESULT FAIL` 的检查等于没有检查：接入 CI 前全量扫描的 138 条问题里 137 条出自 `docs/reviews/`、1 条出自 dated 验证记录、**0 条出自终态文档**，真正的终态文档漂移会被这套噪音淹没。判不准的路径一律按终态文档处理，宁可让它变红。

### 内容断言：行号漂移必红

`MISSING` 与 `OUT_OF_RANGE` 只回答「行号在不在」，不回答「那一行是不是被描述的那件事」。行号是序数：目标文档在上方插一段，它下面所有行号整体后移，而引用字面仍然合法。#1038 的 6 处指针就是这样在 `compose-minimal.md`（+129 行）、`deploy/README.md`（+243 行）之后偏了 74–142 行，门禁照旧 PASS。

所以指向 Markdown 的终态引用必须带内容断言，把「那一行是哪件事」写进引用本身：

- 形式：`` `deploy/README.md:1270`（该行须含 `check-public-job-execution-defaults.sql`） ``；
- 判据：片段必须出现在目标行 **±15 行**内，否则 `DRIFT`，并报出最近命中行与相差行数；
- 只有终态文档判定；过程记录连断言都不校验（冻结）；
- 代码目标的引用允许暂时不带断言，报告用 `ASSERT ... unasserted_code=` 计数——那是待补的欠账，不是失败条件。

窗口取 ±15 的取舍：容忍同文件上方的小幅插入（一段话、几行表格），而 74–142 行的整体漂移必然超出；窗口取 0 会让「在文档顶部加一句话」把下方引用全部判红，重演 #859 那种长期红、最后被无视的检查。边界（15 绿 / 16 红）由 `tests/ci/test_doc_refs_ratchet.py` 的子进程用例钉住。

片段由作者从目标行附近挑，必须是该行真实存在的内容。门禁只校验存在性，不判断它是不是「该行最重要的那件事」，也不证明指针的语义正确——那仍要人读。片段不要带小节序号（`## 7.` 之类）：内容没变、只是重新编号时，带序号的断言会红。

`docs/reviews/` 的豁免有内容守卫，判据两项，都作用在文件自身而不是路径可见性：

- 文件名形如 `<YYYY-MM-DD>-*.md`；
- 一级标题写明审查／复核／核对，且开头 400 字符引用 issue/PR 编号（`#<数字>`）。

只查文件名不够：把终态文档另存成合规文件名，它的引用就全部降级成「记录」不计门禁。加内容判据后，2026-11-17 实测 79 份审查报告全部通过，而 `docs/` 下 130 个终态文档的原文搬到合规文件名下全部被拒（`docs/architecture/speech-api.md` 也在其中——它的首个 `#<数字>` 是目录锚点 `(#1-…`，不是 issue 号）。布局不合约定时检查直接失败，且不能用基线接受——基线只登记引用问题，不登记目录布局。

这条守卫防的是误放，不是对抗伪装：刻意在开头补一句「审查 #859」仍能通过，那样的改动同时改了路径与内容、在 diff 里可见。`tests/ci/test_doc_refs_ratchet.py` 对当前语料逐份核对这两条判据仍然成立（2026-11-17 实测 79 份报告通过、130 个终态文档副本被拒），文档里这一句不靠人工记忆保鲜。

### 围栏代码块不算引用

围栏（三个连续反引号或三个波浪号界定）内的 `路径:行号` 不参与检查。围栏里贴的是命令输出、探针摘录或示例片段，是文档引用的别人产物，不是作者对当前文件的指认；按引用计入时，20 条 `MISSING` 来自审查记录里的探针输出与 `/private/tmp` 绝对路径，指向从未提交的 `zz-probe-*.ts` 与临时 worktree。接入 CI 时实测：1427 条 `路径:行号` 文本里有 119 条落在围栏内，若按引用计入会多出 27 条问题（21 `MISSING`、5 `AMBIGUOUS`、1 `OUT_OF_RANGE`）——其中 20 条 `MISSING` 来自审查记录的探针输出与 `/private/tmp` 临时 worktree 路径；终态文档正文（围栏外）的问题数为 0。
这条规则对终态文档同样生效，也就是「看不见」：终态文档围栏内的文本只计入 `SUMMARY` 的 `fenced_terminal=`，不判定。理想形态是围栏排除只对过程记录生效，但本门禁的证据块本身要在围栏里贴失败的 `RESULT FAIL` 输出（就是本节下文那几段），按范围收窄会把证据块判成坏引用。因此保留统一排除，并用 `fenced_terminal=` 把终态文档这部分规模摆在报告里：当前为 4 条，全部来自本节自己的证据块。收窄到过程记录是未做的后续项。

### 棘轮与基线

`--baseline <json>` 只对基线之外的新增终态问题变红；`terminal` 为空列表时等价全量门禁。[doc-refs-baseline.json](../../scripts/dev-env/doc-refs-baseline.json) 就是这样一份基线：`terminal: []`，`records` 记录过程记录的问题计数（2026-11-17 刷新后为 `docs/reviews`：`AMBIGUOUS=127`、`MISSING=24`、`OUT_OF_RANGE=3`；`docs/validation`：`AMBIGUOUS=1`），只用于在报告里显示增量，不参与判定。要接受一条终态问题只能改 `terminal` 列表，那次改动在 PR diff 里可见，不是静默放行；`--write-baseline` 按当前结果重写基线，基线登记的问题消失时报告打印 `BASELINE stale=`，由同一条命令缩短。过程记录有实质变化时按同一命令刷新 `records`——刷新只改变增量显示，不改变任何判定（2026-11-17 的那次刷新见下节的刷新区块）。

输出固定四段：逐条问题（终态问题在前，新增标注 `[新增,基线外]`、基线已接受标注 `[基线已接受]`；过程记录默认只给计数，`--show-records` 逐条打印）、`SUMMARY`（`refs` 参与判定的引用数、`fenced` 围栏内跳过数、`fenced_terminal` 其中来自终态文档的条数、各判定计数）、`SCOPE`／`RECORDS`（终态与过程记录分目录计数，带基线时给出 `旧->新` 增量）、`RESULT PASS|FAIL`。退出码 0 表示没有新增终态问题（过程记录的增长不影响），1 表示有新增终态问题或目录布局不合约定，2 表示环境不可用（不在 git 仓库、扫描路径不存在、基线不可读）。

### 修复与排障：`--fix` 与 `--show-ok`

`--fix` 处理可机械修复的 `DRIFT`：断言片段在目标文件里唯一命中时，把引用处的行号数字改写为该命中行的行号；命中 0 处或多处时不动该条，仍按 `[DRIFT]` 报告并标 `[UNFIXABLE：原因]`（出现 0 次，或出现 N 次并列出命中的行号）。它只改行号数字，不碰片段文本，也不改被引用的目标文件；同一行有多条引用时按数字区间从右往左替换，后面的引用不会因前面的改写而错位。计划里的位移带符号（`30 -> 500 (+470)`），章节级搬移在计划里直接可见。

`--fix --dry-run` 只打印计划、不写文件：每条列出引用处、`旧行号 -> 新行号 (+位移)` 与命中的那一行原文，结尾 `FIX drift=… fixable=… unfixable=… projected_new=…` 与 `FIX RESULT PASS|FAIL`。它按首次扫描外推门禁，而不是只看 DRIFT：计划未覆盖全部 `DRIFT`、`projected_new` 非零（计划外仍有基线外终态问题）或存在过程记录布局问题时退出码为 1，避免「门禁红但无 DRIFT」显示成可以合并；验收时用 `git status` 的未变化证明没有写入。

`--fix` 写入后立刻用同一套扫描重新自检，并额外跑两条不变式：逐条用**精确行号**核对写入行确有该片段（不复用 ±15 窗口——窗口容得下 15 行误差，正好放过「刚写下去的行号偏 1 行」），以及引用总数不下降（写入丢了引用时逐条核对无从谈起，门禁计数却会静默变少）。输出 `SELFCHECK written=… failures=… drift_before=… drift_after=… unasserted_before=… unasserted_after=…`，违反项以 `FIX SELFCHECK FAIL` 列出。残留 `DRIFT`、新出现 `UNASSERTED`、基线外终态问题、过程记录布局问题或自检失败都以非零退出；`--fix` 不能与 `--write-baseline`、`--json`、`--show`/`--show-ok` 同时使用。

`--fix` 的边界是「唯一命中」，不是「语义正确」：片段唯一但实际指错位置时，工具会把行号改到那一处并让门禁变绿，语义仍要人读。命中多处时刻意不选最近的一条（#1042 是手工处理过这类情形）：工具不替作者猜。

`--show` 仍只打印问题项，但恢复档位标记：基线外的新问题标 `[新增,基线外]`，基线已接受的标 `[基线已接受]`，过程记录标 `[过程记录]`，排障时先看这个。

`--show-ok` 把 OK 侧补回 `--show`：每条 OK 引用也打印被引用行的原文，用来核对「这条为什么算 OK」「片段是不是恰好命中了同一文件里另一处」。没有把全部结果塞进 `--json`：JSON 面向机器，两千多条 OK 行只会淹没真正的问题，人读的排障路径是 `--show`。

上述行为由 `tests/ci/test_doc_refs_ratchet.py` 的用例钉住：dry-run 不改文件且与门禁同判、`--fix` 只动行号数字、多处命中不改且其余照改退出非零、精确行自检拒绝偏 1 行的写入、引用总数下降即失败、layout 计入退出码、过程记录不被改写、`--show-ok` 的原文与目标行一致、`--show` 的两个档位标记。

### AMBIGUOUS 判据审计

`AMBIGUOUS` 的含义是「写法不足以定位」，不是路径错误；判据是否过严要逐目标核对，不能用一张忽略表收场（#859 的验收要求）。2026-11-17 对当次 128 条 `AMBIGUOUS` 逐目标审计：

- **全部落在过程记录**：`docs/reviews` 127 条、dated 记录 `docs/validation/2026-11-14-job-workspace-default.md` 1 条；**终态文档 0 条**——两侧终态问题数都是 0，所以接入 CI 时就是全量门禁。
- **19 个不同目标，全部是裸文件名**，各自在仓库里的同名文件数为 2–7 个：`jobs.py`（3）、`readiness.py`（7）、`app.py`（5）、`settings.py`（4）、`main.py`（4）、`profile_publications.py`（4）、`admission.py`（3）、`runtime_intents.py`（3）、`assets.py`（3）、`builtin_voice_qualification.py`（3）、`public_jobs.py`（2）、`App.tsx`（2）、`runtime.py`（2）、`voice_drafts.py`（2）、`owners.py`（2）、`test_production_jobs.py`（2）、`test_runtime_delivery.py`（2）、`demo-transport.ts`（2），以及一条省略号截断路径 `backend/.../domain/leases.py`（同名 2 个）。
- **0 条判据过严**：逐目标核对同名文件数，没有任何「仓库里只有一个同名文件却被判歧义」的情况；省略号那条属写法不足，归入同一类。
- **处置：不放开判据、不加忽略表。** 过程记录按「保留原时点事实」只计数不门禁；终态文档出现同类写法立即红，基线 `terminal` 保持为空（没有接受任何例外）。

基线 `records` 的刷新与这次审计同批完成。刷新只改变增量显示，不改变任何判定：

```text
$ # 刷新前：基线仍是接入时的计数，RECORDS 显示过程记录的累计增量
SUMMARY refs=1856 fenced=129 fenced_terminal=4 AMBIGUOUS=128 EXTERNAL=1 MISSING=24 OK=1700 OUT_OF_RANGE=3
SCOPE terminal problems=0 new=0 accepted=0
SCOPE record problems=155 docs/reviews AMBIGUOUS=127 MISSING=24 OUT_OF_RANGE=3; docs/validation AMBIGUOUS=1
RECORDS docs/reviews AMBIGUOUS 88->127 (+39) MISSING 23->24 (+1) OUT_OF_RANGE 1->3 (+2)
RESULT PASS                                    # exit=0

$ python3 scripts/dev-env/check-doc-refs.py --write-baseline scripts/dev-env/doc-refs-baseline.json
WROTE scripts/dev-env/doc-refs-baseline.json terminal=0 records=155

$ # 刷新后：计数与现状一致，RECORDS 不再打印增量，终态判定不变
SUMMARY refs=1856 fenced=129 fenced_terminal=4 AMBIGUOUS=128 EXTERNAL=1 MISSING=24 OK=1700 OUT_OF_RANGE=3
SCOPE terminal problems=0 new=0 accepted=0
SCOPE record problems=155 docs/reviews AMBIGUOUS=127 MISSING=24 OUT_OF_RANGE=3; docs/validation AMBIGUOUS=1
RESULT PASS                                    # exit=0，new=0
```

过程记录的计数不是门禁：`docs/reviews` 会继续增长，增长只改变这行报告里的数字，不阻塞合并；要让它重新可见，在 `docs/reviews/` 有实质变化的同一 PR 里再跑一次 `--write-baseline`。

### 作业与门禁

`docs_refs`（`Docs: path:line references`）只检出仓库，用 runner 的系统 Python 3.12+ 与标准库执行 `python3 scripts/dev-env/check-doc-refs.py --baseline scripts/dev-env/doc-refs-baseline.json`，不安装项目依赖；同一次检出上的第二步跑未发布变更清单门禁（见[未发布变更清单门禁](#未发布变更清单门禁)），它要解析发布标签，所以检出用 `fetch-depth: 0` 而不是浅检出；超时 3 min 是预算，本机实测脚本本身约 0.25 s（接入时 385 份 Markdown、1427 条 `路径:行号` 文本）。它没有范围过滤，和 `routing`、`docs_matrix` 一样每个 PR 都执行：源码 PR 删改被引用文件同样会让终态文档的引用失效，按范围过滤就会恰好在最需要它的地方跳过。`Gate: required checks for this change` 要求该作业 success，不按范围映射——`docs` 与全量范围都执行，失败、取消或跳过都使门禁失败。

### 实测反例

四段是 2026-11-16 接入 CI 时的快照，都在本机以 Python 3.12（CI runner 的系统 Python 版本）执行，命令与 CI 相同：第一段是基线树；第二、三段分别往终态文档与过程记录里注入坏引用；第四段把终态文档搬到 `docs/reviews/` 的合规文件名下。计数随 `docs/` 内容变化，包括本节自身的改动，以实际运行输出为准；基线已于 2026-11-17 刷新（见上节刷新区块），第三段的 `RECORDS` 增量因此不再与当前基线逐字一致：

```text
$ python3 scripts/dev-env/check-doc-refs.py --baseline scripts/dev-env/doc-refs-baseline.json
SUMMARY refs=1308 fenced=121 fenced_terminal=4 AMBIGUOUS=89 EXTERNAL=1 MISSING=23 OK=1194 OUT_OF_RANGE=1
SCOPE terminal problems=0 new=0 accepted=0
SCOPE record problems=113 docs/reviews AMBIGUOUS=88 MISSING=23 OUT_OF_RANGE=1; docs/validation AMBIGUOUS=1
RESULT PASS                                    # exit=0

$ # 终态文档注入一条坏引用：docs/architecture/speech-api.md 末尾
[MISSING] docs/architecture/speech-api.md:616  ->  backend/src/speechnexus_backend/does_not_exist.py:7  (仓库内不存在该文件)  [新增,基线外]
SUMMARY refs=1309 fenced=121 fenced_terminal=4 AMBIGUOUS=89 EXTERNAL=1 MISSING=24 OK=1194 OUT_OF_RANGE=1
SCOPE terminal problems=1 new=1 accepted=0
RESULT FAIL                                    # exit=1

$ # docs/reviews/ 新增一份审查报告（标题写审查、开头带 #859），含一条坏引用
SUMMARY refs=1309 fenced=121 fenced_terminal=4 AMBIGUOUS=89 EXTERNAL=1 MISSING=24 OK=1194 OUT_OF_RANGE=1
SCOPE record problems=114 docs/reviews AMBIGUOUS=88 MISSING=24 OUT_OF_RANGE=1; docs/validation AMBIGUOUS=1
RECORDS docs/reviews MISSING 23->24 (+1)
RESULT PASS                                    # exit=0，计数 +1

$ # 把终态文档搬进 docs/reviews/ 的合规文件名：cp speech-api.md …/2026-11-16-speech-api-duplicate.md
LAYOUT docs/reviews/2026-11-16-speech-api-duplicate.md  内容不是审查报告（一级标题须含审查/复核/核对，开头 400 字符须含 #<issue 号>）；过程记录不参与终态门禁，所以 docs/reviews/ 只放审查报告，终态文档请移到 docs/ 的对应目录
SCOPE terminal problems=0 new=0 accepted=0
RESULT FAIL                                    # exit=1
```

第三段是分目录统计的作用：过程记录增长不阻塞合并，但增长量留在日志与基线增量里。第四段是布局守卫的作用：光有合规文件名不够，豁免不能被复制路径拿走。

同一份内容也已在 CI 上跑过两侧（2026-11-16，本门禁建立时的两次运行）：

```text
# 基线树：运行 43749226457，作业 `Docs: path:line references` success（9s）
# 反例提交（终态文档追加同一条坏引用）：运行 43748926560，同一作业 fail（12s）
[MISSING] docs/architecture/speech-api.md:616  ->  backend/src/speechnexus_backend/does_not_exist.py:7  (仓库内不存在该文件)  [新增,基线外]
SCOPE terminal problems=1 new=1 accepted=0
RESULT FAIL
##[error]Process completed with exit code 1.
# 同一次运行：`Gate: required checks for this change` fail，日志里的归因是 routing=failure（见下）
```

### 覆盖边界

- 只判「路径存在、行号不越界、裸名不歧义」与「断言片段在目标行 ±15 行内」，不证明指针的语义正确：片段可以在窗口内另一行命中，也可能被挑成一句稳定但无关的原文。`--show` 只打印问题项及其被引用行原文，需要连 OK 引用一起核对时用 `--show-ok`；两者都不证明语义正确。
- 断言只钉「片段仍在这一行附近」，不判断言：它证明不了被引用文件里文档所说的符号、断言或行为还在。保留被引用文件、只删掉一条断言而后续内容仍落在原行号窗口内，不会变红；这类漂移靠人读或语义门禁补位（`check-doc-statements.py` 只钉住少数已经漂移过的陈述）。来源：#935 的审查。
- `AMBIGUOUS` 是写法不足以定位，不是错误路径；判据不解引用符号，也不看被引用文件是否还承担文档所说的职责（行号合法但指向错符号的漂移对它隐形）。
- 围栏内的 `路径:行号` 完全不可见，包括把真实引用写进围栏的情况；终态文档这部分计入 `fenced_terminal=`（当前 4 条，全部来自本节证据块），把围栏排除收窄到只对过程记录生效是未做的后续项——证据块自身要在围栏里贴失败输出。
- 只扫 `docs/` 与 `deploy/` 下的 Markdown（默认根）：仓库根目录的 `README`／`AGENTS.md`、`.agents/` 下文档、代码注释里的引用不在范围内（`--path` 可覆盖或扩大）。
- 只认脚本列出的扩展名（`.py/.sql/.ts/.tsx/.js/.mjs/.sh/.toml/.yaml/.yml/.json/.md`），无扩展名路径、`#L12` 形式与普通 Markdown 链接不在判据内；目标必须在 git 索引中，未跟踪文件不算存在。
- 过程记录的计数不是门禁：增长不阻塞合并，这是「保留原时点事实」的取舍，不是遗漏。

调度证据等级：**已实测**（2026-11-16 的两次运行，见上）。**限定**：反例提交同时让 `routing` 里的 `tests/ci` 用例（「检出必须与提交的基线一致」）变红，门禁按所见先报 `routing=failure` 后退出。所以那次运行里**直接因本门禁而红的只有 `Docs: path:line references` 作业本身**；`Gate` 的红是同一次运行里另一个作业的红，其日志归因是 `routing`，不能当作「门禁的 `docs_refs` 分支被观测到」的证据。`DOCS_REFS_RESULT != success` 这一分支由 `tests/contract/test_ci_workflow.py` 与 `tests/ci/test_doc_refs_ratchet.py` 的真值表覆盖。

## 未发布变更清单门禁

`docs_refs` 作业的第二步执行 [check-unreleased-notes.py](../../scripts/dev-env/check-unreleased-notes.py)：
以 `git describe --tags --abbrev=0` 取最近发布标签，断言 `docs/releases/unreleased.md` 里
有自该标签以来**新增或修改的每个迁移文件**（名字去掉 `versions/` 前缀与 `.py`）与
**每个被改动文件里的每个 `*_grants` 函数**。这条门禁是「照 `v0.1.0` 部署的人换镜像后会
在无提示的情况下踩到权限退化」的反向约束：清单漏一条，合并就红。

判据是「一行归属」而不是「出现过」：名字必须以表格行首列或列表项开头（允许前置的 `|`、`-`、
`>`、反引号与强调符号），正文里顺带提一次不算，**围栏内的行整段跳过**——清单会引用命令行与
期望输出，迁移名出现在 `text` 块里是最自然的假绿来源。否则删掉运维真正会读的那一行，检查仍会
因别处一句引用而变绿。失败时先打印一行写法要求（首列或列表项、句子中间与围栏内不算），再逐个
点名缺什么，所以只看日志就能改对。删除的迁移不计：它没有要运维执行的动作。空集通过，标签之后
没有任何迁移与授权变动时不要求清单新增条目。

授权定义文件按内容判定：产品源码（`src` 下的 Python）中定义了 `<name>_grants(` 的文件都在
范围内，所以 `job_cancellation_fence_schema.py` 这类 schema 模块与名字以 `_grants` 结尾的
模块同等对待；同一文件里的函数全部要求点名，哪个函数真的变了由清单自己标注。

全历史是前提：检出用 `fetch-depth: 0` 并显式 `fetch-tags: true`。浅检出解析不到标签时脚本
以 2 退出并说明原因，不会把「没有变化」当成通过。放在已存在的 `docs_refs` 作业里而不是新开
作业，是因为它同样要求每个 PR 都执行，而该作业已经为一条文档门禁检出仓库；受保护检查的
作业集合与结果映射因此不变。

覆盖边界：只做名字存在性断言。它不判断清单里的命令能否执行、签名与授权列是否仍与代码一致，
也不发现「授权函数没变、但它引用的列元组在另一个模块里被改宽」——那种模块不定义
`*_grants` 函数，不在判据内。清单本身的正确性仍要人读，门禁只防漏项。

实测（2026-11-20，本机）：当前 `main` 相对 `v0.1.0` 的 6 个迁移与 2 个文件里的 9 个函数全部
被点名时 `RESULT OK`；新增一条未登记的迁移文件、删掉 `0063_deletion_targets_blocked` 的
清单行、或把那行删掉只把它留在一个 `text` 代码块里，均为 `RESULT FAIL 1 problem(s)`；最近标签
之后没有迁移与授权变化的临时仓库为 `RESULT OK`（`empty set passes`）。`tests/ci/test_unreleased_notes_gate.py`
用临时 git 仓库钉住这四条，另加「无标签退出 2」与「删除的迁移不计」。

## 公开语音路由一致性门禁

`python_static` 作业（`Python: lint, types and contract compatibility`）在契约兼容步骤之后、Ruff/mypy 之前执行
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

`routing` 作业用 `.nvmrc` 固定的 Node 执行独立回归与全 Web 静态扫描，不安装 pnpm 依赖；适用于所有路由，失败会阻断最终必需检查。Web 作业的 `pnpm lint` 继续执行相同校验器。

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

## 生成契约消费者绑定门禁

`packages/contracts` 是生成契约包；Web 入口经 `web/<app>/package.json` 的 `"@speechnexus/contracts": "workspace:*"` 依赖与包内 `exports` 子路径消费它，不得以相对路径进入 `packages/contracts/src/**`。同一 `routing` 作业在任何路由上运行独立回归与全仓扫描，不安装 pnpm 依赖；规则细节与例外登记见[生成契约包说明](../../packages/contracts/README.md#typescript-consumer-binding)。

| 规则 | 检查 |
| --- | --- |
| 产物可解析 | `exports` 每个目标必须是包内已存在文件；生成产物改名或移动而 `exports` 未同步时失败。嵌套条件对象缺少字符串 `import`/`types`/`default` 时给出可读错误，不以 `TypeError` 崩溃 |
| Web 绑定 | `web/**` 内不得以相对路径导入 `packages/contracts/src/**`（按打包器的扩展名/`index` 规则解析并以 `realpath` 规范化，`web/` 内的符号链接不能绕过）；导入必须使用包清单子路径；`web/` 内的动态 `import()`/`require()` 必须带字面量说明符，非字面量或模板字面量失败 |
| 依赖必需 | 每个 `@speechnexus/contracts[/…]` 导入方的最近包清单必须声明 `"workspace:*"`；删除依赖或改用版本区间即失败 |
| 两端对齐 | 每个声明子路径必须**机械可判**：①有经包清单的消费者；或②由子路径推导出的生成模块（`packages/contracts/src/speechnexus_contracts/<module>.py`）存在，且仓库内至少一个 `.py` 真的 `import` 该模块（用 Python `tokenize` 判定，注释/文档串/字符串不算）；或③无消费者的生成候选——推导模块存在，且 `contracts/bindings.json` 存在名称规范化后等于子路径（`./zero-dispatch` → `zero_dispatch`）、`client: false`、`source` 文件存在的 profile。非 Python 子路径（`schemas/*.json`）没有候选通道，必须有消费者。不存在 `reason`/`basis`/`decision` 自由文本或登记文件：新增无归属子路径、复制其它候选的保留块、profile 名称或 `client` 不符、profile source 缺失、生成模块缺失都失败 |

**消费者口径**：出现一个经包清单的 import（静态导入、`import type`、副作用导入与字面量 `require()`）即计为消费者；守卫既不校验该绑定是否被使用，也不证明该导入在运行时会执行。注释、字符串与模板正文中的 import 文本不计入消费者。Python 侧同口径：仓库内任一 `.py` 的真实 `import` 语句即算（含测试），由 Python `tokenize` 判定。已知边界：`web` 经另一个非消费者模块再相对导入生成包的转发链不在检查范围。

独立验证（均无需安装依赖）：

```sh
node --test tests/ci/contract-consumers.test.mjs
node scripts/dev-env/validate-contract-consumers.mjs
```

守卫是静态文本检查：它证明导入形态、清单依赖、生成模块/候选归属与 `exports` 声明对齐，不证明运行时解析、打包产物体积、Vite/tsc 版本行为或实际页面调用成功；这些仍由 `pnpm typecheck`、`pnpm build`、`pnpm test` 与真实浏览器能力道覆盖。仓库内 `scripts/`、`tests/` 的探针与夹具在检出上直接运行源码，不是 workspace 包消费者，因此不在 Web 绑定规则内；它们使用相对路径不进入本门禁的消费者计数。

## Web 镜像构建上下文门禁

`deploy/images/web.Dockerfile` 只复制仓库的一部分再在镜像内 `pnpm install --frozen-lockfile` 与 `pnpm --filter "@speechnexus/${APP_NAME}" build`。Web 作业用整仓执行同样的命令，因此 **COPY 集合比真实构建输入窄**这类缺口在 CI 里不可见：#834 让产品源码（`web/admin/src/profiles/profile-test-fixtures.ts` 与 dev 场景页）导入 `tests/fixtures/contracts`，两个 Web 镜像都无法构建，而所有车道全绿——「CI 绿 ≠ 部署能起」。

`web` 作业在 `pnpm build` 之后执行 `pnpm verify:web-image`：它按 Dockerfile 的字面量 `COPY` 集合物化一个只含这些文件的目录，在其中安装锁定依赖并构建每个声明 `build` 的 workspace 应用。构建读到集合外的任何文件（测试夹具、文档、根配置）都会在这里失败，不需要整仓兜底。`changes` 作业（不安装依赖）用 `tests/ci/test_web_image_context.py` 复核静态部分。

| 检查 | 判据 |
| --- | --- |
| 上下文完整性 | 只含 `COPY` 集合的目录内 `pnpm install --frozen-lockfile` 与 `pnpm --filter @speechnexus/<app> build` 全部成功；集合外缺失的输入直接点名 |
| `.dockerignore` | 每个 `COPY` 源都不被 `.dockerignore` 排除；被排除的源在真实构建里复制不到任何东西 |
| 根配置输入 | `package.json`、`pnpm-lock.yaml`、`pnpm-workspace.yaml`、`tsconfig.base.json`、`.npmrc` 必须在 `COPY` 集合内，否则镜像内的安装与类型检查策略与 CI 不同 |
| 无测试数据 | `COPY` 源不得落在 `tests/**`；镜像不包含测试数据是验收判据，不以「复制集合够小」代替 |
| 生产程序 | 每个 Web 应用的 `build` 必须类型检查 `tsconfig.build.json`，且该程序内没有源文件导入 `tests/**`。测试、`src/test/`、`src/**/dev/` 与测试专用夹具模块不在该程序内，dev 场景页经 `import.meta.env.DEV` 门控的 Vite glob 加载，因此不进入镜像构建图 |
| `APP_NAME` | `ARG APP_NAME` 的默认值必须是有 `build` 脚本的 workspace 包 |

独立验证（`--build` 需要 `pnpm` 与已填充的 store；静态部分只需 Python）：

```sh
python3 scripts/dev-env/verify-web-image-context.py
python3 scripts/dev-env/verify-web-image-context.py --build
python3 -m unittest tests.ci.test_web_image_context -v
```

边界：本门禁在 CI 里不运行 `docker build`，因此不证明 Docker 引擎、BuildKit secret 挂载、`.dockerignore` 的完整语义或镜像最终层内容；`.dockerignore` 匹配按「最后一条匹配规则胜出」实现，是 Docker 语义的子集（真实 BuildKit 对 15 条路径的对照一致，见 `docs/reviews/2026-11-15-web-image-build.md`）。它证明的是**构建输入集合**：集合外的依赖会让镜像构建失败，而这一点此前没有任何车道能看见。运行期行为、nginx 模板与镜像内容仍由实际镜像构建与部署验收覆盖。

静态与动态两半分的能力不同：静态部分（`tests/ci/test_web_image_context.py`，跑在 `changes` 作业）判断 `COPY` 源是否落在被排除路径、根配置输入是否齐备、以及源文件里是否有**相对**路径或 `tests/**` 的可达导入。它**不解析 bare specifier**，因此删掉 `COPY packages/contracts` 时静态部分仍通过，只有 `--build` 部分会报 `TS2307 '@speechnexus/contracts/admin'`。这是可接受的取舍而非假绿：改动 `COPY` 集合需要改 `deploy/`，按 [CI 路由](ci-validation.md#路由与范围) 走 `full` 车道，动态部分必然执行。

## Web 部署安全头首屏门禁

`web`／全量路由在 `pnpm build` 之后、预览类浏览器门禁之外运行 `pnpm test:deployed`。它服务的不是 `vite preview`：用已构建的 `web/<app>/dist` 与 `deploy/web/nginx.conf` 里**实际发送的响应头**起两个静态服务，再用真实 Chromium 打开首屏。原因是预览门禁看不见部署策略：`vite preview` 不带 `Content-Security-Policy`，镜像却发 `script-src 'self'`。#840 中两个入口在镜像里白屏——首个渲染路径上的契约校验器在运行期用 `Function(...)` 从源码文本构造校验函数（Ajv 源码里写作 `new Function`，产物里被压缩成 `Function(...)`），浏览器按 eval 处理并抛 `EvalError`，页面正文为空、无标题；同一份 `dist` 在无 CSP 服务下完整渲染，所以无障碍与行为门禁全绿而缺陷只出现在部署里。

| 覆盖 | 判据 |
| --- | --- |
| 真实策略 | 响应头 `Content-Security-Policy` 必须逐字等于 `deploy/web/nginx.conf` 解析出的值；页面不得依赖运行期动态求值 |
| 首屏渲染 | 两个入口分别断言 `document.body.innerText` 非空、一级标题可见 |
| 无静默失败 | 断言 0 个 `pageerror` 与 0 个 console error；CSP 拒绝会同时产生两者，不会沉默通过 |
| 策略一致性 | `nginx.conf` 中每处 `Content-Security-Policy` `add_header` 必须存在（至少 4 处）且取值一致；少一处或取值不同即失败，避免只改部分 location 时门禁按其中一处假绿 |
| 不削弱策略 | 解析出的策略必须仍含 `default-src 'self'` 与 `script-src 'self'`，且不得含 `'unsafe-eval'`：把 CSP 放宽来让页面通过会直接失败 |
| 端口隔离 | 静态服务绑定 `127.0.0.1:4280`（admin）与 `:4281`（workbench），与预览门禁的 4173/4174 不同，可与它们同作业顺序执行而不争端口 |

不依赖 Docker：CSP 是 `nginx.conf` 的纯静态字面量（不含 `${…}`），服务端按 `location /` 的 `add_header` 集合回放，SPA 回退与 `/assets/` 的 404 语义按 `try_files` 复刻，`/v1`、`/v2` 返回可被客户端解析的 503 拒绝。门禁自证覆盖策略解析的四条反例（location 间不一致、少一处声明、含 `'unsafe-eval'`、缺 `location /`），并在改动前的构建上验证首屏断言会红。

边界：这不是「在真镜像里跑过浏览器」。nginx 的 `always` 语义、`location` 内 `add_header` 的继承与覆盖规则、重复响应头处理、`envsubst` 对代理变量的替换、真实上游代理与 TLS 均未覆盖；这些仍需实际镜像构建与容器运行按需检查。门禁证明的是：在**部署配置声明的策略**下，两个入口的首屏能渲染且不产生页面错误。

本机复现（需先 `pnpm build`）：

```sh
pnpm install --frozen-lockfile
pnpm build
pnpm exec playwright install --only-shell chromium
pnpm test:deployed
```

策略解析的反例（location 间不一致、少一处声明、含 `'unsafe-eval'`、缺 `location /`）与首屏断言在同一个规格内，`pnpm test:deployed` 一并执行。

预编译校验器自身的两条守护不在本门禁里：`packages/contracts/tests/formats.test.mjs` 用非法 `date-time` 断言生成的校验器仍返回 `false`，`make contracts-drift`（CI 的 `python_static` 车道）在生成期拒绝未在 `ajv-formats` 注册的 `format`。体积回归（admin +72%、workbench +75% gzip，首屏单个 2.5／3.3 MB 原始脚本）与拆分的真实成本记录在[契约包 README 的跟踪项](../../packages/contracts/README.md#bundle-size-of-the-precompiled-validators)。

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
