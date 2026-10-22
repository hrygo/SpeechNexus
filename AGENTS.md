# SpeechNexus 工程规则

本文件适用于整个仓库。系统、开发者和当前用户指令优先；本文件补充跨任务稳定的工程约束，不替代专业文档、契约或实测记录。

## 工作入口

- [README](README.md) / [English README](README.en.md)：当前实现状态、工具链与启动入口。
- [文档索引](docs/README.md) / [整体技术方案](docs/architecture/technical-blueprint.md)：产品设计、内部协议、事务、部署与工程交接。
- [工程目录与依赖边界](docs/architecture/repository-layout.md)：workspace、依赖方向、契约流程和最小门禁。
- [契约入口](contracts/README.md) / [架构决策](docs/decisions/README.md)：机器语义和已接受约束。
- [开发与测试环境](docs/development-environment.md) / [部署入口](deploy/README.md)：隔离服务、配置和拓扑。

## 事实来源

判断当前行为时按以下顺序取证：

1. 运行结果、代码和测试；
2. `contracts/` 中的机器契约；
3. `docs/` 中状态为 `active` 的设计与状态为 `accepted` 的 ADR；
4. 计划、审查和带日期记录只说明其记录时点，不能证明当前能力。

报告必须区分“已实现”“已通过实测”“契约要求”和“后续计划”。设计 `active` 或 ADR `accepted` 只表示设计基线或决定被接受；健康检查通过不等于模型就绪、可准入、语音质量、容量、长时稳定性或发布资格通过。

当前实现是 FastAPI 后端 / 独立执行网关、运营健康检查与两个 Web 外壳；仅 `contracts/operations.openapi.yaml` 已冻结。后续交付业务能力时，应同步更新本节与两份 README，不能沿用初始化状态或仅根据计划宣告完成。

## 产品与数据边界

- 首期没有登录、用户目录、RBAC、ACL、多租户或个人私有空间。一个部署默认共享 workspace；`/admin` 与 `/workbench` 只代表功能视图，不形成权限边界。
- 语音底座可独立运行，不依赖 WebUI、工作台业务数据或 LLM。工作台通过公开语音协议调用底座。
- PostgreSQL 是任务、版本、用量、租约和 outbox 的权威存储；对象存储保存明确提交的输入与制品。Redis 只可作为可选加速层。
- 工作台不共写底座表；项目、稿件、会议、助手、纪要与有声书编排留在工作台模块，不能进入语音核心或无业务所有权的 `web/shared`。
- 日志、fixture、测试和报告不得包含凭据、完整私有音频、Base64、完整转写、声音特征或内部模型路径。
- 仓库不得提交 `.runtime/`、`.venv/`、`node_modules/`、模型、运行数据或真实凭据。

## 契约与模块

- P0 业务契约尚未冻结。不得把 `/v1/audio/*`、Realtime、Job、音色或工作台占位目录当成已实现能力。
- 先冻结生产者与消费者的机器契约，再实现业务路由。公共协议与内部执行 / 部署控制协议分别建模；默认值、单位、范围、状态和稳定错误码只在契约中定义。
- Python / TypeScript 边界类型从契约生成或校验，生成产物不手改，不维护独立语义副本。正例、负例、事件序列与状态竞争样例须被相关消费者共同验证；schema 语法通过不能代替语义冻结。
- 依赖指向内层：`interfaces / infrastructure → application → domain`；组合根可以装配各层。domain 不导入框架、生成 DTO、数据库或 vendor 类型，adapter 在边界转换。`uv run lint-imports` 是强制门禁。
- `backend` 与 `execution-gateway` 独立部署。按设计，执行网关独占引擎入口、凭据、提交屏障、fencing、abort 与完成证据；当前网关只有运营外壳，上述业务行为仍待实现。
- 部署侧受限控制器只执行批准模板对应实例的准备、停止与 reconcile。业务 API、worker、工作台不直接访问引擎，不持有任意容器管理权限或挂载 Docker socket。
- 持久化按表明确所有权，采用显式短事务和版本化迁移；运行角色不执行 DDL。推理、HTTP、对象写入和长时编码不占用数据库事务；对象先私有 staging，再发布数据库引用。
- SpeechRail 参考来源固定为 `3f57647a3ee79c04318d8bf097489b2b5185da4f`。只提取经验证的领域规则与测试思想；macOS、MLX、CoreML、私有 IPC 与本机生命周期代码不复用。提取时记录来源、许可证和变更范围。

## 模型、资源与网络边界

- 模型分工与加载策略以 [ADR 0002](docs/decisions/0002-speech-models-engines-and-loading.md) 为准；选型已接受不代表 adapter、目标硬件兼容性或真实推理通过。应用与推理运行时分别锁定依赖，vendor 依赖不强塞入应用 Python workspace。
- 启用的实时绑定先加载、热身、就绪，再准入并常驻；VoiceDesign 按需加载。准入覆盖完整执行配方的加载、热身和阶段峰值，共享物理内存只在父预算计一次，创作任务不得借用实时保留。
- 绑定版本、运行实例 / generation、容量租约和资格分别建模，任务增量预算与实例常驻预算分别记账。任务完成、连接断开或取消已接收均不等于算力停止；未知清理保持隔离并占用相关预算，取得对应停止与资源释放证据后才归还。
- 音色设计须经过 Base 克隆与换文复用校验，再显式发布不可变版本；`preview_only` 不能作为可复用音色发布。实例卸载不退役绑定、不删除音色资产；已发布音色的重建材料须按其持久依赖保留。
- 遵守 [ADR 0003](docs/decisions/0003-workbench-llm-network-boundary.md)：语音模型本地推理；工作台 LLM 默认关闭，仅使用部署批准的本机 / 内网服务。端点与凭据由服务端管理，普通请求不能指定任意地址，失败不回退外网。
- 业务音频、转写、书稿、声音参考及派生内容不得出网；同一边界覆盖代理、日志、追踪、错误报告和浏览器资源。业务运行使用预先准备并锁定的模型与镜像，不在推理请求中下载权重。
- LLM 任务、取消、用量与故障归工作台管理，不占用底座语音队列；共享硬件仍计入同一物理预算。内网地址不是出站限制的证据，当前 Compose 尚未实现完整内容网络约束。

## 工具链与验证

- Python 3.14 由 `uv` 管理，精确版本见 `.python-version`；workspace 必须从根目录运行 `uv sync --locked --all-packages --all-groups`、`uv run ...`，并提交 `uv.lock`。
- Node 26 / `pnpm` 12，精确版本见 `.nvmrc` / `package.json`。`web/admin`、`web/workbench`、`web/shared` 是 workspace 成员；共享依赖版本集中定义在 `pnpm-workspace.yaml` catalog。使用 `pnpm install --frozen-lockfile` 并提交 `pnpm-lock.yaml`。
- 结构查询使用已安装的 `codebase-memory` 技能，依赖图结论前检查所引用文件的索引覆盖；缺失、过期或排除范围直接读源文件。配置、字面量与非代码文件使用直接搜索和读取。
- 修改后至少运行与风险匹配的检查；完整本机门禁为 `make verify`，具体步骤以 `scripts/dev-env/verify.sh` 为准。CI 的 Python、Web、Compose 检查不等于完整本机或真实引擎验收。
- 仅改文档时检查相对链接、Markdown 格式与 `git diff --check`；不把未运行的业务测试写成通过。仅改 Python 至少检查 Ruff、mypy、import-linter 与 pytest；仅改 Web 至少执行格式、lint、类型、测试与构建。契约变化运行 `make contracts-check` 并增加对应语义验证；持久化或调度变化增加隔离 PostgreSQL / MinIO 集成测试。
- 本机 `make dev-bootstrap` 固定用于 Linux ARM64 / APT / 用户级 systemd；涉及服务启动时使用隔离的 `.runtime` PostgreSQL / MinIO，不访问同机共享实例。Compose 与本机开发服务共享宿主端口，启动前确认只运行一套拓扑。
- 确定性测试使用 fake adapter，不下载模型、不使用私人音频。真实引擎、质量与负载验证必须另记版本、绑定、拓扑和证据。
- 自动化检查不得把跳过伪装为通过。尚不存在的契约或测试应明确报告缺少覆盖。

## Git 与交付

- 保留用户及并行任务的改动，不覆盖、回退或整文件还原来源不明的修改。
- 一个提交只表达一个逻辑主题，消息使用 `<type>: <why>`。提交前检查 staged diff、空白错误和敏感字段。
- 用户未明确要求时，不自动 commit、push、发布或修改外部运行态。
- 公共契约、架构或运行行为变化时，同步更新实现、测试与对应文档；README.md 与 README.en.md 的状态、命令和产品边界保持一致。历史计划与审查记录保持原时点事实。
