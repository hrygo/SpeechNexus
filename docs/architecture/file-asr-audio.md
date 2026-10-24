---
title: "文件 ASR 有界媒体输入"
status: active
version: "0.1.0"
date: 2026-10-24
---

# 文件 ASR 有界媒体输入

[B03 #30](https://github.com/hrygo/SpeechNexus/issues/30) 已提供原生解码和
[CPU VAD](cpu-vad.md) 组合接口；尚未接入公开上传检查、调度器、ASR 外发或结果发布。
本单元不提供 TTS 编码、流式网络入口、识别交叠合并或外部引擎停止事实。

## 消费接口与职责

| 层 | 接口 | 职责 |
| --- | --- | --- |
| domain | `media.AudioDecoder` / `AudioDecodeStream` | 有界块、源样本时钟、完整解码摘要 |
| infrastructure | `pyav_audio.PyAvAudioDecoder` | PyAV 解复用、解码、混为 mono、重采样与 flush |
| application | `file_asr_audio.FileAsrAudio` | 将 16 kHz PCM 顺序交给 VAD，映射源区间，处理尾部和清理 |
| 消费者 | 后续 gateway / A02 / A04 接入 | 受控对象下载与摘要校验、准入、外发、停止、发布和公开错误转换 |

调用方从已核验、部署内的私有 staging 打开可 seek 的二进制流：

```python
prepared = FileAsrAudio(
    decoder=decoder,
    model=pinned_cpu_vad,
    source=private_source,
    source_format=resolved_source_format,
    limits=resolved_limits,
    vad_policy=frozen_policy,
    cancelled=is_cancelled,
)
try:
    for segment in prepared:
        if segment.vad.has_speech:
            consume_candidate(segment)
    complete = prepared.summary
finally:
    prepared.close()
```

`consume_candidate` 是调用方职责示意，不是当前已有的 ASR dispatch。
适配器不接受 URL、对象路径字符串或客户端路径；它也不自行下载对象或证明输入摘要。
完整命令仍由现有执行屏障验证，不能绕过原执行身份、租约、配置版本或预算。

`PreparedAsrSegment` 包含 VAD 结果、源时钟及源样本半开区间。
只有 `vad.has_speech=true` 才带可供后续 ASR 消费的私有 PCM；无语音结果没有 PCM。
完整迭代、成功处理 VAD 尾帧并清理私有状态后才产生 `summary`。
取消、错误、提前关闭或清理失败均无完整摘要；已发出的局部段不能因此冒充最终制品。
调用方拥有输入文件的生命周期，`close()` 只释放解码器和 VAD 私有状态。

## 支持范围和资源限制

原生配方版本 `pcm-decode-1`。支持 WAV、FLAC、MP3、OGG 和显式 PCM16LE；
raw 输入必须声明采样率和声道数，并包含完整的交错样本。
容器输入可不提供声明；提供时必须与真实解码一致。
当前本地子集为 **8–192 kHz、1–8 声道**，输出为 **16 或 24 kHz、mono、PCM16LE**。
文件 ASR/VAD 组合固定使用 16 kHz。

内部执行命令的 `AudioInput` 允许更宽的采样率和声道范围；合法命令不意味着当前 adapter
具备该格式资格。超出本地子集明确拒绝，接入方应在能力选择/预检时暴露原因。
本单元不缩窄或放宽共享机器契约，也不改生成 DTO。
`tests/contract/test_media_contract.py` 校验原生支持声明与既有五种媒体类型的兼容关系。

| 限制 | 缺省 / 固定值 | 生效位置 |
| --- | --- | --- |
| 编码输入 | 256 MiB，可配置至多 1 GiB | 解码前读取文件大小 |
| 解码时长 | 1 小时，可配置至多 24 小时 | 每个源帧按整数样本检查 |
| 单个源帧 | 65,536 samples，可配置至多 1,048,576 | vendor 返回帧后检查 |
| 墙钟期限 | 30 s，可配置至多 3600 s | 包括消费者停顿；I/O、解码、重采样和输出检查点 |
| 单次输入读取 | 最多 65,536 bytes | 包装二进制流 |
| 累计读取 / I/O 次数 | 文件大小 × 4 + 262,144 bytes / 1,000,000 次 | 限制反复 seek/read 放大 |
| 流 / packet | 恰好一条音频、总流最多 8 / packet 最多 8 MiB | 解复用边界 |
| 输出块 | 最多 100 ms；领域类型允许至多 250 ms | 重采样输出 |

解码器逐块消费，不将整文件 PCM 装入内存；codec 和 resampler 均单线程。
VAD 自身缓冲见其文档。为在 EOF 校正最后一个源区间，组合层额外保留至多一个最大 VAD 段；
按缺省 20 秒为 640,000 bytes，策略上限 30 秒为 960,000 bytes。
此外有 vendor 帧、重采样临时数组和当前输出；消费者必须及时消费，不能无限排队。
这些局部上限不等于进程 RSS、全局并发或已获批的实例预算。

**取消和期限是合作式检查。** 单次 native codec 调用内部、分配发生前以及阻塞文件读取
不能由这些 Python 检查硬中断。公开接入之前仍须在受限执行环境设置进程/内存/CPU 限制，
完成恶意媒体、超时强杀及停止/资源释放资格；不能凭本类归还计算预算。
FFmpeg 协议仅允许 `pipe`，附属资源打开回调一律拒绝，包装器只暴露固定名字 `input`。
vendor 日志未关闭时在读取前失败；每个检查点重新安装静默回调，避免 vendor 恢复默认
stderr 回调后仍被误判为静默。运行环境不能并发改动全局日志回调。
普通媒体错误不回显文件名、内容或 vendor 异常。

## 时钟和尾部契约

源时钟保存真实解码的采样率、声道数和 decoder 提供的首 PTS。
没有 PTS 时为 `None`；PTS 是媒体相对时钟，不能当作录音墙钟。
raw 的解复用时钟由声明格式导出，也不证明录音绝对时间。
保留源起点后，将送入 resampler 的 PTS 规范为从零开始的连续源样本。
文件中途改变格式、PTS 有无状态变化或大于一个源样本的时间跳变都会失败。

输出区间 `[a,b)` 映射到源时钟时，起点为 `floor(a * source_rate / output_rate)`，
终点为 `ceil(b * source_rate / output_rate)`，再按已观察源样本数裁剪。
每次从绝对位置计算，不逐块累计舍入；相邻覆盖区间可能因舍入重叠一个源样本，
不能将覆盖长度之和当作用量账本。
最后一个段在完整 flush 后覆盖真实源 EOF，校正必须处于一个输出样本的舍入范围内。
VAD 推理补零不进入返回 PCM 或样本数；未知时间戳不按文本长度或比例猜测。

`SourceAudioClock.coverage()` 是已解码音频的区间投影，不是字级时间戳。
24 kHz wire 投影仍由 VAD 已有接口提供；网络实时 24→16 kHz 连续重采样尚未接入。

原生错误类别为 `invalid_media`、`media_limit_exceeded`、`media_cancelled`、
`media_timeline_discontinuity`；VAD 继续使用既有 `VadError` / `VadInferenceError`。
这是进程内分类，公开 HTTP/事件错误转换由接入单元按冻结契约实现；当前没有新增公开错误码。

## 验证与交接

```bash
uv run pytest tests/unit/gateway/test_audio_decode.py \
  tests/unit/gateway/test_audio_decode_guards.py \
  tests/unit/gateway/test_file_asr_audio.py tests/contract/test_media_contract.py
```

真实 codec 的确定性测试只使用合成数据；VAD 使用可编排概率 fake。
独立 vendor 依赖、真实 CPU 模型和授权语料探针见
[运行说明](../../deploy/engines/file-asr-audio/README.md)和
[本轮记录](../audits/2026-10-24-b03-bounded-audio-probe.md)。
本接口可供 A02 音频 inspector、A04 文件执行及 B04/B06 媒体准备复用；
各消费者的实际装配、生产对象引用与结果发布仍分别验收，不将本单元计为 B03/G1 完成。
