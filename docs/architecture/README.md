# SpeechNexus 架构文档

[项目主设计](../speechnexus-product-architecture.md)定义产品与模块。本目录维护系统级职责、协议与数据和部署边界；具体组件的实现、事务和接线见[组件说明](../components/README.md)，部署和运行步骤见[运维文档](../operations/README.md)。设计要求、实现与验收分别判断，当前缺口见[交付跟踪矩阵](../plans/delivery-tracking.md)。

## 系统方案与边界

- [整体技术方案](technical-blueprint.md)：工程选型、服务职责、依赖和总体交付边界。
- [部署单元与平台规划](deployment-units.md)：服务与节点、网络、平台及物理拓扑。
- [资源治理与持久化](runtime-governance.md)：准入、预算、任务恢复和资源生命周期。
- [数据删除](data-deletion.md)：预检、删除账本、跨模块范围和恢复屏障。
- [SpeechRail 复用边界](speechrail-reuse.md)：固定来源、许可和允许复用的范围。

## 契约与协调

- [语音接口](speech-api.md)：公开 REST、Job 和实时协议的语义与边界。
- [OpenAI 兼容与增强边界](openai-compatibility.md)：兼容子集和 `nexus` 扩展。
- [规范执行命令](execution-command.md)：网关与生产者间的私有执行输入。
- [契约与协调事务所有权](contract-ownership.md)：生产者、消费者与事务归属。
- [应用事务方案与契约](coordination-transactions.md)：原子准入、锁序、恢复和预算不变量。
- [精简协调 Schema](distributed-coordination-schema.md)：表关系、所有权与约束。

## 组件边界与其他资料

执行网关持有提交屏障、执行意图和停止证据；节点控制器管理批准模板与实例生命周期，业务服务通过既定协议协调，不直连引擎。具体实现和未交付边界分别见[执行网关](../components/execution-gateway.md)与[节点控制器](../components/node-controller.md)。

- [部署拓扑图](diagrams/deployment-clusters-v3-4k/README.md)。
- [设计决定](../decisions/README.md)与[部署单元审查](../archive/audits/2026-10-26-deployment-units-review.md)。
- [UI/UX 与设计 Token](../design/ui-ux-design-framework-and-tokens.md)。
- [旧协调方案](../archive/legacy-postgresql-coordination.md)暂作历史对照；退出条件见 [ADR 0007](../decisions/0007-legacy-coordination-retirement.md)。

[文档索引](../README.md)
