---
title: "执行网关客户端（传输层，不是准入层）"
status: active
date: 2026-10-27
---

# 执行网关客户端（传输层，不是准入层）

`backend/src/speechnexus_backend/speech/infrastructure/gateway_execution_client.py`
只负责已授权执行事实的 HTTP 传输、冻结契约校验、错误分类和私有结果对象读取。
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
| `MinioExecutionResultObjects.read/read_json` | 配置的 MinIO bucket | 固定版本、长度与 SHA-256 校验后返回结果字节 |

认证使用部署签发的组件凭据（Bearer），从 `Settings.execution_gateway_*` 读取；
凭据、响应正文和对象内容不进入异常消息或日志。

## 错误分类

| 类别 | 异常 | 调用方动作 |
| --- | --- | --- |
| 连接失败 | `GatewayConnectionError` | `request_may_have_run=False` 时才可考虑重新提交；仍须先核对原身份 |
| 超时 | `GatewayTimeoutError` | 提交结果未知：按原 `submit_intent_id` 查询，不创建第二个执行 |
| 4xx | `GatewayRejectedError` | 语义拒绝；按 `gateway_code` / `recovery` 处理，不盲目重试 |
| 5xx | `GatewayServerError` | 按 `recovery` 查询或对账；只读调用可按显式策略重试 |
| 响应不合 schema | `GatewayResponseError` | 协议错误；不把响应当作成功事实 |

`timeout_seconds`、`max_retries`、`backoff_seconds` 是构造参数，来自 settings；
提交 POST 永远单次尝试，只有幂等读取使用重试预算。

## 为什么它单独不能让 `/v1/audio/transcriptions` 变成 200

- 公开同步接口还没有生产准入/派发器来产生 lease、attempt、stage、fencing token、
  command digest 和完整 `asr_file` 命令（DEL-96）。
- 网关私有 ASR 结果当前只有 `{"text": ...}`；公开 `TranscriptionResult` 必填
  `language` 和 `duration_ms`，verbose 还需要真实 segments/words（DEL-97）。
- 本客户端没有接入 `create_app`，也不推断缺失字段。等 DEL-96 与 DEL-97 到位后，
  由后续卡把同步端点接到同一执行链。

## 验证

`tests/unit/backend/test_gateway_execution_client.py` 使用 `httpx.MockTransport`
覆盖连接失败、4xx、5xx、超时、响应不合 schema 五类分支，断言发出的 JSON 与构造的
请求完全一致，并覆盖只读重试、固定版本/长度/摘要的结果对象读取。
这些是传输层证据，不是准入、真实引擎、对象存储部署或公开接口可用性证明。
