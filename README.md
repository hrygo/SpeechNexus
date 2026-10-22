# SpeechNexus

**自部署的语音服务与创作工作台**

[English](README.en.md) · [功能与场景](#功能与场景) · [快速开始](#快速开始) · [文档](docs/README.md) · [反馈与建议](https://github.com/hrygo/SpeechNexus/issues)

[![CI](https://github.com/hrygo/SpeechNexus/actions/workflows/ci.yml/badge.svg)](https://github.com/hrygo/SpeechNexus/actions/workflows/ci.yml)

SpeechNexus 面向希望在自有 Linux 环境中部署语音能力的团队，围绕 **语音服务 API、AdminUI 和个人语音工作台**组织产品。它的目标是把语音识别、语音合成、音色创作和日常语音任务连接起来：开发者接入 API，使用者在浏览器中完成任务，维护者管理服务与资源。

> **项目处于早期开发阶段。** 当前可启动 AdminUI 与工作台界面预览，并调用服务健康检查；转写、合成、音色创作和工作台业务尚未开放。下文介绍规划中的产品能力，真实模型与生产部署仍待验证。

## 产品定位

SpeechNexus 希望服务于三类需求：

- **为应用接入语音能力**：通过统一 API 使用文件与实时语音识别、语音合成和音色资源，底座可独立于 WebUI 与 LLM 部署。
- **在浏览器中完成语音工作**：围绕转写、配音、会议、字幕和有声书组织采集、编辑、试听、续作与导出。
- **在自有环境中维护服务**：集中管理模型服务、场景预设、共享音色、任务与资源，采用本地语音推理和可选内网 LLM 的部署路线。

## 功能与场景

| 功能           | 你可以用它做什么                                                   |
| -------------- | ------------------------------------------------------------------ |
| 语音转写       | 上传录音或接入实时音频，获得可校对的文字、字幕及可选说话人标注     |
| 语音合成       | 将文本转换为语音，使用预设或已发布音色，支持文件生成与实时输出     |
| 音色创作       | 通过文字描述设计音色，或从参考样本克隆；经过试听、校验后发布并复用 |
| 会议与实时字幕 | 实时查看转写、标记要点、保存记录，并通过可选内网 LLM 生成会议纪要  |
| 语音助手与提词 | 结合识别、文本生成与合成组织语音交互，或让提词稿跟随讲话位置       |
| 有声书制作     | 导入书稿、整理章节与角色、分配声音，按章生成、局部重做和导出       |

长任务规划支持进度查询、取消、恢复和结果下载；场景预设用于复用转写、复核和合成配置。详细范围见[产品设计](docs/speechnexus-product-architecture.md)。

## 使用方式

### 通过工作台完成任务

工作台按任务组织操作。业务功能开放后的典型流程为：

- **录音转写**：上传音频 → 选择语言与预设 → 查看任务进度 → 校对文字 → 导出转写或字幕。
- **文本配音**：输入文本 → 选择声音 → 生成并试听 → 调整内容 → 导出音频。
- **音色创作**：提交描述或参考样本 → 生成候选 → 试听与复用校验 → 确认发布 → 用于后续配音。

### 通过 API 集成到应用

语音底座规划提供 OpenAI Audio 兼容子集，以及音色、持久任务和场景预设等增强接口，适合接入已有应用、SDK 或 MCP 调用方。实时接口只支持明确声明的兼容子集，并需要 SpeechNexus 会话协商。

当前可调用的是 `/healthz` 与 `/readyz`。语音调用方式和示例见[语音接口设计](docs/architecture/speech-api.md)，业务协议仍待冻结。

### 通过 AdminUI 维护部署

AdminUI 面向部署维护者，规划提供服务概览、模型与音色管理、预设配置、资源预算、全局任务和诊断入口。工作台与 AdminUI 分别组织使用任务和维护任务。

## 快速开始

使用 Docker Compose 预览当前版本。需要安装 Git、Docker 和 Compose 插件。

### 1. 获取代码并准备配置

```bash
git clone https://github.com/hrygo/SpeechNexus.git
cd SpeechNexus
cp deploy/compose.env.example deploy/compose.env
```

编辑 `deploy/compose.env`，替换 PostgreSQL 与 MinIO 的示例密码。若已启用本机开发环境，先运行 `make dev-down`，避免数据库与对象存储端口冲突。

### 2. 构建并启动

```bash
docker compose --env-file deploy/compose.env -f deploy/compose.yaml up --build -d
```

启动后访问：

| 入口           | 地址                            | 当前可体验内容               |
| -------------- | ------------------------------- | ---------------------------- |
| AdminUI        | <http://127.0.0.1:8080>         | 管理端界面预览与能力状态说明 |
| 个人语音工作台 | <http://127.0.0.1:8081>         | 工作台界面预览与任务范围说明 |
| 语音服务       | <http://127.0.0.1:8000/healthz> | 服务存活检查                 |

当前启动过程不加载语音模型。可进一步检查基础依赖是否就绪：

```bash
curl --fail http://127.0.0.1:8000/readyz
```

### 3. 停止服务

```bash
docker compose --env-file deploy/compose.env -f deploy/compose.yaml down
```

该命令保留数据库与对象存储的数据卷。配置项、完整端口和部署边界见[部署说明](deploy/README.md)。

## 部署与数据说明

首期面向可信网络内的共享部署，没有登录、角色权限或多租户隔离。**同一部署的数据由使用者共享**；“个人工作台”表示任务体验，不表示个人私有空间。访问范围由部署方的网络与入口控制。

产品设计要求语音模型本地推理，业务音频、转写、书稿和声音参考及其派生内容不出网。助手与纪要使用的 LLM 为可选集成，默认关闭，仅允许部署批准的本机 / 内网服务，不回退公有云。LLM 集成和完整出站限制仍待实现，详见[网络边界说明](docs/decisions/0003-workbench-llm-network-boundary.md)。

## 路线图

- [x] 可启动的服务、健康检查和两个 Web 入口预览。
- [ ] 文件与实时语音识别、语音合成及模型接入。
- [ ] 音色设计、克隆、校验、发布与持久任务。
- [ ] 工作台转写、配音和管理端业务功能。
- [ ] 会议、字幕、助手、提词与有声书工作流。
- [ ] 真实模型质量、容量、故障恢复与生产部署验证。

具体阶段与依赖见[实施计划](docs/plans/2026-10-22-speechnexus-implementation-plan.md)；模型与推理引擎的首选验证路线见[模型选型说明](docs/decisions/0002-speech-models-engines-and-loading.md)。

## 文档

| 我想了解                   | 阅读入口                                             |
| -------------------------- | ---------------------------------------------------- |
| 产品范围与完整工作流       | [产品设计](docs/speechnexus-product-architecture.md) |
| API 兼容范围与规划调用方式 | [语音接口设计](docs/architecture/speech-api.md)      |
| 部署、端口与环境配置       | [部署说明](deploy/README.md)                         |
| 本机开发与测试             | [开发环境](docs/development-environment.md)          |
| 架构与工程实现             | [技术方案](docs/architecture/technical-blueprint.md) |
| 全部设计与决策记录         | [文档索引](docs/README.md)                           |
| 集群架构与单机 MVP | [集群就绪与部署决策](docs/decisions/0004-cluster-ready-architecture-and-single-node-mvp.md) |

## 参与贡献

欢迎通过 [Issues](https://github.com/hrygo/SpeechNexus/issues) 报告问题、提出使用场景或讨论功能，也欢迎提交 Pull Request。问题报告请包含运行环境、复现步骤和预期行为；请勿附带凭据或私人音频。

本机开发使用 Python 3.14 / uv 与 Node 26 / pnpm 12，准备步骤见[开发环境](docs/development-environment.md)。提交前运行与改动范围匹配的检查，完整本机检查命令为：

```bash
make verify
```

工程约束与验证要求见 [AGENTS.md](AGENTS.md) 和[工程目录说明](docs/architecture/repository-layout.md)。

## 许可证与致谢

当前仓库尚未提供项目 `LICENSE` 文件。模型权重、第三方依赖与声音素材的许可需分别核对。

SpeechNexus 受 [SpeechRail](https://github.com/hrygo/SpeechRail) 的产品与架构经验启发，作为独立项目演进。来源与复用要求见[SpeechRail 复用边界](docs/architecture/speechrail-reuse.md)。
