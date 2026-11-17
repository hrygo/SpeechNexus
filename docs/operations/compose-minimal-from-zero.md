---
title: "从空库按文档起参考部署的执行记录"
status: active
version: "0.1.0"
date: 2026-11-16
---

# 从空库按文档起参考部署的执行记录

本文件记录两次「从空库照文档起一套」的实跑，命令、实测输出与缺口都按原样保留：

- §2–§10：2026-11-16 在一台 macOS 开发机上、以独立 Compose 项目从空 PostgreSQL 卷与空 MinIO
  出发，执行到参考部署五服务健康、派发 worker 启动闸门通过；本机没有真实引擎，止步于一次性
  引擎注册。
- §11：同日在 MegaCube（aarch64 + GB10）上从空卷重跑，并在**真实 GPU 引擎**上跑通一条 ASR
  Job。该轮仍不是零修正通过，暴露的 9 处偏差已由 `#890` 修正，是 `#572`「他人照文档起一套」
  的实跑前提。

## 1. 结论

- 到 `compose-minimal.md` 第 7 节为止：基础拓扑、迁移、18 个非 owner 角色与 3 个 Job 角色、
  `api`/`gateway` 探针全部按文档通过，输出与手册预期一致（§4）。
- `job-dispatch` profile 在完整前置下通过启动闸门：`job dispatch startup gate: OK`，
  worker `RestartCount=0`，每轮 `job_dispatch_batch` 均为 `claimed=0`（§5）。
- 止步于第 8 节一次性引擎注册：本机没有真实引擎，`engine-register` 退出码 `5`、
  `engine_unreachable`。真实 Job 的创建、claim、提交与制品下载**未验证**。
- 即使有引擎，这条文档路径仍然创建不出 Job：两份手册都不发布 binding，创建在解析 binding
  时就返回 `422 model_not_published`，到不了准入的容量判定。这是本卡要还的主要债务（§5）。
- 缺前置时的表现：迁移、派发授权、结果桶三类在 claim 前**点名**拒绝；writer/admission DSN
  缺失是稳定但**不点名**的 `503 job_unavailable`；无 ready 实例从零看到的是
  `422 model_not_published`，手册声称的 `429 job_capacity_exhausted` 在本路径不可达（§6）。
- MegaCube 那次（§11）补齐了「真实引擎 + 已发布 binding」两个缺口，`202 → queued → running →
  succeeded` 全链通过；`GET /v1/models` 仍是 `503`（产品缺陷 `#891`），9 处文档/样例偏差见
  §11.3。

## 2. 环境、基线与边界

| 项 | 值 |
| --- | --- |
| 日期 | 2026-11-16 |
| 主机 | macOS，arm64，无 GPU、无本地引擎；Docker Engine 29.9.3、Compose v5.7.0 |
| 仓库基线 | `origin/main` `25c96360`（worktree `/private/tmp/sn878`，分支 `docs/572-from-zero`） |
| 起点 | 新建的 PostgreSQL 16.15 数据卷、新建的 MinIO 数据卷，无残留容器/网络/卷 |
| 隔离 | Compose 项目名 `sn878`，子网 `172.30.0.0/24`，宿主端口 `8099`/`8010`/`55438`/`59020`/`59021` |
| 未做 | 工作台浏览器段（另有证据）、真实引擎、音频上传、真实 TTS |

本机 `~/.config/uv/uv.toml` 配置了非默认镜像源，`uv run` 会误判 lockfile；本轮所有 `uv`
命令加 `UV_NO_CONFIG=1`。这是本机环境问题，不是仓库缺陷，也没有提交 `uv.lock`。

## 3. 为隔离做的部署配置调整（不是文档新增步骤）

任务要求项目名、子网与端口独立，避免碰到本机其它运行中的栈。按
[`compose-minimal.md:79`](compose-minimal.md) 的"其他网段"说明，三处必须一起改：

1. `deploy/compose.yaml:850` 的子网 `172.28.0.0/24` → `172.30.0.0/24`；
2. `deploy/compose.yaml:90` 的 MinIO 静态地址 `172.28.0.10` → `172.30.0.10`；
3. 两份私有 gateway 配置的 `objects.endpoint`（`deploy/private/gateway-file-asr.example.json:8`、
   `gateway-file-tts.example.json:11`）同步改为 `http://172.30.0.10:9000`。

宿主端口全部走环境文件的既有变量，没有改 Compose 定义。这些改动只存在于测后已删除的
worktree 运行态，不进入本 PR，也不是从零手册的额外步骤。

另外一项外部输入：`compose-minimal.md:246` 要求把已批准的 Silero ONNX 文件放进私有目录。
本机没有该受控制品，用的是本机另一项目已存在的同一开源 Silero VAD ONNX
（2,327,524 字节，`sha256 597d30b3ec076608d059477bb14cfeffdf951bf5cae370d38f65d33bbfe82004`），
按文档流程写入 `vad_model_sha256`。仓库钉在 `deploy/engines/file-asr-audio/artifact-pin.json:139`
的摘要（`sha256:1a153a22…`）对应另一份制品，本机没有；见 §7 第 3 条。由于没有 Job 执行，
网关是否能用这份 ONNX 解码**未验证**。

## 4. 逐步执行记录

命令均从仓库根执行；`COMPOSE=(docker compose)`，并 `export COMPOSE_PROJECT_NAME=sn878`。
文档里的默认宿主端口按实际值替换（文档本身要求这么做）。

### 4.1 第 0–1 节：构建前提与服务集合

`docker version` / `docker compose version` / `docker buildx version` 均可用，标准 BuildKit
构建成功（本机有构建缓存，全部镜像一次 `build` 通过，未使用任何构建镜像源改写 override）。

```text
$ docker compose --env-file deploy/compose.env.example -f deploy/compose.yaml config --services
postgres
minio
minio-init
gateway
api
```

与 `compose-minimal.md:54` 的预期集合一致；`--profile job-dispatch config --services` 输出
`api gateway job-dispatch-worker minio minio-init postgres`，与 `async-job-runbook.md:92`
一致。

### 4.2 第 2 节：环境文件

原样执行 `compose-minimal.md` 第 2 节的 `umask 077` + Python 片段，输出：

```text
compose.env written with generated local credentials (values not printed)
```

文件 mode `0600`，`SPEECHNEXUS_RUNTIME_UID`/`GID` 为当前非 root 用户。随后只在同一文件里改
宿主端口（第 3 节），没有其它改动。

### 4.3 第 3 节：私有文件

按文档复制六份 JSON、放入 VAD 模型、重算 `vad_model_sha256`，并用仓库虚拟环境重算两份
recipe 的 RFC 8785 摘要：

```text
vad_model_sha256: 597d30b3ec076608d059477bb14cfeffdf951bf5cae370d38f65d33bbfe82004
transcription-recipe.json stored_was: sha256:de85ebca… recomputed: sha256:de85ebca… changed: False
speech-recipe.json stored_was: sha256:3a553bbe… recomputed: sha256:3a553bbe… changed: False
```

文档第 3 节的两段校验通过（四个 JSON 可解析、两份 recipe mode `0600`）。示例配方的身份在
当前 head 是自洽的（`binding_publication_id == binding_revision_id`、`stage.instance_id` 与
`runtime_plan.binding` 一致），API 启动没有报摘要或身份不符。

### 4.4 第 4 节：数据库与对象存储

```text
$ docker compose ... up -d --build postgres minio
 Container sn878-postgres-1 Started
 Container sn878-minio-1 Started

$ docker compose ... run --rm minio-init
bucket ready: speechnexus-inputs (versioning enabled)
bucket ready: speechnexus-results (versioning enabled)

$ curl -fsS http://127.0.0.1:59020/minio/health/live
http=200
```

与 `compose-minimal.md:334` 的预期逐字一致。容器名是 `sn878-*`，见 §7 第 1 条。

### 4.5 第 5 节：迁移

```text
$ docker compose ... --profile migration run --rm migrate
migrate_exit=0        # 容器内无迁移日志

$ docker exec sn878-postgres-1 psql -U speechnexus -d speechnexus -Atc \
    'select version_num from speech.alembic_version'
0054_artifact_deleted_ledger
```

与 `compose-minimal.md:366` 声明的当前 head 一致。文档同处的
`docker exec speechnexus-postgres-1 …` 在独立项目名下失败，见 §7 第 1 条。

### 4.6 第 6 节：非 owner 角色、授权与 Job 角色

原样执行第 6 节的三段脚本（生成 15 个角色口令 → 建 18 个角色与授权 → 写回 15 条 DSN），
以及"异步 Job 运行角色"小节的生成与 `provision-job-roles.py`：

```text
roles ready: sn_api sn_gateway sn_gateway_query sn_workspace_writer sn_audit_reader
sn_model_instance_reader sn_quarantine_release sn_catalog_reader sn_artifact_reader
sn_resource_pool_reader sn_binding_list_reader sn_binding_writer sn_binding_admin_reader
sn_binding_lifecycle sn_artifact_retention sn_speech_settlement sn_workbench_runtime
sn_workbench_cleanup
15 role passwords in deploy/compose.env, placeholders remaining: 0
job roles ready: sn_job_writer sn_job_admission sn_job_dispatch; grants: 221 statements
```

221 与手册给出的历史快照（220）不同是预期的：手册明确要求以 `--print-statements` 为准。
本轮没有手工补任何 `GRANT`，也没有改写产品数据。

### 4.7 第 7 节：API 与 gateway

```text
$ docker compose ... up -d --build api gateway
sn878-gateway-1  Up (healthy)
sn878-api-1      Up (healthy)

$ curl http://127.0.0.1:8099/healthz
{"status":"ok","service":"speechnexus-api","version":"0.1.0.dev0"}  http=200
$ curl http://127.0.0.1:8099/readyz
{"status":"ready","checks":[{"name":"postgresql","status":"ok","detail":null},
 {"name":"object_storage","status":"ok","detail":null}]}  http=200
$ curl http://127.0.0.1:8010/healthz
{"status":"ok","service":"speechnexus-execution-gateway","version":"0.1.0.dev0"}  http=200
$ curl http://127.0.0.1:8010/readyz
{"status":"ready","checks":[{"name":"control_postgresql","status":"ok","detail":null},
 {"name":"control_query_postgresql","status":"ok","detail":null},
 {"name":"gateway_ingress","status":"ok","detail":null}]}  http=200
```

与 `compose-minimal.md:1884` 起的预期逐字一致（仅宿主端口不同）。网关私有的 VAD 文件、
两份 gateway JSON、私有目录 sockets/spool 都一次装配成功。

### 4.8 第 8 节：一次性引擎注册（停止点）

环境文件里的引擎地址是示例默认的 `http://host.docker.internal:18100`。操作主机上没有引擎：

```text
$ curl -fsS http://host.docker.internal:18100/health
curl: (52) Empty reply from server

$ docker compose ... --profile engine-registration run --rm engine-register
error: engine_unreachable: health endpoint is unreachable from this operator host
register_exit=5
```

停止点按任务约定记录：**此处需要真实引擎，本轮未验**。没有用任何 fake 引擎、没有手写
`engine_instances`、`leases` 或注入制品。

### 4.9 第 9 节与只读自检

```text
$ docker compose ... --profile job-dispatch up -d
sn878-job-dispatch-worker-1  Up 8 seconds
$ docker inspect --format '{{.RestartCount}} {{.State.Status}} {{.HostConfig.NetworkMode}}' ...worker
0 running container:<gateway 容器 ID>

$ docker compose ... logs --tail=15 job-dispatch-worker
  ok: database: migration head 0054_artifact_deleted_ledger, role guard, startup SELECT
      probes and 67 post-claim write columns
  ok: object storage: versioned buckets speechnexus-results
  ok: gateway staging bucket matches gateway-file-asr.json, gateway-file-tts.json
  pending: real engine and a registered ready instance are a deployment input this gate cannot verify; …
  pending: API-side writer/admission DSNs live in the api container; …
job dispatch startup gate: OK
{"event":"job_dispatch_batch","claimed":0,"succeeded":0,"failed":0,"unknown":0,"skipped":0,"deferred":0,"lease_reclaimed":0,"lease_lost":0}
```

`job-dispatch` 的启动闸门与 `async-job-runbook.md:45` 描述一致：`up -d` 的成功不代表就绪，
就绪要看 `RestartCount`、日志里的闸门结论与批次行。

只读自检（`async-job-runbook.md:15`）在部署后运行：

```text
阶段: post — --phase post(显式覆盖)
[PASS] 前置 1  API 侧 Job writer / admission DSN 与 writer 权限
[PASS] 前置 2  迁移到唯一 head
[PASS] 前置 3  派发角色与五组 grant(读 + 写)
[FAIL] 前置 4  真实引擎与实例注册
        ✗ 实例 instance-asr-1 generation 1 未注册;先执行 engine-registration 并取得 outcome=registered
[PASS] 前置 5  gateway result_bucket 存在且被创建/版本化
[PASS] 前置 6  gateway 网络命名空间与启动顺序
结果: 5/6 PASS;0 PENDING;1 FAIL
```

这条 FAIL 是唯一未满足的硬前置，与 §4.8 的停止点一致。

## 5. 为什么到不了 Job 创建

`async-job-runbook.md:704` 与第 11 节假设"六项前置满足后 `POST /v1/nexus/jobs` 才不再返回
503"。本轮六项里只剩引擎未满足，但创建仍然不可达，真实顺序是：

1. 创建请求先解析模型与 binding。参考部署没有任何已发布 binding
   （`speech_publications=0`、`speech_binding_revisions=0`），于是
   `backend/src/speechnexus_backend/speech/application/public_jobs.py:952` 取不到 revision，
   返回 `422 model_not_published`；
2. 即使 binding 存在，准入还要该工作空间的配额事实（缺失时 `422 workspace_not_configured`，
   `backend/src/speechnexus_backend/speech/infrastructure/public_job_admission.py:190`），
   之后才是实例容量（`…:219` 的 `429 job_capacity_exhausted`）；
3. 这两类输入都不在这两份手册的步骤里：`compose-minimal.md` 第 6 节会写回
   `SPEECHNEXUS_BINDING_WRITER_DATABASE_URL`，但没有"创建草稿 revision 并发布"的步骤；
   手册只把"已发布 binding"当作既有事实引用（`async-job-runbook.md:319`）。

实测原文：

```text
$ curl -sS -X POST http://127.0.0.1:8099/v1/nexus/jobs -H 'content-type: application/json' \
    -H "Idempotency-Key: …" \
    -d '{"kind":"asr","model":"model-compose-example","input":{"audio_asset_id":"asset-000000000000"}}'
HTTP/1.1 422 Unprocessable Content
{"error":{"code":"model_not_published","type":"invalid_request_error",
 "message":"The model is not published.","param":null,"request_id":"…","retryable":false}}
```

因此本轮**没有** Job、没有 claim、没有提交与制品；任务允许的"创建→claim→提交"停止点本身
也不可达。claim 与 submit 的真实性仍只能来自带引擎的窗口。

## 6. 缺前置时的失败形态（逐项实测）

五项都在部署完成后对同一套栈制造，每项结束都恢复原状（重建容器或补回授权）。"claim 前"
按是否产生任何 Job 或租约判定。

| 前置 | 制造方式 | 实际表现 | claim 前 | 点名缺项 | 判定 |
| --- | --- | --- | --- | --- | --- |
| writer / admission DSN 缺失 | 清空 `SPEECHNEXUS_JOB_WRITER_DATABASE_URL` 与 `…_ADMISSION_…`，重建 `api` | `503 job_unavailable`，`message: "Job storage is unavailable."`；API 日志只有一行 503 访问日志 | 是（无 Job） | **否**：不区分两个 DSN，也不提示是哪一个为空 | 不达标 |
| 迁移未执行 | 同一 PostgreSQL 实例建空库 `speechnexus_empty`，用临时 env 文件把 worker 的 `…_JOB_DISPATCH_DATABASE_URL` 指向它 | 启动闸门退出码 `1`，但输出是未捕获的 `psycopg.errors.InvalidSchemaName: schema "speech" does not exist` traceback（`deploy/job-startup-gate.py:538` 先跑角色守卫） | 是（未 start worker） | 部分：只暴露 SQL 异常，手册设计的 `speech.alembic_version is missing: run the migration profile` 没有出现 | 不达标（缺陷 D7） |
| 迁移未执行（API 侧） | 同上，用临时 env 文件把 `api` 的三个 DSN 指向空库，`run --rm --no-deps api` | 进程退出，traceback：`psycopg.errors.UndefinedTable: relation "admission_leases" does not exist`（`speech_runtime.py:91` → `tts_settlement_preflight.py:73`） | 是（API 起不来） | 部分：点名了表，但没有"先跑迁移"的指引 | 不达标（缺陷 D8） |
| 派发角色 / 授权缺失 | `REVOKE SELECT ON speech.coordination_jobs FROM sn_job_dispatch`，重启 worker | 退出码 `1`：`FAIL: dispatch role cannot run the startup probe on coordination_jobs: install job_runtime_grants, job_dispatch_grants, public_job_grants, cancellation_reconciliation_grants, coordination_runtime_grants for this role` | 是（闸门拒绝，未 start） | 是（表 + 五组授权函数） | 达标 |
| 无 ready 实例 / 无可用引擎 | 不注册任何实例，直接创建 Job | `422 model_not_published`（在 binding 解析阶段）；只读自检单独给出 `✗ 实例 instance-asr-1 generation 1 未注册` | 是（无 Job） | **否**：创建错误指向 model，不指向引擎或实例；手册声称的 `429 job_capacity_exhausted` 不可达 | 不达标（缺陷 D4/D6） |
| `result` 桶缺失 | `-e SPEECHNEXUS_EXECUTION_RESULT_BUCKET=speechnexus-results-missing` 运行 worker 闸门 | 退出码 `1`：`FAIL: bucket speechnexus-results-missing (SPEECHNEXUS_EXECUTION_RESULT_BUCKET) does not exist: minio-init creates and versions it; run the base topology before job-dispatch` + 两份私有配置的桶名不一致 | 是（闸门拒绝，未 start） | 是（桶名 + 环境键） | 达标 |
| `result` 桶未版本化 | 用 `minio-init` 镜像建一个不启用版本化的桶，`-e SPEECHNEXUS_EXECUTION_RESULT_BUCKET=speechnexus-results-unversioned` 运行闸门 | 退出码 `1`：`FAIL: bucket speechnexus-results-unversioned (SPEECHNEXUS_EXECUTION_RESULT_BUCKET) is not versioned: the Job result read-back requires object versions; minio-init enables them` + 同样的配置不一致两行 | 是 | 是（桶名 + 未版本化） | 达标 |

恢复原状后重跑闸门：`job dispatch startup gate: OK`，worker `RestartCount=0`。

## 7. 文档与实际不符（缺陷清单）

这些不必在本记录同轮全修，按位置立卡即可。行号为 2026-11-16 的 `origin/main` `25c96360`。

| 编号 | 位置 | 现象 | 建议 |
| --- | --- | --- | --- |
| D1 | `docs/operations/compose-minimal.md:361`（同类还有 `:880`、`:920`、`:1064`、`:1077`） | 核对命令硬编码默认项目名的容器名 `speechnexus-postgres-1`；用独立 Compose 项目（本项目 Agent 约定要求）时直接 `No such container` | 改成 `"${COMPOSE[@]}" --env-file deploy/compose.env -f deploy/compose.yaml exec -T postgres psql …`，或在第 1 节声明项目名变量并统一使用 |
| D2 | `deploy/compose.yaml:90`、`:850` 与 `docs/operations/compose-minimal.md:79` | 子网与 MinIO 静态地址硬编码；文档只说"一起调整部署配置"，没有环境变量或受支持的 override 入口。本机 `172.28.0.0/24` 已被其它项目占用，从零必须改仓库文件（含四份 private 示例） | 提供 `SPEECHNEXUS_COMPOSE_SUBNET` / `SPEECHNEXUS_MINIO_STATIC_IP` 之类的 env 键，或提供并测试一份受支持的 override 文件 |
| D3 | `docs/operations/compose-minimal.md:246` 与 `deploy/engines/file-asr-audio/artifact-pin.json:139` | VAD 模型只有一句"把已批准的 Silero ONNX 普通文件放到私有目录"：没有获取来源、没有校验命令；仓库钉的摘要是 `sha256:1a153a22…`，与常见 Silero 发行版（本轮实测 `597d30b3…`）不同。网关只校验配置里的摘要，因此"任何 Silero ONNX"都能过闸门，但没人能判断哪份是"已批准" | 在 §3 增加受控来源与 `sha256` 核对步骤（例如用 `artifact-pin.json` 的摘要校验），并说明摘要不符时网关的具体报错 |
| D4 | `docs/operations/async-job-runbook.md:32` | 第 4 项写"没有实例时创建返回 `429 job_capacity_exhausted`"；从零实测先得到 `422 model_not_published`（binding 未发布），容量判定在 `public_job_admission.py:219`，排在绑定与配额之后 | 改成"已发布 binding 且工作空间配额存在时，才由容量返回 429；从零未发布 binding 时先 422" |
| D5 | `docs/operations/async-job-runbook.md:109` | "只配 writer 时创建仍返回 `503 job_unavailable`"；实测（writer 已配、admission 为空、无 binding）返回 `422 model_not_published`。只有请求走到准入时才会是 503 | 加"在有已发布 binding 的部署上"限定，并同时给出从零会先看到的 422 |
| D6 | `docs/operations/async-job-runbook.md:319`、`:704`，以及 `docs/operations/compose-minimal.md` 第 6 节 | 两份手册都假设"已发布 binding + 完整 execution_defaults + 工作空间配额"是既有事实，却没有任何步骤产生它们；第 6 节只写回 `SPEECHNEXUS_BINDING_WRITER_DATABASE_URL`，没有草稿/发布命令。结果是"照文档起一套"永远创建不出 Job | 二选一：补一节"从零发布一个 binding（含 manifest 与 evidence）"并纳入自检；或明确写"本参考部署不产生 binding，异步 Job 需要另按 [发布 binding](publish-binding-revision.md) 与[绑定生命周期](binding-lifecycle.md) 取证"，同时把它列为创建 Job 的前置 |
| D7 | `deploy/job-startup-gate.py:538`（守卫 SQL 在 `backend/src/speechnexus_backend/job_dispatch_runtime.py:337` 的 `has_schema_privilege`） | 迁移未执行时，角色守卫先于 `migration_findings` 运行，`has_schema_privilege` 对不存在的 `speech` schema 抛 `InvalidSchemaName`，未捕获 → 直接 traceback 退出；手册承诺的"迁移缺失点名"不出现 | 先跑 `migration_findings`（它用 `to_regclass`，schema 缺失也安全），或给守卫 SQL 加 `to_regnamespace(schema) IS NOT NULL` 前置判断，并捕获 `InvalidSchemaName` |
| D8 | `backend/src/speechnexus_backend/speech_runtime.py:91` → `backend/src/speechnexus_backend/speech/infrastructure/tts_settlement_preflight.py:73` | 迁移未执行时 API 启动在同步 TTS 结算预检里抛 `UndefinedTable: relation "admission_leases" does not exist`，未捕获 traceback、进程退出并进入重启循环；没有"先跑迁移"的点名拒绝 | 预检捕获缺表/缺 schema，按启动闸门的风格输出"迁移未执行，先跑 migration profile"并退出码非零 |
| D9 | `backend/src/speechnexus_backend/speech/interfaces/http/jobs.py:788` | writer/admission 缺失时统一 `503 job_unavailable`，不点名缺哪个 DSN；这与手册写法一致，但不满足 `#572` 的"错误点名缺的是哪一个前置" | 至少在启动期日志点名哪些可选 DSN 为空，或在错误 `message` 里给出可操作提示（不回显 DSN 内容） |
| D10 | `deploy/compose.yaml:1` 与两份手册第 1 节 | Compose 文件自带 `name: speechnexus`，手册没提独立项目名；两套部署直接相遇时会争抢容器/卷/网络名 | 手册第 1 节显式给出 `COMPOSE_PROJECT_NAME`（或 `-p`）示例，并说明端口/子网/项目名三者一起改 |
| D11 | `docs/operations/async-job-runbook.md:323` | 引擎健康探测写成在**操作主机**上 `curl "${SPEECHNEXUS_ENGINE_HEALTH_URL%/}/health"`，而示例值是 `host.docker.internal`，操作主机上不可解析/不可达（本轮 `curl` 得到 `Empty reply from server`，注册得到 `engine_unreachable`） | 改成在注册容器同一网络内探测（例如 `docker compose run --rm --entrypoint curl engine-register …`），或用可被两处共同解析的地址，并说明 `host.docker.internal` 只对容器有意义 |

## 8. 未验证项

| 项 | 状态 | 原因 |
| --- | --- | --- |
| 真实引擎注册（`outcome=registered`） | 未验证 | 本机无引擎；`engine_unreachable`，退出码 `5` |
| 创建 Job（`202 queued`） | 未验证 | 无已发布 binding，`422 model_not_published`；也没有引擎 |
| claim、提交、租约、结果发布、制品下载 | 未验证 | 没有 Job 可被领取；worker 只会输出 `claimed=0` 批次 |
| `POST /v1/nexus/assets` 与音频检查器 | 未验证 | 创建在资产校验之前就失败，本轮没有启用 `asset-inspection` profile |
| VAD 模型实际解码 | 未验证 | 网关只校验文件摘要；没有 Job 触发 ASR |
| 同步 TTS 完整请求 | 未验证 | API 启动预检通过，但没有进行真实调用 |
| 工作台浏览器段 | 未验证（按任务范围排除） | 另有同版证据；本轮不重复 |
| 网络命名空间重建（网关重建后 worker 重建） | 仅核对静态接线与 `HostConfig.NetworkMode` | 未在重建网关后复现 |

## 9. 清理与最终状态

按任务要求使用独立项目与子网，测后连同卷删除：

```text
$ docker compose ... --profile job-dispatch --profile asset-inspection \
    --profile migration --profile engine-registration --profile engine-readiness down -v
 Network sn878_default Removed
 Network sn878_asset-inspection Removed
 Volume sn878_postgres-data Removed
 Volume sn878_minio-data Removed

$ docker ps -a --filter label=com.docker.compose.project=sn878 --format '{{.Names}}'
（空）
$ docker network ls --filter label=com.docker.compose.project=sn878 --format '{{.Name}}'
（空）
$ docker volume ls --filter label=com.docker.compose.project=sn878 --format '{{.Name}}'
（空）
```

清理前栈状态是 5 个 `Up` 服务（`postgres`/`minio`/`gateway`/`api`/`job-dispatch-worker`，
均 healthy 或有轮询心跳）。没有碰其它 Compose 项目、共享容器或宿主上的既有服务。

## 10. 复现要点

1. 空数据卷 + 独立 `COMPOSE_PROJECT_NAME`，先按本机网络情况一次性调整子网、MinIO 静态地址与
   两份私有 gateway endpoint（§3），其余步骤照 `compose-minimal.md` 第 0–8 节顺序执行。
2. 所有 `docker exec <默认容器名>` 形式的核对命令都要换成项目实际容器名或
   `docker compose exec`（D1）。
3. 没有真实引擎时，止步于第 8 节并记录 `engine_unreachable`；不要用 fake 引擎、不要手写
   `engine_instances`/`leases`、不要注入制品。
4. 想真正跑通一条 Job，除引擎外还必须先解决 §5 的 binding/配额缺口（D6）。

## 11. DEL-535：MegaCube 上带真实引擎的从零实跑

本节记录 2026-11-16 在 MegaCube（aarch64 + GB10）上、以独立 Compose 项目从空 PostgreSQL 卷与空
MinIO 卷出发，按[最小 Compose 部署](compose-minimal.md) §0–§7 与
[异步 Job 六项前置](async-job-runbook.md) §7.1–§7.3 起栈，并在**真实 GPU 引擎**上跑通一条 ASR
Job 的窗口。它与 §4–§10 那次（macOS、无本地引擎、止步注册）的区别是：这次 202 → succeeded 全链
通过；相同的是**都不是零修正通过**——本轮按文档执行时暴露了 9 处文档/样例偏差（§11.3，已由
`#890` 修正）。

### 11.1 基线与镜像

| 项 | 值 |
| --- | --- |
| 日期 | 2026-11-16 |
| 仓库基线 | `origin/main` `c0172a22245c6247fa6ea547d120b0b2f1f8faa0`（`2026-11-16T02:41:30Z`，`#880`） |
| 工作副本 | 该 SHA 的 codeload 源码归档，`sha256:2b74b5ca58b0711713e8b408899b4f006cedc886c4734d0ccca4e969e81243ae`（无 `.git`，不是 Git checkout） |
| 主机 | aarch64 + GB10（统一内存，见 §11.3 第 7 条） |
| Compose 项目 | `del535`，独立子网与宿主端口；profile 为 `migration`、`engine-registration`、`job-dispatch`，另起 `asset-inspection`（`POST /v1/nexus/assets` 需要） |
| 未起 | `web` / `workbench-api`（本条不要求同版工作台读取，`DEL-534` 已另证） |

显式构建 7 个应用镜像，`BUILD_EXIT=0`，耗时 32.783 s：

| 镜像 | 镜像 ID |
| --- | --- |
| `del535-api` | `sha256:1507615877b6ca8c9a9bbc7e068b08c3e2a29dd35c994e7f6131bf3cdc39e193` |
| `del535-gateway` | `sha256:dd1ac08d1d5266baa3ddff099d4cfc0df0661bb65d01904cfc10579b440f917f` |
| `del535-migrate` | `sha256:201852588fa90449aa8206435d8d93f286fb73031e011c482d27dac3d869495f` |
| `del535-engine-register` | `sha256:5815e8fb61e00290c707347fa67a9136bf41dfce62c5395131e8784700dbb9e9` |
| `del535-job-dispatch-worker` | `sha256:1a064b5c7b10bac435338c654ae49d3c624db1f71f86d1910282e3126234a22e` |
| `del535-minio-init` | `sha256:93e3d90b7d9548899f3a49ce9f0b75320e430a1669fcda7f5c7f4825070cbfb0` |
| `del535-asset-inspector` | `sha256:979864dacad50ec59ff019ad49272c2a4124d41546c2fcc281d9d32d445229d4` |

MinIO **未**从源码构建：`deploy/images/minio.Dockerfile` 构建期取 `raw.githubusercontent.com` 的
LICENSE 做二次校验，该窗口不可达，构建失败，改用同 tag 的既有镜像
`speechnexus-minio:RELEASE.2025-09-07T16-13-09Z`。这条替代路径已按
[`compose-minimal.md` §0](compose-minimal.md) 的说明记为「未验证」，本窗口也没有验证从源码构建。

应用镜像不带 `org.opencontainers.image.revision` 标签，镜像到提交只绑定到上面那份源码归档，
**未做镜像级 provenance 核验**。

### 11.2 Job 时间线与标识

成功的 Job 与首轮被隔离的 Job 标识分开记录，不混用：

| 事实 | 值 |
| --- | --- |
| 资产上传 | `POST /v1/nexus/assets` → `201`；`asset-462c19b3616c4dc6a8111ebf18b196a9`，`audio/wav`，`bytes=195356`，`duration_ms=4429`，`sample_rate_hz=22050`，`channels=1` |
| 创建 | `POST /v1/nexus/jobs` → `202`；`job-6d2c1d0fd54e4cf8b169467cf6db3d6a` |
| 时间线 | `create-start 03:29:40.864970Z` → `202 03:29:40.940964Z` → `queued 03:29:40.963519Z` → `running 03:29:45.089984Z` → `succeeded 03:29:46.639292Z` |
| Job 文档 | `status=succeeded`、`cleanup_state=complete`、`catalog_revision=2`、`asr=file-asr-qwen3-1p7b@1` |
| 制品 | `artifact-5b93ea73e08c4740d1445673ea45dbbf3a9c062636d3f0c5`，`kind=transcript`，`bytes=92`，`sha256=sha256:3002c23c5e859f295f3bec557a363a7fe5359a3db50b351bfffc9885e5e21d5d`；`GET …/content` `200`，`content-length: 92`，`ETag` 与该 `sha256` 一致 |
| 执行意图 | `submit-519940080fc4f02066b7de05a16a246ef757ca6720fbe933`，`execution_state=succeeded`、`cleanup_state=complete`、无 `error_code`；`engine_request_id=file-command-461b308a7e7949d7848eb891ca9750b8` |
| 停止/释放证据 | `engine_terminal_response`，`execution_stopped=1`，`resources_released=1` |
| 租约 | `lease-6d2c1d0fd54e4cf8b169467cf6db3d6a`，`lease_state=released`、`cleanup_state=complete`、`reserved_slots=1` |
| 配额/实例 | `workspace_quotas` 的 `held_attempts=0`、`quarantined_attempts=0`；`engine_instances` 的 `held_slots=0`、`quarantined_slots=0`、`instance_state=ready` |
| worker | 一个批次 `claimed=1 succeeded=1 failed=0 unknown=0 lease_lost=0` |
| gateway | `POST /internal/v1/executions/submit` `200`、`staging-result` `200` |
| 引擎侧 | 共享 GPU 主机上既有的 vLLM 0.28.0 引擎（未重启、未改配置）日志各出现一次 `POST /v1/audio/transcriptions` `200 OK` |

首轮失败 Job 仅作对照：`job-7fa7edef9bc649ef8c9453e6cdcaec91`，`execution_state=unknown_quarantined`、
`cleanup_state=quarantined`、`error_code=engine_transport_unknown`、租约 `reserved`、`held_slots=1`，
引擎日志没有任何转写请求。容器内对冻结请求做哨兵诊断得到
`ExecutionInputError('execution_input_scope_mismatch')`，即 §11.3 第 1 条。

### 11.3 这轮暴露的 9 条文档/样例偏差

完整原文与实际输出在 `#890`；这里只记「实跑看到什么」与处置位置。

| # | 实跑看到 | 处置 |
| --- | --- | --- |
| 1 | 样例私配 `objects.input_bucket=speechnexus-inputs`，而 API 侧 Job 资产写 `SPEECHNEXUS_OBJECT_STORAGE_BUCKET=speechnexus-results`；gateway 抛 `execution_input_scope_mismatch`，首轮 Job 在调用引擎前被隔离 | 样例改成资产桶，`compose-minimal.md` §3 点名「Job 输入资产桶」，`tests/ci` 加样例一致性守卫 |
| 2 | `verify-file-asr-artifacts.py --emit-binding-request` 产出含 `parameter_defaults`，直接 POST 运维建草稿路由得 `400 invalid_binding_request` | §7.1 写明工具输出是生产者路由的请求体，运维路由的 `execution_defaults` 要另写；工具 help 与 docstring 同步标注 |
| 3 | 工具从 `binding-definition.json` 取 `release_tier=production`，实验部署必须 `experimental` | §7.1 增加「实验部署填 `experimental`，工具输出不等于生产资格」 |
| 4 | 配好 `sn_catalog_reader` 后 `GET /v1/models` 仍 `503 model_catalogue_unavailable` | 本节只订正 §7.1 第 4 步的预期值为 `503` 并指向产品缺陷 `#891`；不放宽 catalog 校验 |
| 5 | 容器内探活打印 `http= 200 None`（`/health` 无 `content-type`） | §7.2 把预期写成 `http= 200 None` |
| 6 | 宿主 `psql "$…_DATABASE_URL"` 不可用：DSN host 是服务名、宿主也没有 `psql`（退出 `127`） | §5、§6、§7.2 三处改为 `docker compose exec -T postgres psql` |
| 7 | GB10 是 `unified_memory` 拓扑：`allocatable_device_memory_bytes=0`，容量在 `allocatable_unified_physical_memory_bytes=12884901888` | §7.3 按 `memory_architecture` 选维度核对，SQL 增加 unified 列 |
| 8 | 第一次 `down -v` 退出 `0` 后仍留 1 个 `com.docker.compose.oneoff=True` 容器与 2 个网络 | `compose-minimal.md` §10.1 增加 one-off 容器的识别与清理 |
| 9 | `deploy/images/minio.Dockerfile` 构建期需要外网，本窗口不可达 → 构建失败 | 只写文档：`§0` 记「无外网时复用同 tag 既有镜像，此路径未验」，不改构建逻辑 |

### 11.4 收尾：label 复核为空

最终 `down -v` 后再按 `compose-minimal.md` §10.1 的三项 label filter 复核（`--format` 未省略），
`com.docker.compose.project=del535` 的容器、网络、卷**均为空**：

```text
containers_begin
containers_end

networks_begin
networks_end

volumes_begin
volumes_end
```

第一次 `down -v` 的退出码是 `0` 但仍有残留（§11.3 第 8 条）；补齐一次性容器与网络后才得到上面的
空输出。共享 GPU 主机上两台既有引擎未被触碰：启动时间与镜像摘要均未变。

### 11.5 未验证项

- `GET /v1/models` 未取得 `200`（产品缺陷 `#891`，见 §11.3 第 4 条），未擅自改产品。
- 未起 `web` / `workbench-api`，未验证同版工作台读取。
- 未测同步 `/v1/audio/transcriptions`、TTS、voice clone、voice design。
- 未测 `speechnexus-register-engine` 重跑 `already_ready`、readiness 收据过期与续期。
- 未测 `SPEECHNEXUS_ASSET_ALLOWED_ORIGINS` override 路径（默认配置上传已成功）。
- 宿主直连 `psql` 未验证（宿主无 `psql`），全部用容器内 `psql`。
- MinIO 未从源码构建成功，复用了同 tag 既有镜像。
- 应用镜像无 revision 标签，镜像→提交的绑定只到源码归档 SHA 一级。

## 12. DEL-537：同一套参考部署上的同步 TTS 前置实测（2026-11-17）

在按 §0–§7 从空卷起好的同一套参考部署上，把同步 TTS 从「已装配」推到「请求一次」，本轮记下
的是**前置输入缺席时的实际形态**，不是产品缺陷，也不是这条链路已经可用：

- `POST /v1/audio/speech` 稳定返回 `503 backend_unavailable`；API 日志为
  `Synchronous speech unavailable; no current qualified voice`。
- `pg_roles` 里只有 `sn_operator_attestation` 与 `sn_qualification_reader` 两个资格相关身份，
  没有资格准入、来源批准与内建声音发布身份。
- 资格事实全为 0：`source_approvals=0`、`approvals=0`、`qualifications=0`、`attestations=0`、
  `provenance=0`、`voice_publications=0`；`speechnexus-qualification-provenance` 输出
  `controller_observed=0`、`operator_attested=0`。
- 带外文件在宿主上不存在：`capacity-manifest.json`、`qualification-report.json`、
  `scenario-evidence.json`、artifact pin、信任声明与声明请求。仓库
  `deploy/engines/file-tts-customvoice/` 只有 `README.md`、`artifact-pin.json`、
  `binding-definition.json`、`runtime-pin.json`、`template.example.json` 与
  `warmup-request.json`。
- 两条 CLI 在任何写入前失败：`speechnexus-attest-builtin-voice` →
  `invalid attestation input: attestation_request_invalid`（退出码 `2`）；
  `speechnexus-run-builtin-qualification` → `invalid private qualification input`（退出码 `2`）。
- gateway 私有端点直接照抄样例（`address=10.0.0.11`、`port=18101`、`bearer_token` 为 64 个 0）
  时引擎回 `401`；换成真实地址与该引擎的 `VLLM_API_KEY` 后，引擎 `/v1/models` 与
  `/v1/audio/speech` 都回 `200`。也就是说 `401` 来自样例占位值，不是网关或 API 的缺陷。

结论：`503 backend_unavailable` 在这套栈上是**必然结果**——参考部署只覆盖异步主链与同步语音
基座，四件测量原件、信任声明与三个受限身份都由部署方提供。补齐路径见
[最小部署 §6「同步 TTS 资格的部署前置」](compose-minimal.md#同步-tts-资格的部署前置) 与
[builtin voice qualification](builtin-voice-qualification.md#前置输入清单)。

本轮未验证：没有在参考部署上取得同步 TTS 的 `200`（缺 GPU 窗口与原件），未运行资格探针、
声明写入与发布命令，未测异步 Job 与工作台。
