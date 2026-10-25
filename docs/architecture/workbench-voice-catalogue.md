# 工作台公开音色目录读取

工作台经语音底座公开协议读取目录和固定修订。底座负责音色设计、克隆、验证、发布与退役；工作台不共写底座表，不把草稿提升为已发布版本。

## 源与投影

权威源是 `contracts/speech-openapi.yaml` 的 `listVoices`、`getVoiceRevision`，修订正文引用 `contracts/voice-resource.schema.json` 的 `VoiceRevision`。修订的语言与格式位于 `capabilities.languages/audio_formats`；目录摘要仍使用根级字段。旧的扁平修订夹具不代表公开协议。

`contracts/bindings.json.http_surfaces.voice_catalogue` 只选择这两个 GET，生成 Python `speechnexus_contracts.voice_catalogue_http`、TypeScript `@speechnexus/contracts/voice-catalogue-http` 及闭包 OpenAPI/JSON 校验器。没有发布、退役或创建权限；生成文件不可手改。正式 C 适配器先校验完整公开响应，包括 source、validation、capabilities、生命周期和未知字段，再投影到工作台领域值。生成类型不进入 domain。

读取固定修订时，正文的 `voice_id` 和 `revision` 必须逐项匹配请求。schema 合法不能代替身份匹配。published 没有退役时间；retired 和 disabled 保留底座的退役时间，disabled 还保留原因，不推导或改写状态。

## v2 与消费者升级

工作台源版本为 `0.1.0.dev2`，新增：

`GET /v2/workbench/voices/{voice_id}/revisions/{revision}`（`getVoiceRevisionV2`，`VoiceRevisionV2`）。

v2 保留既有工作台字段投影，并支持与底座一致的最长 2000 字符停用原因。正式 Web 的 `WorkbenchClient.voiceRevision` 使用这个路由和生成校验器；页面展示完整原因、退役时间、语言、格式与固定绑定。

既有 v1 路由和 `VoiceRevision.disabled_reason.maxLength=128` 保持冻结。原因超出 v1 投影时返回已有的 `503 voice_catalogue_unavailable`，不截断，不返回违反旧校验器的 200。128 字符以内，两版返回相同投影。新增路由不要求旧消费者升级；使用新 Web 前须先部署支持 v2 的 C 服务，前端不静默回退 v1。此源版本是制品标识，不是运行时版本协商。底座公开源、本次未改动的既有请求/响应定义均保持原义。

## 失败和资源边界

只使用服务端配置的固定底座地址，不接收客户端端点，不跟随重定向、不自动重试。JSON 响应正文限制 256 KiB；读取总期限默认 30 秒（可配置 0..120 秒之间的正数），单次原生 HTTP 超时 15 秒。超限、重复 JSON 键、契约非法、错身份、传输错误与期限耗尽均成为目录不可用，不把源正文或私有地址拼入错误。调用者取消继续传播，并关闭响应流。

固定修订的底座 404 映射为 `resource_not_found`。目录列表没有公开声明 404，收到该状态时按不可用处理。页面读取失败后清除上次成功修订，避免将旧事实展示为本次结果。

运行时目前要求底座与对象区配置成组。新增目录集成测试使用不可达的合成对象端点，证明只读目录链路不需要对象操作；这不构成对象存储可用性证明。

## 验证与剩余验收

- `tests/fixtures/contracts/voice-catalogue-http.json`：Python/TypeScript 共用公开源正负例，独立源校验与生成校验比较；包含三种状态、完整来源、2000/2001 字符、旧扁平形状、未知字段与非法能力。
- `tests/unit/backend/test_workbench_public_voice_catalogue.py`：查询转发、固定身份、期限、取消关闭、重复键、正文上限与公开错误映射；移除身份检查应使错身份用例失败。
- `tests/unit/backend/test_workbench_voices_http.py`：v1/v2 的 128、129、2000 字符投影边界。
- `tests/integration/test_workbench_runtime_voice_catalogue.py`：实际 C lifespan、最小角色 PostgreSQL、真实回环 HTTP 源与正式路由，验证正常状态及错身份/非法来源拒绝。
- `tests/integration/test_workbench_real_runtime_voice_catalogue_browser.py`：实际生产 bundle 和上述 C 装配，键盘触发读取，完整 2000 字符与退役时间展示，错身份错误及旧记录清除；浏览器禁止出站。

这些测试的底座响应是合成事实。实际执行结果须以对应 revision 的报告为准，收集到用例或本机跳过不算通过。它们不证明真实音色发布、候选再生、试听质量、复用验证、真实模型或目标多机拓扑；C03 与原 AC 的剩余条件继续保留。
