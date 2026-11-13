# 参考部署一条命令验收

`scripts/dev-env/reference-acceptance.py` 是参考部署（Compose 最小栈）的验收入口：一次运行按
「工具 → 迁移 → 角色 → 桶 → 栈 → 启动闸门 → 健康」执行，逐步打印**步骤名 / 前置检查 /
预期输出 / 实际结论**，任一步失败即以该步的退出码结束并列出未执行的步骤；结尾再记录各步
PASS/FAIL/未运行、环境（checkout commit、项目名、gate 模式、schema）与服务镜像。它只读取
部署输入、不改 `deploy/`、`docs/operations/compose-minimal.md`、后端或契约文件。容器、卷与
网络按 `--project-name` 命名隔离，可反复运行与清理；但 `deploy/compose.yaml` 固定
`172.28.0.0/24`，第二个参考部署栈在同机会因网络重叠（`Pool overlaps`）起不来，因此「与同机
其它参考栈并行」不成立。`--project-name` 用于同一栈的重跑与清理隔离，不用于并行部署。

## 能证明什么

- 环境文件满足 `deploy/compose.yaml` 的每个 `${KEY:?}` 展开（入口运行时按实际展开计数），并满足
  入口额外要求的 6 个键（见「前置输入」）；三条 Job DSN 指向三个互不相同的角色；
- 数据库迁移收敛到**迁移目录派生的唯一 head**（不硬编码版本号）；
- 三个 Job 角色及其五组授权真实安装到运行时身份上；`roles` 步骤把
  `provision-job-roles.py --print-statements` 渲染的每条 `GRANT` 解析成目录函数检查
  （`has_table_privilege` / `has_column_privilege` / `has_schema_privilege`），与已装 ACL
  逐条做「声明的都在」包含比对，缺任一声明授权即红；重复执行不重建角色、不新增授权；
- 私有输入桶与 Artifact/结果桶存在且已启用版本化；
- API 与 gateway 起来后，`deploy/job-startup-gate.py` 在真实 PostgreSQL、MinIO 与私有配置上
  给出 `OK` 判定；
- API 与 gateway 的 `/healthz` 与 `/readyz` 返回 200。

不证明：真实引擎与已注册 ready 实例、真实 ASR/TTS 业务成功、模型资格、容量或端到端异步 Job。
启动闸门把这些列在 `pending` 行，本入口原样转述，不把它们伪装成 PASS。也不判定角色是否
「非 owner」：本入口只拒绝三条 Job DSN 指向同一身份；首跑时 `provision-job-roles.py` 自身会
拒绝 `superuser`/`createdb`/`createrole` 的身份，幂等分支不重做该校验。

## 前置输入

| 输入 | 说明 |
| --- | --- |
| `deploy/compose.env` | 从 `deploy/compose.env.example` 复制并填写。必需键有两个来源：**compose 派生**——`deploy/compose.yaml` 里每个 `${KEY:?}` 展开键（入口运行时按实际展开计数，当前 14 个；本脚本不另列清单）；**入口额外要求 6 个**——`SN_ADMIN_DSN`、三条 Job DSN、`SPEECHNEXUS_OBJECT_STORAGE_BUCKET`、`SPEECHNEXUS_TRANSCRIPTION_INPUT_BUCKET`。后 6 个在 compose 里是 `:-` 默认或根本不出现，入口要求显式非空 |
| 三条 Job DSN | `SPEECHNEXUS_JOB_WRITER_DATABASE_URL`、`SPEECHNEXUS_JOB_ADMISSION_DATABASE_URL`、`SPEECHNEXUS_JOB_DISPATCH_DATABASE_URL`，按 §6 生成，身份互不相同 |
| 两个桶名键 | `SPEECHNEXUS_OBJECT_STORAGE_BUCKET`（Artifact/结果桶）、`SPEECHNEXUS_TRANSCRIPTION_INPUT_BUCKET`（输入桶）；`SPEECHNEXUS_EXECUTION_RESULT_BUCKET` 可省略，省略时等于 Artifact 桶 |
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

# 2) 执行并判定（默认 compose 模式即可从零跑，栈在闸门之前）
python3 scripts/dev-env/reference-acceptance.py
```

`--dry-run` 的结论是 `DRY-RUN PASS steps=7`，`pending=N` 表示尚缺的外部输入（例如未提供的
Silero ONNX）；它从不变更数据库、对象存储或角色。执行模式的结论只有两种，机器可判定：

```text
reference acceptance: PASS steps=7
reference acceptance: FAIL step=migrate exit=20
```

执行与 dry-run 都会在结论前打印运行记录：

```text
步骤记录: tools=PASS migrate=PASS roles=PASS buckets=PASS stack=PASS gate=PASS health=PASS
环境记录: repo=/path/to/repo commit=9398627f project=speechnexus mode=apply gate=compose schema=speech
镜像记录: api=speechnexus-api:latest@sha256:…, gateway=speechnexus-gateway:latest@sha256:…
```

失败时 `步骤记录` 标出失败步，并列出 `未运行:` 的后续步骤。

## 步骤、前置检查、预期输出与退出码

| 步骤 | 前置检查 | 预期输出 | 失败退出码 |
| --- | --- | --- | --- |
| `tools` | `docker version` 可用；Compose v2+；python3 ≥ 3.12；`uv` 可用；compose 的 `${KEY:?}` 派生键与入口额外 6 键非空；三条 Job DSN 互异；compose 能渲染 | 版本行；`N 个 compose 派生键 + 6 个入口额外键（共 N+6 个）非空`；`compose 渲染通过` | 10 |
| `migrate` | `alembic.ini` 的 `script_location` 可解析；迁移目录恰好一个 head；`SPEECHNEXUS_MIGRATION_DATABASE_URL` 可用 | `postgres` healthy；`migrate` 用 `--build` 构建镜像后退出 0；`alembic_version` 单行且等于派生 head | 20 |
| `roles` | §6 基线角色存在；三条 Job DSN 身份互异；`SN_ADMIN_DSN` 是迁移 owner | 首跑：`sn_job_writer sn_job_admission sn_job_dispatch 已创建；grants: N statements；M 项声明授权与已装 ACL 逐条核对全部在位；8 个授权探针通过`。重跑：`三个角色已存在（幂等：未重复 CREATE ROLE，未新增授权）；重渲染 N 条 GRANT、M 项授权与已装 ACL 逐条核对全部在位；8 个授权探针通过` | 30 |
| `buckets` | `MINIO_*` 与两个桶名键非空；`minio` 可启动 | 每个不同桶名一行 `bucket ready: ... (versioning enabled)`；独立只读核对通过 | 40 |
| `stack` | 私有配置与批准模型齐备；API/gateway 基线角色已预置 | `docker compose up -d --build --wait api gateway` 退出 0（输出尾行打印），两个服务 `running/healthy`；`up` 非 0 即红，即使旧容器仍是 `running/healthy` | 60 |
| `gate` | `stack` 步已让 api/gateway `running/healthy`；闸门与私有配置可在 `job-dispatch-worker` 内读到 | `job dispatch startup gate: OK`，`ok:` 行给出数据库/对象存储/私有配置结论，`pending:` 行是真引擎与 API 侧 DSN；同时出现 `FAIL:` 行时一律判失败 | 50 |
| `health` | 容器 healthy 且宿主机端口已发布，端口为 1..65535 的整数 | 四个探针 200：API/gateway 的 `/healthz` 为 `status=ok`，`/readyz` 为 `status=ready` | 70 |

参数错误（`--compose-file` 或 `--env-file` 不存在）返回 2。步骤失败以外的未预期异常也按步骤
归口：打印 `FAIL step=<id> exit=<code>` 与输入提示，不抛 traceback、不回退成 Python 默认退出码 1。

顺序说明：**迁移在角色预置之前**（`provision-job-roles.py` 的 `GRANT` 指向 `speech` 下的关系，
关系不存在时脚本在 `relation does not exist` 上失败，与 §5 在 §6 之前一致）；**栈在闸门之前**
（`job-dispatch-worker` 使用 `network_mode: service:gateway`，gateway 未 healthy 时
`docker compose run` 无法启动，闸门也就无法判定）。退出码标识步骤，不表示行序：栈是 60，
闸门是 50，两者相邻且栈在前。

## 幂等与重跑

- `migrate`：`speechnexus-job-migrate` 在已到 head 的库上是空操作，重复执行不改版本行；每轮
  用 `--build` 构建镜像，避免复用旧镜像得出错误版本结论。
- `roles`：首次运行创建三个角色并安装授权；再次运行时入口先核对角色是否已存在，全部存在则用
  `provision-job-roles.py --print-statements` 重渲染授权，把每条 `GRANT` 解析成目录检查并与
  已装 ACL 逐条比对；任何一条声明授权缺失（包括探针之外的授权，例如对
  `speech.speech_publications` 的 `REVOKE`）都判失败并点名角色/对象/权限。只存在一部分角色时
  直接失败，列出缺失角色，不覆盖已有身份。
- `buckets`：`minio-init` 对已存在的桶只补版本化设置并重新确认；核对步独立读取桶与版本化状态。
- `stack` / `gate` / `health`：`up -d --build --wait` 与只读判定，重复执行结论不变；`gate` 的
  `run --build` 同样避免旧镜像。

## 启动闸门的两种运行方式

`--gate-mode compose`（默认）按部署路径经 `job-dispatch-worker` 服务运行闸门。该服务
`network_mode: service:gateway`，所以 `stack` 步必须先把 `gateway` 起成 `healthy`；`gate` 步仍
保留这条前置，栈没起来时它判失败而不是放行。从零执行默认模式不再在闸门处因「gateway 未启动」
中断——顺序是栈在前、闸门在后，栈失败会以 `stack` 步退出码 60 和容器/构建输出报告真实原因。

`--gate-mode direct` 用同一个镜像、同一份 worker 环境在 Compose 网络上直连运行
`deploy/job-startup-gate.py`，不要求 gateway 容器存在。它验证的是闸门自身全部检查（角色守卫、
同源 SELECT 探针、认领后写入列、迁移 head、两个 Job 桶与版本化、gateway `result_bucket`
一致性）；适合在没有批准模型文件的机器上单独确认授权面与迁移面。

## 常见失败与对应修法

| 现象 | 修法 |
| --- | --- |
| `FAIL — 环境键缺失或为空: KEY` | 在 `deploy/compose.env` 写非空值；三条 Job DSN 用 §6 的脚本从口令占位生成 |
| `FAIL — 三条 Job DSN 必须指向三个互不相同的角色` | 不要复用 API/gateway 角色；按 §6 重新生成三条 DSN |
| `FAIL — 网关基线角色 sn_gateway 不存在` | 先执行 §6 基线角色预置；本入口不替代它 |
| `FAIL — 迁移落后: alembic_version=... head=...` | 跑 `--profile migration run --rm migrate`；不要手工改版本表 |
| `FAIL — 迁移脚本有 2 个 head` | 先合并迁移分支，单库只能有一个 head |
| `FAIL — provision-job-roles.py 失败: ... relation does not exist` | 迁移尚未应用；先跑 `migrate` 步骤 |
| `FAIL — 已装 ACL 与声明的 N 条 GRANT 不一致: … 缺少 …` | 按 §6 重新安装点名的 grant 组；不要只补 8 个探针 |
| `FAIL — 无法解析的授权语句` | `provision-job-roles.py` 渲染出了本入口不认识的 `GRANT` 形态；同步本入口的解析器，不要当作已安装 |
| `FAIL — 桶核对失败` | 检查 MinIO root 凭据与 `minio-init` 输出，版本化不能被删除后继续 |
| `FAIL — 栈未起来（docker compose up 退出码 N）` | 看错误信息里的构建输出；旧容器的 `running/healthy` 不代表新一轮构建成功 |
| `FAIL — 栈未 healthy: api=...` | 按 §9 失败对照表看容器日志（recipe 摘要、DSN、对象存储健康 URL） |
| `FAIL — gateway 未 healthy` | 看 `stack` 步失败原因（私有配置、模型、容器日志）；单独执行 `gate` 时先跑 `stack` 或改用 `--gate-mode direct` |
| `FAIL — 启动闸门判定失败: bucket ... does not exist` | 按闸门点名补桶或版本化，再重跑 `buckets` |
| `FAIL — 缺少 gateway lane 配置 / vad_model_file 不存在 / 摘要不符` | 按 §3 复制并核对私有配置、模型摘要与 `objects.result_bucket` |
| `FAIL — 基线运行角色缺失: sn_api, ...` | 按 §6 预置基线角色后再启动栈 |
| `FAIL — 未预期异常 …` | 核对本步的环境键取值、端口（`SPEECHNEXUS_API_HOST_PORT` 等必须是整数端口）与文件路径后重跑 |

## 与其它入口的关系

- `scripts/dev-env/check-async-job-prereqs.py`：只读六项前置检查，输出 PASS/FAIL/PENDING，
  可在没有 Docker 时跑静态部分；用于诊断，不做任何修复。
- `scripts/dev-env/provision-job-roles.py`：角色与授权的唯一来源；本入口调用它、渲染其语句并
  在已装 ACL 上核对，不复制授权清单。
- `deploy/job-startup-gate.py`：容器启动闸门；本入口只运行它并转述判定，不复制其检查逻辑。
- `docs/operations/compose-minimal.md`：从零部署的完整步骤；本入口是其在验收方向的收口。
