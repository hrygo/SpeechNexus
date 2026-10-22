# SpeechNexus

[English](README.en.md)

SpeechNexus 的后端定位为企业级语音服务底座，以 Linux 上的 vLLM / vLLM-Omni 为主要推理后端，提供 OpenAI 兼容的实时与非实时 ASR/TTS 子集，以及音色创作等增强协议。WebUI 分为 **AdminUI** 与 **个人语音工作台**，面向部署维护和不同语音任务需求。

当前阶段：**设计与仓库初始化**。尚未提供可运行服务或 WebUI；设计中的接口、性能与容量目标未经实现验证。

## 产品方向

- 底座可独立部署，供 SDK、MCP 和外部应用调用，不依赖 WebUI 或 LLM。
- 标准兼容接口之外，提供音色设计、克隆、试听、校验与版本发布，以及预设、持久任务、资源调度和诊断。
- AdminUI 组织模型服务、预设、资源池、任务和运维；个人工作台组织转写、配音、音色创作、会议、助手、提词、字幕与有声书。
- **首期不建设用户身份认证体系**，无登录、用户目录、角色权限或多租户前置；采用明确的共享部署边界，个人工作台不代表个人私有数据空间。
- 采用硬件无关的能力与容量声明，在实际部署验证质量、并发、故障恢复和服务指标。

## 设计与路线

- [产品与架构设计](docs/speechnexus-product-architecture.md)：兼容与增强协议、音色创作、两类 WebUI、场景预设、首期访问边界和资源调度。
- [已确认产品定位与身份范围](docs/decisions/0001-product-boundaries-and-authentication-scope.md)。
- [分阶段实施计划](docs/plans/2026-10-22-speechnexus-implementation-plan.md)：P0 契约、P1 独立语音底座、P2 两类 WebUI、P3 实时应用、P4 有声书、P5 生产验收；身份体系后续独立立项。
- [方案审查记录](docs/audits/2026-10-22-speechnexus-design-review.md)：问题裁决、上游依据、文档核验与待实测项。
- [文档入口](docs/README.md)。

SpeechNexus 由 [SpeechRail](https://github.com/hrygo/SpeechRail) 的产品与架构经验启发，作为独立项目演进。代码复用需按[复用边界](docs/architecture/speechrail-reuse.md)和原始许可证逐项核对。
