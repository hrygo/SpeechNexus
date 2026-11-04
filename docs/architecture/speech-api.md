---
title: "SpeechNexus 语音接口设计"
status: active
version: "0.5.14"
date: 2026-10-23
---

# SpeechNexus 语音接口设计

本文负责实时与非实时 ASR/TTS、音色创作和资源访问的协议语义，属于[项目主设计](../speechnexus-product-architecture.md)的配套规范。P0 已将语义固化为 `contracts/` 下的 OpenAPI、JSON Schema 与正负样例；部分业务路由与执行链已有实现，各项覆盖范围见[交付跟踪](../plans/delivery-tracking.md)。兼容声明只覆盖明确列出的字段、格式和事件，不推断完整 OpenAI API / SDK 兼容。

阅读入口：[REST / Job / 音色](#1-统一接口与传输契约) · [实时状态与事件](#2-实时协议与状态机) · [调用示例](#3-调用示例)

## 1. 统一接口与传输契约

### 1.1 统一的含义

四种能力共享 `SpeechCommand → Admission → Operation → Result / Receipt` 语义，分别通过适合的传输交付。统一不要求把文件上传、持久作业和音频流塞进同一个 HTTP 请求。

| 协议层 | 首期范围 | 调用方依赖 |
| --- | --- | --- |
| OpenAI 兼容层 | Audio 转写与合成的声明子集；标准请求不要求 nexus 或工作台字段 | 标准 SDK 更换 base_url，同步请求的 model 使用已发布 binding ID，合成另需已发布 voice；共享服务密钥是契约允许的部署选项，当前公开语音处理器未实现其校验 |
| SpeechNexus 增强层 | 音色创作、版本 / 预设、durable Job、能力 / 资源、管理；实时 nexus 扩展 | 公开增强 DTO / SDK；不依赖工作台 |
| 工作台应用层 | 项目、会议 / 助手 / 提词 / 有声书编排 | 可选 `/v1/workbench` 和契约声明的 `/v2/workbench`；不宣称 OpenAI 兼容 |

Realtime 契约定义两条连接路径：预创建 SpeechNexus 会话后使用 `session_id`，或标准 SDK 直接以 `model` 打开仅转写的兼容会话。当前生产组合根尚未装配公开 Realtime WebSocket 路由及真实实时执行链；下文的实时协议与示例描述目标，不证明入口可用。两种路径都只兼容本文声明的 ASR 事件和配置子集；不承诺依赖 `response.*`、conversation history 或 tool call 的 OpenAI Realtime 客户端无修改可用。音色设计 / 克隆不冒充标准 speech 参数。完整 SDK 调用与增强边界见 [OpenAI 兼容与增强边界](openai-compatibility.md)。

```text
SpeechCommand
  kind: asr | tts
  context: 服务端解析的 deployment / workspace / policy_revision；可选 client_reference
  input: audio_asset | inline_text | text_asset | live_audio | incremental_text
  profile: profile_id + revision
  binding: model_binding_id + revision
  parameters: 严格类型的场景策略与允许的覆盖
  output: 文本或音频格式、辅助结果、保存策略
  constraints: deadline、可接受的降级、版本前提
```

`context` 不是客户端可自由声明的安全上下文；服务端从部署配置和存在的工作空间构建它。workspace_id 只选择内容 / 预算分组，client_reference 是业务关联字符串，都不构成身份或访问权。无 workspace_id 时使用部署默认值；目标规则要求拒绝未知分组。当前能力目录只校验 workspace 标识格式，不验证其存在或按其过滤，见 §1.5。契约字段使用 `snake_case`，要求拒绝未知字段、布尔冒充数字、单位错误与未知枚举；当前同步 TTS 对 `nexus.overrides` 顶层未知键仍会忽略，见 §1.3。

### 1.2 入口清单

下表列出机器契约的目标入口与语义，不表示每个路由均已接入生产服务。当前同步语音
的装配范围见[同步转写](../operations/synchronous-transcription.md)和[同步语音合成](../components/synchronous-speech.md)；
异步与实时链须分别核对实际部署和验收证据。

当前公开 Job 路由仅装配创建和按 ID 读取；列表、取消、重试与事件流仍是下表的
契约目标。即使创建返回 `202`，生产派发 planner 尚未装配，也不能据此推断任务已执行。

| 接口 | 用途与返回 |
| --- | --- |
| `GET /v1/models` | 部署已发布的 binding 列表；`id` 是 binding ID，`nexus.logical_model` 是展示的逻辑模型名，不暴露私有模型路径 |
| `GET /v1/nexus/capabilities` | 按部署策略、工作空间标识和已发布绑定生成能力快照；当前工作空间不参与目录过滤，详见 §1.5 |
| `GET /v1/nexus/profiles`、`GET /v1/nexus/profiles/{profile_id}/revisions/{revision}` | 列出与读取可用的不可变预设 |
| `POST /v1/audio/transcriptions` | multipart 文件 ASR；成功返回 JSON / text 等声明格式 |
| `POST /v1/audio/speech` | JSON 文本 TTS；首期返回完整音频 |
| `POST /v1/nexus/jobs` | 契约支持持久化 ASR、TTS 及音色设计、克隆、预览和验证作业；返回 `202`、Job 和 Location，各类型须分别核验实际装配 |
| `GET /v1/nexus/jobs`、`GET /v1/nexus/jobs/{job_id}` | 游标分页与作业状态；包含执行版本与结果引用 |
| `POST /v1/nexus/jobs/{job_id}/cancellations` | 幂等请求取消；等待停止证据时返回 `202` 和 `Retry-After`，已终结返回 `200`；接受请求不等于资源已释放，须查看 `cleanup_state` |
| `POST /v1/nexus/jobs/{job_id}/retries` | 从终态任务意图创建新 Job；旧执行尚未得到可核验的停止证据时返回 `409 execution_cleanup_pending`，不复活旧任务或覆盖旧成品 |
| `GET /v1/nexus/jobs/{job_id}/events` | SSE 持久任务事件，支持事件游标续读 |
| `GET /v1/nexus/operations/{operation_id}` | 协调执行状态、cleanup_state 与 receipt；不重放未保存内容 |
| `POST /v1/nexus/operations/{operation_id}/cancellations` | 取消指定实时 / 同步操作；Job 内操作转为所属 Job 的取消命令，避免留下无结果的 required 步骤；进行中 `202`，已终结 `200` |
| `POST /v1/nexus/assets`、`GET /v1/nexus/assets/{asset_id}` | 有界流式上传文件与读取元数据；上传结束校验后才为 `ready` |
| `GET /v1/nexus/assets/{asset_id}/content` | 按部署功能策略下载 ready 输入，支持 Range；不暴露对象存储 key 或服务器路径 |
| `GET /v1/nexus/artifacts/{artifact_id}`、`GET .../{artifact_id}/content` | 读取已提交产物的元数据 / 内容；内容支持 Range，过期或已删除返回 `410 artifact_unavailable`，staging 不可见 |
| `POST /v1/nexus/realtime/sessions` | 验证并固定会话配置，返回 `201`、会话 `id`、连接地址和过期时间 |
| `GET /v1/nexus/realtime/sessions/{session_id}` | 查询会话与 operation 终态；输入确认位置 `input_ack_sample` 是可选字段，首期没有人级 owner 授权 |
| `POST /v1/nexus/realtime/sessions/{session_id}/cancellations` | 从 HTTP 请求关闭会话，便于 WS 不可达时停止执行 |
| `WS /v1/realtime?session_id={session_id}` | 预创建会话的实时 ASR 子集与 `nexus.*` 扩展；session_id 不是访问凭据 |
| `WS /v1/realtime?model={model}` | OpenAI SDK 兼容的 transcription-only 模式；服务端按部署默认值建立会话，不启用 TTS / `nexus.*` 能力 |
| `GET /v1/nexus/voices`、`GET /v1/nexus/voices/{voice_id}/revisions/{revision}` | 部署共享音色目录与版本详情，含兼容绑定与校验事实 |

`GET /v1/models`、`GET /v1/models/{model}` 与 `GET /v1/nexus/capabilities` 三个端点都支持条件请求：`200` 携带基于部署、工作空间、catalog 与功能策略 revision 及响应事实计算的 `ETag`，`If-None-Match` 按 RFC 9110 弱比较命中时返回无 body 的 `304`；`404` / `503` 等错误路径不受影响。

增强资源包括 `/v1/nexus/workspaces`、`/voice-drafts`、`/pronunciation-sets`、`/voice-designs`、`/voice-clones` 和 `/audit-events`。管理资源使用 `/v1/nexus/admin/profiles`、`/v1/nexus/admin/model-bindings`、`/resource-pools`、`/policies`、`/diagnostics` 等明确子资源；隔离租约的运维释放由契约声明 `/v1/nexus/admin/admission-leases/{lease_id}/quarantine-releases`，但当前生产未实现该入口，详见[对账恢复](../operations/reconciliation-recovery.md)。不得仅靠重启或删除记录清空隔离预算。首期不提供 users / memberships / roles API；管理路径本身不形成权限隔离。目标契约使用 `publications` 子资源发布预设和音色的不可变 revision；预设编辑建立新的 draft revision，已发布 revision 不原地修改。当前预设发布尚未交付，见 §1.5；音色各发布路径也须分别核验。

工作台项目、会议、有声书及独立导出任务 / 成品资源置于工作台命名空间；当前契约包含 `/v1/workbench/` 与 `/v2/workbench/`，具体路径以 `contracts/workbench-openapi.yaml` 为准。外部系统直接使用 `/v1/` 与 `/v1/nexus/`，不需要工作台项目或 LLM。P0 为各命名空间分别补齐 OpenAPI，禁止万能管理 RPC。

资产只有在完整接收并验证后才是 `ready`，且 `bytes>0`。音频用途（`asr_input` / `voice_reference`）的 ready 资产必须同时提供大于零的 `duration_ms`、实际 `sample_rate_hz` 与 `channels`；`text_input` 不得携带这三项音频元数据。验证失败保留 `rejected` 事实，后续 Job / 音色命令不能把 staging、rejected、expired 或 deleted 资产当作可执行输入。

当前资产服务须配置存储才能启用，默认工厂使用文本检查器；公开音频检查尚未接入，
不能根据上述目标语义假定音频上传可发布为 `ready`。上传结果未知时按原身份协调，
不以重发创建新的对象写入；当前范围见[资产发布与恢复](../components/production-assets.md#publication-and-recovery)。

### 1.3 同步 REST 子集

以下当前入口的参数和限额错误以服务已装配为前提；路由先检查服务是否可用，未装配时返回
`503 backend_unavailable`，不会先执行这些请求校验。

文件转写支持 `file`、`model`、`language`、`prompt`、`response_format` 及快照声明的 `timestamp_granularities[]`。`response_format` 默认 `json`，基线为 `json`、`text`；`verbose_json`、`srt`、`vtt` 仅在具备相应时间边界证据时开放。`timestamp_granularities[]` 只接受已声明支持的 `segment` / `word`，且必须与 `response_format=verbose_json` 同用，不伪造词级时间戳。当前入口对非法或重复粒度以及与其他格式的组合返回 400 `invalid_request`；通过入口校验但当前执行链不支持的粒度返回 422 `unsupported_parameter`。标准 language 省略表示自动检测，内部规范化为 `asr.language=auto`，不要求标准客户端发送字面值 auto。`prompt` 是有界术语提示；不具备提示能力的绑定明确拒绝。增强结果返回实际语言或 unknown，兼容 JSON 保持所声明的字段形状。

上述时间戳参数是契约范围；当前生产文件 ASR 规划器在准入前拒绝所有非空 `timestamp_granularities[]`。具体装配和结果证据要求见[同步转写](../operations/synchronous-transcription.md)。 当前 multipart 入口把单个空的 `response_format` 字段按默认 `json` 处理；机器契约只允许声明的格式枚举，不接受空字符串。客户端应省略该字段或发送有效格式，不应依赖此实现差异。 `language` 的请求 schema 只限制最大长度，当前命令还要求 2–3 位小写语言码及可选子标签；空值或不符合该形状时入口返回 `400 invalid_request`，选定 binding 后，形状有效但不在其支持域时返回 `422 unsupported_parameter`。

非 verbose `Transcription.usage` 可按绑定真实证据使用 duration 或 token；`verbose_json.usage` 固定为 duration，以匹配 OpenAI SDK 的响应模型。不得为了展示 token 数而破坏 `verbose_json` 的可解析性。

TTS 支持 `model`、`input`、`voice`、`response_format`、`speed`、`instructions` 与 `stream_format=audio`，具体可选参数及取值域以绑定和音色快照为准。首期标准 voice 接受已发布的字符串 ID；不宣称支持 OpenAI 托管音色或任意 voice 对象。`response_format` 默认 `mp3`，基线为 `mp3 / wav / pcm`；MP3 由有界编码步骤提供并计入 CPU、缓冲与执行预算。REST `pcm` 固定为无头、24 kHz、单声道 signed 16-bit little-endian，adapter 负责必要重采样。默认 `speed=1`；非默认 speed 和 instructions 只有绑定实际支持时才接受。`stream_format` 只接受 `audio`，当前入口对其余取值及未知 JSON 字段返回 400 `invalid_request`；公开契约对不支持的标准参数要求 422 `unsupported_parameter`，两者存在差异。契约内但绑定未发布的格式返回 422 `unsupported_parameter`，不静默降级。禁止静默忽略参数或将默认 MP3 替换为 WAV。[OpenAI TTS 格式与默认值](https://developers.openai.com/api/docs/guides/text-to-speech)

同步 TTS 路由从已发布 binding 与 voice revision 解析，缺席即 `model_not_found` / `voice_not_found`，不回退默认模型或草稿；`response_format`、`speed`、`instructions`、`language`、`seed` 按 binding 支持域收窄。音频时长与源 PCM 上界写入合成策略，超出时整条请求失败，不做截断。冻结的 `SpeechRequest` 尚无 `max_output_audio_ms` 字段，当前上界来自部署策略；契约补齐后必须与该值取较小者。响应状态以机器契约为准：`model_not_found` / `voice_not_found` 使用 404，方法不符使用 405 `speech_method_not_allowed`；`instructions` 超过部署上限时返回 422 `unsupported_parameter`。`X-Request-ID` 是 1–128 字符关联值，不满足部署身份规则时由服务端生成并回显。

截至 2026-11-03，生产工厂可在私有配方、独立结算、网关与对象存储配置完整且存在合格声音时装配同步链；缺失配置或资格时保持不可用。完整原命令、原身份恢复、声音资格与锁后复验见[同步语音合成](../components/synchronous-speech.md)。[第六次受控窗口](../audits/2026-11-03-controlled-tts-prepare.md#第六次独立授权窗口完整公开-sdk-关联通过)取得标准 SDK、真实引擎、固定对象及停止结算的关联证据；它不代表全部模型、负载或多机资格。

跨 binding/voice 查询使用同一个只读 REPEATABLE READ 快照；快照本身不保护解析后的准入竞争，必须在准入时按声音及 binding 的共同锁和屏障复验。同步 `SpeechRequest` 不增加 Job binding pin。

当前同步 HTTP 入口仍拒绝 `Idempotency-Key`，返回 422 `unsupported_parameter`；内部持久身份和恢复入口不能当作客户端幂等键重放能力。成功响应不伪造 `X-Operation-ID`。这是当前实现与契约声明之间需要继续解决的差异。

内部 `SpeechPlan` 固定本次解析的 `model_id`、`workspace_id`、
`binding_revision_id`、`workspace_policy_revision` 和 `voice_revision_id`，
供执行适配器使用；适配器不得重新选择最新绑定或工作空间策略。
这些字段是已选事实的交接，不认证声音资格，也不替代持久提交、准入锁后复核或恢复记录。

增强参数放在 TTS JSON 的 `nexus` 对象或 ASR multipart 的 `nexus` JSON 字符串中。两者共同接受 `workspace_id`、`client_reference`、`profile_id`、`profile_revision`、`overrides` 和 `deadline_ms`；TTS 另接受 `voice_revision`、`pronunciation_revision`，ASR 的 `overrides` 只能包含 `asr` 策略，不借用 TTS 字段。当前同步转写入口的 `asr` 覆盖仅接收 `max_segment_ms`；其他字段即使已在共享 ASR 覆盖 schema 中声明，也会返回 `422 unsupported_parameter`。同步 REST 的标准 `speed`、`instructions` 与 `nexus.pronunciation_revision` 是唯一字段来源，`nexus.overrides.tts` 不重复定义它们。 TTS 契约要求 `nexus.overrides` 只含 `tts`，但当前入口没有校验该对象的其他键，会忽略它们；客户端不能依赖此行为，应只发送契约声明的字段。`tts` 对象内部的未知字段仍会被拒绝。完全省略 nexus 时，使用部署发布的默认工作空间、预设和音色版本解析规则，保证标准客户端能直接调用；nexus 为可选扩展，不能成为兼容路径的必填包装。

普通同步 ASR/TTS 不自动建立供用户浏览的永久业务内容记录，也不自动保存源文件或音频；执行和恢复所需的私有持久事实按具体链路保留。服务端检查输入与执行上限，不在超时后偷偷转成后台作业。当前 ASR 上传超限返回 `413 payload_too_large`，音频时长超出同步上限返回 `422 async_required`；TTS 请求体或文本超出入口限制返回 `400 invalid_request`，生成结果超出音频上限返回 `503 speech_output_limit_exceeded`。不能把所有限额错误都视为 `async_required`；需要 Job 时由客户端明确发起。已经开始同步执行但超时的请求不保证得到结果，不能盲目新建相同意图；当前同步入口尚不支持客户端幂等键恢复，TTS 的原身份恢复按[内部恢复入口](../components/synchronous-speech.md)处理。

OpenAI Audio 的字段与兼容范围分别对照[转写接口](https://developers.openai.com/api/reference/resources/audio/subresources/transcriptions/methods/create)及[语音接口](https://developers.openai.com/api/reference/resources/audio/subresources/speech/methods/create)；SpeechNexus 只承诺本文列出的子集和 P0 冻结的 SDK 兼容测试，不声称支持 OpenAI 的全部模型、格式或事件。

### 1.4 持久化 Job 请求与结果

```json
{
  "kind": "asr",
  "workspace_id": "ws_demo",
  "client_reference": "project:prj_demo",
  "input": {"audio_asset_id": "ast_input"},
  "profile": {"id": "file-accurate", "revision": "1"},
  "model": "asr-quality",
  "overrides": {"asr": {"language": "zh"}},
  "output": {"transcript_format": "json", "save_artifacts": true},
  "constraints": {"max_queue_wait_ms": 600000, "allow_degradation": false}
}
```

ASR `input` 只接受所选工作空间存在、功能策略允许且为 `ready` 的 `audio_asset_id`；TTS `input` 使用 `text` 或 `text_asset_id` 二选一，顶层 `voice` 使用 `{id,revision}`，可选 `pronunciation` 也使用 `{id,revision}`。任务创建时解析 `model` 为 `ModelBindingRevision`，固定所有执行依赖，在响应 `effective_configuration` 中回显；排队期间不随“默认模型”或预设发布而变化。

TTS Job 可选 `model_binding={id,revision}` 固定精确版本：新意图要求该版本是当前已发布、未退役的权威指针，支持 `tts_file`，且其逻辑模型与 `model` 一致；不满足时明确拒绝，不回退其他版本。省略时当前实现将 `model` 作为 binding 对象 ID，选择其当前未退役发布版本。相同幂等键的原请求先按原身份重放，即使发布指针后来改变；结果未知时保留原键和完整请求协调，不能更换版本重试。Job 已接受不证明已执行；生产派发 planner 尚未装配，见[公开 Job 派发器](../components/production-jobs.md#公开-job-派发器del-96-第一步)。以下是契约形状示例，资源和时间均为示意。

```json
{
  "id": "job_demo",
  "object": "nexus.job",
  "kind": "asr",
  "status": "queued",
  "cleanup_state": "not_required",
  "request_id": "req_demo",
  "workspace_id": "ws_demo",
  "profile": {"id": "file-accurate", "revision": "1"},
  "execution_revision": "1",
  "progress": {"unit": "audio_ms", "completed": 0, "total": 120000},
  "artifacts": [],
  "result": null,
  "operations": [],
  "effective_configuration": {
    "models": {"asr": {"id": "asr-quality", "revision": "1"}},
    "policy_revision": "1",
    "catalog_revision": "1"
  },
  "error": null,
  "created_at": "2026-10-23T00:00:00Z",
  "updated_at": "2026-10-23T00:00:00Z"
}
```

`status` 取值为 `queued | running | retry_wait | cancelling | succeeded | failed | cancelled | expired`；cleanup_state 单独报告 not_required / pending / complete / quarantined，未清理的 Operation 以引用返回。取消确认超时为 failed / cancellation_unconfirmed，资源继续隔离，旧执行清理前 retry 返回 execution_cleanup_pending。

`result` 始终存在，非 `succeeded` 状态必须为 null。成功时其判别字段 `kind` 必须与 Job 一致：ASR 返回 transcript Artifact，TTS 返回 audio Artifact，音色 design / clone 返回 draft revision 与 preview Artifact，voice preview 返回 preview Artifact，voice validation 返回完整校验结果与 validation-report Artifact；所有成功结果都携带 receipt。`artifacts` 至少包含结果引用的主产物，结果内 Artifact ID 必须引用 `artifacts` 中的同 kind 项。输入过期或任务超过执行前队列期限，转为 `expired` 且 `cleanup_state=not_required`；成功结果达到保留期限时仍保留成功终态，只将 artifact 标为过期。

已提交 Artifact 与 Job 结果共享同一 receipt；`JobArtifact.artifact_id` 在单个成功结果中唯一，结果引用的主 Artifact 必须存在、kind 匹配且 `receipt_id` 等于结果 receipt。Artifact / JobArtifact / SavedAsset / ExportArtifact 的内容字节数必须大于零，不能用空文件冒充已提交制品。`job.succeeded` 也须满足这些跨数组规则；生产者须在写入事件前校验，当前证据及 schema 覆盖限制见 §1.6。

ASR 结果的目标设计使用统一 `TranscriptDocument`：`id`、`revision`、`language`、`text`、`sample_rate_hz`、`coordinate_space`、`segments`、`auxiliary_status`。segment 包含 `segment_id`、`sample_span`、`text`、`text_revision`，以及可选 `words`、`speaker_assignments`。统一坐标为 `canonical_audio`、24 kHz，原文件解码 / 重采样映射的 revision 进入 receipt；不能把原文件 48 kHz 下标直接填入 sample_span。`auxiliary_status` 分别声明 alignment / diarization 的 `not_requested | pending | complete | failed | unavailable`。不把 segment 平均时长伪造成 word timestamps；未知说话人为 null / unknown。当前公开 Job 的 `AsrJobResult` 只固定 `transcript_artifact_id` 和 `receipt_id` 等引用，尚未在该契约中定义上述制品正文结构；消费者不能仅凭 Job schema 假定这些字段已经可用。

TTS 目标设计要求保留实际格式、采样率、声道、时长、摘要与执行回执。当前公开 `Artifact` / `JobArtifact` 只声明媒体类型、字节数、摘要及可选 `receipt_id` 等元数据，不直接声明 `sample_rate_hz`、`channels` 或 `duration_ms`；`TtsJobResult` 返回音频 Artifact 和 receipt 的 ID。Receipt 区分 requested / effective / observed 参数，但各事实字段并非全部必填；缺失或未知事实须按其 `missing` / `unknown` 表示，不能假定所有音频属性都已从公开响应取得。只有实际观察到的事实进入 observed，固定 seed 也不承诺跨引擎 bitwise 一致。

### 1.5 能力协商与版本

能力快照包含 `snapshot_id`、`catalog_revision`、`policy_revision`、生成时间、逻辑模型绑定、参数域、音色 revision、预设 revision、格式、辅助能力及限制。readiness 分别报告配置完整、引擎就绪、质量资格、当前可准入状态；`available` 不等于业务生产质量已通过。

当前 PostgreSQL 目录投影提供已发布 binding 的模型信息，`profiles` 和 `voices` 暂为空；
能力接口对传入的 `workspace_id` 只检查标识格式，不查询其是否存在或按其过滤目录。
当前代码中，HTTP 草稿保存的 `definition_json` 不含 `binding_id`，目录读取却要求该字段；
因此该路径创建并发布的 binding 会使目录读取失败并返回 `503 model_catalogue_unavailable`。
这是对当前写入和读取代码的静态判断，历史实测见[本地记录](../archive/operations/local-end-to-end.md#51-这是主干缺陷不是本-runbook-的配置错误)；尚未在当前版本重跑端到端验证。
默认目录装配未接入实时 readiness 证据，模型的 `engine_ready`、`quality_qualified` 为
`false`，`admission_state` 为 `unavailable`，表示这些事实未由该目录建立，不单独证明
实际引擎故障或资格失效。因此返回快照不证明工作空间存在、可访问或具备准入资格；
提交时仍须经过各自的权威检查。

绑定发布、部署资格与具体实例的运行事实分开。按需模型可以支持提交排队任务而当前尚未加载；不能用单一就绪标志混淆“支持”“可排队”“可立即执行”。实例卸载不删除绑定或音色 revision，重建后重新核对配置与资格；字段和状态由 P0 schema 冻结，组件分工见[整体技术方案](technical-blueprint.md#4-绑定资格与实例分离)。

模型 binding 的退役、激活与历史 revision 回滚已通过显式 head 指针和 append-only 审计事件落地；普通 `retire` 只影响后续选择，在途 Job 保持准入时固化的有效配置。`disable` 还登记持久停用意图，受停用屏障和清理流程约束，其回执不证明执行已停止或容量已释放，见[绑定生命周期](../operations/binding-lifecycle.md)。内建声音已有独立资格与发布路径，受控证据见[声音资格说明](../operations/builtin-voice-qualification.md)；不能把 binding 的验证结果外推为 voice 资格，声音创建等其他路径须各自验收。

目标契约中的模型绑定发布同时负责把一个 draft revision 激活为当前 revision；当前发布 HTTP 路由尚未实现，已有修订的发布使用[运维命令](../operations/publish-binding-revision.md)。契约请求中的 `expected_revision` 是目标 revision，`expected_current_revision` 是发布前的当前指针；两者分离，避免并发发布时“谁最后提交谁生效”的丢失更新。指针已变化返回 `409 configuration_changed`，目标不是 draft 返回 `409 binding_not_draft`；首期没有身份或审批层级，CAS 只保证操作者看到的是自己确认的版本。

场景预设的目标契约采用同样的不可变 revision / 当前指针分离规则。当前实现仅提供 `POST /v1/nexus/admin/profiles` 根据 `expected_latest_revision` 原子分配只读 draft revision 和固定 revision 读取；发布、激活及公共列表仍属未交付范围，见[Profile 草稿](../components/profile-drafts.md)。目标契约要求 `POST .../revisions/{revision}/publications` 提交对应的 `ProfileValidation`，核对参数域、能力组合、override 范围和 production 资格后才切换当前指针；回滚通过 `.../activations` 选择已发布 revision，退役通过 `.../retirements` 清除当前指针而不自动回退。已固定的 Job / 实时会话继续使用其 revision，管理操作只影响后续选择。

快照按部署、工作空间标识、catalog 与功能策略生成；ETag 包含这些身份、revision 和响应事实，不混用不同部署配置。当前成功及 304 响应使用 `Cache-Control: no-store`；条件请求支持 304，但不能据此让缓存保存响应。外部入口保护须按实际部署核验，当前公开语音处理器不验证共享服务密钥。读取不触发加载、下载或卸载，不承诺未来槽位。提交可提供 expected_catalog_revision，不匹配为 `409 configuration_changed`；执行前重新检查资源状态、功能策略和容量。首期没有按用户投影或 authorization_revision。

Profile、voice 和 model binding revision 不可变；升级只影响后续新会话与新 Job。运行中的实时会话不热换模型、采样率或音色版本。队列中的固定版本不可用时明确失败或等待恢复；不得静默漂移到新版本。

### 1.6 错误、分页与幂等

```json
{
  "error": {
    "code": "capacity_unavailable",
    "type": "api_error",
    "message": "当前实时容量已满，请稍后重试。",
    "param": null,
    "request_id": "req_demo",
    "retryable": true,
    "retry_after_ms": 1000
  }
}
```

| HTTP | 稳定 code 示例 | 重试规则 |
| --- | --- | --- |
| 400 | `invalid_json`、`invalid_audio` | 修正输入；不直接重试 |
| 401 / 403 | `service_credential_required` / `origin_rejected`、`feature_disabled` | 仅在配置服务入口凭据或功能 / Origin 策略时适用；不跳转登录 |
| 404 | `resource_not_found` | 对象不存在或不属于该请求工作空间；不据此宣称用户隔离 |
| 410 | `artifact_unavailable`、`event_history_expired` | 内容 / 事件已过保留期；读取当前状态，不重建已删除内容 |
| 409 | `configuration_changed`、`idempotency_conflict`、`operation_in_progress`、`execution_cleanup_pending` | 按响应协调版本或查询原意图 |
| 413 | `payload_too_large` | 分段或改用有配额的文件上传 |
| 422 | `unsupported_parameter`、`incompatible_profile`、`async_required` | 改请求；不得静默删参数 |
| 429 | `quota_exceeded`、`queue_limit_exceeded` | 遵循 Retry-After，受总重试期限约束 |
| 503 / 504 | `backend_unavailable`、`capacity_unavailable` / `operation_timeout` | 写入或执行结果未知时先协调原意图；`retryable` 为 true 本身不保证重发安全。固定只读查询按对应接口规则可显式重试，不凭读取失败创建新意图 |

契约中的分页列表使用 `data`、`has_more`、`next_cursor`，共用的 `limit` 为 1–100，默认 50；Job 列表按 `(created_at,id)` 排序。`JobList` schema 要求 `has_more` 和 `next_cursor` 字段存在，但允许后者为 null，未约束两者的一致性；生产者须保证续页语义，客户端不能只凭结构校验确认可继续读取。OpenAI 兼容的 `/v1/models` 列表使用 `object` 和 `data`，不声明这些分页字段或游标参数。分页游标绑定过滤条件、部署与工作空间，不能跨请求作用域续读；这属于查询一致性，不是权限边界。SSE `id` 是持久化 job_event_seq，支持 `Last-Event-ID`；事件保留期结束后返回 `410 event_history_expired`，客户端读取 Job 当前状态，不伪造历史。

`JobEvent.data` 不是任意 JSON：每个 `job.*` / `artifact.published` 类型都有独立的封闭 data schema，分别约束其声明的字段和状态。目标语义要求 `job.succeeded` 至少携带一个 committed Artifact 和与权威 Job kind 匹配的结构化 `result`；`job.failed` 即使进入 `cleanup_state=quarantined` 也保持失败结果与结构化错误；`job.cancelled` 只允许 `not_required` / `complete`。未知嵌套字段和不匹配的 `type` / `status` 组合按契约错误拒绝，不能借事件扩展绕过稳定字段。工作台的 MinutesJob、AssistantTurn、ExportJob 与 ExportEvent 使用同一原则：失败错误为结构化 `code / message / retryable / param`，非 failed 状态不得携带非空错误对象。

这些成功语义还需生产者校验：当前 `job.succeeded` 事件 schema 要求非空产物和结构化结果，但不限制成功事件的 `cleanup_state`，也不单独验证结果引用与产物数组的一致性。契约测试另以辅助检查验证产物 ID、kind 和 receipt 的跨数组关系；测试辅助函数不等于生产事件写入路径已接入这些检查。事件本身也没有 Job kind 字段，消费者需结合权威 Job 核对类型。

工作台终态还要求产物自洽：MinutesJob `succeeded` 至少提供非空 `minutes_text` 或 `artifact_id`，非成功状态只可保留明确的部分文本，不得发布成品 `artifact_id`；AssistantTurn `succeeded` 必须有非空 `output_text`；RenderUnit `generating` 必须带 `speech_job_id`，`ready` 还必须同时带 `source_artifact_id` 与 `source_receipt_id`；Meeting 的 `sealed` / `minutes_pending` / `minutes_ready` 必须带 seal revision 与结束时间，后两者还必须带 `minutes_id`。Meeting 的 `failed` 表示封存前的采集失败，必须带结束时间和结构化 `error`，且不得携带 seal revision 或 minutes 引用；纪要任务失败保留在 `minutes_pending` 并通过所引 MinutesJob 的结构化错误呈现，不把整个会议改判为采集失败。从语音 Artifact 导入转写、保存副本或 render unit 时必须同时固定 Artifact ID 与 receipt，saved asset 的 receipt 不可为空；非 succeeded 的 ExportJob 不得携带成品 Artifact，显式导出子集不得为空。这些约束不替代跨资源校验，服务端仍须确认 source receipt 等于 Artifact 的 receipt、source Artifact 属于目标项目工作空间、MinutesJob 成功、单元副本已保存且版本一致后才能宣称整书可导出。

目标契约要求创建 Job、创建会话、同步推理、音色创作和发布资源支持 `Idempotency-Key`；实际能力须逐入口核验。当前同步 ASR 和 TTS HTTP 入口均拒绝该请求头，详见[同步转写](../operations/synchronous-transcription.md)和[同步语音合成](../components/synchronous-speech.md)。目标作用域为 `(deployment_id, workspace_id, operation_family, key)`，服务端原子记录规范意图摘要、脱敏的显式配置引用、固定执行版本与执行身份；未明确保存的正文只留摘要，不把同步输入全文放入幂等表。摘要基于规范命令、精确输入内容 / 摘要及显式版本，不混入传输路径或后来变化的默认值；便捷入口与 Job 入口先映射为同一命令。确定部署 / 工作空间并执行当前入口检查后，先查找或原子 claim 幂等记录；只有新意图才解析可变默认预设 / 模型 / 音色并固化，重复请求复用原固定版本。当前删除 / 强制禁用仍按资源规则限制内容返回，不因此重新执行。相同 key / 同意图返回原执行，相同 key / 不同意图返回 `409 idempotency_conflict`。进行中 Job 返回 `202` 与原 Job；进行中同步请求返回 `409 operation_in_progress` 与 `error.nexus.operation_id`；查询路径固定为 `GET /v1/nexus/operations/{operation_id}`。

目标契约中的同步推理终态协调接口为 `GET /v1/nexus/operations/{operation_id}`；不得将其视为当前同步入口已提供客户端幂等恢复的证据。默认仅持久化意图、状态与脱敏 receipt，不保存完整 JSON 转写或音频响应；相同 key 对应操作已完成但内容不可重放时，返回 `409 response_not_replayable` 及 `error.nexus.operation_id`、`error.nexus.receipt` 中的原成功 receipt，绝不再次执行。`succeeded` 必须返回非空 `ExecutionReceipt`，且 Job / Operation / 实时终态引用只允许 `cleanup_state=not_required` 或 `complete`；`pending` / `quarantined` 不得冒充成功。REST 查询和 `nexus.operation.done` 使用同一要求；失败或取消可以保留已观察 receipt，但不得用 receipt 冒充成功。需要可靠获取或重放完整结果的调用方使用 Job。流式响应中断同样不自动重播先前音频。

作业内重试窗口默认最多 24 小时，意图与步骤去重记录在所有 attempt 终结后至少保存 7 天；任务仍未终结或取消状态未知时不得删除该记录。调用方主动 retry 是创建替代 Job 的新意图，使用区别于原 Job 创建请求的新 key，旧 Job 保持原终态；若该 retry 请求结果未知，保留 retry 本身的原 key 和请求协调，不能为同一次 retry 换 key 重发。无用户体系时，同一工作空间共享幂等作用域；客户端为每个新意图生成高熵唯一 key，不以昵称或浏览器标识建立虚假的个人命名空间。P0 的故障用例必须覆盖“远端已执行、客户端超时”和“产物写入后进程退出”。

### 1.7 音色创作与增强协议

本节的设计、克隆、候选验证与发布流程是契约和设计目标；当前生产装配尚未接通
完整音色创作链。已有预置音色资格或同步 TTS 证据不能替代该链路验收。

标准 `/v1/audio/speech` 使用已发布音色或预置 speaker；instructions 仅控制当前绑定支持的表达方式，不隐式创建、克隆或发布音色。音色创作有自己的 `VoiceCommand`，复用 Admission、Operation、Job、Artifact、ExecutionReceipt 与幂等规则，不要求消费方使用工作台。

| 增强入口 | 规范命令 / 结果 |
| --- | --- |
| `POST /v1/nexus/voice-designs` | `voice_design`；描述、预览文本、语言与逻辑模型 → `202` 的 `VoiceCreationAccepted`（draft 与 job）→ 任务完成后的候选、试听 Artifact 与 receipt |
| `POST /v1/nexus/voice-clones` | `voice_clone`；ready 参考音频、可选参考正文、使用确认与逻辑模型 → `202` 的 `VoiceCreationAccepted`（draft 与 job）→ 任务完成后的候选、试听与来源记录 |
| `GET /v1/nexus/voice-drafts/{draft_id}`、`PATCH .../{draft_id}` | 读取固定 revision 或当前 head；PATCH 只接受 display_name 等元数据及 expected_metadata_revision，递增 metadata_revision |
| `POST /v1/nexus/voice-drafts/{draft_id}/generations` | expected_draft_revision + 完整声音输入 / model → 原子创建下一 draft_revision 与 voice_design / voice_clone Job；`202` 的 `VoiceGenerationAccepted` 包含 draft、revision 和 job，生成完成后才可试听 / 校验 |
| `POST /v1/nexus/voice-drafts/{draft_id}/previews` | 对固定候选 revision 进行短文本 TTS，返回 `202` Job；不发布或修改候选 |
| `POST /v1/nexus/voice-drafts/{draft_id}/validations` | `voice_validation`；固定候选与绑定，返回 `202` Job，完成后取得格式 / 素材 / 兼容性 / 质量检查结果 |
| `POST /v1/nexus/voice-drafts/{draft_id}/publications` | 固定候选和校验 revision，原子生成可用于 TTS 的不可变 VoiceRevision；首次成功 `201`，幂等重放返回同一发布 |

Job.kind 为 asr / tts / voice_design / voice_clone / voice_preview / voice_validation。音色使用独立 VoiceCommand，不套用 ASR/TTS profile；从模型绑定、音色功能策略和参数域生成执行配方。便捷入口与 `POST /v1/nexus/jobs` 使用相同规范意图、operation_family 和请求摘要去重，换路径不能重复创建。voice_preview 是独立任务类型，内部复用 TTS 计算与计量；兼容 speech 入口只接受已发布 voice。

Job 入口的音色 body 使用 kind、可选 workspace_id / client_reference、model、input 和 constraints；design 的 input 对应 description / preview_text / language / display_name，clone 对应 reference_audio_asset_id / reference_text / preview_text / language / display_name / source_use_confirmed。初次创建在同一事务写入 VoiceDraft、draft_revision=1、Job 与 outbox；便捷创建响应的 `draft.id` 和 `draft.head_revision` 标识已分配的草稿及版本，结果尚未就绪。Job 的 `effective_configuration` 未声明草稿身份字段，成功结果另含 `voice_draft_id` 和 `draft_revision`。重新生成使用可选顶层 `draft_target={id,expected_revision}`，指定现有草稿；generations 入口把 path ID 与 expected_draft_revision 规范化为该字段，body 的 model 与完整 input 作为新声音配方，省略旧字段不会隐式继承。类型保持 design 或 clone，切换类型创建新草稿。版本比较、分配下一 revision 与 Job / outbox 同事务提交；同幂等键先重放原意图，不因 head 已前进而冲突。已有生成未终结时以新 key 再生成返回 `409 operation_in_progress`。

preview / validation 的 input 固定 voice_draft_id / draft_revision；preview 必填 text，validation 使用能力矩阵中已冻结的测试集 revision，model 默认沿用候选绑定。便捷入口将 path 中的 draft_id 与 body 中的 expected_draft_revision 规范化为相同引用；preview / validation body 另带 text 或 validation_set_revision，不能隐式使用“当前最新”候选。草稿 GET 支持 draft_revision 查询；历史配方、候选和校验可追溯，不覆盖旧 revision。

成功创作任务在 result 返回 voice_draft_id / draft_revision / preview_artifact_ids；preview 返回试听 Artifact，validation 返回 validation_id / validation_set_revision / checks / publication_eligible 与绑定 / 候选 revision。每个 check 显式标记 `required` 且名称在单个校验内唯一；只有所有 required check 均为 `passed`，且其中包含 required、passed 的 `reusable_tts` 检查时，`publication_eligible` 才能为 true。契约允许总体 `status=passed` 时存在 `skipped` 的 optional check，但不能将该项描述为已通过；`status=failed` 必须至少有一个 failed check。候选、校验引用、Artifact 与 Job 成功状态按 [作业与产物发布](runtime-governance.md#22-作业与产物发布) 的同一提交规则写入，不让半发布结果可见；未发布候选不进入生产音色目录。

`VoiceValidation` schema 检查已列出的 required 项及 `reusable_tts` 的状态，但不保证 check 名称唯一，也不能证明固定测试集中的必需项全部列出或检查结果真实。生产者须对照固定测试集和证据执行这些校验；契约测试中的辅助检查不证明生产者已装配该逻辑。

描述设计示例：

```json
{
  "workspace_id": "ws_demo",
  "model": "voice-design",
  "input": {
    "description": "温暖、清晰、节奏舒缓的中文叙述声音",
    "preview_text": "欢迎来到今天的故事。",
    "language": "zh",
    "display_name": "叙述候选"
  }
}
```

样本克隆示例：

```json
{
  "workspace_id": "ws_demo",
  "model": "voice-clone",
  "input": {
    "reference_audio_asset_id": "ast_voice_reference",
    "reference_text": "这是一段用于创建声音的示例素材。",
    "preview_text": "这是新的试听文本。",
    "language": "zh",
    "display_name": "参考声音候选",
    "source_use_confirmed": true
  }
}
```

参考音频必须是部署内 ready asset，禁止通过请求任意下载 URL 或读取服务器路径；素材长度、声道、格式、正文要求和文本上限由能力快照声明。source_use_confirmed 只记录本次调用方的使用确认，不是身份验证或独立授权证明。无用户体系时只能记录匿名调用或共享入口凭据标签，不能声称已验证具体个人、双人审核或实名同意。

发布示例：

```json
{
  "expected_draft_revision": "2",
  "validation_job_id": "job_voice_validation",
  "manual_confirmation": true,
  "display_name": "叙述声音"
}
```

每个 draft_revision 的状态为 `draft → generating → candidate → validating → validated → published`，失败保留原因；生成配方与候选字节不可变。`generating` 必须引用生成 Job 且尚无预览；`candidate` 必须有生成 Job 和至少一个试听 Artifact；`validating` / `validated` / `published` 还必须引用校验 Job 并保留候选期已产生的预览引用；只有 `published` 可以携带发布 Voice ID / revision，且该状态必须同时具备生成、校验和发布引用。`failure` 只允许出现在 `failed`，失败 revision 不能携带发布引用。PATCH 修改声音描述、样本、参考正文、预览文本或绑定返回 `422 unsupported_parameter`，这些修改必须调用 generations 创建新 revision。新 head 在重新生成 / 校验前不能发布，旧校验不能满足新 revision；旧候选、旧校验和已经发布的 VoiceRevision 保留历史事实。新 head 与旧 validation 同时完成时，只更新各自 revision，不让迟到结果覆盖 head。试听与机器校验不自动构成发布确认。publication 固定当前 head 的 expected_draft_revision、对应 validation_job_id、素材 / 绑定和未过期资格；head 已变化返回 `409 configuration_changed`。发布记录回显 `validation_job_id` 与 `validation_id`，生产者必须确认该 Job 已成功返回同一 draft / revision / validation，且 validation 已 passed、publication_eligible，再原子写入 draft revision、VoicePublication、VoiceRevision 和目录指针。VoiceRevision 与 validation 的 model binding 必须一致，其 source 必须指向同一 draft revision；design 不能携带来源音频摘要，clone 必须携带参考资产摘要，builtin 不带 draft 来源。每个草稿 revision 最多一次发布，初次建立 voice_id 和 revision 1，后续发布在已有 voice_id 上分配严格递增且连续的下一个 revision；重复发布返回原结果或明确冲突，不重复建立目录项。首期 manual_confirmation 是显式内容确认，不能证明审核者与创作者是两个人。

上述试听引用是业务要求；当前 `VoiceDraftRevision` schema 对 candidate、preview_only、validating、validated 和 published 的 `preview_artifact_ids` 只在字段存在时限制至少一项，未将该字段列为必填。生产者和消费者须另行核验引用存在且可用，不能仅凭 schema 通过推断已有试听制品。

公开 VoiceRevision 包含来源类型、模型绑定、适用的素材摘要、样本 / 语言能力和验证摘要；引擎侧句柄及其他私有依赖由内部保存，不属于公开 VoiceRevision。公开结果不返回特征向量、任意文件路径或引擎内部句柄。调用 TTS 时仍检查音色与模型绑定兼容性，不把任意后端音色资产宣称为跨模型通用。

描述式生成可能只产生试听音频。只有已验证“候选 → 可复用参考 / 特征 → 后续 TTS”的完整路径，才能发布可持久使用的声音；否则标为 preview_only，不提供 publication。Qwen3-TTS 的 VoiceDesign、Base 和 CustomVoice 具有不同角色，由各自正确的模型绑定完成适配，不依赖同一实例自动支持全部类型。[vLLM-Omni 音色与模型角色](https://docs.vllm.ai/projects/vllm-omni/en/stable/serving/speech_api/#qwen3-tts)

平台维护音色注册表与版本，原生引擎的音色上传 / 删除只允许 adapter 内部调用，不向 WebUI 透传。底座 API 与引擎注册表之间的控制边界独立于首期是否有用户认证。[上游音色存储边界](https://docs.vllm.ai/projects/vllm-omni/en/stable/serving/speech_api/#voices-endpoint)

## 2. 实时协议与状态机

本节描述实时契约和目标状态机；事件 schema 与解析器不证明生产 WebSocket、真实引擎
或会话恢复链已装配，当前交付范围以[交付跟踪](../plans/delivery-tracking.md)为准。

### 2.1 创建、入口检查与连接

创建会话时提交可选 workspace_id / client_reference、profile、modalities（asr、tts 或两者）、逻辑模型和输出需求。REST 校验部署入口、参数、资源状态、版本与能力，固定配置并建立 created 会话；未在 `expires_in_s` 内连接即过期，该值允许 5–300 秒，默认 30 秒。此时不占推理槽。

标准 OpenAI Realtime SDK 可省略 REST 会话创建，直接连接 `/v1/realtime?model={model}`。该兼容模式只建立 transcription-only 会话，使用部署默认工作空间、ASR 预设和音频格式；连接握手时完成准入并返回标准 `session.created`。它不接受 TTS、nexus 假设 / 辅助事件或双工能力。需要固定 profile、workspace、音色或 TTS 时，调用方必须先创建 nexus 会话。连接请求必须且只能提供 `session_id` 或 `model` 之一；`session_id` 是路由标识，不是凭据，服务密钥通过 Authorization 头传递，不放在 URL 中。

首期浏览器无登录 cookie、access token 或用户会话，使用同源入口并校验 WebSocket Origin / Host；SDK 直接连接部署地址。若部署选择共享服务密钥，SDK 使用 Authorization Bearer，浏览器经同源部署代理注入服务端凭据。密钥不进入 URL、页面包、localStorage、日志或 WS 子协议。session_id 只用于路由；连接时检查存在性、工作空间、过期和是否已有活跃连接，不把它当作用户所有权或隐式秘密。

WS 建立后进入 `admitting`，原子申请所需资源；最多等待已批准的实时等待预算。只有分配成功才发送 `session.created`；预先创建的 Nexus 会话还发送 `nexus.session.ready`，标准兼容模式不发送 Nexus 事件。失败发送结构化错误并关闭，不让客户端持续发送无人消费的音频。会话状态为：

```text
created → admitting → active → draining → closed
   └───────────────→ failed / expired
```

用户关闭时的目标策略是 drain 已接受的段与 TTS；客户端发送 `nexus.session.close` 时必须显式提供 `mode=drain` 或 `mode=abort`，后者取消全部 operation。超过 drain deadline 进入失败清理。session terminal 记录保存 15 分钟供调用方协调，实时 PCM 与音频块不因此保留。

会话 REST 投影必须让生命周期与清理事实同时可判定：`created` / `admitting` 返回非空 `connection_url`、`cleanup_state=not_required`、无终态 operation、`closed_at=null`；`active` / `draining` 仍返回连接地址但 `cleanup_state=pending`。`closed` / `failed` / `expired` 的 `connection_url` 为 null 且 `closed_at` 非空；`expired` 固定 `cleanup_state=not_required` 且没有终态 operation。`failed` 必须带结构化 `error`，其余状态 `error=null`；失败前后资源清理仍分别使用 not_required / pending / complete / quarantined，不由会话状态替代。终态 operation 的 `succeeded` 必须引用 receipt；`cancelled` 仍只允许 not_required / complete。`terminal_operations` 中的 operation_id 必须唯一，且每条引用必须解析为同一 session_id 的 Operation；kind、status、cleanup_state 与成功 receipt id 必须和 Operation 投影完全一致。生产者不能在会话中嵌入其他会话或仅存在于事件中的临时 Operation。会话 schema 只校验引用结构及部分状态组合，无法核验 ID 唯一性和跨资源归属 / 投影一致性；契约测试的辅助检查不证明生产者已实现这些验证。

### 2.2 标准子集与命名空间

ASR 客户端事件仅支持 `session.update`、`input_audio_buffer.append`、`input_audio_buffer.commit`、`input_audio_buffer.clear`。ASR 服务端事件支持 `session.created`、`session.updated`、`input_audio_buffer.committed`、`input_audio_buffer.cleared`、可用时的 `input_audio_buffer.speech_started` / `input_audio_buffer.speech_stopped`、`conversation.item.input_audio_transcription.delta`、`conversation.item.input_audio_transcription.completed`、`conversation.item.input_audio_transcription.failed` 与 `error`。下文 completed / failed 在明确 ASR 语境中是上述完整名称的简称。[官方事件参考](https://developers.openai.com/api/reference/resources/realtime/server-events)

`session.update` 仅接受声明的 transcription 字段：`session.type=transcription` 及 `session.audio.input` 下的 `format`、`transcription`、`turn_detection`；PCM 投影为 `{type: "audio/pcm", rate: 24000}`，与当前 OpenAI Realtime transcription session 形状一致。`session.created` / `session.updated` 回显 `object=realtime.transcription_session` 的同一形状，并把绑定、profile、limits 与 config revision 放在可选 `nexus` 对象。与 `nexus.session.update` 映射到同一个配置命令，禁止两套配置状态。首期 active 会话仅允许 ASR 策略白名单在下一段边界变更，固定模型、音色、格式 / 采样率与 TTS 配方不可热换；标准事件未带 expected revision 时由单连接内串行命令按当前 revision 处理。回显实际 config_revision 与生效段，尚未生效的值不能用于当前段。P0 固化字段 schema，不接受未声明的 `include`、`response.*`、conversation history、semantic VAD 或 tool call 事件。[官方客户端字段](https://developers.openai.com/api/reference/resources/realtime/client-events)

`commit` 表示关闭当前输入段并请求其最终结果；`clear` 只丢弃尚未提交的输入缓冲，不撤销已接受的 operation。重复 commit 用客户端 event_id 去重，无新音频且不是已知重试时返回 `empty_audio_buffer`。有实际音频但无语音时，最终 transcript 可以为空。`no_speech` 是设计上的原因语义，当前标准 completed 事件 schema 未声明 reason 字段；客户端不能依赖该字段，若需对外传递原因须先补齐契约。

后端 vLLM Realtime 的事件形状、commit 顺序与输入格式不必与公开接口相同；adapter 转换而非透传。[vLLM 实时转写接口](https://docs.vllm.ai/en/stable/serving/online_serving/speech_to_text/#realtime-api)

TTS、可替换预览、辅助结果与资源控制使用下表的 SpeechNexus 扩展；不借用 OpenAI LLM response 事件承载语音合成。

### 2.3 事件字典

| 方向 / event.type | 关键字段与语义 |
| --- | --- |
| S→C `nexus.session.ready` | `session_id`、`stream_epoch`、`effective_configuration`、limits、input credit；可开始发送 |
| C→S `nexus.session.update` | `event_id`、`expected_config_revision`、ASR 策略白名单 overrides；在下一段边界生效 |
| S→C `nexus.session.updated` | 新 config revision、effective 值；`effective_from_segment_id` 可省略或为 null，未收到更新事件不能认定已生效 |
| S→C `nexus.input.ack` | `stream_epoch`、`accepted_through_sample`、`credit_samples`；确认进入会话有界缓冲，不代表已识别或保存 |
| S→C `nexus.transcription.segment_closed` | `operation_id`、`segment_id`、`item_id`、wire sample_span、`reason=vad/client_commit/budget_rollover`；`content_index` 在 schema 中可省略 |
| S→C `nexus.transcription.hypothesis` | `segment_id`、`revision`、完整 `text`、`sample_span`；替换该段上一个预览 |
| S→C `nexus.transcription.metadata` | `segment_id`、`text_revision`、`metadata_revision`、alignment / diarization status 与结果 |
| C→S `nexus.tts.start` | `event_id`、`utterance_id`；可重申会话固定的 voice、`text_commit_mode` 和 `audio_delivery`，值必须一致；音频格式不在此事件中指定，成功后返回 operation_id |
| S→C `nexus.tts.started` | `utterance_id`、`operation_id`、实际音频 descriptor、text credit |
| C→S `nexus.tts.append_text` | `event_id`、`utterance_id`、`text_seq`、`text`；序号从 1 开始连续递增，文本须为 1–2000 个 Unicode 码点，消息仍受字节上限约束 |
| S→C `nexus.tts.text_ack` | 已接受 text_seq 与剩余 `text_credit`；不表示音频已生成 |
| C→S `nexus.tts.commit_text` | `utterance_id`、`through_text_seq`；冻结指定前缀并允许其进入合成 |
| C→S `nexus.tts.finish_text` | 提交剩余文本并关闭本 utterance 的输入；不关闭 WS |
| C→S `nexus.tts.cancel` | `event_id`、`utterance_id`；请求取消对应计算与发送 |
| S→C `nexus.tts.audio.delta` | `operation_id`、`utterance_id`、`chunk_seq`、`audio`（Base64 PCM）、`start_sample`、`sample_count` |
| S→C `nexus.operation.done` | `operation_id`、`kind`、`status=succeeded/cancelled/failed`、独立 `cleanup_state`；成功须有非 null receipt，失败须有非 null error 或 reason。成功和取消的清理状态只允许 `not_required` / `complete`；停止未知时使用 `failed` + `quarantined`。唯一逻辑终态 |
| S→C `nexus.session.gap` | 输入缺口或新 epoch 的原因；`sample_span` 可省略或为 null，不得以静音假装没有丢音 |
| C→S `nexus.session.close` | `event_id`、`mode=drain/abort`；停止接收新 operation |
| S→C `nexus.session.closed` | 会话终态、清理状态及 `operations` 引用；每项含 operation_id、kind、status 和可选 receipt_id，不包含各 operation 的 cleanup_state；schema 不保证列表完整 |

公开事件 schema 只将 `text_credit` 定义为非负整数，未规定计量单位；字符额度是目标语义。当前工作台消费者保守地按 UTF-8 字节扣减并一次只允许一个未确认 append，多字节文本的可发送量因此可能低于字符额度。生产者和消费者须在正式实时链验收前明确并核对单位；内部执行文本流另有字符位置契约，不能直接混用。

事件 schema 在 `nexus.session.update.overrides` 中同时声明 `asr` 和 `tts` 的结构；这只验证字段形状，不能证明活动会话允许更改 TTS 配方。活动会话仍受上文的固定配置与 ASR 白名单限制，服务端须另行执行语义校验；公开 Realtime 路由尚未装配，该限制目前是目标要求。

标准 OpenAI ASR server event 只保留 OpenAI 声明的字段（`event_id`、`item_id`、`content_index` 等），不附加 SpeechNexus 私有 envelope；客户端用 `item_id` 关联同一段的 completed / failed，通过 `nexus.operation.done` 取得整体 Operation 身份。所有 `nexus.*` server event 附 `event_id`、连接内递增 `seq`、`session_id`、`stream_epoch` 和时间；目标语义要求与命令有关的事件附 `client_event_id`，与执行有关的事件附 `operation_id`；基础事件 schema 将这两个字段设为可省略或 null；部分事件虽要求 `operation_id` 字段存在，仍允许 null。结构校验因此不能普遍证明关联存在，生产者和消费者须另行核验。nexus 客户端命令必带 event_id；标准 ASR 事件的 event_id 可省略，但省略时不能安全重发 append。带 event_id 的命令在会话生命周期内唯一，重复同内容返回原接受结果，重复不同内容拒绝。术语 `S→C` 和 `C→S` 只描述传输方向。

标准 `conversation.item.input_audio_transcription.completed` 携带 OpenAI 形状的 `usage`：默认使用已接受段的样本时长生成 `{"type":"duration","seconds":...}`；只有绑定提供真实 token 证据时才允许 `{"type":"tokens",...}`。不得用估算 token 数或虚构 logprob 填充。`delta` 仅在具备稳定前缀证据时发送；`nexus.transcription.hypothesis` 才是可替换的完整预览。

ASR 的正文终态与整个 Operation 终态分层：completed / failed 只确定该 segment 的声学正文；`nexus.operation.done` 确定含 required 辅助工作的整体执行。标准事件通过 item_id 关联，nexus 客户端再用 `nexus.operation.done` 的 operation_id 关联整体执行，不能把“正文已定稿”直接当作“所有所需结果已完成”。ASR 取消使用 `nexus.operation.done(cancelled)`；已经发布的正文成功事实保留，不再补发与其矛盾的 transcription.failed。ASR 标准事件必须包含与扩展 segment 对应的 item_id 和 content_index。

同一会话最多有一个活跃 TTS utterance；它可以包含多个冻结文本单元，但各单元必须按文本顺序交付音频。新 start 须等待前一 utterance 的 operation.done；取消后可立即停止浏览器播放，新的计算仍须等待旧租约确认释放。utterance 状态为 `accepting → rendering → draining → completed`，任意未终结状态可进入 `stopping → cancelled/failed`。重复相同 text_seq / 内容去重，不连续序号或同序号不同内容返回冲突。

### 2.4 ASR 修订与辅助结果

同一 segment 的 hypothesis revision 单调递增，事件中的 text 是完整替换值。未得到真实稳定前缀证据时不输出“不可修改”的 delta；OpenAI append-only delta 可以缺席，最终 completed 始终携带完整 transcript。启发式 LocalAgreement、重复预览一致或 token rollback 不构成永久稳定性的证明。

final 不强制保留错误预览前缀。发布 completed 后，该 segment 的声学正文不再修改；会议人工校对或会后重识别创建新的业务 TranscriptDocument revision，并保留来源关系。辅助 alignment / diarization 可以晚于正文终态，但必须携带对应 text_revision；旧正文的辅助结果不能挂到新正文。

required 辅助模型使用父 Operation 的 required steps：先发正文 completed，再等待 required steps 在 auxiliary_deadline_ms 内完成，最后只发布一次 operation.done。required 失败时整体为 failed，正文仍可在失败详情中作为“已完成正文、辅助失败”的结果保留；不能改写此前 completed。best_effort 辅助步骤使用独立子执行身份、资源租约和终态，父 Operation 可先成功，后续 metadata 只更新辅助状态，不反转父终态。会话 drain 必须在有限期限内等待或取消仍存活的子执行。

识别段因预算 rollover 关闭时，服务明确报告原因。应用将多个 segment 归并为业务轮次，不能见到一个 completed 就自动让助手回答。匿名说话人 label 仅在当前会话有效，未知和重叠显式表示，不推断实名或跨会话身份。

### 2.5 TTS 文本与音频的两种流

`text_commit_mode=explicit` 时，服务只合成客户端通过 commit_text / finish_text 冻结的文本；已接受但未冻结的尾部仍不进入模型。`sentence` 模式在规则确认安全句界后冻结，使用有版本的文本规划器；数字、缩写与未完成片段不能因网络 chunk 边界被强行切开。首版不提供按每个 token 独立合成的模式。

`audio_delivery=chunked` 表示生成期间持续交付音频，与 text_commit_mode 独立。下游引擎如按每个文本段独立合成，则能力声明 `acoustic_continuity=segment_independent`；不得声称模型声学状态跨段连续。默认助手按稳定句子提交，有声书使用较长、经过验证的叙述单元。[vLLM-Omni 流式接口](https://docs.vllm.ai/projects/vllm-omni/en/stable/serving/speech_api/#streaming-text-input-websocket)

音频 chunk_seq 连续，start_sample / sample_count 描述该 utterance 输出时间轴。目标语义要求全部音频生成并交给传输层后，才发送该操作的 `nexus.operation.done` 成功终态；事件 schema 不单独声明 TTS `complete` 事件，也不能验证音频是否已完整交付。成功终态不能证明浏览器收到全部字节或播放结束；业务 UI 分别显示生成、缓冲与播放状态。

### 2.6 格式、背压、断线与取消

实时 wire 基线为单声道 PCM16 LE、24 kHz；adapter 在受控边界重采样到模型输入规格。TTS 会话握手返回实际输出 descriptor，客户端按 descriptor 播放。格式和采样率在会话内不可变化。时间范围使用 wire sample clock 的非空半开区间 `[start,end)`，不能混入服务端墙钟或模型 16 kHz 下标。事件 `SampleSpan` schema 分别约束起止值，却不能比较二者；`end > start` 由契约测试另行检查，生产者与消费者仍须验证该关系。

初始限制：输入 chunk 建议 20–100 ms、上限 250 ms；单 JSON 消息上限 64 KiB；未消费实时输入 credit 上限 2 秒；客户端网络缓冲上限 5 秒；单个 ASR 段上限由预设与绑定共同约束且不超过 30 秒。Base64 和 JSON 开销计入网络预算。共享 `RealtimeLimits` schema 限定 chunk、消息和段长范围，但未给输入 / 输出 credit 或会话时长设置上述上界；生产者和消费者须另行核验生效配置。辅助模型状态必须有独立的有界内存声明，不能借会议长期连接累计整场 PCM。

超过 credit 继续发送时，先报告背压，持续违规关闭连接；不静默丢 PCM。允许合并过时的预览计算请求，但必须保留尚未处理的已接受 PCM。慢速接收 TTS 的客户端触发有界音频积压，超过协商上限取消 operation；不可无限生成并堆积。

WS 断开即停止接收新操作，取消实时计算并回收 owner，记录明确的断开终态。首版不提供透明音频重放和无缝恢复：重连创建新 session / epoch，应用保留已确认文字，显示输入缺口；不得自动重播旧 TTS。客户端心跳建议 10 秒，30 秒失联进入断开清理；会话最长 8 小时，续接必须建立新 epoch。

取消请求得到接受不等于算力释放。归还预算前须取得适用的执行停止与资源释放证据：adapter 确认执行结束、abort 完成或受控进程退出本身不能替代资源释放事实；具体证据和释放事务见[协调事务](coordination-transactions.md#6-owner提交取消与释放时序)。取消后禁止向客户端继续发布该 operation 的音频或结果；迟到 vendor 输出只作清理，不写业务状态。每个已接受 operation 在服务端恰有一个逻辑终态，网络事件只承诺可去重交付；连接中断时通过 GET 协调，不能承诺客户端恰收一次。

## 3. 调用示例

以下 ID、版本和文本均为说明性示例；运行前须替换为目标部署中实际存在且具备资格的资源。示例展示契约形状，不证明路由或执行链已装配：同步 ASR/TTS 的前置条件分别见[同步转写](../operations/synchronous-transcription.md)和[同步语音合成](../components/synchronous-speech.md)，当前公开 Job 路由仅装配创建和读取，实时及音色创作流程仍须按实际交付与部署证据判断。

### 3.1 同步 TTS 与文件转写

标准兼容请求无需 nexus、workspace 或工作台项目；下例中的 voice 由部署发布的默认 revision 解析。执行事实按私有原身份保留；当前同步响应不提供供客户端查询的 receipt。

```json
{
  "model": "tts-interactive",
  "input": "欢迎使用 SpeechNexus。",
  "voice": "narrator",
  "response_format": "wav"
}
```

下面是同一 `POST /v1/audio/speech` 的增强 JSON body。所有 ID 与文字都是说明性占位值，不表示已有对应资源或已运行该请求。HTTP 使用当前部署入口；首期不要求用户登录。共享服务密钥为契约选项，当前公开语音处理器不验证它；部署若另行提供入口保护，须按实际配置核验，不能把密钥放入示例页面。

```json
{
  "model": "tts-interactive",
  "input": "欢迎使用 SpeechNexus。",
  "voice": "voice_demo",
  "response_format": "pcm",
  "nexus": {
    "workspace_id": "ws_demo",
    "client_reference": "project:prj_demo",
    "profile_id": "speech-interactive",
    "profile_revision": "1",
    "voice_revision": "3",
    "overrides": {"tts": {"audio_delivery": "complete"}},
    "deadline_ms": 10000
  }
}
```

REST 的完整 input 在请求接受时一次性冻结，`audio_delivery` 只能为 `complete`。此示例请求 PCM；成功返回二进制音频，并以 Content-Type、`X-Audio-Format`、`X-Sample-Rate-Hz` 和 `X-Channels` 描述实际格式。当前同步 HTTP 入口不承诺返回 `X-Operation-ID` 或供客户端查询的 operation receipt；结果未知时不能仅凭传输结束判断执行完整或安全重发，须按[同步语音合成](../components/synchronous-speech.md)协调原身份。持续分块与可恢复完整音频分别属于实时接口和 Job 的目标范围，需按实际装配与验收判断。

同步 ASR 使用 multipart 的 `file`、`model=asr-quality`、`language=zh`、`response_format=json`；`nexus` 部分按 [同步 REST 子集](speech-api.md#13-同步-rest-子集) 传入相同工作空间与预设信息。file 是上传内容，不是服务器文件路径。成功 JSON 至少包含 text；持久 Job 的目标是通过转写制品提供更完整的结果，但公开契约目前只固定制品引用，正文结构与辅助结果仍须冻结并交付，不随意改变兼容子集响应形状。

### 3.2 创建实时 ASR 会话

```json
{
  "workspace_id": "ws_demo",
  "client_reference": "project:prj_demo",
  "profile": {"id": "caption-live", "revision": "1"},
  "modalities": ["asr"],
  "models": {"asr": {"id": "asr-quality", "revision": "1"}},
  "audio": {
    "input": {"encoding": "pcm_s16le", "sample_rate_hz": 24000, "channels": 1}
  },
  "overrides": {"asr": {"language": "zh"}},
  "constraints": {"max_admission_wait_ms": 500, "allow_degradation": false}
}
```

该 body 发往 `POST /v1/nexus/realtime/sessions`。ASR-only 时 models 只能包含 asr；TTS-only 只包含 tts；双工同时包含两者。包含 TTS 时，目标语义要求固定 `voice={id,revision}`，并固定 tts 策略和输出 descriptor；创建请求 schema 允许省略 `voice`，但未提供显式选择默认声音的字段，也不保证服务能解析出默认声音，实际解析与资格须由服务验证。start 中重复的值必须与会话一致，换声创建新会话。双工原子预留两者的容量承诺。创建响应以 `id` 返回会话标识，并包含 `expires_at`、`connection_url` 与 `effective_configuration`；按 [实时会话建立](speech-api.md#21-创建入口检查与连接) 连接且收到 ready 后才发送 PCM。

输入 PCM 按固定格式通过 input_audio_buffer.append 发送，sample clock 从该 epoch 的 0 开始连续累计。官方子集事件未提供 sample offset 时由服务按已接受的字节数推导；服务拒绝错格式和不完整样本，不靠客户端墙钟推断缺失数据。支持自动重试的 SpeechNexus SDK 必须发送稳定 event_id；不带 event_id 的兼容调用允许执行，但不支持重复 append 的安全重发。

### 3.3 实时 TTS：逐步提交文本

先用以下 body 创建 TTS-only 会话，收到 ready 后再发送 start / append / commit / finish。字段定义以 [实时事件字典](speech-api.md#23-事件字典) 为准。

```json
{
  "workspace_id": "ws_demo",
  "profile": {"id": "speech-interactive", "revision": "1"},
  "modalities": ["tts"],
  "models": {"tts": {"id": "tts-interactive", "revision": "1"}},
  "voice": {"id": "voice_demo", "revision": "3"},
  "audio": {
    "output": {"encoding": "pcm_s16le", "sample_rate_hz": 24000, "channels": 1}
  },
  "overrides": {"tts": {"text_commit_mode": "explicit", "audio_delivery": "chunked"}},
  "constraints": {"max_admission_wait_ms": 500, "allow_degradation": false}
}
```

```json
{
  "type": "nexus.tts.start",
  "event_id": "evt_start_1",
  "utterance_id": "utt_demo",
  "voice": {"id": "voice_demo", "revision": "3"},
  "text_commit_mode": "explicit",
  "audio_delivery": "chunked"
}
```

```json
{
  "type": "nexus.tts.append_text",
  "event_id": "evt_text_1",
  "utterance_id": "utt_demo",
  "text_seq": 1,
  "text": "欢迎使用 SpeechNexus。"
}
```

```json
{
  "type": "nexus.tts.commit_text",
  "event_id": "evt_commit_1",
  "utterance_id": "utt_demo",
  "through_text_seq": 1
}
```

```json
{
  "type": "nexus.tts.finish_text",
  "event_id": "evt_finish_1",
  "utterance_id": "utt_demo"
}
```

finish_text 不重新合成已经 commit 的文本，只关闭输入并提交未冻结尾部。append 需要 text credit，冻结单元还需获得输出预算；超额会明确失败而非无限排队。audio.delta 对应同一 operation_id，operation.done 后方可发起下一个 utterance。

### 3.4 非实时 TTS 作业

```json
{
  "kind": "tts",
  "workspace_id": "ws_demo",
  "client_reference": "project:prj_demo",
  "input": {"text_asset_id": "ast_chapter_text"},
  "profile": {"id": "audiobook-consistent", "revision": "1"},
  "model": "tts-quality",
  "voice": {"id": "voice_demo", "revision": "3"},
  "pronunciation": {"id": "pron_demo", "revision": "2"},
  "output": {"audio_format": "wav", "save_artifacts": true},
  "constraints": {"max_queue_wait_ms": 600000, "allow_degradation": false}
}
```

该请求是一个有界文本单元的配音 Job；全书编排由工作台应用维护多个 render_unit 并提交底座 Job；底座拥有 JobStep 和公平调度，不能把整个书稿作为一个不可取消的模型执行单元。`save_artifacts=false` 与 durable Job 不相容，应拒绝，不静默忽略。


### 3.5 修改音色后重新生成

向 `POST /v1/nexus/voice-drafts/{draft_id}/generations` 发送以下 body 与新的 Idempotency-Key。服务比较当前 head=1，原子建立 revision=2 和生成 Job。Job 成功后针对 revision=2 试听 / 校验，再用该校验发布；不能复用 revision=1 的校验。

```json
{
  "expected_draft_revision": "1",
  "model": "voice-design",
  "input": {
    "description": "清晰、沉稳、语速舒缓的中文叙述声音",
    "preview_text": "欢迎来到今天的故事。",
    "language": "zh"
  }
}
```

生成失败时 revision=2 保留失败事实；修正后重新生成建立 revision=3。同一 Job 的受限自动重试仍执行 revision=2 的固定配方，全部 attempt 结束后才形成该 revision 的最终生成结果。对失败 voice_design / voice_clone 调用显式 `/jobs/{job_id}/retries`，以新 key 原子创建新 Job 与下一 draft_revision，复制失败任务的固定声音配方；仅当当前 head 仍等于该失败 revision 且无活跃生成时接受，否则返回 configuration_changed。旧候选、旧校验和已发布 voice 版本不被覆盖。


主动删除、依赖预检、明确级联与恢复后的删除账本重放使用[数据删除协议](data-deletion.md)，
不可用 retirement、TTL、archive 或取消完成代替内容删除。
