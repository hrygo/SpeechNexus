# 参考部署一条命令验收

`scripts/dev-env/reference-acceptance.py` 是参考部署（Compose 最小栈）的验收入口：一次运行按
「工具 → 迁移 → 角色 → 桶 → 启动闸门 → 栈 → 健康」执行，逐步打印**步骤名 / 前置检查 /
预期输出 / 实际结论**，任一步失败即以该步的退出码结束，并列出未执行的步骤。它只读取部署输入、
不改 `deploy/`、`docs/operations/compose-minimal.md`、后端或契约文件；容器与网络按
`--project-name` 隔离，可与同机其它 Compose 项目并行。

## 能证明什么

- 环境文件满足 `deploy/compose.yaml` 的 `${KEY:?}` 必需展开，且三条 Job DSN 指向三个互不相同
  的非 owner 角色；
- 数据库迁移收敛到**迁移目录派生的唯一 head**（不硬编码版本号）；
- 三个 Job 角色及其五组授权真实安装到运行时身份上，重复执行不重建、不漂移；
- 私有输入桶与 Artifact/结果桶存在且已启用版本化；
- `deploy/job-startup-gate.py` 在真实 PostgreSQL、MinIO 与私有配置上给出 `OK` 判定；
- API 与 gateway 进入 `healthy`，`/healthz` 与 `/readyz` 返回 200。

不证明：真实引擎与已注册 ready 实例、真实 ASR/TTS 业务成功、模型资格、容量或端到端异步 Job。
启动闸门把这些列在 `pending` 行，本入口原样转述，不把它们伪装成 PASS。

## 前置输入

| 输入 | 说明 |
| --- | --- |
| `deploy/compose.env` | 从 `deploy/compose.env.example` 复制并填写；必需键由 Compose 的 `${KEY:?}` 展开决定 |
| 三条 Job DSN | `SPEECHNEXUS_JOB_WRITER_DATABASE_URL`、`SPEECHNEXUS_JOB_ADMISSION_DATABASE_URL`、`SPEECHNEXUS_JOB_DISPATCH_DATABASE_URL`，按 §6 生成，身份互不相同 |
| `SN_ADMIN_DSN` | 迁移 owner 连接串，由 shell 导出（密钥通道），默认不进环境文件 |
| §6 基线角色 | `sn_api`、`SN_GATEWAY_ROLE`（默认 `sn_gateway`）、`sn_gateway_query` 已预置；本入口不重做 §6 的撤权与基线角色 |
| 私有配置目录 | `SPEECHNEXUS_PRIVATE_CONFIG_DIR` 下的 recipe、gateway lane 配置、批准的 Silero ONNX；缺任一项时 `stack` 步点名缺的是哪个文件 |
| 运行时 UID/GID | `SPEECHNEXUS_RUNTIME_UID` / `SPEECHNEXUS_RUNTIME_GID`，取 `id -u` / `id -g` |

## 一条命令

```bash
set -a; . deploy/compose.env; set +a
export SN_ADMIN_DSN="postgresql://$POSTGRES_USER:$POSTGRES_PASSWORD@127.0.0.1:$SPEECHNEXUS_POSTGRES_HOST_PORT/$POSTGRES_DB"

# 1) 只做静态检查并打印计划，不建角色、不建桶、不改库
python3 scripts/dev-env/reference-acceptance.py --dry-run

# 2) 执行并判定
python3 scripts/dev-env/reference-acceptance.py
```

`--dry-run` 的结论是 `DRY-RUN PASS steps=7`，`pending=N` 表示尚缺的外部输入（例如未提供的
Silero ONNX）；它从不变更数据库、对象存储或角色。执行模式的结论只有两种，机器可判定：

```text
reference acceptance: PASS steps=7
reference acceptance: FAIL step=migrate exit=20
```

## 步骤、前置检查、预期输出与退出码

| 步骤 | 前置检查 | 预期输出 | 失败退出码 |
| --- | --- | --- | --- |
| `tools` | `docker version` 可用；Compose v2+；python3 ≥ 3.12；`uv` 可用；Compose 必需键与三条 Job DSN 非空且互异；Compose 能渲染 | 版本行；`N 个必需 env 键非空`；`compose 渲染通过` | 10 |
| `migrate` | `alembic.ini` 的 `script_location` 可解析；迁移目录恰好一个 head；`SPEECHNEXUS_MIGRATION_DATABASE_URL` 可用 | `migrate` 退出 0；`alembic_version` 单行且等于派生 head | 20 |
| `roles` | §6 基线角色存在；三条 Job DSN 身份互异；`SN_ADMIN_DSN` 是迁移 owner | `job roles ready: ... grants: N statements`；授权探针通过 | 30 |
| `buckets` | `MINIO_*` 与桶名键非空；`minio` 可启动 | 每个不同桶名一行 `bucket ready: ... (versioning enabled)`；独立只读核对通过 | 40 |
| `gate` | 闸门与私有配置可在 worker 内读到；Compose 模式还需要 `gateway` 已经 `healthy` | `job dispatch startup gate: OK`，`ok:` 行给出数据库/对象存储/私有配置结论，`pending:` 行是真引擎与 API 侧 DSN | 50 |
| `stack` | 私有配置与批准模型齐备；API/gateway 基线角色已预置 | `docker compose up -d --build --wait api gateway` 退出 0；两个服务 `running/healthy` | 60 |
| `health` | 容器 healthy 且宿主机端口已发布 | 四个探针 200：API/gateway 的 `/healthz` 为 `status=ok`，`/readyz` 为 `status=ready` | 70 |

参数错误（`--compose-file` 或 `--env-file` 不存在）返回 2。

顺序说明：**迁移在角色预置之前**。`provision-job-roles.py` 的 `GRANT` 指向 `speech` 下的关系，
关系不存在时脚本在 `relation does not exist` 上失败；这与 §5（迁移）在 §6（角色）之前一致。

## 幂等与重跑

- `migrate`：`speechnexus-job-migrate` 在已到 head 的库上是空操作，重复执行不改版本行。
- `roles`：首次运行创建三个角色并安装授权；再次运行时入口先核对角色是否已存在，全部存在则用
  `provision-job-roles.py --print-statements` 重渲染同一组授权并核对探针，输出
  `三个角色已存在（幂等：未重复 CREATE ROLE，未新增授权）；重渲染 N 条 GRANT ...`。只存在
  一部分时直接失败，列出缺失角色，不覆盖已有身份。
- `buckets`：`minio-init` 对已存在的桶只补版本化设置并重新确认；核对步独立读取桶与版本化状态。
- `gate` / `stack` / `health`：只读判定与 `up -d --wait`，重复执行结论不变。

## 启动闸门的两种运行方式

`--gate-mode compose`（默认）按部署路径经 `job-dispatch-worker` 服务运行闸门。该服务
`network_mode: service:gateway`，所以 `gateway` 必须先 `healthy`；gateway 起不来时这一步会失败
并点名"需要先让 gateway healthy（私有配置 + 批准的 Silero ONNX）"。

`--gate-mode direct` 用同一个镜像、同一份 worker 环境在 Compose 网络上直连运行
`deploy/job-startup-gate.py`，不要求 gateway 容器存在。它验证的是闸门自身全部检查（角色守卫、
同源 SELECT 探针、认领后写入列、迁移 head、两个 Job 桶与版本化、gateway `result_bucket`
一致性）；适合在没有批准模型文件的机器上单独确认授权面与迁移面。

## 常见失败与对应修法

| 现象 | 修法 |
| --- | --- |
| `FAIL — 环境键缺失或为空: KEY` | 在 `deploy/compose.env` 写非空值；三条 Job DSN 用 §6 的脚本从口令占位生成 |
| `FAIL — 三条 Job DSN 必须指向三个互不相同的非 owner 角色` | 不要复用 API/gateway 角色；按 §6 重新生成三条 DSN |
| `FAIL — 网关基线角色 sn_gateway 不存在` | 先执行 §6 基线角色预置；本入口不替代它 |
| `FAIL — 迁移落后: alembic_version=... head=...` | 跑 `--profile migration run --rm migrate`；不要手工改版本表 |
| `FAIL — 迁移脚本有 2 个 head` | 先合并迁移分支，单库只能有一个 head |
| `FAIL — provision-job-roles.py 失败: ... relation does not exist` | 迁移尚未应用；先跑 `migrate` 步骤 |
| `FAIL — 已有角色的授权面与声明不一致: ...` | 按 §6 重新安装点名的 grant 组，不要整表补权或换 owner |
| `FAIL — 桶核对失败` | 检查 MinIO root 凭据与 `minio-init` 输出，版本化不能被删除后继续 |
| `FAIL — gateway 未 healthy（compose 模式）` | 准备 §3 私有配置与批准的 Silero ONNX；或先用 `--gate-mode direct` 确认闸门自身检查 |
| `FAIL — 启动闸门判定失败: bucket ... does not exist` | 按闸门点名补桶或版本化，再重跑 `buckets` |
| `FAIL — 缺少 gateway lane 配置 / vad_model_file 不存在 / 摘要不符` | 按 §3 复制并核对私有配置、模型摘要与 `objects.result_bucket` |
| `FAIL — 基线运行角色缺失: sn_api, ...` | 按 §6 预置基线角色后再启动栈 |
| `FAIL — api=.../unhealthy` | 按 §9 失败对照表看容器日志（recipe 摘要、DSN、对象存储健康 URL） |

## 与其它入口的关系

- `scripts/dev-env/check-async-job-prereqs.py`：只读六项前置检查，输出 PASS/FAIL/PENDING，
  可在没有 Docker 时跑静态部分；用于诊断，不做任何修复。
- `scripts/dev-env/provision-job-roles.py`：角色与授权的唯一来源；本入口调用它并在其输出上判定。
- `deploy/job-startup-gate.py`：容器启动闸门；本入口只运行它并转述判定，不复制其检查逻辑。
- `docs/operations/compose-minimal.md`：从零部署的完整步骤；本入口是其在验收方向的收口。
