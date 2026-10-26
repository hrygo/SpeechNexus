---
name: speechnexus-coordination-change
description: "变更或审查 SpeechNexus 协调事务、锁序、租约/预算、fencing、持久恢复及相关 DDL/权限。"
---

# 协调事务与恢复变更

让应用裁决、事务适配器、持久证据和故障恢复保持一致。审查保持只读；实施授权内可完成隔离测试，不因此操作共享数据库、真实引擎或部署运行态。

本技能聚焦明确协调事务端口的原子性、锁、预算、权限和双库落地。需要先判断跨 owner 的原身份、提交未知或取消恢复动作时，按需使用 [speechnexus-recovery-design](../speechnexus-recovery-design/SKILL.md)；已有恢复决定直接沿用，不重复展开全部故障窗口。

## 确定事务边界

从[事务方案](../../../docs/architecture/coordination-transactions.md)与[事务机器契约](../../../contracts/internal/coordination-transactions.schema.json)定位受影响端口；查[所有权矩阵](../../../docs/architecture/contract-ownership.md)确认表、生产者与消费者。只读相关端口和引用范围。

按变更补读：

- 保护责任、数据库可移植性、权限或 DDL：[ADR 0006](../../../docs/decisions/0006-cluster-coordination-and-storage-portability.md)、[协调 Schema](../../../docs/architecture/distributed-coordination-schema.md)。
- 执行命令、删除或停止证据：对应[执行命令](../../../docs/architecture/execution-command.md)、[删除规则](../../../docs/architecture/data-deletion.md)、[执行网关](../../../docs/architecture/execution-gateway.md)。
- 迁移旧原型规则或改变门禁：[ADR 0007](../../../docs/decisions/0007-legacy-coordination-retirement.md)。
- 机器契约或生成类型改变：同时使用 [speechnexus-contract-change](../speechnexus-contract-change/SKILL.md) 完成消费者交接。

列出本次必须原子完成的读写、固定身份、锁顺序、成功/失败结果及结果未知后的查询动作。持久化或运行边界的关键选择没有现有契约或授权覆盖时，给出可评审方案；不以执行数据库迁移代替设计确认。

## 实施中保留的不变量

- 领域/应用决定跨表规则，经窄事务端口执行；基础设施负责事务、锁和条件更新，不增加任意状态更新入口。数据库保留基础约束、历史保护和最小权限，不承担未承诺的全部绕行防护。
- 完整配方的配额、节点/实例预算、全部阶段租约、Job 与 outbox 在同一关系数据库事务中准入/写入；表或进程所有权不构成跨库双写理由。核对锁序、版本/fencing 条件和权威状态与计数投影的一致性。
- 先持久化 intent，再外发完整固定命令。结果未知时查询原身份或进入协调，不新建执行作普通重试，不从可变默认值重建命令。
- 取消、超时、断线和任务结束不是资源释放证据；未知执行保留预算，取得有效停止和资源释放证据后才能归还。模型绑定、instance/generation、容量租约和资格分别记录。
- 短事务不包含推理、HTTP、对象写入或长时编码；对象先写私有 staging，再发布数据库引用。运行角色不执行 DDL、不是协调表 owner，不删除历史或修改控制面预算/策略/拓扑。
- PostgreSQL 是正式产品目标；InnoDB 验证关键协调行为。产品不导入 `tests/support/`，不以原型路径替代生产 adapter。迁移使用版本化机制，保留用户数据及有效历史。

## 验证与完成

按照受影响不变量选择正常、拒绝、重放冲突、并发锁等待、过期 owner/fencing、回滚和未知结果恢复场景；涉及停止或预算时检查证据到达前后的状态。按风险覆盖，不机械复制无关场景。

实施时，执行前读[集成目录规则](../../../tests/integration/AGENTS.md)与[验证指南](../../../docs/agent-verification.md)。协调事务、DDL 或权限变更运行 `make coordination-test` 和 `make integration-test`；缺双库、MinIO 或宿主条件就报告未验证项，不以单库、跳过或顺序调用替代。实现与验证只在授权的项目隔离服务进行。只读审查核对既有证据和缺口，未要求执行测试时不启动服务或运行迁移。

实施中接入当前事务路径或更新旧规则覆盖时，复核 ADR 0007 退出条件；满足后移除旧必跑门禁、废弃 SQL/测试/设计及专属断言，保留有效不变量的新路径覆盖和 Git 追溯；审查时只报告退出条件与缺口。

交付注明端口/表及迁移范围、身份和锁序变化、实际验证的数据库与拓扑、恢复证据和剩余生产接入。双库同进程测试、合成停止与模拟丢回复不能证明真实多机、引擎停止、网络故障或 HA 资格。
