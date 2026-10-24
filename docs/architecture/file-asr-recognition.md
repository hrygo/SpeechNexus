---
title: "文件 ASR 原生识别接口"
status: active
version: "0.1.0"
date: 2026-10-24
---

# 文件 ASR 原生识别接口

本单元组合[文件媒体输入](file-asr-audio.md)、CPU VAD 和固定 vLLM 语音端点。
已实现原生识别端口、HTTP adapter 和顺序文件消费者；公开文件 ASR 路由、持久 dispatch
装配、调度准入、对象核验/发布、交叠合并及辅助时间戳仍另行接入。
不能将原生入口直接注册成绕过租约、提交屏障或完整有效命令的业务路由。

## 端口和结果

| 层 | 类型 | 职责 |
| --- | --- | --- |
| domain | `AsrPolicy`、`AsrAudio`、`AsrText`、`AsrRecognizer` | 冻结参数、最多 30 秒 PCM、关联身份和识别结果；不含端点或 vendor DTO |
| infrastructure | `VllmAsrEndpoint`、`VllmAsrRecognizer` | 固定内网 HTTP 端点、WAV/multipart、私有 SSE 解析和调用生命周期 |
| application | `FileAsrTranscription`、`FileAsrSegment` | 只外发 VAD 人声段，携带源/模型样本区间及配置、配方、模型绑定版本 |

文件消费者是单次使用的异步迭代器，接收已创建的 `FileAsrAudio`，并接管其清理：

```python
transcription = FileAsrTranscription(
    audio=prepared_audio,
    recognizer=approved_recognizer,
    execution_id=admitted_execution_id,
    policy=frozen_policy,
    cancelled=is_cancelled,
)
try:
    async for segment in transcription:
        consume_unpublished_segment(segment)
    complete = transcription.summary
finally:
    await transcription.aclose()
```

`consume_unpublished_segment` 是调用方职责示意；本单元没有结果存储或发布服务。
迭代前必须已通过原执行身份、command digest、binding/generation/资格、租约和入口屏障校验，
持久写入唯一 submit intent；接入者不能因为 HTTP adapter 可调用而跳过这些步骤。
`EngineDispatchPort` 的生产装配和 A 的调度消费者仍未完成，本类本身没有幂等账本。

无语音窗口返回 `no_speech=true`、空文本、空请求 ID，完全不调用 ASR。
人声结果返回文本、源/模型半开样本区间、请求/引擎请求 ID、实际 completion token 数、
config revision、ASR/VAD 策略摘要及模型 binding revision。
输出不再携带 PCM，文本不进入 `repr`；不积压整文件的文本或音频。
最终摘要只在整个源 EOF、全部识别调用与清理完成后可用，提前退出或失败没有成功摘要。
已输出的局部段仍是未发布结果，不意味着整个文件成功。

请求 ID 根据执行 ID、段序号、样本区间、PCM 摘要、ASR 策略摘要和绑定版本确定。
这是关联身份，不是引擎幂等保证；不得重建迭代器重发同一未知执行。
adapter 不自动重试；实例状态查询、原身份恢复与停止事实继续由现有网关组件负责。

## 参数、能力和时间戳

原生配方 `file-asr-1`。每个段固定 16 kHz mono PCM16LE，最多 30 秒，拒绝空或非完整样本。
`AsrPolicy` 在文件内冻结并纳入规范摘要：

| 参数 | 缺省 | 约束 |
| --- | --- | --- |
| config revision | 调用方显式提供 | 必须与 VAD revision 相同 |
| language | `auto` | 沿用契约语言形式；`auto` 在 vendor 请求中省略 |
| prompt | 空 | 最多 10,000 字符，合法 UTF-8，不含 NUL |
| temperature | 0 | 有限 [0,1] |
| max completion tokens | 1024 | 整数 1–4096，属于批准引擎配方，不新增公开参数 |
| 单段调用期限 | 60 秒 | 大于 0、最多 300 秒，覆盖连接、响应、部分事件与慢速输出 |

VAD/ASR 的 config revision 一致，ASR 绑定在文件内保持固定。
合法语言声明不证明该模型的语言资格；语言、prompt 和模型能力仍须由绑定/有效参数解析
明确匹配。词表、发音资源、alignment、diarization、required stages、特殊输出格式与
生产 deadline 没有在这里暗中降级；接入者须在完整命令校验/配方准入时处理或明确拒绝。
本单元不宣称已覆盖完整 `AsrEffectiveParameters`。

`FileAsrSegment` 的时间位置来自真实解码样本和 VAD 音频区间，不按文本长度分摊。
它是粗粒度音频覆盖，不是 vendor 词级对齐或词边界。
不生成虚构的字/词时间戳；要求这些信息时仍需 qualified alignment stage。
当前逐 VAD 段识别，没有添加交叠上下文或按相同文字猜测去重；连续重复语句不能被误删。

## 私有 HTTP 和完成证据

端点只接受服务端配置的私有 IP literal、端口、固定安全模型别名和 binding revision；
不接受 URL、DNS 名、任意路径或重定向，不使用环境代理。
固定路径 `/v1/audio/transcriptions`，只发送有界 WAV，文件名固定 `audio.wav`。
当前 adapter 使用部署内 HTTP；跨网络 TLS、访问控制和出站隔离仍须由批准拓扑提供，
不能把私有地址校验当作网络隔离或 generation/fencing 证明。
Bearer 凭据可由服务端注入，隐藏于 `repr`，不进入公共 DTO。

固定 vLLM 0.28.0 镜像源码的 speech-to-text serving 将 `max_completion_tokens`
传给 `SamplingParams.max_tokens`。普通 JSON 不带 finish reason，
因此 adapter 请求内部 SSE，并显式要求最终 usage：

1. HTTP 200、`text/event-stream`、无压缩，header 请求 ID 必须原值回显。
2. 每个事件的 ID 必须为原请求的 `transcribe-…`，模型别名与配置一致。
3. 只接受一个输出 choice；必须收到结束原因、最终 token usage 与 `[DONE]`。
4. `finish_reason=stop` 才能成功；`length` 报 `asr_output_truncated`，不发布截断文本。
5. 实际 completion tokens 不得超过请求上限；拒绝布尔计数、无效和重复 JSON 字段、
   非有限值、缺失/重复终态、错误身份和异常事件。

客户端限制每事件/单行 65,536 bytes、文本 65,536 bytes、总响应 1 MiB、最多 8192 事件。
这些是应用消费边界，不代替进程内存上限或服务端生成资格。
当前协议要求单个 engine clip；多 clip 的多个终态被拒绝，不能靠文本拼接掩盖边界差异。
模型的实际 clip 配置、单请求生成上界和目标负载仍随固定镜像/绑定取证。

单次 HTTP 调用使用绝对异步期限，取消检查间隔 25 ms；部分 SSE/慢速响应也会被取消并关闭连接。
媒体/ONNX 处理仍采用合作式检查，不由该异步期限硬中断；进程隔离、整体执行 deadline 与
实际停止仍需运行环境和持久执行消费者提供。
外层 task 取消在清理后继续传播 `CancelledError`。
`AsrCallError.request_may_have_run` 只表示是否可能已发起调用：
发送前取消为 false；发送后取消、超时、断线、错误响应都保守保留为 true。
它不能证明失败请求已停止、没有执行，或旧调用资源已归还。

**HTTP 结束、`[DONE]`、连接关闭和客户端取消都不生成资源释放事实。**
真实 abort/status、停止验证、lease 清理、A 预算返还和最终发布继续按原身份和现有契约实现。
没有这些事实的执行保持 unknown/quarantine；原生调用的成功也不是模型或集群资格。

## 验证和运行

```bash
uv run pytest tests/unit/gateway/test_vllm_asr.py \
  tests/unit/gateway/test_asr_http_transport.py tests/unit/gateway/test_file_asr.py \
  tests/contract/test_asr_policy_contract.py
```

确定性测试不加载模型，使用合成 PCM、真实 codec、脚本化 VAD/识别端口、HTTPX transport
以及本机 TCP。真实模型验证扩展现有 controller 探针；只有明确关闭请求、输出和 access log
的批准模板才允许进入真实语料模式。临时内部网络、容器和模型资源由 B 串行管理，
不停止受保护 LLM，不写 A 的预算表。

[真实探针与首次长窗口测量](../audits/2026-10-24-b03-file-asr-probe.md)显示，
健康检查后的首次长窗口可能仍有显著开销。进入可服务资格前须在相同绑定/generation、
精度和最大支持窗口完成实际媒体→模型预热及复测；短句、HTTP 200 或一次快速复测都不能
代替完整资格。当前没有自动将该预热装配到 A/B 控制循环。
