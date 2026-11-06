# 公开语音面契约一致性审计

审计对象：`contracts/speech-openapi.yaml` `0.1.0.dev4` 与 `backend/src/speechnexus_backend/speech/interfaces/http/` 的 FastAPI 实现。
基线：`origin/main` `62ae4805`，分支 `codex/contract-conformance-audit`。
审计时间：2026-11-06（Asia/Shanghai）。审计为只读：不改产品代码、不改契约。

> 当前可重复门禁见 §10。`#432` 合并后 `retryJob`、`streamJobEvents` 已实现，契约已到 `0.1.0.dev5`，
> 因此 §6 的 25 条未实现操作在当前实现上收敛为 23 条。

## 1. 结论摘要

- 契约声明 39 条路径、**42 个操作**；后端注册 **17 个**，**25 个未注册**。
- 已实现的 17 个操作中，**4 个会返回契约未声明的状态码**：
  `getArtifact` 的 304/503、`downloadArtifactContent` 的 304/416/503、`cancelJob` 的 400、`createTranscription` 的 404。
- **25 个未注册操作**中，20 个返回框架默认 `404 {"detail":"Not Found"}`（错误体不符合 `ErrorResponse` 契约），
  `listJobs` 返回 `405 job_method_not_allowed`，`retryJob`/`streamJobEvents` 返回 `404 job_not_found`，
  `listPronunciationSets`/`createPronunciationRevision` 返回 `400 pronunciation_invalid_request`。
- **401 在全表都是"声明未使用"**：公开语音处理器没有实现共享服务密钥校验（`docs/architecture/openai-compatibility.md:91`），
  源码中不存在任何 401 产生路径。
- `createSpeech`/`createTranscription`/`createJob` 声明的 403、409、429 无产生路径；`listModels`、`retrieveModel`、
  `getCapabilities`、`getJob`、`cancelJob`、资产与制品读写均未声明 405，但框架会返回 405。

严重度最高的两条是消费者可直接触发的事故面：

1. `GET /v1/nexus/voices`（`listVoices`）与 `GET /v1/nexus/voices/{voice_id}/revisions/{revision}` 未注册，
   而工作台生产消费者 `backend/src/speechnexus_backend/workbench/infrastructure/public_voice_catalogue.py:31`
   以 `_VOICES_PATH` 调用这两个路径（列表 `:163`、固定 revision `:181`）；404 被映射为 `VoiceCatalogueUnavailableError`（同文件 `:106-109`），工作台音色目录因此固定不可用。
2. `GET /v1/nexus/jobs`（`listJobs`）与 `POST /v1/nexus/jobs` 共用同一路径；未注册的 GET 得到 `405 job_method_not_allowed`，
   而契约给 `listJobs` 声明的是 200/400/401/503，消费者无法据状态码区分"未实现"与"方法不支持"。

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

**不在覆盖范围**：真实引擎执行与 200 音频/转写内容、对象存储真实读写、部署层反向代理/鉴权/限流、
多实例并发下的 408/413/429 竞争窗口、WebSocket 升级（101）。

## 3. 已实现操作：声明 vs 可达

"静态可达"由 handler 分支、错误映射表、中间件与全局处理器合并得出；"(+405)"表示框架对错误方法返回 405，
多数路径契约并未声明该状态（见 §5.3）。

| # | 操作 | 路由 | 契约声明 | 实现证据 | 静态可达 | 未声明返回 | 声明未使用 | 结论 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | listModels | GET /v1/models | 200,304,401,503 | `models.py:41-61` | 200,304,503 | — | 401 | 一致 |
| 2 | retrieveModel | GET /v1/models/{model} | 200,304,401,404,503 | `models.py:63-86` | 200,304,404,503 | — | 401 | 一致 |
| 3 | createTranscription | POST /v1/audio/transcriptions | 200,400,401,403,409,413,422,429,503,504 | `transcriptions.py:516-522` | 200,400,404,413,422,503,504 | **404** | 401,403,409,429 | 未声明返回 |
| 4 | createSpeech | POST /v1/audio/speech | 200,400,401,403,404,405,409,422,429,503,504 | `speech.py:458-464` | 200,400,404,422,503,504 | — | 401,403,409,429 | 一致 |
| 5 | getCapabilities | GET /v1/nexus/capabilities | 200,304,401,404,503 | `models.py:88-120` | 200,304,404,503 | — | 401 | 一致 |
| 6 | listProfiles | GET /v1/nexus/profiles | 200,400,401,403,405,503 | `profile_publications.py:222-229` | 200,400,403,405,503 | — | 401 | 一致 |
| 7 | getProfileRevision | GET /v1/nexus/profiles/{profile_id}/revisions/{revision} | 200,400,401,403,404,405,409,503 | `profile_publications.py:225` | 200,400,403,404,405,409,503 | — | 401 | 一致 |
| 8 | listPublishedProfiles | GET /v2/nexus/profiles | 200,400,401,403,405,503 | `profile_publications.py:224` | 200,400,403,405,503 | — | 401 | 一致 |
| 9 | getPronunciationRevision | GET /v1/nexus/pronunciation-sets/{pronunciation_id}/revisions/{revision} | 200,400,401,403,404,405,429,503 | `pronunciations.py:116` | 200,400,403,404,405,429,503 | — | 401 | 一致（隔离 PG 实测 200/404/429/503） |
| 10 | createJob | POST /v1/nexus/jobs | 202,400,401,403,409,422,429,503 | `jobs.py:293-298` | 202,400,409,422,503 | — | 401,403,429 | 一致 |
| 11 | getJob | GET /v1/nexus/jobs/{job_id} | 200,401,404,503 | `jobs.py:300-306` | 200,404,503 | — | 401 | 一致 |
| 12 | cancelJob | POST /v1/nexus/jobs/{job_id}/cancellations | 200,202,401,404,409,503 | `jobs.py:308-334` | 200,202,400,404,409,503 | **400** | 401 | 未声明返回 |
| 13 | uploadAsset | POST /v1/nexus/assets | 201,400,401,403,404,408,409,410,413,422,429,503 | `assets.py:56-142` | 201,400,403,404,408,409,410,413,422,429,503 | — | 401 | 一致（404 仅有间接路径） |
| 14 | getAsset | GET /v1/nexus/assets/{asset_id} | 200,401,403,404,410,503 | `assets.py:144-158` | 200,403,404,410,503 | — | 401 | 一致 |
| 15 | downloadAssetContent | GET /v1/nexus/assets/{asset_id}/content | 200,206,401,403,404,410,416,503 | `assets.py:160-215` | 200,206,403,404,410,416,503 | — | 401 | 一致 |
| 16 | getArtifact | GET /v1/nexus/artifacts/{artifact_id} | 200,401,404,410 | `artifacts.py:88-114` | 200,304,404,410,503 | **304,503** | 401 | 未声明返回 |
| 17 | downloadArtifactContent | GET /v1/nexus/artifacts/{artifact_id}/content | 200,206,401,404,410 | `artifacts.py:116-189` | 200,206,304,404,410,416,503 | **304,416,503** | 401 | 未声明返回 |

契约行号：路径分别始于 `contracts/speech-openapi.yaml:38`(models)、`:75`(model)、`:117`(transcriptions)、
`:181`(speech)、`:252`(capabilities)、`:289`/`:323`/`:360`(profiles)、`:553`(pronunciation read)、
`:604`(jobs POST)、`:682`(jobs GET)、`:717`(job)、`:741`(cancellations)、`:942`(assets)、`:1003`/`:1031`(asset)、
`:1079`/`:1103`(artifact)。

## 4. 未声明返回：逐条证据

### 4.1 createTranscription → 404（契约未声明 404）

- 映射：`transcriptions.py:465-466` 将 `TranscriptionModelNotFoundError` 映射为 `model_not_found`；`transcription_errors.py:33` 给出 404。
- 产生路径：`application/transcription.py:281` 在模型未发布时抛出该异常。
- 契约：`contracts/speech-openapi.yaml:118` 起该操作只声明 200/400/401/403/409/413/422/429/503/504。
- 运行时：P10 返回 `404 {"code":"model_not_found"}`（§7）。

### 4.2 cancelJob → 400（契约只声明 200/202/401/404/409/503）

- `jobs.py:316-317`：`Idempotency-Key` 不合法时返回 `job_error("invalid_job_request")`；
  `job_errors.py:9` 定义为 400。
- 运行时：P9，`Idempotency-Key: short` → `400 invalid_job_request`。
- 同族问题：`createJob` 的同类错误合法（契约声明了 400），但取消路径漏声明，消费者按 200/202/404/409 分支处理会落入未知状态。

### 4.3 getArtifact → 304（契约未声明 304）

- `artifacts.py:112-113`：`If-None-Match` 命中时返回 `Response(status_code=304)`。
- 运行时：A2，先取 200 的 `ETag` 再回送 `If-None-Match`，实测 304 且无响应体。
- 对照：`listModels`/`retrieveModel`/`getCapabilities` 明确声明 304（`speech-openapi.yaml:38`/`:75`/`:252`），制品两条 GET 未声明，
  说明是遗漏而非统一约定。

### 4.4 getArtifact / downloadArtifactContent → 503（契约未声明 503）

- `artifacts.py:103-104`、`:132-133`：服务未装配时返回 `artifact_service_unavailable`；`artifact_errors.py:16,21` 均为 503。
- 运行时：P2/P2b（未配置制品服务）实测 503。
- 契约对这两条操作只声明 200/206/401/404/410，没有任何 5xx。

### 4.5 downloadArtifactContent → 416（契约未声明 416）

- `artifacts.py:146-156`：`artifact_range_not_satisfiable` 补 `Content-Range: bytes */<size>` 后返回 416；
  `artifact_errors.py:11` 定义为 416。
- 运行时：A3（`Range: bytes=0-1` + stub 抛该错误）实测 416。
- 对照：`downloadAssetContent` 声明了 416（`speech-openapi.yaml:1031`），制品内容路径漏声明。

## 5. 声明未使用与框架层偏差

### 5.1 401：全表未使用

公开语音处理器不校验共享服务密钥，源码中没有 401 的响应分支：唯一的 401 出现是 `artifacts.py:95,124` 的 OpenAPI 响应声明与工作台客户端的响应分类（`workbench/infrastructure/public_speech_render_jobs.py:83`），
`docs/architecture/openai-compatibility.md:91` 已明确"当前公开语音处理器没有实现该密钥的验证"。
17 条已实现操作全部声明 401，但均不可达；消费者不能把 401 当作访问保护已生效的证据。

### 5.2 403 / 409 / 429：仅部分路径可达

| 操作 | 声明的 403/409/429 | 是否有产生路径 | 证据 |
| --- | --- | --- | --- |
| createSpeech | 403,409,429 | 否 | `speech.py:340-443` 无对应分支，`/v1/audio/speech` 无入口中间件 |
| createTranscription | 403,409,429 | 否 | `transcriptions.py:442-501` 无对应分支 |
| createJob | 403,429 | 否 | `jobs.py:171-228` 无 host/origin 检查、无限流 |
| listProfiles / getProfileRevision / listPublishedProfiles / getPronunciationRevision | 403（+429 仅读音） | 是 | `profile_ingress.py:67-97`、`draft_ingress.py:64-67`、`pronunciations.py:146-149,167-169` |
| uploadAsset | 403,429（另 408,413） | 是 | `asset_ingress.py:52-73,98-99` |

### 5.3 405：多数路径未声明

405 由框架对错误方法产生，经 `app.py:436-497` 的全局处理器改写为稳定错误体（assets/artifacts/workspaces/jobs/speech 五个前缀），
其余前缀保留 FastAPI 默认 `{"detail":"..."}`。

- 已声明 405：`createSpeech`、三个 profiles 操作、`getPronunciationRevision`。
- 未声明但可产生 405：`listModels`、`retrieveModel`、`getCapabilities`、`createTranscription`、
  `createJob`、`getJob`、`cancelJob`、`uploadAsset`、`getAsset`、`downloadAssetContent`、`getArtifact`、
  `downloadArtifactContent`。
- 错误体不一致：`/v1/audio/transcriptions`、`/v1/models`、`/v1/nexus/capabilities` 等无前缀处理器的路径返回
  `{"detail":"Method Not Allowed"}`（运行时 P4），不含 `error.code/request_id`，不符合 `ErrorResponse`。

### 5.4 全局 404 语义被前缀处理器改写

`app.py:475-485` 把 `/v1/nexus/jobs/*` 下的任意 404 改写为 `job_not_found`。因此未注册的
`retryJob`（`speech-openapi.yaml:808`）与 `streamJobEvents`（`:839`）返回 `404 job_not_found` 而不是"路由不存在"，运行时 J2/J3 已复现。
`/v1/nexus/pronunciation-sets` 前缀被 `PronunciationIngress` 接管（`pronunciations.py:136-159`），未注册的集合级
GET/POST 在匹配失败时返回 `400 pronunciation_invalid_request`，运行时实测见 §7 的 U4/U5。

## 6. 未实现操作（25 条）

契约声明但后端未注册。运行时列给出当前真实响应（同一应用、无服务依赖）。

| # | 操作 | 路由 | 契约声明 | 运行时响应 |
| --- | --- | --- | --- | --- |
| 18 | listVoices | GET /v1/nexus/voices | 200,401,503 | 404 默认 `{"detail":"Not Found"}` |
| 19 | getVoiceRevision | GET /v1/nexus/voices/{voice_id}/revisions/{revision} | 200,401,404,503 | 404 默认 |
| 20 | retireVoiceRevision | POST /v1/nexus/voices/{voice_id}/revisions/{revision}/retirements | 200,400,401,404,409,422 | 404 默认 |
| 21 | listPronunciationSets | GET /v1/nexus/pronunciation-sets | 200,401,503 | **400 pronunciation_invalid_request** |
| 22 | createPronunciationRevision | POST /v1/nexus/pronunciation-sets | 201,400,401,409,422,429 | **400 pronunciation_invalid_request** |
| 23 | listJobs | GET /v1/nexus/jobs | 200,400,401,503 | **405 job_method_not_allowed** |
| 24 | retryJob | POST /v1/nexus/jobs/{job_id}/retries | 202,401,404,409,422 | 404 job_not_found |
| 25 | streamJobEvents | GET /v1/nexus/jobs/{job_id}/events | 200,401,404,410 | 404 job_not_found（非 text/event-stream） |
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

契约行号：`:397`(voices)、`:427`/`:452`(voice revision/retirement)、`:492`/`:515`(pronunciation sets)、
`:682`(jobs list)、`:808`(retries)、`:839`(events)、`:879`/`:903`(operations)、`:1145`-`:1259`(realtime)、
`:1320`-`:1561`(voice creation)、`:1610`-`:1704`(deletions)。

这些缺失不是"契约先行、消费者未接入"：`public_voice_catalogue.py:31` 已在生产装配路径上调用 `/v1/nexus/voices`，
`public_speech_render_jobs.py:69,91` 调用 `/v1/nexus/jobs` 与 `GET /v1/nexus/jobs/{job_id}`（后者已实现）。
`docs/archive/plans/2026-10-24-team-c-goal.md:78` 也记录了"音色运行时尚未实现这些路由"这一已知缺口。

## 7. 运行时证据

装配：`create_app(settings=...)`，只注入必要依赖；`Host: localhost` 以通过入口的 Host 校验。
探测脚本位于工作树外的 `/tmp/sn-audit-probe/`，不进入仓库。

| 编号 | 请求 | 实测 | 说明 |
| --- | --- | --- | --- |
| P1 | GET /v1/nexus/voices | 404 `{"detail":"Not Found"}` | 未注册；错误体不符合契约 |
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
| J1 | GET /v1/nexus/jobs | 405 `job_method_not_allowed` | **listJobs 未实现**，与 POST 同路径 |
| J2 | POST /v1/nexus/jobs/job-demo/retries | 404 `job_not_found` | 未实现；错误码误导为"任务不存在" |
| J3 | GET /v1/nexus/jobs/job-demo/events | 404 `job_not_found` | 未实现；SSE 消费者拿到 JSON |
| R1 | GET /v1/nexus/realtime/sessions/s-1 | 404 `{"detail":"Not Found"}` | 未注册 |
| V1 | POST /v1/nexus/voice-drafts/d-1/previews | 404 `{"detail":"Not Found"}` | 未注册 |
| U1 | GET /v1/nexus/voices/voice-1/revisions/2 | 404 默认 | 未注册 |
| U2 | POST .../retirements | 404 默认 | 未注册 |
| U3 | GET /v1/nexus/operations/op-1 | 404 默认 | 未注册 |
| U4 | GET /v1/nexus/pronunciation-sets | **400 pronunciation_invalid_request** | 未注册集合级路由被入口中间件改写 |
| U5 | POST /v1/nexus/pronunciation-sets | **400 pronunciation_invalid_request** | 同上 |
| U6 | POST /v1/nexus/deletions 等 3 条删除路由 | 404 默认 | 未注册 |

隔离 PG 正向对照：`SPEECHNEXUS_TEST_DATABASE_URL=postgresql:///sn_profile_it` 下
`tests/integration/test_pronunciation_http.py` 13 项通过（`-k "not generated_consumers"`），
覆盖该路由的 200/404/429/503；说明"已声明且已实现"的路由确实按契约工作。

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
2. **P0**：让未实现路由返回可辨识的稳定错误（而非默认 404 detail、405、400 或 `job_not_found`），
   避免消费者把"未实现"误判为"参数错误/资源不存在"。
3. **P1**：补齐 `getArtifact`/`downloadArtifactContent` 的 304、416、503 声明；补齐 `cancelJob` 的 400、
   `createTranscription` 的 404。
4. **P1**：为 `/v1/audio/transcriptions`、`/v1/models`、`/v1/nexus/capabilities` 等路径补 405 声明与统一错误体，
   或在全局处理器中覆盖这些前缀。
5. **P2**：决定 401 的处置：实现共享服务密钥校验，或从公开契约移除 401 并把"无鉴权"写进部署前置条件。
6. **P2**：清理 `createSpeech`/`createTranscription` 的 403/409/429 与 `createJob` 的 403/429 声明（无产生路径），
   或补上对应入口能力。

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
## 10. 可重复检查与 CI 门禁

一次性审计已固化为离线检查脚本 [check-route-conformance.py](../../scripts/dev-env/check-route-conformance.py)，
入口为 `make contracts-route-conformance`。它只读取当前工作树的契约与实现，不设置 `CONTRACT_BASE_REF`、
不联网、不连接数据库、不启动 lifespan。

检查内容：

1. 用 `create_app(settings=...)` 装配真实应用（合成 `Settings`，只构造路由），读取实际注册的
   `(method, path, operationId)`，包括 FastAPI 延迟 include 的路由。
2. 与 `contracts/speech-openapi.yaml` 对账：契约声明但未注册的 operationId、已注册但契约未声明的路由、
   operationId 对应的 method/path 形状。其他契约已声明的管理面路由单独标记为 `other-contract`，
   不混入语音面失败。
3. 逐条已注册路由静态提取状态码：`JSONResponse`/`Response`/`StreamingResponse`、
   `HTTPException`（含 `status_code=` 与 `HTTPException(418, ...)` 位置参数）、同模块 helper
   （含 `run_in_threadpool(helper, ...)` 间接调用）与 `*_errors.py` 的错误码映射；
   框架对错误方法产生的 405 单独计入。无法静态求值的 `status_code` 或运行时错误码列入
   「UNDETERMINED」，只报告可能值，不猜造、不作为门禁失败。
4. 显式基线：`KNOWN_UNIMPLEMENTED_OPERATIONS` 固定当前 23 条未实现操作并逐条写明原因；
   `KNOWN_UNDECLARED_STATUSES` 固定当前 14 个操作的已知未声明状态，其中非 405 项为
   `createTranscription` 404、`cancelJob` 400、`getArtifact` 304/503、
   `downloadArtifactContent` 304/416/503，其余为缺失的 405 声明。
5. 新增契约未注册操作、已注册未声明路由、未声明状态码、operationId 形状变化、endpoint 源码
   无法定位，或基线过期（操作已实现、状态已声明/不再可达）都会使命令以退出码 1 失败。

用法与自测：

```sh
make contracts-route-conformance
uv run python scripts/dev-env/check-route-conformance.py --json
uv run pytest tests/unit/test_route_conformance.py tests/contract/test_ci_workflow.py -q --no-cov
```

CI 将 `make contracts-route-conformance` 作为 `python_contracts` 作业
（`Python: lint, types and contract compatibility`）的一步，在 Ruff/mypy 前、无 `CONTRACT_BASE_REF` 条件下执行；
`full`、`web`、`no-db` 三种范围都会运行。详见 [CI 验证范围](../validation/ci-validation.md)。

维护规则：实现基线中的操作后必须同步删除条目，否则 `stale_unimplemented_baseline` 失败；
契约补充状态码后必须删除对应 `KNOWN_UNDECLARED_STATUSES` 条目，否则 `stale_status_baseline` 失败。
新增偏离不得直接加入基线，应先修复或按审计补充理由与跟踪项。

边界：ASGI ingress（`AssetIngress`、`ProfileIngress`、`PronunciationIngress` 等）产生的状态码、
框架 404/500、FastAPI 请求参数类型校验自动产生的 422、跨模块 helper 返回的响应、真实引擎与
对象存储的 200 内容不在静态集合内；这些路径仍由集成/验收测试覆盖。
「声明但未静态观察到」只输出、不判失败，因为静态缺失不等于运行时不可达。
endpoint 源码无法定位则相反：它使检查直接失败，不把空状态集当成通过。
