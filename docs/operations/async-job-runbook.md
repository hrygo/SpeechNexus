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

下表的症状是结论；每一处闸门跑在哪一层、实测输出形态与退出码，见[启动闸门的失败输出形态](startup-gate-output.md)。

| 编号 | 前置 | 缺失时的可观察症状 | 缺它的 Job 表现 |
| --- | --- | --- | --- |
| 1 | API 的 `SPEECHNEXUS_JOB_WRITER_DATABASE_URL` 与 `SPEECHNEXUS_JOB_ADMISSION_DATABASE_URL` | `POST /v1/nexus/jobs` 返回 `503 job_unavailable`，`message` 点名空白的键名（两条都空则两条都列，只空 admission 时由创建路径点名它） | 没有 Job 可被 claim |
| 2 | 迁移到唯一 head（`migration` profile，`speechnexus-job-migrate --schema speech`） | Job/准入关系不存在，迁移版本表为空；worker 闸门先比对迁移头并点名 `speech.alembic_version is missing: run the migration profile`（一行 `FAIL:`、退出码 `1`）。API 启动期不是一行 `FAIL:`，而是 traceback + 退出码 `3`：迁移预检在 ASGI lifespan 里跑，uvicorn 启动失败即 `sys.exit(3)`；API 里以一行 `FAIL:` 结束的只有**同步装配**的结算预检（[启动闸门的失败输出形态](startup-gate-output.md)） | 创建失败或进程启动即退出 |
| 3 | 派发角色与五组 grant（启用 `voice_validation` 再加两张 `SELECT`） | worker 的启动闸门点名缺授权的表/列并退出，`restart: unless-stopped` 下形成重启循环 | 启动期失败，不领取租约 |
| 4 | 真实引擎与已注册 `ready` 实例（`engine-registration` profile，同一次写入实例、`host_nodes` 与 `workspace_quotas`），且该 binding 已发布、该工作空间有配额行；readiness 收据未过期（`engine-readiness` profile 定时续期） | 未发布 binding 时创建返回 `422 model_not_published`（`message` 点名发布 binding 与注册引擎）；有 binding、该工作空间没有配额行时返回 `422 workspace_not_configured`；两者都在、但没有匹配的 `ready` 实例时返回 `429 job_capacity_exhausted`（`message` 点名未注册的引擎/实例，已注册但非 `ready` 时点名现有状态）；实例在但收据过期返回 `503 engine_not_ready`（非 `job_unavailable`） | 分别停在绑定解析、配额判定与容量判定，没有可执行实例 |
| 5 | gateway 的 `result_bucket` 存在、被 `minio-init` 创建并启用版本化，且与 env 一致 | worker 的启动闸门点名缺桶/缺版本化/配置不一致并退出 | 启动期失败，不领取租约 |
| 6 | `network_mode: service:gateway` 下的启动顺序与网关重建后的 worker 重建 | worker 命名空间失效或指向旧 gateway | 进程在，网关调用全部失败 |

六项之外还有三项创建 Job 的硬前置，都在第 4 步里一并取证：

- **已发布 binding 与完整 `execution_defaults`**（§7.1）。没有任何已发布 head 时创建返回
  `422 model_not_published`；已发布但定义的 `execution_defaults` 缺项时返回
  `422 job_binding_incomplete`。这两项与引擎无关，从空库起是**最先**出现的错误。
- **工作空间配额行**（§7.3）。`workspace_quotas` 缺行返回 `422 workspace_not_configured`；
  它只由引擎注册写入，没有独立管理端点。
- **上传判定**：`asr_input`/`voice_reference` 上传必须先由 `asset-inspector` 部署单元判定。
  默认部署不装配它，音频上传恒为 `503 asset_inspection_unavailable`；装配与验证见第 10 节。

判定顺序固定为 binding → 配额 → 容量，所以从空库照本文执行时，看到 `429
job_capacity_exhausted` 就说明前两项已经成立（`speech/application/public_jobs.py:822`、
`public_job_admission.py:196`、`:248`）。

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
Job/事件/提交写入，admission 负责原子准入的配额、租约与协调写入。两个 DSN 指向不同数据库
时 API 启动即拒绝。缺其中任一个时，请求只有走到准入才会得到 `503 job_unavailable`；在还没有
已发布 binding 的部署上（例如刚按最小部署手册起好的空库），同一个请求先停在绑定解析，
返回的是 `422 model_not_published`（`job_errors.py:71`），看到它不等于 writer/admission
已配好：`503 job_unavailable` 只覆盖准入阶段的存储不可用（`job_errors.py:133`）。该 503 的
`message` 点名空白的键名：只空 admission 时由创建路径点名
`SPEECHNEXUS_JOB_ADMISSION_DATABASE_URL`（读与取消不受影响），两条都空时路由层同时点名
两条键。

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
docker compose --env-file deploy/compose.env -f deploy/compose.yaml \
  exec -T postgres psql -U speechnexus -d speechnexus -Atc \
  'select version_num from speech.alembic_version'
```

这里经 Compose 服务名 `postgres` 进入容器，不写容器名：`deploy/compose.yaml` 第 1 行的
`name: speechnexus` 与 `COMPOSE_PROJECT_NAME` 决定实际名字（`<项目名>-postgres-1`），
写死会在换了项目名的部署上直接得到 `No such container`。

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
版本号非空。下面的查询走容器内 `psql`：DSN 的 host 是 Compose 服务名 `postgres`，
**宿主解析不到**；宿主也不保证装了 `psql`（`DEL-535` 的窗口里是 `command not found`、退出
`127`）。因此不把 DSN 交给宿主 shell，而是用 owner 身份进 `postgres` 容器执行——容器内本地连接
不需要口令，所以命令行里没有口令：

```bash
docker compose --env-file deploy/compose.env -f deploy/compose.yaml \
  exec -T postgres psql -U speechnexus -d speechnexus -Atc \
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
| api 容器 `Restarting`，日志首行 `FAIL: the settled-capacity preflight cannot read ...` | 该库未执行迁移；同步 TTS 结算预检读到缺失的准入表后按迁移指引拒绝启动（退出码 `1`），先跑 `migration` profile |
| worker 日志 `FAIL: speech.alembic_version is missing: run the migration profile ...` | 启动闸门先比对迁移头、再跑角色守卫；schema 或版本表不存在时点名迁移，不再抛 `InvalidSchemaName` traceback |

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

用 worker 启动时**同样的探测语句**核对派发角色（这只是列权限，改动数据为 0 行）。同样走容器内
`psql`：DSN 的服务名宿主解析不到，宿主也没有 `psql`；角色名与数据库名取自该 DSN 的用户名与
路径（本例为 `sn_job_dispatch` / `speechnexus`），改名时同步替换：

```bash
for stmt in \
  "SELECT job_id FROM speech.speech_jobs LIMIT 0" \
  "SELECT job_id, event_seq, state, occurred_at_us, kind, progress_unit, progress_completed, progress_total FROM speech.speech_job_events LIMIT 0" \
  "SELECT job_id FROM speech.coordination_jobs LIMIT 0" \
  "SELECT operation_id, request_document FROM speech.admission_operations LIMIT 0" \
  "SELECT attempt_id FROM speech.admission_attempts LIMIT 0" \
  "SELECT request_document FROM speech.admission_requests LIMIT 0" \
  "SELECT lease_id, lease_state, cleanup_state, deadline_at_us FROM speech.admission_leases LIMIT 0" \
  "SELECT artifact_id FROM speech.speech_artifacts LIMIT 0"; do
  docker compose --env-file deploy/compose.env -f deploy/compose.yaml \
    exec -T postgres psql -U sn_job_dispatch -d speechnexus -X -q -v ON_ERROR_STOP=1 -c "$stmt" >/dev/null \
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

## 7. 第 4 步：前置 4 —— 已发布 binding、工作空间配额与真实引擎实例

这一段覆盖四件事，缺任何一件 `POST /v1/nexus/jobs` 都在准入前失败：已发布 binding
（§7.1）、注册产出的 `ready` 实例（§7.2）、注册同时写入的 `workspace_quotas` 与
`host_nodes`（§7.3），以及 readiness 收据的续期（§7.4）。顺序是硬的：§7.2 写进实例的
binding 身份必须等于 §7.1 发布出来的 `revision_id`；反过来先注册，只会得到一个 binding
head 与实例对不上的 `ready` 实例，创建 Job 仍然失败。

创建 Job 时的判定顺序是固定的，据此可以判断缺的是哪一项：先解析 binding（无已发布 head →
`422 model_not_published`，`speech/application/public_jobs.py:835`），再校验输入资产（未就绪 →
`422 asset_not_ready`，`speech/application/public_jobs.py:1490`），然后取该工作空间的配额行（无行 →
`422 workspace_not_configured`，`public_job_admission.py:201`），最后才找 ready 实例
（找不到 → `429 job_capacity_exhausted`，`public_job_admission.py:253`）。因此「没有实例时
返回 429」只在 binding、输入资产与配额都已成立时成立；空库上最先看到的是 422。

从空库到第一个 `202` 的完整顺序（含输入资产与 `asset-inspector` 这一步）、每步的预期输出与两条反例的实跑记录见[从零实跑 §13](compose-minimal-from-zero.md#13-从空库到第一个-202-的步骤清单)。

### 7.1 发布 binding：注册与创建 Job 的前置

入口是公共 HTTP 契约的三条已装配写路径（[binding 生命周期](binding-lifecycle.md)），不是
运维 CLI：`POST /v1/nexus/admin/model-bindings`（建草稿）、`POST
…/model-bindings/{binding_id}/publications`（发布）、`POST
…/model-bindings/{binding_id}/revisions/{revision}/activations`（选择 head）。三个身份在
[最小部署手册 §6](compose-minimal.md#公开目录artifact资源池与-binding-身份) 配置，缺任一个
只影响对应端点：`sn_binding_writer`（建草稿，未配置 `503 binding_unavailable`）、
`sn_binding_lifecycle`（发布/激活，未配置 `503 binding_result_unknown`、`retryable: false`）、
`sn_binding_admin_reader` 与 `sn_catalog_reader`（核对读取）。以下示例宿主端口按实际值替换。

**第 1 步 建草稿。** 请求体字段没有一处是「随便填的常数」，每一处都有固定的事实来源；
`execution_defaults` 与 `execution_recipe.capacity_manifest_id` 是后面两步的硬引用：

```bash
curl -sS -X POST http://127.0.0.1:8000/v1/nexus/admin/model-bindings \
  -H 'content-type: application/json' \
  -H "Idempotency-Key: $(python3 -c 'import uuid; print(uuid.uuid4())')" \
  -d @binding-draft.json
```

**`binding-draft.json` 不是 `--emit-binding-request` 输出的原样。** 那个工具产出的是
**生产者路由**的 `BindingDefaultsCreateRequest`（`contracts/binding-defaults-producer.openapi.yaml`，
由 `SPEECHNEXUS_BINDING_DEFAULTS_WRITER_DATABASE_URL` 开启）：工具的输出来自
`binding-definition.json`，带 `parameter_defaults`，不产出 `execution_defaults`。本节这条运维
写路径按 `ModelBindingCreateRequest` 校验（`contracts/admin-openapi.yaml`，
`additionalProperties: false`，没有 `parameter_defaults`），原样 POST 会得到
`400 invalid_binding_request`（`speech/interfaces/http/bindings.py:84`）。两个契约共用
`engine`、`artifact_set`、`execution_recipe`、`parameter_domains`、`release_tier`，差别只在
默认值那一块：

- 不要把 `parameter_defaults` 改个名顶替：生产者侧是稀疏参数图（`asr`/`tts`/`output` 三段，
  只给声明过的字段），运维侧 `execution_defaults` 是完整执行参数（只有 `asr`/`tts`，
  `additionalProperties: false`）。照抄改名会让 `output` 段与缺项一起被拒。
- `execution_defaults.asr` 的 13 个必填字段：`language`、`turn_detection`、`finalization`、
  `preview_interval_ms`、`max_segment_ms`、`rollback_tokens`、`final_deadline_ms`、
  `auxiliary_deadline_ms`、`alignment`、`diarization`、`vocabulary_set_revision`、
  `temperature`、`timeout_seconds`；`execution_defaults.tts` 的 11 个：
  `language`、`text_commit_mode`、`audio_delivery`、`speed`、`instructions`、
  `pronunciation_revision`、`seed`、`planner`、`sample_rate_hz`、`max_output_audio_ms`、
  `timeout_seconds`。
- 取值仍要逐项来自部署事实，工具只提供 `engine` 与 `artifact_set` 的可核验部分。

`release_tier` 由部署申请：**实验部署填 `experimental`**，不要沿用样例与
`deploy/engines/file-asr-audio/binding-definition.json` 里的 `production`。工具输出和样例文件
都只是请求目标，不代表生产资格，也不产生资格回执。

预期 `201`，响应是 `ModelBindingDraftRevision`：`revision`（十进制字符串）、
`revision_id`（由注册表分配、调用方不能指定，形如 `mbrev-…`，`binding_drafts_postgres.py:67`）、
`publication.status: "draft"`，且 `publication.capacity_manifest_id` /
`qualification_report_id` 为 `null`。

| 请求字段 | 取值来源 | 约束与依据 |
| --- | --- | --- |
| `binding_id` | 部署自行分配并保持稳定 | `Identifier`：`^[a-z0-9][a-z0-9._:-]{1,126}[a-z0-9]$`。它同时是 `GET /v1/models` 的 `id` 与 `POST /v1/nexus/jobs` 的 `model` 取值（目录 `id` 即 `object_id`，`model_catalog.py:98`；Job 侧按 head 的 `object_id` 解析，`public_jobs_postgres.py:227`） |
| `logical_model` | 部署自行分配的逻辑名 | `^[a-z0-9][a-z0-9._-]{1,63}$`；只投影为目录的 `nexus.logical_model`（`model_catalog.py:103`），不要求等于引擎模型名或 gateway 的 `served_model` |
| `tasks` | 要跑的任务：ASR 填 `["asr_file"]`，TTS 填 `["tts_file"]` | 创建 Job 按 kind 校验（`speech/application/public_jobs.py:54`），不含对应任务 → `422 model_not_published` |
| `engine.kind` / `image_digest` / `runtime_version` | 部署实际运行的引擎镜像事实（`image_digest` 为 `sha256:<64 位小写十六进制>`） | 草稿只做结构校验，不查外部引用是否存在（[不可变草稿](../components/model-binding-drafts.md)） |
| `artifact_set` | 固定制品清单：file-ASR 由 `scripts/dev-env/verify-file-asr-artifacts.py --emit-binding-request` 从 `artifact-pin.json` 生成；从该输出里取 `artifact_set`（与 `engine`）填进本请求体，其余段按本节规则写 | 同上；真实值必须来自实际挂载的制品，不能手造摘要 |
| `execution_recipe.recipe_revision` / `stage_roles` | 配方的执行角色：文件 ASR 为 `asr_batch` | gateway 只接受 `task_kind=asr_file` + `stage_role=asr_batch` 的命令（`file_asr_command.py:103`） |
| `execution_recipe.capacity_manifest_id` | 部署选定的容量清单 ID，**必须与第 2 步提交的 `capacity_manifest_id` 逐字相同**，且**必须等于 §7.2 注册时的 `manifest_id`** | 与草稿不符 → 第 2 步 `422 capacity_manifest_mismatch`（`binding_lifecycle_postgres.py:292`）；与实例不符 → 创建 Job 时 `422 job_binding_incomplete`（`public_job_admission.py:655`） |
| `parameter_domains` | 该 binding 允许的参数域 | 结构校验在草稿阶段完成 |
| `execution_defaults` | 完整执行参数：ASR 的 13 个必填字段与 TTS 的 11 个必填字段全部给齐（见 `contracts/model-binding-execution-defaults.schema.json`），取值来自部署配置与该 binding 的固定默认 | **创建 Job 的硬前置**：准入从这里取语言、分段、期限等全部执行参数（`public_job_admission.py:134`），缺一项就是 `422 job_binding_incomplete`。它是**只写**字段——写进不可变定义，但 admin 响应把它剥掉（`speech/interfaces/binding_drafts.py:56`），因此核对只能回看这份请求体 |
| `release_tier` | 部署申请的目标等级：实验部署填 `experimental` | 是申请目标，不是资格回执；样例与工具输出里的 `production` 不构成生产资格 |

**第 2 步 发布。** `capacity_manifest_id` 必须等于第 1 步 `execution_recipe` 里的值；
`qualification_report_id` 是部署方声明的资格报告引用；`expected_current_revision` 是提交前
观察到的 head，首次发布为 `null`：

```bash
curl -sS -X POST \
  -H 'content-type: application/json' \
  -H "Idempotency-Key: $(python3 -c 'import uuid; print(uuid.uuid4())')" \
  -d '{"expected_revision":"<第 1 步的 revision>","expected_current_revision":null,
       "capacity_manifest_id":"<与 execution_recipe.capacity_manifest_id 相同>",
       "qualification_report_id":"<部署方声明的资格报告 ID>"}' \
  http://127.0.0.1:8000/v1/nexus/admin/model-bindings/<binding_id>/publications
```

预期 `201` 与更新后的 `ModelBindingRevision`：`publication.status: "published"`、
`published_at` 非空、两个 evidence ID 回显。同一事务插入一行 `speech_publications` 与一行
`speech_binding_publication_evidence`，把 `speech_publication_heads` 指向该 revision，由
触发器推进一次 `speech_catalog_state`，并追加一条 `model_binding.publish` 事件
（`binding_lifecycle_postgres.py:266`）。点名失败：`422 capacity_manifest_mismatch`（清单与
草稿不符）、`409 configuration_changed`（head 已变，消息带观察值）、`409 binding_not_draft`
（目标已发布或已退役，换新 key 也不能重发）、`404 binding_not_found`（revision 不存在）、
`409 idempotency_conflict`（同 key 不同意图）、`503 binding_result_unknown`（未配置写入角色
或提交结果未知，`retryable: false`，只能用原 key 与完整原 body 重放）。

两个 evidence ID 是调用方声明的事实，实现不查它们是否存在或内容是否合格
（[binding 生命周期](binding-lifecycle.md#发布)）；不要为了凑齐发布而伪造清单或资格。
它们证明的是「这次发布引用了哪份清单/报告」，不是「该报告合格」。

**第 3 步 激活（选择 head 并留审计）。** 发布已经让 head 指向该 revision，所以对刚发布的
revision 调一次激活是成功 no-op：只追加一条 `model_binding.activate` 事件，head 与目录
版本都不变（[binding 生命周期](binding-lifecycle.md#激活与回滚)）。它的作用是把 head
选择路径与审计走通；真正回滚时把路径换成目标 revision、`expected_current_revision` 换成
当前 head。

```bash
curl -sS -X POST \
  -H 'content-type: application/json' \
  -H "Idempotency-Key: $(python3 -c 'import uuid; print(uuid.uuid4())')" \
  -d '{"expected_current_revision":"<第 2 步发布的 revision>"}' \
  http://127.0.0.1:8000/v1/nexus/admin/model-bindings/<binding_id>/revisions/<revision>/activations
```

预期 `200` 与同一 `ModelBindingRevision`（`publication.status` 仍为 `published`）。点名失败：
`409 configuration_changed`（消息带本次持锁时观察到的 `expected_current_revision`，不要解析
文案驱动重试）、`409 revision_not_published`（目标已退役或 disabled）、`404 binding_not_found`。

**第 4 步 核对已发布事实。** 只读，两条路径互为交叉检查：

```bash
curl -sS http://127.0.0.1:8000/v1/models
```

预期 `200`，`data` 里是该 binding 的目录项：`id` 为 `binding_id`、`nexus.binding` 为
`{"id": …, "revision": "<十进制文本>"}`（`publication.reference`，`speech/domain/publications.py:61`；**没有
`revision_id`**，`revision_id` 只在 `speech_publication_heads` 与私有输入里用）。
`nexus.readiness` 是**静态投影**（fail-closed），不是实时状态。生产装配不传 readiness 事实
（见 `backend/src/speechnexus_backend/model_catalog_runtime.py:92` 起的装配注释，构造点在 `:112-114`
的 `PostgresModelCatalog(...)`，签名里没有 `readiness=`），缺少证据一律保守取 `false` / `unavailable`，
四项恒为 `configured=true`、`engine_ready=false`、`quality_qualified=false`、
`admission_state=unavailable`，**不随实例注册、实例 `ready` 或 Job `202` 变化**；`unavailable`
只表示该目录没有建立这项事实，不表示引擎故障或不可准入。这套实现也没有可读的实时事实：该 reader
的授权只准入已发布表（`READABLE`，`backend/src/speechnexus_backend/model_catalog_runtime.py:35`），
准入依赖的 `engine_instances`、`workspace_quotas`、`speech_assets` 都在其外，放宽授权会在启动时被拒，
而现场只见通用文案（具体原因被 `from None` 吞掉，见装配注释）。

**不要把上面这段读成「等接入实时事实就对上了」**：契约把 `admission_state` 定义成实时准入判据
（`contracts/speech-openapi.yaml:4063-4070` 的「currently admissible」/「cannot be admitted now」，
另见 `:53-54` 的 `current admission facts`）。但准入按 workspace 计
（`backend/src/speechnexus_backend/speech/infrastructure/public_job_admission.py:695`、`:514`），
本端点没有 workspace 维度（`backend/src/speechnexus_backend/speech/interfaces/http/models.py:42`；
契约 `:56-63` 只有 `X-Request-ID` 与 `If-None-Match`），所以
**二者结构性不符，接入多少实时事实都满足不了该定义**。这是**契约缺陷**，按 `#941` 跟踪
（改口径 / 加 `workspace_id` / 把消费者指向实时面），**不是待接入项**。实时准入不由该端点承载：
判「现在能不能建 Job」看 `POST /v1/nexus/jobs` 的实际应答码（第 11 节），契约声明的实时报告是
`GET /v1/nexus/jobs/admission-readiness`（带 `workspace_id`，当前仍是占位、跟踪 `#863`）。

`DEL-535`（基线 `origin/main` `c0172a22`）当时在这里拿到的是 `503 model_catalogue_unavailable`，
**不是角色没配**：catalog 要求定义里的 `binding_id` 等于发布的 `object_id`
（`model_catalog.py:79`），而那时写路径把 `binding_id` 摘进列、没写进文档，整份目录读面报不可用
（`#891`）。`#894`（`f888cf7c`）已把校验前移到写路径，**此后新建的 binding 都能读到 `200`**。

**仍有一条边界**：`#894` 之前发布的 binding，其不可变文档里没有 `binding_id`，只要它还是该
family 的当前 head，目录就仍然 `503`。目录只列 head 选中的 revision（`model_catalog_sql.py` 的
`list_published_bindings` 与 `speech_publication_heads` 内联），所以恢复方式是**为这个 binding
发布新的 revision**（head 前移即恢复）或**退役该 revision**（退役是可见性墓碑，不回退到旧行）。
历史行不会回填，也不改写不可变记录。

因此 `503` 有两种含义，**不要混**：「head 是本修复之前的发布」与「binding 没发布」。区分它们用
下面三条 SQL。也不要在部署侧放宽 catalog 校验来换一个 `200`：那条校验是对的，缺字段的是生产路径。

```bash
docker compose --env-file deploy/compose.env -f deploy/compose.yaml \
  exec -T postgres psql -U speechnexus -d speechnexus -Atc \
  "select kind, object_id, revision, revision_id from speech.speech_publications where kind='binding'"
docker compose --env-file deploy/compose.env -f deploy/compose.yaml \
  exec -T postgres psql -U speechnexus -d speechnexus -Atc \
  "select object_id, revision, revision_id from speech.speech_publication_heads where kind='binding'"
docker compose --env-file deploy/compose.env -f deploy/compose.yaml \
  exec -T postgres psql -U speechnexus -d speechnexus -Atc \
  "select revision from speech.speech_catalog_state"
```

预期：一行 publication、一行 head（同一个 `revision_id`）、`catalog_state.revision >= 1`
（创建 Job 还要求 catalogue revision 已推进，`speech/application/public_jobs.py:859`）。head 被退役清空后
即使 revision 仍是 `published`，创建 Job 会以 `422 model_binding_not_current` 拒绝
（`speech/application/public_jobs.py:280`）——这与「未发布」是两个不同的码。

**第 5 步 把 `revision_id` 写回两份私有输入。** 参考部署的注册走配方模式，配方与 gateway
端点配置都按名字钉住 binding：不写回，Job 能创建但执行会被拒。

- 私有转写配方（`SPEECHNEXUS_TRANSCRIPTION_RECIPE_FILE`）的 `binding_revision_id` 与
  `binding_publication_id` 都必须等于已发布的 `revision_id`（loader 强制两者相等，且必须
  等于 `runtime_plan.binding.binding_revision_id`，`transcription_recipe.py:217`、`:235`），
  `manifest_id` 必须等于该 binding 的 `execution_recipe.capacity_manifest_id`（`:237`）。
  改动后**必须重算 `recipe_digest`**（RFC 8785，排除该字段自身，`:278`）并重启 `api`，
  否则启动报 `invalid transcription recipe`。
- gateway 私有端点配置（如 `gateway-file-asr-endpoint.json`）的 `binding_revision_id`
  必须等于它——执行命令的身份会被逐字段比对（`file_asr_command.py:108`）；同处的
  `vad_tuning.config_revision` 必须等于 §7.2 注册写入 `workspace_quotas.policy_revision`
  的值（`file_asr_command.py:109`），TTS 对 `binding_policy.config_revision` 有同一约束
  （`file_tts_command.py:161`）。改动后重启 `gateway`。

这两处只影响 **执行**：`202` 只需要数据库里的已发布 binding、配额行与 ready 实例。

**重算 `recipe_digest` 的可执行步骤。** 把下面这段跑在配方上，一次改全绑定身份的三处
（顶层 `binding_revision_id`、`binding_publication_id`、`runtime_plan.binding.binding_revision_id`）
并按 RFC 8785 重算摘要、写回文件。同一段对同步 TTS 的 `speech-recipe.json` 同样适用：TTS
配方没有 `binding_publication_id`，脚本按该字段是否存在处理。`--` 之后是配方路径与新的
`revision_id`：

```bash
REVISION_ID=<已发布 binding 的 revision_id>
uv run --all-packages python - deploy/.runtime/private/transcription-recipe.json "$REVISION_ID" <<'PY'
import hashlib
import json
import sys
from pathlib import Path

import rfc8785

path = Path(sys.argv[1])
revision_id = sys.argv[2]
document = json.loads(path.read_text(encoding="utf-8"))
document["binding_revision_id"] = revision_id
if "binding_publication_id" in document:
    document["binding_publication_id"] = revision_id
document["runtime_plan"]["binding"]["binding_revision_id"] = revision_id
body = {key: value for key, value in document.items() if key != "recipe_digest"}
document["recipe_digest"] = "sha256:" + hashlib.sha256(rfc8785.dumps(body)).hexdigest()
path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
print(f"{path.name}: binding_revision_id={revision_id} recipe_digest={document['recipe_digest']}")
PY
```

同一配方、同一 `revision_id` 重复执行是幂等的：三处身份与摘要都不再变化，文件字节也不变。
配方重写后仍是 `0600` 私有文件（脚本保留原权限，但新建副本时自己 `chmod 600`），然后重启
`api`；`recipe_digest` 与其余字段不自洽时 loader 拒绝启动（转写为
`Invalid private transcription admission recipe`，TTS 为
`Invalid private speech admission recipe <path> (ValueError)`）。

**本节核对状态**：三条 HTTP 命令已在 `DEL-535` 的从空卷实跑中通过（`201`/`201`/`200`，
MegaCube 真实引擎窗口，基线 `c0172a22`；记录见
[从零实跑记录](compose-minimal-from-zero.md)第 11 节）。
本节仍未在**本机**取证（macOS 开发机没有参考部署与引擎）。请求/响应字段与状态码取自
`contracts/binding.openapi.yaml` 与 `contracts/model-binding.schema.json`，事务副作用与
错误码取自 `binding_lifecycle_postgres.py`、`speech/interfaces/http/binding_lifecycle.py`；
「发布即写 head、对同一 revision 再激活是 no-op」这个顺序结论按代码路径推导，不是实测。

### 7.2 注册引擎（同时写入 `host_nodes` 与 `workspace_quotas`）

注册是运维命令 `speechnexus-register-engine`，它把一个**已经运行**的引擎实例写入准入读取
的关系事实并置为 `ready`；同一个事务还写 `host_nodes`（宿主机容量向量）、
`workspace_quotas`（该工作空间的并发 attempt 配额）、`resource_pools`（池 owner 与 fencing
token）。**这两份「容量登记」与「工作空间配额」事实没有别的产品写入入口**：`workspace_quotas`
只有 `_ensure_quota`（`engine_registration.py:477`）会插入，`host_nodes` 只有
`_ensure_host`（`engine_registration.py:353`）会插入（仓库里另一处写入是
`scripts/dev-env/del109-local-gateway-fixture.py` 的本地夹具，不是部署入口）；`POST /v1/nexus/workspaces` 只写
`speech_workspaces`（`workspaces_postgres.py:275`），不产生配额行。边界与参数见
[引擎注册](register-engine.md)。

注册前必须已经发布 binding（否则实例的 binding 身份无从对齐），且引擎 `/health` 从**注册
容器所在的 Compose 网络**可达——不是从操作主机可达。参考部署的 `engine-register` 服务
带有 `extra_hosts: host.docker.internal:host-gateway`，所以示例地址只在容器内有意义：

```bash
docker compose --env-file deploy/compose.env -f deploy/compose.yaml \
  --profile engine-registration run --rm --no-deps \
  --entrypoint python engine-register -c '
import os, urllib.request
url = os.environ["SPEECHNEXUS_ENGINE_HEALTH_URL"].rstrip("/") + "/health"
try:
    with urllib.request.urlopen(url, timeout=5) as response:
        print("http=", response.status, response.headers.get("content-type"))
except Exception as error:
    print("unreachable:", type(error).__name__, error)
'
```

预期打印 `http= 200 None`：第三段是响应的 `content-type`，引擎的 `/health` 不带这个头，所以
实测就是 `None`（`DEL-535`）；返回了 `content-type` 的引擎会打印具体值。`--no-deps` 不重复拉起
`postgres`；探针用镜像自带的 `python`
（`deploy/images/python.Dockerfile:40` 把 venv 放进 `PATH`，Compose 自己的健康检查也用
`python`），不假设镜像装了 `curl`。在操作主机上直接
`curl "${SPEECHNEXUS_ENGINE_HEALTH_URL%/}/health"` 对 `host.docker.internal` 只会得到连接
失败（从空库执行记录里就是 `curl: (52) Empty reply from server`
（`compose-minimal-from-zero.md:190`），注册随即报 `engine_unreachable`），那不是引擎故障。

然后执行一次性注册（`engine-registration` profile）：

```bash
docker compose --env-file deploy/compose.env -f deploy/compose.yaml \
  --profile engine-registration run --rm engine-register
```

预期 stdout 是一个 JSON 文档，含：

```text
"outcome": "registered"
```

完全相同的重跑在健康、身份、owner 与 readiness 记录一致时返回 `"outcome": "already_ready"`。

注册写入的三个身份字段必须与 §7.1 的已发布事实逐字一致，这是从空库起最容易错的一步：

| 注册输入（配方模式 / 手动模式） | 必须等于 | 依据 |
| --- | --- | --- |
| 配方 `binding_revision_id` / `--binding-revision-id` | 已发布 binding 的 `revision_id` | 准入按 `engine_instances.binding_revision_id = <已发布 revision_id>` 找 ready 实例（`public_job_admission.py:217`），并把它与 plan 比对（`:652`） |
| 配方 `manifest_id` / `--manifest-id` | 该 binding 的 `execution_recipe.capacity_manifest_id` | 准入要求 `plan.binding.manifest_id` 与定义里的清单 ID 相等（`public_job_admission.py:655`） |
| 配方 `policy_revision_id` / `--policy-revision` | 部署的 VAD/语音策略版本，且等于 §7.1 第 5 步 gateway 私有配置的 `config_revision` | 该值经注册写入 `workspace_quotas.policy_revision`（`engine_registration.py:479`），准入再把它作为执行的 `policy_revision_id`（`public_job_admission.py:887`）；gateway 逐字段比对（`file_asr_command.py:109`） |
| `--node` / `--gateway` / `--instance` / `--generation` | 部署实参与 gateway 端点配置里的同名身份 | 配方模式下给出与配方不一致的值会以 `<name>_override_conflict` 退出（`engine_registration.py:1078`） |
| `--template-id`、`--config-digest`、`--topology-digest`、`--qualification-digest`、容量向量、`--max-attempts` | 部署与镜像事实；binding 不校验这些值 | `_config_matches`（`engine_registration.py:520`）只把已有行与 plan 自身比对 |

**注册不检查 binding 是否已发布**：整条注册路径没有对 `speech_binding_revisions` 或
`speech_publication_heads` 的查询，`--binding-revision-id` 是调用方声明的字符串（依据
`engine_registration.py` 全文无该表引用推导，未实跑）。所以「注册成功」不能当作「binding
已发布」的证据，两个事实要分别核对。

核对注册事实（只读，同样在容器内执行——DSN 的服务名宿主解析不到，宿主也没有 `psql`）：

```bash
set -a; . deploy/compose.env; set +a
docker compose --env-file deploy/compose.env -f deploy/compose.yaml \
  exec -T postgres psql -U speechnexus -d speechnexus -Atc \
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
| `quota_policy_conflict` / `quota_capacity_conflict` | 该工作空间已有配额行：`policy_revision` 与本次不同，或已有 `max_attempts` 更小（`engine_registration.py:477`） |
| `host_capacity_insufficient` | `--host-capacity`（缺省 `steady + per_slot × max_slots`，且至少覆盖 `peak`，`engine_registration.py:176`）容不下计划，或该节点已有 held/quarantined 占用 |
| `host_architecture_mismatch` / `host_topology_mismatch` | 该 `host_node_id` 已登记过不同的 architecture 或 topology digest（`engine_registration.py:377`、`:382`） |
| 创建返回 `429 job_capacity_exhausted` | 没有 `ready` 实例，或其实例的 binding revision 与请求的发布不一致 |
| 创建返回 `503 engine_not_ready` | 实例仍 `ready`，但 readiness 收据已过期；跑续期（§7.4），不要重新注册新 generation |
| 创建返回 `503 engine_state_mismatch` | 收据身份（instance / generation / topology / fencing token）与固定实例不符，或实例行已不存在；重新注册同一实例，不要新建 generation |
| 创建返回 `503 engine_resource_disabled` | 实例、池、节点或配方审批不处于 active（已停止、排水中或超出审批窗口）；先恢复实例与审批再注册 |
| 创建返回 `503 recipe_not_admitted` | 请求来自另一份配方，与固定实例已批准的配方不一致；按已批准配方重新注册引擎 |
| 创建返回 `503 job_admission_contended` | 版本 CAS 或池 owner fencing 竞争，本次未写行；重试同一请求即可（唯一可重试的准入拒绝） |
| 创建返回 `422 job_request_expired` | 该请求自身的准入截止时间已过（截止时间属于固定意图）；只能提交新请求 |

### 7.3 核对配额与容量登记

注册成功之后，准入要读的三类事实应各有一行。只读核对：

```bash
docker compose --env-file deploy/compose.env -f deploy/compose.yaml \
  exec -T postgres psql -U speechnexus -d speechnexus -Atc \
  "select workspace_id, max_attempts, held_attempts, policy_revision from speech.workspace_quotas"
docker compose --env-file deploy/compose.env -f deploy/compose.yaml \
  exec -T postgres psql -U speechnexus -d speechnexus -Atc \
  "select host_node_id, node_state, memory_architecture,
          allocatable_device_memory_bytes, held_device_memory_bytes,
          allocatable_unified_physical_memory_bytes, held_unified_physical_memory_bytes,
          allocatable_cpu_decode_millicores
     from speech.host_nodes"
docker compose --env-file deploy/compose.env -f deploy/compose.yaml \
  exec -T postgres psql -U speechnexus -d speechnexus -Atc \
  "select instance_id, instance_generation, instance_state, binding_revision_id, manifest_id from speech.engine_instances"
```

预期：`workspace-default` 一行（`max_attempts` 取自 `SPEECHNEXUS_ADMISSION_MAX_ATTEMPTS`，
`policy_revision` 取自配方的 `policy_revision_id`）；`node-local-1` 一行 `node_state=active`，
容量列**按 `memory_architecture` 选维度**核对、不能只看一对列：`device_memory` 拓扑看
`allocatable_device_memory_bytes`；`unified_memory` 拓扑（GB10 等统一内存机型）的 device 维度
本来就是 `0`，容量在 `allocatable_unified_physical_memory_bytes`
（`DEL-535` 实测：`node-local-1|unified_memory` 的 unified 可分配与已占用同为 `12884901888`，
CPU 毫核 `2500/2000`）；`instance-asr-1|1|ready` 一行，其 `binding_revision_id` 等于 §7.1
发布的 `revision_id`、`manifest_id` 等于该 binding 的 `execution_recipe.capacity_manifest_id`。

判定口径：`workspace_quotas` 缺行就是「这个工作空间从没为它注册过引擎」，创建返回
`422 workspace_not_configured`，重试不会改变结果，只能按 §7.2 注册（`public_job_admission.py:172`
的选择是刻意的：空读是确定性配置事实，不是存储故障，所以不回 `503`）。`host_nodes` 的
`allocatable_*` 是注册时写下的宿主机容量，六个维度各自独立、互不代偿：`unified`、`device`、
`host_memory`、`cpu_decode`、`host_pcm_buffer`、`scratch_disk`；拓扑只填其中适用的维度，其余
保持 `0`，所以「有维度非零」不等于「device 列非零」。`held_*` / `quarantined_*` 由准入与租约
结算维护；
注册时的容量校验在 `engine_registration.py:502` 的 `_require_capacity`，准入侧不再重读
`host_nodes` 做容量算术，而是要求存在 ready 实例与 active 池 owner
（`public_job_admission.py:206`、`:663`）。因此「容量登记」这份事实的产生入口就是 §7.2 的注册，
不是单独的管理端点。

本节三条 SQL 都是只读查询，可在任意已完成注册的部署上执行。`DEL-535`（`c0172a22`，MegaCube）
已按容器内 `psql` 实跑，输出与上面预期一致：

```text
workspace-default|8|0|policy-silero-vad-bfdc0193
instance-asr-1|1|ready|mbrev-3cbacc8e26b74859a63e293162064487|capm-file-asr-qwen3-1p7b-gb10-20261027
```

`host_nodes` 一行按统一内存维度读出（见上面「预期」）。**本机仍无注册实例，未实跑**。

### 7.4 readiness 收据续期与观测

注册写入的 readiness 收据有有效期（`--readiness-ttl-seconds`，默认 86400 秒），准入每次
创建都重新核对。收据过期后创建返回 `503 engine_not_ready`（`retryable: false`），它与存储
故障的 `503 job_unavailable` 是两个码：前者重签收据即可放行同一请求，后者是存储拒绝。
`retryable` 只有"同一请求无需带外改动即可成功"才为 `true`，重签收据属于运维动作，所以
该类恢复路径一律是 `false`，恢复方式写在 `message` 里；准入侧唯一可重试的是
`503 job_admission_contended`。同类引擎侧原因各有自己的码（`engine_state_mismatch` /
`engine_resource_disabled` / `recipe_not_admitted`），不再折成"存储不可用"。
收据必须由真实 `/health` 探测重签，不能只改库，所以续期复用注册命令：

```bash
# 单次续期：同一身份、同一 generation，用本次探测重签
docker compose --env-file deploy/compose.env -f deploy/compose.yaml \
  --profile engine-readiness run --rm engine-readiness-renew

# 只读报告：每个 ready 实例一行 JSON，再一行汇总
docker compose --env-file deploy/compose.env -f deploy/compose.yaml \
  --profile engine-readiness run --rm engine-readiness
```

两个 profile 服务分别执行已安装的 `speechnexus-register-engine --renew-readiness` 与
`speechnexus-engine-readiness`；不经 Compose 时可直接运行这两个命令，参数见
[引擎注册](register-engine.md)。
续期输出 `readiness.state`（本次发现的窗口）与 `readiness.renewed`，窗口为 `expiring`
或 `expired` 时另在 stderr 打 `warning:`。报告在任一收据 `expired` / `missing` /
`invalid` 时退出码非零，`--fail-on-expiring` 可让 `expiring` 也非零；把它挂到监控上，
就能在创建开始失败前告警。Compose 不负责调度，须用宿主机 cron 或 systemd timer 以短于
TTL 的间隔调用续期（例如每小时）。

**已知缺陷（跟踪 #845）**：参考部署没有集群内的自动信号覆盖"收据过期"这一状态。
`api` 的 healthcheck 是 `/readyz`，而它被有意设计成不因收据过期而失败（读取与取消必须继续
可用）；报告命令是一次性执行的，Compose 不调度；启动闸门只在启动时打印一次 `pending`。因此
"停止续期 → 所有创建 503 `engine_not_ready`，而 `/readyz` 与容器状态全绿"是可复现的。
上面的宿主机定时器只是缩短窗口，`engine-readiness` 只让外部监控能看到它；在 #845 提供
"本部署能否准入创建"的独立可轮询面之前，这一缺口不能算被配置解决。

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

镜像构建只有一处真相：[最小部署面 §0](compose-minimal.md#0-构建前提) 的一次性
`build <显式服务名>`（含 `api`、`gateway`，不含 `minio`）。这里只 `up -d`，不带 `--build`：
`api` 与 `gateway` 经 `depends_on` 带出 `minio`，`up --build` 会连带构建
`deploy/images/minio.Dockerfile`，而它在构建期取 `raw.githubusercontent.com` 的 LICENSE
做校验，受限网络主机上以 `target minio: failed to solve` 失败并让整条 `up` 失败
（#900、#905）；首次构建后镜像就是本次 checkout 的代码，`up -d` 不需要也不应再带
`--build`。

```bash
docker compose --env-file deploy/compose.env -f deploy/compose.yaml up -d api gateway
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

2. 首次显式构建并启动检查器（不带 `--profile asset-inspection` 时这个容器不会被创建）。
   与其它镜像一样先 `build <显式服务名>`、再 `up -d`（不带 `--build`），理由见
   [最小部署面 §0](compose-minimal.md#0-构建前提)；`--build` 会把 `depends_on` 的构成也带进
   计划，构建了哪些镜像就不再只看这一行命令：

```bash
docker compose --env-file deploy/compose.env -f deploy/compose.yaml \
  --profile asset-inspection build asset-inspector
docker compose --env-file deploy/compose.env -f deploy/compose.yaml \
  --profile asset-inspection up -d asset-inspector
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

六项前置与 §1 的三项额外前置满足后，`POST /v1/nexus/jobs` 才能越过 binding、配额与容量
判定。异步 ASR 还需要一个 `ready`
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
  -d '{"kind":"asr","model":"<已发布 binding 的 binding_id>",
       "input":{"audio_asset_id":"<上一步的 asset id>"}}'
```

`model` 填 `binding_id`（也就是 `GET /v1/models` 返回的 `id`），不是 `logical_model`：ASR/TTS
创建按 head 的 `object_id` 解析（`public_jobs_postgres.py:227`）。TTS 若带上
`model_binding` 引用固定 revision，则 `model` 还要与该 binding 的 `logical_model` 相等
（`speech/application/public_jobs.py:283`）；不指定时取当前 head。

预期 `202`，body 是 Job 文档，`status` 为 `queued`，并带 `Location`。随后轮询：

```bash
curl -sS http://127.0.0.1:8000/v1/nexus/jobs/<job_id>
```

预期状态依次推进 `queued` → `running` → `succeeded`，`artifacts` 出现已提交制品；每个状态
变化都有对应的持久事件。若停在 `queued`，按第 13 节定位。若停在 `running` 超过
`SPEECHNEXUS_JOB_DISPATCH_JOB_TIMEOUT_SECONDS`，该轮派发按未知处理，不把仍可能运行的引擎
写成确定性 `failed`；租约到期后由下一个 worker 按同一 `submit_intent_id` 接手，不会创建
第二个执行。

创建阶段的前置（不在六项计数内）按判定顺序是：已发布 binding 与完整 `execution_defaults`
（§7.1，缺失分别 `422 model_not_published` 与 `422 job_binding_incomplete`）、该工作空间的
配额行（§7.3，缺失 `422 workspace_not_configured`）、ready 实例（§7.2，缺失
`429 job_capacity_exhausted`），TTS 另需声音已发布且未退休（否则 `422 voice_not_published`）。
`execution_defaults` 由 binding 定义的不可变定义提供、不在 admin 响应里回显，所以核对只能
回看 §7.1 第 1 步的请求体；声音按[内建声音资格](builtin-voice-qualification.md)取证。

命令执行状态：这三条命令已在 `DEL-535` 的从空卷实跑中走通（时间线与标识见
[从零实跑记录](compose-minimal-from-zero.md)第 11 节）：`202` → `queued` → `running` →
`succeeded`，制品 `sha256` 与 `ETag` 一致。**未在本机验证**（本机没有参考部署与真实引擎）。

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
| `POST /v1/nexus/jobs` 返回 `503 job_unavailable` | 先读 `message` 点名的空白键（如 `SPEECHNEXUS_JOB_ADMISSION_DATABASE_URL`），再确认 API 是否重建（有已发布 binding 时才会走到这里；否则先看下面的 422） | 1 |
| 创建返回 `422 model_not_published` | 该 `model` 没有任何已发布 binding 作为 head，或 `tasks` 不含 `asr_file`/`tts_file`；`message` 同时点名「发布 binding + 注册引擎」两步，按 §7.1 建立草稿并发布 | 六项之外 |
| 创建返回 `422 model_binding_not_current` | revision 仍 `published`，但 head 已被退役清空；重新激活一个仍 published 的 revision（§7.1 第 3 步） | 六项之外 |
| 创建返回 `429 job_capacity_exhausted` | 先读 `message`：无已注册实例时它点名引擎注册（`engine-registration` profile），已注册但非 `ready` 时点名状态；同时核对 `engine_instances` 是否有该 binding 的 `ready` 实例，且其 `binding_revision_id` / `manifest_id` 与已发布 binding 一致。binding 未发布与无配额行分别是 422，不会走到这里 | 4 |
| 创建返回 `422 workspace_not_configured` | 该工作空间是否注册过引擎（`workspace_quotas` 无行即未配置配额；重试不会改变结果，只能按 §7.2 注册） | 4 |
| 创建返回 `422 job_binding_incomplete` | binding 是否带完整 `execution_defaults`，以及实例 `manifest_id` 是否等于其 `execution_recipe.capacity_manifest_id` | 六项之外 |
| worker 启动即退出 | 先看 `FAIL:` 行：迁移缺失点名 `alembic_version` 与 migration profile；否则看角色守卫、`permission denied`、缺少 `voice_validation_worker_grants` | 2、3 |
| Job 进入 `running` 后结果读不回 | worker 与 gateway 的 `result_bucket` 是否一致、桶是否版本化 | 5 |
| worker 在、但网关调用全部失败 | worker 的网络命名空间是否仍指向当前 gateway 容器 | 6 |
| 迁移报 `alembic_version` 不存在或版本偏低 | 迁移是否执行到目标库、发布制品 head | 2 |
| api 启动退出并打印 `FAIL: ... migration profile` | 同一数据库是否执行过迁移（预检读不到准入表即拒绝启动） | 2 |
| 取消后 Job 停在 `cancelling`、`held_slots` 不归零 | 先看 `/readyz` 的 `cancellation_debt:` 是否带 `never claimed=N`（见 §13.1） | 6 |

### 13.1 取消没有推进时的三步判断

取消受理只写受理事实；把容量真正要回来要有人推进，而推进者不是默认部署的一部分
（`deploy/compose.yaml` 不含取消对账服务，`SPEECHNEXUS_JOB_CANCELLATION_RECONCILIATION_ENABLED`
默认 `false`）。因此「取消后不归还」先分清是哪一种，再动手：

1. **读面**：`GET /readyz` 与 `GET /v1/nexus/admin/diagnostics` 的 `cancellation_debt` detail。
   - `unavailable (not configured)`：没有配 `SPEECHNEXUS_READINESS_CANCELLATION_DSN`，这份部署
     **看不到**这类债（探针永不失败，所以「绿」不代表没有债）。
   - `never claimed=N`：围栏存在但账本没有行——**从来没有 worker 领取过**，这种部署里
     公开面的 `202 cancelling` 不会自己前进。
   - 有阻塞明细（`job -> lease(lease=…, execution=…, subclass=…)`）：worker 在跑，被外部
     事实挡住；`subclass=completed-without-release` 走有界操作员出口，其余走隔离/停止路径。
2. **推进**：先跑一轮有界 pass（不需要常驻，也不需要新依赖）：

   ```bash
   SPEECHNEXUS_JOB_CANCELLATION_RECONCILIATION_ENABLED=true    SPEECHNEXUS_DATABASE_URL=postgresql:///speech    SPEECHNEXUS_DATABASE_SCHEMA=speech    SPEECHNEXUS_EXECUTION_GATEWAY_BASE_URL=http://gateway:8010    SPEECHNEXUS_EXECUTION_GATEWAY_COMPONENT_CREDENTIAL=...    python -m speechnexus_backend.job_cancellation_worker --once
   ```

   一轮之后重读 detail：`never claimed` 消失说明推进者已经在工作，剩下的是阻塞明细。
   时间上界：确认窗口 `CONFIRM_TIMEOUT_SECONDS`（默认 `300` s）+ 退避上限 `300` s；
   期间槽位**不可用**，隔离后也从 `held` 移到 `quarantined`，容量仍被计费。
3. **要回槽位**：执行已 `unknown_quarantined`/`quarantined` 的，隔离后只能经
   `QuarantineReleaseService` 的操作员证词；执行仍活跃的先让网关 `/abort`、`/drain`；
   执行已 `succeeded` 且有终止证据的用 `completed_release_cli`；缺终止证据的等 #338。
   **不要**手工改 `speech_jobs.state`、租约或补写证据行。

逐行核对与第二条查询（覆盖 Job 已离开 `cancelling`、租约仍持有容量的形态——轮询读面
看不到这一类）见[生产 Job 的隔离不了的时候](../components/production-jobs.md#隔离不了的时候可见面与处置)。

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
