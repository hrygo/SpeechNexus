---
title: "执行网关客户端（传输层，不是准入层）"
status: active
date: 2026-10-27
---

# 执行网关客户端（传输层，不是准入层）

`backend/src/speechnexus_backend/speech/infrastructure/gateway_execution_client.py`
负责已授权执行事实的 HTTP 传输、类型化接口的契约校验、错误分类和私有结果对象读取。
它不选择 binding / workspace / pool，不生成 lease、attempt、stage、fencing token 或
command digest，不构造执行命令，不派发调度，也不释放预算。

调用方（DEL-96 的准入/派发器）必须先通过准入事务取得完整身份，再构造
`SubmitExecutionRequest`。客户端只把该请求原样发送到
`POST /internal/v1/executions/submit`，并校验 200 响应符合
`SubmitExecutionResponse`。调用方仍须核对响应中的 `submit_intent_id`、
`command_digest` 等身份与原请求一致；客户端不做业务对账或准入裁决。

## 接口

| 方法 | 网关路由 | 语义 |
| --- | --- | --- |
| `submit(request)` | `POST /internal/v1/executions/submit` | 持久化一个已准入执行；单次尝试，不自动重试 |
| `query(submit_intent_id)` | `GET /internal/v1/executions/{id}` | 读取权威执行状态；只读重试 |
| `staging_result(submit_intent_id)` | `GET /internal/v1/executions/{id}/staging-result` | 读取私有 JSON 结果回执；只读重试 |
| `audio_staging_result(submit_intent_id)` | `GET /internal/v1/executions/{id}/audio-staging-result` | 读取私有音频结果回执；只读重试 |
| `abort(target)` / `abort_idempotent(target)` / `abort_tts(target, ...)` | `POST /internal/v1/executions/{id}/abort` 或 `/abort-intents` | 对固定目标提交停止请求；单次传输，结果未知时按原身份恢复 |
| `query_abort_recovery(id)` / `query_disable_target(target)` / `query_tts_target(target)` | `GET /internal/v1/executions/{id}` | 查询原执行或停止恢复事实；目标查询校验完整身份 |
| `query_tts_configuration_evidence()` | `GET /internal/v1/tts-configuration-evidence` | 读取并校验网关的 TTS 配置证据 |
| `MinioExecutionResultObjects.read/read_json` | 配置的 MinIO bucket | 固定版本、长度与 SHA-256 校验后返回结果字节 |

派发适配器使用 `submit_document`、`query_document` 和 `staging_result_document` 传递原始 JSON 对象：这些方法检查传输和对象形状，不执行类型化方法的完整 schema 投影；请求的冻结语义与响应身份仍须由适配器和调用方校验。

`query_abort_recovery` 对同一执行查询路径发送
`Accept: application/vnd.speechnexus.execution-recovery.v1+json`，读取专用恢复响应；
普通 `query` 返回的旧表示不能替代该恢复回执。`abort_idempotent` 和 `abort_tts` 使用
持久 abort 入口，不回退旧 `/abort`；每次传输本身不自动重试。

认证使用部署签发的组件凭据（Bearer），从 `Settings.execution_gateway_*` 读取；
凭据、响应正文和对象内容不进入异常消息或日志。

## 错误分类

| 类别 | 异常 | 调用方动作 |
| --- | --- | --- |
| 连接失败 | `GatewayConnectionError` | `request_may_have_run=False` 仅表明本次调用未发出，不能证明此前同一意图未执行；先按原身份查询并遵循调用链的提交许可，再决定是否重发 |
| 超时 | `GatewayTimeoutError` | 提交结果未知：按原 `submit_intent_id` 查询，不创建第二个执行 |
| 4xx | `GatewayRejectedError` | 语义拒绝；按 `gateway_code` / `recovery` 处理，不盲目重试 |
| 5xx | `GatewayServerError` | 按 `recovery` 查询或对账；只读调用可按显式策略重试 |
| 响应不合 schema | `GatewayResponseError` | 协议错误；不把响应当作成功事实 |

`timeout_seconds`、`max_retries`、`backoff_seconds` 是构造参数，来自 settings；
提交与停止 POST 每次调用仅尝试一次；只有幂等读取使用重试预算。读取仅对连接、超时和
允许重试的 5xx 使用该预算；4xx、响应格式错误及明确 `retryable=false` 的 5xx 不自动重试。
调用方按原身份执行显式恢复不等于客户端自动重试。

## 与同步转写装配的关系

- 公开同步接口已有[可配置生产装配](../operations/synchronous-transcription.md)，使用准入事务和此客户端构造、提交完整 `asr_file` 命令；这与仍缺生产 planner 的公开异步 Job 派发器是不同范围。
- 网关私有 ASR 结果携带真实解码的 `duration_ms`；`language`、`segments` 和 `words`
  只在引擎实际给出时出现。公开 `verbose_json` 缺少语言、时长或分段证据时返回
  `unsupported_parameter`。应用层在请求 `word` 粒度时要求词证据，但当前生产文件 ASR
  命令规划器会先拒绝任何非空 `timestamp_granularities`，因此不能据此宣称词级请求可用。
  不得由适配器推断缺失事实。
- 正式应用工厂在配置满足时装配客户端；单独构造客户端不证明实例、资格、真实引擎或公开请求成功，也不推断缺失字段。

## 验证

[`tests/unit/backend/test_gateway_execution_client.py`](../../tests/unit/backend/test_gateway_execution_client.py) 使用 `httpx.MockTransport`
覆盖连接失败、4xx、5xx、超时、响应不合 schema 五类分支，断言发出的 JSON 与构造的
请求完全一致，并覆盖只读重试、固定版本/长度/摘要的结果对象读取。
这些是传输层证据，不是准入、真实引擎、对象存储部署或公开接口可用性证明。
