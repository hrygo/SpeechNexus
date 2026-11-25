---
title: 引擎运行时切到 Kubernetes：切片与判据
status: active
date: 2026-11-25
scope: node-controller/src/speechnexus_node_controller
---

# 引擎运行时切到 Kubernetes：切片与判据

把算力面的**引擎实例**从 Docker 容器改成 Kubernetes Pod，GPU 走 `runtimeClassName: nvidia`。目标不是"用上 k8s"，是让 GPU 由调度层管、丢掉跨机自维护的 SSH 隧道、让实例具备声明式自愈。

## 1. 决定与边界

**决定**：只换运行时驱动，不动 `NodeRuntimePort` 契约、不动批准模板的语义、不动网关。

**为什么是这个边界**：`NodeRuntimePort`（`node-controller/src/speechnexus_node_controller/domain/ports.py:46`）四个方法 `prepare` / `stop` / `inspect` / `recover_stop`，每个都接收已记录的 intent 与批准模板，返回 `InstanceObservation`。这是现成的接缝——换驱动就是加第二个 adapter，在 `node-controller/src/speechnexus_node_controller/app.py:85` 的 `runtime=DockerNodeRuntime(...)` 处按配置选择。**契约改一个字，网关、控制器、快照证据都会跟着动，收益为零。**

**不在范围**：应用面（Mac 侧 k8s）不动；网关不进 k8s 的迁移不在本计划；`qwenpaw` / `langfuse` 两个 namespace 不碰。

## 2. 现状（2026-11-25 实测）

| 项 | 实测 |
| --- | --- |
| 控制器拉起引擎 | `node-controller/src/speechnexus_node_controller/infrastructure/docker_runtime.py` + `node-controller/src/speechnexus_node_controller/infrastructure/docker_api.py`，走 Docker API |
| 组合点 | `node-controller/src/speechnexus_node_controller/app.py:85` `runtime=DockerNodeRuntime(...)`，单点 |
| 契约 | `NodeRuntimePort` 四方法，全部 `(RuntimeInstanceIntentRequest, ApprovedRuntimeTemplate) → InstanceObservation` |
| 模板字段 | `image_reference` / `image_digest` / `max_peak_memory_bytes` / `container_memory_limit_bytes` / `gpu_device_count` / `read_only_binds` / `network_name` / `command` / `readiness` / `engine_auth` |
| MegaCube k3s | 单节点 `cube-ccba` v1.35.5，`runtimeclass nvidia` **存在** |
| GPU 在 k8s 里可用 | **已证**：`qwenpaw` namespace 的 `qwenpaw-asr` 正在 8020 提供 `qwen3-asr-0.6b` |
| GPU 暴露形态 | `runtimeClassName: nvidia` + `NVIDIA_VISIBLE_DEVICES=all` + `NVIDIA_DRIVER_CAPABILITIES=compute,utility`，**不申请 `nvidia.com/gpu`** |
| 网络 | `hostNetwork: true`，端口写死（与 compose 的 `network_mode: host` 同构） |
| 模型 | `hostPath` 挂载（`qwenpaw-asr` 挂 `/home/huangzhonghui/qwen3-asr-models/Qwen3-ASR-0.6B`） |

**「`nvidia.com/gpu` 不在 allocatable」不等于「GPU 不可用。** 这台机器用的是 RuntimeClass 处理器模式，不是 device plugin 资源申请。判据落在这里：能起 Pod 并让 vllm 报告 GPU，才算可用。

## 3. 关键设计决定

### 3.1 模板 → Pod 规格

| 模板字段 | Pod 侧 |
| --- | --- |
| `image_reference` + `image_digest` | `image` 用 digest 形式固定，不用 tag |
| `command` | `command` + `args`（模板存的是 argv，原样拆） |
| `container_memory_limit_bytes` | `resources.limits.memory` |
| `max_peak_memory_bytes` | 不设 requests；峰值由 `--gpu-memory-utilization` 约束（同 `qwenpaw-asr` 的做法） |
| `gpu_device_count` | `runtimeClassName: nvidia` + `NVIDIA_VISIBLE_DEVICES`；**不**写 `nvidia.com/gpu` |
| `read_only_binds` | `hostPath` + `readOnly: true` |
| `network_name` | **无等价**，见 3.4 |
| `readiness` | Pod `startupProbe`/`readinessProbe` 与控制器预热预算并存（见 3.3） |
| `engine_auth` | 不变 |

### 3.2 实例身份与 fencing

`instance_id` / `instance_generation` 是控制器的权威身份，不是 Pod 名。映射：

- Pod 名 = `engine-{instance_id}-{generation}`，经 label `speechnexus.io/instance-id`、`speechnexus.io/generation` 标注
- **fencing 用 Pod UID**：只有 UID 匹配创建时记录的 UID 才允许 stop。同名 Pod 被重建会换 UID，旧停止意图无法误杀新实例
- Pod 名带 generation，使旧代与新代不可能撞名

### 3.3 就绪与预热

`ReadinessRule` 的判定仍在控制器侧（`node-controller/src/speechnexus_node_controller/application/readiness.py`），Pod 的探针是**传输层**的健康，两者不是同一件事，不能互相替代。Pod Ready ≠ 实例可提交——预热的 `HTTP_WARMUP_OK` 仍要控制器实测一次。

### 3.4 `network_name` 的处理

Docker 里 `network_name` 让实例进一个专用网络；k8s 没有等价物。三个选项，**选第 2 个**：

1. 忽略字段——静默改变隔离语义，不可接受
2. **模板声明 `network_name` 时，k8s 驱动拒绝该模板**（`unsupported` 观察），逼调用方明确——隔离语义不可被悄悄放宽
3. 用 NetworkPolicy 模拟——引入第二套隔离定义点，与「不变量只有一个定义点」冲突

### 3.5 停止证据

Docker 侧靠容器 exit code + 检查点。k8s 侧：

- 删除 Pod 后必须等到**该 Pod 不再存在于 API**，而不只是"已发删除"
- 退出码从 `containerStatuses[].state.terminated.exitCode` 取；非正常退出记 `error_code`，不伪造成功
- `recover_stop` 沿用 `StopCheckpointStore` 的「先钉再推进」顺序，与驱动无关

### 3.6 控制器自己跑在哪

控制器要访问 k8s API。**控制器作为 Deployment 跑在集群内**，用 ServiceAccount + 最小 RBAC（只对 `pods` 在自己 namespace 内 `create/get/list/watch/delete`）。备选「宿主机挂 kubeconfig」是凭据落盘，不采用。

## 4. 切片

| 切片 | 内容 | 判据（注入必红） |
| --- | --- | --- |
| S1 | 抽出驱动选择：`SPEECHNEXUS_CONTROLLER_RUNTIME=docker\|k8s`，默认 `docker`；k8s 分支先只抛"未实现" | 把选择项写成默认 `k8s` → 走组合根的测试红；写回 `docker` → 绿 |
| S2 | `prepare`：创建 Pod、等 Ready 或预热超时、产出 `InstanceObservation` | 注入"不等 Ready 直接返回 ready" → 红；注入"Pod 创建失败仍返回 ready" → 红 |
| S3 | `stop` + `recover_stop`：UID fencing + 确认 Pod 真的消失 | 注入"不发删除就报 stopped" → 红；注入"忽略 UID 校验" → 同名重建后误杀，红 |
| S4 | `inspect`：读 Pod 相位 + 事件 → `ObservedInstanceState` | 注入"读不到就当 ABSENT" → 红 |
| S5 | 端到端：一个 Job 走 k8s 引擎跑完，与 compose 路径对照同一份证据 | 两条路径的实例事实字段逐项一致；不一致处必须能解释 |

S5 是验收点，不是加功能。

### 4.1 S1 实测（`74113822`，2026-11-25）

判据「默认改成 `k8s` → 现有控制器测试全红」**是错的**：实际 `5 failed / 381 passed`。控制器套件里 381 条测试注入 `ScriptedNodeRuntime` 或直接 patch `_build_dependencies`，从不读默认配置，因此默认值改动只击中真正走组合根的 5 条。夹具没有为了让数字变大而改动。

更值得记的是**注入 B**：让 `k8s` 分支悄悄 `return` 退回 Docker，**排除新补的测试后现有 382 条全绿**。也就是说，一个「选了 k8s 但实际还在用 Docker」的驱动会让整套双机演练结论失效，而它在切片前完全不可见。切片据此补了会红的测试。

同一切片留下的边界：测试夹具注入 service+readiness 的路径**绕过开关**。它是测试专用 seam，生产入口 `main → create_app → _build_dependencies` 不经过；要把守卫上提到 `create_app` 会改变现有注入语义，**留给 S2 定**。

## 5. 回退

- 驱动开关默认 `docker`，切回即恢复；**不改数据库、不改契约**，所以没有迁移回退问题
- k8s 侧只新增命名空间内对象，`kubectl delete ns` 即清空

## 6. 风险与未验证

| 项 | 状态 |
| --- | --- |
| GPU 显存额度机制（#1092 C 切片）在 k8s 下如何取证 | **未验证**，需要一次真实实例的 `torch.cuda.get_device_properties(0).total_memory` |
| 35B 模型与 ASR 实例共存时的显存余量 | **未验证**，40 G 可用，1.7B 模板要 12 G |
| Pod 从创建到 vllm 可服务的实际耗时 | **未验证**，决定预热预算 |
| `hostNetwork` 下多实例端口冲突 | 模板端口写死 18190，多实例需端口分配策略——**本计划不解决**，先按单实例 |
| 控制器的 ServiceAccount 权限是否够 | 未验证，S2 第一件事就是实测 |
| k8s 事件在 `inspect` 里是否比 docker inspect 更可靠 | 未验证 |