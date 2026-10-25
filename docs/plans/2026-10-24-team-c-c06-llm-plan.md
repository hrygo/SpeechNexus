# C06 可选内网 LLM 与固定源纪要生成实施计划

> 沿用已授权 native inline / TDD；使用 python-pro、security-and-hardening、executing-plans；源代码冻结后一次独立只读终审。不提交、推送、发送外部消息或启用真实 LLM。

**Goal:** 实现服务中立文本生成端口、默认关闭的固定内网 OpenAI compatible adapter，以及从明确 sealed revision 生成不可信纪要候选的用例。它是完整 C06 的执行边界；持久任务、重试 attempt、公开接口、助手 turn 和真实部署资格继续实施，不能由本单元替代。

**Spec:** [ADR 0003](../decisions/0003-workbench-llm-network-boundary.md)、[技术方案 §9](../architecture/technical-blueprint.md#9-工作台与内网-llm)、[C06 工作包](2026-10-23-three-team-delivery-plan.md#43-c-团队应用与体验)、现行 workbench MinutesJob / MinutesCreateRequest。

**Base/head:** `a98b2bbfa2576eeee9f5bdf37300605428499d84`；保留既有174文件及其补丁，不写公共生成流水线、契约、锁、部署或 speech 模块。结构索引不存在，coverage 返回 project not found，直接读取相关命名源码；不重建索引。

## Task 1：端口与模型调用边界

Files：新增 application/llm.py、infrastructure/openai_text_llm.py、tests/unit/backend/test_workbench_llm.py。

Interfaces：不可变 profile 固定模型、模板 revision、系统模板、输入/输出字符上限和 token 预算。请求仅包含该 profile 与选定正文；结果只含文本及可选显式 token 用量。DisabledTextLlm 默认拒绝执行。enabled adapter 需要服务端固定端点与部署资格引用；该引用不是网络测试证据。

- RED→GREEN：默认关闭不读内容/不联网；配置错误与错误信息保密；固定模型/模板/参数；有限输入、响应字节、Unicode 和 token 校验；只允许一个完整 assistant 文本结果；工具调用、截断、模型漂移、异常 JSON、重定向、压缩和错误响应拒绝；不代理、不重试、不回退；总 deadline 与任务取消关闭连接；本调用的 transport 日志不泄露 provider 状态文本/headers。
- HTTP 子集：非 streaming `POST /v1/chat/completions`，`n=1`、temperature=0、max_tokens；端点第一版使用固定 private/loopback 数字地址，非 loopback 使用 HTTPS，不支持 DNS 名或自定义路由。凭据和正文不进入 repr/异常/transport 日志。内部 CA 通过服务端严格 SSLContext 配置；每个 client 拥有独立 transport。
- 合成 loopback HTTP 验证真实 httpx 序列化及读取，不下载/加载模型或访问外网；TLS 测试使用本机已有 openssl 生成临时合成证书。Expected：所有测试通过，statement+branch coverage >90%。

## Task 2：封存稿件候选

Files：新增 application/minutes.py、tests/unit/backend/test_workbench_minutes.py、tests/integration/test_workbench_minutes_postgres.py；修改既有 scripts/dev-env/workbench-pg-probe.py 加入测试（保留原测试）。

Interfaces：MinutesGeneration.generate(transcript_id, source_revision, template_revision, llm_model?)；profile 不匹配拒绝，先读明确 sealed revision 与当前 head，再在事务外调用 LLM，完成后重读源/当前 head，删除、变化或取消不能返回候选。候选固定源摘要、模型/模板/参数及用量，正文 repr 隐藏，作为纯文本候选，不发布制品、不改原文、不创建 speech job。

- RED→GREEN：draft/旧 head/错误身份拒绝，空稿明确拒绝；源正文原样，候选输出不解释为代码；固定输出 profile；源变化/删除/异常/取消隔离；等待 LLM 时可继续事务、无 DB 连接占用。
- 实际 socket-only 临时 PG：v2 sealed 源、历史保留、生成等待期无 runtime 连接、删除接受立即屏蔽结果；probe 退出必须 stopped=true、无 skip。
- Python format/lint/mypy/imports、unit/contract、contracts-check/drift；Web 源码未变则复用当前512测试与相同构建产物浏览器证据。Expected：相关门禁全部通过。
- 一次终审，Critical/Important 一轮 TDD 修复；Minor 记录。更新交接、goal、索引与累计补丁，验证干净基线 apply、全文件 hash、链接、真实 index 不变。Goal 保持 active。

## Review Focus

1. 服务端配置不能被普通生成请求替换；enabled 不等于已通过内容网络/混合负载资格。
2. 输入、响应、超时和用量有限；取消保留 CancelledError；provider 错误、端点、凭据及正文不泄露。
3. 不可信模型结果只作为文本，不允许工具/函数执行、HTML、SQL 或路径操作；失败不改原文或触发语音重做。
4. 固定 sealed 源与当前 head 的读取/重检不跨网络持有事务；删除接受、变化、取消不能发布迟到结果。
5. 私有候选不是持久 MinutesJob；检查交接是否准确列出尚待实现的 C06/C08 生产路径与完整范围。

## 本轮验收状态

上述内部端口、adapter 与 sealed 候选已实现并通过111聚焦单元、868后端/契约与211临时PG探针；一次终审两项Important已在一轮TDD修复。完整C06仍需持久任务、公开路由/页面、助手及实际部署资格。全仓当前有一个B/A网关fixture混用固定/实时时钟的已复现失败，未越权改其源文件；提供[两行候选](2026-10-24-team-c-gateway-clock-candidate.patch)及临时组合验证，待B/A集成后全仓门禁才能在权威树上放行。该阻塞不冻结其余C00–C08的独立工作。
