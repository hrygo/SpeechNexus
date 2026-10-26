---
title: CI 验证范围与维护
status: active
---

# CI 验证范围与维护

CI 使用[工作流](../../.github/workflows/ci.yml)和[标准库路由](../../scripts/dev-env/ci-routing.py)选择验证范围。未知路径保守运行全量，不维护易遗漏的业务测试白名单。

| 触发或变更 | 范围 |
| --- | --- |
| PR 仅修改根 README / AGENTS、`docs/` 内 Markdown、`.agents/skills/<skill>/` 内 Markdown / JSON / YAML | 路由回归、Design Token 静态门禁、差异检查与最终门禁；不安装项目依赖、不启动业务测试服务 |
| PR 仅修改已知 Web 源码/静态资产，可混合上述文档 | Python 静态/单元及完整 Web 检查；双数据库与部署检查不适用 |
| PR 仅修改已知独立引擎目录或明确列出的引擎单元测试，可混合上述 Web/文档 | `no-db`：保留 Python 静态/契约/覆盖率、完整 Web、构建和部署检查；六个数据库分片及其汇总不适用 |
| CI、脚本、测试、依赖、配置、契约、迁移、部署、其他路径或空/不确定差异 | 完整 Python/Web/部署检查，六个 PG/InnoDB/MinIO 分片及证据汇总 |
| `main` push、手动运行及未知事件 | 始终全量 |

精确白名单以路由脚本为准。新增应用、资源类型或目录默认全量；缩窄范围前先证明其不依赖被省略的验证，并添加回归。

Skill 的说明、参考、合成评测数据及展示元数据不参与产品构建，复用文档轻量路径。技能内的 Python、Shell、JavaScript 等脚本和未知文件仍走全量；混合业务代码时按业务范围选择，其他 `.agents/` 配置不在白名单内。轻量路径不执行 skill 行为评测，也不证明 JSON/YAML 内容或引用有效，维护者须按变更核对。`main` push 仍按既有约定全量验证。

独立引擎目录白名单为 `deploy/engines/` 下的 `cpu-vad`、`file-asr-audio`、`vllm-asr-lifecycle`；测试白名单为 `test_native_asr_scope.py`、`test_worker_fence.py`（均在 `tests/unit/gateway/`）。集成测试会导入其他部分单元测试工具，因此不按整个 `tests/unit/` 跳过数据库。后端/网关业务实现、共享测试工具、契约、迁移、共享依赖和 CI 配置仍触发全量；任意混合变更取更严格的范围。主干 push 与手动运行继续全量。引擎若接入数据库集成测试，须同步收紧路由并补回归。

## 必需检查与证据

保留 `Python quality and tests`、`Web quality, tests, and build`、`Deployment configuration` 三个受保护检查名。作业级条件让不适用项明确显示 skipped；最终 Python 门禁用 `always()` 执行，核对路由成功和各项实际结果。非法/缺失路由、失败、取消或本应执行却跳过均失败。不适用不表示业务验证已通过。

完整范围仍要求原有覆盖率、契约漂移/兼容检查及六个真实双数据库分片。集成汇总继续核对同一提交、完整收集、互斥并集和实际执行结果，不以数量或仅收集代替证据。

Python 测试作业通过 `python-coverage.py run --workers 2` 在同一 runner 内并发两个 `workbench-pg-probe.py --full-coverage` 进程，运行原完整非集成测试和 Workbench PG／Runtime 全集。各进程独占临时 PG、Unix socket 与 coverage 文件；不连接 runner 的系统数据库。作业仍只准备一次依赖、Linux inspection 环境及隔离 MinIO。父进程先准备四个 fixture 共用的 `speechnexus-workbench-saved` 测试桶，避免两个进程首次创建竞态；其对象按随机 project 前缀隔离，其余桶按文件独占或随机命名。测试按完整收集的文件分组，文件内用例不拆开，较大文件先分到当前用例数较少的一组；这是确定性的数量均衡，不是耗时均衡。

每片保留原完整覆盖源与 branch 配置，只在片内使用 `--cov-fail-under=0` 采集局部数据。父进程必须确认两片执行成功、私有 PG 已停止、同一 SHA/配置、相同完整 collection、精确分配且互斥并集覆盖全集、每个结果实际通过及覆盖数据摘要一致，再显式组合全部分片并按 `pyproject.toml` 原全项目 **80%** 门槛执行 `coverage report`。缺片、失败、跳过、收集限定或仅有局部高覆盖率均不能放行；`PYTEST_ADDOPTS` 不能缩小集合。执行证据校验通过后，复制各片原始覆盖数据并生成组合产物，以 `workbench-pg-full-coverage` 上传；覆盖率不足仍保留这些产物并失败。若测试或执行证据校验失败，上传日志和已生成的分片／生命周期／汇总报告，不生成合格的组合产物。直接运行原 `workbench-pg-probe.py --full-coverage` 仍是单次 pytest 的全项目 80%；`--coverage` 仍是独立的 C 层 90% 检查。

集成分片在安装 Python 依赖后，使用 `uv run python integration-shards.py requirements` 实际收集完整 integration nodeid（包括参数化身份），以执行时相同的资源分组与 SHA-256 分片算法输出 `node_contracts` 与 `browser` 需求。`*_browser.py` 与两个合成浏览器探针模块声明浏览器依赖；回归核对当前所有 Node 浏览器探针消费者均被分类。收集错误或外部 `PYTEST_ADDOPTS` 缩窄选择会失败，不能输出“无需工具”放行。该步骤只收集，实际执行和六份报告的互斥并集核验仍独立必需。

完整集合同时含普通用例与 Web 工具消费者时，普通用例分到前四片，浏览器及显式 Node 契约消费者分到后两片，各组内继续按 nodeid 的 SHA-256 取模；一类用例单独存在时使用全部分片。通用入口仍接受 1–16 片，混合类别为 Web 工具保留 `max(1, count // 3)` 片；单片执行整个集合。预检、执行和报告检查使用同一个 `partition`，工具需求仍根据实际选中用例计算，不能在工作流里按编号跳过工具或业务测试。该分组依据工具成本，不宣称按用例耗时均衡。

有 `browser` 需求的分片安装完整锁定 Web 依赖、构建两个实际入口，并安装相同版本 Chromium headless shell 及系统依赖后运行测试；只有 Node 契约消费者的分片保留锁定生产依赖安装；数据库分片不安装浏览器。需求随真实 nodeid 分区计算，不在 workflow 中固定某几个分片号。汇总 `check` 子命令与 `tests/ci` 轻量路由回归继续仅依赖 Python 标准库。

浏览器准备复用[独立脚本](../../scripts/dev-env/prepare-ci-browser.py)：先从锁定 Playwright CLI 的依赖模拟结果取得缺失/升级包，再向 APT 请求 URI/大小/SHA256 清单。仅在这条清单命令中选择 SHA256 输出，避免 `--print-uris` 的历史 MD5 默认值；不改系统或实际安装配置。归档通过 APT 自带的 `apt-helper download-file` 最多四个并发下载，同时传入强摘要和 `Checksum-FileSize`，让原生获取阶段约束已认证大小；不新增下载器依赖或覆盖传输政策。写入完整 APT 缓存前仍独立核对 SHA256/SHA512 与大小，MD5-only、错误大小、错误强摘要及不匹配的已有缓存均拒绝。不覆盖未知缓存内容。

随后仍执行原 `playwright install --with-deps --only-shell chromium`。信任链依赖 APT 认证的索引与上述写前强校验，不能假定安装器会重新校验完整缓存：APT 2.8 的[现有归档路径](https://github.com/Debian/apt/blob/2.8.1/apt-pkg/acquire-item.cc#L3494-L3507)可能只按大小复用。未知或无法得到强摘要的计划直接失败，不降级到弱摘要或强制信任。

不改软件源、APT/网络安全配置、超时预算或六分片/受保护汇总，不引入跨运行缓存或外部服务。真实 runner 确认 HTTP/HTTPS 配置了 `Timeout`、`Pipeline-Depth`、`No-Cache`，并通过限定字段名诊断定位到直接 HTTP `User-Agent-Non-Interactive`。APT 2.8.1 的[原生实现](https://github.com/Debian/apt/blob/2.8.1/methods/http.cc)按既有配置与 systemd unit 语境选择非交互标记；apt-helper 使用同一 acquire HTTP method，不改写配置值、header 或上下文。仅对上述已核实控制及无控制情形开放预取；HTTPS 同名项、代理、自定义 User-Agent、地址族、速率/队列/hash、特定 APT binary、替代配置、未知/嵌套键或不可读取配置仍沿用原安装器。

诊断输出固定分类、退出码与错误类型；对直接字段仅允许 1–64 字符、字母开头且只含 ASCII 字母/数字/下划线/连字符的字段名。空字段另分类，其他文本只给分类/数量；不输出配置值、代理凭据、嵌套主机名、URL 或原始 stderr。识别出字段名不自动授予预取资格，必须先核对原生语义。未知计划、下载或校验失败会阻止安装，临时预取目录自动清理。首版真实 CI 全部观察到 fallback，不能证明预取或提速；精确支持后仍须取得新的真实 runner 执行证据。

此改进针对已记录的官方字体/库归档串行下载耗尽分片预算，需以新组合实际执行测试验收；单次提速不承诺长期耗时，取消、未开始测试与断言失败分别报告。回退该脚本接线即可恢复原官方安装路径，原失败证据保留。

每个集成分片还检查 PostgreSQL 16 工具，通过 `workbench-pg-probe.py --integration-shard` 启动独立的私有 Unix socket PG，再把 Workbench DSN 传给原 `integration-shards.py run`。双数据库与 MinIO 环境仍指向该作业既有隔离测试服务，不用私有 Workbench DSN 替换协调数据库地址。原分片报告路径、完整 collection、实际执行与意外跳过拒绝规则保持不变；驱动器的生命周期报告与分片报告一并上传，分别保存，不替代分片执行报告。仅有单个分片作业成功不能替代汇总验收。

真实内网模型资格与可复现集成分别验收。`qualification` 标记的三条纪要真实模型用例保留原部署调用与结果断言，不进入普通 `integration` 分片，也不计作 CI 通过；它们验证实际部署资格，不能替代协调事务的确定性门禁。`make workbench-llm-qualification` 要求显式的私有 Workbench PG DSN、获准内网 LLM endpoint/model，并构建真实 Web 入口后执行全部三项。缺少配置时入口直接失败，不以跳过取得资格。普通 CI 的分片完整性仍拒绝任何必需用例跳过，阶段 AC 仍须另附这三项真实模型与版本证据。

不在整个 workflow 的 `on` 中设置 `paths` / `paths-ignore`：被整体过滤的必需检查可能长期 Pending。[GitHub 路径过滤说明](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax#onpushpull_requestpull_request_targetpathspaths-ignore)、[作业条件与 skipped 语义](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/control-jobs-with-conditions)是维护入口；核验日期为 2026-10-25。

## 六分片历史分析与资源分组（2026-10-26）

本次只读查询历史 Actions 作业、步骤时间戳与已上传分片报告，没有触发或重跑 CI。样本为当日 12 次成功全量运行（6 次 push、6 次 PR，共 72 个分片）；排除文档轻量运行、失败和取消，不作为可靠性或长期分位数样本。业务代码随提交变化，这些观测不能视作受控的前后性能实验。

| 历史运行 | 事件 | 整轮秒数 | 六片累计 runner 秒数 | 最慢分片执行秒数 | 分片启动最大等待秒数 |
| --- | --- | ---: | ---: | ---: | ---: |
| [40331636474](https://github.com/hrygo/SpeechNexus/actions/runs/40331636474) | push | 495 | 2018 | 461 | 3 |
| [40315721283](https://github.com/hrygo/SpeechNexus/actions/runs/40315721283) | push | 1214 | 2029 | 569 | 782 |
| [40312196799](https://github.com/hrygo/SpeechNexus/actions/runs/40312196799) | PR | 558 | 1874 | 334 | 3 |
| [40305896128](https://github.com/hrygo/SpeechNexus/actions/runs/40305896128) | PR | 637 | 2115 | 426 | 2 |
| [40300644125](https://github.com/hrygo/SpeechNexus/actions/runs/40300644125) | PR | 522 | 2024 | 489 | 2 |
| [40291095616](https://github.com/hrygo/SpeechNexus/actions/runs/40291095616) | push | 721 | 2358 | 514 | 2 |
| [40289866804](https://github.com/hrygo/SpeechNexus/actions/runs/40289866804) | PR | 553 | 2197 | 487 | 2 |
| [40283969348](https://github.com/hrygo/SpeechNexus/actions/runs/40283969348) | push | 871 | 1754 | 323 | 369 |
| [40282926157](https://github.com/hrygo/SpeechNexus/actions/runs/40282926157) | PR | 616 | 1895 | 351 | 2 |
| [40282156555](https://github.com/hrygo/SpeechNexus/actions/runs/40282156555) | push | 517 | 1837 | 347 | 2 |
| [40281274274](https://github.com/hrygo/SpeechNexus/actions/runs/40281274274) | PR | 519 | 1813 | 331 | 2 |
| [40281123425](https://github.com/hrygo/SpeechNexus/actions/runs/40281123425) | push | 485 | 1756 | 306 | 2 |

整轮从 run `created_at` 到最后作业完成，包含排队与汇总；分片执行从 job `started_at` 到 `completed_at`，包含准备和清理，不是纯 pytest 时间；启动等待从 `changes` 完成算起，也不等同全部 workflow 排队。可用只读命令 `gh api 'repos/hrygo/SpeechNexus/actions/runs/40331636474/jobs?per_page=100'` 复查步骤原始时间戳。

样本给出的优化依据：

- 六片累计执行中位数 **32.61 runner 分钟**，最慢分片作业中位数 **388.5 秒**；整轮中位数 **555.5 秒**。两者不是同一种指标，也没有单片对照，不能声称六倍加速。
- 72 片全部安装完整 Web 依赖、构建 Web 和安装 Chromium；Node 准备、pnpm 激活、Web 安装、构建、Chromium 安装的步骤中位数分别为 **5、2、2、12、21 秒**。其中 Chromium 有 **11/72** 次超过 60 秒，范围 **18–293 秒**。重复准备还扩大了遭遇网络长尾的机会。
- 纯测试驱动步骤中位数 **194.5 秒**；依赖安装通常仅 Python **3 秒**、Web **2 秒**，所以额外引入依赖缓存或增加分片不作为首选。
- **9/12** 次最后结束的主要执行作业是 `Python tests and build`，其完整覆盖率步骤中位数 **432 秒**。只缩短集成分片，通常不能同幅缩短整轮。
- 运行 `40315721283` 有一个分片比轻量检查结束晚 **782 秒**才启动；它本身执行 298 秒。该次 1214 秒整轮不能全归因于测试慢，也不能承诺通过分组消除 runner 排队。

据此先实施两项优化，再处理下节的整轮关键路径：

1. **集中 Web 工具准备，保留六个数据库隔离作业。** 普通测试使用四片，浏览器与 Node 消费者使用两片。使用 revision `96d2e55` 已有的六份报告，先由原版本检查器确认 1807 个身份、1785 通过与 22 个原有 PG-only 不适用参数，再离线重分配：六组为 **429 / 435 / 460 / 433 / 28 / 22** 个用例，后两组分别含 **23 / 17** 个浏览器用例及各 **5** 个 Node 消费者。新分组全集无遗漏、无重复；这不是新分组的数据库执行证据。
2. **消费者回归只收集一次真实全集。** 原回归在六个子进程中重复完整 collection；现在一个隔离子进程收集，检查全部显式消费者确实存在，再验证六组工具需求和全集。合成 CLI 的成功、收集错误、外部选择限制与六组执行报告检查继续保留。全量 Python 与集成日志增加 `--durations=20`，为后续耗时定位保留信息，不改变测试选择或覆盖率。

历史成本推算与已完成的本地测量分别解释：

- 按每次旧运行中前四组实际发生的五项 Node/Web/Chromium 准备步骤相加，新策略可省去的**重复准备毛开销**中位数为 **177 runner 秒/轮**，范围 **156–430 秒**，相当于样本六片累计时间中位数的约 **9%**。用例重新分配会改变 fixture 次数和各组执行时间，因此这不是净 runner 用量或整轮耗时的实测下降。
- 历史工具预检 collection 中位数为 9 秒；若回归内成本接近，少五次 collection 约省 **45 秒**，仅为推算。已完成的本机三组交错旧/新对照中，该回归的 pytest `call` 时间中位数为 **7.06 → 1.15 秒**（约减少 **84%**）。对照使用 `96d2e55` 源码，只替换临时副本的仓库/脚本定位；不比较两种测试路径不同带来的 pytest 启动开销，也不外推为 runner 实测。
- 两个 Web 组的负载集中及四个普通组的用例增多，仍可能改变最长组耗时；当前缺少历史逐用例耗时，**不宣称两组 Web 或六片数量已经最优，不承诺整轮缩短固定秒数**。沿用原 10 分钟预算和严格失败门禁。当前用户要求不重复测试，已有验证直接复用；没有为本次优化运行托管 CI 或真实数据库集成。

## 整体耗时与实施决策（2026-10-26）

整轮完成时间取决于最晚结束的依赖分支，不能把并行作业的节省秒数相加。现有 `changes` 完成后，Python 测试、静态契约、Web、部署及六个集成分片并发；集成证据汇总随后运行，最终 Python 必需检查核对所有适用结果。全量包含 14 个作业，作业间排队与作业内执行分别考虑。

| 分支／步骤 | 12 次历史执行中位数 | 统筹判断与处理 |
| --- | ---: | --- |
| 路由及轻量门禁 | 13.5 秒 | 所有分支的前置，但成本小；继续按准确 diff 路由，文档路径保持轻量 |
| Python 测试与构建 | 488 秒，其中完整覆盖率 432 秒 | 9/12 次为最后结束的主要执行作业；在原作业内并行两个独立测试进程，避免新增 runner 作业与后续调度 |
| 六片集成 | 最慢片 388.5 秒，累计 1956.5 秒 | 仍可能决定最终耗时；集中 Web 准备、消除重复收集，保留六片双库与对象存储验证 |
| Web | 206.5 秒 | 测试 57 秒、实际无障碍检查 58 秒、构建 11 秒、Chromium 准备 19 秒；在该样本中已有余量，保留完整检查与现有并发度 |
| Python 静态与契约 | 81.5 秒 | 漂移检查 19 秒、mypy 19 秒、契约校验 10 秒；与测试并行，继续独立失败，不拆更多作业 |
| 部署配置 | 5.5 秒 | 保留独立检查 |
| 集成汇总／Python 中间与最终门禁 | 7.5／3／3 秒 | 执行本身小；保留同提交证据汇总及既有受保护检查名 |

各行或子步骤中位数不能直接相加还原某次运行；Web/静态子步骤来自同批原始 jobs 数据。实际服务集成测试覆盖真实 PostgreSQL／InnoDB 的事务、并发锁、租约/预算、幂等重放、恢复与 outbox，以及 MinIO 对象版本、删除和 HTTP/Node/浏览器消费者。模型与执行引擎按各测试的显式 fake/合成边界提供；它验证基础设施和生产装配的正确性，不等于真实语音模型或内网 LLM 部署资格。

采用的官方实践及取舍：

- **限定进程并发，保留私有数据库。** 标准 `ubuntu-24.04` runner 的官方资源为公有仓库 4 CPU/16 GB、私有仓库 2 CPU/8 GB，因此上限选两个进程；不假设有更多 CPU，也不把每个测试拆开与共享 PG 并行。按文件分配借鉴 pytest-xdist 的 `loadfile` fixture 复用原则，没有新增 xdist 依赖。[GitHub runner 资源](https://docs.github.com/en/actions/reference/runners/github-hosted-runners)、[pytest-xdist 分配语义](https://pytest-xdist.readthedocs.io/en/stable/distribution.html)。
- **覆盖率先收集再统一门禁。** Coverage.py 支持按文件名组合多个数据源。这里使用公开 `Coverage.combine(..., strict=True, keep=True)` API、先验证每份 branch 数据，再保留输入并执行原配置的最终报告；不能平均两片百分比，也不能用一片成功替代全集。[Coverage 数据组合](https://coverage.readthedocs.io/en/latest/commands/cmd_combine.html)、[公共 API](https://coverage.readthedocs.io/en/latest/api_coverage.html)。
- **区分依赖缓存与执行证据。** 现有 uv 缓存及锁定安装保留。历史 Python 安装通常 3 秒、Web 2–4 秒，新增缓存收益上限小；跨作业传完整环境或构建包还会增加准备依赖、上传/下载与排队。因此本次不增加共享构建作业；测试证据继续使用 artifact，不复用旧运行成绩。[GitHub 缓存与制品](https://docs.github.com/en/actions/concepts/workflows-and-actions/dependency-caching)、[作业输出与依赖](https://docs.github.com/en/actions/how-tos/write-workflows/choose-what-workflows-do/pass-job-outputs)。
- **先减少浏览器准备次数。** Playwright 官方指出浏览器缓存恢复可能接近下载时间，Linux 系统依赖仍须安装，并提醒根据 CI 资源限制并发。本次集中准备的收益有历史步骤支持；不额外增加浏览器并发、缓存或大型镜像拉取，不用未知收益替代现有确定性验证。[Playwright CI](https://playwright.dev/docs/ci)。

覆盖率作业中的 Workbench PG 用例与六片集成有交集：前者为全项目覆盖率提供生产存储路径，后者同时验证完整集成集合、双库与正式消费者。直接删去一侧会改变证明范围；把覆盖采集迁到六片则会增加六片跟踪成本、跨作业覆盖数据与依赖汇总。目前没有逐文件耗时支持其净收益，故本次保持两项门禁，以同机进程并发缩短覆盖率路径。六片也没有单片对照，不能宣称数量最优或六倍收益。

上述官方页面核验日期均为 2026-10-26；资源规格不是本次新 runner 的实测。两个进程的实际 CPU/内存竞争、文件耗时分布和新增普通分片尾部仍需区分于理论依据。

历史情景回放保持每次其他分支结束时间、排队与最终门禁开销不变。令原覆盖率步骤为 `P` 秒，重复 collection 的估算节省为 45 秒，两个进程中较慢者占剩余工作的 `f`，额外准备/收集/组合暂计 10 秒，则该步骤使用 `P' = 10 + f × (P − 45)`。每次整轮减少量取 `max(原 Python 分支结束, 其他分支结束) − max(新 Python 分支结束, 其他分支结束)`，不把所有节省相加。

| 情景 | 12 次逐轮节省的中位数 |
| --- | ---: |
| 即使删除整个 Python 分支、其他分支不变 | 108 秒：给出这批历史样本可由 Python 优化取得的上界 |
| `f = 0.50` | 108 秒 |
| `f = 0.65` | 108 秒 |
| `f = 0.80` | 105.1 秒 |

这是**条件情景，约 1.8 分钟的逐轮节省中位数**，不是新 CI 实测、置信区间，也不是保证；不能从原整轮 555.5 秒直接减去这个中位数得到“优化后中位数”。三个样本即使消除 Python 耗时也没有收益，慢分片或排队已决定结束时间。45 秒和 10 秒均为假设；若并发受到资源争用或文件耗时严重偏斜，实际收益可能更低甚至倒退。六片分组同时改变了其他分支，故该回放不冒充两项优化的联合预测，也不把 177 runner 秒准备毛开销叠加到 wall 收益。

本次按用户要求不触发 CI、不重复业务回归或基准。此前资源分组验证直接复用；新增并行编排的 43 项不同合成/配置边界检查通过，含共享桶预建和失败日志保留；只在修复新发现的并发边界后重跑直接受影响项。Ruff 与目标文件 Linux mypy 通过。没有执行新的双进程全量业务测试，未宣称托管耗时或资源峰值已验证。原单进程入口保留，恢复工作流该步骤即可退出并行编排。

## 首次托管运行与参数 ID 修复（2026-10-26）

[首次运行 40344964032](https://github.com/hrygo/SpeechNexus/actions/runs/40344964032) 中，两个 Python 进程分别 **3723 passed**，用时 **224.40 / 185.56 秒**；汇总在组合覆盖率前拒绝了超限报告，故本轮 Python 作业失败，不能报告全局覆盖率通过或整轮提速验收。

证据文件实长 **57,532,527 / 75,613,431 bytes**。7446 个身份中有 13 个超过 16 KiB，最长 **10,000,125 字符**：pytest 自动把大参数内容转义为用例 ID，再在完整 collection、selected 与 outcomes 中重复保存。原测试的百万字符样本对应产品既有的 1,000,000 字符上限，是边界验证；将内容复制到身份与报告没有验证收益。

修复使用 [pytest 官方 `pytest_make_parametrize_id` hook](https://docs.pytest.org/en/stable/reference/reference.html#pytest.hookspec.pytest_make_parametrize_id)：仅对超过 256 字符的 `str/bytes` 默认 ID 使用参数名、类型、长度和完整 SHA-256；短值、其他类型及显式 ID 保持 pytest 原规则。原始参数值、测试数量与断言不变，不新增同规模边界样本。坏 surrogate 用 `surrogatepass` 生成确定摘要，重复参数仍由 pytest 添加唯一后缀。该规则约束身份尺寸，不是失败详情的通用脱敏机制。

新增 9 项轻量回归，仅用 1700 字符即可复现旧实现的 17041 字符 ID，再验证短 ID、确定性、重复值及显式 ID。修复后的仅收集检查确认原 7446 项按测试函数分组的参数数量保持一致；加入 9 项新回归后，最大 ID 为 **1626 字符**，两片 JSON 内容大小估算为 **1,932,258 / 1,900,544 bytes**。此项只证明收集及尺寸，没有再次执行业务测试。原 **16 MiB 报告、16 KiB 身份、完整执行及全项目 80%** 保护均不放宽；最终结果以修复提交的托管 CI 为准。

### 托管复核及证据下载退出异常

[修复后运行 40348884456](https://github.com/hrygo/SpeechNexus/actions/runs/40348884456) 的 Python 两片分别 **3728 / 3727 passed**，合并全项目覆盖率 **84%**，原 80% 门槛通过；覆盖步骤 294 秒，Python 作业 347 秒。六个集成分片、Web、静态契约及部署检查均通过。这是单轮观测，不能代替同条件耗时对照或长期提速结论。

集成汇总另遇下载动作退出异常：作业 `121668563675` 的六份 artifact 均约六秒下载完成，SHA-256 全匹配，并输出 `Download artifact has finished successfully`，进程随后不退出，耗尽原两分钟预算。仅重试汇总与后续门禁一次，作业 `121671154056` 在同一位置复现；未因此重跑业务分片。日志把故障定位到下载动作完成后的进程退出，尚未确定其具体未释放资源。

工作流保留官方固定版本 v8.0.2，使用其 `github-token` 输入选择公开 REST 路径，并显式固定当前 `github.repository` / `github.run_id`，避免继续走本轮卡住的默认运行时客户端。仅汇总作业增加 `actions: read`，全局仍为 `contents: read`；继续要求摘要不匹配即失败、六片成功、同提交完整互斥集合与实际结果，超时仍为两分钟。官方源码确认该输入切换客户端，公开路径仍计算并比较下载摘要；是否消除托管退出异常必须由新提交实测确认，不能以配置回归代替。依据：[固定版本输入](https://github.com/actions/download-artifact/blob/9000827ccba6bdab643e8b6fd33ac0654aef8333/action.yml)、[客户端路径选择](https://github.com/actions/toolkit/blob/main/packages/artifact/src/internal/client.ts)、[公开路径摘要校验](https://github.com/actions/toolkit/blob/main/packages/artifact/src/internal/download/download-artifact.ts)，核验日期 2026-10-26。

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
| 两个实际构建入口 | 系统浅/深色、显式浅色覆盖深色系统、显式深色覆盖浅色系统，各测 320px／1280px；页面运行 WCAG 2.0/2.1/2.2 A/AA 适用规则与 axe best-practice，所有 violation 失败 |
| 主题语义文字 | 每个页面情景检查 21 对前景／背景：主、次、弱化与强调文字 × 四种表面，三个能力状态，以及强调／警示色对。透明值在明确的 page 底色上合成，普通文本以 4.5:1 检查；每个探针必须产生明确通过证据，不允许 incomplete 或缺失 Token |
| 键盘与焦点 | 首次 Tab 到可见跳转链接，Enter 后主内容真正获得焦点；焦点环可见、至少 2px，对三个基础表面达到 3:1。该项目策略不等于完整证明 WCAG 焦点区域／遮挡成功条件 |
| 用户偏好 | 两入口分别在浅/深色下验证增强对比度、强制颜色和减少动效；状态动效最长 5 秒，减少动效时无无限循环；强制颜色测 UA 实际调整后的颜色 |
| 门禁自证 | 故意注入无名称按钮、低对比度文字、失效跳转目标、隐藏焦点环、局部坏色带及玻璃合成回归，证明对应保护实际失败；亮度公式以已知色对和 4.5 临界值回归 |

axe 的背景图像／渐变 `color-contrast` incomplete 转入[栅格门禁](../../tests/web/raster-contrast.mjs)，不再使用文字位置或前景 Token 白名单。Chromium 在当前 viewport 按真实滚动位置截图，覆盖实际渐变、半透明背景及单层 `backdrop-filter`；DOM Range 定位每条文字行框，检查其中**每个 CSS 像素**的最差对比度，不只取中心点或渐变端点。小文本要求 4.5:1；24px 以上，或至少 18⅔px 且字重 ≥700 的大文本要求 3:1；判断使用未舍入比值。

测量时只临时隐藏选中文字节点的墨迹，保留 `currentColor`、背景、边框与玻璃合成；校验文字几何和原有绘制属性没有变化，异常时也恢复 DOM 和滚动。可能影响同一玻璃背景的文字分开截图，避免隐藏后方文字改变被测底色。每情景最多 512 个文字行框、12 张 viewport 截图，超限失败而非截断。PNG 用现有浏览器解码，不新增图像库、依赖或 CI 作业。报告增加逐行最差背景、比值、阈值、采样数及截图批次，失败额外附背景截图。

该方法限定于固定 Chromium、DPR=1、sRGB 8-bit 的现有静态情景。行框包含字间空白，比逐字形检查保守；通过只覆盖这些实际渲染像素，不能外推到所有尺寸、DPR、浏览器或动态帧。组透明度、可测的半透明前景、前景 filter/mix-blend/mask/clip、伪元素改变文字或背景、生成文字、动态绘制、嵌套玻璃及与文字交叠的非祖先玻璃层等未建模情况，明确失败并要求补充实现或针对性复核，**不保留自动放行例外**。其他种类的 axe incomplete 仍失败。依据：[W3C F83 最不利背景检查](https://www.w3.org/WAI/WCAG22/Techniques/failures/F83)，核验日期 2026-10-25。

成功和失败均上传 `web-accessibility-evidence`，保留 7 天；包含 HTML/JSON、逐情景 axe 与语义色对证据，桌面浅/深色截图，失败时额外保留截图和 trace。报告仅含合成页面。维护测试时同时维护其覆盖声明，不能使用全局关闭规则、自动重试或既有 violation 基线掩盖回归。

本机复现：

```sh
pnpm install --frozen-lockfile
pnpm build
pnpm exec playwright install --only-shell chromium
pnpm test:a11y
```

Linux CI 使用 `--with-deps` 安装浏览器系统依赖。本机须有对应浏览器前置条件；失败不改用 jsdom 伪装浏览器通过。自动门禁覆盖当前工程外壳，不能代替屏幕阅读器、人工键盘走查、缩放/复杂遮挡、所有非文本图形或后续业务流程验收。规范核验日期为 2026-10-25：[WCAG 2.2](https://www.w3.org/TR/WCAG22/)、[文本对比度](https://www.w3.org/WAI/WCAG22/Understanding/contrast-minimum.html)、[Playwright 自动与人工测试边界](https://playwright.dev/docs/accessibility-testing)。

## 轻量路径实测（2026-10-25）

| 指标 | 原文档 PR | 新文档 PR |
| --- | ---: | ---: |
| 实际执行作业 | 13 | 2 |
| 整轮耗时（含调度/排队） | 821 秒 | 16 秒 |
| 累计作业执行时间 | 1584 秒 | 11 秒 |

来源：[原 PR #138](https://github.com/hrygo/SpeechNexus/actions/runs/40155498616)、[新 PR #144 首次运行](https://github.com/hrygo/SpeechNexus/actions/runs/40158818797)。新运行的七个业务作业按范围不适用，最终必需门禁通过。完整路径另由 [PR #143](https://github.com/hrygo/SpeechNexus/pull/143) 的 14 项检查通过验证。

以上是增加 Design Token 前置步骤之前的两次文档改动托管观测；runner 与排队情况不同，不代表当前耗时、长期分位数或业务测试自身提速。`main` 与手动运行继续执行完整验证。
