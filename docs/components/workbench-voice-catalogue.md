# 工作台公开音色目录读取

工作台经语音底座公开协议读取目录和固定修订。音色设计、克隆、验证、发布与退役属于底座职责；工作台不共写底座表，不把草稿提升为已发布版本。这是所有权边界，不表示上述各生命周期能力均已实现；内建声音已有独立[资格与发布路径](../operations/builtin-voice-qualification.md)，其他来源及退役仍须分别核对。

## 源与投影

权威源是 `contracts/speech-openapi.yaml` 的 `listVoices`、`getVoiceRevision`，修订正文引用 `contracts/voice-resource.schema.json` 的 `VoiceRevision`。修订的语言与格式位于 `capabilities.languages/audio_formats`；目录摘要仍使用根级字段。旧的扁平修订夹具不代表公开协议。

`contracts/bindings.json.http_surfaces.voice_catalogue` 只选择这两个 GET，生成 Python `speechnexus_contracts.voice_catalogue_http`、TypeScript `@speechnexus/contracts/voice-catalogue-http` 及闭包 OpenAPI/JSON 校验器。没有发布、退役或创建权限；生成文件不可手改。正式 C 适配器先校验完整公开响应，包括 source、validation、capabilities、生命周期和未知字段，再投影到工作台领域值。生成类型不进入 domain。

读取固定修订时，正文的 `voice_id` 和 `revision` 必须逐项匹配请求。schema 合法不能代替身份匹配。published 没有退役时间；retired 和 disabled 保留底座的退役时间，disabled 还保留原因，不推导或改写状态。

## 底座公开路由与部署

`create_app()` 已注册上述两个 GET，operationId 与生成的只读表面一致，运行时
OpenAPI 也投影同一闭包。生产装配复用 `ModelCatalogRuntime` 的有界只读连接池与
`SPEECHNEXUS_MODEL_CATALOG_READER_DATABASE_URL`，不新增目录状态或工作台写者。

迁移 `0038_voice_catalogue_reader` 接续 main 的 `0037_public_job_retries`，新增 security-barrier 视图
`speech_published_voice_revisions`，按 `voice_id/revision/revision_id` 关联既有
`speech_publications` 与 `speech_builtin_voice_qualifications.document`，使用左连接保留发布事实。
缺少精确资格文档（含 revision_id 不匹配）、JSON null 或正文身份错误不能成为资源不存在；
列表在语言过滤/分页前检查当前 head 的文档身份与缺口，固定版本读取按发布身份返回可重试 503。视图不暴露
来源审批、资格原件、native speaker 映射、私有命令或发布意图；不新增表，不修改发布历史。
当前可读数据来自已有内建音色发布路径；设计与克隆没有持久发布文档生产者，仍待实现。

部署管理员在已有 `model_catalog_reader_grants(schema, role)` 基础上应用
`voice_catalogue_reader_grants(schema, role)`（位于
`speech/infrastructure/voice_catalogue_schema.py`），只追加该视图的 SELECT。
启动审计继续拒绝私有资格表、审批表和任何写权限。旧模型目录角色可以保持原授权，
此时模型读取不受影响，音色读取为 503；应用不会自行迁移或授予权限。

列表读取当前 head，排除退役版本，按 voice ID 升序分页：`limit` 默认 50、范围 1–100，
`cursor` 为上一页最后一项的 ID；`language` 精确过滤已声明语言。`workspace_id` 沿用
部署级目录语义，不建立身份或数据隔离。翻页期间目录仍可变化；固定版本读取不跟随 head，
保留历史，并依据已有 retirement 账本投影 `retired/disabled` 状态。

未配置、无发布事实、权限或数据库不可用、公开文档损坏时返回
`503 voice_catalogue_unavailable`，`retryable=true`、`X-Should-Retry: true`。
已有发布目录内的未知 ID/版本返回 `404 voice_not_found` 且不可重试。
目录完整时，语言过滤或游标之后无匹配项返回 200 空页。
公开源 `0.1.0.dev6` 为两条 GET 新增 `400 voice_invalid_request`：非法路径、未知字段、
无效查询值与重复参数均在读取目录前拒绝（含目录未配置时），`retryable=false`、
`X-Should-Retry: false`；调用者先修正请求，不自动重试。所有响应均 `Cache-Control: no-store`。
200 DTO、字段与枚举保持原义。严格的旧 HTTP 校验器不认识新增 400，须与服务端一同升级
生成包；正式工作台消费者保持已有非 200 → 目录不可用的处理且不重试，正常请求无变化。
公开目录证明发布事实，不把来源或资格的当前有效性推断为可执行性；新执行仍由准入核验。

## v2 与消费者升级

工作台源版本为 `0.1.0.dev2`，新增：

`GET /v2/workbench/voices/{voice_id}/revisions/{revision}`（`getVoiceRevisionV2`，`VoiceRevisionV2`）。

v2 保留既有工作台字段投影，并支持与底座一致的最长 2000 字符停用原因。正式 Web 的 `WorkbenchClient.voiceRevision` 使用这个路由和生成校验器；页面展示完整原因、退役时间、语言、格式与固定绑定。

展示固定 binding 不等于渲染请求已传递该 pin：当前工作台公开 Job 适配器仍未发送 `model_binding`，具体边界见 [Public TTS Job 固定 binding](public-job-model-binding.md)。

既有 v1 路由和 `VoiceRevision.disabled_reason.maxLength=128` 保持冻结。原因超出 v1 投影时返回已有的 `503 voice_catalogue_unavailable`，不截断，不返回违反旧校验器的 200。128 字符以内，两版返回相同投影。新增路由不要求旧消费者升级；使用新 Web 前须先部署支持 v2 的 C 服务，前端不静默回退 v1。此源版本是制品标识，不是运行时版本协商。底座公开源 0.1.0.dev6 的新增 400 见上节；既有成功请求/响应定义保持原义。

## 失败和资源边界

只使用服务端配置的固定底座地址，不接收客户端端点，不跟随重定向、不自动重试。JSON 响应正文限制 256 KiB；生产组合根将读取总期限固定为 30 秒，单次原生 HTTP 超时 15 秒。适配器构造参数允许大于 0 且不超过 120 秒的期限，但当前运行配置不提供相应选项。超限、重复 JSON 键、契约非法、错身份、传输错误与期限耗尽均成为目录不可用，不把源正文或私有地址拼入错误。调用者取消继续传播，并关闭响应流。

固定修订的底座 404 映射为 `resource_not_found`。目录列表没有公开声明 404，收到该状态时按不可用处理。页面读取失败后清除上次成功修订，避免将旧事实展示为本次结果。

运行时目前要求底座与对象区配置成组。新增目录集成测试使用不可达的合成对象端点，证明只读目录链路不需要对象操作；这不构成对象存储可用性证明。

## 验证与剩余验收

- `tests/fixtures/contracts/voice-catalogue-http.json`：Python/TypeScript 共用公开源正负例，独立源校验与生成校验比较；包含三种状态、完整来源、2000/2001 字符、旧扁平形状、未知字段与非法能力。
- `tests/unit/backend/test_workbench_public_voice_catalogue.py`：查询转发、固定身份、期限、取消关闭、重复键、正文上限与公开错误映射；移除身份检查应使错身份用例失败。
- `tests/unit/backend/test_workbench_voices_http.py`：v1/v2 的 128、129、2000 字符投影边界。
- `tests/unit/backend/test_speech_voice_catalogue_http.py`：底座路由注册、同源 OpenAPI、未配置可重试 503、请求错误 400 且不访问目录。
- `tests/integration/test_speech_voice_catalogue.py`：隔离 PG、实际底座 lifespan 与只读角色，覆盖发布版本、分页/语言、404、空目录/缺授权/损坏或缺失文档/资格身份不匹配 503、退役历史、未发布数据隔离及工作台未改动消费者的 200；使用合成发布记录，不证明真实资格操作。
- `tests/integration/test_workbench_runtime_voice_catalogue.py`：实际 C lifespan、最小角色 PostgreSQL、真实回环 HTTP 源与正式路由，验证正常状态及错身份/非法来源拒绝。
- `tests/integration/test_workbench_real_runtime_voice_catalogue_browser.py`：实际生产 bundle 和上述 C 装配，键盘触发读取，完整 2000 字符与退役时间展示，错身份错误及旧记录清除；浏览器禁止出站。

这些测试的底座响应是合成事实。实际执行结果须以对应 revision 的报告为准，收集到用例或本机跳过不算通过。它们不证明真实音色发布、候选再生、试听质量、复用验证、真实模型或目标多机拓扑；C03 与原 AC 的剩余条件继续保留。
