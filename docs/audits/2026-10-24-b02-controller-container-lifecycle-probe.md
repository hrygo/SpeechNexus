---
title: "B02 受限控制器容器生命周期探针"
status: in_progress
version: "0.1.0"
date: 2026-10-24
---

# B02 受限控制器容器生命周期探针

本记录给出受限节点控制器容器 adapter 的首个**真实生命周期证据**：在受权范围内使用合成
无业务容器验证 prepare → ready → stop 与停止双证据。它不证明真实引擎容器（vLLM ASR/TTS）
的加载、就绪、停止或资格，也不证明控制器 HTTP 外壳、持久意图存储与部署单元已交付。

## 1. 范围、环境与制品

探针不加载模型、不占用 GPU、不发布宿主端口、不读写业务语料，只创建一个 CPU 容器并在结束前
删除它。控制器只操作批准模板声明的镜像，探针模板是合成配置，不是产品模板。

| 项 | 事实 |
| --- | --- |
| 主机 | Ubuntu 24.04.5 LTS aarch64；容器运行 `/var/lib/docker`；socket `/var/run/docker.sock`（用户属 `docker` 组，rootful daemon） |
| 容器引擎 | Docker Engine 29.6.2 |
| 镜像 | `python:3.14.9-slim-bookworm`；本地 image id `sha256:7e0522950230cad1ec100ada6e3898c22c6bc82851289b4bdf0bf77c1c829cd5`；仓库摘要 `python@sha256:7e0522950230cad1ec100ada6e3898c22c6bc82851289b4bdf0bf77c1c829cd5` |
| 探针模板 | `template-probe-inert-tcp`：readiness `tcp_port_accepting:8080`、命令 `python -m http.server 8080`、池 `auxiliary`、GPU 请求 0、预留 peak 1 GiB / steady 256 MiB（仅作为报告值） |
| 探针实例 | `node-local-1` / `instance-probe-inert` / generation 1 |
| 复现入口 | `uv run python scripts/dev-env/node-controller-probe.py`（可加 `--image`、`--ready-timeout-seconds`） |

容器配置由 adapter 固定：`Privileged=false`、`CapDrop=["ALL"]`、`SecurityOpt=["no-new-privileges"]`、
`RestartPolicy={"Name":"no"}`、`Init=true`、无宿主端口，绑定仅来自模板声明的只读制品目录
（本探针为空）。探针另外确认了 `nexus.*` 标签写入容器，使运行中的实例在控制器重启后仍可按
`(instance_id, instance_generation)` 发现。

## 2. 实测结果

两次运行都经 `NodeRuntimeControlService` 走完整路径（先落意图、再调用节点运行时、按观察折叠
状态），并使用真实的 Docker Engine API。

**运行 A（adapter 尚未设置 `Init`）**

```json
{
  "image_id": "sha256:7e0522950230cad1ec100ada6e3898c22c6bc82851289b4bdf0bf77c1c829cd5",
  "prepare_state": "ready",
  "ready_seconds": 0.0,
  "observed_peak_bytes": 41328640,
  "stop_state": "succeeded",
  "stop_exit_code": 137,
  "container_removed": true,
  "leftovers": []
}
```

停止用了约 20 s（等于 adapter 的 stop 超时）后以退出码 137（SIGKILL）结束：容器内
`python -m http.server` 是 PID 1，默认不安装 SIGTERM 处理器，于是优雅停止信号被忽略。
这是 adapter 的真实缺口，不是引擎问题。

**运行 B（adapter 设置 `Init=true`，由 init 进程转发信号）**

```json
{
  "image_id": "sha256:7e0522950230cad1ec100ada6e3898c22c6bc82851289b4bdf0bf77c1c829cd5",
  "prepare_state": "ready",
  "ready_seconds": 0.0,
  "observed_peak_bytes": 23117824,
  "stop_state": "succeeded",
  "stop_seconds": 2.108,
  "stop_exit_code": 143,
  "stop_verified_at": "2026-10-23T18:17:14.426989+00:00",
  "container_removed": true,
  "leftovers": []
}
```

停止在 2.1 s 内完成，退出码 143（SIGTERM 正常终止）。adapter 只有在容器退出**且**删除后再次
inspect 确认不存在时才写出 `process_or_container_exited` 与 `resources_released_confirmed`
双确认证据；`observed_peak_bytes` 来自 daemon 的内存统计，用于报告加载峰值而不是扣减预算。

因此 adapter 必须把 143 视作正常停止，而不是失败：控制器报告的是容器退出与资源释放事实，
执行成败仍由网关的执行状态决定。

## 3. 清理与边界

两次运行结束后 `docker ps -a --filter label=nexus.managed-by` 均为空，探针未留下容器或端口。
探针未接触既有 GPU 服务：`qwenpaw-embedding`、`qwenpaw-reranker`、`cube-ccba-qwen36-35b-nvfp4`
与已 `disable --now` 的 `qwen3-asr.service` 状态未改变。

**未验证**：真实引擎容器（Qwen3-ASR / TTS）的 ready 与停止、GPU `DeviceRequests` 路径、
容器内存上界与统一内存语义（adapter 目前不设 `Memory` 上界，预留只作报告值）、多实例并发与
跨 generation 竞争、控制器重启后的重新发现与 reconcile、Docker 之外的容器运行时、HTTP 外壳、
持久意图存储与部署单元。以上都不能由本记录推断为已完成。

[审查记录索引](README.md) · [受限节点控制器](../architecture/node-controller.md) · [B02 #29](https://github.com/hrygo/SpeechNexus/issues/29)
