---
title: "最小同步 ASR/TTS Compose 部署面"
status: active
version: "0.1.0"
date: 2026-10-28
---

# 最小同步 ASR/TTS Compose 部署面

本文给出从空目录到 `api:8000/healthz`、`gateway:8010/healthz` 与两者 `/readyz`
返回 `200` 的可执行步骤。它只装配同步语音请求所需的 PostgreSQL、MinIO、API、
执行网关和一次性引擎注册，不把其它未完成单元伪装成已交付。

## 仍不可用的能力

| 能力 | 当前结论 | 依赖 / 原因 |
| --- | --- | --- |
| 公开 TTS `POST /v1/audio/speech` | **不可用** | 等待 **DEL-122** 的公开 TTS 装配；本卡只预置 gateway 私有 file-TTS 配置引用 |
| 异步 Job 提交、派发与结果 | **不可用** | G5 未解决（[#18](https://github.com/hrygo/SpeechNexus/issues/18)）；本卡有意不启动 `job-dispatch-worker` |
| 公开同步 ASR 成功响应 | **尚不可用** | 链路可装配，但成功终态没有生产写者，已知会等待到 `504 operation_timeout`；见 **DEL-109** 与[本机端到端记录](local-end-to-end.md#112-公开-v1audiotranscriptions已执行504未成功) |
| 节点控制器、runtime delivery、asset maintenance | **未装配** | 本卡按 G3 降级范围有意排除；不要用本 Compose 宣称 G5 完成 |
| 真实模型质量、容量、GPU、物理多节点资格 | **未证明** | Compose 启动、健康检查和合成配置都不能替代模型/设备/拓扑验收 |

因此本文件的“通过”只表示 **最小单元已装配并可探活**，不表示公开 ASR/TTS 业务已可用。

## 0. 构建前提

需要 Docker Engine 23 或更新版本、Compose v2，以及可用的 BuildKit/Buildx：

```bash
docker version
docker compose version
docker buildx version
```

本目录的 `--build` 使用 Dockerfile 1.18 frontend 和 BuildKit cache mount；缺少 Buildx
时 `up --build` 会在解析 Dockerfile 前失败。先修好 Docker Desktop 的 Buildx 插件或
Linux 的 `docker-buildx-plugin`，不要改用关闭 BuildKit 的构建来绕过。

## 1. 固定服务集合

在仓库根目录执行：

```bash
if docker compose version >/dev/null 2>&1; then
  COMPOSE=(docker compose)
else
  COMPOSE=(docker-compose)
fi
"${COMPOSE[@]}" --env-file deploy/compose.env.example \
  -f deploy/compose.yaml config --services
```

预期输出（顺序可不同）：

```text
minio
minio-init
postgres
gateway
api
```

`deploy/compose.yaml` 顶部的注释明确排除四项：`node-controller`、
`runtime-delivery-worker`、`job-dispatch-worker`、`asset-maintain`。`migrate` 和
`engine-register` 是 opt-in profile，`admin` / `workbench` 只在 `web` profile 中，
因此都不属于上面的基础集合。

## 2. 准备环境文件

以下命令生成随机本地口令并写入一个 **0600** 的环境文件；它不会把口令打印到终端。
只能对新建的 PostgreSQL/MinIO 数据卷执行一次。若 `deploy/compose.env` 已存在，先
确认它是本次部署的文件，不要覆盖其它运行态。

```bash
cp deploy/compose.env.example deploy/compose.env
mkdir -p deploy/.runtime/private deploy/.runtime/gateway-locks deploy/.runtime/gateway-spool
chmod 700 deploy/.runtime/private deploy/.runtime/gateway-locks deploy/.runtime/gateway-spool

ROOT=$(pwd)
export SN_ROOT="$ROOT"
python3 - <<'PY'
import os
import re
import secrets
from pathlib import Path

root = Path(os.environ["SN_ROOT"])
path = root / "deploy/compose.env"
text = path.read_text()
value = {
    "POSTGRES_PASSWORD": secrets.token_urlsafe(32),
    "MINIO_ROOT_PASSWORD": secrets.token_urlsafe(32),
    "SPEECHNEXUS_API_ROLE_PASSWORD": secrets.token_urlsafe(32),
    "SPEECHNEXUS_GATEWAY_ROLE_PASSWORD": secrets.token_urlsafe(32),
    "SPEECHNEXUS_EXECUTION_GATEWAY_COMPONENT_CREDENTIAL": secrets.token_hex(32),
}
value["SPEECHNEXUS_API_DATABASE_URL"] = (
    f"postgresql://sn_api:{value['SPEECHNEXUS_API_ROLE_PASSWORD']}@postgres:5432/speechnexus"
)
value["SPEECHNEXUS_GATEWAY_CONTROL_DATABASE_URL"] = (
    f"postgresql://sn_gateway:{value['SPEECHNEXUS_GATEWAY_ROLE_PASSWORD']}"
    "@postgres:5432/speechnexus"
)
owner_dsn = (
    f"postgresql://speechnexus:{value['POSTGRES_PASSWORD']}@postgres:5432/speechnexus"
)
value["SPEECHNEXUS_REGISTRATION_DATABASE_URL"] = owner_dsn
value["SPEECHNEXUS_MIGRATION_DATABASE_URL"] = owner_dsn
value["SPEECHNEXUS_RUNTIME_UID"] = str(os.getuid())
value["SPEECHNEXUS_RUNTIME_GID"] = str(os.getgid())
value["SPEECHNEXUS_PRIVATE_CONFIG_DIR"] = str(root / "deploy/.runtime/private")
value["SPEECHNEXUS_GATEWAY_LOCK_DIR"] = str(root / "deploy/.runtime/gateway-locks")
value["SPEECHNEXUS_GATEWAY_SPOOL_DIR"] = str(root / "deploy/.runtime/gateway-spool")

if os.getuid() == 0:
    raise SystemExit("do not run the runtime containers as root")
for key, item in value.items():
    pattern = rf"(?m)^{re.escape(key)}=.*$"
    if re.search(pattern, text) is None:
        raise SystemExit(f"missing {key} in deploy/compose.env")
    text = re.sub(pattern, f"{key}={item}", text, count=1)
path.write_text(text)
path.chmod(0o600)
print("compose.env written with generated local credentials (values not printed)")
PY
```

上面的 Python 片段必须通过环境变量 `SN_ROOT` 取得仓库绝对路径；若直接手工执行，
先运行 `export SN_ROOT="$ROOT"`。编辑 `deploy/compose.env`，把以下三项改成实际部署值：

- `SPEECHNEXUS_ENGINE_HEALTH_URL`：注册命令可访问的引擎 `/health` origin；
- `SPEECHNEXUS_NODE_ID` / `SPEECHNEXUS_GATEWAY_ID`：要与两份 gateway endpoint JSON
  以及 recipe 的 `runtime_plan.binding` 完全一致；
- `SPEECHNEXUS_OBJECT_STORAGE_BUCKET` / `SPEECHNEXUS_TRANSCRIPTION_INPUT_BUCKET`：两份
  私有 JSON 的桶名必须同步。

`SPEECHNEXUS_REGISTRATION_DATABASE_URL` 与 `SPEECHNEXUS_MIGRATION_DATABASE_URL` 是
一次性 operator 身份；本机示例沿用 bootstrap owner。它们不会传给 `api` 或 `gateway`，
后者只使用 `sn_api` / `sn_gateway`。生产部署必须把这两项换成独立、受审计的迁移/注册
operator 凭据，不能复用运行角色。

## 3. 准备私有文件

复制示例并使用最终运行名：

```bash
D=deploy/.runtime/private
cp deploy/private/transcription-recipe.example.json "$D/transcription-recipe.json"
cp deploy/private/gateway-file-asr.example.json "$D/gateway-file-asr.json"
cp deploy/private/gateway-file-asr-endpoint.example.json "$D/gateway-file-asr-endpoint.json"
cp deploy/private/gateway-file-tts.example.json "$D/gateway-file-tts.json"
cp deploy/private/gateway-file-tts-endpoint.example.json "$D/gateway-file-tts-endpoint.json"
chmod 600 "$D"/*.json
```

按[私有部署输入说明](../../deploy/private/README.md)替换全部合成字段。最少要逐项核对：

1. recipe 的 `binding_publication_id`、`binding_revision_id`、`manifest_id`、
   `policy_revision_id`、`stage.instance_id` 与 `runtime_plan.binding` 一致；
2. ASR endpoint 的 `target` 与 recipe 的 node/gateway/pool/instance/generation 一致；
3. TTS endpoint 的 `target` 与 TTS policy revision、instance/generation 一致；
4. gateway 两份配置里的 `objects.endpoint` 与 `bucket` 与实际 MinIO 一致；
5. endpoint 的地址、端口、served model 与 bearer token 是部署网络内的受控值。

把已批准的 Silero ONNX 普通文件放到私有目录，并用其真实摘要更新 ASR 配置。示例中的
全零摘要会拒绝启动：

```bash
cp /approved/private/silero_vad.onnx "$D/silero_vad.onnx"
chmod 600 "$D/silero_vad.onnx"
python3 - <<'PY'
import hashlib
import json
from pathlib import Path

root = Path("deploy/.runtime/private")
model = root / "silero_vad.onnx"
config_path = root / "gateway-file-asr.json"
config = json.loads(config_path.read_text())
config["vad_model_sha256"] = hashlib.sha256(model.read_bytes()).hexdigest()
config_path.write_text(json.dumps(config, indent=2) + "\n")
config_path.chmod(0o600)
print("vad_model_sha256:", config["vad_model_sha256"])
PY
```

recipe 修改后必须重算 RFC 8785 摘要，否则 API 启动时报配置摘要不符：

```bash
uv run python - <<'PY'
import hashlib
import json
from pathlib import Path

import rfc8785

path = Path("deploy/.runtime/private/transcription-recipe.json")
document = json.loads(path.read_text())
document.pop("recipe_digest", None)
digest = "sha256:" + hashlib.sha256(rfc8785.dumps(document)).hexdigest()
original = json.loads(path.read_text())
original["recipe_digest"] = digest
path.write_text(json.dumps(original, indent=2) + "\n")
path.chmod(0o600)
print("recipe_digest:", digest)
PY
```

至少做一次语法/权限检查：

```bash
python3 -m json.tool "$D/transcription-recipe.json" >/dev/null
python3 -m json.tool "$D/gateway-file-asr.json" >/dev/null
python3 -m json.tool "$D/gateway-file-tts.json" >/dev/null
python3 - "$D/transcription-recipe.json" <<'PY'
import os
import stat
import sys

path = sys.argv[1]
mode = stat.S_IMODE(os.stat(path).st_mode)
if mode != 0o600:
    raise SystemExit(f"{path} must be mode 0600, got {mode:04o}")
print(f"private mode ok: {path} 0600")
PY
```

## 4. 启动数据库与对象存储

构建并启动 PostgreSQL、MinIO，然后执行一次桶初始化：

```bash
"${COMPOSE[@]}" --env-file deploy/compose.env -f deploy/compose.yaml \
  up -d --build postgres minio
"${COMPOSE[@]}" --env-file deploy/compose.env -f deploy/compose.yaml \
  run --rm minio-init
```

预期 `minio-init` 成功退出并输出两行（桶名按环境文件而定）：

```text
bucket ready: speechnexus-inputs (versioning enabled)
bucket ready: speechnexus-results (versioning enabled)
```

确认对象存储存活：

```bash
curl -fsS http://127.0.0.1:59000/minio/health/live
```

## 5. 迁移到唯一 head

迁移使用独立 DDL 身份，不能用 `sn_api` / `sn_gateway`：

```bash
"${COMPOSE[@]}" --env-file deploy/compose.env -f deploy/compose.yaml \
  --profile migration run --rm migrate
```

预期退出码为 `0`，容器内打印的迁移日志可能为空。核对版本表：

```bash
docker exec speechnexus-postgres-1 psql -U speechnexus -d speechnexus -Atc \
  'select version_num from speech.alembic_version'
```

本候选的预期值为 `0021_publication_evidence`。若容器名不同，先用
`"${COMPOSE[@]}" ... ps --format json` 或 `docker ps` 查实际名称；不要跳过或
手工修改版本表。

## 6. 创建非 owner 运行角色与最小授权

以下脚本必须使用 **迁移 owner DSN** 运行；它创建两个独立角色并调用仓库中声明的
授权函数，不手抄权限 SQL。先在当前 shell 载入环境文件：

```bash
set -a
. deploy/compose.env
set +a
export SN_ADMIN_DSN="postgresql://${POSTGRES_USER}:${POSTGRES_PASSWORD}@127.0.0.1:55432/${POSTGRES_DB}"

uv run python - <<'PY'
import os
from psycopg import sql
import psycopg
from speechnexus_backend.speech.infrastructure.asset_schema import asset_runtime_grants
from speechnexus_backend.speech.infrastructure.coordination_grants import (
    coordination_runtime_grants,
)

schema = "speech"
roles = {
    "sn_api": os.environ["SPEECHNEXUS_API_ROLE_PASSWORD"],
    "sn_gateway": os.environ["SPEECHNEXUS_GATEWAY_ROLE_PASSWORD"],
}
with psycopg.connect(os.environ["SN_ADMIN_DSN"], autocommit=True) as conn:
    for role, password in roles.items():
        exists = conn.execute(
            "SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)
        ).fetchone()
        if exists is None:
            conn.execute(
                sql.SQL(
                    "CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT"
                ).format(sql.Identifier(role))
            )
        conn.execute(
            sql.SQL("ALTER ROLE {} PASSWORD {}").format(
                sql.Identifier(role), sql.Literal(password)
            )
        )
        memberships = conn.execute(
            "SELECT 1 FROM pg_auth_members m JOIN pg_roles r ON r.oid = m.member "
            "WHERE r.rolname = %s LIMIT 1",
            (role,),
        ).fetchone()
        forbidden = conn.execute(
            "SELECT rolsuper OR rolcreatedb OR rolcreaterole FROM pg_roles "
            "WHERE rolname = %s",
            (role,),
        ).fetchone()
        if memberships is not None or forbidden is None or any(forbidden):
            raise SystemExit(f"role {role} is not a fresh non-owner runtime role")

    for statement in coordination_runtime_grants(
        schema, speech_role="sn_api", gateway_role="sn_gateway"
    ):
        conn.execute(statement)
    for statement in asset_runtime_grants(schema, "sn_api"):
        conn.execute(statement)

    # Transcription resolves the current published binding through the same
    # runtime facts port. Grant only the two read relations it currently uses.
    conn.execute(
        sql.SQL('GRANT SELECT ON {}.speech_publications, {}.speech_binding_revisions TO {}')
        .format(
            sql.Identifier(schema),
            sql.Identifier(schema),
            sql.Identifier("sn_api"),
        )
    )
    print("roles ready: sn_api sn_gateway")
PY
```

若这里报 `role cannot be dropped because objects depend on it`、owner membership 或 schema
CREATE 权限，停止启动服务；不要用超级用户连接绕过检查。API 启动时还会再次核验
`sn_api` 不是数据库/表 owner，也不是可继承的 owner 成员。

## 7. 启动 API 与 gateway

```bash
"${COMPOSE[@]}" --env-file deploy/compose.env -f deploy/compose.yaml \
  up -d --build api gateway
```

Compose 会让 `api` 等待 `gateway` healthy，并让两者等待 `minio-init` 成功。检查容器：

```bash
"${COMPOSE[@]}" --env-file deploy/compose.env -f deploy/compose.yaml ps api gateway
```

预期两个容器均为 `Up` 且状态包含 `healthy`（可重复执行 `docker ps`，健康检查间隔为
5 秒）。然后逐条验证公开探针：

```bash
curl -sS -w '\nhttp=%{http_code}\n' http://127.0.0.1:8000/healthz
curl -sS -w '\nhttp=%{http_code}\n' http://127.0.0.1:8000/readyz
curl -sS -w '\nhttp=%{http_code}\n' http://127.0.0.1:8010/healthz
curl -sS -w '\nhttp=%{http_code}\n' http://127.0.0.1:8010/readyz
```

预期 API：

```json
{"status":"ok","service":"speechnexus-api","version":"0.1.0.dev0"}
http=200
```

```json
{"status":"ready","checks":[{"name":"postgresql","status":"ok","detail":null},{"name":"object_storage","status":"ok","detail":null}]}
http=200
```

预期 gateway：

```json
{"status":"ok","service":"speechnexus-execution-gateway","version":"0.1.0.dev0"}
http=200
```

```json
{"status":"ready","checks":[{"name":"control_postgresql","status":"ok","detail":null},{"name":"gateway_ingress","status":"ok","detail":null}]}
http=200
```

`/readyz` 不调用真实模型，也不证明 binding、recipe、引擎或公开 ASR/TTS 业务成功。
它只证明该进程、数据库连接、对象存储（API）和本进程 ingress/文件 worker（gateway）已就绪。

## 8. 一次性注册引擎

只有目标引擎已经在 `SPEECHNEXUS_ENGINE_HEALTH_URL` 上返回 `200` 时才执行。命令固定使用
环境文件中的 node、gateway、instance、generation，因此重复执行不会新建实例：

```bash
"${COMPOSE[@]}" --env-file deploy/compose.env -f deploy/compose.yaml \
  --profile engine-registration run --rm engine-register
```

预期 stdout 是一个 JSON 文档，且：

```text
"outcome": "registered"
```

完全相同的重跑应返回 `"outcome": "already_ready"`。不要修改 generation 或 instance
来“绕过”冲突；先核对旧 head、owner 和 readiness 证据。

## 9. 失败时先看什么

| 现象 | 优先检查 |
| --- | --- |
| Compose 报 `required variable ... is missing a value` | `deploy/compose.env` 缺少对应键；不要用 shell 临时变量代替可复现配置文件 |
| API 启动日志含 `Synchronous transcription is unavailable; missing configuration` | 六个成组变量中至少一个为空；检查 env 文件是否被正确加载 |
| API 启动报 recipe digest / `invalid transcription recipe` | 重算 `recipe_digest`，并确保 recipe 是当前 UID 所有、`0600`、普通文件 |
| gateway 退出并报 `invalid gateway file ASR configuration` | 检查 endpoint/ recipe 身份、VAD 文件摘要、桶名、endpoint 与对象凭据是否成组提供 |
| gateway 报 `cannot initialize the pinned CPU VAD runtime` | Python 镜像缺少固定 VAD 依赖或 ONNX 文件不是批准的 Silero 模型；不要换成任意 ONNX |
| `/readyz` 为 `503`，`object_storage` 为 `failed` | MinIO 未 healthy、桶未初始化或 API 的 endpoint/凭据错误 |
| `/readyz` 为 `503`，`postgresql` 为 `failed` | PostgreSQL 未 healthy、角色口令/DNS 错误或实例不在同一 Compose 网络 |
| `minio-init` 报 `bucket versioning was not enabled` | 检查 MinIO 健康、root 凭据和 bucket policy；不能删除版本化要求后继续 |
| `engine-register` 报 `engine_unreachable` | 从 Compose 网络探测 `SPEECHNEXUS_ENGINE_HEALTH_URL`；该 URL 必须能从容器访问，不能只从宿主机访问 |
| `engine-register` 报 `invalid_recipe` | recipe 结构、摘要、stage/instance 或 binding plan 不一致 |
| `engine-register` 报 `pool_owner_conflict` / `previous_generation_active` | 已有活动 owner 或旧 generation 仍 ready；先核对/停止旧实例，不能修改 token 假装接管 |
| 容器已启动但 API 转写返回 `504 operation_timeout` | 已知 DEL-109 终态缺口；不要用增加超时或重提新执行掩盖 |
| 公开 `/v1/audio/speech` 不可用 | 当前合同仍是 DEL-122 之前的装配；本卡不提供公开 TTS 成功路径 |

## 10. 停止与清理边界

```bash
"${COMPOSE[@]}" --env-file deploy/compose.env -f deploy/compose.yaml down
```

`down` 不删除命名卷 `postgres-data` / `minio-data`；不要用 `down -v` 清理真实执行
证据或未完成的对账事实。私有文件和 env 文件也不得提交到仓库。停止容器不等于释放
已准入的执行/租约或停止引擎；按[对账恢复](reconciliation-recovery.md)和具体实例
停止流程取得证据后再归还预算。

## 11. 验证边界

本 runbook 的本地验证只覆盖：Compose 解析、服务集合、私有配置引用、迁移入口、
非 owner 角色连接、探针响应和一次性注册命令。它不覆盖：真实引擎 ASR、公开 TTS、
异步 Job、G5 物理拓扑、模型质量、GPU 容量、真实内容网络或最终 Gate。把这些分别
取证，不能用本文件的 `/health` 成功替代。
