---
title: "受限节点控制器：接口、规则与当前实现"
status: active
version: "0.1.0"
date: 2026-10-24
---

# 受限节点控制器：接口、规则与当前实现

受限节点控制器（`node-controller`）是节点级的部署控制器，认领调度器为**本节点**创建的
`runtime-control` prepare / stop 意图，报告实例观察与停止证据。机器契约唯一权威是
[`contracts/internal/runtime-control.openapi.yaml`](../../contracts/internal/runtime-control.openapi.yaml)；
本文记录端口草图、可执行规则与实现状态，不重新定义协议字段。

控制器只接受批准模板对应的实例，不接受任意镜像、宿主挂载或 shell 命令；它不接收业务音频
或正文，也不拥有预算权威：实例内存的扣减、peak → steady 的账目变更与 lease 释放仍由
Scheduler 在自己的事务中完成，控制器只提供意图与观察事实。角色与部署边界见
[ADR 0004](../decisions/0004-cluster-ready-architecture-and-single-node-mvp.md)，
生命周期用例见[应用事务方案](coordination-transactions.md#7-实例生命周期与详细实施要求)。

## 1. 端口草图

当前单元只有领域与应用层，不需要 HTTP 或容器即可验证规则：

```text
scheduler ──intent──▶ NodeRuntimeControlService ──▶ IntentStore (持久意图事实)
                            │                        │
                            │                        └─ 幂等键 (node_id, intent_id)、版本
                            └──prepare/stop/inspect──▶ NodeRuntimePort (节点容器运行时)
scheduler ◀──observation/reconcile 快照───────────────┘
```

| 端口 / 位置 | 职责 | 实现状态 |
| --- | --- | --- |
| `domain/intents.py` | 意图身份、留宿内存值、观察状态、停止证据与实例头派生 | 已实现并有单元测试 |
| `domain/templates.py` | 批准模板注册表：镜像摘要固定、允许池、内存架构与峰值上限 | 已实现；模板清单与部署绑定待接入 |
| `domain/ports.py` | `IntentStore`、`NodeRuntimePort`、reconcile 快照 | 协议草图；持久化与容器 adapter 待交付 |
| `application/runtime_control.py` | 认领意图、幂等重放、generation / fencing 门、drain 门、观察折叠与 reconcile | 已实现并有单元测试 |
| `interfaces/` | `internal/runtime-control` HTTP 外壳与组件凭据 | 未实现（本单元为空包） |
| `infrastructure/` | 持久意图存储与容器运行时 adapter | 未实现（本单元为空包） |

`tests/fake/controller.py` 提供确定性的 `InMemoryIntentStore` 与 `ScriptedNodeRuntime`，
它们不接触真实容器；`tests/contract/test_node_controller_contract.py` 断言本包的错误码、
状态、池与内存架构枚举、请求字段与契约逐项相等，并把本包可产生的每种意图状态序列化成
契约响应形状校验，避免出现第二套字段定义。

## 2. 可执行规则

- **节点范围**：`target_node_id != self.node_id` 直接拒绝（`target_node_mismatch`），不留意图。
- **批准模板**：`approved_template_id` 必须解析到已批准模板，且请求池在该模板允许集合内，
  峰值不超过模板上限，内存架构一致；否则 `unapproved_runtime_template` 或
  `invalid_runtime_request`。
- **幂等**：`(node_id, intent_id)` 为幂等键。完全相同的规范请求返回原记录并标记
  `idempotent_replay = true`，不再调用节点运行时；同键不同请求返回 `intent_conflict`；
  终态意图不会被改写为新请求，重试必须使用新 `intent_id`。
- **generation**：prepare 意图的 generation 必须严格大于该实例已持久化的 high water，
  否则 `stale_instance_generation`；旧 generation 的意图行保留，历史不被改写。
- **fencing**：同一实例的 fencing token 不得下降，下降时返回 `stale_fencing_token` 并带上
  观察到的当前 token。
- **drain 门**：前一 generation 尚未取得验证过的停止时，拒绝建立替代 generation
  （`gateway_drain_incomplete`）；旧准备流程未解决时同样保持未 drain，直到其迟到观察被折叠。
- **peak → steady**：加载与 warmup 期间意图保持 `reconciling` 且报告 peak；只有观察到
  `ready` 才转 `succeeded` 并报告 steady。暂时失败不提前退还峰值。
- **停止证据**：stop 意图只有在同时取得容器退出与资源释放确认时才能转 `succeeded`；
  观察到 `stopped` 却没有证据时进入 `quarantined` 并继续报告原留宿内存，不能凭超时或断连
  冒充停止。
- **迟到观察隔离**：观察的实例身份、generation、模板与配置摘要必须与意图完全一致；不一致
  或不属于任何已认领意图的实例进入 quarantine 名单，不修改当前 generation。
- **reconcile**：同一快照内同一 `(instance_id, instance_generation)` 出现两次即拒绝，
  不采用不确定的“最后一次观察”。

控制器记录的原因码（`RuntimeIntentErrorCode`）与 HTTP 冲突码（`RuntimeControlErrorCode`）
分开：前者写入意图事实，后者是契约冻结的响应枚举。

## 3. 当前未实现与交接

- HTTP 外壳（`internal/runtime-control` 路由、`X-Request-ID`、组件凭据校验）与持久意图存储、
  容器 adapter 尚未实现；本单元不部署新进程，`deploy/` 与 Compose 服务随之一起交付。
- 批准模板清单（`approved_template_id` → 固定镜像摘要、峰值上限）需要 B00 制品摘要与
  部署绑定共同落地，当前只有校验规则与注册表。
- 真实多实例竞争、跨节点 `intent_id` 复用、控制器不可用时的接管与恢复没有真实证据；
  这些属于 [B02 #29](https://github.com/hrygo/SpeechNexus/issues/29) 的 G1/G2 检查项。
- 本包不写 Scheduler 的预算表；`create_runtime_intent`、`observe_runtime` 的事务侧实现由
  runtime 端口维护者提供，控制器通过协议报告事实。

[架构文档](README.md) · [整体技术方案](technical-blueprint.md) · [应用事务方案](coordination-transactions.md)
