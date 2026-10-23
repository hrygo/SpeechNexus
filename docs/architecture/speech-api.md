---
title: "SpeechNexus 语音接口设计"
status: active
version: "0.5.4"
date: 2026-10-23
---

# SpeechNexus 语音接口设计

本文负责实时与非实时 ASR/TTS、音色创作和资源访问的协议语义，属于[项目主设计](../speechnexus-product-architecture.md)的配套规范。P0 已将本文语义固化为 `contracts/` 下的 OpenAPI、JSON Schema 与正负样例；业务路由仍待实现。兼容声明只覆盖明确列出的字段、格式和事件，不推断完整 OpenAI API / SDK 兼容。

阅读入口：[REST / Job / 音色](#1-统一接口与传输契约) · [实时状态与事件](#2-实时协议与状态机) · [调用示例](#3-调用示例)

## 1. 统一接口与传输契约

### 1.1 统一的含义

四种能力共享 `SpeechCommand → Admission → Operation → Result / Receipt` 语义，分别通过适合的传输交付。统一不要求把文件上传、持久作业和音频流塞进同一个 HTTP 请求。

| 协议层 | 首期范围 | 调用方依赖 |
| --- | --- | --- |
| OpenAI 兼容层 | Audio 转写与合成的声明子集；标准请求不要求 nexus 或工作台字段 | 标准 SDK 更换 base_url、逻辑 model / voice；共享服务密钥按部署选择 |
| SpeechNexus 增强层 | 音色创作、版本 / 预设、durable Job、能力 / 资源、管理；实时 nexus 扩展 | 公开增强 DTO / SDK；不依赖工作台 |
| 工作台应用层 | 项目、会议 / 助手 / 提词 / 有声书编排 | 可选 /v1/workbench；不宣称 OpenAI 兼容 |

Realtime 提供两条连接路径：预创建 SpeechNexus 会话后使用 `session_id`，或标准 SDK 直接以 `model` 打开仅转写的兼容会话。两者都只兼容本文声明的 ASR 事件和配置子集；不承诺依赖 `response.*`、conversation history 或 tool call 的 OpenAI Realtime 客户端无修改可用。音色设计 / 克隆不冒充标准 speech 参数。完整 SDK 调用与增强边界见 [OpenAI 兼容与增强边界](openai-compatibility.md)。

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

`context` 不是客户端可自由声明的安全上下文；服务端从部署配置和存在的工作空间构建它。workspace_id 只选择内容 / 预算分组，client_reference 是业务关联字符串，都不构成身份或访问权。无 workspace_id 时使用部署默认值；未知分组明确拒绝。所有字段为 `snake_case`，未知字段、布尔冒充数字、单位错误与未知枚举均拒绝。

### 1.2 入口清单

| 接口 | 用途与返回 |
| --- | --- |
| `GET /v1/models` | 部署已发布的逻辑模型列表；不暴露私有模型路径 |
| `GET /v1/nexus/capabilities` | 按部署策略、工作空间和已发布绑定生成一致能力快照 |
| `GET /v1/nexus/profiles`、`GET /v1/nexus/profiles/{profile_id}/revisions/{revision}` | 列出与读取可用的不可变预设 |
| `POST /v1/audio/transcriptions` | multipart 文件 ASR；成功返回 JSON / text 等声明格式 |
| `POST /v1/audio/speech` | JSON 文本 TTS；返回完整音频或声明的音频流 |
| `POST /v1/nexus/jobs` | 创建持久化 ASR/TTS 作业；返回 `202`、Job 和 Location |
| `GET /v1/nexus/jobs`、`GET /v1/nexus/jobs/{job_id}` | 游标分页与作业状态；包含执行版本与结果引用 |
| `POST /v1/nexus/jobs/{job_id}/cancellations` | 幂等请求取消；进行中为 `202`，已终结为 `200` |
| `POST /v1/nexus/jobs/{job_id}/retries` | 创建关联原任务的新 Job；不复活旧任务或覆盖旧成品 |
| `GET /v1/nexus/jobs/{job_id}/events` | SSE 持久任务事件，支持事件游标续读 |
| `GET /v1/nexus/operations/{operation_id}` | 协调执行状态、cleanup_state 与 receipt；不重放未保存内容 |
| `POST /v1/nexus/operations/{operation_id}/cancellations` | 取消指定实时 / 同步操作；Job 内操作转为所属 Job 的取消命令，避免留下无结果的 required 步骤；进行中 `202`，已终结 `200` |
| `POST /v1/nexus/assets`、`GET /v1/nexus/assets/{asset_id}` | 有界流式上传文件与读取元数据；上传结束校验后才为 `ready` |
| `GET /v1/nexus/assets/{asset_id}/content` | 按部署功能策略下载 ready 输入，支持 Range；不暴露对象存储 key 或服务器路径 |
| `GET /v1/nexus/artifacts/{artifact_id}`、`GET .../{artifact_id}/content` | 读取已提交产物的元数据 / 内容；内容支持 Range，过期或已删除返回 `410 artifact_unavailable`，staging 不可见 |
| `POST /v1/nexus/realtime/sessions` | 验证并固定会话配置，返回 `201`、session_id、连接地址和过期时间 |
| `GET /v1/nexus/realtime/sessions/{session_id}` | 查询会话、operation 终态与输入确认位置；首期没有人级 owner 授权 |
| `POST /v1/nexus/realtime/sessions/{session_id}/cancellations` | 从 HTTP 请求关闭会话，便于 WS 不可达时停止执行 |
| `WS /v1/realtime?session_id={session_id}` | 预创建会话的实时 ASR 子集与 `nexus.*` 扩展；session_id 不是访问凭据 |
| `WS /v1/realtime?model={model}` | OpenAI SDK 兼容的 transcription-only 模式；服务端按部署默认值建立会话，不启用 TTS / `nexus.*` 能力 |
| `GET /v1/nexus/voices`、`GET /v1/nexus/voices/{voice_id}/revisions/{revision}` | 部署共享音色目录与版本详情，含兼容绑定与校验事实 |

增强资源包括 `/v1/nexus/workspaces`、`/profile-drafts`、`/voice-drafts`、`/pronunciation-sets`、`/voice-designs`、`/voice-clones` 和 `/audit-events`。管理资源使用 `/v1/nexus/admin/model-bindings`、`/resource-pools`、`/policies`、`/diagnostics` 等明确子资源；隔离租约的运维释放使用 `/v1/nexus/admin/admission-leases/{lease_id}/quarantine-releases`，只接受独立核验证据，不能仅靠重启或删除记录清空隔离预算。首期不提供 users / memberships / roles API；管理路径本身不形成权限隔离。预设和音色通过 `publications` 子资源生成不可变 revision，普通 PATCH 只更新草稿。

工作台项目、会议、有声书及独立导出任务 / 成品资源置于 `/v1/workbench/`；外部系统直接使用 `/v1/` 与 `/v1/nexus/`，不需要工作台项目或 LLM。P0 为各命名空间分别补齐 OpenAPI，禁止万能管理 RPC。

### 1.3 同步 REST 子集

文件转写支持 `file`、`model`、`language`、`prompt`、`response_format` 及快照声明的 `timestamp_granularities[]`。`response_format` 默认 `json`，基线为 `json`、`text`；`verbose_json`、`srt`、`vtt` 仅在具备相应时间边界证据时开放。`timestamp_granularities[]` 只接受已声明支持的 `segment` / `word`，且必须与 `response_format=verbose_json` 同用；不满足时返回 422，不伪造词级时间戳。标准 language 省略表示自动检测，内部规范化为 `asr.language=auto`，不要求标准客户端发送字面值 auto。`prompt` 是有界术语提示；不具备提示能力的绑定明确拒绝。增强结果返回实际语言或 unknown，兼容 JSON 保持所声明的字段形状。

非 verbose `Transcription.usage` 可按绑定真实证据使用 duration 或 token；`verbose_json.usage` 固定为 duration，以匹配 OpenAI SDK 的响应模型。不得为了展示 token 数而破坏 `verbose_json` 的可解析性。

TTS 支持 `model`、`input`、`voice`、`response_format`、`speed`、`instructions` 与 `stream_format=audio`，具体可选参数及取值域以绑定和音色快照为准。首期标准 voice 接受已发布的字符串 ID；不宣称支持 OpenAI 托管音色或任意 voice 对象。`response_format` 默认 `mp3`，基线为 `mp3 / wav / pcm`；MP3 由有界编码步骤提供并计入 CPU、缓冲与执行预算。REST `pcm` 固定为无头、24 kHz、单声道 signed 16-bit little-endian，adapter 负责必要重采样。默认 `speed=1`；非默认 speed 和 instructions 只有绑定实际支持时才接受。其他格式及 `stream_format=sse` 不在首期兼容子集，显式请求返回 `unsupported_parameter`。禁止静默忽略参数或将默认 MP3 替换为 WAV。[OpenAI TTS 格式与默认值](https://developers.openai.com/api/docs/guides/text-to-speech)

增强参数放在 TTS JSON 的 `nexus` 对象或 ASR multipart 的 `nexus` JSON 字符串中。两者共同接受 `workspace_id`、`client_reference`、`profile_id`、`profile_revision`、`overrides` 和 `deadline_ms`；TTS 另接受 `voice_revision`、`pronunciation_revision`，ASR 的 `overrides` 只能包含 `asr` 策略，不借用 TTS 字段。同步 REST 的标准 `speed`、`instructions` 与 `nexus.pronunciation_revision` 是唯一字段来源，`nexus.overrides.tts` 不重复定义它们。完全省略 nexus 时，使用部署发布的默认工作空间、预设和音色版本解析规则，保证标准客户端能直接调用；nexus 为可选扩展，不能成为兼容路径的必填包装。

普通同步 ASR/TTS 不建立永久业务记录，也不自动保存源文件或音频。服务端先检查输入大小、最大音频时长 / 文本长度和同步执行预算；超出预算返回 `422 async_required`，由客户端明确改为 Job，不在超时后偷偷转成后台作业。已经开始同步执行但超时的请求不保证得到结果，客户端使用相同幂等键协调结果，不能盲目新建相同意图。

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

```json
{
  "id": "job_demo",
  "object": "nexus.job",
  "kind": "asr",
  "status": "queued",
  "cleanup_state": "not_required",
  "request_id": "req_demo",
  "profile": {"id": "file-accurate", "revision": "1"},
  "execution_revision": "exec_demo",
  "progress": {"unit": "audio_ms", "completed": 0, "total": 120000},
  "artifacts": [],
  "error": null
}
```

`status` 取值为 `queued | running | retry_wait | cancelling | succeeded | failed | cancelled | expired`；cleanup_state 单独报告 not_required / pending / complete / quarantined，未清理的 Operation 以引用返回。取消确认超时为 failed / cancellation_unconfirmed，资源继续隔离，旧执行清理前 retry 返回 execution_cleanup_pending。结果成功后，`artifacts` 包含受控下载引用和 `receipt_id`。输入过期或任务超过执行前队列期限，转为 `expired`；成功结果达到保留期限时仍保留成功终态，只将 artifact 标为过期。

ASR 结果使用统一 `TranscriptDocument`：`id`、`revision`、`language`、`text`、`sample_rate_hz`、`coordinate_space`、`segments`、`auxiliary_status`。segment 包含 `segment_id`、`sample_span`、`text`、`text_revision`，以及可选 `words`、`speaker_assignments`。统一坐标为 `canonical_audio`、24 kHz，原文件解码 / 重采样映射的 revision 进入 receipt；不能把原文件 48 kHz 下标直接填入 sample_span。`auxiliary_status` 分别声明 alignment / diarization 的 `not_requested | pending | complete | failed | unavailable`。不把 segment 平均时长伪造成 word timestamps；未知说话人为 null / unknown。

TTS 制品携带实际 `format`、`sample_rate_hz`、`channels`、`duration_ms`、摘要与 `ExecutionReceipt`。Receipt 区分 requested / effective / observed 参数；只有实际观察到的事实进入 observed，固定 seed 也不承诺跨引擎 bitwise 一致。

### 1.5 能力协商与版本

能力快照包含 `snapshot_id`、`catalog_revision`、`policy_revision`、生成时间、逻辑模型绑定、参数域、音色 revision、预设 revision、格式、辅助能力及限制。readiness 分别报告配置完整、引擎就绪、质量资格、当前可准入状态；`available` 不等于业务生产质量已通过。

绑定发布、部署资格与具体实例的运行事实分开。按需模型可以支持提交排队任务而当前尚未加载；不能用单一就绪标志混淆“支持”“可排队”“可立即执行”。实例卸载不删除绑定或音色 revision，重建后重新核对配置与资格；字段和状态由 P0 schema 冻结，组件分工见[整体技术方案](technical-blueprint.md#4-绑定资格与实例分离)。

模型绑定发布同时负责把一个 draft revision 激活为当前 revision。请求中的 `expected_revision` 是目标 revision，`expected_current_revision` 是发布前的当前指针；两者分离，避免并发发布时“谁最后提交谁生效”的丢失更新。指针已变化返回 `409 configuration_changed`，目标不是 draft 返回 `409 binding_not_draft`；首期没有身份或审批层级，CAS 只保证操作者看到的是自己确认的版本。

快照按部署、工作空间、catalog 与功能策略生成；ETag 与缓存键包含这些 revision，不混用不同部署配置。配置了外部网关或共享服务密钥时，入口检查先于 304。读取不触发加载、下载或卸载，不承诺未来槽位。提交可提供 expected_catalog_revision，不匹配为 `409 configuration_changed`；执行前重新检查资源状态、功能策略和容量。首期没有按用户投影或 authorization_revision。

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
| 503 / 504 | `backend_unavailable`、`capacity_unavailable` / `operation_timeout` | 先协调原意图；只有声明 retryable 才重试 |

列表统一 `data`、`has_more`、`next_cursor`，按 `(created_at,id)` 排序；page limit 为 1–100，默认 50。游标绑定过滤条件、部署与工作空间，不能跨请求作用域续读；这属于查询一致性，不是权限边界。SSE `id` 是持久化 job_event_seq，支持 `Last-Event-ID`；事件保留期结束后返回 `410 event_history_expired`，客户端读取 Job 当前状态，不伪造历史。

`JobEvent.data` 不是任意 JSON：每个 `job.*` / `artifact.published` 类型都有独立的严格 data schema，状态、错误、进度、产物和终态 `cleanup_state` 必须与该事件类型匹配。`job.failed` 即使进入 `cleanup_state=quarantined` 也保持失败结果与结构化错误；`job.cancelled` 只允许 `not_required` / `complete`。未知嵌套字段和不匹配的 `type` / `status` 组合按契约错误拒绝，不能借事件扩展绕过稳定字段。工作台的 MinutesJob、AssistantTurn、ExportJob 与 ExportEvent 使用同一原则：失败错误为结构化 `code / message / retryable / param`，非 failed 状态不得携带非空错误对象。

创建 Job、创建会话、同步推理、音色创作和发布资源支持 `Idempotency-Key`。作用域为 `(deployment_id, workspace_id, operation_family, key)`，服务端原子记录规范意图摘要、脱敏的显式配置引用、固定执行版本与执行身份；未明确保存的正文只留摘要，不把同步输入全文放入幂等表。摘要基于规范命令、精确输入内容 / 摘要及显式版本，不混入传输路径或后来变化的默认值；便捷入口与 Job 入口先映射为同一命令。确定部署 / 工作空间并执行当前入口检查后，先查找或原子 claim 幂等记录；只有新意图才解析可变默认预设 / 模型 / 音色并固化，重复请求复用原固定版本。当前删除 / 强制禁用仍按资源规则限制内容返回，不因此重新执行。相同 key / 同意图返回原执行，相同 key / 不同意图返回 `409 idempotency_conflict`。进行中 Job 返回 `202` 与原 Job；进行中同步请求返回 `409 operation_in_progress` 与 operation 查询引用。

同步推理的终态协调接口为 `GET /v1/nexus/operations/{operation_id}`。默认仅持久化意图、状态与脱敏 receipt，不保存完整 JSON 转写或音频响应；相同 key 对应操作已完成但内容不可重放时，返回 `409 response_not_replayable` 及成功 receipt，绝不再次执行。需要可靠获取或重放完整结果的调用方使用 Job。流式响应中断同样不自动重播先前音频。

作业内重试窗口默认最多 24 小时，意图与步骤去重记录在所有 attempt 终结后至少保存 7 天；任务仍未终结或取消状态未知时不得删除该记录。调用方主动 retry 创建新 Job 与新 key，旧 Job 保持原终态。无用户体系时，同一工作空间共享幂等作用域；客户端为每个新意图生成高熵唯一 key，不以昵称或浏览器标识建立虚假的个人命名空间。P0 的故障用例必须覆盖“远端已执行、客户端超时”和“产物写入后进程退出”。

### 1.7 音色创作与增强协议

标准 `/v1/audio/speech` 使用已发布音色或预置 speaker；instructions 仅控制当前绑定支持的表达方式，不隐式创建、克隆或发布音色。音色创作有自己的 `VoiceCommand`，复用 Admission、Operation、Job、Artifact、ExecutionReceipt 与幂等规则，不要求消费方使用工作台。

| 增强入口 | 规范命令 / 结果 |
| --- | --- |
| `POST /v1/nexus/voice-designs` | `voice_design`；描述、预览文本、语言与逻辑模型 → `202` Job → 候选 VoiceDraft、试听 Artifact 与 receipt |
| `POST /v1/nexus/voice-clones` | `voice_clone`；ready 参考音频、可选参考正文、使用确认与逻辑模型 → `202` Job → 候选 VoiceDraft、试听与来源记录 |
| `GET /v1/nexus/voice-drafts/{draft_id}`、`PATCH .../{draft_id}` | 读取固定 revision 或当前 head；PATCH 只接受 display_name 等元数据及 expected_metadata_revision，递增 metadata_revision |
| `POST /v1/nexus/voice-drafts/{draft_id}/generations` | expected_draft_revision + 完整声音输入 / model → 原子创建下一 draft_revision 与 voice_design / voice_clone Job；返回 `202`，生成完成后才可试听 / 校验 |
| `POST /v1/nexus/voice-drafts/{draft_id}/previews` | 对固定候选 revision 进行短文本 TTS，返回 `202` Job；不发布或修改候选 |
| `POST /v1/nexus/voice-drafts/{draft_id}/validations` | `voice_validation`；固定候选与绑定，返回 `202` Job 和格式 / 素材 / 兼容性 / 质量检查结果 |
| `POST /v1/nexus/voice-drafts/{draft_id}/publications` | 固定候选和校验 revision，原子生成可用于 TTS 的不可变 VoiceRevision；首次成功 `201`，幂等重放返回同一发布 |

Job.kind 为 asr / tts / voice_design / voice_clone / voice_preview / voice_validation。音色使用独立 VoiceCommand，不套用 ASR/TTS profile；从模型绑定、音色功能策略和参数域生成执行配方。便捷入口与 `POST /v1/nexus/jobs` 使用相同规范意图、operation_family 和请求摘要去重，换路径不能重复创建。voice_preview 是独立任务类型，内部复用 TTS 计算与计量；兼容 speech 入口只接受已发布 voice。

Job 入口的音色 body 使用 kind、可选 workspace_id / client_reference、model、input 和 constraints；design 的 input 对应 description / preview_text / language / display_name，clone 对应 reference_audio_asset_id / reference_text / preview_text / language / display_name / source_use_confirmed。初次创建在同一事务写入 VoiceDraft、draft_revision=1、Job 与 outbox；创建响应中的 effective_configuration 包含已分配的草稿 / revision，结果尚未就绪。重新生成使用可选顶层 `draft_target={id,expected_revision}`，指定现有草稿；generations 入口把 path ID 与 expected_draft_revision 规范化为该字段，body 的 model 与完整 input 作为新声音配方，省略旧字段不会隐式继承。类型保持 design 或 clone，切换类型创建新草稿。版本比较、分配下一 revision 与 Job / outbox 同事务提交；同幂等键先重放原意图，不因 head 已前进而冲突。已有生成未终结时以新 key 再生成返回 `409 operation_in_progress`。

preview / validation 的 input 固定 voice_draft_id / draft_revision；preview 必填 text，validation 使用能力矩阵中已冻结的测试集 revision，model 默认沿用候选绑定。便捷入口将 path 中的 draft_id 与 body 中的 expected_draft_revision 规范化为相同引用；preview / validation body 另带 text 或 validation_set_revision，不能隐式使用“当前最新”候选。草稿 GET 支持 draft_revision 查询；历史配方、候选和校验可追溯，不覆盖旧 revision。

成功创作任务在 result 返回 voice_draft_id / draft_revision / preview_artifact_ids；preview 返回试听 Artifact，validation 返回 validation_id / validation_set_revision / checks / publication_eligible 与绑定 / 候选 revision。每个 check 显式标记 `required`；只有所有 required check 均为 `passed`，且候选具备已验证的可复用 TTS 路径时，`publication_eligible` 才能为 true，optional check 的 skipped 不冒充整体验证通过。候选、校验引用、Artifact 与 Job 成功状态按 [作业与产物发布](runtime-governance.md#22-作业与产物发布) 的同一提交规则写入，不让半发布结果可见；未发布候选不进入生产音色目录。

描述设计示例：

```json
{
  "workspace_id": "ws_demo",
  "model": "voice-design",
  "description": "温暖、清晰、节奏舒缓的中文叙述声音",
  "preview_text": "欢迎来到今天的故事。",
  "language": "zh",
  "display_name": "叙述候选"
}
```

样本克隆示例：

```json
{
  "workspace_id": "ws_demo",
  "model": "voice-clone",
  "reference_audio_asset_id": "ast_voice_reference",
  "reference_text": "这是一段用于创建声音的示例素材。",
  "preview_text": "这是新的试听文本。",
  "language": "zh",
  "display_name": "参考声音候选",
  "source_use_confirmed": true
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

每个 draft_revision 的状态为 `draft → generating → candidate → validating → validated → published`，失败保留原因；生成配方与候选字节不可变。PATCH 修改声音描述、样本、参考正文、预览文本或绑定返回 `422 unsupported_parameter`，这些修改必须调用 generations 创建新 revision。新 head 在重新生成 / 校验前不能发布，旧校验不能满足新 revision；旧候选、旧校验和已经发布的 VoiceRevision 保留历史事实。新 head 与旧 validation 同时完成时，只更新各自 revision，不让迟到结果覆盖 head。试听与机器校验不自动构成发布确认。publication 固定当前 head 的 expected_draft_revision、对应 validation_job_id、素材 / 绑定和未过期资格；head 已变化返回 `409 configuration_changed`。每个草稿 revision 最多一次发布，初次建立 voice_id，后续发布追加同一 voice_id 的不可变 revision；重复发布返回原结果或明确冲突，不重复建立目录项。首期 manual_confirmation 是显式内容确认，不能证明审核者与创作者是两个人。

VoiceRevision 包含来源类型、后端绑定、素材摘要与依赖、样本 / 语言能力、验证结果及私有 backend_voice_handle；公开结果不返回特征向量、任意文件路径或引擎内部句柄。调用 TTS 时仍检查音色与模型绑定兼容性，不把任意后端音色资产宣称为跨模型通用。

描述式生成可能只产生试听音频。只有已验证“候选 → 可复用参考 / 特征 → 后续 TTS”的完整路径，才能发布可持久使用的声音；否则标为 preview_only，不提供 publication。Qwen3-TTS 的 VoiceDesign、Base 和 CustomVoice 具有不同角色，由各自正确的模型绑定完成适配，不依赖同一实例自动支持全部类型。[vLLM-Omni 音色与模型角色](https://docs.vllm.ai/projects/vllm-omni/en/stable/serving/speech_api/#qwen3-tts)

平台维护音色注册表与版本，原生引擎的音色上传 / 删除只允许 adapter 内部调用，不向 WebUI 透传。底座 API 与引擎注册表之间的控制边界独立于首期是否有用户认证。[上游音色存储边界](https://docs.vllm.ai/projects/vllm-omni/en/stable/serving/speech_api/#voices-endpoint)

## 2. 实时协议与状态机

### 2.1 创建、入口检查与连接

创建会话时提交可选 workspace_id / client_reference、profile、modalities（asr、tts 或两者）、逻辑模型和输出需求。REST 校验部署入口、参数、资源状态、版本与能力，固定配置并建立 created 会话，30 秒内未连接即过期；此时不占推理槽。

标准 OpenAI Realtime SDK 可省略 REST 会话创建，直接连接 `/v1/realtime?model={model}`。该兼容模式只建立 transcription-only 会话，使用部署默认工作空间、ASR 预设和音频格式；连接握手时完成准入并返回标准 `session.created`。它不接受 TTS、nexus 假设 / 辅助事件或双工能力。需要固定 profile、workspace、音色或 TTS 时，调用方必须先创建 nexus 会话。`session_id` 与 `model` 互斥，同时提供返回 400，两者都不进入 URL 作为凭据。

首期浏览器无登录 cookie、access token 或用户会话，使用同源入口并校验 WebSocket Origin / Host；SDK 直接连接部署地址。若部署选择共享服务密钥，SDK 使用 Authorization Bearer，浏览器经同源部署代理注入服务端凭据。密钥不进入 URL、页面包、localStorage、日志或 WS 子协议。session_id 只用于路由；连接时检查存在性、工作空间、过期和是否已有活跃连接，不把它当作用户所有权或隐式秘密。

WS 建立后进入 `admitting`，原子申请所需资源；最多等待已批准的实时等待预算。只有分配成功才发送 `session.created` 与 `nexus.session.ready`。失败发送结构化错误并关闭，不让客户端持续发送无人消费的音频。会话状态为：

```text
created → admitting → active → draining → closed
   └───────────────→ failed / expired
```

用户关闭时默认 drain 已接受的段与 TTS，`mode=abort` 则取消全部 operation；超过 drain deadline 进入失败清理。session terminal 记录保存 15 分钟供调用方协调，实时 PCM 与音频块不因此保留。

### 2.2 标准子集与命名空间

ASR 客户端事件仅支持 `session.update`、`input_audio_buffer.append`、`input_audio_buffer.commit`、`input_audio_buffer.clear`。ASR 服务端事件支持 `session.created`、`session.updated`、`input_audio_buffer.committed`、`input_audio_buffer.cleared`、可用时的 `input_audio_buffer.speech_started` / `input_audio_buffer.speech_stopped`、`conversation.item.input_audio_transcription.delta`、`conversation.item.input_audio_transcription.completed`、`conversation.item.input_audio_transcription.failed` 与 `error`。下文 completed / failed 在明确 ASR 语境中是上述完整名称的简称。[官方事件参考](https://developers.openai.com/api/reference/resources/realtime/server-events)

`session.update` 仅接受声明的 transcription 字段：`session.type=transcription` 及 `session.audio.input` 下的 `format`、`transcription`、`turn_detection`；PCM 投影为 `{type: "audio/pcm", rate: 24000}`，与当前 OpenAI Realtime transcription session 形状一致。`session.created` / `session.updated` 回显 `object=realtime.transcription_session` 的同一形状，并把绑定、profile、limits 与 config revision 放在可选 `nexus` 对象。与 `nexus.session.update` 映射到同一个配置命令，禁止两套配置状态。首期 active 会话仅允许 ASR 策略白名单在下一段边界变更，固定模型、音色、格式 / 采样率与 TTS 配方不可热换；标准事件未带 expected revision 时由单连接内串行命令按当前 revision 处理。回显实际 config_revision 与生效段，尚未生效的值不能用于当前段。P0 固化字段 schema，不接受未声明的 `include`、`response.*`、conversation history、semantic VAD 或 tool call 事件。[官方客户端字段](https://developers.openai.com/api/reference/resources/realtime/client-events)

`commit` 表示关闭当前输入段并请求其最终结果；`clear` 只丢弃尚未提交的输入缓冲，不撤销已接受的 operation。重复 commit 用客户端 event_id 去重，无新音频且不是已知重试时返回 `empty_audio_buffer`。有实际音频但无语音时，最终 transcript 可以为空，reason 为 `no_speech`。

后端 vLLM Realtime 的事件形状、commit 顺序与输入格式不必与公开接口相同；adapter 转换而非透传。[vLLM 实时转写接口](https://docs.vllm.ai/en/stable/serving/online_serving/speech_to_text/#realtime-api)

TTS、可替换预览、辅助结果与资源控制使用下表的 SpeechNexus 扩展；不借用 OpenAI LLM response 事件承载语音合成。

### 2.3 事件字典

| 方向 / event.type | 关键字段与语义 |
| --- | --- |
| S→C `nexus.session.ready` | `session_id`、`stream_epoch`、`effective_configuration`、limits、input credit；可开始发送 |
| C→S `nexus.session.update` | `event_id`、`expected_config_revision`、ASR 策略白名单 overrides；在下一段边界生效 |
| S→C `nexus.session.updated` | 新 config revision、effective 值和 `effective_from_segment_id`；无回显即未生效 |
| S→C `nexus.input.ack` | `stream_epoch`、`accepted_through_sample`、`credit_samples`；确认进入会话有界缓冲，不代表已识别或保存 |
| S→C `nexus.transcription.segment_closed` | `operation_id`、`segment_id`、wire sample_span、`reason=vad/client_commit/budget_rollover` |
| S→C `nexus.transcription.hypothesis` | `segment_id`、`revision`、完整 `text`、`sample_span`；替换该段上一个预览 |
| S→C `nexus.transcription.metadata` | `segment_id`、`text_revision`、`metadata_revision`、alignment / diarization status 与结果 |
| C→S `nexus.tts.start` | `event_id`、`utterance_id`；可重申会话固定的 voice revision / text policy / 格式，值必须一致；成功后返回 operation_id |
| S→C `nexus.tts.started` | `utterance_id`、`operation_id`、实际音频 descriptor、text credit |
| C→S `nexus.tts.append_text` | `event_id`、`utterance_id`、`text_seq`、`text`；仅接受连续序号 |
| S→C `nexus.tts.text_ack` | 已接受 text_seq 与仍可发送的字符 credit；不表示音频已生成 |
| C→S `nexus.tts.commit_text` | `utterance_id`、`through_text_seq`；冻结指定前缀并允许其进入合成 |
| C→S `nexus.tts.finish_text` | 提交剩余文本并关闭本 utterance 的输入；不关闭 WS |
| C→S `nexus.tts.cancel` | `event_id`、`utterance_id`；请求取消对应计算与发送 |
| S→C `nexus.tts.audio.delta` | `operation_id`、`utterance_id`、`chunk_seq`、`audio`（Base64 PCM）、`start_sample`、`sample_count` |
| S→C `nexus.operation.done` | `operation_id`、`kind`、`status=succeeded/cancelled/failed`、独立 `cleanup_state`、receipt、error / reason；唯一逻辑终态。`cancelled` 只允许 `not_required` / `complete`；停止未知时使用 `failed` + `quarantined` |
| S→C `nexus.session.gap` | 输入缺口或新 epoch 的 sample_span / 原因；不得以静音假装没有丢音 |
| C→S `nexus.session.close` | `event_id`、`mode=drain/abort`；停止接收新 operation |
| S→C `nexus.session.closed` | 会话终态及未成功 operation 的引用 |

标准 OpenAI ASR server event 只保留 OpenAI 声明的字段（`event_id`、`item_id`、`content_index` 等），不附加 SpeechNexus 私有 envelope；客户端用 `item_id` 关联同一段的 completed / failed，通过 `nexus.operation.done` 取得整体 Operation 身份。所有 `nexus.*` server event 附 `event_id`、连接内递增 `seq`、`session_id`、`stream_epoch` 和时间；与命令有关的事件附 `client_event_id`，与执行有关的事件附 `operation_id`。nexus 客户端命令必带 event_id；标准 ASR 事件的 event_id 可省略，但省略时不能安全重发 append。带 event_id 的命令在会话生命周期内唯一，重复同内容返回原接受结果，重复不同内容拒绝。术语 `S→C` 和 `C→S` 只描述传输方向。

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

音频 chunk_seq 连续，start_sample / sample_count 描述该 utterance 输出时间轴。发送 complete 表示所有音频已生成并交给传输层，不能证明浏览器收到全部字节或播放结束。业务 UI 分别显示生成、缓冲与播放状态。

### 2.6 格式、背压、断线与取消

实时 wire 基线为单声道 PCM16 LE、24 kHz；adapter 在受控边界重采样到模型输入规格。TTS 会话握手返回实际输出 descriptor，客户端按 descriptor 播放。格式和采样率在会话内不可变化。时间范围使用 wire sample clock 的半开区间 `[start,end)`，不能混入服务端墙钟或模型 16 kHz 下标。

初始限制：输入 chunk 建议 20–100 ms、上限 250 ms；单 JSON 消息上限 64 KiB；未消费实时输入 credit 上限 2 秒；客户端网络缓冲上限 5 秒；单个 ASR 段上限由预设与绑定共同约束且不超过 30 秒。Base64 和 JSON 开销计入网络预算。辅助模型状态必须有独立的有界内存声明，不能借会议长期连接累计整场 PCM。

超过 credit 继续发送时，先报告背压，持续违规关闭连接；不静默丢 PCM。允许合并过时的预览计算请求，但必须保留尚未处理的已接受 PCM。慢速接收 TTS 的客户端触发有界音频积压，超过协商上限取消 operation；不可无限生成并堆积。

WS 断开即停止接收新操作，取消实时计算并回收 owner，记录明确的断开终态。首版不提供透明音频重放和无缝恢复：重连创建新 session / epoch，应用保留已确认文字，显示输入缺口；不得自动重播旧 TTS。客户端心跳建议 10 秒，30 秒失联进入断开清理；会话最长 8 小时，续接必须建立新 epoch。

取消请求得到接受不等于算力释放。只有 adapter 确认执行结束、abort 完成或受控进程已退出后，才归还资源。取消后禁止向客户端继续发布该 operation 的音频或结果；迟到 vendor 输出只作清理，不写业务状态。每个已接受 operation 在服务端恰有一个逻辑终态，网络事件只承诺可去重交付；连接中断时通过 GET 协调，不能承诺客户端恰收一次。

## 3. 调用示例

### 3.1 同步 TTS 与文件转写

标准兼容请求无需 nexus、workspace 或工作台项目；下例中的 voice 由部署发布的默认 revision 解析，实际绑定 / 版本写入 receipt。

```json
{
  "model": "tts-interactive",
  "input": "欢迎使用 SpeechNexus。",
  "voice": "narrator",
  "response_format": "wav"
}
```

下面是同一 `POST /v1/audio/speech` 的增强 JSON body。所有 ID 与文字都是说明性占位值，不表示已有对应资源或已运行该请求。HTTP 使用当前部署入口；首期不要求用户登录。若部署启用共享服务密钥，由 SDK Bearer 或同源代理提供，不能把该密钥放入示例页面。

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

REST 的完整 input 在请求接受时一次性冻结，`audio_delivery` 只能为 `complete`。成功返回二进制 PCM，Content-Type 与协商 descriptor 在响应头回显，X-Request-ID / X-Operation-ID 供协调；中途失败不能再改 HTTP status，客户端据传输结束与 operation receipt 判断是否完整。需要持续分块交付时使用实时接口，需要可恢复完整音频时使用 Job。

同步 ASR 使用 multipart 的 `file`、`model=asr-quality`、`language=zh`、`response_format=json`；`nexus` 部分按 [同步 REST 子集](speech-api.md#13-同步-rest-子集) 传入相同工作空间与预设信息。file 是上传内容，不是服务器文件路径。成功 JSON 至少包含 text；完整原生结果与辅助状态通过 Job 的 TranscriptDocument 获取，不随意改变兼容子集响应形状。

### 3.2 创建实时 ASR 会话

```json
{
  "workspace_id": "ws_demo",
  "client_reference": "project:prj_demo",
  "profile": {"id": "caption-live", "revision": "1"},
  "modalities": ["asr"],
  "models": {"asr": "asr-quality"},
  "audio": {
    "input": {"encoding": "pcm_s16le", "sample_rate_hz": 24000, "channels": 1}
  },
  "overrides": {"asr": {"language": "zh"}},
  "constraints": {"max_admission_wait_ms": 500, "allow_degradation": false}
}
```

该 body 发往 `POST /v1/nexus/realtime/sessions`。ASR-only 时 models 只能包含 asr；TTS-only 只包含 tts；双工同时包含两者。包含 TTS 时，创建 body 必须额外固定 `voice={id,revision}`，或显式选择部署默认声音并在响应中解析为固定 revision，同时固定 tts 策略和输出 descriptor；start 中重复的值必须与会话一致，换声创建新会话。双工原子预留两者的容量承诺。创建响应返回 session_id、expires_at、connection_url 与 effective_configuration；按 [实时会话建立](speech-api.md#21-创建入口检查与连接) 连接且收到 ready 后才发送 PCM。

输入 PCM 按固定格式通过 input_audio_buffer.append 发送，sample clock 从该 epoch 的 0 开始连续累计。官方子集事件未提供 sample offset 时由服务按已接受的字节数推导；服务拒绝错格式和不完整样本，不靠客户端墙钟推断缺失数据。支持自动重试的 SpeechNexus SDK 必须发送稳定 event_id；不带 event_id 的兼容调用允许执行，但不支持重复 append 的安全重发。

### 3.3 实时 TTS：逐步提交文本

先用以下 body 创建 TTS-only 会话，收到 ready 后再发送 start / append / commit / finish。字段定义以 [实时事件字典](speech-api.md#23-事件字典) 为准。

```json
{
  "workspace_id": "ws_demo",
  "profile": {"id": "speech-interactive", "revision": "1"},
  "modalities": ["tts"],
  "models": {"tts": "tts-interactive"},
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
