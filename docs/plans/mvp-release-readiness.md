# MVP 发布就绪判定表

**核验基线**：`origin/main` @ `6cc8090ad6d8247c4e10cd68d5adfae2bf02066c`（`git fetch` 后实测；任务指定的 `f01b10dd` 为其父提交）。独立工作树 `/private/tmp/sn818`。
**核验日期**：2026-11-15。核验方式：新工作树内读源码、Compose、契约、测试、CI 与本机文档记录；GitHub issue/PR 状态经 `gh` 读取。
**增量核验**（2026-11-15，卡 #832）：`origin/main` @ `bfafc8942ba85b6590a79d914959e356bb01013d`，独立工作树 `/private/tmp/sn833`。只重新核验了 F-DELETE 行、B6 与其部署入口相关陈述，其余行沿用上面的基线。
**增量核验**（2026-11-15，卡 #829）：`origin/main` @ `5cd5a5d0c86a91f73fca6f7cf797e38097b41782`，独立工作树 `/private/tmp/sn841`。重新核验 F-CLIENT 行与 B1：同版镜像构建与前端产物/契约包/API 三方来源一致性按实测更新；工作台同版读取的真实浏览器证据在本机变红（CSP 拒绝 `unsafe-eval`，另见 §4 B7）。其余行沿用上面的基线。
**增量核验**（2026-11-15，卡 #823）：`origin/main` @ `c7e43f9a2926507578513029ee8d1ee07ebce688`，独立工作树 `/private/tmp/sn855`。只重新核验三处陈旧陈述（同步 ASR 终态写入、公开 Job 路由集合、生产 planner）并就地订正，其余行沿用上面的基线。
**增量核验**（2026-11-16，卡 #851）：`origin/main` @ `76300ad56`（`#851` 合入后），独立工作树 `/private/tmp/sn867`。只重新核验 F-ASSET 行、部署入口行与 §1 里已修复的 Web 入口缺陷陈述，其余行沿用上面的基线。
**增量核验**（2026-11-16，卡 #867）：`origin/main` @ `44b41647`（`#867` 合入后），独立工作树 `/private/tmp/sn872`。只把 F-ASSET 行按部署级证据改为 A 档，并相应更新 §1 与 §3；其余行沿用上面的基线。
**增量核验**（2026-11-16，卡 DEL-534）：`origin/main` @ `c51c5443`（`#868` 合入后），独立工作树 `/private/tmp/sn875`。把 F-CLIENT 行按 MegaCube 上的默认部署浏览器证据改为 A 档（`#842` 之后成立），并相应更新 §1 与 §3；其余行沿用上面的基线。
**增量核验**（2026-11-16，卡 #829 AC4）：`origin/main` @ `c0172a22`，独立工作树 `/private/tmp/sn885`。只处理「构建方式写进部署文档、下一个照文档能重复」这一条：把 `deploy/README.md` 的构建段改成带判据的写法，并把受限网络下的构建方式固化进仓库（把产品 `deploy/images/web.Dockerfile` 的 base 镜像与 npm 源参数化 + `deploy/compose.build-mirror.override.yaml` 只传这三个 build arg，默认值＝现值、默认路径 `compose config` 前后逐字节相同、产物同摘要）；F-CLIENT 行的「仍未覆盖」表述随之订正。其余行沿用上面的基线。

## §1 结论

**MVP 现在不能发布。**

- MVP 界定为 12 个判定行。**其中 8 行没有任何 A 档证据**（A 档＝真实部署或真实引擎上的端到端），见 §3。
- 存在的 A 档证据是三处：两条同步入口在 **MegaCube（Linux aarch64 + NVIDIA GB10）单次受控窗口**里跑通过（2026-11-02 / 2026-11-03）；声音引擎在**同一主机上的接受面直连探测**（2026-11-07/08）；**F-ASSET 的受控输入资产在参考部署上跑通三类用途**（`#867` 的 `asr_input`/`voice_reference`、`#851` 的 `text_input`，2026-11-16）；以及 **F-CLIENT 在同一台 GPU 主机的默认部署上由同版工作台读到真实引擎产出的同一份制品**（`DEL-534`，2026-11-16：真实 GPU ASR Job → 92 字节制品 → 浏览器落盘字节重算 `sha256` 与 Job 记录一致，`pageErrors=[]`，CSP 未放宽）。第四条已经把「公开 Job 从创建到制品可下载、并被人读到」这一段走通了，**但它用的数据库是复刻来的、不是从空库走文档迁移路径**，所以 `#572`「按文档从零跑通」仍未取证。
- **最长阻塞链只有一条**：真实引擎/GPU 窗口 → 参考部署完整 `up`（迁移 + `minio-init` + 引擎注册 + 三个 Job 角色与授权）→ `job-dispatch-worker` 启动闸门 → 一条 asr 或 tts Job 从 `queued` 走到 `succeeded` → Artifact 可下载 → 工作台按同版读到。**这条链上没有任何一环有 A 档记录**，其中最关键的一环（#572）自 2026-11-08 开卡至今未执行过。
- **Web 入口级缺陷已修，剩余缺口是证据不是缺陷**：`#829` 用同版镜像起真实栈实测时，两个前端镜像在自身 `Content-Security-Policy`（`script-src 'self'`，无 `'unsafe-eval'`）下渲染空文档，原因是生成契约校验器走 `ajv.compile`（`new Function`）。`#842`（`c9a48963`）改为预编译 standalone 校验器后该路径消失：`admin` 1208 字符、`workbench` 752 字符、`pageerror` 0，**未放开 `'unsafe-eval'`**。F-CLIENT 仍缺的不是渲染，而是**同版消费同一 Job 的同一 Artifact**（需一份可读的真实 artifact，随 `#572` 一并取证）。
- 主链之外的第二个硬缺口是**语音侧删除**：`/v1/nexus/deletions` 与 `/v1/nexus/deletion-plans` 是中央占位路由，恒返回 `503 operation_not_implemented`（`backend/src/speechnexus_backend/speech/interfaces/http/unimplemented.py:72`）；语音侧删除**不在首发范围**（实施卡 [#819](https://github.com/hrygo/SpeechNexus/issues/819) 仍 open）。工作台侧删除的实现与部署前提已经具备：参考部署以 `workbench-api` profile 装配独立工作台后端，#832 在真实容器栈上取得该路径的端到端证据（见 §2 第 9 行）。

### 证据档位

| 档 | 含义 | 本仓库的典型载体 |
| --- | --- | --- |
| **A** | 真实部署或真实引擎上的端到端 | 仅在受控 GPU 窗口内出现过，CI 与参考部署都未产生 |
| **B** | 真实进程/真实 PG/真实对象存储/真实 HTTP，但单机或未走真实引擎 | `tests/integration/**`（真实 PG + InnoDB + 版本化 MinIO + 真实 Chromium/生产 bundle），以及 2026-11-13 的容器级实操 |
| **C** | 组件级、单元、fake、契约级 | `tests/unit`、`tests/contract`、`MockTransport` 网关、`tests/fake/*` |
| **D** | 仅文档陈述或无证据 | PRD/矩阵里的自述、未执行的步骤清单 |

判定口径：**只有 A 档才计入 MVP 达标**。矩阵 [`delivery-tracking.md`](delivery-tracking.md) 的「验证」列已按此逐行回代码核实，凡矩阵与代码冲突处按代码计。

## §2 判定表

| # | MVP 需求行 | 要求什么证据 | 当前真实证据（档位 + 出处） | A 档 | 判定 | 阻塞在谁/什么 |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | **F-ASSET** 受控输入资产（`asr_input`/`voice_reference` → `ready`） | 真实音频检查事实 + 固定对象版本 + 真实 HTTP 上传 | **B**：2026-11-13 在 Compose 项目 `sn680` 用真实容器、真实检查器、真实 PG/MinIO 跑通 `POST /v1/nexus/assets` → `201 ready`（`duration_ms`/`sample_rate_hz` 来自回执），并有 `text` 选择器 → `503`、无残留行的反例（[runbook `docs/operations/async-job-runbook.md:922`](../operations/async-job-runbook.md) 起）。同轮为让 API 起来用了两处**非生产**改动（清空 `depends_on`、合成配方 + 超范围授权）。另有进程内 HTTP + 真实解码子进程用例 `tests/unit/backend/test_voice_reference_upload.py` | **A（`asr_input` / `voice_reference` / `text_input` 三类均有）** | **达标** | **A 档（`#867`，2026-11-16）**：独立 Compose 项目 `sn869`，真实 `postgres:16.15-alpine` + MinIO + `migrate` @ `0054_artifact_deleted_ledger` + 文档 §6 的 18 个非 owner 角色 + `provision-job-roles.py`（221 条授权）+ 从仓库源码构建的 `api` 与 `asset-inspector`（`asset-inspection` profile）。`asr_input` 上传 32044 B / 1 s / 16 kHz 单声道 WAV → **`201 Created`、`status=ready`、`duration_ms=1000`、`sample_rate_hz=16000`、`channels=1`**，`sha256` 与源文件一致，对象 `storage_version_id` 固定，下载 32044 B 且 SHA-256 一致；同文件改 `voice_reference` → 同样 `201 ready` 与三列事实。反例齐备：随机字节 → `422 invalid_asset_audio` 且无新行；默认 `text` → `503 asset_inspection_unavailable`（`retryable:false`，行数不变）；停掉检查器 → 同一 503 响应体逐字相同且 `api` 保持 `Up (healthy)`；`text` 却配了 URL/令牌 → **api 启动失败并点名变量**（不降级）。`#851` 另给了 `text_input` 用途在带 `Origin` 的浏览器式请求下 `201`（且证明差异只来自该头）。检查器容器为 `Privileged=false`、`CapDrop=[ALL]`、`ReadonlyRootfs=true`、`no-new-privileges`、`PidsLimit=64`、`Memory=768MiB`、`NanoCpus=2.0`、无宿主端口、只接 `internal: true` 网络——**不含任何提权**。**与旧 B 档的差别**：2026-11-13 那次（`sn680`）为让 API 起来用了两处非生产改动（清空 `depends_on`、合成配方 + 超范围授权），本轮两者都不需要（`--no-deps` 只是命令行范围控制；私有样例原样通过生产 loader）。**本行仍未覆盖**：带 `Origin` 的音频上传（`#851` 只实测了 `text_input`）；超时/取消/令牌不一致三项验收（`#867` §8 A4/A5/A7）
| 2 | **F-JOB** 持久 Job（创建/读取/取消/重试/SSE/列表） | 权威状态一致、事件序号续读、取消围栏真库证据 | **A（窄口径，创建/读取/终态）**：2026-11-16 参考部署**从空卷**起栈（`DEL-535`，MegaCube GB10，`origin/main c0172a22`），`POST /v1/nexus/jobs` → `202` 后 `queued → running → succeeded`，时间线 202 `03:29:40.940964Z` / queued `03:29:40.963519Z` / running `03:29:45.089984Z` / succeeded `03:29:46.639292Z`（`job-6d2c1d0fd54e4cf8b169467cf6db3d6a`，`cleanup_state=complete`，`catalog_revision=2`）。缺前置三条抽验（`#572` AC2）在**从零栈**上复现且点名缺项：空 Job DSN → `503 job_unavailable` 列出两个键名；缺迁移 → 启动门退出码 1 并点名「跑 migration profile」；无 ready 实例 → `429 job_capacity_exhausted` 且未写 Job 行 | **有（窄口径）** | 创建/读取/派发/终态达标；**取消围栏、重试、SSE 事件序号续读未在真实部署上覆盖** | 本行为初个 A 档；取消与事件流仍只有隔离 PG 证据（`tests/integration/test_public_job_event_stream_tcp.py` 等）。取消后算力归还已修（`#888`，`54507e0b`）：多租约逐条隔离、任何租约未落地不推进终态、`/readyz` 与 diagnostics 的 `cancellation_debt` 探针可见积压。**仍未收口的一类**：执行已 `succeeded` 而后到的取消会让 Job 停在 `cancelling`、租约停在 `reserved`（容量不归零，需操作员出口，见 `docs/plans/2026-11-16-succeeded-then-cancelled.md` 的方案 B）；参考部署仍无取消对账服务（`enabled` 默认 `False`）。两处都要进发布说明 |
| 3 | **F-ASYNC** 派发→执行→结果发布→终态 | 真实工作进程消费持久 Job 并走真实引擎 | **A**：`DEL-535` 同一部署里 `job-dispatch` profile 的**真实 worker** 领取持久 Job 并经真实网关调用真实引擎：worker 批次 `claimed=1 succeeded=1 failed=0 unknown=0 lease_lost=0`，网关 `POST /internal/v1/executions/submit 200`、`staging-result 200`，引擎 `del109-asr`（vLLM 0.28.0）日志 `2026-11-16T03:29:45.194438823Z … "POST /v1/audio/transcriptions HTTP/1.1" 200 OK`；submit intent `execution_state=succeeded`、`engine_request_id=file-command-461b308a7e7949d7848eb891ca9750b8` | **有** | **达标** | 真实规划器、真实 worker、真实引擎、真实终态写入者全链在场；未覆盖：派发途中断言（worker 崩溃/租约丢失）与引擎超时，那些由隔离用例覆盖 |
| 4 | **F-ARTIFACT** 发布原子性、下载、保留 | staging 不可公开、固定对象版本、真实对象存储下载、保留/过期可复核 | **A（窄口径，发布与下载）**：`DEL-535` 里 Job 产出的制品 `artifact_id=artifact-5b93ea73e08c4740d1445673ea45dbbf3a9c062636d3f0c5`（`kind=transcript`，`bytes=92`，`sha256=sha256:3002c23c…e21d5d`，receipt `receipt-8040ae7c649fc2d78e5dd368f4637386e1cbbf475929fc82`，`expires_at=2026-12-16T03:29:46.173481Z`）；`GET …/content` 返回 `200`、`content-length: 92`、`ETag` 与 `sha256` 逐字一致，落盘字节独立重算摘要相符 | **有（窄口径）** | 发布/下载达标；**保留期与过期链路未在参考部署上运行** | `artifact-retention` profile 与 30 天默认值仍未在真实部署观测（`#572` 未覆盖）；另有一处未闭合观察：`content-type: application/octet-stream` 与元数据 `media_type=application/json` 不一致（`DEL-535` §4 其他观察） |
| 5 | **F-CLIENT** SDK 与工作台同版消费 | 契约 → 生成客户端 → 真实消费者三段同步；前端产物、契约包与 API 的来源提交一致 | **B + C（2026-11-15 #829 实测）**：`web/*/package.json` 已以 `workspace:*` 显式依赖 `@speechnexus/contracts` 并经生成的校验客户端消费 `/v1/workbench/*`（#822）。**同版镜像与三方同源已验**：`speechnexus/web-workbench:5cd5a5d0`（image ID `4ac8e05c2567`，label `org.speechnexus.source.commit=5cd5a5d0c86a91f73fca6f7cf797e38097b41782`、`dirty=0`）；`--no-cache` 构建与缓存构建的 `web-dist` 树摘要相同（`80ada7a38a4e08dce8c3009cd196482e26d9854b8930c7e44ef9136f2b7d7f9d`，24 文件，宿主 `pnpm build` 同摘要）；契约包 `packages/contracts` tree 摘要 `4d9e968303e8ee77a8f76bc6e954b247417eea2492bd5e3f56d0c7e003892935`（TS 包 `@speechnexus/contracts` 0.1.0、Python 包 `speechnexus-contracts` 0.1.0.dev0）；API 用同一提交构建的 `speechnexus/workbench-api:5cd5a5d0`（image ID `d1e50518136e…`），`workbench-migrate` 报 `revision 11`。**浏览器消费未通过**：真实 Chromium 打开该镜像页面得到空文档（`body_text_length: 0`），pageerror 为 CSP 拒 `unsafe-eval`（`deploy/web/nginx.conf` 的 `script-src 'self'` × `packages/contracts/src/profile-runtime.mjs` 的 `ajv.compile`）；同一 bundle 去掉该响应头后完整渲染，并经前端代理读到真实 `GET /v1/workbench/projects` `200`（`content-length: 389`）、`GET /v1/workbench/projects/{id}` `200`（`320`）、`…/transcripts` `200`（`69`）。`admin` 镜像同样白屏。**工作台读 #572 同一 Job 的同一 Artifact 未验**（见 §4 B1）。 | **A（`#842` 修复后在默认部署上取得）** | **达标** | **A 档（`DEL-534`，2026-11-16，MegaCube GB10）**：真实参考部署、`bypassCSP=false` 的真实 Chromium 下，workbench 渲染标题 `SpeechNexus 语音工作台`、`文件转写` 按钮存在、**`pageErrors=[]`、`consoleErrors=[]`**；界面路径「文件转写 → 读取原状态 → 任务状态 succeeded → 原始转写制品 → 下载原始制品」，网络层 `GET /v1/nexus/jobs/…` `200`（`1115`）、`GET /v1/nexus/artifacts/…` `200`（`509`）、`GET …/content` `200`（`92`，`etag` 与 Job 记录一致）；**浏览器落盘的 92 字节独立重算 `sha256=3002c23c…21d5d` 与 Job 记录逐项一致**。产物来自同轮真实 GPU ASR Job（`job-77c94daf…` `succeeded`，租约 `released`、`held_slots=0`，实例由产品 `engine-register` 注册、未手写数据）。**CSP 未为开页面放宽**：运行容器内 4 处 `script-src 'self'`、0 处 `unsafe-eval`，`runtime.mjs` 只导入生成校验器——`#842` 的修法是生成期 ajv standalone 预编译，不是放开策略。反例 ②：上游不可达时前端**不是静默空列表**，弹 `role=alert`「读取未完成。已有草稿仍保留，请检查连接后重新读取。」，网络层 `502` 且 nginx 点名 upstream。反例 ①：用**真实旧提交源码**构建的镜像（`index-Fm6cc_n5.js` vs 本轮 `index-CPo5Ns--.js`）读本轮制品，**仍然** `MISMATCH_NOT_REPORTED`（静默读到）——已判入发布说明的已知限制。**本行仍未覆盖**：Python 镜像无 source label（同源依据是 690 个 `.py` 逐文件 sha256 相同）。**构建方式已收口（`#829` AC4，2026-11-16）**：`deploy/README.md` 给出常规网络与受限网络的判据（冷拉 base 镜像耗时：≤30 s 走标准路径、30 s–5 min 加缓存与窗口、>5 min 或单层停滞即受限），受限路径由产品 `deploy/images/web.Dockerfile` 自身的 `NODE_BASE_IMAGE`/`NGINX_BASE_IMAGE`/`NPM_REGISTRY` 三个 build arg 承载（默认值＝现值），`deploy/compose.build-mirror.override.yaml` 只传这三个参数；**同一台 macOS arm64 主机上三次 `--no-cache` 构建产出同一 24 文件 `web-dist` 树** `5310e398a263e48effdce29887291dc2664f41c8db9bc45ee7198a020b2a3088`（参数化前的文件 107.31 s / 参数化后不传参数 95.13 s / 参数化后直接 `docker build` 指向 daocloud + `registry.npmmirror.com` 42.52 s / 按文档的 Compose override 命令 `--no-cache` 44.48 s，均 exit 0）。默认路径不改由三条可机械核对的边界钉住：build arg 默认值仍是 Docker Hub 引用、`deploy/compose.yaml` 不传这三个参数、示例 override 只加参数且不在 Compose 自动加载名（`docker compose --profile web config` 与改前逐字节相同）。**未复核的是「受限吞吐下的真实构建」**：MegaCube 那次用的是未入库的文件，本轮没有在受吞吐限制的主机上复跑；见 §8
| 6 | **F-TTS** 同步合成入口 | 真实合成 + 受控输出 + 与异步链共存 | **A（窄口径）**：2026-11-03 第六窗口，MegaCube（Linux aarch64 + GB10），标准 SDK → 公开 HTTP → 真实网关 → 受控 CustomVoice，`200` / `96044 bytes` / 2 s PCM16 24 kHz mono，请求/命令/结果/停止关联齐全（`docs/archive/audits/2026-11-03-controlled-tts-prepare.md:252`、`docs/archive/audits/evidence/2026-11-03-tts-window-6.json`）。范围限固定声音、单节点、单实例、短英文；窗口早于当前 head | **有（窄口径）** | 入口可用；**参考部署装配已补齐（#821），本部署未复跑真实 200** | 装配缺口已在 #821 补齐：`deploy/private/speech-recipe.example.json`（schema 与 `tts_admission_v1` 一致、摘要可校验、无凭据）、`deploy/compose.yaml` 固定 `SPEECHNEXUS_SPEECH_RECIPE_FILE` 与 `SPEECHNEXUS_SPEECH_DEFAULT_LANGUAGE` 并空值透传 `SPEECHNEXUS_SPEECH_SETTLEMENT_DATABASE_URL`、runbook §6 两条路径预置 `sn_speech_settlement` 与 `sn_api` 的 `speech_synthesis_reader_grants`，另加装配检查 `scripts/dev-env/check-tts-assembly.py`（四态逐字段，不判受理）与配方样例的加载守卫。**仍未覆盖**：TTS 实例注册、注册值与 plan 逐项相等、真实引擎与一次公开 `200`（声音资格入口已由 `#878` 补齐：运维自证路径默认关闭、需显式声明，读面区分两种来源）——本机（macOS，无 GPU）无法复跑那次窗口，逐条前置与来源见 §4 B4 |
| 7 | **F-ASR** 同步转写入口 | 真实引擎 + 源时钟 + 公开调用同版 | **A（窄口径）**：2026-11-02 21:34，MegaCube，`POST /v1/audio/transcriptions` → 真实网关 → 受控 Qwen3-ASR generation 3 → 版本化 MinIO，`200` / 1.237 s，文本与 5.36 s 合成句一致，DB 原生终态 `succeeded` 与租约释放齐全（PR #317 正文）。证据体存 PR 正文与 issue #243，未随仓库提交；窗口早于当前 head | **有（窄口径）** | 入口可用，参考部署未复现 | `docs/operations/compose-minimal.md:20` 把本部署未验证的步骤限定为「一次真实的公开 `POST /v1/audio/transcriptions` 返回 `200`」；终态写入者已存在（`execution-gateway/src/speechnexus_gateway/infrastructure/file_execution_worker.py:380` 调用 `ExecutionCompletionService.complete`，`execution-gateway/src/speechnexus_gateway/application/execution_completion.py:55` 写 `record_stop(SUCCEEDED)`），`deploy/README.md` 按「已装配、未在本部署实测」陈述同一事实；2026-10-28 的 `504` 记录早于 DEL-115（PR #284） |
| 8 | **F-SHARED** workspace 作为分组而非身份边界 | 默认 workspace、跨模块引用一致性 | **C**：同步入口允许省略 `nexus` 用默认 workspace、文件 ASR 核对对象与执行身份 workspace 均为源码级事实；无真实部署证据 | **无** | 未达标 | 无 A 档；不构成阻塞主因，随 #572 一并取证 |
| 9 | **F-DELETE** 预检/确认/持久账本 | 固定版本预检 + 明确范围确认 + 幂等账本 + 真实执行路径 | **A（工作台单元，窄口径）/ D（语音侧）**：2026-11-15 在参考部署 Compose 项目 `sn832`（真实 `postgres:16.15-alpine`、`workbench-migrate`、`workbench-api`，起栈命令见 `docs/operations/compose-minimal.md` §6）用真实 HTTP 跑通 `POST /v1/workbench/deletion-plans` → `200`、以 `plan_id`/`plan_revision`/`plan_digest` 固定的 `POST /v1/workbench/deletions` → `202`、`GET /v1/workbench/deletions/{id}` → `succeeded`/`complete`，`workbench.deletion_ledger` 落一行且 `workbench.projects` 归零。**含内容的级联删除另有一次实测**：同名栈里给项目挂一条转写与两个 revision（正文非空），`mode=cascade` 的计划把转写列为 `disposition: cascade` 依赖，确认后 `deleted` 为 `[["workbench","project",…,"1"],["workbench","transcript","tr-sn832","2"]]`、`resource_tombstones` 两行都带 revision、`deletion_outbox` 的 `completed_at`/`objects_erased_at` 齐全，`transcripts` 与 `transcript_revisions` 行数与残留正文分别为 0——擦除的是内容，不只是元数据行。错 `plan_revision` 与错 `plan_digest` 均返回 `409 deletion_plan_changed`，同一 plan 用固定值重提仍 `202`；`Host`/`Origin` 违规返回 `400 invalid_request`。实现为 `backend/src/speechnexus_backend/workbench/application/deletions.py`、`backend/src/speechnexus_backend/workbench/infrastructure/postgres_deletions.py`，另有真实 PG + 真实 Chromium 用例 `tests/integration/test_workbench_real_runtime_browser.py`。**语音侧删除恒 503**（`backend/src/speechnexus_backend/speech/interfaces/http/unimplemented.py:72`），**不在首发范围**；资产到期清理进程 `speechnexus-asset-maintain` 不在参考部署 | **有（工作台单元窄口径；语音侧无）** | 工作台侧达标；语音侧不在首发范围 | ① 语音侧无实现，按 #819 的实施范围另案处理；② 工作台单元部署入口已由 #832 补齐（此前 `WEB_WORKBENCH_UPSTREAM` 指向不存在的主机）；③ #540 仍 open（`speech_job_intents` 未纳入删除面）；④ 该部署单元不配置对象区，含已保存资产的项目删除会停在 `cleanup_state=pending`（`ObjectZoneUnavailableError`，行已擦除、对象字节待结清，见 `tests/integration/test_workbench_recovery_postgres.py::test_an_object_zone_failure_keeps_the_deletion_owed`），配齐对象区四键后才发布 `complete` |
| 10 | **Gate：#572** 参考部署跑通一次异步 Job 端到端 | 完整 `up` + 迁移 + 桶 + 引擎注册 + worker 闸门 + 三个 HTTP 调用 | **A（链路）/ 未达标（照文档）**：2026-11-16 `DEL-535` 在 MegaCube GB10 上**从空卷**照 `compose-minimal.md` §0–§7 与 `async-job-runbook.md` §7.1–§7.3 起栈，真实 GPU 引擎上跑通 `202 → queued → running → succeeded`，制品下载与 `sha256` 一致，租约 `released`、`held_slots=0`、配额 `held_attempts=0`，收尾 label filter 三项为空。**但需 1 处阻断级样例修正才成功**：`deploy/private/gateway-file-asr.example.json` 的输入桶与 `SPEECHNEXUS_OBJECT_STORAGE_BUCKET` 不一致 → 首个 Job 在调引擎前被隔离（`execution_input_scope_mismatch`）。另暴露 9 条文档/样例偏差 | **有（链路）** | 未达标：差「修正后照文档零变更重跑一次」，**且新增一处前置**（`#897`：`172.28.0.0/24` 被占的主机上必须改仓库文件才能起栈） | 链路证据已 A 档。9 条偏差已全部修进主干（`#893`，`7fac20e4`：样例桶名两条 lane 都改、按 `compose.env.example` 取期望值的守卫、DEL-535 记录），零修正复验由 `DEL-536` 在 GPU 窗口执行。**`#897` 是这一行新增的阻断项**：子网与 MinIO 静态地址写死在 `deploy/compose.yaml`，无 env/override 入口，`DEL-535` 当时只能自己换网段——那就不是「照文档」。原始偏差清单见 `DEL-535` 回传 |
| 11 | **Gate：#529** 声音泳道真实引擎/GPU 验收 | design 与 clone 各一条 Job 到 `succeeded`，preview artifact 可读成 candidate；冷加载/显存/P95 实测 | **A（仅引擎接受面）/ 无（Job 级）**：网关 VoiceDesign stage、Base 克隆泳道与节点模板已合并（PR #543、#545，`execution-gateway/src/speechnexus_gateway/file_speech_runtime.py:89`）；真实引擎接受面在 MegaCube DEL-521/DEL-522 实测（数字落在 `deploy/engines/file-voice-design/README.md`）。但两条泳道**从未经网关产生过一条 `succeeded` Job**；`deploy/engines/file-voice-design`、`file-voice-clone` 只有 `template.example.json`，无 `artifact-pin.json`/`runtime-pin.json`，`start_period` 无实测值 | **无（Job 级）** | 未达标 | #529 仍 open；缺模型制品 pin、GPU 窗口与一次泳道端到端 |
| 12 | **部署入口**（Epic #342）「他人可照着起一套」 | 从零起库起服务、健康检查、跑一次 Job 的完整可执行步骤 | **C**：`deploy/compose.yaml` 有 6 个显式 profile（7 处服务声明），`docs/operations/compose-minimal.md`（1983 行）与 `async-job-runbook.md`（965 行）逐步可读；`scripts/dev-env/reference-acceptance.py` 有 `--dry-run` 并在 CI 真实执行；CI 另有 `docker compose config --quiet`。但 CI **从不启动栈**（`.github/workflows/ci.yml` 的 `compose` 作业只做 config 校验，`reference_acceptance` 只跑 `--dry-run`），验收入口自述「不证明：真实引擎与已注册 ready 实例、真实 ASR/TTS 业务成功…端到端异步 Job」（`docs/operations/reference-acceptance.md:26`） | **无** | 未达标 | ① 无一次「照着文档从零起一套并跑通」的记录；② LICENSE 已补齐（MIT，`LICENSE`，2026-11-16），`pyproject.toml`×7 与 `package.json`×5 均声明 `MIT`；**wheels 不内嵌 `LICENSE`** 这一条仍是已知限制；③ 示例私有配方此前原样加载会失败（转写样例的 `binding_publication_id` 与 `binding_revision_id` 不一致，见 `transcription_recipe.py:218`）；#821 已修正该值并新增 `tts_admission_v1` 样例，两样例现由 `tests/unit/backend/test_private_recipe_examples.py` 通过生产 loader 校验；④ 独立 Workbench API 的部署入口已由 #832 补齐并有真实起栈证据，但该单元只装配工作台业务后端（无语音底座/对象区/LLM 配置），且「照着文档从零起一套」仍未完成；⑤ `#851` 起栈时补齐了 `SPEECHNEXUS_ASSET_ALLOWED_ORIGINS` / `_HOSTS` 两个必配的浏览器边界输入并加守卫（键名从 Settings 模型派生，删任一键即红），但这是**把「照文档起一套」的一个静默失败点消掉**，不改本行「无一次从零起一套并跑通 Job 的记录」这一判定；⑥ **构建入口**此前在受限网络下无可用方式（`#829` AC4：当时取证用的 mirror 文件不在仓库里）：本轮已把判据写进 [deploy/README.md](../../deploy/README.md)，并把受限网络构建固化进仓库（产品 Dockerfile 的 base 镜像与 npm 源参数化，示例 override 只传参数；默认值＝现值，`compose config` 与改前逐字节相同，同一 `web-dist` 摘要）；这只消掉「构建方式不可重复」这一项，本行「无一次从零起一套并跑通」的判定不变 |

## §3 A 档为 0 的行（MVP 硬阻塞集合）

1. **F-JOB** 持久 Job 生命周期
2. **F-ASYNC** 异步执行闭环
3. **F-ARTIFACT** 制品发布与下载
4. **F-SHARED** workspace 分组语义
5. **F-DELETE** 删除语义
6. **#572** 参考部署异步 Job 端到端（已跑通一次真实端到端，缺「从空库按文档」与「claim 前点名失败」）
7. **#529** 声音泳道（Job 级；引擎接受面另有 A 档）
8. **部署入口**「他人可照着起一套」

**A 档覆盖已扩到异步主链**：`DEL-535`（2026-11-16，MegaCube GB10，从空卷起栈）在真实 GPU 引擎上跑通完整 Job，F-JOB（窄口径：创建/读取/派发/终态）、F-ASYNC、F-ARTIFACT（窄口径：发布/下载）三条取得 A 档。同步入口一侧，有 A 档的只有 F-TTS、F-ASR 两条（各为单次受控窗口、范围受限；两条入口在参考部署里的装配此前缺失，TTS 一侧已在 #821 补齐但未复跑真实 `200`），以及 **F-ASSET（`#867` 三类用途在参考部署上跑通、`#851` 补上浏览器 Origin 输入）** 与 **F-CLIENT（`DEL-534` 默认部署下同版工作台读到真实制品）**。

## §4 硬阻塞清单

**B1 — 参考部署跑不通一次异步 Job（#572）**
- 要什么证据：一次可在文档步骤内复现的 `up` + 迁移 + `minio-init` + `engine-register` + `job-dispatch-worker` 启动闸门 `OK` + `POST /v1/nexus/assets` → `POST /v1/nexus/jobs` → `queued→running→succeeded` → artifact `GET` 可读回，全程记录主机、镜像 digest、提交与时间。
- 现在卡在哪：六项前置（writer/admission DSN、迁移 head、派发角色与五组 grant、真实引擎注册、result 桶、网络与生命周期）只被静态自检覆盖；`POST /v1/nexus/jobs`、Job 轮询与结果读回三条命令从未执行（`docs/operations/async-job-runbook.md:705`）。
- 谁做：后端（Job/派发）+ 运维（Compose/角色/桶）在同一窗口内配合；窗口由具备 GPU 的部署方提供。
- **2026-11-15 增补**：ASR 路径的实测证据已转录进 [#572](https://github.com/hrygo/SpeechNexus/issues/572) 的评论（任务系统 `DEL-531`，MegaCube）：`job-410574cd…` `queued→running→succeeded`、`asset-416749…`（195356 bytes / 4429 ms / 22050 Hz / 单声道）、`artifact-ea4fe1b4…`（`content-length: 92`，`etag "sha256:3002c23c5e859f295f3bec557a363a7fe5359a3db50b351bfffc9885e5e21d5d"`，`sha256(file)` 与该 etag 摘要相同）、`lease-410574cd… released`。它证明「API → 派发 → 真实 ASR 引擎 → Artifact 下载」这条链可用。
  **仍未验的是消费侧同版读取**：该次运行的栈已 `down -v`，artifact 行与对象均不存在，本机（macOS arm64，无引擎）无法重放同一份 artifact，因此不得用注入、改行或 mock 代替。`#829` 在消费侧只走到「同版镜像 + 三方同源 + 读写工作台自身业务后端」；浏览器一层另被 B7 阻断。

- **2026-11-16 增补（#852，同一次参考部署窗口实测）：取消受理后可以永久占住实例槽位。**
  窗口里观察到一次公开取消（`POST /v1/nexus/jobs/{id}/cancel` → `202 cancelling`）之后的稳定状态：
  `speech_jobs.state=cancelling`、`admission_leases.lease_state=reserved`、
  `submit_intents.execution_state=unknown_quarantined`、`engine_instances.held_slots=1`，
  此后无人推进。用真实 `JobCancellationReconciler` + `GatewayCancellationConfirmer` +
  `PostgresCancellationReconciliationStore` 在隔离栈复现了同一条链：受理后每轮向网关提问
  得到 `pending`，`max_attempts` 用尽即写 `exhausted`，而领取条件排除 `exhausted`，于是
  该行永不再被领取；网关侧不改写 `unknown_quarantined`，租约到期只是过期而不是回归容量，
  派发器又早已不在该 Job 上，`cancelling` 因此无主。参考部署 `deploy/compose.yaml` 里没有
  取消对账服务、该 worker 也默认关闭，所以真实窗口连推进者都不存在。
  现已收口（#852）：确认窗口（默认 300 秒）或尝试预算任一到达即做一次终态判定，且判定
  以「全部租约都已落地」为条件——逐条按 `cancellation_unconfirmed` 隔离，每条租约把自己的
  `reserved_slots` 从 held 桶移到 quarantined 桶（`held_attempts` 只在同一 attempt 首次
  隔离时移动一次），全部落地后 Job 才落 `failed/cancellation_unconfirmed`，重试按
  `409 execution_cleanup_pending` 拒绝（`retryable` 为假、不带 `Retry-After`）。任一租约
  被拒时 Job 保持 `cancelling`、账本保持可领取，`/readyz` 与
  `GET /v1/nexus/admin/diagnostics` 的 `cancellation_debt` 检查以 detail 报出
  「Job → 租约 → 两侧状态」，worker 每轮向 stderr 输出一条明细，处置走网关
  abort/drain（见 [production-jobs.md](../components/production-jobs.md#隔离不了的时候可见面与处置)）。
  仍未覆盖：参考部署未运行该 worker（`deploy/compose.yaml` 无该服务，`ENABLED` 默认 `false`），
  因此「默认部署里取消能自愈」没有部署级证据；执行仍活跃或已 `succeeded` 的租约在没有停止
  证据时仍保持 `reserved`，需 #338 或操作员处置；本切片不新增告警规则与自动扫描（#528）。

**B2 — 声音泳道无 Job 级证据（#529）**
- 要什么证据：design 与 clone 各一条 Job 到 `succeeded`，preview artifact 被 `GET /voice-drafts/{id}` 读成 candidate；附冷加载、空闲回收、显存峰值、P95、与 asr/tts 混跑不干扰的实测数字，并给出 `start_period` 实测值。
- 现在卡在哪：网关 stage 与节点模板已合并，但模型侧 `artifact-pin.json`/`runtime-pin.json` 与 `start_period` 未固定，泳道从未接真实引擎生成过 Job。
- 谁做：网关/节点实施者 + 持有 MegaCube 或等价 GPU 主机的执行方。

**B3 — 语音侧删除未实现（F-DELETE）**
- 要什么证据：`/v1/nexus/deletion-plans` 与 `/v1/nexus/deletions` 的真实预检（固定 version/digest）、范围确认、`Deletion` 资源 + ledger + tombstone + outbox 幂等恢复，以及删除后重启重放；不得以资产 TTL 清理代替。
- 现在卡在哪：两个操作仍是中央占位（恒 `503 operation_not_implemented`）；工作台的删除只覆盖工作台自有内容，且其依赖的 Workbench API 不在参考部署内。
- 谁做：后端（Speech 删除面）+ 需先决定删除是否纳入 MVP 首发（见 §5）。

**B4 — 参考部署已装配公开同步 TTS，真实 200 未复跑**
- 要什么证据：从仓库可得的同步 TTS 配方样例 + `SPEECHNEXUS_SPEECH_RECIPE_FILE`/settlement 角色在 Compose 中的装配路径，并一次真实 HTTP 200。
- 装配部分（#821 已交付）：`deploy/private/speech-recipe.example.json` 与 `tts_admission_v1` schema 一致、RFC 8785 摘要可由 runbook §3 的脚本重算、不含凭据与私有路径；`deploy/compose.yaml` 固定配方路径、默认语言、空值透传结算 DSN，并新增 fail-closed 的 `SPEECHNEXUS_SPEECH_ENABLED`（`Literal["true","false"]`，未设置即开、拼错启动失败）；runbook §3/§6 与升级路径重放 `sn_speech_settlement`（`disable_settlement_grants` + `tts_settlement_worker_grants`）与 `sn_api` 的 `speech_synthesis_reader_grants`；`scripts/dev-env/check-tts-assembly.py` 让三份私有文件分别过各自的生产 loader，逐字段输出「仍是样例值 / 不一致 / 本地检查通过 / 无法从文件判定」并单列只由 loader 校验的叶子，样例值或文件间不一致即 exit 1，**不给出「ready」判词且自述 exit 0 不代表可受理**；`tests/unit/backend/test_private_recipe_examples.py` 让两份样例漂移、部分替换、以及 gateway loader 拒绝的 `task`/`sample_rate_hz`/`speed` 在 CI 变红。
- 装配之外还差的受理前置（文件级替换不足）：① 结算 DSN 须非空，否则 `build_speech` 只告警点名缺失键并返回空装配（`speech_runtime.py:49-66`），三份文件再完美也是 `503 backend_unavailable`；② TTS 实例须注册进 `runtime_instance_heads`（`qualified_speech_facts_postgres.py:76`），而本 Compose 的 `engine-register` 只吃转写 recipe（`engine_registration.py:1136`），TTS 只能手动模式注册（`_spec_from_manual`）；③ 注册值与配方 plan 须逐项相等（`transcription_admission.py:88-123`），注册期还要求 plan 预算装得下宿主机容量（`_require_capacity`）；④ `policy_revision_id` 须同时等于工作区配额行的 `policy_revision`（`transcription_admission.py:116`）与内建声音资格的 `gateway_policy_revision_id`（`backend/src/speechnexus_backend/speech/infrastructure/builtin_voice_qualification.py:54`）；⑤ 该 binding 下须有已发布且未退役的内建声音（`has_qualified_voice`）。逐条来源与退出方式见 [deploy/README.md](../../deploy/README.md) 的「Synchronous TTS assembly」。
- 仍然缺的那一步：在真实引擎主机上以本装配跑一次公开 `POST /v1/audio/speech` 的 `200`（记录镜像 digest、提交、时间、体积/格式/采样率）。本机 macOS 无 GPU/引擎，无法提供。
- **2026-11-16 实测结论（`DEL-533`，MegaCube GB10，参考部署按文档装配）：`200` 未取得，且卡点不是装配而是产品缺口。**
  两次实测均 `503 backend_unavailable`（`content-length: 200`，无音频产物），API 日志「`Synchronous speech unavailable; no current qualified voice`」。装配层已通（结算 DSN 已配、启动不再点名缺键、`check-tts-assembly.py` `EXIT=0`，其中「仍是样例值 0 项 / 不一致 0 项」）；事实层部分通（binding 已发布 `mbrev-c2423c6a…`、TTS 实例 `ready`、来源批准在册，全部经产品入口创建，未手写 `engine_instances`/`leases`）。
  **断点在「资格入口」**：声音资格与探针授权**强制要求** node-controller 产出的 `native_runtime_evidence`——
  `qualification_native_observation.py` 的 `require_native_ready` 在 `native is None` 时抛 `ValueError("native_runtime_evidence_unavailable")`；
  而手动注册（`engine_registration.py:905`）把 delivery 写成 `blocked / operator_attested`，**结构上永远不产生该证据**；
  参考部署又按设计不含 node-controller（`deploy/compose.yaml:9`「engine lifecycle is operator/external in this bootstrap」）。
  **因此「外部托管、我们无权重启」的引擎在本参考部署里永远拿不到声音资格。** 2026-11-03 那次 `200` 是受控窗口里 controller 托管引擎跑出来的——能力存在，**本部署复现不了**。
- **该缺口已由用户裁决并实现（`#878`，2026-11-16 合入）：为外部托管引擎开放一条受控资格路径。** 部署方必须显式声明信任（默认关闭，未声明即 `exit 2` 点名变量），声明与写入逐项核对实例/生成/fencing/镜像/模板/制品/策略摘要，落库为 `operator_attested` 并写 provenance；读面把两种来源分开（`speechnexus-qualification-provenance`），准入要求正向证据（控制器探针授权或窗口内声明），无证据的资格行被拒并点名修复方式。**仍未取得的仍是同一步：一次真实 `200`**，见 [同步 TTS 真实 200 收口](#) 的验收要求，以及 [operator-attested-voice-qualification.md](../design/operator-attested-voice-qualification.md)。
- 谁提供什么：持有 GPU 引擎窗口的执行方提供（a）引擎地址与受控 `vllm_api_key_v1` 端点凭据（64 位十六进制）、（b）已发布且对该实例合格的内建声音版本（`voice_id`/`revision`/`revision_id`/native speaker）、（c）该实例的 binding revision、manifest 与运行时 plan 的权威取值、（d）引擎侧允许一次短英文合成的确认。部署/后端据此替换样例并复跑。

**B5 — 制品保留期代码与入口就绪，缺参考部署实测（F-ARTIFACT，#820）**
- 要什么证据：发布时写入 `expires_at_us`（PRD：普通生成音频默认 30 天）、过期后下载返回明确不可用，并有复核用的固定版本与持久清理账本。
- 当前实现：发布按 `SPEECHNEXUS_ARTIFACT_RETENTION_SECONDS`（默认 30 天，1 小时至 10 年）写 `expires_at_us = created_at_us + 窗口`；下载在记录期限处返回 `410 artifact_expired`，清理确认后返回 `410 artifact_deleted`，已铸造未提交身份返回 `404 artifact_not_published`，未签发标识返回 `404 artifact_not_found`；有界清理入口 `speechnexus-artifact-retention` 分「标记过期 → 确认移除版本 → 标记已删除」两段，使用独立身份 `sn_artifact_retention`，只允许 `SELECT` 与 `UPDATE (artifact_state, deleted_at_us)`，确认移除时在同一语句写入 `deleted_at_us` 作为持久删除账本，每次决策另输出 JSON 日志。参考部署以 `artifact-retention` profile 提供该进程，runbook 含角色与授权步骤。
- 还差什么：只在隔离 PG + 版本化 MinIO 上跑过（`tests/integration/test_artifact_retention_pg.py`）以及进程级一次 pass（`--once`），未在真实参考部署上启动该 profile 并留存日志，因此不构成 A 档；「照文档从零起一套」仍属 MVP 未覆盖范围。
- 谁做：运维（一次真实部署运行与日志留证）。

**B6 — 独立 Workbench API 部署入口（已解决，#832）**
- 结论：纳入 MVP，部署单元已给出。参考部署新增 `workbench-api` profile：`workbench-migrate` 一次性执行/核对工作台命名空间与两份授权，`workbench-api` 以 `deploy/workbench-startup-gate.py` 为入口起真实的 ASGI 工厂；`WEB_WORKBENCH_UPSTREAM` 现在指向该 profile 装配的服务，前端代理到它的请求由真实服务回答。
- 证据：2026-11-15 在 Compose 项目 `sn832` 起真实栈，`docker compose --profile workbench-api run --rm workbench-migrate` 输出三行 `OK:`；`workbench-api` 健康检查（`GET /v1/workbench/status`）`200`；删除的预检→确认→账本端到端见 §2 第 9 行。身份最小化由启动闸门强制：运行身份在工作台命名空间之外持有任何关系或序列权限即拒绝启动，指向 `sn_api` 这类共用宽身份因此起不来；清理身份另需独立。
- 仍未覆盖：该单元不配置语音底座、对象区与 LLM，工作台能力因此停在 `teleprompter=available`、其余 `disabled`，含已保存资产的删除停在 `cleanup_state=pending` 等对象区；前端 bundle 的镜像构建在本轮环境里因既有的 `tests/fixtures/contracts/*.json` 未被 `deploy/images/web.Dockerfile` 复制而失败（缺陷另案 #834 处理，与本卡无关），因此「浏览器经前端代理→工作台 API」只有代理层证据（用仓库 `deploy/web/nginx.conf` 起 nginx，指向真实服务为 `200`、指向不存在主机为 `502`），没有完整浏览器链路证据。

**B7 — Web 镜像在自身 CSP 下白屏（#840，2026-11-15 实测）**
- 事实：`deploy/web/nginx.conf` 发送 `script-src 'self'`（无 `'unsafe-eval'`），而生成契约校验器由 `packages/contracts/src/profile-runtime.mjs` 的 `ajv.compile()` 在首个渲染路径上编译（内部 `new Function`）。真实 Chromium 打开 `speechnexus/web-workbench:5cd5a5d0` 与 `speechnexus/web-admin:5cd5a5d0` 均为空文档，pageerror 为 `EvalError: … 'unsafe-eval' is not an allowed source of script: script-src 'self'`。
- 隔离对照：把同一镜像的 `/usr/share/nginx/html` 原样导出、用不带 CSP 的静态服务提供，页面完整渲染（正文 734 字符、标题可见、0 pageerror）。因此阻断点是响应头，不是 bundle 与后端。
- 为什么没被发现：两个浏览器门禁跑 `vite preview`（`tests/web/preview-servers.mjs`），不带任何 CSP；`#832` 的代理证据只 `curl` 不开浏览器；仓库内没有「在镜像里开过浏览器」的记录。
- 影响：任何人按文档起 `web` profile 都得到白屏 UI，而 `/healthz` 仍 `200`，健康检查不报。属部署入口级缺陷，修法与门禁见 #840（构建期 ajv standalone 预编译，不改 CSP 语义）。

## §5 发布前必须补的最小动作清单（按可执行顺序）

1. 固定本次发布的候选提交与镜像 digest，把「A 档证据必须落在哪个提交上」写清（否则任何窗口都不能算作本次发布的证据）。
2. 申请一次真实引擎窗口，在同一窗口内按 B1 的步骤执行并留存原始输出；任一步失败先修，不换环境重跑掩盖。
3. 在同一窗口顺带执行 B3 的语音侧删除路径（若决定纳入 MVP）与 B2 的泳道 Job，避免二次排队。
4. 补 B4（同步 TTS 装配样例与 settlement 角色）；B5 的发布写入与清理入口已实现，仍需一次参考部署运行留证——两者都可在无 GPU 窗口时完成。
5. ~~补 B6 的决定与最小部署入口~~：已完成（#832）。剩余的是该单元之外的下游输入（语音底座地址、对象区、内网 LLM），属于部署方提供项，不在首发阻塞链上。
6. 修 #840：Web 镜像在自身 CSP 下白屏（`ajv.compile` 的 `unsafe-eval`），并把「真实镜像产物 + 真实 CSP 头」的首屏渲染门禁加进 CI。
7. ~~修订三处陈旧陈述~~：已完成（#823）。同步 ASR 的终态写入者、公开 Job 六个路由的装配范围与生产 planner 的装配状态都已按代码就地订正（`execution-gateway/src/speechnexus_gateway/infrastructure/file_execution_worker.py:380`、`backend/src/speechnexus_backend/speech/interfaces/http/jobs.py:802` 起、`backend/src/speechnexus_backend/job_dispatch_runtime.py:321`），矩阵按 `delivery-issues.json` 重渲染；三处均只声明装配/实现证据，未升格为真实部署验证。
8. 在参考部署上补一次「照着文档从零起一套」的记录（含 LICENSE 决定），完成后才可宣称 Epic #342 的 MVP 口径达标。
9. 发布说明写明 0055 的资格证据限制：0055 之前产生、没有 provenance 行的内建资格不再自动被准入接受（同步 TTS 与公开 Job 均拒绝），恢复须**发布新的 voice revision** 并为该 revision 走控制器探测路径或运维自证路径取得资格（同一 revision 不能补发，重复发布报 `publication_conflict`）；条目随发行版一起对外。

## §6 建议新建的实施卡（待统一建卡，本卡不创建）

| 标题 | 一句话范围 |
| --- | --- |
| 语音侧删除：预检、确认与删除账本的最小可验收实现 | 把 `/v1/nexus/deletion-plans`、`/v1/nexus/deletions` 从占位换成真实预检+账本，覆盖 `speech_job_intents`（并吸收 #540），带真实部署重启重放证据 |
| ~~制品保留期：发布写入 `expires_at_us` 并让过期下载可复核~~（#820 已实施，待参考部署留证） | 发布路径按 PRD 30 天写保留期，补过期判定与清理入口，附过期后下载反例 |
| 同步 TTS 参考部署装配：配方样例、settlement 角色与一次 200 | 让同步 TTS 能从仓库起得来，并在真实部署上取得一次 HTTP 200 |
| ~~独立 Workbench API 部署入口（或明确 MVP 不含）~~（#832 已实施） | 参考部署以 `workbench-api` profile 装配工作台后端、迁移与启动闸门，`WEB_WORKBENCH_UPSTREAM` 指向真实主机 |
| 生成契约包的消费者绑定：把 36 处相对路径导入改为声明依赖 | 让 `@speechnexus/contracts` 成为 web 的显式依赖并使用声明的 `exports`，消除「同版」只靠目录结构的隐式性 |
| 文档陈旧陈述对齐：同步 ASR 终态、公开 Job 路由、生产 planner | 三处与当前代码冲突的陈述就地订正，并加最小守护防止再漂移 |

## §7 明确不在本次 MVP 内

- **实时语音会话与双工流**（#339）：`/v1/realtime`、`createRealtimeSession` 等仍是占位（`backend/src/speechnexus_backend/speech/interfaces/http/unimplemented.py:65` 起），本表不含其验收。
- **#343 复合业务超出基础可用的部分**：项目/稿件/会议/纪要/助手/有声书/提词/字幕的真实服务链路。
- **#340 声音设计/克隆的全量能力**：本表只取 #529 的一次泳道端到端；声音发布、发音词典、退役与跨版本依赖不在 MVP 内。
- **#345/#346 治理细分**：profile 发布/激活治理、逐绑定质量与容量资格、发布目录投影的一致性；MVP 只要求能注册一个可用实例并跑通 Job。
- **物理多机拓扑与故障演练**（O-PHYSICAL）：#572 的验收对象是参考部署（单机 Compose），多机不构成 MVP 前置；但这意味着「集群同构」在 MVP 阶段只有实现一致性，没有物理证据。
- 浏览器全矩阵资格、双语可访问性矩阵、MCP 安装与工具消费（C-MCP）。

## §8 未能核实的项

| 项 | 原因 |
| --- | --- |
| 同步 ASR / TTS 两次受控窗口的证据体 | 仓库只有 TTS 第六窗口的脱敏 JSON；ASR 的原始记录在 PR #317 正文与 issue #243，属仓库外材料，只能按文字陈述采信 |
| #529 的 GPU 实测原始报告 | 数字在 issue 评论与 Paperclip 文档（`gpu-feasibility`）里，仓库内只有转录后的 README 表格，无法重算 |
| MegaCube 当前是否仍可用 | 无该主机的访问，未探测 |
| 参考部署在 Linux 上的实际可执行性 | 本机 macOS；未起栈（只读核验），未验证 Compose 在目标平台的运行行为 |
| 受限网络下的真实 Web 构建（`#829` AC4） | 本轮在 macOS arm64（非受限路由）上实测：三个 build arg 的接线、默认值等价、同产物摘要都成立，但「在被限流的链路上真的构建成功」没有复跑。`#572` 那次用的 mirror 文件与 override 当时不在仓库里，本轮按记录把等价能力做进产品 Dockerfile 的参数，未回到该主机验证 |
| 生产/多机拓扑的实际容量 | 无目标环境 |
