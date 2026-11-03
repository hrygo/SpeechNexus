---
title: "同步文件转写的生产装配"
status: active
version: "0.1.0"
date: 2026-10-28
---

# 同步文件转写的生产装配

`POST /v1/audio/transcriptions` 的业务规则与公开契约不变；本文描述后端进程如何把一次
同步请求接到**一个**既有执行网关客户端。此前 `create_app` 没有构造 `Transcriber`，
默认装配下该路由固定返回 `503 backend_unavailable`。现在组合根显式注入生产实现，
当基础配置有效但同步转写所需的可选配置缺失时，路由返回 `503`，日志列出缺失的
环境变量名。基础配置缺失或配置配对无效会使进程启动失败，不会先启动路由再返回 `503`。

本装配不新增公开 DTO、不修改 `contracts/`、不改变网关内部，也不创建第二份 HTTP 客户端。
它只消费既有 `GatewayExecutionClient`、准入事务、私有对象读写和 PyAV 本地探测。

## 启用条件

基础 `Settings` 必须提供 `SPEECHNEXUS_DATABASE_URL`、`SPEECHNEXUS_OBJECT_STORAGE_ENDPOINT`、
`SPEECHNEXUS_OBJECT_STORAGE_HEALTHCHECK_URL` 和结果桶 `SPEECHNEXUS_OBJECT_STORAGE_BUCKET`；
缺失或无效时配置校验失败。同步转写还需要以下配置；基础配置有效且以下值完整时才装配：

| 环境变量 | 含义 |
| --- | --- |
| `SPEECHNEXUS_TRANSCRIPTION_RECIPE_FILE` | 绝对路径，指向下述 owner-only 私有准入配方 |
| `SPEECHNEXUS_TRANSCRIPTION_INPUT_BUCKET` | 网关可读的私有输入桶，必须开启版本化 |
| `SPEECHNEXUS_EXECUTION_GATEWAY_BASE_URL` | 已有执行网关的固定 origin |
| `SPEECHNEXUS_EXECUTION_GATEWAY_COMPONENT_CREDENTIAL` | 网关组件凭据 |
| `SPEECHNEXUS_OBJECT_STORAGE_ACCESS_KEY` / `_SECRET_KEY` | 仅对输入与结果范围授权的身份 |

对象存储的 access/secret key 和网关的 URL/credential 分别必须成对提供；只提供其中一项
会使 `Settings` 校验失败。两项都缺失或缺少配方、输入桶时，同步转写不装配，日志列出
相关缺失项。基础对象存储 endpoint 和结果桶分别用于输入/结果对象访问。

可选的有界轮询与执行配置：`SPEECHNEXUS_TRANSCRIPTION_POLL_SECONDS`、
`SPEECHNEXUS_TRANSCRIPTION_MAX_AUDIO_MS`、`SPEECHNEXUS_TRANSCRIPTION_MAX_UPLOAD_BYTES`、
`SPEECHNEXUS_TRANSCRIPTION_TIMEOUT_SECONDS`、`SPEECHNEXUS_TRANSCRIPTION_MAX_COMMAND_BYTES`、
`SPEECHNEXUS_TRANSCRIPTION_STORAGE_TIMEOUT_SECONDS`。

入口为 `speechnexus_backend.transcription_runtime:create_app_from_env`（`main.py` 默认
`uvicorn` factory）。`create_app` 仍只接受注入的 `transcriptions=`，不自行创建运行时。
所需配置齐全并开始装配时，配方文件结构错误、摘要不符或权限不符使启动失败；如果
其他必需配置缺失，装配会先返回未配置，此时不会读取或验证配方。缺失配置不会静默降级为假引擎。

Compose 的最小装配、私有文件引用、迁移/角色与注册命令见
[最小 Compose 部署面](compose-minimal.md)。Compose 提供引用和示例，
不提供已批准配方、模型、引擎或已注册实例。

## 私有准入配方

配方承载公开 binding 文档不应包含的私有权威：绑定/清单修订、运行时计划、策略修订、
阶段身份和引擎参数。它是部署文件，不是公开注册接口，也不能由数据库已发布 binding
反推。读取规则：

- 绝对路径、当前 UID 所有、无 group/other 权限、非空普通文件，最多 65,536 bytes；
  不跟随最终 symlink；
- 拒绝重复键、非有限数字、未知字段与隐式类型转换；
- `recipe_digest` 为 `sha256:` 加 RFC 8785 规范 JSON（去掉 `recipe_digest` 自身）的
  SHA-256；不符即拒绝；
- 顶层 binding/manifest/service_class 必须与 `runtime_plan` 一致；
- `stage.reserved_slots` 不得小于 `stage.minimum_slots`；
- `binding_publication_id` 是公开 publication 的 revision ID，必须与实际执行的
  `binding_revision_id` 一致；不允许按一个 revision 校验后执行另一个 revision；
- `quota_dimensions` 当前只接受 `["concurrent_attempts"]`。

合成示例（真实部署必须替换为已批准值）：

```json
{
  "protocol": "transcription_admission_v1",
  "model_id": "asr-quality",
  "binding_publication_id": "binding-rev-3",
  "binding_revision_id": "binding-rev-3",
  "manifest_id": "manifest-1",
  "policy_revision_id": "policy-rev-1",
  "service_class": "batch",
  "admission_expires_at_us": 4000000000000000,
  "quota_dimensions": ["concurrent_attempts"],
  "stage": {
    "stage_id": "stage-asr-1",
    "instance_id": "instance-1",
    "minimum_slots": 1,
    "reserved_slots": 1
  },
  "runtime_plan": { "...": "完整 ApprovedRuntimePlan 字段" },
  "asr": {
    "silence_duration_ms": 400,
    "preview_interval_ms": 1000,
    "temperature": 0.0,
    "final_deadline_ms": 1000,
    "auxiliary_deadline_ms": 5000
  },
  "recipe_digest": "sha256:<64 lowercase hex>"
}
```

`asr` 参数直接受冻结执行命令约束：`silence_duration_ms` 0–30000、`preview_interval_ms` 100–5000、
`final_deadline_ms` 100–10000、`auxiliary_deadline_ms` 100–10000、`temperature` 0–1。
`final_deadline_ms` 是“段关闭到正文 final”的窗口。公开请求的 `nexus.deadline_ms`
与部署上限取较小值后形成执行计划期限：准入用它计算执行 deadline，网关轮询也用它
作为等待预算。当前实现先做请求接收、事实读取、媒体检查、对象写入和准入，提交网关后
才开始轮询计时；这些阶段及结果读取有各自边界，不能把该值当作整个 HTTP 请求的硬截止时间。

## 引擎实例注册前提

同步请求进入准入前，目标实例必须已有 `engine_instances` / `runtime_instance_heads`
ready 事实以及成功的 prepare intent。当前受支持的运维入口是
[`speechnexus-register-engine`](register-engine.md)；它不替代节点控制器，
也不构成模型/硬件资格。没有该事实时同步路径会以
`Transcription admission facts are not provisioned` 或准入拒绝结束。

## 一次同步请求的链路

1. 公开路由完成既有请求校验与 `AudioProbe`（生产为进程内 PyAV，无 `ffprobe` 进程）。
   时长、采样率、声道来自真实解码结果；不支持或超限在调用引擎前失败。
2. 应用层按公开 binding/workspace 事实生成 `TranscriptionPlan`，包含已发布的
   `binding_publication_id` 与处理后的上限。
3. `GatewayTranscriber` 把上传字节写入版本化私有输入桶，PUT 后 HEAD/GET 固定版本读回，
   核对长度与 SHA-256；未确认可读引用就不进入准入。
4. 读 live 配额版本、池 owner/fencing、实例 generation/版本，构造既有
   `AdmitRecipe` 并在同一个关系数据库事务中准入。
5. 用准入返回的完整身份构造 `SubmitExecutionRequest`，命令摘要为 RFC 8785 规范 JSON
   的 SHA-256；只调用一次 `GatewayExecutionClient.submit`。
6. 按原 `submit_intent_id` 轮询状态；成功后读取 `staging-result`，核对回执身份，再从
   固定版本私有对象读回结果正文并验摘要。
7. `text` 只来自该结果文档。`language` / `duration_ms` 缺省时保持缺省，不回填请求参数、
   不推断、不估算；`verbose_json` 因此无法渲染时返回 `422 unsupported_parameter`。
   分段和词时间戳分别要求每个非空转写片段都提供对应证据；缺少任一片段时省略该类
   时间戳，公开接口拒绝需要它的格式，不返回只覆盖部分正文的字幕或时间戳。
   当前生产文件 ASR 命令规划器在准入前拒绝任何非空 `timestamp_granularities`；
   即使引擎可能返回时间证据，也不代表显式请求 segment/word 粒度已经可用。

## 失败语义

| 情况 | 路由结果 |
| --- | --- |
| 基础配置有效，但同步转写所需配置缺失 | `503 backend_unavailable`，日志含具体环境变量名 |
| 基础配置无效或网关/对象存储凭据未成对 | 启动配置校验失败，无可用路由响应 |
| 连接失败、超时、5xx、网关响应无法解释、结果对象不可读 | 结果未知语义：`503 backend_unavailable`，不当作已失败，也不重发新执行 |
| 网关返回 408 / 429 | 结果未知语义：`503 backend_unavailable`；按原执行身份协调 |
| 网关明确 4xx 拒绝（非 408/429） | `503 backend_unavailable`，不返回部分正文 |
| 网关终态 `failed` / `aborted` | `503 backend_unavailable` |
| 网关状态 `unknown_quarantined`、回执身份不匹配或缺固定对象版本 | 结果未知语义：`503 backend_unavailable`，保留原身份并协调 |
| 轮询中检查到计划等待期限到达 | `504 operation_timeout`；不表示整个 HTTP 请求的各阶段均受同一硬期限约束 |
| 结果缺少 `text` 或字段不合规 | `503 transcription_output_invalid`，不猜 |
| 结果声明的 `duration_ms` 超过本次计划的音频时长上限 | `503 transcription_output_limit_exceeded`；该错误标为不可重试 |
| 需要分段或词证据，但结果包含字段不完整的对应条目 | `503 transcription_output_invalid`；不能将畸形条目当作有效时间戳 |
| 请求 `verbose_json`、`srt` 或 `vtt` 但结果没有分段证据 | `422 unsupported_parameter`（`response_format`）；即使正文为空也不伪造分段 |
| 请求 `verbose_json` 但回执无语言/时长 | `422 unsupported_parameter`（`response_format`） |
| 请求非空 `timestamp_granularities[]` | 通过上游请求和 binding 校验后，当前生产规划器返回 `422 unsupported_parameter`（`timestamp_granularities[]`），发生在输入写入和准入之前；上游校验也可能先拒绝请求 |

结果未知时由有权检查内部执行事实的操作者保留并查询原 `submit_intent_id`；公开响应
不承诺向客户端提供该内部身份，不能要求普通调用方凭响应自行查询。无法定位原身份时应
保留未知状态并升级协调。本装配不自动重提、不创建第二执行、不释放或归还预算。

当前错误信封中的 `backend_unavailable` 和 `operation_timeout` 均标为 `retryable: true`，
但同步转写路由拒绝 `Idempotency-Key`，不能仅凭该标志认定重发 POST 会安全重放原执行。
调用方应核对其 SDK 或代理的自动重试设置，结果未知时先协调原身份；`X-Request-ID` 是
请求关联信息，不应被当作公开幂等或恢复契约。错误信封的重试声明与原身份恢复之间的
差异须在相关契约和消费者验收中处理。

## 验证范围

单元测试使用真实 `GatewayTranscriber`、真实 `TranscriptionCommandPlanner`、真实
`GatewayExecutionClient`（`httpx.MockTransport` 假网关）与真实 HTTP 路由，证明默认
装配会发出 `submit → query → staging-result` 调用链、正文来自结果文档、超时为未知语义、
缺字段不猜测，以及未配置时仍为 503 且日志点名的环境变量。

未覆盖：真实 PostgreSQL 准入事务、真实 MinIO 版本化读写、真实网关进程与真实引擎。
这些需要隔离的 Linux/集群环境由对应任务验证；本文与单元测试不构成生产资格或 Gate 放行。
