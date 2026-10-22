---
title: "集群就绪架构与单机 MVP 同构部署"
status: accepted
date: 2026-10-22
---

# 0004：集群就绪架构与单机 MVP 同构部署

## 背景

SpeechNexus 的首轮交付（MVP）采用单节点 Docker Compose 部署，不承诺节点级高可用或实时会话无缝热迁移；但产品演进需要把无状态 API、实时网关、后台 Worker 以及多类语音推理引擎扩展到多台算力节点以逐步增加容量。

如果在 MVP 阶段采用“单机内存态协调、进程内混跑后台调度、前端容器硬编码直连单实例或跨进程共享宿主本地目录”的捷径，后续向多节点集群演进时将不得不重构核心状态机、调度器和执行网关。因此，需要在架构层面确立**集群原生（Cluster-Ready）设计**，使单机 MVP 成为集群架构在 `Node=1, Replica=1` 下的同构部署特例。

## 决定

### 1. 总体同构原则：架构层集群原生，部署层单机收敛

1. **禁止单机隐式状态**：所有业务与控制组件之间仅允许通过 HTTP/JSON、WebSocket、PostgreSQL 事务 / Outbox 和 S3 兼容对象存储协作。禁止使用进程内内存锁充当全局准入，禁止使用本地 IPC（Unix Domain Socket / 共享内存）或跨容器共享本地目录传递业务音频、转写或制品。
2. **本地临时存储有界且私有**：每个进程 / 容器的 `/tmp` 仅用于当次进程内有界流式编解码或重采样缓冲；任何跨进程传递的输入资产（`Asset`）与输出制品（`Artifact`）必须经对象存储私有 `staging` 写入、校验摘要与长度后，在 PostgreSQL 事务中原子发布引用。
3. **显式节点、角色与实例身份**：每个运行进程必须显式配置所属物理 / 虚拟节点标识（`node_id`）、进程角色（`process_role`）与唯一实例标识（`instance_id` 或 `gateway_id`）。租约、Fencing Token、实时会话 Owner 和容器启停意图均绑定明确的节点与实例身份，不依赖进程 PID 或 `localhost` 隐式推断。

### 2. 五层解耦架构与多节点扩展契约

| 架构平面 | 包含组件 / 角色 | 集群就绪架构契约 | 单机 MVP 部署形态（`Node=1`） |
| --- | --- | --- | --- |
| **1. 北向入口层** | 统一入口代理（Nginx / Envoy）、`admin`、`workbench` | 统一承接 TLS、Host / Origin 与 API 路由；REST 请求无状态负载均衡，`/v1/realtime` 按会话协商路由至固定 Owner | 单入口或回环端口绑定；前端静态容器与后端 API 保持同源路由契约 |
| **2. 无状态控制与会话层** | `api`、`workbench-api`、`realtime-gateway` | `api` 多副本通过 PostgreSQL 协调全局幂等与配额；`realtime-gateway` 多实例各自承载不同会话，会话在 `admitting` 阶段原子绑定固定 `owner_gateway_id` | 单机运行独立进程角色或最小容器集合；每个进程显式声明 `node_id=node-local-1` 与 `instance_id` |
| **3. 协调与后台执行层** | `scheduler`、`job-worker`、`workbench-export-worker` | `scheduler` 按逻辑资源池持有带 `fencing_token` 的 PostgreSQL 租约（`scheduler_pool_leases`）；Worker 通过 `FOR UPDATE SKIP LOCKED` 并发领取步骤并核验 `attempt` 与 `fencing_token` | 1 个 `scheduler` 持有全部池租约，1 个 `job-worker` 消费任务；每个进程独立管理有界连接池 |
| **4. 算力执行与节点控制层** | `execution-gateway`、`node-controller`、各类推理引擎容器 | 每个 `execution-gateway` 绑定 `node_id` 与受管引擎入口，串行化 `submit_intent` 屏障；`node-controller` 按 `target_node_id == self.node_id` 认领持久启停意图 | 本机部署 1 个 `execution-gateway`（`gateway-1`）与 1 个本地受限控制器，纳管本机推理容器 |
| **5. 共享权威数据层** | PostgreSQL、S3 兼容对象存储（MinIO） | 按 `speech`、`gateway_control`、`workbench` 划分表所有权与数据库凭据；对象存储隔离 `staging/` 与已提交 `artifacts/` | 单实例 PostgreSQL 16 与单实例 MinIO 容器，仅切换环境变量 DSN / Endpoint 即可对接外部高可用集群 |

### 3. 三级资源拓扑与同机父物理预算

为同时满足单台统一内存主机（如 Linux ARM64 / NVIDIA GB10）的防超卖保护与未来多 GPU 节点的横向扩容，资源治理采用**数据驱动的三级拓扑模型**：

```text
HostNode (host_node_id, 节点级共享父物理预算: 统一内存 / 显存 / CPU / 临时磁盘)
  └── EngineInstance (instance_id, host_node_id, gateway_id, binding_revision_id, generation, 常驻与增量槽位)
        └── ResourcePool (pool_id, 逻辑能力池: asr-realtime / tts-realtime / voice-creation 等)
```

1. **按节点核算父物理预算**：映射到同一 `host_node_id` 的所有运行实例与按需加载峰值共享该节点的父物理预算；不同 `host_node_id` 的物理预算彼此独立。
2. **跨层级原子准入**：Scheduler 在分配容量租约或编排按需实例（如 VoiceDesign）加载时，在同一 PostgreSQL 事务中同时校验并扣减全局 / 工作空间配额、目标 `EngineInstance` 槽位以及所属 `HostNode` 的父物理预算余量。
3. **零代码扩容算力节点**：MVP 单机阶段 `host_nodes` 仅注册 `node-local-1` 一条记录；集群扩容时只需新增算力节点记录并启动对应节点的 `execution-gateway`、`node-controller` 与模型实例，准入事务与调度算法无需改动。

### 4. 实时会话路由、执行网关接管与节点控制器边界

1. **实时会话固定 Owner 与显式重连**：
   - `POST /v1/nexus/realtime/sessions` 创建会话后，`WS /v1/realtime?session_id={session_id}` 路由至目标 `realtime-gateway` 实例并在 `admitting` 阶段原子写入 `owner_gateway_id` 与 `stream_epoch`。
   - 实时网关实例故障退出后，当前连接断开并触发关联 Operation 取消协调；客户端重连建立新会话或新 `stream_epoch`，明确报告样本缺口（`nexus.session.gap`），不在多节点间同步实时 PCM 内存缓冲。
2. **Execution Gateway 两阶段提交屏障与接管封锁**：
   - `execution-gateway` 独占其受管引擎的网络入口与凭据，严格执行“核验租约 / attempt / 策略版本 / fencing token → 持久化 `submit_intent(submitting)` → 外发引擎并关联 `engine_request_id` → 收集完成或 abort 证据”。
   - 更换同一引擎入口的网关 Owner 前，必须先封锁旧网关入口并协调全部 `submitting` 与在途请求；无法逐请求确认停止时，由节点控制器排空或重建受控引擎实例，在取得停止证据前相关容量保持 `quarantined`。
3. **节点级受限部署控制器（Node-Scoped Reconciler）**：
   - Scheduler 将实例准备与空闲回收意图写入持久控制表 `runtime_instance_intents`（指定 `target_node_id`、批准模板、配置摘要、实例 `generation` 与 `fencing_token`）。
   - 部署在各算力节点上的受限控制器仅认领 `target_node_id == self.node_id` 的意图，执行本机容器启停并回报容器退出与资源释放证据；业务 API、Worker 与工作台不挂载 Docker socket，也不直接调用容器运行时。

## 影响

- `backend` 与 `execution-gateway` 的进程配置统一包含 `node_id`、角色与实例身份，单机 Compose 与本机开发脚本从首日起按显式身份运行。
- P0 机器契约与数据库迁移需将 `host_node_id`、`gateway_id`、`instance_generation`、`fencing_token`、`scheduler_pool_leases` 与 `runtime_instance_intents` 作为基础结构一次性冻结，避免 P1–P5 返工。
- 单机 MVP 交付聚焦于在单节点拓扑上跑通上述集群就绪状态机、事务边界与受控故障恢复；跨主机网络策略、多副本负载均衡、外部高可用 PostgreSQL / 对象存储切换及多节点故障注入属于 P5 生产资格验证范围。
- `accepted` 表示该集群就绪架构与单机 MVP 同构部署原则已确认，不代表多节点运行、自动故障接管或生产高可用已经交付和验证。

[决策索引](README.md) · [整体技术方案](../architecture/technical-blueprint.md) · [资源治理](../architecture/runtime-governance.md) · [项目主设计](../speechnexus-product-architecture.md)
