# 最小同构集群 S3–S4：算力面装配证据

- 采集时间：2026-11-21 00:10–00:45（Asia/Shanghai）
- 范围：S3（网关、控制器在 MegaCube 运行）与 S4（ASR 引擎实例），对应实施卡 #1087
- 前置：S1 应用面与 S2 隧道（`127.0.0.1:15440` / `19100` 已通）

## 镜像

`speechnexus-python:203d412b` 与 `speechnexus-node-controller:203d412b` 均按仓库 `203d412b`
构建后经 `docker save | ssh docker load` 送到 MegaCube；未使用 `sn534-*` 旧标签，也未在
MegaCube 上重新构建（避免把网络与构建链差异引入证据）。

## 网关

- `/healthz` 200；`/readyz` 200，明细 `control_postgresql`、`control_query_postgresql`、
  `gateway_ingress` 全为 `ok`——**PG 经隧道跨主机读写成立**，这是 S2 正向链路的第一个真实消费者。
- 监听 `127.0.0.1:8010`；容器 `network_mode: host` + `read_only` + `cap_drop: ALL`。
- 私有配置文件要求「属于运行身份、权限 0600」，宿主 bind mount 无法满足，改由一次性
  `config-init` 服务把配置复制进卷并 `chown 10001` 后以只读卷挂载；spool / ingress 目录同样
  需要 `0700` 且属主为 10001，否则网关拒绝启动。

## 节点控制器

- `/healthz` 200；`/readyz` 200，明细 `container_daemon` 与 `runtime_intent_store` 均 `ok`。
- Docker socket 属组 gid `988`、模式 `660`，控制器以非 root 运行，必须 `group_add: ["988"]`；
  否则 `container_daemon` 报 `unavailable`（这是 readiness 的真实反例，不是配置噪声）。
- 模板 `engine_auth` 为 `none` 时不能同时配置 `ENGINE_AUTH_ROOT_KEY`，否则控制器拒绝启动。

## 引擎实例（手工）

`local/qwen3-asr-vllm:0.28.0-arm64`（vLLM 0.28.0），`--memory=12g`、`--gpus all`、`--shm-size=4g`、
`--cap-drop=ALL`、`--no-new-privileges`、`--read-only`、`--tmpfs /tmp:size=8g`、
`-p 127.0.0.1:18190:8000`、只读模型绑定 `Qwen3-ASR-1.7B`、`HOME=/tmp`、
`TRITON_CACHE_DIR` 指向宿主可写目录（`chmod 777`）。

启动参数：`--dtype bfloat16 --gpu-memory-utilization 0.08 --max-num-seqs 2 --max-model-len 8192
--limit-mm-per-prompt {"audio":1} --enforce-eager`。

| 项 | 实测 |
| --- | --- |
| `/health` | 200 |
| `/v1/models` | `qwen3-asr-1.7b`，`max_model_len=8192` |
| 容器内存 | 4.02 GiB / 12 GiB |
| 监听 | 仅 `127.0.0.1:18190` |
| 宿主可用内存 | 80 GiB（启动前 93 GiB） |

### 三次启动失败（都记下来，因为都会复发）

1. `--tmpfs /root:size=64m` 太小 → Triton 缓存 `.so` `failed to map segment`。
2. 只读根文件系统且无 `/root` 可写层 → flashinfer 写 `/root/.cache` 报 `Read-only file system`。
3. `--cap-drop=ALL` 之后 root 失去 `CAP_DAC_OVERRIDE`，宿主目录属主为 uid 1000、模式 0755 →
   Triton 缓存目录 `Permission denied`；`chmod 777` 后通过。

## 未完成（留给 #1087 的剩余项与 #1088）

- 引擎是手工 `docker run` 启动，**没有控制器生命周期**：prepare → READY → stop 三段回执未做，
  预算释放证据未取得。按规划这不能算同构完成，只证明引擎能在 MegaCube 上以受控参数服务。
- 模板已就位（`template-asr-1.7b`），readiness 用 `http_status_ok`；控制器 prepare 路径待下一轮。
- 网关到引擎的实际推理调用由 S5 的 Job 端到端覆盖。

## 运维陷阱：桥接重建可能被上一个会话占住

桥接 Pod 删除重建时，MegaCube 上旧 sshd 会话仍持有 `127.0.0.1:15440`/`:19100`，新连接的
`-R` 会失败，`ExitOnForwardFailure=yes` 让新 Pod 直接 `Error`（日志
`remote port forwarding failed for listen port 15440`）。服务端未开 `ClientAliveInterval`，
半开连接不会自己消失。本轮通过结束残留的 `sshd: huangzhonghui@notty` 会话清除。
长期方案（后续卡）：由部署侧在重建前清理本用户残留会话，或改用可复用的 ControlMaster 连接；
不能靠调大 `ServerAliveInterval` 解决，它只作用于客户端。
