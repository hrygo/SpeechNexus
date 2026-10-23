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

领域与应用层的规则不需要 HTTP 或容器即可验证；HTTP 外壳、持久意图存储与容器 adapter 另有
真实进程级探针与合成容器探针（见第 2、3 节结尾）：

```text
scheduler ──HTTP/组件凭据──▶ interfaces/http ──▶ NodeRuntimeControlService ──▶ IntentStore
                                                    │                          (本地持久意图)
                                                    │                          │
                                                    │                          └─ 幂等键 (node_id, intent_id)、版本
                                                    └──prepare/stop/inspect──▶ NodeRuntimePort ──▶ 容器 daemon
scheduler ◀──observation/reconcile 快照─────────────┘
```

| 端口 / 位置 | 职责 | 实现状态 |
| --- | --- | --- |
| `domain/intents.py` | 意图身份、留宿内存值、观察状态、停止证据与实例头派生 | 已实现并有单元测试 |
| `domain/templates.py` | 批准模板注册表：镜像摘要固定、允许池、内存架构与峰值上限 | 已实现；模板清单与部署绑定待接入 |
| `domain/ports.py` | `IntentStore`、`NodeRuntimePort`、reconcile 快照 | 协议草图；持久化与容器 adapter 待交付 |
| `application/runtime_control.py` | 认领意图、幂等重放、generation / fencing 门、drain 门、观察折叠与 reconcile | 已实现并有单元测试 |
| `infrastructure/docker_api.py` | 本地 daemon 的最小 Engine API 客户端（固定操作集合，不接任意镜像/挂载/shell） | 已实现并有 double 测试 |
| `infrastructure/docker_runtime.py` | 受控容器 adapter：镜像摘要核对、标签化实例身份、prepare/stop/inspect 与停止双证据 | 已实现；合成容器真实探针见下文 |
| `infrastructure/template_config.py` | 批准模板配置文件加载（未知字段拒绝） | 已实现并有单元测试 |
| `infrastructure/sqlite_intent_store.py` | 本地持久意图事实（版本守卫、schema 版本、WAL + 同步提交） | 已实现并有单元测试 |
| `interfaces/http/` | `internal/runtime-control` 路由、组件凭据、`X-Request-ID`、契约错误信封与 `/healthz`、`/readyz` | 已实现并有单元测试与合约校验 |
| `app.py` / `main.py` / `settings.py` | 组合根、进程入口 `speechnexus-node-controller`、启动即校验的配置 | 已实现；部署单元待 A 排队集成 |

`tests/fake/controller.py` 与 `tests/fake/docker_api.py` 提供确定性的 `InMemoryIntentStore`、
`ScriptedNodeRuntime` 与容器 daemon double，
它们不接触真实容器；`tests/contract/test_node_controller_contract.py` 断言本包的错误码、
状态、池与内存架构枚举、请求字段与契约逐项相等，并把本包可产生的每种意图状态序列化成
契约响应形状校验，避免出现第二套字段定义。

## 2. 受控容器 adapter 与批准模板

adapter（`DockerNodeRuntime`）通过本地 daemon socket 操作容器，并把「能运行什么」全部留在
批准模板里：镜像引用与本地 image id、argv、readiness 规则、GPU 请求、只读制品绑定都由模板
声明，请求本身无法放宽其中任何一项。

- **镜像核对**：创建容器前 inspect 本地镜像，`image id` 必须等于模板的 `image_digest`；
  不匹配报 `unapproved_runtime_template`，不自动 pull。
- **实例身份**：容器写入 `nexus.*` 标签（node / intent / instance / generation / template /
  config digest / pool / gateway / binding / manifest / fencing token），因此控制器重启后仍能
  按 `(instance_id, instance_generation)` 找到自己的实例，也能识别身份不一致的容器。
- **容器加固**：`Privileged=false`、`CapDrop=["ALL"]`、`no-new-privileges`、`RestartPolicy=no`、
  `Init=true`、不发布宿主端口，绑定只来自模板的只读制品目录；GPU 请求按模板的
  `gpu_device_count` 生成 `DeviceRequests`。
- **prepare 幂等**：同一 `(instance_id, generation)` 已有容器且标签摘要一致时复用，不再创建
  第二个；标签摘要不同则报 `intent_conflict`。
- **readiness**：模板声明 `process_running` 或 `tcp_port_accepting:port`。容器运行但未通过
  readiness 时报告 `loading`（保持 peak），通过后才报告 `ready`（切 steady）。
- **停止与证据**：先优雅停止、等待退出、删除容器，再 inspect 确认不存在，才写出
  `process_or_container_exited` 与 `resources_released_confirmed` 双确认证据；等待超时、删除
  失败或容器仍存活时只报告当前观察，不伪造证据（意图留在未解决或 quarantine）。
- **退出码语义**：退出码 0 与 143（SIGTERM）都是正常终止；容器是否成功服务由网关的执行事实
  判断，控制器不把退出码当作执行结果。
- **配置来源**：批准模板来自部署配置文件（`template_config.py`，未知字段拒绝），不是请求。
  当前 adapter 不设 `Memory` 上界，容器内存只作为观察峰值报告；保留由 Scheduler 记账。
- **daemon 故障**：daemon 无应答或返回错误状态时，adapter 报
  `runtime_controller_unavailable`（可重试），调用方不会拿到假的准备成功或停止成功；模板与实例
  身份等**领域拒绝**仍原样抛出，不被这条翻译吞掉。

合成容器上的真实 prepare → ready → stop 证据（含为何需要 `Init=true`）见
[B02 容器生命周期探针](../audits/2026-10-24-b02-controller-container-lifecycle-probe.md)。

## 3. HTTP 外壳、组件凭据与本地持久意图

`interfaces/http/` 实现契约里的三个控制操作与两个运维端点，进程入口是
`speechnexus-node-controller`（`app.py` 为组合根，`main.py` 用 uvicorn 工厂启动）。

- **路由与节点范围**：路径里的 `node_id` 必须等于本控制器的 `node_id`，否则
  `target_node_mismatch`；路由只把 HTTP 转成应用服务调用，节点范围、幂等、generation / fencing
  与停止证据的规则仍留在领域与应用层。
- **组件凭据**：每个节点一份 bearer 组件凭据（`SPEECHNEXUS_CONTROLLER_COMPONENT_TOKEN`），与
  scheduler、gateway、公共和 admin 凭据分开，使用常量时间比较；缺失或不匹配返回
  `401 component_credential_invalid`。它是组件凭据，不建立用户身份，也不放宽节点范围检查。
- **错误信封**：契约枚举的每个拒绝码都映射到唯一 HTTP 状态，映射表由单元测试断言无遗漏；
  `409` 是节点范围 / 幂等 / generation / fencing / drain 冲突，`422` 是 schema 或批准模板校验
  失败，`404 runtime_intent_not_found` 表示本节点没有该意图，`503
  runtime_controller_unavailable` 表示节点运行时没能完成或核验该动作——此时意图保持未解决，
  绝不报告成已停止。请求体不符合冻结形状时同样返回契约错误信封，而不是框架默认形状。
- **`X-Request-ID`**：校验客户端传入的相关 ID，非法或缺失时生成一个并在响应回显。
- **阻塞边界**：应用服务是同步的（要 inspect 本地 daemon 并等待容器退出），路由把调用放到工作
  线程执行，不阻塞事件循环。
- **本地持久意图**：`infrastructure/sqlite_intent_store.py` 把一个节点的意图事实保存在本地
  SQLite 文件里（WAL、`synchronous=FULL`、短事务、`version` 守卫、显式 schema 版本），控制器
  重启后仍能重放、识别并继续折叠观察。它**不是第二份协调权威**：权威意图、预算与结算仍在
  Scheduler 的数据库里，控制器只通过协议报告事实；因此这里没有共享库耦合，也没有跨库迁移成本。
- **本地共享编解码**：HTTP 响应与持久记录都由 `domain/payloads.py` 从同一份领域事实投影出来，
  避免出现第二套字段定义；合约校验会同时验证生产投影与冻结响应形状。

真实进程级证据（生产入口进程、一次性真实凭据、真实 daemon、真实持久存储、重启后意图仍在）见
[B02 控制器 HTTP 外壳与持久意图探针](../audits/2026-10-24-b02-controller-http-shell-probe.md)。

## 4. 可执行规则

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

## 5. 部署接口（交 A 排队集成）

控制器进程可以独立运行，但**还没有部署单元**：镜像、Compose 服务、卷与凭据注入属于 A 的部署
集成范围。这里只固定进程需要的接口，部署侧不复制第二份定义。

- 入口：`speechnexus-node-controller`（uvicorn 工厂 `speechnexus_node_controller.app:create_app`）。
- 配置（`SPEECHNEXUS_CONTROLLER_*`；缺少组件凭据、批准模板文件或意图存储路径时启动失败）：
  `COMPONENT_TOKEN`、`APPROVED_TEMPLATES_FILE`、`INTENT_STORE_PATH`；可选 `NODE_ID`、`HOST`、
  `PORT`（默认 8020）、`DOCKER_SOCKET_PATH`、`DOCKER_REQUEST_TIMEOUT_SECONDS`、
  `STOP_TIMEOUT_SECONDS`、`EXIT_WAIT_SECONDS`、`READINESS_TIMEOUT_SECONDS`、`LOG_LEVEL`。
- 健康检查：`/healthz` 报进程存活，`/readyz` 检查本地容器 daemon 与意图存储，任一不可用即
  `503`；两者遵循 [运维契约](../../contracts/operations.openapi.yaml)。
- 需要挂载：容器 socket（只给控制器进程，业务 API、worker 与工作台不挂载）、批准模板文件、
  意图存储目录。
- 传输安全：组件凭据不替代 TLS；控制面内部仍需 TLS 或等价保护。

## 6. 当前未实现与交接

- 没有 `deploy/` 与 Compose 单元：镜像、健康检查、卷与凭据注入仍需与 A 的部署集成排队交付，
  第 5 节固定了它需要的接口。
- 批准模板清单（`approved_template_id` → 固定镜像摘要、峰值上限）需要 B00 制品摘要与
  部署绑定共同落地，当前只有校验规则、注册表与配置文件加载。
- warmup 门尚未实现：模板不能声明 `requires_warmup_before_ready`，readiness 只由模板的
  `process_running` / `tcp_port_accepting` 判定；加载期的 peak 留宿仍按“未 ready”处理。
- 真实引擎容器（Qwen3-ASR / TTS）的 ready 与停止、GPU `DeviceRequests` 路径、容器内存上界与
  统一内存语义、控制器重启后的重新发现与 reconcile 只有合成容器证据，没有任何真实引擎资格
  结论。
- 真实多实例竞争、跨节点 `intent_id` 复用、控制器不可用时的接管与恢复没有真实证据；进程被
  杀死后的恢复只验证到“本机重启后意图事实仍在”，不等于故障接管已具备。这些属于
  [B02 #29](https://github.com/hrygo/SpeechNexus/issues/29) 的 G1/G2 检查项。
- 本包不写 Scheduler 的预算表；`create_runtime_intent`、`observe_runtime` 的事务侧实现由
  runtime 端口维护者提供，控制器通过协议报告事实。

[架构文档](README.md) · [整体技术方案](technical-blueprint.md) · [应用事务方案](coordination-transactions.md)
