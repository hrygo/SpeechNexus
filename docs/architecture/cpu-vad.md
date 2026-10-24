---
title: "CPU VAD：有界切段与会话状态"
status: active
version: "0.1.0"
date: 2026-10-24
---

# CPU VAD：有界切段与会话状态

这是 [B06 #33](https://github.com/hrygo/SpeechNexus/issues/33) 的原生消费接口。
领域切段、CPU ONNX adapter 和合成探针已实现；另有[原生文件识别消费者](file-asr-recognition.md)。
公开实时入口、24→16 kHz 流式重采样、持久执行链路、Operation/receipt 与完整配方准入尚未装配。
合成结果不能替代真实录音质量或集群资格。

## 代码与消费接口

| 职责 | 实现 |
| --- | --- |
| 固定策略、阈值/滞回/去抖、前缀、有界切段及结果 | `speechnexus_gateway.domain.vad` |
| 单会话顺序输入、尾帧、提交、clear、失败隔离 | `speechnexus_gateway.application.vad.CpuVadSession` |
| 固定摘要、CPU provider、独立 recurrent state/context | `speechnexus_gateway.infrastructure.silero_onnx` |
| 概率序列 fake | `tests.fake.vad` |
| 固定模型与合成音频探针 | `scripts/dev-env/vad-probe.py` |

调用方先核对已准入的实例/配方及 owner/stream epoch，然后为一个流创建一个 `CpuVadSession`：

```python
session = CpuVadSession(model=model, policy=resolved_policy)
segments = session.push(pcm16, sample_start=session.accepted_samples)
```

输入为已重采样的 **16 kHz、单声道、PCM16 little-endian**；每次最多 250 ms，字节数为正偶数。
`sample_start` 必须等于本会话的已接受样本数，拒绝间隙、重放和乱序。入口去重、旧 epoch 拒绝、
全局会话数量、credit 和串行化由实时 owner 负责；本类不建立第二份协调或预算权威。
输出按序返回，不在 VAD 内排队、访问数据库、发 HTTP 或自行调用 ASR。

每个 `VadSegment` 包含单调 sequence、16 kHz 半开样本区间、关闭原因、`config_revision`、
策略摘要及私有 PCM。**仅 `has_speech=true` 的段可以进入 ASR**。无语音段不保留 PCM；
调用方应按既有协议产生空正文 / `no_speech`，不能把原静音绕过门禁再次发给 ASR。
创建 Operation、计量实际接受样本以及发布最终事件仍属于后续集成工作。

`commit()` 处理尾帧并关闭剩余段；没有未提交音频时报 `EmptyVadBufferError`，客户端 event_id
去重必须在调用前完成。`clear()` 丢弃尚未提交的音频和模型状态，保留输入时钟与段序号。
`close()` 丢弃私有缓冲并终止会话；需要 drain 时先 commit。它们均不证明外部 ASR 已停止，
不能据此关闭执行、释放 lease 或归还 A 的预算。

## 固定策略与边界

配方版本为 `cpu-vad-1`。`VadPolicy` 的参数、配置 revision 与配方版本共同进入规范摘要。
策略在会话内冻结，调用方从有效配置显式构造；当前接口不实现跨段热更新确认。

| 参数 | 缺省值 | 本地约束 / 语义 |
| --- | --- | --- |
| 开始阈值 / 结束阈值 | 0.5 / 0.35 | 均为有限 [0,1]，结束阈值不高于开始阈值 |
| 最短连续人声 | 96 ms | 32–500 ms；按 512 样本帧向上取整，用于抑制短促触发 |
| 前缀缓冲 | 300 ms | 0–3000 ms 的最大回看；最多保留 `max_samples - min_speech_samples`，不挤出人声触发空间 |
| 结束静音 | 500 ms | 200–3000 ms；在帧末确认，至多多一个 32 ms 帧 |
| 最大段长 | 20000 ms | 1000–30000 ms；是包含前缀与尾静音的硬上限，必要时在帧内精确切开 |

超过最大长度关闭原段，原因 `budget_rollover`；持续人声无重复前缀、无丢失地续段。
静音也按有界窗口返回无语音结果，不随文件长度积压 PCM。待确认的人声 onset 跨静音窗口边界
保留去抖历史，不把已经开始的候选帧先宣布为静音。

外发模型固定每帧 512 样本，64 样本 context、`[2,1,128]` recurrent state 均属于单个 detector。
模型和只读 ONNX session 可由同一进程复用，每次 `new_detector()` 新建状态；不共享 vendor
wrapper 的可变全局状态。每次 ONNX 调用显式传入状态，CPU intra/inter-op 线程数均为 1。
模型错误、非有限概率、异常输出或 reset 失败会终止会话；原状态不得普通重试或冒充连续恢复，
上层需明确断开/新 epoch 与 gap，不恢复未知的音频内容。

每会话跨调用保留的私有 PCM 上限为 `(max_samples + effective_prefix_samples) * 2 + 2048` bytes；
另有固定大小模型状态。输入块最多 8000 bytes；调用结果由调用方及时消费并受其 credit/队列上限
约束，本接口不把已返回的段永久留存。这个缓冲上限不替代包含临时数组/返回值的进程内存峰值
测量或容量预算。`repr` 不含 PCM。

16 kHz 是模型时钟，公共 wire 仍为 24 kHz。`wire_sample_span` 仅提供零点对齐时钟的覆盖投影：
起点向下取整 `floor(start*3/2)`，终点向上取整 `ceil(end*3/2)`，不累计逐块舍入误差。
尾帧只在推理输入补零，返回 PCM 和模型样本时钟不包含补零。
生产媒体 adapter 仍须记录重采样延迟、实际接受的 wire 尾部和原文件时钟映射；不能把这个比例
投影当作任意解码/重采样链路的精确映射或用量权威。

## 固定模型与运行环境

官方 Silero 制品来源 commit 为 `bfdc0193023f121ea5b3cc7b176dbed570a68a59`，模型 SHA-256：
`1a153a22f4509e292a94e67d6f9b85e8deb25b4988682b7e174c65279d8788e3`。
以本地固定文件和期望摘要创建 `SileroOnnxCpuModel`；构造时先校验摘要，再从内存装载模型，
只允许 `CPUExecutionProvider`，不下载、不访问 GPU、不尝试其他 provider。

vendor 运行依赖由 [独立环境说明](../../deploy/engines/cpu-vad/README.md)固定，
不改变应用 workspace 的锁文件，也不代表默认 Compose 已装配此能力。
I/O 参考官方固定版本的 `src/silero_vad/utils_vad.py`；新代码独立实现会话状态与本项目切段，
没有移植 SpeechRail/macOS 的代码。上游 MIT 文本见同目录 `SILERO_LICENSE`。

验证入口：

```bash
uv run pytest tests/unit/gateway/test_vad.py tests/unit/gateway/test_silero_onnx.py tests/contract/test_vad_contract.py
```

真实 CPU 合成结果、命令与未验证项见 [B06 探针记录](../audits/2026-10-24-b06-cpu-vad-probe.md)。
交接 #31/#39 的是原生 API、固定语义、fake、正负/边界测试与证据；浏览器只消费既有公开协议，
不直接加载本 adapter，也不获得模型路径或私有 PCM。
