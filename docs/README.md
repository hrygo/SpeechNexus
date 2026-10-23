# SpeechNexus 文档

[产品与架构设计](speechnexus-product-architecture.md)是本独立项目的主文档，当前文档版本为 0.5.0。设计规定目标行为；仓库已建立可构建的 FastAPI / 执行网关与两个 Web 入口外壳，并交付 P0 公共、管理与内部机器契约，但尚未实现语音或工作台业务。契约冻结、schema 校验和 OpenAI SDK 形状测试不等于服务端路由或真实模型已可用。文档版本独立于软件版本。

| 文档                                                                                                | 职责                                                                            |
| --------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------- |
| [产品与架构设计](speechnexus-product-architecture.md)                                               | 产品范围、AdminUI / 工作台、领域与模块、场景预设、共享访问、部署与验收          |
| [整体技术方案](architecture/technical-blueprint.md)                                                 | 技术基线、内部协议、执行配方、事务 / 迁移、Web / 内网 LLM、观测、发布与工程交接 |
| [工程目录与依赖边界](architecture/repository-layout.md)                                             | Python / Web workspace、模块方向、契约生命周期、测试层级与部署入口              |
| [语音接口](architecture/speech-api.md)                                                              | OpenAI 兼容子集、增强 REST / Job、音色版本、实时事件和调用示例                  |
| [OpenAI 兼容与增强边界](architecture/openai-compatibility.md)                                       | SDK 调用、精确兼容子集、`nexus` 扩展规则与契约门禁                              |
| [资源治理与持久化](architecture/runtime-governance.md)                                              | 池预算、准入 / 调度、取消与 fencing、计量、任务发布、保留与删除                 |
| [规范执行命令与内部文本流](architecture/execution-command.md)                                       | 完整有效命令、摘要、受控资源引用与 TTS 文本提交                                 |
| [数据删除与依赖裁决](architecture/data-deletion.md)                                                 | 预检、明确级联、独立副本、异步清理和恢复账本                                    |
| [精简分布式协调 Schema](architecture/distributed-coordination-schema.md)                            | 普通约束、当前指针、表所有权与历史原型隔离                                      |
| [应用事务方案与契约](architecture/coordination-transactions.md)                                     | 程序职责、原子步骤、锁序、预算、幂等/未知恢复及业务编码交接                     |
| [受限节点控制器](architecture/node-controller.md)                                                   | 批准模板的 prepare / stop、generation / fencing、drain 门与停止证据规则         |
| [契约与事务所有权](architecture/contract-ownership.md)                                              | 18 端口/表归属、快照恢复映射与首批生成契约包交接                                |
| [UI/UX 设计框架与设计 Token 系统](architecture/ui-ux-design-framework-and-tokens.md)                | 2026 设计最佳实践、双面品牌调性、VoiceStudio 经验复用与三层 Token 体系          |
| [SpeechRail 复用边界](architecture/speechrail-reuse.md)                                             | 固定来源、可提取规则、需重写的基础设施和许可证                                  |
| [三团队整体实施计划](plans/2026-10-23-three-team-delivery-plan.md)                                  | 整体实施调度入口：团队分工、任务依赖、阶段关口、集成节奏与产能估算              |
| [整体实施跟踪矩阵](plans/2026-10-23-implementation-tracking.md)                                     | GitHub 总控 #6、6 Epic、25 工作包与 6 Gate 的状态、依赖和证据矩阵               |
| [分阶段实施计划](plans/2026-10-22-speechnexus-implementation-plan.md)                               | P0–P5、退出条件、工作包、交接产物与预算假设                                     |
| [集群协调重构计划](plans/2026-10-23-cluster-coordination-refactor.md)                               | 精简 DDL/契约/双库原型的交付与生产接入、集群故障资格的剩余验收                  |
| [架构与契约修复闭环](audits/2026-10-23-architecture-contract-repair-closure.md)                     | 初验与终审补充的修复映射、验证证据及未验证运行边界                              |
| [集群协调重构后的架构与契约审查](audits/2026-10-23-architecture-contract-post-refactor-review.md)   | 运行角色最小权限、双库门禁、契约结果覆盖、端口原型一致性与开放项                |
| [B00 MegaCube 环境与能力缺口前检](audits/2026-10-24-b00-megacube-environment-and-capability-gap.md) | GB10 主机与既有 GPU 占用、制品获取与摘要、首个真实 ASR 生命周期探针与停止证据   |
| [B02 控制器容器生命周期探针](audits/2026-10-24-b02-controller-container-lifecycle-probe.md)         | 合成容器的 prepare / ready / stop 与停止双证据、`Init` 缺口与清理结果           |
| [设计审查记录](audits/2026-10-22-speechnexus-design-review.md)                                      | 已发现问题、修正裁决、来源核验、文档检查与未验证项                              |
| [UI/UX 设计框架终审](audits/2026-10-23-ui-ux-design-framework-audit.md)                             | 多层次排版对比度审查、DTCG Token 资产、矢量品牌、响应式与 CI 拦截能力           |
| [开发与测试环境](development-environment.md)                                                        | 当前主机工具链、独立服务、媒体与无头浏览器验证、初始化和隔离边界                |
| [开发环境就绪审查](audits/2026-10-22-development-environment-readiness.md)                          | 当前主机验证证据、隔离边界、真实推理缺口与 P0 / P1 可用范围                     |
| [架构决策](decisions/README.md)                                                                     | 已确认范围、决策原因和影响                                                      |
| [语音模型、推理引擎与加载策略](decisions/0002-speech-models-engines-and-loading.md)                 | 已接受的首选验证矩阵、常驻 / 按需加载、音色创作闭环与待验证范围                 |
| [工作台 LLM 与业务内容的网络边界](decisions/0003-workbench-llm-network-boundary.md)                 | 仅本机 / 内网 LLM、内容不出网、可选集成与部署责任                               |
| [集群就绪架构与单机 MVP 同构部署](decisions/0004-cluster-ready-architecture-and-single-node-mvp.md) | 架构层集群原生、三级资源拓扑、分池租约、节点控制器与单机 MVP 演进边界           |
| [集群协调与存储可移植性](decisions/0006-cluster-coordination-and-storage-portability.md)            | 已接受的应用事务保护边界、不采用 etcd、PG 正式交付及 MySQL 关键协调验证         |
| [遗留协调原型的退出与删除](decisions/0007-legacy-coordination-retirement.md)                        | 旧 PG 必跑门禁、SQL/测试/设计的退出条件与 Git 追溯；有效业务不变量迁入新路径    |

领域设计使用稳定文件名持续维护；实施计划与审查保留日期；ADR 以四位编号记录决定。协议 / 数据规则分别在其负责文档集中维护，主文档给出职责与入口。冲突需修订设计，不能以工作记录或历史材料静默覆盖公共语义。

机器契约入口为 [contracts/README.md](../contracts/README.md)。`make contracts-check` 与 `uv run pytest tests/contract` 校验 OpenAPI / JSON Schema、正负样例、事件联合与 OpenAI SDK REST / Realtime 形状；这些检查不启动业务服务。协调原型按[集成测试说明](../tests/integration/README.md)使用独立 PG/MySQL 测试库运行；CI 同时提供一次性 PG16 与 MySQL8.4 服务。旧 PG 触发器测试仅过渡保留，按 ADR 0007 退出；新受控事务测试持续验证当前架构要求。

包内生产迁移已交付 [Job/outbox 创建与恢复](architecture/production-jobs.md)和
[`0002_coordination` 存储/权限基线](architecture/production-coordination-storage.md)；
这与根目录设计/测试草案分离，其余生产事务与真实集群资格继续按所有权取证。

[架构文档](architecture/README.md) · [计划](plans/README.md) · [审查记录](audits/README.md) · [项目说明](../README.md)
