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
它按本文的六项逐条判定为 `PASS`/`FAIL`/`PENDING`，FAIL 时点名缺哪一项；`PENDING` 表示
依赖的部署产物（私有配置、ready 实例）尚未生成，是"还没到该阶段"而不是配置错误，判定
规则见[第 12 节](#12-自检脚本)。本手册的每条命令都标注了执行状态；未在本机
验证的命令一律注明原因，见[第 14 节](#14-本机验证记录与未验证项)。

本文不改变能力结论：**满足六项前置不等于异步 Job 已验收**。真实模型质量、GPU 容量、物理
多节点、内容网络与最终 Gate 仍按[文档索引](../README.md)分别取证。

## 1. 六项前置速查

| 编号 | 前置 | 缺失时的可观察症状 | 缺它的 Job 表现 |
| --- | --- | --- | --- |
| 1 | API 的 `SPEECHNEXUS_JOB_WRITER_DATABASE_URL` 与 `SPEECHNEXUS_JOB_ADMISSION_DATABASE_URL` | `POST /v1/nexus/jobs` 返回 `503 job_unavailable` | 没有 Job 可被 claim |
| 2 | 迁移到唯一 head（`migration` profile，`speechnexus-job-migrate --schema speech`） | Job/准入关系不存在，迁移版本表为空 | 创建失败或 worker 启动即退出 |
| 3 | 派发角色与五组 grant（启用 `voice_validation` 再加两张 `SELECT`） | worker 的启动闸门点名缺授权的表/列并退出，`restart: unless-stopped` 下形成重启循环 | 启动期失败，不领取租约 |
| 4 | 真实引擎与已注册 `ready` 实例（`engine-registration` profile） | 创建返回 `429 job_capacity_exhausted`，或提交必然失败 | 有 Job 但没有可执行实例 |
| 5 | gateway 的 `result_bucket` 存在、被 `minio-init` 创建并启用版本化，且与 env 一致 | worker 的启动闸门点名缺桶/缺版本化/配置不一致并退出 | 启动期失败，不领取租约 |
| 6 | `network_mode: service:gateway` 下的启动顺序与网关重建后的 worker 重建 | worker 命名空间失效或指向旧 gateway | 进程在，网关调用全部失败 |

第 7 步在这六项之外还有一项硬前置：`asr_input`/`voice_reference` 上传必须先由
`asset-inspector` 部署单元判定。默认部署不装配它，音频上传恒为
`503 asset_inspection_unavailable`；装配与验证见第 10 节。

## 2. `up -d`/`--wait` 成功 ≠ 就绪

`docker compose ... up -d --wait` 只等**已定义健康检查**的服务。参考部署里
`job-dispatch-worker` 没有 healthcheck，`--wait` 对它没有可等待的探针，因此：

- `up -d`、`--wait` 返回 0 只表示容器被创建；
- DSN 为空、授权缺失、迁移落后或 Job 桶缺失时，worker 的 entrypoint 启动闸门
  （`deploy/job-startup-gate.py`）在领取租约前点名退出，`restart: unless-stopped` 让它反复
  重启，而 Compose 仍报告成功；
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
上述判据都需要运行中的参考部署，**本片未在本机执行**（第 14 节）；其中重启次数与网络
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
| API 反复重启，日志点名 `authority` / `permission` | 角色权限面与声明不符（见第 6 节，授权未安装或授予了更宽权限） |
| 只配 writer 就能创建、但返回 5xx | 缺少 admission 同库身份；不要用 writer 顶替 |

命令执行状态：DSN 生成脚本已在本机对 env 副本执行；`up -d api`、日志检查与 `/readyz`
**未在本机验证**（本片不启动完整参考部署，见第 14 节）。

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

预期是**一行非空版本号**，且与所用发布制品的唯一 head 一致。不要把某个具体版本号当作
固定目标：仓库的迁移链会前进，判据始终是"与制品内的唯一 head 相同"，启动闸门也正是用它
比对（`expected_migration_head()`）。

同一入口也可以不经过 Compose 直接执行（本机已对一次性 PostgreSQL 16.15 实例验证）：

```bash
SPEECHNEXUS_MIGRATION_DATABASE_URL='postgresql://speechnexus:<owner-password>@127.0.0.1:55492/speechnexus' \
  uv run speechnexus-job-migrate --schema speech
```

预期：无 stdout、退出码 `0`。随后 15 张 Job/准入关系应全部存在。这里的"head"是仓库
`backend/src/speechnexus_backend/speech/infrastructure/migrations/versions/` 下唯一的
`alembic heads` 结果；数据库版本低于它时，自检会指出落后并列出缺的 revision，而不是只看
版本号非空。下面的查询使用
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
| 版本号低于发布制品 head | 迁移落后于镜像；自检会列出缺的 revision，先执行迁移再启动新版本进程 |
| 版本表有多行 | 多个 head 已被应用；先按[对账恢复](reconciliation-recovery.md)收敛再升级 |

## 6. 第 3 步：前置 3 —— 派发角色与五组 grant

派发角色需要五组授权：`job_runtime_grants`、`job_dispatch_grants`、`public_job_grants`、
`cancellation_reconciliation_grants` 与 `coordination_runtime_grants`。启用
`voice_validation` 时再加 `voice_validation_worker_grants`（只有两张 `SELECT`），否则 worker
在领取前拒绝启动。writer 与 admission 也在这里一并安装，三者身份必须互不相同。

writer 还有一组**不在 Job 授权函数里**的基线授权：8 张发布/素材表的 `SELECT`（`speech_workspaces`、
`speech_publications`、`speech_publication_heads`、`speech_catalog_state`、
`speech_publication_retirements`、`speech_binding_revisions`、`speech_assets`、
`speech_pronunciation_revisions`）、`admission_operations` 与 `coordination_jobs` 的 `SELECT`，
以及 `coordination_jobs` 的 4 个认领列 `UPDATE`。缺任一项，API 的 `JobRuntime.open()` 会以
`Public Job writer startup rejected` 拒绝启动：容器进入 `Restarting`，`/readyz` 不可达，
Job 创建得到的是**连接失败而非任何 HTTP 状态码**（实测 `curl(7)`／`http_code=000`），
不是早期版本所写的 `503`。下面的脚本已包含
这组基线（共 219 条语句）。

先在当前 shell 载入环境文件，并准备迁移 owner 连接。`SN_ADMIN_DSN` 走获准的秘密渠道，
不要写进命令历史：

```bash
set -a
. deploy/compose.env
set +a
export SN_ADMIN_DSN="postgresql://${POSTGRES_USER}:${POSTGRES_PASSWORD}@127.0.0.1:${SPEECHNEXUS_POSTGRES_HOST_PORT:-55432}/${POSTGRES_DB}"
```

创建与授权由仓库脚本 `scripts/dev-env/provision-job-roles.py` 完成，步骤与
[最小部署手册 §6 的异步 Job 运行角色](compose-minimal.md#异步-job-运行角色与授权)
一致，这里只执行同一条命令：

```bash
uv run --all-packages python scripts/dev-env/provision-job-roles.py
```

脚本从三个 Job DSN 里读取角色名与口令，只做 `CREATE ROLE`、`ALTER ROLE ... PASSWORD` 和
追加式 `GRANT`，不改写产品数据、不执行迁移。角色已存在时直接停止，不猜测归属；期望输出
`job roles ready: sn_job_writer sn_job_admission sn_job_dispatch; grants: <N> statements`。
语句数随授权函数变化，不要当固定值：以 `--print-statements` 的输出条数为准，首次安装后把
该值记为本次基线。`--print-statements` 只渲染 SQL，不连接数据库。

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

这些探测只覆盖 `SELECT`。派发角色的**写入**列（`speech_jobs` 的 `state`/`status_version`/
`result_json`/`error_code`/`updated_at_us`，`coordination_jobs` 的 4 个认领列，
`speech_job_events` 的 8 个事件列，`speech_job_cancellation_not_submitted` 的 3 个列）由自检
用 `has_column_privilege` 逐列核对；手工核对时可审阅 `job_dispatch_grants` 等函数的
`GRANT UPDATE (...)` / `GRANT INSERT (...)` 语句。

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
命令执行状态：健康探测与注册命令**未在本机验证**（本片没有真实引擎，见第 14 节）；
注册事实查询已执行。

## 8. 第 5 步：前置 5 —— gateway 的 `result_bucket`

gateway 把 staging 结果写进私有配置里的 `objects.result_bucket`，派发 worker 按
`SPEECHNEXUS_EXECUTION_RESULT_BUCKET` 读回。两者不一致或该桶缺失/未版本化时，早期实现在
claim 之后、第一次结果发布时才失败；进程内的 `ResultArtifactPublisher.check_ready()` 只探
Artifact 桶，不能替代这一步。参考部署现在把补丁放在容器 entrypoint：
`job-dispatch-worker` 先运行 `deploy/job-startup-gate.py`，由它核对这两个桶的存在与版本化、
核对两份 gateway 私有配置的 `objects.result_bucket` 与 env 一致，通过后才 `exec` 派发进程。
因此这里的手工核对与 `minio-init` 输出是同一组判据的运维复核，不是唯一的失败前移手段。

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
private = Path(values.get("SPEECHNEXUS_PRIVATE_CONFIG_DIR", ".runtime/private"))
if not private.is_absolute():
    private = Path("deploy") / private  # Compose 按 compose.yaml 所在目录解析相对路径
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

2026-11-13 在容器 PostgreSQL 16.15 与容器 MinIO（`deploy/compose.yaml`）上实跑，输出与上面
逐字一致；同一环境的启动闸门在 result 桶缺失时以退出码 `1` 点名
`SPEECHNEXUS_EXECUTION_RESULT_BUCKET`（第 14 节）。

## 9. 第 6 步：前置 6 —— 启动顺序与网络命名空间

`job-dispatch-worker` 用 `network_mode: "service:gateway"` 加入 gateway 的完整网络命名空间，
因此只能在 `gateway` healthy 之后启动，且 gateway 被重建后必须显式重建 worker。Compose 的
`depends_on.restart: true` 只处理显式 Compose 更新，daemon 重启或外部重建不在此列。启动闸门
在 worker 容器内运行并以 `exec` 替换为派发进程，不改变这个网络命名空间关系；闸门失败时容器
直接退出并点名缺项，`RestartCount` 增长的原因在 `logs job-dispatch-worker` 里可直接读到。

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

两条启动命令与 `docker inspect` **未在本机验证**（本片不启动完整部署，见第 14 节）。
第 6 项的运行时分支**未在真实参考部署上复现**（本片不启动完整拓扑）；它只在一次性的
合成容器上验证过判定逻辑，不构成部署证据。真实拓扑按上面的 `docker inspect` 与
`RestartCount` 核对。

## 10. 第 7 步的前置：`asset-inspector` 部署单元与音频检查装配

第 7 步的第一个动作是上传一个 `ready` 的 `asr_input` 资产。音频上传不是"把字节写进对象
存储"就完事：`asr_input` 与 `voice_reference` 必须先由音频检查器解码并给出媒体事实，判定
通过才落库。这项能力**不在这六项计数内**，但同样是硬前置——默认部署面没有装配任何音频
检查器，上传是确定失败而不是降级。

默认装配的面貌：选择器 `SPEECHNEXUS_ASSET_INSPECTOR` 默认 `text`
（`backend/src/speechnexus_backend/settings.py:68`），`text` 检查器只校验 `text_input`，
两个音频用途一律判为不可用（`backend/src/speechnexus_backend/speech/application/assets.py:92`），
`reserve_asset()` 于是在写入任何记录之前抛出 `asset_inspection_unavailable`
（`backend/src/speechnexus_backend/speech/domain/assets.py:277`）。这就是"按文档走到第 7 步却
撞上 503"的全部原因：默认拓扑里没有能检查音频的组件。

### 10.1 装配事实

| 项 | 事实 |
| --- | --- |
| 服务 / profile | 服务名 `asset-inspector`，`profiles: ["asset-inspection"]`（`deploy/compose.yaml:241`）；不启用该 profile 时服务不被创建，默认服务集合仍是第 3 节的五个 |
| 构建 | 独立镜像 `asset-inspector/Dockerfile`（`deploy/compose.yaml:245`）：解码器 `av` 与 `numpy` 只装在检查器镜像里，API 镜像不含解码能力 |
| 网络 | 只接入 `asset-inspection`（`internal: true`，无默认网关，`deploy/compose.yaml:590`）；成员仅 `api` 与该容器；不发布宿主端口，无 volume、无 bind mount、不挂 docker socket |
| 健康检查 | `GET /healthz`（未鉴权、不写盘），`start_period 5s`、`interval 5s`、`timeout 3s`、`retries 20`（`deploy/compose.yaml:291`） |
| 加固 | `read_only`、`cap_drop: [ALL]`、`no-new-privileges`、`pids_limit: 64`、`mem_limit: 768m`、`cpus: 2.0`、`user 10001:10001` |
| 与 API 的关系 | API 不 `depends_on` 检查器（`deploy/compose.yaml:196`）：检查器停着只让检查类请求失败，API 照常启动，文本资产、`/readyz` 与保留期维护不受影响 |
| 选择键 | `SPEECHNEXUS_ASSET_INSPECTOR`，取值域恰为 `text` \| `audio`，默认 `text`，空值按 `text` 处理（`backend/src/speechnexus_backend/settings.py:68`） |
| 端点 / 令牌 | `SPEECHNEXUS_ASSET_INSPECTION_URL` 只写 origin，不带路径、查询参数或内嵌凭据；令牌以 `SPEECHNEXUS_ASSET_INSPECTION_TOKEN` 注入 API，同一值以 `SPEECHNEXUS_INSPECTION_TOKEN` 注入容器（`deploy/compose.yaml:264`） |

组件设计、协议与验收条件见[音频检查容器](../components/asset-inspection-container.md)；本节只给
"把它接进参考部署、跑通第 7 步"的最小路径。

### 10.2 三步装配

1. 在 `deploy/compose.env` 设置三键。令牌自备，两个容器共用同一个值，用下面的命令生成：

```bash
python3 -c 'import secrets; print(secrets.token_hex(32))'
```

```dotenv
SPEECHNEXUS_ASSET_INSPECTOR=audio
SPEECHNEXUS_ASSET_INSPECTION_URL=http://asset-inspector:8080
SPEECHNEXUS_ASSET_INSPECTION_TOKEN=<上一步输出的令牌>
```

2. 首次构建并启动检查器（不带 `--profile asset-inspection` 时这个容器不会被创建）：

```bash
docker compose --env-file deploy/compose.env -f deploy/compose.yaml \
  --profile asset-inspection up -d --build asset-inspector
```

3. 重建 `api`，让选择器生效。三键在进程启动期解析，改了 `compose.env` 必须重建容器
（`up -d` 会因环境变化重建；进程内 reload 不生效）：

```bash
docker compose --env-file deploy/compose.env -f deploy/compose.yaml up -d api
```

装配是"要么全对、要么启动失败"：任何一键写错都在启动期被点名，见 §10.5，不存在"配了一半
先降级到文本"的中间态。

### 10.3 验证

检查器不发布宿主端口，探针要在容器里执行。从 API 容器发请求同时证明"API 能按服务名到达
检查器"：

```bash
docker compose --env-file deploy/compose.env -f deploy/compose.yaml \
  exec -T api python -c \
  "import urllib.request; print(urllib.request.urlopen('http://asset-inspector:8080/readyz', timeout=2).read().decode())"
```

预期 `200`，body 形如（`max_encoded_bytes`/`max_wall_seconds` 随镜像默认值变化；判据是
`contract_version` 与 `recipe_version`）：

```json
{"contract_version":1,"recipe_version":"pcm-decode-1","max_encoded_bytes":4194304,"max_wall_seconds":20.0,"in_flight":0}
```

`/readyz` 在启动自检（真实解码一段内置合成 WAV）通过之前返回 `503`
（`asset-inspector/src/speechnexus_asset_inspector/app.py:108`），所以这条 `200` 表示"真的能
解码"，不只是端口通。

端到端判据是第 11 节的上传：`purpose=asr_input` 的合成 WAV 返回 `201`，`status` 为
`ready`，且 `media_type`/`duration_ms`/`sample_rate_hz`/`channels` 来自检查器回执而不是本地
猜测。若仍返回 `503 asset_inspection_unavailable`，说明装配没生效——按 §10.4 判形态，再按
§10.2 逐键核对。

### 10.4 未装配时看到什么

停掉检查器不会改变 API 的可用性（API 全程 `healthy`、不重启），只让检查类请求失败。两条
路径给出同一个响应体：

| 状态 | `purpose=asr_input` 上传的响应 |
| --- | --- |
| 选择器 `text`（默认），或 `audio` 但 URL/令牌为空 | `503` `asset_inspection_unavailable`，`param: null` |
| `audio` 配置正确，但检查器容器停着或不可达 | 同上，一字不差 |

两行的完整响应（实测，`request_id` 的真实值从略）：

```json
{
  "error": {
    "code": "asset_inspection_unavailable",
    "type": "service_unavailable_error",
    "message": "Asset inspection is unavailable.",
    "param": null,
    "request_id": "…",
    "retryable": false
  }
}
```

更细的失败分类——检查器不服务某个 `purpose` 时同码带 `param: purpose`、检查器忙或超出解码
预算时为可重试的 `asset_inspection_busy`——见[音频检查容器](../components/asset-inspection-container.md)
的协议失败分类，本节只区分"没装配"与"装配好了"。

响应头带 `X-Should-Retry: false`，`error.retryable` 为 `false`：同一个请求重试不会成功，必须
先改部署（`backend/src/speechnexus_backend/speech/interfaces/http/asset_errors.py:40` 给出
503 映射，同文件 `:86` 的 `_RETRYABLE` 只包含 `asset_inspection_busy`）。失败不写
`speech_assets` 行，也不留 `staging`/`reserved` 记录，所以没有可重试的原上传，重试等于再上传
一次。

### 10.5 键写错时的启动期拒绝

装配点在选择器上做校验：先跑退役键检查（`backend/src/speechnexus_backend/asset_runtime.py:216`
调用 `_refuse_retired_inspector_settings`），再由 `_build_http_audio_inspector` 校验 URL 与
令牌。配错不会变成运行期 503，而是 API 起不来并点名变量：

| 配置 | 结果 |
| --- | --- |
| `audio` 但 URL 为空 | `ValueError: SPEECHNEXUS_ASSET_INSPECTION_URL is required when SPEECHNEXUS_ASSET_INSPECTOR=audio` |
| `audio` 但令牌为空或全空白 | `ValueError: SPEECHNEXUS_ASSET_INSPECTION_TOKEN is required when SPEECHNEXUS_ASSET_INSPECTOR=audio; a blank value is not a configured token` |
| `text` 却设置了 URL 或令牌 | `ValueError: SPEECHNEXUS_ASSET_INSPECTION_URL configured but SPEECHNEXUS_ASSET_INSPECTOR=text; select audio or unset every asset-inspection setting` |
| `text` 却把某个音频上限改离默认值 | `ValueError: SPEECHNEXUS_AUDIO_INSPECTION_MAX_DURATION_MS only take effect with SPEECHNEXUS_ASSET_INSPECTOR=audio; select audio or keep the default value` |
| 检查器容器拿不到令牌 | 容器退出码 `1`：`asset inspector configuration error: SPEECHNEXUS_INSPECTION_TOKEN must be a non-empty bearer token` |

第 3、4 行是同一种防降级校验的两个分支（`text` 下配了音频前置、`text` 下改了音频上限）：
两处都在启动期拒绝，不会把音频意图静默降级成文本检查。最后一行是容易漏的一环：Compose 里
检查器的 `SPEECHNEXUS_INSPECTION_TOKEN` 与 API 的 `SPEECHNEXUS_ASSET_INSPECTION_TOKEN` 取自
同一个环境变量（`deploy/compose.yaml:264`），不需要给两个服务各写一遍；但漏填会让检查器起
不来，而 API 只会给出 §10.4 的 503。

### 10.6 退役键：配了就起不来

旧的本机进程检查器已删除，它用过的键仍声明在 `Settings` 里，**只用于拒绝**。任何选择器下
（`text` 或 `audio`）命中都会让 API 启动失败，所以从旧部署升级必须删掉这些变量，不能指望
它们被忽略（`backend/src/speechnexus_backend/asset_runtime.py:122`）：

| 键 | 现状 | 处置 |
| --- | --- | --- |
| `SPEECHNEXUS_AUDIO_INSPECTION_RUNTIME_ROOT`、`_COMMAND`、`_LAUNCHER_PYTHON`、`_BUBBLEWRAP`、`_PRLIMIT` | 本机进程检查器的路径、命令、解释器与沙箱键，已无消费者 | 删除或留空（空值按未配置处理） |
| `SPEECHNEXUS_AUDIO_INSPECTION_MEMORY_BYTES`、`_CPU_SECONDS`、`_OUTPUT_BYTES` | 解码预算，已迁到检查器侧的 `SPEECHNEXUS_INSPECTION_*` | 保持发布的默认值；要调整就改容器侧同名键 |

实测的拒绝消息（前者取自 `text` 部署，`audio` 同样命中；后者是预算迁移）：

```text
ValueError: SPEECHNEXUS_AUDIO_INSPECTION_RUNTIME_ROOT no longer select a local inspection process; audio inspection runs in the asset-inspector deployment unit; configure SPEECHNEXUS_ASSET_INSPECTION_URL and SPEECHNEXUS_ASSET_INSPECTION_TOKEN
```

```text
ValueError: SPEECHNEXUS_AUDIO_INSPECTION_MEMORY_BYTES is owned by the inspector container now, set SPEECHNEXUS_INSPECTION_MEMORY_BYTES
```

拒绝发生在创建连接池与对象存储客户端之前（`backend/src/speechnexus_backend/asset_runtime.py:252`
的 `AssetRuntime.__init__`），因此不会留下半装配状态：API 进程直接退出，
`restart: unless-stopped` 下表现为重启循环，`logs api` 的首条即点名该键。

命令执行状态：§10.2 的两条启动命令、§10.3 的两条判据、§10.4 的两行失败形态，以及
§10.5/§10.6 的拒绝消息都在本机按原样执行过（基线 `origin/main` `4cfdfe77`），逐条输出见
[第 14 节](#14-本机验证记录与未验证项)。

## 11. 第 7 步：提交并观察一次 Job

六项前置满足后，`POST /v1/nexus/jobs` 才不再返回 `503`。异步 ASR 还需要一个 `ready`
的输入 Asset；上传音频需要已装配的真实检查器，未装配时上传**直接失败**
`503 asset_inspection_unavailable`（`x-should-retry: false`），且不会产生 `staging`/`reserved`
行——`reserve_asset()` 在写入任何记录之前就抛出该错（`speech/domain/assets.py`）。这不在六项
计数内，但同样阻止 Job 创建。装配步骤见第 10 节，组件设计见[音频检查容器](../components/asset-inspection-container.md)。

上传输入资产（`purpose` 只接受 `asr_input`、`voice_reference`、`text_input`）：

```bash
curl -sS -X POST http://127.0.0.1:8000/v1/nexus/assets \
  -H "Idempotency-Key: $(python3 -c 'import uuid; print(uuid.uuid4())')" \
  -F purpose=asr_input \
  -F file=@./sample.wav   # 换成已获准的测试音频；不要用真实业务录音
```

预期 `201`，响应里的 `status` 为 `ready`。若返回 `503 asset_inspection_unavailable`，说明音频
检查器未装配（不是配置错），先按 #589 解决装配；此时不会留下 `staging` 记录可供重试。

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
变化都有对应的持久事件。若停在 `queued`，按第 13 节定位。若停在 `running` 超过
`SPEECHNEXUS_JOB_DISPATCH_JOB_TIMEOUT_SECONDS`，该轮派发按未知处理，不把仍可能运行的引擎
写成确定性 `failed`；租约到期后由下一个 worker 按同一 `submit_intent_id` 接手，不会创建
第二个执行。

创建阶段还有两个不属于六项的前置：binding 的冻结 `execution_defaults` 必须能补全所有执行
参数（否则 `422 job_binding_incomplete`），声音必须已发布且未退休（否则 `422 voice_not_published`）。
按[绑定发布](publish-binding-revision.md)与[内建声音资格](builtin-voice-qualification.md)分别取证。

命令执行状态：以上三条 HTTP 命令**未在本机验证**（没有完整参考部署与真实引擎）。

## 12. 自检脚本

只读检查六项前置，不改文件、不写数据库、不执行迁移、不重启容器。它和 worker 容器里的
启动闸门分工不同：自检给运维在部署前后做完整判定（含无法在容器内看到的私有配置与实例
注册），闸门在容器启动时只阻断 claim 并点名容器内可判定的缺项。

脚本把每一项判成三种状态：

| 状态 | 含义 |
| --- | --- |
| `PASS` | 该项当前满足；运行时证据不足时附 `SKIP` 说明，不影响静态结论 |
| `FAIL` | 配置写了但不对：桶名与 gateway 私有配置不一致、DSN 为空、迁移落后 head、权限缺失等，**任何阶段都必须修** |
| `PENDING` | 只依赖部署产物（私有配置未生成、实例未注册）且尚未到该阶段；部署前不构成错误 |

只有前置 4（私有配方、尚未注册的实例）和前置 5（私有 gateway 配置）会在部署前把尚未
生成的产物记 `PENDING`；前置 1/2/3/6 在任何阶段都是 `PASS` 或 `FAIL`，真实配置错误不会
被阶段降级。实例行已存在但 `instance_state` 不是 `ready` 属于部署后的活证据，任何阶段
都记 `FAIL`；`PENDING` 只表示“查不到该实例行，尚未注册”。

### 12.1 两次运行：部署前看 PENDING，部署后看真 FAIL

`--phase auto`（默认）按客观信号选阶段：Compose 项目容器、`deploy/.runtime` 或私有配置
目录下已生成非隐藏文件，任一出现即按部署后判定，否则按部署前。`compose-minimal.md`
第 2 节 `mkdir -p` 出来的三个空目录（`private`/`gateway-locks`/`gateway-spool`）以及
`.DS_Store` 之类隐藏文件都不算部署产物。

刚检出的仓库还没有 `deploy/.runtime`，先跑一次，确认没有 FAIL、只有预期的 PENDING：

```bash
python3 scripts/dev-env/check-async-job-prereqs.py \
  --env-file deploy/compose.env -f deploy/compose.yaml
```

```text
阶段: pre — auto:未发现容器、deploy/.runtime 或私有配置(尚未部署)
...
[PENDING] 前置 4  真实引擎与实例注册
        … 私有配方尚未生成:…/deploy/.runtime/private/transcription-recipe.json;部署准备阶段按 compose-minimal 生成后,engine-registration 才能读取
[PENDING] 前置 5  gateway result_bucket 存在且被创建/版本化
        … 私有 gateway 配置尚未生成:…/deploy/.runtime/private/gateway-file-asr.json;部署准备阶段从 deploy/private/*.example.json 复制后,才能核对 result_bucket
```

完成第 3–8 节并 `up` 之后再跑一次。此时 `deploy/.runtime` 已存在，`auto` 已按部署后判定；
部署门禁建议显式 `--phase post`，避免私有配置漏生成时被 `PENDING` 掩盖：

```bash
python3 scripts/dev-env/check-async-job-prereqs.py \
  --env-file deploy/compose.env -f deploy/compose.yaml --phase post
```

预期 `PENDING` 全部变成 `PASS`。部署后仍出现 `PENDING` 或残留 `FAIL`，说明私有配置、
引擎注册或授权确实没做完：`PENDING` 只表示"还没到该阶段"，不表示"以后会自动满足"。

选项：

| 选项 | 作用 |
| --- | --- |
| `--env-file PATH` | 指定环境文件（默认 `deploy/compose.env`） |
| `--compose-file PATH`、`-f PATH` | 指定 Compose 定义（默认 `deploy/compose.yaml`；`-f` 与 `docker compose` 一致） |
| `--phase auto\|pre\|post` | 判定阶段；`auto` 按容器、`deploy/.runtime`、私有配置目录判定，`pre`/`post` 为显式覆盖 |
| `--offline` | 跳过 psql / HTTP / Docker 探测，只做静态判定 |

退出码 `0` 表示没有 FAIL（允许 SKIP 与 PENDING，摘要会点名 PENDING 是"尚未到该阶段"）；
`1` 表示至少一项 FAIL，或 env 文件不存在（此时直接打印生成该文件的指引，不输出六项判定）；
`2` 表示 compose 文件缺失/不可读或参数错误。每项一行 `[PASS]`/`[FAIL]`/`[PENDING]`，
FAIL 与 PENDING 下逐条给出原因，末尾分别汇总编号。

判定边界：

脚本用部署机的 `python3` 运行，兼容 Python 3.9 及以上（不依赖 `uv` 或仓库虚拟环境）。

- 静态判定读 env、`deploy/compose.yaml` 的服务块（键必须在 `environment` 里且非空，注释里的
  字样不算接线）、私有配置文件形态（存在性、mode、`result_bucket` 一致性、
  `network_mode`/`depends_on` 声明）；
- 运行时判定只做 `SELECT`（`has_table_privilege`/`has_column_privilege` 与 `LIMIT 0` 列探测）、
  引擎 `/health` GET 和 `docker inspect` 读取；
- 权限项覆盖 writer 的 17 张表 `SELECT`、15 张表的列级 `INSERT`/`UPDATE`（含 `speech_jobs`
  的 `error_code` 等终止写列），以及派发角色的启动探测与认领后写入列（列数随写面增长，
  以闸门实际输出为准）；
- 没有 `psql`、Docker daemon 或数据库从宿主机不可达时，相应运行时证据标 `SKIP` 并说明原因；
- 对象桶的存在与版本化需要签名凭据，脚本不持有凭据，因此该项恒为 SKIP，靠 `minio-init`
  输出核对；
- 私有 JSON 的坏文件与坏字段分开点名：不是合法 JSON、缺 `objects`、缺
  `objects.result_bucket` 各有提示，运维能直接判断该修文件还是修字段；
- 授权形状的最终权威是进程启动校验与 `docker logs`；脚本通过不等价于启动验证通过。

把任一项人为置为不满足（例如清空 `SPEECHNEXUS_JOB_WRITER_DATABASE_URL`）时，对应项会
FAIL 并点名该键（以下为实测输出节选，阶段为部署后）：

```text
阶段: post — auto:deploy/.runtime 已存在
[FAIL] 前置 1  API 侧 Job writer / admission DSN 与 writer 权限
        ✗ SPEECHNEXUS_JOB_WRITER_DATABASE_URL 为空;POST /v1/nexus/jobs 直接返回 503 job_unavailable
...
结果: 5/6 PASS;0 PENDING;1 FAIL
FAIL(配置写了但不对,任何阶段都必须修):前置 1
```

### 12.2 三态判定的实测输入

在 `/private/tmp/sn-572b`（基于 `decf0104`）、以 `deploy/compose.yaml` 与合成 env 执行，
2026-11-08：

| 输入 | 结果 |
| --- | --- |
| 刚检出（无 `deploy/.runtime`）、`--env-file deploy/compose.env.example`、默认 `auto` | 阶段 `pre`；前置 1/3 因 Job DSN 为空 FAIL，前置 4/5 为 PENDING（私有配方与 gateway 配置尚未生成），前置 2/6 PASS；退出码 `1` |
| 只执行 `mkdir -p deploy/.runtime/{private,gateway-locks,gateway-spool}`、未生成任何文件，默认 `auto` | 阶段仍为 `pre`（空骨架不是部署产物）；前置 4/5 保持 PENDING；退出码 `1` |
| 已生成两份 gateway 私有配置但 `result_bucket=speechnexus-results-typo`（与 env 的 `speechnexus-results` 不一致），`--phase pre` | 前置 5 FAIL 并点名两份配置；`--phase pre` 没有把它降级成 PENDING；退出码 `1` |
| 私有配置与 env 一致并补齐 `transcription-recipe.json`，三条 Job DSN 为指向未启动 `postgres` 的合成占位身份 | 阶段 `post`（`auto` 看到已生成的私有配置）；6/6 PASS、退出码 `0`；运行时项均 SKIP（数据库、引擎、对象存储从本机不可达） |
| 一次性 PostgreSQL 中写入 `engine_instances.instance_state=draining`（实例行已存在），`--phase pre` | 前置 4 FAIL 并点名 `instance_state=draining`；删除该实例行后同一输入在 `pre` 为 PENDING、`post` 为 FAIL |

阶段语义由 `tests/unit/test_async_job_prereqs_phase.py` 固定：把桶名不一致改成 PENDING、
把私有配置缺失改回 FAIL、把已注册但非 ready 的实例改回 PENDING、删掉阶段判定、让
`resolve_phase` 恒返回 `pre`、或让 `main()` 忽略 `--phase`，都会让对应用例变红。

## 13. 失败排查

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

## 14. 本机验证记录与未验证项

本片在 `/private/tmp/sn-572`（基于 `origin/main` 的 `cc22f6d0`）执行，2026-11-08。
一次性 PostgreSQL 16.15 容器 `sn572-pg`（`127.0.0.1:55492`）用于迁移与授权验证，
用完即删；它不是产品部署证据。

已在本机执行：

| 命令 / 操作 | 结果 |
| --- | --- |
| `docker compose ... config --services`（基础 / `job-dispatch` / `migration`） | 分别解析出 5 / 6 / 6 个服务，见第 3 节 |
| `docker compose ... --profile job-dispatch config --format json` | 打印 `service:gateway`、`restart: True`、两个桶名与 `healthcheck: False` |
| `uv run speechnexus-job-migrate --schema speech`（迁移 DSN 指向一次性 PG） | 退出码 `0`，无 stdout |
| `select version_num from speech.alembic_version` | 一行非空版本号（与发布制品的唯一 head 一致） |
| 15 张 Job/准入关系计数 | `15` |
| 第 4 节的 DSN 生成脚本（作用于 env 副本） | 输出三个角色名，值未打印，mode `0600` |
| 第 6 节的角色与授权脚本（DSN 派生的角色名与口令） | 输出 `grants: 220 statements`（该轮基线的快照；条数随授权函数变化，以 `--print-statements` 为准），退出码 `0` |
| `JobRuntime.open()`（用第 6 节建出的 writer/admission 角色） | 输出 `OPENED: PublicJobs jobs is not None: True`、`CLOSED` |
| 撤销 writer 的 8 张表 `SELECT` 后再次 `open()` | 报 `Public Job writer startup rejected`；恢复授权后再次 `OPENED` |
| worker 同款列权限探测、认领后写入列与 owner/DDL 守卫（`f`） | 全部通过 |
| 同源断言测试（`tests/unit/test_async_job_prereqs_check.py`） | 5 项通过；临时删掉生产的一条探测后第 1 项变红 |
| `check-async-job-prereqs.py`（Python 3.9.6 / 3.12.14 解析并运行 `--offline`） | 均通过；语法按 3.9 校验 |
| `check-async-job-prereqs.py` 的全部几种输入 | 见下 |
| `python3 scripts/dev-env/check-doc-refs.py` | `RESULT PASS`（refs=88，EXTERNAL=1，OK=87） |
| `ruff format --check .` / `ruff check .` | 均通过；文件数随仓库变化，以命令实际输出为准，不引用固定值 |
| `uv run mypy` | 新增脚本无错误；仓库其余 5 个文件在 macOS 上报 `pidfd_*` 缺失（Linux 限定的既有问题，与本次改动无关） |

自检的实测输入：

| 输入 | 结果 |
| --- | --- |
| 六项静态满足的 env 副本（DSN 指向未发布的 `postgres` 主机名） | 6/6 PASS，退出码 `0`；运行时项均 SKIP 并给出原因 |
| 同上但 `SPEECHNEXUS_JOB_WRITER_DATABASE_URL` 留空 | 前置 1 FAIL，其余 5 项 PASS，退出码 `1` |
| 同上但 DSN 指向已迁移并授权的 `127.0.0.1:55492` | 前置 1/2/3 PASS 且带运行时证据；前置 4 FAIL（无 ready 实例），退出码 `1` |
| 同上但把 `SPEECHNEXUS_EXECUTION_RESULT_BUCKET` 改成别的桶名 | 前置 5 FAIL，点名两份私有配置的 `result_bucket` 不一致 |
| 同上但撤销 writer 的 8 张表 `SELECT` | 前置 1 FAIL，逐条列出缺的 `SELECT`（旧版脚本在同一输入上 PASS） |
| 同上但把 `alembic_version` 改回 `0046_public_job_intents` | 前置 2 FAIL，点名落后 head 并列出缺的 `0047`/`0048`（旧版 PASS） |
| 同上但撤销派发角色的 `UPDATE (error_code)` | 前置 3 FAIL，点名 `speech_jobs.error_code`（旧版 PASS） |
| api 的两条 DSN 只出现在注释里 | 前置 1 FAIL，说明 `environment` 未接线（旧版 PASS） |
| `SPEECHNEXUS_PRIVATE_CONFIG_DIR=./.runtime/private` | 前置 4/5 按 `deploy/` 解析到私有配置（旧版判为不存在） |

未在本机验证（附原因）：

| 命令 / 操作 | 原因 |
| --- | --- |
| `docker compose ... up -d --build` 完整参考部署 | 需要构建 Python 镜像、MinIO 与 PostgreSQL 全套拓扑；本片范围是手册与自检，不做端到端启动 |
| `docker compose ... up -d api` 加载新 Job 身份、`logs api` 判定 | 同上，没有运行中的参考部署 |
| `docker compose ... --profile migration run --rm migrate` | 迁移入口本身已用同一命令对一次性 PG 验证；Compose 侧只验证了解析 |
| `minio-init` 与 `bucket ready ... versioning enabled` 输出 | 需要运行中的 MinIO 与 bootstrap 凭据 |
| `engine-register` 与 `"outcome": "registered"` | 没有真实引擎；按项目约定不得用 fake 引擎代替 |
| `docker compose ... up -d`（`job-dispatch` profile）与 `docker inspect` 网络命名空间 | 没有启动参考部署；第 6 项的真实拓扑未复现，判定逻辑只在一次性合成容器上试过 |
| `POST /v1/nexus/assets`、`POST /v1/nexus/jobs`、`GET /v1/nexus/jobs/{id}` | 需要完整参考部署、真实引擎与 ready 输入资产 |

### 2026-11-13 增补（#649，基线 `origin/main` `60b70d57`）

本轮只补齐参考部署里属于配置 / 装配 / 文档 / 初始化脚本的前置；真实引擎端到端不在此列，
#572 保持打开。验证在独立 worktree 中执行，容器 PostgreSQL 16.15 与容器 MinIO 来自
`deploy/compose.yaml`，测后删除。

已在本机执行：

| 命令 / 操作 | 实际结果 |
| --- | --- |
| `docker compose build minio-init`（`60b70d57` 原状） | 失败：`Distribution not found at: file:///app/asset-inspector`。`deploy/images/python.Dockerfile` 未 COPY `asset-inspector` workspace 成员；补齐后构建成功 |
| `--profile job-dispatch config --format json` | 服务 `api gateway job-dispatch-worker minio minio-init postgres`；worker `entrypoint = ["python", "/opt/speechnexus/job-startup-gate.py"]`、`network_mode = service:gateway`、两个桶均为 `speechnexus-results`，并挂载闸门脚本与私有配置目录 |
| `--profile migration run --rm migrate` | 退出码 `0`；`speech.alembic_version = 0052_quarantine_releases`，`speech` schema 82 张表 |
| `run --rm minio-init` | `bucket ready: speechnexus-inputs (versioning enabled)`、`bucket ready: speechnexus-results (versioning enabled)` |
| `uv run --all-packages python scripts/dev-env/provision-job-roles.py`（对容器 PG） | `job roles ready: sn_job_writer sn_job_admission sn_job_dispatch; grants: 220 statements`（该轮基线快照，条数以 `--print-statements` 为准），退出码 `0` |
| 启动闸门成功路径（容器内，真实 PG/MinIO） | `job dispatch startup gate: OK`，随后 exec 到 `gate-exec-reached-worker` |
| 启动闸门 + 未创建的 result 桶 | 退出码 `1`：`FAIL: bucket ... (SPEECHNEXUS_EXECUTION_RESULT_BUCKET) does not exist`，加两份私有配置 `result_bucket` 不一致；没有 exec，worker 未启动 |
| 启动闸门 + 未启用版本化的 result 桶 | 退出码 `1`：`FAIL: bucket speechnexus-unversioned (...) is not versioned` |
| 启动闸门 + 撤销派发角色 `SELECT ON speech.coordination_jobs` | 退出码 `1`：`FAIL: dispatch role cannot run the startup probe on coordination_jobs`，点名五组授权 |
| 启动闸门 + 撤销 `speech.alembic_version` 只读 `SELECT`（模拟本次之前的旧部署） | 旧行为是 `pending: migration head not verifiable` + 退出码 `0`；返修后改为 `FAIL` 点名该表与 `GRANT SELECT ON speech.alembic_version TO sn_job_dispatch`（升级前置），退出码 `1` |
| 反例：从 `deploy/init-object-storage.py` 删掉 result 桶条目 | `tests/ci/test_reference_deploy_job_prereqs.py` 1 项 FAIL；`git checkout --` 恢复后 14 项 OK |
| `python3 -m unittest discover -s tests/ci` | 91 项 OK |
| `python3 scripts/dev-env/check-doc-refs.py` | `RESULT PASS` |
| `ruff check .` / `ruff format --check .` / `uv run mypy --platform linux` / `uv run lint-imports` | 全部通过；`mypy` 1413 个源文件无错误 |

未在本机验证（附原因）：

| 命令 / 操作 | 原因 |
| --- | --- |
| 完整 `up -d --build`（`api`、`gateway` 与 `job-dispatch` profile）与 `/readyz` | 本轮只起容器 PG/MinIO 并跑一次性迁移、桶初始化与启动闸门；完整拓扑还需要私有配置与真实模型窗口 |
| `engine-register` 与 `asr`/`tts` Job 端到端 | 没有真实引擎；按项目约定不得用 fake 引擎替代真实部署验收 |
| worker 在 `network_mode: service:gateway` 下真实 claim、结果发布与命名空间重建 | 需要完整参考部署；本轮只证明闸门在 worker 进程之前失败并点名 |

### 2026-11-13 增补（#680，基线 `origin/main` `4cfdfe77`）

本轮为第 7 步补音频检查器前置（第 10 节），并在独立 worktree `/private/tmp/sn680` 上按该节
命令实跑。PostgreSQL 16.15、MinIO、检查器与 API 全部来自 `deploy/compose.yaml`（Compose
项目名 `sn680`），测后连同数据卷一并删除。检查器镜像与 API 镜像都由本轮从仓库源码构建。

已在本机执行：

| 命令 / 操作 | 实际结果 |
| --- | --- |
| `--profile asset-inspection config --services` | `api asset-inspector gateway minio minio-init postgres`；不带 profile 时仍是 `api gateway minio minio-init postgres` |
| `--profile asset-inspection up -d --build asset-inspector` | 镜像构建成功，容器 `Up (healthy)`；`docker inspect` 显示只接入 `sn680_asset-inspection`、`HostConfig.PortBindings` 为空、`PidsLimit=64`、`Memory=805306368`、`CapDrop=["ALL"]`、`User=10001:10001` |
| 检查器空令牌（`docker run -e SPEECHNEXUS_INSPECTION_TOKEN= <镜像>`） | 退出码 `1`，`asset inspector configuration error: SPEECHNEXUS_INSPECTION_TOKEN must be a non-empty bearer token` |
| `exec -T api python -c "…/readyz…"` | `{"contract_version":1,"recipe_version":"pcm-decode-1","max_encoded_bytes":4194304,"max_wall_seconds":20.0,"in_flight":0}` |
| `POST /v1/nexus/assets`（`asr_input` + 合成 0.5 s/16 kHz 单声道 WAV；选择器 `audio`，检查器在线） | `201`，`location: /v1/nexus/assets/asset-…`，`status=ready`、`media_type=audio/wav`、`bytes=16044`、`duration_ms=500`、`sample_rate_hz=16000`、`channels=1` |
| 同上但选择器为 `text`（URL/令牌留空） | `503`，`code=asset_inspection_unavailable`、`type=service_unavailable_error`、`param=null`、`retryable=false`、响应头 `x-should-retry: false`；`speech_assets` 行数前后不变 |
| 同上但保留 `audio` 而停掉检查器容器 | 与上一行相同的 503 响应体；同时 API `RestartCount` 仍为 `0`、`State` 为 `running`、`/readyz` 仍 `200` |
| 同上但 `SPEECHNEXUS_AUDIO_INSPECTION_RUNTIME_ROOT=/var/lib/sn/inspection`（`text` 部署） | API 启动失败：`ValueError: SPEECHNEXUS_AUDIO_INSPECTION_RUNTIME_ROOT no longer select a local inspection process; …` |
| 同上但 `SPEECHNEXUS_AUDIO_INSPECTION_MEMORY_BYTES=536870912` | API 启动失败：`ValueError: SPEECHNEXUS_AUDIO_INSPECTION_MEMORY_BYTES is owned by the inspector container now, set SPEECHNEXUS_INSPECTION_MEMORY_BYTES` |
| `audio` 但 URL 与令牌留空 | API 启动失败：`ValueError: SPEECHNEXUS_ASSET_INSPECTION_URL is required when SPEECHNEXUS_ASSET_INSPECTOR=audio` |
| `text` 却发现设置了检查器 URL | API 启动失败：`ValueError: SPEECHNEXUS_ASSET_INSPECTION_URL configured but SPEECHNEXUS_ASSET_INSPECTOR=text; select audio or unset every asset-inspection setting` |
| `text` 却把 `SPEECHNEXUS_AUDIO_INSPECTION_MAX_DURATION_MS` 改成 `60000` | API 启动失败：`ValueError: SPEECHNEXUS_AUDIO_INSPECTION_MAX_DURATION_MS only take effect with SPEECHNEXUS_ASSET_INSPECTOR=audio; select audio or keep the default value` |
| `python3 scripts/dev-env/check-doc-refs.py` | `SUMMARY refs=371 EXTERNAL=1 OK=370`、`RESULT PASS` |
| `ruff format --check .`（0.17.2，含 Markdown 内的 Python 代码块） | `1809 files already formatted`，退出码 `0` |

为了让 API 在本机起来，用了两处**非生产**改动。它们只存在于 `/private/tmp`，不改变任何
仓库文件，也不构成第 10 节的步骤：

1. 一次性 override 文件清空 `api` 的 `depends_on`——本机没有真实 gateway 与引擎，而
   `deploy/compose.yaml` 里 `api` 依赖 `gateway: service_healthy`。
2. 私有配置目录用 `deploy/private/*.example.json` 派生的合成配方（把
   `binding_publication_id` 对齐 `binding_revision_id` 并重算 RFC 8785 摘要），并给
   `sn_api` 角色授予 `SELECT/INSERT/UPDATE/DELETE ON ALL TABLES IN SCHEMA speech`——范围宽于
   生产最小授权。示例配方原样加载会在 `TranscriptionRecipe.__post_init__` 处失败
   （`The published and execution binding revisions must match`）；受审 private 配置仍只能
   由部署方提供。

未在本机验证（附原因）：

| 命令 / 操作 | 原因 |
| --- | --- |
| 第 11 节的 `POST /v1/nexus/jobs` 与后续状态轮询 | 需要真实引擎与受审配方；本机只有合成私有配置 |
| 受审配方 + 最小授权的完整 `up -d --build`（`api`、`gateway`、`job-dispatch`） | 同上；本轮只证明检查器装配与上传判据 |
| `make stack-config` 对三个 profile 的解析 | 未执行；本轮只按 profile 逐次跑 `config --services` |
