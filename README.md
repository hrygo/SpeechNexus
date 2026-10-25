# SpeechNexus · 声枢

![SpeechNexus · 声枢 — 自部署的语音服务与创作中枢](web/shared/src/assets/brand-banner.svg)

**让录音变成文字，让文字拥有声音，在自有环境中完成语音工作。**

[English](README.en.md) · [功能与场景](#功能与场景) · [当前可用范围](#当前可用范围) · [快速开始](#快速开始) · [文档](#文档) · [反馈与建议](https://github.com/hrygo/SpeechNexus/issues)

[![CI](https://github.com/hrygo/SpeechNexus/actions/workflows/ci.yml/badge.svg)](https://github.com/hrygo/SpeechNexus/actions/workflows/ci.yml)
[![Inference: vLLM](https://img.shields.io/badge/Inference-vLLM-FF6B35)](https://github.com/vllm-project/vllm)
[![Inference: vLLM-Omni](https://img.shields.io/badge/Inference-vLLM--Omni-FF8F35)](https://github.com/vllm-project/vllm-omni)
[![ASR: Qwen3-ASR](https://img.shields.io/badge/ASR-Qwen3--ASR-615CED)](https://github.com/QwenLM/Qwen3-ASR)
[![TTS: Qwen3-TTS](https://img.shields.io/badge/TTS-Qwen3--TTS-8B5CF6)](https://github.com/QwenLM/Qwen3-TTS)
[![Docker Compose](https://img.shields.io/badge/deploy-Docker_Compose-2496ED?logo=docker&logoColor=white)](deploy/README.md)

SpeechNexus 是面向自有 Linux 环境的语音服务与创作中枢。它将语音识别、语音合成与音色创作连接到日常工作：使用者在浏览器中转写、配音和整理内容，开发者通过 API 接入语音能力，维护者集中管理模型、资源与任务。

> **早期开发中，当前适合界面预览与开发评估。** 默认启动不加载语音模型，尚不能直接完成真实录音转写或配音。部分页面和服务模块已接入并单独验证，完整语音流程与生产部署仍待联调验收。以下场景描述产品目标，具体可用性见[当前可用范围](#当前可用范围)。

## 为什么选择 SpeechNexus

- **在自有环境中处理内容**：面向本地语音推理与内网部署，音频、转写、书稿和声音参考的处理遵循部署网络边界。
- **围绕任务完成创作**：工作台将采集、编辑、试听、续作和导出组织在同一条工作流中。
- **也能成为应用的语音底座**：API 面向独立集成，语音服务可独立于 WebUI、工作台业务和 LLM 运行。

产品由三个入口组成：**语音工作台**服务于内容创作，**AdminUI**服务于部署维护，**语音 API**服务于应用集成。它们的功能范围与当前交付状态分别见下文。

## 功能与场景

以下为目标工作流，尚未全部开放。

| 你想完成的事 | 目标体验与产物 |
| --- | --- |
| 将录音整理成文字 | 上传音频，识别后校对，导出转写或字幕；按模型能力提供说话人标注 |
| 为文稿配音 | 输入文本、选择声音，生成并试听，调整后导出音频 |
| 创建可复用声音 | 通过描述设计或参考样本克隆，经过试听、校验和确认发布后用于配音 |
| 记录会议与实时字幕 | 实时查看文字、标记要点、保存记录；可选内网 LLM 辅助生成纪要 |
| 使用语音助手或提词 | 进行语音交互，或让提词稿跟随讲话位置 |
| 制作有声书 | 整理章节与角色、分配声音、按章生成、局部重做并导出 |

例如，录音转写的目标路径是：**上传录音 → 选择语言与预设 → 查看进度 → 校对文字 → 导出结果**。配音则从文本和声音选择开始，通过试听与局部调整完成交付。

完整范围见[产品设计](docs/speechnexus-product-architecture.md)。

## 模型与推理引擎

首选 **Qwen3 语音模型家族**，分别承载识别、配音与声音创作；通过 **vLLM / vLLM-Omni** 提供推理服务。

| 模型与引擎选型 | 面向的体验 |
| --- | --- |
| **Qwen3-ASR · vLLM** | 0.6B 用于实时识别路线，1.7B 用于文件转写与完整段复核 |
| **Qwen3-TTS · vLLM-Omni** | CustomVoice 负责预设声音配音，VoiceDesign 根据描述设计声音，Base 负责样本克隆与创作音色复用 |
| **Silero VAD · ONNX Runtime CPU** | 检测人声并切分音频，为识别准备有效输入 |

以上为已确定的选型路线：ASR、VAD 与 CustomVoice TTS 已有局部实测，完整业务接入与质量、恢复及集群资格仍待验证。首页徽章用于展示选型，不代表默认启动即具备这些能力。模型分工与验证要求见[模型与引擎说明](docs/decisions/0002-speech-models-engines-and-loading.md)，TTS 局部证据见[文件 TTS](docs/architecture/file-tts.md)。

## 当前可用范围

以下是截至 **2026-10-25** 的仓库交付摘要；单独验证的模块不代表默认部署已具备完整能力。

| 范围 | 当前状态 | 体验前需要了解 |
| --- | --- | --- |
| 默认 Compose 启动 | 两个 Web 入口、语音服务及基础依赖健康检查 | 可预览界面；不加载模型，不提供真实语音闭环 |
| 文件转写、配音与任务页面 | 已接入公开 API 消费流程 | 真实语音执行、结果生成与完整部署仍待联调 |
| 网关文件 ASR 装配 | 私有配置可将既有持久 worker 接到固定代次端点、CPU VAD 与对象存储 | 已验证隔离存储和回环 HTTP；真实模型默认运行、停止/发布与公开链仍待验收，见[配置说明](docs/architecture/file-asr-runtime.md) |
| 项目、稿件与会议记录 | 页面及独立工作台服务已有验证 | 需要独立配置和数据库准备，默认 Compose 尚未完整装配 |
| 场景预设草稿 | Admin 页面支持完整配置创建、固定修订读取、显式确认和原键恢复，已有 HTTP/PG/浏览器验证 | 需要显式配置预设写入服务；发布、激活与退役尚未接入 |
| 实时字幕、实时配音与音色创建 | 已有页面消费者，使用合成服务验证交互 | 真实服务与模型待接入；创建任务不等于发布可复用音色 |
| 素材上传与下载 | 显式配置后可用，已验证 UTF-8 文本处理 | 默认未启用对象存储凭据；音频检查待接入 |
| 真实模型与生产运行 | ASR、VAD、CustomVoice TTS 已有局部探针与模块验证 | 完整识别/合成、质量、容量、恢复与集群运行仍待验收 |

工程实现与验证证据见[架构文档](docs/architecture/README.md)、[实施跟踪](docs/plans/2026-10-23-implementation-tracking.md)和[审查记录](docs/audits/README.md)。

## 快速开始

使用 Docker Compose 预览当前版本。需要 Git、Docker 和 Compose 插件；本节启动单节点预览环境。

### 1. 获取代码并准备配置

```bash
git clone https://github.com/hrygo/SpeechNexus.git
cd SpeechNexus
cp deploy/compose.env.example deploy/compose.env
```

编辑 `deploy/compose.env`，替换 PostgreSQL 与 MinIO 的示例密码。若已启动本项目的原生开发服务，先运行 `make dev-down`，避免端口冲突。

### 2. 构建并启动

```bash
docker compose --env-file deploy/compose.env -f deploy/compose.yaml up --build -d
```

等待服务启动后，在浏览器中打开：

| 入口 | 地址 | 当前用途 |
| --- | --- | --- |
| 语音工作台 | <http://127.0.0.1:8081> | 预览转写、配音等任务页面与能力状态 |
| AdminUI | <http://127.0.0.1:8080> | 预览部署管理界面与能力说明 |
| 语音服务 | <http://127.0.0.1:8000/healthz> | 检查服务是否存活 |

检查基础依赖是否就绪：

```bash
curl --fail http://127.0.0.1:8000/readyz
```

`readyz` 成功表示基础依赖就绪，不表示模型已加载或转写、配音已可用。页面显示未配置或准备中时，请结合[当前可用范围](#当前可用范围)判断；仅完成上述启动不会启用真实语音能力。

若启动失败，先查看服务状态与日志；分享日志前移除凭据和业务内容：

```bash
docker compose --env-file deploy/compose.env -f deploy/compose.yaml ps
docker compose --env-file deploy/compose.env -f deploy/compose.yaml logs --tail=100 api
```

### 3. 停止服务

```bash
docker compose --env-file deploy/compose.env -f deploy/compose.yaml down
```

该命令保留数据库与对象存储的数据卷。默认发布端口仅绑定本机回环地址；配置、完整端口及额外模块的启用条件见[部署说明](deploy/README.md)。

## 将语音能力接入应用

SpeechNexus 的接口设计采用 **OpenAI Audio 兼容子集 + `nexus` 扩展**：标准子集用于识别与合成，扩展用于音色、持久任务和场景预设。兼容范围以明确声明的接口与事件为准。

相关服务实现并启用后，标准 OpenAI SDK 可将 `base_url` 指向部署的 `/v1`。目前公共语音流程仍在实施，契约和调用示例用于开发对接，不能视为默认部署已支持全部接口。

- [语音接口设计](docs/architecture/speech-api.md)：接口、事件和调用方式。
- [OpenAI 兼容边界](docs/architecture/openai-compatibility.md)：支持的子集与扩展规则。
- [机器契约](contracts/README.md)：供实现与消费者共同校验的协议定义。

## 部署与数据边界

**首期面向可信网络内的共享部署，没有登录、角色权限或多租户隔离。** 同一部署的数据由使用者共享；“个人语音工作台”描述使用体验，不代表个人私有空间。工作台与 AdminUI 的入口划分也不构成权限隔离，访问范围由部署方控制。

产品要求语音模型本地推理，业务音频、转写、书稿、声音参考及派生内容留在部署网络内。助手与纪要的 LLM 集成可选、默认关闭，仅允许部署批准的本机或内网服务，不回退公有云。完整 LLM 集成与出站限制仍待验收，详见[网络边界说明](docs/decisions/0003-workbench-llm-network-boundary.md)。

显式启用素材服务后，新素材默认保留 **7 天**，可通过部署配置或上传时指定保留期；修改默认值不影响已有素材的到期时间。持久存储、备份与清理配置见[素材服务说明](docs/architecture/production-assets.md)。

## 后续方向

- 打通真实文件转写与文本配音，从提交任务到结果下载。
- 完善音色设计、克隆、试听、校验与发布闭环。
- 接通实时字幕、实时配音和会议保存，再完善助手、提词与有声书。
- 完成管理端业务、资源治理，以及真实模型质量、恢复和生产部署验收。

阶段与依赖见[实施计划](docs/plans/2026-10-22-speechnexus-implementation-plan.md)，模型与引擎验证路线见[模型选型说明](docs/decisions/0002-speech-models-engines-and-loading.md)。

## 文档

| 我想了解 | 阅读入口 |
| --- | --- |
| 产品范围与工作流 | [产品设计](docs/speechnexus-product-architecture.md) |
| 部署、端口与配置 | [部署说明](deploy/README.md) |
| API 集成与兼容范围 | [语音接口](docs/architecture/speech-api.md) |
| 开发环境与验证 | [开发指南](docs/development-environment.md) |
| 架构与模块边界 | [技术方案](docs/architecture/technical-blueprint.md) |
| 全部设计、计划与证据 | [文档索引](docs/README.md) |

## 反馈与贡献

欢迎通过 [Issues](https://github.com/hrygo/SpeechNexus/issues) 提出使用场景、功能建议或问题。报告问题时请说明运行环境、复现步骤、预期行为和实际结果，避免附带凭据、私人音频或完整业务转写。

参与开发请先阅读[开发指南](docs/development-environment.md)与 [AGENTS.md](AGENTS.md)，按改动范围运行检查。完整本机验证入口为 `make verify`，运行前需满足指南中的宿主与隔离服务条件。

## 许可证与致谢

当前仓库尚未提供项目 `LICENSE` 文件。模型权重、第三方依赖与声音素材的许可需分别核对。

SpeechNexus 受 [SpeechRail](https://github.com/hrygo/SpeechRail) 的产品与架构经验启发，作为独立项目演进。来源与复用要求见[SpeechRail 复用边界](docs/architecture/speechrail-reuse.md)。
