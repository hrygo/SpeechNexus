---
title: "B03 真实文件 ASR 与首次长窗口开销"
status: in_progress
version: "0.1.0"
date: 2026-10-24
---

# B03 真实文件 ASR 与首次长窗口开销

本轮在 `3b9789c5a20fab33ccf9db0f324b788beddcf836`（#80）上交付
[原生文件识别](../architecture/file-asr-recognition.md)，将已实现的解码/VAD 实际接到
Qwen3-ASR 0.6B / 1.7B。没有注册公开 ASR、持久 submit dispatch、结果发布或预算返还服务。
真实调用和本机控制器停止有证据；完整文件业务、质量、取消/abort 与集群资格仍未完成。

## 固定环境与边界

- Linux ARM64 / NVIDIA GB10，单 GPU；现有 35B、embedding/reranker 保持运行。
  35B 的 `StartedAt` 前后均为 `2026-10-23T03:49:18.877205405Z`。
- Python 3.14.9；AV 19.0.2、NumPy 2.4.6、ONNX Runtime 1.31.0、
  HTTPX 0.28.1、httpcore 1.0.9、anyio 4.15.1；来自独立 vendor 锁，不改变共享 `uv.lock`。
- 固定镜像 image ID
  `sha256:61cc94fec25198aeefcd93b96088a8e8db106d61d834a36d3b89ba7bf5b10765`；
  镜像内 vLLM 0.28.0 / Python 3.12。bf16、单并发、8192 model length、
  GPU memory utilization 0.05、eager；容器 Memory/MemorySwap 分别 10/12 GiB。
- 三个 ASR 权重文件的当前 SHA-256 重新匹配
  [B02 固定摘要](2026-10-24-b02-real-asr-controller-probe.md)。
  对这些已停止的 ASR 权重共 6,574,613,216 bytes 建议回收干净页缓存，
  不做全局 drop_caches，不触及 35B 权重或服务。
- Silero ONNX 沿用固定摘要
  `1a153a22f4509e292a94e67d6f9b85e8deb25b4988682b7e174c65279d8788e3`；
  配方 `cpu-vad-1` / `pcm-decode-1` / `file-asr-1`，config revision `file-asr-probe-1`。
- VAD 缺省最大段 20 秒，ASR 最多 1024 completion tokens、temperature=0；
  合成样本语言 en，真实样本 auto。媒体处理期限 300 秒、单次 HTTP 期限 60 秒。
- 模板显式关闭请求、输出与 uvicorn access log；内部 Docker bridge 经 inspect 确认
  `Internal=true`，没有宿主端口。源路径、模型路径、音频与转写正文不进入 Git 或报告。

请求参数和内部 SSE 的依据是该固定镜像中的
`entrypoints/speech_to_text/{base,transcription}/{protocol,serving}.py`。
确认 `max_completion_tokens` 进入 `SamplingParams.max_tokens`；
adapter 同时校验实际 finish reason/usage，而不是只依据参数名或 HTTP 200 判断成功。
这些代码没有复制到仓库，也没有改镜像或 vendor server。

## 第一轮：真实模型与媒体组合

两个模型各执行相同九例，再执行一例故意只允许 1 token 的合成负例。
合成句长 4.760 秒；五种编码来自同一合成源，不是五个独立质量样本。
两段真实录音分别来自 AISHELL-4 和 AliMeeting 近场派生目录，均取 20 秒前缀；
截取前重新核对完整源 SHA-256，与 #80 使用同一语料索引。

| 输入 | 0.6B 处理耗时 | 1.7B 处理耗时 | 每模型实际 ASR 调用 |
| --- | --- | --- | --- |
| 合成人声 WAV | 0.248315 s | 0.394946 s | 1 |
| 同源 PCM16LE | 0.144387 s | 0.319341 s | 1 |
| 同源 MP3 | 0.155257 s | 0.333961 s | 1 |
| 同源 FLAC | 0.156522 s | 0.333295 s | 1 |
| 同源 OGG/Opus | 0.158382 s | 0.334718 s | 1 |
| 4 秒静音 WAV | 0.013299 s | 0.015358 s | 0 |
| 30 秒纯音 WAV | 0.080657 s | 0.083361 s | 0 |
| AISHELL-4 20 秒前缀 | 0.449177 s | 0.870835 s | 4 |
| AliMeeting 派生 20 秒前缀 | 32.508964 s | 31.276152 s | 1 |

处理耗时包含解码、VAD、HTTP、识别和结果校验，不包含模型启动。
所有完整样本均覆盖源 EOF；五种合成编码的归一化 WER 均为 0。
两个无语音样本确实没有调用识别器，避免旧探针直接外发无语音时的幻觉与长延迟。
这不证明 VAD 对任意音乐、噪声或真实录音都没有误判。

两个模型的 1-token 负例均返回 `finish_reason=length` 和实际 `completion_tokens=1`，
被 adapter 拒绝为 `asr_output_truncated`；没有将截断正文返回为成功。
每个成功 SSE 都核对原请求 ID、固定模型、终态 usage 和 `[DONE]`。
这些结果不代替请求级 status/abort、任意输入的生成资格或资源释放证据。

首次 HTTP 健康就绪为 92.657 / 98.775 秒，stop 为 1.980 / 1.996 秒，exit code 均 0。
控制器确认实例退出和删除后的资源释放双证据，未用 HTTP 结束冒充 stop。
两轮均零遗留、无紧急清理；系统/daemon 内存观察不当作加载峰值或稳态资格。

## 第二轮：同进程内立即重复相同片段

第一轮 20 秒单段的 31–33 秒耗时不能直接解释为稳态性能。
因此各模型另建一次批准实例，在同一进程中先后处理完全相同的片段。
重复项记录 `repeat_of`，不增加独立样本或质量覆盖数。

| 输入 | 0.6B 首次 / 立即重复 | 1.7B 首次 / 立即重复 |
| --- | --- | --- |
| AISHELL-4，同样 4 段 | 0.447445 / 0.398701 s | 0.887594 / 0.830596 s |
| AliMeeting 派生，同样 1 段 | 32.386264 / 0.665396 s | 31.129174 / 1.711020 s |

各模型、各片段的首次/重复文本摘要和 completion token 数完全一致；
两模型在不同轮次的真实文本摘要也保持一致。只比较摘要，不输出正文。
第二轮健康就绪为 88.660 / 98.870 秒；1.7B stop 为 2.058 秒；
两模型均有 stop 双证据、零遗留并保持 35B 不变。

**能证明的结论**：当前固定配置下，首次该长窗口有显著额外开销，立即重复明显更快。
不能仅据耗时把原因定为 CUDA Graph、torch.compile、FlashInfer 或其他某个编译器；
本轮没有取得足以定位该机制的 profile。一次重复也不是稳态 P95、并发或长期资格。

**对就绪的要求**：HTTP `/health` 只证明服务活着。ASR 实例进入可服务资格之前，
应在相同镜像、binding/generation、精度和最大支持窗口上完成实际媒体→模型的预热，
并记录首轮/复测及失败；只预热短句不能证明长窗口已就绪。
预热与资格完成前保持加载峰值预算，不能凭健康检查切 steady 或宣称实时 ready。
该条件交接 B02/B07 与 A 的 readiness/准入 provider，当前未自动装配到控制循环。

## 证据与未验证项

原始报告位于忽略目录 `.runtime/probes/b03-file-asr/` 下的
`0p6b-evidence`、`1p7b-evidence`、`0p6b-repeat-evidence`、`1p7b-repeat-evidence`，
各有唯一运行子目录；private selection 清单仅在本机。
四个运行日志为 `/tmp/b03-file-asr-{0p6b,1p7b}-{probe,repeat}.log`。
临时录音/SQLite 随运行退出删除；每次只清理本轮唯一标签的实例，空内部网络在全部退出后删除。

确定性验证覆盖原生参数、实际 codec 与 source clock、VAD 门禁、结果身份/版本、
UTF-8 分片 SSE、截断/缺失/错误终态、重复字段、响应/文本/token 上限、
取消与绝对期限、错误回显保护、不重试和部分关闭；真实 TCP 验证代理及重定向隔离。
测试只用合成数据与 fake，不读取私人语料或加载模型。
独立 13 项依赖使用哈希安装成功；同一隔离环境中的本机 TCP ASR adapter 与 CPU VAD
首帧冒烟成功，仅证明依赖可运行，不作为真实引擎或质量资格。

尚未验证：真实录音人工/参考标注质量、噪声/多人/多语言覆盖、长文件交叠与辅助 revision、
词级时间戳、真实请求 status/abort、进程硬隔离、完整有效参数、持久 dispatch/未知恢复、
A/B/C 对象/发布/预算联合链路、预热自动控制、长期混合负载与目标物理集群。
B03/G1 和团队 B Goal 保持进行中。
