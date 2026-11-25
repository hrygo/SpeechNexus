---
title: 引擎运行时上 k8s 之后：下一迭代规划
status: active
date: 2026-11-26
scope: node-controller, deploy/cluster, backend 准入与就绪
---

# 引擎运行时上 k8s 之后：下一迭代规划

引擎已经跑在 Pod 上：三条 Job、同一实例身份、停掉 docker 引擎后仍然成功，产物逐字节一致。这份规划回答下一件事——**这套东西拿去当企业级基座，还差什么**，以及按什么顺序补。

本文只定顺序、判据和边界，不复述已完成工作的过程记录（见 [引擎运行时切到 Kubernetes](2026-11-25-engine-runtime-k8s.md)）。

## 1. 完成之后的真实形状

```
应用面（Mac，k8s 命名空间 speechnexus）
  api ── postgres ── minio ── bridge ─┐
  job-dispatch（Deployment，持桶租约，单副本）
  engine-readiness / -renew（CronJob）
                                       │ SSH 隧道
算力面（MegaCube k3s，cube-ccba）      ▼
  engine-instance-asr-1-2（Pod，runtimeClassName: nvidia，无 nvidia.com/gpu）
  engine-shim（host 网络，18190 → Pod）
  gateway、node-controller（仍是 compose 容器，host 网络）
```

两处刻意的不对称，都必须说明白：

| 单元 | 形态 | 为什么还没进集群 |
| --- | --- | --- |
| 引擎 | **Pod** | 本计划的目标，已完成 |
| 控制器 | compose 容器 | 镜像从未进过 k3s containerd（`ErrImagePull` 403），清单 `deploy/cluster/compute/controller-deployment.yaml` 已就绪未应用 |
| 网关 | compose 容器 | 依赖 loopback 与 host 端口，迁移方案未定 |

## 2. 三个结构性问题

这次迁移暴露的不是一串 bug，是三处**结构缺口**。按它们排序，而不是按现象排序。

### 2.1 资源账本没有释放路径

过期、取消、失败三种结束方式里，只有"成功"走了完整的释放路径。其余两种会在 `admission_leases`、`admission_attempts`、`engine_instances.held_slots`、`host_nodes.held_*` 上留下不降的计数（#1248、#1235）。

后果不是"少了一点容量"，而是 `AdmissionRuntime.__post_init__` 的 `Batch slots exceed all unreleased slots` 被折叠成 `engine_state_mismatch`，运维照文案重新注册引擎、注册成功、503 照旧——**排障方向被文案带偏**。

### 2.2 拒绝原因在到达运维之前被折叠

三种内部拒绝共用一个对外码（#1245），就绪判定有两份实现且结论相反（#1246），驱动的 quarantine 原因不落库不进接口（#1241）。三者叠加的效果是：**平台说不出自己为什么拒绝**。

### 2.3 运行态只有 kubectl 看得见

回执续期、派发器存活、引擎地址，现在都要人去 `kubectl` 看。CronJob 报警（#1256）只解决了"有人看的时候能看见"，没解决"接口能回答"（#1253）。

## 3. 迭代顺序

判据一律沿用本仓库的规矩：**新增检查必须附一次"注入被禁止的东西即失败"的实测**；结论区分已实现 / 已验证 / 未验证。

### I1 — 账本闭合（最高优先，解锁其它一切）

没有它，任何真实负载都会把平台锁死，后面的迭代都在修同一种故障。

| 切片 | 内容 | 判据 | 依据 |
| --- | --- | --- | --- |
| D1 | 过期未派发的 Job：释放租约、归零 `held_*`，并留下可解释的终态 | 造一个 120 s 内未派发的 Job → 账本回零、后续建 Job 成功；注入"只改状态不释放" → 红 | #1248 |
| D2 | 取消未提交执行的 Job：同上 | 取消后账本回零；注入"跳过释放" → 红 | #1235 |
| D3 | 释放路径只留一个定义点，三条结束路径共用 | 全仓同一判定文本只出现一次 | 判据纪律 |

### I2 — 说清楚为什么拒绝

| 切片 | 内容 | 判据 | 依据 |
| --- | --- | --- | --- |
| R1 | 就绪判定合并为一份实现，CLI 与 HTTP 走同一函数 | 造出"两份结论相反"的输入 → 断言两份一致；注入旧分支 → 红 | #1246 |
| R2 | `engine_state_mismatch` 拆成可区分的对外码，至少把"账本不一致"与"实例真的不匹配"分开 | 每种原因各一条反例，码不重复 | #1245 |
| R3 | quarantine 原因落库并经接口返回 | 注入一次失败 prepare → 接口能读出原因 | #1241 |

### I3 — 双机形态收口

| 切片 | 内容 | 判据 | 前置 |
| --- | --- | --- | --- |
| P1 | 控制器进集群（清单已就绪） | Pod Ready、`/healthz` 200、经 SA 创建并停止一个引擎实例 | **镜像进 containerd**（见决策 D1） |
| P2 | 引擎地址由实例事实驱动，删掉 `ENGINE_ADDRESS` 手写常量 | Pod 重建换 IP 后无需人工干预，续租仍成功 | P1 或独立改法 |
| P3 | 回执续期状态进 `admission-readiness` 接口 | 续租失败时接口能说出"已连续 N 次失败" | R1 |
| P4 | 显存额度在 k8s 下取证 | Pod 内 `torch.cuda.get_device_properties(0).total_memory` 与批准值比对，改小后断言红 | 独立，见 #1092 C |
| P5 | Pod 从创建到可服务的实测耗时 | 三次采样给出预热预算建议值，替换当前 120 s 拍定值 | 独立 |

### 不在本规划内

- 多实例端口分配（`hostNetwork` 下 18190 写死）：属于容量规划，先保持单实例并写明限制
- 网关进 k8s：形态未定，先不动
- 35B 与 ASR 共存显存余量：需要真实并跑，排在 P4 之后

## 4. 与既有卡的对应

本规划的切片不新开重复卡，都落在既有工作包下：

| 工作包 | 覆盖切片 | 说明 |
| --- | --- | --- |
| #338 执行、算力与停止结算 | D1–D3、R1–R3 | 本轮八条 issue 已挂到 X-RECOVER / X-NODE / X-ADMIT |
| #1087 算力面装配 | P1 | 控制器与网关的容器装配，引擎部分已完成 |
| #1088 跨主机端到端与五类故障演练 | I1 的验收场景 | 本会话做的是三段成功对照与 stop/fencing，五类故障演练**未做** |
| #1092 显存额度取证 | P4 | 切片 C 原样保留，只换成 k8s 下的取证口径 |
| #1250 | 收尾 | 从算力面 `compose.yaml` 删除已取代的 `dispatch` 服务 |

## 5. 需要决定的事

1. **控制器镜像怎么进集群**（阻塞 P1）：MegaCube 的 sudo 密码、一个内网 registry、或接受控制器长期留在 compose——三者选一。
2. **混合形态是否可接受为开发基线**：控制器 compose + 引擎 Pod。若可接受，P1 降级为"生产化前置"而不是当前迭代项。
3. **显存取证口径**（P4）：继续用容器内 `torch.cuda.get_device_properties` 读数，还是改由调度层 cgroup/显存计量读数。
4. **多实例端口策略**：`hostNetwork` + 写死 18190 的单实例限制，是接受、还是现在就要端口分配。