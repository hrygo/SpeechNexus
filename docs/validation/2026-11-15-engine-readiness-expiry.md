---
title: "引擎 readiness 过期从 job_unavailable 拆出并给出续期路径（#828）"
status: active
version: "0.3.0"
date: 2026-11-15
---

# 引擎 readiness 过期从 job_unavailable 拆出并给出续期路径（#828）

## 结论

- 创建关闭的 `503 job_unavailable` 兜底已删除。准入拒绝逐码映射到公开错误码，
  `job_unavailable` 只表示"存储真的拒绝了这次读/写"。引擎侧原因各有自己的码：
  `engine_not_ready`（收据过期）、`engine_state_mismatch`（收据身份/世代/拓扑/fencing
  不符或实例行不存在）、`engine_resource_disabled`（实例、池、节点或配方审批未激活）、
  `recipe_not_admitted`（与固定实例已批准配方不一致）。
- `retryable` 只有一个定义：**同一请求能否在没有带外改动的情况下成功**。`true` 只覆盖
  不需要运维介入就能自愈的情形（容量租约释放、并发写完成、连接级结果未知按原身份重放、
  清理自行结束）；需要重签收据、恢复实例、重新登记、补配额行的恢复一律 `false`，恢复步骤
  写在 `message` 里。定义只写在 `contracts/shared/errors.schema.json` 的
  `ErrorBody.retryable`，旧的"按『是否重放未知结果』计算"第二定义已删除——两套定义并存正是
  `execution_cleanup_pending` 在两侧取相反值的原因。
- 三个兜底通道关闭，且关闭方式都是"回答声明过的 JSON，而不是编一个存储故障"：
  `rejection_code()` 未映射不再 `KeyError` 穿透，`_raise_admission_rejection()` 未登记码不再
  静默返回，`job_error()` 未声明码不再改写成 `job_unavailable`。三者现在都答 `500
  admission_state_invalid`（`api_error`，`retryable: false`，message 点名非法码）。
- 词汇表单一来源：`domain/admission.PUBLIC_ADMISSION_CODES` 是唯一映射表，
  `frozenset(PUBLIC_ADMISSION_CODES.values()) | CAPACITY_REJECTION_CODES` 推导出 HTTP 接受
  集合，所以"映射里加了码但接受集合没加"这种半改状态无法编译通过（它由推导消除，不靠再列一张表）。
- voice-draft 提交面按真实原因分开回答：引擎侧原因 503 `voice_draft_engine_unavailable`
  （`retryable: false`，逐因文案给恢复命令）、并发竞争 503
  `voice_draft_admission_contended`（`retryable: true`）、准入截止时间已过 422
  `voice_draft_request_expired`、内部不变量失败 500 `voice_draft_contract_violation`。只有
  请求自身事实（绑定、输入素材、参数、配额行）才是 422 `voice_draft_input_unavailable`。
- `unreplayable` 文案按 `(status, code)` 分支：413 说体量上限、422 `job_request_expired` 说
  准入期限已过，恢复后的页面只说"协议已拒绝且不能按原键再次提交"这一类级事实，不再对任何
  原因都套 413 的解释。
- 契约变成机器可读：`createJob` 的 422 / 500 / 503 响应用
  `allOf: [ErrorResponse] + error.properties.code.enum` 列出该响应可返回的全部码；共享
  `errors.schema.json` 的 `code` 仍为 pattern（跨四协议，不改枚举）。生成物由
  `make contracts-generate` 重出，未手改。
- 续期入口：`speechnexus-register-engine --renew-readiness`（部署装配为
  `engine-readiness-renew`）。它在收据仍有效时也强制重签，因此定时间隔短于 TTL 即可；
  输出 `readiness.state` / `readiness.renewed`，`expiring` / `expired` 另在 stderr 告警。
- 过期可观测：只读报告 `speechnexus-engine-readiness`（部署装配为 `engine-readiness`），
  按数据库时钟逐实例给出 `valid` / `expiring` / `expired` / `missing` / `invalid` 与剩余
  秒数，过期时退出码 `5`；`--fail-on-expiring` 让 `expiring` 也非零。
- 未改变的东西：`/readyz` 仍不因收据过期失败（读取与取消必须继续可用），admission 判定
  未放宽，任何拒绝仍不写行。`/readyz` 全绿而创建全 503 的缺口是已知缺陷，见下文与 #845。

## 环境与基线

| 项 | 值 |
| --- | --- |
| 主机 | macOS arm64，2026-11-15 |
| 基线 | `origin/main` = `6a4d7184`（含 #831 CI 重构、#844 本卡审查文档） |
| worktree | `/private/tmp/sn838`，分支 `fix/828-readiness-expiry` |
| Python / pytest / pnpm | 3.14.9 / 9.1.1 / 12.14.0 |
| PostgreSQL | 一次性容器 `sn838-pg`（`postgres:16.15-alpine`，`127.0.0.1:55461`），测后删除 |
| MySQL | 仓库既有隔离容器 `speechnexus-coordination-mysql`（`127.0.0.1:55434`）；每个用例自建唯一 database/schema |
| DSN 环境变量 | `SPEECHNEXUS_TEST_DATABASE_URL`、`SPEECHNEXUS_TEST_MYSQL_URL` |
| 基线用例数（在 `origin/main` 上实测） | `tests/ci` 404、`tests/unit` 7146、`tests/unit/backend` 3273、`tests/contract` 3815 |

本机 `uv` 配置了清华镜像，`uv run` 会重写 `uv.lock`；全部命令使用
`uv run --no-sync ...`，并在每次运行后确认 `uv.lock` 与基线一致。

## 修复前的机制（#828 与审查 B3 指向的同一处）

1. `runtime_observations.VerifiedRuntimeReadiness.require_valid()` 在
   `now_us >= expires_at_us` 时抛 `reconciliation_required`；身份不符也用同一个码。
2. `domain/admission.require_live_authority()` 把它转成
   `AdmissionRejectedError(code)`，`public_jobs._submission_result()` 再转成拒绝结果。
3. `infrastructure/public_job_admission.rejection_code()` 只保留容量类码
   （`job_capacity_exhausted`），其余一律折成 `job_unavailable`。
4. `interfaces/http/job_errors` 把 `job_unavailable` 映射为
   `503 / "Job storage is unavailable." / retryable: false`。

因此"引擎仍 ready、只是收据过期"与"存储读失败"在调用方看来完全一致；#572 验收中首次创建
就是这样被报成存储不可用，而重跑 `engine-register` 后同一请求返回 `202`。同一个兜底让
收据身份不符、实例被停用、配方不一致、版本竞争全部得到"存储不可用"，这是审查 B3 要求删除
而不是再加一个码的原因。

## 修复后的机制

### 完备映射（B3）

`domain/admission.py` 声明准入词汇表：容量类 `CAPACITY_REJECTION_CODES`、
`ADMISSION_REJECTION_CODES`（后者含容量类）与公开码映射 `PUBLIC_ADMISSION_CODES`。
`infrastructure/public_job_admission.py` 直接以 `_PUBLIC_CODES = PUBLIC_ADMISSION_CODES`
引用该映射，`rejection_code()` 先判容量、其余 `return _PUBLIC_CODES[code]`；未映射码不再
`KeyError` 穿透调用栈，而是转成 `PublicJobAdmissionRejectedError("admission_state_invalid")`。

未映射码的完整路径是"声明过的缺陷信封"，不是存储故障：

| 通道 | 修复前的行为 | 现在 |
| --- | --- | --- |
| `rejection_code()` 收到未映射码 | `KeyError` 穿透到 HTTP 边界，或按调用方兜底成存储故障 | `PublicJobAdmissionRejectedError("admission_state_invalid")` → 500 |
| `_raise_admission_rejection()` 收到未登记码 | 静默 `return`，调用方落 `JobRejectedError("contract")` → `503 job_unavailable` | 抛 `JobAdmissionRejectedError("admission_state_invalid")` → 500 |
| `job_error()` 收到未声明码 | `code = "job_unavailable"` 兜底改写 → `503 "Job storage is unavailable."` | 500 `admission_state_invalid`，message 点名该码 |
| voice-draft 边界收到 `KeyError` | 被 `except (…, KeyError, …)` 吞成 `503 voice_draft_unavailable` + `retryable: true` | 专用 `except KeyError` → 500 `voice_draft_contract_violation` |

| 内部准入码 | 触发原因 | 公开码 | 状态 / 类型 | `retryable` | message 指向的恢复 |
| --- | --- | --- | --- | --- | --- |
| `capacity_exhausted`、`quota_exceeded` | 配额/节点/实例槽已满 | `job_capacity_exhausted` | 429 / `rate_limit_error` | `true`（带 `Retry-After`） | 等前一批释放后再重试 |
| `readiness_expired` | 收据窗口已过 | `engine_not_ready` | 503 / `service_unavailable_error` | `false` | `speechnexus-register-engine --renew-readiness`，再发新请求 |
| `reconciliation_required`、`not_found`、`stale_instance_generation` | 收据身份（instance/generation/topology/fencing）不符，或实例/池行不存在 | `engine_state_mismatch` | 503 / `service_unavailable_error` | `false` | 重新登记引擎后再发新请求 |
| `policy_or_resource_disabled` | 实例、池、节点或配方审批不 active | `engine_resource_disabled` | 503 / `service_unavailable_error` | `false` | 恢复实例后再发新请求 |
| `recipe_mismatch` | 请求与固定实例已批准配方不一致 | `recipe_not_admitted` | 503 / `service_unavailable_error` | `false` | 按已批准配方重新登记引擎 |
| `version_conflict`、`stale_fencing_token`、`owner_busy` | 版本 CAS / 池 owner fencing 竞争 | `job_admission_contended` | 503 / `service_unavailable_error` | `true` | 重试同一请求 |
| `lease_not_active` | 该请求自身的准入截止时间已过 | `job_request_expired` | 422 / `invalid_request_error` | `false` | 提交新请求（截止时间烘焙在意图里） |
| `unapproved_runtime_template` | 实例所属模板未获准 | `engine_resource_disabled` | 503 / `service_unavailable_error` | `false` | 恢复/重新登记获准模板的实例 |
| `gateway_drain_incomplete` | 网关仍在排空上一世代 | `job_admission_contended` | 503 / `service_unavailable_error` | `true` | 重试同一请求 |
| `idempotency_conflict`、`intent_conflict` | 同一幂等键/身份属于另一个意图 | `idempotency_conflict` | 409 / `invalid_request_error` | `false` | 重放或查询原意图 |
| `invalid_counter`、`invalid_execution_request`、`invalid_runtime_request` | 时钟或版本计数器越界、内部命令非法 | `admission_state_invalid` | 500 / `api_error` | `false` | 部署缺陷，不得按原样重放 |

`job_unavailable`（503，`service_unavailable_error`，`retryable: false`）现在只有存储自身
拒绝读写时产生；`job_outcome_unknown`（503，`retryable: true`）仍是连接级失败/结果未知。
准入侧可重试的只有 `job_capacity_exhausted`（429）、`job_admission_contended`（503，含版本
CAS / fencing 竞争与 `gateway_drain_incomplete`）与 `job_outcome_unknown`（503）；等待清理结束
的 `execution_cleanup_pending`（409）按同一定义也是 `true`。

词汇表 19 个准入码 = 2 个容量类 + 17 个映射项；`PUBLIC_ADMISSION_CODES` 是唯一来源，
`_PUBLIC_CODES` 直接引用它，HTTP 接受集合
`frozenset(PUBLIC_ADMISSION_CODES.values()) | CAPACITY_REJECTION_CODES | {"job_capacity_exhausted"}`
由它推导，`_CREATE_REFUSAL_CODES` 再叠加提交面自己的 6 个 422 输入拒绝码。

### 契约（B2）

`contracts/speech-openapi.yaml` 的 `createJob`：

- 503 响应枚举 7 个码：`job_unavailable`、`job_outcome_unknown`、
  `job_admission_contended`、`engine_not_ready`、`engine_state_mismatch`、
  `engine_resource_disabled`、`recipe_not_admitted`；
- 422 响应枚举 19 个码（含新 `job_request_expired`）；
- 500 响应枚举 `admission_state_invalid`（`ErrorType` 没有 `server_error`，故用
  `api_error`）；
- `description` 给出引擎侧拒绝表（码 / 原因 / 恢复）并声明"这些都不是
  `job_unavailable`"、`retryable: false` 除竞争类之外、TTL 属运维截止时间。

收窄的写法有约束：结构变更检查器按"解析后的形状"比对，只把 `$ref` 换成 `allOf` 会让同一响应
看起来丢了 `required` / `properties`（`make contracts-speech-changes` 报
`type changed from ['object'] to []`）。因此每个收窄响应保留
`type: object` + `additionalProperties: false` + `required: [error]` +
`properties.error`（指向共享 `ErrorBody`），再把 `allOf: [ErrorResponse, code.enum]` 当作
附加约束挂上；语义等价（`allOf` 只会更严），结构检查器报 `Known HTTP structural checks:
passed`。这也是"加枚举"与"不制造假破坏"能同时成立的原因。

`contracts/shared/errors.schema.json` 的 `ErrorBody.retryable.description` 是**唯一**定义
（本次唯一改动该文件的字段），原文里"speech Job 按『是否重放未知结果』计算"的第二定义与
`contracts/workbench-openapi.yaml` 复述它的整段文字都已删除——两套定义同时存在会让同语义的
码在两侧取相反值（旧文里 `execution_cleanup_pending` speech 侧 `false`、workbench 侧
`true`）。当前定义：

```text
Whether the same request can succeed without an out-of-band change. The value belongs
to the refusing operation's disposition, not to the status or the code: ... `true`
means repeating the same request later can be admitted with no operator, configuration
or data change: a released capacity lease, a concurrent writer finishing, a
connection-level outcome that the original identity replays, and a cleanup that
completes on its own all qualify. `false` means the same request is answered the same
way until something outside the request changes, and the operation's published code and
message name that change: provisioning the workspace quota, re-signing an engine
readiness receipt, restoring a disabled instance, or re-registering against an approved
recipe. The field never grants permission to drop an identity ...
```

三处取值冲突按这一条定义消解，而不是各留一套说辞：

| 冲突 | 旧值 | 现在 | 依据 |
| --- | --- | --- | --- |
| `execution_cleanup_pending`（409） | speech `false` / workbench `true` | 两侧都 `true` | 清理自行结束，同一请求随后成功，无需带外改动 |
| 存储拒绝：`job_unavailable` / `voice_draft_unavailable` / publication `unavailable`（503） | `false` / `true` 并存 | 全部 `false` | 存储恢复是带外改动；同语义同取值 |
| 未知结果：`job_outcome_unknown` / `outcome_unknown`（503） | speech `true` / workbench `false` | 各自按码声明，均在契约里写明"重放是恢复未知结果、不是本字段授权重试" | 两处都不得暗示 `retryable` 授予丢弃身份 |

`contracts/workbench-openapi.yaml` 的分发操作段落改为陈述同一条规则并指向共享文件，
voice-draft / pronunciation 的 6 个非提交 503 响应从 `retryable: const true` 改为
`enum [true, false]`（存储拒绝取 `false`，未知结果取 `true`）。

`code` 仍是 pattern（`^[a-z][a-z0-9_]{2,63}$`），没有变成枚举：跨四协议共享的
`error.code` 不能枚举，按操作收窄放在各自的响应上。

## 修复后的真实响应

同一台 PostgreSQL、同一套生产装配（`create_app(settings, jobs=PublicJobs(store, store))`
+ 真实 ASGI HTTP），同一请求体与同一 Idempotency-Key，逐段只改一个状态位：

收据过期（`expires_at_us` 刚过，实例仍 `ready`）：

```text
503
{"error": {"code": "engine_not_ready", "message": "The engine readiness receipt for the pinned instance has expired; re-register the engine (speechnexus-register-engine --renew-readiness), then send a new request.", "param": null, "request_id": "8e7028a709db4eeda8231b498cb2539f", "retryable": false, "type": "service_unavailable_error"}}
no row written: True
```

收据身份的 `fencing_token` +1（窗口有效、存储健康——审查点名的关键对照）：

```text
503
{"error": {"code": "engine_state_mismatch", "message": "The pinned engine instance no longer matches this request's runtime state; re-register the engine, then send a new request.", "param": null, "request_id": "aaf2fd91958f4257b0a6e3cf4ef1e242", "retryable": false, "type": "service_unavailable_error"}}
no row written: True
```

真实存储故障（撤销 admission 角色对 `workspace_quotas` 的 `SELECT`），收据全程有效：

```text
503
{"error": {"code": "job_unavailable", "message": "Job storage is unavailable.", "param": null, "request_id": "bb18c07c565749fab75b8b9c67b94703", "retryable": false, "type": "service_unavailable_error"}}
no row written: True
```

重签收据后**同一 Idempotency-Key** 重放返回 `202`：见
`tests/integration/test_public_job_admission_http_pg.py::test_an_elapsed_readiness_receipt_is_engine_not_ready_not_a_storage_outage`
（固定用例断言 `202` 且"只预留一次"）。三段与 `fencing_token` 对照的固定用例分别是
该文件里的过期用例、`::test_a_drifted_receipt_identity_is_engine_state_mismatch_not_a_storage_outage`
与 `::test_a_failed_quota_read_keeps_the_storage_answer_while_readiness_is_healthy`。

## 未映射码与结构守护的实测（M1 / M2）

三个兜底通道逐个复现，输出都是 `create_app` 真实装配下渲染的信封：

```text
job_error("engine_retired")            -> 500 code='admission_state_invalid' type='api_error' retryable=False
    msg='The Job API emitted an undeclared error code (engine_retired); this is a deployment defect and the request must not be replayed as-is.'
_raise_admission_rejection('workspace_quota_missing') -> 500 code='admission_state_invalid' type='api_error' retryable=False
    msg='Admission state failed an internal invariant; this is a deployment defect and the request must not be replayed as-is.'
voice-draft submit, KeyError('rejection_code')        -> 500 code='voice_draft_contract_violation' type='api_error' retryable=False
voice-draft submit, PublicJobUnavailableError         -> 503 code='voice_draft_unavailable' type='service_unavailable_error' retryable=False
```

AST 守护（`tests/contract/test_public_job_admission_error_vocabulary.py`，28 用例）不再只断言
"并集非空"，而是逐模块、逐 raise 点断言：

- `ADMISSION_MODULES` 13 个模块（旧清单只有 6 个），新增
  `domain/runtime_intents.py`、`infrastructure/runtime_sql.py`、`owners_sql.py`、
  `runtime_observation_sql.py`、`qualification_admission_postgres.py`、
  `tts_settlement_sql.py`、`builtin_voice_qualification.py`；
- 每个 raise 点的字面量必须 ⊆ 词汇表，跨模块集合等价（新增或漏掉一个码即红）；
- 变量传参的 raise 点必须登记在 `PASSTHROUGH_RAISE_POINTS` 白名单，且其生产者有界
  （`admission_sql.py` 的 `_runtime` / `admit_in_transaction`、`domain/admission.py` 两处、
  `domain/runtime_observations.py` 的 `expired_code`，后者的默认值与调用点单独钉住）；
- 生产侧 `jobs_sql.JobResult(error_code=…)` 的字面量与提交面拒绝集合对齐。

三个反例（各 1 个 hunk，跑红后恢复）：

| 反例 hunk | 结果 |
| --- | --- |
| 在 `domain/runtime_intents.py`（旧清单未扫描）加 `raise RuntimeRejectedError("quadrant_violation")` | 3 项红，含 `domain/runtime_intents.py:_probe_raise:567 ('quadrant_violation') carries ['quadrant_violation']` |
| 在 `domain/runtime_intents.py` 加 `raise RuntimeRejectedError(code)`（未登记白名单） | `test_every_variable_passed_code_is_declared_as_a_passthrough` 红：`domain/runtime_intents.py:_probe_variable:567 (code)` |
| 从 `ADMISSION_MODULES` 删掉 `domain/runtime_intents.py` | `assert 12 == 13` 且词汇表集合等价红 |

## voice-draft 提交面的逐因答复（M3）

`VoiceDrafts` + 真实 ASGI 装配，同一个 `validateVoiceDraftRevision` 请求，逐个注入准入码：

```text
engine_not_ready        503 voice_draft_engine_unavailable  retryable=False
  "The pinned engine's readiness receipt expired; re-register the engine (speechnexus-register-engine --renew-readiness), then resubmit."
engine_state_mismatch   503 voice_draft_engine_unavailable  retryable=False
  "The pinned engine no longer matches this request's runtime state; re-register the engine, then resubmit."
engine_resource_disabled 503 voice_draft_engine_unavailable retryable=False
  "The pinned engine instance, its pool, its template or its recipe approval is not active; restore it, then resubmit."
recipe_not_admitted     503 voice_draft_engine_unavailable  retryable=False
  "The request does not match the recipe approved for the pinned engine; register the engine against the approved recipe, then resubmit."
job_request_expired     422 voice_draft_request_expired     retryable=False
  "The admission deadline for this request elapsed before it was admitted; submit a new request."
admission_state_invalid 500 voice_draft_contract_violation  retryable=False
job_admission_contended 503 voice_draft_admission_contended retryable=True
  "Admission state has not settled; retry the request."
voice_not_published     422 voice_draft_input_unavailable   retryable=False   # 真正的输入事实
```

四件事由此固定：引擎侧原因不回 422（`tests/unit/backend/test_voice_drafts_http.py` 逐因断言
status / code / type / `retryable` / `X-Should-Retry` / message 含恢复命令 / 不含
"Published model or ready input required."）；四个引擎原因的 message 互不相同
（`test_the_five_engine_causes_do_not_share_one_message`）；竞争不被说成存储故障
（`test_a_concurrency_race_is_not_called_a_storage_outage` 断言 message 含 "settled" 且不含
"storage"）；契约 5 个提交操作的 503 枚举补 `voice_draft_admission_contended` /
`voice_draft_engine_unavailable`，`retryable` 与 `X-Should-Retry` 改为 `enum [true,false]`
（经 `_APPROVED_RESPONSE_VOCABULARY_WIDENINGS` 的 27 条精确记录放行）。

## `unreplayable` 文案按码分支（M4）

`file-task-session.ts` 的 `unreplayableCause(status, code)` 与
`unreplayablePriorAttemptClause(code)` 让两处硬编码 413 措辞的 notice 分支：`job_request_expired`
说"准入期限在获准前已过"，413 `job_request_too_large` 才说"请求体超过本部署的接收上限"，
其他码只说类级事实。恢复后的页面（只有 `settled` 记录、没有码）与面板状态段落改用
"协议已拒绝这次请求且不能按原键再次提交"，不再对任何原因断言 413。文案钉在
`file-task-rejection.test.ts`（三个码各一条 + 分派表两个码）与
`FileTaskHost.test.tsx`（真实挂载：422 期限拒绝的 notice、刷新后中性文案、面板段落不含 413）。

## 反例（提交树上各 1 个 hunk，逐条跑红后 `git checkout --` 恢复）

| 反例 hunk | 结果（本次实测） |
| --- | --- |
| `rejection_code()` 的 `except KeyError` 改回 `return "job_unavailable"` | `test_an_unmapped_code_answers_the_declared_defect_envelope` 红 |
| `job_error()` 未声明码改回 `code = "job_unavailable"` | `test_the_error_table_never_invents_a_storage_answer` 红 |
| voice-draft 的 `engine_not_ready` 改回 `_error("input_unavailable")` | `test_engine_side_admission_causes_state_their_own_recovery[engine_not_ready-…]` 红 |
| `file-task-session.ts` 的 notice 改回硬编码 413 措辞 | `FileTaskHost.test.tsx`「gives the user an exit after a 413 body-bound refusal」红（1 failed / 31 passed） |
| 共享 schema 恢复"按『是否重放未知结果』计算"第二定义 | `test_the_shared_field_carries_the_same_dispatch_rule_and_its_two_limits` 红（`restates a deleted rule`） |
| 提交面 503 枚举加一个未登记的码（`voice_draft_capacity`） | `test_every_entry_matches_a_finding_of_the_real_comparison` 红（`assert 26 == 27`：登记项失配，新码无批准） |
| 三个 AST 反例（未扫描模块加字面量 / 变量传参 / 删模块清单项） | 见上一节，逐条红 |

B3 的结构性守护（不是"再列一张表"）：

- `test_the_public_mapping_is_total_over_the_declared_vocabulary`：
  `set(PUBLIC_ADMISSION_CODES) == ADMISSION_REJECTION_CODES - CAPACITY_REJECTION_CODES`，任一侧
  单独增删即红；
- `test_the_acceptance_set_is_derived_from_the_mapping`：
  `_ADMISSION_REJECTION_CODES` 与 `_CREATE_REFUSAL_CODES` 必须等于由映射推导的集合，所以
  "映射加了码、接受集合没加"这一半改状态不可能存在；
- `test_the_error_table_never_invents_a_storage_answer` / `test_no_store_refusal_returns_silently`：
  未声明码在渲染层与提交层都只能得到 500 缺陷信封，取不到 `job_unavailable`；
- `test_every_mapped_public_code_is_declared_on_its_status` 与其反向用例：映射出现的每个
  公开码必须在该状态响应的枚举里，枚举里的每个码也必须是后端真会用该状态发出的码。

## 续期入口与可观测信号（真实运行）

`tests/integration/test_engine_registration_pg.py::test_the_report_and_the_renewal_expose_an_expiring_then_expired_receipt`
在真实 PostgreSQL 上跑 CLI 主函数（`register_main` / `engine_readiness.main`）与真实时钟：

| 步骤 | 观测 |
| --- | --- |
| `--readiness-ttl-seconds 1` 注册后立即报告（`--warn-within-seconds 3600`） | `readiness_state: "expiring"`，`remaining_seconds >= 0`，汇总 `expiring: 1`，退出码 `0` |
| 真实等待 1.2 秒后报告（默认窗口） | `readiness_state: "expired"`，`remaining_seconds < 0`，stderr `error: readiness_expired: instance-reg-1 g1`，退出码 `5` |
| `--renew-readiness` 续期 | `readiness.state: "expired"`、`readiness.renewed: true`，stderr `warning: readiness expired for instance-reg-1 g1 …; re-signed by this run; … 503 engine_not_ready` |
| 续期后报告（`--warn-within-seconds 0`） | `readiness_state: "valid"`，`expires_at_us` 与续期输出一致，退出码 `0` |
| 收据仍有效时再续期 | `("valid", true)`，stderr 为空（只有需要告警时才告警） |
| TTL 4 秒、等 3.2 秒后再续期 | `("expiring", true)`，stderr `warning: readiness expiring for instance-reg-1 g1` |

同样的信号面还有：`deploy/job-startup-gate.py` 的 `pending` 行点名续期与报告；
`deploy/README.md`、`docs/operations/register-engine.md`、`docs/operations/async-job-runbook.md`
说明定时器、退出码与恢复步骤。

### 已知缺陷：`/readyz` 全绿而创建全 503（跟踪 #845）

有意**不**做的：不让 `/readyz` 因 readiness 过期而失败。产品读取与取消在收据过期时必须继续
可用，否则一次漏跑定时器会把整个 API 摘出负载均衡。代价是补偿不完整：

- `api` 的 healthcheck 是 `/readyz`，收据过期时仍 `200`；
- 报告是外部一次性执行的，Compose 不调度；启动闸门只打印一次 `pending`；
- 因此"停止续期 → 所有创建 `503 engine_not_ready`，而 `/readyz` 与容器状态全绿"可复现。

这一缺口不是配置问题，也没有在本 PR 内解决：跟踪 [issue #845]
(<https://github.com/hrygo/SpeechNexus/issues/845>)——为"本部署当前能否准入创建"提供
独立的可轮询信号。在它落地前，宿主机定时器只是缩短窗口，`engine-readiness` 只让外部监控
能看到窗口。

## 契约变更与消费者同步

| 位置 | 变更 |
| --- | --- |
| `contracts/speech-openapi.yaml` `createJob` | 503/422/500 响应改为 `allOf` + `code.enum`；`description` 增加引擎侧拒绝表与 `retryable` 规则；补 `Cache-Control: no-store`、`Retry-After` |
| `contracts/shared/errors.schema.json` | 仅 `ErrorBody.retryable.description`（唯一定义，删除旧的第二定义）；`code` 保持 pattern |
| `contracts/workbench-openapi.yaml` | 分发操作段落陈述同一定义并指向共享文件，删除复述旧第二定义的整段 |
| `scripts/dev-env/check-http-contract-changes.py` | 新增 `_APPROVED_RESPONSE_VOCABULARY_WIDENINGS`（27 条精确 finding 记录，含 `issue`/`from_version`/`to_version`/`reason`），只放行同名同版本对的行 |
| `packages/contracts/**`（`speech.generated.ts`、`source-inventory.json`、各协议 JSON） | `make contracts-generate` 重出，未手改；`contracts-drift` 报 `up to date` |
| `backend/.../infrastructure/public_job_admission.py` | 删除兜底，`_PUBLIC_CODES` 全量映射，未映射 `KeyError` |
| `backend/.../domain/admission.py` | 新增 `CAPACITY_REJECTION_CODES`、`ADMISSION_REJECTION_CODES` 词汇表；`require_live_authority()` 传 `expired_code="readiness_expired"` |
| `backend/.../application/public_jobs.py` | `_ADMISSION_REJECTION_CODES` 补齐 5 个新码，它们以 `JobAdmissionRejectedError` 直达 HTTP |
| `backend/.../interfaces/http/job_errors.py` | 新增 6 个码（4 个引擎侧 503、`job_request_expired` 422、`admission_state_invalid` 500）；`retryable` 仅 `job_outcome_unknown` / `job_capacity_exhausted` / `job_admission_contended` |
| `backend/.../interfaces/http/voice_drafts.py` | submit 面按码分支：容量→429 `capacity`、竞争→503 `contended`、四个引擎原因→503 `engine_*`、期限→422 `request_expired`、内部不变量→500 `admission_defect`、请求自身事实→422 `input_unavailable`；非提交操作收到准入拒绝→500 `contract_violation`；新增 `except KeyError` 专用分支 |
| `backend/.../interfaces/http/job_errors.py` | 未声明码改答 500 `admission_state_invalid`；`retryable` 集合加 `execution_cleanup_pending`；`job_admission_contended` / `engine_resource_disabled` 文案改为逐因表述 |
| `backend/.../domain/admission.py` | 新增 `PUBLIC_ADMISSION_CODES`（17 项，唯一映射来源）与两个泄漏码；`PUBLIC` 表由 domain 持有，infrastructure 只引用 |
| `backend/.../infrastructure/public_jobs_postgres.py` | 第二次尝试的 `version_conflict` 经 `rejection_code()` 报告，不再复用 `job_admission_contended` 死分支 |
| `web/workbench/src/file-workflow/file-task-session.ts` | 拒绝表补 `engine_state_mismatch` / `engine_resource_disabled` / `recipe_not_admitted`（`intent-kept`）、`job_request_expired`（`unreplayable`）、`admission_state_invalid`（`unknown-outcome`）；新增 `unreplayableCause` / `unreplayablePriorAttemptClause` 按 `(status, code)` 分支文案，恢复路径改中性表述 |
| `web/workbench/src/file-workflow/FileTaskPanel.tsx` | `unreplayable` 状态段落去掉 413 断言，只说类级事实 |
| `web/workbench/src/file-workflow/file-task-rejection.test.ts` | 上述 5 行逐条用例 |

## 兼容影响（按消费者逐个核对）

| 消费者 | 可能误处理的点 | 现状 |
| --- | --- | --- |
| workbench `rejectionDisposition`（TS） | 新码不在表里会落到"未列 5xx → `unknown-outcome`"，把可保留的意图花掉 | 已逐行加入；`pnpm test` workbench `1075 passed`，`file-workflow` 456 passed |
| workbench `settlesReplayedIntent` | 新码若被列入会在不成立的时机消耗意图 | 未加入（503 均不消耗；422 `job_request_expired` 走 `unreplayable`，不进入该列表） |
| `voice_drafts` submit 面 | 原来所有非容量准入拒绝借 `voice_draft_unavailable`（可重试 503），修复前的 422 版则把引擎侧原因说成输入错误 | 引擎侧 503 `voice_draft_engine_unavailable`（`retryable: false`，message 给恢复命令）、竞争 503 `voice_draft_admission_contended`（`true`）、期限已过 422 `voice_draft_request_expired`、内部缺陷 500 `voice_draft_contract_violation`；只有请求自身事实才是 422 `voice_draft_input_unavailable` |
| 外部 API 客户端按 `code == "job_unavailable"` 判断"存储故障" | 现在这些原因是独立的码，不再共享该码；按状态码 503 判断"稍后重试"的客户端会看到 `retryable: false` | 契约在 `createJob` 描述与枚举里给出全部码与 retryable，客户端需按码分支；旧客户端看到未知码时按 5xx 处理仍安全（`retryable: false` 抑制盲重试） |
| 监控/告警按 503 计数 | 状态码不变，引擎侧原因仍计入 503 | 无需改；按码区分需要读 body |
| `job_error()` 未知码路径 | 未知码不再回落 `job_unavailable`，改答 500 `admission_state_invalid` 并在 message 点名该码 | 关键码全部在 `_ERRORS` 表内；未声明码意味着某层漏登记，按缺陷暴露而非伪装成存储故障 |

## 验证

| 检查 | 结果（本次实测，2026-11-15） | 基线 `origin/main` = `6a4d7184` |
| --- | --- | --- |
| `uv run --no-sync pytest tests/unit` | 收集 7176，`7173 passed, 3 skipped` | 7146 |
| `uv run --no-sync pytest tests/unit/backend` | 收集 3303，`3303 passed` | 3273 |
| `uv run --no-sync pytest tests/contract` | `3868 passed` | 3815 |
| `uv run --no-sync pytest tests/ci` | `414 passed, 1357 subtests passed` | 404 |
| 三者合并（CI 的 Python 面） | `11455 passed, 3 skipped, 1357 subtests passed` | — |
| `tests/integration`（本改动相关 5 个文件：`public_job_admission_http_pg` / `engine_registration_pg` / `production_admission` / `voice_drafts_pg` / `public_job_atomic_admission_pg`） | `178 passed` | — |
| `make contracts-check` | `contract validation: passed (37 files)` | — |
| `make contracts-generate` + bindings `--check` | `Contract bindings: up to date` | — |
| `CONTRACT_BASE_REF=origin/main make contracts-speech-changes` | `Known HTTP structural checks: passed`（27 条词汇放宽逐条打印为 `approved response vocabulary widening:`） | — |
| `uv run --no-sync ruff format --check .` | `1905 files already formatted` | — |
| `uv run --no-sync ruff check .` | `All checks passed!` | — |
| `uv run --no-sync mypy --platform linux` | `Success: no issues found in 1482 source files` | — |
| `uv run --no-sync lint-imports` | `Contracts: 9 kept, 0 broken.` | — |
| `pnpm typecheck` | admin / workbench / shared / contracts 全部 Done | — |
| `pnpm test` | workbench `1075 passed`（65 files）、admin `151 passed`（19 files）、contracts SDK 用例全绿 | workbench 1070 / admin 151 |
| `pnpm exec prettier --check "web/**/*.{css,html,json,ts,tsx}"` | `All matched files use Prettier code style!` | — |
| `uv.lock` | 与基线一致（所有 Python 命令走 `uv run --no-sync`，`make` 目标运行后核对并 `git checkout --`） | — |

## 未验证项

- 真实引擎的 `/health`：本机没有真实引擎，续期用的是集成测试里的本地 HTTP 桩。
  `--renew-readiness` 走与注册完全相同的探测与事务路径，但探测真实模型不做验证。
- `docker compose --profile engine-readiness run` 的实际容器启动：本机只验证了 Compose 可
  渲染、服务/参数/环境接线存在（`tests/ci/test_reference_deploy_engine_readiness.py` 文本
  守护）与 CLI 本体真实运行，没有构建镜像跑容器。
- 宿主机定时器（cron/systemd）本身：部署侧步骤已写入文档，本机未安装任何定时任务。
- `/readyz` 侧的可轮询准入信号未实现（#845），本 PR 只把缺口写成事实。
- `scripts/dev-env/check-doc-refs.py` 报 `RESULT FAIL`（`refs=1234`、`AMBIGUOUS=67`、
  `MISSING=27`、`OK=1139`）；`MISSING=27` 与本片改动前完全一致，属既有状态，本次未引入
  新失败项，也未修既有项。
- 未做真实多进程并发下的续期/创建竞争压测：`version_conflict` /
  `job_admission_contended` 的映射与 HTTP 面已覆盖，持续并发不在本片。
- 完整 `tests/integration` 在本机不可作门禁：`2214 passed, 315 skipped, 9 failed, 97 errors`
  的失败全部落在需要对象存储、网关或真实引擎的文件（`test_assets*`、
  `test_gateway_*`、`test_workbench_*_probes`），与本改动文件无重叠；本片只以相关 5 个文件
  的 `178 passed` 作为集成证据。
- `_APPROVED_RESPONSE_VOCABULARY_WIDENINGS` 的 27 条记录是把"已审阅的词汇放宽"与"静默放宽"
  分开的机制，但它只证明本 PR 的放宽被逐条审阅；未来协议再放宽时仍需逐条登记，未做自动化
  生成或核对旧记录的巡检。
- voice-draft 面的 503/422/500 分支覆盖的是注入 `JobAdmissionRejectedError` 的 HTTP 面；
  真实引擎准备态（收据过期、实例停用、配方不符）触发这些码的端到端路径在
  `tests/integration/test_voice_drafts_pg.py` 的存储用例之外没有真实引擎可用。
