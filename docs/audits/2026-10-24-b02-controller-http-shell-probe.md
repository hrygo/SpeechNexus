---
title: "B02 控制器 HTTP 外壳与持久意图探针"
status: in_progress
version: "0.1.0"
date: 2026-10-24
---

# B02 控制器 HTTP 外壳与持久意图探针

本记录给出受限节点控制器的首个**真实进程级证据**：启动生产入口
`speechnexus-node-controller`，用一次性组件凭据、真实容器 daemon 与真实持久意图存储，走
`internal/runtime-control` HTTP 完成 prepare → ready → stop，在已认领实例仍运行时杀死该进程
并核验重启后的常规快照不会误隔离或改写记录，再优雅重启核验已记录意图仍在。它不证明真实
引擎容器（Qwen3-ASR / TTS）、GPU 请求路径、warmup 门、多实例竞争或可部署单元已经可用。

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

## 5. 进程被杀死后的常规快照与未认领 generation

控制器在已认领实例仍运行时被 `SIGKILL`（没有优雅退出机会，容器保持运行），随后用同一临时
SQLite 意图存储与批准模板文件重启，再折叠一次常规快照。两次运行结果一致：

| 观测 | 运行 1 | 运行 2 |
| --- | --- | --- |
| 杀死控制器时容器仍在运行 | true | true |
| 常规快照（`ready`，generation 1）HTTP | 200 | 200 |
| 该快照报告的隔离实例 | `[]` | `[]` |
| 意图记录（状态/观察/错误码/更新时间）未变 | true | true |
| 未认领 generation 快照（`ready`，generation 2）HTTP | 200 | 200 |
| 该快照报告的隔离实例 | `["instance-probe-inert"]` | 同左 |
| 重启后的 stop | 200 `succeeded`，退出码 143，容器已删除 | 同左 |
| 重启进程后意图仍在 / 遗留容器 | true / `[]` | true / `[]` |

运行 2 的相关字段：

```json
{
  "crash_container_live": true,
  "crash_reconcile_status": 200,
  "crash_reconcile_quarantined": [],
  "crash_reconcile_record_unchanged": true,
  "unclaimed_generation_reconcile_status": 200,
  "unclaimed_generation_quarantined": ["instance-probe-inert"],
  "stop_status": 200,
  "stop_state": "succeeded",
  "stop_exit_code": 143,
  "container_removed": true,
  "restart_preserved": true,
  "leftovers": []
}
```

规则对照（修复前后）见第 4 节与本轮代码差异。修复前的 `reconcile` 对“没有未解决意图”的观察
一律在非 `absent` 时写入隔离名单，于是控制器重启后把**自己已经就绪并仍在运行**的实例上报为
隔离；同时它**不**报告“已认领 generation 在没有停止证据的情况下消失”。反证：把
`application/runtime_control.py` 回退到修复前，本轮新增用例 2 项失败
（`test_reconcile_keeps_a_claimed_ready_instance_out_of_quarantine` 报
`('instance-asr-1',) == ()`，`test_reconcile_quarantines_a_claimed_generation_that_vanished_without_evidence`），
其余 6 项通过；修复后 8 项全部通过，控制器 153 项通过。

未认领 generation 的观察只进入隔离名单，且不落任何新意图：回读仍只有 generation 1 的记录。
因此外部快照不能把本控制器没有认领过的容器当成自己的实例接管。

### 本次未收窄的边界（现状对照，不是已验证能力）

- 快照的 `fencing_token`：契约在该操作 409 描述里写了 “Reconciliation fencing token … does not
  match the controller scope”，当前实现只在 schema 层要求 ≥1，没有与节点已记录的最高 token
  比较。要么补节点级 fencing 高位与 409 拒绝，要么修正契约描述，二者都需要 A 参与评审。
- 快照里的 `stop_evidence`：`reconcile` 路径不调用 `daemon.inspect`，调用方断言的证据会按
  `RuntimeStopEvidence` 折叠进未解决的 stop 意图并转 `succeeded`；控制器自身的核验只发生在
  `reconcile_instance`（节点内观察）里。谁有权断言停止证据、是否收窄该入口，属仍需冻结的窄
  协议，本轮不改语义。
- 控制器进程被杀死后的“接管”与节点内观察生产者仍未交付；本单元只证明同一控制器进程重启后
  折叠常规快照与未认领 generation 快照的行为，不证明多控制器竞争或跨节点接管。

## 6. 未验证与边界

- 真实引擎容器（Qwen3-ASR / TTS）的加载、就绪、停止与资格，以及 GPU `DeviceRequests` 路径，
  仍只有合成容器证据；warmup 门没有实现，模板文件也不能声明 `requires_warmup_before_ready`。
- 合成容器上已验证进程被杀死后重启的常规快照折叠（第 5 节）；未验证多实例并发生命周期、
  跨节点同 `intent_id` 复用，以及被杀后的接管与节点内观察生产者。
- 没有 `deploy/` 与 Compose 单元：控制器进程、镜像、健康检查、卷与凭据注入仍需与 A 的部署
  集成排队交付；TLS 与组件凭据轮换属于部署要求。
- 本记录只覆盖本机单节点、真实进程 + 真实 daemon + 真实本地存储这一条路径。
