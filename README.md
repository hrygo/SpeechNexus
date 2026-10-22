# SpeechNexus

[English](README.en.md)

SpeechNexus 是面向企业私有化部署的语音平台，规划通过 WebUI、REST、WebSocket 与 MCP 提供实时和非实时 ASR/TTS，并支持语音助手、会议助手、提词器、实时字幕与有声书制作。

当前阶段：**设计与仓库初始化**。本仓库包含产品与架构提案，尚未提供可运行的服务或 WebUI；设计中的接口、性能和容量指标不代表已经实现或验证。

## 产品方向

- 统一实时与非实时识别、合成的执行语义、结果和错误模型。
- 使用场景参数预设，在模型能力与企业策略边界内适配业务。
- 提供企业身份、工作空间权限、数据隔离、配额、资源调度与审计。
- 以 Linux 上的 vLLM / vLLM-Omni 为主要推理后端，通过适配器隔离模型差异。
- 采用硬件无关的能力与容量声明，在目标部署验证并发和服务指标。

## 设计与路线

- [产品与架构设计](docs/specs/2026-10-22-speechnexus-product-architecture-design.md)：统一接口、实时事件、8 套场景预设、权限、调度、持久任务和业务工作流。
- [分阶段实施计划](docs/specs/2026-10-22-speechnexus-product-architecture-design.md#15-分阶段实施计划)：P0 契约验证、P1 平台底座、P2 基础工作台、P3 实时应用、P4 有声书、P5 生产验收。
- [文档入口](docs/README.md)。

SpeechNexus 由 [SpeechRail](https://github.com/hrygo/SpeechRail) 的产品与架构经验启发，作为独立项目演进。代码复用需按设计中的复用矩阵和原始许可证逐项核对。
