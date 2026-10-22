# SpeechNexus

[English](README.en.md)

SpeechNexus 的后端定位为企业级语音服务底座，以 Linux 上的 vLLM / vLLM-Omni 为主要推理后端，提供 OpenAI 兼容的实时与非实时 ASR/TTS 子集，以及音色创作等增强协议。WebUI 分为 **AdminUI** 与 **个人语音工作台**，面向部署维护和不同语音任务需求。

当前阶段：**工程骨架初始化**。已建立可构建的 FastAPI 服务 / 执行网关、AdminUI 与工作台入口外壳，以及 PostgreSQL / 对象存储就绪检查。语音、音色、Job、实时与工作台业务协议仍待 P0 冻结，设计中的性能与容量目标未经实现验证。

## 快速开始

```bash
make dev-bootstrap
make verify
```

`make dev-bootstrap` 初始化隔离数据库、对象存储、锁定的 Python / Node 依赖和浏览器。已有开发服务时可直接运行 `make install && make verify`。基础服务与 Web 入口需要分别占用一个终端：

```bash
make api-dev
```

```bash
make gateway-dev
```

```bash
make web-dev
```

`make stack-config` 校验 Compose 开发拓扑；完整运行说明见[部署入口](deploy/README.md)。当前可访问的是 `/healthz`、`/readyz` 和两个界面外壳，不是语音业务 API。

## 工程基线

- Python 3.12 使用 `uv` workspace，后端与应用网关分别拥有 `src/` 包、严格类型检查、分层依赖契约和独立入口。
- React 19 + TypeScript 6 + Vite 8 使用 `pnpm` workspace；AdminUI、工作台与共享组件分别构建，版本集中在 catalog。
- 公共契约位于 `contracts/`；当前只冻结运营健康检查，不提前伪造尚未评审的语音协议。
- 单元、契约、fake 与集成测试目录已分离；CI 执行 Python、前端和 Compose 配置门禁。
- 首期无登录、用户目录、RBAC 或多租户。界面与文档明确数据由当前部署共享。

## 产品方向

- 底座可独立部署，供 SDK、MCP 和外部应用调用，不依赖 WebUI 或 LLM。
- 标准兼容接口之外，提供音色设计、克隆、试听、校验与版本发布，以及预设、持久任务、资源调度和诊断。
- AdminUI 组织模型服务、预设、资源池、任务和运维；个人工作台组织转写、配音、音色创作、会议、助手、提词、字幕与有声书。
- **首期不建设用户身份认证体系**，无登录、用户目录、角色权限或多租户前置；采用明确的共享部署边界，个人工作台不代表个人私有数据空间。
- 采用硬件无关的能力与容量声明，在实际部署验证质量、并发、故障恢复和服务指标。

## 设计与路线

- [产品与架构设计](docs/speechnexus-product-architecture.md)：兼容与增强协议、音色创作、两类 WebUI、场景预设、首期访问边界和资源调度。
- [工程目录与依赖边界](docs/architecture/repository-layout.md)：当前 workspace、分层方向、契约流程、测试层级和验证命令。
- [已确认产品定位与身份范围](docs/decisions/0001-product-boundaries-and-authentication-scope.md)。
- [分阶段实施计划](docs/plans/2026-10-22-speechnexus-implementation-plan.md)：P0 契约、P1 独立语音底座、P2 两类 WebUI、P3 实时应用、P4 有声书、P5 生产验收；身份体系后续独立立项。
- [方案审查记录](docs/audits/2026-10-22-speechnexus-design-review.md)：问题裁决、上游依据、文档核验与待实测项。
- [开发与测试环境](docs/development-environment.md)：本机工具链、独立数据库与对象存储、媒体和浏览器自检、启动命令。
- [开发环境就绪审查](docs/audits/2026-10-22-development-environment-readiness.md)：已验证能力、隔离边界与推理环境剩余缺口。
- [部署初始化](deploy/README.md)：Compose 开发拓扑、端口、配置权威与边界。
- [文档入口](docs/README.md)。

SpeechNexus 由 [SpeechRail](https://github.com/hrygo/SpeechRail) 的产品与架构经验启发，作为独立项目演进。代码复用需按[复用边界](docs/architecture/speechrail-reuse.md)和原始许可证逐项核对。
