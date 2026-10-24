# SpeechNexus 架构文档

[项目主设计](../speechnexus-product-architecture.md)定义产品与模块，以下文档负责对应技术细节。P0 公共与内部机器契约已交付，语音、工作台、资源治理和真实模型行为仍待实现；工程目录文档记录当前 workspace 与门禁。

- [整体技术方案](technical-blueprint.md)：工程选型、内部协议、执行配方、事务 / 迁移、Web / 内网 LLM、观测与交付。
- [工程目录与依赖边界](repository-layout.md)：当前 workspace 布局、模块方向、契约生命周期和验证入口。
- [语音接口](speech-api.md)：REST / Job、音色生命周期、实时协议与示例。
- [OpenAI 兼容与增强边界](openai-compatibility.md)：SDK 调用、兼容子集、`nexus.*` 扩展规则与契约门禁。
- [资源治理与持久化](runtime-governance.md)：调度、fencing、用量、发布与数据生命周期。
- [规范执行命令](execution-command.md)：完整参数、摘要与内部 TTS 文本流。
- [数据删除](data-deletion.md)：依赖预检、级联确认、清理证据和恢复账本。
- [精简协调 Schema](distributed-coordination-schema.md)：普通约束、当前指针、表所有权与历史保护；[旧 PG 原型](legacy-postgresql-coordination.md)保留历史证据。
- [应用事务方案与契约](coordination-transactions.md)：程序职责、锁序、预算、恢复身份、旧规则映射和后续编码交接。
- [受限节点控制器](node-controller.md)：批准模板的 prepare / stop 规则、generation / fencing 与停止证据，以及当前实现与待交付边界。
- [执行网关提交事务](execution-gateway.md)：`create_submit` 的持久意图、锁序、完整请求摘要、版本守卫、错误分类与当前未交付的停止/计量端口；
- [CPU VAD](cpu-vad.md)：有界切段、会话推理状态、无语音门禁、固定模型与原生消费接口；公开实时链路仍待集成。
- [生产协调存储基线](production-coordination-storage.md)：包内 `0002_coordination`、15 表、A/B 最小权限、升级回滚与迁移互斥证据；事务实现另行验收。
- [资产发布与保留期存储](production-assets.md)：实际文本检查、PG/版本化 MinIO 发布、原身份恢复和删除账本；公开路由与常驻维护进程后续接入。
- [生产 owner 任期事务](production-owners.md)：获取/续约/激活、完整当前权威查询、锁后数据库取时和未知提交恢复。
- [集群协调与存储可移植性决定](../decisions/0006-cluster-coordination-and-storage-portability.md)：集群能力不降级、应用事务边界与数据库支持范围；[重构计划](../plans/2026-10-23-cluster-coordination-refactor.md)记录实际交付与待验收范围。
- [UI/UX 设计框架与设计 Token 系统](ui-ux-design-framework-and-tokens.md)：2026 设计最佳实践、双面品牌调性、VoiceStudio 经验复用与三层 Token 体系。
- [SpeechRail 复用边界](speechrail-reuse.md)：领域经验、来源和移植条件。

[文档索引](../README.md)
