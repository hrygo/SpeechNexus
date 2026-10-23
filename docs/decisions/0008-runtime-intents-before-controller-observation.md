---
title: 控制器观察前的持久 runtime 意图
status: accepted
date: 2026-10-24
---

# ADR 0008 控制器观察前的持久 runtime 意图

## 背景

ADR 0006 要求在短关系数据库事务内记录意图和预算，再调用外部控制器。
既有 RuntimeSnapshot 直接要求完整 RuntimeInstanceIntentResponse，无法表达
“协调者已持久提交，但控制器尚未观察”。用默认 absent/ready/stopped 或当前时间
补齐响应，会把协调事实与控制器事实混在一起。

同时，coordination_outbox 的 job_id 强制非空，不能保存 runtime 控制意图。
创建伪造 Job 或在第二个数据库另写事件，均不符合原子性与表所有权约定。

## 决定

1. 在原数据库内扩展 outbox，使用互斥 Job/runtime 目标及节点作用域外键。
   旧 Job 写入默认保持 job；runtime 必须引用原 node/intent，不占用伪造 Job 身份。
   沿用事件主键/摘要和投递状态，不在数据库事务内执行外发。
2. RuntimeSnapshot 保留实际观察分支，新增无观察分支：runtime 为 NULL，
   保存冻结 request、协调记录时间、version 和 pending/reconciling/quarantined。
   无观察分支不能报告 succeeded；observe_runtime 成功必须有实际控制器响应。
   committed 表示协调事务提交，不授予 ready/停止/预算释放权限。
3. 协调时间与控制器时间分别保存。有观察时保留控制器原响应；旧记录缺少事实时
   不填入当前时间。旧 writer 隔离和历史核对继续 A07，不普通重放/派发缺失事实的记录。
4. 使用新迁移 0005，保持 0001–0004 原 DDL。运行角色不更新创建时间/事件目标，
   gateway 不读取 A 的私有观察正文；迁移不自动扩大既有运行角色的列授权。

## 兼容边界与消费者顺序

新增 nullable 分支会影响假定 runtime 永远是对象的协调消费者，因此按契约包约定
登记本 ADR，先交付消费者与共享正负例，再接入新 runtime 生产事务。内部包仍为
pre-release；消费以集成 SHA/生成清单锁定，不能只凭 1.0.0 信封版本字符串推断
包含哪个预发布分支。稳定发布后的协议变更须另行规定版本迁移。

| 消费位置 | 本次处理 |
| --- | --- |
| A 的协调 Python validator | 打包完整分支与 create/observe 请求，执行源契约/离线正负例 |
| 包内 TypeScript 协调 parser 测试 | 消费同一 JSON 样例，覆盖无观察、既有观察和拒绝伪造完成；不新增公开 TS 协调 API |
| 既有 A Job/owner/claim 边界 | 各自返回原 Fact/Owner/ClaimSnapshot，不消费 RuntimeSnapshot |
| B gateway query/recovery | 返回 ExecutionSnapshot；保留 B #65 的 bundle roots、语义与消费样例 |
| B node-controller HTTP | 请求/响应和观察枚举保持原协议，只输出实际 RuntimeInstanceIntentResponse |
| C 的产品消费者 | 继续使用公开语音/工作台协议，不接入内部协调信封 |
| 后续 A runtime 事务/派发 | 必须显式处理 runtime 为 NULL，且通过实际状态/停止证据控制预算；尚未实现 |

已有观察回执继续有效。迁移后引入 runtime 事件之前，部署必须更新按 event_kind
选择目标的消费者；本迁移不生成事件、不启动控制器或引擎。公共语音/OpenAI
兼容范围不因本次内部协议扩展而变化。

## 结果与未完成项

可在没有控制器观察时如实返回原意图，并在同一数据库安排后续持久派发。
目标 CHECK/外键、时间、权限和升级回滚由 PG/受控 InnoDB oracle 验证；
JSON 语义、资源裁决及历史恢复由受控应用事务负责，不把基础约束当作完整生命周期。

create_runtime_intent、observe_runtime、真实派发和联合资格按
[runtime 实施单元](../plans/2026-10-24-a01-runtime-intent-unit.md)继续推进。
本决定和存储准备不证明 GPU、真实停止、多主机或完整恢复已通过资格，
也不满足 ADR 0007 旧机制整体退出条件。
