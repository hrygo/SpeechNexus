# 未发布变更：`v0.1.0` → 当前 `main`

本页是从 `v0.1.0` 升到当前 `main` 的运维清单：照着 `v0.1.0` 的发布说明部署过的环境，
换镜像前后必须完成的动作都在「运维必须做」里，逐条可粘贴。历史发布说明不改，本页随
合并更新。

`scripts/dev-env/check-unreleased-notes.py` 守这一页：自最近发布标签以来新增或修改的
每个迁移文件、每个 `*_grants` 定义函数，都必须在本页有以它开头的一行（表格行或列表项），
正文里顺带提一次不算。缺一条即 CI 红，因此下面的迁移与授权函数清单不是手抄的。

## 运维必须做

### 1. 迁移到 `0063_deletion_targets_blocked`

`v0.1.0` 的目录树停在 `0057_asr_settlement`（发布说明写的 head 是
`0056_debt_probe_live_state_index`，但 tag 里已经有 0057），此后新增六个 revision：

| 迁移 | 做什么 |
| --- | --- |
| `0058_speech_deletion_plans` | 新增 `speech_deletion_plans`：删除预检的不可变计划（300 s TTL），只加存储，不开启任何删除 |
| `0059_speech_deletion_ledger` | 新增 `speech_deletion_ledger`；资产级删除账本新增 `confirmed_deletion` 取值，不再把确认删除记成保留期到期 |
| `0060_left_cancelling_debt_index` | 建 `speech_job_cancellation_fence_age_scan`：Job 已离开 `cancelling` 但仍持容量的债务读按受理时刻范围扫描 |
| `0061_ledger_debt_index` | 建 `speech_job_cancellation_ledger_debt_scan`；**已被 0062 取代**，仍必须按顺序执行（0062 依赖它） |
| `0062_lease_debt_index` | 删掉 0061 的索引，改建 `speech_job_cancellation_lease_debt_scan`：永久取消债的界从「失败历史」换成「仍持有容量的租约」 |
| `0063_deletion_targets_blocked` | 删除账本的 `target_kind` 加 `artifact`、`voice_revision`，`status` 加 `blocked`、`failed`，并新增 `error` JSONB 列（与这两个状态一一对应） |

```bash
docker compose --env-file deploy/compose.env -f deploy/compose.yaml \
  --profile migration run --rm migrate
docker compose --env-file deploy/compose.env -f deploy/compose.yaml \
  exec -T postgres psql -U speechnexus -d speechnexus -Atc \
  'select version_num from speech.alembic_version'
```

第二段的期望输出是 `0063_deletion_targets_blocked`。迁移身份是独立 DDL 身份
（`SPEECHNEXUS_MIGRATION_DATABASE_URL`），不要用 `sn_api`。六个 revision 的
`downgrade` 全部显式 `raise`（计划是删除凭据、账本是删除事实、索引是轮询读的访问路径），
回退没有 `alembic downgrade` 这条路径，只能显式处理数据后手工回收。

核实：**实跑 + 读代码**。2026-11-20 在临时 PostgreSQL 18.6 上以
`SPEECHNEXUS_MIGRATION_DATABASE_URL` 跑 `speechnexus-job-migrate --schema speech`，
`speech.alembic_version` 得到 `0063_deletion_targets_blocked`；六个文件与各自的
`down_revision` 链、`downgrade` 的 `raise` 逐个读过。

### 2. 重跑 `cancellation_debt_reader_grants`：账本授权从 4 列扩到 5 列

该函数（`backend/src/speechnexus_backend/speech/infrastructure/job_cancellation_fence_schema.py:123`）
现在额外授予 `speech_job_cancellation_reconciliations.attempts`。停在旧四列授权上的部署，
债务探针的整条语句以 `42501` 失败，`/readyz` 的 `cancellation_debt` detail 整体退化为
`cancellation_debt: unavailable (permission)`——`/readyz` 仍是 200，Job 创建也不受影响，
所以这件事不会自己冒出来。

只有配置了只读探针身份的环境需要这一条（`SPEECHNEXUS_READINESS_CANCELLATION_DSN`；该键
不在 `deploy/compose.yaml` 的 `environment:` 里，只有自建 override 注入时才生效）。下面的
片段从 `deploy/compose.env` 派生 owner DSN，口令不进命令行；角色名从探针 DSN 里的用户名
还原，所以不会把授权贴到别的身份上：

```bash
set -a; . deploy/compose.env; set +a
export SN_ADMIN_DSN="postgresql://${POSTGRES_USER}:${POSTGRES_PASSWORD}@127.0.0.1:${SPEECHNEXUS_POSTGRES_HOST_PORT:-55432}/${POSTGRES_DB}"

uv run --all-packages python - <<'PY'
import os
from urllib.parse import unquote, urlsplit

import psycopg

from speechnexus_backend.speech.infrastructure.job_cancellation_fence_schema import (
    cancellation_debt_reader_grants,
)

key = "SPEECHNEXUS_READINESS_CANCELLATION_DSN"
dsn = os.environ.get(key, "").strip()
if not dsn:
    print(f"skip {key}: not configured, so no probe identity needs the grant")
else:
    role = unquote(urlsplit(dsn).username or "")
    with psycopg.connect(os.environ["SN_ADMIN_DSN"], autocommit=True) as connection:
        for statement in cancellation_debt_reader_grants("speech", role):
            connection.execute(statement)
    print(f"re-applied cancellation_debt_reader_grants to {role}")
PY
```

`GRANT` 对下一次查询即生效，不需要重启 API。核对是否生效：下一次轮询的 detail 不再是
`unavailable (permission)`，而是账本事实对应的标记

```bash
curl -sS http://127.0.0.1:8000/readyz \
  | python3 -c 'import json, sys; print([c["detail"] for c in json.load(sys.stdin)["checks"] if c["name"] == "cancellation_debt"][0])'
```

核实：**实跑**。2026-11-20 在临时 PostgreSQL 上以真实 `cancellation_debt_reader_grants`
授权后 `REVOKE SELECT (attempts)`，用该角色执行探针同形的
`SELECT job_id, settled_at_us, last_outcome, next_check_at_us, attempts` 得到
`SQLSTATE 42501 permission denied for table speech_job_cancellation_reconciliations`；重跑
该函数后同一条语句成功。本节片段按原文在一处临时 PostgreSQL 上执行，退出码 0。权限到
detail 的映射读自
`backend/src/speechnexus_backend/speech/infrastructure/readiness.py:465`
（`InsufficientPrivilege` → `permission`）。

### 3. 重跑 `deletion_plan_grants` 与 `deletion_execution_grants`：两个新授权组

这两组是 `v0.1.0` 之后新增的
（`backend/src/speechnexus_backend/speech/infrastructure/job_schema.py:65`、
`backend/src/speechnexus_backend/speech/infrastructure/job_schema.py:94`），不属于 Job
writer 的必需面（`backend/src/speechnexus_backend/job_runtime.py:126` 把它们列为
optional）：不重跑时 writer 照常启动，只有删除路由整体回
`503 deletion_plans_not_configured` / `503 deletion_not_configured`
（`backend/src/speechnexus_backend/speech/interfaces/http/deletion_errors.py:60`、
`backend/src/speechnexus_backend/speech/interfaces/http/deletion_errors.py:70` 的消息直接
点名该函数）。要用删除能力的部署必须重跑；不用的部署可以跳过。

```bash
set -a; . deploy/compose.env; set +a
export SN_ADMIN_DSN="postgresql://${POSTGRES_USER}:${POSTGRES_PASSWORD}@127.0.0.1:${SPEECHNEXUS_POSTGRES_HOST_PORT:-55432}/${POSTGRES_DB}"

uv run --all-packages python - <<'PY'
import os
from urllib.parse import unquote, urlsplit

import psycopg

from speechnexus_backend.speech.infrastructure.job_schema import (
    deletion_execution_grants,
    deletion_plan_grants,
)

key = "SPEECHNEXUS_JOB_WRITER_DATABASE_URL"
dsn = os.environ.get(key, "").strip()
if not dsn:
    raise SystemExit(f"{key} is blank, so there is no Job writer identity to grant")
role = unquote(urlsplit(dsn).username or "")
with psycopg.connect(os.environ["SN_ADMIN_DSN"], autocommit=True) as connection:
    for grants in (deletion_plan_grants, deletion_execution_grants):
        for statement in grants("speech", role):
            connection.execute(statement)
        print(f"re-applied {grants.__name__} to {role}")
PY
```

这两组给的是列级 `INSERT`/`UPDATE`（`speech_deletion_ledger`、`speech_content_deletions`、
`speech_asset_events`、`speech_artifacts`、`speech_assets`、`admission_operations` 各按声明
列），外加恰好两张表的 `DELETE`；没有表级 `INSERT`，也没有执行/结算/租约证据的写权。
writer 启动期逐列比对声明面，多授一列会被拒绝。

核实：**实跑 + 读代码**。2026-11-20 在迁移到 `0063` 的临时 PostgreSQL 上，
`provision-job-roles.py --print-statements` 渲染的 249 条语句全部成功执行；同一 schema 上
用 `_verify_privileges` 核对两个 writer 身份：带这两组授权时通过，把这两组置空
（其余不变）时同样通过——即「缺授权只影响删除路由、不影响启动」在真实 schema 上成立。
503 的两个 code 读自 `deletion_errors.py` 与 `domain/deletion.py`；本节片段按原文在同一
临时 PostgreSQL 上执行，退出码 0。

### 4. 可选：新增 `admission-watch` profile 与五个环境变量

`deploy/compose.yaml` 新增可选服务 `admission-watch`（`profiles: ["admission-watch"]`），
按间隔轮询 `GET /v1/nexus/jobs/admission-readiness`，只在判定变化时记一行，连续
`ALERT_AFTER` 次部署级拒绝后打一条告警。它只读、不重启任何东西，用的是准入身份
（`SPEECHNEXUS_JOB_ADMISSION_DATABASE_URL`，Job writer 看不到配额计数），DSN 为空时启动
即失败。基础 `up` 不会带它，进程自身也不带开关默认关闭。

想要巡检就显式启动，并在 `deploy/compose.env` 里给出五个键（`deploy/compose.env.example`
已给出同名默认值）：

```bash
docker compose --env-file deploy/compose.env -f deploy/compose.yaml \
  --profile admission-watch up -d admission-watch
```

| 键（`deploy/compose.env.example` 与 `deploy/compose.yaml` 都已有） | 默认 |
| --- | --- |
| `SPEECHNEXUS_ADMISSION_WATCH_ENABLED` | `true`（profile 启动即开；置 `false` 是 no-op） |
| `SPEECHNEXUS_ADMISSION_WATCH_INTERVAL_SECONDS` | `60` |
| `SPEECHNEXUS_ADMISSION_WATCH_ALERT_AFTER` | `3` |
| `SPEECHNEXUS_ADMISSION_WATCH_WORKSPACES` | `workspace-default` |
| `SPEECHNEXUS_ADMISSION_WATCH_TRANSACTION_TIMEOUT_SECONDS` | `3` |

核实：**读代码 + 文档**。键名在 `deploy/compose.env.example` 与 `deploy/compose.yaml` 的
`admission-watch` 服务里逐个对上，代码侧读 `backend/src/speechnexus_backend/admission_watch_runtime.py`。
**未实测**：该 profile 在真实部署上的行为（间隔、告警阈值在负载下的实际手感）没有跑过。

### 本次变动的授权函数

门禁按文件判定，所以下面九个函数都必须在本页出现；只有标「变」的需要重跑。

| 函数 | 文件 | 变了什么 |
| --- | --- | --- |
| `cancellation_debt_reader_grants` | `job_cancellation_fence_schema.py` | **变**：账本授权 4 列 → 5 列（加 `attempts`），见第 2 条 |
| `job_cancellation_fence_grants` | `job_cancellation_fence_schema.py` | 未变（同文件被改，函数体没动） |
| `cancellation_not_submitted_grants` | `job_cancellation_fence_schema.py` | 未变 |
| `cancellation_reconciliation_grants` | `job_cancellation_fence_schema.py` | 未变 |
| `gateway_cancellation_fence_grants` | `job_cancellation_fence_schema.py` | 未变 |
| `deletion_plan_grants` | `job_schema.py` | **新增**，见第 3 条 |
| `deletion_execution_grants` | `job_schema.py` | **新增**，见第 3 条 |
| `job_runtime_grants` | `job_schema.py` | 未变 |
| `job_dispatch_grants` | `job_schema.py` | 未变 |

## 行为变化

- 语音删除从占位 503 变成三段可用：`POST /v1/nexus/deletion-plans`（预检，返回不可变计划）、
  `POST /v1/nexus/deletions`（确认执行，`202`）、`GET /v1/nexus/deletions/{id}`（读记录）；
  目标 kind 为 `asset`、`artifact`、`voice_revision`、`operation_content`，仍被引用的目标以
  `status=blocked` 加公开错误体回答而不是删除（[#1000](https://github.com/hrygo/SpeechNexus/pull/1000)、
  [#1004](https://github.com/hrygo/SpeechNexus/pull/1004)、
  [#1006](https://github.com/hrygo/SpeechNexus/pull/1006)、
  [#1030](https://github.com/hrygo/SpeechNexus/pull/1030)）。
- 删除的公开错误目录补齐：每个 `code` 带固定 HTTP 状态、`type` 与消息，`_ERRORS[code]` 是唯一
  映射（未收录的 code 在映射处直接失败，而不是编一个状态出来）；缺授权与缺对象存储的三个 503
  明确 `retryable: false`，重试修不了部署缺口（[#1009](https://github.com/hrygo/SpeechNexus/pull/1009)）。
- `GET /v1/nexus/jobs/admission-readiness` 从占位 503 变成真实报告，且与创建准入走同一个判定
  入口：报告说 `admitting` 当且仅当同一请求体会被接受，不再有两份规则各自判定
  （[#1037](https://github.com/hrygo/SpeechNexus/pull/1037)）。
- `cancellation_debt` detail 新增两个标记：`retrying on schedule`（退避中、可再领取）与并列的
  `retrying at attempt ceiling`（账本 `attempts` 到达存储上界 64），逐 Job 明细附
  `[retrying: attempts=N, next in Ts]`；「按计划重试」不再与「worker 在推进」同一句话
  （[#1042](https://github.com/hrygo/SpeechNexus/pull/1042)）。
- 取消拿到网关 `stopped` 证据后当场归还准入容量：Job 结算为 `cancelled` 的同时租约
  `released`、`held_slots`/`held_attempts` 归零，不再默认依赖取消对账 worker
  （[#999](https://github.com/hrygo/SpeechNexus/pull/999)）。
- 取消对账在容量未归还时不再写 `settled`/`exhausted`：账本继续按有界退避领取，直到释放
  真的返回，避免「账本说已关闭 + 租约仍持有」
  （[#1036](https://github.com/hrygo/SpeechNexus/pull/1036)）。
- 永久取消债按「仍持有容量的租约」定界，Job 离开 `cancelling` 之后仍占容量的形态也能被读出
  （[#1010](https://github.com/hrygo/SpeechNexus/pull/1010)、
  [#1007](https://github.com/hrygo/SpeechNexus/pull/1007)）。
- 准入游标预算耗尽时创建按「结果未知」回执（`job_outcome_unknown`），不再让拒签冒成未处理
  异常（[#1022](https://github.com/hrygo/SpeechNexus/pull/1022)）。
- `host_capacity_insufficient` 报出当前 `allocatable`、`held + quarantined` 与三条出路，而不是
  只说「不够」；注册成功时若同一宿主机放不下第二个同形状实例，stderr 提前一条 warning，
  stdout 逐字不变（[#991](https://github.com/hrygo/SpeechNexus/pull/991)、
  [#996](https://github.com/hrygo/SpeechNexus/pull/996)）。
- 对外行为不变、只是收敛了实现：Job kind 的词汇表与泳道归属改为 domain 单一表
  （[#1043](https://github.com/hrygo/SpeechNexus/pull/1043)、
  [#1048](https://github.com/hrygo/SpeechNexus/pull/1048)），取消对账的重试上界收敛到
  `ATTEMPT_CEILING` 单一常量（[#1045](https://github.com/hrygo/SpeechNexus/pull/1045)）。

## 证据

当前 `main` 上覆盖这些变化、且能在 CI 或真实 PostgreSQL 上重跑的检查：

| 变化 | 覆盖它的检查 |
| --- | --- |
| 六个迁移 | `tests/integration/test_left_cancelling_debt_index_pg.py`（0060）、`tests/integration/test_lease_debt_index_pg.py`（0061/0062/0063 的索引取代与 downgrade 拒绝）、`tests/integration/test_permanent_debt_plans_pg.py`（债务读的查询计划）；0058/0059 由下一行三段删除用例在应用过迁移的真实库上建表 |
| 删除预检 / 执行 / 读取三段 | `tests/integration/test_speech_deletion_plans_pg.py`、`tests/integration/test_speech_deletion_execute_pg.py`、`tests/integration/test_speech_deletion_read_pg.py`、`tests/unit/backend/test_speech_deletion_plans.py`、`tests/unit/backend/test_speech_deletion_execution.py`、`tests/unit/backend/test_speech_deletion_read.py`、`tests/contract/test_speech_deletion_codec.py` |
| 删除错误目录与缺授权的两个 503 | `tests/integration/test_speech_deletion_execute_pg.py`、`tests/integration/test_speech_deletion_plans_pg.py`、`tests/integration/test_speech_deletion_read_pg.py` |
| 创建准入与就绪报告同一判定 | `tests/integration/test_public_job_admission_readiness_pg.py`、`tests/contract/test_job_admission_readiness.py` |
| 取消债 detail 的两个新标记 | `tests/integration/test_cancellation_debt_retrying_pg.py`、`tests/integration/test_cancel_failure_slot_pg.py`、`tests/unit/backend/test_cancellation_ledger_bound.py`（上界常量） |
| 已验证停止当场归还容量 | `tests/integration/test_public_job_cancellation_pg.py`、`tests/integration/test_cancellation_settlement_crash_pg.py`、`tests/unit/backend/test_job_dispatch_capacity_release.py` |
| 对账耗尽不记 settled | `tests/integration/test_cancellation_exhausted_release_pg.py` |
| 授权面本身 | `tests/integration/test_role_grant_coverage_pg.py`（真实库上按每个受限身份逐面核对声明的授权面）、`tests/ci/test_compose_minimal_role_provisioning.py`（授权组仍被声明） |
| admission-watch | `tests/integration/test_admission_watch_pg.py`、`tests/integration/test_admission_watch_process_pg.py`、`tests/unit/backend/test_admission_watch.py` |
| 本页不会腐烂 | `scripts/dev-env/check-unreleased-notes.py` 与 `tests/ci/test_unreleased_notes_gate.py` |

未覆盖，逐条点名：

- **升级路径本身没有 CI。**没有任何检查在「`v0.1.0` 的 ACL + 新迁移」上跑一遍：上面的授权
  结论来自读代码与 2026-11-20 的手工实跑，不是门禁。下一次改授权面时，门禁只保证本页点名，
  不保证命令仍然可粘贴。
- **`admission-watch` profile 的真实部署行为未实测**（见第 4 条）。隔离 PG 上的进程级用例
  不覆盖真实间隔、告警阈值与日志量。
- **门禁的判据是名字，不是语义。**它不检查本页写的命令是否还能执行，也不判断函数签名或
  授权列是否与描述一致；`job_cancellation_fence_grants` 这类「同文件变了、函数没变」的条目
  靠本页标注区分。
- **列级授权面从被 import 的常量模块派生**（例如 `deletion_ledger_schema.py` 供
  `deletion_execution_grants` 使用）。这些模块不定义 `*_grants` 函数，它们单独变动不会让
  门禁变红，只会让授权面与文档同时失真。
- **删除路由的执行语义只有隔离 PG 证据。**真实对象存储上的制品删除、以及真实网关上的
  声音版本回收未在本轮验证。