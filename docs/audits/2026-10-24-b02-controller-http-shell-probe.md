---
title: "B02 控制器 HTTP 外壳与持久意图探针"
status: in_progress
version: "0.1.0"
date: 2026-10-24
---

# B02 控制器 HTTP 外壳与持久意图探针

本记录给出受限节点控制器的首个**真实进程级证据**：启动生产入口
`speechnexus-node-controller`，用一次性组件凭据、真实容器 daemon 与真实持久意图存储，走
`internal/runtime-control` HTTP 完成 prepare → ready → stop，并在重启该进程后核验已记录意图
仍在。它不证明真实引擎容器（Qwen3-ASR / TTS）、GPU 请求路径、warmup 门、多实例竞争或可部署
单元已经可用。

## 1. 范围、环境与制品

探针不加载模型、不占用 GPU、不发布宿主端口，容器是合成惰性容器（`python -m http.server`），
结束前删除；组件凭据是一次性合成值，不写入本文；批准模板文件与意图存储都在一次性临时目录，
进程退出后删除。

| 项 | 事实 |
| --- | --- |
| 主机 | Ubuntu 24.04.5 LTS aarch64；Docker Engine 29.6.2；socket `/var/run/docker.sock` |
| 镜像 | `python:3.14.9-slim-bookworm`；本地 image id `sha256:7e0522950230cad1ec100ada6e3898c22c6bc82851289b4bdf0bf77c1c829cd5` |
| 探针模板 | `template-probe-inert-tcp`：readiness `tcp_port_accepting:8080`、命令 `python -m http.server 8080`、池 `auxiliary`、GPU 请求 0 |
| 控制器配置 | `SPEECHNEXUS_CONTROLLER_*`：节点 `node-local-1`、一次性组件凭据、临时批准模板文件、临时 SQLite 意图存储、`127.0.0.1:8021` |
| 复现入口 | `uv run python scripts/dev-env/node-controller-http-probe.py`（可加 `--image`、`--port`、`--ready-timeout-seconds`） |

## 2. 实测结果（两次运行）

两次都启动生产入口进程并通过 HTTP 操作，不是进程内直调：

| 观测 | 运行 1 | 运行 2 |
| --- | --- | --- |
| `/healthz` | 200 | 200 |
| 无凭据读取意图 | 401 | 401 |
| 访问其他 `node_id` | 409 `target_node_mismatch` | 409 `target_node_mismatch` |
| prepare 响应 | 200，`intent_state=succeeded` | 200，`intent_state=succeeded` |
| prepare → ready 耗时 | 1.128 s | 1.161 s |
| 观察峰值（unified） | 23,265,280 B | 22,994,944 B |
| stop 响应 | 200，`intent_state=succeeded` | 200，`intent_state=succeeded` |
| stop 耗时 | 2.097 s | 2.116 s |
| stop 退出码 | 143（SIGTERM，正常终止） | 143 |
| 容器已删除 | true | true |
| 重启进程后意图仍在 | true | true |
| 遗留容器 | `[]` | `[]` |

运行 2 的完整输出（运行 1 只有耗时与峰值差异）：

```json
{
  "container_id": "f10dd65c80f19eafe7eb699193d87cff430febf22b2f86500f964217707ad5ad",
  "controller_command": "speechnexus-node-controller",
  "health_status": 200,
  "unauthenticated_status": 401,
  "foreign_node_status": 409,
  "image_reference": "python:3.14.9-slim-bookworm",
  "image_id": "sha256:7e0522950230cad1ec100ada6e3898c22c6bc82851289b4bdf0bf77c1c829cd5",
  "prepare_status": 200,
  "prepare_state": "succeeded",
  "ready_seconds": 1.161,
  "observed_peak_bytes": 22994944,
  "stop_status": 200,
  "stop_state": "succeeded",
  "stop_seconds": 2.116,
  "stop_exit_code": 143,
  "container_removed": true,
  "restart_preserved": true,
  "intents_after_restart": [
    {
      "intent_id": "intent-probe-prepare",
      "intent_state": "succeeded",
      "observed_instance_state": "ready"
    },
    {
      "intent_id": "intent-probe-stop",
      "intent_state": "succeeded",
      "observed_instance_state": "stopped"
    }
  ],
  "leftovers": []
}
```

## 3. daemon 视角的容器加固事实

停止前直接向 daemon 读取该容器的 `HostConfig` / `Config`（只读），确认模板声明的运行形态
真的落到了容器上，而不是只写在代码里：

| 字段 | 值 |
| --- | --- |
| `Privileged` | false |
| `CapDrop` | `["ALL"]` |
| `SecurityOpt` | `["no-new-privileges"]` |
| `Init` | true |
| `RestartPolicy` | `{"Name": "no", "MaximumRetryCount": 0}` |
| `PortBindings` | `{}`（无宿主端口） |
| `NetworkMode` | bridge |
| `User` | 空（镜像默认用户） |

## 4. 停机双证据与重启后的持久意图

stop 意图只有在容器退出**并且**删除被确认后才写出双确认证据，本次拿到
`stop_evidence.process_or_container_exited = true`、`resources_released_confirmed = true`、
`exit_code = 143`、`verified_at` 非空；同一容器随后 `inspect` 为不存在。停止第一个进程后用同一
SQLite 路径启动第二个进程，`GET` 两个意图仍返回 `succeeded`，说明意图身份、状态与停止证据
都落在本地持久存储里，而不是只活在一次进程生命周期内。

## 5. 未验证与边界

- 真实引擎容器（Qwen3-ASR / TTS）的加载、就绪、停止与资格，以及 GPU `DeviceRequests` 路径，
  仍只有合成容器证据；warmup 门没有实现，模板文件也不能声明 `requires_warmup_before_ready`。
- 未验证多实例并发生命周期、跨节点同 `intent_id` 复用、控制器进程被杀死后的 reconcile 与接管。
- 没有 `deploy/` 与 Compose 单元：控制器进程、镜像、健康检查、卷与凭据注入仍需与 A 的部署
  集成排队交付；TLS 与组件凭据轮换属于部署要求。
- 本记录只覆盖本机单节点、真实进程 + 真实 daemon + 真实本地存储这一条路径。
