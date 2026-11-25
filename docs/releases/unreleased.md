# 未发布变更：`v0.1.0` → 当前 `main`

本页是从 `v0.1.0` 升到当前 `main` 的运维清单：照着 `v0.1.0` 的发布说明部署过的环境，
换镜像前后必须完成的动作都在「运维必须做」里，逐条可粘贴。历史发布说明不改，本页随
合并更新。

`scripts/dev-env/check-unreleased-notes.py` 守这一页：自最近发布标签以来新增或修改的
每个迁移文件、每个 `*_grants` 定义函数，都必须在本页有以它开头的一行（表格行或列表项），
正文里顺带提一次不算。缺一条即 CI 红，因此下面的迁移与授权函数清单不是手抄的。

条目行的写法是固定的：表格行的**首列**以名字开头（`| 名字 | 说明 |`），或列表项以名字开头
（`- 名字 说明`），名字本身可以带反引号。名字放在句子中间、表格的后续列、或代码围栏（包括
「期望输出」块）里都不算条目；门禁变红时会先打印这条写法要求，再逐条点名缺了什么。

这条检查与「`路径:行号` 引用校验」共用 `Docs: path:line references` 作业：PR 的检查列表只有
一个状态，它变红可能是引用失效，也可能是本页漏登记，点进去看是 `check-doc-refs.py` 还是
`check-unreleased-notes.py` 那一步。改发布标签流程（打 tag、改 `git describe` 语义、或不打注
解标签）时要想到它：基线来自 `git describe --tags --abbrev=0`，取不到标签就以 2 退出，不会
被当成「没有变化」。

换镜像这一步不在下面的清单里：发布镜像由 `.github/workflows/release-images.yml` 在发布 tag 上
构建并推送到 GHCR，digest 清单 `speechnexus-<tag>-images.txt` 挂在同一个 GitHub Release 上
（[#1035](https://github.com/hrygo/SpeechNexus/pull/1035)）。没有这份资产的 tag 没有发布镜像，
只能从源码构建；要消费发布镜像就按 `deploy/README.md` 的 Published release images 一节按 digest
固定。

## 运维必须做

### 1. 迁移到 `0066_claim_bucket_table`

#### 部署顺序：迁移（含授权重放）先于新构建

顺序是硬约束，不是建议：先把库迁到 head（当前是 `0068_canonical_event_tables`，其中包含
`0064` 加上的 `bucket_no` 与 `due_at`），重放下节三个身份的 `coordination_jobs` INSERT 授权，
**再**启动新构建。新构建的 Job 写入在 INSERT 里固定提供 `bucket_no` 与 `due_at`
（`backend/src/speechnexus_backend/speech/infrastructure/job_writer_columns.py:29`），
这两列都由 `0064` 添加；写入者不做「列不在就换一种 INSERT」的探测，也不保留第二套
支持的 schema。`due_at` 由两个写入者从 `speech_jobs.created_at_us` 派生成 naive UTC
`TIMESTAMP(6)`，不需要新迁移，也不需要回填新列。

新构建先启动、迁移后跑时，每一次新建 Job 都在 INSERT 处以
`psycopg.errors.UndefinedColumn: column "bucket_no" of relation coordination_jobs
does not exist`（SQLSTATE `42703`）失败：公开创建面把它归为 `storage_rejected`
的拒绝（`backend/src/speechnexus_backend/speech/infrastructure/public_jobs_postgres.py:757`），
协调写入者把它归为 `reconciliation_required`
（`backend/src/speechnexus_backend/speech/infrastructure/jobs_postgres.py:113`）。
两者都不是数据问题，重试和 `recover_create_job` 都绕不过去：语句引用了不存在的列。
授权未重放是同一位置、同为「新建 Job 必失败」的另一种形态——`42501 permission denied
for table coordination_jobs`（见下节），所以迁移与授权重放必须一起早于新构建。迁移完成
之后不要回退到 `0064` 以下再运行新构建，否则同样是这个错误。

`v0.1.0` 的目录树停在 `0057_asr_settlement`（发布说明写的 head 是
`0056_debt_probe_live_state_index`，但 tag 里已经有 0057），此后新增十个 revision：

| 迁移 | 做什么 |
| --- | --- |
| `0058_speech_deletion_plans` | 新增 `speech_deletion_plans`：删除预检的不可变计划（300 s TTL），只加存储，不开启任何删除 |
| `0059_speech_deletion_ledger` | 新增 `speech_deletion_ledger`；资产级删除账本新增 `confirmed_deletion` 取值，不再把确认删除记成保留期到期 |
| `0060_left_cancelling_debt_index` | 建 `speech_job_cancellation_fence_age_scan`：Job 已离开 `cancelling` 但仍持容量的债务读按受理时刻范围扫描 |
| `0061_ledger_debt_index` | 建 `speech_job_cancellation_ledger_debt_scan`；**已被 0062 取代**，仍必须按顺序执行（0062 依赖它） |
| `0062_lease_debt_index` | 删掉 0061 的索引，改建 `speech_job_cancellation_lease_debt_scan`：永久取消债的界从「失败历史」换成「仍持有容量的租约」 |
| `0063_deletion_targets_blocked` | 删除账本的 `target_kind` 加 `artifact`、`voice_revision`，`status` 加 `blocked`、`failed`，并新增 `error` JSONB 列（与这两个状态一一对应） |
| `0064_canonical_jobs_columns` | `coordination_jobs` 加 canonical 列 `bucket_no`、`due_at`、`claim_expires_at`、`last_event_seq` 并按既有 epoch 列回填（`0` → `NULL`、UTC 墙上时间、`bucket_for(job_id)`）；纯加法，无读取者，`downgrade` 只删这四列 |
| `0065_canonical_jobs_reconcile` | 重跑 0064 的回填，覆盖 0064 之后写入、只有草案列的行；`claim_expires_at` 与 `due_at` 是 **fill-if-null**（只在 canonical 列为 `NULL` 时从草案 epoch 列／`speech_jobs.created_at_us` 换算，canonical 一旦写下就是权威，重跑不覆盖在飞租约），`bucket_no` 仍按 `bucket_for(job_id)` 重算；按 `MAX(speech_job_events.event_seq)` 回填 `last_event_seq`（无事件为 `0`）；提交前对账四条不变量（含「每行都有 `bucket_no`」与「草案说已领取的行必须已在 canonical 有租约」）。**不收紧 `bucket_no`**：写入切换（#1176）已让两个写入者在 INSERT 时提供 `bucket_for(job_id)`，但 `NOT NULL` 仍由后续修订落地；`downgrade` 把 `last_event_seq` 归零并防御性清掉 `NOT NULL`，不删列，也不回退 `claim_expires_at`（那可能是在飞租约） |
| `0066_claim_bucket_table` | 建 `coordination_claim_buckets`（`bucket_no` `DECIMAL(38,0)` 主键、`claim_owner_id` `VARCHAR(128)`、`lease_until` `TIMESTAMP(6)`、`version` `DECIMAL(38,0)` NOT NULL，即 `migrations/0003_portable_claim_buckets.sql` 的 canonical 形状），并按 Python 的 `BUCKETS` 常量（256）播种 256 个 `version = 0`、无 owner、无租约的空桶；已存在的桶不重写，重入只补缺失的那些；`downgrade` 只 DROP 这张表 |
| `0067_coordination_keyed_locks` | 建 `coordination_keyed_locks`：协调命名空间的键锁行表（`bucket_no` `DECIMAL(38,0)` 主键、`version` `DECIMAL(38,0)` 行锁许可列、`created_at` `TIMESTAMP(6)`），供 backend 与执行网关两个角色在同一行上取 `FOR UPDATE`；纯建表、不播种，行由第一次用到该桶的事务懒建，`downgrade` 只 DROP 这张表 |
| `0068_canonical_event_tables` | 建 canonical `job_events`（`job_id` `VARCHAR(128)`、`event_seq` `DECIMAL(38,0)`、`event_digest` `VARCHAR(71)`、`payload_ref` `VARCHAR(512)` 可空，主键 `(job_id, event_seq)`，外键指向 `coordination_jobs(job_id)`），并给 `coordination_outbox` 加可空 `delivered_at TIMESTAMP(6)`；**纯加法，无写者、无读者**，草案列 `delivered_at_us` 原样保留，`downgrade` 只删这一列与 `job_events` 表 |
| `0008_runtime_delivery` | 参数标记迁到 `?` 契约后，迁移把唯一的占位符内联成迁移事务时间戳；行为不变，仍给旧 outbox 行补 `unknown` 投递相位 |
| `0023_publication_lifecycle`（视图定义） | 视图里的 `::numeric` 改成标准 `CAST(... AS numeric)`，语义等价；只在重新 bootstrap 时生效，不新增迁移 |

```bash
docker compose --env-file deploy/compose.env -f deploy/compose.yaml \
  --profile migration run --rm migrate
docker compose --env-file deploy/compose.env -f deploy/compose.yaml \
  exec -T postgres psql -U speechnexus -d speechnexus -Atc \
  'select version_num from speech.alembic_version'
```

第二段的期望输出是 `0068_canonical_event_tables`。迁移身份是独立 DDL 身份
（`SPEECHNEXUS_MIGRATION_DATABASE_URL`），不要用 `sn_api`。`0058`–`0063` 六个 revision 的
`downgrade` 全部显式 `raise`（计划是删除凭据、账本是删除事实、索引是轮询读的访问路径），
回退没有 `alembic downgrade` 这条路径，只能显式处理数据后手工回收。`0064` 只删自己加的
canonical 列，`0065` 把 `last_event_seq` 归零（并防御性清掉 `bucket_no` 上可能存在的
`NOT NULL`）而不删列：`last_event_seq` 当前没有任何读取者，`claim_expires_at` 不回退是因为它
可能已是新的认领路径写下的在飞租约，擦掉它等于让 Job 失权。
`0066` 的 `downgrade` 只 DROP `coordination_claim_buckets`：种子是表的内容，回到 0065 时这张表
本该不存在，其余协调对象与行都不动。`0067` 的 `downgrade` 同样只 DROP
`coordination_keyed_locks`：表里的行是锁行本身，没有别的表引用它，也没有需要保留的内容。
`0068` 的 `downgrade` 只删本修订加的两样东西（`coordination_outbox.delivered_at` 列与
`job_events` 表），两列此刻都没有写者，所以回退不丢事实；`delivered_at_us` 的值与
`coordination_jobs` 的行都不动。

`0068` 自己只建对象，但 S1（事件双写）给了 `job_events` 第一个写者，于是多了一条运维动作：
部署新构建之前重放 `job_dispatch_grants`（见第 9 条）。读路径仍留在草案
`speech_job_events`，S2 才切，所以这段时间 `job_events` 有写入者、没有读取者。回退只需重放
回旧授权，影子行可以留着 —— 没有读者。`job_event_grants` 是 S1 要用的声明，
`job_dispatch_grants` 调用它，不另抄一份列清单。

核实：**实跑 + 读代码**。2026-11-20 在临时 PostgreSQL 18.6 上以
`SPEECHNEXUS_MIGRATION_DATABASE_URL` 跑 `speechnexus-job-migrate --schema speech`，
`speech.alembic_version` 得到 `0063_deletion_targets_blocked`；六个文件与各自的
`down_revision` 链、`downgrade` 的 `raise` 逐个读过。

`0065` 的补充核实（2026-11-23，同机 PostgreSQL 18.6）：空库上
`speechnexus-job-migrate --schema sn_0065_headcheck` 后
`alembic_version = 0065_canonical_jobs_reconcile`，`coordination_jobs` 的
`bucket_no`／`due_at`／`claim_expires_at` 为可空、`last_event_seq` 为 NOT NULL；
`tests/integration/test_canonical_jobs_0065_pg.py` 在 `Asia/Shanghai` 会话下实跑，
覆盖 `MAX(event_seq)` 事件头、0064 之后写入窗口行的二次回填、`AT TIME ZONE 'UTC'`
的墙上时间、`downgrade` 回 0064 三类判据。

`0066` 的补充核实（2026-11-23，本机 PostgreSQL 18.6，`SPEECHNEXUS_TEST_DATABASE_URL`）：
空 schema 上 `alembic upgrade head` 后 `coordination_claim_buckets` 为 256 行、桶号恰好
`0..255`、`SUM(version) = 0` 且 `claim_owner_id`／`lease_until` 全为 `NULL`；同一 schema 上
把 `alembic_version` 戳回 `0065` 再 `upgrade head`（`step_below_*` 的形态）只补回被删掉的桶，
已有租约保持原值；`downgrade 0065` 后表消失、`coordination_jobs` 行仍在。
`tests/integration/test_claim_bucket_table_0066_pg.py` 实跑：迁移前 `read_fence`／`claim_batch`
以 `42P01` 失败，迁移后同一条 `claim_batch` 认领到期 Job；授权身份核对
`has_table_privilege`／`has_column_privilege`。

#### 同一部署要重跑 Job 写入/受理/派发三个身份的 `coordination_jobs` INSERT 授权

写入切换 #1176 在两个写入者的 INSERT 里加了 `bucket_no`，并把该列放进
`COORDINATION_JOB_INSERT_COLUMNS`。`job_runtime_grants`、`coordination_runtime_grants`
与 `public_job_admission_grants` 生成的授权语句因此都多了一列。停在旧六列授权上的部署，
**每一次新建 Job 都会以 `42501 permission denied for table coordination_jobs` 失败**
（列级授权缺列时 PostgreSQL 只报表名），公开创建面报 `role_privileges_missing`。迁移本身
不改授权，必须重放这三个身份的函数：

```bash
set -a; . deploy/compose.env; set +a
export SN_ADMIN_DSN='<owner/DDL 身份的 DSN，经批准的秘密渠道提供>'
uv run --all-packages python - <<'PY'
import os
from urllib.parse import unquote, urlsplit

import psycopg

from speechnexus_backend.speech.infrastructure.job_schema import job_runtime_grants
from speechnexus_backend.speech.infrastructure.public_job_admission_grants import (
    public_job_admission_grants,
)

schema = os.environ.get("SN_SCHEMA", "speech")


def role(key: str) -> str:
    parts = urlsplit(os.environ[key])
    name = unquote(parts.username or "")
    if not name:
        raise SystemExit(f"{key} does not name a role")
    return name


statements = [
    *job_runtime_grants(schema, role("SPEECHNEXUS_JOB_WRITER_DATABASE_URL")),
    *job_runtime_grants(schema, role("SPEECHNEXUS_JOB_DISPATCH_DATABASE_URL")),
    *public_job_admission_grants(schema, role("SPEECHNEXUS_JOB_ADMISSION_DATABASE_URL")),
]
with psycopg.connect(os.environ["SN_ADMIN_DSN"], autocommit=True) as connection:
    for statement in statements:
        connection.execute(statement)
print(len(statements), "grant statements replayed")
PY
```

派发身份同时从 `job_runtime_grants` 与 `coordination_runtime_grants` 得到 INSERT 权限，
`GRANT` 是并集，所以重放 `job_runtime_grants` 已覆盖新增列；`coordination_runtime_grants`
的声明也已同步，下一次 `provision-job-roles.py` 会带上它。核实：**实跑 + 读代码**。
2026-11-23 在 `PostgreSQL 18.6`（`SPEECHNEXUS_TEST_DATABASE_URL`）上以
`tests/integration/test_job_writer_bucket_no_pg.py` 分别用写入者身份走 admission-less
fallback、用受理身份走 `create_in_transaction`，两行 `bucket_no` 都等于
`bucket_for(job_id)`；把写入者改回写 `0` 时该用例转红。

### 2. 重跑 `cancellation_debt_reader_grants`：账本授权从 4 列扩到 5 列

该函数（`backend/src/speechnexus_backend/speech/infrastructure/job_cancellation_fence_schema.py:123`）
现在额外授予 `speech_job_cancellation_reconciliations.attempts`。停在旧四列授权上的部署，
债务探针的整条语句以 `42501` 失败，`/readyz` 的 `cancellation_debt` detail 整体退化为
`cancellation_debt: unavailable (permission)`——`/readyz` 仍是 200，Job 创建也不受影响，
所以这件事不会自己冒出来。

只有配置了只读探针身份的环境需要这一条（`SPEECHNEXUS_READINESS_CANCELLATION_DSN`；该键
不在 `deploy/compose.yaml` 的 `environment:` 里，只有自建 override 注入时才生效）。运行前
备好两个 DSN：`SN_ADMIN_DSN`（owner/DDL 身份）与
`SPEECHNEXUS_READINESS_CANCELLATION_DSN`（探针身份）。

第一段只在 compose 部署上有用，口令不进命令行；`<...>` 处按部署替换：

```bash
set -a; . deploy/compose.env; set +a
export SN_ADMIN_DSN="postgresql://${POSTGRES_USER}:${POSTGRES_PASSWORD}@127.0.0.1:${SPEECHNEXUS_POSTGRES_HOST_PORT:-55432}/${POSTGRES_DB}"
export SPEECHNEXUS_READINESS_CANCELLATION_DSN='<探针角色的 DSN，URL 形式>'
```

没有 `deploy/compose.env` 的环境（CI、按[最小部署手册 §6](../operations/compose-minimal.md#6-创建非-owner-运行角色与最小授权)手工预置的库）跳过第一段，直接导出同名的两个变量。第二段是这一条的承诺，逐字执行：角色名从探针 DSN 里的用户名还原，所以不会把授权贴到别的身份上；`SN_SCHEMA` 默认 `speech`，只在与部署不同的 schema 上重放时才需要覆盖。

```bash
uv run --all-packages python - <<'PY'
import os
from urllib.parse import unquote, urlsplit

import psycopg

from speechnexus_backend.speech.infrastructure.job_cancellation_fence_schema import (
    cancellation_debt_reader_grants,
)

schema = os.environ.get("SN_SCHEMA", "speech")
key = "SPEECHNEXUS_READINESS_CANCELLATION_DSN"
dsn = os.environ.get(key, "").strip()
if not dsn:
    print(f"skip {key}: not configured, so no probe identity needs the grant")
else:
    role = unquote(urlsplit(dsn).username or "")
    with psycopg.connect(os.environ["SN_ADMIN_DSN"], autocommit=True) as connection:
        for statement in cancellation_debt_reader_grants(schema, role):
            connection.execute(statement)
    print(f"re-applied cancellation_debt_reader_grants to {role}")
PY
```

`GRANT` 对下一次查询即生效，不需要重启 API。核对是否生效：下一次轮询的 detail 不再是
`unavailable (permission)`，而是账本事实对应的标记

```bash
curl -sS "http://127.0.0.1:${SPEECHNEXUS_API_HOST_PORT:-8000}/readyz" \
  | python3 -c 'import json, sys; print([c["detail"] for c in json.load(sys.stdin)["checks"] if c["name"] == "cancellation_debt"][0])'
```

端口取自 `SPEECHNEXUS_API_HOST_PORT`（默认 `8000`），与 `deploy/compose.yaml` 的 api 端口映射
`127.0.0.1:${SPEECHNEXUS_API_HOST_PORT:-8000}:8000` 同源，改过宿主端口的部署不用改命令。从
`deploy/compose.env` 取值的 shell 先 `set -a; . deploy/compose.env; set +a` 再执行这一段（CI
与已导出该变量的环境跳过）；块本身不含这一行，所以它是逐字可执行的。

核实：**实跑**。2026-11-20 在临时 PostgreSQL 上以真实 `cancellation_debt_reader_grants`
授权后 `REVOKE SELECT (attempts)`，用该角色执行探针同形的
`SELECT job_id, settled_at_us, last_outcome, next_check_at_us, attempts` 得到
`SQLSTATE 42501 permission denied for table speech_job_cancellation_reconciliations`；重跑
该函数后同一条语句成功。权限到 detail 的映射读自
`backend/src/speechnexus_backend/speech/infrastructure/readiness.py:465`
（`InsufficientPrivilege` → `permission`）。

上面第二段与 `curl` 那一段现在有 CI：`tests/integration/test_upgrade_path_pg.py` 在「`v0.1.0`
的四列 ACL + head 迁移」的真库上重放这一条——撤掉 `attempts` 后断言 `42501` 与
`unavailable (permission)`，**在重跑之前**再断言一次仍然是 `42501`，然后执行本节命令原文，
断言读面恢复，最后用本节 `curl` 原文打一个环回 `/readyz` 核对同一句 detail。该用例从本页
读出要执行的块，所以**把命令改坏**（例如把块里的 `connection.execute(statement)` 换成
`pass`，或整段换成 `SELECT 1`）会让它红；换成一条**更强**的授权命令仍会绿——它守的是「照抄
能修好」，不是「字面不许改」。

### 3. 重跑 `deletion_plan_grants` 与 `deletion_execution_grants`：两个新授权组

这两组是 `v0.1.0` 之后新增的
（`backend/src/speechnexus_backend/speech/infrastructure/job_schema.py:65`、
`backend/src/speechnexus_backend/speech/infrastructure/job_schema.py:94`），不属于 Job
writer 的必需面（`backend/src/speechnexus_backend/job_runtime.py:126` 把它们列为
optional）：不重跑时 writer 照常启动，只有删除路由整体回
`503 deletion_plans_not_configured` / `503 deletion_not_configured`
（`backend/src/speechnexus_backend/speech/interfaces/http/deletion_errors.py:60`、
`backend/src/speechnexus_backend/speech/interfaces/http/deletion_errors.py:70` 的消息直接
点名该函数）。要用删除能力的部署必须重跑；不用的部署可以跳过这一条，但「仅需」不等于
「可以不做」：跳过的部署删不了任何东西，删除路由一直回上面那两条 503（`retryable: false`），
直到重跑为止。这两半由
`tests/integration/test_speech_deletion_plans_pg.py` 的
`test_missing_deletion_grants_fail_the_route_not_the_process` 与
`tests/integration/test_speech_deletion_execute_pg.py` 的
`test_missing_execution_grants_fail_the_route_not_the_process` 在真库上钉住：**进程照常启动、
路由才失败**。

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

门禁按文件判定，所以下面二十四个函数都必须在本页出现。**只有标「变」与「新增」的需要动作**：
标「变」的只要配置了对应身份就要重跑；标「新增」的只在要用删除能力的部署上需要（第 3 条）；
标「未变」的只是被同一个文件带到本页，不需要重跑。`canonical_event_grants` 是唯一的例外：
它是新增函数，但**现在没有任何调用点**，部署上不需要重放（见第 1 节的 `0068` 段）。

| 函数 | 文件 | 变了什么 |
| --- | --- | --- |
| `cancellation_debt_reader_grants` | `job_cancellation_fence_schema.py` | **变**：账本授权 4 列 → 5 列（加 `attempts`），见第 2 条 |
| `job_cancellation_fence_grants` | `job_cancellation_fence_schema.py` | 未变（同文件被改，函数体没动） |
| `cancellation_not_submitted_grants` | `job_cancellation_fence_schema.py` | 未变 |
| `cancellation_reconciliation_grants` | `job_cancellation_fence_schema.py` | 未变 |
| `gateway_cancellation_fence_grants` | `job_cancellation_fence_schema.py` | 未变 |
| `deletion_plan_grants` | `job_schema.py` | **新增**，见第 3 条 |
| `deletion_execution_grants` | `job_schema.py` | **新增**，见第 3 条 |
| `job_runtime_grants` | `job_schema.py` | **变**：`coordination_jobs` 的 INSERT 授权随 `COORDINATION_JOB_INSERT_COLUMNS` 自动加 `bucket_no`、`due_at`（函数体未改，见第 1 条） |
| `coordination_runtime_grants` | `coordination_grants.py` | **变**：`coordination_jobs` 的 INSERT 列加 `bucket_no`、`due_at`（见第 1 条）；给 speech 角色 `coordination_claim_buckets` 的 `SELECT` 与 `claim_owner_id`／`lease_until`／`version` 三列 `UPDATE`，不给 `INSERT`／`DELETE`；gateway 角色新增 `coordination_jobs` 的 `UPDATE (claim_version)` 行锁许可（见第 5 条，不写这一列）。迁移后重放一次即可（`provision-job-roles.py` 会带上） |
| `public_job_admission_grants` | `public_job_admission_grants.py` | **变**：`coordination_jobs` 的 INSERT 列加 `bucket_no`、`due_at`，见第 1 条 |
| `binding_disable_admission_grants` | `admission_postgres.py` | 未变（SQL 参数标记迁到 `?`，授权列未动） |
| `tts_voice_admission_grants` | `admission_grants.py` | **变**：追加 `coordination_keyed_lock_grants` 的四条，见第 7 条 |
| `speech_synthesis_reader_grants` | `qualified_speech_facts_postgres.py` | 未变（参数标记迁移） |
| `runtime_delivery_grants` | `runtime_delivery_schema.py` | 未变（参数标记迁移） |
| `audit_reader_grants` | `publication_lifecycle_schema.py` | 未变（同文件，DDL 的 `::` 改为 `CAST`，授权列未动） |
| `binding_lifecycle_grants` | `publication_lifecycle_schema.py` | 未变（同文件，DDL 的 `::` 改为 `CAST`） |
| `binding_publication_grants` | `publication_lifecycle_schema.py` | 未变（同文件，DDL 的 `::` 改为 `CAST`） |
| `settlement_worker_grants` | `settlement_worker_verifier.py` | **变**：末尾追加 `coordination_keyed_lock_grants` 的四条（`USAGE`、`SELECT (bucket_no)`、`INSERT (bucket_no)`、`UPDATE (version)`），见第 8 条 |
| `job_dispatch_grants` | `job_schema.py` | **变**：末尾追加 `coordination_keyed_lock_grants` 的四条（`USAGE`、`SELECT (bucket_no)`、`INSERT (bucket_no)`、`UPDATE (version)`），见第 6 条；S1 起再追加 `job_event_grants` 与 `coordination_jobs` 的 `UPDATE (last_event_seq)`，见第 9 条 |
| `job_event_grants` | `canonical_event_schema.py` | **新增**：`job_events` 的 `USAGE`、按列 `SELECT (event_seq, event_digest, payload_ref)` 与 `INSERT (job_id, event_seq, event_digest, payload_ref)`；S1 起由 `job_dispatch_grants` 调用，见第 9 条 |
| `coordination_keyed_lock_grants` | `coordination_keyed_lock_schema.py` | **新增**，见第 6 条 |
| `gateway_keyed_lock_grants` | `gateway_query_grants.py` | **新增**，见第 6 条 |
| `gateway_query_grants` | `gateway_query_grants.py` | 未变（同文件被改，函数体没动；只读读者的授权面由 `reader_authority.py` 逐列核对，本次没有扩它） |
| `gateway_query_target_grants` | `gateway_query_grants.py` | 未变 |
| `canonical_event_grants` | `canonical_event_schema.py` | **变**：拆成 `job_event_grants`（事件表那两条）加 `coordination_outbox` 的 `INSERT (delivered_at)`／`UPDATE (delivered_at)`；自身仍无事件行的 `UPDATE`/`DELETE`。S1 只用到 `job_event_grants`，outbox 那两条还没有调用点 |

### 5. 重跑 `coordination_runtime_grants`：网关改为锁 canonical Job 行读围栏

取消围栏不再取 PostgreSQL advisory lock。受理事务与网关提交在 `coordination_jobs` 的
**同一行**上取 `SELECT … FOR UPDATE`：网关在读围栏前锁这一行，受理在写围栏前锁同一行，
所以提交要么先于受理提交，要么在等到行锁后看见已提交的围栏并拒绝创建执行。这一行正是
派发认领 CAS 与终态迁移锁的行，锁序仍是 bucket → Job → 围栏/outbox。

`coordination_runtime_grants` 因此给 gateway 角色加了一条
`GRANT UPDATE (claim_version) ON coordination_jobs`：PostgreSQL 要求 `FOR UPDATE` 持有
目标行某一列的 `UPDATE` 权限，`claim_version` 是 fencing 计数列，网关只借它取行锁，不写
（`GATEWAY_UPDATES` 里既有的 `UPDATE (version)` 是同一种行锁许可）。即使误写也只推进 fencing
计数，当前 owner 的 CAS 会失配并在租约到期后被重领——失败关闭；`claim_owner_id`、
`claim_expires_at`、`job_state` 仍被拒，网关不能认领、续租或关闭。
`gateway_cancellation_fence_grants` 没变。

停在旧授权上的部署，网关每次 `create_submit` 读围栏都在
`SELECT job_id FROM coordination_jobs … FOR UPDATE` 处以
`42501 permission denied for table coordination_jobs` 失败：派发器拿不到提交结果，新执行
停止创建——不是「围栏失效后照常派发」，但同样必须重放。重放的是同一组函数，按
`provision-job-roles.py` 的 owner 身份执行；没有 compose 环境时按下面两段手工做，角色名从
DSN 还原，不会贴到别的身份上：

```bash
set -a; . deploy/compose.env; set +a
export SN_ADMIN_DSN="postgresql://${POSTGRES_USER}:${POSTGRES_PASSWORD}@127.0.0.1:${SPEECHNEXUS_POSTGRES_HOST_PORT:-55432}/${POSTGRES_DB}"

uv run --all-packages python - <<'PY'
import os
from urllib.parse import unquote, urlsplit

import psycopg

from speechnexus_backend.speech.infrastructure.coordination_grants import (
    coordination_runtime_grants,
)

schema = os.environ.get("SN_SCHEMA", "speech")
key = "SPEECHNEXUS_JOB_DISPATCH_DATABASE_URL"
dsn = os.environ.get(key, "").strip()
if not dsn:
    raise SystemExit(f"{key} is blank, so there is no dispatch identity to grant")
dispatch = unquote(urlsplit(dsn).username or "")
gateway = os.environ.get("SN_GATEWAY_ROLE", "sn_gateway")
with psycopg.connect(os.environ["SN_ADMIN_DSN"], autocommit=True) as connection:
    for statement in coordination_runtime_grants(
        schema, speech_role=dispatch, gateway_role=gateway
    ):
        connection.execute(statement)
print(f"re-applied coordination_runtime_grants to {dispatch} and {gateway}")
PY
```

核实：**实跑 + 读代码**。2026-11-24 在 PG 18.6 上以真实角色验证：在临时 schema 里重放
`coordination_runtime_grants` 为 `coordination_jobs` 生成的两条网关语句
（`GRANT SELECT` 与 `GRANT UPDATE (claim_version)`）后，与 `cancellation_fence_present` 同形的
`SELECT job_id FROM coordination_jobs WHERE job_id = ? FOR UPDATE` 成功；给 gateway 角色
`REVOKE UPDATE (claim_version)` 后同一条语句以 `42501 permission denied for table
coordination_jobs` 失败；再重放这两条语句后恢复成功。围栏的
写入端由 `test_job_cancellation_fence_pg.py` 的
`test_acceptance_lock_waits_for_submit_and_then_refuses` 在真库上钉住「提交在 Job 行锁上
等待，受理提交后拒绝创建执行」。

### 6. 重跑六个身份的键锁授权：绑定禁用栅栏改用 `coordination_keyed_locks` 行锁

catalog 的绑定互斥此前是 `pg_advisory_xact_lock(hashtextextended('speechnexus:catalog:…'))`，
两侧分别是 backend 与执行网关。它们**是 `speech` schema 里的两个不同角色**，所以替代它的行锁
表落在两侧都已有关系的 `speech`，而不是任何一侧自己的命名空间：只在一侧够得到的表会让互斥
**静默裂成两把**，而两侧依赖的正是这一份排序。这就是 `0067_coordination_keyed_locks` 建出
`coordination_keyed_locks` 的唯一理由。

**本版把两侧一起迁过去了。** 互斥是跨进程的，只能同进：只改一侧会得到「两侧各锁各的」，
而不是「一侧更慢」——

| 侧 | 位置 | 迁移前 | 迁移后 |
| --- | --- | --- | --- |
| backend 禁用栅栏 | `backend/src/speechnexus_backend/speech/infrastructure/binding_disable_barrier.py` | `pg_advisory_xact_lock`，键含 `current_schema()` | `coordination_keyed_locks` 上的行锁 |
| 执行网关提交 | `execution-gateway/src/speechnexus_gateway/infrastructure/binding_disable_guard.py` | 同上 | 同一行 |

两侧都通过 `speechnexus_portable_sql.keyed_lock` 取值，而它列在两侧都允许导入的那一层：

- 键文本：`binding_lock_key(binding_id)` → `speechnexus:catalog:binding:<binding_id>`；
- 行身份：`bucket_for_key(...)` = `sha256(键)[:8] % 1024`，所以表最多 1024 行；
- 表名：`COORDINATION_KEYED_LOCK_TABLE`，唯一的字面量也在同一个模块；
- 语句：`insert_if_absent`（懒建桶行、唯一冲突回滚到保存点）+ `SELECT … FOR UPDATE`。

键文本里**没有 schema**：行身份是「(表所在 schema, 桶号)」，而两侧的池把 `search_path` 都设成
`{schema},pg_catalog,pg_temp`，同一张表由同一个解析得到。把 schema 拼进键文本反而留下一个
静默分裂口（首个 `search_path` 条目不同、但表其实是同一张），却换不来任何额外保证。

**锁序。** 绑定行锁是该事务的**第一把锁**，一直持到提交：

```
coordination_keyed_locks(binding 键)
  → coordination_keyed_locks(voice 键)
  → 准入容量/租约行
  → claim bucket
  → canonical Job
  → public Job / outbox / 取消围栏
```

网关侧在同一位置取同一行，之后才读 `submit_intents` 与 `speech_publication_retirements`；
禁用提交后网关读得到那条 retirement，正是因为写者持着同一行写完它。反序只在一侧改成「先读后
锁」会把串行化变成死锁，所以两侧在同一版里一起换。

**列与授权。** 列名取自 `speechnexus_portable_sql.keyed_lock` 的唯一清单（桶号 + 行锁许可列），
与 workbench 的 `keyed_locks` 是同一组列。两处差异是刻意的，也都在 DDL 里写死：

| 列 | 本表 | workbench `keyed_locks` | 为什么不同 |
| --- | --- | --- | --- |
| `bucket_no` / `version` | `DECIMAL(38,0)`，无 `CHECK` | `numeric(19,0)`，`CHECK (>= 0)` | 与同 schema 的 `coordination_claim_buckets` 一致；桶号由 `bucket_for_key` 算出，越界值不会被取用，只是多一行死数据 |
| `created_at` | `TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP` | 无 | 桶行懒建，记下第一次被用到的时间；没有任何运行角色读写它，所以它不产生授权事实 |

`version` 是**行锁许可列**，不是 CAS 版本：PostgreSQL 的 `FOR UPDATE` 要求行上某列有 `UPDATE`
权限，这一列就是那份许可，没有写入者。

授权面按事实只有一个定义点（`coordination_keyed_lock_schema.coordination_keyed_lock_grants`），
每个入口都调它，不各写一份。取这把锁的身份从两个扩到六个：

| 身份 | 授权入口 | 为什么取这把锁 |
| --- | --- | --- |
| Job writer / dispatcher | `job_dispatch_grants` | 公开 Job 创建与派发写者 |
| `sn_api` | `coordination_runtime_grants` | 同步准入（`PostgresAdmissionTransactions`）与 TTS 恢复在同一事务里先取锁 |
| `sn_job_admission` | `public_job_admission_grants` | 准入事务先取锁 |
| `sn_binding_lifecycle` | `binding_lifecycle_grants` | `model_binding.disable` 先取锁再追加 retirement |
| `sn_operator_attestation` | `operator_attestation_writer_grants` | 运营者认证事务先取锁 |
| `sn_gateway`（执行网关） | `gateway_keyed_lock_grants` | 提交意图创建前取同一行 |

`sn_operator_attestation` 此前**任何表都没有 `UPDATE`**。行锁许可让它多一条
`UPDATE (version)`，而这一列从不被写值，所以「已记录的认证不可被改写」仍然成立。同一个文件里
其余三个授权函数逐字未变，登记在此是因为 `builtin_voice_grants.py` 作为整体被复核过：

| 授权函数 | 本版 |
| --- | --- |
| `builtin_publisher_grants` | **变**：追加 `coordination_keyed_lock_grants` 的四条，见第 7 条 |
| `builtin_source_approval_writer_grants` | **变**：同上，见第 7 条 |
| `qualification_reader_grants` | 未改 |
| `qualification_writer_grants` | **变**：同上，见第 7 条 |
| `operator_attestation_writer_grants` | 多一条 `UPDATE (version)`（行锁许可） |

**启动闸门从「允许」改成「要求」。** 迁移前这张表只登记为「可以存在」，因为还没有调用点用它；
现在缺表或缺授权不再是「少一条授权」，而是**第一条锁语句就 `42501`**。所以：

- Job writer（`job_runtime._verify_privileges`）与准入身份（`_verify_admission_privileges`）
  在启动核对里点名这张表，并要求 `SELECT (bucket_no)` / `INSERT (bucket_no)` /
  `UPDATE (version)` 逐列到位；库没迁移时按 `schema_not_migrated` 判红，而不是报「缺授权」。
- `sn_binding_lifecycle`（`binding_runtime`）、`sn_api`（`speech_runtime.build_speech`）同样在
  启动时核对这一面，走的是同一个 `require_coordination_keyed_lock_grants`。
- `require_coordination_keyed_lock_grants` 先探表存在、再逐列问授权：表不存在时
  `has_column_privilege` 本身会抛错，所以「没迁移」与「缺授权」是两条不同的启动失败。
- 网关侧没有启动闸门：它在第一次提交时取这把行锁，缺表或缺授权以
  `503 control_store_unavailable` 暴露，而不是在启动时点名。这一条是已知的缺口，不是遗漏
  —— 网关的核对需要一条真跑锁再回滚的探针，属于后续切片。

**一处刻意的收窄。** 公开 Job 的结算型查找（`public_jobs_postgres._read` 带 `binding_lock`）
此前一律 `SET TRANSACTION READ ONLY`。只读事务里 PostgreSQL 既禁止 `INSERT`（懒建桶行）也禁止
`SELECT … FOR UPDATE`，所以这一条现在只在**不带锁**时置只读；语句仍是单条 `SELECT`，该事务唯一
可能写入的就是锁自己的桶行。

**重放授权（顺序：先迁移、再授权、最后启动新构建）。** 这六个函数用同一个 `SN_ADMIN_DSN`
（owner/DDL 身份，经批准的秘密渠道提供）：

```bash
set -a; . deploy/compose.env; set +a
export SN_ADMIN_DSN='<owner/DDL 身份的 DSN，经批准的秘密渠道提供>'
uv run --all-packages python - <<'SCRIPT'
import os
from urllib.parse import unquote, urlsplit

import psycopg

from speechnexus_backend.speech.infrastructure.builtin_voice_grants import (
    operator_attestation_writer_grants,
)
from speechnexus_backend.speech.infrastructure.coordination_grants import (
    coordination_runtime_grants,
)
from speechnexus_backend.speech.infrastructure.gateway_query_grants import (
    gateway_keyed_lock_grants,
)
from speechnexus_backend.speech.infrastructure.job_schema import job_dispatch_grants
from speechnexus_backend.speech.infrastructure.public_job_admission_grants import (
    public_job_admission_grants,
)
from speechnexus_backend.speech.infrastructure.publication_lifecycle_schema import (
    binding_lifecycle_grants,
)

schema = os.environ.get("SN_SCHEMA", "speech")


def role(key: str) -> str:
    name = unquote(urlsplit(os.environ[key]).username or "")
    if not name:
        raise SystemExit(f"{key} is blank, so there is no identity to grant")
    return name


statements = [
    *job_dispatch_grants(schema, role("SPEECHNEXUS_JOB_WRITER_DATABASE_URL")),
    *job_dispatch_grants(schema, role("SPEECHNEXUS_JOB_DISPATCH_DATABASE_URL")),
    *coordination_runtime_grants(
        schema,
        speech_role=role("SPEECHNEXUS_API_DATABASE_URL"),
        gateway_role=os.environ.get("SN_GATEWAY_ROLE", "sn_gateway"),
    ),
    *public_job_admission_grants(
        schema, role("SPEECHNEXUS_JOB_ADMISSION_DATABASE_URL")
    ),
    *binding_lifecycle_grants(schema, role("SPEECHNEXUS_BINDING_LIFECYCLE_DATABASE_URL")),
    *operator_attestation_writer_grants(
        schema, role("SPEECHNEXUS_OPERATOR_ATTESTATION_DATABASE_URL")
    ),
    *gateway_keyed_lock_grants(schema, os.environ.get("SN_GATEWAY_ROLE", "sn_gateway")),
]
with psycopg.connect(os.environ["SN_ADMIN_DSN"], autocommit=True) as connection:
    for statement in statements:
        connection.execute(statement)
print(len(statements), "grant statements replayed")
SCRIPT
```

单角色部署里没有对应 DSN 的身份跳过那一段即可；**不要**用 `GRANT ALL` 代替：列级授权是行身份
的一部分（`INSERT` 只能写桶号、`UPDATE` 只能碰许可列）。

核实：**实跑 + 读代码**。2026-11-24 本机 PostgreSQL 18.6（`SPEECHNEXUS_TEST_DATABASE_URL`）：

- `tests/integration/test_coordination_keyed_lock_pg.py` 用两个真实角色在同一键上取锁：第二个
  连接在 `pg_blocking_pids` 里点名等第一个，异键互不等待；撤掉网关的 `UPDATE (version)` 后同一
  段代码以 `42501` 失败而不是静默放行。
- `test_gateway_disable_submit_barrier_pg.py` 是跨角色 oracle：禁用侧用 `binding_lock_key` 取行
  锁并写 retirement，网关提交在 `pg_blocking_pids` 里等它（`wait_event_type=Lock`、
  `wait_event=transactionid`），提交后网关答 `409 policy_or_resource_disabled` 且没有写入
  `submit_intents`；禁用侧事务内读到的正是 `bucket_for_key(binding_lock_key('tts-quality'))` 那
  一行。
- `test_job_cancellation_fence_pg.py::test_acceptance_lock_waits_for_submit_and_then_refuses` 未
  改动照常通过（它是 Job 行锁与取消围栏的同一套排序）。

### 7. 目录身份锁族整族迁到 `coordination_keyed_locks`

catalog 族**其余的 14 个站点**此前仍是
`pg_advisory_xact_lock(hashtextextended('speechnexus:catalog:' || current_schema() || ':…'))`：
3 个绑定键、8 个声音键，以及 3 个 `(kind, object_id)` 通用站点。它们与第 6 条那条绑定互斥是
**同一把逻辑锁**——发布、退役、撤销、来源批准、TTS 准入、探针授权与批准、操作员认证都在它下面
判定「我看到的目录事实仍是最新的」。所以载体也只能是同一张表上的同一行：参与者里有一半在目录
事实**还不存在**时运行（来源批准、探针授权、操作员认证），而 `SELECT … FOR UPDATE` 在行不存在
时两引擎都不锁任何东西——「每处各找一条天然行」换出来的不是弱一点的锁，是静默失效。

| 站点 | 文件 | 键 |
| --- | --- | --- |
| `append_publication`、`retire_publication` | `speech/infrastructure/publications_sql.py` | `(kind, object_id)` |
| `publish_binding_revision` | `speech/infrastructure/publications_postgres.py` | `(kind, object_id)` |
| `publish_builtin_voice` | `builtin_voice_publication.py` | `binding:<id>` → `voice:<id>` |
| `revoke_builtin_voice` | `builtin_voice_revocation.py` | `voice:<id>` |
| `approve_builtin_voice_source` | `builtin_voice_source_approval.py` | `voice:<id>` |
| 探针授权签发 | `qualification_probe_authorizer.py` | `binding:<id>` → `voice:<id>` |
| 探针批准 | `qualification_probe_approval.py` | `binding:<id>` → `voice:<id>` |
| 操作员认证资格 | `operator_attested_qualification.py` | `voice:<id>` |
| `_lock_tts_voice` | `speech/infrastructure/admission_postgres.py` | `voice:<id>` |
| 资格准入 | `speech/infrastructure/qualification_admission_postgres.py` | `voice:<id>` |

**一次提交整族切换，不分批。** 咨询锁与行锁是两个互不相交的载体：同键文本下，持行锁的事务与取
咨询锁的事务**互不等待**（证据见本节末）。所以「先迁一半」得到的不是「一侧更慢」，而是**静默
双锁**：已迁站点之间串行、未迁站点之间串行、两组之间不串行，任何一侧都不报错。14 个站点在这次
提交里一起换，混合载体的窗口不存在。

**取锁点与键文本各只有一个定义点。** 新增的 `speech/infrastructure/catalog_keyed_lock.py` 是
「按目录身份取行锁」的唯一入口（`lock_catalog(kind, object_id)`、`lock_voice(voice_id)`）；绑定
族的入口仍是 `binding_disable_barrier.lock_binding`（执行网关也调它，不能再开第二个），所以
`builtin_voice_publication`、探针授权/批准、操作员认证里的绑定侧都改为调它。键文本只由
`speechnexus_portable_sql.keyed_lock` 拼：

- `catalog_lock_key(kind, object_id)` → `speechnexus:catalog:<kind>:<object_id>`；
- `voice_lock_key(voice_id)` → `speechnexus:catalog:voice:<voice_id>`，
  `binding_lock_key(binding_id)` 同样收敛到 `catalog_lock_key`（三个函数，一份拼接）；
- `kind` 必须是单个小写键片段（`[a-z][a-z0-9_]{0,31}`），`object_id` 仍走 `require_text`。

相对迁移前的键文本，**`current_schema()` 被去掉了**。理由与第 6 条同源：行身份是「(表所在
schema, 桶号)」，而两侧的池把 `search_path` 都设成 `{schema},pg_catalog,pg_temp`；把 schema
拼进键文本只多一个静默分裂口（首个 `search_path` 条目不同、但表其实是同一张），换不来额外保证。
这次键文本变化**不需要**与未迁写者协商：它们根本不与行锁竞争——无论键文本是否相同，咨询锁与
行锁都不互相阻塞（本节末第 4 条实测）。两种载体能共存的唯一形态是整族一次切换，这正是本次提交
做的事。

**锁序不变。** 该行锁是事务的**第一把行锁**，持到提交：

```text
coordination_keyed_locks(binding 键) → coordination_keyed_locks(voice 键)
  → 准入容量/租约行 → claim bucket → canonical Job → public Job / outbox / 取消围栏
```

绑定在前、声音在后，全部参与者一致。同一事务里重复取同一个键不自阻塞（桶行已插、行锁在手，
第二次 `INSERT` 走唯一冲突分支）。三处站点在这把行锁之前还持有一把**非 catalog** 的咨询锁
（`builtin_voice_publication` 的 `builtin-voice-intent`、`builtin_voice_source_approval` 的
`source-approval`、`publications_postgres` 的 `publish-intent`）：咨询锁与行锁不在同一命名空间，
也不参与行锁排序，所以「第一把行锁」这条不变量仍然成立。

**这次没有迁的 catalog 邻居。** `binding_lifecycle_postgres.py:375` 的 `:idempotency:` 咨询锁
留在原处：它在此前就被登记为**删除**目标——唯一索引 + 「先插入」已经替代了它，不是待迁的行锁。
`session-tuning` 与迁移期的咨询锁同样不在本族内。

**授权面。** 这些身份现在都要能取那把行锁，所以 10 个授权函数末尾追加
`coordination_keyed_lock_grants` 的四条（`USAGE`、`SELECT (bucket_no)`、`INSERT (bucket_no)`、
`UPDATE (version)`）；`coordination_keyed_lock_grants` 本身没有改。

| 授权函数 | 文件 | 变了什么 |
| --- | --- | --- |
| `publication_grants` | `publication_schema.py` | **变**：追加键锁四条（发布/退役 CLI 的写者身份） |
| `model_catalog_reader_grants` | `publication_schema.py` | 未变（同文件被改，函数体没动） |
| `voice_publication_grants` | `voice_publication_schema.py` | **变**：追加键锁四条（声音发布身份） |
| `voice_draft_publication_reader_grants` | `voice_publication_schema.py` | 未变（同文件被改，函数体没动） |
| `voice_evidence_recorder_grants` | `voice_publication_schema.py` | 未变（同文件被改，函数体没动） |
| `pronunciation_publication_grants` | `pronunciation_draft_schema.py` | **变**：追加键锁四条（发音发布身份） |
| `pronunciation_draft_grants` | `pronunciation_draft_schema.py` | 未变（同文件被改，函数体没动） |
| `qualification_authorizer_grants` | `qualification_admission_grants.py` | **变**：追加键锁四条（探针授权签发） |
| `qualification_admission_grants` | `qualification_admission_grants.py` | 未变（同文件被改，函数体没动；该身份从 `public_job_admission_grants` 已拿到键锁） |
| `tts_voice_admission_grants` | `admission_grants.py` | **变**：追加键锁四条（TTS 准入的语音侧） |

`builtin_publisher_grants`、`builtin_source_approval_writer_grants`、`qualification_writer_grants`
的三条变更登记在第 6 条的同一张表里。

**启动闸门跟着扩。** 两个身份此前没有这张表的授权，启动核对把「别的表上的任何授权」判成越权，
所以扩授权必须同时扩判据，否则身份直接起不来：

- 发音发布身份（`pronunciation_runtime.verify_publisher_role`）与声音发布身份
  （`job_runtime._verify_publication_surface`，新常量 `_PUBLICATION_DECLARED_RELATIONS`）把
  `coordination_keyed_locks` 登记为**已声明的关系**，再调同一个
  `require_coordination_keyed_lock_grants` 逐列核对面——列级授权下按表判恒为假，所以放行只在
  「已声明」这一侧做一次，判据仍只有一份。漏掉这一步的后果是**身份起不来**：
  `test_public_jobs_http.py::test_lifespan_uses_a_separate_verified_publisher_role` 在旧判据下
  以 `Public Job writer startup rejected` 失败。
- 其余新拿授权的身份（TTS 准入、探针授权/批准、来源批准、内置发布）**没有**新增启动闸门：缺
  授权时会在第一条锁语句上以 `42501` 失败。这是已知缺口，与第 6 条网关侧那条同类。

**重放授权（顺序：先迁移、再授权、最后启动新构建）。**

```bash
uv run python - <<'GRANTS'
import os, psycopg
from speechnexus_backend.speech.infrastructure.admission_grants import tts_voice_admission_grants
from speechnexus_backend.speech.infrastructure.builtin_voice_grants import (
    builtin_publisher_grants,
    builtin_source_approval_writer_grants,
    qualification_writer_grants,
)
from speechnexus_backend.speech.infrastructure.pronunciation_draft_schema import (
    pronunciation_publication_grants,
)
from speechnexus_backend.speech.infrastructure.publication_schema import publication_grants
from speechnexus_backend.speech.infrastructure.qualification_admission_grants import (
    qualification_authorizer_grants,
)
from speechnexus_backend.speech.infrastructure.voice_publication_schema import (
    voice_publication_grants,
)

schema = os.environ["SN_SCHEMA"]
statements = [
    *publication_grants(schema, os.environ["SPEECHNEXUS_PUBLICATION_WRITER_ROLE"]),
    *voice_publication_grants(schema, os.environ["SPEECHNEXUS_VOICE_PUBLISHER_ROLE"]),
    *pronunciation_publication_grants(schema, os.environ["SPEECHNEXUS_PRONUNCIATION_PUBLISHER_ROLE"]),
    *builtin_publisher_grants(schema, os.environ["SPEECHNEXUS_BUILTIN_VOICE_PUBLISHER_ROLE"]),
    *builtin_source_approval_writer_grants(
        schema, os.environ["SPEECHNEXUS_BUILTIN_SOURCE_APPROVAL_ROLE"]
    ),
    *qualification_writer_grants(schema, os.environ["SPEECHNEXUS_BUILTIN_QUALIFICATION_ROLE"]),
    *qualification_authorizer_grants(schema, os.environ["SPEECHNEXUS_QUALIFICATION_AUTHORIZER_ROLE"]),
    *tts_voice_admission_grants(schema, os.environ["SPEECHNEXUS_TTS_ADMISSION_ROLE"]),
]
with psycopg.connect(os.environ["SN_ADMIN_DSN"], autocommit=True) as connection:
    for statement in statements:
        connection.execute(statement)
print(len(statements), "grant statements replayed")
GRANTS
```

角色名按部署实际取值；单角色部署把同一个 login 传进去即可（重复授权是幂等的）。**不要**用
`GRANT ALL` 代替：列级授权是行身份的一部分（`INSERT` 只能写桶号、`UPDATE` 只能碰许可列）。

核实：**实跑 + 读代码**。2026-11-24 本机 PostgreSQL 18.6：

- 真库并发判据（真 schema、真非 owner 角色、真迁移 DDL）：同键第二个会话在 `pg_blocking_pids`
  里点名等持锁方（`wait_event_type=Lock`、`wait_event=transactionid`）；异键、以及同 id 的绑定
  键，在声音键持有期间立刻拿到锁。
- **混合载体不互相阻塞**：持行锁期间，另一会话以同一键文本取 `pg_advisory_xact_lock` 立刻成功
  ——这就是「整族必须一次切换」的实测依据。
- 守卫 `check-portable-sql.py`：`catalog` 族的 `advisory-lock` 站点从 146 条基线降到 132 条，
  `fixed=14`、`new=0`；异常清单同步删掉 7 条、改写 3 条计数。
- 四个跨进程用例从「等咨询锁」改为「等行锁」：`test_tts_durable_pg.py`、
  `test_builtin_voice_qualification.py`、`test_builtin_voice_source_approval_pg.py`、
  `test_binding_publication_cli.py`。注入验证：把 `_lock_tts_voice` 换回咨询锁，
  `test_retirement_waits_and_new_admission_rechecks` 以
  `admission completed before the voice lock was released` 失败；把发布路径上的三处
  `lock_catalog` 全部换回咨询锁，
  `test_publication_waits_for_retirement_and_rejects_old_observation` 以
  `publication did not wait on the binding lock` 失败。

### 8. 重跑 `settlement_worker_grants`：TTS 取消/准入屏障改用 `coordination_keyed_locks` 行锁

同步 TTS 的**准入**与**取消**串行化同一个 `operation_id`。这一对不能在天然行上做：取消行在没有
人取消时不存在，而准入插 `speech_tts_settlement`、取消插 `speech_tts_cancellation`，两侧没有共同
行可锁；`SELECT … FOR UPDATE` 在行不存在时不锁任何东西，而结算扫描只从结算行读 `abort_reason`，
原因没在准入时快照进去就没有补救路径。所以两侧都改成锁
`coordination_keyed_locks` 上该操作的那一行（键文本不变：
`'speechnexus:tts-cancel:' || current_schema() || ':' || <operation_id>`，拼接只在
`tts_cancel_barrier.tts_cancel_lock_key` 一处）。

`settlement_worker_grants` 因此多出 `coordination_keyed_lock_grants` 的四条（`USAGE`、
`SELECT (bucket_no)`、`INSERT (bucket_no)`、`UPDATE (version)`）：取消事务的第一把锁就是这一行。
准入侧不需要新授权——`sn_api` 的键锁面早已由 `coordination_runtime_grants` 给出，本次没有扩它。

停在旧授权上的部署，两条路径的症状不同，但都必须重放：

| 路径 | 现象 |
| --- | --- |
| 结算 worker 启动 | `verify_settlement_worker` 逐列比对时点名 `coordination_keyed_locks` 缺列（或迁移 `0067` 未跑时点名关系不存在），进程拒绝启动 |
| 同步 TTS 取消 | 若绕过启动闸门直接取锁，第一条 `INSERT INTO coordination_keyed_locks` 以 `42501 permission denied for table coordination_keyed_locks` 失败 |

按 `provision-job-roles.py` 的 owner 身份重放一次即可（它调同一个 `settlement_worker_grants`）；
单角色部署直接对 `sn_speech_settlement` 重跑这一组语句：

```bash
python - <<'SCRIPT'
import os
import psycopg

from speechnexus_backend.speech.infrastructure.settlement_worker_verifier import (
    settlement_worker_grants,
)

schema = os.environ.get("SN_SCHEMA", "speech")
with psycopg.connect(os.environ["SN_ADMIN_DSN"], autocommit=True) as connection:
    for statement in settlement_worker_grants(schema, "sn_speech_settlement"):
        connection.execute(statement)
SCRIPT
```

`coordination_keyed_locks` 的列清单只有一个定义点（`coordination_keyed_lock_schema`）：DDL、这套
授权和两个启动闸门都按它核对，所以不要用 `GRANT ALL` 或表级 `GRANT` 代替 —— 列级授权是行身份
的一部分（`INSERT` 只能写桶号，`UPDATE` 只能碰行锁许可列），而表级探针在列级授权下恒为假。

核实：**实跑 + 读代码**。2026-11-25 本机 PostgreSQL（`SPEECHNEXUS_TEST_DATABASE_URL`）：

- `tests/integration/test_tts_durable_pg.py` 里两条 oracle：持有该操作的行锁时，同一操作的准入与
  取消**同时**出现在 `pg_blocking_pids` 里（两个 backend 都等同一个持有者），提交后两侧都落地且
  结算根的 `abort_reason` 是取消原因；持有 A 操作的行锁时，B 操作的取消不等。
- `tests/integration/test_tts_factory_pg.py::test_tts_admission_rechecks_expiry_after_final_lock`
  的「结算锁」变体现在改用该行锁：准入确实等待，授权在等待期间过期后仍按 `SpeechUnavailableError`
  拒绝且不写结算根。
- `tests/ci/test_tts_cancel_keyed_lock.py` 钉住键文本只有一个拼写点、两个站点只调
  `lock_tts_cancel` 且不再出现 `pg_advisory_xact_lock`；注入第二份键前缀或放回 advisory lock
  都会红。

### 9. 重放 `job_dispatch_grants`：事件双写要写 canonical `job_events` 与事件头

S1 起进度迁移与终态迁移、S1b 起 started 迁移（`_mark_running`）在同一笔事务里额外追写一行
canonical `job_events`，并把 `coordination_jobs.last_event_seq` 推进到新序号。需要的权限全部由
`job_dispatch_grants` 声明（第 6 条那个重放脚本的入参没变，直接重跑即可）：`job_event_grants`
给的 `USAGE`、`SELECT (event_seq, event_digest, payload_ref)`、
`INSERT (job_id, event_seq, event_digest, payload_ref)`，以及 `coordination_jobs` 的
`UPDATE (last_event_seq)` —— 只有这一列，`claim_version` 不在此列，事件双写不会推进租约栅栏。
S1b 不新增授权面，也不新增列，所以这一节对 S1b 部署是「无操作」：同样的重放脚本、同样的入参。

漏重放不会挡住进程启动，只在**领取之后第一次 started / 进度 / 终态迁移**处炸：
`psycopg.errors.InsufficientPrivilege: permission denied for table job_events`（SQLSTATE
`42501`），写者把它归成 `DispatchUnavailableError`，Job 状态与草案事件一起回滚，租约到期后
重新可领取。两种情况要分开看：`deploy/job-startup-gate.py` 在启动时核对 `job_events` 的
`INSERT` 与 `last_event_seq` 的 `UPDATE`，门禁变红就是这条漏了；门禁没跑过的部署才会走到
首次写入的 `42501`。

**历史 Job 的 started 只有草案一侧，这个缺口按设计不补。** S1b 之前启动的 Job，其 started
事实是当时只写草案留下的；`adopt_historical_events` 对已有事件的 Job 直接拒绝，一次性回填
不是修补手段。所以在读路径切到 canonical（S2）之前，这批 Job 必须能按名点出来：
`job_dispatch_postgres.started_event_gaps()` 用一句只读查询列出「草案有 started、canonical
侧一条事件都没有」的 Job，带 `no-coordination-row` / `no-canonical-event` 两种成因，运维用
owner DSN 跑它（与 `portable-reconcile.py --dsn` 同口径）。它们不是写入失败：S1b 之后新写
的 started 在同一笔事务里两侧都有，缺一侧会让整笔迁移回滚。

**没有 `coordination_jobs` 行的 Job 被拒绝，而不是跳过。** `job_events.job_id` 的外键指向
`coordination_jobs`，没有父行就没有合法的影子行；跳过会留下「草案有 started、canonical 永远
没有」这种补不回来的缺口。拒绝是类型化的 `DispatchProtocolError`（消息点名 Job），整笔事务
回滚，公开 Job 仍是 `pending`、草案也不多出 started 行。它不会打断以前能派发的 Job：所有候选
语句都从 `coordination_jobs` 出发，这种 Job 根本进不了派发队列（`test_canonical_event_double_write_pg.py`
里有一组对照：同一轮里正常 Job 被领取，没有父行的那个不在候选集里）。

回退：重放旧版 `job_dispatch_grants`（不含这两组授权）即可，已写下的 canonical 影子行留着 ——
没有读者，S2 之前不需要清理。

**已知缺口（S2 之前必须补）**：`job_event_grants` 的列级 `SELECT` 只有
`(event_seq, event_digest, payload_ref)`，而 `claims.STATEMENTS["job_event.read"]` 在
`job_events.job_id` 上过滤。实测（2026-11-25，本机 PostgreSQL 18.6，非属主身份）：以派发身份
原样执行那条读语句得到 `psycopg.errors.InsufficientPrivilege: permission denied for table
job_events`（`42501`）。S2 是第一次真正使用那条语句的阶段，所以把 `job_id` 补进授予面与读翻转
必须在同一提交里，否则新读路径一上线就以 `42501` 失败。

核实：**实跑**（2026-11-25，本机 PostgreSQL 18.6，非属主身份，`SPEECHNEXUS_TEST_DATABASE_URL`）：
`tests/integration/test_canonical_event_double_write_pg.py` 以真实授权身份跑
`mark_running` → `record_progress` → `finish`，断言 canonical 序号从 1 起**密**、无缺口
（started 在内），摘要按草案行字段用 canonical 序号重算后逐行相等、`payload_ref` 为 `NULL`，
并在 `job_events` 被改名后确认公开 Job、草案事件与事件头都不前进。**未实测**：真实部署上漏
重放的告警先后（启动门禁 vs 首次写入 42501）；真实生产库里是否真的存在只有公开行的 Job
（这一轮只证明它进不了派发队列，没有查过任何生产库）。

## 边界澄清（行为未变）

- 单次合成的**入口声明**上限是 4096 字符（`MAX_SPEECH_TEXT_CHARS`）：同步入口超界 `400 invalid_request`（带 `param: input`），异步 Job 对 **>4096** 的请求体答 `422 unsupported_parameter`（**异步只保证 `error.code`，`param` 为 `null`**）；不切分、不截断，更长的正文由调用方分片。
- 入口声明不等于可执行上限：正文还要满足 planner 与 binding 的更严限制，以更严者为准。**planner 上界 400 对所有 binding 生效且调用方无法调高**，因此同步入口 401–4096 一律被拒（`422 unsupported_parameter`，`param: input`）；CustomVoice 的 binding 400 codepoints 只是更早一层。planner 上界与契约声明之间的这段差另见 [#1073](https://github.com/hrygo/SpeechNexus/issues/1073)。

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
- 事件双写（S1）：`record_progress` 与 `finish` 在同一笔事务里额外写一行 canonical
  `job_events`，`payload_ref` 恒为 `NULL`，canonical 序号取 `coordination_jobs.last_event_seq + 1`
  （不是草案的 `MAX(event_seq) + 1`）；事件摘要按 canonical 序号计算，因此能与草案行对上。
  **读路径、公开响应、SSE 序号都不变**，canonical 行目前没有读取者。
- started 事件纳入双写（S1b）：`mark_running` 在同一笔事务里也追写 canonical `job_events`，
  `kind` 取草案 started 行的默认值 `lifecycle`（摘要覆盖 `kind`，两侧必须同一个字面量）。
  于是一次完整生命周期的 canonical 序号是**从 1 起连续无缺口**的 —— 之前缺的正是 started，
  读翻转之后 SSE 续读会从进度直接跳到终态。`claim_version` 不推进（它是租约栅栏）。
  没有 `coordination_jobs` 行的 Job 在这里被类型化拒绝、整笔回滚，而不是「只写草案」：
  那种 Job 也进不了派发队列，所以拒绝不会打断任何以前能派发的 Job。**对外行为不变**。
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
| 第 2 条的升级重放 | `tests/integration/test_upgrade_path_pg.py`（真库上构造 v0.1.0 的四列 ACL，重放本节命令原文，并断言未重跑前仍然是坏的） |
| `0068` 的两件加法 | `tests/integration/test_canonical_event_tables_0068_pg.py`：真库上逐列核对 `job_events`（类型、NULL 性、主键、外键）与 `coordination_outbox.delivered_at` 的 `timestamp(6)`，把 `alembic_version` 戳回 `0067` 再 `upgrade head` 与直接重跑 DDL 两遍都不报错且已有事件行不丢，`downgrade` 只删本修订的两样东西，`canonical_event_grants` 按列核对到「事件表没有 UPDATE/DELETE」 |
| 事件双写 S1/S1b | `tests/integration/test_canonical_event_double_write_pg.py`：真库、真实授权身份跑 `mark_running` → `record_progress` → `finish`，断言 canonical 序号从 1 起密、逐行摘要重算相等、`payload_ref` 为 `NULL`；`job_events` 改名后 started / 进度 / 终态三条路径都不留半成品；没有 `coordination_jobs` 行的 Job 被拒绝且同一轮的对照 Job 仍被领取；`started_event_gaps` 按名点出历史缺口 |
| 本页不会腐烂 | `scripts/dev-env/check-unreleased-notes.py` 与 `tests/ci/test_unreleased_notes_gate.py` |

未覆盖，逐条点名：

- **升级路径只有第 2 条有 CI。**`tests/integration/test_upgrade_path_pg.py` 在「`v0.1.0`
  的四列 ACL + head 迁移」上重放第 2 条：撤掉 `attempts` 后读到 `42501` 与
  `unavailable (permission)`，未重跑前再断言一次仍然坏，执行本节命令原文后恢复。第 3 条
  「缺授权只影响路由、不影响启动」由两个删除用例逐组钉住，但**不是**「旧 ACL + 新迁移」这个
  组合形态；第 1 条的 **compose 命令与 `version_num` 核对**仍只有手工实跑——**迁移链本身有
  CI**（`tests/integration/test_lease_debt_index_pg.py` 把链跑到 `0063_deletion_targets_blocked`，
  `tests/integration/test_asr_settlement_migration_pg.py` 断言 0057–0059 拒绝 `downgrade`）
  ——只是没有测试在容器里跑那两条 compose 命令并读 `speech.alembic_version`。
  `check-unreleased-notes.py` 只保证本页点名，不保证这三处命令仍然可粘贴。
- **`admission-watch` profile 的真实部署行为未实测**（见第 4 条）。隔离 PG 上的进程级用例
  不覆盖真实间隔、告警阈值与日志量。
- **门禁的判据是名字，不是语义。**它不检查本页写的命令是否还能执行，也不判断函数签名或
  授权列是否与描述一致；`job_cancellation_fence_grants` 这类「同文件变了、函数没变」的条目
  靠本页标注区分。条目行只认表格首列与列表项，名字写在表格后续列或代码围栏里会红——这是
  判据的取舍（开头已写明写法），不是遗漏。
- **列级授权面从被 import 的常量模块派生**（例如 `deletion_ledger_schema.py` 供
  `deletion_execution_grants` 使用）。这些模块不定义 `*_grants` 函数，它们单独变动不会让
  门禁变红，只会让授权面与文档同时失真。
- **删除路由的执行语义只有隔离 PG 证据。**真实对象存储上的制品删除、以及真实网关上的
  声音版本回收未在本轮验证。