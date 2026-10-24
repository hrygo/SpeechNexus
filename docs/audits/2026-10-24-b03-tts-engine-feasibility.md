---
title: "B03 Qwen3-TTS 真实引擎可行性复核"
status: in_progress
version: "0.1.0"
date: 2026-10-24
---

# B03 Qwen3-TTS 真实引擎可行性复核

本记录更正此前一个错误判断：曾据模型 README 的“目前只支持离线推理”和一次不完整的
镜像检索，得出“本机 `vllm/vllm-omni:latest` 不支持 Qwen3-TTS 在线服务”。重新核对镜像
源码与真实运行后，两者都不成立。

## 镜像事实

- 本机镜像 `vllm/vllm-omni:latest` 内含 `vllm` 0.32.0 与 `vllm-omni` 0.32.0rc1。
- 源码包含 `vllm_omni/model_executor/models/qwen3_tts/`（talker、code2wav、12 Hz tokenizer）、
  `entrypoints/openai/tts_adapters/qwen3_tts.py` 与 `serving_speech.py`，支持
  `Base`、`CustomVoice`、`VoiceDesign` 三种任务。
- 在线服务需要显式 `--omni`。不带该参数时 `vllm-omni serve` 走普通 vLLM 路由，
  `/v1/audio/speech` 返回 404，容易被误判为“模型不支持”。
- 当前 vLLM 0.32 的 V2 model runner 在 Qwen3-TTS 上触发 `get_rope_state` 断言失败；
  设置 `VLLM_USE_V2_MODEL_RUNNER=0` 后退回旧 runner 可正常加载。
- 模型 README 中的“only offline inference”描述与本机这版 vllm-omni 不符，按实际运行结果为准。

## 运行条件与结果

容器使用固定镜像、只读挂载 `Qwen3-TTS-12Hz-1.7B-CustomVoice`、`--internal` bridge 网络、
不发布宿主端口、Memory/MemorySwap 16 GiB。35B 常驻时 GPU 空闲约 10.9 GiB，因此使用
`--gpu-memory-utilization 0.06 --max-model-len 4096`；8192 上下文在该预算下 KV 需要
0.88 GiB 而只有 0.77 GiB，启动会明确报错，这和模型能力无关。

启动命令（等价于探针）：

```bash
docker network create --internal --driver bridge speechnexus-tts-probe
docker run -d --name nexus-tts-probe --gpus all \
  --network speechnexus-tts-probe --memory 16g --memory-swap 16g \
  -e VLLM_USE_V2_MODEL_RUNNER=0 \
  -v /path/to/Qwen3-TTS-12Hz-1.7B-CustomVoice:/models/tts:ro \
  --entrypoint vllm-omni vllm/vllm-omni:latest serve /models/tts --omni \
  --served-model-name qwen3-tts-1p7b-custom --host 0.0.0.0 --port 8000 \
  --gpu-memory-utilization 0.06 --max-model-len 4096 --disable-uvicorn-access-log
```

| 项目 | 结果 |
| --- | --- |
| `/health` 就绪 | 约 200 秒（含首次 torch.compile 与 CUDA Graph 捕获） |
| 请求 1：中文、`voice=Vivian`、`response_format=wav` | 200 `audio/wav`，334,124 字节，6.96 秒音频，24 kHz 单声道 PCM16，生成耗时 4.59 秒 |
| 请求 2：`task_type=CustomVoice` + `instructions` + `response_format=pcm` | 200 `audio/pcm`，184,320 字节（3.84 秒），生成耗时 1.59 秒 |
| 回环识别（Qwen3-ASR 0.6B） | 字符错误率 0.0，与输入文本完全一致，识别耗时 0.25 秒 |

回环识别的意义是确认输出确实是可理解的语音且内容正确，而不只是“返回了非空 WAV”。
两次请求各自只验证了一条固定中文句子，不构成质量评测。

清理：两个临时容器与两个内部网络均已删除，受保护 35B 的 `StartedAt` 始终为
`2026-10-23T03:49:18.877205405Z`。音频样本只留在本机忽略目录，未进入 Git 或报告。

## 仍未验证

- 仅 `CustomVoice`；`Base`（音色克隆）与 `VoiceDesign` 未运行，`Base` 还涉及 3 秒参考音频的
  受控引用与隐私边界。
- 未验证流式输出（`stream=true`）、首包延迟、并发、P95 与长文本上限。
- 未验证音色发布、声音一致性、跨语言质量与真实业务文本。
- 未形成批准模板、引擎包装入口或网关 TTS 契约；当前只有“引擎能真实合成”的证据。

这些发现改变了下一步：TTS 不需要自建离线推理包装，可以直接按 OpenAI 兼容的
`/v1/audio/speech` 设计固定内网适配器，并与现有 warmup 门、输出上限和 source clock
规则对齐。
