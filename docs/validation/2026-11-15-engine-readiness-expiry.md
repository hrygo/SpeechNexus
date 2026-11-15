---
title: "引擎 readiness 过期从 job_unavailable 拆出并给出续期路径（#828）"
status: active
version: "0.2.0"
date: 2026-11-15
---

# 引擎 readiness 过期从 job_unavailable 拆出并给出续期路径（#828）

## 结论

- 创建关闭的 `503 job_unavailable` 兜底已删除。准入拒绝逐码映射到公开错误码，
  `job_unavailable` 只表示"存储真的拒绝了这次读/写"。引擎侧原因各有自己的码：
  `engine_not_ready`（收据过期）、`engine_state_mismatch`（收据身份/世代/拓扑/fencing
  不符或实例行不存在）、`engine_resource_disabled`（实例、池、节点或配方审批未激活）、
  `recipe_not_admitted`（与固定实例已批准配方不一致）。
- `retryable` 的语义被钉住：`true` 只在"同一请求无需带外改动即可成功"时为真。重签收据、
  恢复实例、重新登记都属运维动作，所以上述四个码一律 `retryable: false`，恢复方式写在
  `message` 里；准入侧唯一可重试的是 `job_admission_contended`（版本 CAS / fencing
  竞争，重试同一请求即可）。这条定义写进了
  `contracts/shared/errors.schema.json` 的 `ErrorBody.retryable` 描述，取值本身随之修正。
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

`domain/admission.py` 声明准入词汇表：容量类 `CAPACITY_REJECTION_CODES` 与
`ADMISSION_REJECTION_CODES`（后者含容量类）。`infrastructure/public_job_admission.py`
的 `_PUBLIC_CODES` 是对"非容量准入码"的**全量**映射，`rejection_code()` 先判容量、其余
`return _PUBLIC_CODES[code]`（未映射直接 `KeyError`，没有默认值）。

| 内部准入码 | 触发原因 | 公开码 | 状态 / 类型 | `retryable` | message 指向的恢复 |
| --- | --- | --- | --- | --- | --- |
| `capacity_exhausted`、`quota_exceeded` | 配额/节点/实例槽已满 | `job_capacity_exhausted` | 429 / `rate_limit_error` | `true`（带 `Retry-After`） | 等前一批释放后再重试 |
| `readiness_expired` | 收据窗口已过 | `engine_not_ready` | 503 / `service_unavailable_error` | `false` | `speechnexus-register-engine --renew-readiness`，再发新请求 |
| `reconciliation_required`、`not_found`、`stale_instance_generation` | 收据身份（instance/generation/topology/fencing）不符，或实例/池行不存在 | `engine_state_mismatch` | 503 / `service_unavailable_error` | `false` | 重新登记引擎后再发新请求 |
| `policy_or_resource_disabled` | 实例、池、节点或配方审批不 active | `engine_resource_disabled` | 503 / `service_unavailable_error` | `false` | 恢复实例后再发新请求 |
| `recipe_mismatch` | 请求与固定实例已批准配方不一致 | `recipe_not_admitted` | 503 / `service_unavailable_error` | `false` | 按已批准配方重新登记引擎 |
| `version_conflict`、`stale_fencing_token`、`owner_busy` | 版本 CAS / 池 owner fencing 竞争 | `job_admission_contended` | 503 / `service_unavailable_error` | `true` | 重试同一请求 |
| `lease_not_active` | 该请求自身的准入截止时间已过 | `job_request_expired` | 422 / `invalid_request_error` | `false` | 提交新请求（截止时间烘焙在意图里） |
| `idempotency_conflict`、`intent_conflict` | 同一幂等键/身份属于另一个意图 | `idempotency_conflict` | 409 / `invalid_request_error` | `false` | 重放或查询原意图 |
| `invalid_counter`、`invalid_execution_request`、`invalid_runtime_request` | 时钟或版本计数器越界、内部命令非法 | `admission_state_invalid` | 500 / `api_error` | `false` | 部署缺陷，不得按原样重放 |

`job_unavailable`（503，`service_unavailable_error`）现在只有存储自身拒绝读写时产生；
`job_outcome_unknown`（503，`retryable: true`）仍是连接级失败/结果未知。

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

`contracts/shared/errors.schema.json` 的 `ErrorBody.retryable.description` 追加了取值
定义（本次唯一改动该文件的字段）：

```text
It answers only 'can this same request succeed without an out-of-band change?': a
recovery that needs an operator step -- provisioning the workspace quota, re-signing
an engine readiness receipt, restoring a disabled instance, re-registering against an
approved recipe -- is false even though the recovery is known and repeatable, and the
recovery itself is named by the operation's published code and message rather than by
this field.
```

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

## 反例（提交树上各 1 个 hunk，逐条跑红后 `git checkout --` 恢复）

| 反例 hunk | 结果 |
| --- | --- |
| 从 `createJob` 的 503 枚举里删掉 `engine_not_ready`（B2 判据） | 契约用例 3 项红：`AssertionError: ('engine_not_ready', '503')` / 枚举成员缺项；`contracts-drift` 红：`Generated contract drift: src/speech.generated.ts; run make contracts-generate` |
| 从 `_PUBLIC_CODES` 删掉 `reconciliation_required` 一行（B3 判据） | 集合相等用例红（`{'idempotency…'} == frozenset({'i…'})` 不成立）、逐码用例红、readiness 侧逐码用例红 |
| 把 `reconciliation_required` 改回 `job_unavailable`（只折回一个原因） | 真实 HTTP 的 `fencing_token +1` 用例红、`test_the_storage_answer_is_reserved_for_the_store` 红、逐码用例红 |
| 把 `readiness_expired` 改回 `job_unavailable`（#828 原状） | 过期用例红、`test_the_readiness_neighbours_keep_distinct_answers[readiness_expired-engine_not_ready]` 红 |
| 恢复 `.get(code, "job_unavailable")` 兜底默认值 | `test_an_unmapped_code_fails_loudly_instead_of_becoming_an_outage` 红（`KeyError` 不再发生） |
| `renewed = readiness.expires_at_us <= now_us`（去掉主动续期） | 续期集成用例红、`test_existing_result_renews_a_still_valid_receipt_only_when_asked` 红 |
| `job_error()` 把 `job_unavailable` 改写成 `engine_not_ready`（两个 503 合一） | 存储故障 HTTP 用例红、`test_a_store_that_really_refused_the_read_keeps_the_outage_answer` 红 |
| `expiry_state()` 恒返回 `valid`（隐藏 expiring/expired） | 报告与实时用例 7 项红（含真实时钟枯竭用例） |

B3 的结构性守护（不是"再列一张表"）：

- `test_the_public_mapping_is_total_over_the_declared_vocabulary`：
  `set(_PUBLIC_CODES) == ADMISSION_REJECTION_CODES - CAPACITY_REJECTION_CODES`，任一侧
  单独增删即红；
- `test_every_raised_admission_code_is_declared`：AST 扫描决定/执行准入的 6 个模块里
  `AdmissionRejectedError` / `OwnerRejectedError` / `RuntimeRejectedError` 的字符串字面量，
  必须是词汇表子集，新增 raise 点绕不过映射；
- `test_an_unmapped_code_fails_loudly_instead_of_becoming_an_outage`：未映射码 `KeyError`；
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
| `contracts/shared/errors.schema.json` | 仅 `ErrorBody.retryable.description`（取值定义）；`code` 保持 pattern |
| `packages/contracts/**`（`speech.generated.ts`、`source-inventory.json`、各协议 JSON） | `make contracts-generate` 重出，未手改；`contracts-drift` 报 `up to date` |
| `backend/.../infrastructure/public_job_admission.py` | 删除兜底，`_PUBLIC_CODES` 全量映射，未映射 `KeyError` |
| `backend/.../domain/admission.py` | 新增 `CAPACITY_REJECTION_CODES`、`ADMISSION_REJECTION_CODES` 词汇表；`require_live_authority()` 传 `expired_code="readiness_expired"` |
| `backend/.../application/public_jobs.py` | `_ADMISSION_REJECTION_CODES` 补齐 5 个新码，它们以 `JobAdmissionRejectedError` 直达 HTTP |
| `backend/.../interfaces/http/job_errors.py` | 新增 6 个码（4 个引擎侧 503、`job_request_expired` 422、`admission_state_invalid` 500）；`retryable` 仅 `job_outcome_unknown` / `job_capacity_exhausted` / `job_admission_contended` |
| `backend/.../interfaces/http/voice_drafts.py` | submit 面：容量→`capacity`、`job_admission_contended`→`unavailable`（可重试 503），其余准入码→`input_unavailable`（422），不再借道存储 503 |
| `backend/.../infrastructure/public_jobs_postgres.py` | 第二次尝试的 `version_conflict` 经 `rejection_code()` 报告，不再复用 `job_admission_contended` 死分支 |
| `web/workbench/src/file-workflow/file-task-session.ts` | 拒绝表补 `engine_state_mismatch` / `engine_resource_disabled` / `recipe_not_admitted`（`intent-kept`）、`job_request_expired`（`unreplayable`）、`admission_state_invalid`（`unknown-outcome`）；修正过期的"exactly three retryable codes"注释与失效行号（审查 B4） |
| `web/workbench/src/file-workflow/file-task-rejection.test.ts` | 上述 5 行逐条用例 |

## 兼容影响（按消费者逐个核对）

| 消费者 | 可能误处理的点 | 现状 |
| --- | --- | --- |
| workbench `rejectionDisposition`（TS） | 新码不在表里会落到"未列 5xx → `unknown-outcome`"，把可保留的意图花掉 | 已逐行加入；`pnpm test` workbench `1070 passed`，`file-workflow` 451 passed |
| workbench `settlesReplayedIntent` | 新码若被列入会在不成立的时机消耗意图 | 未加入（503 均不消耗；422 `job_request_expired` 走 `unreplayable`，不进入该列表） |
| `voice_drafts` submit 面 | 原来所有非容量准入拒绝借 `voice_draft_unavailable`（可重试 503）；现除竞争外一律 422 | 已改；消费者可据 422 `input_unavailable` 提示用户修正绑定/实例 |
| 外部 API 客户端按 `code == "job_unavailable"` 判断"存储故障" | 现在这些原因是独立的码，不再共享该码；按状态码 503 判断"稍后重试"的客户端会看到 `retryable: false` | 契约在 `createJob` 描述与枚举里给出全部码与 retryable，客户端需按码分支；旧客户端看到未知码时按 5xx 处理仍安全（`retryable: false` 抑制盲重试） |
| 监控/告警按 503 计数 | 状态码不变，引擎侧原因仍计入 503 | 无需改；按码区分需要读 body |
| `job_error()` 未知码路径 | 未知码仍回落 `job_unavailable`（未识别的内部码只能是存储层） | 保留；准入侧的"未映射"在 `rejection_code()` 已提前 `KeyError` |

## 验证

| 检查 | 结果（本次实测） | 基线 |
| --- | --- | --- |
| `uv run --no-sync pytest tests/unit` | `7164 passed, 3 skipped`（收集 7167） | 7146 → +21 |
| `uv run --no-sync pytest tests/unit/backend` | `3294 passed`（收集） | 3273 → +21 |
| `uv run --no-sync pytest tests/contract` | `3853 passed` | 3815 → +38（readiness 契约 12 + 映射词汇表 26） |
| `uv run --no-sync pytest tests/ci` | `414 passed, 1357 subtests passed` | 404 → +10 |
| `tests/integration/test_public_job_admission_http_pg.py` + `test_engine_registration_pg.py` + `test_production_admission.py` | `86 passed` | — |
| `make contracts-check` | `contract validation: passed (37 files)` | — |
| `make contracts-generate` + drift | `Contract bindings: up to date` | — |
| `uv run --no-sync ruff format --check .` | `1904 files already formatted` | — |
| `uv run --no-sync ruff check .` | `All checks passed!` | — |
| `uv run --no-sync mypy --platform linux` | `Success: no issues found in 1481 source files` | — |
| `uv run --no-sync lint-imports` | `Contracts: 9 kept, 0 broken.` | — |
| `pnpm typecheck` | admin / workbench / shared / contracts 全部 Done | — |
| `pnpm test` | workbench `1070 passed`（65 files）、admin `151 passed` | — |
| `uv.lock` | 与基线一致（`uv run --no-sync`） | — |

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
