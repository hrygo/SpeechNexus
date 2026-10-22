# SpeechNexus 工程规则

本文件适用于整个仓库。系统、开发者和当前用户指令优先；本文件补充跨任务稳定的工程约束，不替代专业文档、契约或实测记录。

## 事实来源

判断当前行为时按以下顺序取证：

1. 运行结果、代码和测试；
2. `contracts/` 中的机器契约；
3. `docs/` 中状态为 active 的设计与 ADR；
4. 计划、审查和带日期记录只说明其记录时点，不能证明当前能力。

报告必须区分“已实现”“已通过实测”“契约要求”和“后续计划”。健康检查通过不等于语音质量、容量、长时稳定性或发布资格通过。

## 产品与数据边界

- 首期没有登录、用户目录、RBAC、ACL、多租户或个人私有空间。一个部署默认共享 workspace；`/admin` 与 `/workbench` 只代表功能视图，不形成权限边界。
- 语音底座可独立运行，不依赖 WebUI、工作台业务数据或 LLM。工作台通过公开语音协议调用底座。
- PostgreSQL 是任务、版本、用量、租约和 outbox 的权威存储；对象存储保存明确提交的输入与制品。Redis 只可作为可选加速层。
- 日志、fixture、测试和报告不得包含凭据、完整私有音频、Base64、完整转写、声音特征或内部模型路径。
- 仓库不得提交 `.runtime/`、`.venv/`、`node_modules/`、模型、运行数据或真实凭据。

## 契约与模块

- P0 业务契约尚未冻结。不得把 `/v1/audio/*`、Realtime、Job、音色或工作台占位目录当成已实现能力。
- 先冻结生产者与消费者的机器契约，再实现业务路由。默认值、单位、范围、状态和稳定错误码只在契约中定义。
- 保持 `domain → application → infrastructure/interfaces` 的方向；组合根可以装配各层。`uv run lint-imports` 是强制门禁。
- `backend` 与 `execution-gateway` 独立部署。执行网关拥有引擎提交屏障、fencing、abort 与完成证据，其他进程不得直接持有引擎凭据。
- SpeechRail 参考来源固定为 `3f57647a3ee79c04318d8bf097489b2b5185da4f`。只提取经验证的领域规则与测试思想；macOS、MLX、CoreML、私有 IPC 与本机生命周期代码不复用。提取时记录来源、许可证和变更范围。

## 工具链与验证

- Python 3.14 由 `uv` 管理；workspace 必须从根目录运行 `uv sync --locked --all-packages --all-groups`、`uv run ...`，并提交 `uv.lock`。
- Node 26 / `pnpm` 12。`web/admin`、`web/workbench`、`web/shared` 是 workspace 成员；版本集中定义在 `pnpm-workspace.yaml` catalog，并提交 `pnpm-lock.yaml`。
- 修改后至少运行与风险匹配的测试；完整本地门禁为 `make verify`。涉及服务启动时使用隔离的 `.runtime` PostgreSQL / MinIO，不访问同机共享实例。
- 确定性测试使用 fake adapter，不下载模型、不使用私人音频。真实引擎、质量与负载验证必须另记版本、绑定、拓扑和证据。
- 自动化检查不得把跳过伪装为通过。尚不存在的契约或测试应明确报告缺少覆盖。

## Git 与交付

- 保留用户及并行任务的改动，不覆盖、回退或整文件还原来源不明的修改。
- 一个提交只表达一个逻辑主题，消息使用 `<type>: <why>`。提交前检查 staged diff、空白错误和敏感字段。
- 用户未明确要求时，不自动 commit、push、发布或修改外部运行态。
- 公共契约、架构或运行行为变化时，同步更新实现、测试与对应文档；历史计划与审查记录保持原时点事实。
