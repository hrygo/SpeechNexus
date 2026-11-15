---
title: "引擎 readiness 过期从 job_unavailable 拆出并给出续期路径（#828）"
status: active
version: "0.1.0"
date: 2026-11-15
---

# 引擎 readiness 过期从 job_unavailable 拆出并给出续期路径（#828）

## 结论

- readiness 收据过期不再被报成 `503 job_unavailable`。过期拒绝走新公开错误码
  `engine_not_ready`（`503 service_unavailable_error`，`retryable: true`），恢复方式是在
  真实 `/health` 探测后重签收据；存储故障保持 `job_unavailable`
  （`retryable: false`），容量不足保持 `429 job_capacity_exhausted`，三者互不吞并。
- 续期入口：`speechnexus-register-engine --renew-readiness`（部署装配为
  `engine-readiness-renew`）。它在收据仍有效时也强制重签，因此定时间隔短于 TTL 即可；
  输出 `readiness.state` / `readiness.renewed`，`expiring` / `expired` 另在 stderr 告警。
- 过期可观测：新增只读报告 `speechnexus-engine-readiness`（部署装配为
  `engine-readiness`），按数据库时钟逐实例给出 `valid` / `expiring` / `expired` /
  `missing` / `invalid` 与剩余秒数，过期时退出码 `5`；`--fail-on-expiring` 让
  `expiring` 也非零。
- 未改变的东西：`/readyz` 仍只做数据库与对象存储探测（readiness 过期时读取与取消必须继续
  可用），admission 判定未放宽，任何拒绝仍不写行。

## 环境与基线

| 项 | 值 |
| --- | --- |
| 主机 | macOS arm64，2026-11-15 |
| 基线 | `origin/main` = `5cd5a5d0`（rebase 前为 `6c886c86`；#833/#836 的 workbench-api 部署单元已并入） |
| worktree | `/private/tmp/sn838`，分支 `fix/828-readiness-expiry` |
| Python / pytest / pnpm | 3.14.9 / 9.1.1 / 12.14.0 |
| PostgreSQL | 一次性容器 `sn838-pg`（`postgres:16.15-alpine`，`127.0.0.1:55460`），测后删除 |
| MySQL | 仓库既有隔离容器 `speechnexus-coordination-mysql`（`127.0.0.1:55434`）；每个用例自建唯一 database/schema |
| DSN 环境变量 | `SPEECHNEXUS_TEST_DATABASE_URL`、`SPEECHNEXUS_TEST_MYSQL_URL` |

## 现状（修复前的机制）

1. `runtime_observations.VerifiedRuntimeReadiness.require_valid()` 在
   `now_us >= expires_at_us` 时抛 `reconciliation_required`；同一个码也用于身份不符。
2. `domain/admission.require_live_authority()` 把它转成
   `AdmissionRejectedError(code)`，`public_jobs._submission_result()` 再转成拒绝结果。
3. `infrastructure/public_job_admission.rejection_code()` 只保留容量类码
   （`job_capacity_exhausted`），其余一律折成 `job_unavailable`。
4. `interfaces/http/job_errors` 把 `job_unavailable` 映射为
   `503 / "Job storage is unavailable." / retryable: false`。

因此「引擎仍 ready、只是收据过期」与「存储读失败」在调用方看来完全一致；#572 验收中首次创建
就是这样被报成存储不可用，而重跑 `engine-register` 后同一请求返回 `202`。收据 TTL 默认
86400 秒，除 `--readiness-ttl-seconds` 外没有任何续期或剩余窗口的观测面。

## 修复后的真实响应

同一台 PostgreSQL、同一套生产装配（`create_app(settings, jobs=PublicJobs(store, store))` +
真实 TCP/ASGI HTTP），同一个请求体与 Idempotency-Key：

过期收据（`expires_at_us` 刚过 1 µs，实例仍 `ready`）：

```text
503
{"error": {"code": "engine_not_ready", "message": "The engine readiness receipt for the pinned instance has expired; register the engine again, then replay the same request.", "param": null, "request_id": "ed084d5328894ea68e6b1e37421f12c3", "retryable": true, "type": "service_unavailable_error"}}
state unchanged: True
```

重签收据后，**同一 Idempotency-Key** 重放：

```text
202 job-ada0ff5a30e340689bb549df81ec018e queued workspace-test
```

真实存储故障（撤销 admission 角色对 `workspace_quotas` 的 `SELECT`），收据全程有效：

```text
503
{"error": {"code": "job_unavailable", "message": "Job storage is unavailable.", "param": null, "request_id": "c73c91b0914743319dca31fbe9ee438e", "retryable": false, "type": "service_unavailable_error"}}
```

三段的固定用例：
`tests/integration/test_public_job_admission_http_pg.py::test_an_elapsed_readiness_receipt_is_engine_not_ready_not_a_storage_outage`
与 `::test_a_failed_quota_read_keeps_the_storage_answer_while_readiness_is_healthy`。

## 反例（提交树上各 1 个 hunk，逐条跑红后 `git checkout --` 恢复）

| 反例 hunk | 结果 |
| --- | --- |
| `rejection_code()` 恢复 `return "job_unavailable"`（#828 原状） | 真实 HTTP 用例红：`assert ('job_unavail…', False) == ('engine_not_…', True)`；契约映射用例 3 项红 |
| `renewed = readiness.expires_at_us <= now_us`（去掉主动续期） | 续期用例红：`assert ('valid', False) == ('valid', True)`；事务单测红 |
| `job_error()` 把 `job_unavailable` 改写成 `engine_not_ready`（两个 503 合一） | 存储故障 HTTP 用例红：`assert ('engine_not_…', True) == ('job_unavail…', False)`；契约方向用例红 |
| `expiry_state()` 恒返回 `valid`（隐藏 expiring/expired） | 报告与实时用例 7 项红（含真实时钟枯竭用例） |

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

有意**不**做的：不让 `/readyz` 因 readiness 过期而失败。产品读取与取消在收据过期时必须继续
可用，否则一次漏跑定时器会把整个 API 摘出负载均衡。

## 契约变更与消费者同步

| 位置 | 变更 |
| --- | --- |
| `contracts/speech-openapi.yaml` `createJob.description` | 声明 `503 engine_not_ready`（`retryable: true`、不写行、重签后同一 key 可放行），并说明它既非 `job_unavailable` 也非 `job_capacity_exhausted`；收据 TTL 属运维截止时间 |
| `packages/contracts/src/speech.generated.ts`、`packages/contracts/source-inventory.json` | 由 `make contracts-generate` 重新生成，未手改；`make contracts-drift` 报 `up to date` |
| `contracts/shared/errors.schema.json` | 未改：`code` 是 pattern（`^[a-z][a-z0-9_]{2,63}$`）而非枚举，新码不需要改 schema |
| `web/workbench/src/file-workflow/file-task-session.ts` | 拒绝表新增 `(503, engine_not_ready) → intent-kept`，并更新表头说明；未加入 `settlesReplayedIntent`（503 不消耗意图） |
| `web/workbench/src/file-workflow/file-task-rejection.test.ts` | 新增该行用例 |
| `backend/.../http/voice_drafts.py` | admission 拒绝分支把 `engine_not_ready` 与 `job_admission_contended` 一起归为可重试 503（`voice_draft_unavailable`），避免落到 422 `input_unavailable` |
| `backend/.../http/job_errors.py` | 新增 `engine_not_ready` 文案；`retryable` 列表加入该码 |

## 验证

| 检查 | 结果 |
| --- | --- |
| `uv run pytest tests/unit/backend -q` | `3294 passed`（rebase 后基线 3273，+21 为本次新增） |
| `uv run pytest tests/unit -q` | `7151 passed, 3 skipped`（跳过项是 Linux 限定的 `prlimit`） |
| `uv run pytest tests/contract -q` | `3825 passed`（基线 3813，+12） |
| `uv run pytest tests/ci -q` | `408 passed, 1357 subtests passed`（rebase 后基线 398，+10） |
| `tests/integration/test_public_job_admission_http_pg.py` + `test_engine_registration_pg.py` | `13 passed`（含新增过期/续期、存储故障方向、真实时钟 expiring/expired 与两种续期） |
| `tests/integration/test_production_admission.py -k authority_windows` | `8 passed`（readiness 窗口分支改为 `readiness_expired`） |
| `make contracts-check` | `contract validation: passed (37 files)` |
| `make contracts-drift` | `Contract bindings: up to date` |
| `uv run ruff format --check .` / `uv run ruff check .` | 通过（1899 files） |
| `uv run mypy --platform linux` | `Success: no issues found in 1477 source files` |
| `uv run lint-imports` | `Contracts: 9 kept, 0 broken` |
| `pnpm typecheck` | admin / workbench / shared / contracts 全部 Done |
| `pnpm test` | workbench `1065 passed`，其余 workspace 同轮通过 |

基线数字以本机实测为准。rebase 前在本机测得 `unit/backend=3245`、`ci=370`，与任务给出的
`3272` / `390` 有差；rebase 到 `5cd5a5d0` 后同两项变为 `3273` / `398`（#833 的 workbench-api
工作多出 28 个用例），差值来源仍未定位（疑与本机与 Linux CI 的用例收集差异有关），因此这里只报
实测值与本轮增量；`tests/contract` 的 `3813` 与实测一致。

## 未验证项

- 真实引擎的 `/health`：本机没有真实引擎，续期用的是集成测试里的本地 HTTP 桩。`--renew-readiness`
  走的是与注册完全相同的探测与事务路径，但探测真实模型不做验证。
- `docker compose --profile engine-readiness run` 的实际容器启动：本机只验证了 Compose 可渲染、
  服务/参数/环境接线存在（`tests/ci/test_reference_deploy_engine_readiness.py` 文本守护）与
  CLI 本体真实运行，没有构建镜像跑容器。
- 宿主机定时器（cron/systemd）本身：部署侧步骤已写入文档，本机未安装任何定时任务。
- `scripts/dev-env/check-doc-refs.py` 报 `RESULT FAIL`（`AMBIGUOUS=64`、`MISSING=27`）；
  在暂存本次全部改动后对比，`refs=1211` 与失败项完全一致，属既有状态，本次未引入新失败项，
  也未修既有项。
- 未做真实多进程并发下的续期/创建竞争压测：resume 后首次创建的单次场景已覆盖，持续并发不在本片。
