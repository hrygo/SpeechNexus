# 声音草稿创建、修订生成、预览、校验与元数据

对应 [#494](https://github.com/hrygo/SpeechNexus/issues/494)、
[#514](https://github.com/hrygo/SpeechNexus/issues/514) 与
[#495](https://github.com/hrygo/SpeechNexus/issues/495)。公开源为
`contracts/speech-openapi.yaml` 与 `contracts/voice-resource.schema.json`；当前实现：

| 操作 | 成功响应 |
| --- | --- |
| `POST /v1/nexus/voice-designs` | 202，`VoiceCreationAccepted` |
| `POST /v1/nexus/voice-clones` | 202，`VoiceCreationAccepted` |
| `GET /v1/nexus/voice-drafts/{draft_id}` | 200，`VoiceDraftView` |
| `PATCH /v1/nexus/voice-drafts/{draft_id}` | 200，`VoiceDraft` |
| `POST /v1/nexus/voice-drafts/{draft_id}/generations` | 202，`VoiceGenerationAccepted` |
| `POST /v1/nexus/voice-drafts/{draft_id}/previews` | 202，`Job` |
| `POST /v1/nexus/voice-drafts/{draft_id}/validations` | 202，`Job` |

创建请求保留现有 `model`、`input`、可选 workspace/client reference/constraints。
clone 必须明确 `source_use_confirmed: true`，输入素材须是对应 workspace 中 ready 的
`voice_reference`。未知字段、无效请求返回 400。Host/Origin 使用现有部署白名单检查，
拒绝返回 403；未找到草稿返回 404。创建、读取、元数据的响应均由生成 HTTP 校验器检查。

落库的 `input_json` 是修订回显的 UTF-8 JSON 文档：服务端以 `ensure_ascii=False`
序列化并按 UTF-8 字节数校验 65536 上限，与 HTTP 入站上限和数据库 CHECK 使用同一
长度口径。中文与非 BMP 文本按其在 wire 上的 UTF-8 大小计，不因 ASCII 转义膨胀；
写入口在提交前拒绝超出上限的存储文档，避免先写 Job 再回滚。

Python 消费入口为 `speechnexus_contracts.voice_drafts_http`，TypeScript 为
`@speechnexus/contracts/voice-drafts-http`。这两个入口选择上述七条方法；发布操作仍由后续切片实现。Python HTTP 用例消费生成校验器；前端实际页面接入仍需消费者验证。

## 协议升级

speech source 当前为 `0.1.0.dev11`（#514 从 `dev9` 升至 `dev10`，#495 升至
`dev11`）。七条由占位变为实现后，503 从
`OperationNotImplemented` 换成与 `retireVoiceRevision` 同形的内联声明：`error.code` 为
`voice_draft_unavailable` / `voice_draft_outcome_unknown`，保留必需的
`Cache-Control: no-store` 与 `X-Should-Retry: true`，`retryable` 为 `const true`。
`check-openai-contract-compat.py` 允许基线为占位 503 的 response 整体替换，前提是替换物
仍是带必需缓存头与 `ErrorResponse` 信封的可重试 503；七条因此通过兼容门禁，不需要放宽
基线或修改检查器。

创建入参的 `language` 按请求契约执行：`type: string`、`maxLength: 35`，不按
`LanguageTag` 正则拒绝大小写或标签形状；`EN-US`、`en-US`、`zh-CN`、`auto`、`english`
均可创建。落库与修订回显使用同一规范化：主语言子标签转小写（`EN-US` → `en-US`），
无法表示为公开 `LanguageTag` 的值回退为 BCP 47 `und`（`english`、`x!`）；原始请求仍保留
在 Job intent 中。这样请求契约不拒绝调用方，响应也不会违反既有 `LanguageTag`。
空白字符串和超过 35 字符的输入在提交前 400 且不落库。七条另声明
`500 voice_draft_contract_violation`（`api_error`、`retryable: false`）：自产响应违反契约时
按实现缺陷返回 500，而不是伪装成可重试 503；错误响应本身也过同一生成校验器。新增声明
Host/Origin 403，以及草稿读取的无效输入 400。旧消费者应先升级生成包和 503/500 判定，
再启用服务。

## 修订生成与校验

`generateVoiceDraftRevision` 在草稿行锁内比较 `expected_draft_revision` 与当前 head：
同一事务内追加下一条 `speech_voice_draft_revisions`，同时写入生成 Job、提交行、
admission、协调 Job 与 outbox，并把草稿 head 指向新修订。修订行只追加；`(draft_id,
revision)` 为主键，`generation_job_id` 唯一，输入和 binding 固定保存。草稿已发布、
`preview_only`、kind 不符或 head 不一致分别返回 409，缺失草稿返回 404。输入按草稿 kind
校验，不能把 clone 输入提交到 design 草稿。

带 `Idempotency-Key` 的生成先按 key 查原 Job 与本草稿的修订映射，再进入任何 binding 或
草稿锁。因此即使 head 已推进，同键重放仍返回原 Job 与原修订，不追加第二条修订。该顺序
是恢复语义的一部分，不能把重放移到草稿锁之后。

`validateVoiceDraftRevision` 只接受固定候选修订：目标必须是当前 head，生成 Job 已成功，
且结果的 `receipt_id` 与 ready preview Artifact 全部匹配；否则 409（stale）或 422
（`voice_draft_not_candidate`）。校验 Job 以 `voice_validation` kind 提交，pinned 到该修订
记录的 binding；省略 `model` 时从 binding 的 `logical_model` 推导，显式不一致则 422。
`validation_set_revision` 是调用方固定的不透明修订号，本切片不维护校验集目录。

校验结果读取走 `PublicJobs.artifacts`：Job 必须 succeeded，结果引用
`validation_report_artifact_id` 与 `receipt_id`，并存在同 workspace、ready、
`validation_report` kind 且回执匹配的 Artifact；单读与分页批读共用该规则。失败、取消或
过期 Job 本身仍按 Job 契约返回 named failure，不伪造校验通过。

`createVoicePreview` 只预览当前 head 上的候选声音：draft 必须存在且 kind 为
`design`/`clone`（否则 404/400），`expected_draft_revision` 必须等于 head（否则 409
`voice_draft_conflict`），关联 generation Job 必须 succeeded 且结果的 receipt 与全部
ready preview Artifact 匹配（否则 422 `voice_draft_not_candidate` 或 503
`voice_draft_unavailable`）。`text` 的长度 1..2000 由请求契约在入站处校验，越界返回 400。
预览 Job 以 `voice_preview` kind 提交，输入固定为
`{voice_draft_id, draft_revision, text}`，binding 取目标修订已发布绑定的 pinned 引用；
省略 `model` 时从绑定 `logical_model` 推导，显式不一致返回 422
`voice_draft_input_unavailable`。

`voice_preview` Job 停在 `pending`，本切片不派发、不产音频；`DISPATCHABLE_JOB_KINDS`
仍为 `("asr", "tts")`，kind 过滤让预览行不占批次也不毒化同批 ASR/TTS。预览执行消费者
仍待后续切片。读取按单数
`preview_artifact_id` 权威回读：Job 必须 succeeded，Artifact 必须同 workspace、ready、
`preview` kind 且 receipt 匹配；缺失或非 ready 时 `GET /v1/nexus/jobs/{job_id}` 与列表
返回 503，不把伪造结果投影成成功。生成结果的复数 `preview_artifact_ids` 规则不变，
两条 resolver 不共用字段名。

带 `Idempotency-Key` 的预览先在无锁早查重中按 key 查原 Job，因此 head 推进后同键重放仍返回
原 Job，不新增第二个预览 Job。若早查重与并发提交竞争而落空，`PublicJobs.submit` 会在
`_submit_new` 因 stale/not_candidate 失败后再查一次原键；此时 store 可能已经取过并释放
binding 与草稿行锁，所以回退不保证在“任何行锁之前”，但重放仍优先于返回冲突。新建路径在
草稿行锁内重新比较 head 并要求 generation Job succeeded；只有确实要新建时才进入该判定。

通用 `POST /v1/nexus/jobs` 可提交 `voice_validation`，但重试带 `draft_target` 的生成 Job
会被拒绝，避免把修订追加误当成新草稿生成。

## 插入窗口拒绝

store 在 prepare 完成后、插入事务提交前可能拒绝已准备的写入。拒绝原因分为三类，HTTP
边界按原因返回不同状态，不能统一当成「事实已变」或可重试 outage：

- `facts_changed`（store code `job_rejected`）：固定 binding 不再是选中版本，或候选
  preview 证据不再 ready。事务已回滚，没有任何 Job、修订、草稿或 outbox 写入提交；
  重放同一请求不能成功。voice 的创建、生成、预览与校验四条路径声明了 409，因此映射为
  既有稳定 code `voice_draft_conflict`（`type: invalid_request_error`、
  `retryable: false`、`Cache-Control: no-store`、`X-Should-Retry: false`）。
- `identity_conflict`（store code `job_identity_conflict`）与 `revision_exhausted`：
  保留 409 映射，但 `identity_conflict` 只在键值 UUID 碰撞等极端构造下出现。
  无键提交的 `key_digest` 为 NULL、`job_id` 是 uuid4，唯一约束不会因此冲突，所以它不是
  正常可复现路径；`revision_exhausted` 表示 head 修订号已耗尽。两者同样没有提交写入，
  重放同一请求不能成功，按 409 `voice_draft_conflict` 返回；重新发起会获得新身份，
  不标记可重试。
- `storage_rejected`（确定性数据库拒绝，如 CHECK 违约）与 `contract`（`created` 结果缺少
  Job/修订载荷）：属于确定性实现缺陷，不是客户端冲突，返回 500
  `voice_draft_contract_violation`（`type: api_error`、`retryable: false`、
  `X-Should-Retry: false`）。

提交事务窗口内的连接故障（连接池关闭、获取超时、`TooManyRequests` 等）不是上述拒绝
原因，也不是 `voice_draft_conflict`：它们与 `psycopg.OperationalError` 同类，store 无法
证明事务没有提交，因此统一返回 `unknown`。voice 创建与生成再按是否携带原
`Idempotency-Key` 返回 503 `voice_draft_outcome_unknown`（`retryable: true`）或 409
`voice_draft_outcome_unknown`（`retryable: false`）。`voice_draft_unavailable` 只用于
提交前已确定没有写入的 prepare/read 故障。

`getVoiceDraft` 与 `updateVoiceDraftMetadata` 不提交 Job，不会出现上述拒绝；若该异常
仍到达这两条路径，按实现缺陷返回 500，而不是可重试的 503。

通用 `POST /v1/nexus/jobs` 对同一 store 结果保持 503 `job_unavailable` 且
`retryable: false`。两条路径的差异来自已声明的响应集合：voice 操作声明了 409 冲突语义与
500 契约违约，通用 Job 面没有对应的「事实已变」冲突 code，因此保留现有的不可重试 503；
两者都不承诺重试可以成功。

## 权威状态与恢复

`VoiceDrafts.create` 调用既有 `PublicJobs.submit`。沿用其 workspace 作用域幂等键锁、
完整请求摘要、固定 binding/configuration、Job 提交文档、协调 Job 和 outbox。
草稿与生成输入在该同一事务插入；草稿失败会回滚整笔 Job 提交，没有第二套派发。
草稿 ID 由已接受的 Job 身份固定，因此重启和同键重放能找到原草稿。同键异意图返回 409。
幂等键可选；缺键的新请求是新意图。提交结果未知时保留原请求与键进行协调，不换键重试。

草稿创建时 head revision 为 `"1"`，生成追加后 head 递增；每次修订关联自己的权威
generation Job。读取以 head 关联的 Job 为据：成功且结果的 kind/draft ID/revision 匹配时
投影 `candidate`，
失败、取消或过期投影 `failed`，并在 `revision.failure` 给出稳定 code 与固定文案；Job 没有
`error_code` 时按取消/过期分别给出 `voice_generation_cancelled` / `voice_generation_expired`。
`candidate` 不信任结果 JSON 自述：投影回读 `PublicJobs.artifacts`，要求非空 preview ID、
匹配的 `receipt_id` 与 ready 的 preview Artifact；缺失、错误归属或非 ready 时不产出
候选，而是按存储结果不可用返回 503。`?draft_revision=` 只匹配当前 head；非 head 取值
返回 404，不回退到 head。旧修订通过生成回放或校验响应读取。元数据编辑不会生成新修订或改变
输入、binding、Job。
完整状态枚举继续来自原契约；本切片不提供任意状态修改端口。

PATCH 只接受 `expected_metadata_revision` 和 `display_name`。PG 草稿行锁内先查原键回执，
再检查发布状态和 metadata revision CAS，然后**在事务内**用与 GET 共用的
`resolve_voice_previews` 校验 candidate 的 receipt 与 ready preview；校验失败在 UPDATE
和回执写入之前抛出并回滚，未验证候选不会留下 display_name、metadata revision 或回执。
CAS 冲突、已发布草稿或同键异意图返回 409；原回执重放也先按当前权威行校验 candidate，
再返回记录中的草稿投影，不受后来 metadata 版本影响。回执只保存公开 `VoiceDraft` 投影，
不复制 `revision.input`/binding；最长 36KB 输入带键 PATCH 仍为 200，回执保持在 8192 字节
CHECK 内。尝试通过 PATCH 改状态、输入或生成结果返回 422，且不写入。

`speech_voice_metadata_commands` 是唯一 metadata 回执表，`result_json` 只存上述投影；
`speech_voice_drafts.input_json` 是权威输入本身（DDL 允许 2..65536 字节），不是回执，
`binding_json` 上限 1024，二者都不复制到回执。创建路径没有第二份整输入回执。

`POST /v1/nexus/jobs` 的 `VoiceDesignJobCreate` / `VoiceCloneJobCreate` 同样通过
`PublicJobs.submit` 接入：202 返回 Job，并在同一事务创建一条草稿；草稿 ID 固定为
`draft-` + Job ID，可用 `/v1/nexus/voice-drafts/{draft_id}` 读取。它与专用创建入口共享
幂等键语义：同键同意图回放原 Job 与原草稿，不新增行；专用入口只是额外在 202 中返回
`draft` 投影。需要草稿投影时优先用专用入口，通用入口适合已经按 Job 编排的调用方。

创建提交的失败按“是否已确定未提交”区分：

- `voice_draft_unavailable`（503，`retryable: true`，`X-Should-Retry: true`）表示提交前的
  fact 读取或 prepare 存储失败，此时尚未进入插入事务，可以确定没有写入；客户端可用原请求
  原键重试。
- `voice_draft_outcome_unknown`（503，`retryable: true`，`X-Should-Retry: true`）只在请求
  携带原 `Idempotency-Key` 时返回：提交可能已 commit 但确认丢失，客户端用同一键重放会
  回放原草稿与原 Job，不会新建。不同键仍是新意图。
- 无 `Idempotency-Key` 的请求无法安全重放，因此同一未知窗口返回 409
  `voice_draft_outcome_unknown`（`retryable: false`、`X-Should-Retry: false`）：重发会被
  当作新意图，可能再建草稿与生成任务；客户端应先协调原请求，不能把它当普通 503 重试。

这一区分同时约束契约和生成包，不通过放宽 503 重试声明来换取占位门禁通过。

公开响应不含引擎句柄、特征向量、音频 base64、对象键或内部模型路径；错误使用固定文案。
生成、修订、预览与校验已由本组件提供；发布端口仍由后续切片实现。Job/outbox 接受不等于真实模型已执行；
现有生产 dispatch planner、真实引擎、资源准入和目标拓扑资格仍须各自验收。

草稿的生成 Job 仍写入协调 Job 与 outbox。默认 dispatch lane 仍只领取 ASR/TTS：
`PostgresDispatchQueue` 的 `kinds` 默认 `DISPATCHABLE_JOB_KINDS`（`("asr", "tts")`）。
voice lane 是可选消费者，调用方显式传 `kinds=VOICE_GENERATION_JOB_KINDS` 才领取
`voice_design`/`voice_clone`。kind 过滤在同一条 claim SQL 内完成，不占默认批次上限，
也不会在行转换时抛 `DispatchProtocolError` 毒化同批 ASR/TTS。生产装配（planner、引擎
适配器、资源准入与超时结算）归 [#528](https://github.com/hrygo/SpeechNexus/issues/528)
（原生父 [#340](https://github.com/hrygo/SpeechNexus/issues/340)）；当前没有默认启动的
voice 消费者。

## 迁移与生产装配

`0043_voice_draft_revisions` 接在 `0042` 之后，增加
`speech_voice_draft_revisions`，把既有草稿的当前 head 回填为 revision 1，并将 Job kind
CHECK 扩展到 `voice_validation`。`0044_voice_preview_jobs` 只把同一 CHECK 再扩展到
`voice_preview`，不改表、索引或授权；`alembic heads` 只有 0044。两版 downgrade 都先恢复
窄 CHECK：有 validation 或 preview Job 时数据库拒绝，或迁移显式报出
`voice_preview` 行数，防止静默丢失历史。

`create_app` 在既有 `JobRuntime` 里用同一 PG pool 装配 `VoiceDrafts` 与
`PostgresVoiceDrafts`。沿用 `SPEECHNEXUS_JOB_WRITER_DATABASE_URL` 和显式 schema。
部署先应用迁移，再由 owner 审核 `voice_draft_grants`、`public_job_grants` 及原 Job
提交权限。运行角色对修订表只有 SELECT/INSERT；草稿表仅能 UPDATE display name、metadata revision、
更新时间、head revision 和 generation Job；不授予 status/input/binding 更新或历史删除。
启动校验列级权限，并对修订表逐列拒绝 UPDATE，同时拒绝 DELETE；不执行 DDL。
旧 Job writer 必须同步迁移和授权后升级；未配置 provider 时七条返回真实 503。

`voice_validation` 的 target binding revision 必须在 `definition` 中声明
`logical_model`，且 `tasks` 包含 `voice_validation`；缺失或未发布时提交返回 422
`model_binding_not_published`，任务不支持时返回 422 `model_binding_task_not_supported`。
`POST /v1/nexus/jobs` 的泛化 validation 入口同样从候选修订注入该 binding，调用方无需自行
选择 revision；不存在的修订返回 404，陈旧 head 返回 409，非候选返回 422。

## 验证与接收边界

`tests/unit/backend/test_voice_creation_jobs.py` 与 `test_voice_drafts_http.py` 覆盖 Job 重放、
生成校验器、严格输入、Host/Origin、状态修改拒绝、错误区分、candidate 回执/Artifact 反例、
精确修订查询、修订提交确认丢失的 503 形状，以及创建确认丢失时带键回放与无键 409 的分支。
`tests/integration/test_voice_drafts_pg.py` 使用调用方隔离 DSN、随机 schema 与 finally DROP，
覆盖迁移、创建/读取/推进、元数据 CAS/重放、并发同键、Job 回滚、候选阈值、中文/非 BMP
字节边界、voice+ASR/TTS 混合派发、修订追加/回放/陈旧 head、校验目标与报告读取、
泛化 Job 的 binding 注入与 404/409/422、损坏校验结果读取为 503，以及正式 lifespan 权限
装配。`test_voice_draft_revisions_migration_pg.py` 单独验证 0043 回填、同一 schema 内 kind
CHECK 过滤，以及有 validation Job 时 downgrade 拒绝且保留 0043。
`test_voice_draft_previews_migration_pg.py` 验证 0044 只扩 kind、upgrade/downgrade/upgrade
往返，以及存在 preview Job 时 downgrade 明确报错且保留 0044。预览用例覆盖同键重放跨 head
推进、stale/unknown draft/unknown revision、候选与 receipt/ready 门槛、HTTP 400/404/409/422
形状，以及 succeeded 预览 Job 在 GET 与列表上的单数 Artifact 投影。
缺 `SPEECHNEXUS_TEST_DATABASE_URL` 时 pytest.fail；不能把缺配置当作通过。

本轮在隔离 PostgreSQL 上实际执行了草稿、混合派发与 HTTP 用例；这是单库实测，不等于
双库或目标部署验收。真实生成、Linux/CUDA、双库和正式页面接入仍未据此验收。路由门禁
未实现基线由 **13 降至 12**（本切片移除 `createVoicePreview`），契约中指向占位组件的 503 由
9 降至 8。
历史审计保留原时点记录，当前结果以门禁输出为准。

验证入口：

```sh
make contracts-route-conformance
make contracts-check contracts-drift
uv run pytest tests/unit/backend/test_voice_creation_jobs.py tests/unit/backend/test_voice_drafts_http.py
SPEECHNEXUS_TEST_DATABASE_URL=... uv run pytest tests/integration/test_voice_drafts_pg.py
SPEECHNEXUS_TEST_DATABASE_URL=... uv run pytest tests/integration/test_voice_draft_previews_migration_pg.py
```
