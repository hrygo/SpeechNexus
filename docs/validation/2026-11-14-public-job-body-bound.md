---
title: 公开 Job 创建的请求体字节上限实测（2026-11-14）
status: active
---

# 公开 Job 创建的请求体字节上限实测

本记录是 issue #703 的实施与反例证据，保留基线、命令与原始输出。测量只使用隔离数据库与
合成数据；默认上限值未改动（仍为 262144 B）。

## 结论

`POST /v1/nexus/jobs` 存在一个**契约此前未声明、由部署配置决定**的请求体字节上限
（`job_receive_max_bytes`，默认 262144 B = 256 KiB）。超限在 JSON 解析与准入之前拒绝。
#703 之后：

- 契约的 `createJob` 声明 `413 PayloadTooLarge`（复用共享组件），并在 description 里说明
  上限由部署配置决定、默认 256 KiB，稳定码为 `job_request_too_large`；
- 超限答复从 400 `invalid_job_request` 改为 **413 `job_request_too_large`**，零行写入；
- 两个上限各归各码：请求体字节 → 413，正文 `input.text` 码点（4096）→ 422
  `unsupported_parameter`（#690 已收敛），二者不再共用 400。

## 环境

- 基线：`origin/main` `4ad6cad7e6cc691e7df487d5da343f92483efdfb`，独立 worktree
  `/private/tmp/sn724`（分支 `fix/703-body-bound`），CPython 3.14.9、uv 0.13.5、
  系统 node v26.12.1、pnpm 12.14.0。
- PostgreSQL 18.6：临时集群
  `initdb --auth-local=trust --auth-host=trust --no-locale --encoding=UTF8`，
  仅监听 127.0.0.1:55439，独立目录，测量后销毁。
- MySQL 8.4.8：项目既有隔离容器 `speechnexus-coordination-mysql`（127.0.0.1:55434），
  仅用于满足双数据库集成夹具的前提。
- 装配：`tests/integration/test_public_job_admission_http_pg.py` 的 `admission_database`
  夹具，**生产准入装配**（`PostgresPublicJobs(admission_pool=…, require_admission=True)`），
  经真 HTTP（httpx + ASGI）发请求，`create_app` 与组合根一致。

## 原始输出

同一装配，`job_receive_max_bytes = 262144`：

```text
PROBE receive_bound=262144B plan_ceiling=4096codepoints
PROBE wide-body-413          wire= 275644B status=413 code=job_request_too_large retryable=False job=-
PROBE text-100k              wire= 300175B status=413 code=job_request_too_large retryable=False job=-
PROBE text-4097-422          wire=  12466B status=422 code=unsupported_parameter retryable=False job=-
PROBE normal-202             wire=    195B status=202 code=- retryable=- job=job-ab5226fca8ee43208b401a911b072021
PROBE same-key-replay        wire=    195B status=202 code=- retryable=- job=job-ab5226fca8ee43208b401a911b072021
```

- `wide-body-413`：正文取满 4096 码点、仅由非契约成员 `padding` 把 wire 撑到 275 644 B；
  413 之前没有任何行写入，`_snapshot` 前后相等（十张公共/准入表行数、配额计数与版本、
  节点预算、实例槽位全部不动）。
- `text-100k`：正文 100 000 汉字（wire 300 175 B）——本 issue 最初实测的那条请求；现在同样得
  413，且不再与「坏 JSON」共用 400。
- `text-4097-422`：正文超计划上限但 wire 远低于字节上限，仍是 422 `unsupported_parameter`。
- `normal-202` 与 `same-key-replay`：同键重放返回同一个 `job-…`，且重放不写新行。

## 反例（变异实验，均已还原，未进入提交）

| 变异 | 改动 | 期望 | 实际 |
| --- | --- | --- | --- |
| A | `jobs.py` 的 `_TooLargeError` 分支改回 `job_error("invalid_job_request", …)` | 契约用例与跨层用例红 | 红：`assert 400 == 413`（`test_body_over_the_receive_bound_answers_the_declared_413`、`test_declared_content_length_over_the_bound_answers_the_declared_413`、`test_body_over_the_receive_bound_is_refused_before_any_write[postgresql]`） |
| B | 删掉契约 `createJob` 的 `"413": PayloadTooLarge` 声明 | 契约校验类检查红 | 红：`check-route-conformance.py` → `undeclared_response_status: createJob can return 413; declared responses are 202,400,401,403,404,405,409,422,429,503`，退出码 1；同时 `test_create_job_declares_a_413_payload_too_large` 等 3 条 `KeyError: '413'` |
| C | 把 `ContractValidationError` 分支改报 `job_request_too_large`（两上限并成一个码） | 4097 正文用例红 | 红：`assert 413 == 422`（`test_text_above_the_declared_ceiling_keeps_its_own_422` 两条 + `test_text_above_the_plan_ceiling_is_refused_without_rows[postgresql]`） |
| D | 工作台处置表把 `[413, "job_request_too_large"]` 改为 `"unknown-outcome"` | 工作台用例红 | 红：3 条（处置表行、`does not let the body-bound refusal fall through to the unknown 5xx branch`、会话级 `treats a body over the deployment receive bound as a refusal, not an unknown outcome`），notice 变成「提交结果未知，原提交可能已执行」 |
| E | 把 `--declared<0` 与 `declared>max_bytes` 合并回同一个 `_TooLargeError` | 负长度用例红 | 红：`assert 413 == 400`（`test_negative_content_length_stays_a_400_not_a_413`） |

`make contracts-speech-changes`（`--surface speech`，baseline 解析为 `cef3943…`）在变异 B 下
**仍然是绿的**：该工具的未验证范围明确包含「added statuses/media … evolution」，所以它不承担
「声明的 413 被删」这类 status 增删的检测；这一层由离线 `check-route-conformance.py`
（静态推导 handler 与 `*_errors.py` 的码→状态表，再与契约声明求差）承担。二者都需要跑。

## 代码与契约落点

| 层 | 位置 | 内容 |
| --- | --- | --- |
| 契约 | `contracts/speech-openapi.yaml` `createJob` description | 请求体字节上限的存在、由部署配置决定、默认 256 KiB、413 与 `job_request_too_large`、与 422 正文上限的区别 |
| 契约 | 同文件 `createJob` responses | 新增 `"413": $ref "#/components/responses/PayloadTooLarge"` |
| 契约 | 同文件 `components.responses.PayloadTooLarge` | 共享描述补「declared or deployment-configured bound」与非重试语义 |
| 错误表 | `backend/src/speechnexus_backend/speech/interfaces/http/job_errors.py` | `job_request_too_large` → `(413, "invalid_request_error", "The Job request body exceeds the deployment receive limit.")`，不在 retryable 名单 |
| 映射 | `backend/src/speechnexus_backend/speech/interfaces/http/jobs.py` `_create` | `except _TooLargeError:` → `job_error("job_request_too_large", …)` |
| 接收 | 同文件 `_receive` | 负 `Content-Length` 与超限分开：负值仍是 400 `invalid_job_request`，只有超限才是 413 |
| 装配 | `backend/src/speechnexus_backend/app.py:749` | 未改动；`max_bytes=resolved_settings.job_receive_max_bytes` 仍是唯一上限装配点 |

## 与 issue 原始描述的差异

issue #703 的表格写「正文 100,000 汉字（≤ 契约 `maxLength: 200000`）」。该前提在 #690 之后已
不成立：`TtsJobInput.text.maxLength` 现为 4096，正文本身再也无法把 wire 撑过 262144 B
（4096 码点的最坏规范文档约 27.6 KiB）。但**字节上限本身仍然存在且可达**：

- 任何非契约成员或未来放宽的字段都能把 wire 推过上限（本记录用 `padding`）；
- 超限发生在 schema 校验之前，所以「未知字段 → 422」永远不会替它答题；
- 因而「上限存在这件事必须被声明」「超限必须答 413」两条仍然成立，也正是本卡交付的内容。

默认上限值本身未改动。

## 未验证项

- 未接真实引擎、对象存储或 GPU：本记录只覆盖 HTTP 接收 → 契约校验 → 准入事务 → 公共行写入
  这一段；413 是接收层拒绝，与执行无关。
- 未压测超大请求体的内存与连接占用；`_receive` 分块累计后才判超限，所以正好超限的请求会先
  收满 `max_bytes` 再拒。
- 未验证反向代理（uvicorn/nginx）自身的请求体上限是否先于应用层拒绝；若代理更小，客户端会
  收到代理的 413 且不带本契约信封。
- 只测了 `kind=tts`；其他 kind 共用同一接收层与映射，但未逐 kind 实跑。
