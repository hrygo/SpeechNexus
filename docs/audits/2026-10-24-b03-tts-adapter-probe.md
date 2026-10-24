---
title: "B03 TTS 适配器真实探针"
status: in_progress
version: "0.1.0"
date: 2026-10-24
---

# B03 TTS 适配器真实探针

本记录验证[文件 TTS 原生合成接口](../architecture/file-tts.md)在真实 1.7B
CustomVoice 权重上的一次闭合：由已提交的 `VllmTtsSynthesizer` 外发、
`LocalTtsEncoder` 交付，再经已提交的 24 kHz→16 kHz 解码与 0.6B ASR 适配器回环。
它不证明 Base/VoiceDesign、流式、并发、质量或公开路由已可用。

## 环境与制品

- TTS 镜像 `vllm/vllm-omni:latest`，镜像 ID
  `sha256:5a12eac1a2dd3540a6ec29e42ef0dd5286c75ee07f68e497cb5f0df6e1311a52`；
  容器以 `--omni`、`VLLM_USE_V2_MODEL_RUNNER=0`、`--gpu-memory-utilization 0.06`、
  `--max-model-len 4096` 加载只读 `Qwen3-TTS-12Hz-1.7B-CustomVoice`，不发布宿主端口。
- ASR 镜像 `local/qwen3-asr-vllm:0.28.0-arm64`，镜像 ID
  `sha256:61cc94fec25198aeefcd93b96088a8e8db106d61d834a36d3b89ba7bf5b10765`；
  加载只读 `Qwen3-ASR-0.6B`，`--gpu-memory-utilization 0.05`、`--max-num-seqs 1`。
- 两个容器各自 `--internal` bridge 网络、`--memory/--memory-swap` 16 GiB / 10 GiB，
  串行运行：TTS 停止并确认退出后才启动 ASR。
- 输入文本是固定合成句，只存在于本机与容器之间；报告不含文本、模型路径、凭据或音频字节。

## 结果

探针脚本 `scripts/dev-env/tts-adapter-probe.py` 一次运行（2026-10-24 UTC）：

| 项目 | 结果 |
| --- | --- |
| 1.7B CustomVoice 就绪（HTTP `/health`） | 186.079 秒 |
| `wav` 用例：`voice=Vivian`，`response_format=wav` | 200 `audio/wav`，6,320 ms，PCM 303,360 字节；经适配器与 WAV 编码共 4.323 秒，交付 303,404 字节 |
| `pcm_mp3` 用例：`instructions` + `response_format=pcm` | 200 `audio/pcm`，5,920 ms，PCM 284,160 字节；本地 MP3 交付 48,044 字节，共 2.4 秒 |
| 交付 WAV 解码 24 kHz→16 kHz | 源 24,000 Hz / 151,680 样本 → 101,120 样本，6,320 ms |
| 0.6B ASR 就绪 | 90.036 秒 |
| ASR 回环 | 0.247 秒，字符错误率 0.0，规范化后完全一致 |
| TTS 停止 | 6.839 秒，状态 `exited`，退出码 0，`Running=false` |
| ASR 停止 | 0.892 秒，状态 `exited`，退出码 0，`Running=false` |
| 清理 | 容器遗留 0、网络遗留 0；受保护 35B `StartedAt` 仍为 `2026-10-23T03:49:18.877205405Z` |

交付音频摘要：`speech-wav.wav` =
`sha256:033a431cc8f2dd601262d786611e4e1dbf6e16951b38db2ade86b506b778b8dc`，
`speech-pcm_mp3.mp3` =
`sha256:760c1090195db1e741c199eaae4e9bd5dcab40a95515a39651f12dd4859ce362`。
原始报告与音频只在忽略的 `.runtime/probes/b04-tts/`，未进入 Git。

两例都未触发 `X-Audio-Truncated`，响应头采样率与声道和策略声明一致，说明通过的是
“引擎声明并给出单声道 24 kHz PCM16”的路径，而不是对任意返回体的乐观接受。
停止证据来自容器退出与资源释放，不是 HTTP 断开或取消请求。

## 边界和未验证

- 只有 `CustomVoice`，且只有固定合成句、`speed=1.0`、24 kHz 输出；`Base` 克隆与
  `VoiceDesign` 需要各自的参考材料、隐私边界与质量单元。
- 未验证流式 `/v1/audio/speech`（`stream`/`stream_format`）、首包延迟、并发、P95 与长文本。
- 未验证音色发布、跨语言质量、8 kHz 输出与非 1.0 语速。
- 公开 `/v1/audio/speech` 路由、持久 dispatch 装配、调度准入、对象写入/发布与结果广播
  仍属其他单元；本探针不把适配器可调用等同于服务已就绪。
- 引擎在非流式生成校验失败时可能自行换 seed 重试一次；本记录只证明网关侧一次 HTTP 外发。

下一步：把适配器接入引擎 dispatch 与网关执行边界，并补齐 Base/VoiceDesign 与流式/并发单元。
