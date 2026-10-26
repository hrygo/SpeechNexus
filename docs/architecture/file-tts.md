---
title: "文件 TTS 原生合成接口"
status: active
version: "0.1.0"
date: 2026-10-24
---

# 文件 TTS 原生合成接口

本单元把已复核的 vLLM-Omni `/v1/audio/speech` 能力做成网关原生端口：冻结文本与音色策略、
固定私网端点、结构化校验引擎音频、并把规范化 PCM16 有界编码为交付格式。
持久 dispatch 装配见[文件 TTS 默认执行装配](file-tts-runtime.md)。公开
`/v1/audio/speech` 路由、调度准入、对象发布、音色发布以及 Base/VoiceDesign 仍另行接入；
不能把适配器直接注册成绕过租约和提交屏障的业务路由。

## 端口和结果

| 层 | 类型 | 职责 |
| --- | --- | --- |
| domain | `TtsPolicy`、`TtsRequest`、`TtsAudio`、`TtsDelivery`、`TtsSynthesizer`、`TtsDeliveryEncoder` | 冻结策略、有界文本、规范化 PCM16、交付编码事实；不含端点或 vendor DTO |
| infrastructure | `VllmTtsEndpoint`、`VllmTtsSynthesizer` | 固定私网 HTTP 端点、单次 JSON 调用、响应头与 RIFF 结构校验 |
| infrastructure | `LocalTtsEncoder` | 本地 PCM/WAV/MP3 交付编码，显式字节、时长与取消边界 |
| application | `FileTtsSynthesis`、`FileTtsResult` | 单次外发一次合成，再编码为声明的交付格式 |
| domain / infrastructure | `PreparedTtsResult`、`MinioTtsResults` | 独立音频回执，经共享私有 S3 写入与固定版本读回，不发布或修改计量 |

端点是配置的私网 IP 明文（回环或私有地址）、端口、`served_model` 和 binding revision，
与 ASR 适配器共用同一 `PrivateEngineEndpoint` 校验。调用方不能从请求、重定向或环境代理
改写目标；token 不进入 `repr`。

`FileTtsSynthesis` 是单次使用的编排，调用前必须已经通过执行身份、command digest、
binding/generation/资格、租约和入口屏障校验，并持久写入唯一 submit intent：

```python
synthesis = FileTtsSynthesis(
    synthesizer=approved_synthesizer,
    encoder=LocalTtsEncoder(),
    request=TtsRequest(request_id, frozen_text),
    policy=frozen_policy,
    delivery_format=TtsDeliveryFormat.WAV,
    max_payload_bytes=32 * 1024 * 1024,
    cancelled=is_cancelled,
    execution_id=admitted_execution_id,
)
result = await synthesis.run()
```

结果包含执行 ID、请求 ID、策略摘要、binding revision、引擎媒体类型、采样率、声道、
PCM 字节数、时长和交付载荷。文本与音频载荷不进入 `repr`；`pcm_bytes` 是规范化后的
单声道 PCM16 大小，不是引擎响应大小。

### 私有音频 staging

已具备准入与唯一执行资格的调用方可以改用
`await synthesis.stage_result(workspace_id=..., request_digest=..., store=...)`，
连接现有合成、编码与 `MinioTtsResults`。它只支持持久执行身份允许的 24 kHz mono
交集，返回固定对象版本与源 PCM 样本元数据。原生接口的 8 kHz 声明不因此获得资格。
单次对象已尝试后，即使确认丢失，也不能在同一 synthesis 上重新外发或再次 PUT。

调用方须通过原 B 区间端口登记实际生成样本，再调用原观察事务登记回执。
新音频查询、完整区间检查、升级顺序及回退边界见
[执行回执](execution-staging-results.md)和 [ADR 0010](../decisions/0010-private-audio-receipt-compatibility.md)。
原命令消费见下文 `CommandFileTts`；默认部署工厂见[文件 TTS 默认执行装配](file-tts-runtime.md)。
A 的公开发布链仍未装配，组件接线不能替代公开闭环。

staging 本地稳定错误为 `tts_result_invalid`、`tts_result_cancelled`、
`tts_result_deadline_exceeded`、`tts_result_storage_unknown`、
`tts_result_versioning_required`、`tts_result_readback_failed`、
`tts_result_identity_mismatch`。它们不直接扩充公开执行错误枚举；调用方仍按原
unknown/停止协议保留占用，PUT 或读回失败不重试或删除未知对象。

## 参数、边界和失败语义

原生配方 `file-tts-1`。`TtsPolicy` 在单次合成内冻结并纳入规范摘要：

- `voice` 是预设 speaker 标识；`task` 目前只有 `CustomVoice`，与已验证的 checkpoint 对应。
- `instructions` 可选、最多 4096 字符且拒绝 NUL；`speed` 允许 0.25–4.0，默认 1.0。
- `language` 可选 BCP-47 形式标签，`seed` 可选 64 位整数；两者进入策略摘要。HTTP 边界把
  已声明的主语言子标签转换为 vLLM-Omni 的固定名称（如 `zh`/`zh-CN`→`Chinese`、
  `en`/`en-US`→`English`），未映射标签保持原值交由引擎拒绝，不伪造成功。
- `engine_format` 只允许 `wav`/`pcm`，即本单元能结构校验的两种引擎响应。
- `sample_rate_hz` 允许 8000/24000；策略声明值必须与引擎响应头和 WAV 头一致，不一致即失败。
- `max_pcm_bytes`（1 KiB–64 MiB）、`max_audio_ms`（≤ 1 小时）和 `timeout_seconds`
  （≤ 600 秒）同时约束响应体、解码时长与整个调用。

文本最多 4096 字符且不得超过 16384 UTF-8 字节；更长文本必须由上游切分单元处理，
本适配器拒绝而不是让引擎侧超限。`TtsRequest` 与 `TtsPolicy` 的校验错误是调用方配置错误，
不产生外发。

失败码稳定：`tts_cancelled`、`tts_deadline_exceeded`、`tts_transport_unknown`、
`tts_response_invalid`、`tts_output_truncated`、`tts_output_limit_exceeded`。
`request_may_have_run` 只说明是否可能有字节已经离开网关：取消发生在发送前为 false，
发送后为 true；它从不表示网关已确认引擎停止。未知或失败时调用方继续占用执行预算，
直到拿到独立停止与资源释放证据。

引擎在非流式生成校验失败时可能自行换 seed 重试一次；网关只保证自己只发一次 HTTP 请求，
不声明引擎侧只执行一次生成。输出被引擎标记截断（`X-Audio-Truncated: true`）时按
`tts_output_truncated` 失败，不把部分音频当作完整结果交付。

## 原命令消费

`CommandFileTts` 把已持久化的原 `SubmitExecutionRequest` 冻结成一次合成：校验 command digest
与总长度、`kind=tts`/`task_kind=tts_file`/`stage_role=tts_connector`、binding/policy revision、
非空 `voice_revision_id`、匹配的 node/gateway/pool/instance/generation target，以及
`text` 非空且 `text_stream` 为空。原请求还必须带有非空 `output_stream_id`、
`max_output_audio_ms` 和等于 binding 的 24 kHz `sample_rate_hz`；这三个事实用于生成区间
和硬输出上界，缺失或越界不会进入合成。它只接受
`text_commit_mode=explicit`、`audio_delivery=complete`、无 pronunciation revision、
`voice_kind=builtin` 且无 voice material/词表/发音资源，并要求文本长度不超过命令声明的
`planner.max_chars`：本单元只做单段完整交付，长文本切分属于后续单元，不会被静默截断。

接受的参数覆盖到策略字段：`instructions`、`speed`、`language`、`seed`；`response_format`
决定交付格式（wav/mp3/pcm），引擎格式、采样率与各项上限仍来自批准 binding。
`validate_request` 只做无 I/O 的冻结检查；`synthesize` 至多一次引擎调用；
`stage_result` 复用私有 staging 回执。三者都不写终止状态、不发布制品、不签发停止或释放证据；
持久 worker 在登记音频回执前，先通过原区间端口写入完整生成范围 `[0, source_pcm_samples)`；
覆盖缺口或越过冻结上界时隔离并保留占用。执行身份、准入、计量与对外发布仍由 A 的原事务负责。

## 私有 HTTP 和结构校验

adapter 只使用配置的私网地址与端口，`trust_env=False`、不跟随重定向、不自动重试，
并设置 `Accept-Encoding: identity`。请求体是冻结的 JSON 文档：`model`、`input`、`voice`、
`task_type`、`response_format`、`sample_rate`、`speed`；`instructions`、`language`、`seed`
仅在策略显式声明时发送。公开 BCP-47 主语言子标签在 vendor 边界转换为该镜像要求的名称，
区域变体共享主语言；这项转换只改变厂商请求表示，不改变冻结策略摘要或执行身份。

只有以下条件同时成立才接受响应：

- 状态 200，`Content-Type` 与请求格式一致（`audio/wav` 或 `audio/pcm`），编码为 identity。
- `X-Audio-Sample-Rate` 等于策略声明采样率，`X-Audio-Channels` 为 1。
- 响应体不超过 `max_pcm_bytes + 64 KiB` 包络预算。
- `audio/wav` 是合法 RIFF/WAVE：`fmt` 为 PCM16、单声道、声明采样率，存在非空偶数字节的
  `data` chunk，声明的 RIFF 长度覆盖全部 chunk，且 chunk 数有界。
- `audio/pcm` 是非空偶数字节；时长在 `max_audio_ms` 内且至少 1 ms。

adapter 不解析引擎正文语义，只做身份、格式、结构和预算校验。WAV 容器被剥离为规范化
PCM16；交付编码使用 stdlib `wave`（WAV/PCM）或 PyAV `libmp3lame`（MP3，64 kbps 单声道），
按秒分块并在块间检查取消与字节预算。编码是本地工作，不改变引擎执行事实。

## 验证和运行

确定性测试覆盖策略/请求/音频边界、WAV 与 PCM 负例（状态、重定向、类型、编码、采样率头、
声道、截断标志、错误采样率、立体声、8 位、非 PCM 格式、缺 `data`、奇数字节、声明长度
不符、chunk 越界）、总字节与时长上限、发送前取消零外发、发送后取消保留未知执行、
绝对期限、传输失败为 unknown、端点身份不能由调用方改写，以及 PCM/WAV/MP3 编码边界。

真实探针 `scripts/dev-env/tts-adapter-probe.py` 使用本适配器驱动固定镜像与只读 1.7B
CustomVoice 权重，逐次停止容器后启动 0.6B ASR 容器，用已提交的解码器把交付 WAV 解到
16 kHz 再经 ASR 适配器回环。报告只记录摘要、时长、字节数、失败码与清理结果，
不记录模型路径、凭据或音频内容；证据目录在忽略的 `.runtime/` 下。

未验证：Base（音色克隆）与 VoiceDesign 任务、流式与首包延迟、并发/P95、非 1.0 语速、
8000 Hz 输出、长文本切分、音色发布与跨语言质量。这些能力不能由本单元的接口或测试推断为可用。
