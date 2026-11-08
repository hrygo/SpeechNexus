---
title: "参考部署异步 Job 的六项前置与自检"
status: active
version: "0.1.0"
date: 2026-11-08
---

# 参考部署异步 Job 的六项前置与自检

本文给出参考部署（`deploy/compose.yaml`）从零到**能够提交并跑完一次异步 Job** 的步骤清单。
启用 `job-dispatch` profile（[#571](https://github.com/hrygo/SpeechNexus/pull/571)）只是让派发
进程进入拓扑；创建、迁移、授权、引擎注册、结果桶和网络命名空间仍是六项独立前置，任一缺失
都表现为 Job 停在 `queued`/`running` 而没有面向调用方的错误。

配套的只读自检是
[`scripts/dev-env/check-async-job-prereqs.py`](../../scripts/dev-env/check-async-job-prereqs.py)：
它按本文的六项逐条判定，FAIL 时点名缺哪一项。本手册的每条命令都标注了执行状态；未在本机
验证的命令一律注明原因，见[第 13 节](#13-本机验证记录与未验证项)。

本文不改变能力结论：**满足六项前置不等于异步 Job 已验收**。真实模型质量、GPU 容量、物理
多节点、内容网络与最终 Gate 仍按[文档索引](../README.md)分别取证。

## 1. 六项前置速查

| 编号 | 前置 | 缺失时的可观察症状 | 缺它的 Job 表现 |
| --- | --- | --- | --- |
| 1 | API 的 `SPEECHNEXUS_JOB_WRITER_DATABASE_URL` 与 `SPEECHNEXUS_JOB_ADMISSION_DATABASE_URL` | `POST /v1/nexus/jobs` 返回 `503 job_unavailable` | 没有 Job 可被 claim |
| 2 | 迁移到唯一 head（`migration` profile，`speechnexus-job-migrate --schema speech`） | Job/准入关系不存在，迁移版本表为空 | 创建失败或 worker 启动即退出 |
| 3 | 派发角色与五组 grant（启用 `voice_validation` 再加两张 `SELECT`） | worker 启动角色校验失败，`restart: unless-stopped` 下形成重启循环 | 不领取租约，或先 claim 后失败 |
| 4 | 真实引擎与已注册 `ready` 实例（`engine-registration` profile） | 创建返回 `429 job_capacity_exhausted`，或提交必然失败 | 有 Job 但没有可执行实例 |
| 5 | gateway 的 `result_bucket` 存在、被 `minio-init` 创建并启用版本化，且与 env 一致 | claim 之后首次结果发布失败 | 引擎已执行，结果读不回 |
| 6 | `network_mode: service:gateway` 下的启动顺序与网关重建后的 worker 重建 | worker 命名空间失效或指向旧 gateway | 进程在，网关调用全部失败 |

## 2. `up -d`/`--wait` 成功 ≠ 就绪

`docker compose ... up -d --wait` 只等**已定义健康检查**的服务。参考部署里
`job-dispatch-worker` 没有 healthcheck，`--wait` 对它没有可等待的探针，因此：

- `up -d`、`--wait` 返回 0 只表示容器被创建；
- DSN 为空或授权缺失时 worker 在领取租约前退出，`restart: unless-stopped` 让它反复重启，
  而 Compose 仍报告成功；
- API 的 Job 身份校验发生在**启动期**，通过 `/readyz` 也不代表 `/v1/nexus/jobs` 可用
  （`/readyz` 只做数据库 `SELECT 1` 与对象存储健康 URL）。

因此"就绪"的判据是下面几条同时成立，而不是某条命令的退出码：

```bash
docker compose --env-file deploy/compose.env -f deploy/compose.yaml ps
```
```bash
docker compose --env-file deploy/compose.env -f deploy/compose.yaml \
  ps -q job-dispatch-worker | xargs docker inspect --format '{{.RestartCount}}'
```
```bash
docker compose --env-file deploy/compose.env -f deploy/compose.yaml \
  logs --tail=50 job-dispatch-worker
```
```bash
curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8000/readyz
```
```bash
curl -sS http://127.0.0.1:8000/v1/nexus/jobs?limit=1
```

预期：`ps` 中 worker 为 `Up`；`RestartCount` 为 `0`（非 0 说明它在重启循环里）；`logs`
每轮输出一行
`{"event":"job_dispatch_batch",...}`（只有计数，没有命令或凭据）；`/readyz` 为 `200`；
列表接口返回 `200`（空页也是 `200`，只有存储未装配才是 `503 job_unavailable`）。
上述判据都需要运行中的参考部署，**本片未在本机执行**（第 13 节）；其中重启次数与网络
命名空间的读取逻辑由自检脚本覆盖，日志与列表判据只能在实际部署上取证。

## 3. 第 0 步：基础拓扑

先按[最小 Compose 部署](compose-minimal.md) 第 0–8 节完成环境文件、私有配置、迁移、
非 owner 角色、`api`/`gateway` 探活和一次性引擎注册。本节只列与异步 Job 直接相关的
解析验证，避免重复那份手册。

确认 profile 解析出预期服务集合（不需要 Docker daemon，也不需要先启动容器）：

```bash
docker compose --env-file deploy/compose.env.example -f deploy/compose.yaml \
  --profile job-dispatch config --services | sort
```

预期输出（顺序可不同）：

```text
api
gateway
job-dispatch-worker
minio
minio-init
postgres
```

基础集合（不带 profile）是 `api`、`gateway`、`minio`、`minio-init`、`postgres`，
`migrate`、`engine-register`、`job-dispatch-worker` 都不在其中。

## 4. 第 1 步：前置 1 —— API 的 Job writer 与 admission 身份

创建 Job 需要两个**独立**的非 owner 身份，且必须指向同一服务器和数据库：writer 负责
Job/事件/提交写入，admission 负责原子准入的配额、租约与协调写入。只配 writer 时创建仍
返回 `503 job_unavailable`；两个 DSN 指向不同数据库时 API 启动即拒绝。

下面三个 DSN 一次生成并写回环境文件。角色名固定为 `sn_job_writer`、`sn_job_admission`、
`sn_job_dispatch`，口令随机生成且**不打印**；服务器、端口和数据库名沿用
`SPEECHNEXUS_API_DATABASE_URL`，因此改了数据库名也不会漂移。

> 本机验证：对 `deploy/compose.env` 的副本执行过，输出 `job DSNs written for roles: ...`，
> 文件权限为 `0600`。

```bash
python3 - <<'PY'
import re
import secrets
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

path = Path("deploy/compose.env")
text = path.read_text()
api = re.search(r"(?m)^SPEECHNEXUS_API_DATABASE_URL=(.+)$", text)
if api is None:
    raise SystemExit("SPEECHNEXUS_API_DATABASE_URL missing; run the baseline environment step first")
base = urlsplit(api.group(1).strip())
if not base.hostname:
    raise SystemExit("SPEECHNEXUS_API_DATABASE_URL has no host")

targets = {
    "SPEECHNEXUS_JOB_WRITER_DATABASE_URL": "sn_job_writer",
    "SPEECHNEXUS_JOB_ADMISSION_DATABASE_URL": "sn_job_admission",
    "SPEECHNEXUS_JOB_DISPATCH_DATABASE_URL": "sn_job_dispatch",
}
netloc = base.netloc.rsplit("@", 1)[-1]
for key, user in targets.items():
    password = secrets.token_urlsafe(32)
    dsn = urlunsplit((base.scheme, f"{user}:{password}@{netloc}", base.path, "", ""))
    pattern = rf"(?m)^{re.escape(key)}=.*$"
    if re.search(pattern, text) is None:
        raise SystemExit(f"missing {key} in deploy/compose.env")
    text = re.sub(pattern, f"{key}={dsn}", text, count=1)
path.write_text(text)
path.chmod(0o600)
print("job DSNs written for roles: sn_job_writer sn_job_admission sn_job_dispatch (values not printed)")
PY
```

写完后让 API 重新创建容器以加载新环境（`up -d` 检测到环境变化会重建 `api`）：

```bash
docker compose --env-file deploy/compose.env -f deploy/compose.yaml up -d api
```

预期 `api` 为 `Up (healthy)`，且日志没有 Job 身份校验错误：

```bash
docker compose --env-file deploy/compose.env -f deploy/compose.yaml \
  logs --tail=30 api
```

失败时的症状：

| 症状 | 原因 |
| --- | --- |
| `POST /v1/nexus/jobs` 返回 `503 job_unavailable` | writer 或 admission DSN 仍为空，或 `api` 未重建 |
| API 反复重启，日志点名 `authority` / `permission` | 角色权限面与声明不符（见第 5 节，授权未安装或授予了更宽权限） |
| 只配 writer 就能创建、但返回 5xx | 缺少 admission 同库身份；不要用 writer 顶替 |

命令执行状态：DSN 生成脚本已在本机对 env 副本执行；`up -d api`、日志检查与 `/readyz`
**未在本机验证**（本片不启动完整参考部署，见第 13 节）。

## 5. 第 2 步：前置 2 —— 迁移

迁移必须用独立 DDL 身份执行，`migration` profile 只执行一次，不随后续 `up` 启动：

```bash
docker compose --env-file deploy/compose.env -f deploy/compose.yaml \
  --profile migration run --rm migrate
```

预期退出码 `0`，容器内迁移日志**可能为空**。随后核对版本表与 Job 关系：

```bash
docker exec speechnexus-postgres-1 psql -U speechnexus -d speechnexus -Atc \
  'select version_num from speech.alembic_version'
```

预期是**一行非空版本号**，且与所用发布制品的唯一 head 一致。在仓库当前 head 上实测为
`0048_voice_evidence_scan_index`；不要把该值当作固定目标，升级后以发布制品的 head 为准。

同一入口也可以不经过 Compose 直接执行（本机已对一次性 PostgreSQL 16.15 实例验证）：

```bash
SPEECHNEXUS_MIGRATION_DATABASE_URL='postgresql://speechnexus:<owner-password>@127.0.0.1:55492/speechnexus' \
  uv run speechnexus-job-migrate --schema speech
```

预期：无 stdout、退出码 `0`。随后 15 张 Job/准入关系应全部存在。下面的查询使用
环境文件里的迁移 DSN，先载入环境（口令来自环境，不进入命令行参数）：

```bash
set -a; . deploy/compose.env; set +a
psql "$SPEECHNEXUS_MIGRATION_DATABASE_URL" -Atc \
  "select count(*) from pg_class c join pg_namespace n on n.oid=c.relnamespace
   where n.nspname='speech' and c.relkind='r'
     and relname in ('coordination_jobs','coordination_outbox','admission_operations',
       'admission_attempts','admission_requests','admission_leases','speech_jobs',
       'speech_job_events','speech_job_submissions','speech_job_cancellations',
       'speech_job_cancellation_fences','speech_job_cancellation_reconciliations',
       'speech_job_cancellation_not_submitted','speech_job_intents','speech_artifacts')"
```

预期输出 `15`。

| 症状 | 原因 |
| --- | --- |
| `relation "speech.alembic_version" does not exist` | 迁移未执行，或执行到另一个数据库/schema |
| 版本号为空 | 迁移中断在版本记录提交前；不要手工写版本表，按[对账恢复](reconciliation-recovery.md)处理 |
| 版本号低于发布制品 | 镜像或发布制品不匹配；先升级迁移，再启动新版本进程 |

## 6. 第 3 步：前置 3 —— 派发角色与五组 grant

派发角色需要五组授权：`job_runtime_grants`、`job_dispatch_grants`、`public_job_grants`、
`cancellation_reconciliation_grants` 与 `coordination_runtime_grants`。启用
`voice_validation` 时再加 `voice_validation_worker_grants`（只有两张 `SELECT`），否则 worker
在领取前拒绝启动。writer 与 admission 也在这里一并安装，三者身份必须互不相同。

先在当前 shell 载入环境文件，并准备迁移 owner 连接。`SN_ADMIN_DSN` 走获准的秘密渠道，
不要写进命令历史：

```bash
set -a
. deploy/compose.env
set +a
export SN_ADMIN_DSN="postgresql://${POSTGRES_USER}:${POSTGRES_PASSWORD}@127.0.0.1:${SPEECHNEXUS_POSTGRES_HOST_PORT:-55432}/${POSTGRES_DB}"
```

下面的脚本从三个 Job DSN 里读取角色名与口令，创建角色并安装授权；它只做 `CREATE ROLE`、
`ALTER ROLE ... PASSWORD` 和追加式 `GRANT`，不改写产品数据、不执行迁移。角色已存在时直接
停止，不猜测归属。

> 本机验证：对一次性 PostgreSQL 16.15 执行过同一份代码，输出
> `job roles ready: sn_job_writer sn_job_admission sn_job_dispatch; grants: 208 statements`，
> 退出码 `0`。

```bash
uv run python - <<'PY'
"""创建异步 Job 的三个非 owner 角色并安装文档声明的授权函数。

必须使用迁移 owner DSN(`SN_ADMIN_DSN`)运行。角色名与口令取自 `deploy/compose.env`
中的三个 Job DSN,因此授权目标与运行时身份不会漂移。只新增角色和追加式 GRANT。
"""

import os
from urllib.parse import unquote, urlsplit

import psycopg
from psycopg import sql

from speechnexus_backend.speech.infrastructure.coordination_grants import (
    coordination_runtime_grants,
)
from speechnexus_backend.speech.infrastructure.job_cancellation_fence_schema import (
    cancellation_not_submitted_grants,
    cancellation_reconciliation_grants,
    job_cancellation_fence_grants,
)
from speechnexus_backend.speech.infrastructure.job_schema import (
    job_dispatch_grants,
    job_runtime_grants,
)
from speechnexus_backend.speech.infrastructure.public_job_admission_grants import (
    public_job_admission_grants,
)
from speechnexus_backend.speech.infrastructure.public_job_schema import public_job_grants
from speechnexus_backend.speech.infrastructure.public_job_submission_schema import (
    public_job_submission_grants,
)
from speechnexus_backend.speech.infrastructure.voice_draft_schema import (
    voice_draft_grants,
    voice_validation_worker_grants,
)

SCHEMA = "speech"
ROLE_KEYS = (
    "SPEECHNEXUS_JOB_WRITER_DATABASE_URL",
    "SPEECHNEXUS_JOB_ADMISSION_DATABASE_URL",
    "SPEECHNEXUS_JOB_DISPATCH_DATABASE_URL",
)


def identity(key: str) -> tuple[str, str]:
    raw = os.environ.get(key, "").strip()
    if not raw:
        raise SystemExit(f"{key} is blank; fill the Job DSNs in deploy/compose.env first")
    parts = urlsplit(raw)
    if not parts.username or not parts.hostname:
        raise SystemExit(f"{key} is not a usable postgresql:// DSN")
    return unquote(parts.username), unquote(parts.password or "")


writer, admission, dispatch = (identity(key)[0] for key in ROLE_KEYS)
roles = dict(zip(("writer", "admission", "dispatch"), (writer, admission, dispatch)))
if len(set(roles.values())) != 3:
    raise SystemExit("the three Job DSNs must name three distinct non-owner roles")
gateway_role = os.environ.get("SN_GATEWAY_ROLE", "sn_gateway")

statements: list[str] = []
statements += list(job_runtime_grants(SCHEMA, writer))
statements += list(job_dispatch_grants(SCHEMA, writer))
statements += list(public_job_grants(SCHEMA, writer))
statements += list(voice_draft_grants(SCHEMA, writer))
statements += list(public_job_submission_grants(SCHEMA, writer))
statements += list(job_cancellation_fence_grants(SCHEMA, writer))
statements += list(cancellation_not_submitted_grants(SCHEMA, writer))
statements += list(cancellation_reconciliation_grants(SCHEMA, writer))
statements += list(public_job_admission_grants(SCHEMA, admission))
statements += list(job_runtime_grants(SCHEMA, dispatch))
statements += list(job_dispatch_grants(SCHEMA, dispatch))
statements += list(public_job_grants(SCHEMA, dispatch))
statements += list(cancellation_reconciliation_grants(SCHEMA, dispatch))
statements += list(
    coordination_runtime_grants(SCHEMA, speech_role=dispatch, gateway_role=gateway_role)
)
kinds = {
    kind.strip()
    for kind in os.environ.get("SPEECHNEXUS_JOB_DISPATCH_KINDS", "asr,tts").split(",")
    if kind.strip()
}
if "voice_validation" in kinds:
    statements += list(voice_validation_worker_grants(SCHEMA, dispatch))

with psycopg.connect(os.environ["SN_ADMIN_DSN"], autocommit=True) as conn:
    if conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (gateway_role,)).fetchone() is None:
        raise SystemExit(f"gateway role {gateway_role} is missing; run the baseline roles first")
    existing = [
        role
        for name, role in ((key, identity(key)[0]) for key in ROLE_KEYS)
        if conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone()
    ]
    if existing:
        raise SystemExit(f"roles already exist: {', '.join(existing)}; inspect ownership first")
    for key in ROLE_KEYS:
        role, password = identity(key)
        conn.execute(
            sql.SQL("CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT").format(
                sql.Identifier(role)
            )
        )
        conn.execute(
            sql.SQL("ALTER ROLE {} PASSWORD {}").format(sql.Identifier(role), sql.Literal(password))
        )
    for statement in statements:
        conn.execute(statement)
    for role in (*roles.values(), gateway_role):
        row = conn.execute(
            "SELECT rolsuper OR rolcreatedb OR rolcreaterole FROM pg_roles WHERE rolname = %s",
            (role,),
        ).fetchone()
        if row is None or any(row):
            raise SystemExit(f"role {role} is not a usable non-owner role")
print(f"job roles ready: {writer} {admission} {dispatch}; grants: {len(statements)} statements")
PY
```

脚本默认 `SN_GATEWAY_ROLE=sn_gateway`；若基础部署的 gateway 角色名不同，用实际 DSN 的用户名
覆盖该变量。`coordination_runtime_grants` 要求两个角色不同，因此必须先有基础 gateway 角色。

用 worker 启动时**同样的探测语句**核对派发角色（这只是列权限，改动数据为 0 行）：

```bash
set -a; . deploy/compose.env; set +a
for stmt in \
  "SELECT job_id FROM speech.speech_jobs LIMIT 0" \
  "SELECT job_id, event_seq, state, occurred_at_us, kind, progress_unit, progress_completed, progress_total FROM speech.speech_job_events LIMIT 0" \
  "SELECT job_id FROM speech.coordination_jobs LIMIT 0" \
  "SELECT operation_id, request_document FROM speech.admission_operations LIMIT 0" \
  "SELECT attempt_id FROM speech.admission_attempts LIMIT 0" \
  "SELECT request_document FROM speech.admission_requests LIMIT 0" \
  "SELECT lease_id, lease_state, cleanup_state, deadline_at_us FROM speech.admission_leases LIMIT 0" \
  "SELECT artifact_id FROM speech.speech_artifacts LIMIT 0"; do
  psql "$SPEECHNEXUS_JOB_DISPATCH_DATABASE_URL" -X -q -v ON_ERROR_STOP=1 -c "$stmt" >/dev/null \
    || { printf 'FAILED: %s\n' "$stmt"; exit 1; }
done
printf 'dispatch probes ok\n'
```

预期输出 `dispatch probes ok`；任一条失败会打印该语句并退出非零，错误信息点名关系
（例如 `permission denied for table speech_jobs`）。本机已对同一批语句验证通过。

已部署环境升级后，不要在已有角色上重跑上面的建角色脚本；按
[deploy/README.md](../../deploy/README.md#job-dispatcher-job-dispatch-profile) 的升级段用
owner 连接重放对应授权函数，然后重启 `api` 与 worker，让启动校验重新核对权限面。

| 症状 | 原因 |
| --- | --- |
| worker 反复重启，日志含 `Dispatch requires a non-owner role without DDL` | 角色是 superuser、持有 schema `CREATE`，或是表 owner 成员 |
| worker 反复重启，日志含 `permission denied for table ...` | 五组 grant 未装全，或装到了另一个角色/数据库 |
| 选择器里出现 `voice_validation` 但启动即退出 | 缺 `voice_validation_worker_grants` 的两张 `SELECT` |
| API 启动即拒绝并提示权限面不符 | writer 角色被授予了表级 `INSERT` 或多余列；按其提示撤销后重新授予列级权限 |

## 7. 第 4 步：前置 4 —— 真实引擎与实例注册

注册前必须已经发布 binding（实例与配方里的 binding revision 一致），且引擎 `/health` 从
**执行注册的主机**可达。注册把实例写入准入事实并置为 `ready`；没有 ready 实例时创建返回
`429 job_capacity_exhausted`，而不是静默排队。创建 Job 还要求该 binding 的
`execution_defaults` 能补全全部执行参数（见第 10 步）。

```bash
curl -fsS "${SPEECHNEXUS_ENGINE_HEALTH_URL%/}/health"
```

预期 `200`。然后执行一次性注册（`engine-registration` profile）：

```bash
docker compose --env-file deploy/compose.env -f deploy/compose.yaml \
  --profile engine-registration run --rm engine-register
```

预期 stdout 是一个 JSON 文档，含：

```text
"outcome": "registered"
```

完全相同的重跑在健康、身份、owner 与 readiness 记录一致时返回 `"outcome": "already_ready"`。
核对注册事实（只读）：

```bash
set -a; . deploy/compose.env; set +a
psql "$SPEECHNEXUS_MIGRATION_DATABASE_URL" -Atc \
  "select e.instance_id, e.instance_generation, e.instance_state
     from speech.engine_instances e
    where e.instance_id = '${SPEECHNEXUS_ENGINE_INSTANCE_ID}'"
```

预期一行 `instance-asr-1|1|ready`。未注册时输出为空——本机对一次性 PG 实测为空，这与
"没有可执行实例"一致，不能当作注册成功。

| 症状 | 原因 |
| --- | --- |
| `engine_unreachable` | 健康 URL 只能在宿主机访问；须从注册容器/网络内可达 |
| `invalid_recipe` | 配方摘要、`stage.instance_id` 或 binding plan 与实际不一致 |
| `pool_owner_conflict` / `previous_generation_active` | 旧 owner 或旧 generation 仍 ready；先核对/停止旧实例，不要改 token 假装接管 |
| 创建返回 `429 job_capacity_exhausted` | 没有 `ready` 实例，或其实例的 binding revision 与请求的发布不一致 |

`engine-health 200` 只证明 HTTP 可达，不证明模型或实例身份；注册命令也不启动任何容器。
命令执行状态：健康探测与注册命令**未在本机验证**（本片没有真实引擎，见第 13 节）；
注册事实查询已执行。

## 8. 第 5 步：前置 5 —— gateway 的 `result_bucket`

gateway 把 staging 结果写进私有配置里的 `objects.result_bucket`，派发 worker 按
`SPEECHNEXUS_EXECUTION_RESULT_BUCKET` 读回。两者不一致时，Job 会在 claim 之后、第一次结果
发布时失败。该桶必须由 `minio-init` 创建并启用版本化——`check_ready` 只检查 Artifact 桶，
不能替代这一步。

核对 env 与两份私有配置一致：

```bash
python3 - <<'PY'
import json
from pathlib import Path

values = {}
for line in Path("deploy/compose.env").read_text().splitlines():
    if "=" in line and not line.lstrip().startswith("#"):
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()

result = values.get("SPEECHNEXUS_EXECUTION_RESULT_BUCKET", "")
private = Path(values.get("SPEECHNEXUS_PRIVATE_CONFIG_DIR", "deploy/.runtime/private"))
for name in ("gateway-file-asr.json", "gateway-file-tts.json"):
    configured = json.loads((private / name).read_text())["objects"]["result_bucket"]
    if configured != result:
        raise SystemExit(f"{name}: result_bucket={configured} != env {result}")
print(f"result_bucket consistent: {result}")
PY
```

预期输出 `result_bucket consistent: speechnexus-results`。本机已对私有配置样例验证；
把 env 改成另一个桶名会输出 `gateway-file-asr.json: result_bucket=... != env ...` 并以非零退出。

`minio-init` 的成功输出是桶被创建并启用版本化的证据：

```bash
docker compose --env-file deploy/compose.env -f deploy/compose.yaml run --rm minio-init
```

预期每个不同桶名一行，且都带 `versioning enabled`：

```text
bucket ready: speechnexus-inputs (versioning enabled)
bucket ready: speechnexus-results (versioning enabled)
```

`run --rm minio-init` 与桶输出**未在本机验证**（需要运行中的 MinIO 与 bootstrap 凭据）。

## 9. 第 6 步：前置 6 —— 启动顺序与网络命名空间

`job-dispatch-worker` 用 `network_mode: "service:gateway"` 加入 gateway 的完整网络命名空间，
因此只能在 `gateway` healthy 之后启动，且 gateway 被重建后必须显式重建 worker。Compose 的
`depends_on.restart: true` 只处理显式 Compose 更新，daemon 重启或外部重建不在此列。

先确认解析后的接线（不需要 daemon）：

```bash
docker compose --env-file deploy/compose.env -f deploy/compose.yaml \
  --profile job-dispatch config --format json |
python3 -c '
import json, sys
services = json.load(sys.stdin)["services"]
worker = services["job-dispatch-worker"]
print("network_mode:", worker["network_mode"])
print("depends_on.gateway:", worker["depends_on"]["gateway"])
print("worker result bucket:", worker["environment"].get("SPEECHNEXUS_EXECUTION_RESULT_BUCKET"))
print("minio-init result bucket:", services["minio-init"]["environment"].get("SPEECHNEXUS_EXECUTION_RESULT_BUCKET"))
print("worker healthcheck:", "healthcheck" in worker)
'
```

本机以 `deploy/compose.env.example` 实测输出（`compose.env` 的取值相同时结果一致）：

```text
network_mode: service:gateway
depends_on.gateway: {'condition': 'service_healthy', 'restart': True, 'required': True}
worker result bucket: speechnexus-results
minio-init result bucket: speechnexus-results
worker healthcheck: False
```

按顺序启动：先让基础拓扑（含 gateway）healthy，再启用 profile：

```bash
docker compose --env-file deploy/compose.env -f deploy/compose.yaml up -d --build api gateway
docker compose --env-file deploy/compose.env -f deploy/compose.yaml \
  --profile job-dispatch up -d
```

核对 worker 的网络命名空间就是当前 gateway 容器、且没有重启过：

```bash
gateway_id=$(docker compose --env-file deploy/compose.env -f deploy/compose.yaml \
  ps -q gateway)
docker inspect --format '{{.HostConfig.NetworkMode}} {{.RestartCount}} {{.State.Status}}' \
  "$(docker compose --env-file deploy/compose.env -f deploy/compose.yaml \
      ps -q job-dispatch-worker)"
```

预期形如 `container:<gateway 的完整 ID> 0 running`。若第一段不是该 gateway 的 ID，说明
worker 仍在旧命名空间；重建 worker：

```bash
docker compose --env-file deploy/compose.env -f deploy/compose.yaml \
  --profile job-dispatch up -d --force-recreate job-dispatch-worker
```

重建 gateway 后同样重跑上面两条命令；只重建 gateway 不会让 worker 自动换命名空间。

两条启动命令与 `docker inspect` **未在本机验证**（本片不启动完整部署，见第 13 节）。
自检脚本的第 6 项在合成容器上验证过判定逻辑：能识别"worker 未运行"与"命名空间指向
另一容器"，也能在网关健康时判 PASS。

## 10. 第 7 步：提交并观察一次 Job

六项前置满足后，`POST /v1/nexus/jobs` 才不再返回 `503`。异步 ASR 还需要一个 `ready`
的输入 Asset；上传音频需要已安装的真实检查器，未安装时上传停在 `staging`，这不在六项计数
内，但同样会阻止 Job 创建。

上传输入资产（`purpose` 只接受 `asr_input`、`voice_reference`、`text_input`）：

```bash
curl -sS -X POST http://127.0.0.1:8000/v1/nexus/assets \
  -H "Idempotency-Key: $(python3 -c 'import uuid; print(uuid.uuid4())')" \
  -F purpose=asr_input \
  -F file=@./sample.wav   # 换成已获准的测试音频；不要用真实业务录音
```

预期 `201`，响应里的 `status` 为 `ready`（未安装真实检查器时为 `staging`，此时先解决检查器）。

创建 Job：

```bash
curl -sS -X POST http://127.0.0.1:8000/v1/nexus/jobs \
  -H 'content-type: application/json' \
  -H "Idempotency-Key: $(python3 -c 'import uuid; print(uuid.uuid4())')" \
  -d '{"kind":"asr","model":"<已发布 binding 的对象 ID>",
       "input":{"audio_asset_id":"<上一步的 asset id>"}}'
```

预期 `202`，body 是 Job 文档，`status` 为 `queued`，并带 `Location`。随后轮询：

```bash
curl -sS http://127.0.0.1:8000/v1/nexus/jobs/<job_id>
```

预期状态依次推进 `queued` → `running` → `succeeded`，`artifacts` 出现已提交制品；每个状态
变化都有对应的持久事件。若停在 `queued`，按第 12 节定位。若停在 `running` 超过
`SPEECHNEXUS_JOB_DISPATCH_JOB_TIMEOUT_SECONDS`，该轮派发按未知处理，不把仍可能运行的引擎
写成确定性 `failed`；租约到期后由下一个 worker 按同一 `submit_intent_id` 接手，不会创建
第二个执行。

创建阶段还有两个不属于六项的前置：binding 的冻结 `execution_defaults` 必须能补全所有执行
参数（否则 `422 job_binding_incomplete`），声音必须已发布且未退休（否则 `422 voice_not_published`）。
按[绑定发布](publish-binding-revision.md)与[内建声音资格](builtin-voice-qualification.md)分别取证。

命令执行状态：以上三条 HTTP 命令**未在本机验证**（没有完整参考部署与真实引擎）。

## 11. 自检脚本

只读检查六项前置，不改文件、不写数据库、不执行迁移、不重启容器：

```bash
python3 scripts/dev-env/check-async-job-prereqs.py
```

选项：

| 选项 | 作用 |
| --- | --- |
| `--env-file PATH` | 指定环境文件（默认 `deploy/compose.env`） |
| `--compose-file PATH` | 指定 Compose 定义（默认 `deploy/compose.yaml`） |
| `--offline` | 跳过 psql / HTTP / Docker 探测，只做静态判定 |

退出码 `0` 表示 6 项都没有 FAIL（允许 SKIP）；`1` 表示至少一项 FAIL，或 env 文件不存在
（此时直接打印生成该文件的指引，不输出六项判定）；`2` 表示 compose 文件缺失/不可读或参数
错误。每项一行 `[PASS]`/`[FAIL]`，FAIL 下逐条给出原因，末尾汇总缺项编号。

判定边界：

- 静态判定读 env、`deploy/compose.yaml` 与私有配置文件形态（存在性、mode、`result_bucket`
  一致性、`network_mode`/`depends_on` 声明）；
- 运行时判定只做 `SELECT`、`LIMIT 0` 列权限探测、引擎 `/health` GET 和 `docker inspect` 读取；
- 没有 `psql`、Docker daemon 或数据库从宿主机不可达时，相应运行时证据标 `SKIP` 并说明原因；
- 对象桶的存在与版本化需要签名凭据，脚本不持有凭据，因此该项恒为 SKIP，靠 `minio-init`
  输出核对；
- 授权形状的最终权威是进程启动校验与 `docker logs`；脚本通过不等价于启动验证通过。

把任一项人为置为不满足（例如清空 `SPEECHNEXUS_JOB_WRITER_DATABASE_URL`）时，对应项会
FAIL 并点名该键（以下为实测输出节选）：

```text
[FAIL] 前置 1  API 侧 Job writer / admission DSN
        ✗ SPEECHNEXUS_JOB_WRITER_DATABASE_URL 为空;POST /v1/nexus/jobs 直接返回 503 job_unavailable
...
结果: 5/6 PASS;缺项或配置不满足:前置 1
```

## 12. 失败排查

| 现象 | 先看 | 前置 |
| --- | --- | --- |
| `up -d`/`--wait` 成功但 Job 一直 `queued` | worker 是否在反复重启；`logs job-dispatch-worker` 的首条错误 | 3、6 |
| `POST /v1/nexus/jobs` 返回 `503 job_unavailable` | writer/admission DSN 是否为空、API 是否重建 | 1 |
| 创建返回 `429 job_capacity_exhausted` | `engine_instances` 是否有该 binding 的 `ready` 实例 | 4 |
| 创建返回 `422 job_binding_incomplete` | binding 是否带完整 `execution_defaults` | 六项之外 |
| worker 启动即退出 | 角色守卫、`permission denied`、缺少 `voice_validation_worker_grants` | 3 |
| Job 进入 `running` 后结果读不回 | worker 与 gateway 的 `result_bucket` 是否一致、桶是否版本化 | 5 |
| worker 在、但网关调用全部失败 | worker 的网络命名空间是否仍指向当前 gateway 容器 | 6 |
| 迁移报 `alembic_version` 不存在或版本偏低 | 迁移是否执行到目标库、发布制品 head | 2 |

## 13. 本机验证记录与未验证项

本片在 `/private/tmp/sn-572`（基于 `origin/main` 的 `cc22f6d0`）执行，2026-11-08。
一次性 PostgreSQL 16.15 容器 `sn572-pg`（`127.0.0.1:55492`）用于迁移与授权验证，
用完即删；它不是产品部署证据。

已在本机执行：

| 命令 / 操作 | 结果 |
| --- | --- |
| `docker compose ... config --services`（基础 / `job-dispatch` / `migration`） | 分别解析出 5 / 6 / 6 个服务，见第 3 节 |
| `docker compose ... --profile job-dispatch config --format json` | 打印 `service:gateway`、`restart: True`、两个桶名与 `healthcheck: False` |
| `uv run speechnexus-job-migrate --schema speech`（迁移 DSN 指向一次性 PG） | 退出码 `0`，无 stdout |
| `select version_num from speech.alembic_version` | `0048_voice_evidence_scan_index` |
| 15 张 Job/准入关系计数 | `15` |
| 第 4 节的 DSN 生成脚本（作用于 env 副本） | 输出三个角色名，值未打印，mode `0600` |
| 第 6 节的角色与授权脚本（DSN 派生的角色名与口令） | 输出 `grants: 208 statements`，退出码 `0` |
| worker 同款列权限探测（8 条）与 owner/DDL 守卫（`f`） | 全部通过 |
| `check-async-job-prereqs.py` 的全部几种输入 | 见下 |
| `python3 scripts/dev-env/check-doc-refs.py` | `RESULT PASS`（refs=88，EXTERNAL=1，OK=87） |
| `ruff format --check .` / `ruff check .` | `1671 files already formatted` / `All checks passed!` |
| `uv run mypy` | 新增脚本无错误；仓库其余 5 个文件在 macOS 上报 `pidfd_*` 缺失（Linux 限定的既有问题，与本次改动无关） |

自检的四个实测输入：

| 输入 | 结果 |
| --- | --- |
| 六项静态满足的 env 副本（DSN 指向未发布的 `postgres` 主机名） | 6/6 PASS，退出码 `0`；运行时项均 SKIP 并给出原因 |
| 同上但 `SPEECHNEXUS_JOB_WRITER_DATABASE_URL` 留空 | 前置 1 FAIL，其余 5 项 PASS，退出码 `1` |
| 同上但 DSN 指向已迁移并授权的 `127.0.0.1:55492` | 前置 1/2/3 PASS 且带运行时证据；前置 4 FAIL（无 ready 实例），退出码 `1` |
| 同上但把 `SPEECHNEXUS_EXECUTION_RESULT_BUCKET` 改成别的桶名 | 前置 5 FAIL，点名两份私有配置的 `result_bucket` 不一致 |

未在本机验证（附原因）：

| 命令 / 操作 | 原因 |
| --- | --- |
| `docker compose ... up -d --build` 完整参考部署 | 需要构建 Python 镜像、MinIO 与 PostgreSQL 全套拓扑；本片范围是手册与自检，不做端到端启动 |
| `docker compose ... up -d api` 加载新 Job 身份、`logs api` 判定 | 同上，没有运行中的参考部署 |
| `docker compose ... --profile migration run --rm migrate` | 迁移入口本身已用同一命令对一次性 PG 验证；Compose 侧只验证了解析 |
| `minio-init` 与 `bucket ready ... versioning enabled` 输出 | 需要运行中的 MinIO 与 bootstrap 凭据 |
| `engine-register` 与 `"outcome": "registered"` | 没有真实引擎；按项目约定不得用 fake 引擎代替 |
| `docker compose ... up -d`（`job-dispatch` profile）与 `docker inspect` 网络命名空间 | 没有启动参考部署；第 6 项只用合成容器验证了自检的判定分支 |
| `POST /v1/nexus/assets`、`POST /v1/nexus/jobs`、`GET /v1/nexus/jobs/{id}` | 需要完整参考部署、真实引擎与 ready 输入资产 |