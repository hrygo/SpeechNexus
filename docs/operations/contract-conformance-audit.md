# 公开语音面契约一致性审计

审计对象：`contracts/speech-openapi.yaml`（审计基线版本 `0.1.0.dev4`）与
`backend/src/speechnexus_backend/speech/interfaces/http/` 的 FastAPI 实现。
审计基线：`origin/main` `62ae4805`，分支 `codex/contract-conformance-audit`。
审计时间：2026-11-06（Asia/Shanghai）。审计为只读：不改产品代码、不改契约。

**未实现操作处置**：2026-11-06，基线 `934855c3`（`codex/slice-473`，#473）。21 条未实现操作改由中央占位路由注册并返回
`503 operation_not_implemented`；门禁的未实现基线从「未注册」改为「已注册且只返回占位错误」。历史实测见 §6.1，
当前实测、表达选择与理由见 §6.2，处置见 §9 第 2 项，门禁见 §10。

**引用与结论复核**：2026-11-06 18:00（Asia/Shanghai），基线 `3951718e`（`codex/slice-469`，与当日 `origin/main` 同 commit）。
复核范围：`jobs.py` / `job_errors.py` / `app.py` 的 8 条漂移引用及其结论（§11）、受后续实现影响的计数与结论（§1、§3.1、§6.1、§9），
以及全部契约行号引用（改为操作/路径锚点）。

引用约定：

- 会随实现演进的引用写「仓库根相对路径::符号」，符号不随行号漂移；本次复核涉及的 jobs/app 引用已全部改为符号，
  其余行号引用在复核基线仍指向所述代码，保留用于精确取证。
- 契约引用写 `contracts/speech-openapi.yaml` 的 `::<operationId>` 或 `::<路径>`，不写行号。
- 契约版本以**本文件所在 commit** 的 `contracts/speech-openapi.yaml` 为准。复核基线时为 `0.1.0.dev6`
  （#466 尚未合入；合入后版本随文件变化，本文不预写未来版本）。

> 当前可重复门禁见 §10。§3–§8 保留 `62ae4805` 的审计基线事实；复核基线的差异以【复核 3951718e】标注，
> 汇总见 §3.1（新增 4 个已实现操作）、§6.1（未实现操作清单）与 §11（8 条漂移引用逐条复核）。

【#471 更新】`listJobs` 已由真实实现接替占位（§3.2）；【#490 更新】`retireVoiceRevision` 的 `retire`
已由真实实现接替占位，`disable` 明确返回 422（§3.3）。已实现与未实现操作的唯一口径是 §10 门禁脚本的
`KNOWN_UNIMPLEMENTED_OPERATIONS`：计入与移出该基线随实现切片变化，本文件不复述计数，§6.1 只指向该基线。
J1 已修复；历史表保留原时点响应。保留 #466 的契约测试和 #473 的 `0.1.0.dev8` 契约，
`listJobs` 的 405 已声明，`known_status_gaps` 保持 **0**。
#490 把 `retireVoiceRevision` 的 503 从 `OperationNotImplemented` 去占位为内联响应：保留 required 的
`Cache-Control` / `X-Should-Retry` 与含 `ErrorResponse` 的 `allOf`，把可重试语义写进 body；不新增状态码，
其余占位操作的声明未变。门禁对「基线恰好引用占位组件」的 503 有窄例外，见
[HTTP 兼容检查规则](../validation/http-contract-change-checks.md#replacing-the-central-placeholder-503)。

## 1. 结论摘要

审计基线 `62ae4805`：契约声明 39 条路径、**42 个操作**；后端注册 **17 个**，**25 个未注册**。
【复核 3951718e】`#432`（`retryJob`、`streamJobEvents`）与 `#442/#443`（`listVoices`、`getVoiceRevision`）已合入，
后端注册 **21 个**、未注册 **21 个**；契约仍为 39 条路径、42 个操作、`0.1.0.dev6`。
§3/§6 的表格保留基线的 17/25 划分，新增的 4 个操作在 §3.1 单独核算。

- 已实现的 17 个基线操作中，**4 个会返回契约未声明的状态码**：
  `getArtifact` 的 304/503、`downloadArtifactContent` 的 304/416/503、`cancelJob` 的 400、`createTranscription` 的 404。
  【复核 3951718e】仍成立；上述四处的 handler 与 `*_errors.py` 映射未变（见 §4）。
- **25 个未注册操作**中，20 个返回框架默认 `404 {"detail":"Not Found"}`（错误体不符合 `ErrorResponse` 契约），
  `listJobs` 返回 `405 job_method_not_allowed`，`retryJob`/`streamJobEvents` 返回 `404 job_not_found`，
  `listPronunciationSets`/`createPronunciationRevision` 返回 `400 pronunciation_invalid_request`。
  【复核 3951718e】该段是基线事实：`retryJob`/`streamJobEvents` 已注册，不再返回"路由不存在"的 `job_not_found`。
  复核基线时 21 个未注册操作中，18 个返回默认 404，`listJobs` 返回 405，两条集合级读音路由返回 400（§6.1）。
  【#473】已过期：21 条全部注册为占位路由并返回 `503 operation_not_implemented`，实测与理由见 §6.2。
- **401 在全表都是"声明未使用"**：公开语音处理器没有实现共享服务密钥校验（`docs/architecture/openai-compatibility.md:91`），
  源码中不存在任何 401 产生路径。
  【复核 3951718e】仍成立：`backend/src/speechnexus_backend/speech/interfaces/http/artifacts.py::create_artifact_router`
  的两处 401 仍只是 OpenAPI 响应声明；新增的 4 个操作同样只声明 401、不产生 401。
- `createSpeech`/`createTranscription`/`createJob` 声明的 403、409、429 无产生路径；`listModels`、`retrieveModel`、
  `getCapabilities`、`getJob`、`cancelJob`、资产与制品读写均未声明 405，但框架会返回 405。
  【复核 3951718e】仍成立；`createJob` 的 409 可经 `idempotency_conflict` 产生，§5.2 只把 403/429 列为无路径。
  405 缺口另见 §3.1/§5.3。

严重度最高的两条是消费者可直接触发的事故面：

1. **【基线】** `GET /v1/nexus/voices`（`listVoices`）与 `GET /v1/nexus/voices/{voice_id}/revisions/{revision}`
   未注册，而工作台生产消费者 `backend/src/speechnexus_backend/workbench/infrastructure/public_voice_catalogue.py::_VOICES_PATH`
   以该常量调用这两个路径（列表 `PublicVoiceCatalogue.list_summaries`、固定 revision `PublicVoiceCatalogue.get_revision`）；
   `PublicVoiceCatalogue._body` 把 404 映射为 `VoiceCatalogueUnavailableError`，工作台音色目录因此固定不可用。
   **【复核 3951718e】已过期**：`#442/#443` 已注册两条 GET；未配置目录返回可重试
   `503 voice_catalogue_unavailable`（运行时实测），非法请求返回 400，正常读取返回 200。
   工作台消费者仍以同一路径消费，但不再得到 404；处置见 §9.1。
2. `GET /v1/nexus/jobs`（`listJobs`）与 `POST /v1/nexus/jobs` 共用同一路径；未注册的 GET 得到 `405 job_method_not_allowed`，
   而契约给 `listJobs` 声明的是 200/400/401/503，消费者无法据状态码区分"未实现"与"方法不支持"。
   【复核 3951718e】仍成立（运行时实测 405 `job_method_not_allowed`）。
   【#473】已处置：`listJobs` 现注册为占位 GET，返回 503 `operation_not_implemented`；同一路径的 405 仍为
   Job 面既有的 `job_method_not_allowed`，`Allow` 合并列出 `GET, POST`（§6.2）。

## 2. 方法与证据边界

1. **契约侧**：直接解析 YAML，逐操作取 `responses` 的键集合（不采信文档描述）。
2. **实现侧静态判定**：以 `app.py` 的路由装配为根，逐个 handler 追踪 `JSONResponse/Response/StreamingResponse` 的
   `status_code`、稳定错误映射表（`*_errors.py`）、领域异常到错误的转换、ASGI 中间件（`AssetIngress`、
   `ProfileIngress`、`PronunciationIngress`、`WorkspaceIngress`、`DraftIngress`）与 `app.py` 的全局
   `HTTPException` 处理器。
3. **运行时抽样**：用 `create_app(...)` 显式装配依赖，经 `httpx.ASGITransport` 直接驱动真实 ASGI 应用
   （不启动 uvicorn，不访问真实引擎）。需要业务对象的探测用最小 stub 触发指定分支；不使用真实模型或引擎。
4. **隔离 PG 正向对照**：`SPEECHNEXUS_TEST_DATABASE_URL=postgresql:///sn_profile_it`（PostgreSQL 18.6，库内无表，
   由测试自行 alembic 建 schema），运行 `tests/integration/test_pronunciation_http.py`，
   13 项通过、覆盖 `getPronunciationRevision` 的 200/404/429/503。
5. **环境**：Python 3.14.9，依赖来自主检出 `/Users/hrygo/Documents/SpeechNexus/.venv`，
   `PYTHONPATH` 指向本工作树 `backend/src`、`packages/contracts/src`、`packages/media/src`；
   已确认 `speechnexus_backend` 与 `speechnexus_contracts` 均从工作树导入。
   本工作树无 `node_modules`，Node 消费者检查无法运行（见 §8）。
6. **复核基线方法（3951718e）**：用 `scripts/dev-env/check-route-conformance.py` 对账当前路由与契约
   （42 个操作、`known_unimplemented` 21 条、`known_status_gaps` 16 个操作，退出码 0）；
   用 `create_app(...)` + `httpx.ASGITransport` 重跑关键探测（§7.1）；运行
   `tests/unit/backend/test_public_job_http_surface.py`、`test_public_job_event_stream.py`、
   `test_speech_voice_catalogue_http.py`、`test_workbench_public_voice_catalogue.py`（120 项通过）。

**不在覆盖范围**：真实引擎执行与 200 音频/转写内容、对象存储真实读写、部署层反向代理/鉴权/限流、
多实例并发下的 408/413/429 竞争窗口、WebSocket 升级（101）。

## 3. 已实现操作：声明 vs 可达

"静态可达"由 handler 分支、错误映射表、中间件与全局处理器合并得出；"(+405)"表示框架对错误方法返回 405，
多数路径契约并未声明该状态（见 §5.3）。本表是 `62ae4805` 的 17 个基线操作；复核基线新增的 4 个操作见 §3.1。

| # | 操作 | 路由 | 契约声明 | 实现证据 | 静态可达 | 未声明返回 | 声明未使用 | 结论 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | listModels | GET /v1/models | 200,304,401,503 | `backend/src/speechnexus_backend/speech/interfaces/http/models.py:41-61` | 200,304,503 | — | 401 | 一致 |
| 2 | retrieveModel | GET /v1/models/{model} | 200,304,401,404,503 | `backend/src/speechnexus_backend/speech/interfaces/http/models.py:63-86` | 200,304,404,503 | — | 401 | 一致 |
| 3 | createTranscription | POST /v1/audio/transcriptions | 200,400,401,403,409,413,422,429,503,504 | `transcriptions.py:516-522` | 200,400,404,413,422,503,504 | **404** | 401,403,409,429 | 未声明返回 |
| 4 | createSpeech | POST /v1/audio/speech | 200,400,401,403,404,405,409,422,429,503,504 | `speech.py:458-464` | 200,400,404,422,503,504 | — | 401,403,409,429 | 一致 |
| 5 | getCapabilities | GET /v1/nexus/capabilities | 200,304,401,404,503 | `backend/src/speechnexus_backend/speech/interfaces/http/models.py:88-120` | 200,304,404,503 | — | 401 | 一致 |
| 6 | listProfiles | GET /v1/nexus/profiles | 200,400,401,403,405,503 | `backend/src/speechnexus_backend/speech/interfaces/http/profile_publications.py:222-229` | 200,400,403,405,503 | — | 401 | 一致 |
| 7 | getProfileRevision | GET /v1/nexus/profiles/{profile_id}/revisions/{revision} | 200,400,401,403,404,405,409,503 | `backend/src/speechnexus_backend/speech/interfaces/http/profile_publications.py:225` | 200,400,403,404,405,409,503 | — | 401 | 一致 |
| 8 | listPublishedProfiles | GET /v2/nexus/profiles | 200,400,401,403,405,503 | `backend/src/speechnexus_backend/speech/interfaces/http/profile_publications.py:224` | 200,400,403,405,503 | — | 401 | 一致 |
| 9 | getPronunciationRevision | GET /v1/nexus/pronunciation-sets/{pronunciation_id}/revisions/{revision} | 200,400,401,403,404,405,429,503 | `pronunciations.py:116` | 200,400,403,404,405,429,503 | — | 401 | 一致（隔离 PG 实测 200/404/429/503） |
| 10 | createJob | POST /v1/nexus/jobs | 202,400,401,403,409,422,429,503 | `backend/src/speechnexus_backend/speech/interfaces/http/jobs.py::create_job_router.create_job`、`_create` | 202,400,409,422,503 | — | 401,403,429 | 一致 |
| 11 | getJob | GET /v1/nexus/jobs/{job_id} | 200,401,404,503 | `backend/src/speechnexus_backend/speech/interfaces/http/jobs.py::create_job_router.get_job`、`_read` | 200,404,503 | — | 401 | 一致 |
| 12 | cancelJob | POST /v1/nexus/jobs/{job_id}/cancellations | 200,202,401,404,409,503 | `backend/src/speechnexus_backend/speech/interfaces/http/jobs.py::create_job_router.cancel_job`、`_cancel` | 200,202,400,404,409,503 | **400** | 401 | 未声明返回 |
| 13 | uploadAsset | POST /v1/nexus/assets | 201,400,401,403,404,408,409,410,413,422,429,503 | `backend/src/speechnexus_backend/speech/interfaces/http/assets.py:56-142` | 201,400,403,404,408,409,410,413,422,429,503 | — | 401 | 一致（404 仅有间接路径） |
| 14 | getAsset | GET /v1/nexus/assets/{asset_id} | 200,401,403,404,410,503 | `backend/src/speechnexus_backend/speech/interfaces/http/assets.py:144-158` | 200,403,404,410,503 | — | 401 | 一致 |
| 15 | downloadAssetContent | GET /v1/nexus/assets/{asset_id}/content | 200,206,401,403,404,410,416,503 | `backend/src/speechnexus_backend/speech/interfaces/http/assets.py:160-215` | 200,206,403,404,410,416,503 | — | 401 | 一致 |
| 16 | getArtifact | GET /v1/nexus/artifacts/{artifact_id} | 200,401,404,410 | `backend/src/speechnexus_backend/speech/interfaces/http/artifacts.py:88-114` | 200,304,404,410,503 | **304,503** | 401 | 未声明返回 |
| 17 | downloadArtifactContent | GET /v1/nexus/artifacts/{artifact_id}/content | 200,206,401,404,410 | `backend/src/speechnexus_backend/speech/interfaces/http/artifacts.py:116-189` | 200,206,304,404,410,416,503 | **304,416,503** | 401 | 未声明返回 |

契约锚点：以下均指 `contracts/speech-openapi.yaml` 中对应 operationId 的定义 —— `listModels`、`retrieveModel`、
`createTranscription`、`createSpeech`、`getCapabilities`、`listProfiles`/`getProfileRevision`/`listPublishedProfiles`、
`getPronunciationRevision`、`createJob`、`getJob`、`cancelJob`、`uploadAsset`、`getAsset`/`downloadAssetContent`、
`getArtifact`/`downloadArtifactContent`。

### 3.1 复核基线新增的已实现操作（4 条）

`#432` 与 `#442/#443` 合入后新增注册的 4 个操作。契约声明取自本文件所在 commit 的
`contracts/speech-openapi.yaml`（复核基线 `0.1.0.dev6`）；可达性由 handler 分支、错误映射与运行时探测（§7.1）合并得出。
405 仍按 §5.3 单列。

| # | 操作 | 路由 | 契约声明 | 实现锚点 | 静态可达 | 未声明返回 | 声明未使用 | 结论 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 18 | retryJob | POST /v1/nexus/jobs/{job_id}/retries | 202,400,401,404,409,422,503 | `backend/src/speechnexus_backend/speech/interfaces/http/jobs.py::create_job_router.retry_job`、`_retry` | 202,400,404,409,422,503 | 405 | 401 | 一致（405 见 §5.3） |
| 19 | streamJobEvents | GET /v1/nexus/jobs/{job_id}/events | 200,400,401,404,410,503 | `backend/src/speechnexus_backend/speech/interfaces/http/jobs.py::create_job_router.stream_job_events`、`_open_stream`、`_event_stream` | 200,400,404,503 | 405 | 401,410 | 声明未使用 410；405 见 §5.3 |
| 20 | listVoices | GET /v1/nexus/voices | 200,400,401,503 | `backend/src/speechnexus_backend/speech/interfaces/http/voices.py::create_voice_router.list_voices`、`backend/src/speechnexus_backend/speech/interfaces/http/voices.py::create_voice_router.read` | 200,400,503 | 405 | 401 | 一致（405 见 §5.3） |
| 21 | getVoiceRevision | GET /v1/nexus/voices/{voice_id}/revisions/{revision} | 200,400,401,404,503 | `backend/src/speechnexus_backend/speech/interfaces/http/voices.py::create_voice_router.get_revision`、`backend/src/speechnexus_backend/speech/interfaces/http/voices.py::create_voice_router.read` | 200,400,404,503 | 405 | 401 | 一致（405 见 §5.3） |

`streamJobEvents` 的 `410`（`event_history_expired`）在实现中没有产生路径：
`_open_stream` 只在任务不存在或不可读时返回 404/503，成功分支是 200 事件流；实现中没有 410 分支。这是本次复核新发现的一处"声明未使用"。

### 3.2 #471 新增 listJobs

`GET /v1/nexus/jobs` 已注册到 `jobs.py::create_job_router.list_jobs` / `_list`，
经 `PublicJobs.list_jobs` 和 `PostgresPublicJobs.list_jobs` 读取持久事实。
200 返回生成契约校验的 `JobList`；非法枚举、limit、workspace 标识或游标返回
400 `invalid_job_request`；未装配或存储不可读为 503 `job_unavailable`。
401 的未实现边界沿用原审计；错误方法返回已声明的 405 `job_method_not_allowed`，
`Allow: GET, POST`。真实路由先于中央占位注册，`listJobs` 同时从占位表与未实现基线移除；
`assert_placeholder_ownership` 保持启用，避免真实 GET 被占位 503 遮蔽。
分页、默认 workspace、游标作用域及迁移见 [生产 Job 文档](../components/production-jobs.md#公开-job-列表)。
定向 HTTP/应用层用例见 `tests/unit/backend/test_public_job_list.py`；
隔离持久与 EXPLAIN 用例见 `tests/integration/test_public_job_list_pg.py`。

### 3.3 #490 新增 retireVoiceRevision（retire）

`POST /v1/nexus/voices/{voice_id}/revisions/{revision}/retirements` 已注册到
`backend/src/speechnexus_backend/speech/interfaces/http/voices.py::create_voice_router.retire`，
经 `PostgresVoiceRetirements` 写入既有 `speech_publication_retirements`（`mode`/`reason` 列由
`0023_publication_lifecycle` 提供，无需新迁移），再由 `PostgresVoiceCatalogue.get_revision` 读回
生成契约校验的 `VoiceRevision`（`status=retired`、`retired_at` 生效，精确修订仍可读）。
重复提交相同 `mode`/`reason` 是已存墓碑的重放（200），改名或改因则是 409；
退役非当前选中的修订为 409；未知 voice/revision 为 404。
`retire` 成功后新引用被拒（公开列表过滤 `retired_at_us IS NULL`），已持有依赖租约的在飞任务不被本操作中断。

`disable` 当前返回 422 `voice_disable_unsupported`，不落库：本部署没有
「durable voice-disable intent + 按 `voice_revision_id` 捕获在飞 target 的协调路径」，
而既有 binding-disable 协调器只接受 `kind='binding'` 的事件与 `binding_revision_id` 捕获。
在缺少该路径时不假装取消成功，也不另造第二套取消机制；该缺口是后续切片的输入。

该操作的 503 由 `OperationNotImplemented` 去占位为内联响应（`contracts/speech-openapi.yaml::retireVoiceRevision`）：
仍要求 `Cache-Control` / `X-Should-Retry`，body 保留 `ErrorResponse` 的 `allOf`，`retryable` 固定为 true，
code 覆盖 `voice_catalogue_unavailable` / `voice_retirement_unavailable`；503 表示部署未配置生命周期 writer
或存储不可用，可重试，**不再**是 `operation_not_implemented`。替换占位 503 的窄门禁例外见
[HTTP 兼容检查规则](../validation/http-contract-change-checks.md#replacing-the-central-placeholder-503)。
定向 HTTP 用例见 `tests/unit/backend/test_voice_retirement_http.py`，
隔离 PG 用例见 `tests/integration/test_voice_retirement_pg.py`。

## 4. 未声明返回：逐条证据

### 4.1 createTranscription → 404（契约未声明 404）

已收敛：#462 / speech `0.1.0.dev7` 补充 `404 NotFound`，复用公共错误 envelope；下列为原审计证据。

- 映射：`transcriptions.py:465-466` 将 `TranscriptionModelNotFoundError` 映射为 `model_not_found`；`transcription_errors.py:33` 给出 404。
- 产生路径：`application/transcription.py:281` 在模型未发布时抛出该异常。
- 契约：`contracts/speech-openapi.yaml::createTranscription` 只声明 200/400/401/403/409/413/422/429/503/504。
- 运行时：P10 返回 `404 {"code":"model_not_found"}`（§7）。

### 4.2 cancelJob → 400（契约只声明 200/202/401/404/409/503）

已收敛：#462 / speech `0.1.0.dev7` 补充 `400 BadRequest`；下列为原审计证据。

- `backend/src/speechnexus_backend/speech/interfaces/http/jobs.py::create_job_router.cancel_job` 与 `_idempotency_key`：
  `Idempotency-Key` 不合法时返回 `job_error("invalid_job_request")`；`backend/src/speechnexus_backend/speech/interfaces/http/job_errors.py::_ERRORS`（`invalid_job_request`） 定义为 400。
  【复核 3951718e】成立：键校验先于服务调用，非法键仍返回 400。
- 运行时：P9，`Idempotency-Key: short` → `400 invalid_job_request`；复核基线复现见 §7.1 R3。
- 同族问题：`createJob` 的同类错误合法（契约声明了 400），但取消路径漏声明，消费者按 200/202/404/409 分支处理会落入未知状态。

### 4.3 getArtifact → 304（契约未声明 304）

已收敛：#462 / speech `0.1.0.dev7` 为两条制品 GET 声明 304、ETag 和条件请求参数。
304 按实际 HTTP 语义显式使用 `content: {}`，没有错误正文；下列为原审计证据。

- `backend/src/speechnexus_backend/speech/interfaces/http/artifacts.py:112-113`：`If-None-Match` 命中时返回 `Response(status_code=304)`。
- 运行时：A2，先取 200 的 `ETag` 再回送 `If-None-Match`，实测 304 且无响应体。
- 对照：`listModels`/`retrieveModel`/`getCapabilities` 明确声明 304（`contracts/speech-openapi.yaml` 对应操作），
  制品两条 GET 未声明，说明是遗漏而非统一约定。

### 4.4 getArtifact / downloadArtifactContent → 503（契约未声明 503）

已收敛：#462 / speech `0.1.0.dev7` 为两条 GET 补充 `503 ServiceUnavailable`；下列为原审计证据。

- `backend/src/speechnexus_backend/speech/interfaces/http/artifacts.py:103-104,132-133`：服务未装配时返回 `artifact_service_unavailable`；
  `artifact_errors.py:16,21` 均为 503。
- 运行时：P2/P2b（未配置制品服务）实测 503。
- 契约对这两条操作只声明 200/206/401/404/410，没有任何 5xx。

### 4.5 downloadArtifactContent → 416（契约未声明 416）

已收敛：#462 / speech `0.1.0.dev7` 补充 416，复用既有 byte-range 错误 schema 和
`Content-Range: bytes */<size>`；下列为原审计证据。

- `backend/src/speechnexus_backend/speech/interfaces/http/artifacts.py:146-156`：`artifact_range_not_satisfiable` 补 `Content-Range: bytes */<size>` 后返回 416；
  `artifact_errors.py:11` 定义为 416。
- 运行时：A3（`Range: bytes=0-1` + stub 抛该错误）实测 416。
- 对照：`downloadAssetContent` 声明了 416（`contracts/speech-openapi.yaml::downloadAssetContent`），制品内容路径漏声明。

## 5. 声明未使用与框架层偏差

### 5.1 401：全表未使用

公开语音处理器不校验共享服务密钥，源码中没有 401 的响应分支：唯一的 401 出现是
`backend/src/speechnexus_backend/speech/interfaces/http/artifacts.py:95,124` 的 OpenAPI 响应声明与工作台客户端的响应分类
（`workbench/infrastructure/public_speech_render_jobs.py:83`），
`docs/architecture/openai-compatibility.md:91` 已明确"当前公开语音处理器没有实现该密钥的验证"。
17 条基线已实现操作全部声明 401，但均不可达；消费者不能把 401 当作访问保护已生效的证据。
【复核 3951718e】仍成立；新增的 4 个操作同样只声明 401（§3.1）。

### 5.2 403 / 409 / 429：仅部分路径可达

| 操作 | 声明的 403/409/429 | 是否有产生路径 | 证据 |
| --- | --- | --- | --- |
| createSpeech | 403,409,429 | 否 | `speech.py:340-443` 无对应分支，`/v1/audio/speech` 无入口中间件 |
| createTranscription | 403,409,429 | 否 | `transcriptions.py:442-501` 无对应分支 |
| createJob | 403,429 | 否 | `backend/src/speechnexus_backend/speech/interfaces/http/jobs.py::_create` 无 host/origin 检查、无限流 |
| listProfiles / getProfileRevision / listPublishedProfiles / getPronunciationRevision | 403（+429 仅读音） | 是 | `profile_ingress.py:67-97`、`draft_ingress.py:64-67`、`pronunciations.py:146-149,167-169` |
| uploadAsset | 403,429（另 408,413） | 是 | `asset_ingress.py:52-73,98-99` |

【复核 3951718e】结论未变：`_create` 仍无 403/429 分支；`createJob` 的 409 仅由 `idempotency_conflict` 产生，
不属于本表列出的"无产生路径"项。

### 5.3 405：多数路径未声明

已收敛：#462 / speech `0.1.0.dev7` 补齐当前全部 16 个已注册操作缺失的 405。
models、capabilities、transcriptions 和两条 voices GET 的框架 405 统一为
`method_not_allowed` 公共错误 envelope，保留 `Allow`；已有资源错误码保持原行为。
下列为原审计基线，不代表当前返回格式。

405 由框架对错误方法产生，经 `backend/src/speechnexus_backend/app.py::create_app.asset_http_error` 的全局处理器改写为稳定错误体
（assets/artifacts/workspaces/jobs/speech 五个前缀），其余前缀保留 FastAPI 默认 `{"detail":"..."}`。

- 已声明 405：`createSpeech`、三个 profiles 操作、`getPronunciationRevision`。
- 未声明但可产生 405：`listModels`、`retrieveModel`、`getCapabilities`、`createTranscription`、
  `createJob`、`getJob`、`cancelJob`、`uploadAsset`、`getAsset`、`downloadAssetContent`、`getArtifact`、
  `downloadArtifactContent`、`retryJob`、`streamJobEvents`、`listVoices`、`getVoiceRevision`。
- 错误体不一致：`/v1/audio/transcriptions`、`/v1/models`、`/v1/nexus/capabilities` 等无前缀处理器的路径返回
  `{"detail":"Method Not Allowed"}`（运行时 P4），不含 `error.code/request_id`，不符合 `ErrorResponse`。
- 【复核 3951718e】五前缀与错误体结论未变；新增的 4 个操作同样只产生未声明的 405（§3.1）。

### 5.4 全局 404 语义被前缀处理器改写

`backend/src/speechnexus_backend/app.py::create_app.asset_http_error` 的 `JOB_PREFIX` 分支把 `/v1/nexus/jobs/*` 下的任意 404
改写为 `job_not_found`。因此未注册子路径（如 `/v1/nexus/jobs/{job_id}/unknown-subpath`）返回 `404 job_not_found`
而不是"路由不存在"（复核基线实测 §7.1 R6）。

【复核 3951718e】**部分过期**：`retryJob`（`contracts/speech-openapi.yaml::retryJob`）与 `streamJobEvents`
（`contracts/speech-openapi.yaml::streamJobEvents`）已由 `#432` 注册，其 `404 job_not_found` 现在表示任务不存在，不再是"路由不存在"；
改写本身仍作用于未注册子路径。

`/v1/nexus/pronunciation-sets` 前缀被 `PronunciationIngress` 接管（`backend/src/speechnexus_backend/speech/interfaces/http/pronunciations.py::PronunciationIngress.__call__`），
未注册的集合级 GET/POST 在匹配失败时返回 `400 pronunciation_invalid_request`，复核基线实测见 §7.1 R9。

## 6. 未实现操作（25 条，审计基线）

审计基线 `62ae4805` 下契约声明但后端未注册的操作。运行时列是基线实测（同一应用、无服务依赖）。
【复核 3951718e】`listVoices`/`getVoiceRevision`/`retryJob`/`streamJobEvents` 已实现并移出本表（§3.1），
其余未实现操作见 §6.1，条数以 §10 门禁基线为准。本表保留基线编号、基线契约声明与基线响应；已实现行在响应列标注，当前声明见 §3.1/§3.2。

| # | 操作 | 路由 | 契约声明 | 运行时响应 |
| --- | --- | --- | --- | --- |
| 18 | listVoices | GET /v1/nexus/voices | 200,401,503 | **【已实现】** 见 §3.1 |
| 19 | getVoiceRevision | GET /v1/nexus/voices/{voice_id}/revisions/{revision} | 200,401,404,503 | **【已实现】** 见 §3.1 |
| 20 | retireVoiceRevision | POST /v1/nexus/voices/{voice_id}/revisions/{revision}/retirements | 200,400,401,404,409,422 | 404 默认 |
| 21 | listPronunciationSets | GET /v1/nexus/pronunciation-sets | 200,401,503 | **400 pronunciation_invalid_request** |
| 22 | createPronunciationRevision | POST /v1/nexus/pronunciation-sets | 201,400,401,409,422,429 | **400 pronunciation_invalid_request** |
| 23 | listJobs | GET /v1/nexus/jobs | 200,400,401,503 | 基线 **405 job_method_not_allowed**；#471 已实现，见 §3.2 |
| 24 | retryJob | POST /v1/nexus/jobs/{job_id}/retries | 202,401,404,409,422 | **【已实现】** 见 §3.1 |
| 25 | streamJobEvents | GET /v1/nexus/jobs/{job_id}/events | 200,401,404,410 | **【已实现】** 见 §3.1 |
| 26 | getOperation | GET /v1/nexus/operations/{operation_id} | 200,401,404,503 | 404 默认 |
| 27 | cancelOperation | POST /v1/nexus/operations/{operation_id}/cancellations | 200,202,401,404,409 | 404 默认 |
| 28 | createRealtimeSession | POST /v1/nexus/realtime/sessions | 201,400,401,403,409,422,429,503 | 404 默认 |
| 29 | getRealtimeSession | GET /v1/nexus/realtime/sessions/{session_id} | 200,401,404,410 | 404 默认 |
| 30 | cancelRealtimeSession | POST /v1/nexus/realtime/sessions/{session_id}/cancellations | 200,202,401,404,409 | 404 默认 |
| 31 | connectRealtime | GET /v1/realtime | 101,400,401,403,404,409,503 | 404 默认（无 WebSocket 路由） |
| 32 | createVoiceDesign | POST /v1/nexus/voice-designs | 202,400,401,409,422,429 | 404 默认 |
| 33 | createVoiceClone | POST /v1/nexus/voice-clones | 202,400,401,409,422,429 | 404 默认 |
| 34 | getVoiceDraft | GET /v1/nexus/voice-drafts/{draft_id} | 200,401,404 | 404 默认 |
| 35 | updateVoiceDraftMetadata | PATCH /v1/nexus/voice-drafts/{draft_id} | 200,400,401,404,409,422 | 404 默认 |
| 36 | generateVoiceDraftRevision | POST /v1/nexus/voice-drafts/{draft_id}/generations | 202,400,401,404,409,422 | 404 默认 |
| 37 | createVoicePreview | POST /v1/nexus/voice-drafts/{draft_id}/previews | 202,400,401,404,409,422 | 404 默认 |
| 38 | validateVoiceDraftRevision | POST /v1/nexus/voice-drafts/{draft_id}/validations | 202,400,401,404,409,422 | 404 默认 |
| 39 | publishVoiceDraftRevision | POST /v1/nexus/voice-drafts/{draft_id}/publications | 200,201,400,401,404,409,422 | 404 默认 |
| 40 | speechCreateDeletionPlan | POST /v1/nexus/deletion-plans | 200,404,409,422 | 404 默认 |
| 41 | speechCreateDeletion | POST /v1/nexus/deletions | 202,404,409,422 | 404 默认 |
| 42 | speechGetDeletion | GET /v1/nexus/deletions/{deletion_id} | 200,404,409,422 | 404 默认 |

契约锚点（`contracts/speech-openapi.yaml`）：`listVoices`、`getVoiceRevision`/`retireVoiceRevision`、
`listPronunciationSets`/`createPronunciationRevision`、`listJobs`、`retryJob`、`streamJobEvents`、
`getOperation`/`cancelOperation`、`createRealtimeSession`/`getRealtimeSession`/`cancelRealtimeSession`/`connectRealtime`、
`createVoiceDesign` 至 `publishVoiceDraftRevision`（9 条）、`speechCreateDeletionPlan`/`speechCreateDeletion`/`speechGetDeletion`。

这些缺失不是"契约先行、消费者未接入"：`backend/src/speechnexus_backend/workbench/infrastructure/public_voice_catalogue.py::_VOICES_PATH`
是生产消费者的路径常量，`PublicVoiceCatalogue.list_summaries`/`get_revision` 据此调用 `/v1/nexus/voices`；
`backend/src/speechnexus_backend/workbench/infrastructure/public_speech_render_jobs.py::PublicSpeechRenderJobs.submit` 调用 `POST /v1/nexus/jobs`，
`backend/src/speechnexus_backend/workbench/infrastructure/public_speech_render_jobs.py::PublicSpeechRenderJobs.read_job` 调用 `GET /v1/nexus/jobs/{job_id}`（后者已实现）。
`docs/archive/plans/2026-10-24-team-c-goal.md:78` 也记录了"音色运行时尚未实现这些路由"这一已知缺口。

### 6.1 未实现操作

未实现操作清单只在 §10 门禁脚本
[check-route-conformance.py](../../scripts/dev-env/check-route-conformance.py) 的
`KNOWN_UNIMPLEMENTED_OPERATIONS` 中维护：成员、条数与原因以该常量为准，本文不复制。
占位路由与 `operation_not_implemented` 的语义见 §6.2，集合级读音路由被改写为
`400 pronunciation_invalid_request` 的例外见 §5.4。
实现一个操作时同时删除占位条目与该基线条目（§10 第 4 项）。

已实现、因此不在该常量中的操作：`listVoices`/`getVoiceRevision`/`retryJob`/`streamJobEvents`（§3.1）、
`listJobs`（§3.2）、`retireVoiceRevision`（§3.3），以及 #494 的
`createVoiceDesign`/`createVoiceClone`/`getVoiceDraft`/`updateVoiceDraftMetadata`。

### 6.2 #473：未实现操作的稳定错误（#473 交付基线实测）

`934855c3` 上复核 §6.1 时确认：21 条操作仍无生产实现，但响应分三类（18 条框架 404 裸 `detail`、`listJobs` 的 405、
两条集合级读音路由的 400），消费者无法与「资源不存在 / 方法不支持 / 参数错误」区分。契约版本 `0.1.0.dev8` 起，
21 条全部由中央占位路由注册并返回同一种可辨识错误。
【#471】其中 `listJobs` 已交给真实实现；【#490】`retireVoiceRevision` 也已交给真实实现（`disable`
以 422 明确未支持）；【#494】`createVoiceDesign`/`createVoiceClone`/`getVoiceDraft`/`updateVoiceDraftMetadata`
同样已由真实实现接替占位，下表 32–35 行保留 #473 基线响应。其余未实现操作继续返回占位错误，
当前清单以 §6.1 指向的门禁基线为准。

实测方法：`create_app(settings=...)` 组装真实应用（无服务注入、不启动 lifespan），逐条发请求并记录状态码、
错误码、`retryable` 与 `X-Request-ID`；下表为 #473 前后同口径实测。

| # | 操作 | 路由 | #473 前 | #473 后 |
| --- | --- | --- | --- | --- |
| 20 | retireVoiceRevision | POST /v1/nexus/voices/{voice_id}/revisions/{revision}/retirements | 404 框架默认 | **503 `operation_not_implemented`** |
| 21 | listPronunciationSets | GET /v1/nexus/pronunciation-sets | **400 `pronunciation_invalid_request`** | **503 `operation_not_implemented`** |
| 22 | createPronunciationRevision | POST /v1/nexus/pronunciation-sets | **400 `pronunciation_invalid_request`** | **503 `operation_not_implemented`** |
| 23 | listJobs | GET /v1/nexus/jobs | **405 `job_method_not_allowed`** | **503 `operation_not_implemented`** |
| 26 | getOperation | GET /v1/nexus/operations/{operation_id} | 404 框架默认 | **503 `operation_not_implemented`** |
| 27 | cancelOperation | POST /v1/nexus/operations/{operation_id}/cancellations | 404 框架默认 | **503 `operation_not_implemented`** |
| 28 | createRealtimeSession | POST /v1/nexus/realtime/sessions | 404 框架默认 | **503 `operation_not_implemented`** |
| 29 | getRealtimeSession | GET /v1/nexus/realtime/sessions/{session_id} | 404 框架默认 | **503 `operation_not_implemented`** |
| 30 | cancelRealtimeSession | POST /v1/nexus/realtime/sessions/{session_id}/cancellations | 404 框架默认 | **503 `operation_not_implemented`** |
| 31 | connectRealtime | GET /v1/realtime | 404 框架默认 | **503 `operation_not_implemented`**（仅 HTTP 请求；WebSocket 升级仍被框架拒绝，见下） |
| 32 | createVoiceDesign | POST /v1/nexus/voice-designs | 404 框架默认 | **【#494 已实现】** |
| 33 | createVoiceClone | POST /v1/nexus/voice-clones | 404 框架默认 | **【#494 已实现】** |
| 34 | getVoiceDraft | GET /v1/nexus/voice-drafts/{draft_id} | 404 框架默认 | **【#494 已实现】** |
| 35 | updateVoiceDraftMetadata | PATCH /v1/nexus/voice-drafts/{draft_id} | 404 框架默认 | **【#494 已实现】** |
| 36 | generateVoiceDraftRevision | POST /v1/nexus/voice-drafts/{draft_id}/generations | 404 框架默认 | **503 `operation_not_implemented`** |
| 37 | createVoicePreview | POST /v1/nexus/voice-drafts/{draft_id}/previews | 404 框架默认 | **503 `operation_not_implemented`** |
| 38 | validateVoiceDraftRevision | POST /v1/nexus/voice-drafts/{draft_id}/validations | 404 框架默认 | **503 `operation_not_implemented`** |
| 39 | publishVoiceDraftRevision | POST /v1/nexus/voice-drafts/{draft_id}/publications | 404 框架默认 | **503 `operation_not_implemented`** |
| 40 | speechCreateDeletionPlan | POST /v1/nexus/deletion-plans | 404 框架默认 | **503 `operation_not_implemented`** |
| 41 | speechCreateDeletion | POST /v1/nexus/deletions | 404 框架默认 | **503 `operation_not_implemented`** |
| 42 | speechGetDeletion | GET /v1/nexus/deletions/{deletion_id} | 404 框架默认 | **503 `operation_not_implemented`** |

32–35 四条的 #473 基线响应同为占位 `operation_not_implemented`（503），已由 #494 实现，行内不再重复该值。

统一响应的共同约束（`contracts/speech-openapi.yaml::OperationNotImplemented`）：状态 503、`error.type` 为
`service_unavailable_error`、`error.code` 恒为 `operation_not_implemented`、`error.retryable` 恒为 `false`、
`Cache-Control: no-store`、`X-Should-Retry: false`，且响应头与包封共享同一个 `request_id`。
21 条中的 16 条在契约里指向该组件；其余 5 条（`listPronunciationSets`、`listJobs`、`getOperation`、
`createRealtimeSession`、`connectRealtime`）沿用原有的 `ServiceUnavailable` 声明——它接受同一响应体，
替换会从版本化面移除 `Retry-After` 头，被结构门禁判为破坏性变更（见下）。

**表达选择：A（统一 503 + 专用错误码）。** 理由与另外两个方案的实测差异：

- **不新增 `ErrorType` 枚举值。** `contracts/shared/errors.schema.json::ErrorType` 是语音、realtime、admin、workbench
  四个公开面共用的单一来源，501 需要新增 `not_implemented_error`（方案 B），会同时改共享 schema、四个面的生成校验器
  与全部消费者；503 复用既有 `service_unavailable_error`，只新增一个状态声明。
- **「未实现」不得与真实故障共用错误码。** 占位路由的错误映射
  （`backend/src/speechnexus_backend/speech/interfaces/http/unimplemented_errors.py::_ERRORS`）只有
  `operation_not_implemented` 一项，门禁静态校验它必须恰好等于 `{operation_not_implemented: 503}`。
  真实故障继续用 `backend_unavailable`、`artifact_service_unavailable`、`job_unavailable` 等自己的码，
  因此**只有 `error.code` 能区分**：`job_unavailable` 同为 503 + `service_unavailable_error` 且 `retryable` 也是 `false`，
  状态码、类型与 `retryable` 三者都不足以识别「未实现」；
  这与方案 C（保留 404/405/400 仅换错误码）不同：C 的状态码本身仍会把「未实现」读成「资源不存在 / 方法不支持」，
  且 404 与真实 `*_not_found` 无法靠状态码区分。
- **契约代价已量化。** 21 条原本只声明成功与业务错误的操作，实际会返回 405（同路径其它方法）与 503（未实现）；
  本次为每条补 `405`（复用既有 `MethodNotAllowed`，21 处），并为原本没有 503 的 16 条补 `503`
  （新组件 `OperationNotImplemented`），共 37 个新增声明；另有 5 条原本就声明了 503，保留原 `ServiceUnavailable`
  声明不动——同一响应体满足它，替换则属于移除 `Retry-After` 的破坏性变更，`contracts-speech-changes` 与
  `check-openai-contract-compat.py` 都不放行。这与 #462 对已实现操作补 405 的做法一致，`KNOWN_UNDECLARED_STATUSES` 仍为空，
  未放宽任何既有检查。ADR 0012 第 5 条「未实现的操作返回 `503`/`501` 与既有错误包封」本就是本处置的依据。

实现与门禁：

- 注册面：`backend/src/speechnexus_backend/speech/interfaces/http/unimplemented.py::UNIMPLEMENTED_OPERATIONS`
  是唯一的占位表，`create_unimplemented_router()` 逐条注册；`include_in_schema=False`，服务端 OpenAPI 不把这些操作
  宣传为可用（ADR 0012 第 5 条「不以占位响应通过契约检查」）。
- 405 语义：同一路径上的 405 仍是方法不允许（不是 `operation_not_implemented`），错误码沿用各面既有的
  `method_not_allowed` / `job_method_not_allowed`；`Allow` 头合并**同一模板**上全部已注册方法，而不是框架默认
  只列第一条部分匹配路由。参数化路径同样合并：`DELETE /v1/nexus/voice-drafts/{draft_id}` 返回
  `Allow: GET, PATCH`（`getVoiceDraft` 与 `updateVoiceDraftMetadata` 同模板），`DELETE /v1/nexus/jobs` 返回
  `Allow: GET, POST`。读音集合级路径用通用 `method_not_allowed`，item 级路径用 `pronunciation_method_not_allowed`：
  后者的契约组件把 `Allow` 固定为 `GET`，而集合级同时接受 `GET` 与 `POST`，不能共用该组件。
- 归属守卫与路径别名：`assert_placeholder_ownership` 按 Starlette 实际服务的资源比较模板，除参数名/converter 外
  还吸收 `redirect_slashes` 的尾斜杠别名——`/v1/nexus/voice-drafts/{draft_id}/`、`.../{draft_id}//` 与占位表声明的
  无斜杠模板视为同一 owner，报错仍显示占位表原 template。该归一化只检查占位表拥有的 method+template：语音草稿占位
  只拥有 GET/PATCH，因此 `DELETE /v1/nexus/voice-drafts/abc` 实测 405 `Allow: GET, PATCH`，而额外注册真实
  `DELETE /v1/nexus/voice-drafts/{draft_id}/` 后，`DELETE /abc/` 实测 200；DELETE 不因 GET/PATCH 的别名规则被守卫占用。
  原始 ASGI scope 实测（`starlette==1.9.0`）：GET 尾斜杠请求 307 到无斜杠路径，若真实路由只注册在带斜杠模板上，
  无斜杠请求仍被占位遮蔽（503）、带斜杠请求才到真实实现。
  重复内部斜杠、前导 `//`、`%2F`、大小写差异均为 404；`%20` 在解码后由参数匹配；`.`/`..` 段应用不解析（404），
  这些实测状态由原始 ASGI scope 回归断言固定，避免 HTTP 客户端预先归一化点段。根路径也单独固定：`GET /` 为 200、
  `GET //` 为 404，`normalize_template("/")` 与 `normalize_template("//")` 保持不同；Starlette 不把根路径重定向到
  双斜杠路径，也不应把后者折叠为根路径。
  这些路径不构成同一注册模板，或发生在 ASGI 路由之前，进程内守卫无法观察；部署若在入口做大小写、重复斜杠或点段
  归一化，需自行确认不会让真实路由落在占位之后。
- 读音集合级路径：`PronunciationIngress` 原先在路由前把集合级请求改写为 400；现改为在完成 Host/Origin 校验后放行到
  占位路由，容量与固定 revision 形状校验仍只作用于 item 路径。Host/Origin 被拒绝时仍返回
  `403 pronunciation_host_rejected`（入口边界，与是否有实现无关）。
- `connectRealtime`：HTTP GET 返回上述 503；契约声明的 101 属 WebSocket 升级，框架对未注册的 WS 路由直接拒绝握手，
  无法承载 HTTP 错误包封。该限制不变，也不把 WS 升级记为已实现。

## 7. 运行时证据（审计基线 62ae4805）

装配：`create_app(settings=...)`，只注入必要依赖；`Host: localhost` 以通过入口的 Host 校验。
探测脚本位于工作树外的 `/tmp/sn-audit-probe/`，不进入仓库。
本表是 `62ae4805` 的基线实测；其中 P1、J2、J3、U1 涉及后续已实现或已变更的路由，复核基线结果见 §7.1 与 §3.1/§6.1。

| 编号 | 请求 | 实测 | 说明 |
| --- | --- | --- | --- |
| P1 | GET /v1/nexus/voices | 404 `{"detail":"Not Found"}` | 未注册；错误体不符合契约（复核基线已变为 503，见 §7.1 R7） |
| P2 | GET /v1/nexus/artifacts/artifact-demo | 503 `artifact_service_unavailable` | 契约未声明 503 |
| P2b | GET /v1/nexus/artifacts/artifact-demo/content | 503 `artifact_service_unavailable` | 同上 |
| P3 | PUT /v1/nexus/jobs | 405 `job_method_not_allowed` | createJob 未声明 405 |
| P4 | PUT /v1/audio/transcriptions | 405 `{"detail":"Method Not Allowed"}` | 无前缀处理器，错误体不合契约 |
| P5 | GET /v1/nexus/capabilities?workspace_id=bad id | 404 `capability_workspace_not_found` | 契约声明 404，正向对照 |
| P6 | GET /v1/nexus/profiles（未配置服务） | 503 `profile_unavailable` | 契约声明 503，正向对照 |
| P7 | POST /v1/nexus/jobs（400 KB body） | 400 `invalid_job_request` | 超限走 400 而非 413；契约只声明 400，故一致 |
| P8 | GET /v1/audio/speech | 405 `speech_method_not_allowed` | 契约声明 405，正向对照 |
| P9 | POST /v1/nexus/jobs/job-demo/cancellations，`Idempotency-Key: short` | 400 `invalid_job_request` | **契约未声明 400** |
| P10 | POST /v1/audio/transcriptions，`model=whisper-x`（服务抛 `TranscriptionModelNotFoundError`） | 404 `model_not_found` | **契约未声明 404**；服务 stub 复现 `application/transcription.py:281` 的异常 |
| A1 | GET /v1/nexus/artifacts/artifact-demo（stub 制品服务） | 200 + `ETag` | 正向对照 |
| A2 | 同上 + `If-None-Match` | 304，空体 | **契约未声明 304** |
| A3 | GET /v1/nexus/artifacts/artifact-demo/content，`Range: bytes=0-1` | 416 `artifact_range_not_satisfiable` | **契约未声明 416** |
| A4 | GET /v1/nexus/artifacts/missing-one | 404 `artifact_not_found` | 契约声明 404 |
| J1 | GET /v1/nexus/jobs | 基线 405 `job_method_not_allowed` | #471 已实现：当前 200/400/503，见 §3.2；该行保留基线事实 |
| J2 | POST /v1/nexus/jobs/job-demo/retries | 404 `job_not_found` | 未实现；错误码误导为"任务不存在"（复核基线已注册，见 §7.1 R4） |
| J3 | GET /v1/nexus/jobs/job-demo/events | 404 `job_not_found` | 未实现；SSE 消费者拿到 JSON（复核基线已注册，见 §7.1 R5） |
| R1 | GET /v1/nexus/realtime/sessions/s-1 | 404 `{"detail":"Not Found"}` | 未注册 |
| V1 | POST /v1/nexus/voice-drafts/d-1/previews | 404 `{"detail":"Not Found"}` | 未注册 |
| U1 | GET /v1/nexus/voices/voice-1/revisions/2 | 404 默认 | 未注册（复核基线已变为 503/200，见 §3.1） |
| U2 | POST .../retirements | 404 默认 | 未注册 |
| U3 | GET /v1/nexus/operations/op-1 | 404 默认 | 未注册 |
| U4 | GET /v1/nexus/pronunciation-sets | **400 pronunciation_invalid_request** | 未注册集合级路由被入口中间件改写 |
| U5 | POST /v1/nexus/pronunciation-sets | **400 pronunciation_invalid_request** | 同上 |
| U6 | POST /v1/nexus/deletions 等 3 条删除路由 | 404 默认 | 未注册 |

隔离 PG 正向对照：`SPEECHNEXUS_TEST_DATABASE_URL=postgresql:///sn_profile_it` 下
`tests/integration/test_pronunciation_http.py` 13 项通过（`-k "not generated_consumers"`），
覆盖该路由的 200/404/429/503；说明"已声明且已实现"的路由确实按契约工作。

### 7.1 复核基线重跑的关键探测（3951718e）

装配与 §7 相同：`create_app(settings=Settings(...))` + `httpx.ASGITransport`，`Host: localhost`。
需要命中"任务不存在"分支时注入最小 stub 服务（`retry`/`get`/`events` 抛 `PublicJobNotFoundError`）。

| 编号 | 请求 | 实测 | 用途 |
| --- | --- | --- | --- |
| R1 | GET /v1/nexus/jobs | 405 `job_method_not_allowed` | `listJobs` 仍未注册（§1 第 2 条成立） |
| R2 | PUT /v1/nexus/jobs | 405 `job_method_not_allowed` | `createJob` 的未声明 405 仍存在 |
| R3 | POST /v1/nexus/jobs/job-demo/cancellations，`Idempotency-Key: short` | 400 `invalid_job_request` | `cancelJob` 未声明 400 仍成立（§4.2） |
| R4 | POST /v1/nexus/jobs/job-demo/retries（stub 抛 not found） | 404 `job_not_found` | 路由已注册；404 仅在任务不存在时返回 |
| R5 | GET /v1/nexus/jobs/job-demo/events（stub 抛 not found） | 404 `job_not_found` | 同上；不再是"路由不存在" |
| R6 | GET /v1/nexus/jobs/job-demo/unknown-subpath | 404 `job_not_found` | 全局 404 改写仍作用于未注册子路径（§5.4） |
| R7 | GET /v1/nexus/voices | 503 `voice_catalogue_unavailable`（retryable） | 已注册；§1 第 1 条已过期 |
| R8 | GET /v1/nexus/voices/voice-demo-1/revisions/2 | 503 `voice_catalogue_unavailable` | 同上 |
| R9 | GET /v1/nexus/pronunciation-sets（`Host: localhost`） | 400 `pronunciation_invalid_request` | §5.4 的集合级改写仍成立 |

## 8. 未验证项

| 项 | 原因 |
| --- | --- |
| 200 音频/转写内容、200/206 下载内容 | 需要真实引擎与对象存储；审计只读，未装配 |
| 410（资产/制品过期） | 需要过期记录与存储版本；只按代码路径确认可达（`application/assets.py:231-240`、`application/artifacts.py:73`） |
| 408 / 413 / 429（`uploadAsset`） | 需要并发占满入口或超限上传；静态确认 `asset_ingress.py:66-72,98-99` |
| 403（Host/Origin 拒绝） | 探测使用 `Host: localhost` 通过校验；拒绝分支由 `origin_policy.py:36-39` 静态确认 |
| 401 | 实现不存在该路径，无法运行验证 |
| `/v1/realtime` 101 升级 | 无 WebSocket 路由 |
| Node 消费者检查 | 本工作树无 `node_modules`，`test_generated_consumers_read_actual_loopback_http` 因找不到 `openapi-fetch` 失败；不是产品缺陷 |

## 9. 处置优先级

1. **P0**：为 `listVoices`/`getVoiceRevision` 补实现，或从契约与工作台消费者中撤下；当前工作台音色目录必然 503。
   【复核 3951718e】**已完成**：`#442/#443` 补齐两条 GET；未配置目录为可重试 503，正常读取 200（§9.1）。
2. **P0**：让未实现路由返回可辨识的稳定错误（而非默认 404 detail、405、400 或 `job_not_found`），
   避免消费者把"未实现"误判为"参数错误/资源不存在"。
   **【#473 已完成】**：21 条统一返回 `503 operation_not_implemented`（`retryable: false`），与
   `backend_unavailable`、`*_not_found`、`method_not_allowed` 均可区分；实测与理由见 §6.2。
   【#471】`listJobs` 已实现，J1 从此项移除；其余未实现路由以 §6.1 指向的门禁基线为准。
3. **P1**：补齐 `getArtifact`/`downloadArtifactContent` 的 304、416、503 声明；补齐 `cancelJob` 的 400、
   `createTranscription` 的 404。
4. **P1**：为 `/v1/audio/transcriptions`、`/v1/models`、`/v1/nexus/capabilities` 等路径补 405 声明与统一错误体，
   或在全局处理器中覆盖这些前缀。
5. **P2**：决定 401 的处置：实现共享服务密钥校验，或从公开契约移除 401 并把"无鉴权"写进部署前置条件。
6. **P2**：清理 `createSpeech`/`createTranscription` 的 403/409/429 与 `createJob` 的 403/429 声明（无产生路径），
   或补上对应入口能力。

【复核 3951718e】§9 的第 1 项已完成；第 2–6 项仍成立，且第 4 项的 405 缺口新增
`retryJob`/`streamJobEvents`/`listVoices`/`getVoiceRevision`（§3.1）。
【#473】第 2 项已完成；第 3–6 项仍成立。

### 9.1 #442 音色目录公开 GET 处置

以上计数与探测保留审计基线事实。[#442](https://github.com/hrygo/SpeechNexus/issues/442)
在 PR #443 补齐下列两条 GET；本节只更新音色目录处置，不重写其余路由的历史结论。

| 操作 | 实现与覆盖 |
| --- | --- |
| `listVoices` | 已注册；当前已发布 head 的摘要、分页与语言过滤；未配置/无发布/缺失或不匹配文档为可重试 503；非法请求为不可重试 400 |
| `getVoiceRevision` | 已注册；固定发布版本 200、目录内未知 ID/版本 404；未配置/缺失或不匹配文档为可重试 503；非法请求为不可重试 400；保留退役历史 |

读取既有发布事实和资格文档，迁移 `0038_voice_catalogue_reader` 仅新增公开只读视图，
不建第二套目录状态。部署与权限见[音色目录组件](../components/workbench-voice-catalogue.md#底座公开路由与部署)。
`tests/integration/test_speech_voice_catalogue.py` 经实际底座 lifespan、最小 PG 角色与工作台
原消费者验证；合成发布记录不证明真实音色资格、Linux/CUDA、物理多机或生产部署已验收。
公开源 `0.1.0.dev6` 新增两条 GET 的 `400 voice_invalid_request` 及不可重试头/错误约束，
生成 Python/TypeScript 校验器与共享正负例同步；成功 DTO、字段与枚举未扩展。
具体候选与原始实测输出保存在 PR/DEL-494 交付记录。

### 9.2 #462 已实现返回状态的契约收敛

在固定基线 `3f48b0195023282bde030deb79ecf5858f161c9d`，门禁实际记录
**16 个操作 / 23 个状态对**（16 个 405 加 7 个其他状态）；任务描述中的
21 个状态对是较早计数。`0.1.0.dev7` 补齐这 23 对，
`KNOWN_UNDECLARED_STATUSES` 收缩为空，即 **0 个操作 / 0 个状态对**。
21 条未实现操作的基线和「已声明但未静态观察到」的独立报告保留，未因本次声明视为实现
（#473 起该基线改为「已注册且只返回占位错误」，见 §6.2/§10）。
401/403 等入口/访问语义未在本次改变。

所有新增错误响应复用 `ErrorResponse`，不新增枚举值。304 是无正文的条件响应，
其空 content 与 ETag 头明确声明，不能给实际空体伪造错误 schema。
Python/TypeScript 绑定与打包 HTTP 投影由源重新生成；严格消费者需同步更新。
合成正负例在 `tests/fixtures/contracts/speech-http-statuses.json` 和
`voice-catalogue-http.json`，实际 ASGI 响应验证在
`tests/contract/test_speech_http_statuses.py`，覆盖 405 envelope/Allow/request ID、
404/400、304 空体/ETag、416 和 503，以及裸 detail/缺字段反例。
这些检查不证明真实引擎、对象存储或生产访问保护已经验收。

## 10. 可重复检查与 CI 门禁

一次性审计已固化为离线检查脚本 [check-route-conformance.py](../../scripts/dev-env/check-route-conformance.py)，
入口为 `make contracts-route-conformance`。它只读取当前工作树的契约与实现，不设置 `CONTRACT_BASE_REF`、
不联网、不连接数据库、不启动 lifespan；声明 405 的操作另在进程内发送 ASGI 请求验证响应信封。

检查内容：

1. 用 `create_app(settings=...)` 装配真实应用（合成 `Settings`，只构造路由），读取实际注册的
   `(method, path, operationId)`，包括 FastAPI 延迟 include 的路由。
2. 与 `contracts/speech-openapi.yaml` 对账：契约声明但未注册的 operationId、已注册但契约未声明的路由、
   operationId 对应的 method/path 形状。其他契约已声明的管理面路由单独标记为 `other-contract`，
   不混入语音面失败。
3. 逐条已注册路由静态提取状态码：`JSONResponse`/`Response`/`StreamingResponse`、
   `HTTPException`（含 `status_code=` 与 `HTTPException(418, ...)` 位置参数）、同模块 helper
   （含 `run_in_threadpool(helper, ...)` 直接调用与 `functools.partial` 绑定）与 `*_errors.py`
   的错误码映射；
   框架对错误方法产生的 405 单独计入。无法静态求值的 `status_code` 或运行时错误码列入
   「UNDETERMINED」，只报告可能值，不猜造、不作为门禁失败。
4. 显式基线：`KNOWN_UNIMPLEMENTED_OPERATIONS` 固定当前未实现操作清单（条数随实现变化，以脚本条目为准）并逐条写明原因；
   `KNOWN_UNDECLARED_STATUSES` 经 #462 收缩为空（原 16 个操作 / 23 个状态对），
   任何新增未声明状态都必须修复，不能加入旧基线。
   【#473】未实现基线不再以「未注册」为证据：门禁解析
   `unimplemented.py::UNIMPLEMENTED_OPERATIONS` 与 `unimplemented_errors.py::_ERRORS`，要求
   **(a)** 基线集合与占位表逐条相等（`unimplemented_table_unknown` / `stale_unimplemented_baseline`），
   **(b)** 每条基线的路由存在且由其占位模块注册（`declared_not_registered` /
   `unimplemented_placeholder_drift`），
   **(c)** 该路由可静态观察到的状态恰好是 `{405, 503}`（多一个 200 或换成别的错误码都会失败），
   **(d)** 占位错误映射恰好是 `{operation_not_implemented: 503}`（`unimplemented_error_map_drift`）；
   占位 handler 传入的是常量而非字面量，门禁按「单条目工厂只可能返回该条目状态」静态判定，因此不依赖调用点的字符串。
   因此占位响应不会「通过」契约检查：实现该操作时必须同时删除占位条目与基线条目，否则门禁失败。
5. 每条声明 405 的操作在进程内发起一次 ASGI 请求，方法由 `methods_for_path` 从该路径未注册的方法中
   选出，要求返回包含 `code/type/message/param/request_id/retryable` 的公开错误信封且不携带裸 `detail`，
   非空 `Allow` 与 `code`、`retryable=False`，并回显请求的 `request_id`；
   缺失即 `method_not_allowed_envelope_missing`。

   **`evaluate()` / 报告层**：直接调用 `evaluate()`，若 `method_not_allowed_envelopes` 为 `None`
   或缺少某个操作的 key，会为缺少证据的操作产生 `method_not_allowed_envelope_missing` finding。
   将返回的报告交给 `format_report()`，对应行打印 `405-envelope=FAILED`，不输出 OK 尾巴。
   `evaluate()` 本身只返回报告，不决定进程退出码。

   **CI / CLI 入口层**：CI 的 `make contracts-route-conformance` 调用脚本入口，先执行 `build_report()`。
   探针返回 `None`、空映射或缺 key 时，`require_envelope_coverage()` 在调用 `evaluate()` 前抛出
   `RuntimeError`；脚本退出码为 **2**，stderr 输出 `route conformance check could not run:`，
   诊断包含 `refusing to report declared 405s as verified`。stdout 为空，不打印文本或 JSON 报告，
   因而 CI 日志中没有 `405-envelope=FAILED` 行或 OK 尾巴，门禁步骤失败。

   单测分别覆盖两层，并断言探测集合等于声明 405 的集合；partial mapping 用例只去掉 `getJob` 的 key，
   直接调用 `evaluate()`，确认 finding 精确且仅指向 `getJob`。

   `app.py` 不再维护「哪些路径有信封」的硬编码清单：
   到达路由方法拒绝分支的 405 会统一为信封，包含 `/docs`、`/healthz`；未注册路径仍返回 404。
   但未配置资产或 workspace 服务时 ingress 先返回 503（如 `workspace_service_unavailable`），不会到达 405 分支。
   资源面专用错误码（jobs/assets/artifacts/workspace/speech）仍由各自分支先选中。
   探针以只启用 ingress 的资产和 workspace 服务替身验证 405；任何业务方法调用立即失败。
   当前仅支持普通路径参数；Starlette converter（`{id:int}`、`{rest:path}`）、懒加载 `include_router`
   的非空 prefix，以及任何不是「无子路由 StaticFiles」的 `Mount`（含 `/widgets/{id}` 这类带参数的
   挂载前缀、FastAPI/Starlette 子应用和没有 `.routes` 的裸 ASGI 应用）在发送请求前显式报错（退出码 2），
   错误信息点名被拒形态，不静默按简化路径匹配，也不改写路由。`Mount` 先于子路由匹配且接受任意方法，
   `methods_for_path` 不会下降进去，探针会选中子路由已注册的方法并真正调用其 endpoint；`Mount.routes`
   又是 `getattr(base, "routes", [])`，裸 ASGI 应用没有该属性却接受前缀下所有路径，空列表不能证明安全，
   因此只放行无子路由的 `StaticFiles`。`Host` 同样无法建模，一并拒绝。
   探针期间另有 dispatch 计数守护：`DispatchGuard` 在探测窗口内统计普通 Starlette `Route.handle`
   （方法被接受后才计入，405 不计）、`Mount.handle`（未建模的挂载应用）与每个可达 FastAPI ASGI handler
   （直接路由的 `route.app` 与懒加载 include 的 effective context app；生成器 endpoint 直接调用
   `dependant.call`、不经过 `run_endpoint_function`，包装 ASGI handler 才能覆盖），计数必须为 0；
   一旦有请求到达 handler，门禁在接收探测结果前抛出 `RuntimeError`，退出码 2 且 stderr 明确写出
   `endpoint was invoked during probe` 与被派发的 `method path (route template)` 或挂载，不把副作用
   留到门禁结束后。安装由所有活动 guard 共享，最后一个退出时才恢复，重入或交错使用不会留下 hook。
6. 新增契约未注册操作、已注册未声明路由、未声明状态码、operationId 形状变化、405 响应信封缺失、
   endpoint 源码无法定位，或基线过期（操作已实现、状态已声明/不再可达、占位形状/归属/错误码漂移）
   都会使命令以退出码 1 失败。

用法与自测：

```sh
make contracts-route-conformance
uv run python scripts/dev-env/check-route-conformance.py --json
uv run pytest tests/unit/test_route_conformance.py tests/contract/test_ci_workflow.py -q --no-cov
```

复核基线 `3951718e` 执行结果：`findings: []`、`known_unimplemented` 21 条、
`known_status_gaps` 16 个操作，退出码 0。
【#473】`934855c3` + #473 执行结果：`findings: []`、声明 42 个操作 / 注册 42 个、
`known_unimplemented` 21 条（全部 `observed=405,503`）、`known_status_gaps` 0，退出码 0。
【#475】在 #473 之上加入 405 信封探测：42 条声明 405 的操作全部 `405-envelope=verified`，
`findings: []`，退出码 0；反例（注册 `/v1/nexus/widgets` 并在契约声明 405、保留旧的路径清单兜底）
得到 `method_not_allowed_envelope_missing`，退出码 1。

#471 候选执行结果：`findings: []`、`known_unimplemented` 20 条、
`known_status_gaps` 0 个操作，退出码 0，无 stale 条目。保留 #466 已清空的状态基线；
`listJobs` 的框架错误方法 405 已由当前契约声明，GET 的成功/参数/存储分支为 200/400/503。

CI 将 `make contracts-route-conformance` 作为 `python_contracts` 作业
（`Python: lint, types and contract compatibility`）的一步，在 Ruff/mypy 前、无 `CONTRACT_BASE_REF` 条件下执行；
`full`、`web`、`no-db` 三种范围都会运行。详见 [CI 验证范围](../validation/ci-validation.md)。

维护规则：实现基线中的操作后必须同时删除 `KNOWN_UNIMPLEMENTED_OPERATIONS` 条目与该操作在
`UNIMPLEMENTED_OPERATIONS` 中的占位，否则 `stale_unimplemented_baseline` / `unimplemented_table_unknown` 失败；
契约补充状态码后必须删除对应 `KNOWN_UNDECLARED_STATUSES` 条目，否则 `stale_status_baseline` 失败。
占位路由的形状、归属模块或错误码发生变化时 `unimplemented_placeholder_drift` /
`unimplemented_error_map_drift` 失败，不能靠改基线绕过。
新增偏离不得直接加入基线，应先修复或按审计补充理由与跟踪项。

边界：ASGI ingress（`AssetIngress`、`ProfileIngress`、`PronunciationIngress` 等）产生的状态码不在
静态集合内，但声明 405 的操作已通过进程内 ASGI 请求实测相关方法拒绝分支的状态码、信封和 `Allow`。
其他 ingress 状态码、框架 404/500、FastAPI 请求参数类型校验自动产生的 422、跨模块 helper 返回的
响应、真实引擎与对象存储的 200 内容仍需集成/验收测试覆盖。405 探测不读取存储、不调用业务服务；
它验证真实中间件栈与全局异常处理器的输出，不证明未配置部署可用，也不证明所有 ingress 状态可达。
「声明但未静态观察到」只输出、不判失败，因为静态缺失不等于运行时不可达。
endpoint 源码无法定位则相反：它使检查直接失败，不把空状态集当成通过。

### 10.1 实现占位操作时 503 应如何改

占位操作转为真实实现时，把该操作的 503 从
`$ref: "#/components/responses/OperationNotImplemented"` 改为内联响应，并同时满足：

- `Cache-Control` 保持 required，schema 只允许 `no-store` 指令。该头不能删除：
  占位响应表示能力当前不可用，响应不得进入共享缓存。
- `X-Should-Retry` 保持 required，取值由 `"false"` 改为 `"true"`：实现后的 503
  是容量或依赖不可用、可重试，而不再是「永久未实现」。
- `error.code` 换成真实的可重试错误码（如 `backend_unavailable`），不再声明
  `operation_not_implemented`；`error.type` 保留 `service_unavailable_error` 语义，
  `retryable` 与 `X-Should-Retry` 的取值一致。
- 响应体继续用 `allOf` 组合 `#/components/schemas/ErrorResponse`，只在内联分支声明
  该操作的错误码与可重试语义。

门禁拒绝以下形状：

- 继续引用共享组件，包括带 sibling 的 `$ref`：共享 503 可能携带为其他操作声明的
  保证，无法证明它就是本操作的替换。
- 缺少任一 required 头，或把 required 改成可选：头名和取值都是版本化响应保证，
  删除会按响应保证丢失处理。
- `Cache-Control` 的取值不是 `no-store` 指令：`public`、`max-age`、`x-no-store`、
  `no-store-please` 以及 `no-store, max-age=0` 这类多指令组合都不接受；非字符串
  取值、外部或不可解析的 `$ref` 也不能通过。
- `X-Should-Retry` 不是 `"true"`，包括仍为 `"false"`：永久未实现信号没有反转。
- 缺少 `allOf` + `ErrorResponse` 组合，或 `error.code` 仍声明
  `operation_not_implemented`。
- 仍指向占位组件但削弱其共享定义：`error.code`、`error.type`、`retryable` 必须分别
  精确为 `operation_not_implemented`、`service_unavailable_error`、`false`；把任一
  常量改成普通类型或其他字面量都会逐条报告。

不能只删头了事：两个 required 头属于既有响应契约，直接删除会按响应保证丢失被拒绝，
应先改成实现后的真实语义。规则实现见
[HTTP 兼容检查规则](../validation/http-contract-change-checks.md#replacing-the-central-placeholder-503)，
正反用例见 `tests/contract/test_http_contract_changes.py`。

## 11. 引用与结论复核记录（3951718e）

对 8 条漂移引用逐条重新定位并复核结论。原引用是 `62ae4805` 的行号；新锚点是当前 HEAD 的符号，
不随行号漂移。结论按"成立 / 部分过期 / 已过期"判定，过期项不删改原文，只在 §1/§5.4/§6/§9 就近标注。

| # | 原引用（62ae4805） | 结论（基线） | 当前锚点（3951718e） | 复核结论 |
| --- | --- | --- | --- | --- |
| 1 | `backend/src/speechnexus_backend/speech/interfaces/http/jobs.py:293-298` | `createJob` 与契约一致 | `backend/src/speechnexus_backend/speech/interfaces/http/jobs.py::create_job_router.create_job`（`_create`） | **成立**：202/400/409/422/503 可达，401/403/429 仅声明；405 仍按 §5.3 单列 |
| 2 | `backend/src/speechnexus_backend/speech/interfaces/http/jobs.py:300-306` | `getJob` 一致 | `backend/src/speechnexus_backend/speech/interfaces/http/jobs.py::create_job_router.get_job`（`_read`） | **成立**：200/404/503 可达 |
| 3 | `backend/src/speechnexus_backend/speech/interfaces/http/jobs.py:308-334` | `cancelJob` 返回契约未声明的 400 | `backend/src/speechnexus_backend/speech/interfaces/http/jobs.py::create_job_router.cancel_job`（`_cancel`） | **成立**：400 未声明且可达（§7.1 R3） |
| 4 | `backend/src/speechnexus_backend/speech/interfaces/http/jobs.py:316-317` | 非法 `Idempotency-Key` → `invalid_job_request`（400） | `backend/src/speechnexus_backend/speech/interfaces/http/jobs.py::_idempotency_key`、`backend/src/speechnexus_backend/speech/interfaces/http/jobs.py::create_job_router.cancel_job` | **成立**：键校验先于服务调用，仍返回 400 |
| 5 | `backend/src/speechnexus_backend/speech/interfaces/http/jobs.py:171-228` | `createJob` 无 403/429 产生路径 | `backend/src/speechnexus_backend/speech/interfaces/http/jobs.py::_create` | **成立**：`_create` 无 Host/Origin 拒绝与限流分支 |
| 6 | `backend/src/speechnexus_backend/speech/interfaces/http/job_errors.py:9` | `invalid_job_request` 定义为 400 | `backend/src/speechnexus_backend/speech/interfaces/http/job_errors.py::_ERRORS`（`invalid_job_request`） | **成立**：映射未变 |
| 7 | `backend/src/speechnexus_backend/app.py:436-497` | 全局处理器把五前缀的 404/405 改写为稳定错误体 | `backend/src/speechnexus_backend/app.py::create_app.asset_http_error` | **成立**：五前缀与 404/405 分支仍在；行号已移至 454-515 |
| 8 | `backend/src/speechnexus_backend/app.py:475-485` | `/v1/nexus/jobs/*` 的 404 改写为 `job_not_found`，未注册的 `retryJob`/`streamJobEvents` 因此返回 404 | `backend/src/speechnexus_backend/app.py::create_app.asset_http_error`（`JOB_PREFIX` 分支） | **部分过期**："未注册 → 404"已过期，`#432` 已注册两条路由，404 现在表示任务不存在；"404 改写为 `job_not_found`"仍成立，未注册子路径继续被改写（§7.1 R4-R6） |

计数说明：issue 记为 `jobs.py` 6 条；实际为 `http/jobs.py` 5 条 + 同结论引用的 `http/job_errors.py:9` 1 条，
加上 `app.py` 2 条，共 8 条。另有 `backend/src/speechnexus_backend/workbench/infrastructure/public_speech_render_jobs.py:69,91`
（`listJobs` 消费者）在 HEAD 未漂移，已随 §6 一并改为 `PublicSpeechRenderJobs.submit`/`read_job` 符号锚点，不计入上述 8 条。
另发现 §3/§4/§6 注中的契约行号引用在 `0.1.0.dev6` 下已整体漂移，
本次一并改为 operationId/路径锚点（这些引用不在 §11 的 8 条之列）。
