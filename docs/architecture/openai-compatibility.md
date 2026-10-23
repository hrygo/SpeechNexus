---
title: "OpenAI 兼容与 SpeechNexus 增强边界"
status: active
version: "1.0.6"
date: 2026-10-23
---

# OpenAI 兼容与 SpeechNexus 增强边界

本文冻结 P0 的 OpenAI SDK 兼容承诺、增强扩展方式和契约门禁。机器可校验源是 `contracts/speech-openapi.yaml`、`contracts/realtime-events.schema.json` 及其样例；本文解释调用边界，不替代 schema。

## 1. 兼容承诺

兼容承诺只覆盖明确声明的路径、字段、格式、事件和错误 envelope，不推断完整 OpenAI API / SDK 兼容。标准客户端将 `base_url` 指向 `<deployment>/v1`，标准请求不需要 `nexus`、workspace 或工作台字段。

| 能力 | 兼容范围 | 明确不支持 |
| --- | --- | --- |
| 模型列表 | `GET /v1/models`、`GET /v1/models/{model}` 的 OpenAI `list` / `model` 形状；`nexus` 附加绑定与就绪事实 | 不暴露模型路径、引擎端点或托管模型目录 |
| 文件转写 | `POST /v1/audio/transcriptions`：`file`、`model`、`language`、`prompt`、`response_format`、`timestamp_granularities[]` | `temperature`、`include`、`chunking_strategy`、`stream`、diarized JSON 等未声明参数显式拒绝 |
| 文本合成 | `POST /v1/audio/speech`：`model`、`input`、`voice`、`instructions`、`response_format`、`speed`、`stream_format=audio` | `stream_format=sse`、OpenAI 托管音色名、任意 voice 对象、未验证格式 |
| 实时转写 | `GET /v1/realtime?model=<logical-model>` 打开 transcription-only 会话；声明的事件子集 | `response.*`、conversation history、tool call、semantic VAD、双工 TTS |
| 错误 envelope | `{"error": {"code", "type", "message", "param", ...}}`，额外携带 `request_id`、`retryable`、`retry_after_ms` | 不以 HTTP 200 包装失败，不返回非 OpenAI 形状的顶层错误 |

`response_format=json` 至少返回 `text`。`verbose_json`、`srt`、`vtt` 只在绑定具备真实时间边界证据时开放；缺少证据返回 `422 unsupported_parameter`，不伪造 `segments`、词级时间戳或 `avg_logprob`。`srt` / `vtt` / `text` 使用 `text/plain`；`json` / `verbose_json` 使用 `application/json`。

REST `verbose_json.usage` 只使用 `{"type":"duration","seconds":...}`，与官方 SDK 的
`TranscriptionVerbose` 响应模型一致；真实 token usage 只出现在非 verbose
`Transcription.usage`。若服务端在 `verbose_json` 返回 token usage，官方 Python SDK
无法解析该响应，因此契约测试将其作为负例拒绝。

非 verbose `Transcription.logprobs` 若由绑定提供真实证据，必须使用官方 SDK 的
`token / bytes / logprob` 形状；P0 不接受 `include` 参数，因此不会为了展示效果虚构
或默认填充该字段。

TTS 的 `response_format` 基线为 `mp3`、`wav`、`pcm`。REST `pcm` 固定为无头、24 kHz、单声道、signed 16-bit little-endian。`speed`、`instructions` 和格式按绑定能力域收窄；契约中的 `instructions` 上界保持官方 4096 字符，绑定只能在该标准域内进一步收窄。请求超出实际域时返回 `422 unsupported_parameter`，不静默 clamp 或忽略。

## 2. 官方 SDK 调用

### 2.1 Python SDK

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://127.0.0.1:8000/v1",
    api_key="deployment-shared-key",  # 未启用共享密钥时可用任意非空占位值
)

models = client.models.list()

with open("sample.wav", "rb") as audio:
    transcript = client.audio.transcriptions.create(
        model="asr-quality",
        file=audio,
        response_format="json",
    )

speech = client.audio.speech.create(
    model="tts-interactive",
    voice="narrator",
    input="欢迎使用 SpeechNexus。",
    response_format="wav",
)
speech.write_to_file("output.wav")
```

`api_key` 是 OpenAI SDK 的必填构造参数，不表示首期存在用户账号。部署启用共享服务密钥时，所有持有者共享同一入口权限；未启用时服务端不把该值解释为用户身份。

### 2.2 JavaScript SDK

仓库契约测试锁定 OpenAI JavaScript SDK `7.43.0`，验证 Node 客户端实际发送的
`models.list()` / `models.retrieve()`、multipart 转写字段、TTS JSON body、二进制响应和
429 错误映射；标准调用形状与 Python 门禁一致。

```ts
import OpenAI from "openai";

const client = new OpenAI({
  baseURL: "http://127.0.0.1:8000/v1",
  apiKey: "deployment-shared-key",
});

const speech = await client.audio.speech.create({
  model: "tts-interactive",
  voice: "narrator",
  input: "欢迎使用 SpeechNexus。",
  response_format: "wav",
});
const buffer = Buffer.from(await speech.arrayBuffer());
```

### 2.3 Realtime

标准 Realtime SDK 使用 `model` 查询参数连接时，服务端创建一个仅转写的兼容会话。Python
客户端需要安装 `openai[realtime]`；仓库契约测试锁定并在 OpenAI Python SDK `2.54.0` 上验证
以下请求形状：

```python
with client.realtime.connect(model="asr-quality") as connection:
    connection.session.update(
        session={
            "type": "transcription",
            "audio": {
                "input": {
                    "format": {"type": "audio/pcm", "rate": 24000},
                    "transcription": {"model": "asr-quality", "language": "zh"},
                    "turn_detection": {"type": "server_vad", "silence_duration_ms": 900},
                },
            },
        }
    )
    # 发送 input_audio_buffer.append / commit，消费声明的事件子集。
```

兼容模式只保证声明的标准 ASR 事件；`conversation.item.input_audio_transcription.completed` 包含 OpenAI 形状的 `usage`（由已接受段样本时长计算），不使用虚构 token 数。`session.audio.input` 的层级与官方 SDK 2.x transcription session 形状一致；`include`、semantic VAD 和未声明音频格式显式拒绝。TTS、可修订假设、辅助结果、背压 credit 与整体终态使用 `nexus.*` 事件，必须通过 `POST /v1/nexus/realtime/sessions` 创建会话后再连接 `GET /v1/realtime?session_id=...`。两种模式互斥，同时提供 `session_id` 和 `model` 返回 400。

服务拒绝未声明事件而不是静默忽略。依赖 OpenAI LLM response、tool call、conversation history 或 semantic VAD 的客户端不属于兼容范围。

## 3. 增强扩展规则

扩展方式沿用 SpeechRail 的边界经验：标准协议保持原形状，增强能力使用独立命名空间和显式版本，不通过改变标准字段含义实现“增强”。

1. **路径命名空间**：增强 REST 只能位于 `/v1/nexus/`；工作台业务只能位于 `/v1/workbench/`。标准 `/v1/audio/*`、`/v1/models` 不承担音色创作、Job 或工作台语义。
2. **字段命名空间**：标准 JSON 请求中的增强字段只能放在可选 `nexus` 对象；multipart 转写的 `nexus` 是 JSON 字符串，并由 `TranscriptionNexusOptions` 冻结内部 schema。标准客户端省略该字段时行为完整。
3. **事件命名空间**：标准 ASR 事件保持 OpenAI 名称；TTS、假设修订、辅助元数据、credit、gap 和操作终态使用 `nexus.*`。同一配置只有一套状态，`session.update` 与 `nexus.session.update` 映射到同一规范命令。
4. **严格校验**：未知字段、未知事件、布尔冒充整数、非法单位和不支持的标准参数返回稳定错误，不做静默降级或别名翻译。
5. **版本固定**：请求可携带 profile、voice、pronunciation 与 model binding revision；执行前重新核对能力、策略和容量。默认值只在首次接受意图时解析并固化。
6. **职责分离**：`nexus` 只承载语音底座增强；项目、会议、助手和有声书编排留在 `/v1/workbench/`，不反向写入底座表。
7. **可验证等价**：便捷入口与 `POST /v1/nexus/jobs` 必须映射到同一规范意图和幂等摘要。新增扩展必须同时提供正例、负例、事件序列和所有消费者的等价测试。

### 3.1 SpeechRail 参考

SpeechRail 在固定来源 commit 上采用“标准事件子集 + 独立命名空间扩展”的方式：保留 OpenAI 的 `session.update`、`input_audio_buffer.*` 与转写事件名称，把增量 TTS、可替换假设和辅助控制放在 `speechrail.*` 事件与 `session.speechrail` 配置中；MCP 只做无状态 REST 代理，不持有实时连接。SpeechNexus 采用同一结构，但把命名空间改为 `nexus.*` / `nexus`，并把能力发现、revision pin、持久 Job 和租约语义纳入底座契约。来源与复用边界见[SpeechRail 复用边界](speechrail-reuse.md)。

### 3.2 与内部控制错误模型的边界

公共入口的 `ErrorResponse` 必须保持 OpenAI 可解析的 `error.code/type/message/param` envelope，并只以可选扩展字段增加 `request_id`、`retryable` 与 `retry_after_ms`。speech、Realtime、admin 与 workbench 的公共错误统一引用 `contracts/shared/errors.schema.json`，避免各命名空间独立漂移；该错误域包含官方 503 使用的 `service_unavailable_error`，并由契约测试验证。内部 `execution-control` / `runtime-control` 不是 OpenAI SDK 消费者，错误中还需要 fencing、generation、submit-intent 与清理证据等控制事实，因此不强制压成公共 envelope。两边可以复用稳定公共错误码的语义，但不能为了“形状统一”改变 OpenAI 标准错误的含义，也不能把内部端点、实例或 token 泄漏到公共响应。

## 4. 契约门禁

`make contracts-check` 校验 OpenAPI / JSON Schema 语法与本地引用。`tests/contract/test_p0_public_contracts.py` 进一步验证：

- 标准路径、请求字段和响应形状存在；
- 官方 OpenAI Python SDK 实际生成的路径、multipart 字段和 JSON body 能通过契约 schema；
- 官方 OpenAI JavaScript SDK 实际生成的路径、multipart 字段、JSON body、错误映射和音频响应能被客户端解析；
- `models.list()`、`audio.transcriptions.create()`、`audio.speech.create()` 能解析声明响应；
- 官方 SDK 的 Realtime connection 实际序列化的 `session.update` 与
  `input_audio_buffer.*` 事件能通过事件 schema，标准 `session.created` 可由 SDK 解析；
- 429 错误 envelope 能由 SDK 映射为 `RateLimitError`；
- 未知字段、未声明事件、非法资质组合和克隆确认缺失被拒绝。

REST 测试使用 `httpx.MockTransport`，Realtime 测试使用官方 SDK 的真实连接序列化路径并捕获发送帧；两者共同证明契约与 SDK 的请求 / 响应形状一致，不证明服务端路由、WebSocket 握手或真实模型已通过。服务端实现后，还须以同一组样例运行 HTTP / WebSocket 集成测试，并记录实际 binding、拓扑和验证范围。

PR CI 使用 `CONTRACT_BASE_REF=<target-branch> make contracts-compat` 对比目标分支：删除标准路径 / 操作 / 字段 / 响应状态 / 事件类型或枚举值、增加必填字段、收紧参数域、改变默认值或字段格式都会失败。门禁会解析本地及 `contracts/shared/` 引用后比较语义，因此把等价的内联域抽到共享 schema 不会被误报为破坏性变更，而引用的共享域真的删除枚举值时仍会失败。对 `speech-openapi.yaml`，门禁只沿 `/v1/models`、`/v1/audio/*` 与 `/v1/realtime` 递归追踪实际可达的组件；对 `realtime-events.schema.json`，只比较声明的标准事件定义、公开 union 中的标准事件及其递归引用，再覆盖 `contracts/shared/` 公共域。标准请求、模型和实时会话中的可选 `nexus` 属性属于增强边界，兼容门禁不沿该属性递归比较；其正例、负例与传输收敛由契约测试单独负责。其余标准字段仍递归检查，因此不能用“扩展”名义删除或改写标准字段。内部和管理契约仍按 ADR 审阅，不以内部字段变化冒充 OpenAI 兼容性变更。

`make contracts-sdk-test`（即 `pnpm test:contracts-sdk`）锁定 OpenAI JavaScript SDK 的实际 REST 形状；与 Python
SDK 门禁一起，CI 同时检查两个官方客户端的请求序列化、响应解析和错误映射。两者仍不替代未来服务端 HTTP 集成测试。

## 5. 新增兼容或增强能力的检查清单

1. 对照目标 OpenAI SDK 版本记录实际请求与响应形状，不凭文档记忆猜测。
2. 把新增标准字段列入对应 request schema，说明默认值、单位、范围、响应格式和能力前提。
3. 对不支持的标准字段给出 `unsupported_parameter` 等稳定错误，不移除后继续执行。
4. 增强字段放入 `nexus` 命名空间；增强事件使用 `nexus.*`，不借用 LLM response 事件。
5. 增加正例、负例、SDK parity 和必要的状态 / 事件序列测试。
6. 更新 [语音接口](speech-api.md)、本文、契约索引和实际的 Python / TypeScript 消费者。
7. 若改变公共语义或兼容范围，先记录 ADR 并同步所有消费者；只通过 schema 语法不能冻结语义。

[语音接口](speech-api.md) · [契约索引](../../contracts/README.md) · [SpeechRail 复用边界](speechrail-reuse.md)
