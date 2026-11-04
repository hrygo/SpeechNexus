---
title: CI 验证范围与维护
status: active
---

# CI 验证范围与维护

CI 使用[工作流](../../.github/workflows/ci.yml)和[标准库路由](../../scripts/dev-env/ci-routing.py)选择验证范围。未知路径保守运行全量，不维护易遗漏的业务测试白名单。

| 触发或变更 | 范围 |
| --- | --- |
| PR 仅修改根 `README.md` / `README.en.md` / `AGENTS.md`、`docs/` 内 Markdown、`.agents/skills/<skill>/` 内 Markdown / JSON / YAML | 路由回归、Design Token 静态门禁、差异检查、Node Controller 镜像范围检查与其门禁、最终门禁；镜像构建按独立范围判定，通常跳过。不安装项目依赖、不启动业务测试服务。差异检查以取得准确基线为前提；否则回退全量 |
| PR 仅修改已知 Web 源码/静态资产，可混合上述文档 | Python 静态/单元及完整 Web 检查；双数据库与部署检查不适用 |
| PR 仅修改已知独立引擎目录或明确列出的引擎单元测试，可混合上述 Web/文档 | `no-db`：保留 Python 静态/契约/覆盖率、完整 Web、构建和部署检查；六个数据库分片及其汇总不适用 |
| CI、脚本、测试、依赖、配置、契约、迁移、部署、其他路径或空/不确定差异 | 完整 Python/Web/部署检查，六个 PG/InnoDB/MinIO 分片及证据汇总 |
| `main` push、手动运行及未知事件 | 始终全量 |

精确白名单以路由脚本为准。新增应用、资源类型或目录默认全量；缩窄范围前先证明其不依赖被省略的验证，并添加回归。

Skill 的说明、参考、合成评测数据及展示元数据不参与产品构建，复用文档轻量路径。技能内的 Python、Shell、JavaScript 等脚本和未知文件仍走全量；混合业务代码时按业务范围选择，其他 `.agents/` 配置不在白名单内。轻量路径不执行 skill 行为评测，也不证明 JSON/YAML 内容或引用有效，维护者须按变更核对。`main` push 仍按既有约定全量验证。

独立引擎目录白名单为 `deploy/engines/` 下的 `cpu-vad`、`file-asr-audio`、`vllm-asr-lifecycle`；测试白名单为 `test_native_asr_scope.py`、`test_worker_fence.py`（均在 `tests/unit/gateway/`）。集成测试会导入其他部分单元测试工具，因此不按整个 `tests/unit/` 跳过数据库。后端/网关业务实现、共享测试工具、契约、迁移、共享依赖和 CI 配置仍触发全量；任意混合变更取更严格的范围。主干 push 与手动运行继续全量。引擎若接入数据库集成测试，须同步收紧路由并补回归。

## 必需检查与证据

三个受保护检查为 `Gate: required checks for this change`、`Web: lint, types, tests, build and accessibility`、`Compose: configuration validation`。它们分别替代原 `Python quality and tests`、`Web quality, tests, and build`、`Deployment configuration`；变更显示名称时必须同步 `main` 的 required status checks，并保持 GitHub Actions app 绑定及其他保护策略。迁移时先确认新名称在当前提交实际通过，再替换保护列表，不将缺失检查视为通过。

作业级条件让不适用项明确显示 skipped；最终 CI 门禁用 `always()` 执行，核对路由成功和各项实际结果。非法/缺失路由、失败、取消或本应执行却跳过均失败。不适用不表示业务验证已通过。

Node Controller 镜像验证由 CI 以同提交的 [reusable workflow](../../.github/workflows/node-controller-image.yml) 调用，不再单独订阅 PR、push 或手动事件。PR 根据准确 base SHA 与 checkout 的差异选择；控制器源码、镜像文件、构建上下文忽略规则、根锁文件和 workspace 元数据，以及镜像路由或门禁文件变更触发镜像验证。使用 NUL 分隔并关闭重命名检测，确保移出受监控目录的旧路径也被检查；空差异保守执行，base 无效或不可获取时失败。`main` push、手动及未知事件始终执行。fork PR 仍使用调用工作流的事件与 checkout，公开仓库取 base，不传私有凭据。

镜像 workflow 在 amd64 和 arm64 的 GitHub 托管 runner 分别以空缓存构建并运行镜像、哈希拒绝和合成 CA 检查。其内部 `always()` 门禁要求 scope 成功且输出只能为 `true` 或 `false`：前者要求整个矩阵成功，后者要求矩阵 skipped。调用方的既有 `Gate: required checks for this change` 再要求该 workflow 成功并提供合法输出；缺失、失败、取消或不适用范围以外的跳过都不能放行。镜像验证不新增分支保护状态名，也不证明真实部署 CA 或物理节点已通过验证。

显示名称区分执行、证据验收和最终合并门禁；内部 job ID、依赖、命令、范围与超时不随改名改变：

| job ID | Actions 显示名称 | 实际职责 |
| --- | --- | --- |
| `changes` | `Scope routing and CI / design-token checks` | 检查路由边界与 Design Token，按准确差异选择范围 |
| `python_checks` | `Python: tests, coverage and package build` | Python 单元、契约、CI、Workbench PG/Runtime 及选定 Speech PG 测试，合并覆盖率、媒体工具链及 Python 包构建 |
| `python_contracts` | `Python: lint, types and contract compatibility` | Ruff、mypy、模块边界、生成漂移、协议兼容与 schema 校验 |
| `integration_shards` | `Integration 0–3: PG/MySQL/MinIO`；`Integration 4–5: Web + PG/MySQL/MinIO` | 六个真实隔离服务分片；混合集合中的浏览器与 Node HTTP 消费者分配到后两片，具体工具需求按各片实际用例计算 |
| `coordination` | `Gate: six-shard integration evidence` | 验收六片成功及同提交、完整互斥集合、实际执行证据，不在此节点重新执行测试 |
| `python_checks_gate` | `Gate: Python execution and static checks` | 要求 Python 执行与静态/契约两个作业均成功，不执行测试 |
| `node_controller_image` | `Node Controller image evidence` | 调用镜像 scope、双架构构建与内部证据门禁，按输入差异决定是否构建 |
| `python` | `Gate: required checks for this change` | 按已验证范围核对全部适用结果，阻止失败、取消与意外跳过 |
| `web` | `Web: lint, types, tests, build and accessibility` | 格式、lint、TypeScript、SDK 契约、workspace 测试和构建、真实浏览器无障碍与对比度 |
| `deployment` | `Compose: configuration validation` | 执行 `docker compose config --quiet`，不启动服务或发布部署 |

完整范围仍要求原有覆盖率、契约漂移/兼容检查及六个真实双数据库分片。集成汇总继续核对同一提交、完整收集、互斥并集和实际执行结果，不以数量或仅收集代替证据。唯一允许的跳过是 `tests/integration/test_gateway_submit_ledger.py` 中以 `[mysql]` 结尾的既有 PostgreSQL 专属产品适配器参数；这不构成 MySQL 协调路径通过的证据，其他必需用例跳过会使汇总失败。

Python 测试作业通过 `python-coverage.py run --workers 2` 在同一 runner 内并发两个 `workbench-pg-probe.py --full-coverage` 进程，运行配置的非集成测试目录、脚本显式列出的 Workbench PG／Runtime 文件，以及发布证据、绑定生命周期和停用意图三个 Speech PG 测试文件。Workbench 文件按 `WORKBENCH_PG_TESTS` 和 `WORKBENCH_RUNTIME_TESTS` 清单选择，新增文件不会自动进入此作业；标记为 `integration` 的用例由独立分片收集和执行；未带该标记的用例不因此自动进入分片，真实模型资格测试见下文。各进程独占临时 PG、Unix socket 与 coverage 文件；Speech PG 测试在 full coverage 模式使用同一 worker 的私有 socket，不连接 runner 的系统数据库。集成分片仍使用显式配置的双库地址。作业仍只准备一次依赖、Linux inspection 环境及隔离 MinIO。父进程先准备四个 fixture 共用的 `speechnexus-workbench-saved` 测试桶，避免两个进程首次创建竞态；其对象按随机 project 前缀隔离，其余桶按文件独占或随机命名。测试按完整收集的文件分组，文件内用例不拆开，较大文件先分到当前用例数较少的一组；这是确定性的数量均衡，不是耗时均衡。

Python 测试作业在临时 GitHub 托管 VM 安装 `bubblewrap`，通过 [CI 专用脚本](../../scripts/dev-env/ci-inspection-profile.py)核对固定官方包与规则摘要后加载两条 AppArmor 规则，并先运行真实隔离启动探针。脚本校验仓库、事件、作业、提交及 runner 身份，拒绝覆盖既有规则，每个作业最多尝试加载一次。清理只移除确认由本次加载的规则；加载结果、规则身份或新增同 UID 进程停止状态不确定时保留策略直至 VM 销毁并使作业失败，不重试或全局重载。工作流尝试上传规则身份和清理记录，保留 7 天；作业中断或制品上传失败时不保证留存。此配置仅适用于该托管作业，不能作为其他主机或生产部署已具备隔离条件的证据。

每片保留原完整覆盖源与 branch 配置，只在片内使用 `--cov-fail-under=0` 采集局部数据。父进程必须确认两片执行成功、私有 PG 已停止、同一 SHA/配置、相同完整 collection、精确分配且互斥并集覆盖全集、每个结果实际通过及覆盖数据摘要一致，再显式组合全部分片并按 `pyproject.toml` 原全项目 **80%** 门槛执行 `coverage report`。缺片、失败、跳过、收集限定或仅有局部高覆盖率均不能放行；入口拒绝任何非空 `PYTEST_ADDOPTS`，即使其选项不缩小集合。这里的完整集合以 pytest 收集时各 worker 观察到的结果为准；同一插件或配置在两片共同遗漏用例时，同集与分配校验不能独立发现，仍须审查测试根目录、pytest 配置和插件变更。执行证据校验通过后，复制各片原始覆盖数据并生成组合产物，以 `workbench-pg-full-coverage` 上传；覆盖率不足仍保留这些产物并失败。若覆盖率步骤已运行且失败、作业未取消，工作流尝试上传已生成的日志及分片／生命周期／汇总报告；步骤未运行、作业取消或上传失败时不能保证留存。测试或执行证据校验失败不会生成合格的组合产物。直接使用 `scripts/dev-env/workbench-pg-probe.py` 的 `--full-coverage` 模式仍是单次 pytest 的全项目 80%；`--coverage` 仍是独立的 C 层 90% 检查。两种模式都须安装锁定依赖并提供 `--pg-bin`，运行前置条件见[验证指南](../agent-verification.md)，具体参数可用 `uv run python scripts/dev-env/workbench-pg-probe.py --help` 查看。

集成分片在安装 Python 依赖后，通过 `uv run python scripts/dev-env/integration-shards.py requirements --index 0 --count 6` 等命令实际收集完整 integration nodeid（包括参数化身份），以执行时相同的资源分组与 SHA-256 分片算法输出对应分片的 `node_contracts` 与 `browser` 需求；工作流分别传入各分片的 index。`*_browser.py` 与两个合成浏览器探针模块声明浏览器依赖；回归核对当前所有 Node 浏览器探针消费者均被分类。收集错误或设置了非空 `PYTEST_ADDOPTS` 均会失败，即使该选项本身不缩小选择，也不能输出“无需工具”放行。该步骤只收集，实际执行和六份报告的互斥并集核验仍独立必需。预检记录 pytest 最终收集结果，未单独校验所有插件对收集的影响；汇总比较各片报告的相同全集，也不能独立发现所有分片共同遗漏的用例。须保持受控依赖与 pytest 配置，并审查收集范围的变更。

完整集合同时含普通用例与 Web 工具消费者时，普通用例分到前四片，浏览器及显式 Node 契约消费者分到后两片，各组内继续按 nodeid 的 SHA-256 取模；一类用例单独存在时使用全部分片。通用入口仍接受 1–16 片，混合类别为 Web 工具保留 `max(1, count // 3)` 片；单片执行整个集合。预检、执行和报告检查使用同一个 `partition`，工具需求仍根据实际选中用例计算，不能在工作流里按编号跳过工具或业务测试。该分组依据工具成本，不宣称按用例耗时均衡。

有 `browser` 需求的分片安装完整锁定 Web 依赖、构建两个实际入口，并安装相同版本 Chromium headless shell 及系统依赖后运行测试；只有 Node 契约消费者的分片保留锁定生产依赖安装；数据库分片不安装浏览器。需求随真实 nodeid 分区计算，不在 workflow 中固定某几个分片号。汇总 `check` 子命令与 `tests/ci` 轻量路由回归继续仅依赖 Python 标准库。

浏览器准备复用[独立脚本](../../scripts/dev-env/prepare-ci-browser.py)：先从锁定 Playwright CLI 的依赖模拟结果取得缺失/升级包，再向 APT 请求 URI/大小/SHA256 清单。仅在这条清单命令中选择 SHA256 输出，避免 `--print-uris` 的历史 MD5 默认值；不改系统或实际安装配置。归档通过 APT 自带的 `apt-helper download-file` 最多四个并发下载，同时传入强摘要和 `Checksum-FileSize`，让原生获取阶段约束已认证大小；不新增下载器依赖或覆盖传输政策。写入完整 APT 缓存前仍独立核对 SHA256/SHA512 与大小，MD5-only、错误大小、错误强摘要及不匹配的已有缓存均拒绝。不覆盖未知缓存内容。

随后仍执行原 `playwright install --with-deps --only-shell chromium`。信任链依赖 APT 认证的索引与上述写前强校验，不能假定安装器会重新校验完整缓存：APT 2.8 的[现有归档路径](https://github.com/Debian/apt/blob/2.8.1/apt-pkg/acquire-item.cc#L3494-L3507)可能只按大小复用。未知或无法得到强摘要的计划直接失败，不降级到弱摘要或强制信任。

预取不更改软件源、APT/网络安全配置或安装器。仅对 HTTP/HTTPS 的 `Timeout`、`Pipeline-Depth`、`No-Cache`，直接 HTTP 的 `User-Agent-Non-Interactive` 及无相关控制项的情形开放；HTTPS 同名项、代理、自定义 User-Agent、地址族、速率/队列/hash、`apt-get` 或 `apt-helper` 的 Acquire 专用配置、替代配置、HTTP/HTTPS 传输命名空间中的未知或嵌套键以及不可读取配置，触发原安装器路径。该检查不是对所有 APT 配置项的穷举审计。APT 2.8.1 的[原生实现](https://github.com/Debian/apt/blob/2.8.1/methods/http.cc)按既有配置与 systemd unit 语境选择非交互标记；apt-helper 使用同一 acquire HTTP method，不改写配置值、header 或上下文。

预取策略诊断输出固定分类、退出码与错误类型；对直接字段仅允许 1–64 字符、字母开头且只含 ASCII 字母/数字/下划线/连字符的字段名。空字段另分类，其他文本只给分类/数量；该诊断不输出配置值、代理凭据、嵌套主机名、URL 或原始 stderr。原生 APT 下载与安装子进程沿用其自身标准输出和错误输出，不能把上述过滤保证扩展到它们的日志。识别出字段名不自动授予预取资格，必须先核对原生语义。未知计划、下载或校验失败会阻止安装，临时预取目录自动清理。真实 runner 的预取行为及收益须以当次运行证据验证。

分片验收应区分取消、未开始测试与断言失败；回退脚本接线可恢复原官方安装路径。

每个集成分片还检查 PostgreSQL 16 工具，通过 `workbench-pg-probe.py --integration-shard` 启动独立的私有 Unix socket PG，再把 Workbench DSN 传给原 `integration-shards.py run`。双数据库与 MinIO 环境仍指向该作业既有隔离测试服务，不用私有 Workbench DSN 替换协调数据库地址。原分片报告路径、完整 collection、实际执行与意外跳过拒绝规则保持不变；驱动器的生命周期报告与分片报告一并上传，分别保存，不替代分片执行报告。仅有单个分片作业成功不能替代汇总验收。

真实内网模型资格与可复现集成分别验收。`qualification` 标记的三条纪要用例调用配置的真实模型，不进入普通 `integration` 分片，也不计作 CI 通过；它们使用测试装配的工作台运行时与隔离数据库，不能单独证明目标生产部署或替代协调事务的确定性门禁。`make workbench-llm-qualification` 要求显式的私有 Workbench PG DSN、获准内网 LLM endpoint/model，并构建真实 Web 入口后选择这三项；缺少上述配置时入口直接失败。该命令没有禁止 `PYTEST_ADDOPTS` 等外部选项缩小选择，验收时须确认三项均实际通过，不能将跳过或部分通过当成资格证据。普通 CI 的分片完整性仍拒绝任何必需用例跳过，阶段 AC 仍须另附这三项真实模型与版本证据。

不在整个 workflow 的 `on` 中设置 `paths` / `paths-ignore`：被整体过滤的必需检查可能长期 Pending。[GitHub 路径过滤说明](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax#onpushpull_requestpull_request_targetpathspaths-ignore)、[作业条件与 skipped 语义](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/control-jobs-with-conditions)是维护入口；核验日期为 2026-10-25。

汇总使用 runner 自带 GitHub CLI 和标准库脚本 `download-integration-evidence.py`：只列当前仓库/运行，最多一页 100 个制品且拒绝截断，只接受精确的六个分片名称；元数据响应上限为 1 MiB，制品最多三个并发下载进程。每次请求限 15 秒，进程失败或超时最多尝试三次，每次使用独立临时文件，完整性校验失败不放行；令牌仅经环境传入。先核对未过期、所属运行、压缩大小与 API 提供的 SHA-256，再只读 ZIP 内该片的两份固定 JSON，不提取任意路径；单 ZIP 接受上限 16 MiB，单 JSON 保持原集成报告的 8 MiB 上限；下载先写临时文件，进程退出后才检查实际大小，因此这些上限不是传输期间的磁盘写入硬限制。输出目录必须事先不存在，全部下载及归档校验成功后才开始发布报告；既有同提交、完整互斥集合与逐项执行检查继续独立执行。仅汇总作业有 `actions: read`，全局仍仅 `contents: read`，超时为两分钟；不以 CLI 下载成功替代业务证据验收。依据：[官方 artifact REST API](https://docs.github.com/en/rest/actions/artifacts)、[GitHub CLI API](https://cli.github.com/manual/gh_api)，核验日期 2026-10-27。

历史性能分析和运行故障记录见[归档](../archive/validation/2026-10-ci-validation-history.md)。

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
