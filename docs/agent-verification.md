# Agent 验证指南

从仓库根目录执行命令，按本次改动选择适用行；涉及多个边界时取并集。配置、契约和 CI 有变化时重新核对入口，不为一次小改动默认运行完整本机验证。

## 工具链与依赖

精确 Python / Node 版本见 `.python-version`、`.nvmrc` 与 `package.json`；共享 Web 依赖由 `pnpm-workspace.yaml` catalog 管理。依赖变更使用包管理器更新对应 lockfile，不手改 lockfile。

依赖缺失或 lockfile 改变，且当前验证确实需要时，才执行相应锁定安装：

```bash
uv sync --locked --all-packages --all-groups
pnpm install --frozen-lockfile
```

## 按改动选择检查

| 改动 | 验证入口与范围 |
| --- | --- |
| 文档、`AGENTS.md`、无脚本的 skill、Issue / PR 模板 | 核对本地链接、frontmatter、命令、规则衔接与声明；表单额外检查 YAML、字段类型、唯一 ID 和必填项；`git diff --check`。不默认安装依赖、初始化服务或运行业务测试；本地结构检查不代表 GitHub 页面已生效 |
| skill 中的脚本 | 在隔离样例中运行受影响分支、失败与重试出口；再按脚本语言选择检查 |
| Python | `uv run ruff format --check .`、`uv run ruff check .`、`uv run mypy`、`uv run lint-imports`，以及覆盖改动的 `uv run pytest` 测试 |
| Web | `pnpm format:check`、`pnpm lint`（含设计 Token 门禁）、`pnpm typecheck`、`pnpm test`、`pnpm build`、`pnpm test:a11y`；真实浏览器前置与证据范围见 [CI 规则](validation/ci-validation.md) |
| 机器契约 | `make contracts-check`、`uv run pytest tests/contract`；生成类型受影响时先 `make contracts-generate`，再 `make contracts-drift` 并核对消费者 |
| 版本化 HTTP 源 | 设置已核实的 `CONTRACT_BASE_REF`，按[结构检查范围](validation/http-contract-change-checks.md)运行 `make contracts-http-changes`；完整 speech HTTP 表面另用 `make contracts-speech-changes`，不以结构检查替代语义和消费者验收 |
| 公开实时 runtime 声明 | 设置已核实的 `CONTRACT_BASE_REF`，运行 `make contracts-realtime-changes`；[实时结构检查](validation/realtime-contract-change-checks.md)保守拒绝约束变化，不替代时序与真实消费者验收 |
| OpenAI 兼容子集 | 在机器契约检查上增加 `make contracts-sdk-test`；将 `CONTRACT_BASE_REF` 设为已核实的目标分支基线后运行 `make contracts-compat`，不从当前本地分支猜主干 |
| 持久化、调度或恢复 | 相关 fake 与隔离 PostgreSQL / MinIO 集成测试，覆盖受影响的事务和故障路径 |
| 协调事务、DDL 或权限 | `make coordination-test` 与 `make integration-test`；先读[目录规则](../tests/integration/AGENTS.md)及[隔离说明](../tests/integration/README.md)，保留双库行为与并发证据 |
| 部署配置 | `make stack-config` 及受影响镜像或服务的针对性检查 |
| 完整本机验证 | 条件满足时运行 `make verify`；其入口是 `scripts/dev-env/verify.sh`，CI 调度与范围见 [CI 规则](validation/ci-validation.md)及 [ci.yml](../.github/workflows/ci.yml) |
| 发布候选 / 上线前检查 | `./scripts/dev-env/release-preflight.sh`；默认只读，C5/C6 需 `--allow-write`，证据范围与 SKIP 条件见[上线前检查脚本](operations/release-preflight.md) |

格式检查不代替构建，schema 语法不代替语义验证，生成产物不代替生产消费者接入。复用仍有效的验证结果；相关改动、失败或未解疑点才需要扩大或重复验证。

`make verify` 不覆盖上表所有独立检查：它不运行 `pnpm test:a11y`，也不运行
HTTP、协调或实时结构变更检查；OpenAI 兼容检查仅在本地存在 `origin/main` 时执行。
这些检查适用时仍须按对应行单独运行，并核实基线；不能将 `make verify` 成功等同于
这些检查已通过。

## 服务与宿主前置

`scripts/dev-env/` 的环境管理针对 **Linux ARM64、APT、用户级 systemd**。先核验实际宿主、脚本前置条件、已有服务和端口；[开发环境文档](development-environment.md)的历史就绪记录不能证明当前机器可运行。

- 只用本项目隔离的 PostgreSQL、MinIO 和协调测试 MySQL，不访问同机共享或生产实例。本机 PostgreSQL / MySQL 数据使用 `.runtime/`，测试 MinIO 容器使用有界 tmpfs；也可显式配置其他获准的隔离服务。测试会创建、修改和清理自己的临时 namespace、角色与桶。
- `make coordination-test` 和 `make integration-test` 同时要求 `SPEECHNEXUS_TEST_DATABASE_URL`、`SPEECHNEXUS_TEST_MYSQL_URL`；后一目标还要求 `SPEECHNEXUS_TEST_MINIO_ENDPOINT`、`SPEECHNEXUS_TEST_MINIO_ACCESS_KEY`、`SPEECHNEXUS_TEST_MINIO_SECRET_KEY`。配置缺失即说明未验证，不通过跳过绕开门禁。
- Make 目标会调用受管服务脚本；`make verify` 还提供本机隔离默认值、确保项目 MySQL / MinIO 服务并检查环境。因此这些命令包含运行态操作。`make integration-test` 在检查 MinIO 配置前会先调用 MySQL 服务脚本，配置不完整也可能已产生 MySQL 侧操作。授权涵盖实现与隔离测试且条件满足时继续；只有文档、查询或审查授权时不启动服务。
- 本机 MySQL 服务脚本可能强制移除标签或镜像不匹配的同名容器；运行上述目标前核实该容器的归属和用途，详见[开发环境说明](development-environment.md#运行组件)。
- 不把运行角色用于 DDL 或测试管理员操作。不读取无关凭据，不在日志和报告输出完整 DSN、密钥或业务内容。
- 宿主或服务条件缺失时，完成可独立执行的检查，并报告未验证项及所需条件；不为绕过前置而改向共享服务或自行迁移环境。

`workbench-pg-probe.py` 每次创建独立的 `.runtime/team-c/pg-*` 集群。默认在确认停库后删除
该次的 `data/`，测试失败也按此规则清理；从未启动服务器的初始化失败会清理部分数据。
已生成的 `pytest.log`、`postgres.log`、覆盖率文件及生命周期 JSON 保留；这些文件并非每次
运行都会生成，例如测试子进程超时可能没有 `pytest.log`。报告用 `data_removed` 标明是否已
确认完整清理。排障确实需要数据库时显式传 `--keep-data`，完成后清理该次保留的数据。
启动失败、超时或中断时，后台启动可能继续；即使暂未观察到服务，也保留数据、记录
`startup-unconfirmed` 并使探针失败。停库失败、状态查询失败或超时同样保留数据并使探针失败。
报告中的 `shutdown_observed` 记录停库检查当时是否观察到无运行服务且无 PID 文件；未尝试
启动时为 `null`，`false` 表示未能确认停库，不代表确认服务仍在运行。启动未确认时，即使
此字段为 `true`，后台仍可能继续启动，`stopped` 保持 `false` 且数据保留。
文件清理失败也记录原因并返回失败；删除可能已部分完成，只能保证如实报告残留，不能保证
数据完整。不按目录年龄自动扫描或清理其他运行的集群。

## 证据与交付

报告本次 diff 对应的实际命令、结果、revision、运行环境与覆盖限制；区分通过、失败、跳过、未运行。验证修改了文件时，重新审阅最终 diff，并核对已有证据是否仍适用。

确定性测试使用 fake adapter 和合成或最小脱敏数据，不下载、加载模型。真实引擎、质量或负载验证另记引擎/模型版本、绑定、拓扑和结果；下载、加载与环境变更遵循各自授权。

单库、单进程双连接、合成停止观察、健康检查、媒体/浏览器冒烟分别只证明实际覆盖范围。跳过不算通过；分支 CI 不冒充合并后验证；原型结果不冒充生产装配、真实停止或物理多机/HA 资格。
