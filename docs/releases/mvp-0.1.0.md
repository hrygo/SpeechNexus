---
title: "SpeechNexus MVP 0.1.0 发布说明"
status: active
version: "0.1.0"
date: 2026-11-18
---

# SpeechNexus MVP 0.1.0 发布说明

本说明陈述该版本实际具备的能力、部署与运维入口、升级与回滚边界，以及 16 条已知限制。每一
条都带证据档位与出处：**只有 A 档（真实部署或真实引擎上的端到端）计入「能做什么」**；B 档
（真实进程、真实 PostgreSQL、真实对象存储，但未走真实引擎或未在参考部署上）与 C 档
（组件级、fake、契约级）只说明「实现存在」，不是发布承诺。档位定义与逐行推导见
[MVP 发布就绪判定表](../plans/mvp-release-readiness.md)，本说明不重复它的论证。

**核验基线**：`origin/main` @ `27c932a9` 之前的能力陈述沿用 `767d36e4b18c18119db9ada67e3786ab0e4d8c16`
（2026-11-17T20:48:37+08:00）的核验；同步 TTS、同步 ASR 与转写结算三处已按 2026-11-18 的三个真机窗口更新，分别落在候选 `4542ab6b`、`19c7f09a` 与 `cc981fd4` + `#981` 的守卫移动上（见下方三条增量核验）。
**发布标识**：本版本已打附注标签 `v0.1.0`，指向包含本节文档的那个提交；用
`git describe --tags --exact-match` 或 `git rev-list -n 1 v0.1.0` 核对，不要凭本页正文推断。
标签固定的是**源码与文档**的基线，不固定镜像：Python 侧服务由 `deploy/compose.yaml` 现场构建，
Web 两个镜像按 `deploy/README.md`「Web images」以构建提交为 tag 产出，因此每次部署实际运行的镜像
仍由它自己的构建提交决定，见 §1。

**增量核验**（2026-11-18，卡 `DEL-539`）：在候选 SHA `4542ab6b`（当前 `origin/main` `7c420783`
的父提交）上只更新了同步 TTS 一行（§2、§7.1）、同步 ASR 的缺口与原因（§2、§6 已知限制 15、
§7.4），并新增已知限制 14；其余能力的证据未重新复核。

**增量核验**（2026-11-18，卡 `DEL-540`）：在候选 SHA `19c7f09a279075aea609303bb73dca57b9d4bf23`（`#972` 合入后的 `origin/main`）上更新同步 ASR 一行（§2、§7.1）、把 §7.4 的 ASR 缺口换成实测事实，并新增已知限制 16、改写已知限制 15 的默认值前提；其余能力的证据未重新复核。

**增量核验**（2026-11-18，卡 `DEL-543`）：源码树 = `cc981fd4` 加 `#981` 的守卫移动（`backend/src/speechnexus_backend/asr_settlement_worker.py` 的 blob `b32a9032…` 与 `af437dd0` 逐字相同），在 MegaCube GB10 的 `del541` 参考部署形态上取得同步转写结算的真机读数：`--check` 通过、第一次 `200` 后租约自动归还、紧接着第二次仍 `200`、结算 DSN 缺失时 `503`。§2 的同步 ASR 行、§6 已知限制 16、§7.1 与 §7.4 据此改写；其余能力的证据未重新复核。

**增量核验**（2026-11-19，卡 `DEL-545`）：在同一台机器的**全新数据卷**上（项目 `del545b`，迁移 head `0057_asr_settlement`，`--check` 通过）把同步 TTS 与同步 ASR 同时注册进一套栈并各打一次，取得三段 `200`：TTS-1 `200`（`299564` B、`3.106965 s`）→ ASR `200`（`132` B、`0.894278 s`）→ TTS-2 `200`（`414764` B、`4.106552 s`），三条租约全部 `released` 且 `held_slots` / `held_attempts` 回 0。§2 第 6 行、§7.1 与已知限制 14 的「同栈共存未验」据此改为已验；其余能力的证据未重新复核。

## 1. 版本标识

### 1.1 标识清单：每个值是什么、谁产生

| 标识 | 本版本取值 | 定义与来源 | 谁产生 / 谁核对 |
| --- | --- | --- | --- |
| 镜像 tag | Web 两个镜像：`speechnexus/web-workbench:<sha>`、`speechnexus/web-admin:<sha>`（`<sha>` 是构建提交；构建命令见 [deploy/README.md](../../deploy/README.md)「Web images」）。Python 侧由 `deploy/compose.yaml` 现场构建的服务共 12 个（`minio-init`、`api`、`gateway`、`binding-disable-worker`、`job-dispatch-worker`、`artifact-retention`、`migrate`、`engine-register`、`engine-readiness-renew`、`engine-readiness`、`workbench-migrate`、`workbench-api`），共用同一个 `deploy/images/python.Dockerfile`；`asset-inspector` 用它自己的 `asset-inspector/Dockerfile`；`admin`/`workbench` 共用 `deploy/images/web.Dockerfile`（按 `APP_NAME` 区分）。Compose 给这些镜像的名字是 `<COMPOSE_PROJECT_NAME>-<service>`，**没有版本 tag**。`minio` 复用同 tag 的既有镜像 `speechnexus-minio:RELEASE.2025-09-07T16-13-09Z` | 镜像名由构建方决定，仓库不发布带版本号的镜像制品，也不保存镜像 digest 清单 | 构建方：按 `deploy/README.md` 的「Web images」命令（`docker build … --label`）给 Web 镜像打上 `org.speechnexus.source.commit` 与 `org.speechnexus.source.dirty`；Python 镜像**不带**任何 label（这些 Dockerfile 都不写 `LABEL`；仓库里唯一写 `LABEL` 的是 `deploy/images/minio.Dockerfile`，声明的是上游 minio 的来源，不是本仓库提交） |
| Python 包版本 | `0.1.0.dev0` | 8 个 `pyproject.toml` 的 `version` 字段（根、`backend`、`execution-gateway`、`node-controller`、`asset-inspector`、`packages/contracts`、`packages/media`、`deploy/engines/vllm-asr-lifecycle`）。API 的 `/healthz` 与 `GET /v1/nexus/version` 的 `version` 字段都取自 `backend/src/speechnexus_backend/__init__.py:3` | 人手声明；不随契约修订变化 |
| TypeScript 包版本 | `0.1.0` | 5 个 `package.json` 的 `version` 字段（根、`packages/contracts`、`web/admin`、`web/shared`、`web/workbench`） | 人手声明；不随契约修订变化 |
| 契约 `info.version` | 按文件各自计数，**不存在一个「契约总版本」**。8 个 OpenAPI 文件各有自己的值：`contracts/speech-openapi.yaml:4` = `0.1.0.dev18`、`contracts/admin-openapi.yaml:4` = `0.1.0.dev11`、`contracts/workbench-openapi.yaml:4` = `0.1.0.dev2`、`contracts/internal/execution-control.openapi.yaml:4` = `0.4.0.dev1`、`contracts/internal/asset-inspection.openapi.yaml:4` 与 `contracts/internal/runtime-control.openapi.yaml:4` = `0.1.0.dev1`、`contracts/operations.openapi.yaml:4` 与 `contracts/binding-defaults-producer.openapi.yaml:4` = `0.1.0.dev0` | 每个 OpenAPI 文件自己的 `info.version` 字符串。它被人手维护，`scripts/dev-env/check-http-contract-changes.py` 只把它当作「破坏性变更允许清单」的版本对键，没有「必须递增」的守卫 | 人手维护；运行期不读它 |
| `contract_digest` | `sha256:da291ca201964dd1ec16ca1293f124765c15a447574db437fadc135126c9ec5e` | `contracts/` 下每个 `*.yaml`/`*.json` 文件按相对路径字节序，对「`<相对路径> SPACE <该文件 sha256> NEWLINE`」求总 `sha256`（算法在 `scripts/dev-env/generate-contract-bindings.py:823`）。生成期同时写进 Python 包（`packages/contracts/src/speechnexus_contracts/contract_digest.py`）与 TypeScript 包（`packages/contracts/src/contract_digest.generated.ts`） | 生成器产出一个值，两侧消费者各自持有；API 在运行期自报（`backend/src/speechnexus_backend/speech/interfaces/http/version_identity.py:30`），两个 Web 入口把同一个值烘焙进 bundle 并在页面启动时比对（`web/shared/src/ui/contract-identity.tsx:1`） |

**复核时点（2026-11-17）**：当时上表取值逐项重取后与 2026-11-16 的基线相同——
`contract_digest` 离线重算仍是 `sha256:ad581b46…7863`；`/healthz` 与 `GET /v1/nexus/version`
的 `version` 仍取自 `backend/src/speechnexus_backend/__init__.py:3` 的 `__version__`，摘要仍取自
生成的 `CONTRACT_DIGEST`；8 个 `pyproject.toml` 仍是 `0.1.0.dev0`，5 个 `package.json` 仍是
`0.1.0`；8 个 OpenAPI 文件的 `info.version` 仍是 `0.1.0.dev18` / `0.1.0.dev11` / `0.1.0.dev2` /
`0.4.0.dev1` / `0.1.0.dev1` ×2 / `0.1.0.dev0` ×2。该时点之前，基线之后合并的提交没有改
`contracts/` 的字节。

**摘要的两次后续变化**：`#1009`（2026-11-19 11:04）改了 `contracts/` 的字节，`contract_digest`
变为 `sha256:5c2d96be…e25d`，本页表格当时没有同步；本次 `#941` 把
`nexus.readiness.admission_state` 的契约描述改为静态投影（见已知限制 13），摘要再变为
`sha256:2411332…4942`，上表当时已按该值更新。`#941` 这次是**描述口径**订正：字段名、枚举、
响应形状与生成消费者接口均未变。随后 `#819` 补齐三个删除操作的真实 `503` 声明，摘要再变为
`sha256:da291ca…ec5e`，上表与自查命令已按当前值同步。

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
# {"version":"0.1.0.dev0","contract_digest":"sha256:da291ca201964dd1ec16ca1293f124765c15a447574db437fadc135126c9ec5e"}

# 2. 在要核对的提交上离线重算，并直接比对运行期值
UV_NO_CONFIG=1 uv run python scripts/dev-env/print-contract-digest.py --expect sha256:da291ca201964dd1ec16ca1293f124765c15a447574db437fadc135126c9ec5e

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
| `workspace` 作为分组语义：默认 workspace 承载 Job 与配额，Job 的 `workspace_id` 与准入读取的配额行是同一个 `workspace-default` | [F-SHARED](../prd.md#f-shared) | A（默认 workspace + 注册/配额/准入引用一致；窄口径） | 2026-11-17 从**空 PostgreSQL/MinIO 卷**起参考部署（项目 `sn935`）：`workspace-default` 由迁移种下，配额行由 `engine-register` 经产品入口写入（脚本该行以 `PASS workspace_quotas:` 给出 `workspace-default`、配额 `8`、`policy-revision-compose-example`），`POST /v1/nexus/jobs` `202` 且 Job 回报 `workspace_id=workspace-default`、准入读取的正是该配额行；起点由 `--phase all` 的 `require-empty` 断言钉住（[从零实跑 §14](../operations/compose-minimal-from-zero.md)（`docs/operations/compose-minimal-from-zero.md:628`））。**边界**：单主机、单 workspace，该栈无真实引擎（Job 止于 `202`）；第二个 workspace 的分组行为与身份边界、工作台模块的 workspace 语义未验（判定表第 8 行） |
| 同版浏览器消费：默认部署上由真实 Chromium 打开同版工作台，读到真实 GPU ASR Job 产出的同一份制品并落盘，`pageErrors=[]`，CSP 未放宽；同版读取另有一条同 Job 同 Artifact 的浏览器用例 | [F-CLIENT](../prd.md#f-client) | A（`DEL-534` 窗口 + `#952` 同版读取） | `DEL-534` 的证据体不在仓库内，结论与逐项数值在[判定表第 5 行](../plans/mvp-release-readiness.md)；PR `#952`（源 head `5fbea085`，合入 `b6ec7871`；审查 `#956` 三轮）在真实 Chromium + 真实 PostgreSQL/版本化 MinIO + `create_app` 组合根 + 部署镜像层下让页面读取同一 Job 的同一 Artifact，页面/`/content`/提交行五项一致，bundle 烘焙摘要与 `/v1/nexus/version` 逐字相同，三条反例均红（边界：不启动 compose 栈，Job 与制品行是夹具状态；A 档只限「同版读取」，引擎产出那一半仍不可得）。可执行的离线/在线核对入口是[前后端同版核对](../operations/same-version-check.md) |
| 同步 TTS：`POST /v1/audio/speech` 在按 `compose-minimal.md` §0–§7 起栈的参考部署形态上（七个镜像均由 `4542ab6b` 构建，引擎为同主机外部托管的 `del109-tts`，不是由本 Compose 启动的引擎）以标准请求取得真实 `200`（`audio/wav`、`299564 B`、6.24 s、24 kHz mono） | [F-TTS](../prd.md#f-tts) | A（候选 head `4542ab6b` 真实 `200`） | `DEL-539`（2026-11-18，MegaCube GB10，真实 `del109-tts`）：七个镜像由候选 SHA `4542ab6bc503ca2fb76b2a990aaf0c0dfc6eb9f0` 本地构建（api `sha256:1793ea17598d06df4e767de7650c5fc888ba226ffa303915c5863009e2b6c3a3`、gateway `sha256:41117871b6d2f5d881465d3d19d3f920ac1a4da4795350259bf92121a0c7dd80`），请求 `model=file-tts-qwen3-customvoice-del539`、`voice=voice-vivian-del539`（`voice_revision=1`）、`response_format=wav`、`deadline_ms=120000`、`max_output_audio_ms=30000` → `200 OK` / `content-type: audio/wav` / `content-length: 299564` / `x-sample-rate-hz: 24000` / `x-channels: 1` / `x-request-id: 918f7502…`；音频 mono / 24000 Hz / 16-bit / 149760 frames / 6.24 s，`sha256=0df873ed14e8272402ce449c50d821834ddc1953730308d982d8b8eb1bad762e`；引擎侧 `[OmniTiming] req=speech-9bb81dae… total=2.78s engine=2.78s`（`frames=78 × 1920 = 149760` 与 WAV 帧数相等）；执行 `succeeded`、`cleanup_state=complete`、租约 `released`、停止证据 `engine_terminal_response`。资格为 `operator_attested` 自证（`attest-vivian-del539-20261118`），不是控制器观测；墙钟 `3.055215 s` 未在重建容器后复测；**与 ASR 同栈共存已由 `DEL-545` 验过**（同一栈、两个实例、TTS → ASR → TTS 三段 `200`，见 §7.1） |
| 同步 ASR：`POST /v1/audio/transcriptions` 在按 `compose-minimal.md` §0–§7 精确构建并起栈的参考部署形态上（七个镜像均由 `19c7f09a` 本地构建，引擎为同主机外部托管的 `del109-asr`，不是由本 Compose 启动的引擎）取得真实 `200`；成功后租约在 `DEL-543` 的同一形态上验证为自动归还（见 §7.1 与已知限制 16） | [F-ASR](../prd.md#f-asr) | A（候选 head `19c7f09a` 真实 `200`） | `DEL-540`（2026-11-18，MegaCube GB10，真实 `del109-asr`）：multipart 请求 `model=file-asr-qwen3-1p7b-del540`、`language=en`、`response_format=json`、`file=asr-input.wav`（`299564` B、`sha256=d79ed0a2…`、24 kHz mono 16-bit、6.24 s，取自 `DEL-539` 窗口的 TTS 产物）→ `200 OK` / `content-length: 167` / `content-type: application/json` / `time_total=0.956069 s` / `x-request-id=b2a34de2…`；正文为 `Hello from SpeedGenexus. This is a real product path synthesis from release candidate Del five thirty nine.`、`usage={"type":"duration","seconds":6.24}`；引擎侧 `del109-asr` 同窗口一次 `POST /v1/audio/transcriptions … 200 OK`（`2026-11-18T07:14:14.236812287Z`）；执行 `succeeded`、`cleanup_state=complete`、停止证据 `engine_terminal_response`。**该轮暴露的缺口已关闭**：`DEL-540` 上成功后租约未归还（`lease_state=reserved`、`cleanup_state=pending`、`budget_released_at_us=NULL`、实例 `held_slots=1`/`max_slots=1`）；`DEL-543`（2026-11-18，同一 `del541` 参考部署形态、源码树 = `cc981fd4` + `#981` 的守卫移动）验证为第一次 `200` 后自动 `released`、紧接着第二次仍 `200` 并在一个扫描周期内同样归还，`#974` 已关闭；剩余限制见已知限制 16（只覆盖未知与缺证据形状）。早于当前 head 的历史 `200`（2026-11-02 受控窗口）保留为历史，不再是唯一证据 |
| 工作台侧删除：`POST /v1/workbench/deletion-plans` → `200`、以 `plan_id`/`plan_revision`/`plan_digest` 固定的 `POST /v1/workbench/deletions` → `202` → `succeeded`/`complete`，账本落行、`cascade` 模式擦除正文而不只删元数据行，错 revision/错 digest `409 deletion_plan_changed` | [F-DELETE](../prd.md#f-delete)（仅工作台单元） | A（工作台单元窄口径） | Compose 项目 `sn832` 真实栈实测，逐项见[判定表第 9 行](../plans/mvp-release-readiness.md)；实现为 `backend/src/speechnexus_backend/workbench/application/deletions.py`。**语音侧删除不在本版本**（`/v1/nexus/deletion-plans`、`/v1/nexus/deletions` 恒 `503 operation_not_implemented`，见已知限制 9） |
| 运维可见面：`/healthz`、`/readyz`、`GET /v1/nexus/version`、`GET /v1/nexus/admin/diagnostics`（含 `cancellation_debt` 明细与 `never claimed` / `never claimable (exhausted)` / `due now` 三类账本状态）、启动闸门（Job/workbench/隔离释放/保留期各自在领取前点名缺项） | [O-OBSERVE](../prd.md#o-observe) | A（探活与闸门本身）+ 明确缺口 | 探活期望值见 [compose-minimal.md §7](../operations/compose-minimal.md)（`docs/operations/compose-minimal.md:2184`）；闸门缺项命名见 [deploy/README.md](../../deploy/README.md) 与 [production-jobs.md](../components/production-jobs.md)。**`/readyz` 不反映 Job 准入可用性**，见已知限制 10 |

**不在本版本可达范围内的能力**（不是「以后会做」的承诺，只是当前边界）：实时语音会话
（`/v1/realtime` 等仍是占位）、语音侧删除、声音设计/克隆的 Job 级端到端、物理多机拓扑与故障
演练、浏览器全矩阵资格。范围定义见[判定表 §7](../plans/mvp-release-readiness.md)。声音设计/克隆
的 Job 级泳道（`#529`）已被用户后置，引擎接受面另有 A 档，不计入首发阻塞。

## 3. 部署

**唯一真相是[最小同步 ASR/TTS Compose 部署面](../operations/compose-minimal.md) §0–§8 与
[deploy/README.md](../../deploy/README.md)；「从空卷到第一个 `202`」的顺序、判据与可复跑脚本在
[从空库实跑 §13](../operations/compose-minimal-from-zero.md)（`docs/operations/compose-minimal-from-zero.md:498`），
binding 发布与配额/容量核对的命令在[异步 Job 手册 §7.1–§7.3](../operations/async-job-runbook.md)。
本节不复制这些步骤**，只列这个首发版本独有的前置与顺序陷阱；照文档做完仍起不来的情形，先在
§4.3 的自查信号里找对应的失败类别。

首发特有的前置共五类：

1. **两个 grant 函数必须在迁移后重跑**（仅当配置了
   `SPEECHNEXUS_QUARANTINE_RELEASE_DATABASE_URL` 或 `SPEECHNEXUS_READINESS_CANCELLATION_DSN`）。
   隔离释放角色的声明授权面从 9 张关系扩到 12 张，新增的三张是 `speech_jobs`、
   `speech_job_cancellation_fences`、`speech_job_cancellation_reconciliations`
   （`backend/src/speechnexus_backend/speech/infrastructure/quarantine_release_schema.py` 的
   `READ_COLUMNS`）。以 owner 重跑 `quarantine_release_grants` 与
   `cancellation_debt_reader_grants`。漏跑的四条自查信号见 §4.3，升级路径见
   [§6 已有部署的授权升级](../operations/compose-minimal.md)（`docs/operations/compose-minimal.md:1750`）。
2. **异步 Job 的三个角色与九组授权**：`sn_job_writer` / `sn_job_admission` /
   `sn_job_dispatch` 由 `scripts/dev-env/provision-job-roles.py` 预置；admission 角色必须与
   Job writer 指向同一数据库和服务器，否则启动拒绝。旧版本升级上来、Job writer 仍持有**表级**
   `INSERT` 时，必须先 `REVOKE` 再重授列级权限（[deploy/README.md](../../deploy/README.md)
   的「Optional writers and background units」给出逐条函数清单）。
3. **binding 发布、`execution_defaults`、配额与实例注册**：从空库创建 Job 前，判定顺序是固定
   的四步——该 `model` 有已发布 binding revision（否则 `422 model_not_published`）、输入资产已
   `ready`（`422 asset_not_ready`）、该 workspace 有配额行（`422 workspace_not_configured`）、
   有与已发布 revision 一致的 `ready` 实例与容量（`429 job_capacity_exhausted`）。所以「无实例
   时返回 `429`」只在前三项成立后才可见；从空库最先撞到的是第一步（见
   [从零到第一个 202 的判定顺序](../operations/compose-minimal-from-zero.md)（`docs/operations/compose-minimal-from-zero.md:498`））。
   binding 由三条 admin 路由产生（`POST /v1/nexus/admin/model-bindings` → `…/publications` →
   `…/revisions/{revision}/activations`，命令见 `docs/operations/async-job-runbook.md` 的 §7.1）；
   配额与 `host_nodes` 行只由一次性注册写入（[compose-minimal.md §8](../operations/compose-minimal.md)（`docs/operations/compose-minimal.md:2240`）），
   注册写入的 `binding_revision_id` 必须等于已发布 revision，两边要分别核对。配额与 `host_nodes`
   这两份事实都没有第二个入口，也不必手写 SQL（[§13.3](../operations/compose-minimal-from-zero.md)）。
   已发布的 binding 必须带 `execution_defaults`，否则 asr/tts 创建返回 `422 job_binding_incomplete`；
   升级前用只读预检
   `psql "$SPEECHNEXUS_DATABASE_URL" -f scripts/dev-env/check-public-job-execution-defaults.sql`
   列出缺项，对列出的 binding 重新发布，**不要自动回填**（口径与命令来源：`deploy/README.md:1125`）。
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
   部分（[§6 已有部署的授权升级](../operations/compose-minimal.md)（`docs/operations/compose-minimal.md:1750`））。这一轮至少包括
   `quarantine_release_grants`、`cancellation_debt_reader_grants`、Job writer 的九组函数、
   `gateway_query_grants`/`gateway_abort_grants` 两份一起、`artifact_retention_grants`。
   旧部署还要补 `GRANT SELECT ON speech.alembic_version TO sn_job_dispatch`（派发启动门要读
   head；角色早于该授权的部署必须补，见 `deploy/compose.env.example`）。

   漏跑的可自查信号（四条，都被实现与用例钉住）：

   - `quarantine_release` 面缺授权 → API 启动**点名表列**被拒，或操作员 CLI 退出码 3、
     `unavailable: quarantine_release_privilege_missing`；
   - `cancellation_debt_reader_grants` 缺账本新列（`last_outcome`/`next_check_at_us`）→ 该探针
     整体 `cancellation_debt: unavailable (permission)`（同族 `stop_evidence_debt` 不读这两列，
     不受影响），`/readyz` 仍为 `200`；
   - `stop_evidence_debt` 需要同一身份的列级授权 → 缺授权时同样读不到，读面只剩
     `stop_evidence_debt: unavailable (permission)`；
   - Job 派发启动门缺 `GRANT SELECT ON speech.alembic_version` → 容器退出并点名该表。

   `0056` 建的索引本身不需要任何授权（索引不是可授权对象），只读诊断身份**不加新授权**
   就能用上它；实测只施加 `cancellation_debt_reader_grants` 的 7 条语句，两条债务查询都走
   `speech_jobs_live_state_scan`（0.216 ms / 3.228 ms）。
4. **起 gateway，再起 api 与 worker**。原因是 TTS 内部命令新增的可选字段是**单向兼容**：新
   gateway 能处理旧 backend 的 builtin 命令，旧 gateway 处理新 backend 的 custom 命令会走未知
   路径（`ENGINE_TRANSPORT_UNKNOWN`），不会静默回退。因此不允许「新 backend + 旧 gateway」
   （`deploy/README.md:1135`）。
5. **按需启用 profile**：`job-dispatch`、`artifact-retention`、`workbench-api`、`web`、
   `asset-inspection`。每个 profile 都有自己的启动闸门，闸门失败是**容器退出/重启循环**，不是
   `up -d` 失败——`up -d` 返回 0 不代表该单元可用。

### 4.2 迁移能不能回退：核实过程与结论

**结论：数据库不回退。迁移链是前向唯一的，没有可用的 down 路径；回滚靠镜像，不靠迁移降级。**

核实方式：逐个打开 `backend/src/speechnexus_backend/speech/infrastructure/migrations/versions/`
下的 55 个迁移文件（编号 `0001`–`0056`，缺 `0026`），读每个 `downgrade()` 的函数体，按行为
分类：

| 类别 | 数量 | 代表文件与实测行为 |
| --- | --- | --- |
| 无条件拒绝降级（`downgrade()` 只有 `raise RuntimeError`，没有一句 DDL） | 16 | `0001_jobs.py`：「Destructive Job/outbox downgrade requires an explicit recovery plan」；`0029_builtin_voice_qualification.py`、`0031_tts_settlement.py`、`0033_profile_publications.py` 同类 |
| 条件拒绝（表里有历史数据就 `raise`，只有空表才执行 DDL） | 26 | `0020_artifacts.py`：非空即「Committed Artifacts require preservation and recovery」；`0035_job_cancellation_fences.py`：三张表任一非空即拒绝；`0046_public_job_intents.py` 用 SQL `RAISE EXCEPTION` 实现同一语义；`0052_quarantine_releases.py`：有账本行即拒绝 |
| 无条件执行 DDL | 13 | 其中 4 个**会丢数据**：`0041_voice_drafts.py`（删两张草稿表）、`0043_voice_draft_revisions.py`（删 revision 表）、`0049_workspace_commands.py`（删幂等命令账本）、`0054_artifact_deleted_ledger.py`（删 `deleted_at_us` 账本列）；其余 9 个是视图/索引/派生列（`0019`、`0028`、`0038`、`0040`、`0042`、`0045`、`0048`、`0053`、`0056`） |

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
| 迁移 | `... --profile migration run --rm migrate`；再 `... exec -T postgres psql -U <user> -d <db> -Atc 'select version_num from speech.alembic_version'` | 退出码 `0`，且查询结果等于所用制品的 head（本版本 `0056_debt_probe_live_state_index`）。不相等即「迁移没跑完/跑错身份」，不要手工改版本表 |
| 授权（隔离释放） | 四条，任一命中即漏跑或授权面不全：① API 启动被拒并点名表列（`Quarantine release startup rejected (... declared surface at <table>.<column>)`，`backend/src/speechnexus_backend/quarantine_release_runtime.py:137`）；② 只跑操作员 CLI 时退出码 `3`、输出 `unavailable: quarantine_release_privilege_missing`；③ `/readyz` 的 `cancellation_debt` **整体**变为 `cancellation_debt: unavailable (permission)`（旧账本授权缺 `last_outcome`/`next_check_at_us` 两列；同族 `stop_evidence_debt` 不读这两列，不受影响）；④ 身份能读 Job/围栏/账本但读不到租约/执行表时，detail 退化为 `cancellation_debt: … (jobs: lease-unread=1; never claimed: …; never claimable (exhausted): …; due now: …) … [lease detail unavailable: permission]` | 四条都是「已配置身份但授权不对」的信号，不是故障；③④ 都**不会**让 `/readyz` 失败。③ 是 `#951` 给账本新列加授权之后新增的一条 |
| 服务 | `curl -sS -w '\nhttp=%{http_code}\n' http://127.0.0.1:8000/healthz`、`…/readyz`、`http://127.0.0.1:8010/healthz`、`…/readyz` | 四条都是 `200`，体见 [compose-minimal.md §7](../operations/compose-minimal.md)（`docs/operations/compose-minimal.md:2184`）。`/readyz` 只证明 Postgres 与对象存储连通，不证明 Job、binding、recipe 或引擎可用 |
| 同版 | `curl -sS http://127.0.0.1:8000/v1/nexus/version` 的 `contract_digest`，与该提交上 `print-contract-digest.py` 的重算值、页面 bundle 里烘焙的值 | 三者逐字相同 |
| 异步 Job 前置 | `python3 scripts/dev-env/check-async-job-prereqs.py --env-file deploy/compose.env` | 退出码 `0`（允许 `PENDING`/`SKIP`）；`1` 表示至少一项 `FAIL`，`2` 是参数或路径错。它只读，**只判「缺少权限」，不判「多授予了权限」** |
| 派发 worker | 启动日志里 `deploy/job-startup-gate.py` 的 `OK:` 行；`docker compose ps` 显示该容器在运行且未重启；`logs job-dispatch-worker` 有批次计数 | 无 `OK:` 行或循环重启即前置缺失。迁移 head 不等于镜像内 head 时闸门直接拒绝并点名两侧
（`deploy/job-startup-gate.py:353`）。本机以空环境启动该闸门复现为 5 行 `FAIL:` 加末行汇总、退出码 `1`，不回显 DSN |
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
| 所有在启动期比对「声明授权面」的单元 | 隔离释放角色按列全等比对（多一列、少一列都拒绝，`backend/src/speechnexus_backend/quarantine_release_runtime.py:137`）；Artifact 保留身份只允许改声明列，多出列即拒绝（`backend/src/speechnexus_backend/artifact_retention_runtime.py:158`）；Job writer 的表级 `INSERT` 亦同理 | 升级时按新声明重放过授权，ACL 因此是「新面」。旧镜像声明面更窄 → 上述单元拒绝启动。**所以回滚必须同时把 ACL 退回旧声明面**（`REVOKE` 新增列/关系后重跑旧版本的 grant 函数），这正是 [compose-minimal.md §6](../operations/compose-minimal.md)（`docs/operations/compose-minimal.md:1750`） 写的「回退时…按保存的 ACL 恢复受影响的其他角色」 |
| `workbench-migrate` | 已存在的命名空间只核对记录 revision 等于本构建的（`SCHEMA_REVISION = 11`） | 回滚工作台镜像时，旧 `workbench-migrate` 对不同 revision 会失败；该单元只能前向 |

`#951` 给只读债务身份新增的账本两列（`last_outcome`/`next_check_at_us`）不在上述三处里：这个
身份没有启动期比对，回退授权只会让 `cancellation_debt` 明细整体降级为
`cancellation_debt: unavailable (permission)`（`/readyz` 仍 `200`），不会拒绝启动。

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
   `Up`；按 [§10.1 的 label filter](../operations/compose-minimal.md)（`docs/operations/compose-minimal.md:2385`） 确认没有遗留的一次性
   容器。
2. **把 ACL 退回旧声明面**：对升级时被扩权的角色 `REVOKE` 新增的列/关系，再用旧版本的 grant
   函数重放。自查：旧镜像启动不再出现 `... startup rejected`；旧镜像与旧授权面不读
   `last_outcome`/`next_check_at_us`，`/readyz` 的 `cancellation_debt` 不应变成
   `cancellation_debt: unavailable (permission)`。**若保留新镜像**，则必须保留这两列的授权，
   否则该探针整体降级（`stop_evidence_debt` 不受影响）。
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

以下 16 条：前 13 条与 [issue #912](https://github.com/hrygo/SpeechNexus/issues/912) 的清单一一对应，
第 14、15 条是 `DEL-539`（2026-11-18）实测新增；第 16 条原先记录 `DEL-540` 的「成功后不结算」，该缺口已由 `DEL-543`（同日）验证修好后改写为「未知与缺证据形状」这一仍然存在的限制。按
「使用者会踩到什么 → 怎么处置」重写。「处置」列只有两种取值：**可操作**（有命令或步骤能消掉或
绕过）与**无处置，只能避免**（技术上没有出口，唯一的做法是不进入该场景）。

| # | 使用者会踩到什么 | 处置 | 来源与判据 |
| --- | --- | --- | --- |
| 1 | 升级或迁移后，**0055 之前产生、没有 provenance 行的内建声音资格不再被准入接受**：同步 TTS 答 `404 voice_not_found`（与已撤销资格同码），公开 Job 答 `422 voice_not_published`。两者都不是 `backend_unavailable`，日志里另有 `code=qualification_source_unverified state=…`。审计读面把这类行标为 `legacy_pre_0055` | **可操作**：为**新的** voice revision 重新取得资格（控制器探测或运维自证两条路径之一），再发布。同一 revision 不能补发，重复发布报 `publication_conflict`（`backend/src/speechnexus_backend/builtin_voice_publication.py:249`）。`unverified`、声明已过窗口、缺探针授权的行同样被拒 | [Builtin Voice 资格边界](../operations/builtin-voice-qualification.md)、[运维声明资格的读面与拒绝码](../design/operator-attested-voice-qualification.md)（拒绝码与两条对外表现的对照表）；迁移 `0055_operator_attestation` |
| 2 | 对一个**已经 `succeeded`** 的执行发取消：Job 会停在 `cancelling`、租约停在 `reserved`、`held_slots` 不归零；对账账本的重试预算（`SPEECHNEXUS_JOB_CANCELLATION_RECONCILIATION_MAX_ATTEMPTS`，默认 8、上限 32）用尽后写 `last_outcome='exhausted'`，领取谓词（`last_outcome IS DISTINCT FROM 'exhausted'`）从此排除该行，所以它不会被再领取。数据库触发器拒绝 `cancelling → succeeded`，因此它不会自己变成成功，也不会自己变成失败 | **可操作（有界）**：只有带网关 `engine_terminal_response` 证据（`execution_stopped=1`、`resources_released=1`）的那一类能走「有界操作员出口」五步（识别 → 跨结算周期复读 → 现场 `drain` → `completed_release_cli` 写证词并释放 → 核对 `held_slots` 归零）。`/readyz` 与 `admin/diagnostics` 的 `cancellation_debt` 明细按子类给出处置路：`completed-without-release` 走该出口，`execution-unknown` 走隔离/已验证停止路径，并按账本时刻标出 `never claimed` / `never claimable (exhausted)` / `due now`（`#951`）。**`exhausted` 只能靠操作员出口**：再跑多少轮 worker 都不会领取它；**缺终止证据的那一类无出口**，只能等 #338 的停止确认协议 | [取消失败的终态判定](../components/production-jobs.md#取消失败的终态判定)、[有界操作员出口](../components/production-jobs.md#执行已完成取消后到有界操作员出口) |
| 3 | 配了 `SPEECHNEXUS_QUARANTINE_RELEASE_DATABASE_URL` 或 `SPEECHNEXUS_READINESS_CANCELLATION_DSN` 的部署升级后，隔离释放角色启动被拒，消息点名某张表某一列；或者 `cancellation_debt` 整体变成 `unavailable (permission)` | **可操作**：迁移后以 owner 重跑 `quarantine_release_grants` 与 `cancellation_debt_reader_grants`。隔离释放的声明授权面从 9 张关系扩到 12 张（新增 `speech_jobs`、`speech_job_cancellation_fences`、`speech_job_cancellation_reconciliations`）；`#951` 之后只读债务身份的账本授权还多出 `last_outcome`/`next_check_at_us` 两列，旧的账本两列授权会让该探针整体降级。漏跑或授权面不全有四条可自查信号，见 §4.3；都不是故障而是「授权没跟上」 | `deploy/compose.env.example` 的 `SPEECHNEXUS_QUARANTINE_RELEASE_DATABASE_URL` 段；`backend/src/speechnexus_backend/speech/infrastructure/job_cancellation_fence_schema.py:107` 的 `cancellation_debt_reader_grants`；升级路径见 [§6 已有部署的授权升级](../operations/compose-minimal.md)（`docs/operations/compose-minimal.md:1750`） |
| 4 | 操作员为了「干净」重取一份 drain 应答，或者拿一份几天前的旧应答去释放租约——工具会接受 | **无处置，只能避免**：时间窗只证明「这份 drain 发生在该执行结束之后、记录之前」，**不证明它是刚取的**。上界是操作员自己填的 `observed_at`，因此执行结束后任意久取到的真 drain 仍会通过（实测 24 小时前的应答照样释放成功）。唯一做法是**现场取 drain**，这也是它被写成操作步骤而不是校验项的原因 | [有界操作员出口](../components/production-jobs.md#执行已完成取消后到有界操作员出口)「时间窗强制的是先后，不是新鲜度」 |
| 5 | 换了一半的部署被当成同版：旧消费者静默读到新制品；新页面配旧接口（`404`）页面不告警；只升/降级 workbench API 时语音底座摘要仍然相等；长开页面期间后端换版不重查 | **可操作（部分）**：用[前后端同版核对](../operations/same-version-check.md)的人工四项（镜像 label、`web-dist` 摘要、契约摘要 curl 对照 bundle 烘焙值、后端源码树）。运行期判据只覆盖「两侧都带 `GET /v1/nexus/version` 且契约摘要不同」这一类，`404` 被当成「未知」而不是「不一致」，工作台 API 单独升降级不在该判据内，页面只在加载时比对一次——这四类缺口不变。判定表第 5 行已达标（**A：仅同版读取**），指的是「页面能读到同一 Job 的同一 Artifact」有证据（`DEL-534` 默认部署窗口 + `#952` 组合根用例，源 head `5fbea085`、合入 `b6ec7871`、审查 `#956` 三轮），不是「任何混搭都能被发现」；`#952` 不启动 compose 栈、Job 与制品行是夹具状态，**引擎产出那一半未验** | [前后端同版核对](../operations/same-version-check.md)「明示限制」；[判定表第 5 行](../plans/mvp-release-readiness.md) |
| 6 | 以为「取消会自动收敛」、以为「保留期已有历史账本」 | **可操作（一半）+ 无处置（一半）**：① 参考部署**不启动**取消对账 worker，`SPEECHNEXUS_JOB_CANCELLATION_RECONCILIATION_ENABLED` 默认 `false`，所以默认部署里没有任何推进者——要自动收敛必须显式启用该 worker 并接受它的轮询间隔。`#951` 让读面按账本时刻标出三类事实：`never claimed`（自围栏写入以来没有任何进程领取过这条受理，不证明现在没有进程在跑）、`never claimable (exhausted)`（领取谓词永远排除它，只能靠操作员出口）、`due now`（此刻处于可领取状态，健康 worker 在轮询间隙也会为真，**持续出现**才说明没人推进）。三个标记都只描述账本事实，探针看不到进程是否在跑，也没有增加推进者；未配 `SPEECHNEXUS_READINESS_CANCELLATION_DSN` 时读面仍是 `unavailable (not configured)`。② 迁移 `0054` 之前被标为 `deleted` 的行没有 `deleted_at_us`，那段历史不可追溯，**没有处置**：不要回填计算值，用 `select count(*) … deleted_at_us is null` 把它读成「0054 之前的删除」 | `deploy/compose.yaml` 的服务清单（无取消对账服务）；`backend/src/speechnexus_backend/job_cancellation_runtime.py:99` 的 `enabled` 默认值；`0054_artifact_deleted_ledger` 的 docstring 与 `deploy/README.md` 的 artifact-retention 段 |
| 7 | 想用镜像反向定位源码、想从 wheel 里拿到 `LICENSE`、想在受限网络上从源码构建 `minio` | **可操作（各自一半）**：① 应用镜像**没有** `org.opencontainers.image.revision` label（这些 Dockerfile 都不写 `LABEL`；仓库里唯一写 `LABEL` 的是 `deploy/images/minio.Dockerfile`，声明的是上游 minio 的来源，不是本仓库提交），只能用构建记录 + 源码归档 SHA 绑定，或用同版核对第 4 项的逐文件 `sha256` 做法；② wheel 不内嵌 `LICENSE`：子包 `pyproject.toml` 只写 `license = "MIT"`，没有 `license-files`（根 `pyproject.toml` 才写了 `license-files = ["LICENSE"]`），所以许可文本以仓库根的 `LICENSE` 为准，不要从制品里找；③ `deploy/images/minio.Dockerfile` 构建期要访问外网取 LICENSE 校验，受限网络下**无可行构建路径**，复用同 tag 的既有镜像并记为「该路径未验证」 | [从零实跑 §11.1](../operations/compose-minimal-from-zero.md)（`docs/operations/compose-minimal-from-zero.md:356`）、[§11.5](../operations/compose-minimal-from-zero.md)（`docs/operations/compose-minimal-from-zero.md:393`）；[compose-minimal.md §0](../operations/compose-minimal.md)（`docs/operations/compose-minimal.md:28`） |
| 8 | 期望「混搭一定可见」——实际上旧消费者静默读新制品的问题**没有被解决**，只是被收窄了 | **无处置，只能避免**：运行期判据只覆盖「两侧都带 `GET /v1/nexus/version` 且契约修订不同」这一类；由 #853 引入的运行期守卫让这一类从静默变成告警（`role="alert"`，带 `data-nexus-contract-check="mismatch"`），但它**不拒签请求、不做版本协商**，页面开着期间也不重查。旧消费者那一类只能靠构建事实清单在部署前判定 | [同版核对](../operations/same-version-check.md)「这套判据为什么够用」与「明示限制」 |
| 9 | 调语音侧删除：`POST /v1/nexus/deletion-plans`、`POST /v1/nexus/deletions`、`GET /v1/nexus/deletions/{id}` 恒返回 `503 operation_not_implemented` | **无处置，只能避免**：语音侧删除不在首发范围（实施卡 #819 仍 open），要删语音侧内容只能按资产 TTL 与对象版本策略处理，不能用这两个入口。工作台侧删除可用（见 §2），但含已保存资产的项目删除在未配对象区时会停在 `cleanup_state=pending`（行已擦除、对象字节待结清） | `backend/src/speechnexus_backend/speech/interfaces/http/unimplemented.py:82`（三行占位）；[判定表第 9 行](../plans/mvp-release-readiness.md) |
| 10 | 用 `/readyz` 200 判断「可以创建 Job 了」——它不反映 Job 准入可用性，也不反映有多少容量被未收口的取消占着 | **可操作**：`/readyz` 与 `GET /v1/nexus/admin/diagnostics` 里的 `cancellation_debt` detail 才是那部分事实的读面（它**永远报告 `ok`**，不进聚合状态，因为 `/readyz` 是 Compose 健康检查，一个 Job 占着槽位不该让整套部署起不来）。detail 还按账本时刻把每个受理标成 `never claimed` / `never claimable (exhausted)` / `due now` 三类（`#951`）：前两类分别表示「自围栏写入以来无人领取」与「不会再被自动领取」，持续出现 `due now` 才说明没人推进。判断「能不能建 Job」用 `POST /v1/nexus/jobs` 的实际应答码，或 `check-async-job-prereqs.py`。**债务极大时探针会变盲而不是变红**：探针自身的超时先于服务预算到期，`check()` 捕获后只把 detail 降级成 `unavailable (timeout)`，`/readyz` 仍是 `200`——所以「绿」在积压很大时只代表「没查出」，不代表「没有债」。实测代价随**仍持有容量的租约数**增长（≈1.2–2.4 µs/条），`≥5 万`条时单次约 111 ms（占探针预算 4.6%）；`cancellation_debt` 另有一段随上线时长线性增长的历史扫描，`0056` 的索引把它从 37.4 ms 降到 0.90 ms | [隔离不了的时候：可见面与处置](../components/production-jobs.md#隔离不了的时候可见面与处置) |
| 11 | 以为浏览器侧已被完整验证：浏览器覆盖只有 Chromium 无头；CI 不跑镜像内 nginx 的完整链路 | **无处置，只能避免**：三个 Playwright 配置都用 `chromium`（`playwright.a11y.config.mjs:19`、`playwright.behavior.config.mjs:27`、`playwright.deployed.config.mjs:29`）；CI 的 deployed-headers 门禁（`.github/workflows/ci.yml:1184`）是把**同一份构建产物**用从 `deploy/web/nginx.conf` 解析出的响应头起静态服务、跑 Chromium 首屏，不是跑镜像里的 nginx 容器。因此不要据 CI 绿宣称「浏览器已通过」；发布前在默认部署上人工跑一次同版核对第 3 项 | `.github/workflows/ci.yml` 的 Web job；`playwright.deployed.config.mjs` |
| 12 | 以为「启动失败的输出里没有凭据」——**不成立**：`api` 的 lifespan 失败是 traceback，里面原样带着驱动的连接报文（真口令错时含 `connection to server at "<host>", port <port> failed: FATAL: password authentication failed for user "<role>"`，一次失败打印两次）；`gateway` 控制面、`job-dispatch-worker` 的两条路径同样是 traceback | **无处置，只能避免**：**一行结论类**的闸门（结算预检、asset-inspector、workbench-migrate、workbench-api、`speechnexus-job-dispatch-worker` 自身入口）确实不泄露，实测按 SQLSTATE 分类后只留「登录被拒 / 服务器不可达 / 异常类型名」；但 traceback 类单元打印的是驱动原文，**任何「启动失败不泄露凭据」的通稿都是假的**。处置只有一条：这些单元的输出**不要整段贴进公开 issue 或聊天**，要贴就贴 `FAIL:` 行与异常类型名 | `docs/operations/startup-gate-output.md` 的 24 行表；`docs/operations/async-job-runbook.md` §1；`compose-minimal.md` §9.1 |
| 13 | 以为 `GET /v1/models` 的 `nexus.readiness.admission_state` 是「现在能不能建 Job」的答案——**契约已把它定义成静态投影**（`contracts/speech-openapi.yaml` 的 `ModelReadiness.admission_state`：它表示本部署是否建立了按 workspace 的准入事实面，**不是某个 workspace 此刻能否被准入**），而本端点的取值恒为 `unavailable` | **无处置，只能避免**：这不是「还没接实时证据」——`admission_state` 是**按 workspace** 计（`backend/src/speechnexus_backend/speech/infrastructure/public_job_admission.py:695` 用 `job.workspace_id`、`:514` 按 `speech_assets.workspace_id` 过滤），而 `GET /v1/models` 的签名里**没有 workspace**（`backend/src/speechnexus_backend/speech/interfaces/http/models.py:42`；契约 `:59-68` 只有 `X-Request-ID` 与 `If-None-Match`），所以这个字段**接不进实时准入**：接入多少实时事实都改变不了这一点。口径已按 [#941](https://github.com/hrygo/SpeechNexus/issues/941) 订正为静态投影，**消费者不需要改**。判「能不能建 Job」看 `POST /v1/nexus/jobs` 的实际应答码；按 workspace 的实时读面是 `GET /v1/nexus/jobs/admission-readiness`（带 `workspace_id`），**当前仍是占位**，答 `503 operation_not_implemented` | 静态投影的装配与原因在 `backend/src/speechnexus_backend/model_catalog_runtime.py:92` 起的装配注释；口径订正见 [#941](https://github.com/hrygo/SpeechNexus/issues/941)；运维侧措辞见 [异步 Job 运行手册](../operations/async-job-runbook.md) §4 |
| 14 | 把 `operator_attested` 资格链下取得的同步 TTS `200` 当成容量或部署形态结论：该次实测的 `capacity-manifest` 里 `management_mode` 与实际部署形态不一致，`host_peak_held` 低于声明峰值 | **无处置，只能避免**：资格链允许在已知不一致下自证（`DEL-539` 实际通过），但这两项不能用来推导容量、峰值或部署形态；要容量结论必须有与真实部署一致的 manifest 与测量。这两条不一致已作为该次实测的保留限制进判定表第 6 行 | [判定表第 6 行](../plans/mvp-release-readiness.md)、`DEL-539` 报告 §3–§4、[运维声明资格的读面与拒绝码](../design/operator-attested-voice-qualification.md) |
| 15 | 从旧 env 文件升级或自己起栈时，把同步上传桶与网关输入桶配成不同名字：`POST /v1/audio/transcriptions` 在引擎调用前答 `503 backend_unavailable`，账本只留 `unknown_quarantined` / `error_code=engine_transport_unknown`，引擎侧没有任何转写请求 | **可操作（配置一致即可）**：`SPEECHNEXUS_TRANSCRIPTION_INPUT_BUCKET` 必须与网关 ASR 私有配置 `objects.input_bucket` 取同一桶名，否则 `execution-gateway/src/speechnexus_gateway/infrastructure/execution_objects_minio.py:111` 在引擎调用前抛 `execution_input_scope_mismatch`。默认值已由 [#972](https://github.com/hrygo/SpeechNexus/issues/972) 对齐（两处同为 `speechnexus-results`），`tools` 阶段会前置拦截不一致；从旧文件继承下来的值仍要自己核对 | [判定表第 7 行](../plans/mvp-release-readiness.md)、`deploy/compose.yaml:118`、`deploy/private/gateway-file-asr.example.json:9`、`execution-gateway/src/speechnexus_gateway/infrastructure/execution_objects_minio.py:111` |
| 16 | 同步转写的**未知与缺证据形状**租约没有自动推进者：`200` 之后的成功路径已自动结算（见 §7.1），`failed`/`aborted` 只要清理完成且有停止证据也会被结算；但引擎提交未知（`unknown_quarantined`）、缺停止或清理证据、路由不匹配、以及被擦除的请求文档（无 root）这四类，`AsrSettlementWorker` 不会替它们下结论：前三类的 root 保持未结算（`speech_asr_settlement.settled_at_us` 为 `NULL`），被擦除的那类连 root 都没有；四种情况下槽位都仍被占 | **无处置，只能避免（有读面）**：`GET /v1/nexus/admin/diagnostics` 的 `asr_settlement_debt` detail 按 `unknown` / `cleanup_pending` / `route_mismatch` / `settled_but_held` / `redacted_held` 五个分类报出条数与样本（`settled_but_held` 与 `redacted_held` 正是「已结算或不可归因、但槽位仍被占」），`python -m speechnexus_backend.asr_settlement_worker --once` 可以手动再扫一轮（每轮只结算能判定的；它按配好的 gateway/节点路由扫描，`route_mismatch` 与无 root 的两类扫不到），但**没有**「强制释放」入口：这四类只能等事实明确，或按 `completed_release_cli` 的取消形状另走——而同步转写没有 `speech_jobs` 行，该工具会以 `completed_release_not_accepted_cancellation` 拒绝（已实测），所以对本形状等于没有出口。不要用「同步 ASR 成功率高」推断「租约从不占槽」；占槽时先看该 detail。另一条未验项：升级回填（`0057` 把升级前仍被持有的同步 ASR 租约回填成 root）在真机上未被走到——`DEL-543` 复用的库当轮已无遗留租约，`--once` 如实报 `scanned=0` | [判定表第 7 行](../plans/mvp-release-readiness.md)、`backend/src/speechnexus_backend/asr_settlement_worker.py`、[#974](https://github.com/hrygo/SpeechNexus/issues/974) |

**一处已不再成立的历史观察**：`DEL-535` 当时见到的 `GET /v1/models` → `503 model_catalogue_unavailable` 在本版本已不复现——`DEL-537` 在同一路径上实测 `200`。要注意的是它的 `readiness` 仍是**静态投影**（`engine_ready=false`、`admission_state=unavailable`）：契约现在明确这个字段只报告本部署是否建立了按 workspace 的准入事实（`#941` 口径订正），**本端点从不承载实时准入**——**不是「等接入实时证据就对上了」**。**不要用这个响应判断能不能建Job**：同一套部署上 Job 实际创建并成功。判「能不能建 Job」看 `POST /v1/nexus/jobs` 的实际应答码（见已知限制 10 与 [缺什么 → 看到什么 → 做什么](../operations/compose-minimal.md) §9.1）。

## 7. 验证过什么、没验证什么

### 7.1 A 档：真实部署 / 真实引擎上的端到端

- **异步主链，零修正照文档（`DEL-537`，2026-11-16，MegaCube Linux aarch64 + NVIDIA GB10，
  第二轮先 `down -v` 清卷）**：先 `down -v` 清卷，再按 `compose-minimal.md` §0–§7 原样执行（只改
  gitignored `deploy/compose.env` 的宿主端口），7 个应用镜像由基线 `b63fe004` 本地构建。
  **同一轮内** `compose-minimal.md` §7.1 创建并发布 binding（`published_at 14:03:37.507518Z`）、
  §7.3 注册实例，随后全链
  `POST /v1/nexus/assets` `201` → `POST /v1/nexus/jobs` `202` → `pending → running → succeeded`
  （`job-d9feb971cb4d4ad3a784904d4af19710`，14:05:55.047 → 14:05:57.442），制品 92 B 且
  `sha256`/`etag` 与下载字节一致，租约 `released`、`held_slots=0`、`held_attempts=0`、
  `nonreleased_leases=0`，引擎侧一次真实 `200`（`14:05:56.474287514Z`）。因此「空卷 → 发布
  binding → Job `succeeded`」是**同一次连续运行**，它同时可支撑 F-JOB / F-ASYNC / F-ARTIFACT
  的同一段链路；**判定表仍按 `DEL-535` 记第 2–4 行**，本层按 Gate `#572` 与部署入口计入。
- **从空卷起栈（`DEL-535`，同日更早，`origin/main` `c0172a22`，独立 Compose 项目 `del535`）**：
  从**空** PostgreSQL 卷与空 MinIO 卷起栈，`assets 201 ready` → `jobs 202` →
  `queued → running → succeeded` → 制品 `GET 200` 且落盘字节 `sha256` 一致；租约 `released`、
  `held_slots=0`、配额 `held_attempts=0`。原始时间线与标识见
  [从零实跑 §11.2](../operations/compose-minimal-from-zero.md)（`docs/operations/compose-minimal-from-zero.md:396`）。
- **从真正空卷到第一个 `202`（`#935`/`#876`，2026-11-17，macOS arm64）**：项目 `sn935` 从空
  PostgreSQL/MinIO 卷起同一套，`check-from-empty-first-job.sh --phase all` 的 `require-empty`
  先断言五行计数与 `held_slots` 全为 0（`PASS empty start: publications=0 binding_revisions=0
  workspace_quotas=0 host_nodes=0 engine_instances=0 held_slots=0`），随后发布 binding、上传
  资产、注册引擎、创建 Job 全走产品入口（零手写 SQL），`POST /v1/nexus/jobs` `202` 且
  `workspace_id=workspace-default`。这一层覆盖 F-SHARED（默认 workspace 与注册/配额/准入引用
  一致）与部署入口的第二层证据；边界是单主机、单 workspace、无真实引擎（Job 止于 `202`），
  第二个 workspace 的分组行为/身份边界与工作台模块的 workspace 语义未验。原始输出见
  [从零实跑 §14](../operations/compose-minimal-from-zero.md)（`docs/operations/compose-minimal-from-zero.md:628`）。
- **资产侧（`#867`/`#851`）**：参考部署上三类用途与全部反例（见 §2 第一行）。
- **同版浏览器消费（`DEL-534` + `#952`）**：`DEL-534` 在默认部署上用真实 Chromium 的同版工作台
  读到真实 GPU ASR Job 的同一份制品，`pageErrors=[]`、CSP 未放宽；`#952`（源 head
  `5fbea085`，合入 `b6ec7871`，审查 `#956` 三轮）另在真实 Chromium + 真实 PostgreSQL/版本化
  MinIO + `create_app` 组合根 + 部署镜像层下让页面读取同一 Job 的同一 Artifact，页面身份、
  字节数、`/content` 的 `etag`/`content-length` 与提交行 `sha256` 五项一致，bundle 烘焙摘要与
  `/v1/nexus/version` 逐字相同。边界：它不启动 compose 栈，Job 与制品行是夹具状态（本机无
  GPU）；**A 档只限「同版读取」这一半，引擎产出那一半仍不可得**，`DEL-534` 是参考部署上的
  另一段证据。
- **工作台侧删除（Compose 项目 `sn832`）**：预检 → 确认 → 账本端到端，含级联擦除正文。
- **同步 TTS 在候选 head 的参考部署形态上的真实 `200`（`DEL-539`，2026-11-18，MegaCube GB10）**：按
  `compose-minimal.md` §0–§7 起栈、七个镜像均由 `4542ab6b` 构建，引擎是同主机外部托管的 `del109-tts`
  （不是由本 Compose 启动的引擎）；以
  `model=file-tts-qwen3-customvoice-del539`、`voice=voice-vivian-del539`（`voice_revision=1`）、
  `response_format=wav`、`deadline_ms=120000`、`max_output_audio_ms=30000` 取得 `200 audio/wav`
  （`299564 B`、6.24 s、24 kHz mono、`sha256=0df873ed…`）；引擎侧
  `[OmniTiming] req=speech-9bb81dae… total=2.78s engine=2.78s` 与 WAV 帧数 `149760 = 78 × 1920`
  精确对齐；执行 `succeeded`、租约 `released`、停止证据 `engine_terminal_response`。边界：资格来自
  `operator_attested` 自证（已知限制 14 的 `capacity-manifest` 两项与真实部署不一致）、墙钟未在
  重建 API/gateway 容器后复测。与 ASR 同栈共存见下一节的 `DEL-545`。
- **同一栈上同步 TTS 与同步 ASR 共存（`DEL-545`，2026-11-19，MegaCube GB10）**：全新数据卷、
  全新项目 `del545b`（旧卷保留未删），迁移 head `0057_asr_settlement`，`python -m
  speechnexus_backend.asr_settlement_worker --check` 输出 `asr settlement: check ok`。两次注册
  （ASR 先、TTS 后）都显式传同一个 `--host-capacity 22486808986,0,0,6480,15099495,0`（两条 plan 逐维之和
  再留 20%）与同一个 `--policy-revision policy-silero-vad-del537`，两次均 `registered`，没有出现
  `host_capacity_insufficient`；`instance-asr-1` 与 `instance-tts-1` 同属 `node-asr-del541` / `gateway-1`，
  `base_budget_kind=steady`。三段请求按 TTS → ASR → TTS 顺序：TTS-1 `200`（`299564` B、
  `time_total=3.106965`）、ASR `200`（`132` B、`0.894278`，正文 `{"text": "Speech Nexus verification.\nThe
  quick brown fox jumps over the lazy dog.", "usage": {"type": "duration", "seconds": 6.24}}`）、TTS-2 `200`
  （`414764` B、`4.106552`）——**ASR 的成功没有把 TTS 的槽占死**。三条租约 `lease_state=released`，
  `budget_released_at_us` / `settled_at_us` 齐全（TTS-1 `1795021925644449` / `1795021925659817`、
  ASR `1795021940501134` / `1795021940506034`、TTS-2 `1795021978922358` / `1795021978935795`），两个实例
  `held_slots=0`、工作空间 `held_attempts=0`。边界：GPU 显存与真实 GPU 指标未读（共享宿主 `nvidia-smi`
  不可用）；停止/取消收敛、`quarantine` 恢复、实例基础预算释放未验；单节点、单实例、固定声音、短英文同步链，
  未做并发或持续负载；使用了 `/tmp` 下的部署 override（让 API 加入 gateway 网络命名空间，未写 repo 文件）。
  另有一次**不带 `nexus.workspace_id`** 的预检请求落到默认 `workspace-default`（该栈没有这个工作空间的
  配额）而得到 `503 backend_unavailable`，没有创建租约或用例——它记录的是「请求必须带隔离工作空间」这条
  使用约束，不是结算链故障。
- **同步 ASR 在候选 head 的参考部署形态上的真实 `200`（`DEL-540`，2026-11-18，MegaCube GB10）**：按
  `compose-minimal.md` §0–§7 精确构建并起栈、七个镜像均由 `19c7f09a` 本地构建，引擎是同主机外部托管的
  `del109-asr`（不是由本 Compose 启动的引擎）；以 `model=file-asr-qwen3-1p7b-del540`、`language=en`、
  `response_format=json` 上传 `asr-input.wav`（`299564` B、`sha256=d79ed0a2…`、24 kHz mono 16-bit、6.24 s，
  取自 `DEL-539` 窗口的 TTS 产物）取得 `200 OK`（`content-length: 167`、`time_total=0.956069 s`——
  **这是该次 HTTP 响应的时间，不是端到端时延**、`x-request-id=b2a34de2…`），正文
  `Hello from SpeedGenexus. This is a real product path synthesis from release candidate Del five thirty nine.`、
  `usage={"type":"duration","seconds":6.24}`；引擎侧 `del109-asr` 同窗口一次
  `POST /v1/audio/transcriptions` `200 OK`（`2026-11-18T07:14:14.236812287Z`）。执行 `succeeded`、
  `cleanup_state=complete`、停止证据 `engine_terminal_response`。边界：
  本次 `200` 之后租约未归还（`lease_state=reserved`、`cleanup_state=pending`、`held_slots=1/1`），该缺口随后由 `DEL-543` 在同一形态上修好并实测：第一次 `200` 后租约 `released`、`budget_released_at_us` 与
  root `settled_at_us` 非空、`held_slots`/`held_attempts` 回 0，紧接着第二次 `200` 并在一个扫描周期内同样归还；
  转写文本未与源文本逐字比对（`DEL-540` 的响应把 `SpeechNexus` 识别成 `SpeedGenexus`；`DEL-543` 只留了状态码、字节数、耗时与 `x-request-id`，正文未归档）、GPU 显存未读。
  早于当前 head 的 2026-11-02 受控窗口 `200`（1.237 s，记录在 PR #317 正文与 issue #243）作为历史保留，
  不再是唯一证据。

这些证据合起来，判定表计为 **11 行达标**（F-ASSET、F-JOB、F-ASYNC、F-ARTIFACT、F-CLIENT、
F-SHARED、F-DELETE、F-TTS、F-ASR、Gate `#572`、部署入口）；声音设计/克隆的 Job 级泳道（`#529`）
无 A 档但已被用户后置，不计入首发阻塞。**原第 16 条里的「成功后不结算」已随 `#974` 关闭**：同步转写成功后的租约自动归还已在 `DEL-543` 的同一形态上实测（见 §7.1）；该条随即改写为仍然存在的限制——「未知与缺证据形状的同步 ASR 租约没有自动推进者」（见 §6 第 16 行）。
逐行推导见[判定表](../plans/mvp-release-readiness.md)。

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
| 第二位操作者 / 第三台主机的独立复验 | 部署入口的两层证据是同一操作者换两台主机（`DEL-537` 在 MegaCube GB10，`#935`/`#876` 在 macOS arm64）；判定表第 12 行的「他人」只到这个范围，没有第二位操作者或第三台主机。MegaCube 宿主还须按 §0 预存同 tag 的 MinIO 镜像 |
| 同步 ASR 转写文本与源文本的逐字比对 | `DEL-540` 的 `200` 不是逐字相同：`SpeechNexus` 被识别成 `SpeedGenexus`（同音误识），而源文本未归档，因此只能陈述这一例的可见差异，不能给出字错率 |
| 并发同步转写的排队形态 | `DEL-543` 只做了串行两次（第一次 `200` → 租约自动归还 → 紧接着第二次 `200`），单槽实例在并发到达时的排队、超时与拒绝形态未测 |
| 同步 ASR 的端到端时延 | 实测 `time_total=0.956069 s` 是这次 HTTP 响应的时间（curl 含 DNS/连接/传输），不覆盖客户端排队、请求体准备与后续读回，不能当端到端时延或 P95 |
| 同步 ASR 的 GPU 显存 | 该引擎主机上 `nvidia-smi` 不可用，未读到显存占用 |
| 重建 API/gateway 容器后的同步 TTS 墙钟 | 该参考部署形态（§0–§7 起栈）测得 `3.055215 s` 是重建前的值，重建后未复测；重建后的墙钟只能由响应 `Date`、`OmniTiming total=2.78s` 与 WAV 帧数互为佐证，不构成实测值（`DEL-539`） |
| 制品保留期在参考部署上运行 | `artifact-retention` profile 从未在该部署上启动留证 |
| 取消在默认部署里的自愈 | 默认部署不运行取消对账 worker；`#951` 只让读面可观察（`never claimed` / `never claimable (exhausted)` / `due now`），没有增加推进者 |
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
