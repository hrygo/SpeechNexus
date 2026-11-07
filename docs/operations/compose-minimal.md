---
title: "最小同步 ASR/TTS Compose 部署面"
status: active
version: "0.1.0"
date: 2026-10-28
---

# 最小同步 ASR/TTS Compose 部署面

本文给出从仓库根目录和新的本地数据卷出发，到 `api:8000/healthz`、`gateway:8010/healthz`
与两者 `/readyz` 返回 `200` 的步骤。它只装配同步语音请求所需的 PostgreSQL、MinIO、API、
执行网关和一次性引擎注册，不把其它未完成单元伪装成已交付。

## 仍不可用的能力

| 能力 | 当前结论 | 依赖 / 原因 |
| --- | --- | --- |
| 公开 TTS `POST /v1/audio/speech` | **本 Compose 未装配** | 同步 TTS 工厂已实现，但本配置只预置 gateway 私有 file-TTS 配置引用，未提供 API 配方及结算数据库配置；其他部署的受控实测见[窗口记录](../archive/audits/2026-11-03-controlled-tts-prepare.md) |
| 异步 Job 完整执行链 | **未贯通** | 本配置不启动 `job-dispatch-worker`；API 的创建和读取路由需要另配 `SPEECHNEXUS_JOB_WRITER_DATABASE_URL`；原子准入还需独立的 `SPEECHNEXUS_JOB_ADMISSION_DATABASE_URL` 与同库 admission 角色，未配置时创建返回 503（读取与取消仍可用）。派发入口已装配生产 planner 与结果发布器，但本配置既不启动它，也不提供真实引擎。当前范围见 [F-JOB](../prd.md#f-job) 和 [F-ASYNC](../prd.md#f-async) |
| 公开同步 ASR 成功响应 | **本部署未验证** | 文件 worker 已有成功终态写入；历史部署曾返回 `504 operation_timeout`，见[本机端到端记录](../archive/operations/local-end-to-end.md#112-公开-v1audiotranscriptions已执行504未成功)。不能将那次结果或组件实现推定为当前部署的公开链路验收 |
| 节点控制器、runtime delivery、asset maintenance | **未装配** | 本配置有意排除这些服务；不能以本 Compose 的探活结果证明完整部署与发布资格 |
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
COMPOSE=(docker compose)
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
`engine-register` 分别属于按需启用的 `migration` 和 `engine-registration` profile；
`binding-disable-worker` 属于 `binding-disable` profile，`admin` / `workbench` 属于
`web` profile，因此都不属于上面的基础集合。

默认网络固定使用 `172.28.0.0/24`，MinIO 地址为 `172.28.0.10`；启动前须核对该网段
不会与宿主机路由或其他容器网络冲突。若需使用其他网段，部署配置中的 Compose 子网、
MinIO 静态地址和两份 gateway 私有配置的对象存储 endpoint 必须一起调整。文件语音对象
适配器要求私有 IP 地址，不能直接改成 `minio` DNS 名称；API 使用的 Compose 服务名
`minio` 是另一条配置路径。详见[私有部署输入说明](../../deploy/private/README.md)。

## 2. 准备环境文件

以下命令生成随机本地口令并写入一个 **0600** 的环境文件；它不会把口令打印到终端。
只能对新建的 PostgreSQL/MinIO 数据卷执行一次。若 `deploy/compose.env` 已存在，先
确认它是本次部署的文件，不要覆盖其它运行态。命令会将已有的三个 `.runtime` 目录改为
`0700`，执行前须确认其归属和用途；生成中途失败时，`compose.env` 或目录可能已经创建，
下次执行前先检查并按实际状态处理。请使用非 root 用户，脚本会在复制环境文件后才检查 UID。

```bash
umask 077
if [ -e deploy/compose.env ] || [ -L deploy/compose.env ]; then
  printf '%s\n' 'deploy/compose.env 已存在，请先核对并单独处理。' >&2
  exit 1
fi
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
    "SPEECHNEXUS_GATEWAY_QUERY_ROLE_PASSWORD": secrets.token_urlsafe(32),
    "SPEECHNEXUS_EXECUTION_GATEWAY_COMPONENT_CREDENTIAL": secrets.token_hex(32),
}
value["SPEECHNEXUS_API_DATABASE_URL"] = (
    f"postgresql://sn_api:{value['SPEECHNEXUS_API_ROLE_PASSWORD']}@postgres:5432/speechnexus"
)
value["SPEECHNEXUS_GATEWAY_CONTROL_DATABASE_URL"] = (
    f"postgresql://sn_gateway:{value['SPEECHNEXUS_GATEWAY_ROLE_PASSWORD']}"
    "@postgres:5432/speechnexus"
)
value["SPEECHNEXUS_GATEWAY_CONTROL_QUERY_DATABASE_URL"] = (
    f"postgresql://sn_gateway_query:{value['SPEECHNEXUS_GATEWAY_QUERY_ROLE_PASSWORD']}"
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
先运行 `export SN_ROOT="$ROOT"`。编辑 `deploy/compose.env`，按实际部署核对以下配置：

- `SPEECHNEXUS_ENGINE_HEALTH_URL`：注册命令可访问的引擎 `/health` origin；
- `SPEECHNEXUS_NODE_ID` / `SPEECHNEXUS_GATEWAY_ID`：要与两份 gateway endpoint JSON
  以及 recipe 的 `runtime_plan.binding` 完全一致；
- `SPEECHNEXUS_ENGINE_INSTANCE_ID` / `SPEECHNEXUS_ENGINE_GENERATION`：注册使用的实例与代次；
  实例须与 recipe 的 `stage.instance_id` 和 ASR endpoint 一致，代次须与 ASR endpoint 一致；
- `SPEECHNEXUS_OBJECT_STORAGE_BUCKET` / `SPEECHNEXUS_TRANSCRIPTION_INPUT_BUCKET`：两份
  私有 JSON 的桶名必须同步。

生成脚本使用示例的 `POSTGRES_USER=speechnexus` 和 `POSTGRES_DB=speechnexus` 写入
各数据库连接地址。若在启动前更改数据库名，须同步更新 API、gateway、gateway 查询、
迁移及注册连接地址中的数据库名；更改 bootstrap 用户名时，须更新使用该身份的迁移和
注册地址，同时保留独立的运行角色。还须重新核对第 6 节的授权目标；只改这两个变量
不会自动改写已生成的连接地址。已有数据卷不能通过修改环境文件完成用户或数据库迁移。

`SPEECHNEXUS_REGISTRATION_DATABASE_URL` 与 `SPEECHNEXUS_MIGRATION_DATABASE_URL` 是
一次性 operator 身份；本机示例沿用 bootstrap owner。它们不会传给 `api` 或 `gateway`，
基础配置中 API 使用 `sn_api`，gateway 分别使用 `sn_gateway` 写入和 `sn_gateway_query`
只读查询。API 的 profile、binding、Job 等可选数据库连接默认为空；启用对应能力时须按
各自要求另行配置身份与授权，不能假定基础角色已覆盖。生产部署必须把迁移和注册连接
换成独立、受审计的 operator 凭据，不能复用运行角色。

## 3. 准备私有文件

以下步骤仅创建新的私有配置；任何目标已存在时停止，先核对归属和内容，不覆盖已有配置。

```bash
D=deploy/.runtime/private
for name in transcription-recipe.json gateway-file-asr.json gateway-file-asr-endpoint.json \
  gateway-file-tts.json gateway-file-tts-endpoint.json; do
  if [ -e "$D/$name" ] || [ -L "$D/$name" ]; then
    printf '目标已存在：%s\n' "$D/$name" >&2
    exit 1
  fi
done
cp deploy/private/transcription-recipe.example.json "$D/transcription-recipe.json"
cp deploy/private/gateway-file-asr.example.json "$D/gateway-file-asr.json"
cp deploy/private/gateway-file-asr-endpoint.example.json "$D/gateway-file-asr-endpoint.json"
cp deploy/private/gateway-file-tts.example.json "$D/gateway-file-tts.json"
cp deploy/private/gateway-file-tts-endpoint.example.json "$D/gateway-file-tts-endpoint.json"
chmod 600 "$D/transcription-recipe.json" "$D/gateway-file-asr.json" \
  "$D/gateway-file-asr-endpoint.json" "$D/gateway-file-tts.json" \
  "$D/gateway-file-tts-endpoint.json"
```

按[私有部署输入说明](../../deploy/private/README.md)替换全部合成字段。最少要逐项核对：

1. recipe 的 `binding_publication_id`、`binding_revision_id`、`manifest_id`、
   `policy_revision_id`、`stage.instance_id` 与 `runtime_plan.binding` 一致；
2. ASR endpoint 的 `target` 与 recipe 的 node/gateway/pool/instance/generation 一致；
3. TTS endpoint 的 `target` 与 TTS policy revision、instance/generation 一致；
   使用 `file_tts_v2`，`builtin_voice_binding` 的公开 id/revision、内部 revision id 来自
   同一权威声音版本，`native_speaker_id` 精确匹配 `tts_policy.voice`；旧 v1 不再接受。
   [升级规则](file-tts-runtime.md#私有配置格式)要求停止旧 worker 后激活，
   样例映射不提供声音资格；
4. gateway 两份配置里的 `objects.endpoint` 与 `bucket` 与实际 MinIO 一致；
5. endpoint 的地址、端口、served model 与 bearer token 是部署网络内的受控值。

把已批准的 Silero ONNX 普通文件放到私有目录，并用其真实摘要更新 ASR 配置。示例中的
全零摘要会拒绝启动：

```bash
if [ -e "$D/silero_vad.onnx" ] || [ -L "$D/silero_vad.onnx" ]; then
  printf '%s\n' '目标模型文件已存在，请先核对。' >&2
  exit 1
fi
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

上述检查只解析三个 JSON，并检查 recipe 的权限位；它不检查两份 endpoint JSON、
文件所有者、符号链接、摘要或配置间的身份一致性。`os.stat` 会跟随链接；API 读取
recipe 时另要求当前运行 UID 拥有的普通文件，且拒绝最终路径为符号链接。须结合上面的
逐项核对与实际运行身份验证，不能把这段检查通过当作配置已获批准或启动必然成功。

## 4. 启动数据库与对象存储

构建并启动 PostgreSQL、MinIO，然后执行一次桶初始化：

```bash
"${COMPOSE[@]}" --env-file deploy/compose.env -f deploy/compose.yaml \
  up -d --build postgres minio
"${COMPOSE[@]}" --env-file deploy/compose.env -f deploy/compose.yaml \
  run --rm minio-init
```

预期 `minio-init` 成功退出，并对每个不同桶名输出一行；默认两个桶名不同，输出如下：

```text
bucket ready: speechnexus-inputs (versioning enabled)
bucket ready: speechnexus-results (versioning enabled)
```

确认对象存储存活；以下地址使用默认宿主机端口。如果修改了环境文件中的
`SPEECHNEXUS_MINIO_S3_HOST_PORT`，须使用实际端口：

```bash
curl -fsS http://127.0.0.1:59000/minio/health/live
```

## 5. 迁移到唯一 head

迁移使用独立 DDL 身份，不能用 `sn_api` / `sn_gateway`。升级前须停止或隔离旧版本
的 publication 和 lifecycle 写者，直到迁移、授权与新版本切换完成；否则旧写者仍可能
绕过 head 或 disable 意图。`0025` 会校验并回填旧 0023 的 disable 事件；遇到历史
事实不一致或表结构异常时事务回滚，必须人工核查，不能删除历史或跳过校验：

```bash
"${COMPOSE[@]}" --env-file deploy/compose.env -f deploy/compose.yaml \
  --profile migration run --rm migrate
```

预期退出码为 `0`，容器内打印的迁移日志可能为空。核对版本表：

```bash
docker exec speechnexus-postgres-1 psql -U speechnexus -d speechnexus -Atc \
  'select version_num from speech.alembic_version'
```

上述容器名、用户和数据库名对应默认配置；若部署值不同，先核对实际名称再查询。
仓库当前迁移链的 head 为 `0039_public_job_progress`；实际部署必须以所用发布制品中的唯一
head 为准，查询结果须与之相符。不要把旧版 `0027_gateway_abort_intents` 当作当前 head，
也不要跳过检查或手工修改版本表。

## 6. 创建非 owner 运行角色与最小授权

以下脚本必须使用 **迁移 owner DSN** 运行；它创建 API、gateway writer、gateway
query 三个独立角色并调用仓库授权函数。此段只用于新建、独占的 SpeechNexus 数据库：
它会撤销数据库级 `PUBLIC TEMPORARY/CREATE`、`public` schema 的 CREATE 和大对象
写函数的 PUBLIC EXECUTE。已有共享数据库应先导出 ACL、核对其他角色依赖，再由部署
负责人评审等价最小授权；不要直接套用。应用启动只检查这些前置条件，不修改 ACL。
脚本使用 `autocommit=True`：每条已成功执行的撤权、角色创建或授权会立即提交；中途
失败不会整体回滚。失败后须先核对已创建的角色和实际 ACL，按保存的原状态及获准的
恢复步骤处理；直接重跑会因已有角色而停止，不能假设数据库仍处于执行前状态。
先在当前 shell 载入环境文件：

```bash
set -a
. deploy/compose.env
set +a
export SN_ADMIN_DSN="postgresql://${POSTGRES_USER}:${POSTGRES_PASSWORD}@127.0.0.1:${SPEECHNEXUS_POSTGRES_HOST_PORT:-55432}/${POSTGRES_DB}"

uv run python - <<'PY'
import os
from psycopg import sql
import psycopg
from speechnexus_backend.speech.infrastructure.asset_schema import asset_runtime_grants
from speechnexus_backend.speech.infrastructure.coordination_grants import (
    coordination_runtime_grants,
)
from speechnexus_backend.speech.infrastructure.gateway_query_grants import (
    gateway_query_grants, gateway_query_target_grants,
)
from speechnexus_backend.speech.infrastructure.job_cancellation_fence_schema import (
    gateway_cancellation_fence_grants,
)
from speechnexus_backend.speech.infrastructure.gateway_abort_intent_schema import (
    gateway_abort_grants, gateway_binding_barrier_grants,
)

schema = "speech"
roles = {
    "sn_api": os.environ["SPEECHNEXUS_API_ROLE_PASSWORD"],
    "sn_gateway": os.environ["SPEECHNEXUS_GATEWAY_ROLE_PASSWORD"],
    "sn_gateway_query": os.environ["SPEECHNEXUS_GATEWAY_QUERY_ROLE_PASSWORD"],
}
with psycopg.connect(os.environ["SN_ADMIN_DSN"], autocommit=True) as conn:
    # PostgreSQL 角色属于整个集群，即使数据库是专用的也要先核对。
    for role in roles:
        if conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone():
            raise SystemExit(f"role {role} already exists; inspect ownership before proceeding")
    database = conn.execute("SELECT current_database()").fetchone()[0]
    conn.execute(sql.SQL("REVOKE CREATE, TEMPORARY ON DATABASE {} FROM PUBLIC")
                 .format(sql.Identifier(database)))
    conn.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
    large_object_writers = (
        "lo_create", "lo_creat", "lo_from_bytea", "lo_import", "lo_export", "lo_unlink",
        "lo_put", "lo_truncate", "lo_truncate64", "lowrite",
    )
    for (signature,) in conn.execute(
        "SELECT p.oid::regprocedure::text FROM pg_proc p "
        "JOIN pg_namespace n ON n.oid=p.pronamespace "
        "WHERE n.nspname='pg_catalog' AND p.proname=ANY(%s)",
        (list(large_object_writers),),
    ).fetchall():
        # regprocedure is server-generated, including the complete argument types.
        conn.execute(sql.SQL("REVOKE EXECUTE ON FUNCTION {} FROM PUBLIC")
                     .format(sql.SQL(signature)))
    for role, password in roles.items():
        exists = conn.execute(
            "SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)
        ).fetchone()
        if exists is not None:
            raise SystemExit(f"role {role} appeared during setup; stop without changing it")
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
    for statement in gateway_query_grants(schema, "sn_gateway_query"):
        conn.execute(statement)
    for statement in gateway_cancellation_fence_grants(schema, "sn_gateway"):
        conn.execute(statement)
    for statement in gateway_query_target_grants(schema, "sn_gateway"):
        conn.execute(statement)
    for statement in (
        *gateway_abort_grants(schema, "sn_gateway"),
        *gateway_binding_barrier_grants(schema, "sn_gateway"),
    ):
        conn.execute(statement)

    # Transcription resolves the selected published binding and checks its
    # retirement through the runtime facts port.
    conn.execute(
        sql.SQL('GRANT SELECT ON {}.speech_publications, {}.speech_binding_revisions, '
                '{}.speech_publication_heads, {}.speech_publication_retirements TO {}')
        .format(
            sql.Identifier(schema),
            sql.Identifier(schema),
            sql.Identifier(schema),
            sql.Identifier(schema),
            sql.Identifier("sn_api"),
        )
    )
    print("roles ready: sn_api sn_gateway sn_gateway_query")
PY
```

若这里报 `role cannot be dropped because objects depend on it`、owner membership 或 schema
CREATE 权限，停止启动服务；不要用超级用户连接绕过检查。API 启动时还会再次核验
`sn_api` 不是数据库/表 owner，也不是可继承的 owner 成员。

### 已有部署的授权升级

上面的脚本只适用于新建、独占的数据库；已有部署不重跑它的撤权与建角色部分。应用新迁移
后，用同一个迁移 owner 连接对既有角色重放对应的授权函数即可，`GRANT` 是追加式操作，
重复执行不改变既有权限：

```bash
uv run python - <<'PY'
import os

import psycopg
from speechnexus_backend.speech.infrastructure.gateway_abort_intent_schema import (
    gateway_abort_grants,
)
from speechnexus_backend.speech.infrastructure.gateway_query_grants import (
    gateway_query_grants,
)

schema = "speech"
with psycopg.connect(os.environ["SN_ADMIN_DSN"], autocommit=True) as conn:
    for statement in gateway_abort_grants(schema, "sn_gateway"):
        conn.execute(statement)
    for statement in gateway_query_grants(schema, "sn_gateway_query"):
        conn.execute(statement)
print("gateway write and query grants refreshed")
PY
```

两份授权须同时重放。查询角色的列集由网关按精确集合校验，漏掉 `gateway_query_grants`
时控制面以 `invalid execution query reader authority` 拒绝启动；漏掉
`gateway_abort_grants` 新增的尝试列时启动继续，但持久取消接收能力被关闭
（fail-soft），网关不再接收新的取消。升级顺序：先迁移，再重放两份授权，最后启动
服务；启动日志出现上述拒绝时停止服务并核对授权，不带病运行。

Gateway query 身份仅取得六张执行恢复关系及安装身份表的必要列读取权，不得写产品或持久对象。
原生 `pg_settings` 的默认会话参数 UPDATE 与 SET/set_config 属于会话能力，不作为
产品写权；该例外不涵盖其他系统目录或额外授权。启动时核验登录身份、有效
身份及可继承/切换角色、PUBLIC、owner、DDL/DML、序列和特权函数能力；额外业务读权
也会拒绝。每次查询在单个 `REPEATABLE READ READ ONLY` 事务内核对完整原执行文档、
身份和规范摘要，再投影取消收据；合法删除后只报告 `document_state=redacted` 并保留
原摘要。原 GET 默认 `application/json` 保留旧封闭响应；显式发送
`Accept: application/vnd.speechnexus.execution-recovery.v1+json` 才返回带必需
`abort_recovery` 的版本化表示，正式客户端为 `query_abort_recovery()`。
旧表示不携带该观察；新表示的 `receipt=null` 只表示该次快照无
收据，`claimed` 仅表示一次外发认领已消耗，均不证明停止或预算释放。取消摘要包含
路径中的 `submit_intent_id` 和原请求体的五个字段；不能只对 body 求摘要。

启动时实际核对 writer/reader 的 primary、数据库、schema、cluster 和安装 UUID；之后
每个 reader 快照复核固定身份及非 standby，不依赖 writer 持续在线。物理复制出来且
保留全部身份的两个可写集群仍须由部署保证单一权威，不能把此检查称为 HA 拓扑验收。

部署前保存原 ACL 与私有配置。回退时先停止本次 gateway/worker，切回已验版本和
对应配置，再按保存的 ACL 恢复受影响的其他角色；不要通过给 reader 加写权、owner
身份或切回 writer 凭据绕过启动检查。此变更不修改既有执行或预算数据。

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
5 秒）。然后逐条验证公开探针；以下地址使用默认宿主机端口，如果修改了
`SPEECHNEXUS_API_HOST_PORT` 或 `SPEECHNEXUS_GATEWAY_HOST_PORT`，须替换为实际端口：

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
{"status":"ready","checks":[{"name":"control_postgresql","status":"ok","detail":null},{"name":"control_query_postgresql","status":"ok","detail":null},{"name":"gateway_ingress","status":"ok","detail":null}]}
http=200
```

`/readyz` 不调用真实模型，也不证明 binding、recipe、引擎或公开 ASR/TTS 业务成功。
它检查数据库连接的 `SELECT 1`、API 配置的对象存储健康 URL，以及 gateway 的 ingress
所有权和已要求的文件 worker 运行状态。对象存储健康检查不验证 bucket、版本化或读写凭据；
具体检查项随部署配置变化，本 Compose 为 gateway 配置了独立查询数据库连接，因此包含
`control_query_postgresql`。

## 8. 一次性注册引擎

只有目标引擎已经在 `SPEECHNEXUS_ENGINE_HEALTH_URL` 上返回 `200`，且已独立核对端点
属于目标实例和 generation 时才执行。健康响应本身不验证实例身份或模型。命令使用
环境文件中的 node、gateway、instance、generation，并核对前三者与 recipe；重复执行
相同注册不会新建实例，具体边界见[引擎注册](register-engine.md)：

```bash
"${COMPOSE[@]}" --env-file deploy/compose.env -f deploy/compose.yaml \
  --profile engine-registration run --rm engine-register
```

预期 stdout 是一个 JSON 文档，且：

```text
"outcome": "registered"
```

完全相同的重跑在健康检查通过、已有身份、owner 和 readiness 记录一致时返回
`"outcome": "already_ready"`；若 readiness 已过期，命令会用本次健康探测刷新它并写入
数据库。健康探测失败、存在停止意图或原证据不一致时仍可能失败。不要修改 generation 或
instance 来“绕过”冲突；先核对旧 head、owner 和 readiness 证据。

## 9. 失败时先看什么

| 现象 | 优先检查 |
| --- | --- |
| Compose 报 `required variable ... is missing a value` | `deploy/compose.env` 缺少对应键；不要用 shell 临时变量代替可复现配置文件 |
| API 启动日志含 `Synchronous transcription is unavailable; missing configuration` | 六个成组变量中至少一个为空；检查 env 文件是否被正确加载 |
| API 启动报 recipe digest / `invalid transcription recipe` | 重算 `recipe_digest`，并确保 recipe 是当前 UID 所有、`0600`、普通文件 |
| gateway 退出并报 `invalid gateway file ASR configuration` | 检查 endpoint/ recipe 身份、VAD 文件摘要、桶名、endpoint 与对象凭据是否成组提供 |
| gateway 报 `cannot initialize the pinned CPU VAD runtime` | Python 镜像缺少固定 VAD 依赖或 ONNX 文件不是批准的 Silero 模型；不要换成任意 ONNX |
| `/readyz` 为 `503`，`object_storage` 为 `failed` | 检查 API 配置的 MinIO 健康 URL 和网络连通性；桶、版本化与凭据须另行验证 |
| `/readyz` 为 `503`，`postgresql` 为 `failed` | PostgreSQL 未 healthy、角色口令/DNS 错误或实例不在同一 Compose 网络 |
| `minio-init` 报 `bucket versioning was not enabled` | 检查 MinIO 健康、root 凭据和 bucket policy；不能删除版本化要求后继续 |
| `engine-register` 报 `engine_unreachable` | 从 Compose 网络探测 `SPEECHNEXUS_ENGINE_HEALTH_URL`；该 URL 必须能从容器访问，不能只从宿主机访问 |
| `engine-register` 报 `invalid_recipe` | recipe 结构、摘要、stage/instance 或 binding plan 不一致 |
| `engine-register` 报 `pool_owner_conflict` / `previous_generation_active` | 已有活动 owner 或旧 generation 仍 ready；先核对/停止旧实例，不能修改 token 假装接管 |
| 容器已启动但 API 转写返回 `504 operation_timeout` | 按原身份检查准入、网关、worker 和终态事实；历史超时原因不能直接套用到新运行，也不要用增加超时或重提新执行掩盖 |
| 公开 `/v1/audio/speech` 不可用 | 本 Compose 没有提供 API 侧同步 TTS 所需配方及结算数据库配置；另见[同步 TTS 装配要求](../components/synchronous-speech.md) |

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
取证，不能用本文件的 `/healthz` 成功替代。

## 12. 按需启用 binding 停用 worker

基础 Compose 拓扑不启动该 worker。启用 `binding-disable` profile 后，它才处理持久化的
停用意图。停用回执只确认选择边界和持久意图；即使 worker 离线，清理状态仍报告
`pending_or_unknown`。API 或 gateway 就绪不证明停用已处理，也不证明容量已释放。

调整运行授权前，使用独立迁移身份升级到当前唯一迁移 head；应从实际发布版本确定并核对
head，不套用其他发布版本的值。在 `sn_api` 和 `sn_gateway` 所在 PostgreSQL 数据库中
准备两个不同的非 owner LOGIN 角色，此处为 `sn_disable_scan` 和 `sn_disable_settle`。
worker 要求 PostgreSQL 16 或更新版本，并在启动时拒绝可切换其他角色、角色管理权限及
预定义 `pg_*` 角色能力；连接成功本身不代表权限检查通过。
两个角色均不得继承其他运行角色或 owner 角色、拥有应用表、在 schema 中创建对象，或获得
超出各自端口声明的权限。扫描角色只授予 `disable_coordinator_grants("speech", "sn_disable_scan")`；
结算角色只授予 `disable_settlement_grants("speech", "sn_disable_settle")`。worker 会检查登录
和有效身份，比较数据库名、服务端地址及端口；部署仍须独立确认两条连接访问同一权威实例，
不能仅凭这些字段相同排除代理或复制环境中的多权威问题。

通过部署批准的秘密管理流程创建这两个角色。以下可选授权脚本使用第 6 节准备的 owner DSN，
不创建角色或处理口令。基础 gateway 授权已在第 6 节安装；核对发布版本的授权函数后，
仅对目标部署数据库执行。脚本不创建或授权生命周期写入角色。

```bash
# SN_ADMIN_DSN 通过第 6 节所述的获准秘密渠道提供。
uv run python - <<'PYROLE'
import os
import psycopg
from speechnexus_backend.speech.infrastructure.admission_postgres import binding_disable_admission_grants
from speechnexus_backend.speech.infrastructure.binding_disable_targets_schema import (
    disable_coordinator_grants, disable_settlement_grants,
)
with psycopg.connect(os.environ["SN_ADMIN_DSN"]) as conn:
    for grant in (
        *disable_coordinator_grants("speech", "sn_disable_scan"),
        *disable_settlement_grants("speech", "sn_disable_settle"),
        *binding_disable_admission_grants("speech", "sn_api"),
    ):
        conn.execute(grant)
print("disable worker roles and existing runtime port grants installed")
PYROLE
```

如果启用生命周期 API，须使用 `binding_lifecycle_grants` 单独配置其既有非 owner 身份，
并设置 `SPEECHNEXUS_BINDING_LIFECYCLE_DATABASE_URL`；不要向上述两个新角色授予生命周期
写权限。扫描和结算 DSN 分别通过部署私有配置设置为 `SPEECHNEXUS_DISABLE_DATABASE_URL`
和 `SPEECHNEXUS_DISABLE_SETTLEMENT_DATABASE_URL`，必须对应不同角色和同一数据库。worker
使用 gateway 已配置的 gateway/node 标识与组件凭据。

部署授权和新 gateway 镜像后，启用 worker：

```bash
"${COMPOSE[@]}" --env-file deploy/compose.env -f deploy/compose.yaml \
  --profile binding-disable up -d --build gateway binding-disable-worker
"${COMPOSE[@]}" --env-file deploy/compose.env -f deploy/compose.yaml \
  --profile binding-disable ps gateway binding-disable-worker
```

`network_mode: service:gateway` 加入 gateway 的完整网络命名空间；worker 在其中使用
`http://127.0.0.1:8010`。这不形成独立网络隔离边界；远程 gateway 须使用单独认证的 TLS
部署。worker 退出或反复重启需要运维处理。当前 worker 没有 readiness 或心跳端点；容器
运行中和 gateway `/readyz` 通过均不证明处理成功。

worker 分页扫描持久意图；首次外发前先确认目标的取消意图已提交，提交结果不明时本轮
不外发。对已认领过的目标先按原身份查询，查询成功后再以同一身份发送
幂等取消；查询失败时该轮不会发送取消，而会在后续轮次重访。租约结算结果未知时查询原
租约，只有事实显示已释放才计入结算；无法确认的目标
保留待处理并在后续轮次重访。日志中的 `attempted`、`queried`、`unavailable`、`settled`
和 `pending` 是单轮计数，不是全局清理完成证明。
其中 `attempted` 和 `queried` 分别在发起取消和查询前递增，不表示请求成功；
`settled` 只计入本轮释放后确认已释放的租约，开始时已释放的租约不计入。
`pending` 统计本轮未能确认释放的租约，不能作为尚待取消的执行总数。

每个 worker 只捕获、取消和结算其配置的 gateway/node 范围内的目标；其他节点需要相应
worker 或独立恢复流程。单次捕获是有界批次，不证明已枚举全部目标或所有执行已停止。

worker 的命名空间绑定具体 gateway 容器。替换 gateway 后，待其健康再显式重建 worker，
并核对实际网络命名空间及扫描是否恢复。Compose 的 `depends_on.restart` 只处理显式 Compose
更新；daemon 重启和外部重建须另行监管。回退时保留持久意图账本：停止 worker 只会暂停
清理，不会撤销停用或产生停止证据。已有 unknown 执行继续占用容量，直到既有证据规则允许释放。
