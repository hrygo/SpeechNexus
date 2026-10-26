# SpeechNexus 架构文档

[项目主设计](../speechnexus-product-architecture.md)定义产品与模块，以下文档负责对应技术细节。P0 公共与内部机器契约已交付，语音、工作台、资源治理和真实模型行为仍待实现；工程目录文档记录当前 workspace 与门禁。

- [整体技术方案](technical-blueprint.md)：工程选型、内部协议、执行配方、事务 / 迁移、Web / 内网 LLM、观测与交付。
- [部署单元与平台规划](deployment-units.md)：分布式服务、原生平台与容器边界、节点绑定、Linux 媒体沙箱、当前装配缺口和验收顺序。
- [部署单元方案审查（2026-10-26）](../audits/2026-10-26-deployment-units-review.md)：松耦合与跨平台审查、实施任务及 D1/G5 门禁。
- [工程目录与依赖边界](repository-layout.md)：当前 workspace 布局、模块方向、契约生命周期和验证入口。
- [语音接口](speech-api.md)：REST / Job、音色生命周期、实时协议与示例。
- [OpenAI 兼容与增强边界](openai-compatibility.md)：SDK 调用、兼容子集、`nexus.*` 扩展规则与契约门禁。
- [资源治理与持久化](runtime-governance.md)：调度、fencing、用量、发布与数据生命周期。
- [规范执行命令](execution-command.md)：完整参数、摘要与内部 TTS 文本流。
- [原命令与核验文件输入](frozen-execution-inputs.md)：原持久请求只读核验、一次提交命令桥、固定版本对象输入与既有文件消费者；真实引擎与最终发布仍待装配。
- [数据删除](data-deletion.md)：依赖预检、级联确认、清理证据和恢复账本。
- [精简协调 Schema](distributed-coordination-schema.md)：普通约束、当前指针、表所有权与历史保护；[旧 PG 原型](legacy-postgresql-coordination.md)保留历史证据。
- [应用事务方案与契约](coordination-transactions.md)：程序职责、锁序、预算、恢复身份、旧规则映射和后续编码交接。
- [受限节点控制器](node-controller.md)：批准模板的 prepare / stop 规则、generation / fencing 与停止证据，以及当前实现与待交付边界。
- [执行网关提交事务](execution-gateway.md)：`create_submit` 的持久意图、锁序、完整请求摘要、版本守卫、错误分类与当前未交付的停止/计量端口；
- [CPU VAD](cpu-vad.md)：有界切段、会话推理状态、无语音门禁、固定模型与原生消费接口；公开实时链路仍待集成。
- [文件 ASR 有界媒体输入](file-asr-audio.md)：受控二进制流、五种格式、源时钟/尾部、文件 VAD 组合及尚未接入的外发/发布。
- [文件 ASR 原生识别接口](file-asr-recognition.md)：固定内网 vLLM、请求/终态/token 校验、无语音零外发和源/模型样本关联。
- [文件 ASR 默认执行装配](file-asr-runtime.md)：私有配置、默认工厂、原 worker 与结果回执；真实模型、可信停止和公开链资格分别待验。
- [文件 TTS 原生合成接口](file-tts.md)：固定内网 `/v1/audio/speech`、响应头/RIFF 结构校验、单次外发与有界 PCM/WAV/MP3 交付编码。
- [文件 TTS 默认执行装配](file-tts-runtime.md)：私有配置、受控 generation、原命令 worker、生成区间与音频回执；真实私有执行和整代停止已有隔离证据，公开链与生产资格仍待。
- [CustomVoice 制品与参数交接](file-tts-binding-handoff.md)：固定制品、镜像/源码、完整注册输入与显式默认，区分参数声明、发布与运行资格。
- [工作台导出发布前完整性检查](workbench-export-integrity.md)：持久进度、真实媒体解码、归档限额与取消复核；完整单元清单、续作和真实质量仍待交付。
- [生产协调存储基线](production-coordination-storage.md)：包内 `0002_coordination`、15 表、A/B 最小权限、升级回滚与迁移互斥证据；事务实现另行验收。
- [完整准入的私有命令存储](production-admission-storage.md)：`0007` 的 operation 权威、原业务/准入请求、独立摘要与正文 tombstone 约束；独立准入已消费，实际业务/调度仍待接入。
- [完整配方准入独立事务](production-admission.md)：受信命令/计划、全部阶段原子预留、原身份恢复与显式并发配额范围；真实业务/资格仍待装配。
- [固定节点HTTPS客户端](runtime-controller-client.md)：原runtime意图提交/查询、证书与总期限/有界响应，节点观察生产者及持久控制循环仍待。
- [资产发布与保留期存储](production-assets.md)：实际文本检查、PG/版本化 MinIO 发布、原身份恢复和删除账本；公开路由与常驻维护进程后续接入。
- [生产 owner 任期事务](production-owners.md)：获取/续约/激活、完整当前权威查询、锁后数据库取时和未知提交恢复。
- [集群协调与存储可移植性决定](../decisions/0006-cluster-coordination-and-storage-portability.md)：集群能力不降级、应用事务边界与数据库支持范围；[重构计划](../plans/2026-10-23-cluster-coordination-refactor.md)记录实际交付与待验收范围。
- [UI/UX 设计框架与设计 Token 系统](ui-ux-design-framework-and-tokens.md)：2026 设计最佳实践、双面品牌调性、VoiceStudio 经验复用与三层 Token 体系。
- [SpeechRail 复用边界](speechrail-reuse.md)：领域经验、来源和移植条件。

[文档索引](../README.md)
