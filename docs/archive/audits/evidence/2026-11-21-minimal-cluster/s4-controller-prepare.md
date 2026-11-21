# 最小同构集群 S4（未完成）：控制器驱动的 prepare

- 采集时间：2026-11-21 01:56–02:20（Asia/Shanghai）
- 结论：**控制器驱动的 `prepare → ready → stop` 全链走通，停止证据齐备，S4 完成。**
  前两次尝试停在 `reconciling/loading`，根因是引擎绑定地址与容器网络不匹配，见文末「定位与修复」。

## 做到了什么

控制器只需要 docker 与获准模板，不依赖网关和数据库，因此拆除后的算力面单独恢复即可验证：

```
POST /internal/v1/nodes/node-megacube-1/instance-intents   → 200
{"action":"prepare","intent_state":"reconciling","observed_instance_state":"loading"}
```

控制器按 `template-asr-1.7b` 创建了 `speechnexus-instance-instance-asr-5-g1`，容器内
`curl 127.0.0.1:18190/health` 返回 `200`，`uvicorn` 打出 `Application startup complete`。

## 卡在哪

意图在 7 分钟内一直是 `reconciling` / `loading`，即使引擎已在容器内健康：

```
intent_state reconciling  observed_instance_state loading  error_code None
observed_peak_memory unified_memory_bytes 4371275776
```

即控制器的就绪判据没有认可这个实例。可能的差异点（未定论）：模板的 readiness 规则与
引擎实际监听/路径不一致；或控制器从自己的进程看 `127.0.0.1:18190` 的时点与容器启动顺序错开。

## 过程中修掉的两个真实问题

1. **模板文件权限**：控制器以非 root 用户直接 bind mount `./approved-templates.json`，
   宿主上 0600 会导致启动即 `PermissionError`；必须 0644。
2. **模板显存参数过小**：`--gpu-memory-utilization 0.05` 在 `--max-model-len 8192` 下
   KV cache 只有 0.38 GiB，vLLM 直接拒绝启动；提到 0.25 后引擎正常加载。

## 恢复到基线

实例容器（`instance-asr-4`、`instance-asr-5`）与算力面全部 `docker compose down --volumes`，
MegaCube 上没有留下本集群的容器。生产容器未受影响。

## 与验收的关系

S4 不在本次验收条件里（验收要求的是跨主机 Job、五类故障与 teardown），因此 S4 的缺口不改变
S5–S8 的结论；但「完成 S0–S8」这个目标因此仍未达成，需要继续追 readiness 判据。

## 定位与修复：引擎绑定地址

控制器用容器网络地址做就绪探测，而模板把引擎绑定在 `127.0.0.1`：容器内 `curl 127.0.0.1:18190/health`
返回 200，宿主与控制器侧却连不上（`000`），于是意图永远停在 `loading`。

把模板命令改为 `--host 0.0.0.0` 后，同一模板同一镜像立即就绪。容器在 Docker 默认桥接网络里，
不发布端口，因此没有新增对外监听；`network_name: host` 被控制器拒绝（只接受已批准的
自定义网络），这条弯路也记在这里。

## 全链实测

```
prepare  intent_state=succeeded  observed_instance_state=ready
stop     intent_state=succeeded  observed_instance_state=stopped
stop_evidence {"process_or_container_exited":true,
               "resources_released_confirmed":true,
               "exit_code":0,
               "runtime_container_id":"c4642aeef2fe…",
               "verified_at":"2026-11-20T18:12:03.243475Z"}
```

停止后 `docker ps -a` 中该实例容器为 0，控制器随即清理了容器。收尾再次 `docker compose down --volumes`，
MegaCube 上没有留下本集群容器。
