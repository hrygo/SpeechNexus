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
| 公开 TTS `POST /v1/audio/speech` | **已装配，未在本机验证** | 配方样例、`SPEECHNEXUS_SPEECH_RECIPE_FILE`、`SPEECHNEXUS_SPEECH_DEFAULT_LANGUAGE` 与独立结算角色 `sn_speech_settlement` 都在本部署面内（§3、§6），API 侧运行时授权由 `speech_synthesis_reader_grants` 为 `sn_api` 预置。装配不等于可用：仍需真实引擎、已发布且合格的内置声音版本与一次 `200` 实测，本机（macOS，无 GPU）无法提供那次窗口。其他部署的受控实测见[窗口记录](../archive/audits/2026-11-03-controlled-tts-prepare.md) |
| 异步 Job 完整执行链 | **未贯通** | 默认 `up` 不启动 `job-dispatch-worker`；`job-dispatch` profile 可按需启动该派发进程，但需要单独的 worker DSN（`SPEECHNEXUS_JOB_DISPATCH_DATABASE_URL`，示例中默认为空，空值在领取租约前 fail-closed）与真实引擎。派发车道限定为参考部署装配的 `asr`、`tts`；`SPEECHNEXUS_JOB_DISPATCH_KINDS` 写其它 kind 时启动闸门点名拒绝，不会认领该车道的 Job（要启用别的车道需先装配 gateway 车道，见 [§6](#异步-job-运行角色与授权)）。API 的创建和读取路由需要另配 `SPEECHNEXUS_JOB_WRITER_DATABASE_URL`；原子准入还需独立的 `SPEECHNEXUS_JOB_ADMISSION_DATABASE_URL` 与同库 admission 角色，未配置时创建返回 503（读取与取消仍可用）。派发入口已装配生产 planner 与结果发布器，容器 entrypoint 是 `deploy/job-startup-gate.py`：迁移 head、派发角色写入列、Artifact 桶与 gateway staging 结果桶缺失或未版本化时在启动期点名退出，不会先领取 Job；三个 Job 角色由 [§6 的异步 Job 运行角色](#异步-job-运行角色与授权) 预置。本配置不提供真实引擎。当前范围见 [F-JOB](../prd.md#f-job) 和 [F-ASYNC](../prd.md#f-async) |
| 公开同步 ASR 成功响应 | **已在该部署形态上取得真实 `200`（`DEL-540`，2026-11-18，候选 head `19c7f09a`）；成功后由 ASR 结算 worker 自动释放** | 装配：本文件的 `api`、`gateway`、私有 `transcription-recipe.json`/`gateway-file-asr.json` 与 §6 的 `sn_api` 转写授权组成同步 ASR 输入面；网关文件 worker 由 `execution-gateway/src/speechnexus_gateway/file_asr_runtime.py:117` 构造，只在拿到引擎终态事实时写成功终态（`execution-gateway/src/speechnexus_gateway/infrastructure/file_execution_worker.py:380` 调用 `execution-gateway/src/speechnexus_gateway/application/execution_completion.py:55` 的 `record_stop(SUCCEEDED)`）。实现：成功终态路径有 `tests/integration/test_gateway_file_worker.py` 与 `tests/unit/gateway/test_execution_completion.py`。**实测**：按本文件 §0–§7 精确构建并起栈的参考部署形态（七个镜像均由该 head 本地构建，引擎为同主机外部托管的 `del109-asr`，不是由本 Compose 启动的引擎）上，公开 `POST /v1/audio/transcriptions` 返回 `200 OK`（`content-length: 167`、`time_total=0.956069 s`、`x-request-id=b2a34de2…`），引擎侧同窗口一次 `200 OK`；请求、响应、标识与执行终态见 [MVP 判定表第 7 行](../plans/mvp-release-readiness.md)。**复制这次 `200` 的前提**：本 Compose 不提供真实引擎与已注册实例，须自备同机外部托管的引擎并按 §6 注册实例。历史本机装配在 `c435908` 上的 `504 operation_timeout` 是终态写入者补齐（DEL-115，PR #284）之前的记录，见[本机端到端记录](../archive/operations/local-end-to-end.md#112-公开-v1audiotranscriptions已执行504未成功)。**结算**：成功后由 ASR 结算 worker 在准入事务内写下的 `speech_asr_settlement` root 上自动释放租约（`lease_state=released`、`budget_released_at_us` 非空、实例 `held_slots` 与工作区 `held_attempts` 回到 0），无需任何命令；释放前单槽仍被占用。升级到含本修复的版本必须配置 `SPEECHNEXUS_SPEECH_SETTLEMENT_DATABASE_URL` 并跑完 `0057_asr_settlement` 迁移，否则同步 ASR 返回 `503 backend_unavailable` 并在启动日志点名该键（没有「服务但不结算」的开关）。**实测**：`DEL-543`（2026-11-18，MegaCube GB10，`del541` 参考部署形态，引擎为同主机外部托管的 `del109-asr`）在同一次窗口里取得完整读数——第一次 `POST /v1/audio/transcriptions` → `200` 后租约 `released`、`budget_released_at_us` 与 root `settled_at_us` 非空、实例 `held_slots` 与工作区 `held_attempts` 回 `0`；紧接着第二次仍 `200` 并在一个扫描周期内同样归还；把 `SPEECHNEXUS_SPEECH_SETTLEMENT_DATABASE_URL` 置空重启后同步 ASR 答 `503 backend_unavailable` 且启动日志点名该键，恢复后 `200` 并照常归还。逐项读数见 [MVP 判定表第 7 行](../plans/mvp-release-readiness.md) |
| 节点控制器、runtime delivery、asset maintenance | **未装配** | 本配置有意排除这些服务；不能以本 Compose 的探活结果证明完整部署与发布资格 |
| Profile 发布 / 激活（`POST .../publications`、`.../activations`） | **默认未启用；启用时资格由部署方文档提供** | 默认 `SPEECHNEXUS_PROFILE_WRITER_DATABASE_URL` 为空，`sn_profile_writer` 未预置，发布路由返回 `503 profile_result_unknown`（能力未启用）。启用 writer 必须同时提供已评审发布证据文档 `SPEECHNEXUS_PROFILE_PUBLICATION_EVIDENCE_FILE`：缺任一输入、文档不可读或格式非法时 API 在启动阶段点名变量退出，不会等发布时才回 503。资格内容与评审责任在部署方，本配置不提供任何默认资格。步骤见 [§6 预设发布资格输入](#预设发布资格输入与-sn_profile_writer-角色) |
| 自定义发音词典与 voice 发布（`/v1/nexus/pronunciations*`、`/v1/nexus/voice-publications*`） | **默认未启用** | 发音词典的三个身份与 `sn_voice_publisher` 不在本参考部署预置：读取返回 `503 pronunciation_unavailable`，`publish: true` 返回 `422 pronunciation_publication_not_supported`，voice 发布返回 `503 voice_draft_unavailable`。原因与启用路径见 [故意不预置的身份](#故意不预置的身份) |
| 真实模型质量、容量、GPU、物理多节点资格 | **未证明** | Compose 启动、健康检查和合成配置都不能替代模型/设备/拓扑验收 |

因此本文件的“通过”只表示 **最小单元已装配并可探活**，不表示公开 ASR/TTS 业务已可用。

## 0. 构建前提

需要 Docker Engine 23 或更新版本、Compose v2，以及可用的 BuildKit/Buildx：

```bash
docker version
docker compose version
docker buildx version
```

本目录的镜像构建使用 Dockerfile 1.18 frontend 与 BuildKit cache mount；缺少 Buildx 时会在
解析 Dockerfile 前失败。先修好 Docker Desktop 的 Buildx 插件或 Linux 的
`docker-buildx-plugin`，不要改用关闭 BuildKit 的构建来绕过。

**首次 `up` 之前显式构建一次产品镜像，之后所有 `up -d` 都不带 `--build`。** 在 §2 生成
`deploy/compose.env` 之后执行：

```bash
docker compose --env-file deploy/compose.env -f deploy/compose.yaml build \
  api gateway migrate engine-register job-dispatch-worker minio-init asset-inspector
```

服务名必须逐个列出，不能省成 `docker compose build`（那会构建整个项目），也不能把
`--build` 加回 `up`：`api` 与 `gateway` 通过 `depends_on` 带出 `minio`，`up --build` 会连带
构建 `minio`；而 `deploy/images/minio.Dockerfile` 在构建期取 `raw.githubusercontent.com` 的
LICENSE 做校验，受限网络主机上会以 `target minio: failed to solve` 失败，并让整条 `up` 失败。
显式点名服务时 Compose 只构建点到的镜像，上面这条命令里没有 `minio`。

`minio` 仍需要一份可用镜像：本机已有同 tag 镜像
（`speechnexus-minio:RELEASE.2025-09-07T16-13-09Z`）时直接复用；没有时须先在能访问那两个下载
通道的窗口构建，再把镜像导入受限主机。镜像缺失时 `up -d postgres minio` 会尝试现场构建该镜像
并失败，因此这一步不能跳过。`minio` 的构建只发生在那样的出网窗口，本文件不把它写成交付步骤。

## 1. 固定服务集合

在仓库根目录执行。先给本次部署一个独占的 Compose 项目名：`deploy/compose.yaml` 第 1 行自带
`name: speechnexus`，两套部署直接相遇时会争抢同名容器、卷与网络，第二次 `up` 会接管或
报冲突。`COMPOSE_PROJECT_NAME` 覆盖该 `name:` 字段，且下面每次 `"${COMPOSE[@]}"` 调用都会
沿用它：

```bash
export COMPOSE_PROJECT_NAME=speechnexus-ref   # 每套部署唯一；不设置则回落到 speechnexus
COMPOSE=(docker compose)
"${COMPOSE[@]}" --env-file deploy/compose.env.example \
  -f deploy/compose.yaml config --services
```

项目名只隔离容器/卷/网络命名，不改变端口占用与网段：项目名（本节）、宿主端口（§2）、
Compose 子网与 MinIO 静态地址（本节末尾）三者要一起确定，任何一项撞车都起不完全。

预期输出（顺序可不同）：

```text
minio
minio-init
postgres
gateway
api
```

`deploy/compose.yaml` 顶部的注释明确排除四项：`node-controller`、
`runtime-delivery-worker`、`asset-maintain`、`voice-evidence-worker`（自动记录需要独立的
recorder DSN 与报告对象存储读取凭据）。`migrate` 和 `engine-register` 分别属于按需启用的
`migration` 和 `engine-registration` profile；`binding-disable-worker` 属于
`binding-disable` profile，`job-dispatch-worker` 属于 `job-dispatch` profile，
`admin` / `workbench` 属于 `web` profile，因此都不属于上面的基础集合。独立工作台后端
`workbench-migrate` / `workbench-api` 属于 `workbench-api` profile（见 §6）；它与
`web` profile 是两件事：`web` 只起前端，`workbench-api` 起前端代理指向的真实服务。两个前端
镜像的构建命令、来源提交 label 与可复现性结论见
[deploy/README.md 的 Web images 段](../../deploy/README.md#web-images)。**已知缺陷**：两个前端镜像在自身
`Content-Security-Policy` 下渲染空文档（`/healthz` 仍 `200`，因此健康检查看不出来），修复与门禁见
[#840](https://github.com/hrygo/SpeechNexus/issues/840)；在修复合入前，`web` profile 只用于验证代理
路径与服务装配，不作为可用的界面证据。Job 派发
profile 的变量与默认 `dispatch_kinds` 见
[deploy/README.md](../../deploy/README.md#job-dispatcher-job-dispatch-profile)；
本参考部署只装配 `asr`、`tts` 车道，启动闸门对其它 kind 点名拒绝（见 §6 的异步 Job 段）。

默认网络使用 `172.28.0.0/24`（`deploy/compose.yaml:850` 的 `ipam`），MinIO 地址为
`172.28.0.10`（`deploy/compose.yaml:90` 的 `ipv4_address`）；启动前须核对该网段不会与
宿主机路由或其他容器网络冲突。两项都由本机插值的环境变量承载，默认值就是上面两个：

| 变量 | 默认值 | 作用 |
| --- | --- | --- |
| `SPEECHNEXUS_COMPOSE_SUBNET` | `172.28.0.0/24` | `networks.default` 的子网 |
| `SPEECHNEXUS_MINIO_STATIC_IP` | `172.28.0.10` | `minio` 的 `ipv4_address`，必须是同一子网的第 10 个主机地址 |

两者都不进容器，只影响 Compose 渲染；不设置时渲染结果与引入变量前逐字节相同。它们必须一起
改，第二项不需要也不应该手算：`deploy/compose-network.py` 从一个 CIDR 推导出这一对值与私有
配置要写的 `objects.endpoint`。本机 `172.28.0.0/24` 已被占用的照抄步骤见 §1.1。

与之成组改的还有四份 gateway 私有配置的 `objects.endpoint`（复制到私有目录后改副本）：
`deploy/private/gateway-file-asr.example.json:8`、`deploy/private/gateway-file-tts.example.json:11`、
`deploy/private/gateway-file-voice-clone.example.json:5`、`deploy/private/gateway-file-voice-design.example.json:5`。
提交在仓库里的示例固定写默认地址 `http://172.28.0.10:9000`，换子网时改的是私有副本，不是这些
示例；副本里漏改任何一处都会让网关连不上对象存储。文件语音对象
适配器要求私有 IP 地址，不能直接改成 `minio` DNS 名称；API 使用的 Compose 服务名
`minio` 是另一条配置路径。详见[私有部署输入说明](../../deploy/private/README.md)。

### 1.1 本机 `172.28.0.0/24` 被占用时

下面是照抄即可的换网段路径。示例把子网换成 `172.30.0.0/24`、把宿主 API 端口换成 `8099`；
本机值不同时整段替换成自己的值，不要照抄任何内网地址。项目名仍按本节开头单独设置。

1. 用一个 CIDR 推导两行赋值和私有配置要写的 endpoint，不手算 MinIO 地址：

```bash
python3 deploy/compose-network.py 172.30.0.0/24
```

预期输出（前两行写进环境文件，后两行是注释）：

```text
SPEECHNEXUS_COMPOSE_SUBNET=172.30.0.0/24
SPEECHNEXUS_MINIO_STATIC_IP=172.30.0.10
# objects.endpoint for the copies of deploy/private/gateway-file-*.json:
# http://172.30.0.10:9000
```

2. 在 §2 生成 `deploy/compose.env` 之后，把该文件里这三行的值改成示例值（§2 从当前
`deploy/compose.env.example` 复制，三行都已存在；用编辑器改值等价）：

```bash
sed -i.bak \
  -e 's/^SPEECHNEXUS_COMPOSE_SUBNET=.*/SPEECHNEXUS_COMPOSE_SUBNET=172.30.0.0\/24/' \
  -e 's/^SPEECHNEXUS_MINIO_STATIC_IP=.*/SPEECHNEXUS_MINIO_STATIC_IP=172.30.0.10/' \
  -e 's/^SPEECHNEXUS_API_HOST_PORT=.*/SPEECHNEXUS_API_HOST_PORT=8099/' \
  deploy/compose.env
rm -f deploy/compose.env.bak
```

3. 按 §3 复制私有文件之后，把这些副本里的 `objects.endpoint` 从默认地址换成第 1 步打印的
地址；§3 复制了 asr 与 tts 两份，若另外复制了 voice-clone/voice-design 的 lane 配置，同样处理：

```bash
D=deploy/.runtime/private
sed -i.bak 's#http://172.28.0.10:9000#http://172.30.0.10:9000#' \
  "$D/gateway-file-asr.json" "$D/gateway-file-tts.json"
rm -f "$D"/*.bak
```

4. 启动前确认渲染值就是刚写的这两个（`grep` 只挑这三行）：

```bash
"${COMPOSE[@]}" --env-file deploy/compose.env -f deploy/compose.yaml config \
  | grep -E 'ipv4_address:|subnet:|published: "8099"'
```

预期输出（顺序可不同）：

```text
        published: "8099"
        ipv4_address: 172.30.0.10
        - subnet: 172.30.0.0/24
```

端口 `8000` 也被占用的主机同样按上面的写法改宿主端口；容器内端口不变。同一台机器上并行起
第二套参考部署时，子网与全部宿主端口都要与本机已占用的那套不同，默认值会让第二套在创建网络
时报 `Pool overlaps`。


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
    # Signs the workbench list cursors; the workbench unit fails closed without it.
    "SPEECHNEXUS_WORKBENCH_CURSOR_KEY": secrets.token_urlsafe(32),
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

生成的口令在第一次引导时写入 `deploy/compose.env`，此后复用；重新生成就是轮换，必须同时改
角色口令与使用它的 DSN，只改文件里的口令会让 DSN 与角色口令不一致、连接直接失败。§6 预置的
十七个非基线角色口令按同一规则在 §6 首次引导时生成并写回同一文件。

上面的 Python 片段必须通过环境变量 `SN_ROOT` 取得仓库绝对路径；若直接手工执行，
先运行 `export SN_ROOT="$ROOT"`。编辑 `deploy/compose.env`，按实际部署核对以下配置：

- `SPEECHNEXUS_ENGINE_HEALTH_URL`：注册命令可访问的引擎 `/health` origin；
- `SPEECHNEXUS_NODE_ID` / `SPEECHNEXUS_GATEWAY_ID`：要与两份 gateway endpoint JSON
  以及 recipe 的 `runtime_plan.binding` 完全一致；
- `SPEECHNEXUS_ENGINE_INSTANCE_ID` / `SPEECHNEXUS_ENGINE_GENERATION`：注册使用的实例与代次；
  实例须与 recipe 的 `stage.instance_id` 和 ASR endpoint 一致，代次须与 ASR endpoint 一致；
- `SPEECHNEXUS_OBJECT_STORAGE_BUCKET` / `SPEECHNEXUS_TRANSCRIPTION_INPUT_BUCKET`：前者是
  Job 输入资产桶（`/v1/nexus/assets` 的落点），后者是同步转写的上传桶。两者的**角色**不同，
  但参考部署必须给它们**同一个桶名**：网关只有一个 `objects.input_bucket`，同时服务这两条
  路径，分开配置会让同步转写在调用引擎前失败（不变量与实测见 §3 第 4 项）。私有 JSON 里
  `objects.input_bucket` 绑定前者；`objects.result_bucket` 必须同时等于
  `SPEECHNEXUS_EXECUTION_RESULT_BUCKET`（异步 staging 读取桶）与
  `SPEECHNEXUS_OBJECT_STORAGE_BUCKET`（后端同步转写的结果读回桶），参考部署让三者同名，
  否则同步结果读回失败（见 §3 第 4 项）。
- `SPEECHNEXUS_EXECUTION_RESULT_BUCKET`：gateway 私有 JSON 的 `result_bucket` 与
  `job-dispatch` profile 的 staging 读取桶；`minio-init` 会一并创建并启用版本化。参考部署
  必须让它与 `SPEECHNEXUS_OBJECT_STORAGE_BUCKET` 同名，因为后端同步转写按后者读回结果。

生成脚本使用示例的 `POSTGRES_USER=speechnexus` 和 `POSTGRES_DB=speechnexus` 写入
各数据库连接地址。若在启动前更改数据库名，须同步更新 API、gateway、gateway 查询、
迁移及注册连接地址中的数据库名；更改 bootstrap 用户名时，须更新使用该身份的迁移和
注册地址，同时保留独立的运行角色。还须重新核对第 6 节的授权目标；只改这两个变量
不会自动改写已生成的连接地址。已有数据卷不能通过修改环境文件完成用户或数据库迁移。

`SPEECHNEXUS_REGISTRATION_DATABASE_URL` 与 `SPEECHNEXUS_MIGRATION_DATABASE_URL` 是
一次性 operator 身份；本机示例沿用 bootstrap owner。它们不会传给 `api` 或 `gateway`，
基础配置中 API 使用 `sn_api`，gateway 分别使用 `sn_gateway` 写入和 `sn_gateway_query`
只读查询。API 的 profile、Job 与 audio inspection 等可选数据库连接默认为空；启用对应能力时须按
各自要求另行配置身份与授权，不能假定基础角色已覆盖。工作区写、审计读取、实例读取、隔离
释放、公开目录、Artifact、资源池、binding 列表、binding 写、binding admin 读取与 binding
生命周期以及独立工作台后端的运行/清理身份的连接地址由第 6 节的角色预置步骤写入本文件；
每类身份的当前处置见
[透传 DSN 覆盖表](#透传-dsn-覆盖表)。生产部署必须把迁移和注册连接
换成独立、受审计的 operator 凭据，不能复用运行角色。

## 3. 准备私有文件

以下步骤仅创建新的私有配置；任何目标已存在时停止，先核对归属和内容，不覆盖已有配置。

```bash
D=deploy/.runtime/private
for name in transcription-recipe.json gateway-file-asr.json gateway-file-asr-endpoint.json \
  gateway-file-tts.json gateway-file-tts-endpoint.json speech-recipe.json; do
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
cp deploy/private/speech-recipe.example.json "$D/speech-recipe.json"
chmod 600 "$D/transcription-recipe.json" "$D/gateway-file-asr.json" \
  "$D/gateway-file-asr-endpoint.json" "$D/gateway-file-tts.json" \
  "$D/gateway-file-tts-endpoint.json" "$D/speech-recipe.json"
```

按[私有部署输入说明](../../deploy/private/README.md)替换全部合成字段。最少要逐项核对：

1. recipe 的 `binding_publication_id`、`binding_revision_id`、`manifest_id`、
   `policy_revision_id`、`stage.instance_id` 与 `runtime_plan.binding` 一致；
   `speech-recipe.json` 没有 `binding_publication_id`（TTS 的发布事实在准入事务中读取），
   但同样要求 `binding_revision_id` 与 `manifest_id` 同时等于
   `runtime_plan.binding` 内的值、`service_class` 与 `runtime_plan.service_class` 一致、
   `quota_dimensions` 恰为 `["concurrent_attempts"]`、`stage.reserved_slots` 不小于
   `minimum_slots`，且 `admission_expires_at_us` 尚未过期；样例里的
   `model_id`/`approved_template_id`/`instance_id` 必须与部署的 TTS 模板与实例一致；
   `pool_id` 相反**必须保持 `tts-batch`**，它是 `file_tts_v2` 的域常量
   （`domain/tts.py:129` 改了即拒），不是占位；
2. ASR endpoint 的 `target` 与 recipe 的 node/gateway/pool/instance/generation 一致；
3. TTS endpoint 的 `target` 与 TTS policy revision、instance/generation 一致；
   使用 `file_tts_v2`，`builtin_voice_binding` 的公开 id/revision、内部 revision id 来自
   同一权威声音版本，`native_speaker_id` 精确匹配 `tts_policy.voice`；旧 v1 不再接受。
   [升级规则](file-tts-runtime.md#私有配置格式)要求停止旧 worker 后激活，
   样例映射不提供声音资格；
4. gateway 配置的 `objects.endpoint` 与实际 MinIO 一致，两个桶名分别绑定到不同的键，不能合并成
   一句「与 bucket 一致」：

   - 声明了 `objects.input_bucket` 的 lane（`file_asr_v1`、`file_voice_clone_v1`）读的是
     **Job 输入资产桶**，必须等于 `SPEECHNEXUS_OBJECT_STORAGE_BUCKET`：`/v1/nexus/assets`
     上传的资产写在这个桶里（`asset_runtime.py:264`），Job 命令携带的
     `storage.storage_bucket` 也取它（`public_job_admission.py:466`）。写错的后果不是拒绝
     启动，而是**首个 Job 在调用引擎前被隔离**：gateway 对
     `reference.storage_bucket != objects.input_bucket` 直接抛
     `execution_input_scope_mismatch`（`execution_objects_minio.py:110`）。
   - `objects.result_bucket` 必须同时等于 `SPEECHNEXUS_EXECUTION_RESULT_BUCKET` 与
     `SPEECHNEXUS_OBJECT_STORAGE_BUCKET`：派发 worker 按前者读回异步 staging 结果
     （`job_dispatch_runtime.py:258` 用 `settings.execution_result_bucket` 构造读回客户端），
     后端同步转写按后者读回同一批结果（`transcription_runtime.py:118` 用
     `settings.object_storage_bucket` 构造 `MinioExecutionResultObjects`）。参考部署让三者
     同名；只把 staging 桶配成另一个名字时，网关写入与提交都成功，直到结果读回才抛
     `result_object_scope_rejected`（`gateway_execution_client.py:720`），同步请求以
     `503 backend_unavailable` 结束，参考部署取同一个 `speechnexus-results`
     （见 §2 的环境文件注释）。两条等式由不同步骤检查：同步读回那条（lane ==
     `SPEECHNEXUS_OBJECT_STORAGE_BUCKET`）由 `tools` 步骤对已存在的私有 lane 配置检查
     （`lane_result_bucket_gaps`）；staging 那条（lane ==
     `SPEECHNEXUS_EXECUTION_RESULT_BUCKET`）由 `stack` 步骤
     （`reference-acceptance.py:1428-1432`）与启动闸门
     （`deploy/job-startup-gate.py:446-482`）检查。

   这两个桶的绑定都不来自 `SPEECHNEXUS_TRANSCRIPTION_INPUT_BUCKET` 键；参考部署上该键
   **必须与 `SPEECHNEXUS_OBJECT_STORAGE_BUCKET` 相同**。它是同步转写的上传落点
   （`transcription_runtime.py:111`），而网关的 `objects.input_bucket` 只有一个：
   同一进程既服务异步 Job 输入，也服务同步转写上传。两个桶名不相等时，同步请求在
   **调用引擎之前**就被 `execution_input_scope_mismatch` 拒绝，对外固定
   `503 backend_unavailable`——引擎侧完全看不到该请求，dispatch 标记后约 16 ms 即被隔离
   （DEL-539 实测，2026-11-18）。隔离占用计入 `engine_instances.quarantined_slots`，而
   `CHECK (held_slots <= max_slots - quarantined_slots)`
   （`backend/src/speechnexus_backend/speech/infrastructure/coordination_schema.py:145`）
   使隔离槽位与已持有槽位共享 `max_slots` 上限，所以这不是零成本失败。默认值已对齐为同一个桶；
   「网关单输入桶」是当前实现边界，按 lane 配置第二个输入桶属于后续工作。
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

for name in ("transcription-recipe.json", "speech-recipe.json"):
    path = Path("deploy/.runtime/private") / name
    document = json.loads(path.read_text())
    document.pop("recipe_digest", None)
    digest = "sha256:" + hashlib.sha256(rfc8785.dumps(document)).hexdigest()
    original = json.loads(path.read_text())
    original["recipe_digest"] = digest
    path.write_text(json.dumps(original, indent=2) + "\n")
    path.chmod(0o600)
    print(f"{name} recipe_digest:", digest)
PY
```

至少做一次语法/权限检查：

```bash
python3 -m json.tool "$D/transcription-recipe.json" >/dev/null
python3 -m json.tool "$D/speech-recipe.json" >/dev/null
python3 -m json.tool "$D/gateway-file-asr.json" >/dev/null
python3 -m json.tool "$D/gateway-file-tts.json" >/dev/null
python3 - "$D/transcription-recipe.json" "$D/speech-recipe.json" <<'PY'
import os
import stat
import sys

for path in sys.argv[1:]:
    mode = stat.S_IMODE(os.stat(path).st_mode)
    if mode != 0o600:
        raise SystemExit(f"{path} must be mode 0600, got {mode:04o}")
    print(f"private mode ok: {path} 0600")
PY
```

只要同步 ASR（不要同步 TTS）的部署在 `deploy/compose.env` 里设
`SPEECHNEXUS_SPEECH_ENABLED=false`，不要改 `deploy/compose.yaml`。该键是
`Literal["true","false"]`、默认 `true`：未设置或留空仍是开启（compose 始终透传固定的配方路径，
因此缺文件照样启动失败，fail-closed），只有字面 `false` 关闭并在 API 日志留下
`Synchronous speech disabled by SPEECHNEXUS_SPEECH_ENABLED=false`，拼错则配置校验失败并点名该键
（`SPEECHNEXUS_SPEECH_ENABLED must be 'true' or 'false'; got 'ture'`）。改仓库文件的副本会在每次
升级时产生无法校验的漂移，因此不作为受支持做法。

只解析四个 JSON，并检查两份 recipe 的权限位；它不检查两份 endpoint JSON、
文件所有者、符号链接、摘要或配置间的身份一致性。`os.stat` 会跟随链接；API 读取
recipe 时另要求当前运行 UID 拥有的普通文件，且拒绝最终路径为符号链接。须结合上面的
逐项核对与实际运行身份验证，不能把这段检查通过当作配置已获批准或启动必然成功。

## 4. 启动数据库与对象存储

启动 PostgreSQL 与 MinIO，然后执行一次桶初始化。镜像来自 §0 的一次性构建（`postgres` 用
官方镜像、`minio` 用已导入的同 tag 镜像），这里不再带 `--build`：

```bash
"${COMPOSE[@]}" --env-file deploy/compose.env -f deploy/compose.yaml \
  up -d postgres minio
"${COMPOSE[@]}" --env-file deploy/compose.env -f deploy/compose.yaml \
  run --rm minio-init
```

预期 `minio-init` 成功退出，并对每个**不同**桶名输出一行。默认三个键（同步转写输入桶、
Artifact 桶、`SPEECHNEXUS_EXECUTION_RESULT_BUCKET`）都指向 `speechnexus-results`，
因此只有一行：

```text
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
"${COMPOSE[@]}" --env-file deploy/compose.env -f deploy/compose.yaml \
  exec -T postgres psql -U speechnexus -d speechnexus -Atc \
  'select version_num from speech.alembic_version'
```

上述命令经 Compose 服务名 `postgres` 进入容器，因此不绑定容器名：项目名取自 §1 的
`COMPOSE_PROJECT_NAME`，容器实际叫 `<项目名>-postgres-1`，直接写死它会得到
`No such container`。用户与数据库名对应默认配置；若部署值不同，先核对实际名称再查询。
仓库当前迁移链的 head 为 `0057_asr_settlement`；实际部署必须以所用发布制品中的唯一
head 为准，查询结果须与之相符。不要把旧版 `0027_gateway_abort_intents` 或
`0039_public_job_progress` 当作当前 head，也不要跳过检查或手工修改版本表。

### 结算 worker 的两个运维入口

升级到含 `0057_asr_settlement` 的版本后，先验启动边界、再一次结旧账，不要靠「重启看看」：

```bash
# 只验边界：两个连接池、预检与结算角色的实际授权；成功打印一行 `asr settlement: check ok`
"${COMPOSE[@]}" --env-file deploy/compose.env -f deploy/compose.yaml \
  exec -T api python -m speechnexus_backend.asr_settlement_worker --check

# 只扫一轮：把升级前遗留的同步 ASR 租约结掉（API 进程内的运行时平时自动跑）
"${COMPOSE[@]}" --env-file deploy/compose.env -f deploy/compose.yaml \
  exec -T api python -m speechnexus_backend.asr_settlement_worker --once
```

`--check` 失败时打印一行 `FAIL: …` 并以退出码 1 结束（例如缺 `0057_asr_settlement` 的迁移、
结算角色缺列级授权），不会留下半开状态；`--once` 打印
`asr settlement: scanned=… released=… pending=…`，`pending` 表示本轮未拿到可判定的终态事实，
后续轮次会重访。两个入口都经 `python -m` 运行，与 API 启动时的自检是同一段代码。

## 6. 创建非 owner 运行角色与最小授权

以下脚本必须使用 **迁移 owner DSN** 运行，创建 API、gateway writer、gateway query、工作区
writer、审计 reader、实例读取 reader、隔离释放、公开目录 reader、Artifact reader、资源池
reader、binding 列表 reader、binding 写、binding admin reader、binding 生命周期、Artifact
保留期清理、同步 TTS/ASR 结算、外部托管引擎的资格声明、资格来源审计读取与独立工作台后端的
运行/清理两个身份共二十个独立非 owner 角色（三个基线角色 + 十七个其它身份，其中
`sn_qualification_reader` 只服务宿主侧审计命令）；另有三个 Job 角色由
[异步 Job 运行角色](#异步-job-运行角色与授权) 的 `provision-job-roles.py` 预置，因此
`deploy/compose.env.example` 共有二十三个 `*_ROLE_PASSWORD`。工作台两个角色的
授权目标是工作台自己的命名空间，本脚本只创建角色，授权由 §6 的迁移步安装。每个透传键当前是否有预置步骤见文末
[透传 DSN 覆盖表](#透传-dsn-覆盖表)，未预置的键及其原因见
[故意不预置的身份](#故意不预置的身份)。预设发布的写角色 `sn_profile_writer` 不在此脚本内：
它必须与部署方的资格文档同时启用，步骤见
[预设发布资格输入与 sn_profile_writer 角色](#预设发布资格输入与-sn_profile_writer-角色)。
各身份的最小授权都由仓库授权函数声明；实例读取、资源池读取与 binding 列表读取三个只读
投影目前没有对应的 `*_grants` 函数，脚本按各自运行时启动校验所核验的关系显式授权。此段
结算角色 `sn_speech_settlement` 同时服务同步 TTS 与同步 ASR。即使同步 TTS 关闭，该角色也必须有
`speech_asr_settlement` 的 `SELECT` 与列级 `UPDATE (next_check_at_us, settled_at_us)`，否则转写工厂
启动时的共享自检会拒绝启动——不存在「只关 TTS 就不用授 ASR」的组合。

此段只用于新建、独占的 SpeechNexus 数据库：
它会撤销数据库级 `PUBLIC TEMPORARY/CREATE`、`public` schema 的 CREATE 和大对象
写函数的 PUBLIC EXECUTE。已有共享数据库应先导出 ACL、核对其他角色依赖，再由部署
负责人评审等价最小授权；不要直接套用。应用启动只检查这些前置条件，不修改 ACL。
脚本使用 `autocommit=True`：每条已成功执行的撤权、角色创建或授权会立即提交；中途
失败不会整体回滚。失败后须先核对已创建的角色和实际 ACL，按保存的原状态及获准的
恢复步骤处理；直接重跑会因已有角色而停止，不能假设数据库仍处于执行前状态。

工作区 writer、审计 reader、实例读取 reader、隔离释放、公开目录 reader、Artifact reader、
资源池 reader、binding 列表 reader、binding 写、binding admin reader、binding 生命周期、
Artifact 保留期清理、同步 TTS/ASR 结算、外部托管引擎资格声明、资格来源审计读取与工作台运行/清理
共十七个角色的口令在
`deploy/compose.env.example` 里只有 `change-me-*` 占位。口令在第一次引导时生成并写入
`deploy/compose.env`，此后复用；重新生成就是轮换，必须同时 `ALTER ROLE ... PASSWORD`
并改写对应 DSN，只改文件里的口令会让连接失败。口令不打印到终端，只在建角色脚本和下面的
DSN 写回里从环境读取。

**顺序不能反**：`. deploy/compose.env` 会用文件里的值覆盖同名变量，所以先载入环境文件，
再生成并写回口令。先导出再载入会把这些变量换回文件里的 `change-me-*` 占位，于是本轮
「重新生成」的口令一个都没生效，建角色与拼 DSN 用的都是仓库里的公开占位值——整个流程
自洽、不报错，这正是它危险的地方。

先在当前 shell 载入环境文件，并准备迁移 owner 连接：

```bash
set -a
. deploy/compose.env
set +a
export SN_ADMIN_DSN="postgresql://${POSTGRES_USER}:${POSTGRES_PASSWORD}@127.0.0.1:${SPEECHNEXUS_POSTGRES_HOST_PORT:-55432}/${POSTGRES_DB}"
```

生成这十七个口令并写回 `deploy/compose.env`：只替换空值和 `change-me-*` 占位，已经是随机
值的键原样保留，因此重跑本步不会把已建角色的口令换成新值（换口令要按轮换步骤同时改 DSN）。
值来自 `secrets.token_urlsafe(32)`，字母表是 `A-Za-z0-9_-`，不含引号、空白、`#`、`$` 和
换行，所以按 `KEY=value` 原样落盘即可，既不需要引号也不需要转义，放进
`postgresql://role:pass@` 的 userinfo 也不需要百分号编码；脚本仍会核对生成值的字符集，
不符合就停止，不会写出半截配置。文件以 `0600` 落盘，脚本结束前核对不再有占位值。这十七个
键是 host 侧输入：`deploy/compose.yaml` 不引用它们、也没有 `env_file`，只有 DSN 会按需透传
给容器，口令本身不进任何容器环境。

```bash
umask 077
python3 - <<'PY'
import re
import secrets
from pathlib import Path

KEYS = (
    "SPEECHNEXUS_WORKSPACE_WRITER_ROLE_PASSWORD",
    "SPEECHNEXUS_AUDIT_READER_ROLE_PASSWORD",
    "SPEECHNEXUS_MODEL_INSTANCE_READER_ROLE_PASSWORD",
    "SPEECHNEXUS_QUARANTINE_RELEASE_ROLE_PASSWORD",
    "SPEECHNEXUS_CATALOG_READER_ROLE_PASSWORD",
    "SPEECHNEXUS_ARTIFACT_READER_ROLE_PASSWORD",
    "SPEECHNEXUS_RESOURCE_POOL_READER_ROLE_PASSWORD",
    "SPEECHNEXUS_BINDING_LIST_READER_ROLE_PASSWORD",
    "SPEECHNEXUS_BINDING_WRITER_ROLE_PASSWORD",
    "SPEECHNEXUS_BINDING_ADMIN_READER_ROLE_PASSWORD",
    "SPEECHNEXUS_BINDING_LIFECYCLE_ROLE_PASSWORD",
    "SPEECHNEXUS_SPEECH_SETTLEMENT_ROLE_PASSWORD",
    "SPEECHNEXUS_ARTIFACT_RETENTION_ROLE_PASSWORD",
    "SPEECHNEXUS_OPERATOR_ATTESTATION_ROLE_PASSWORD",
    "SPEECHNEXUS_QUALIFICATION_READER_ROLE_PASSWORD",
    "SPEECHNEXUS_WORKBENCH_RUNTIME_ROLE_PASSWORD",
    "SPEECHNEXUS_WORKBENCH_CLEANUP_ROLE_PASSWORD",
)
PLACEHOLDER = "change-me"
# token_urlsafe(32) 固定产出 43 个 URL-safe 字符；改生成方式时这里会先失败。
SAFE = re.compile(r"^[A-Za-z0-9_-]{43}$")

path = Path("deploy/compose.env")
text = path.read_text()
for key in KEYS:
    found = re.search(rf"(?m)^{re.escape(key)}=(.*)$", text)
    value = found.group(1).strip() if found is not None else ""
    state = "reused"
    if not value or value.startswith(PLACEHOLDER):
        value = secrets.token_urlsafe(32)
        if SAFE.match(value) is None:
            raise SystemExit(f"generated value for {key} is not dotenv/URL safe")
        state = "generated"
    line = f"{key}={value}"
    text = (
        re.sub(rf"(?m)^{re.escape(key)}=.*$", line, text, count=1)
        if found is not None
        else text + line + "\n"
    )
    print(f"{key}: {state}, length {len(value)}")
path.write_text(text)
path.chmod(0o600)
left = [key for key in KEYS if re.search(rf"(?m)^{re.escape(key)}=(?:change-me.*)?$", text)]
if left:
    raise SystemExit(f"placeholder role passwords remain in deploy/compose.env: {sorted(left)}")
print(f"{len(KEYS)} role passwords in deploy/compose.env, placeholders remaining: 0")
PY
set -a
. deploy/compose.env
set +a
```

写回后重新载入一次环境文件：此后 shell 里的 `SPEECHNEXUS_*_ROLE_PASSWORD` 就是文件里的那份
值，建角色与 DSN 写回都用它。口令已持久化，换 shell 只要先 `. deploy/compose.env` 就拿到同一
组值。已存在的角色不要靠重跑本步换口令：本步只填空值和占位值，轮换要 `ALTER ROLE` 并同步改
DSN。

建角色与授权（口令只从环境读取，不打印）：

```bash
uv run --all-packages python - <<'PY'
import os
from psycopg import sql
import psycopg
from speechnexus_backend.speech.infrastructure.artifact_schema import (
    artifact_reader_grants,
    artifact_retention_grants,
)
from speechnexus_backend.speech.infrastructure.asset_schema import asset_runtime_grants
from speechnexus_backend.speech.infrastructure.binding_draft_schema import binding_draft_grants
from speechnexus_backend.speech.infrastructure.binding_publication_evidence_schema import (
    binding_admin_reader_grants,
)
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
from speechnexus_backend.speech.infrastructure.publication_lifecycle_schema import (
    audit_reader_grants, binding_lifecycle_grants, binding_publication_grants,
)
from speechnexus_backend.speech.infrastructure.publication_schema import (
    model_catalog_reader_grants,
)
from speechnexus_backend.speech.infrastructure.quarantine_release_schema import (
    quarantine_release_grants,
)
from speechnexus_backend.speech.infrastructure.binding_disable_targets_schema import (
    disable_settlement_grants,
)
from speechnexus_backend.speech.infrastructure.builtin_voice_grants import (
    operator_attestation_writer_grants,
    qualification_reader_grants,
)
from speechnexus_backend.speech.infrastructure.settlement_worker_verifier import (
    settlement_worker_grants,
)
from speechnexus_backend.speech.infrastructure.qualified_speech_facts_postgres import (
    speech_synthesis_reader_grants,
)
from speechnexus_backend.speech.infrastructure.asr_settlement_schema import (
    asr_settlement_writer_grants,
)
from speechnexus_backend.speech.infrastructure.workspace_schema import workspace_writer_grants

schema = "speech"
roles = {
    "sn_api": os.environ["SPEECHNEXUS_API_ROLE_PASSWORD"],
    "sn_gateway": os.environ["SPEECHNEXUS_GATEWAY_ROLE_PASSWORD"],
    "sn_gateway_query": os.environ["SPEECHNEXUS_GATEWAY_QUERY_ROLE_PASSWORD"],
    "sn_workspace_writer": os.environ["SPEECHNEXUS_WORKSPACE_WRITER_ROLE_PASSWORD"],
    "sn_audit_reader": os.environ["SPEECHNEXUS_AUDIT_READER_ROLE_PASSWORD"],
    "sn_model_instance_reader": os.environ["SPEECHNEXUS_MODEL_INSTANCE_READER_ROLE_PASSWORD"],
    "sn_quarantine_release": os.environ["SPEECHNEXUS_QUARANTINE_RELEASE_ROLE_PASSWORD"],
    "sn_catalog_reader": os.environ["SPEECHNEXUS_CATALOG_READER_ROLE_PASSWORD"],
    "sn_artifact_reader": os.environ["SPEECHNEXUS_ARTIFACT_READER_ROLE_PASSWORD"],
    "sn_resource_pool_reader": os.environ["SPEECHNEXUS_RESOURCE_POOL_READER_ROLE_PASSWORD"],
    "sn_binding_list_reader": os.environ["SPEECHNEXUS_BINDING_LIST_READER_ROLE_PASSWORD"],
    "sn_binding_writer": os.environ["SPEECHNEXUS_BINDING_WRITER_ROLE_PASSWORD"],
    "sn_binding_admin_reader": os.environ["SPEECHNEXUS_BINDING_ADMIN_READER_ROLE_PASSWORD"],
    "sn_binding_lifecycle": os.environ["SPEECHNEXUS_BINDING_LIFECYCLE_ROLE_PASSWORD"],
    # Synchronous TTS settlement runs in its own pool, so it must not reuse
    # `sn_api`: that would put the API's schema-wide authority on a second
    # connection and the worker would refuse to start on the wider surface.
    "sn_speech_settlement": os.environ["SPEECHNEXUS_SPEECH_SETTLEMENT_ROLE_PASSWORD"],
    # The workbench unit's two roles: the serving identity and the erasing one.
    # Their grants target the workbench namespace, which the §6 migration step
    # bootstraps and grants; this script only creates the logins.
    "sn_workbench_runtime": os.environ["SPEECHNEXUS_WORKBENCH_RUNTIME_ROLE_PASSWORD"],
    "sn_workbench_cleanup": os.environ["SPEECHNEXUS_WORKBENCH_CLEANUP_ROLE_PASSWORD"],
    "sn_artifact_retention": os.environ["SPEECHNEXUS_ARTIFACT_RETENTION_ROLE_PASSWORD"],
    # External-engine qualification: the host-side attestation command appends one
    # approval plus one declaration row and holds nothing wider.
    "sn_operator_attestation": os.environ["SPEECHNEXUS_OPERATOR_ATTESTATION_ROLE_PASSWORD"],
    # The provenance report runs on the host and only needs to read the ledger:
    # qualifications, approvals, the provenance labels and the evidence rows that
    # back them. It must not be the schema owner, or the audit would confirm
    # whatever the writer wrote without any privilege boundary in between.
    "sn_qualification_reader": os.environ["SPEECHNEXUS_QUALIFICATION_READER_ROLE_PASSWORD"],
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
    # Synchronous TTS reads published voice/binding facts and writes its own
    # settlement root in the admission transaction, so one declaration covers both
    # halves. Without it the factory reports no qualified voice and keeps TTS off
    # even when every TTS key and the recipe file are present.
    for statement in speech_synthesis_reader_grants(schema, "sn_api"):
        conn.execute(statement)
    # Synchronous ASR writes its settlement root in the same admission
    # transaction, so the API identity needs exactly that column-level write.
    for statement in asr_settlement_writer_grants(schema, "sn_api"):
        conn.execute(statement)
    # The settlement runtime opens a second pool under its own identity. The
    # single declaration below is the exact union both workers verify at startup:
    # lease-settlement reads/writes, the TTS root/cancellation surface and the ASR
    # root cursor. Granting anything else to this role makes startup refuse.
    for statement in settlement_worker_grants(schema, "sn_speech_settlement"):
        conn.execute(statement)
    # The retention sweep runs as its own identity: SELECT plus
    # UPDATE(artifact_state, deleted_at_us) and nothing else. It refuses to start on a role that
    # holds any wider authority, so it cannot share `sn_api`'s schema privileges.
    for statement in artifact_retention_grants(schema, "sn_artifact_retention"):
        conn.execute(statement)
    # An operator-attested qualification is declared by its own minimal identity:
    # column-level INSERT on the approval ledger plus one attestation row, no
    # UPDATE/DELETE anywhere and no publication-head authority.
    for statement in operator_attestation_writer_grants(schema, "sn_operator_attestation"):
        conn.execute(statement)
    # The provenance audit reads published qualifications and the evidence rows
    # behind their source label; it is read-only and holds no write anywhere.
    for statement in qualification_reader_grants(schema, "sn_qualification_reader"):
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
    for statement in workspace_writer_grants(schema, "sn_workspace_writer"):
        conn.execute(statement)
    for statement in audit_reader_grants(schema, "sn_audit_reader"):
        conn.execute(statement)
    for statement in model_catalog_reader_grants(schema, "sn_catalog_reader"):
        conn.execute(statement)
    for statement in artifact_reader_grants(schema, "sn_artifact_reader"):
        conn.execute(statement)
    for statement in binding_draft_grants(schema, "sn_binding_writer"):
        conn.execute(statement)
    for statement in binding_admin_reader_grants(schema, "sn_binding_admin_reader"):
        conn.execute(statement)
    # Lifecycle publication records evidence in the same transaction that
    # advances the selected head, so both declarations belong to one identity.
    for statement in (
        *binding_lifecycle_grants(schema, "sn_binding_lifecycle"),
        *binding_publication_grants(schema, "sn_binding_lifecycle"),
    ):
        conn.execute(statement)
    # The release transaction writes B's stop evidence and A's capacity counters,
    # so its identity is declared by quarantine_release_grants(): schema USAGE
    # plus the exact column surface of the twelve relations it reads, updates and
    # inserts. Do not hand-write this GRANT and do not reuse the owner, the api
    # runtime role or any gateway/worker writer. Upgrading an existing deployment
    # must re-run this function and cancellation_debt_reader_grants: the declared
    # surface now reads three more relations column-level (speech_jobs, the
    # cancellation fence and the reconciliation ledger), and the optional debt
    # probe additionally reads execution_stop_evidence and
    # submit_intents.submit_intent_id.
    for statement in quarantine_release_grants(schema, "sn_quarantine_release"):
        conn.execute(statement)
    # No grants function declares these three projections yet, so mirror the
    # read-only reader functions exactly: schema USAGE plus SELECT on the
    # relations each runtime verifies at startup (READABLE in
    # model_instance_runtime.py, resource_pool_runtime.py and
    # binding_list_runtime.py).
    conn.execute(
        sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(
            sql.Identifier(schema), sql.Identifier("sn_model_instance_reader")
        )
    )
    conn.execute(
        sql.SQL("GRANT SELECT ON {}.engine_instances, {}.speech_binding_revisions TO {}")
        .format(
            sql.Identifier(schema),
            sql.Identifier(schema),
            sql.Identifier("sn_model_instance_reader"),
        )
    )
    conn.execute(
        sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(
            sql.Identifier(schema), sql.Identifier("sn_resource_pool_reader")
        )
    )
    conn.execute(
        sql.SQL("GRANT SELECT ON {}.engine_instances, {}.resource_pools TO {}")
        .format(
            sql.Identifier(schema),
            sql.Identifier(schema),
            sql.Identifier("sn_resource_pool_reader"),
        )
    )
    conn.execute(
        sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(
            sql.Identifier(schema), sql.Identifier("sn_binding_list_reader")
        )
    )
    conn.execute(
        sql.SQL('GRANT SELECT ON {}.speech_binding_families, {}.speech_binding_revisions, '
                '{}.speech_publications, {}.speech_publication_heads, '
                '{}.speech_publication_retirements TO {}')
        .format(
            sql.Identifier(schema),
            sql.Identifier(schema),
            sql.Identifier(schema),
            sql.Identifier(schema),
            sql.Identifier(schema),
            sql.Identifier("sn_binding_list_reader"),
        )
    )

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
    print(
        "roles ready: sn_api sn_gateway sn_gateway_query sn_workspace_writer "
        "sn_audit_reader sn_model_instance_reader sn_quarantine_release "
        "sn_catalog_reader sn_artifact_reader sn_resource_pool_reader "
        "sn_binding_list_reader sn_binding_writer sn_binding_admin_reader "
        "sn_binding_lifecycle sn_artifact_retention sn_operator_attestation "
        "sn_speech_settlement sn_workbench_runtime sn_workbench_cleanup"
    )
PY
```

若这里报 `role cannot be dropped because objects depend on it`、owner membership 或 schema
CREATE 权限，停止启动服务；不要用超级用户连接绕过检查。API 启动时还会再次核验
`sn_api` 不是数据库/表 owner，也不是可继承的 owner 成员。

脚本成功后，把十五个非基线身份的连接地址写回 `deploy/compose.env`（其中
`sn_workspace_writer`、`sn_audit_reader`、`sn_model_instance_reader` 与
`sn_quarantine_release` 四个此前已有写回，本节新增十一个）；脚本替换同名空行或追加，不打印
口令。写回脚本用 `os.environ[...]` 读取上面已写回文件的那十五个口令变量，两者必须一一对应；
`sn_qualification_reader` 不在此列：它只服务宿主侧审计命令，口令在环境文件里，连接地址按
下一节用 host 端口现场拼出，本 Compose 不转发。
变量已持久化，所以换 shell 后先 `. deploy/compose.env` 再执行即可，未载入环境文件时它会因
缺少 `SPEECHNEXUS_*_ROLE_PASSWORD` 抛 `KeyError` 而不是写出一组空口令。

```bash
python3 - <<'PY'
import os
import re
from pathlib import Path

values = {
    "SPEECHNEXUS_WORKSPACE_WRITER_DATABASE_URL": (
        "postgresql://sn_workspace_writer:"
        f"{os.environ['SPEECHNEXUS_WORKSPACE_WRITER_ROLE_PASSWORD']}"
        "@postgres:5432/speechnexus"
    ),
    "SPEECHNEXUS_AUDIT_READER_DATABASE_URL": (
        "postgresql://sn_audit_reader:"
        f"{os.environ['SPEECHNEXUS_AUDIT_READER_ROLE_PASSWORD']}"
        "@postgres:5432/speechnexus"
    ),
    "SPEECHNEXUS_MODEL_INSTANCE_READER_DATABASE_URL": (
        "postgresql://sn_model_instance_reader:"
        f"{os.environ['SPEECHNEXUS_MODEL_INSTANCE_READER_ROLE_PASSWORD']}"
        "@postgres:5432/speechnexus"
    ),
    "SPEECHNEXUS_QUARANTINE_RELEASE_DATABASE_URL": (
        "postgresql://sn_quarantine_release:"
        f"{os.environ['SPEECHNEXUS_QUARANTINE_RELEASE_ROLE_PASSWORD']}"
        "@postgres:5432/speechnexus"
    ),
    "SPEECHNEXUS_MODEL_CATALOG_READER_DATABASE_URL": (
        "postgresql://sn_catalog_reader:"
        f"{os.environ['SPEECHNEXUS_CATALOG_READER_ROLE_PASSWORD']}"
        "@postgres:5432/speechnexus"
    ),
    "SPEECHNEXUS_ARTIFACT_READER_DATABASE_URL": (
        "postgresql://sn_artifact_reader:"
        f"{os.environ['SPEECHNEXUS_ARTIFACT_READER_ROLE_PASSWORD']}"
        "@postgres:5432/speechnexus"
    ),
    "SPEECHNEXUS_RESOURCE_POOL_READER_DATABASE_URL": (
        "postgresql://sn_resource_pool_reader:"
        f"{os.environ['SPEECHNEXUS_RESOURCE_POOL_READER_ROLE_PASSWORD']}"
        "@postgres:5432/speechnexus"
    ),
    "SPEECHNEXUS_BINDING_LIST_READER_DATABASE_URL": (
        "postgresql://sn_binding_list_reader:"
        f"{os.environ['SPEECHNEXUS_BINDING_LIST_READER_ROLE_PASSWORD']}"
        "@postgres:5432/speechnexus"
    ),
    "SPEECHNEXUS_BINDING_WRITER_DATABASE_URL": (
        "postgresql://sn_binding_writer:"
        f"{os.environ['SPEECHNEXUS_BINDING_WRITER_ROLE_PASSWORD']}"
        "@postgres:5432/speechnexus"
    ),
    "SPEECHNEXUS_BINDING_ADMIN_READER_DATABASE_URL": (
        "postgresql://sn_binding_admin_reader:"
        f"{os.environ['SPEECHNEXUS_BINDING_ADMIN_READER_ROLE_PASSWORD']}"
        "@postgres:5432/speechnexus"
    ),
    "SPEECHNEXUS_BINDING_LIFECYCLE_DATABASE_URL": (
        "postgresql://sn_binding_lifecycle:"
        f"{os.environ['SPEECHNEXUS_BINDING_LIFECYCLE_ROLE_PASSWORD']}"
        "@postgres:5432/speechnexus"
    ),
    "SPEECHNEXUS_SPEECH_SETTLEMENT_DATABASE_URL": (
        "postgresql://sn_speech_settlement:"
        f"{os.environ['SPEECHNEXUS_SPEECH_SETTLEMENT_ROLE_PASSWORD']}"
        "@postgres:5432/speechnexus"
    ),
    "SPEECHNEXUS_ARTIFACT_RETENTION_DATABASE_URL": (
        "postgresql://sn_artifact_retention:"
        f"{os.environ['SPEECHNEXUS_ARTIFACT_RETENTION_ROLE_PASSWORD']}"
        "@postgres:5432/speechnexus"
    ),
    "SPEECHNEXUS_WORKBENCH_DATABASE_URL": (
        "postgresql://sn_workbench_runtime:"
        f"{os.environ['SPEECHNEXUS_WORKBENCH_RUNTIME_ROLE_PASSWORD']}"
        "@postgres:5432/speechnexus"
    ),
    "SPEECHNEXUS_WORKBENCH_CLEANUP_DATABASE_URL": (
        "postgresql://sn_workbench_cleanup:"
        f"{os.environ['SPEECHNEXUS_WORKBENCH_CLEANUP_ROLE_PASSWORD']}"
        "@postgres:5432/speechnexus"
    ),
}
path = Path("deploy/compose.env")
text = path.read_text()
for key, item in values.items():
    pattern = rf"(?m)^{re.escape(key)}=.*$"
    line = f"{key}={item}"
    text = re.sub(pattern, line, text, count=1) if re.search(pattern, text) else text + line + "\n"
path.write_text(text)
path.chmod(0o600)
print(
    "workspace writer, audit reader, instance reader, quarantine release, catalogue "
    "reader, Artifact reader, resource pool reader, binding list reader, binding "
    "writer, binding admin reader, binding lifecycle, speech settlement, Artifact "
    "retention and workbench runtime/cleanup DSNs written to deploy/compose.env"
)
PY
```

这些行都按示例的 `postgres` 服务名和 `speechnexus` 库名生成；改了 Compose 服务名、端口或
数据库名时同步修改。

### 工作区写角色 `sn_workspace_writer`

`POST /v1/nexus/workspaces` 与 `PATCH /v1/nexus/workspaces/{workspace_id}` 用独立 writer
身份写 `speech_workspaces` 与 0049 的 `speech_workspace_commands` 重放账本。读取继续使用
`sn_api` 的资产运行时角色，该角色对 `speech_workspaces` 只有 `UPDATE (metadata_revision)`，
不能改写显示名或状态。未配置 `SPEECHNEXUS_WORKSPACE_WRITER_DATABASE_URL` 时两个写操作
返回 `503 workspace_service_unavailable`，列表与单条读取不受影响。

`deploy/compose.yaml` 的 `api` 服务以空默认透传该变量：只写 `deploy/compose.env` 而不建
角色时容器照常启动，两个写操作仍返回上面的 503；建了角色但权限面不符时 API 启动失败并
报 `Workspace writer startup rejected`，不会带着错误身份上线。授权由
`workspace_writer_grants(schema, role)` 声明，只含两张表的 `SELECT` 与列级
`INSERT`/`UPDATE`；不要手写 `GRANT`，也不要给整表 `INSERT`/`UPDATE`、`DELETE`、
`TRUNCATE`、`REFERENCES` 或 schema `CREATE`。核对列级授权（表级 `SELECT` 由启动校验和
读取路径验证）：

```bash
"${COMPOSE[@]}" --env-file deploy/compose.env -f deploy/compose.yaml \
  exec -T postgres psql -U speechnexus -d speechnexus -Atc \
  "select table_name, privilege_type, column_name from information_schema.column_privileges \
   where grantee = 'sn_workspace_writer' and privilege_type in ('INSERT','UPDATE') order by 1, 2, 3"
```

### 审计读取角色 `sn_audit_reader`

`GET /v1/nexus/audit-events` 用独立只读身份读 `speech_publication_lifecycle_events`。
生命周期 writer 对同一张表持有 `INSERT`，复用 writer 身份会让审计读取路径也能追加或
重放证据。授权由 `audit_reader_grants(schema, role)` 声明：schema `USAGE` 加该表的
`SELECT`，不含任何写权限；启动校验不通过时报 `Audit reader startup rejected` 并退出。
未配置 `SPEECHNEXUS_AUDIT_READER_DATABASE_URL` 时该端点返回
`503 audit_service_unavailable`。

`deploy/compose.yaml` 的 `api` 服务以空默认透传该变量：只写 `deploy/compose.env` 而不建
角色时容器照常启动，该端点仍返回上面的 503；角色与连接地址都按上一步预置后，同一部署面
返回 `200` 与空列表，容器不需要改动。

### 实例读取角色 `sn_model_instance_reader`

`GET /v1/nexus/admin/model-instances` 与
`GET /v1/nexus/admin/model-instances/{instance_id}/generations/{generation}` 用独立只读
身份读 `engine_instances` 与它引用的不可变 `speech_binding_revisions`，只投影已记录的实例
代次。实例意图、租约、提交意图、命令、停止证据与凭据都不在该身份可读范围内，复用 owner
或任何写入角色会让这两个读取端点同时获得命令实例的能力。

该读取面目前没有对应的仓库授权函数，`sn_model_instance_reader` 的授权就是上面脚本里
声明的 schema `USAGE` 与这两张表的 `SELECT`；不要手写额外的表或列授权，也不要给整表
`INSERT`/`UPDATE`、`DELETE`、`TRUNCATE`、`REFERENCES` 或 schema `CREATE`。进程启动时
自己核验同一组关系（`model_instance_runtime.py` 的 `READABLE`）：schema `USAGE`、两张表
的 `SELECT`、且无上述写权限，任一不满足即报
`Model instance reader startup rejected` 并退出，不会带着错误身份上线。未配置 `SPEECHNEXUS_MODEL_INSTANCE_READER_DATABASE_URL` 时两个端点返回
`503 model_instance_service_unavailable`。

`deploy/compose.yaml` 的 `api` 服务以空默认透传该变量：只写 `deploy/compose.env` 而不建
角色时容器照常启动，两个端点仍返回上面的 503；角色与连接地址都预置后，列表返回 `200`，
单条读取按记录返回 `200` 或 `404 model_instance_unknown` /
`404 model_instance_generation_unknown`。核对写权限确实没有落到该角色（含列级授权）：

```bash
"${COMPOSE[@]}" --env-file deploy/compose.env -f deploy/compose.yaml \
  exec -T postgres psql -U speechnexus -d speechnexus -Atc \
  "select table_name, privilege_type, column_name from information_schema.column_privileges \
   where grantee = 'sn_model_instance_reader' and privilege_type in ('INSERT','UPDATE','REFERENCES') order by 1, 2, 3"
```

该查询应无输出。该角色的表级授权只应是 `engine_instances` 与
`speech_binding_revisions` 的 `SELECT`，可对照 `information_schema.table_privileges`
或启动校验输出核对；两张表之外的表一律不授权。

### 异步 Job 运行角色与授权

启用 `job-dispatch` profile 前先预置三个互不相同的非 owner 角色：`sn_job_writer` 供 API
创建/读取 Job 与 voice 草稿，`sn_job_admission` 供原子准入，`sn_job_dispatch` 供派发
worker。三者不得复用 `sn_api`、`sn_gateway` 或 bootstrap
owner；数据库级撤权已由本节开头的新装脚本完成，这里只建角色并追加授权。

角色名与口令来自三条 Job DSN。先在本轮生成口令并把 DSN 写回 `deploy/compose.env`，
口令不打印到终端；三条 DSN 默认留空，所以不启用异步 Job 的部署仍可渲染并启动：

```bash
umask 077
export SPEECHNEXUS_JOB_WRITER_ROLE_PASSWORD="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
export SPEECHNEXUS_JOB_ADMISSION_ROLE_PASSWORD="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
export SPEECHNEXUS_JOB_DISPATCH_ROLE_PASSWORD="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
```

```bash
python3 - <<'PY'
import os
import re
from pathlib import Path

roles = {
    "SPEECHNEXUS_JOB_WRITER_DATABASE_URL": (
        "sn_job_writer",
        "SPEECHNEXUS_JOB_WRITER_ROLE_PASSWORD",
    ),
    "SPEECHNEXUS_JOB_ADMISSION_DATABASE_URL": (
        "sn_job_admission",
        "SPEECHNEXUS_JOB_ADMISSION_ROLE_PASSWORD",
    ),
    "SPEECHNEXUS_JOB_DISPATCH_DATABASE_URL": (
        "sn_job_dispatch",
        "SPEECHNEXUS_JOB_DISPATCH_ROLE_PASSWORD",
    ),
}
path = Path("deploy/compose.env")
text = path.read_text()
for key, (role, password_key) in roles.items():
    url = f"postgresql://{role}:{os.environ[password_key]}@postgres:5432/speechnexus"
    pattern = rf"(?m)^{re.escape(key)}=.*$"
    line = f"{key}={url}"
    text = (
        re.sub(pattern, line, text, count=1)
        if re.search(pattern, text)
        else text + line + "\n"
    )
path.write_text(text)
path.chmod(0o600)
print("Job DSNs written to deploy/compose.env")
PY
```

三条 DSN 同样按示例的 `postgres` 服务名与 `speechnexus` 库名生成；改了 Compose 服务名、
端口或库名时同步修改。随后用迁移 owner 安装角色与授权（脚本读取 DSN 中的身份，所以授权
目标与运行时身份不会漂移；角色已存在时直接停止）：

```bash
set -a
. deploy/compose.env
set +a
export SN_ADMIN_DSN="postgresql://${POSTGRES_USER}:${POSTGRES_PASSWORD}@127.0.0.1:${SPEECHNEXUS_POSTGRES_HOST_PORT:-55432}/${POSTGRES_DB}"

uv run --all-packages python scripts/dev-env/provision-job-roles.py
```

期望输出 `job roles ready: sn_job_writer sn_job_admission sn_job_dispatch; grants: <N>
statements`；语句数随授权函数变化，不要把它当固定值——以 `--print-statements` 的输出条数
为准，首次安装后把该值记为本次基线。`--print-statements` 只渲染 SQL、不连接数据库，便于
先评审。授权面：

- `sn_job_writer`：`job_runtime_grants`、`job_dispatch_grants`、`public_job_grants`、
  `voice_draft_grants`、`public_job_submission_grants`、`job_cancellation_fence_grants`、
  `cancellation_not_submitted_grants`、`cancellation_reconciliation_grants`，外加
  `speechnexus_backend.job_runtime` 启动校验需要的发布/素材基线 `SELECT` 和
  `coordination_jobs` 的四个认领列 `UPDATE`。缺任一项时 API 以
  `Public Job writer startup rejected` 退出，不会带着错误权限面上线。
- `sn_job_admission`：`public_job_admission_grants`（原子准入的写入面）。
- `sn_job_dispatch`：`job_runtime_grants`、`job_dispatch_grants`、`public_job_grants`、
  `cancellation_reconciliation_grants`、`coordination_runtime_grants`，外加迁移元数据
  `speech.alembic_version` 的只读 `SELECT`（启动闸门用它核对发布 head）。缺这条 `SELECT`、
  缺认领后的任一写入列、或 head 落后时，worker 的启动闸门在本容器启动阶段点名并退出，
  不会先领取 Job 再失败。

**升级前置（必须，不是建议）**：在本次之前建好的 `sn_job_dispatch` 没有
`speech.alembic_version` 的只读授权，而缺授权与"库真的落后"在闸门看来无法区分，所以闸门
对读不到的 head 一律拒绝启动并点名该表。升级镜像或重启 `job-dispatch` profile 之前先补：

```sql
GRANT SELECT ON speech.alembic_version TO sn_job_dispatch;
```

`job-dispatch-worker` 的入口是 `deploy/job-startup-gate.py`（由 `deploy/compose.yaml`
挂载并以该脚本作为 entrypoint）。它先核对角色守卫、迁移 head、与 worker 启动同源的
`SELECT` 探针、认领后的写入列（派发状态、结果 Artifact 发布、终止时的容量释放），以及
Artifact 桶与 gateway staging 结果桶的存在和版本化，全部通过后才 `exec` 真正的派发进程；
真实引擎与已注册实例作为部署方输入以 `pending` 行提示，不作为闸门失败条件（引擎可用性
另按 #572 的真实验收处理）。`SPEECHNEXUS_JOB_DISPATCH_KINDS` 只能取本参考部署装配的
`asr`、`tts`：其它 kind（例如 `voice_design`、`voice_validation`）在闸门里点名拒绝，
因为 worker 会认领该车道而在提交/发布阶段才失败。要启用别的车道，先装配 gateway 的
私有配置与 `SPEECHNEXUS_GATEWAY_FILE_*_CONFIG_FILE`，再把该车道加入闸门的
`GATEWAY_CONFIGS`，并同时安装该车道需要的授权组（`voice_validation` 对应
`voice_validation_worker_grants`）。

### 隔离释放角色 `sn_quarantine_release`

`POST /v1/nexus/admin/admission-leases/{lease_id}/quarantine-releases` 用独立的非 owner
身份执行一次跨所有者事务：写 B 的 `execution_stop_evidence` 运维证据、把唯一提交意图从
`unknown_quarantined/quarantined` 关闭为 `aborted/complete`，再写 A 的
`admission_quarantine_releases` 账本并归还 `workspace_quotas`、`host_nodes`、
`engine_instances`、`admission_leases` 的容量。因此该身份持有 B 两张表的窄写权，绝不能
复用迁移 owner、`sn_api` 运行时角色或任一 gateway/worker 写入身份。

授权由 `quarantine_release_grants(schema, role)` 声明：schema `USAGE`（无 `CREATE`）加
十二张关系的列级 `SELECT`/`UPDATE`/`INSERT`，其中只有
`execution_stop_evidence` 与 `admission_quarantine_releases` 两张表有 `INSERT`，账本表只有
`SELECT + INSERT`（append-only 触发器保证不可改写，故连该表的 `UPDATE` 都不授予）。
不要手写额外的表、列或序列授权，也不要给 `DELETE`、`TRUNCATE`、`TRIGGER`、
`REFERENCES`、schema `CREATE` 或用 `FOR UPDATE` 去要账本的 `UPDATE` 权限。

进程启动时逐列核对实际 ACL 与声明面：多给一列、少给一列、schema 内任何序列的
`USAGE`/`SELECT`/`UPDATE`、`admission_quarantine_releases_append_only` 触发器缺失或被
禁用，任一情况都以 `Quarantine release startup rejected` 拒绝启动，不会带着错误身份上线。

**升级必做**：该身份现在自己核验「围栏已受理、账本未 `settled`、Job 仍 `cancelling`」，
声明面因此新增 `speech_jobs`、`speech_job_cancellation_fences`、
`speech_job_cancellation_reconciliations` 三张关系的列级只读；可选的取消债务探针身份
（`SPEECHNEXUS_READINESS_CANCELLATION_DSN`）同时新增 `execution_stop_evidence` 与
`submit_intents.submit_intent_id`，账本授权从两列（`job_id`、`settled_at_us`）扩到四列
（再加 `last_outcome`、`next_check_at_us`）。配置了
`SPEECHNEXUS_QUARANTINE_RELEASE_DATABASE_URL` 的环境，升级时必须以 owner 重跑
`quarantine_release_grants`；配置了 `SPEECHNEXUS_READINESS_CANCELLATION_DSN` 的环境还必须
重跑 `cancellation_debt_reader_grants`。漏跑的可自查信号有四类（③ 与 ④ 互斥：④ 是账本
授权已生效时正常的 Job 级降级）：

1. 隔离释放角色：API 启动被拒并点名被拒的表/列（`Quarantine release startup rejected
   (... declared surface at <table>.<column>)`）；
2. 隔离释放角色：只跑操作员 CLI 时退出码 3、输出
   `unavailable: quarantine_release_privilege_missing`；只配了探针 DSN、没有隔离释放角色
   的部署不适用这一条；
3. 取消债务探针身份仍是旧的两列账本授权：探针整体是
   `cancellation_debt: unavailable (permission)`，而不是 Job 级降级形态；
4. 账本四列已授权、仍读不到租约/执行/终止证据表：这是正常的 Job 级降级，detail 按账本
   事实打印**命中的**标记（`never claimed` / `never claimable (exhausted)` / `due now`），
   三者按行互斥、只打印非零项。能看到这些标记就说明账本列授权已生效；整个 detail 退化为
   第 3 类则说明还没生效。

这些信号都**不会**让 `/readyz` 失败。`GRANT` 对下一次查询即生效：同一 DSN/角色的探针在
下一轮轮询就恢复，不需要重启 API；只有更换 DSN 指向新角色时才要重启重新装配。隔离释放
角色的结论来自启动期逐列校验，重跑授权后必须重新启动（或首次启动）API 才会通过。降级
形态与三个标记的语义见
[production-jobs.md 的可见面](../components/production-jobs.md#隔离不了的时候可见面与处置)，
此处不重复定义。

未配置 `SPEECHNEXUS_QUARANTINE_RELEASE_DATABASE_URL` 时该端点返回
`503 quarantine_release_unavailable`（`retryable: true`），所以运维脚本反复看到它时先确认
这个键是否配置，而不是继续轮询。已配置但授权被回收时返回
`503 quarantine_release_privilege_missing`，表/列缺失时返回
`503 quarantine_release_schema_not_migrated`，两者都 `retryable: false`：修部署，不重试。

`deploy/compose.yaml` 的 `api` 服务以空默认透传该变量：只写 `deploy/compose.env` 而不建
角色时容器照常启动，该端点仍返回上面的 503；角色与连接地址都预置后，同一部署面即可释放，
容器不需要改动。核对列级授权（表级 `SELECT` 由启动校验和事务读取路径验证）：

```bash
"${COMPOSE[@]}" --env-file deploy/compose.env -f deploy/compose.yaml \
  exec -T postgres psql -U speechnexus -d speechnexus -Atc \
  "select table_name, privilege_type, column_name from information_schema.column_privileges \
   where grantee = 'sn_quarantine_release' and privilege_type in ('INSERT','UPDATE') order by 1, 2, 3"
```

输出应只含声明面里的列：三处容量投影的计数与 `version`、`submit_intents` 的
`execution_state/cleanup_state/terminated_at_us/updated_at_us/version`、
`admission_leases` 的 `lease_state/cleanup_state/budget_released_at_us/version`、
`resource_pools.version`，以及 `execution_stop_evidence` 与
`admission_quarantine_releases` 的整表 `INSERT`（`column_name` 为
`NULL`）。再确认没有 `DELETE`/`TRUNCATE`/`REFERENCES` 落到该角色：

```bash
"${COMPOSE[@]}" --env-file deploy/compose.env -f deploy/compose.yaml \
  exec -T postgres psql -U speechnexus -d speechnexus -Atc \
  "select table_name, privilege_type from information_schema.table_privileges \
   where grantee = 'sn_quarantine_release' and privilege_type in \
     ('DELETE','TRUNCATE','REFERENCES','TRIGGER') order by 1, 2"
```

该查询应无输出。

### 公开目录、Artifact、资源池与 binding 身份

下面七个身份覆盖 `api` 容器里其余会按需启用的数据库读写面。它们与工作区写、审计读取、
实例读取、隔离释放一样，由 `deploy/compose.yaml` 的 `api` 服务以空默认透传：连接地址为空
时容器照常启动，对应端点保持表中那一个稳定 503（这是「能力未启用」，不是待排查的故障）；
写了地址但角色或授权缺失时 API 在启动阶段点名拒绝，不会带着错误身份上线，也不会等到第一个
请求才失败。

| 身份 | 键 | 端点 / 用途 | 未配置时 | 授权声明 |
| --- | --- | --- | --- | --- |
| `sn_catalog_reader` | `SPEECHNEXUS_MODEL_CATALOG_READER_DATABASE_URL` | `GET /v1/models`、`GET /v1/models/{model}`、`GET /v1/nexus/voices` | `503 model_catalogue_unavailable`、`503 voice_catalogue_unavailable` | `model_catalog_reader_grants` |
| `sn_artifact_reader` | `SPEECHNEXUS_ARTIFACT_READER_DATABASE_URL` | `GET /v1/nexus/artifacts/{artifact_id}` 与 `.../content` | `503 artifact_service_unavailable` | `artifact_reader_grants` |
| `sn_resource_pool_reader` | `SPEECHNEXUS_RESOURCE_POOL_READER_DATABASE_URL` | `GET /v1/nexus/admin/resource-pools` | `503 resource_pool_service_unavailable` | 本节脚本内显式 `GRANT`（无授权函数） |
| `sn_binding_list_reader` | `SPEECHNEXUS_BINDING_LIST_READER_DATABASE_URL` | `GET /v1/nexus/admin/model-bindings` | `503 model_binding_service_unavailable` | 本节脚本内显式 `GRANT`（无授权函数） |
| `sn_binding_writer` | `SPEECHNEXUS_BINDING_WRITER_DATABASE_URL` | `POST /v1/nexus/admin/model-bindings` 与 draft 读取 | `503 binding_unavailable` | `binding_draft_grants` |
| `sn_binding_admin_reader` | `SPEECHNEXUS_BINDING_ADMIN_READER_DATABASE_URL` | admin binding GET 投影与发布事实读取 | `503 binding_unavailable` | `binding_admin_reader_grants` |
| `sn_binding_lifecycle` | `SPEECHNEXUS_BINDING_LIFECYCLE_DATABASE_URL` | `POST .../model-bindings/{binding_id}/publications`、`POST .../model-bindings/{binding_id}/revisions/{revision}/retirements` 与 `.../activations`，以及停用意图 | `503 binding_result_unknown`（`retryable: false`） | `binding_lifecycle_grants` + `binding_publication_grants` |

`sn_catalog_reader` 只读已发布事实：`speech_publications`、只含已发布的
`speech_published_binding_revisions` 视图、`speech_catalog_state`、
`speech_publication_heads` 与 `speech_publication_retirements`，永远读不到草稿
`speech_binding_revisions` 本体，也不能推进目录版本。启动自检不合格时报
`Model catalogue reader startup rejected`。它同时驱动 `GET /v1/nexus/voices`，所以只配
`/v1/models` 一半能力并不存在：一个身份、两个公开读端点。

`sn_artifact_reader` 只 `SELECT` `speech_artifacts` 与 `speech_workspaces`，字节仍由对象
存储凭据按版本读取；对象存储键缺失属于 §2 的必填项，不属于本身份。启动自检不合格时报
`Artifact reader startup rejected (<异常类型>)`。

`sn_resource_pool_reader` 只 `SELECT` `engine_instances` 与 `resource_pools`；准入命令、
租约、提交意图与凭据都不在其可读范围。启动自检不合格时报
`Resource pool reader startup rejected`。

`sn_binding_list_reader` 只 `SELECT` `speech_binding_families`、
`speech_binding_revisions`、`speech_publications`、`speech_publication_heads` 与
`speech_publication_retirements`。命令、幂等键、发布证据与生命周期事件历史不在其可读
范围。启动自检不合格时报 `Binding list reader startup rejected`。

`sn_binding_writer` 对 `speech_binding_families`、`speech_binding_revisions`、
`speech_binding_commands` 持有 `SELECT`/`INSERT`，并且只有
`speech_binding_families.latest_revision` 的列级 `UPDATE`。它不与
`SPEECHNEXUS_BINDING_DEFAULTS_WRITER_DATABASE_URL` 同时配置：两种 producer 模式互斥，
同时出现时 API 在启动阶段拒绝（见[故意不预置的身份](#故意不预置的身份)）。启动自检不合格
时报 `Binding writer startup rejected`。

`sn_binding_admin_reader` 对 `speech_binding_families`、`speech_binding_revisions`、
`speech_binding_commands`、`speech_publications`、`speech_binding_publication_evidence`
与 `speech_publication_retirements` 只有 `SELECT`，用于 admin GET 与发布事实投影。启动
自检不合格时报 `Binding admin reader startup rejected`。

`sn_binding_lifecycle` 是 binding 发布、退休、激活与停用意图的唯一写入身份：两份声明
（`binding_lifecycle_grants` 与 `binding_publication_grants`）必须一起重放，因为它要在同一
事务里写证据、事件、选中 head、目录版本和素材引用释放。启动自检不合格时报
`Binding lifecycle startup rejected`。

核对这七个身份的实际表级授权（列级 `UPDATE` 不出现在这个视图里，单独看下一条）：

```bash
"${COMPOSE[@]}" --env-file deploy/compose.env -f deploy/compose.yaml \
  exec -T postgres psql -U speechnexus -d speechnexus -Atc \
  "select grantee, table_name, privilege_type from information_schema.table_privileges \
   where grantee in ('sn_catalog_reader','sn_artifact_reader','sn_resource_pool_reader', \
     'sn_binding_list_reader','sn_binding_writer','sn_binding_admin_reader', \
     'sn_binding_lifecycle') and privilege_type in ('SELECT','INSERT','UPDATE') \
   order by 1, 2, 3"
```

预期结果逐身份如下（`SELECT` 之外只允许 `sn_binding_writer` 与 `sn_binding_lifecycle`
的 `INSERT`，`INSERT` 列集由启动校验逐列核对）：

- `sn_catalog_reader`：`SELECT` 于 `speech_publications`、
  `speech_published_binding_revisions`、`speech_catalog_state`、
  `speech_publication_heads`、`speech_publication_retirements`；
- `sn_artifact_reader`：`SELECT` 于 `speech_artifacts`、`speech_workspaces`；
- `sn_resource_pool_reader`：`SELECT` 于 `engine_instances`、`resource_pools`；
- `sn_binding_list_reader`：`SELECT` 于 `speech_binding_families`、
  `speech_binding_revisions`、`speech_publications`、`speech_publication_heads`、
  `speech_publication_retirements`；
- `sn_binding_writer`：三张 binding 表的 `SELECT`/`INSERT`；
- `sn_binding_admin_reader`：上面六张表的 `SELECT`；
- `sn_binding_lifecycle`：`speech_publication_heads`、`speech_binding_disable_intents`、
  `speech_publications`、`speech_publication_retirements`、
  `speech_publication_lifecycle_events` 与 `speech_binding_publication_evidence` 的
  `SELECT`/`INSERT`，`speech_catalog_state` 与 `speech_binding_revisions` 的 `SELECT`。

五个只读 identity 必须没有任何写权限：

```bash
"${COMPOSE[@]}" --env-file deploy/compose.env -f deploy/compose.yaml \
  exec -T postgres psql -U speechnexus -d speechnexus -Atc \
  "select grantee, table_name, privilege_type from information_schema.table_privileges \
   where grantee in ('sn_catalog_reader','sn_artifact_reader','sn_resource_pool_reader', \
     'sn_binding_list_reader','sn_binding_admin_reader') \
   and privilege_type in ('INSERT','UPDATE','DELETE','TRUNCATE','REFERENCES','TRIGGER') \
   order by 1, 2, 3"
```

该查询应无输出。`sn_binding_writer` 的列级 `UPDATE` 只应是
`speech_binding_families.latest_revision`：

```bash
"${COMPOSE[@]}" --env-file deploy/compose.env -f deploy/compose.yaml \
  exec -T postgres psql -U speechnexus -d speechnexus -Atc \
  "select table_name, column_name from information_schema.column_privileges \
   where grantee = 'sn_binding_writer' and privilege_type = 'UPDATE' order by 1, 2"
```

该查询应只输出 `speech_binding_families|latest_revision` 一行；出现第二行即权限面过宽，
重放 `binding_draft_grants` 前先撤销多余列。生命周期身份只持有三个表上的列级 `UPDATE`
（发布指针、目录版本与素材引用释放），同样不体现在表级视图里：

```bash
"${COMPOSE[@]}" --env-file deploy/compose.env -f deploy/compose.yaml \
  exec -T postgres psql -U speechnexus -d speechnexus -Atc \
  "select table_name, column_name from information_schema.column_privileges \
   where grantee = 'sn_binding_lifecycle' and privilege_type = 'UPDATE' order by 1, 2"
```

该查询应只输出 `speech_asset_references|released_at_us`、`speech_catalog_state|generated_at_us`、
`speech_catalog_state|revision`、`speech_publication_heads|revision`、
`speech_publication_heads|revision_id` 与 `speech_publication_heads|updated_at_us` 六行；
多出任何一行都表示该身份可以把发布事实改成任意值。

已有部署按[已有部署的授权升级](#已有部署的授权升级)重放：七个身份都在该脚本的
`new_roles` 与授权重放列表内，`GRANT` 是追加式操作，重复执行不改变既有权限。

### Artifact 保留期清理角色 `sn_artifact_retention`

`artifact-retention` profile 的清理进程不读请求、不写业务行：一次 pass 只把到期且
`ready` 的制品改成 `expired`，再删除已 `expired` 行的固定对象版本并把行改成 `deleted`。
它因此使用**独立身份**，而不是 `sn_api`：API 运行身份按设计持有 schema 级读写与
`coordination_runtime_grants` 的宽权限，清理进程启动时会逐一拒绝这类权限（见下），
共用同一身份会让 profile 直接起不来。

| 项 | 值 |
| --- | --- |
| 键 | `SPEECHNEXUS_ARTIFACT_RETENTION_DATABASE_URL`（容器内为 `SPEECHNEXUS_DATABASE_URL`） |
| 角色 | `sn_artifact_retention` |
| 授权 | `artifact_retention_grants`：`USAGE` 于 schema、`SELECT` 于 `speech_artifacts`、列级 `UPDATE (artifact_state, deleted_at_us)` |
| 未配置 | 为空则容器启动即失败（`SPEECHNEXUS_DATABASE_URL` 无值），不会带着空身份空转 |
| 启动拒绝 | `Artifact retention startup rejected (<异常类型>)` |

只允许这两条语句所需的最小面：角色若能 `INSERT`、`DELETE`、`TRUNCATE`、`REFERENCES` 或
改写其它列（包括 `expires_at_us`），清理就从「按记录期限撤内容」变成「可以任意删除或延长
保留」，因此启动校验会点名列拒绝。核对列级授权：

```bash
"${COMPOSE[@]}" --env-file deploy/compose.env -f deploy/compose.yaml \
  exec -T postgres psql -U speechnexus -d speechnexus -Atc \
  "select table_name, column_name from information_schema.column_privileges \
   where grantee = 'sn_artifact_retention' and privilege_type = 'UPDATE' order by 1, 2"
```

该查询应只输出 `speech_artifacts|artifact_state` 与 `speech_artifacts|deleted_at_us` 两行：
前者是保留决策，后者是删除账本（清理确认后写入的移除时刻）。确认没有表级写权限：

```bash
"${COMPOSE[@]}" --env-file deploy/compose.env -f deploy/compose.yaml \
  exec -T postgres psql -U speechnexus -d speechnexus -Atc \
  "select table_name, privilege_type from information_schema.table_privileges \
   where grantee = 'sn_artifact_retention' and privilege_type in \
     ('INSERT','UPDATE','DELETE','TRUNCATE','REFERENCES','TRIGGER') order by 1, 2"
```

该查询应无输出。

迁移 `0054_artifact_deleted_ledger` 给 `speech_artifacts` 增加 `deleted_at_us` 作为持久
删除账本。**迁移前已经是 `deleted` 的行没有账本，也不可追溯**：表里只有发布时刻
`created_at_us` 与期限 `expires_at_us`，没有任何状态变更时刻（无 `updated_at_us`、无修订
列、无审计表），迁移因此不回填，历史行保留 `NULL`，一致性 CHECK 以 `NOT VALID` 安装——
只约束迁移之后的新写入。核对这段历史有多少行：

```bash
"${COMPOSE[@]}" --env-file deploy/compose.env -f deploy/compose.yaml \
  exec -T postgres psql -U speechnexus -d speechnexus -Atc \
  "select count(*) from speech_artifacts where artifact_state = 'deleted' and deleted_at_us is null"
```

该计数就是「迁移前删除、无账本」的行数。不要用 `now()`、`created_at_us` 或
`VALIDATE CONSTRAINT` 把它补成看起来完整的假时刻；如要补，先由运维确认这些行的真实移除
时刻来源。列注释同样写明了这一语义。

### 外部托管引擎的资格声明角色 `sn_operator_attestation`

生命周期由运维掌握、没有 node-controller 参与的外部引擎，其 builtin 声音资格由
`speechnexus-attest-builtin-voice` 在单个事务内写入一条 approval 与一条声明记录。
仓库授权函数 `operator_attestation_writer_grants` 以 `sn_operator_attestation` 为目标声明该
身份的全部授权：只读核对所需的来源批准、binding 与 release 证据、注册账本、资源池与工作区
配额，列级 `INSERT` 追加 approval，并追加声明记录；不持有任何表的
`UPDATE`/`DELETE`/`TRUNCATE`，也没有资格、发布或 publication head 的写权限。持有发布 head 权限的身份会被
`writer_identity_forbidden` 拒绝——重指 head 足以撤回一条公开资格，因此不能由声明身份持有。

这条路径默认关闭，也不被任何容器自动启用：命令在宿主执行，`deploy/compose.yaml` 不转发该键，
`deploy/compose.env.example` 只带口令占位。未导出连接地址时命令在连接前退出并点名变量，不会
把「没有 controller」读成「声明通过」：

```text
error: SPEECHNEXUS_OPERATOR_ATTESTATION_WRITER_DATABASE_URL is not set
```

宿主侧按 host 端口导出（容器内的 `postgres` 别名在宿主不可解析）：

```bash
export SPEECHNEXUS_OPERATOR_ATTESTATION_WRITER_DATABASE_URL="postgresql://sn_operator_attestation:${SPEECHNEXUS_OPERATOR_ATTESTATION_ROLE_PASSWORD}@127.0.0.1:${SPEECHNEXUS_POSTGRES_HOST_PORT:-55432}/${POSTGRES_DB}"
```

随后先记录声明、再发布（两者都要求私有 recipe 与 release 证据齐备）：

```bash
uv run speechnexus-attest-builtin-voice \
  --schema speech --workspace-id <workspace> \
  --trust-declaration-file /etc/speechnexus/operator-trust.json \
  --attestation-request-file /etc/speechnexus/operator-attestation.json \
  --artifact-pin <artifact-pin.json> --manifest <manifest.json> \
  --report <report.json> --scenario-evidence <scenario.json>
uv run speechnexus-publish-builtin-voice ...
```

发布后用**只读审计身份**核对来源。`sn_qualification_reader` 只持有
`qualification_reader_grants` 声明的读权限（已发布资格、批准、`provenance` 标签，以及支撑
标签的声明记录与 probe 授权），没有任何写权限；用 owner 跑这条审计等于让写者自己作证。
宿主侧按 host 端口导出该 DSN（容器内的 `postgres` 别名在宿主不可解析）：

```bash
export SPEECHNEXUS_QUALIFICATION_READER_DATABASE_URL="postgresql://sn_qualification_reader:${SPEECHNEXUS_QUALIFICATION_READER_ROLE_PASSWORD}@127.0.0.1:${SPEECHNEXUS_POSTGRES_HOST_PORT:-55432}/${POSTGRES_DB}"
uv run speechnexus-qualification-provenance --schema speech
```

`SPEECHNEXUS_QUALIFICATION_READER_DATABASE_URL` 是 host 侧键，本 Compose 不转发；命令以
`SET TRANSACTION READ ONLY` 打开事务，读失败退出码为 3，从不把「没读出来」当成资格结论。
`source_kind` 只有三种取值：`controller_observed`（必须带 `probe_authorization_id`）、
`operator_attested`（必须带 `attestation_id`）、`unverified`（两者都没有，显式标为来源未知，
不会因为缺少某行被算作控制器观测）。

信任声明文件（`speechnexus-operator-attested-engine-v1`）是部署方对「这些实例的生命周期由
运维掌握」的显式决定：目标安装、逐实例白名单、必须承认的能力缺口清单、单次声明时长上限
（≤30 天）。它是部署方私有输入，放在仓库外、权限 `0600`；仓库不提供默认声明，缺失即拒绝。
声明成功不等于引擎可服务：本路径不产生 native runtime 证据、停止证据、预算释放或实例生命
周期收敛，同步 TTS 仍要求执行网关与该实例的 live 策略一致。边界与具名错误码见
[builtin voice qualification](builtin-voice-qualification.md#外部托管引擎的-operator-声明资格)。

### 同步 TTS 资格的部署前置

§0–§7 装配的是**异步主链**与同步语音的基座。同步 TTS 要返回 `200`，还需要一组只由部署方
提供的事实：四件测量原件、信任声明与声明请求、三个受限身份，以及一个真实引擎。它们不是本
文件省略的步骤，也不由任何容器自动生成：`deploy/compose.yaml` 不转发这三条资格 DSN，角色
口令与连接串都留在宿主。参考部署预置的资格相关内容只有 `sn_operator_attestation`（写入声明）
与 `sn_qualification_reader`（只读审计）。因此在一套刚按 §0–§7 起好的栈上，同步 TTS 的稳定
结果是 `503 backend_unavailable`。那是尚未提供的部署输入，既不是这套栈上可以现场修好的
缺陷，也不是「本部署不支持 TTS」。

**四件测量原件。** `speechnexus-attest-builtin-voice`、`speechnexus-publish-builtin-voice` 与
`speechnexus-run-builtin-qualification` 读同一组原文件：artifact pin、
`capacity-manifest.json`、`qualification-report.json` 与 `scenario-evidence.json`。仓库的
`deploy/engines/file-tts-customvoice/` 只有 `README.md`、`artifact-pin.json`、
`binding-definition.json`、`runtime-pin.json`、`template.example.json` 与
`warmup-request.json`，没有这三份测量原件：它们只能由部署方在自己的 GPU 窗口测得，manifest
的字段、产出与核对方式见 [capacity manifest](capacity-manifest.md)。

**信任声明与声明请求。** 两份文件都是部署方的显式决定，不是命令能自行推导的输入：
`speechnexus-operator-attested-engine-v1` 声明目标 installation、逐实例白名单、能力缺口与
声明时长上限，`speechnexus-operator-attested-qualification-v1` 记录 voice/revision、binding
与 revision、instance 与 generation、fencing、模板、镜像与模型制品摘要。格式与具名拒绝码见
[外部托管引擎的资格声明角色](#外部托管引擎的资格声明角色-sn_operator_attestation)。

**三个受限身份。** 由数据库 owner 按部署自己的评审结论创建，普通业务角色不得继承，也不得
获得审批或发布写权限：

| 身份 | 授权声明 | 服务入口 |
| --- | --- | --- |
| `sn_qualification_admission` | `public_job_admission_grants` + `qualification_admission_grants` | `speechnexus-run-builtin-qualification`：正常完整配方预算准入，加 probe 授权、准入与终止证据的受限读写 |
| `sn_builtin_source_approval` | `builtin_source_approval_writer_grants` | `speechnexus-approve-builtin-source`：只追加来源批准 |
| `sn_builtin_voice_publisher` | `publication_grants` + `builtin_publisher_grants` | `speechnexus-publish-builtin-voice`：写资格、`provenance`、publication 与 publication head |

三条命令的连接串按 host 端口导出，与 `sn_operator_attestation` 同一条路径（本 Compose 不
转发）：`SPEECHNEXUS_QUALIFICATION_ADMISSION_DATABASE_URL`、
`SPEECHNEXUS_SOURCE_APPROVAL_WRITER_DATABASE_URL` 与 `SPEECHNEXUS_PUBLICATION_WRITER_DATABASE_URL`。
`speechnexus-run-builtin-qualification` 的 `--issue` 与 `--approval-id` 另用独立资格 writer
身份（`qualification_writer_grants`，`SPEECHNEXUS_QUALIFICATION_WRITER_DATABASE_URL`），
不在这三个之内；写审批资格与发布权限分开是刻意的，见
[builtin voice qualification](builtin-voice-qualification.md)。

**真实引擎。** 执行网关私有 TTS 端点配置的 `address`/`port` 必须是 Compose 网络可达的真实
引擎，`bearer_token` 必须是该引擎的 `VLLM_API_KEY`；
`deploy/private/gateway-file-tts-endpoint.example.json` 的 `10.0.0.11:18101` 与 64 个 0 只是
形状占位，直接使用会得到引擎的 `401`。

**缺什么 → 观察到什么 → 怎么补：**

| 缺的输入 | 观察到的现象 | 怎么补 |
| --- | --- | --- |
| 四件测量原件 | 两条 CLI 在任何写入前退出：`error: invalid attestation input: attestation_request_invalid`（退出码 `2`）与 `invalid private qualification input`（退出码 `2`） | 在真实 GPU 窗口按 [capacity manifest](capacity-manifest.md) 与 [qualification report](qualification-report.md) 测出原件，放仓库外、权限 `0600` |
| 信任声明或声明请求 | 声明缺失为 `error: trust_declaration_missing`（退出码 `4`）；请求原件不合规同样是 `attestation_request_invalid`（退出码 `2`）。两者都零写入 | 按上文的声明格式准备两份文件，交由部署方评审并签署 |
| 三个受限身份 | `pg_roles` 里没有这三个角色，资格事实表全为 `0`；`speechnexus-qualification-provenance` 输出全 `0`，`controller_observed` 与 `operator_attested` 皆无 | 建三个非 owner 角色并重放上表各自的授权声明，再按 host 端口导出连接串 |
| 已发布且合格的内建声音 | 同步 TTS `503 backend_unavailable`，API 日志为 `Synchronous speech unavailable; no current qualified voice`（`backend/src/speechnexus_backend/speech_runtime.py:100`） | 先写来源批准与资格（控制器探针或 operator 声明），再用 `speechnexus-publish-builtin-voice` 发布 |
| 真实引擎地址与 token | 照抄样例的 `10.0.0.11:18101` 与 64 个 0，引擎的 `/v1/models` 与 `/v1/audio/speech` 都回 `401`；换成真实地址与 `VLLM_API_KEY` 后两者都 `200` | 换成本部署引擎的地址、端口与该引擎的 `VLLM_API_KEY` |

补齐顺序是身份与原件 → 来源批准 → 资格（控制器探针或 operator 声明）→ 发布 → 一次真实请求。
五者齐备之前不会出现同步 TTS 的 `200`：[§11](#11-验证边界) 已把公开 TTS 排除在本 runbook 的
本地验证范围之外，此处说明的正是它要求另取证的那部分。

### 预设发布资格输入与 sn_profile_writer 角色

`POST /v1/nexus/admin/profiles/{profile_id}/revisions/{revision}/publications` 在写入前只
接受一个只读资格端口的观察结果。该端口要求把目标 revision 的**定义字节**与调用方提交的
**校验报告字节**同时绑定到部署方评审过的记录上。仓库不内置、也不生成这种资格：默认
`SPEECHNEXUS_PROFILE_WRITER_DATABASE_URL` 为空时整条发布路径返回
`503 profile_result_unknown`，属于「能力未启用」；一旦启用 writer，writer 与资格文档必须
同时配置，缺任一输入或文档不可读、格式非法时 API 在启动阶段点名缺失的变量并退出，不会等
到发布时才回 503。资格文档是部署方的评审记录，代码只做字节绑定，不证明真实引擎、模型
质量、容量或物理拓扑。

**a. 生成写角色口令并写回 DSN。** `sn_profile_writer` 是独立非 owner 身份，不得复用
`sn_api`、迁移 owner 或任一 gateway/worker 角色：

```bash
umask 077
export SPEECHNEXUS_PROFILE_WRITER_ROLE_PASSWORD="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
python3 - <<'PY'
import os
import re
from pathlib import Path

url = (
    "postgresql://sn_profile_writer:"
    + os.environ["SPEECHNEXUS_PROFILE_WRITER_ROLE_PASSWORD"]
    + "@postgres:5432/speechnexus"
)
path = Path("deploy/compose.env")
text = path.read_text()
line = "SPEECHNEXUS_PROFILE_WRITER_DATABASE_URL=" + url
pattern = r"(?m)^SPEECHNEXUS_PROFILE_WRITER_DATABASE_URL=.*$"
text = re.sub(pattern, line, text, count=1) if re.search(pattern, text) else text + line + "\n"
path.write_text(text)
path.chmod(0o600)
print("profile writer DSN written to deploy/compose.env")
PY
```

DSN 沿用示例的 `postgres` 服务名与 `speechnexus` 库名；改了 Compose 服务名、端口或库名
时同步修改。

**b. 建角色并重放两个授权函数。** 先把数据库迁移到 §5 渲染的唯一 head（发布表来自
`0033_profile_publications`，退休列来自 `0050_profile_retirements`），再用迁移 owner
连接创建角色并重放 `profile_draft_grants` 与 `profile_publication_grants`：

```bash
set -a
. deploy/compose.env
set +a
export SN_ADMIN_DSN="postgresql://${POSTGRES_USER}:${POSTGRES_PASSWORD}@127.0.0.1:${SPEECHNEXUS_POSTGRES_HOST_PORT:-55432}/${POSTGRES_DB}"
uv run --all-packages python - <<'PY'
import os

import psycopg
from psycopg import sql
from speechnexus_backend.speech.infrastructure.profile_draft_schema import profile_draft_grants
from speechnexus_backend.speech.infrastructure.profile_publication_schema import (
    profile_publication_grants,
)

schema, role = os.environ.get("SPEECHNEXUS_DATABASE_SCHEMA", "speech"), "sn_profile_writer"
with psycopg.connect(os.environ["SN_ADMIN_DSN"], autocommit=True) as conn:
    existing = conn.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (role,)).fetchone()
    if existing is None:
        conn.execute(
            sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                sql.Identifier(role),
                sql.Literal(os.environ["SPEECHNEXUS_PROFILE_WRITER_ROLE_PASSWORD"]),
            )
        )
    for statement in (
        *profile_draft_grants(schema, role),
        *profile_publication_grants(schema, role),
    ):
        conn.execute(statement)
print("profile writer role and grants ready: " + role)
PY
```

角色已存在时沿用其现有口令，脚本不改写口令。新装与既有部署都用同一段重放：`GRANT` 是
追加式操作，重复执行不改变既有权限；不要手工增删表级、列级授权，API 启动会逐列核对实际
ACL 与端口声明面，多给或少给都以 `Profile writer startup rejected` 拒绝启动。

**c. 生成已评审发布证据文档。** 先从目标部署取该 revision 已存的定义，并把评审过的校验
报告保存为 JSON 文件（内容必须与发布请求体里的 `validation` 完全相同），再由下面的脚本
计算两个摘要并写出文档。文档放在只读私有目录（§3 的 `deploy/.runtime/private`），权限
`0600`：

```bash
mkdir -p deploy/.runtime/private
uv run --all-packages python - <<'PY'
import json
import os
from pathlib import Path

import psycopg
from psycopg import sql
from speechnexus_backend.speech.application.profile_documents import encode_document
from speechnexus_backend.speech.domain.configuration import document_digest

identity, revision = os.environ["PROFILE_ID"], os.environ["PROFILE_REVISION"]
validation = json.loads(Path(os.environ["PROFILE_VALIDATION_FILE"]).read_text())
schema = os.environ.get("SPEECHNEXUS_DATABASE_SCHEMA", "speech")
query = sql.SQL(
    "SELECT definition_json FROM {}.speech_profile_revisions "
    "WHERE profile_id=%s AND revision=%s"
).format(sql.Identifier(schema))
with psycopg.connect(os.environ["SN_ADMIN_DSN"]) as conn:
    row = conn.execute(query, (identity, revision)).fetchone()
if row is None:
    raise SystemExit("no stored revision for " + identity + "/" + revision)
document = {
    "format": "speechnexus.profile-publication-evidence/1",
    "entries": [
        {
            "profile_id": identity,
            "revision": revision,
            "definition_digest": document_digest(row[0].encode()),
            "validation_digest": document_digest(encode_document(validation)),
            "reviewed_by": os.environ["PROFILE_REVIEWER"],
            "reviewed_at": os.environ["PROFILE_REVIEWED_AT"],
        }
    ],
}
target = Path("deploy/.runtime/private/profile-publication-evidence.json")
if target.exists():
    raise SystemExit("evidence document already exists; review before replacing it")
target.write_text(json.dumps(document, indent=2) + "\n")
target.chmod(0o600)
print("wrote " + str(target))
PY
```

`PROFILE_ID`、`PROFILE_REVISION` 是发布目标，`PROFILE_VALIDATION_FILE` 是评审过的校验报告
JSON，`PROFILE_REVIEWER` 是评审者标签（1..256 字符），`PROFILE_REVIEWED_AT` 是 ISO 8601
时间（带时区）。没有评审过任何 revision 时写
`{"format": "speechnexus.profile-publication-evidence/1", "entries": []}`：这是显式声明
「暂无可发布 revision」，草稿与固定读取仍可用，发布被稳定拒绝而不是 503。文档只在 API
启动时读取一次，内容变化后必须重启 API。

**d. 写回文档路径并重启 API。** 容器内路径固定位于只读私有目录：

```bash
python3 - <<'PY'
import re
from pathlib import Path

line = "SPEECHNEXUS_PROFILE_PUBLICATION_EVIDENCE_FILE=/run/speechnexus-private/profile-publication-evidence.json"
path = Path("deploy/compose.env")
text = path.read_text()
pattern = r"(?m)^SPEECHNEXUS_PROFILE_PUBLICATION_EVIDENCE_FILE=.*$"
text = re.sub(pattern, line, text, count=1) if re.search(pattern, text) else text + line + "\n"
path.write_text(text)
path.chmod(0o600)
print("publication evidence path written to deploy/compose.env")
PY
"${COMPOSE[@]}" --env-file deploy/compose.env -f deploy/compose.yaml up -d api
```

只写 writer DSN 而不写文档路径，或路径指向不可读、格式非法的文档时，API 启动失败并点名
变量，容器不进入可服务状态；写了文档路径而没有 writer DSN 同样启动失败，并点名
`SPEECHNEXUS_PROFILE_WRITER_DATABASE_URL`（两者必须成对出现）：

```text
ValueError: SPEECHNEXUS_PROFILE_PUBLICATION_EVIDENCE_FILE is required when
SPEECHNEXUS_PROFILE_WRITER_DATABASE_URL enables the profile writer: publication authority
is deployment-owned reviewed evidence, and an unwired authority would answer every
publication with 503 profile_result_unknown
```

这是刻意的 fail-closed：配置错误在启动时暴露，不会伪装成发布时的 503。

**e. 发布一次并核对结果。** 请求体与评审时的 `validation` 完全相同，带显式
`Idempotency-Key`：

```bash
curl -sS -w '\nhttp=%{http_code}\n' -X POST \
  "http://127.0.0.1:8000/v1/nexus/admin/profiles/${PROFILE_ID}/revisions/${PROFILE_REVISION}/publications" \
  -H 'content-type: application/json' \
  -H 'idempotency-key: profile-publication-1' \
  --data @deploy/.runtime/private/publication-request.json
```

预期 `http=200`，响应 `publication.status` 为 `published` 且 `published_at` 非空；同键重放
返回同一结果，换键重发已发布 revision 返回 `409 profile_already_published`。目标未出现在
证据文档中、定义摘要或校验报告摘要不一致时返回 `422 profile_not_eligible`，拒绝在写入发布
事实之前完成（只落一条不可变拒绝回执），不是 503。

### 已有部署的授权升级

上面的脚本只适用于新建、独占的数据库；已有部署不重跑它的撤权与建角色部分。应用新迁移
后，用同一个迁移 owner 连接对既有角色重放对应的授权函数即可，`GRANT` 是追加式操作，
重复执行不改变既有权限。`0049_workspace_commands` 之后新增的工作区写、审计读取、实例读取、
隔离释放，以及本节新增的公开目录、Artifact、资源池、binding 列表、binding 写、
binding admin 读取、binding 生命周期、同步 TTS/ASR 结算、Artifact 保留清理、外部托管引擎资格声明
、资格来源审计读取与工作台运行/清理两个身份共十七个角色在既有部署上同样需要预置：下面的脚本先按需创建这十七个
角色，再重放全部相关授权。工作台授权针对工作台
命名空间，由 §6 的 `speechnexus-workbench-migrate` 幂等重放，不在本脚本内。角色尚不存在时，
先用 §6 的口令步骤生成口令（该步只填空值和占位值，已有随机值原样保留）；角色已存在时用其
现有口令，脚本不会修改已存在角色的口令，用新生成的口令写连接地址会连不上。实例
读取、资源池读取与 binding 列表读取三个面按上文的显式 `GRANT` 重放，因为它们没有对应的
仓库授权函数；隔离释放面按 `quarantine_release_grants` 重放，binding 生命周期面按
`binding_lifecycle_grants` 与 `binding_publication_grants` 两份声明一起重放。

```bash
uv run --all-packages python - <<'PY'
import os

import psycopg
from psycopg import sql
from speechnexus_backend.speech.infrastructure.artifact_schema import (
    artifact_reader_grants,
    artifact_retention_grants,
)
from speechnexus_backend.speech.infrastructure.binding_draft_schema import binding_draft_grants
from speechnexus_backend.speech.infrastructure.binding_publication_evidence_schema import (
    binding_admin_reader_grants,
)
from speechnexus_backend.speech.infrastructure.gateway_abort_intent_schema import (
    gateway_abort_grants,
)
from speechnexus_backend.speech.infrastructure.gateway_query_grants import (
    gateway_query_grants,
)
from speechnexus_backend.speech.infrastructure.publication_lifecycle_schema import (
    audit_reader_grants, binding_lifecycle_grants, binding_publication_grants,
)
from speechnexus_backend.speech.infrastructure.publication_schema import (
    model_catalog_reader_grants,
)
from speechnexus_backend.speech.infrastructure.quarantine_release_schema import (
    quarantine_release_grants,
)
from speechnexus_backend.speech.infrastructure.binding_disable_targets_schema import (
    disable_settlement_grants,
)
from speechnexus_backend.speech.infrastructure.builtin_voice_grants import (
    operator_attestation_writer_grants,
    qualification_reader_grants,
)
from speechnexus_backend.speech.infrastructure.settlement_worker_verifier import (
    settlement_worker_grants,
)
from speechnexus_backend.speech.infrastructure.qualified_speech_facts_postgres import (
    speech_synthesis_reader_grants,
)
from speechnexus_backend.speech.infrastructure.asr_settlement_schema import (
    asr_settlement_writer_grants,
)
from speechnexus_backend.speech.infrastructure.workspace_schema import workspace_writer_grants

schema = "speech"
new_roles = {
    "sn_workspace_writer": os.environ["SPEECHNEXUS_WORKSPACE_WRITER_ROLE_PASSWORD"],
    "sn_audit_reader": os.environ["SPEECHNEXUS_AUDIT_READER_ROLE_PASSWORD"],
    "sn_model_instance_reader": os.environ["SPEECHNEXUS_MODEL_INSTANCE_READER_ROLE_PASSWORD"],
    "sn_quarantine_release": os.environ["SPEECHNEXUS_QUARANTINE_RELEASE_ROLE_PASSWORD"],
    "sn_catalog_reader": os.environ["SPEECHNEXUS_CATALOG_READER_ROLE_PASSWORD"],
    "sn_artifact_reader": os.environ["SPEECHNEXUS_ARTIFACT_READER_ROLE_PASSWORD"],
    "sn_resource_pool_reader": os.environ["SPEECHNEXUS_RESOURCE_POOL_READER_ROLE_PASSWORD"],
    "sn_binding_list_reader": os.environ["SPEECHNEXUS_BINDING_LIST_READER_ROLE_PASSWORD"],
    "sn_binding_writer": os.environ["SPEECHNEXUS_BINDING_WRITER_ROLE_PASSWORD"],
    "sn_binding_admin_reader": os.environ["SPEECHNEXUS_BINDING_ADMIN_READER_ROLE_PASSWORD"],
    "sn_binding_lifecycle": os.environ["SPEECHNEXUS_BINDING_LIFECYCLE_ROLE_PASSWORD"],
    "sn_artifact_retention": os.environ["SPEECHNEXUS_ARTIFACT_RETENTION_ROLE_PASSWORD"],
    "sn_operator_attestation": os.environ["SPEECHNEXUS_OPERATOR_ATTESTATION_ROLE_PASSWORD"],
    "sn_qualification_reader": os.environ["SPEECHNEXUS_QUALIFICATION_READER_ROLE_PASSWORD"],
    "sn_speech_settlement": os.environ["SPEECHNEXUS_SPEECH_SETTLEMENT_ROLE_PASSWORD"],
    # The workbench unit's roles are created here too; their grants live in the
    # workbench namespace and are replayed idempotently by
    # `speechnexus-workbench-migrate` (§6 独立 Workbench API 部署单元).
    "sn_workbench_runtime": os.environ["SPEECHNEXUS_WORKBENCH_RUNTIME_ROLE_PASSWORD"],
    "sn_workbench_cleanup": os.environ["SPEECHNEXUS_WORKBENCH_CLEANUP_ROLE_PASSWORD"],
}
with psycopg.connect(os.environ["SN_ADMIN_DSN"], autocommit=True) as conn:
    for role, password in new_roles.items():
        if conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone():
            continue
        conn.execute(
            sql.SQL("CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT")
            .format(sql.Identifier(role))
        )
        conn.execute(
            sql.SQL("ALTER ROLE {} PASSWORD {}").format(
                sql.Identifier(role), sql.Literal(password)
            )
        )
    for statement in gateway_abort_grants(schema, "sn_gateway"):
        conn.execute(statement)
    for statement in gateway_query_grants(schema, "sn_gateway_query"):
        conn.execute(statement)
    for statement in workspace_writer_grants(schema, "sn_workspace_writer"):
        conn.execute(statement)
    for statement in audit_reader_grants(schema, "sn_audit_reader"):
        conn.execute(statement)
    for statement in quarantine_release_grants(schema, "sn_quarantine_release"):
        conn.execute(statement)
    for statement in model_catalog_reader_grants(schema, "sn_catalog_reader"):
        conn.execute(statement)
    # The retention sweep is its own identity: SELECT plus
    # UPDATE(artifact_state, deleted_at_us) only, and it refuses to start on
    # anything wider.
    for statement in artifact_retention_grants(schema, "sn_artifact_retention"):
        conn.execute(statement)
    # An operator-attested qualification is declared by its own minimal identity:
    # column-level INSERT on the approval ledger plus one attestation row, no
    # UPDATE/DELETE anywhere and no publication-head authority.
    for statement in operator_attestation_writer_grants(schema, "sn_operator_attestation"):
        conn.execute(statement)
    for statement in qualification_reader_grants(schema, "sn_qualification_reader"):
        conn.execute(statement)
    # Synchronous TTS: the api identity needs the same voice/binding reads plus the
    # settlement root write, and the settlement runtime its own exact surface. Both
    # are replayed here so an upgraded deployment reaches the same ACL as a fresh one.
    for statement in speech_synthesis_reader_grants(schema, "sn_api"):
        conn.execute(statement)
    for statement in asr_settlement_writer_grants(schema, "sn_api"):
        conn.execute(statement)
    for statement in settlement_worker_grants(schema, "sn_speech_settlement"):
        conn.execute(statement)
    for statement in artifact_reader_grants(schema, "sn_artifact_reader"):
        conn.execute(statement)
    for statement in binding_draft_grants(schema, "sn_binding_writer"):
        conn.execute(statement)
    for statement in binding_admin_reader_grants(schema, "sn_binding_admin_reader"):
        conn.execute(statement)
    for statement in (
        *binding_lifecycle_grants(schema, "sn_binding_lifecycle"),
        *binding_publication_grants(schema, "sn_binding_lifecycle"),
    ):
        conn.execute(statement)
    conn.execute(
        sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(
            sql.Identifier(schema), sql.Identifier("sn_model_instance_reader")
        )
    )
    conn.execute(
        sql.SQL("GRANT SELECT ON {}.engine_instances, {}.speech_binding_revisions TO {}")
        .format(
            sql.Identifier(schema),
            sql.Identifier(schema),
            sql.Identifier("sn_model_instance_reader"),
        )
    )
    conn.execute(
        sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(
            sql.Identifier(schema), sql.Identifier("sn_resource_pool_reader")
        )
    )
    conn.execute(
        sql.SQL("GRANT SELECT ON {}.engine_instances, {}.resource_pools TO {}")
        .format(
            sql.Identifier(schema),
            sql.Identifier(schema),
            sql.Identifier("sn_resource_pool_reader"),
        )
    )
    conn.execute(
        sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(
            sql.Identifier(schema), sql.Identifier("sn_binding_list_reader")
        )
    )
    conn.execute(
        sql.SQL('GRANT SELECT ON {}.speech_binding_families, {}.speech_binding_revisions, '
                '{}.speech_publications, {}.speech_publication_heads, '
                '{}.speech_publication_retirements TO {}')
        .format(
            sql.Identifier(schema),
            sql.Identifier(schema),
            sql.Identifier(schema),
            sql.Identifier(schema),
            sql.Identifier(schema),
            sql.Identifier("sn_binding_list_reader"),
        )
    )
print(
    "gateway, workspace writer, audit reader, instance reader, quarantine release, "
    "catalogue reader, Artifact reader, resource pool reader, binding list reader, "
    "binding writer, binding admin reader, binding lifecycle, artifact retention and "
    "operator attestation grants refreshed"
)
PY
```

两份 gateway 授权须同时重放。查询角色的列集由网关按精确集合校验，漏掉
`gateway_query_grants` 时控制面以 `invalid execution query reader authority` 拒绝启动；
漏掉 `gateway_abort_grants` 新增的尝试列时启动继续，但持久取消接收能力被关闭
（fail-soft），网关不再接收新的取消。升级顺序：先迁移，再重放授权，最后启动
服务；启动日志出现上述拒绝时停止服务并核对授权，不带病运行。工作区写、审计读取、实例读取
与隔离释放的授权同样在迁移后重放：漏掉 `workspace_writer_grants` 时 API 以
`Workspace writer startup rejected` 拒绝启动，漏掉 `audit_reader_grants` 时以
`Audit reader startup rejected` 拒绝启动，漏掉实例读取的两张表 `SELECT` 时以
`Model instance reader startup rejected` 拒绝启动，漏掉 `quarantine_release_grants` 或
启动期序列判定未通过时以 `Quarantine release startup rejected` 拒绝启动，拒绝信息同时
点名不符的关系、列或序列；四个角色未配置连接地址时对应端点保持 503。

可选的取消债务/派发债务探针身份不在上面的重放列表内：它没有服务启动校验，也没有独立
端点。配置了 `SPEECHNEXUS_READINESS_CANCELLATION_DSN` 的部署要另以 owner 对该探针角色
重跑 `cancellation_debt_reader_grants`，账本授权为四列（`job_id`、`settled_at_us`、
`last_outcome`、`next_check_at_us`）。漏跑时探针整体退化为
`cancellation_debt: unavailable (permission)`，`/readyz` 不会因此失败；`GRANT` 对下一次
查询即生效，同一 DSN/角色的探针下一轮轮询就恢复，不需要重启 API。降级形态与三个账本
标记见
[production-jobs.md 的可见面](../components/production-jobs.md#隔离不了的时候可见面与处置)，
此处不重复定义。

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

### 独立 Workbench API 部署单元 `workbench-api`

参考部署以 `workbench-api` profile 装配独立工作台后端：`workbench-migrate` 一次性执行
工作台命名空间的 owner 迁移并授予两个专用非 owner 角色，`workbench-api` 是前端
`/v1`、`/v2/workbench/*` 代理指向的真实服务，入口为
`deploy/workbench-startup-gate.py`。它与 `web` profile 是两件事：`web` 只起两个前端，
前端在 `WEB_WORKBENCH_UPSTREAM` 上的请求由本单元回答。

工作台拥有自己的 PostgreSQL 命名空间（`SPEECHNEXUS_WORKBENCH_DATABASE_SCHEMA`，默认
`workbench`），与 `speech` 分开。它的 DDL 由
`backend/src/speechnexus_backend/workbench/infrastructure/runtime_schema.py` 组装，只在
owner 事务里对新命名空间执行一次，不进入 `speech` 的 Alembic 链，也不在运行期建表。两个
身份各自只有一个授权声明：

| 角色 | 授权声明 | 权限面 |
| --- | --- | --- |
| `sn_workbench_runtime` | `runtime_grants_sql` | schema USAGE 加 `TABLES` 声明的每张表的 SELECT/INSERT，加 `_UPDATE_COLUMNS` 声明的逐列 UPDATE；`deletion_ledger`、`erased_mutation_receipts`、`runtime_schema_revision` 只读 |
| `sn_workbench_cleanup` | `cleanup_grants_sql` | 业务行 DELETE、删除账本与擦除回执 INSERT、`deletion_outbox` 两列 UPDATE |

身份边界是可执行的，不是部署约定：`workbench-api` 启动时由 `check_database` 核对运行身份
的登录身份、owner/DDL/DML、逐表与逐列权限面、`runtime_schema_revision`，由
`check_cleanup_identity` 核对清理身份不是 owner 或特权角色。两个身份受同样两条边界约束：
在工作台命名空间之外持有任何关系或序列权限会被点名拒绝，判定范围包含身份能 `SET ROLE` 到达
的每个角色——普通的 `GRANT` 靠继承就能看见，而 `GRANT wider TO role WITH INHERIT FALSE`
对任何针对登录身份的权限判定都不可见，却仍能 `SET ROLE wider` 取到那份权威，所以扫描按
「能到达的权威」判定，并点名越界授权实际挂在哪个角色上。成员关系本身不禁止：只授予工作台
命名空间权限的组角色是正常部署形态，边界是可达权威而不是成员关系的有无。因此把任一 DSN
指向 `sn_api` 这类宽身份都不能启动：运行 DSN 由 `check_database` 拒绝，清理 DSN 由
`check_cleanup_identity` 拒绝，两者都点名越界的关系。

迁移与启动：

```bash
"${COMPOSE[@]}" --env-file deploy/compose.env -f deploy/compose.yaml \
  --profile workbench-api run --rm workbench-migrate
"${COMPOSE[@]}" --env-file deploy/compose.env -f deploy/compose.yaml \
  --profile web --profile workbench-api up -d
```

`workbench-migrate` 对每一步打印一行 `OK: ...`（命名空间、版本、两份授权、授权后复核），
任一步不成立时打印 `FAIL: <具名条件>` 并以非零退出；`depends_on` 的
`service_completed_successfully` 保证 API 不会在没有迁移的命名空间上启动。对已迁移的命名
空间重跑只核对 `runtime_schema_revision` 与重放两份授权（幂等），不改写结构、不回填版本。
`workbench-api` 的入口闸门先核验必需输入（运行/清理 DSN、workspace、deployment、cursor
key、schema）与上述权限面，全部成立才打印 `OK:` 并 exec ASGI 工厂。以下条件在启动前以
`FAIL:` 点名，不会带着缺失配置起服务：空白键、schema 写成 `speech`、两个 DSN 指向同一角色、
越界的 schema 授权（点名关系，以及能 `SET ROLE` 到达该授权的角色）。两种失败只给结论不给
分量：身份是 schema owner 或带特权标志时，闸门打印 `… unavailable or unsafe.` 后退出，不指出
是哪个标志，因为此时要做的不是逐项修补而是换掉这个身份；迁移步的 owner DSN 若不能建 schema
或不是命名空间 owner，则以 `FAIL: the owner connection could not complete the workbench
migration (<异常类型>)` 退出，具名的是要检查的两个输入而不是具体的 SQL 权限。owner、运行、
清理三个 DSN 指向同一角色这类矛盾由 `workbench-migrate` 在连接前点名（闸门拿不到 owner
DSN）。健康检查打 `/v1/workbench/status`，它只在真实 lifespan 预检通过后才可答。

本单元装配的是工作台业务后端：项目、稿件、会议、提词器、书稿/章节以及删除的
预检→确认→账本。它默认不配置语音底座地址、对象区与内网 LLM，因此状态接口按部署事实报告
能力——`teleprompter` 为 `available`，转写、助手、纪要与有声书导出为 `disabled`。绑定语音
底座、保存/下载与纪要生成需要部署方另外提供 `SPEECHNEXUS_WORKBENCH_SPEECH_BASE_URL`、
对象区四键与 `SPEECHNEXUS_WORKBENCH_LLM_*`；缺失时对应能力保持 `disabled`，不自行猜默认值。

### 透传 DSN 覆盖表

`deploy/compose.yaml` 透传的全部 29 个 `*_DATABASE_URL` 键按下表处置：要么本文有可执行的
预置步骤，要么在[故意不预置的身份](#故意不预置的身份)里写明原因与「部署方怎么判断它已经
启用」。`未配置时的实际行为` 一列只写未配置时可观察到的稳定结果——渲染期失败、具名启动
拒绝、或固定的 HTTP 状态与错误码，不写无法从报文核对的「能力未启用」。有行关闭的是叠加
能力而不是基线写入，那一行按实测写出真正发生的事，不用统一的失败形态去凑。

本表由 `tests/ci/test_reference_deploy_dsn_coverage.py` 守护：新增透传键既没有步骤也不在
清单里、删掉某个已预置身份的步骤、把清单项删掉、把某行的身份改成脚本不创建的角色、把端点
改成契约里不存在的路径、把失败码换成别的能力真实存在的码、把 DSN 写回里的「角色↔口令变量」
换错、或改掉某个计数，都会让它变红。`守护` 列只表示「另有哪个用例钉住该行的同一事实」：

- `覆盖`：本表用例，逐行核对键集合、身份、端点、失败码与写回配对；
- `角色`：`tests/ci/test_compose_minimal_role_provisioning.py`，核对本行所属角色的授权
  声明与角色口令来源；故意不预置的行没有角色，因此只有 `覆盖`；
- `Job`：`tests/ci/test_reference_deploy_job_prereqs.py`，只出现在 Job 三键行；
- `发布`：`tests/ci/test_reference_deploy_profile_publication.py`，只出现在
  `sn_profile_writer` 行。

「表中没有死键」的判定范围是 `backend/src`、`execution-gateway/src` 与 `deploy/*.py` 里的
读取位置：settings 字段（含 `env_prefix` 组合）、`Field`/`validation_alias` 别名、
`os.environ[...]`/`os.environ.get(...)`/`os.getenv(...)` 字面，以及绑定到这类名字后再被
下标或 `.get` 读取的模块常量。`scripts/`、`tests/`、`web/` 不在扫描范围内：那里的名字是
夹具、运维命令或前端字段，不能证明产品侧有消费者，计入会把死键判活。新增的透传键若只被
`scripts/` 消费会被判为死键——这是需要人工判定并补读取位置或改写本段的信号。

| 键 | 身份 | 端点 / 用途 | 未配置时的实际行为 | 处置 | 守护 |
| --- | --- | --- | --- | --- | --- |
| `SPEECHNEXUS_API_DATABASE_URL` | `sn_api` | 同步转写、资产与工作区读取的基线身份（容器内名为 `SPEECHNEXUS_DATABASE_URL`） | 渲染期必填：缺值 `docker compose config` 失败 | 预置 | 覆盖+角色 |
| `SPEECHNEXUS_GATEWAY_CONTROL_DATABASE_URL` | `sn_gateway` | 执行网关控制面写入 | 渲染期必填：缺值 `docker compose config` 失败 | 预置 | 覆盖+角色 |
| `SPEECHNEXUS_GATEWAY_CONTROL_QUERY_DATABASE_URL` | `sn_gateway_query` | 执行网关恢复只读查询 | 渲染期必填：缺值 `docker compose config` 失败 | 预置 | 覆盖+角色 |
| `SPEECHNEXUS_MIGRATION_DATABASE_URL` | bootstrap owner | `migration` profile 的一次性迁移 | 渲染期必填；生产须换独立 operator 凭据 | 预置 | 覆盖 |
| `SPEECHNEXUS_REGISTRATION_DATABASE_URL` | bootstrap owner | `engine-registration` profile 的一次性注册 | 渲染期必填；生产须换独立 operator 凭据 | 预置 | 覆盖 |
| `SPEECHNEXUS_PROFILE_WRITER_DATABASE_URL` | `sn_profile_writer` | Profile 发布 | `503 profile_result_unknown`；配了 DSN 却缺证据文档时启动失败 | 预置 | 覆盖+发布 |
| `SPEECHNEXUS_WORKSPACE_WRITER_DATABASE_URL` | `sn_workspace_writer` | `POST/PATCH /v1/nexus/workspaces` | `503 workspace_service_unavailable` | 预置 | 覆盖+角色 |
| `SPEECHNEXUS_AUDIT_READER_DATABASE_URL` | `sn_audit_reader` | `GET /v1/nexus/audit-events` | `503 audit_service_unavailable` | 预置 | 覆盖+角色 |
| `SPEECHNEXUS_MODEL_INSTANCE_READER_DATABASE_URL` | `sn_model_instance_reader` | admin 模型实例读取 | `503 model_instance_service_unavailable` | 预置 | 覆盖+角色 |
| `SPEECHNEXUS_QUARANTINE_RELEASE_DATABASE_URL` | `sn_quarantine_release` | 隔离释放事务 | `503 quarantine_release_unavailable` | 预置 | 覆盖+角色 |
| `SPEECHNEXUS_JOB_WRITER_DATABASE_URL` | `sn_job_writer` | Job 创建/读取与 voice 草稿 | 空值 → `503 job_unavailable`（创建与读取都不可用） | 预置 | 覆盖+Job |
| `SPEECHNEXUS_JOB_ADMISSION_DATABASE_URL` | `sn_job_admission` | 原子准入 | 空值 → Job 创建 `503 job_unavailable`（准入未装配） | 预置 | 覆盖+Job |
| `SPEECHNEXUS_JOB_DISPATCH_DATABASE_URL` | `sn_job_dispatch` | `job-dispatch` profile 派发 | 空值 → 启动闸门点名拒绝启动，不领取 Job | 预置 | 覆盖+Job |
| `SPEECHNEXUS_DISABLE_DATABASE_URL` | `sn_disable_scan` | `binding-disable` profile 扫描 | 空值 → worker 启动即退出并点名显式连接 | 预置 | 覆盖 |
| `SPEECHNEXUS_DISABLE_SETTLEMENT_DATABASE_URL` | `sn_disable_settle` | 停用租约结算 | 空值 → worker 启动即退出并点名显式连接（与扫描角色同一条拒绝） | 预置 | 覆盖 |
| `SPEECHNEXUS_SPEECH_SETTLEMENT_DATABASE_URL` | `sn_speech_settlement` | 同步 TTS 与同步 ASR 的结算运行时（独立连接池）：租约结算的列级读写、`speech_tts_settlement`/`speech_tts_cancellation` 与 `speech_asr_settlement` 的结算面 | 空值 → 同步 TTS 与同步 ASR 均不可用：转写路由 `503` 并点名该键；配了 DSN 但角色授权与声明不符时启动拒绝服务 | 预置 | 覆盖+角色 |
| `SPEECHNEXUS_MODEL_CATALOG_READER_DATABASE_URL` | `sn_catalog_reader` | `GET /v1/models`、`GET /v1/models/{model}`、`GET /v1/nexus/voices` | `503 model_catalogue_unavailable`、`503 voice_catalogue_unavailable` | 预置 | 覆盖+角色 |
| `SPEECHNEXUS_ARTIFACT_READER_DATABASE_URL` | `sn_artifact_reader` | Artifact 元数据与内容 | `503 artifact_service_unavailable` | 预置 | 覆盖+角色 |
| `SPEECHNEXUS_ARTIFACT_RETENTION_DATABASE_URL` | `sn_artifact_retention` | `artifact-retention` profile 的保留期清理 | 空值 → 容器启动即失败（无身份）；共用 `sn_api` 等宽权限身份时启动点名拒绝 | 预置 | 覆盖+角色 |
| `SPEECHNEXUS_WORKBENCH_DATABASE_URL` | `sn_workbench_runtime` | `workbench-api` profile 的服务身份：`/v1/workbench/projects`、`/v1/workbench/projects/{project_id}/transcripts` 与 `POST /v1/workbench/deletion-plans`、`POST /v1/workbench/deletions` | 空值或 DSN 指向 owner/宽身份时启动闸门点名拒绝启动，不服务 | 预置 | 覆盖+角色 |
| `SPEECHNEXUS_WORKBENCH_CLEANUP_DATABASE_URL` | `sn_workbench_cleanup` | `workbench-api` 的删除确认后清理与启动期账本重放 | 空值 → 删除路由保持不可用、启动闸门拒绝启动；与运行身份同名时点名拒绝启动 | 预置 | 覆盖+角色 |
| `SPEECHNEXUS_RESOURCE_POOL_READER_DATABASE_URL` | `sn_resource_pool_reader` | `GET /v1/nexus/admin/resource-pools` | `503 resource_pool_service_unavailable` | 预置 | 覆盖+角色 |
| `SPEECHNEXUS_BINDING_LIST_READER_DATABASE_URL` | `sn_binding_list_reader` | `GET /v1/nexus/admin/model-bindings` | `503 model_binding_service_unavailable` | 预置 | 覆盖+角色 |
| `SPEECHNEXUS_BINDING_WRITER_DATABASE_URL` | `sn_binding_writer` | binding draft 写入与 draft 读取 | 空值 → 写入 `503 binding_result_unknown`、draft 读取 `503 binding_unavailable` | 预置 | 覆盖+角色 |
| `SPEECHNEXUS_BINDING_ADMIN_READER_DATABASE_URL` | `sn_binding_admin_reader` | admin binding 读取与发布事实 | `503 binding_unavailable` | 预置 | 覆盖+角色 |
| `SPEECHNEXUS_BINDING_LIFECYCLE_DATABASE_URL` | `sn_binding_lifecycle` | binding 发布、退休、激活与停用意图（`POST .../model-bindings/{binding_id}/publications` 等） | `503 binding_result_unknown`（`retryable: false`） | 预置 | 覆盖+角色 |
| `SPEECHNEXUS_BINDING_DEFAULTS_WRITER_DATABASE_URL` | `sn_binding_defaults_writer`（不创建） | 默认参数 producer 模式 | 键为空即该模式关闭：legacy binding 写入仍由 `sn_binding_writer` 正常处理（`POST` 照常 `201`）；defaults 快照 `GET` 不由该模式服务，`BindingIngress` 按无效 binding 请求拒绝（`400 invalid_binding_request`）；两键同配时 API 在构造路由前拒绝启动（`Configure one explicit binding writer mode`） | 故意不预置 | 覆盖 |
| `SPEECHNEXUS_PRONUNCIATION_READER_DATABASE_URL` | `sn_pronunciation_reader`（不创建） | 发音词典固定读取 | `503 pronunciation_unavailable` | 故意不预置 | 覆盖 |
| `SPEECHNEXUS_PRONUNCIATION_WRITER_DATABASE_URL` | `sn_pronunciation_writer`（不创建） | 发音词典草稿写入 | `503 pronunciation_unavailable` | 故意不预置 | 覆盖 |
| `SPEECHNEXUS_PRONUNCIATION_PUBLISHER_DATABASE_URL` | `sn_pronunciation_publisher`（不创建） | 发音词典原子发布 | `publish: true` → `422 pronunciation_publication_not_supported`；读取与草稿按上面两条 | 故意不预置 | 覆盖 |
| `SPEECHNEXUS_VOICE_PUBLISHER_DATABASE_URL` | `sn_voice_publisher`（不创建） | voice 发布 | 未配 Job writer 时启动即拒绝并点名两个键（本键已配置、`SPEECHNEXUS_JOB_WRITER_DATABASE_URL` 为空）；两键都为空时发布 `503 voice_draft_unavailable`；writer 已配时跨库启动拒绝并点名同库要求 | 故意不预置 | 覆盖 |

表中没有死键：29 个透传键都有运行时消费者。设置里另有一个不在本表范围的键：
`SPEECHNEXUS_PUBLICATION_WRITER_DATABASE_URL` 属于 host 侧 `speechnexus-publish-binding`
CLI 的配置，不经过本 Compose；它不是死键，只是不属于本部署面。
`SPEECHNEXUS_OPERATOR_ATTESTATION_WRITER_DATABASE_URL` 同理：它是 host 侧
`speechnexus-attest-builtin-voice` 的配置，宿主用 host 端口导出，本 Compose 不转发它，因此也
不在上表；它不是死键，角色与授权见
[外部托管引擎的资格声明角色](#外部托管引擎的资格声明角色-sn_operator_attestation)。
`SPEECHNEXUS_QUALIFICATION_READER_DATABASE_URL` 同理：它是 host 侧
`speechnexus-qualification-provenance` 的配置，宿主用 host 端口导出，本 Compose 不转发，
因此也不在上表；身份为 `sn_qualification_reader`，只读授权见
[外部托管引擎的资格声明角色](#外部托管引擎的资格声明角色-sn_operator_attestation)。

`SPEECHNEXUS_SPEECH_SETTLEMENT_DATABASE_URL` 是同步 TTS 与同步 ASR 的共同必要输入：缺失时
`backend/src/speechnexus_backend/speech_runtime.py:50` 的必填清单不完整，工厂按
「启动告警点名全部缺失键」返回空装配（同步 TTS 关闭），API 其余路由照常。因此本条在
基座拓扑里以空值转发（`:?` 守卫会让从未启用同步 TTS 的部署连 `docker compose config`
都过不去），预置 `sn_speech_settlement` 并写回值后重启 API 才装配同步 TTS。

### 故意不预置的身份

以下五项不给预置步骤，原因与启用判断写在各项内。它们的共同点是：未配置时对应能力关闭，
可观察结果与表中「未配置时的实际行为」那一格一致（具名启动拒绝、固定状态码或「写入照常、
快照路径不被服务」），不会「假装可用」；一旦配置了地址，缺失的角色或授权会让 API 在启动
阶段点名拒绝，不会带着错误身份上线。这五项里有两项还有「配了不等于生效」的前置，分别在
项内写明：`sn_binding_defaults_writer` 与 `sn_binding_writer` 互斥，`sn_voice_publisher`
依赖 Job writer。

**`sn_binding_defaults_writer`（`SPEECHNEXUS_BINDING_DEFAULTS_WRITER_DATABASE_URL`）**
是 `sn_binding_writer` 之外的另一种 producer 模式，两者互斥：设置里两者同时存在时
`Settings` 校验即抛 `Configure one explicit binding writer mode`，API 在构造路由前就拒绝
启动。本参考部署选择 `sn_binding_writer` 模式，所以不创建它。

关闭该模式时不会把 binding 写入一起关掉，也不会出现 `503 binding_unavailable`：legacy
`POST /v1/nexus/admin/model-bindings` 仍由 `sn_binding_writer` 处理（参考部署实测 `201`），
变化的只是默认参数快照那条 `GET .../revisions/{revision}/parameter-defaults`——producer
关闭时该路由不注册，`BindingIngress` 把这类未识别路径按无效 binding 请求拒绝，实测
`400 invalid_binding_request`。也就是说参考部署里「默认参数 producer 未启用」的可观察事实
是「写入照常、快照路径不可用」，不是写入 503；这也是本表唯一一行关闭叠加能力而非基线写面
的条目。

部署方判断方式：该键为空即该模式未启用；要改用它，先清空
`SPEECHNEXUS_BINDING_WRITER_DATABASE_URL` 再创建 `sn_binding_defaults_writer` 并重放
`binding_defaults_grants`（它已包含 `binding_draft_grants` 的全部授权），启动自检通过后
默认参数 producer 才生效，快照 `GET` 同时出现。

**`sn_pronunciation_reader` / `sn_pronunciation_writer` / `sn_pronunciation_publisher`**
（`SPEECHNEXUS_PRONUNCIATION_READER_DATABASE_URL`、
`SPEECHNEXUS_PRONUNCIATION_WRITER_DATABASE_URL`、
`SPEECHNEXUS_PRONUNCIATION_PUBLISHER_DATABASE_URL`）属于独立的发音词典能力，不在本参考
部署声明装配的同步 ASR/TTS 面上（见[仍不可用的能力](#仍不可用的能力)），所以不创建这三个
身份。部署方判断方式：三个键为空时读取端点返回 `503 pronunciation_unavailable`，
`publish: true` 的写入返回 `422 pronunciation_publication_not_supported`，`publish: false`
的草稿写入同样 503；这三个稳定码表示能力未启用。要启用，按
[发音词典组件说明](../components/pronunciation-drafts.md#identities-grants-and-startup-verification)
创建三个非 owner 角色并分别重放读取（两表加发布账本的 `SELECT`）、
`pronunciation_draft_grants`、`pronunciation_publication_grants`；读取面目前也没有授权
函数，按 runtime 的声明显式 `GRANT`，身份配错时启动报 `Pronunciation ... startup rejected`。

**`sn_voice_publisher`（`SPEECHNEXUS_VOICE_PUBLISHER_DATABASE_URL`）** 不预置，因为
voice 发布要求先有 `voice_design` / `voice_validation` 车道产生的证据：本参考部署的
`job-dispatch` 启动闸门明确拒绝这两个 kind（见[异步 Job 段](#异步-job-运行角色与授权)），
没有任何可发布的测量记录，创建该角色只会得到一个写入即失败的发布路径。

**只配这一个键现在会在启动期被拒绝。** publisher 连接池由 `JobRuntime` 在 `open()` 里
创建，而 `JobRuntime` 只在 `SPEECHNEXUS_JOB_WRITER_DATABASE_URL` 非空时才被构造；组合根
因此在构造任何运行体前检查伴生 DSN，消息同时点名
`SPEECHNEXUS_VOICE_PUBLISHER_DATABASE_URL` 与 `SPEECHNEXUS_JOB_WRITER_DATABASE_URL`，并说明
两键要么同时配置、要么都留空（#660）。在此之前，配了该键却漏配 writer 与两个键都为空给
操作者的可观察结果完全相同：发布端点回到 `503 voice_draft_unavailable`，无法从启动日志
区分。两键都为空时发布端点仍是 `503 voice_draft_unavailable`，Job 创建与读取不受影响。
同类「配置了却不生效」的其余键见
[配置前提扫描](../operations/configuration-prerequisites.md)。

部署方判断方式：两键任缺一项时进程不启动并点名缺的那个键；两键都为空时发布返回
`503 voice_draft_unavailable`。要启用，按顺序执行：

1. 先启用 Job writer 三键——`SPEECHNEXUS_JOB_WRITER_DATABASE_URL`、
   `SPEECHNEXUS_JOB_ADMISSION_DATABASE_URL`、`SPEECHNEXUS_JOB_DISPATCH_DATABASE_URL` 与
   三个非 owner 角色，见[异步 Job 运行角色与授权](#异步-job-运行角色与授权)；
2. 保证 `sn_voice_publisher` 与 `sn_job_writer` 指向**同一个数据库**：`JobRuntime` 在启动时
   比较两个连接的数据库身份，不同库拒绝启动，并点名这两个键与
   「must connect to the same database」；其余 writer 身份、授权或迁移问题仍统一包装成
   `Public Job writer startup rejected; verify migration and writer role`；
3. 装配 gateway 的 voice 车道私有配置与 `SPEECHNEXUS_JOB_DISPATCH_KINDS`，再创建
   `sn_voice_publisher` 并重放 `voice_publication_grants`，最后配置该键。

## 7. 启动 API 与 gateway

`api` 与 `gateway` 的镜像来自 §0 的一次性构建命令，这里只启动，不再带 `--build`：

```bash
"${COMPOSE[@]}" --env-file deploy/compose.env -f deploy/compose.yaml \
  up -d api gateway
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

注册不是「起一个容器」：它在同一个事务里写入准入读取的四类事实——`host_nodes`（宿主机容量
向量）、`workspace_quotas`（该工作空间的并发 attempt 配额）、`resource_pools`（池 owner 与
fencing token）与 `engine_instances` / `runtime_instance_heads`（ready 实例与 generation）。
因此它是**容量登记与工作空间配额唯一的产生入口**，也是从空库到「`POST /v1/nexus/jobs`
返回 `202`」这条路上的第二段：`host_nodes` 只在这里插入（`engine_registration.py:370`，表结构见
`speech/infrastructure/coordination_schema.py:29`），`workspace_quotas` 也只在这里插入
（`engine_registration.py:490`，表结构见 `speech/infrastructure/coordination_schema.py:20`）。

注册之前必须已经有一个**已发布** binding，且注册写入的 binding 身份要与它逐字相同，否则实例
虽然 `ready`，准入仍以 `422 job_binding_incomplete` / `429 job_capacity_exhausted` 拒绝。
发布 binding、`execution_defaults`、配额与容量的完整顺序和每条参数的取值来源见
[异步 Job 手册 §7.1–§7.3](async-job-runbook.md#71-发布-binding注册与创建-job-的前置)。

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

注册成功后核对四类事实（只读，`registered` 输出的 `rows` 里也有同样的行）：

```bash
"${COMPOSE[@]}" --env-file deploy/compose.env -f deploy/compose.yaml \
  exec -T postgres psql -U speechnexus -d speechnexus -Atc \
  "select q.workspace_id, q.max_attempts, q.policy_revision from speech.workspace_quotas q"
"${COMPOSE[@]}" --env-file deploy/compose.env -f deploy/compose.yaml \
  exec -T postgres psql -U speechnexus -d speechnexus -Atc \
  "select host_node_id, node_state, allocatable_device_memory_bytes from speech.host_nodes"
"${COMPOSE[@]}" --env-file deploy/compose.env -f deploy/compose.yaml \
  exec -T postgres psql -U speechnexus -d speechnexus -Atc \
  "select instance_id, instance_generation, instance_state, binding_revision_id, manifest_id \
   from speech.engine_instances"
```

预期 `workspace-default` 一行（`max_attempts` 取自 `SPEECHNEXUS_ADMISSION_MAX_ATTEMPTS`，
`policy_revision` 取自私有配方的 `policy_revision_id`）、`node-local-1` 一行 `active`、
`instance-asr-1|1|ready` 一行，且 `binding_revision_id` 等于已发布 binding 的 `revision_id`
（形如 `mbrev-…`）、`manifest_id` 等于该 binding 的 `execution_recipe.capacity_manifest_id`。
`workspace_quotas` 没有独立的管理入口：`POST /v1/nexus/workspaces` 只写
`speech_workspaces`（`workspaces_postgres.py:275`），配额行只由本节的注册写入
（`engine_registration.py:488` 的 `_ensure_quota`）；同一工作空间重复注册时 `policy_revision`
必须一致，否则注册以 `quota_policy_conflict` 失败。

**宿主机的容量向量只在首次注册时写入，且必须一次声明够**：`host_nodes.allocatable_*` 由
`--host-capacity` 决定，缺省值是**本次注册这一条 plan 的需求**（`engine_registration.py:180`
的 `steady + per_slot × max_slots`，逐维取与 `peak` 的较大者）。注册事务先按 `peak` 预留，
观察成功后把这一份换成 `steady`（`held = held − peak + steady`，
`speech/domain/runtime_observations.py:474`），所以缺省注册成功后留下的余量逐维是
`max(per_slot × max_slots, peak − steady)`（即「缺省 allocatable − `steady`」）。余量归零的准确条件是
`peak == steady` 且 `per_slot × max_slots == 0`——单槽常驻引擎就属这种（`DEL-544` 现场即是），
第二个实例会以 `host_capacity_insufficient` 失败（报错形如「need at least … beyond current holds」）；
`peak > steady` 或 `max_slots > 1` 时通常还能挤进一个较小的实例。当节点的 `allocatable` 仍是缺省值（等于本次 plan 的需求）时，首次注册（`outcome=registered`）会在 stderr 提前给出这条信号：按注册后的 `held + quarantined` 已放不下第二个同形状实例时，命令打印一行 `warning:`（含「under the current holds」的逐维剩余量与再一个实例的需求，并并列三条出路：先释放 held/quarantined、变更评审下调整 `allocatable_*`（运行角色改不了）、重建数据卷并在首次注册传显式 `--host-capacity`），stdout 的 JSON 不变。这一行只在首次
插入时写（`engine_registration.py:370` 的 `ON CONFLICT (host_node_id) DO NOTHING`），运行角色没有放大
路径（`host_nodes` 的 UPDATE 权限只含 `node_state`/`version`/`held_*`/`quarantined_*`）；要扩只能走变更
评审下的行调整，或者重建数据卷后重来。因此**打算在这台宿主机上跑多个实例**（例如同一台机器既有 ASR
又有 TTS）时，第一次注册就显式传 `--host-capacity`，按宿主机真实可用量写，并留够所有实例峰值之和。
核对方式：

```bash
"${COMPOSE[@]}" --env-file deploy/compose.env -f deploy/compose.yaml \
  exec -T postgres psql -U speechnexus -d speechnexus -Atc \
  "select host_node_id, node_state, allocatable_unified_physical_memory_bytes, \
          held_unified_physical_memory_bytes, quarantined_unified_physical_memory_bytes \
   from speech.host_nodes"
```

`held + quarantined` 逼近 `allocatable` 就说明这台宿主机只剩下已注册实例的份额。已经用缺省值注册过的
节点加不了第二个实例；要改只能重建数据卷后按上面的规则重来（或在变更评审下由 DBA 改行，产品路径不支持）。

**一个工作空间只有一套配额策略**：`policy_revision` 是工作空间级的准入策略版本，不是每个
binding 自己的属性。因此同一工作空间里的所有模态（文件 ASR、文件 TTS、后续其他 binding）
必须用**同一个** `policy_revision`；它取自各自私有配方的 `policy_revision_id`（手工注册时是
`--policy-revision`），所以两份配方必须写同一个值——参考部署的两份样例配方就是同一个
`policy-revision-compose-example`。先注册的那个 binding 决定了这一行的取值。

已经存在一行、而本次注册带的是另一个值时，只有两条正当出路，都不改动已写入的行：

1. **对齐配方**（参考部署的做法）：把本次注册的 `policy_revision` 改成上面查询里已有行的值，
   重新注册。适合两个模态本来共用一套准入策略的部署。
2. **另建工作空间**：确实需要两套策略时，`POST /v1/nexus/workspaces` 建一个工作空间，把该
   binding 注册到它上面，请求时带对应的 `nexus.workspace_id`。配额、尝试与租约都按工作空间
   分开计，不要指望同一工作空间里并存两套策略。

不要 `UPDATE speech.workspace_quotas` 改 `policy_revision` 绕过：写入前的一致性检查是准入
读取该行的前置，改行会让注册时的核对与实际准入判据分叉。

## 9. 失败时先看什么

每一步「拒绝启动」的判定跑在哪一层、实测打印什么（一行 `FAIL:` 还是 traceback，退出码几）、文档承诺什么，见[启动闸门的失败输出形态](startup-gate-output.md)；下表只给现象与首查项。

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
| `engine-register` 报 `quota_policy_conflict` / `quota_capacity_conflict` | 该工作空间已有配额行：`policy_revision` 必须与本次注册提供的值一致、`max_attempts` 不得更小；不要用改行的方式绕过，先确认哪个部署先写了这行。同一工作空间的多个模态共用一套策略，两套策略要分工作空间——见 [§8 的一个工作空间只有一套配额策略](#一个工作空间只有一套配额策略) |
| `engine-register` 报 `host_capacity_insufficient` | `--host-capacity`（缺省 `steady + per_slot × max_slots`，至少覆盖 `peak`）小于计划需求，或该节点已有 held/quarantined 占用。**在同一节点注册第二个实例、余量不够时是这条**：缺省值等于第一个实例自己的需求，`peak == steady` 的单槽常驻计划会把该维余量吃光，且该行只在首次插入时写；报错里带 `allocatable`/`held+quarantined` 两个向量与出路。注意同一错误码有两条路径：参数校验那条（`engine_registration.py:1029`，退出码 2）在连库前就拒绝，本节这条（`:524`，退出码 4）发生在已有节点行上；见[§8 的宿主机容量规则](#宿主机的容量向量只在首次注册时写入且必须一次声明够) |
| `POST /v1/nexus/jobs`（及列表、读取、取消、重试、事件流）返回 `503 job_unavailable` | 优先当「缺前置」读，不要当引擎或容量故障：`error.message` 已按 §9.1 的表点名缺哪个键或该跑哪条命令；`retryable: false`，重试同一请求不会改变结果 |
| `POST /v1/nexus/jobs` 返回 `422 model_not_published` | 没有任何已发布 binding 作为该 `model` 的 head，或 `execution_recipe` / catalogue 尚未就绪；先按[异步 Job 手册 §7.1](async-job-runbook.md#71-发布-binding注册与创建-job-的前置) 发布（从空库首次创建时这是最先出现的错误，早于引擎与容量判定） |
| `POST /v1/nexus/jobs` 返回 `422 workspace_not_configured` | 该工作空间没有 `workspace_quotas` 行——即没为它注册过引擎；配额只由 §8 的注册写入，重试不会改变结果 |
| `POST /v1/nexus/jobs` 返回 `422 job_binding_incomplete` | 已发布 binding 的不可变定义缺少 `execution_defaults.<kind>` 字段，或实例的 `manifest_id` / `binding_revision_id` 与该 binding 不一致；`execution_defaults` 在 admin 响应里不回显（`speech/interfaces/binding_drafts.py:56`），须回到创建草稿时的请求体核对 |
| `engine-register` 报 `pool_owner_conflict` / `previous_generation_active` | 已有活动 owner 或旧 generation 仍 ready；先核对/停止旧实例，不能修改 token 假装接管 |
| 容器已启动但 API 转写返回 `504 operation_timeout` | 按原身份检查准入、网关、worker 和终态事实；历史超时原因不能直接套用到新运行，也不要用增加超时或重提新执行掩盖 |
| `POST .../quarantine-releases` 持续 `503 quarantine_release_unavailable` | 先查 `SPEECHNEXUS_QUARANTINE_RELEASE_DATABASE_URL` 是否配置：空值使端点永久停在可重试的 503，不会自愈；再查角色口令、DNS 与连接池 |
| 同一端点返回 `503 quarantine_release_privilege_missing` | 运行期授权被回收或与声明面不符；重放 `quarantine_release_grants`，不要改为授予 owner 或整表写权 |
| 同一端点返回 `503 quarantine_release_schema_not_migrated` | 该库未迁移到当前 head（或 `SPEECHNEXUS_DATABASE_SCHEMA` 指错）；先迁移再重试 |
| 同一端点返回 `503 quarantine_release_failed` | 存储以未声明的错误拒绝了事务，已确认回滚且未释放容量；按 API 日志的 `sqlstate` 定位，不要当业务冲突处理 |
| 公开 `/v1/audio/speech` 返回 `503 backend_unavailable`（`retryable: false`）且 API 日志有 `Synchronous speech unavailable` | 先修装配，不要重试：`speech-recipe.json` 缺失/摘要不符时 API 启动即失败；`SPEECHNEXUS_SPEECH_SETTLEMENT_DATABASE_URL` 为空或资格缺失时工厂保持关闭（API 日志点名缺失键与「no current qualified voice」）。用 `uv run --all-packages python scripts/dev-env/check-tts-assembly.py --private-dir deploy/.runtime/private` 逐字段核对：三份私有文件各自过生产 loader，输出按「仍是样例值 / 不一致 / 本地检查通过 / 无法从文件判定」四类列出，另单列只由 loader 校验的叶子；只有前两类为空才 exit 0，而 `exit 0` 只说明文件已替换且相互一致，**不代表可受理**（结算 DSN、实例注册、声音资格、真实引擎仍未验证）。装配后仍需真实引擎与合格声音版本；另见[同步 TTS 装配要求](../components/synchronous-speech.md) |
| 音频上传（`asr_input`、`voice_reference`）返回 `503 asset_inspection_unavailable` | `SPEECHNEXUS_ASSET_INSPECTOR` 默认 `text`，只校验 `text_input`；要接收音频须同时配置 `audio`、检查器 URL 与令牌。这是刻意 fail-closed 的默认值，不是同步 TTS 或文本 ASR 的故障 |

### 9.1 「缺什么 → 看到什么 → 做什么」：Job 前置条件映射表

Job 路由在前置缺失时一律返回 `503 job_unavailable`（响应体结构与 `retryable: false` 不变），
区别只在 `error.message`：它点名缺失的条件与修复动作，绝不包含 DSN 取值、主机、库名、
用户名或凭据。下表是唯一的运维映射，逐行对应
`backend/src/speechnexus_backend/speech/domain/job_prerequisites.py` 中的
`JOB_PREREQUISITES`：实现、公开响应与本表共用同一份常量，
`tests/ci/test_job_prerequisite_map.py` 按行比对条件、`detail` 原文与动作，改一侧不改另一侧即红。

不同缺失不共用同一句文案：同一台部署分别触发两行时，响应能直接指出该配哪个键。

只有「迁移完全没跑」（关系缺失，SQLSTATE `42P01`/`3F000`）会落到 `schema_not_migrated`：
一个中途停下的迁移可能只是缺列（`42703`），那种驱动错误仍归 `store_unavailable` 那一行，
即通用文案加「看 api 日志」，不会被误报成「该跑迁移」，也不会被错报成授权故障（#920 review R1）。

未迁移在**两个时点**上表现不同，别按同一句话预期：

- **进程已在跑**（启动后库被回退、或指向了另一个 schema）：读、创建、取消各出口都返回上表
  `schema_not_migrated` 那一行的原文，`503`、无 traceback；这是 `tests/integration/test_public_job_prerequisites_pg.py`
  实跑的形态。
- **启动期未迁移**（库从未迁移过）：迁移前置检查（`job_schema_preflight.py` 的
  `require_migrated_schema`）在**角色守卫之前**判定，抛出的 `JobSchemaNotMigratedError` 消息就是上表原文；
  但它发生在 ASGI lifespan 里，uvicorn 在启动失败时打印该异常及其 traceback（`Exception in 'lifespan'
  protocol`）、以**退出码 3** 结束进程。因此启动期**不是**「一行 `FAIL:`、无 traceback」。能打印一行
  `FAIL: ...` 并以退出码 1 结束的，是**同步装配阶段**的结算预检（`SettlementPreflightUnavailableError`，
  见 `backend/src/speechnexus_backend/speech_runtime.py` 的 `build_speech`）：它在 uvicorn 加载应用时就失败，可被
  `speechnexus_backend/main.py` 捕获。两者都不再报「角色缺授权」这种错方向的诊断。

| 缺失条件（`condition`） | 响应 `detail`（`error.message` 原文） | 观察到的场景与做什么 |
| --- | --- | --- |
| `writer_dsn_blank` | `SPEECHNEXUS_JOB_WRITER_DATABASE_URL is blank: the Job surface is not assembled, so no Job route can create, read or cancel. Set that key for the api process; it is the identity that reads and writes Job storage.` | writer DSN 是装配整条 Job 面的唯一键；它为空时连读取都不可用，所以这里只点名它；**该做什么**：`Set SPEECHNEXUS_JOB_WRITER_DATABASE_URL in the api environment.` |
| `admission_dsn_blank` | `SPEECHNEXUS_JOB_ADMISSION_DATABASE_URL is blank: a create cannot reserve capacity and write its Job in one admission transaction, so no Job is written. Reads and cancellations keep working; set it to the same database as the Job writer.` | writer 已配置、只缺 admission 时，读取与取消仍可用，只有创建被拒；**该做什么**：`Set SPEECHNEXUS_JOB_ADMISSION_DATABASE_URL to the Job writer's database.` |
| `schema_not_migrated` | `The Job database has not run the migration profile: a required relation is missing, so no Job can be stored. Run docker compose --profile migration run --rm migrate, then restart the api process.` | `UndefinedTable` / `InvalidSchemaName`（SQLSTATE `42P01` / `3F000`）归到这一行：先迁移，不要当权限问题重试；**该做什么**：`Run docker compose --profile migration run --rm migrate.` |
| `role_privileges_missing` | `The Job role lacks a grant the store needs, so the database refused the read or the write. Run scripts/dev-env/provision-job-roles.py for the api role, then send a new request.` | `InsufficientPrivilege`（SQLSTATE `42501`）归到这一行：关系存在但角色缺授权，重放授权脚本，不改迁移；**该做什么**：`Run scripts/dev-env/provision-job-roles.py for the api role.` |
| `store_unavailable` | `Job storage is unavailable.` | 不属于配置缺失：解码缺陷、连接池超时等真实故障保持这句通用文案，不点名任何键；**该做什么**：`Read the api log for the storage failure; no configuration key is missing.` |


## 10. 停止与清理边界

`down` 只清理本次解析到的项目与 profile 资源。启动时用过 `--profile` 的部署（如
`job-dispatch`、`binding-disable`、`engine-registration`）必须列出同一组 profile 再停止：

```bash
"${COMPOSE[@]}" --env-file deploy/compose.env -f deploy/compose.yaml \
  --profile job-dispatch --profile binding-disable down   # 与本轮启动用过的 profile 集合一致
```

一次性验收栈要连数据卷一起丢弃时用 `down -v`，profile 同样要带齐。profile 不一致时，未启用
profile 的服务不会被停止或删除，网络因仍有端点而删除失败（`Resource is still in use`），
命令仍然退出 `0`。`down` 不删除命名卷 `postgres-data` /
`minio-data`；不要用 `down -v` 清理真实执行证据或未完成的对账事实。私有文件和 env 文件
也不得提交到仓库。停止容器不等于释放已准入的执行/租约或停止引擎；按[对账恢复](reconciliation-recovery.md)
和具体实例停止流程取得证据后再归还预算。

### 10.1 清栈复核：用 label filter，不用退出码

`down` / `down -v` 的退出码不是清栈证据。以下情形都不删除目标资源、也不报错，退出码仍为
`0`：

- `--profile` 集合与启动时不同（见本节首段）：这些服务不被停止或删除，网络因仍有端点而
  删除失败；
- `-p` / `COMPOSE_PROJECT_NAME` 与启动时不同，或当前 shell 没有 export 它而部署用了独立
  项目名——§1 的默认回落是 `speechnexus`，用它去停 `speechnexus-ref` 就是这种情况；
- 用另一份 `compose.yaml` 执行（别的目录、旧副本，或 `COMPOSE_FILE` 指向别的文件）：项目名
  随之变成那份文件里的 `name:`。
- `docker compose run --rm` 建立的一次性容器带 `com.docker.compose.oneoff=True` label 且仍算
  本项目，`down` 不清理它：`--rm` 只在进程正常退出时删除，窗口被打断、该容器仍在运行或它自己
  又拉起了依赖时都会留下。实测（`DEL-535`，MegaCube）第一次 `down -v` 退出 `0` 后，按项目
  label 仍看到 1 个 one-off 容器与 2 个网络，网络侧报 `Resource is still in use`；停删该容器
  与网络后三项才为空。

实测（2026-11-16，Docker Engine 29.9.3、Compose v5.7.0，最小探针项目，均以 `EXIT=0` 结束）：

- `up --profile extra` 后丢掉 profile 执行 `down -v`：报 `Resource is still in use`，仍有
  1 个容器与 1 个网络存活；补齐 profile 重跑后三项同时为空。
- 用错 `-p` 执行 `down -v`：无任何输出，2 个容器、1 个网络、1 个卷全部残留。
- 换一份 `compose.yaml` 执行 `down -v`：无任何输出，3 个容器全部残留。

清栈后用 label filter 复核三项，**三项都为空**才算清完：

```bash
PROJECT="${COMPOSE_PROJECT_NAME:-speechnexus}"   # 必须等于 §1 启动时实际生效的项目名
docker ps -a       --filter label=com.docker.compose.project="$PROJECT" --format '{{.Names}}'
docker network ls  --filter label=com.docker.compose.project="$PROJECT" --format '{{.Name}}'
docker volume ls   --filter label=com.docker.compose.project="$PROJECT" --format '{{.Name}}'
```

`--format` 不能省略：无匹配时 `docker ps -a --filter …` 仍打印表头行（`CONTAINER ID  IMAGE
COMMAND …`），`docker network ls` 同理（`NETWORK ID  NAME  DRIVER  SCOPE`）。输出只有表头
表示空，但它是非空输出，肉眼与 `[ -n "$(…)" ]` 都会读成残留；加 `--format` 后无匹配才是零行。

任一项非空时按 containers → networks → volumes 处理，顺序不能反：

| 非空项 | 先定位 | 删除 | 约束 |
| --- | --- | --- | --- |
| 容器 | `docker inspect <容器名> --format '{{index .Config.Labels "com.docker.compose.project"}}'` 核对该容器确实属于本项目 | `docker rm -f <容器名>` | `-f` 跳过优雅停止，处理中的请求会中断；删前先看 `docker inspect <容器名> --format '{{index .Config.Labels "com.docker.compose.oneoff"}}'`：值为 `True` 说明它是 `docker compose run` 的一次性容器，同样先 `rm -f` 再删网络 |
| 网络 | `docker network inspect <网络名> --format '{{range .Containers}}{{.Name}} {{end}}'` | `docker network rm <网络名>` | 仍有端点时报 `network ... has active endpoints`，先删容器 |
| 卷 | `docker volume inspect <卷名> --format '{{.Driver}} {{.Labels}}'` | `docker volume rm <卷名>` | 容器仍挂载时报 `volume is in use` 且退出码 `1`（实测）；只在确认卷内无需保留的数据后删除 |

`postgres-data` / `minio-data` 保存本部署的全部持久事实，删卷不可恢复，`down -v` 会一并删除。
需要保留现场时只 `down`（不带 `-v`）：此时卷那一项允许非空，但验收记录要写明保留了哪些卷
及原因，容器与网络两项仍必须为空。

同一台主机上并行多个验收窗口时（例如 `#572` 的从零验收 `DEL-535` 与其它栈共用 GPU 主机），
每套部署用独立 `COMPOSE_PROJECT_NAME`，收工时 `down -v` 后按本节三项复核全部为空再结束窗口。
否则下一轮会拿到其它项目的残留容器、网络或卷，或在脏环境上取证。项目级容器枚举在
`scripts/dev-env/check-async-job-prereqs.py` 里也用于读取部署前置（`docker_containers`），
它是前置判定、不判清栈；清栈的空判据只有本节这三条命令。

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
uv run --all-packages python - <<'PYROLE'
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

如果启用生命周期 API，须使用 `binding_lifecycle_grants` 与
`binding_publication_grants` 两组声明配置其既有非 owner 身份，并设置
`SPEECHNEXUS_BINDING_LIFECYCLE_DATABASE_URL`；不要向上述两个新角色授予生命周期
写权限。扫描和结算 DSN 分别通过部署私有配置设置为 `SPEECHNEXUS_DISABLE_DATABASE_URL`
和 `SPEECHNEXUS_DISABLE_SETTLEMENT_DATABASE_URL`，必须对应不同角色和同一数据库。worker
使用 gateway 已配置的 gateway/node 标识与组件凭据。

部署授权和新 gateway 镜像后，启用 worker：

```bash
"${COMPOSE[@]}" --env-file deploy/compose.env -f deploy/compose.yaml \
  build gateway binding-disable-worker
"${COMPOSE[@]}" --env-file deploy/compose.env -f deploy/compose.yaml \
  --profile binding-disable up -d gateway binding-disable-worker
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
