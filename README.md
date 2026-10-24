# SpeechNexus · 声枢

![SpeechNexus · 声枢 — 自部署的语音服务与创作中枢](web/shared/src/assets/brand-banner.svg)

**自部署的语音服务与创作中枢**

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

语音底座已冻结 P0 契约：OpenAI Audio 兼容子集，以及音色、持久任务和场景预设等 `nexus` 增强接口，适合接入已有应用、SDK 或 MCP 调用方。标准 OpenAI SDK 可将 `base_url` 指向部署的 `/v1`；实时接口支持 `model` 直连的仅转写兼容模式和预创建的 nexus 会话，两者都只覆盖明确声明的事件子集。

当前提供 `/healthz`、`/readyz`，以及配置后可用的 Asset 上传、元数据、内容下载和共享 Workspace GET/游标列表接口；首批检查实际 UTF-8 文本，音频检查待 B03 接入，其他语音与工作台路由继续实施。新资产默认保留 7 天，可由部署配置或上传时显式 TTL 覆盖；已有资产到期时间不随默认值变化。调用协议、兼容范围和示例见[语音接口设计](docs/architecture/speech-api.md)与[OpenAI 兼容与增强边界](docs/architecture/openai-compatibility.md)，机器契约见[contracts](contracts/README.md)。

契约修复补全了规范执行命令、同步幂等恢复、严格 receipt 与数据删除生命周期；范围及升级边界见 [ADR 0005](docs/decisions/0005-complete-execution-and-data-lifecycle-contracts.md)。这些业务协议尚未实现为运行时路由。

集群支持保持完整；[ADR 0006](docs/decisions/0006-cluster-coordination-and-storage-portability.md)确定不采用 etcd、以受控应用事务降低数据库绑定，PostgreSQL 为唯一正式交付数据库，MySQL/InnoDB 仅作关键协调验证。已交付[精简 DDL、应用事务方案与契约](docs/architecture/coordination-transactions.md)，共享测试在真实 PG/InnoDB 上验证协调原型；生产业务接入和完整集群故障资格仍待完成，进度见[重构计划](docs/plans/2026-10-23-cluster-coordination-refactor.md)。

[首个生产协调单元](docs/architecture/production-jobs.md)已实现 Job/outbox 原子创建、完整身份幂等与未知提交只读恢复，含 PG adapter、最低角色和版本化迁移；它尚未接入公开业务路由，不代表完整公开 Job 或文件语音闭环已实现。

[`0002_coordination` 生产存储基线](docs/architecture/production-coordination-storage.md)已补齐 15 张协调表及 A/B 最小权限，保留既有 Job/outbox 数据。其余调度、准入、网关事务与真实运行资格仍按所有权分别实现和验收。

[生产 owner 任期事务](docs/architecture/production-owners.md)已提供获取、续约、激活和当前权威查询，覆盖锁后数据库取时、完整快照及未知提交恢复；完整调度循环仍待接入，独立 runtime 与配方准入见下文。

[运行时意图事务](docs/architecture/production-runtime-intents.md)已提供独立 create/query/observe 端口：同事务预留完整 peak，核验受信 warmup/资格后转 steady，确认停止且清理完成后才归还基线；PG/InnoDB 合成事实验证不代表真实 GPU 资格。原意图持久投递已由独立 worker 消费；批准计划/资格提供者与完整调度/观察循环仍待接入，没有新增公开路由。

[固定节点 HTTPS 客户端](docs/architecture/runtime-controller-client.md)提供有界、证书校验的原 runtime 意图提交/查询，复用同源生成的 HTTP 校验；超时只查原身份，HTTP ready 不生成资格。B 的 GET/精确重放只读持久快照，实际推进仍需节点内观察生产者；[持久投递 worker](docs/architecture/production-runtime-delivery.md)已装配原请求、租约恢复与投递确认；TLS 部署和完整控制循环仍待接入。

[租约隔离与释放事务](docs/architecture/production-leases.md)提供独立 `quarantine_lease`、`release_lease` 和只读租约查询：未知占用移入隔离并保持总量，核对网关持久停止/释放或未外发事实后才归还当前 lease 增量，最后阶段才归还 attempt 配额。实例基线仍由 runtime 停止流程处理；独立准入共用同一预算权威，实际调度消费者仍待接入。

[`0007` 准入命令存储](docs/architecture/production-admission-storage.md)新增 A 私有的 operation 原命令与每个 attempt 的完整准入请求记录，区分业务与准入摘要，保留删除 tombstone 形状；旧记录不伪造正文。存储已由后续独立准入事务消费；命令清理和公开 Operation/receipt 仍待接入。

[完整配方准入独立事务](docs/architecture/production-admission.md)提供 `admit_recipe` 与原 attempt 查询，同事务保存私有原命令、全部阶段租约、六维/槽位/并发 attempt 配额和 Job/outbox；未知只查原身份。当前仅支持明确的并发配额策略，其他业务配额账本、原命令解析/资格 provider、调度循环和公开 Operation/receipt 尚未装配。

[执行网关](docs/architecture/execution-gateway.md)已提供持久提交/停止/未知隔离、受控执行观察、受信命令正文清理与输出区间追加/原事实查询事务，以及显式配置后启用的认证 HTTP 入口屏障与只读执行查询。真实回环 HTTP 与 PG 联调已验证；未知恢复、观察、正文清理、区间去重及无外发清理保护也经 PG/InnoDB 合成验证。真实计量采集/消费、删除许可签发、提交/中止路由、真实引擎和跨宿主资格仍待交付，默认 Compose 尚未启用该控制入口。

[受限节点控制器](docs/architecture/node-controller.md)已有 HTTP/SQLite/Docker 实现，批准模板固定容器内存上限和 HTTP 健康判据。Qwen3-ASR 0.6B/1.7B 的真实加载、合成语音和停止清理已有[本机证据](docs/audits/2026-10-24-b02-real-asr-controller-probe.md)；静音幻觉、长非语音慢响应、TTS、A 预算控制循环和集群资格仍待解决，不代表公开语音链路已可用。

[CPU VAD](docs/architecture/cpu-vad.md)已实现会话状态隔离、前缀缓冲与版本化有界切段。固定 ONNX CPU 模型的[合成探针](docs/audits/2026-10-24-b06-cpu-vad-probe.md)能切出人声，并使静音/正弦音不产生 ASR 候选段；公开实时入口、调度链路中的 ASR 外发及真实录音质量仍待集成和验收。

[文件媒体输入](docs/architecture/file-asr-audio.md)已提供五种格式的有界解码/重采样、源样本时钟与文件 VAD 组合接口。[本机探针](docs/audits/2026-10-24-b03-bounded-audio-probe.md)通过合成媒体及两段授权真实录音；它只证明输入处理完成，音频 Asset 检查接入、识别质量、ASR/TTS 服务及资源硬隔离仍待验收。

[文件识别原生接口](docs/architecture/file-asr-recognition.md)已将媒体/VAD 接到固定内网 vLLM ASR，校验请求身份、结束原因和实际 token 上限，拒绝截断结果。0.6B/1.7B 已完成合成和授权真实录音调用；公开 ASR、持久派发、结果发布与完整质量/性能资格仍待接入。

[Job 认领](docs/architecture/production-jobs.md#worker-认领与当前权威查询)已提供带版本和到期条件的 worker 认领与只读当前权威查询；重新认领不授予再次提交引擎的权限，完整 worker 循环和制品发布 fencing 继续实施。

[资产发布存储单元](docs/architecture/production-assets.md)已提供真实文本校验、PG/版本化 MinIO 发布、原身份恢复和保留期删除账本；已接公开上传/下载与独立维护入口；音频检查依赖 B03，Artifact、Operation/receipt 等后续接入。

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
