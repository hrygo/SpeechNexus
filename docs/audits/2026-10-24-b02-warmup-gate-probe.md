---
title: "B02 真实 ASR 预热就绪门探针"
status: in_progress
version: "0.1.0"
date: 2026-10-24
---

# B02 真实 ASR 预热就绪门探针

本探针验证 #85 合入的 `http_warmup_ok` 门在真实容器上确实按声明工作：
HTTP 健康之后实例先进入 `warming_up`，逐次发送基于固定合成语音的最大窗口请求，
达到声明次数后才报告 `ready`。它同时记录首轮与立即重复的单次耗时，用于判断
“健康即就绪”是否会漏掉首次长窗口开销。

## 环境与边界

- Linux ARM64 / NVIDIA GB10，单 GPU；固定镜像 image ID
  `sha256:61cc94fec25198aeefcd93b96088a8e8db106d61d834a36d3b89ba7bf5b10765`，
  模板命令与 B03 实测一致（bf16、eager、单并发、8192 model length）。
- 预热请求体为 20.0 秒、单声道、16 kHz 的固定合成语音（espeak-ng，单一技术句子），
  以与引擎适配器相同的 multipart 字段构造；正文只存在临时目录，运行后删除，
  报告只保留音频与请求体摘要。
- 每次运行使用全新 `--internal` bridge 网络，运行结束删除；容器不发布宿主端口，
  模板显式关闭请求、输出与 access log。
- 探针不写 Scheduler 预算、不做质量评估、不代表 peak→steady 的实际缩减：
  请求的 steady 仍显式等于 peak。

## 复现

```bash
uv run python scripts/dev-env/asr-warmup-probe.py \
  --templates-file .runtime/probes/b03-file-asr/0p6b.json \
  --template-id template-file-asr-0p6b-probe \
  --served-model-name qwen3-asr-0p6b \
  --evidence-directory .runtime/probes/b02-warmup \
  --window-seconds 20 --attempts 2
```

1.7B 使用 `1p7b.json` 与 `template-file-asr-1p7b-probe` / `qwen3-asr-1p7b`，其余参数相同。

## 结果

| 模型 | 状态序列 | 第一次预热 | 立即重复 | ready 总时长 |
| --- | --- | --- | --- | --- |
| 0.6B（运行一） | `loading → warming_up → ready` | 31,736 ms | 436 ms | 126.68 s |
| 0.6B（运行二） | `loading → warming_up → ready` | 29,739 ms | 405 ms | 118.64 s |
| 1.7B（运行三） | `loading → warming_up → ready` | 50,210 ms | 2,190 ms | 165.55 s |

三次运行使用同一份合成窗口（音频摘要
`sha256:bf6e5614ff6718519cd61a30cfb17c0d23147db4929f6a1be87f0de208fbd9b2`），
请求体摘要因随机 multipart boundary 不同而不相等。两次都观察到 `warming_up`
先于 `ready`，说明门确实拦截了“HTTP 健康即就绪”。

装载后 daemon 观察到的内存均约为 4.9 GiB，停止在 3–4 秒完成，并取得
`process_or_container_exited` 与 `resources_released_confirmed` 双证据；每次运行零遗留，
内部网络已删除，受保护 35B 的 `StartedAt` 保持
`2026-10-23T03:49:18.877205405Z`。

## 能证明与不能证明

能证明：真实容器上预热门按模板声明执行、每次观察最多一次请求、成功次数达到后才
`ready`、首次最大窗口请求显著慢于立即重复、停止仍以双证据结束。

不能证明：稳态 P95、并发与混合负载、预热与真实业务的排队关系、A 控制循环下的
准入/预算缩减、TTS 或流式路径、物理多机接管。预热请求体为合成语音，不代表真实
录音质量；`ready` 只是本门通过，不是模型资格结论。1.7B 的第一次预热为 50.2 秒，
说明预热成本随绑定变化，不能用一个模型的数值套用到另一个模型。
