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
| `image_reference` + `image_digest` | `image` 用**批准的原样引用**；完整性由**运行中容器的 imageID** 核对 `image_digest` 证明，与 `docker_runtime` 同一不变量 |
| `command` | `command` + `args`（模板存的是 argv，原样拆） |
| `container_memory_limit_bytes` | `resources.limits.memory` |
| `max_peak_memory_bytes` | 不设 requests；峰值由 `--gpu-memory-utilization` 约束（同 `qwenpaw-asr` 的做法） |
| `gpu_device_count` | `runtimeClassName: nvidia` + `NVIDIA_VISIBLE_DEVICES`；**不**写 `nvidia.com/gpu` |
| `read_only_binds` | `hostPath` + `readOnly: true` |
| `network_name` | **无等价**，见 3.4 |
| `readiness` | Pod `startupProbe`/`readinessProbe` 与控制器预热预算并存（见 3.3） |
| `engine_auth` | 不变 |

**里程碑实测（2026-11-25，MegaCube k3s）**：驱动为 `template-asr-1.7b` 产出的 Pod 体直接 apply 到真实集群，调度、`runtimeClassName: nvidia`、hostPath 只读挂载、argv→`args`、`NVIDIA_VISIBLE_DEVICES`、就绪探针与 12 GiB 上限全部通过；`/health` 200、`/v1/models` 返回 `qwen3-asr-1.7b`、容器内 `nvidia-smi` 见 `VLLM::EngineCore`。

同一 Pod 体若把 `image` 写成 digest 形式则拉不下来：`local/qwen3-asr-vllm@sha256:…` 对 containerd 是**另一个镜像名**，本地构建、从未推送的镜像因此无法解析。所以这里不按 digest 拼引用，而是像 `docker_runtime` 那样按批准引用运行、再核对运行中容器的 `imageID`。

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

### 4.2 S5 验收记录（2026-11-25 完成）

同一个 Job 定义、同一个实例身份，分别由 compose 引擎与 k8s 引擎执行，产物逐字节一致。

| 路径 | Job | 引擎 | 结果 | 产物 |
| --- | --- | --- | --- | --- |
| compose | `job-991788410b564ddda2eaca46126c8ac5` | docker 容器 `speechnexus-asr-1`，`127.0.0.1:18190` | `succeeded`，`cleanup_state=complete` | 74 B，`sha256:da4d6eb2…e0c53` |
| k8s #1 | `job-31771a045dc94435badba668beb8e9fe` | Pod `engine-instance-asr-1-2`，`10.42.0.103:18190` | `succeeded`，`cleanup_state=complete` | 74 B，**同一摘要** |
| k8s #2 | `job-7f29f824398c4ae394538577a3c8a7a4` | 同上，**docker 引擎已停止** | `succeeded`，`cleanup_state=complete` | 74 B，**同一摘要** |

产物内容：`{"text":"The quick brown fox jumps over the lazy dog.","duration_ms":3447}`。

三条 Job 的实例事实一致：`execution_revision=1`、`cleanup_state=complete`、同一 `effective_configuration`（`model-asr-minimal` revision 1、`policy_revision` 1、`degradation=none`）。**唯一不同是执行它的引擎进程**，这正是本切片要证明的那一项。

第 3 条是取证关键：把 docker 引擎 `docker stop` 之后重跑同一 Job 仍然成功，且 Pod 日志出现 `POST /v1/audio/transcriptions HTTP/1.1 200 OK`。所以它不可能由 compose 引擎代劳。

引擎侧此前已验证：Pod `1/1 Running`、`/health → 200`、`/v1/models → qwen3-asr-1.7b`；驱动经 in-cluster ServiceAccount 走真实 API server 报 `prepare -> ready`；喂 docker 来源摘要时驱动报 `quarantined` 并点名两个摘要。

#### 4.2.1 验收过程中发现的真实阻塞（与本切片无关）

S5 之前连续多轮无法进行，原因是应用面自身，**compose 路径同样无法建出 Job**，因此当时没有可对照的基准。三点已分别建 issue：#1245（三种内部拒绝码折叠成一句「重新注册」）、#1246（就绪判定两份实现，CLI 与 HTTP 结论相反）、#1248（派发前过期的 Job 永久泄漏准入租约，并把平台锁成 `engine_state_mismatch`）。

#### 4.2.2 派发器的部署位置

派发器跑在**算力面**时，每一轮 claim 都要跨 SSH 隧道回应用面数据库，实测一轮约 115 s；而准入截止由绑定 `timeout_seconds` 决定（本次 120 s），于是 Job 常在派发前过期，正好落进 #1248 的泄漏。

把同一个派发器放进应用面集群（本地数据库往返），一轮 claim 实测 **0.15 s**，Job 从 `queued` 到 `succeeded` 约 10 s。

结论：**派发器必须与应用面数据库同域部署**，算力面只跑引擎与网关。这一点写入下一轮的实施项，不由本切片顺带改动。

#### 4.2.3 S3 的 `stop` / `recover_stop` 在真实集群上驱动（2026-11-25）

在 `controller-probe`（in-cluster ServiceAccount `speechnexus-node-controller`）里用仓库源码驱动真实 API server：

```
STEP1 created engine-instance-stop-probe-1 phase Running uid 19d2b02d-…
STEP2 stop -> stopped | error engine_exit_code_nonzero
STEP2 pod after stop: None
STEP3 rebuilt engine-instance-stop-probe-1 uid 6646a16c-… | new uid: True
STEP4 recover_stop -> quarantined
STEP4 FENCE_HELD: True
```

四点值得记：

- `stop` 确实让 Pod 从 API 里**消失**（不是只报状态）
- `engine_exit_code_nonzero` 是如实的：探针 Pod 跑的是 `sleep infinity`，被删除时非零退出，驱动拒绝把它报成干净退出
- **UID fencing 在真实集群上成立**：同名重建得到新 UID 之后，重放同一次 stop 报 `quarantined`，新 Pod **原样活着**，旧代次的意图没有误杀它
- 引擎 Pod `engine-instance-asr-1-2` 未受影响，`/health` 仍 `200`

#### 4.2.4 仍未验证

- 控制器作为**常驻集群内服务**：镜像 `speechnexus-node-controller` 只在 docker，k3s containerd 无法拉取 `local/` 镜像，导入需要 root（或一个内网 registry）。`prepare` 与 `stop` 都是在一次性 Pod 里用仓库源码驱动验证的，不是由常驻控制器发出的。
- 由**控制器管辖**的引擎实例的 `stop`：手工启动的引擎容器 `managed-by=speechnexus-operator-manual`，控制器不接管，也没有释放路径。上节的 stop 针对的是探针自建的 Pod。
- 本次 k8s 路径的就绪回执仍是按 docker 引擎签署的（回执窗口 24 h 内有效），执行时端点已指向 Pod。回执证明的是实例身份，不是「哪个进程在服务」。

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