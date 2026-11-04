# SpeechNexus 文档

交付主入口是 [PRD](prd.md)、[产品架构设计](speechnexus-product-architecture.md)及[需求矩阵](plans/delivery-tracking.md)：分别规定完整需求与验收、方案约束、需求到主 Epic 和实施证据的关联。十个 Epic 维护能力边界，具体实施 issue 以后按需创建；没有实施卡的需求仍在矩阵中。设计规定目标行为；仓库已有持久事务、语音执行和工作台消费者的分段实现，文件 ASR、CustomVoice 与 CPU VAD 已有真实隔离实验。[受控同步 TTS 窗口](audits/2026-11-03-controlled-tts-prepare.md)已取得一次公开 SDK 链路及关联证据；其他业务链、生产资格和目标拓扑仍须分别验收。契约或组件检查通过不能代替这些证据。当前组合与管理先读[完整交付矩阵](plans/delivery-tracking.md)和[总控 #336](https://github.com/hrygo/SpeechNexus/issues/336)；[原项目交接](archive/plans/2026-10-26-project-handoff.md)保留三队交接时点，团队 B 的详细证据见[收尾交接](archive/plans/2026-10-26-team-b-handoff.md)。文档版本独立于软件版本。

| 文档                                                                                                | 职责                                                                            |
| --------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------- |
| [项目经理工作约定](project-manager-guide.md) | 接管入口、排期协作、集成验收、进度维护与证据清理的长期规则 |
| [Codex / subagent / Delegra 交付 SOP](operations/agent-delivery-sop.md) | 单一队列、执行路由、最小上下文、同版验证、等待与故障恢复 |
| [项目经理接管基线](archive/plans/2026-10-26-project-manager-baseline.md) | 接管时点的组合、能力/缺口、决策队列、三队交接与证据接收快照 |
| [Agent 工作指南](agent-guide.md) | 根/目录规则、项目技能、任务入口与交付边界 |
| [历史协调方案](archive/legacy-postgresql-coordination.md) | 历史架构资料；暂作对照保留，退出条件见 ADR 0007 |
| [Agent 验证指南](agent-verification.md) | 按改动选择检查、锁定依赖、隔离服务前置与证据范围 |
| [组件与实现说明](components/README.md) | 组件职责、接口、事务及实现接线 |
| [运维与部署文档](operations/README.md) | 安装配置、运行恢复与发布操作 |
| [验证规则](validation/README.md) | CI 调度与契约变更结构检查的范围和限制 |
| [上线前检查脚本](operations/release-preflight.md) | C0–C11 局部检查和 C12 独立验收要求；默认不写产品数据库，但会加载 shell 配置，可能访问网络及写缓存；另有历史候选假设 |
| [产品与架构设计](speechnexus-product-architecture.md)                                               | 产品范围、AdminUI / 工作台、领域与模块、场景预设、共享访问、部署与验收          |
| [整体技术方案](architecture/technical-blueprint.md)                                                 | 技术基线、内部协议、执行配方、事务 / 迁移、Web / 内网 LLM、观测、发布与工程交接 |
| [部署单元与平台规划](architecture/deployment-units.md) | 独立服务、平台与节点边界、当前入口/缺口及跨平台和多机验收 |
| [工程目录与依赖边界](components/repository-layout.md)                                             | Python / Web workspace、模块方向、契约生命周期、测试层级与部署入口              |
| [语音接口](architecture/speech-api.md)                                                              | OpenAI 兼容子集、增强 REST / Job、音色版本、实时事件和调用示例                  |
| [OpenAI 兼容与增强边界](architecture/openai-compatibility.md)                                       | SDK 调用、精确兼容子集、`nexus` 扩展规则与契约门禁                              |
| [资源治理与持久化](architecture/runtime-governance.md)                                              | 池预算、准入 / 调度、取消与 fencing、计量、任务发布、保留与删除                 |
| [规范执行命令与内部文本流](architecture/execution-command.md)                                       | 完整有效命令、摘要、受控资源引用与 TTS 文本提交                                 |
| [原命令与核验文件输入](components/frozen-execution-inputs.md) | 原持久请求只读核验、一次提交命令桥、每次读取时固定对象版本及真实执行剩余边界；原命令不携带 S3 version ID |
| [文件 ASR 私有结果](components/file-asr-results.md) | 完整正文有界汇总、固定版本对象写入与读回核验；持久登记与公开发布边界 |
| [执行回执登记与查询](components/execution-staging-results.md) | 原观察事务、固定版本私有回执、CAS/恢复、权限与只读查询；不发布或退预算 |
| [原生 ASR 请求清理](../deploy/engines/vllm-asr-lifecycle/README.md) | 固定引擎插件、内部/子请求归属、在途提交保护与取消清理；不代表 GPU 停止证明 |
| [受控 ASR 原生鉴权与代次](components/controlled-asr-generation.md) | 原生代次 key、批准模板检查、私有部署描述与文件命令目标校验 |
| [文件 ASR 默认执行装配](operations/file-asr-runtime.md) | 私有配置、正式工厂和原 worker/回执；真实模型隔离实验已有，生产预算及公开链资格待验 |
| [同步文件转写的生产装配](operations/synchronous-transcription.md) | `/v1/audio/transcriptions` 的 A 侧组合根、私有准入配方、输入 staging 与网关轮询；[隔离 JSON 最小链已有证据](https://github.com/hrygo/SpeechNexus/pull/317)，目标部署及完整场景仍须验收 |
| [同步 TTS 的持久意图与恢复](components/synchronous-speech.md) | 完整命令准入、声音退役屏障与恢复；正式工厂仅在配置和资格满足时启用 |
| [数据删除与依赖裁决](architecture/data-deletion.md)                                                 | 预检、明确级联、独立副本、异步清理和恢复账本                                    |
| [精简分布式协调 Schema](architecture/distributed-coordination-schema.md)                            | 普通约束、当前指针、表所有权与历史原型隔离                                      |
| [应用事务方案与契约](architecture/coordination-transactions.md)                                     | 程序职责、原子步骤、锁序、预算、幂等/未知恢复及业务编码交接                     |
| [受限节点控制器](components/node-controller.md)                                                   | 批准模板的 prepare / stop、generation / fencing、drain 门与停止证据规则         |
| [执行网关提交事务](components/execution-gateway.md) | 持久意图、提交屏障、停止证据与输出区间；真实计量采集器及完整资格仍待 |
| [契约与事务所有权](architecture/contract-ownership.md)                                              | 18 端口/表归属、快照恢复映射与首批生成契约包交接                                |
| [UI/UX 设计框架与设计 Token 系统](design/ui-ux-design-framework-and-tokens.md) | 设计方向、双入口品牌、VoiceStudio 经验与三层 Token；研究为 2026 年资料，生效 Token 及实现状态以源码为准 |
| [SpeechRail 复用边界](architecture/speechrail-reuse.md)                                             | 固定来源、可提取规则、需重写的基础设施和许可证                                  |
| [Paperclip 工作流与团队](operations/paperclip.md) | 当前项目经理与 A/B/C、QA、审查、证据岗位的协作、工作区、预算及交付约定 |
| [预设声音资格与发布](operations/builtin-voice-qualification.md) | 独立审批、资格探测、不可变发布、精确准入与撤销；真实资格按具体窗口取证 |
| [本地最小可运行装配](archive/operations/local-end-to-end.md) | 指定历史基线的迁移、发布与执行记录；当时公开转写返回 504，worker 只登记 staging，120s 用于 API 轮询。后续已补成功终态驱动，原 504 不证明当前部署结果；11.2.2 的人工 `record_stop` 仅是历史测试路径 |
| [agent 环境准备与验收](archive/operations/paperclip/environment-readiness.md) | 历史三机工具链、隔离服务和 Harness 准备快照；旧配置与启用门禁不代表当前状态 |
| [三团队整体实施计划](archive/plans/2026-10-23-three-team-delivery-plan.md)                                  | 历史规划：团队分工、任务依赖、原阶段关口、集成节奏与产能估算              |
| [产品需求](prd.md) | 完整范围、十个 MECE 能力域、稳定需求与可观察验收条件 |
| [需求与交付矩阵](plans/delivery-tracking.md) | 需求、设计、主 Epic、实现/集成/验证、缺口及证据；实施 issue 按需关联 |
| [结构化关联源](plans/delivery-issues.json) | 矩阵数据源；当前实施 issue 可以为空，GitHub 状态只读刷新 |
| [分阶段实施计划](archive/plans/2026-10-22-speechnexus-implementation-plan.md)                               | 历史 P0–P5、退出条件、工作包、交接产物与初始预算假设                           |
| [集群协调重构计划](archive/plans/2026-10-23-cluster-coordination-refactor.md)                               | 历史 DDL/契约/双库原型交付及当时的生产接入和集群资格计划                         |
| [架构与契约修复闭环](audits/2026-10-23-architecture-contract-repair-closure.md) | 历史审查：初验与终审的修复映射、验证证据及当时未验证边界 |
| [集群协调重构后的架构与契约审查](audits/2026-10-23-architecture-contract-post-refactor-review.md) | 历史审查：运行角色、双库门禁、契约结果、端口原型与当时开放项 |
| [B00 MegaCube 环境与能力缺口前检](audits/2026-10-24-b00-megacube-environment-and-capability-gap.md) | 历史环境快照：GB10 主机、GPU 占用、制品及首次 ASR 生命周期探针 |
| [B02 控制器容器生命周期探针](audits/2026-10-24-b02-controller-container-lifecycle-probe.md) | 历史探针：合成容器的 prepare / ready / stop、停止双证据与清理结果 |
| [B02 控制器 HTTP 外壳与持久意图探针](audits/2026-10-24-b02-controller-http-shell-probe.md) | 历史探针：控制器进程鉴权、生命周期、停机双证据与重启后的持久意图 |
| [CPU VAD 原生接口](components/cpu-vad.md) | 会话隔离、有界切段、固定 CPU 模型及文件执行装配；实时链路待验 |
| [B06 CPU VAD 合成探针](audits/2026-10-24-b06-cpu-vad-probe.md) | 历史探针：合成人声/静音/正弦音、输入分块一致性与模型状态隔离；不代表真实录音资格 |
| [文件 ASR 有界媒体输入](components/file-asr-audio.md) | 五种格式解码/重采样、源样本时钟、文件 VAD 组合与接入边界 |
| [B03 有界媒体与真实录音输入探针](audits/2026-10-24-b03-bounded-audio-probe.md) | 历史探针：合成媒体、两段授权录音及尾部处理实测；不代表识别质量或完整服务 |
| [文件 ASR 原生识别接口](components/file-asr-recognition.md) | 固定内网 ASR、VAD 门禁、SSE 完成/截断判定、源时钟及可配置的持久 worker 装配 |
| [文件 ASR 固定制品与参数交接](operations/file-asr-binding-handoff.md) | 1.7B/VAD/引擎锁定、离线校验、原命令参数与 A 配置/发布的接入边界 |
| [文件 TTS 原生合成接口](components/file-tts.md) | 固定内网 synthesis、响应头/RIFF 结构校验、单次外发与有界 PCM/WAV/MP3 交付编码 |
| [文件 TTS 默认执行装配](operations/file-tts-runtime.md) | 私有配置、受控 generation、原命令 worker、生成区间与音频回执；受控公开同步链已有证据，完整资格待验 |
| [B03 真实文件 ASR 与首次长窗口开销](audits/2026-10-24-b03-file-asr-probe.md) | 历史探针：0.6B/1.7B、真实录音、无语音零外发、截断负例与重复测量 |
| [B03 TTS 适配器真实探针](audits/2026-10-24-b03-tts-adapter-probe.md) | 历史探针：1.7B CustomVoice 的 WAV/MP3、重采样与 ASR 回环、停止和清理证据 |
| [设计审查记录](audits/2026-10-22-speechnexus-design-review.md) | 历史审查：发现的问题、修正裁决、来源核验及当时未验证项 |
| [UI/UX 设计框架终审](audits/2026-10-23-ui-ux-design-framework-audit.md)                             | 历史设计与 CI 审查；报告中的绝对化无障碍和拦截评级超出所列证据 |
| [开发与测试环境](development-environment.md)                                                        | Linux 参考开发环境、独立服务、初始化和隔离边界；宿主状态需实测                |
| [开发环境就绪审查](audits/2026-10-22-development-environment-readiness.md)                          | 2026-10-22 的环境验证与隔离边界快照，不代表当前主机状态                     |
| [架构决策](decisions/README.md)                                                                     | 架构决定、理由和影响；区分已接受与拟议状态 |
| [语音模型、推理引擎与加载策略](decisions/0002-speech-models-engines-and-loading.md)                 | 已接受的首选验证矩阵、常驻 / 按需加载、音色创作闭环与待验证范围                 |
| [工作台 LLM 与业务内容的网络边界](decisions/0003-workbench-llm-network-boundary.md)                 | 仅本机 / 内网 LLM、内容不出网、可选集成与部署责任                               |
| [集群就绪架构与单机 MVP 同构部署](decisions/0004-cluster-ready-architecture-and-single-node-mvp.md) | 架构层集群原生、三级资源拓扑、分池租约、节点控制器与单机 MVP 演进边界           |
| [集群协调与存储可移植性](decisions/0006-cluster-coordination-and-storage-portability.md)            | 已接受的应用事务保护边界、不采用 etcd、PG 正式交付及 MySQL 关键协调验证         |
| [遗留协调原型的退出与删除](decisions/0007-legacy-coordination-retirement.md)                        | 旧 PG 必跑门禁、SQL/测试/设计的退出条件与 Git 追溯；有效业务不变量迁入新路径    |

领域设计使用稳定文件名持续维护；实施计划与审查保留日期；ADR 以四位编号记录决定。协议 / 数据规则分别在其负责文档集中维护，主文档给出职责与入口。冲突需修订设计，不能以工作记录或历史材料静默覆盖公共语义。

机器契约入口为 [contracts/README.md](../contracts/README.md)。`make contracts-check` 与 `uv run pytest tests/contract` 校验 OpenAPI / JSON Schema、正负样例、事件联合与 OpenAI SDK REST / Realtime 形状；这些检查不启动业务服务，但部分契约测试会启动临时 loopback HTTP 测试服务和子进程。协调原型按[集成测试说明](../tests/integration/README.md)使用独立 PG/MySQL 测试库运行；CI 同时提供一次性 PG16 与 MySQL8.4 服务。旧 PG 触发器测试仅过渡保留，按 ADR 0007 退出；新受控事务测试持续验证当前架构要求。

包内生产迁移已交付 [Job/outbox 创建与恢复](components/production-jobs.md)和
[`0002_coordination` 存储/权限基线](operations/production-coordination-storage.md)；
[资产发布存储单元](components/production-assets.md)使用 `0003_assets` 等迁移接入真实文本、版本化 MinIO 与保留期账本；`0004_asset_storage` 固定存储命名空间，旧资产的接管与完整恢复仍需单独验证。
[租约隔离与释放事务](components/production-leases.md)消费网关持久未知/停止/未外发事实；隔离保留占用，释放只扣当前 lease 增量，最后 stage 才结算 attempt 配额。
[完整准入的私有命令存储](components/production-admission-storage.md)通过0007新增operation/原请求权威与私有正文约束，独立准入已消费该存储。[公开 Job 派发器](components/production-jobs.md#公开-job-派发器del-96-第一步)已有领域、真实 PG 领取/终态存储、网关客户端适配器、结果发布组件和独立 worker 入口；生产 planner 尚未装配，当前 worker 在领取前退出，与真实网关的完整异步链仍须联验。
[完整配方准入独立事务](components/production-admission.md)提供全部阶段原子预留与原身份恢复，其他业务配额账本与真实provider仍待装配。
[固定节点HTTPS客户端](components/runtime-controller-client.md)复用runtime源契约，提供有界原意图提交/查询；[持久投递worker](operations/production-runtime-delivery.md)已接原请求与未知查询。
[节点停止检查点恢复](components/node-stop-recovery.md)及[可信停止到网关](components/trusted-node-stop.md)已有原生接口和分段证据；固定 TLS 来源与默认网关周期驱动已由 #123/#127 交付，真实引擎故障资格与完整调度仍待接入。
这些包内实现与根目录设计/测试草案分离，其余生产事务与真实集群资格继续按所有权取证。

[架构文档](architecture/README.md) · [计划](plans/README.md) · [审查记录](audits/README.md) · [项目说明](../README.md)
