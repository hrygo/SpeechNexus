# SpeechNexus

[English](README.en.md)

SpeechNexus 面向 Linux 上可独立部署的企业级语音服务，目标是以 vLLM / vLLM-Omni 为主要推理后端，提供 OpenAI 兼容的实时与非实时 ASR/TTS 子集，以及音色创作等增强协议。WebUI 分为 **AdminUI** 与 **个人语音工作台**，面向部署维护和不同语音任务需求。

当前阶段：**工程骨架已建立，P0 业务契约待冻结**。

| 范围             | 当前状态                                                                                                                          |
| ---------------- | --------------------------------------------------------------------------------------------------------------------------------- |
| 后端与执行网关   | 已实现独立 FastAPI 入口、严格配置、请求关联 ID、`/healthz` 与 `/readyz`；API 探测 PostgreSQL / 对象存储，网关探测控制库           |
| AdminUI / 工作台 | 已实现分别构建和部署的界面外壳，业务流程待接入                                                                                    |
| 机器契约         | 当前仅冻结[运营健康检查](contracts/operations.openapi.yaml)；语音、音色、Job、Realtime、工作台与内部执行 / 实例控制契约待 P0 交付 |
| 模型与部署资格   | 首选模型和加载策略已记录于 ADR；真实推理、语音质量、混合负载、取消、长时稳定性和生产资格尚未验证                                  |

健康检查与工具链冒烟只证明各自检查的范围，不能证明设计中的语音能力、性能或容量已经可用。

## 快速开始

从仓库根目录执行。当前 `dev-bootstrap` 固定用于 Linux ARM64、APT 与用户级 systemd 环境，会安装锁定工具链、Chromium，并初始化和启动隔离 PostgreSQL / MinIO。其他主机可参考[部署入口](deploy/README.md)准备 Compose 开发拓扑；不能直接套用本机初始化脚本。

```bash
make dev-bootstrap
make verify
```

工具链为 Python 3.14（[.python-version](.python-version)）、Node 26（[.nvmrc](.nvmrc)）与 pnpm 12（[package.json](package.json)）。已有本机开发服务时，使用 `make install` 安装锁定依赖，`make dev-up` 启动服务，`make dev-status` 检查状态。`make verify` 还依赖 Docker Compose CLI 进行配置校验。

应用启动命令分别在独立终端执行：

| 命令               | 本机开发入口                                                      |
| ------------------ | ----------------------------------------------------------------- |
| `make api-dev`     | 语音 API 外壳：`http://127.0.0.1:8000`                            |
| `make gateway-dev` | 执行网关外壳：`http://127.0.0.1:8010`                             |
| `make web-dev`     | AdminUI：`http://localhost:5173`；工作台：`http://localhost:5174` |

服务启动后可检查 API 的运营端点；执行网关使用相同路径、端口为 `8010`：

```bash
curl --fail http://127.0.0.1:8000/healthz
curl --fail http://127.0.0.1:8000/readyz
```

本机 PostgreSQL / MinIO 使用 `127.0.0.1:55432` / `127.0.0.1:59000`，数据和凭据位于被 Git 忽略的 `.runtime/`。Compose 拓扑使用相同数据库与对象存储宿主端口，两套拓扑只能启动一套；Compose 的 Web 端口为 `8080` / `8081`。`make stack-config` 仅校验 Compose 配置。完整说明见[开发与测试环境](docs/development-environment.md)和[部署入口](deploy/README.md)。

## 工程基线

- Python 3.14 使用 `uv` workspace，后端与执行网关分别拥有 `src/` 包、严格类型检查、分层依赖契约和独立入口；`uv.lock` 与 `pnpm-lock.yaml` 固定依赖。
- React 19 + TypeScript 6 + Vite 8 使用 `pnpm` workspace；AdminUI、工作台与共享组件分别构建，版本集中在 catalog。
- 公共契约位于 `contracts/`；当前只冻结运营健康检查，不提前伪造尚未评审的语音协议。
- 单元、契约、fake 与集成测试目录已分离；目录存在不表示对应业务已有覆盖。`make verify` 执行本机完整门禁，CI 分别执行 Python、前端和 Compose 配置检查，具体步骤见[验证脚本](scripts/dev-env/verify.sh)与 [CI 配置](.github/workflows/ci.yml)。
- 首期无登录、用户目录、RBAC 或多租户。界面与文档明确数据由当前部署共享。

```text
backend/             语音底座与可选工作台模块
execution-gateway/   独立执行网关
web/                 admin、workbench 与 shared workspace 成员
contracts/           公共与内部机器契约入口
deploy/              开发 Compose、镜像与配置模板
tests/               单元、契约、fake 与集成测试
scripts/dev-env/     隔离环境初始化、启动与验证
docs/                产品、架构、ADR、计划与审查
```

协作约束见 [AGENTS.md](AGENTS.md)；模块边界和最小验证要求见[工程目录与依赖边界](docs/architecture/repository-layout.md)。

## 产品方向

- 底座可独立部署，供 SDK、MCP 和外部应用调用，不依赖 WebUI 或 LLM。
- 标准兼容接口之外，提供音色设计、克隆、试听、校验与版本发布，以及预设、持久任务、资源调度和诊断。
- AdminUI 组织模型服务、预设、资源池、任务和运维；个人工作台组织转写、配音、音色创作、会议、助手、提词、字幕与有声书。
- **首期不建设用户身份认证体系**，无登录、用户目录、角色权限或多租户前置；采用明确的共享部署边界，个人工作台不代表个人私有数据空间。
- 语音模型本地推理；工作台的可选 LLM 默认关闭，仅接入部署批准的本机 / 内网服务。音频、转写、书稿、声音参考及派生内容不得出网，失败不回退公有云。完整出站限制与 LLM 集成仍待实现。
- 采用硬件无关的能力与容量声明，在实际部署验证质量、并发、故障恢复和服务指标。

首选验证矩阵包括 Qwen3-ASR、Qwen3-TTS 的 CustomVoice / VoiceDesign / Base、Silero VAD、Qwen3-ForcedAligner 和 Nemotron-3-Diarization。已启用的实时绑定预加载并常驻，VoiceDesign 按需加载；音色设计须经过 Base 复用校验后显式发布。模型分工、辅助运行时、资源预算与验收要求见 [ADR 0002](docs/decisions/0002-speech-models-engines-and-loading.md)，这些是已接受的设计，尚未完成适配或真实推理验证。

## 设计与路线

- [产品与架构设计](docs/speechnexus-product-architecture.md)：兼容与增强协议、音色创作、两类 WebUI、场景预设、首期访问边界和资源调度。
- [整体技术方案](docs/architecture/technical-blueprint.md)：内部执行 / 部署控制协议、事务与迁移、契约类型生成、Web 状态、观测、发布和工程交接。
- [工程目录与依赖边界](docs/architecture/repository-layout.md)：当前 workspace、分层方向、契约流程、测试层级和验证命令。
- [已确认产品定位与身份范围](docs/decisions/0001-product-boundaries-and-authentication-scope.md)。
- [语音模型、推理引擎与加载策略](docs/decisions/0002-speech-models-engines-and-loading.md)。
- [工作台 LLM 与业务内容的网络边界](docs/decisions/0003-workbench-llm-network-boundary.md)。
- [分阶段实施计划](docs/plans/2026-10-22-speechnexus-implementation-plan.md)：P0 契约、P1 独立语音底座、P2 两类 WebUI、P3 实时应用、P4 有声书、P5 生产验收；身份体系后续独立立项。
- [方案审查记录](docs/audits/2026-10-22-speechnexus-design-review.md)：问题裁决、上游依据、文档核验与待实测项。
- [开发与测试环境](docs/development-environment.md)：本机工具链、独立数据库与对象存储、媒体和浏览器自检、启动命令。
- [开发环境就绪审查](docs/audits/2026-10-22-development-environment-readiness.md)：已验证能力、隔离边界与推理环境剩余缺口。
- [部署初始化](deploy/README.md)：Compose 开发拓扑、端口、配置权威与边界。
- [文档入口](docs/README.md)。

SpeechNexus 由 [SpeechRail](https://github.com/hrygo/SpeechRail) 的产品与架构经验启发，作为独立项目演进。代码复用需按[复用边界](docs/architecture/speechrail-reuse.md)和原始许可证逐项核对。
