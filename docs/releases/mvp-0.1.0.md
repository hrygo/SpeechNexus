---
title: "SpeechNexus MVP 0.1.0 发布说明"
status: active
version: "0.1.0"
date: 2026-11-16
---

# SpeechNexus MVP 0.1.0 发布说明

本说明陈述该版本实际具备的能力、部署与运维入口、升级与回滚边界，以及 11 条已知限制。每一
条都带证据档位与出处：**只有 A 档（真实部署或真实引擎上的端到端）计入「能做什么」**；B 档
（真实进程、真实 PostgreSQL、真实对象存储，但未走真实引擎或未在参考部署上）与 C 档
（组件级、fake、契约级）只说明「实现存在」，不是发布承诺。档位定义与逐行推导见
[MVP 发布就绪判定表](../plans/mvp-release-readiness.md)，本说明不重复它的论证。

**核验基线**：`origin/main` @ `b63fe004a6e47073e2cdd873995d42b62c9c3b46`
（2026-11-16T20:52:01+08:00），核验日期 2026-11-16。仓库没有任何发布 tag（`git tag` 为空），
所以本文描述的「0.1.0」是一个基线及其构建产物，不是一个已打标的制品；每次部署实际运行的镜像
由它自己的构建提交决定，见 §1。

## 1. 版本标识

### 1.1 标识清单：每个值是什么、谁产生

| 标识 | 本版本取值 | 定义与来源 | 谁产生 / 谁核对 |
| --- | --- | --- | --- |
| 镜像 tag | Web 两个镜像：`speechnexus/web-workbench:<sha>`、`speechnexus/web-admin:<sha>`（`<sha>` 是构建提交；构建命令见 [deploy/README.md](../../deploy/README.md)「Web images」）。Python 侧由 `deploy/compose.yaml` 现场构建的服务共 12 个（`minio-init`、`api`、`gateway`、`binding-disable-worker`、`job-dispatch-worker`、`artifact-retention`、`migrate`、`engine-register`、`engine-readiness-renew`、`engine-readiness`、`workbench-migrate`、`workbench-api`），共用同一个 `deploy/images/python.Dockerfile`；`asset-inspector` 用它自己的 `asset-inspector/Dockerfile`；`admin`/`workbench` 共用 `deploy/images/web.Dockerfile`（按 `APP_NAME` 区分）。Compose 给这些镜像的名字是 `<COMPOSE_PROJECT_NAME>-<service>`，**没有版本 tag**。`minio` 复用同 tag 的既有镜像 `speechnexus-minio:RELEASE.2025-09-07T16-13-09Z` | 镜像名由构建方决定，仓库不发布带版本号的镜像制品，也不保存镜像 digest 清单 | 构建方：按 `deploy/README.md` 的「Web images」命令（`docker build … --label`）给 Web 镜像打上 `org.speechnexus.source.commit` 与 `org.speechnexus.source.dirty`；Python 镜像**不带**任何 label（这些 Dockerfile 都不写 `LABEL`；仓库里唯一写 `LABEL` 的是 `deploy/images/minio.Dockerfile`，声明的是上游 minio 的来源，不是本仓库提交） |
| Python 包版本 | `0.1.0.dev0` | 8 个 `pyproject.toml` 的 `version` 字段（根、`backend`、`execution-gateway`、`node-controller`、`asset-inspector`、`packages/contracts`、`packages/media`、`deploy/engines/vllm-asr-lifecycle`）。API 的 `/healthz` 与 `GET /v1/nexus/version` 的 `version` 字段都取自 `backend/src/speechnexus_backend/__init__.py:3` | 人手声明；不随契约修订变化 |
| TypeScript 包版本 | `0.1.0` | 5 个 `package.json` 的 `version` 字段（根、`packages/contracts`、`web/admin`、`web/shared`、`web/workbench`） | 人手声明；不随契约修订变化 |
| 契约 `info.version` | 按文件各自计数，**不存在一个「契约总版本」**。8 个 OpenAPI 文件各有自己的值：`contracts/speech-openapi.yaml:4` = `0.1.0.dev18`、`contracts/admin-openapi.yaml:4` = `0.1.0.dev11`、`contracts/workbench-openapi.yaml:4` = `0.1.0.dev2`、`contracts/internal/execution-control.openapi.yaml:4` = `0.4.0.dev1`、`contracts/internal/asset-inspection.openapi.yaml:4` 与 `contracts/internal/runtime-control.openapi.yaml:4` = `0.1.0.dev1`、`contracts/operations.openapi.yaml:4` 与 `contracts/binding-defaults-producer.openapi.yaml:4` = `0.1.0.dev0` | 每个 OpenAPI 文件自己的 `info.version` 字符串。它被人手维护，`scripts/dev-env/check-http-contract-changes.py` 只把它当作「破坏性变更允许清单」的版本对键，没有「必须递增」的守卫 | 人手维护；运行期不读它 |
| `contract_digest` | `sha256:ad581b4691e104fea9fad79d28fe3b0a5b08faace31156bdb7b2fe7d5c9c7863` | `contracts/` 下每个 `*.yaml`/`*.json` 文件按相对路径字节序，对「`<相对路径> SPACE <该文件 sha256> NEWLINE`」求总 `sha256`（算法在 `scripts/dev-env/generate-contract-bindings.py:823`）。生成期同时写进 Python 包（`packages/contracts/src/speechnexus_contracts/contract_digest.py`）与 TypeScript 包（`packages/contracts/src/contract_digest.generated.ts`） | 生成器产出一个值，两侧消费者各自持有；API 在运行期自报（`backend/src/speechnexus_backend/speech/interfaces/http/version_identity.py:30`），两个 Web 入口把同一个值烘焙进 bundle 并在页面启动时比对（`web/shared/src/ui/contract-identity.tsx:1`） |

契约摘要与 `GET /v1/nexus/version` 的应答形状在 `packages/contracts/src/speechnexus_contracts/version.openapi.json:81`
（`required: ["version", "contract_digest"]`）；该路由由健康检查路由无条件挂载
（`backend/src/speechnexus_backend/speech/interfaces/http/health.py:36`），响应带 `Cache-Control: no-store`。

### 1.2 三者为什么是这个关系

三个值回答三个不同的问题，且互相推不出来：

- **包版本**（`0.1.0.dev0` / `0.1.0`）回答「这是哪一版代码」。它由人手声明，契约改动不会让它
  动，所以它不能用来判断前后端是否同版。本版本就是这种状态：包版本停在 `0.1.0.dev0`，而
  speech 契约已走到 `0.1.0.dev18`。
- **契约 `info.version`** 回答「这个文件被人改到第几版」，是给人读的计数。它按文件独立递增
  （speech 18 次、admin 11 次、workbench 2 次，内部执行控制另在 `0.4` 线上），因此没有单一的
  「契约版本」；它也不参与运行期判定——没有任何代码读它来判混搭。
- **`contract_digest`** 回答「这两个进程是不是由同一份契约字节生成的」。这是唯一一个
  **机器可核对**的值：Python 与 TypeScript 消费者由同一目录生成，API 报它、浏览器烘焙它，
  两者不等就说明部署里混了不同契约修订的产物。它不是 git 修订，也不暴露路径或凭据。

关系因此是：**包版本管代码、`info.version` 管人读的契约修订次数、`contract_digest` 管机器判
同版**。三者不一致是正常的（本版本即如此），能对齐的只有 `contract_digest`；把包版本或
`info.version` 相等当成「同版」会在契约改动而包版本未改时给出错误结论。

### 1.3 自查：三个值分别怎么取

```bash
# 1. 运行时自报（部署侧、构建侧都应一致）
curl -sS http://127.0.0.1:8000/v1/nexus/version
# {"version":"0.1.0.dev0","contract_digest":"sha256:ad581b46…7863"}

# 2. 在要核对的提交上离线重算，并直接比对运行期值
UV_NO_CONFIG=1 uv run python scripts/dev-env/print-contract-digest.py --expect sha256:ad581b46…

# 3. 页面 bundle 里烘焙的值（应与 1、2 相同；bundle 里另有测试夹具的假摘要，形如重复字符）
grep -o 'sha256:[0-9a-f]\{64\}' web/workbench/dist/assets/*.js | sort -u
```

三项一致才说明「部署 = 这个提交的契约」。人工核对清单的完整四项（镜像 label、`web-dist` 树
摘要、契约摘要、后端源码树）在[前后端同版核对](../operations/same-version-check.md)。

## 2. 能做什么

只列取得 A 档证据的能力；每行写清它的窄口径与「本版本未覆盖」的部分。

| 能力 | 对应 PRD 验收 | 证据档位 | 证据与出处 |
| --- | --- | --- | --- |
| 受控输入资产：`asr_input`、`voice_reference`、`text_input` 三类上传返回 `201 ready`，音频 ready 带真实检查事实（时长、采样率、声道、字节数、`sha256`）与固定对象版本；随机字节 `422 invalid_asset_audio`，缺检查器 `503 asset_inspection_unavailable` 且不降级 | [F-ASSET](../prd.md#f-asset) | A | 参考部署（真实 `postgres:16.15-alpine` + MinIO + 迁移 `0054` + §6 预置的 20 个非 owner 角色 + `provision-job-roles.py` + 从仓库源码构建的 `api`/`asset-inspector`）上的实跑记录见[从零实跑 §11.2](../operations/compose-minimal-from-zero.md)（`docs/operations/compose-minimal-from-zero.md:402`；`asset-462c19b3…`，`bytes=195356`、`duration_ms=4429`、`sample_rate_hz=22050`、`channels=1`）；三类用途与全部反例见判定表第 1 行 |
| 持久 Job：`POST /v1/nexus/jobs` → `202` → `queued → running → succeeded`，创建/读取/派发/终态四个动作在参考部署上跑通；缺前置时在写入前点名失败（空 Job DSN → `503 job_unavailable`、缺迁移 → 启动门退出码 1、无 ready 实例 → `429 job_capacity_exhausted` 且不写行） | [F-JOB](../prd.md#f-job) | A（窄口径：创建/读取/派发/终态） | [从零实跑 §11.2](../operations/compose-minimal-from-zero.md)（`docs/operations/compose-minimal-from-zero.md:403`；`job-6d2c1d0f…`）与[时间线](../operations/compose-minimal-from-zero.md)（`docs/operations/compose-minimal-from-zero.md:404`）。**取消围栏、重试、SSE 事件序号续读只有隔离 PG 证据，未在参考部署上实际调用**（[判定表第 2 行](../plans/mvp-release-readiness.md)） |
| 异步执行闭环：真实派发 worker 领取持久 Job，经真实网关调用真实引擎并写回终态 | [F-ASYNC](../prd.md#f-async) | A | 同一部署：worker 批次 `claimed=1 succeeded=1 failed=0 unknown=0 lease_lost=0`，网关 `submit`/`staging-result` 均 `200`，引擎侧（vLLM 0.28.0）日志出现一次 `POST /v1/audio/transcriptions 200 OK`（[§11.2](../operations/compose-minimal-from-zero.md)（`docs/operations/compose-minimal-from-zero.md:411`）、[引擎侧](../operations/compose-minimal-from-zero.md)（`docs/operations/compose-minimal-from-zero.md:413`）） |
| 制品发布与下载：staging 不可公开读取、发布固定对象版本、`GET …/content` 返回字节且 `ETag` 与 `sha256` 逐字一致、落盘字节独立重算相符 | [F-ARTIFACT](../prd.md#f-artifact) | A（窄口径：发布与下载） | [§11.2](../operations/compose-minimal-from-zero.md)（`docs/operations/compose-minimal-from-zero.md:406`；`artifact-5b93ea73…`，92 字节，`sha256:3002c23c…`）。**保留期与过期链路未在参考部署上观测**：`artifact-retention` profile 存在但从未在该部署上启动留证 |
| 同版浏览器消费：默认部署上由真实 Chromium 打开同版工作台，读到真实 GPU ASR Job 产出的同一份制品并落盘，`pageErrors=[]`，CSP 未放宽 | [F-CLIENT](../prd.md#f-client) | A（`DEL-534` 窗口） | 该次窗口的证据体不在仓库内，仓库内的结论与逐项数值在[判定表第 5 行](../plans/mvp-release-readiness.md)；可执行的离线/在线核对入口是[前后端同版核对](../operations/same-version-check.md) |
| 同步语音入口：`POST /v1/audio/transcriptions` 与 `POST /v1/audio/speech` 在标准 SDK 下各取得过一次真实 `200` | [F-ASR](../prd.md#f-asr)、[F-TTS](../prd.md#f-tts) | A（窄口径、单次受控窗口、早于当前 head） | ASR：2026-11-02 MegaCube 窗口，`200`/1.237 s/文本与 5.36 s 合成句一致（记录在 PR #317 正文与 issue #243，仓库外材料）；TTS：2026-11-03 第六窗口，`200`/96044 bytes/2 s PCM16 24 kHz（[受控验收记录](../archive/audits/2026-11-03-controlled-tts-prepare.md)，脱敏 JSON 已入库）。**参考部署未复跑这两个 `200`**：ASR 缺最后一步公开调用（[compose-minimal.md](../operations/compose-minimal.md)（`docs/operations/compose-minimal.md:20`）），TTS 缺已合格内建声音（见已知限制 1 与 §4 的声音资格前置） |
| 工作台侧删除：`POST /v1/workbench/deletion-plans` → `200`、以 `plan_id`/`plan_revision`/`plan_digest` 固定的 `POST /v1/workbench/deletions` → `202` → `succeeded`/`complete`，账本落行、`cascade` 模式擦除正文而不只删元数据行，错 revision/错 digest `409 deletion_plan_changed` | [F-DELETE](../prd.md#f-delete)（仅工作台单元） | A（工作台单元窄口径） | Compose 项目 `sn832` 真实栈实测，逐项见[判定表第 9 行](../plans/mvp-release-readiness.md)；实现为 `backend/src/speechnexus_backend/workbench/application/deletions.py`。**语音侧删除不在本版本**（`/v1/nexus/deletion-plans`、`/v1/nexus/deletions` 恒 `503 operation_not_implemented`，见已知限制 9） |
| 运维可见面：`/healthz`、`/readyz`、`GET /v1/nexus/version`、`GET /v1/nexus/admin/diagnostics`（含 `cancellation_debt` 明细）、启动闸门（Job/workbench/隔离释放/保留期各自在领取前点名缺项） | [O-OBSERVE](../prd.md#o-observe) | A（探活与闸门本身）+ 明确缺口 | 探活期望值见 [compose-minimal.md §7](../operations/compose-minimal.md)（`docs/operations/compose-minimal.md:2124`）；闸门缺项命名见 [deploy/README.md](../../deploy/README.md) 与 [production-jobs.md](../components/production-jobs.md)。**`/readyz` 不反映 Job 准入可用性**，见已知限制 10 |

**不在本版本可达范围内的能力**（不是「以后会做」的承诺，只是当前边界）：实时语音会话
（`/v1/realtime` 等仍是占位）、语音侧删除、声音设计/克隆的 Job 级端到端、物理多机拓扑与故障
演练、浏览器全矩阵资格。范围定义见[判定表 §7](../plans/mvp-release-readiness.md)；`workspace`
作为分组语义（F-SHARED）没有 A 档证据，因此不在上表。

## 3. 部署

**唯一真相是[最小同步 ASR/TTS Compose 部署面](../operations/compose-minimal.md) §0–§7 与
[deploy/README.md](../../deploy/README.md)。本节不复制它们的步骤**，只列这个首发版本独有的
前置与顺序陷阱；照文档做完仍起不来的情形，先在 §4.3 的自查信号里找对应的失败类别。

首发特有的前置共五类：

1. **两个 grant 函数必须在迁移后重跑**（仅当配置了
   `SPEECHNEXUS_QUARANTINE_RELEASE_DATABASE_URL` 或 `SPEECHNEXUS_READINESS_CANCELLATION_DSN`）。
   隔离释放角色的声明授权面从 9 张关系扩到 12 张，新增的三张是 `speech_jobs`、
   `speech_job_cancellation_fences`、`speech_job_cancellation_reconciliations`
   （`backend/src/speechnexus_backend/speech/infrastructure/quarantine_release_schema.py` 的
   `READ_COLUMNS`）。以 owner 重跑 `quarantine_release_grants` 与
   `cancellation_debt_reader_grants`。漏跑的三条自查信号与升级路径见
   [§6 已有部署的授权升级](../operations/compose-minimal.md)（`docs/operations/compose-minimal.md:1690`）。
2. **异步 Job 的三个角色与九组授权**：`sn_job_writer` / `sn_job_admission` /
   `sn_job_dispatch` 由 `scripts/dev-env/provision-job-roles.py` 预置；admission 角色必须与
   Job writer 指向同一数据库和服务器，否则启动拒绝。旧版本升级上来、Job writer 仍持有**表级**
   `INSERT` 时，必须先 `REVOKE` 再重授列级权限（[deploy/README.md](../../deploy/README.md)
   的「Optional writers and background units」给出逐条函数清单）。
3. **binding、`execution_defaults`、配额与实例注册**：注册是一次性命令而不是起容器，它是
   `host_nodes`、`workspace_quotas`、`resource_pools`、`engine_instances` 的唯一产生入口
   （[compose-minimal.md §8](../operations/compose-minimal.md)（`docs/operations/compose-minimal.md:2180`））。已发布的 binding 必须带
   `execution_defaults`，否则 asr/tts 创建返回 `422 job_binding_incomplete`；升级前用只读预检
   `psql "$SPEECHNEXUS_DATABASE_URL" -f scripts/dev-env/check-public-job-execution-defaults.sql`
   列出缺项，对列出的 binding 重新发布，**不要自动回填**（口径与命令来源：`deploy/README.md:1073`）。
4. **声音资格**：参考部署的 bootstrap 明确「engine lifecycle is operator/external」
   （`deploy/compose.yaml:9`），因此没有 node-controller 也就没有 native runtime 证据。外部托管
   引擎只能走运维自证路径：先声明信任（默认关闭，未声明即 `exit 2` 点名变量），再
   `speechnexus-attest-builtin-voice`，再 `speechnexus-publish-builtin-voice`，读面用
   `speechnexus-qualification-provenance` 区分两种来源。边界与具名拒绝码见
   [Builtin Voice 资格边界](../operations/builtin-voice-qualification.md)。**发布说明意义上的一句
   实话**：这条路径给的是「有来源、可审计的资格」，不等于一次真实合成 `200` 已经取到。
5. **首发默认装配的三个「看起来起好了但不能用」**：
   - `SPEECHNEXUS_ASSET_INSPECTOR=text` 是默认值，**音频上传会 `503`**；要用音频 Job 必须显式
     切 `audio` 并同时提供检查器 URL 与令牌（`asset-inspection` profile）；
   - 参考部署**不启动** `job-dispatch-worker`、`artifact-retention`、取消对账；派发要按需加
     `--profile job-dispatch`，且 `up -d` 成功不证明 worker 就绪（它没有 healthcheck）；
   - `engine-readiness` 的收据续期**不被 Compose 调度**，须外部定时器在 TTL 内调用，否则过期后
     每次 Job 创建都是 `503 engine_not_ready`（`deploy/compose.yaml:29`）。

构建顺序也有一个首发陷阱：首次 `up` 之前**显式点名服务**构建一次，之后 `up -d` 不带
`--build`。省成 `docker compose build` 会把 `minio` 一起构建，而它在受限网络下必然失败
（[compose-minimal.md §0](../operations/compose-minimal.md)（`docs/operations/compose-minimal.md:28`））。

## 4. 升级

### 4.1 顺序

顺序是「镜像 → 迁移 → 授权 → 服务」，其中服务一侧 gateway 必须早于 backend 与 worker：

1. **构建镜像**（`deploy/compose.yaml` 构建，或按 `deploy/README.md` 单独构建 Web 镜像并 pin 到
   部署侧 override）。记录构建提交与镜像 ID。
2. **迁移到唯一 head**：`--profile migration run --rm migrate`。当前 head 是
   `0056_debt_probe_live_state_index`（[compose-minimal.md §5](../operations/compose-minimal.md)（`docs/operations/compose-minimal.md:467`））。
   升级前须停止或隔离旧版本的 publication 与 lifecycle 写者，直到迁移、授权与新版本切换完成。
3. **重放授权**：新建部署跑 §6 的建角色脚本；**已有部署只重放授权函数**，不重跑撤权与建角色
   部分（[§6 已有部署的授权升级](../operations/compose-minimal.md)（`docs/operations/compose-minimal.md:1690`））。这一轮至少包括
   `quarantine_release_grants`、`cancellation_debt_reader_grants`、Job writer 的九组函数、
   `gateway_query_grants`/`gateway_abort_grants` 两份一起、`artifact_retention_grants`。
   旧部署还要补 `GRANT SELECT ON speech.alembic_version TO sn_job_dispatch`（派发启动门要读
   head；角色早于该授权的部署必须补，见 `deploy/compose.env.example`）。

   漏跑的可自查信号（四条，都被实现与用例钉住）：

   - `quarantine_release` 面缺授权 → API 启动**点名表列**被拒，或操作员 CLI 退出码 3、
     `unavailable: quarantine_release_privilege_missing`；
   - `cancellation_debt_reader_grants` 缺新列 → 该探针整体 `unavailable (permission)`
     （`cancellation_debt` 不受影响），`/readyz` 仍为 `200`；
   - `stop_evidence_debt` 需要同一身份的列级授权 → 同样降级为 `(permission)`；
   - Job 派发启动门缺 `GRANT SELECT ON speech.alembic_version` → 容器退出并点名该表。

   `0056` 建的索引本身不需要任何授权（索引不是可授权对象），只读诊断身份**不加新授权**
   就能用上它；实测只施加既有 7 条列级授权的临时角色，两条债务查询都走
   `speech_jobs_live_state_scan`（0.216 ms / 3.228 ms）。
4. **起 gateway，再起 api 与 worker**。原因是 TTS 内部命令新增的可选字段是**单向兼容**：新
   gateway 能处理旧 backend 的 builtin 命令，旧 gateway 处理新 backend 的 custom 命令会走未知
   路径（`ENGINE_TRANSPORT_UNKNOWN`），不会静默回退。因此不允许「新 backend + 旧 gateway」
   （`deploy/README.md:1082`）。
5. **按需启用 profile**：`job-dispatch`、`artifact-retention`、`workbench-api`、`web`、
   `asset-inspection`。每个 profile 都有自己的启动闸门，闸门失败是**容器退出/重启循环**，不是
   `up -d` 失败——`up -d` 返回 0 不代表该单元可用。

### 4.2 迁移能不能回退：核实过程与结论

**结论：数据库不回退。迁移链是前向唯一的，没有可用的 down 路径；回滚靠镜像，不靠迁移降级。**

核实方式：逐个打开 `backend/src/speechnexus_backend/speech/infrastructure/migrations/versions/`
下的 54 个迁移文件（编号 `0001`–`0055`，缺 `0026`），读每个 `downgrade()` 的函数体，按行为
分类：

| 类别 | 数量 | 代表文件与实测行为 |
| --- | --- | --- |
| 无条件拒绝降级（`downgrade()` 只有 `raise RuntimeError`，没有一句 DDL） | 16 | `0001_jobs.py`：「Destructive Job/outbox downgrade requires an explicit recovery plan」；`0029_builtin_voice_qualification.py`、`0031_tts_settlement.py`、`0033_profile_publications.py` 同类 |
| 条件拒绝（表里有历史数据就 `raise`，只有空表才执行 DDL） | 26 | `0020_artifacts.py`：非空即「Committed Artifacts require preservation and recovery」；`0035_job_cancellation_fences.py`：三张表任一非空即拒绝；`0046_public_job_intents.py` 用 SQL `RAISE EXCEPTION` 实现同一语义；`0052_quarantine_releases.py`：有账本行即拒绝 |
| 无条件执行 DDL | 12 | 其中 4 个**会丢数据**：`0041_voice_drafts.py`（删两张草稿表）、`0043_voice_draft_revisions.py`（删 revision 表）、`0049_workspace_commands.py`（删幂等命令账本）、`0054_artifact_deleted_ledger.py`（删 `deleted_at_us` 账本列）；其余 8 个是视图/索引/派生列（`0019`、`0028`、`0038`、`0040`、`0042`、`0045`、`0048`、`0053`） |

另有两条与迁移无关但同样阻断「降级」的事实：

- **工作台命名空间没有 Alembic 分支**：`backend/src/speechnexus_backend/workbench/migrate.py`
  是一次性 owner 迁移——新命名空间 bootstrap，已存在的命名空间只**核对**记录 revision 是否等于
  本构建的（当前 `SCHEMA_REVISION = 11`，
  `backend/src/speechnexus_backend/workbench/infrastructure/runtime_schema.py:59`），从
  repair drift。所以工作台 schema 只能前向。
- **根目录 `migrations/` 只有一个 SQL 草案**（`0001_cluster_coordination_draft.sql`），不参与
  产品迁移链。

因此实际后果是：

- `alembic downgrade` 在这条链上不是可用操作。跑它要么立刻抛错，要么在**空表**的开发库上「成
  功」，而那个「成功」恰好证明它没有处理真实数据的能力。
- 发布回滚的手段只有一个：**把镜像切回旧版本，数据库留在新 schema 上**。这不是「两边都回滚」，
  后果见 §5。

### 4.3 每一步的可自查信号

| 步骤 | 自查信号 | 判据 |
| --- | --- | --- |
| 镜像 | `docker compose --env-file deploy/compose.env -f deploy/compose.yaml build <逐个服务名>` | 退出码 `0`；Web 镜像再核 label：`docker image inspect --format '{{index .Config.Labels "org.speechnexus.source.commit"}} {{index .Config.Labels "org.speechnexus.source.dirty"}}' speechnexus/web-workbench:<sha>` 输出 `<sha> 0` |
| 迁移 | `... --profile migration run --rm migrate`；再 `... exec -T postgres psql -U <user> -d <db> -Atc 'select version_num from speech.alembic_version'` | 退出码 `0`，且查询结果等于所用制品的 head（本版本 `0055_operator_attestation`）。不相等即「迁移没跑完/跑错身份」，不要手工改版本表 |
| 授权（隔离释放） | 三条，任一命中即漏跑：① API 启动被拒并点名表列（`Quarantine release startup rejected (... declared surface at <table>.<column>)`，`backend/src/speechnexus_backend/quarantine_release_runtime.py:137`）；② 只跑操作员 CLI 时退出码 `3`、输出 `unavailable: quarantine_release_privilege_missing`；③ `/readyz` 的 detail 退化为 `cancellation_debt: … (jobs: lease-unread=1) … [lease detail unavailable: permission]` 且**不会**让 `/readyz` 失败 | 三条都是「已配置身份但授权不对」的信号，不是故障 |
| 服务 | `curl -sS -w '\nhttp=%{http_code}\n' http://127.0.0.1:8000/healthz`、`…/readyz`、`http://127.0.0.1:8010/healthz`、`…/readyz` | 四条都是 `200`，体见 [compose-minimal.md §7](../operations/compose-minimal.md)（`docs/operations/compose-minimal.md:2124`）。`/readyz` 只证明 Postgres 与对象存储连通，不证明 Job、binding、recipe 或引擎可用 |
| 同版 | `curl -sS http://127.0.0.1:8000/v1/nexus/version` 的 `contract_digest`，与该提交上 `print-contract-digest.py` 的重算值、页面 bundle 里烘焙的值 | 三者逐字相同 |
| 异步 Job 前置 | `python3 scripts/dev-env/check-async-job-prereqs.py --env-file deploy/compose.env` | 退出码 `0`（允许 `PENDING`/`SKIP`）；`1` 表示至少一项 `FAIL`，`2` 是参数或路径错。它只读，**只判「缺少权限」，不判「多授予了权限」** |
| 派发 worker | 启动日志里 `deploy/job-startup-gate.py` 的 `OK:` 行；`docker compose ps` 显示该容器在运行且未重启；`logs job-dispatch-worker` 有批次计数 | 无 `OK:` 行或循环重启即前置缺失。迁移 head 不等于镜像内 head 时闸门直接拒绝并点名两侧
（`deploy/job-startup-gate.py:353`） |
| 一条 Job 端到端 | `POST /v1/nexus/assets` → `201 ready`；`POST /v1/nexus/jobs` → `202`；轮询到 `succeeded`；`GET …/content` → `200` 且 `ETag` 等于 `sha256` | 创建若 `503 engine_not_ready`，先跑 `engine-readiness` 的续期（它不被 Compose 调度） |

### 4.4 升级前必须核对的既有数据

- **已发布 binding 的 `execution_defaults`**：缺则 asr/tts 创建 `422 job_binding_incomplete`。
  预检脚本见 §3 第 3 条；处置是重新发布，不是回填。
- **Job writer 的表级 `INSERT`**：旧版本角色可能持有表级权限，新版本启动按列集比对会拒绝。
  先 `REVOKE` 再重授（函数清单见 `deploy/README.md`）。
- **未收口的取消**：升级前先把处于 `cancelling` 且持有容量的 Job 处理掉（见已知限制 2）。旧
  镜像没有这个版本的操作员出口，切回去之后就没有工具能收口它们。
- **私有配置与 ACL 备份**：回滚要用。至少保存 `deploy/compose.env`、`deploy/private/` 下三份
  文件、以及 `pg_dump` 的角色授权视图（或 §6 脚本重放前的 `\dp` 输出）。
- **迁移只会新建索引，但会阻塞写入一个事务窗口**：`0056` 用 `CREATE INDEX`（不是
  `CONCURRENTLY`：迁移运行器整链在一个事务内，`CONCURRENTLY` 非法）。实测 1M 行 `speech_jobs`
  上建索引 `47.5 ms`、持 `ShareLock`，期间**阻塞写入、不阻塞读取**，窗口等于整条迁移事务。
  表越大窗口越长且线性增长。手工预建过同名索引会让迁移失败（无 `IF NOT EXISTS`）；失败即整链
  回滚，安全，但排查时要先看 `pg_indexes` 里有没有 `speech_jobs_live_state_scan`。


## 5. 回滚

### 5.1 结论

**回滚的唯一形式是「镜像回滚 + 授权回退」，数据库不回退。** 数据库留在新 schema 上，因此旧
版本运行在一个它设计时还不存在的 schema 之上。这在多数情况下能起得来（API 启动**不校验**迁移
head，见下），但有三个地方会**硬拒绝启动**，以及四类数据会让旧版本的**行为**坏掉。

### 5.2 旧镜像面对新 schema 会发生什么

**会硬拒绝启动的三处（都在启动期，点名具体缺项，不会带病运行）。`0056` 只新增一个索引、不改任何列与约束，因此它不新增拒绝面——但 head 变了，回滚到 0055 之前的镜像时下面第一行照旧生效：**

| 单元 | 机制 | 回滚后果 |
| --- | --- | --- |
| `job-dispatch-worker` | `deploy/job-startup-gate.py` 要求库内 `speech.alembic_version` 等于**镜像内**的 head（`deploy/job-startup-gate.py:353`） | 旧镜像的 head 更小 → 拒绝启动并点名两侧 head。要么不把这个 profile 回滚（让新 worker 继续跑，它按声明面读列，不回改数据），要么先接受该单元不可用 |
| 所有在启动期比对「声明授权面」的单元 | 隔离释放角色按列全等比对（多一列、少一列都拒绝，`backend/src/speechnexus_backend/quarantine_release_runtime.py:137`）；Artifact 保留身份只允许改声明列，多出列即拒绝（`backend/src/speechnexus_backend/artifact_retention_runtime.py:140`）；Job writer 的表级 `INSERT` 亦同理 | 升级时按新声明重放过授权，ACL 因此是「新面」。旧镜像声明面更窄 → 上述单元拒绝启动。**所以回滚必须同时把 ACL 退回旧声明面**（`REVOKE` 新增列/关系后重跑旧版本的 grant 函数），这正是 [compose-minimal.md §6](../operations/compose-minimal.md)（`docs/operations/compose-minimal.md:1690`） 写的「回退时…按保存的 ACL 恢复受影响的其他角色」 |
| `workbench-migrate` | 已存在的命名空间只核对记录 revision 等于本构建的（`SCHEMA_REVISION = 11`） | 回滚工作台镜像时，旧 `workbench-migrate` 对不同 revision 会失败；该单元只能前向 |

**不会让旧镜像起不来、但会让行为出错的四类新数据：**

1. **`0054` 的删除账本列与新检查**。`speech_artifacts` 新增 `deleted_at_us`，并加约束
   `(artifact_state = 'deleted') = (deleted_at_us IS NOT NULL)`，`NOT VALID` 仍对新写入生效
   （`backend/src/speechnexus_backend/speech/infrastructure/artifact_schema.py:51`）。旧版本保留期
   清理把行置 `deleted` 时**不写**该列 → 违反约束、该次清理报错。**发布与下载不受影响**：派发器
   的 `INSERT` 用显式列清单（`ARTIFACT_INSERT_COLUMNS`，不含 `deleted_at_us`），新列可空，
   写 `ready` 行时约束要求它为空，恰好满足。→ 处置：回滚时不要回滚 `artifact-retention`，或者
   接受该 profile 不可用。
2. **`0055` 的资格 provenance**。`speech_builtin_voice_operator_attestations` 与
   `speech_builtin_voice_qualification_provenance` 是 0055 新增的。旧镜像不知道这两张表，能正常
   写资格行，但**写不出 provenance 行**。等版本再前滚回来，这些资格行因为「没有 provenance
   行」被准入拒绝（同步 TTS `404 voice_not_found`、公开 Job `422 voice_not_published`）。→
   恢复方式与已知限制 1 相同：对**新的** voice revision 重新取得资格；同一 revision 不能补发。
3. **`cancelling` 的债**。旧镜像没有本版本的「已完成执行、取消后到」操作员出口，回滚后这类
   Job 仍是 `cancelling` 且租约 `reserved`、`held_slots` 不归零，只能等前滚后的新版本处置。→
   升级前先清 §4.4 第 3 条。
4. **新枚举值与账本行**（`0050` 的 `retire` 操作、`0051` 的 `model_binding.publish` 事件、
   `0052` 的隔离释放账本、`0049` 的工作区命令账本）。旧镜像不读它们，也不会被它们卡住；代价是
   旧版本看不到这些历史——例如回滚期间的幂等重放会重新受理一条已在 `0049` 账本里的命令，
   `0052` 的释放证据在旧版本读面上不可见。迁移降级会删除这些账本，因此**不要用降级来「对齐」**。

**一句话的可操作结论**：回滚可行，但它不是「切个 tag」，而是「切镜像 + 把 ACL 退回旧声明面 +
接受旧版本读不到新账本」。回滚后不要跑任何迁移降级；数据库前向不回退。

### 5.3 回滚步骤与自查

1. **停新版本服务**：先停 worker/profile 单元（`job-dispatch`、`artifact-retention`、
   `binding-disable`），再停 `api` 与 `gateway`。自查：`docker compose ps` 里目标容器不再
   `Up`；按 [§10.1 的 label filter](../operations/compose-minimal.md)（`docs/operations/compose-minimal.md:2285`） 确认没有遗留的一次性
   容器。
2. **把 ACL 退回旧声明面**：对升级时被扩权的角色 `REVOKE` 新增的列/关系，再用旧版本的 grant
   函数重放。自查：旧镜像启动不再出现 `... startup rejected`；`/readyz` 的
   `cancellation_debt` detail 不退化为 `[lease detail unavailable: permission]`。
3. **切回旧镜像与旧私有配置**：`deploy/compose.env`、`deploy/private/` 三份文件、两份 gateway
   配置。自查：gateway 与 api 容器 `healthy`；四条约 `curl` 返回 `200`。
4. **确认数据边界**：不要执行 `alembic downgrade`；不要删 `speech_artifacts.deleted_at_us`；
   不要删两张 provenance 表。自查：
   `select count(*) from speech_artifacts where artifact_state='deleted' and deleted_at_us is null`
   的数是「0054 之前就已删除、不可追溯」的行数，不是故障，但也不要为它们补一个计算出来的时间。
5. **恢复派发（可选）**：若旧 worker 被 head 校验挡住而业务需要派发，保留**新** worker 而不是
   回滚它；它的声明面与新 ACL 相容。自查：启动闸门输出 `OK:` 行、`check-async-job-prereqs.py`
   退出码 `0`。
6. **归档证据**：回滚前后各存一份 `/readyz`、`GET /v1/nexus/version`、`speech.alembic_version`、
   受影响角色的 `\dp`。

### 5.4 不能安全回滚的情形

- **已经跑过 `artifact-retention` 的新版本**：`deleted_at_us` 里已经有真实删除时刻，降级会删掉
  这份账本本身（`0054` 的 `downgrade` 无条件删列），这属于销毁证据，不是回滚。
- **已经积累了 `cancelling` 债**：旧版本没有收口工具（见 §5.2 第 3 类）。
- **回滚期间发布了 qualification**：会产生没有 provenance 的资格行（见 §5.2 第 2 类）。
- **工作台命名空间**：只能前向，旧 `workbench-migrate` 对更高 revision 会失败。

## 6. 已知限制

以下 11 条与 [issue #912](https://github.com/hrygo/SpeechNexus/issues/912) 的清单一一对应，按
「使用者会踩到什么 → 怎么处置」重写。「处置」列只有两种取值：**可操作**（有命令或步骤能消掉或
绕过）与**无处置，只能避免**（技术上没有出口，唯一的做法是不进入该场景）。

| # | 使用者会踩到什么 | 处置 | 来源与判据 |
| --- | --- | --- | --- |
| 1 | 升级或迁移后，**0055 之前产生、没有 provenance 行的内建声音资格不再被准入接受**：同步 TTS 答 `404 voice_not_found`（与已撤销资格同码），公开 Job 答 `422 voice_not_published`。两者都不是 `backend_unavailable`，日志里另有 `code=qualification_source_unverified state=…`。审计读面把这类行标为 `legacy_pre_0055` | **可操作**：为**新的** voice revision 重新取得资格（控制器探测或运维自证两条路径之一），再发布。同一 revision 不能补发，重复发布报 `publication_conflict`（`backend/src/speechnexus_backend/builtin_voice_publication.py:249`）。`unverified`、声明已过窗口、缺探针授权的行同样被拒 | [Builtin Voice 资格边界](../operations/builtin-voice-qualification.md)、[运维声明资格的读面与拒绝码](../design/operator-attested-voice-qualification.md)（拒绝码与两条对外表现的对照表）；迁移 `0055_operator_attestation` |
| 2 | 对一个**已经 `succeeded`** 的执行发取消：Job 会停在 `cancelling`、租约停在 `reserved`、`held_slots` 不归零、对账账本 `attempts` 涨到 64 后饱和并继续以最大退避领取。数据库触发器拒绝 `cancelling → succeeded`，所以它不会自己变成成功，也不会自己变成失败 | **可操作（有界）**：只有带网关 `engine_terminal_response` 证据（`execution_stopped=1`、`resources_released=1`）的那一类能走「有界操作员出口」五步（识别 → 跨结算周期复读 → 现场 `drain` → `completed_release_cli` 写证词并释放 → 核对 `held_slots` 归零）。`/readyz` 与 `admin/diagnostics` 的 `cancellation_debt` 明细按子类给出处置路：`completed-without-release` 走该出口，`execution-unknown` 走隔离/已验证停止路径。**缺终止证据的那一类无出口**，只能等 #338 的停止确认协议 | [取消失败的终态判定](../components/production-jobs.md#取消失败的终态判定)、[有界操作员出口](../components/production-jobs.md#执行已完成取消后到有界操作员出口) |
| 3 | 配了 `SPEECHNEXUS_QUARANTINE_RELEASE_DATABASE_URL` 或 `SPEECHNEXUS_READINESS_CANCELLATION_DSN` 的部署升级后，隔离释放角色启动被拒，消息点名某张表某一列 | **可操作**：迁移后以 owner 重跑 `quarantine_release_grants` 与 `cancellation_debt_reader_grants`（授权面 9 → 12 张关系）。漏跑只有三条可自查信号，见 §4.3；三条都不是故障而是「授权没跟上」 | `deploy/compose.env.example` 的 `SPEECHNEXUS_QUARANTINE_RELEASE_DATABASE_URL` 段；[§6 已有部署的授权升级](../operations/compose-minimal.md)（`docs/operations/compose-minimal.md:1690`） |
| 4 | 操作员为了「干净」重取一份 drain 应答，或者拿一份几天前的旧应答去释放租约——工具会接受 | **无处置，只能避免**：时间窗只证明「这份 drain 发生在该执行结束之后、记录之前」，**不证明它是刚取的**。上界是操作员自己填的 `observed_at`，因此执行结束后任意久取到的真 drain 仍会通过（实测 24 小时前的应答照样释放成功）。唯一做法是**现场取 drain**，这也是它被写成操作步骤而不是校验项的原因 | [有界操作员出口](../components/production-jobs.md#执行已完成取消后到有界操作员出口)「时间窗强制的是先后，不是新鲜度」 |
| 5 | 换了一半的部署被当成同版：旧消费者静默读到新制品；新页面配旧接口（`404`）页面不告警；只升/降级 workbench API 时语音底座摘要仍然相等；长开页面期间后端换版不重查 | **可操作（部分）**：用[前后端同版核对](../operations/same-version-check.md)的人工四项（镜像 label、`web-dist` 摘要、契约摘要 curl 对照 bundle 烘焙值、后端源码树）。**其中 `404` 这一类运行期判据无能为力**：`404` 被当成「未知」而不是「不一致」，所以必须靠第 3 项 curl 对照；工作台 API 单独升降级不在该判据范围内，只能靠发布流程 | [前后端同版核对](../operations/same-version-check.md)「明示限制」；[判定表第 5 行](../plans/mvp-release-readiness.md) |
| 6 | 以为「取消会自动收敛」、以为「保留期已有历史账本」 | **可操作（一半）+ 无处置（一半）**：① 参考部署**不启动**取消对账 worker，`SPEECHNEXUS_JOB_CANCELLATION_RECONCILIATION_ENABLED` 默认 `false`，所以默认部署里没有任何推进者——要自动收敛必须显式启用该 worker 并接受它的轮询间隔。② 迁移 `0054` 之前被标为 `deleted` 的行没有 `deleted_at_us`，那段历史不可追溯，**没有处置**：不要回填计算值，用 `select count(*) … deleted_at_us is null` 把它读成「0054 之前的删除」 | `deploy/compose.yaml` 的服务清单（无取消对账服务）；`0054_artifact_deleted_ledger` 的 docstring 与 `deploy/README.md` 的 artifact-retention 段 |
| 7 | 想用镜像反向定位源码、想从 wheel 里拿到 `LICENSE`、想在受限网络上从源码构建 `minio` | **可操作（各自一半）**：① 应用镜像**没有** `org.opencontainers.image.revision` label（这些 Dockerfile 都不写 `LABEL`；仓库里唯一写 `LABEL` 的是 `deploy/images/minio.Dockerfile`，声明的是上游 minio 的来源，不是本仓库提交），只能用构建记录 + 源码归档 SHA 绑定，或用同版核对第 4 项的逐文件 `sha256` 做法；② wheel 不内嵌 `LICENSE`：子包 `pyproject.toml` 只写 `license = "MIT"`，没有 `license-files`（根 `pyproject.toml` 才写了 `license-files = ["LICENSE"]`），所以许可文本以仓库根的 `LICENSE` 为准，不要从制品里找；③ `deploy/images/minio.Dockerfile` 构建期要访问外网取 LICENSE 校验，受限网络下**无可行构建路径**，复用同 tag 的既有镜像并记为「该路径未验证」 | [从零实跑 §11.1](../operations/compose-minimal-from-zero.md)（`docs/operations/compose-minimal-from-zero.md:356`）、[§11.5](../operations/compose-minimal-from-zero.md)（`docs/operations/compose-minimal-from-zero.md:393`）；[compose-minimal.md §0](../operations/compose-minimal.md)（`docs/operations/compose-minimal.md:28`） |
| 8 | 期望「混搭一定可见」——实际上旧消费者静默读新制品的问题**没有被解决**，只是被收窄了 | **无处置，只能避免**：运行期判据只覆盖「两侧都带 `GET /v1/nexus/version` 且契约修订不同」这一类；由 #853 引入的运行期守卫让这一类从静默变成告警（`role="alert"`，带 `data-nexus-contract-check="mismatch"`），但它**不拒签请求、不做版本协商**，页面开着期间也不重查。旧消费者那一类只能靠构建事实清单在部署前判定 | [同版核对](../operations/same-version-check.md)「这套判据为什么够用」与「明示限制」 |
| 9 | 调语音侧删除：`POST /v1/nexus/deletion-plans`、`POST /v1/nexus/deletions`、`GET /v1/nexus/deletions/{id}` 恒返回 `503 operation_not_implemented` | **无处置，只能避免**：语音侧删除不在首发范围（实施卡 #819 仍 open），要删语音侧内容只能按资产 TTL 与对象版本策略处理，不能用这两个入口。工作台侧删除可用（见 §2），但含已保存资产的项目删除在未配对象区时会停在 `cleanup_state=pending`（行已擦除、对象字节待结清） | `backend/src/speechnexus_backend/speech/interfaces/http/unimplemented.py:82`（三行占位）；[判定表第 9 行](../plans/mvp-release-readiness.md) |
| 10 | 用 `/readyz` 200 判断「可以创建 Job 了」——它不反映 Job 准入可用性，也不反映有多少容量被未收口的取消占着 | **可操作**：`/readyz` 与 `GET /v1/nexus/admin/diagnostics` 里的 `cancellation_debt` detail 才是那部分事实的读面（它**永远报告 `ok`**，不进聚合状态，因为 `/readyz` 是 Compose 健康检查，一个 Job 占着槽位不该让整套部署起不来）。判断「能不能建 Job」用 `POST /v1/nexus/jobs` 的实际应答码，或 `check-async-job-prereqs.py`。**债务极大时探针会变盲而不是变红**：探针自身的超时先于服务预算到期，`check()` 捕获后只把 detail 降级成 `unavailable (timeout)`，`/readyz` 仍是 `200`——所以「绿」在积压很大时只代表「没查出」，不代表「没有债」。实测代价随**仍持有容量的租约数**增长（≈1.2–2.4 µs/条），`≥5 万`条时单次约 111 ms（占探针预算 4.6%）；`cancellation_debt` 另有一段随上线时长线性增长的历史扫描，`0056` 的索引把它从 37.4 ms 降到 0.90 ms | [隔离不了的时候：可见面与处置](../components/production-jobs.md#隔离不了的时候可见面与处置) |
| 11 | 以为浏览器侧已被完整验证：浏览器覆盖只有 Chromium 无头；CI 不跑镜像内 nginx 的完整链路 | **无处置，只能避免**：三个 Playwright 配置都用 `chromium`（`playwright.a11y.config.mjs:19`、`playwright.behavior.config.mjs:27`、`playwright.deployed.config.mjs:29`）；CI 的 deployed-headers 门禁（`.github/workflows/ci.yml:1184`）是把**同一份构建产物**用从 `deploy/web/nginx.conf` 解析出的响应头起静态服务、跑 Chromium 首屏，不是跑镜像里的 nginx 容器。因此不要据 CI 绿宣称「浏览器已通过」；发布前在默认部署上人工跑一次同版核对第 3 项 | `.github/workflows/ci.yml` 的 Web job；`playwright.deployed.config.mjs` |

**一处已不再成立的历史观察**：`DEL-535` 当时见到的 `GET /v1/models` → `503 model_catalogue_unavailable` 在本版本已不复现——`DEL-537` 在同一路径上实测 `200`。要注意的是它的 `readiness` 仍是**保守投影**（`engine_ready=false`、`admission_state=unavailable`），因为默认目录不接实时 readiness 证据（`docs/prd.md:1011` 的既定行为）。**不要用这个响应判断能不能建Job**：同一套部署上 Job 实际创建并成功。判「能不能建 Job」看 `POST /v1/nexus/jobs` 的实际应答码（见已知限制 10 与 [缺什么 → 看到什么 → 做什么](../operations/compose-minimal.md) §9.1）。

## 7. 验证过什么、没验证什么

### 7.1 A 档：真实部署 / 真实引擎上的端到端

- 参考部署（MegaCube Linux aarch64 + NVIDIA GB10，`origin/main` `c0172a22`，独立 Compose 项目
  `del535`，从**空** PostgreSQL 卷与空 MinIO 卷起栈）：`POST /v1/nexus/assets` `201 ready` →
  `POST /v1/nexus/jobs` `202` → `queued → running → succeeded` → 制品 `GET` `200` 且落盘字节
  `sha256` 一致；租约 `released`、`held_slots=0`、配额 `held_attempts=0`。原始时间线与标识见
  [从零实跑 §11.2](../operations/compose-minimal-from-zero.md)（`docs/operations/compose-minimal-from-zero.md:396`）。
- 同一次窗口的资产侧：三类用途与全部反例（见 §2 第一行）。
- 2026-11-02 与 2026-11-03 两个受控窗口的同步 ASR / 同步 TTS 各一次真实 `200`（范围限固定
  声音、单节点、单实例、短英文；早于当前 head）。
- `DEL-534` 窗口：默认部署上真实 Chromium 以同版工作台读到真实 GPU ASR Job 的同一份制品，
  `pageErrors=[]`、CSP 未放宽。
- 工作台侧删除的预检 → 确认 → 账本端到端（Compose 项目 `sn832`）。

### 7.2 B 档：真实进程 / 真实 PostgreSQL / 真实对象存储，但未走真实引擎或未在参考部署上

- 取消围栏、重试、SSE 事件序号续读：隔离 PG + 真实 TCP 用例（`tests/integration/`）。
- 制品保留期清理：隔离 PG + 版本化 MinIO，加一次进程级 `--once`。
- 工作台删除的恢复语义、对象区不可用时的 `cleanup_state=pending`
  （`tests/integration/test_workbench_recovery_postgres.py`）。

### 7.3 C 档：组件级、fake、契约级

- `tests/unit`、`tests/contract`、`MockTransport` 网关、`tests/fake/*`；CI 的 `compose config`
  校验、`reference-acceptance.py --dry-run`。**这些不启动栈**。

### 7.4 明确没有验证的项

| 项 | 为什么没有 |
| --- | --- |
| 参考部署上「零修正照文档」的完整复现 | 两次 GPU 窗口实跑（`DEL-535`、`DEL-536`）都不是零修正：前者要改样例桶名，后者要自行拆出「只构建非 minio 镜像」的序列。这两类偏差都已经修进主干（`#893` 样例桶名；`#903` 子网与 MinIO 地址改为 env 推导，不再需要改仓库文件；`#908` runbook 与验收脚本的构建序列），但「修好之后照文档零变更重跑一次」仍未做，因此判定表第 10 行（Gate `#572`）按「照文档」口径仍未达标 |
| 参考部署上的同步 ASR / 同步 TTS 真实 `200` | ASR 缺最后一步公开调用；TTS 缺已合格内建声音（运维自证路径已实现，真实 `200` 未取得） |
| 制品保留期在参考部署上运行 | `artifact-retention` profile 从未在该部署上启动留证 |
| 取消在默认部署里的自愈 | 默认部署不运行取消对账 worker |
| 真实引擎故障、引擎/控制器重启、物理多机与故障演练 | 无目标环境；`O-PHYSICAL` 不在 MVP 范围 |
| 受限网络下的真实 Web 构建 | 只在非受限路由的主机上验过参数化与产物摘要等价 |
| 浏览器矩阵（非 Chromium）、可访问性全矩阵 | CI 只有 Chromium 无头 |
| 镜像级 provenance 核验 | 应用镜像无 revision label；镜像→提交只到源码归档 SHA 一级 |
| 工作台 API 单独升降级的同版判据 | 该判据只问语音底座；工作台由另一个进程服务 |

## 8. 相关文档

- 判定表与逐行证据：[MVP 发布就绪判定表](../plans/mvp-release-readiness.md)
- 部署唯一真相：[最小同步 ASR/TTS Compose 部署面](../operations/compose-minimal.md)、
  [deploy/README.md](../../deploy/README.md)、[异步 Job 手册](../operations/async-job-runbook.md)
- 从零实跑的实测记录：[最小 Compose 从零实跑](../operations/compose-minimal-from-zero.md)
- 升级相关：[前后端同版核对](../operations/same-version-check.md)、
  [Builtin Voice 资格边界](../operations/builtin-voice-qualification.md)
- 取消与有界出口：[生产 Job/outbox 与 worker 认领](../components/production-jobs.md)
- 契约版本与生成：[contracts/README.md](../../contracts/README.md)、
  [packages/contracts/README.md](../../packages/contracts/README.md)
