---
title: 本地最小可运行装配：从迁移到一次真实转写
status: active
version: "0.1.0"
date: 2026-10-28
---

# 本地最小可运行装配：从迁移到一次真实转写

这是一份**单机、隔离依赖**的装配 runbook。目标是让第二个人能在本机把
PostgreSQL / MinIO / Speech API / 发布与 admin 投影装配起来，并明确说出
“哪一步已经证明、哪一步在主干上做不到、缺什么、谁解除”。

本文不是生产部署资格，也不替代真实引擎、GPU、节点控制器或正式消费者验收。

## 0. 交付状态

| 项 | 值 |
| --- | --- |
| 基线 | `c43590879711796c2de09e18b01f29e87e82db25`（`origin/main`，DEL-107 阶段 1 合入后） |
| 运行日期 | 2026-10-28（Asia/Shanghai；命令输出为 UTC） |
| 主机 | macOS arm64，Docker，本 checkout 的 `.venv`（Python 3.14.9） |
| 隔离 PG | `127.0.0.1:55442`，`postgres:16.15-alpine` |
| 隔离 MinIO | `127.0.0.1:59010`（S3）/ `59011`（console），`speechnexus-minio:RELEASE.2025-09-07T16-13-09Z` |
| API | `127.0.0.1:58000`；未配置 admin reader 的对照实例 `58010`；公开转写用同一进程、追加 3.4 的配置后重启 |
| 执行网关 | `127.0.0.1:58030`（带 file-ASR；9.4 是本机启动记录，11.2 用的是 SM+PO 常驻的同一配置实例） |
| GB10 隧道 | `127.0.0.1:18100`，实测 `/health=200`、`/v1/models=qwen3-asr-1.7b` |
| VAD 制品 | Silero ONNX `1a153a22…878e3`，`2,327,524 B`，与 artifact pin 一致 |

**已实跑并通过**

1. 迁移到 `0021_publication_evidence (head)`；
2. 非 owner 角色 + 最小授权 + 独立 MinIO 桶；
3. API 启动、`/readyz`、空目录 `GET /v1/models`（200 + ETag + 304）；
4. `POST /v1/nexus/admin/model-bindings` 创建 draft（契约夹具，以及补了本地
   `capacity_manifest_id` 的随附 ASR 定义副本）；
5. `speechnexus-publish-binding` 仅用**本地测试证据 ID** 发布成功、重复发布幂等、
   错误码；仓库随附 ASR 定义原样不可发布，见 5.4；
6. admin GET 的 draft / published / 删除证据后 503 / 未配置 reader 的 503；
7. 用**测试注册路径**播种完整 revision 后，目录 200、ETag 变化、旧 ETag 200、新 ETag 304；
8. 带 file-ASR 的网关启动，`/readyz` 200（`control_postgresql` + `gateway_ingress`；
   file worker 在 `gateway_ingress` 探针内部检查）；
9. 用 fixture 直接播种 `engine_instances` ready 行（**不是** node controller 注册）；
10. 网关 `/internal/v1/executions/submit` → 真实 Silero VAD → 真实 Qwen3-ASR →
    版本化结果对象；12 秒公共测试语音得到非空正文与 `duration_ms=12000`；
11. 重复提交幂等：`idempotent_replay=true`、同一 `engine_request_id`、结果对象只有
    1 个版本、`execution_stop_evidence=0`。

**已执行但未通过：公开转写**

`POST /v1/audio/transcriptions` 已真实发出一次（12 秒测试语音、`model=asr-bundled-local`），
120 秒后返回 `504 operation_timeout`。同一请求在网关侧确实完成了 VAD + ASR，并把 185 B
结果写进 MinIO（`sha256:c707b70…`）；但 `submit_intents.execution_state` 一直停在
`executing`：file execution worker 只登记 staging，没有任何成功路径把执行推进到
`succeeded`，API 轮询到 `timeout_seconds=120` 后超时。**r10 的时序测量把 120 秒定位在
API 轮询，不是提交握手**：同一 API 连接先收到 submit 200，再发出 458 次 GET；两条候选
假设（ingress 锁、10 秒 deadline 重试）均被实测排除，见 11.2.1。原始响应、网关状态、
人工驱动者与清理见 11.2 / 11.2.2。**这条 504 不是重试能解决的，也不能当作公开转写可用。**

**停在第 7 步（支持路径）**

用 HTTP POST 创建的 draft，经支持的发布命令发布后，`GET /v1/models` 返回
`503 model_catalogue_unavailable`。根因不是本 runbook 的配置，而是主干的两处
存储/读取形状不一致；详见[第 5.1 节](#51-这是主干缺陷不是本-runbook-的配置错误)。
因此“HTTP draft → publish → 目录可见”这条支持链在 `c435908` 上**做不到**。

**明确未执行**

- 真实的 capacity manifest 与 qualification report：全仓没有这两份产出物，不能用
  `local-test-*` 或任何虚构 ID 冒充生产注册证据；第 5.4 节给出边界。
- 公开 `/v1/audio/transcriptions` 的成功响应：请求已真实发出，但只得到 504；file worker
  在 staging 登记后没有终态写者，API 轮询 120 秒截止，见 11.2 / 11.2.1 / 11.2.2。
- node controller 注册与真实 readiness 回执：第 10 节只用了 fixture 行，
  `runtime_instance_intents` 仍为 0。
- `speechnexus-runtime-delivery-worker` 实跑。
- 生产级最小权限 MinIO 身份：本轮隔离桶用了实例 root，见 9.4 的偏差声明。
- 质量、时延、容量、fencing / 预算释放 / 停止与正式消费者接入。

**阅读约定**

- 标 `[未执行]` 的命令没有在本机执行过；它们只是下一接收人的输入骨架，不能当作证据。
- `<...>` 是占位符；本机实跑时替换成一次性本地口令/密钥后执行，未写入仓库。
- 依赖 `$SN_LOCAL_ROOT` 的命令假定已执行 2.1 的 `export`；9.x 的命令自带默认值。
- 所有时间、ETag、`revision_id`、`published_at_us` 都会随运行变化；把“实际输出”当作
  形状与判定依据，不要复制固定值做断言。
- `submit_intents.recorded_at` 是提交落库时刻，不是请求完成时刻；`terminated_at` 可能
  被测试对账回填。时序判定用 11.2.1 的请求墙钟 + 网关 access log，不要只看这两列。

## 1. 启动顺序总览

| # | 步骤 | 状态 |
| --- | --- | --- |
| 1 | 隔离 PG / MinIO | 已执行 |
| 2 | `alembic upgrade head` | 已执行 |
| 3 | 创建四个非 owner 角色与授权 | 已执行 |
| 4 | 创建私有对象桶 | 已执行 |
| 5 | 启动 API | 已执行 |
| 6 | 空目录读 `GET /v1/models` | 已执行 |
| 7 | 创建 draft binding | 已执行 |
| 8 | 发布 binding（带两个本地测试证据 ID；真实 ASR 定义见 5.4） | 本地测试定义已执行 |
| 9 | admin GET 三态 | 已执行 |
| 10 | 发布后目录读 | **失败：主干缺陷，见 5.1** |
| 11 | 网关（带 file-ASR）启动 | 已执行 |
| 12 | 引擎注册 / readiness | **fixture 播种；真实注册 `[未执行]`（DEL-104）** |
| 13 | 一次真实转写（网关 internal API） | 已执行 |
| 14 | 公开 `/v1/audio/transcriptions` | **已执行：504 `operation_timeout`；worker 只登记 staging、未写终态（11.2 / 11.2.1 / 11.2.2）** |

**与任务书顺序的差异**：任务书给的是 `… → 引擎注册 → 发布 binding → 请求`。本文把
发布放在引擎注册和网关之前，因为 `speechnexus-publish-binding` 只校验证据 ID 与
revision 的容量声明，不读 `engine_instances`；而主干目前没有把 runtime intent 转成
`ready` 的 provider（第 10 节）。所以**发布成功只代表目录可见，不代表可服务**。
本轮的内部转写走执行网关的 submit（第 11.1 节）；公开路由也已用私有 recipe 与
live admission 真跑到网关，但停在“结果登记后没有终态写者”，见第
11.2 / 11.2.1 / 11.2.2 节。

## 2. 隔离依赖

### 2.1 启动 PostgreSQL 与 MinIO

不要使用共享的 `5432` / `59000`；本文固定使用 `55442` / `59010-59011`。

```bash
export SN_LOCAL_ROOT="$PWD/.paperclip-runtime/del109"
mkdir -p "$SN_LOCAL_ROOT"

docker rm -f del109-pg del109-minio 2>/dev/null || true

docker run -d --name del109-pg \
  -e POSTGRES_PASSWORD='<local-admin-password>' \
  -e POSTGRES_DB=speechnexus \
  -p 127.0.0.1:55442:5432 \
  postgres:16.15-alpine

docker run -d --name del109-minio \
  -e MINIO_ROOT_USER='<local-minio-user>' \
  -e MINIO_ROOT_PASSWORD='<local-minio-password>' \
  -p 127.0.0.1:59010:9000 \
  -p 127.0.0.1:59011:9001 \
  speechnexus-minio:RELEASE.2025-09-07T16-13-09Z \
  server /data --console-address :9001
```

等待并验证：

```bash
docker exec del109-pg pg_isready -U postgres -d speechnexus
curl -sf -o /dev/null -w 'minio_live=%{http_code}\n' \
  http://127.0.0.1:59010/minio/health/live
```

实际输出：

```text
/var/run/postgresql:5432 - accepting connections
minio_live=200
```

### 2.2 迁移到 head

迁移使用**独立 DDL 身份**，不经过运行时角色。`alembic.ini` 的
`script_location` 指向包内迁移；`env.py` 读取
`SPEECHNEXUS_MIGRATION_DATABASE_URL`，默认 schema 为 `speech`。

```bash
export SN_ADMIN_DSN='postgresql://postgres:<local-admin-password>@127.0.0.1:55442/speechnexus'

SPEECHNEXUS_MIGRATION_DATABASE_URL="$SN_ADMIN_DSN" \
  .venv/bin/alembic upgrade head

SPEECHNEXUS_MIGRATION_DATABASE_URL="$SN_ADMIN_DSN" \
  .venv/bin/alembic current
```

实际输出（`upgrade` 在本 checkout 无日志行，退出码 0；`current` 给出 head）：

```text
0021_publication_evidence (head)
```

核对版本表与证据表：

```bash
docker exec del109-pg psql -U postgres -d speechnexus -Atc \
  "select version_num from speech.alembic_version;
   select count(*) from pg_tables
    where schemaname='speech'
      and tablename='speech_binding_publication_evidence';"
```

实际输出：

```text
0021_publication_evidence
1
```

### 2.3 创建非 owner 角色与最小授权

四个运行时角色分工如下；**owner 只用于迁移、测试播种和清理**：

| 角色 | 授权来源 | 用途 |
| --- | --- | --- |
| `sn_api` | 仅 `LOGIN` | `SPEECHNEXUS_DATABASE_URL` 的 readiness 连接；本装配不赋予表权限 |
| `sn_binding_writer` | `binding_draft_grants` | `POST /v1/nexus/admin/model-bindings` |
| `sn_publication_writer` | `binding_draft_grants` + `publication_grants` + `publication_evidence_grants` | `speechnexus-publish-binding` |
| `sn_binding_admin_reader` | `binding_admin_reader_grants` | admin GET 投影 |
| `sn_catalog_reader` | `model_catalog_reader_grants` | 公开目录 `GET /v1/models` |

授权语句不手抄 SQL，而是直接调用仓库中的授权声明函数，避免与代码漂移。
脚本可重复执行（已存在的角色只重置口令，GRANT 幂等）：

```bash
export SN_ROLE_PASSWORD='<local-role-password>'
.venv/bin/python - <<'PY'
import os
import psycopg
from psycopg import sql
from speechnexus_backend.speech.infrastructure.binding_draft_schema import binding_draft_grants
from speechnexus_backend.speech.infrastructure.binding_publication_evidence_schema import (
    binding_admin_reader_grants,
    publication_evidence_grants,
)
from speechnexus_backend.speech.infrastructure.publication_schema import (
    model_catalog_reader_grants,
    publication_grants,
)

schema = os.environ.get("SN_DATABASE_SCHEMA", "speech")
password = os.environ["SN_ROLE_PASSWORD"]
roles = {
    "sn_binding_writer": (binding_draft_grants,),
    "sn_publication_writer": (
        binding_draft_grants,
        publication_grants,
        publication_evidence_grants,
    ),
    "sn_binding_admin_reader": (binding_admin_reader_grants,),
    "sn_catalog_reader": (model_catalog_reader_grants,),
}
with psycopg.connect(os.environ["SN_ADMIN_DSN"], autocommit=True) as conn:
    for role in ("sn_api", *roles):
        exists = conn.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (role,)).fetchone()
        if exists is None:
            conn.execute(sql.SQL("CREATE ROLE {} LOGIN").format(sql.Identifier(role)))
        conn.execute(
            sql.SQL("ALTER ROLE {} PASSWORD {}").format(
                sql.Identifier(role), sql.Literal(password)
            )
        )
    for role, declarations in roles.items():
        for declaration in declarations:
            for statement in declaration(schema, role):
                conn.execute(statement)
    print("roles ready:", "sn_api", *roles)
PY
```

实际输出：

```text
roles ready: sn_api sn_binding_writer sn_publication_writer sn_binding_admin_reader sn_catalog_reader
```

启动时会由各 provider 重新核验角色：必须是 non-owner、没有 schema `CREATE`、
没有超出端口的表权限；本装配首次启动即通过，说明授权面与端口匹配。

### 2.4 创建私有对象桶

本段只需要桶存在；API readiness 只探测 MinIO 健康端点，不探测桶版本化。

```bash
.venv/bin/python - <<'PY'
from minio import Minio
client = Minio(
    "127.0.0.1:59010",
    access_key="<local-minio-user>",
    secret_key="<local-minio-password>",
    secure=False,
)
if not client.bucket_exists("speechnexus-dev"):
    client.make_bucket("speechnexus-dev")
print("bucket:", client.bucket_exists("speechnexus-dev"))
PY
```

实际输出：

```text
bucket: True
```

## 3. 启动 API

### 3.1 环境变量

基础装配**不启用**同步转写；因此这里不设置 `SPEECHNEXUS_TRANSCRIPTION_*` 与对象访问
密钥。这会让 `/v1/audio/transcriptions` 在启动时明确不可用，但不影响目录、admin 与
发布命令。第 3.4 节给出本轮公开请求实际使用的追加配置。

| 变量 | 本装配取值/来源 | 理由 |
| --- | --- | --- |
| `SPEECHNEXUS_ENVIRONMENT` | `development` | 本地装配 |
| `SPEECHNEXUS_PROCESS_ROLE` | `api` | 启用 API 组合根 |
| `SPEECHNEXUS_NODE_ID` | `node-local-1` | 单机身份 |
| `SPEECHNEXUS_INSTANCE_ID` | `api-1` | 单进程身份 |
| `SPEECHNEXUS_HOST` / `PORT` | `127.0.0.1` / `58000` | 避开共享端口 |
| `SPEECHNEXUS_DATABASE_URL` | `postgresql://sn_api:...@127.0.0.1:55442/speechnexus` | readiness 连接；本装配只用于 `SELECT 1` |
| `SPEECHNEXUS_DATABASE_SCHEMA` | `speech` | 迁移默认 schema |
| `SPEECHNEXUS_OBJECT_STORAGE_ENDPOINT` | `http://127.0.0.1:59010` | 私有 MinIO |
| `SPEECHNEXUS_OBJECT_STORAGE_HEALTHCHECK_URL` | `http://127.0.0.1:59010/minio/health/live` | readiness 探测 |
| `SPEECHNEXUS_OBJECT_STORAGE_BUCKET` | `speechnexus-dev` | 已创建桶 |
| `SPEECHNEXUS_MODEL_CATALOG_READER_DATABASE_URL` | `postgresql://sn_catalog_reader:...` | 公开目录 reader |
| `SPEECHNEXUS_BINDING_WRITER_DATABASE_URL` | `postgresql://sn_binding_writer:...` | draft POST |
| `SPEECHNEXUS_BINDING_ADMIN_READER_DATABASE_URL` | `postgresql://sn_binding_admin_reader:...` | admin GET 投影 |

把以下内容保存为 `$SN_LOCAL_ROOT/api.env`（文件权限应为 `0600`；不要提交）：

```bash
cat > "$SN_LOCAL_ROOT/api.env" <<'ENV'
SPEECHNEXUS_ENVIRONMENT=development
SPEECHNEXUS_PROCESS_ROLE=api
SPEECHNEXUS_NODE_ID=node-local-1
SPEECHNEXUS_INSTANCE_ID=api-1
SPEECHNEXUS_HOST=127.0.0.1
SPEECHNEXUS_PORT=58000
SPEECHNEXUS_LOG_LEVEL=info
SPEECHNEXUS_DATABASE_URL=postgresql://sn_api:<local-role-password>@127.0.0.1:55442/speechnexus
SPEECHNEXUS_DATABASE_SCHEMA=speech
SPEECHNEXUS_OBJECT_STORAGE_ENDPOINT=http://127.0.0.1:59010
SPEECHNEXUS_OBJECT_STORAGE_HEALTHCHECK_URL=http://127.0.0.1:59010/minio/health/live
SPEECHNEXUS_OBJECT_STORAGE_BUCKET=speechnexus-dev
SPEECHNEXUS_MODEL_CATALOG_READER_DATABASE_URL=postgresql://sn_catalog_reader:<local-role-password>@127.0.0.1:55442/speechnexus
SPEECHNEXUS_BINDING_WRITER_DATABASE_URL=postgresql://sn_binding_writer:<local-role-password>@127.0.0.1:55442/speechnexus
SPEECHNEXUS_BINDING_ADMIN_READER_DATABASE_URL=postgresql://sn_binding_admin_reader:<local-role-password>@127.0.0.1:55442/speechnexus
ENV
chmod 600 "$SN_LOCAL_ROOT/api.env"
```

### 3.2 前台启动

在一个**保持打开的终端**里执行；不要把进程放在会被 shell 回收的后台作业里：

```bash
set -a
. "$SN_LOCAL_ROOT/api.env"
set +a
exec .venv/bin/speechnexus-api
```

实际启动日志（顺序与内容以本次为准）：

```text
Synchronous transcription is unavailable; missing configuration: SPEECHNEXUS_TRANSCRIPTION_RECIPE_FILE, SPEECHNEXUS_EXECUTION_GATEWAY_BASE_URL, SPEECHNEXUS_EXECUTION_GATEWAY_COMPONENT_CREDENTIAL, SPEECHNEXUS_TRANSCRIPTION_INPUT_BUCKET, SPEECHNEXUS_OBJECT_STORAGE_ACCESS_KEY, SPEECHNEXUS_OBJECT_STORAGE_SECRET_KEY
INFO:     Started server process [...]
INFO:     Waiting for application startup.
INFO:     Application startup complete.
INFO:     Uvicorn running on http://127.0.0.1:58000 (Press CTRL+C to quit)
```

`Synchronous transcription is unavailable` 是**基础装配**的预期结果：转写路由没有
私有 recipe、网关 URL/凭据和对象密钥。它不阻止目录、admin、binding 路由。追加 3.4 的
配置并重启后，这行告警消失，但公开路由仍可能失败（11.2）。

### 3.3 就绪检查

另一个终端：

```bash
curl -sS -w '\nreadyz=%{http_code}\n' http://127.0.0.1:58000/readyz
```

实际输出：

```json
{"status":"ready","checks":[{"name":"postgresql","status":"ok","detail":null},{"name":"object_storage","status":"ok","detail":null}]}
readyz=200
```

### 3.4 为公开转写追加的 API 配置（本轮已执行）

3.1 的基础装配刻意不启用同步转写。要让 `POST /v1/audio/transcriptions` 通过启动期的
“成组配置”检查，还必须再提供七项（`backend/src/speechnexus_backend/transcription_runtime.py`）：

| 变量 | 本轮取值 | 理由/来源 |
| --- | --- | --- |
| `SPEECHNEXUS_EXECUTION_GATEWAY_BASE_URL` | `http://127.0.0.1:58030` | 9.4 的执行网关 |
| `SPEECHNEXUS_EXECUTION_GATEWAY_COMPONENT_CREDENTIAL` | `<gateway-component token>` | 与网关 `SPEECHNEXUS_GATEWAY_COMPONENT_TOKEN` 同值；只写进 0600 env |
| `SPEECHNEXUS_TRANSCRIPTION_RECIPE_FILE` | `.paperclip-runtime/del109/transcription-recipe.json` | 私有准入 recipe；本机副本 `recipe_digest=sha256:6549a48e…1fde864` |
| `SPEECHNEXUS_TRANSCRIPTION_INPUT_BUCKET` | `del109-file-asr` | API 把上传输入写进这个桶 |
| `SPEECHNEXUS_OBJECT_STORAGE_ACCESS_KEY` / `_SECRET_KEY` | 隔离 MinIO 身份 | 上传输入、读取结果；本轮仍是实例 root（偏差，见 9.4） |
| `SPEECHNEXUS_OBJECT_STORAGE_BUCKET` | `del109-file-asr` | **覆盖** 3.1 的 `speechnexus-dev`；否则结果 reader 会去错的桶找 staging 对象 |

另外两项环境事实也是硬前提：

- 桶 `del109-file-asr` 必须启用 versioning。API 用固定 `version_id` 读回结果对象；
  未版本化的对象走不完这条读路径；
- `sn_api` 需要 AssetRuntime 授权：`speech_assets`、`speech_asset_storage_authority`、
  `speech_asset_commands` 的 SELECT/INSERT。本机用
  `asset_runtime_grants("speech", "sn_api")` 补授，证据
  `public-admission-asset-grants.out` 只有一行 `asset_runtime_grants applied to sn_api`。
  这是 fixture 授权，不等于生产 RBAC 方案。

本机把上述内容写进单独的 `$SN_LOCAL_ROOT/api-transcription.env`（0600，未提交），再按
3.2 的方式前台重启 API。重启日志里**不再出现**
`Synchronous transcription is unavailable; missing configuration: ...`，`/readyz` 仍为 200：

```json
{"status":"ready","checks":[{"name":"postgresql","status":"ok","detail":null},{"name":"object_storage","status":"ok","detail":null}]}
```

recipe 的 `asr.final_deadline_ms` / `auxiliary_deadline_ms` 本轮设为 `10000`。早先设
`1000` 时，真实 ASR 阶段报 `asr_deadline_exceeded`（原文见 8 与 9.5）；这不是网关配置
错误，是给了真实模型一个不现实的预算。

**偏差声明**：这里的 recipe 是本地 fixture 语义（`binding_revision_id=binding-del109`、
`manifest-del109`、`policy-del109`），`binding_publication_id` 指向真实发布的
`mbrev-db04…`。它证明了组合根和准入代码能接通，不等于生产 recipe、容量或资格已被批准。

## 4. 空目录读

```bash
curl -sS -D "$SN_LOCAL_ROOT/models-empty.headers" \
  -o "$SN_LOCAL_ROOT/models-empty.json" \
  http://127.0.0.1:58000/v1/models
```

实际状态/头/正文：

```text
HTTP/1.1 200 OK
cache-control: no-store
etag: "sha256:e5808a9cacfa25fa90050ece6c5539ace6a02d0dab00bce436096788e16261cd"
content-type: application/json

{"object":"list","data":[]}
```

条件请求：

```bash
ETAG=$(awk 'BEGIN{IGNORECASE=1} /^etag:/ {sub(/^[^:]*: /,""); gsub(/\r/,""); print}' \
  "$SN_LOCAL_ROOT/models-empty.headers")
curl -sS -o /dev/null -w 'conditional=%{http_code}\n' \
  -H "If-None-Match: $ETAG" http://127.0.0.1:58000/v1/models
```

实际输出：

```text
conditional=304
```

`ETag` 由 `catalogue_etag(kind, policy, workspace_id, catalog_revision, payload)` 生成；
空目录的 `catalog_revision` 仍是稳定的 1（`COUNT(*) + 1`）。`Cache-Control: no-store`
表示客户端可以条件请求，但不得把响应当可复用缓存。

## 5. 创建 draft、发布与 admin 投影

### 5.1 这是主干缺陷，不是本 runbook 的配置错误

在写后续命令之前先给出结论：**HTTP POST 创建的 revision，发布后不会出现在公开目录**。

本机实测：

```text
POST /v1/nexus/admin/model-bindings        -> 201，publication.status = draft
speechnexus-publish-binding                -> exit 0，created=true
GET /v1/models                             -> 503 model_catalogue_unavailable
```

根因是两处存储形状不一致：

1. `POST` 路径在 `prepare_binding_draft` 中把 `binding_id` 从存储文档里
   `pop` 掉（`backend/src/speechnexus_backend/speech/interfaces/binding_drafts.py:77-78`），
   存进 `speech_binding_revisions.definition_json` 的是**创建请求去掉 `binding_id`**；
2. 目录 reader 直接把 `definition_json` 交给 `model_document`，后者要求
   `definition["binding_id"] == publication.object_id`
   （`backend/src/speechnexus_backend/speech/application/model_catalog.py:79-80`），
   随后 `capability_model` 还要求整份文档满足 `ModelBindingRevision`
   （同文件 `:137`）。HTTP 响应里看到的 `binding_id`、`schema_version`、`revision`、
   `revision_id`、`publication`、`created_at` 是 `revision_to_wire` 临时补出来的
   （`backend/src/speechnexus_backend/speech/interfaces/binding_drafts.py:50-57`），
   **并没有存进 `definition_json`**。

本机从数据库直接看到的键（`asr-quality`，HTTP 创建）：

```text
["artifact_set", "engine", "execution_recipe", "logical_model",
 "parameter_domains", "release_tier", "tasks"]
```

目录 reader 的 SQL 只选 `v.definition_json`
（`backend/src/speechnexus_backend/speech/infrastructure/model_catalog_sql.py:26`），
`PostgresModelCatalog` 在 `:43` 取值、`:56` 调 `capability_model`，所以发布后整个
snapshot 失败，`/v1/models` 变成 503。

**缺什么 / 谁能解除**

- 缺一个主干修复：要么在 `definition_json` 存完整 revision 文档（与
  `revision_to_wire` 同形），要么让目录 reader 用 `publication.object_id` 重建
  `binding_id` 并补齐 revision 元数据。
- 解除人：唯一 SM+PO 决定开卡；实现后需要重新跑本 runbook 的第 5–8 节。
- 本文不修改代码，也不把测试播种当作支持路径。

### 5.2 创建 draft

用仓库契约夹具里的 `create-original` 作为最小合法请求体（不要手写 DTO）：

```bash
.venv/bin/python - <<'PY'
import json
p = json.load(open("tests/fixtures/contracts/binding-http.json"))
body = next(item["value"]["body"] for item in p["examples"]
            if item["id"] == "create-original")
open(".paperclip-runtime/del109/binding-request.json", "w").write(
    json.dumps(body, ensure_ascii=False) + "\n"
)
print("binding_id:", body["binding_id"])
PY

curl -sS -D "$SN_LOCAL_ROOT/draft.headers" \
  -o "$SN_LOCAL_ROOT/draft.json" \
  -X POST http://127.0.0.1:58000/v1/nexus/admin/model-bindings \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: del109-local-draft-1' \
  --data-binary @"$SN_LOCAL_ROOT/binding-request.json"

REVISION_ID=$(jq -r .revision_id "$SN_LOCAL_ROOT/draft.json")
CAPACITY=$(jq -r .execution_recipe.capacity_manifest_id "$SN_LOCAL_ROOT/binding-request.json")
echo "revision_id=$REVISION_ID capacity=$CAPACITY"
```

实际输出：

```text
HTTP/1.1 201 Created
binding_id: asr-quality
revision_id=mbrev-e17008dfd6fb4295bd1601435748b560 capacity=cap-asr-quality-7
```

201 响应里的 `publication` 必须是 draft 形状：

```json
{
  "binding_id": "asr-quality",
  "revision": "1",
  "revision_id": "mbrev-e17008dfd6fb4295bd1601435748b560",
  "publication": {
    "capacity_manifest_id": null,
    "disabled_reason": null,
    "published_at": null,
    "qualification_report_id": null,
    "retired_at": null,
    "status": "draft"
  }
}
```

固定 GET 读取 draft：

```bash
curl -sS -w '\nadmin_draft=%{http_code}\n' \
  http://127.0.0.1:58000/v1/nexus/admin/model-bindings/asr-quality/revisions/1
```

实际输出：

```text
{"publication":{"capacity_manifest_id":null,"disabled_reason":null,"published_at":null,"qualification_report_id":null,"retired_at":null,"status":"draft"}}
admin_draft=200
```

此时公开目录仍为空，ETag 与空目录一致（draft 不可见）：

```bash
curl -sS -D "$SN_LOCAL_ROOT/models-after-draft.headers" \
  -o "$SN_LOCAL_ROOT/models-after-draft.json" \
  http://127.0.0.1:58000/v1/models
```

实际输出：

```text
etag: "sha256:e5808a9cacfa25fa90050ece6c5539ace6a02d0dab00bce436096788e16261cd"
{"object":"list","data":[]}
```

### 5.3 发布（证据必填）

```bash
export SPEECHNEXUS_PUBLICATION_WRITER_DATABASE_URL='postgresql://sn_publication_writer:<local-role-password>@127.0.0.1:55442/speechnexus'
export SPEECHNEXUS_DATABASE_SCHEMA=speech

.venv/bin/speechnexus-publish-binding \
  --binding-id asr-quality \
  --revision 1 \
  --revision-id "$REVISION_ID" \
  --capacity-manifest-id "$CAPACITY" \
  --qualification-report-id qrep-asr-quality-7
```

实际输出（`published_at_us` 每次不同）：

```text
published binding_id=asr-quality revision=1 revision_id=mbrev-e17008dfd6fb4295bd1601435748b560 published_at_us=1793141245728838 capacity_manifest_id=cap-asr-quality-7 qualification_report_id=qrep-asr-quality-7 created=true
```

重复执行同一条命令：

```text
published binding_id=asr-quality revision=1 revision_id=mbrev-e17008dfd6fb4295bd1601435748b560 published_at_us=1793141245728838 capacity_manifest_id=cap-asr-quality-7 qualification_report_id=qrep-asr-quality-7 created=false
```

`created=false`、`published_at_us` 保持不变，是幂等回放，不是第二次写入。

admin GET 此时应如实投影为 published：

```bash
curl -sS -w '\nadmin_published=%{http_code}\n' \
  http://127.0.0.1:58000/v1/nexus/admin/model-bindings/asr-quality/revisions/1
```

实际输出：

```json
{
  "publication": {
    "capacity_manifest_id": "cap-asr-quality-7",
    "disabled_reason": null,
    "published_at": "2026-10-27T22:47:25.728838Z",
    "qualification_report_id": "qrep-asr-quality-7",
    "retired_at": null,
    "status": "published"
  }
}
admin_published=200
```

然后执行**第 5.1 节**的目录读：在 `c435908` 上它是 503：

```text
HTTP/1.1 503 Service Unavailable
{"error":{"code":"model_catalogue_unavailable","type":"service_unavailable_error","message":"The model catalogue is unavailable.","param":null,"request_id":"9e5cd59ec6494b72b500546f50962e4b","retryable":false}}
```

### 5.4 仓库随附 ASR 定义不能作为真实发布证据

`deploy/engines/file-asr-audio/binding-definition.json` 是候选定义，不是可直接提交的
完整创建请求。它的 `execution_recipe` 只有 `recipe_revision`、`stage_roles`、
`requires_complete_reservation`，没有 `capacity_manifest_id`；现行
`contracts/model-binding.schema.json:460-475` 却把该字段列为必需。发布层还会再校验
存储 revision 里的引用必须与 `--capacity-manifest-id` 完全一致
（`backend/src/speechnexus_backend/speech/infrastructure/publications_postgres.py:143-151`）。

**支持的 HTTP 路径：原样定义在草稿层就被拒绝。** 用契约夹具的 engine/artifact_set
补齐身份，但保留随附定义的 recipe 原样：

```bash
.venv/bin/python - <<'PY'
import json
from copy import deepcopy
fixture = json.load(open("tests/fixtures/contracts/binding-http.json"))
request = deepcopy(next(i["value"]["body"] for i in fixture["examples"]
                        if i["id"] == "create-original"))
definition = json.load(open("deploy/engines/file-asr-audio/binding-definition.json"))
request.update({
    "binding_id": "asr-bundled-default",
    "logical_model": "asr-bundled-default",
    "tasks": definition["tasks"],
    "execution_recipe": definition["execution_recipe"],
    "parameter_domains": definition["parameter_domains"],
    "release_tier": definition["release_tier"],
})
open(".paperclip-runtime/del109/bundled-asr-request.json", "w").write(
    json.dumps(request, ensure_ascii=False, indent=2) + "\n"
)
PY

curl -sS -w '\nhttp=%{http_code}\n' \
  -X POST http://127.0.0.1:58000/v1/nexus/admin/model-bindings \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: del109-bundled-asr-default-1' \
  --data-binary @.paperclip-runtime/del109/bundled-asr-request.json
```

实际输出：

```text
{"error":{"code":"invalid_binding_request","type":"invalid_request_error","message":"Invalid binding request.","param":null,"request_id":"4985186b73454a5b9410d46b16535f4c","retryable":false}}
http=400
```

**发布层守卫：只有绕过 HTTP 校验的测试播种才会走到这里。** 把随附定义按第 7 节的
方式规范编码后直接插入 `speech_binding_revisions`（**仅测试，不是运维路径**），再执行
同一发布命令：

```bash
.venv/bin/speechnexus-publish-binding \
  --binding-id asr-bundled-seeded --revision 1 \
  --revision-id bundled-missing-capacity-1 \
  --capacity-manifest-id local-test-capacity-1 \
  --qualification-report-id local-test-qualification-1
```

实际输出：

```text
evidence mismatch: binding_id=asr-bundled-seeded revision=1 execution_recipe declares capacity_manifest_id=missing, not local-test-capacity-1
exit=6
```

因此准确边界是：**随附定义不能直接成为可发布 revision；支持的 HTTP 路径在 400 就
停住，继续手工播种才得到发布层的 `missing`。** 不要把 `missing` 当成唯一失败点。

**路径 A：本地验证用。** 仅为了证明 draft/publish 机制，可以复制定义并显式加一个
本地测试引用；它不证明容量存在或合格：

```bash
.venv/bin/python - <<'PY'
import json
from copy import deepcopy
fixture = json.load(open("tests/fixtures/contracts/binding-http.json"))
request = deepcopy(next(i["value"]["body"] for i in fixture["examples"]
                        if i["id"] == "create-original"))
definition = json.load(open("deploy/engines/file-asr-audio/binding-definition.json"))
request.update({
    "binding_id": "asr-bundled-local",
    "logical_model": "asr-bundled-local",
    "tasks": definition["tasks"],
    "execution_recipe": {**definition["execution_recipe"],
                         "capacity_manifest_id": "local-test-capacity-1"},
    "parameter_domains": definition["parameter_domains"],
    "release_tier": definition["release_tier"],
})
open(".paperclip-runtime/del109/bundled-asr-local-request.json", "w").write(
    json.dumps(request, ensure_ascii=False, indent=2) + "\n"
)
PY

curl -sS -w '\nhttp=%{http_code}\n' \
  -X POST http://127.0.0.1:58000/v1/nexus/admin/model-bindings \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: del109-bundled-asr-local-1' \
  --data-binary @.paperclip-runtime/del109/bundled-asr-local-request.json
```

实际输出（`http=201`；关键字段由保存的响应经 `jq` 投影）：

```json
{
  "binding_id": "asr-bundled-local",
  "revision": "1",
  "revision_id": "mbrev-db04f96ee00e454a804d3631ac2c8780",
  "publication": {
    "capacity_manifest_id": null,
    "disabled_reason": null,
    "published_at": null,
    "qualification_report_id": null,
    "retired_at": null,
    "status": "draft"
  }
}
http=201
```

再用 `local-test-capacity-1` / `local-test-qualification-1` 发布（`REVISION_ID` 从上一步
响应读取；本轮实际值见输出）：

```bash
REVISION_ID=$(jq -r .revision_id .paperclip-runtime/del109/bundled-asr-local-draft.json)
export SPEECHNEXUS_PUBLICATION_WRITER_DATABASE_URL='postgresql://sn_publication_writer:<local-role-password>@127.0.0.1:55442/speechnexus'
export SPEECHNEXUS_DATABASE_SCHEMA=speech
.venv/bin/speechnexus-publish-binding \
  --binding-id asr-bundled-local --revision 1 --revision-id "$REVISION_ID" \
  --capacity-manifest-id local-test-capacity-1 \
  --qualification-report-id local-test-qualification-1
```

实际输出：

```text
published binding_id=asr-bundled-local revision=1 revision_id=mbrev-db04f96ee00e454a804d3631ac2c8780 published_at_us=1793144241281521 capacity_manifest_id=local-test-capacity-1 qualification_report_id=local-test-qualification-1 created=true
exit=0
```

**`local-test-capacity-1` 不是容量证明，也不是资格报告；这条路径只能用于本地机制验证，
不得用于生产注册。**

**路径 B：真实发布 `[未执行]`。** 需要一份真实、可追溯的 capacity manifest 和
qualification report，并把同一 `capacity_manifest_id` 写入 ASR revision 与发布证据。
当前仓库没有这两份产出物，`verify-file-asr-artifacts.py` 只把调用方传入的 ID 注入
请求，不证明 manifest 存在、容量足够或资格成立
（`scripts/dev-env/verify-file-asr-artifacts.py:434-440`）；`release_tier=production`
也只是目标等级，不是发布回执。归属：容量与资格证据的产出方（SM+PO 新卡）；本轮没有
卡片编号，不编造。解除前不会用虚构 ID 做真实注册。

### 5.5 发布失败码

本机实际得到的退出码与原文：

| 场景 | 退出码 | 实际输出 |
| --- | --- | --- |
| 缺 `SPEECHNEXUS_PUBLICATION_WRITER_DATABASE_URL` | 2 | `speechnexus-publish-binding: error: Set SPEECHNEXUS_PUBLICATION_WRITER_DATABASE_URL to the provisioned publication writer DSN` |
| 缺必填参数（如 `--revision-id`） | 2 | `usage: speechnexus-publish-binding ...` + `the following arguments are required: --revision-id` |
| 容量证据与 revision 不一致 | 6 | `evidence mismatch: binding_id=asr-quality revision=1 execution_recipe declares capacity_manifest_id=cap-asr-quality-7, not cap-wrong-0001` |
| 已发布的 revision 换成另一个 `revision_id` | 4 | `conflict: binding_id=asr-quality revision=1 is already published as revision_id=mbrev-e17008dfd6fb4295bd1601435748b560, not mbrev-does-not-exist` |
| `revision_id` 不存在且该 revision 尚未发布 | 3 | 预期 `not published: speech_binding_revisions has no row for binding_id=... revision=...`；本机因该 revision 已发布先命中 4 |

退出码 5 表示“写结果未知”（`publication unavailable: ... re-run the same binding/revision/revision_id to resolve it`）。
它不是一个可以自动重试成“确定成功”的码；用同一三元组重放来查询原事实。

## 6. admin 三态与本机真实触发

`docs/operations/backfill-binding-publication-evidence.md` 定义三态；本机逐一触发：

| 存储状态 | HTTP | 本机实际 `publication`/错误 |
| --- | --- | --- |
| 无 publication 的 draft | 200 | `status=draft`，全部发布字段 null |
| publication + 匹配证据 | 200 | `status=published`，两个证据 ID 与 `published_at` 原样返回 |
| publication 存在但证据行被删 | 503 | `code=binding_unavailable`，`message="Fixed binding metadata is unavailable."`，`retryable=true` |
| 未配置 `SPEECHNEXUS_BINDING_ADMIN_READER_DATABASE_URL` | 503 | 同上；不会回退到 writer，也不会把已发布 revision 谎报成 draft |

删除证据行（owner 仅用于本机破坏性验证）：

```bash
docker exec del109-pg psql -U postgres -d speechnexus -c \
  "DELETE FROM speech.speech_binding_publication_evidence WHERE object_id='asr-quality';"

curl -sS -w '\nadmin_missing_evidence=%{http_code}\n' \
  http://127.0.0.1:58000/v1/nexus/admin/model-bindings/asr-quality/revisions/1
```

实际输出：

```json
{"error":{"code":"binding_unavailable","type":"service_unavailable_error","message":"Fixed binding metadata is unavailable.","param":null,"request_id":"2d3431b6b89b4dbd80512973fed7dbce","retryable":true}}
admin_missing_evidence=503
```

再用同一发布命令重放，证据会被补回，admin GET 恢复 `published`：

```text
restore_exit=0
published binding_id=asr-quality ... created=false
admin_restored=200
"published"
```

**注意**：`c435908` 上的 503 message 是通用的
`Fixed binding metadata is unavailable.`；按原因区分 message 的增量
（`cd6d68e`，2026-10-27 的后续修复提交，未进入 `main`）不随本文交付。不要把本文的通用
message 当成“无法区分原因”的长期契约。

未配置 admin reader 的对照实例：

```bash
# 另开一个终端；从 api.env 去掉 admin reader，端口改 58010
grep -v 'SPEECHNEXUS_BINDING_ADMIN_READER_DATABASE_URL' "$SN_LOCAL_ROOT/api.env" \
  | sed 's/^SPEECHNEXUS_PORT=.*/SPEECHNEXUS_PORT=58010/' > "$SN_LOCAL_ROOT/api-no-admin.env"
set -a; . "$SN_LOCAL_ROOT/api-no-admin.env"; set +a
exec .venv/bin/speechnexus-api
```

实际请求：

```text
admin_unconfigured=503
{"error":{"code":"binding_unavailable","type":"service_unavailable_error","message":"Fixed binding metadata is unavailable.","param":null,"request_id":"93c209cbc6e049788e13c3cc7db1444a","retryable":true}}
```

## 7. 目录 ETag 的最小替代验证（测试注册路径）

**这不是支持路径，只证明目录 reader/ETag 本身可用。** 它使用
`tests/integration/test_publications_storage.py` 的完整 revision 文档（含
`schema_version`、`binding_id`、`revision`、`revision_id`、`publication`、
`created_at`），并用仓库的 `encode_document` 写成 RFC8785 规范字节。生产代码
不应依赖这个播种方式；支持路径的修复见 5.1。

先清理上一节用于证明缺陷的 publication（owner 操作），让目录回到空：

```bash
docker exec del109-pg psql -U postgres -d speechnexus -c \
  "DELETE FROM speech.speech_binding_publication_evidence WHERE object_id='asr-quality';
   DELETE FROM speech.speech_publications WHERE object_id='asr-quality';"
```

实际输出（空目录 ETag 回到初始值）：

```text
empty_again=200
etag: "sha256:e5808a9cacfa25fa90050ece6c5539ace6a02d0dab00bce436096788e16261cd"
{"object":"list","data":[]}
```

播种 `tts-quality` revision 1：

```bash
.venv/bin/python - <<'PY'
import json, psycopg
from tests.integration.test_publications_storage import DEFINITION
from speechnexus_backend.speech.application.binding_documents import encode_document

definition = {**DEFINITION, "binding_id": "tts-quality", "revision": "1",
              "revision_id": "binding-rev-1"}
key = "sha256:" + "a1" * 32
request = "sha256:" + "b2" * 32
document = "sha256:" + "c3" * 32
raw = encode_document(definition).decode()
with psycopg.connect(
    "postgresql://postgres:<local-admin-password>@127.0.0.1:55442/speechnexus",
    autocommit=True,
) as conn:
    conn.execute("SET search_path TO speech, pg_catalog")
    conn.execute("INSERT INTO speech_binding_families (binding_id, latest_revision) VALUES ('tts-quality','1')")
    conn.execute(
        "INSERT INTO speech_binding_commands "
        "(key_digest,binding_id,document_version,intent_json,request_digest) "
        "VALUES (%s,'tts-quality',1,'{}',%s)",
        (key, request),
    )
    conn.execute(
        "INSERT INTO speech_binding_revisions "
        "(binding_id,revision,revision_id,key_digest,document_version,"
        " definition_json,definition_digest,request_digest,created_at_us) "
        "VALUES ('tts-quality','1','binding-rev-1',%s,1,%s,%s,%s,1700000000000000)",
        (key, raw, document, request),
    )
print("seeded tts-quality revision 1; canonical bytes", len(raw))
PY
```

实际输出：

```text
seeded tts-quality revision 1; canonical bytes 1275
```

发布并读目录：

```bash
export SPEECHNEXUS_PUBLICATION_WRITER_DATABASE_URL='postgresql://sn_publication_writer:<local-role-password>@127.0.0.1:55442/speechnexus'
export SPEECHNEXUS_DATABASE_SCHEMA=speech
.venv/bin/speechnexus-publish-binding \
  --binding-id tts-quality --revision 1 --revision-id binding-rev-1 \
  --capacity-manifest-id cap-tts-quality-1 \
  --qualification-report-id qrep-tts-quality-1

curl -sS -D "$SN_LOCAL_ROOT/models-tts.headers" \
  -o "$SN_LOCAL_ROOT/models-tts.json" http://127.0.0.1:58000/v1/models
jq '{count:(.data|length),ids:[.data[].id],first:.data[0].nexus}' \
  "$SN_LOCAL_ROOT/models-tts.json"
```

实际输出：

```text
published binding_id=tts-quality revision=1 revision_id=binding-rev-1 published_at_us=1793141422590968 capacity_manifest_id=cap-tts-quality-1 qualification_report_id=qrep-tts-quality-1 created=true
models_tts=200
etag: "sha256:c8c5ab5f01e46632a7e7be85fd77fc20323b517fb08c14d4f865c508cbcc1e91"
{
  "count": 1,
  "ids": ["tts-quality"],
  "first": {
    "logical_model": "qwen3-tts-customvoice",
    "binding": {"id": "tts-quality", "revision": "1"},
    "tasks": ["tts_file", "tts_realtime"],
    "release_tier": "production",
    "readiness": {
      "configured": true,
      "engine_ready": false,
      "quality_qualified": false,
      "admission_state": "unavailable"
    }
  }
}
```

再播种并发布 revision 2，验证“只列最新 revision、ETag 变化、旧 ETag 200/新 ETag 304”：

```bash
.venv/bin/python - <<'PY'
import psycopg
from tests.integration.test_publications_storage import DEFINITION
from speechnexus_backend.speech.application.binding_documents import encode_document

definition = {
    **DEFINITION,
    "binding_id": "tts-quality",
    "revision": "2",
    "revision_id": "binding-rev-2",
    "logical_model": "qwen3-tts-customvoice-v2",
    "execution_recipe": {
        **DEFINITION["execution_recipe"],
        "capacity_manifest_id": "cap-tts-quality-2",
    },
}
key = "sha256:" + "a2" * 32
request = "sha256:" + "b2" * 32
document = "sha256:" + "c4" * 32
raw = encode_document(definition).decode()
with psycopg.connect(
    "postgresql://postgres:<local-admin-password>@127.0.0.1:55442/speechnexus",
    autocommit=True,
) as conn:
    conn.execute("SET search_path TO speech, pg_catalog")
    conn.execute(
        "INSERT INTO speech_binding_commands "
        "(key_digest,binding_id,document_version,intent_json,request_digest) "
        "VALUES (%s,'tts-quality',1,'{}',%s)",
        (key, request),
    )
    conn.execute(
        "INSERT INTO speech_binding_revisions "
        "(binding_id,revision,revision_id,key_digest,document_version,"
        " definition_json,definition_digest,request_digest,created_at_us) "
        "VALUES ('tts-quality','2','binding-rev-2',%s,1,%s,%s,%s,1700000000000001)",
        (key, raw, document, request),
    )
    conn.execute("UPDATE speech_binding_families SET latest_revision='2' WHERE binding_id='tts-quality'")
print("seeded revision 2")
PY

.venv/bin/speechnexus-publish-binding \
  --binding-id tts-quality --revision 2 --revision-id binding-rev-2 \
  --capacity-manifest-id cap-tts-quality-2 \
  --qualification-report-id qrep-tts-quality-2

curl -sS -D "$SN_LOCAL_ROOT/models-tts-r2.headers" \
  -o "$SN_LOCAL_ROOT/models-tts-r2.json" http://127.0.0.1:58000/v1/models
jq '{count:(.data|length),id:.data[0].id,logical_model:.data[0].nexus.logical_model,revision:.data[0].nexus.binding.revision}' \
  "$SN_LOCAL_ROOT/models-tts-r2.json"
```

实际输出：

```text
published binding_id=tts-quality revision=2 revision_id=binding-rev-2 published_at_us=1793141435522911 capacity_manifest_id=cap-tts-quality-2 qualification_report_id=qrep-tts-quality-2 created=true
models_r2=200
etag: "sha256:678aa80beee5e674522e48d6da7859fac9667c0aafe18dbb7c3851625fa84ffa"
{
  "count": 1,
  "id": "tts-quality",
  "logical_model": "qwen3-tts-customvoice-v2",
  "revision": "2"
}
```

条件请求：

```bash
OLD=$(awk 'BEGIN{IGNORECASE=1} /^etag:/ {sub(/^[^:]*: /,""); gsub(/\r/,""); print}' \
  "$SN_LOCAL_ROOT/models-tts.headers")
NEW=$(awk 'BEGIN{IGNORECASE=1} /^etag:/ {sub(/^[^:]*: /,""); gsub(/\r/,""); print}' \
  "$SN_LOCAL_ROOT/models-tts-r2.headers")
curl -sS -o /dev/null -w 'old_etag=%{http_code}\n' \
  -H "If-None-Match: $OLD" http://127.0.0.1:58000/v1/models
curl -sS -o /dev/null -w 'new_etag=%{http_code}\n' \
  -H "If-None-Match: $NEW" http://127.0.0.1:58000/v1/models
```

实际输出：

```text
old_etag=200
new_etag=304
```

**这里证明的是**：空目录 → 有发布 → 新 revision 的 ETag 单调变化；目录只列最新
revision；304 只对当前 ETag 生效。**没有证明**：HTTP draft → 目录（5.1 缺陷未修）。

## 8. 失败模式表

| 现象/原文 | 原因 | 处置 |
| --- | --- | --- |
| `curl: (7) Failed to connect to 127.0.0.1 port 58000` | API 进程没在跑，或后台作业随 shell 结束被回收 | 在前台终端运行 3.2；确认 `readyz=200` |
| `POST /v1/nexus/admin/model-bindings` 返回 400 `invalid_binding_request` | 把 `deploy/engines/file-asr-audio/binding-definition.json` 原样当完整创建请求；它的 recipe 缺 `capacity_manifest_id`（5.4） | 先补齐完整创建身份与**真实** capacity manifest 引用；只用 `local-test-*` 做本地机制验证 |
| `speechnexus-publish-binding: error: Set SPEECHNEXUS_PUBLICATION_WRITER_DATABASE_URL ...`，退出 2 | 没设发布 writer DSN | 设 5.3 的 DSN；不要复用 owner 或通用 DSN |
| `evidence mismatch: ... declares capacity_manifest_id=missing, not ...`，退出 6 | 绕过 HTTP 校验测试播种的随附 ASR 定义没有该字段；支持的 POST 更早在 400 停住（5.4） | 这条播种只能证明发布守卫；真实路径需要真实 capacity manifest/qualification report |
| `evidence mismatch: ... declares capacity_manifest_id=..., not ...`，退出 6 | 证据 ID 与 revision 的 `execution_recipe.capacity_manifest_id` 不一致 | 从草稿/响应里取与 revision 相同的真实容量 ID；不要猜，也不要用 `local-test-*` 做真实注册 |
| `conflict: ... is already published as revision_id=...`，退出 4 | 同 `(binding_id, revision)` 已发布为另一个 revision_id | 用原 `revision_id` 重放；不要覆盖 |
| `publication unavailable: ...`，退出 5 | 写结果未知；本机另一次触发是播种文档不是 RFC8785 规范字节（`decode_document` 拒绝） | 用同一三元组重放查询；播种时用 `encode_document`，不要 `json.dumps` |
| `GET /v1/models` 发布后 503 `model_catalogue_unavailable` | HTTP draft 的 `definition_json` 缺 `binding_id` 等完整 revision 字段（5.1） | 主干修复前不要把该路径当可运行；测试路径见第 7 节 |
| admin GET 503 `Fixed binding metadata is unavailable.` | 证据行缺失/不一致，或未配置 admin reader | 用发布命令补证据；检查 `SPEECHNEXUS_BINDING_ADMIN_READER_DATABASE_URL` |
| `engine_instances` / `runtime_instance_intents` 都是 0 | 尚未注册引擎、未投递 runtime intent | 这不是错误；执行第 10 节前先提供 node controller/readiness |
| `pydantic_core._pydantic_core.ValidationError: 1 validation error for Settings` / `Value error, Configure one explicit binding writer mode`，退出 1 | 同时设置了两种 binding writer 模式 | 只保留一种（实测记录见本节末） |
| `ModuleNotFoundError: No module named 'onnxruntime'` | 网关普通依赖不含部署侧 CPU 推理 extra | 按 9.4 在**本 checkout 的** `.venv` 安装 `onnxruntime==1.31.0`；不要换到别的 venv |
| `ValueError: invalid gateway file ASR configuration` | 组合根把底层校验异常折叠成一句；本轮实测触发项是 `worker_limits.max_result_bytes=8388608` 超过硬上限 | 用 9.3 的逐段校验定位；把 `max_result_bytes` 改成 ≤ 4 MiB（本轮 4,194,304） |
| 网关启动成功但 `require_correlation_echo=true` 时执行失败 | 该 vLLM/OpenAI 兼容面不回显 `X-Request-ID`；网关按缺省 `true` 会拒绝 | 仅对该端点显式 `false`；同时接受“无法用回显证明响应对应本次请求”的风险（9.4） |
| `nohup: nice(5) failed: operation not permitted` | 本机 sandbox 禁止 `nohup` 的 `nice` | 前台运行 `speechnexus-gateway`，另开终端做 curl；不要用后台作业 |
| `psycopg.errors.SyntaxError: syntax error at or near "$1"`，语句为 `ALTER ROLE ... PASSWORD $1` | `ALTER ROLE ... PASSWORD` 不接受绑定参数 | 用 `psycopg.sql.Literal`/SQL 文本转义，或 `psql` 的 `\password`；不要把口令拼进 argv |
| `psycopg.errors.UndefinedTable: relation "resource_pools" does not exist` | 连接没有把 `speech` 放进 `search_path` | 连接后 `SET search_path TO speech, public`，或全部写全限定表名 |
| `engine_instances_host_node_id_fkey` | 直接播种实例前没有 `host_nodes` 行 | 先插 `host_nodes`（`node-del109`），再插 `engine_instances`；真实路径由 node controller 创建 |
| `say` 生成 0 帧 AIFF / 引擎 400 `Failed to apply Qwen3ASRProcessor` | 本机 sandbox 下 `say` 没有输出音频；空 WAV 被引擎正确拒绝 | 用公共 Silero 测试语音（9.5）；不要用空文件判断引擎失败 |
| submit 返回 200 `executing`，但 staging 还是 `null` | 文件执行是异步的；提交成功不等于结果已登记 | 按 11.1 轮询 `/staging-result`，直到 `staging_result != null`；不要重发新 intent |
| `uv run speechnexus-gateway` 找不到命令 / `.venv/bin` 里没有 `speechnexus-*` | 仓库根是 `package = false` 的 uv workspace；普通 `uv sync` 不装成员包，还会剪掉已装成员 | 用 `uv sync --all-packages`；装完检查 `.venv/bin/speechnexus-gateway` |
| `python -m speechnexus_gateway.main` 立即退出 0 且无日志 | `main.py` 没有 `if __name__ == "__main__"` 守卫 | 用 `python -c "from speechnexus_gateway.main import main; main()"` 或装好的 `speechnexus-gateway` |
| 公开请求 503 `backend_unavailable`，启动日志有 `Synchronous transcription is unavailable; missing configuration: ...` | API 仍是 3.1 的基础装配，没有转写配置组 | 按 3.4 补齐七项并重启；只补一部分仍会 503 |
| 进程内堆栈 `TranscriptionUnavailableError: Transcription admission was refused: quota_exceeded`，公开面 503 | `workspace_quotas.held_attempts` 已达上限；上一请求的尝试没有释放 | 先查 `admission_attempts.quota_state`、`admission_leases`；按 11.2.2 用真实 staging 对账/释放，不要连续重试 |
| 同上，拒绝码为 `capacity_exhausted` | `engine_instances.held_slots` 已达 `max_slots` | 确认租约是否仍 `reserved`；按 11.2.2 对账释放 |
| 同上，拒绝码为 `reconciliation_required` | 同一 operation 的持久事实不完整（上一租约/提交未知），准入拒绝继续 | 保留现场，按 11.2.2 的 `record_stop` + `release_in_transaction` 对账；不要插新行绕过 |
| 网关日志 `asr_deadline_exceeded`，`AsrCallError(..., request_may_have_run=True)` | recipe 的 `asr.final_deadline_ms=1000` 小于真实模型耗时 | 用 9.5 的真实时延定预算；本轮改成 `10000` 后通过 |
| 公开请求 120 秒后 `504 operation_timeout`，但网关 `/staging-result` 已有 185 B 结果 | `file_execution_worker._register` 只登记 staging；`execution_state` 仍是 `executing`，成功终态没有写者（11.2.1） | 重试解决不了；见 11.2.2 的人工驱动者字段与边界，不要声称公开转写可用 |
| 把 `submit_intents.recorded_at` 当成 API 放弃时刻，误判“提交握手阻塞 120s” | `recorded_at` 是提交落库时刻；r9 在请求开始后 0.6s 落库，同连接先 POST 200、再 458 次 GET 200 | 用请求文件 birth/mtime + 网关 access log 定墙钟；不要用 `recorded_at` 当完成时刻（11.2.1） |
| `terminated_at` 显示执行 2.586s，误当成原始终止事实 | 该值是 08:55:18 测试 `record_stop` 用 staging 登记的 `updated_at_us` 回填；清理前 `stuck-c4-before.json` 是 `executing/pending` | 查 `execution_stop_evidence.observed_at_us` 与对账脚本；回填值不是原始执行时长（11.2.1） |
| `gateway-lock/*.lock` 存在且被网关进程持有，误判为“上一次请求没释放锁” | 这是 `flock` 进程生命周期锁，不是每请求互斥；每请求 send 锁 `control_timeout_seconds=3s` | `lsof` 确认持有者是当前网关进程；不要删锁或重启网关来“解锁”（11.2.1） |
| 以为 `execution_gateway_timeout_seconds=10` 造成 120s 重试风暴 | submit 单次不重试；10s/重试预算只作用于幂等读；r9 只有 1 次 POST，随后是 API 轮询 GET | 查 `gateway_execution_client.py` 与网关日志；120s 是 `TranscriptionPlan.timeout_seconds` 的轮询截止（11.2.1） |
| 504 响应头 `date` 比 `time_total` 早 120s | 本机 uvicorn 的默认 Date 头；本轮没有把它当归因证据 | 用 `-w time_total`、文件 birth/mtime、access log 交叉判定；不要用 `date` 头算完成时刻 |
| 504 之后下一请求立刻 503 `capacity_exhausted` / `quota_exceeded` | 超时只结束 HTTP，不释放 `admission_leases` 与 `held_slots` | 先按 11.2.2 对账释放，再重试 |
| 网关 `POST /internal/v1/executions/submit` 一次返回 `422 Unprocessable Content`；同一文档扁平化重放返回 `200 idempotent_replay=true` | 具体触发条件未完全确认（持久文档是 wrapped `{execution_request, output_stream_id}`，HTTP schema 要 flat `execution_request`） | 用持久文档核对字段形状；不要把 422 写成稳定的参数错误 |
| 重复执行角色脚本报 `role already exists` | 手工 `CREATE ROLE` 非幂等 | 用 2.3 的脚本（先查 `pg_roles`）或 `DROP ROLE` 后重建 |

两种 writer 互斥的本轮实测（2026-10-28，独立端口 `58020`，不影响 `58000` 的运行实例）：

```bash
export SN_LOCAL_ROOT="$PWD/.paperclip-runtime/del109"
set -a
. "$SN_LOCAL_ROOT/api.env"
set +a
SPEECHNEXUS_PORT=58020 \
SPEECHNEXUS_BINDING_DEFAULTS_WRITER_DATABASE_URL="$SPEECHNEXUS_BINDING_WRITER_DATABASE_URL" \
  .venv/bin/speechnexus-api
```

实际输出（节选）与退出码：

```text
pydantic_core._pydantic_core.ValidationError: 1 validation error for Settings
  Value error, Configure one explicit binding writer mode [type=value_error]
exit=1
```

失败发生在绑定端口之前：`curl -m 2 http://127.0.0.1:58020/readyz` 是连接失败，不是 503。

## 9. 网关 file-ASR 配置样例与启动

9.1–9.3 给出字段级结构与形状校验；9.4 是本轮真实启动的完整过程，9.5 是不经网关的
引擎直连对照。样例中的 `000...` 摘要、token 与 `/absolute/...` 路径是占位符，不是凭据。

### 9.1 网关私有 JSON

文件必须是当前运行 UID 所有、无 group/other 权限的普通文件，绝对路径，≤65,536 bytes。
字段语义与校验来源见 `docs/architecture/file-asr-runtime.md`。

```bash
export SN_LOCAL_ROOT="${SN_LOCAL_ROOT:-$PWD/.paperclip-runtime/del109}"
mkdir -p "$SN_LOCAL_ROOT"
cat > "$SN_LOCAL_ROOT/gateway-file-asr.example.json" <<'JSON'
{
  "protocol": "file_asr_v1",
  "controlled_endpoint_file": "/absolute/private/config/asr-endpoint.json",
  "require_correlation_echo": false,
  "vad_model_file": "/absolute/private/models/silero_vad.onnx",
  "vad_model_sha256": "0000000000000000000000000000000000000000000000000000000000000000",
  "objects": {
    "endpoint": "http://127.0.0.1:59010",
    "input_bucket": "speechnexus-dev",
    "result_bucket": "speechnexus-dev",
    "spool_directory": "/absolute/private/spool",
    "tls_ca_file": null,
    "result_timeout_seconds": 30
  },
  "vad_tuning": {
    "config_revision": "silero-vad-2026-10",
    "threshold": 0.5,
    "end_threshold": 0.35,
    "min_speech_ms": 96,
    "prefix_padding_ms": 300,
    "silence_duration_ms": 500,
    "max_segment_ms": 20000
  },
  "input_limits": {
    "max_bytes": 268435456,
    "memory_bytes": 8388608,
    "timeout_seconds": 60
  },
  "decode_limits": {
    "max_encoded_bytes": 268435456,
    "max_duration_ms": 120000,
    "max_frame_samples": 65536,
    "timeout_seconds": 30
  },
  "worker_limits": {
    "max_result_bytes": 4194304,
    "max_command_bytes": 65536,
    "max_pending": 2,
    "shutdown_seconds": 10
  },
  "max_completion_tokens": 2048,
  "max_call_seconds": 120
}
JSON
chmod 600 "$SN_LOCAL_ROOT/gateway-file-asr.example.json"
```

| 字段 | 来源/取值理由 |
| --- | --- |
| `protocol` | 固定 `file_asr_v1`；见组合根 `_Deployment` |
| `controlled_endpoint_file` | 由 `scripts/dev-env/provision-controlled-asr.py` 生成的 0600 私有描述；网关只拿派生 key，不拿根密钥 |
| `require_correlation_echo` | 端点回显 `X-Request-ID` 时保持 `true`；本轮 GB10 vLLM 不回显，样例因此显式 `false`（风险见 9.4） |
| `vad_model_file` / `_sha256` | Silero ONNX 的绝对路径 + 64 位小写 SHA-256；启动会核对实际内容，不是只读 pin |
| `objects.endpoint` | 私有 IP origin，不使用 DNS/代理/重定向；本机隔离 MinIO 用 loopback |
| `objects.spool_directory` | 有界临时缓冲；当前 UID 所有、无 group/other 权限 |
| `vad_tuning` | 完整 `VadPolicy`：`config_revision`、阈值、最短语音、前后缀、静音、最大段 |
| `input_limits` | `ExecutionInputLimits`：`max_bytes`、`memory_bytes`、`timeout_seconds` |
| `decode_limits` | `AudioDecodeLimits`：编码字节、时长、帧样本、超时 |
| `worker_limits` | `FileExecutionLimits`：结果/命令字节、并发准备数、关闭等待；`max_result_bytes` 硬上限 4 MiB |
| `max_completion_tokens` / `max_call_seconds` | 单段 token 与调用秒数上限；由 `AsrPolicy` 校验 |

### 9.2 受控端点描述

由部署方用控制器根密钥和已批准目标离线派生；`bearer_token` 是 64 位小写十六进制，
不是根密钥。文件必须 0600、非 symlink；网关配置只引用它。

```bash
export SN_LOCAL_ROOT="${SN_LOCAL_ROOT:-$PWD/.paperclip-runtime/del109}"
mkdir -p "$SN_LOCAL_ROOT"
cat > "$SN_LOCAL_ROOT/controlled-asr-endpoint.example.json" <<'JSON'
{
  "protocol": "vllm_api_key_v1",
  "target": {
    "host_node_id": "node-gb10-1",
    "gateway_id": "gateway-asr-1",
    "pool_id": "asr-batch",
    "instance_id": "asr-1",
    "instance_generation": 1,
    "binding_revision_id": "mbrev-placeholder"
  },
  "address": "127.0.0.1",
  "port": 18100,
  "served_model": "qwen3-asr-1.7b",
  "bearer_token": "0000000000000000000000000000000000000000000000000000000000000000"
}
JSON
chmod 600 "$SN_LOCAL_ROOT/controlled-asr-endpoint.example.json"
```

确认文件可被解析且关键字段就位（不校验派生凭据本身）：

```bash
export SN_LOCAL_ROOT="${SN_LOCAL_ROOT:-$PWD/.paperclip-runtime/del109}"
.venv/bin/python -c "import json,os; p=os.path.join(os.environ['SN_LOCAL_ROOT'],'controlled-asr-endpoint.example.json'); d=json.load(open(p)); print('endpoint_shape_ok', d['protocol'], d['target']['host_node_id'], d['port'], d['served_model'])"
```

实际输出：

```text
endpoint_shape_ok vllm_api_key_v1 node-gb10-1 18100 qwen3-asr-1.7b
```

| 字段 | 来源/理由 |
| --- | --- |
| `protocol` | 固定 `vllm_api_key_v1` |
| `target.*` | 必须与网关 `NODE_ID`/`GATEWAY_ID` 和已准入实例的 node/gateway/pool/instance/generation/binding 完全一致 |
| `address` / `port` | 固定私网 IP 字面量；本机隧道为 `127.0.0.1:18100` |
| `served_model` | 引擎实际提供的模型名；本机探活为 `qwen3-asr-1.7b` |
| `bearer_token` | 控制器派生的代次 key；不得把根密钥、真实 token 写入仓库/日志/文档 |

### 9.3 已执行的形状校验

以下命令读取 9.1 实际写出的文件，验证 JSON 结构、嵌套类型与 ASR 结果硬上限；不读受控端点、不加载模型、不连引擎：

```bash
export SN_LOCAL_ROOT="${SN_LOCAL_ROOT:-$PWD/.paperclip-runtime/del109}"

.venv/bin/python - <<'PY'
import json
import os
from speechnexus_gateway.file_asr_runtime import _Deployment
from speechnexus_gateway.domain.vad import VadPolicy
from speechnexus_gateway.domain.execution_inputs import ExecutionInputLimits
from speechnexus_gateway.domain.media import AudioDecodeLimits
from speechnexus_gateway.domain.file_execution import FileExecutionLimits
from speechnexus_gateway.domain.asr import AsrPolicy
from speechnexus_gateway.domain.asr_results import validate_asr_result_limit

p = json.load(open(os.path.join(os.environ["SN_LOCAL_ROOT"], "gateway-file-asr.example.json")))
d = _Deployment.model_validate(p)
vad = VadPolicy(**d.vad_tuning)
inputs = ExecutionInputLimits(**d.input_limits)
decode = AudioDecodeLimits(**d.decode_limits)
worker = FileExecutionLimits(**d.worker_limits)
validate_asr_result_limit(worker.max_result_bytes)
AsrPolicy(vad.config_revision,
          max_completion_tokens=d.max_completion_tokens,
          timeout_seconds=d.max_call_seconds)
print("shape_valid", d.protocol, vad.config_revision,
      inputs.max_bytes, decode.max_duration_ms, worker.max_pending)
PY
```

实际输出：

```text
shape_valid file_asr_v1 silero-vad-2026-10 268435456 120000 2
```

### 9.4 网关启动（已执行）

**1. 安装部署侧 CPU 推理 extra（只装进本 checkout 的 `.venv`）**

```bash
uv pip install --python .venv/bin/python 'onnxruntime==1.31.0'
```

实际输出（节选）：

```text
 + flatbuffers==25.12.19
 + onnxruntime==1.31.0
 + protobuf==7.36.2
```

**2. 取得 pin 指向的 Silero ONNX，并核对内容摘要**

```bash
mkdir -p .paperclip-runtime/del109/models
curl -fsSL --max-time 120 \
  -o .paperclip-runtime/del109/models/silero_vad.onnx \
  'https://raw.githubusercontent.com/snakers4/silero-vad/bfdc0193023f121ea5b3cc7b176dbed570a68a59/src/silero_vad/data/silero_vad.onnx'
chmod 600 .paperclip-runtime/del109/models/silero_vad.onnx
shasum -a 256 .paperclip-runtime/del109/models/silero_vad.onnx
stat -f 'size=%z' .paperclip-runtime/del109/models/silero_vad.onnx
```

实际输出：

```text
1a153a22f4509e292a94e67d6f9b85e8deb25b4988682b7e174c65279d8788e3  .paperclip-runtime/del109/models/silero_vad.onnx
size=2327524
```

与 `deploy/engines/file-asr-audio/artifact-pin.json` 的 `vad.artifact.digest` 完全一致。
网关启动时会重新计算实际文件摘要，不是只读 pin。

**3. 准备私有 JSON、受控端点、网关角色、authority 行与输入对象**

```bash
.venv/bin/python scripts/dev-env/del109-local-gateway-fixture.py
```

实际输出：

```json
{
  "bucket": "del109-file-asr",
  "object_key": "private/del109/speech-12s-7958f003.wav",
  "bytes": 384078,
  "submit": ".../.paperclip-runtime/del109/submit.json",
  "config": ".../.paperclip-runtime/del109/gateway-file-asr.json",
  "gateway_env": ".../.paperclip-runtime/del109/gateway.env"
}
```

这个脚本只用于一次性隔离环境，做五件事：

- 以隔离 PG 的 owner 创建 `sn_gateway` 登录，并执行
  `coordination_runtime_grants("speech", speech_role="sn_api", gateway_role="sn_gateway")`
  的 gateway 半边；
- 直接播种 `resource_pools` / `workspace_quotas` / `host_nodes` / `engine_instances` /
  `runtime_instance_heads` / `admission_attempts` / `admission_leases`（第 10 节）；
- 把 12 秒公共测试语音上传到版本化桶 `del109-file-asr`；
- 用 `scripts/dev-env/provision-controlled-asr.py` 从一次性根密钥派生
  `vllm_api_key_v1` 描述；`bearer_token` 只写进 0600 文件，不回显；
- 写 `gateway-file-asr.json` 与 `gateway.env`（全部 0600），其中
  `require_correlation_echo=false`。

**偏差声明**：脚本把隔离 MinIO 实例的 root 身份写进
`SPEECHNEXUS_GATEWAY_FILE_ASR_OBJECT_ACCESS_KEY/_SECRET_KEY`。原因是本轮拿不到
`mc`（静态下载 410，`minio/mc` 镜像也不可用），没有创建 bucket-scoped 服务身份。
这个偏差只允许出现在一次性隔离桶；生产必须换成只对该桶 Get/Put/Delete/List 的
身份。网关代码本身不要求 root。

**4. 前台启动**

```bash
set -a
. .paperclip-runtime/del109/gateway.env
set +a
exec .venv/bin/speechnexus-gateway
```

实际输出（节选）：

```text
INFO:     Started server process [38237]
INFO:     Waiting for application startup.
INFO:     Application startup complete.
INFO:     Uvicorn running on http://127.0.0.1:58030 (Press CTRL+C to quit)
```

这是本机自己启动的一次记录。11.2 实际使用的是 SM+PO 在 `9d1b9bb` 上用同一份
`gateway.env` / `gateway-file-asr.json` 常驻的 `58030` 进程（PID 与启动时刻不同，
`/readyz` 同为 `ready`）；本轮只消费它，没有停止、重启或替换。

**5. 就绪检查**

```bash
curl -sS -D .paperclip-runtime/del109/gateway-readyz.headers \
  -o .paperclip-runtime/del109/gateway-readyz.json \
  -w 'readyz_http=%{http_code}\n' http://127.0.0.1:58030/readyz
jq . .paperclip-runtime/del109/gateway-readyz.json
```

实际输出：

```json
{
  "status": "ready",
  "checks": [
    { "name": "control_postgresql", "status": "ok", "detail": null },
    { "name": "gateway_ingress", "status": "ok", "detail": null }
  ]
}
readyz_http=200
```

`gateway_ingress` 在 `file_execution_required` 时会检查
`app.state.file_execution_runtime.running`（`execution-gateway/src/speechnexus_gateway/app.py:62-65`）。
它**没有**单独的 `file_worker` 探针名；把它读成“模型输出已验证”是错的。

**本轮踩到的启动硬上限**：`worker_limits.max_result_bytes` 必须 ≤ 4 MiB
（`MAX_ASR_RESULT_BYTES`），否则整个加载被折叠成
`ValueError: invalid gateway file ASR configuration`。把 8 MiB 改成 `4194304` 后通过；
定位方法是逐段调用组合根调用的原生类型，而不是猜字段。

**correlation echo 的真实风险**：该 GB10 vLLM 端点不回显 `X-Request-ID`，所以
`require_correlation_echo` 必须显式为 `false`。这表示网关无法用回显证明“这条响应确实
属于这次请求”；结果仍由 `request_digest`、私有对象版本和摘要绑定，但传输层对应关系
没有被证明。不要把 `false` 当成普通开关，也不要在生产端点沿用而不评估。

#### 9.4.1 四个实测过的启动坑

SM+PO 在本机主干 `9d1b9bb` 上用同一份 `gateway.env` / `gateway-file-asr.json` 启动时
逐个踩到并复现了下面四条；本轮照此启动成功。它们不是“环境玄学”，每条都有确定原因：

1. **`python -m speechnexus_gateway.main` 静默退出 0。** `main.py` 没有
   `if __name__ == "__main__"` 守卫，模块导入后什么都不做。用
   `python -c "from speechnexus_gateway.main import main; main()"`，或装好后的
   `speechnexus-gateway` 脚本。
2. **`uv run speechnexus-gateway` 找不到任何 `speechnexus-*`。** 仓库根是
   `package = false` 的 uv workspace，成员包只有 `uv sync --all-packages` 才会装；
   普通 `uv sync` 不但不装，还会把已装好的成员包剪掉。装完检查
   `.venv/bin/speechnexus-gateway` 是否存在。
3. **`onnxruntime` 不在网关开发依赖里。** 缺包时 VAD 初始化抛
   `cannot initialize the pinned CPU VAD runtime`（异常被折叠成一句 `VadError`，
   看不出是缺包）。按本节第 1 步装进本 checkout 的 `.venv`。
4. **`require_correlation_echo` 必须显式 `false`。** 该 Qwen3-ASR 端点不回显
   `X-Request-ID`，缺省 `true` 会被网关拒绝。代价是网关无法用回显证明“这条响应确实
   属于这次请求”；对应关系只剩 `request_digest`、对象版本与摘要。不要把 `false`
   当普通开关，也不要在别的生产端点上无评估地照抄。

启动后先看 `/readyz`（本节第 5 步），再发任何 submit；`readyz=200` 只证明控制库与
ingress 探针，不证明模型输出、终态回写或公开路由可用。

### 9.5 引擎侧探活与直连转写（已执行）

隧道是 SM+PO 在宿主上常驻的 `ssh -N -L 18100:127.0.0.1:8100 megacube`；本机只消费
`127.0.0.1:18100`，不连接、不启停 GB10。

```bash
curl -sS -m 5 -o /dev/null -w 'health=%{http_code}\n' http://127.0.0.1:18100/health
curl -sS -m 5 http://127.0.0.1:18100/v1/models | jq -r '.data[0].id'
```

实际输出：

```text
health=200
qwen3-asr-1.7b
```

直连引擎（**不经过网关**）发送一段 12 秒公共测试语音：

```bash
curl -sS --max-time 60 -w '\nhttp=%{http_code} time=%{time_total}\n' \
  -F 'file=@.paperclip-runtime/del109/audio/speech-12s.wav;type=audio/wav' \
  -F 'model=qwen3-asr-1.7b' -F 'response_format=json' \
  http://127.0.0.1:18100/v1/audio/transcriptions
```

实际输出：

```json
{"text":"And says, \"How do I get to Dublin?\" And the answer that comes back is, \"Well, I wouldn't start from here, Sonny.\" That is to say, much of political.","usage":{"type":"duration","seconds":12}}
http=200 time=1.464986
```

响应形状：只有整段 `text` 与 `usage.seconds`，没有 `segments`、词级或句级时间戳。
任何依赖分段/时间戳的步骤都会在这一层失败；本 runbook 因此不假设它们存在。

输入来源与摘要：

- `tests/data/test.wav`（`snakers4/silero-vad@bfdc019` 的公共测试音频，60 秒，
  SHA-256 `89f17d9c94c4b31eb320f424628bcbc920abaddbee6e2760fd868bfb1d9a2e47`）；
- 用 `ffmpeg -t 12 -ac 1 -ar 16000` 截成 `speech-12s.wav`（384,078 B，
  SHA-256 `84ffdcae36011af8fabcae1f49a84553479ea7a9e0924b9a7de40ec8a6b028a9`）。

这是公共测试音频，不是用户或生产音频。直连成功只证明“隧道 + 引擎 + 协议”可用，
不证明网关的 VAD、对象、账本或准入；后者见 11.1。

## 10. 引擎注册与 readiness：主干做不到，本轮用 fixture 行

> **交付边界（必读）**：`[已执行] 实例注册入口已合入主干（2f96fb3 / #275）：speechnexus-register-engine，`
> `用法见 docs/operations/register-engine.md；本节 fixture 行仅用于对照，运维请走该入口。`
> `注意：根 pyproject.toml 是 package = false 的 uv workspace，未执行 uv sync --all-packages 时`
> `uv run speechnexus-register-engine 不可用，需用 python -m speechnexus_backend.engine_registration。`
> DEL-104（真实引擎经网关全链路，#243 AC5）当前为 `blocked`；本节 fixture 只用于
> 本地验证，不是运维路径，也不构成注册、readiness 或生产资格。

主干事实没有变化：

- `create_runtime_intent` 只写 `preparing` 实例 + outbox
  （`backend/src/speechnexus_backend/speech/infrastructure/runtime_postgres.py:154`，
  `runtime_sql.py:545` 的 `INSERT INTO engine_instances`）；
- 转 `ready` 需要 `observe_runtime` 的受信 readiness 回执
  （`runtime_postgres.py:162`），而当前组合根没有对应 provider；
- 部署单元表明确写着 Node Controller 存在、Scheduler/Job Worker 的独立启动入口与
  完整业务装配待补齐（`docs/architecture/deployment-units.md:31`、`:35`）；
- `speechnexus-runtime-delivery-worker` 是投递 worker，不是注册器；本机只执行了
  `--help`：

```text
usage: speechnexus-runtime-delivery-worker [-h] [--once]

Deliver original runtime intents with persistent unknown recovery

options:
  -h, --help  show this help message and exit
  --once      Run one bounded pass, then exit
```

本机数据库实际状态：

```bash
docker exec del109-pg psql -U postgres -d speechnexus -Atc \
  "select instance_id||'|'||instance_generation||'|'||instance_state||'|'||host_node_id||'|'||pool_id||'|'||gateway_id||'|'||binding_revision_id
     from speech.engine_instances order by instance_id;
   select 'runtime_instance_intents|'||count(*) from speech.runtime_instance_intents;"
```

实际输出：

```text
instance-del109|1|ready|node-del109|asr-batch|gateway-del109|binding-del109
runtime_instance_intents|0
```

**这两行的含义必须一起读**：`engine_instances` 有一行 `ready`，但
`runtime_instance_intents` 是 0。前者是
`scripts/dev-env/del109-local-gateway-fixture.py` 直接 `INSERT` 的 fixture，后者证明
**没有任何真实 runtime intent 被创建、投递或观察**。这不是 node controller 注册成功，
也不是 readiness 回执。

fixture 播种的 authority 行（与 9.4 的脚本一致）：

| 表 | 行 | 作用 |
| --- | --- | --- |
| `host_nodes` | `node-del109` / `unified_memory` / `active` | 满足 `engine_instances` 的外键 |
| `resource_pools` | `asr-batch` / `batch` / `active` | submit 的第一把锁 |
| `workspace_quotas` | `workspace-del109` / `max_attempts=1` | `admission_attempts` 的外键 |
| `engine_instances` | `instance-del109` / gen 1 / `ready` / `max_slots=1` | `verify_submit_eligibility` 要求 `ready` 且代次对齐 |
| `runtime_instance_heads` | `instance-del109` / `current=1` / `high_water=1` | 代次头 |
| `admission_attempts` | `operation-del109` / `attempt-del109` / `batch` | 租约的尝试 |
| `admission_leases` | `lease-del109` / `reserved` / `pending` / deadline +1h | submit 的租约事实 |

这组行让网关真的执行了 `_lock_authority_rows`、`verify_submit_identity` 和
`verify_submit_eligibility`（`execution-gateway/src/speechnexus_gateway/infrastructure/submit_sql.py:245-345`），
包括 `instance_state=ready`、generation 头、lease deadline、fencing token 的比较。
它**不**证明：

- node controller 创建、投递或观察了实例；
- readiness 回执、`preparing → ready` 转移、代次递增或 fencing 转移；
- 预算、配额释放、停止证据或 GPU 资源实际释放。

**解除条件（具名）**：SM+PO/部署方提供 GB10 侧真实运行的 node controller（或等价
受信 readiness 回执），并在 `runtime_instance_intents` 写入对应的 prepare intent；
然后启动 `speechnexus-runtime-delivery-worker`，观察实例从 `preparing` 到 `ready`。
在那之前，本节只能以 fixture 行作为“最小替代”，不能写成注册已验证。

**本轮公开请求又留下了一条同类事实**：`operation-c4…` 的租约在 504 后仍是
`reserved`、`held_slots=1`，没有任何组件自动收敛。真实 staging 摘要存在，所以能用
生产 `record_stop` + `release_in_transaction` 人工对账（11.2.2）；这再次说明“结果
终态/发布 worker”没有装配。

## 11. 一次真实转写

### 11.1 经执行网关 internal submit（已执行）

前提：9.4 的网关在 `127.0.0.1:58030` 运行，10 的 fixture 行存在，输入对象是 9.5 的
12 秒公共测试语音。

提交（`submit.json` 由 fixture 脚本生成，含 identity、command digest 和真实对象摘要）：

```bash
set -a
. .paperclip-runtime/del109/gateway.env
set +a
curl -sS -D .paperclip-runtime/del109/submit.headers \
  -o .paperclip-runtime/del109/submit.response.json \
  -w 'submit_http=%{http_code} time=%{time_total}\n' \
  -X POST http://127.0.0.1:58030/internal/v1/executions/submit \
  -H "Authorization: Bearer $SPEECHNEXUS_GATEWAY_COMPONENT_TOKEN" \
  -H 'Content-Type: application/json' \
  --data-binary @.paperclip-runtime/del109/submit.json
jq '{submit_intent_id, execution_state, engine_request_id, idempotent_replay}' \
  .paperclip-runtime/del109/submit.response.json
```

实际输出：

```json
{
  "submit_intent_id": "intent-del109",
  "execution_state": "executing",
  "engine_request_id": "file-command-664100f692f048d5806a9ccf22460c73",
  "idempotent_replay": false
}
submit_http=200 time=0.05
```

轮询 staging（文件执行是异步的；提交 200 不等于结果已登记）：

```bash
for i in $(seq 1 60); do
  code=$(curl -sS -o .paperclip-runtime/del109/staging.json -w '%{http_code}' \
    -H "Authorization: Bearer $SPEECHNEXUS_GATEWAY_COMPONENT_TOKEN" \
    http://127.0.0.1:58030/internal/v1/executions/intent-del109/staging-result)
  if [ "$code" = 200 ] && jq -e '.staging_result != null' \
      .paperclip-runtime/del109/staging.json >/dev/null 2>&1; then break; fi
  sleep 1
done
echo "poll_http=$code"
jq '.staging_result | {storage_version, reference}' .paperclip-runtime/del109/staging.json
```

实际输出：

```json
{
  "storage_version": "d83608ea-1762-4902-bbff-ba55da84dbb8",
  "reference": {
    "asset_or_artifact_id": "asr-result-87c4537e271aaef75f3ccb8b2eab37ae4494b46ab21885cc24e94ee31618749f",
    "workspace_id": "workspace-del109",
    "storage_bucket": "del109-file-asr",
    "object_key": "private/execution-results/87c4537e271aaef75f3ccb8b2eab37ae4494b46ab21885cc24e94ee31618749f/transcription.json",
    "content_sha256": "sha256:c707b7078ba7f8161ea3738e14beaa3b97d627193e147c009c193cb2cc4477b8",
    "byte_length": 185
  }
}
```

读回固定版本私有对象并核对摘要（使用隔离 MinIO 的 root 身份；只读取结果）：

```bash
set -a
. .paperclip-runtime/del109/minio.env
set +a
MINIO_ROOT_USER="$MINIO_ROOT_USER" MINIO_ROOT_PASSWORD="$MINIO_ROOT_PASSWORD" \
  .venv/bin/python - <<'PY'
import hashlib, json, os
from minio import Minio
staging = json.load(open('.paperclip-runtime/del109/staging.json'))['staging_result']
ref = staging['reference']
c = Minio('127.0.0.1:59010', access_key=os.environ['MINIO_ROOT_USER'],
          secret_key=os.environ['MINIO_ROOT_PASSWORD'], secure=False)
r = c.get_object(ref['storage_bucket'], ref['object_key'], version_id=staging['storage_version'])
try:
    body = r.read()
finally:
    r.close(); r.release_conn()
digest = 'sha256:' + hashlib.sha256(body).hexdigest()
print('object_sha256=' + digest)
print('receipt_matches', digest == ref['content_sha256'])
print(json.dumps(json.loads(body), ensure_ascii=False, indent=2))
PY
```

实际输出：

```text
object_sha256=sha256:c707b7078ba7f8161ea3738e14beaa3b97d627193e147c009c193cb2cc4477b8
receipt_matches True
{
  "text": "And says, \"How do I get to Dublin?\"\nAnd the answer that comes back is, \"Well, I wouldn't start from here, Sonny.\"\nThat is to say, much of political.",
  "duration_ms": 12000
}
```

重复提交同一 `submit.json`：

```bash
curl -sS -o .paperclip-runtime/del109/submit.replay.json -w 'replay_http=%{http_code}\n' \
  -X POST http://127.0.0.1:58030/internal/v1/executions/submit \
  -H "Authorization: Bearer $SPEECHNEXUS_GATEWAY_COMPONENT_TOKEN" \
  -H 'Content-Type: application/json' \
  --data-binary @.paperclip-runtime/del109/submit.json
jq '{idempotent_replay, execution_state, engine_request_id}' \
  .paperclip-runtime/del109/submit.replay.json
```

实际输出：

```json
{
  "idempotent_replay": true,
  "execution_state": "executing",
  "engine_request_id": "file-command-664100f692f048d5806a9ccf22460c73"
}
replay_http=200
```

结果对象的版本数与账本计数：

```text
result_object_versions 1 ['d83608ea-1762-4902-bbff-ba55da84dbb8']
submit_intents=1
execution_stop_evidence=0
```

**这一节证明了**：真实网关进程用真实 Silero ONNX、真实 PyAV 解码、真实 MinIO 固定
版本对象和真实 Qwen3-ASR 1.7B 完成了一次 12 秒转写；结果正文从对象读回并用
SHA-256 核对；重复 intent 没有再次派发，也没有写入第二个结果版本；没有伪造停止证据。

**这一节没有证明**：公开 `/v1/audio/transcriptions`、私有准入 recipe、live quota /
admission / pool owner / fencing 转移、node controller readiness、预算释放、停止、
质量、时延或容量。`execution_state=executing` 是账本在结果登记后的当前状态；不要把它
误读成“GPU 已释放”或“执行已终结”。

### 11.2 公开 `/v1/audio/transcriptions`（已执行：504，未成功）

先给结论：这一跳**真实执行过**，但没有成功。API 在 120 秒后返回
`504 operation_timeout`；同一请求在网关侧完成了 VAD + ASR 并把 185 B 结果写进 MinIO，
可执行账本停在 `executing`，没有成功路径把它推进到终态。**不要**把 11.1 的 staging
成功或本节的 504 读成“公开转写可用”。

r10 对 `submit_intents`、网关访问日志和 ingress 锁做了交叉核对：120 秒花在 API
轮询，不是提交握手；两条候选假设（ingress 锁、deadline 重试）均被排除。时序与代码
根因见 11.2.1，先不要按“提交阻塞”去调网关超时或清锁。

相邻两次失败不能合并成一个问题：

| 轮次 | 公开面 | 实际原因 | 处置 |
| --- | --- | --- | --- |
| r8 08:49 | `503 backend_unavailable` | 准入拒绝；进程内 trace 是 `reconciliation_required`，表示池归属或释放事实需要对齐 | 保留现场，按 11.2.2 核对；不要普通重放，也不要用它解释 r9 |
| r9 08:50 | `504 operation_timeout` | 提交已在请求开始后约 0.6 秒落库，执行约 2.586 秒产出 staging，随后 API 轮询满 120 秒；缺少成功终态写者 | 见 11.2.1 / 11.2.2；不是网关慢或 ingress 锁 |

本轮前提：

| 项 | 值 |
| --- | --- |
| API | `127.0.0.1:58000`，用 3.4 的配置重启 |
| 私有 recipe | `.paperclip-runtime/del109/transcription-recipe.json`，`recipe_digest=sha256:6549a48e…1fde864` |
| 已发布 binding | `asr-bundled-local` rev 1 → `revision_id=mbrev-db04f96ee00e454a804d3631ac2c8780` |
| 音频 | `audio/speech-12s.wav`，384,078 B，12 秒公共测试语音 |
| 网关 / 引擎 | SM+PO 的 `127.0.0.1:58030` / `18100`；本轮只消费，不启停 |

执行的命令：

```bash
curl -sS -D .paperclip-runtime/del109/transcription-r9.headers \
  -o .paperclip-runtime/del109/transcription-r9.json \
  -w 'transcription_http=%{http_code} time=%{time_total}s bytes=%{size_download}\n' \
  -X POST http://127.0.0.1:58000/v1/audio/transcriptions \
  -F file=@.paperclip-runtime/del109/audio/speech-12s.wav \
  -F model=asr-bundled-local \
  -F response_format=json
```

实际输出：

```text
transcription_http=504 time=120.125005s bytes=212
```

原始响应头与正文：

```http
HTTP/1.1 504 Gateway Timeout
date: Wed, 28 Oct 2026 00:50:50 GMT
cache-control: no-store
content-length: 212
content-type: application/json
x-request-id: 48a77608763e451b9765f74b40ed71a9
```

```json
{"error":{"code":"operation_timeout","type":"service_unavailable_error","message":"The transcription request exceeded its deadline.","param":null,"request_id":"48a77608763e451b9765f74b40ed71a9","retryable":true}}
```

`request_id` 由 API 中间件生成。响应头的 `date` 与 `time_total` 的结束时刻不一致
（早 120 秒）；它是本机 uvicorn 的默认 Date 头，本轮没有把它当归因证据。判定以
`time_total`、文件 birth/mtime 和网关 access log 为准（11.2.1）。
`retryable=true` 只表示错误类别可重试，**不代表现在重试会成功**——根因是终态没有回写。

同一请求在网关侧的事实：

```bash
curl -sS -o .paperclip-runtime/del109/gateway-status-c4.json -w 'http=%{http_code}\n' \
  -H "Authorization: Bearer $SPEECHNEXUS_GATEWAY_COMPONENT_TOKEN" \
  http://127.0.0.1:58030/internal/v1/executions/submit-c4fee1356ac468cf431337cf1e91b600c83d1c7c3e9d048d
jq '{execution_state, cleanup_state, engine_request_id, deadline_at, staging_artifact_ref}' \
  .paperclip-runtime/del109/gateway-status-c4.json
```

实际输出（节选）：

```json
{
  "execution_state": "executing",
  "cleanup_state": "pending",
  "engine_request_id": "file-command-8722f870bc2f4d73a4170f7c2ab9324f",
  "deadline_at": "2026-10-28T00:52:50.594716Z",
  "staging_artifact_ref": {
    "storage_bucket": "del109-file-asr",
    "object_key": "private/execution-results/ffc11d31.../transcription.json",
    "content_sha256": "sha256:c707b7078ba7f8161ea3738e14beaa3b97d627193e147c009c193cb2cc4477b8",
    "byte_length": 185
  }
}
```

即：**真实结果已经产生**（185 B，与 11.1 同一内容摘要），但 `execution_state`
没有离开 `executing`。API 侧 `TranscriptionPlan.timeout_seconds` 缺省 120
（`backend/src/speechnexus_backend/speech/domain/transcription.py:194`），轮询到点后抛
`TranscriptionDeadlineExceededError`，映射成 `504 operation_timeout`
（`backend/src/speechnexus_backend/speech/interfaces/http/transcriptions.py:481-482`）。

为什么没终结：文件 worker 的成功路径 `_register` 只调用
`record_execution_observation(..., staging_result=...)` 登记 staging 引用；
`ExecutionObservation.apply` 在没有 `record_stop` 时保留当前 `EXECUTING`，并显式拒绝
terminal/complete 事实（`execution-gateway/src/speechnexus_gateway/infrastructure/file_execution_worker.py:273`、
`execution-gateway/src/speechnexus_gateway/domain/observation.py:114`）。域模型虽然定义了
`complete_successfully()`（`execution-gateway/src/speechnexus_gateway/domain/submission.py:801`），
但**没有生产调用者**（测试会调用）；`record_stop(SUCCEEDED)` 只在恢复/测试对账路径出现。因此“提交 +
登记 staging”已装配，“成功终态”没有写者。部署单元表对 Scheduler/Job Worker 写的
“独立启动入口及完整业务装配待补齐”就是这条
（`docs/architecture/deployment-units.md:31`、`:35`）。

### 11.2.1 时序测量：120 秒是 API 轮询，不是提交握手

先把表放出来。查询命令（`owner.env` 只提供本机测试口令，DSN 不写入本文）：

```bash
set -a; . .paperclip-runtime/del109/owner.env; set +a
psql "postgresql://postgres@127.0.0.1:55442/speechnexus" -P pager=off -c "
SELECT version, execution_state, cleanup_state,
       to_char(to_timestamp(recorded_at_us/1e6) AT TIME ZONE 'UTC','HH24:MI:SS.MS') AS recorded_utc,
       to_char(to_timestamp(dispatched_at_us/1e6) AT TIME ZONE 'UTC','HH24:MI:SS.MS') AS dispatched_utc,
       to_char(to_timestamp(updated_at_us/1e6) AT TIME ZONE 'UTC','HH24:MI:SS.MS') AS updated_utc,
       CASE WHEN terminated_at_us IS NULL THEN NULL
            ELSE to_char(to_timestamp(terminated_at_us/1e6) AT TIME ZONE 'UTC','HH24:MI:SS.MS') END AS terminated_utc,
       round((dispatched_at_us-recorded_at_us)/1e6,3) AS rec_to_disp_s,
       round((terminated_at_us-COALESCE(dispatched_at_us,recorded_at_us))/1e6,3) AS disp_to_term_s,
       staging_output_object_key IS NOT NULL AS has_staging
FROM speech.submit_intents WHERE submit_intent_id LIKE 'submit-c4fee%';"
```

实际输出（r10，原始文件 `r10-submit-timing.psql`；为 Markdown 去掉了表格行尾空格）：

```text
 version | execution_state | cleanup_state | recorded_utc | dispatched_utc | updated_utc  | terminated_utc | rec_to_disp_s | disp_to_term_s | has_staging
---------+-----------------+---------------+--------------+----------------+--------------+----------------+---------------+----------------+-------------
       4 | succeeded       | complete      | 00:50:50.618 | 00:50:50.629   | 00:55:18.202 | 00:50:53.214   |         0.010 |          2.586 | t
(1 row)
```

**读法（关键）**：`recorded_utc` 是提交落库时刻，不是 API 放弃时刻。r9 的请求文件
`transcription-r9.curl` 创建于 `08:50:50`（Asia/Shanghai），`recorded` 是
`08:50:50.618`，只晚 0.6 秒；`dispatched` 再晚 10 ms。`terminated_utc=00:50:53.214`
也不是原始终止：它是 08:55:18 的测试对账 `record_stop` 用 staging 登记的
`updated_at_us` 回填的（`reconcile-c4-test.py` 的 `observed_at_us=int(row["updated_at_us"])`）。
清理前 `stuck-c4-before.json` 只有 `execution_state=executing`、`has_staging=true`，
没有终止事实；对账后 `updated_at` 才变成 `00:55:18.202`。**不要把这一列当原始执行
时长证据**；原始 staging 登记时间是 `00:50:53.214`，执行本身约 2.586 秒。

请求墙钟（原始文件 `r10-request-bounds.txt`）：

```text
.paperclip-runtime/del109/transcription-r9.curl    birth=08:50:50 mtime=08:52:50
.paperclip-runtime/del109/transcription-r9.headers birth=08:50:50 mtime=08:52:50
.paperclip-runtime/del109/transcription-r9.json    birth=08:52:50 mtime=08:52:50
```

网关访问日志（`/private/tmp/gw5.log`，SM+PO 常驻实例；原始片段
`r10-gateway-access.txt`）：

```text
POST /internal/v1/executions/submit (all experiments): 6
GET submit-c4fee (all clients): 463
GET submit-c4fee from API client 127.0.0.1:61040: 458

483: 127.0.0.1:61040 - "POST /internal/v1/executions/submit HTTP/1.1" 200 OK
484: 127.0.0.1:61040 - "GET /internal/v1/executions/submit-c4fee... HTTP/1.1" 200 OK
...
941: 127.0.0.1:61040 - "GET /internal/v1/executions/submit-c4fee... HTTP/1.1" 200 OK
```

结论：

1. **提交没阻塞**：同一 API 客户端 `61040` 在 08:50:50 先拿到 submit `200`，随后
   从日志第 484 行到第 941 行连续 458 次 GET，全部 `200`。如果提交卡 120 秒，不可能
   先有 POST 200 再出现这串 GET。
2. **执行没慢**：staging 在 08:50:53 已登记（185 B），之后 API 又轮询了约 117 秒；
   504 是 `TranscriptionPlan.timeout_seconds=120` 的轮询截止
   （`backend/src/speechnexus_backend/speech/domain/transcription.py:194`），不是网关
   执行超时。
3. **状态没推进**：worker 的 `_register` 只登记 staging；`observe()` 永远返回
   `EXECUTING`（`file_execution_worker.py:363`），`submit_execution._reconcile` 也只
   采纳 `EXECUTING`（`application/submit_execution.py:184`）。成功终态没有写者。
4. **ingress 锁假设排除**：`ProcessIngressOwnership` 在进程启动时对
   `gateway-lock/<sha256>.lock` 做一次 `flock(LOCK_EX|LOCK_NB)` 并持有到进程退出
   （`execution-gateway/src/speechnexus_gateway/infrastructure/process_ownership.py:34`）；
   `lsof` 显示 PID `95889` 持有该文件（`r10-lock-owner.txt`）。这是**生命周期锁**，
   不是每请求互斥。每请求的 `GatewayIngress.dispatch` 只用
   `lane.sending.acquire(timeout=self._timeout)`，`control_timeout_seconds` 为 3 秒
   （`application/ingress.py:72`、`settings.py:41`），不可能等 120 秒。
5. **10 秒 deadline 重试假设排除**：submit 是单次发送，
   `GatewayExecutionClient.submit` 明确“never retried”
   （`backend/src/speechnexus_backend/speech/infrastructure/gateway_execution_client.py:9,239`）；
   `execution_gateway_max_retries` 只用于幂等读。日志只有 1 次 r9 POST，没有 10 秒
   一轮的 POST 重试。
6. **逻辑上也不可能**：`_poll` 是在 `_submit` 返回之后才设置
   `deadline = time.monotonic() + plan.timeout_seconds`
   （`backend/src/speechnexus_backend/speech/infrastructure/gateway_transcription.py:211`）。
   若 submit 真卡 120 秒，轮询的 120 秒预算再叠加，总耗时必然 ≥240 秒；r9 实测总耗时
   是 120.125 秒。因此 submit 必须在轮询开始前就已返回。

**因此，这一跳的停止点是“staging 登记后没有终态写者”，不是“提交握手阻塞”或
“ingress 锁未释放”。** 下一接收人若要修，应从 file worker 的成功终态事务入手，而不是
调大网关超时或清 lock 文件。

504 后的持久事实（`stuck-c4-before.json`，清理前）：

```json
{
  "lease": {"lease_state": "reserved", "cleanup_state": "pending", "version": 1},
  "intent": {"execution_state": "executing", "cleanup_state": "pending", "has_staging": true},
  "instance": {"max_slots": 1, "held_slots": 1, "state": "ready"},
  "workspace_quota": {"held_attempts": 1, "max_attempts": 2}
}
```

**504 不会释放租约**：超时只结束 HTTP 请求，`admission_leases` 仍是 `reserved`、
`held_slots=1`。同一 workspace 的下一次请求会先撞 `capacity_exhausted` /
`quota_exceeded`，而不是再次执行——这是本机前几轮 503 的直接原因之一。

为了让隔离环境回到可用状态，本轮用**仅测试用途**的 `reconcile-c4-test.py` 做人工对账：
读取该 intent 已登记的真实 staging 摘要，用网关生产账本的
`record_stop(ENGINE_TERMINAL_RESPONSE)` 把执行置为 `succeeded`，再用后端生产
`release_in_transaction` 释放租约；随后把 `workspace_quotas.max_attempts` 从临时值 2
恢复为 1。

```text
record_stop succeeded version 4
release committed lease-c4fee1356ac468cf431337cf1e91b600c83d1c7c3e9d048d
exit=0
```

清理后（`stuck-c4-after.json`）：

```json
{
  "lease": {"lease_state": "released", "cleanup_state": "complete", "version": 2},
  "intent": {"execution_state": "succeeded", "cleanup_state": "complete"},
  "instance": {"max_slots": 1, "held_slots": 0, "state": "ready"},
  "workspace_quota": {"held_attempts": 0, "max_attempts": 1}
}
```

**这段对账不是产品能力**：它只证明“有真实 staging 时，生产 release/record_stop 能正确
收敛”，同时反证**没有已装配的 worker 自动做这件事**。第二个人照跑时，不要把它当成
公开转写成功路径。

### 11.2.2 人工驱动者：谁调用 `record_stop`，字段怎么来

**产品路径**：正确位置是 file execution worker 的成功分支。`stage_result` 返回并登记
staging 后，worker 必须在同一可信进程内以真实引擎终态响应调用
`record_stop(..., ExecutionState.SUCCEEDED)`（或提供等价的终态事务），再让 admission
按该事实释放租约。`c435908` 的 worker 只到 `_register` 登记 staging；
`complete_successfully()` **没有生产调用者**（测试会调用），因此公开请求必然等到 504。这是装配缺口，不是
API 配置错误，也不是网关超时设置。

**人工路径**：只有在没有产品写者、但已有真实 staging 终态结果时，才由持有控制库与网关
凭据的受信任进程（本轮是 SM+PO 指定的本机测试执行者）做一次证据收敛。它不是 HTTP
客户端，不是 API 的替代实现，更不是“公开转写成功”的证明。不要在没有 staging、没有
引擎终态响应或已有其他 stop evidence 时调用。

#### 先做只读核对

把 `<SUBMIT_INTENT_ID>` 换成本次请求的 `submit-c4fee...`。下面的查询实际执行过；它
不修改任何行：

```bash
set -a; . .paperclip-runtime/del109/owner.env; set +a
psql "postgresql://postgres@127.0.0.1:55442/speechnexus" -P pager=off -c "
SELECT s.submit_intent_id, s.version, s.execution_state, s.cleanup_state,
       s.staging_output_object_key IS NOT NULL AS has_staging,
       (s.staging_result_document::jsonb)->'reference'->>'content_sha256' AS staging_digest,
       to_char(to_timestamp(s.updated_at_us/1e6) AT TIME ZONE 'UTC',
               'YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"') AS current_updated_utc,
       (SELECT count(*) FROM speech.execution_stop_evidence e
         WHERE e.submit_intent_id = s.submit_intent_id) AS stop_evidence_rows
FROM speech.submit_intents s
WHERE s.submit_intent_id = '<SUBMIT_INTENT_ID>';"
```

必须同时看到：`execution_state=executing`、`cleanup_state=pending`、
`has_staging=t`、`staging_digest` 是 `sha256:` 加 64 个小写十六进制、`stop_evidence_rows=0`。
否则停止，不要构造摘要或时间。`current_updated_utc` 来自 `updated_at_us`：在调用
`record_stop` **之前**、且没有后续 observation 时，它就是 `_register` 登记 staging 的
时刻；调用后这一列会被终态事务推进，不能再拿来当 staging 时间。本机清理前的对应值
是 `2026-10-28T00:50:53.214...Z`（见 11.2.1 的 timing 表）；清理后复跑同一查询会得到
`succeeded`、`stop_evidence_rows=1`，那是已收敛状态，不能作为再次调用的判据。

#### 字段映射

| 参数 | 取值 | 原因 / 边界 |
| --- | --- | --- |
| `terminal_state` | `succeeded` | 真实引擎已返回整段结果；成功终态不能用于 `unknown_quarantined` 或未派发的 intent |
| `evidence_kind` | `engine_terminal_response`（枚举 `ENGINE_TERMINAL_RESPONSE`） | `RecordStop` 规定 `SUCCEEDED` 只接受这一种 kind；`engine_abort_ack` 只适用于 abort，`unconfirmed_timeout` / `none` 不构成已验证 stop |
| `evidence_digest` | `staging_result_document.reference.content_sha256` | 这是结果对象的固定内容摘要，必须原样为 `sha256:<64 lowercase hex>`；不要用 `request_digest`、音频 SHA 或临时对象 digest |
| `observed_at_us` | 调用前读到的 `submit_intents.updated_at_us` | 域模型要求它不早于 `dispatched_at`、不晚于本次 `record_stop` 时间；它对应 staging 登记事实，不能取 `now()` 或对账后的 `terminated_at`；调用后要查 `execution_stop_evidence.observed_at_us` 复核 |
| `evidence_id` | `terminal-<submit_intent_id 去掉 submit- 前缀后前 48 个字符>` | 确定、可重放，且满足共享 Identifier；本轮 ID 去前缀后正好 48 字符；同一个 intent 只能有一条 stop evidence |
| `execution_stopped` | `true` | 只有在引擎终态结果已固定、不会再有输出时才可置真 |
| `resources_released` | `true` | 只有在能确认该次引擎调用已结束、容量不再被占用时才可置真；本轮脚本把它作为“整段结果已返回”的测试断言，不是引擎显式 ACK |
| `reason` | `NULL` | `engine_terminal_response` 不要求 reason；`operator_verified_instance_terminated` 才要求且不能用于成功终态 |
| `error_code` | `NULL` | 只有 `failed` 终态才允许并必须带 error code |

这里有一个必须说清的限制：`record_stop` 只校验摘要格式和账本一致性，**不会重新读取
MinIO、不会重新调用引擎**；`execution_stopped` / `resources_released` 是调用者提交的
布尔断言。上面的摘要映射要求调用者先从 staging 文档核对，不能因为 `record_stop`
返回成功结果就宣称对象内容或资源释放已被独立验证。

#### 已执行的人工收敛命令

本轮实际执行的是下面这一条。脚本先读真实 staging，按上表组装
`VerifiedStopEvidence`，调用网关生产账本的 `record_stop`，再用后端生产
`release_in_transaction` 释放租约；脚本末尾把测试期间临时放大的
`workspace_quotas.max_attempts` 恢复为 1。它不插入新 intent，也不直接改
`execution_state`：

```bash
set -a; . .paperclip-runtime/del109/gateway.env; set +a
.venv/bin/python .paperclip-runtime/del109/reconcile-c4-test.py
```

实际输出：

```text
record_stop succeeded version 4
release committed lease-c4fee1356ac468cf431337cf1e91b600c83d1c7c3e9d048d
exit=0
```

脚本里的关键调用等价于：

```python
evidence = VerifiedStopEvidence(
    evidence_id="terminal-" + submit_intent_id.removeprefix("submit-")[:48],
    submit_intent_id=submit_intent_id,
    evidence_kind=StopEvidenceKind.ENGINE_TERMINAL_RESPONSE,
    evidence_digest=staging_document["reference"]["content_sha256"],
    observed_at_us=staging_record_updated_at_us,
    execution_stopped=True,
    resources_released=True,
    reason=None,
)
ledger.record_stop(RecordStop(evidence, ExecutionState.SUCCEEDED))
# then, with the current lease version:
release_in_transaction(cursor, ReleaseLease(lease_id, version), clock=...)
```

这段代码是说明字段来源的**等价片段**，不是另一次已执行命令；真正执行过的是上面的
`reconcile-c4-test.py`。如果脚本不在你的工作区，必须按同一字段映射重写并先在隔离库
验证，不能复制一个固定摘要直接跑。

#### 调用后的验收

```bash
psql "postgresql://postgres@127.0.0.1:55442/speechnexus" -P pager=off -c "
SELECT s.submit_intent_id, s.execution_state, s.cleanup_state,
       e.evidence_kind, e.evidence_digest,
       l.lease_state, l.cleanup_state AS lease_cleanup
FROM speech.submit_intents s
LEFT JOIN speech.execution_stop_evidence e USING (submit_intent_id)
LEFT JOIN speech.admission_leases l ON l.lease_id = s.lease_id
WHERE s.submit_intent_id = '<SUBMIT_INTENT_ID>';"
```

期望：`succeeded / complete / engine_terminal_response / 同一 staging digest /
released / complete`，并且 `engine_instances.held_slots=0`、
`workspace_quotas.held_attempts=0`。本轮对应的清理后事实是
`stuck-c4-after.json`：`lease=released/complete`、`held_slots=0`、
`held_attempts=0`、`max_attempts=1`。

**不要做**：不要用 `UPDATE speech.submit_intents SET execution_state='succeeded'` 绕过
证据表；不要把 `request_digest` 或音频 SHA 当 `evidence_digest`；不要把当前墙钟当
`observed_at_us`；不要在已有 stop evidence 上制造第二条；不要把 `resources_released`
写成 true 而实际无法证明资源已释放。任何一条不满足，就只能保留
`executing`/`unknown_quarantined` 现场并交给结果发布/worker 装配实现方。

**停在第 11.2 步，缺的是**：文件 worker 成功路径的终态写者——在 staging 登记后写
`record_stop(ENGINE_TERMINAL_RESPONSE, SUCCEEDED)` 或等价的终态事务，并与 admission
deadline / 租约释放联动；不是“提交握手”或“ingress 锁”。解除人：SM+PO 指派的结果
发布/worker 装配实现方；本文不编造卡号。

**另外两条本轮实测的边界**：

- 早先几轮公开请求是 `503 backend_unavailable`（r3–r8），进程内诊断分别为
  `quota_exceeded`、`capacity_exhausted`、`reconciliation_required`；HTTP 面的证据是
  `transcription-r3..r8.headers/json`，原因归因来自 `public-r7-trace.out` /
  `public-r8-trace.out` 的进程内 trace。两者不能互相替代：r8 的 503 是准入拒绝，
  r9 的 504 是轮询截止，不能合并成一个“超时”问题；
- 网关曾在一次旧请求上返回 `422 Unprocessable Content`；把同一持久文档解包成 flat
  `execution_request` 重放则返回 `200 idempotent_replay=true`
  （`gateway-replay-b45.json`）。具体 422 的归因**没有完全确认**，不要写成稳定的
  参数错误。

**没有证明**：公开成功响应、结果发布、`retryable=true` 的实际可重试性、并发下的
配额/租约正确性、生产级对象身份与 RBAC。

## 12. 边界声明

**这份装配证明了**

- `c435908` 的迁移可以从空库到 `0021` head；
- 四个非 owner 角色能通过各自 provider 的启动核验，且不会回退到 owner；
- API 的 readiness、空目录 200/ETag/304；
- draft POST、发布 CLI 的必填证据、幂等回放、错误码；本地测试 capacity/qualification
  ID 可以走通 draft/publish 机制，但仓库随附 ASR 定义原样会在草稿层 400，测试播种后
  发布层报 `capacity_manifest_id=missing`（5.4）；
- admin GET 的 draft/published/证据缺失 503/未配置 reader 503；
- 目录 reader 本身在“完整 revision 文档”上能返回 200、ETag 变化与 304；
- 带 file-ASR 的网关能在真实 DB/MinIO/VAD/端点配置上启动，`/readyz` 通过；
- 网关 internal submit 能经真实 Silero VAD 与真实 Qwen3-ASR 完成一次 12 秒转写，
  结果对象固定版本读回、摘要匹配、正文非空、`duration_ms=12000`；
- 同 intent 重放不增加结果版本，`execution_stop_evidence` 保持 0；
- 公开 `POST /v1/audio/transcriptions` 已真实发出并走完 API → admission → 网关 →
  真实 ASR → MinIO staging；请求本身在 120 秒后以 `504 operation_timeout` 结束（11.2）；
- r10 的时序交叉核对：r9 的 submit 由同一 API 连接拿到 200，随后连续 458 次 GET 200；
  `recorded_at` 是提交落库（请求开始后 0.6s），`terminated_at` 是 08:55 测试对账回填；
  120 秒是 API 轮询截止；ingress 生命周期锁与 10 秒 deadline 重试假设均被排除（11.2.1）；
- 有真实 staging 时，生产 `record_stop(ENGINE_TERMINAL_RESPONSE)` 与
  `release_in_transaction` 能把 `executing/reserved` 收敛为 `succeeded/released`
  （仅测试脚本调用，11.2.2 给出字段来源、完整命令与限制）。

**这份装配没有证明**

- HTTP draft → publish → 目录可见（主干缺陷，5.1）；
- 用真实 capacity manifest / qualification report 发布随附 ASR 定义（两类证据均无
  产出物；5.4 的 `local-test-*` 只证明机制）；
- ~~受支持的引擎实例注册入口~~ ✅ `[已执行]` `2f96fb3`（#275）提供 `speechnexus-register-engine`，用法见 `docs/operations/register-engine.md`；本节保留 fixture 行仅为对照；
- node controller 注册、真实 readiness 回执、`preparing → ready` 代次转移；
- 公开 `/v1/audio/transcriptions` 的**成功响应**：请求能进到执行，但 file worker 在
  staging 登记后没有终态写者，API 轮询 120 秒截止，实测 504；私有 recipe 与 live
  admission 只在本 fixture 语义下跑通（人工字段映射见 11.2.2）；
- 504 后的自动租约释放/对账：本轮是手工测试脚本收敛的，不是产品行为；11.2.2 的
  `resources_released=true` 也只是测试断言，不是引擎显式 ACK；
- 最小权限对象身份（本轮隔离桶用了 root，9.4 已声明）；
- 预算/租约/fencing 转移、产品自动生成的成功停止证据、GPU 资源释放；
- 质量、时延、容量、长期稳定性；
- 生产单机或集群、TLS/鉴权/RBAC、跨主机/Linux/CUDA 资格；
- 任何“绿色 CI / 局部测试”之外的正式消费者接入。

**拓扑**：单机；无 node controller、无 planner、无 Scheduler/Job Worker、无工作台
消费者；PostgreSQL 是唯一权威，MinIO 只保存明确提交的输入/制品。真实音频与凭据
留在部署网络，不进入本文或仓库。本文使用的音频是公共 Silero 测试片段。

## 13. 清理

本机实跑结束后可删除隔离容器；`$SN_LOCAL_ROOT`（本机为 `.paperclip-runtime/del109/`）
未提交，包含一次性本机口令的 `api.env` / `gateway.env` / `owner.env` / `minio.env`、
原始 HTTP 输出与占位 JSON。`*.env` 已被 `.gitignore` 忽略，其余文件不受忽略保护：
不要 `git add -A`，提交前显式检查。

```bash
# 本轮未执行：保留现场以便复核第 9–11 节
docker rm -f del109-pg del109-minio
```

本任务自己启动的网关实例（58030 的早期副本）已在上一轮 Ctrl-C 停止；**第 11.2 节用的是
SM+PO 常驻的 `127.0.0.1:58030`**，本轮没有停止、重启或替换它，也没有操作
`del109-pg` / `del109-minio` / GB10 隧道 / `del109-asr` 容器。本任务的 API（58000）
在证据采集后已 Ctrl-C 停止。数据库和 MinIO 容器保留，以便第二个人复核第 9–11 节。
重新启动时重复 2.1；数据库卷未使用 named volume，容器删除后数据会丢失，这是本装配
刻意的隔离选择。

## 附录 A：本次运行的证据文件

原始输出保存在 `$SN_LOCAL_ROOT`（本机为 `.paperclip-runtime/del109/`，未提交）：

- `models-empty.headers` / `models-empty.json` / `models-empty-304.headers`
- `draft.headers` / `draft.json` / `admin-draft.json`
- `bundled-asr-draft.*`（原样随附定义 400）、`bundled-asr-seeded-publish.out`
  （测试播种后的 missing 原文）与 `bundled-asr-local-draft.*`（本地测试引用 201）
- `publish-1.out` / `publish-repeat.out` / `publish-restore.out`
- `admin-published.json` / `admin-missing-evidence.json` / `admin-unconfigured.json`
- `models-tts.headers` / `models-tts.json` / `models-tts-r2.headers` / `models-tts-r2.json`
- `writer-conflict.out`（第 8 节两种 writer 模式互斥的真实输出）
- `models/silero_vad.onnx`（SHA-256 与 pin 一致）
- `audio/silero-test.wav` / `audio/speech-12s.wav`
- `gateway-file-asr.json` / `controlled-asr-endpoint.json` / `gateway.env`
- `gateway-readyz.json` / `gateway-readyz.headers`
- `submit.response.json` / `staging.json` / `submit.replay.json`
- `submit.json`（fixture 生成的执行请求；含对象摘要，不含凭据）
- `transcription-r9.headers` / `transcription-r9.json`（公开请求 504 的原始响应）
- `gateway-status-c4.json`（同一请求的网关状态：`executing` + 185 B staging 引用）
- `stuck-c4-before.json` / `stuck-c4-after.json`（504 后的租约/配额事实与手工对账结果）
- `reconcile-c4-test.py` / `reconcile-c4-test.out`（仅测试用途的对账脚本与输出）
- `r10-preflight-poststop.out`（11.2.2 只读核对在清理后复跑的真实输出；此时应显示
  `succeeded` / `stop_evidence_rows=1`，不能作为再次调用前的判据）
- `r10-post-check.out`（11.2.2 调用后验收：stop evidence、lease、instance、quota）
- `r10-submit-timing.psql` / `r10-stop-evidence.psql`（`recorded/dispatched/terminated`
  时序与回填证据）
- `r10-request-bounds.txt`（r9 请求墙钟：文件 birth/mtime）
- `r10-gateway-access.txt`（同一 API 连接的 1 次 POST 200 + 458 次 GET 200）
- `r10-lock-owner.txt`（`gateway-lock/*.lock` 的进程级 `flock` 持有者）
- `gateway-status-c4-r10.json`（人工对账后的 `succeeded` 对照）
- `r11-patch-apply-check.txt`（`DEL-109.patch` 在 `c435908` 的仓库外 scratch tree 上的
  应用与逐文件摘要核对；scratch tree 路径与摘要见该文件）
- `public-r7-trace.out` / `public-r8-trace.out`（进程内诊断：`capacity_exhausted` /
  `reconciliation_required`；不是 HTTP 请求证据）

对应的可复现脚本：`scripts/dev-env/del109-local-gateway-fixture.py`。

这些文件只证明本机时点；交付给第二个人时，以本文命令和判定条件为准。
