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
[`compose-minimal.md:76`（该行须含 `三者要一起确定`）](compose-minimal.md) 的"其他网段"说明，三处必须一起改：

1. `deploy/compose.yaml:850` 的子网 `172.28.0.0/24` → `172.30.0.0/24`；
2. `deploy/compose.yaml:90` 的 MinIO 静态地址 `172.28.0.10` → `172.30.0.10`；
3. 两份私有 gateway 配置的 `objects.endpoint`（`deploy/private/gateway-file-asr.example.json:8`、
   `gateway-file-tts.example.json:11`）同步改为 `http://172.30.0.10:9000`。

宿主端口全部走环境文件的既有变量，没有改 Compose 定义。这些改动只存在于测后已删除的
worktree 运行态，不进入本 PR，也不是从零手册的额外步骤。

另外一项外部输入：`compose-minimal.md:246`（该行须含 `SPEECHNEXUS_GATEWAY_LOCK_DIR`） 要求把已批准的 Silero ONNX 文件放进私有目录。
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

与 `compose-minimal.md:54`（该行须含 `显式点名服务时 Compose 只构建点到的镜像`） 的预期集合一致；`--profile job-dispatch config --services` 输出
`api gateway job-dispatch-worker minio minio-init postgres`，与 `async-job-runbook.md:109`（该行须含 `job-dispatch-worker`）
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

与 `compose-minimal.md:334`（该行须含 `quota_dimensions`） 的预期逐字一致。容器名是 `sn878-*`，见 §7 第 1 条。

### 4.5 第 5 节：迁移

```text
$ docker compose ... --profile migration run --rm migrate
migrate_exit=0        # 容器内无迁移日志

$ docker exec sn878-postgres-1 psql -U speechnexus -d speechnexus -Atc \
    'select version_num from speech.alembic_version'
0054_artifact_deleted_ledger
```

与 `compose-minimal.md:366`（该行须含 `SPEECHNEXUS_EXECUTION_RESULT_BUCKET`） 声明的当前 head 一致。文档同处的
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

与 `compose-minimal.md:2303`（该行须含 `speechnexus-api`） 起的预期逐字一致（仅宿主端口不同）。网关私有的 VAD 文件、
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

`job-dispatch` 的启动闸门与 `async-job-runbook.md:45`（该行须含 `上传判定`） 描述一致：`up -d` 的成功不代表就绪，
就绪要看 `RestartCount`、日志里的闸门结论与批次行。

只读自检（`async-job-runbook.md:15`（该行须含 `配套的只读自检是`））在部署后运行：

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

`async-job-runbook.md:704`（该行须含 `select workspace_id, max_attempts`） 与第 11 节假设"六项前置满足后 `POST /v1/nexus/jobs` 才不再返回
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
   手册只把"已发布 binding"当作既有事实引用（`async-job-runbook.md:319`（该行须含 `FAILED: %s`））。

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
| D1 | `docs/operations/compose-minimal.md:361`（该行须含 `result_object_scope_rejected`）（同类还有 `:880`、`:920`、`:1064`、`:1077`） | 核对命令硬编码默认项目名的容器名 `speechnexus-postgres-1`；用独立 Compose 项目（本项目 Agent 约定要求）时直接 `No such container` | 改成 `"${COMPOSE[@]}" --env-file deploy/compose.env -f deploy/compose.yaml exec -T postgres psql …`，或在第 1 节声明项目名变量并统一使用 |
| D2 | `deploy/compose.yaml:90`、`:850` 与 `docs/operations/compose-minimal.md:105`（该行须含 `172.28.0.0/24`） | 子网与 MinIO 静态地址硬编码；文档只说"一起调整部署配置"，没有环境变量或受支持的 override 入口。本机 `172.28.0.0/24` 已被其它项目占用，从零必须改仓库文件（含四份 private 示例） | 提供 `SPEECHNEXUS_COMPOSE_SUBNET` / `SPEECHNEXUS_MINIO_STATIC_IP` 之类的 env 键，或提供并测试一份受支持的 override 文件 |
| D3 | `docs/operations/compose-minimal.md:246`（该行须含 `SPEECHNEXUS_GATEWAY_LOCK_DIR`） 与 `deploy/engines/file-asr-audio/artifact-pin.json:139` | VAD 模型只有一句"把已批准的 Silero ONNX 普通文件放到私有目录"：没有获取来源、没有校验命令；仓库钉的摘要是 `sha256:1a153a22…`，与常见 Silero 发行版（本轮实测 `597d30b3…`）不同。网关只校验配置里的摘要，因此"任何 Silero ONNX"都能过闸门，但没人能判断哪份是"已批准" | 在 §3 增加受控来源与 `sha256` 核对步骤（例如用 `artifact-pin.json` 的摘要校验），并说明摘要不符时网关的具体报错 |
| D4 | `docs/operations/async-job-runbook.md:32`（该行须含 `迁移到唯一 head`） | 第 4 项写"没有实例时创建返回 `429 job_capacity_exhausted`"；从零实测先得到 `422 model_not_published`（binding 未发布），容量判定在 `public_job_admission.py:253`，排在绑定、输入资产与配额之后 | 改成"已发布 binding 且工作空间配额存在时，才由容量返回 429；从零未发布 binding 时先 422" |
| D5 | `docs/operations/async-job-runbook.md:109`（该行须含 `job-dispatch-worker`） | "只配 writer 时创建仍返回 `503 job_unavailable`"；实测（writer 已配、admission 为空、无 binding）返回 `422 model_not_published`。只有请求走到准入时才会是 503 | 加"在有已发布 binding 的部署上"限定，并同时给出从零会先看到的 422 |
| D6 | `docs/operations/async-job-runbook.md:319`（该行须含 `dispatch probes ok`）、`:704`，以及 `docs/operations/compose-minimal.md` 第 6 节 | 两份手册都假设"已发布 binding + 完整 execution_defaults + 工作空间配额"是既有事实，却没有任何步骤产生它们；第 6 节只写回 `SPEECHNEXUS_BINDING_WRITER_DATABASE_URL`，没有草稿/发布命令。结果是"照文档起一套"永远创建不出 Job | 二选一：补一节"从零发布一个 binding（含 manifest 与 evidence）"并纳入自检；或明确写"本参考部署不产生 binding，异步 Job 需要另按 [发布 binding](publish-binding-revision.md) 与[绑定生命周期](binding-lifecycle.md) 取证"，同时把它列为创建 Job 的前置 |
| D7 | `deploy/job-startup-gate.py:538`（守卫 SQL 在 `backend/src/speechnexus_backend/job_dispatch_runtime.py:337` 的 `has_schema_privilege`） | 迁移未执行时，角色守卫先于 `migration_findings` 运行，`has_schema_privilege` 对不存在的 `speech` schema 抛 `InvalidSchemaName`，未捕获 → 直接 traceback 退出；手册承诺的"迁移缺失点名"不出现 | 先跑 `migration_findings`（它用 `to_regclass`，schema 缺失也安全），或给守卫 SQL 加 `to_regnamespace(schema) IS NOT NULL` 前置判断，并捕获 `InvalidSchemaName` |
| D8 | `backend/src/speechnexus_backend/speech_runtime.py:91` → `backend/src/speechnexus_backend/speech/infrastructure/tts_settlement_preflight.py:73` | 迁移未执行时 API 启动在同步 TTS 结算预检里抛 `UndefinedTable: relation "admission_leases" does not exist`，未捕获 traceback、进程退出并进入重启循环；没有"先跑迁移"的点名拒绝 | 预检捕获缺表/缺 schema，按启动闸门的风格输出"迁移未执行，先跑 migration profile"并退出码非零 |
| D9 | `backend/src/speechnexus_backend/speech/interfaces/http/jobs.py:788` | writer/admission 缺失时统一 `503 job_unavailable`，不点名缺哪个 DSN；这与手册写法一致，但不满足 `#572` 的"错误点名缺的是哪一个前置" | 至少在启动期日志点名哪些可选 DSN 为空，或在错误 `message` 里给出可操作提示（不回显 DSN 内容） |
| D10 | `deploy/compose.yaml:1` 与两份手册第 1 节 | Compose 文件自带 `name: speechnexus`，手册没提独立项目名；两套部署直接相遇时会争抢容器/卷/网络名 | 手册第 1 节显式给出 `COMPOSE_PROJECT_NAME`（或 `-p`）示例，并说明端口/子网/项目名三者一起改 |
| D11 | `docs/operations/async-job-runbook.md:634`（该行须含 `只会得到连接`） | 引擎健康探测写成在**操作主机**上 `curl "${SPEECHNEXUS_ENGINE_HEALTH_URL%/}/health"`，而示例值是 `host.docker.internal`，操作主机上不可解析/不可达（本轮 `curl` 得到 `Empty reply from server`，注册得到 `engine_unreachable`） | 改成在注册容器同一网络内探测（例如 `docker compose run --rm --entrypoint curl engine-register …`），或用可被两处共同解析的地址，并说明 `host.docker.internal` 只对容器有意义 |

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

## 13. 从空库到第一个 `202` 的步骤清单

本节是这份记录里唯一按顺序可直接照做的部分：把从空库到 `POST /v1/nexus/jobs` 返回 `202`
的顺序、每一步的前置检查、预期输出与缺失时的表现列在一起。命令正文仍在
[最小 Compose 部署](compose-minimal.md) 与[异步 Job 六项前置](async-job-runbook.md)，
本节不复述，只给顺序、判据和可复跑的检查入口
[`scripts/dev-env/check-from-empty-first-job.sh`](../../scripts/dev-env/check-from-empty-first-job.sh)
（覆盖下表第 9–14 步的应用层动作，拓扑层按手册执行）。

创建请求的判定顺序是固定的，缺项可以直接从返回码定位（2026-11-17 实测，见 §14）：

| 顺序 | 判定的事实 | 缺失时的返回 | 依据 |
| --- | --- | --- | --- |
| 1 | 该 `model` 有已发布 binding revision | `422 model_not_published` | `backend/src/speechnexus_backend/speech/application/public_jobs.py:912` |
| 2 | 请求里的输入资产已检查且 `ready`、用途匹配 | `422 asset_not_ready` | `backend/src/speechnexus_backend/speech/application/public_jobs.py:1490`（在准入之前） |
| 3 | 该工作空间有 `workspace_quotas` 行 | `422 workspace_not_configured` | `backend/src/speechnexus_backend/speech/infrastructure/public_job_admission.py:201` |
| 4 | 与已发布 revision 身份一致的 `ready` 实例、active 池 owner 与容量 | `429 job_capacity_exhausted` | `backend/src/speechnexus_backend/speech/infrastructure/public_job_admission.py:253` |

因此「没有实例时返回 `429`」只在第 1–3 项都成立时可见；从空库最先看到的是第 1 项。
第 2 项排在准入之前，所以「配额还没建」的部署上，先撞到的是资产未就绪而不是配额。

### 13.1 步骤表

| # | 步骤 | 权威命令 | 前置检查 | 预期输出 | 缺失时 |
| --- | --- | --- | --- | --- | --- |
| 1 | 定项目名、子网、宿主端口 | [§1](compose-minimal.md#1-固定服务集合)、[§1.1](compose-minimal.md)、[§2](compose-minimal.md#2-准备环境文件) | `"${COMPOSE[@]}" … config \| grep -E 'ipv4_address:\|subnet:\|published:'` | 与 `deploy/compose.env` 里 `SPEECHNEXUS_COMPOSE_SUBNET` / `SPEECHNEXUS_MINIO_STATIC_IP` / 宿主端口一致 | 子网被占时网络创建 `Pool overlaps`；两项都由 env 承载，**不需要改仓库文件** |
| 2 | 应用镜像 | [§0](compose-minimal.md#0-构建前提) | `docker images` 里有 `<项目>-api` 等 7 个 | `BUILD_EXIT=0`；`minio` 复用同 tag 既有镜像 | 缺 `speechnexus-minio:<tag>` 时 `up` 会现场构建并在取 LICENSE 处失败 |
| 3 | 私有文件 + VAD 摘要 | [§3](compose-minimal.md#3-准备私有文件) | `python3 -m json.tool` 六份 + `chmod 600` 检查 | `vad_model_sha256` 等于仓库钉的摘要 | 全零摘要拒绝启动；摘要与文件不符只在首个 Job 的执行段暴露（§4 记录） |
| 4 | postgres / minio / 桶 | [§4](compose-minimal.md#4-启动数据库与对象存储) | `curl -fsS http://127.0.0.1:<S3端口>/minio/health/live` | `bucket ready: speechnexus-results (versioning enabled)`（同步输入桶、Artifact 桶与 staging 桶默认同名，只建一个） | 桶缺失 → 派发启动闸门点名桶名与键名 |
| 5 | 迁移到唯一 head | [§5](compose-minimal.md#5-迁移到唯一-head) | `select version_num from speech.alembic_version` | 唯一 head | 未迁移：API 起不来（traceback）、闸门先抛 `InvalidSchemaName` |
| 6 | 非 owner 角色 + 三个 Job 角色 | [§6](compose-minimal.md#6-创建非-owner-运行角色与最小授权) | `provision-job-roles.py --print-statements` | `roles ready: …`、`job roles ready: …`（语句数以 `--print-statements` 为准） | 缺角色：可选能力稳定 `503`，Job 侧闸门点名五组 grant |
| 7 | api / gateway | [§7](compose-minimal.md#7-启动-api-与-gateway) | 四个公开探针 | `/healthz`、`/readyz` 全 `200` | 起不来说明 §2–§6 有缺项，按探针 `detail` 定位 |
| 8 | `asset-inspector`（音频检查器） | [runbook §10](async-job-runbook.md) | 从 `api` 容器内请求检查器 `/readyz` | `{"contract_version":1,"recipe_version":"pcm-decode-1",…}` | 未装配：音频上传 `503 asset_inspection_unavailable`，Job 永远到不了第 2 项判定 |
| 9 | 发布 binding | [runbook §7.1](async-job-runbook.md#71-发布-binding注册与创建-job-的前置) | 三条 admin 路由：草稿、发布、激活 | `201` / `201`（`publication.status: published`）/ `200` | `422 model_not_published`（第 1 项判定） |
| 10 | 回写私有输入 | [runbook §7.1 第 5 步](async-job-runbook.md#71-发布-binding注册与创建-job-的前置) | 配方 `recipe_digest` 自洽、`binding_revision_id` 等于已发布 `revision_id` | 重启后 API 启动无 `invalid transcription recipe` | 按 runbook 只影响执行段（本轮未跑到执行，未验证）：`202` 仍可创建 |
| 11 | 上传输入资产 | [runbook §11](async-job-runbook.md#11-第-7-步提交并观察一次-job) | `POST /v1/nexus/assets`（`purpose=asr_input`） | `201`、`status: ready`、检查器回执的时长/采样率/字节数 | 资产未就绪：`422 asset_not_ready`（第 2 项判定） |
| 12 | 注册引擎 | [runbook §7.2](async-job-runbook.md) | 从注册容器内探一次 `/health` | `"outcome": "registered"`（重跑 `already_ready`） | `engine_unreachable`、`quota_policy_conflict`、`instance_config_conflict` 等，见 §13.2 |
| 13 | 核对三类事实 | [runbook §7.3](async-job-runbook.md#73-核对配额与容量登记) | 三条只读 SQL | 每类各一行 | 缺哪行对应第 3 / 4 项判定 |
| 14 | 创建 Job | [runbook §11](async-job-runbook.md#11-第-7-步提交并观察一次-job) | `POST /v1/nexus/jobs` | `202`、`status: queued` | §13 开头的四类码 |

顺序约束是硬的：第 12 步写入实例的 `binding_revision_id` 必须等于第 9 步发布出来的
`revision_id`。反过来说明「注册成功」不能当作「binding 已发布」的证据——注册路径不查
`speech_publication_heads`，两边要分别核对第 13 步的三行。

### 13.2 补上的三处步骤：入口、产物与失败面

三处过去只在别的手册里被当作既有事实，现在都有明确的产生入口，且都不需要手写 SQL。

**（1）发布 binding。** 入口是三条已装配的写路由，不是 CLI：`POST
/v1/nexus/admin/model-bindings`（草稿）→ `POST …/{binding_id}/publications` → `POST
…/{binding_id}/revisions/{revision}/activations`。产物是一个事务里的 `speech_publications`、
`speech_binding_publication_evidence`、`speech_publication_heads` 与推进后的
`speech_catalog_state`。前置是 `sn_binding_writer` 与 `sn_binding_lifecycle` 两个身份
（第 6 步创建）；缺身份时草稿 `503 binding_unavailable`、发布 `503 binding_result_unknown`。
发布不是幂等重发：目标已发布时 `409 binding_not_draft`，head 变化时 `409 configuration_changed`。
请求体的字段取值来源逐项见[runbook §7.1](async-job-runbook.md#71-发布-binding注册与创建-job-的前置)。
`execution_defaults` 在草稿请求里是可选的，但少了 ASR 段或缺所需的 13 个字段之一，创建时是
`422 job_binding_incomplete`（`backend/src/speechnexus_backend/speech/infrastructure/public_job_admission.py:138`）；
给出但不合法的取值则在草稿期就是 `400 invalid_binding_request`。

**（2）工作空间配额。** `workspace_quotas` 没有独立管理入口：`POST /v1/nexus/workspaces`
只写 `speech_workspaces`，配额行只由注册写入（`backend/src/speechnexus_backend/engine_registration.py:488`
的 `_ensure_quota`，INSERT 在 `:490`）。同一工作空间重复注册时 `policy_revision` 必须与已有行一致，
否则注册以 `quota_policy_conflict` 退出
（`backend/src/speechnexus_backend/engine_registration.py:502`）。约束对照的是**配方**的
`policy_revision_id`，不是 binding 的字段：`workspace_quotas.policy_revision` 取注册输入，
gateway 在执行期再按私有端点配置的 `vad_tuning.config_revision` 逐字段比对。

**（3）宿主机容量。** `host_nodes` 同样只有一个写入口：`_ensure_host`
（`backend/src/speechnexus_backend/engine_registration.py:364`，INSERT 在 `:370`）。容量向量
来自注册输入的计划（`steady + per_slot × max_slots` 与 `peak` 取大），准入侧不再重读
`host_nodes` 做算术，只要求存在 ready 实例与 active 池 owner。因此「容量登记」不是单独的管理
端点，就是第 12 步本身。

注册侧与本轮实测到的点名失败（每条都是产品侧出口，不是包装）：

| 出口 | 触发方式（实测） | 实际输出 |
| --- | --- | --- |
| `quota_policy_conflict` | 配方 `policy_revision_id` 与已有 `workspace_quotas.policy_revision` 不同 | `error: quota_policy_conflict: existing workspace quota policy revision does not match the recipe`，退出码 `4` |
| `instance_config_conflict` | 发布新 revision 后直接重跑注册（实例仍钉在旧 revision） | `error: instance_config_conflict: the ready instance has different binding, model plan or capacity facts`，退出码 `4` |
| `engine_unreachable` | `/health` 只在操作主机可达 | `error: engine_unreachable: health endpoint is unreachable from this operator host`，退出码 `5` |

### 13.3 「产品侧没有入口、只能手写 SQL」的清单

**0 条。** 本轮从空库到 `202` 的每一步都能走产品入口：binding 走三条 admin 路由，资产走
`POST /v1/nexus/assets`，配额与容量登记走 `speechnexus-register-engine`。全仓库 `INSERT INTO
workspace_quotas` / `INSERT INTO host_nodes` 只出现在 `engine_registration.py`（其余命中都是
测试夹具），也没有对应的 HTTP 管理面，所以这两份事实既**没有第二个入口**，也**不需要手工补**。

下面两类是本轮仍要手工处理的，它们都不是 SQL 写库，但不能算产品入口：

| 事项 | 状态 | 说明 |
| --- | --- | --- |
| 把已发布 `revision_id` 回写私有配方与 gateway 端点配置，并按 RFC 8785 重算 `recipe_digest` | 手工 + 仓库外脚本 | 没有 CLI 接受「binding 已发布，改这两份私有文件」；runbook §7.1 给的是可复制的 Python 片段，本轮按该片段执行 |
| 读回发布 head（发布请求的 `expected_current_revision`） | HTTP 可读 | `GET /v1/nexus/admin/model-bindings` 的 `current_revision` 即可，不必查库；本轮脚本已改用它 |
| 核对三类事实（配额 / 容量 / 实例） | 只读 SQL | 没有只读 HTTP 面暴露 `workspace_quotas` 与 `host_nodes`；这是观测缺口，不是写入口缺口 |

### 13.4 可复跑脚本与两条反例

`scripts/dev-env/check-from-empty-first-job.sh`（空库断言在
[`scripts/dev-env/check-from-empty-first-job.sh:330`](../../scripts/dev-env/check-from-empty-first-job.sh)）
把第 9–14 步做成可按阶段重跑、每步自带断言的脚本：

```bash
scripts/dev-env/check-from-empty-first-job.sh --phase all \
  --url http://127.0.0.1:8137 --gateway-url http://127.0.0.1:8138 --project <Compose 项目名>
```

它不构建镜像、不跑迁移、不改 `deploy/compose.yaml`；阶段为 `require-empty`、`probe`、
`counterexample-unpublished`、`publish`、`asset`、`register`、`create`、`create-expect-429`、
`counterexample-policy`，任一断言不成立即以非零退出。

**这里的 `PASS` 证明什么，不含糊：**

- `--phase all` **第一步**是 `require-empty`：断言 `speech_publications`、`speech_binding_revisions`、
  `workspace_quotas`、`host_nodes`、`engine_instances` 五行计数与 `held_slots` 全为 `0`。任一非零就
  退出 `1` 并逐项点名（例如 `publications=1 binding_revisions=2 held_slots=1`），所以绿色的 `all`
  等价于「从这个空库走到了 `202`」，在已用过的栈上重跑会直接失败而不是悄悄通过。
- `create` 阶段**只在真的拿到 `202` 时**才 `PASS`；容量拒绝是独立的 `create-expect-429` 阶段
  （断言 `429` + `job_capacity_exhausted`）。已占用容量的栈上跑 `create` 会失败并提示改用
  `create-expect-429`，不允许两个分支共用一个模式。
- 两条反例都是常驻检查：`counterexample-unpublished` 与 `counterexample-policy`，`all` 里都会跑到；
  第二条改的是私有配方，阶段结束自动恢复（再注册回到 `already_ready`）。

**已知限制。** 脚本文件被本文档按 `path:line` 引用，所以删掉文件会让
`scripts/dev-env/check-doc-refs.py` 报 `MISSING`、`RESULT FAIL`。但那个门禁只管文件是否存在、
行号是否越界：**保留文件、只删掉脚本里的某一条断言，门禁仍然是 `PASS`**。断言本身的判据是每个阶段
的预期状态码与错误码，改坏或删掉它只有重跑对应阶段才会红（`create` 阶段现在就是这个作用——判定
标准从「202 或 429 都算过」收紧成「只有 202 算过」）。

## 14. 本轮实跑：从空库到 `202`（2026-11-17，macOS）

这一节记录 2026-11-17 在一台 macOS 开发机上、以独立 Compose 项目 `sn935` 从**空
PostgreSQL 卷与空 MinIO 卷**出发，按 §13 走到 `POST /v1/nexus/jobs` 返回 `202` 的实跑。
与 §4–§10 那次（止步于引擎注册）的区别是这次创建请求返回了 `202`；与 §11（MegaCube、真实
GPU 引擎）的区别是**这次没有真实引擎**，注册针对的是一个只回答 `/health` 的本地替身。

### 14.1 环境与基线

| 项 | 值 |
| --- | --- |
| 日期 | 2026-11-17 |
| 主机 | macOS，arm64，无 GPU、无本地 ASR 引擎；Docker Engine 29.9.3、Compose v5.7.0 |
| 仓库基线 | `origin/main` `1721ddb2`（worktree `/private/tmp/sn935`，分支 `docs/from-empty-to-first-job`） |
| 起点 | 新建的 PostgreSQL 16.15 卷、新建的 MinIO 卷，无残留容器/网络/卷 |
| 隔离 | 项目名 `sn935`，子网 `172.31.0.0/24`（`SPEECHNEXUS_COMPOSE_SUBNET`），宿主端口 `8137`/`8138`/`55443`/`59010`/`59011`（全部走 `deploy/compose.env`，**没有改任何仓库文件**） |
| 应用镜像 | 本轮从该 SHA 的源码构建 7 个：`sn935-api f6fb167e6021`、`sn935-gateway 96ae65bf1de3`、`sn935-migrate d421c1336e4f`、`sn935-engine-register 8fb142486a31`、`sn935-job-dispatch-worker 51c4034a9365`、`sn935-minio-init ffbe17302a9e`、`sn935-asset-inspector 38e3cfaf5d66`；`BUILD_EXIT=0` |
| `minio` 镜像 | 复用同 tag 既有镜像 `speechnexus-minio:RELEASE.2025-09-07T16-13-09Z`，未从源码构建 |
| VAD 模型 | 本机已有的 Silero ONNX（操作主机私有目录下的 `silero_vad.onnx`，不记录绝对路径），2327524 字节，`sha256:1a153a22f4509e292a94e67d6f9b85e8deb25b4988682b7e174c65279d8788e3`，与 `deploy/engines/file-asr-audio/artifact-pin.json:139` 钉的摘要**逐字相同**（§4 那次本机只有另一份 `597d30b3…`，因此这里 D3 的摘要核对这次是过的） |
| 未起 | `job-dispatch-worker`、`web`、`workbench-api`；没有真实引擎 |

### 14.2 拓扑层实跑输出

```text
$ docker compose … exec -T postgres psql -U speechnexus -d speechnexus -Atc \
    'select version_num from speech.alembic_version'
0056_debt_probe_live_state_index

$ roles ready: sn_api sn_gateway sn_gateway_query sn_workspace_writer sn_audit_reader \
    sn_model_instance_reader sn_quarantine_release sn_catalog_reader sn_artifact_reader \
    sn_resource_pool_reader sn_binding_list_reader sn_binding_writer sn_binding_admin_reader \
    sn_binding_lifecycle sn_artifact_retention sn_operator_attestation sn_speech_settlement \
    sn_workbench_runtime sn_workbench_cleanup
$ job roles ready: sn_job_writer sn_job_admission sn_job_dispatch; grants: 223 statements

$ curl -sS -w '\nhttp=%{http_code}\n' http://127.0.0.1:8137/readyz
{"status":"ready","checks":[{"name":"postgresql","status":"ok","detail":null},
 {"name":"object_storage","status":"ok","detail":null},
 {"name":"cancellation_debt","status":"ok","detail":"cancellation_debt: unavailable (not configured)"},
 {"name":"stop_evidence_debt","status":"ok","detail":"stop_evidence_debt: unavailable (not configured)"}]}
http=200

$ docker compose … exec -T api python -c "import urllib.request; print(urllib.request.urlopen('http://asset-inspector:8080/readyz', timeout=3).read().decode())"
{"contract_version":1,"recipe_version":"pcm-decode-1","max_encoded_bytes":4194304,"max_wall_seconds":20.0,"in_flight":0}
```

`/readyz` 现在是四项检查，`compose-minimal.md:2218`（该行须含 `本参考部署选择`） 起写的是两项：新增的
`cancellation_debt` / `stop_evidence_debt` 在未配置时按 `ok` 报告，属文档未跟上的漂移（§14.5）。

### 14.3 空库 → `202`：五个关键状态

起始事实（空库，只有迁移与角色）：

```text
publications=0 binding_revisions=0 quotas=0 host_nodes=0 instances=0
catalogue_state=1 workspaces=1
```

**① 未发布 binding：`422`。**

```text
$ curl -sS -X POST http://127.0.0.1:8137/v1/nexus/jobs \
    -H 'content-type: application/json' -H "Idempotency-Key: …" \
    -d '{"kind":"asr","model":"model-compose-example","input":{"audio_asset_id":"asset-000000000000"}}'
http=422
{"error":{"code":"model_not_published","type":"invalid_request_error",
 "message":"The model 'model-compose-example' has no published binding revision and no engine
 instance is registered for it: publish the binding and register the engine
 (engine-registration profile), so admission has a ready instance to select. Retrying the same
 request cannot create either.","param":null,"request_id":"…","retryable":false}}
```

**② 发布 binding：`201` → `201` → `200`。**

```text
draft       http=201  revision=1  revision_id=mbrev-1901846251a84ccabd84a31674a1bdcd
publication http=201  publication.status=published  published_at=2026-11-17T05:40:49.180455Z
activation  http=200  publication.status=published（同一 revision，成功 no-op）
DB         publications=1 heads=1 catalogue_revision=2 quotas=0
```

**③ 已发布、资产未就绪、无配额：`422` 逐项点名。** 先撞到资产（判定第 2 项），修好资产后撞到配额
（第 3 项）：

```text
$ POST /v1/nexus/jobs   model=model-compose-example  audio_asset_id=asset-000000000000（不存在）
http=422 {"error":{"code":"asset_not_ready",…,"message":"The input asset is not ready."}}

$ POST /v1/nexus/assets  purpose=asr_input  file=1 s 16 kHz 单声道合成 WAV
http=201 {"id":"asset-56e3ce4f75b84a32b988d3a2231f4e98","status":"ready","media_type":"audio/wav",
 "bytes":32044,"duration_ms":1000,"sample_rate_hz":16000,"channels":1}

$ POST /v1/nexus/jobs   model=model-compose-example  audio_asset_id=asset-56e3ce4f…
http=422 {"error":{"code":"workspace_not_configured",…,
 "message":"The workspace has no provisioned Job quota.","retryable":false}}
```

**④ 注册引擎：三类事实一次写入。**

```text
$ docker compose … --profile engine-registration run --rm engine-register
"outcome": "registered"

workspace-default|8|0|policy-revision-compose-example
node-local-1|active|allocatable_device_memory=4500000000|allocatable_cpu_decode_millicores=1100
instance-asr-1|1|ready|mbrev-1901846251a84ccabd84a31674a1bdcd|capacity-manifest-compose-example
```

**⑤ 创建 Job：`202`。**

```text
$ curl -sS -w '\nhttp=%{http_code}\n' -X POST http://127.0.0.1:8137/v1/nexus/jobs \
    -H 'content-type: application/json' -H "Idempotency-Key: …" \
    -d '{"kind":"asr","model":"model-compose-example","input":{"audio_asset_id":"asset-56e3ce4f…"}}'
http=202
{"id":"job-34ff980f7d8249d3988b6703c7bf11e4","object":"nexus.job","kind":"asr","status":"queued",
 "cleanup_state":"not_required","workspace_id":"workspace-default","execution_revision":"1",
 "effective_configuration":{"catalog_revision":"2","degradation":"none",
 "models":{"asr":{"id":"model-compose-example","revision":"1"}},"policy_revision":"1"}, …}
```

### 14.4 三条反例的红绿

| 反例 | 做法 | 实际结果 |
| --- | --- | --- |
| 去掉「发布 binding」 | 对 `model-unpublished-example` 只建草稿，不发布，直接创建 | `422 model_not_published`，message 点名 publish + register；脚本 `--phase counterexample-unpublished` 每次运行都验一次 |
| `policy_revision` 与 binding/配额不一致 | 改配方 `policy_revision_id` 为 `policy-revision-mismatch`、重算摘要后重跑注册 | `error: quota_policy_conflict: existing workspace quota policy revision does not match the recipe`，退出码 `4`；脚本 `--phase counterexample-policy`，阶段结束恢复配方 |
| 删掉断言/脚本 | 脚本被本文档按 `path:line` 引用 | 删除 `scripts/dev-env/check-from-empty-first-job.sh` 后 `scripts/dev-env/check-doc-refs.py` 报 `MISSING`，门禁变红 |

第二条的名称来自注册而不是创建：准入侧不比对 `policy_revision`（它把配方里的值透传成执行参数），
所以「配方与配额不一致」在从零路径上表现为**注册拒绝**，而不是创建失败。

### 14.5 实跑暴露的偏差（本次记录，未改产品代码）

| # | 现象 | 位置 | 处置 |
| --- | --- | --- | --- |
| D12 | 实例已 `ready` 后 `GET /v1/models` 仍报 `"engine_ready":false,"admission_state":"unavailable"`：目录的 readiness 是静态投影，从未接入实例事实 | `backend/src/speechnexus_backend/speech/infrastructure/model_catalog_postgres.py:35`（`readiness or ReadinessFacts()` 恒为空），`public_job_admission` 才是真判据 | 本轮只记录：不要用 `/v1/models` 的 readiness 判断能否创建 Job；runbook §7.1 第 4 步「未注册实例时是 unavailable」暗示它会变，需要订正措辞 |
| D13 | `api` 的 `/readyz` 现在有四项检查（多 `cancellation_debt`、`stop_evidence_debt`） | `compose-minimal.md:2218`（该行须含 `本参考部署选择`） 起写两项 | 文档漂移，记录于此 |
| D14 | 发布新 revision 后实例身份失效：直接重跑注册被 `instance_config_conflict` 拒绝；创建变成 `429 job_capacity_exhausted`，message 是「no engine instance is registered for model …」 | `engine_registration.py:638`、`public_job_admission.py:211`–`:253` | 记录：换 revision 需要新的 generation/实例轮换路径，本轮**未验证**该路径 |
| D15 | 单个注册实例的 `max_slots=1`，一个 queued Job 就占满：同一栈上第二次创建是 `429`，message 为「Job capacity is exhausted; retry after the indicated delay.」 | 配方 `runtime_plan.max_slots`、`public_job_admission.py:253` | 不是缺陷，是容量语义：占用持续到该 Job 的租约被释放或准入截止（`retry_after`）到期；写进 §13 判定表第 4 项 |

### 14.6 未验证项

| 项 | 状态 | 原因 |
| --- | --- | --- |
| 真实引擎上的 Job 执行（claim → submit → 制品） | 未验证（本轮） | 注册针对的是只回答 `/health` 的本地替身；同一仓库在 MegaCube 上有真实引擎证据（§11），本轮不复现 |
| 替身 `/health` 之外的行为 | 不存在 | 替身只有 `/health`；`202` 只需要准入事实，不需要引擎能力 |
| `artifact_set` 的制品完整性 | 未核 | 本机没有 ASR 权重，草稿的 `artifact_set`/`engine` 取自仓库钉住的 `artifact-pin.json` 与 `runtime-pin.json`，**未**运行 `verify-file-asr-artifacts.py` 的逐文件校验 |
| 发布新 revision 后的实例/ generation 轮换 | 未验证 | 见 D14 |
| `GET /v1/models` 的 readiness 变为 ready | 未取得 | 见 D12，产品侧未接入 |
| `job-dispatch-worker` 启动闸门与派发 | 未验证（本轮） | 不需要真实引擎也能起闸门，但本轮目标止于 `202`；§4.9 与 §11 各有记录 |
| 同步 ASR/TTS | 未验证 | 不在本卡范围；§12 记录同步 TTS 的前置形态 |

### 14.7 审查后复跑（同一台机器，新空卷）

首轮交付后，审查指出「脚本绿」并不能证明「从空库」——`create` 阶段曾在两条分支里择一通过
（`held_slots≠0` 时就断言 `429`），同一栈上重跑也会 `PASS`。脚本已按此收紧：`all` 第一步断言空库
（五行计数与 `held_slots` 全为 0），`create` 只在真 `202` 时通过，容量拒绝改为独立的
`create-expect-429`。在新空卷上复跑：

```text
$ scripts/dev-env/check-from-empty-first-job.sh --phase all --project sn935 …   # 退出码 0
== phase require-empty ==
PASS empty start: publications=0 binding_revisions=0 workspace_quotas=0 host_nodes=0 engine_instances=0 held_slots=0
== phase counterexample-unpublished ==
PASS create Job without the publication step: error code model_not_published
== phase publish ==
PASS published model-compose-example revision 1 as mbrev-f7e859d84f5a4ab9bb8d113b7177935b
PASS activated model-compose-example revision 1
== phase asset ==
PASS asset asset-b3ae1489a0484c2199f68459881fb7b7 ready (audio/wav, 1000 ms)
== phase register ==
PASS engine-register outcome: registered
PASS workspace_quotas: workspace-default|8|policy-revision-compose-example
PASS host_nodes: node-local-1|active
PASS engine_instances: instance-asr-1|1|ready|mbrev-f7e859d84f5a4ab9bb8d113b7177935b|capacity-manifest-compose-example
== phase create ==
PASS Job job-daeeb965f3c94175872bafaa399e03b8 queued (queued)
== phase counterexample-policy ==
PASS counterexample holds: policy_revision mismatch is exit 4 and names quota_policy_conflict
RESULT PASS
```

同一栈上重跑（证明脚本绿不再能被「用过的栈」伪造）：

```text
$ … --phase all                    → 退出码 1
FAIL the stack is not empty ( publications=1 binding_revisions=2 workspace_quotas=1
     host_nodes=1 engine_instances=1 held_slots=1): a PASS here has to mean 'from an
     empty database', so start from fresh PostgreSQL and MinIO volumes (… down -v) and re-run

$ … --phase create                 → 退出码 1
FAIL create expects a free capacity slot but 1 slot(s) are already held: assert the
     capacity refusal with --phase create-expect-429, or start from an empty stack

$ … --phase create-expect-429      → RESULT PASS
PASS create Job while 1 slot(s) are already held: error code job_capacity_exhausted
PASS capacity refusal holds with 1 held slot(s): the next create is 429 job_capacity_exhausted
```

### 14.8 收尾

测后按 `compose-minimal.md` §10.1 的三项 label filter 复核并 `down -v`；`deploy/compose.env`
与 `deploy/.runtime/private/**` 是本机运行态（git 忽略），不入库。本轮没有修改任何仓库文件里的
端口、子网、镜像或产品代码；隔离全部经 `deploy/compose.env` 的既有变量完成。
