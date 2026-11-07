# 声音草稿创建、读取与元数据

对应 [#494](https://github.com/hrygo/SpeechNexus/issues/494)。公开源为
`contracts/speech-openapi.yaml` 与 `contracts/voice-resource.schema.json`；本切片实现：

| 操作 | 成功响应 |
| --- | --- |
| `POST /v1/nexus/voice-designs` | 202，`VoiceCreationAccepted` |
| `POST /v1/nexus/voice-clones` | 202，`VoiceCreationAccepted` |
| `GET /v1/nexus/voice-drafts/{draft_id}` | 200，`VoiceDraftView` |
| `PATCH /v1/nexus/voice-drafts/{draft_id}` | 200，`VoiceDraft` |

创建请求保留现有 `model`、`input`、可选 workspace/client reference/constraints。
clone 必须明确 `source_use_confirmed: true`，输入素材须是对应 workspace 中 ready 的
`voice_reference`。未知字段、无效请求返回 400。Host/Origin 使用现有部署白名单检查，
拒绝返回 403；未找到草稿返回 404。创建、读取、元数据的响应均由生成 HTTP 校验器检查。

落库的 `input_json` 是修订回显的 UTF-8 JSON 文档：服务端以 `ensure_ascii=False`
序列化并按 UTF-8 字节数校验 65536 上限，与 HTTP 入站上限和数据库 CHECK 使用同一
长度口径。中文与非 BMP 文本按其在 wire 上的 UTF-8 大小计，不因 ASCII 转义膨胀；
写入口在提交前拒绝超出上限的存储文档，避免先写 Job 再回滚。

Python 消费入口为 `speechnexus_contracts.voice_drafts_http`，TypeScript 为
`@speechnexus/contracts/voice-drafts-http`。这两个入口仅选择上述四条方法；其余声音创作
操作继续是显式占位。Python HTTP 用例消费生成校验器；前端实际页面接入仍需消费者验证。

## 协议升级

speech source 从 `0.1.0.dev8` 升至 `0.1.0.dev9`。四条由占位变为实现后，503 从
`OperationNotImplemented` 换成与 `retireVoiceRevision` 同形的内联声明：`error.code` 为
`voice_draft_unavailable` / `voice_draft_outcome_unknown`，保留必需的
`Cache-Control: no-store` 与 `X-Should-Retry: true`，`retryable` 为 `const true`。
`check-openai-contract-compat.py` 允许基线为占位 503 的 response 整体替换，前提是替换物
仍是带必需缓存头与 `ErrorResponse` 信封的可重试 503；四条因此通过兼容门禁，不需要放宽
基线或修改检查器。

创建入参的 wire 契约保持原样：`VoiceDesignInput` / `VoiceCloneInput` 的 `language` 仍是
`type: string`、`maxLength: 35`。收紧它会同时改动基线里 `generateVoiceDraftRevision` 的
请求 schema，兼容门禁按请求体变化拒绝，因此约束落在写入口：服务端按修订 schema 的
`LanguageTag` 校验将要落库的输入，`auto`、`zh-CN` 可以创建，`EN-US`、`english`、`x!`
直接 400 且不落库，不再先写 Job 再让读取回显违约。四条另声明
`500 voice_draft_contract_violation`（`api_error`、`retryable: false`）：自产响应违反契约时
按实现缺陷返回 500，而不是伪装成可重试 503；错误响应本身也过同一生成校验器。新增声明
Host/Origin 403，以及草稿读取的无效输入 400。旧消费者应先升级生成包和 503/500 判定，
再启用服务。

## 权威状态与恢复

`VoiceDrafts.create` 调用既有 `PublicJobs.submit`。沿用其 workspace 作用域幂等键锁、
完整请求摘要、固定 binding/configuration、Job 提交文档、协调 Job 和 outbox。
草稿与生成输入在该同一事务插入；草稿失败会回滚整笔 Job 提交，没有第二套派发。
草稿 ID 由已接受的 Job 身份固定，因此重启和同键重放能找到原草稿。同键异意图返回 409。
幂等键可选；缺键的新请求是新意图。提交结果未知时保留原请求与键进行协调，不换键重试。

本切片的草稿固定 head revision 为 `"1"`，创建后状态为 `generating`。读取以关联的
权威 generation Job 为据：成功且结果的 kind/draft ID/revision 匹配时投影 `candidate`，
失败、取消或过期投影 `failed`，并在 `revision.failure` 给出稳定 code 与固定文案；Job 没有
`error_code` 时按取消/过期分别给出 `voice_generation_cancelled` / `voice_generation_expired`。
`candidate` 不信任结果 JSON 自述：投影回读 `PublicJobs.artifacts`，要求非空 preview ID、
匹配的 `receipt_id` 与 ready 的 preview Artifact；缺失、错误归属或非 ready 时不产出
候选，而是按存储结果不可用返回 503。`?draft_revision=` 只返回该精确修订，非 head 取值
返回 404，不回退到 head；本切片 head 为 `"1"`，因此 `?draft_revision=2` 是 404 而不是
用 rev1 冒充。元数据编辑不会生成新修订或改变输入、binding、Job。
完整状态枚举继续来自原契约；本切片不提供任意状态修改端口。

PATCH 只接受 `expected_metadata_revision` 和 `display_name`。PG 草稿行锁内先查原键回执，
再检查发布状态和 metadata revision CAS，再更新元数据并存回执。CAS 冲突、已发布草稿或
同键异意图返回 409；原回执重放不受后来版本变化或发布状态影响。尝试通过 PATCH 改状态、
输入或生成结果返回 422，且不写入。

创建提交的失败按“是否已确定未提交”区分：

- `voice_draft_unavailable`（503，`retryable: true`，`X-Should-Retry: true`）表示存储失败
  发生在确定未提交的路径，客户端可用原请求原键重试。
- `voice_draft_outcome_unknown`（503，`retryable: true`，`X-Should-Retry: true`）只在请求
  携带原 `Idempotency-Key` 时返回：提交可能已 commit 但确认丢失，客户端用同一键重放会
  回放原草稿与原 Job，不会新建。不同键仍是新意图。
- 无 `Idempotency-Key` 的请求无法安全重放，因此同一未知窗口返回 409
  `voice_draft_outcome_unknown`（`retryable: false`、`X-Should-Retry: false`）：重发会被
  当作新意图，可能再建草稿与生成任务；客户端应先协调原请求，不能把它当普通 503 重试。

这一区分同时约束契约和生成包，不通过放宽 503 重试声明来换取占位门禁通过。

公开响应不含引擎句柄、特征向量、音频 base64、对象键或内部模型路径；错误使用固定文案。
新生成、预览、校验和发布端口仍由后续切片实现。Job/outbox 接受不等于真实模型已执行；
现有生产 dispatch planner、真实引擎、资源准入和目标拓扑资格仍须各自验收。

草稿的生成 Job 仍写入协调 Job 与 outbox，但本执行单元只规划 ASR/TTS：`DispatchJob`
接受集合和 PG claim 查询共同使用 `DISPATCHABLE_JOB_KINDS`，voice 行在同一条 claim SQL
内被过滤，不占批次上限，也不会在行转换时抛 `DispatchProtocolError` 毒化同批 ASR/TTS。
voice 协调行保持 pending，等待后续声音执行切片接入；当前 dispatcher 不消费它。

## 迁移与生产装配

主干最新已合并迁移为 `0040_public_job_listing`，`0041` 空闲。新迁移
`0041_voice_drafts` 直接接在 `0040` 之后，不回写已合并迁移；若后续主干占用该位置，
集成者应在同一候选 rebase 后重核顺序。
迁移增加 `speech_voice_drafts`、`speech_voice_metadata_commands`，扩展 Job kind CHECK；
不回写既有迁移。有声音 Job 时 downgrade 的窄 CHECK 会拒绝，防止静默丢失历史。

`create_app` 在既有 `JobRuntime` 里用同一 PG pool 装配 `VoiceDrafts` 与
`PostgresVoiceDrafts`。沿用 `SPEECHNEXUS_JOB_WRITER_DATABASE_URL` 和显式 schema。
部署先应用迁移，再由 owner 审核 `voice_draft_grants`、`public_job_grants` 及原 Job
提交权限。运行角色新增两表 SELECT/INSERT，仅能 UPDATE display name、metadata revision
和更新时间；不授予 status/head/input/binding 更新或历史删除。启动校验这些权限，不执行 DDL。
旧 Job writer 必须同步迁移和授权后升级；未配置 provider 时四条返回真实 503。

## 验证与接收边界

`tests/unit/backend/test_voice_creation_jobs.py` 与 `test_voice_drafts_http.py` 覆盖 Job 重放、
生成校验器、严格输入、Host/Origin、状态修改拒绝、错误区分、candidate 回执/Artifact 反例、
精确修订查询，以及提交确认丢失时带键回放与无键 409 的分支。
`tests/integration/test_voice_drafts_pg.py` 使用调用方隔离 DSN、随机 schema 与 finally DROP，
覆盖迁移、创建/读取/推进、元数据 CAS/重放、并发同键、Job 回滚、候选阈值、中文/非 BMP
字节边界、voice+ASR/TTS 混合派发和正式 lifespan 权限装配。
缺 `SPEECHNEXUS_TEST_DATABASE_URL` 时 pytest.fail；不能把缺配置当作通过。

本轮在隔离 PostgreSQL 上实际执行了草稿、混合派发与 HTTP 用例；这是单库实测，不等于
双库或目标部署验收。真实生成、Linux/CUDA、双库和正式页面接入仍未据此验收。路由门禁
未实现基线由 **19 降至 15**（本切片移除四条），契约中指向占位组件的 503 由 15 降至 11。
历史审计保留原时点记录，当前结果以门禁输出为准。

避免改变锁文件 registry 的验证入口：

```sh
PATH="$PWD/.venv/bin:$PATH" make PYTHON=python3 contracts-route-conformance
PATH="$PWD/.venv/bin:$PATH" make PYTHON=python3 contracts-check contracts-drift
PATH="$PWD/.venv/bin:$PATH" python3 -m pytest tests/unit/backend/test_voice_creation_jobs.py tests/unit/backend/test_voice_drafts_http.py
PATH="$PWD/.venv/bin:$PATH" python3 -m pytest tests/integration/test_voice_drafts_pg.py
```
