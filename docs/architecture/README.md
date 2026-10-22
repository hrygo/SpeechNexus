# SpeechNexus 架构文档

[项目主设计](../speechnexus-product-architecture.md)定义产品与模块，以下文档负责对应技术细节。整体技术方案、语音、资源治理与复用文档仍是待实现的设计基线；工程目录文档记录当前 workspace 与门禁。

- [整体技术方案](technical-blueprint.md)：工程选型、内部协议、执行配方、事务 / 迁移、Web / 内网 LLM、观测与交付。
- [工程目录与依赖边界](repository-layout.md)：当前 workspace 布局、模块方向、契约生命周期和验证入口。
- [语音接口](speech-api.md)：REST / Job、音色生命周期、实时协议与示例。
- [资源治理与持久化](runtime-governance.md)：调度、fencing、用量、发布与数据生命周期。
- [分布式协调与容量治理 Schema（P0-B 草案）](distributed-coordination-schema.md)：三级资源拓扑表、分池租约、网关提交屏障、锁顺序与原子事务模板。
- [SpeechRail 复用边界](speechrail-reuse.md)：领域经验、来源和移植条件。

[文档索引](../README.md)
