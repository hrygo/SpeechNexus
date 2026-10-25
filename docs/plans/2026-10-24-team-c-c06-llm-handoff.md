# C06 内网 LLM 调用与封存源纪要候选交接

核验日期：2026-10-24（Asia/Shanghai）。本地 `base_sha=head_sha=a98b2bbfa2576eeee9f5bdf37300605428499d84`；未创建 commit、push 或外部消息。完整 C00–C08 goal 仍 active。

## 实现与边界

- 服务中立 `TextLlm`、不可变 profile/request/completion/usage 和默认 `DisabledTextLlm`。模型、模板 revision、系统模板、输入/输出字符与 token 预算由服务端固定；未启用时连源正文也不读取。
- `OpenAiTextLlm` 只支持非 streaming 的 OpenAI compatible `POST /v1/chat/completions` 文本子集：一个 assistant choice、`finish_reason=stop`、`n=1`、temperature=0、固定 max_tokens。不接受工具/函数调用、拒绝响应、模型漂移、重复 JSON 字段、异常 Unicode、无效用量或超预算输出；用量缺省保留 unknown。
- 首版端点为固定数字 loopback / RFC1918 / IPv6 ULA 地址，非 loopback 要求 HTTPS，不支持 DNS 或自定义 proxy 路由。凭据仅由服务端配置注入；普通生成参数不含 URL/凭据。每次调用创建并关闭独立 client/transport，禁止环境代理、重定向、重试与回退，设总 deadline、读超时与响应字节上限；取消传播并关闭 HTTP 流。服务端可传入严格证书/IP SAN 校验的 SSLContext，支持内部 CA，拒绝关闭验证或事后降级。锁定 HTTPX/HTTPCore 的 producer logger 使用稳定 ContextVar filter，隔离本次调用的状态文本/headers/端点，不修改全局级别或屏蔽无关并发调用。
- `MinutesGeneration` 只消费指定且仍为当前 head 的 sealed revision，原样提交选定正文；完成后重读可见源和当前 head。删除接受、版本变化、身份/正文漂移和取消不能返回迟到候选。源数据、回执及语音任务均不被此用例写入。候选保留源 revision ID/摘要、模型、模板 revision/摘要、生成参数及显式用量，正文从 repr 隐藏，仅作为不可信纯文本。
- 每次源读取都先释放短事务再调用 LLM。返回内存候选后发生的删除不能撤回既有副本；未来持久任务必须在最终发布事务重新检查源/删除/取消屏障。

## 验证证据

- 两个新 unit 模块：111 passed；三个新增生产模块 statement+branch coverage 100%。先观察缺实现 RED，另以“报告 token 超预算却被接受”的失败测试验证预算修复。日志/报告在 `.runtime/team-c/c06-{llm-red,minutes-red,budget-red,coverage}.log` 及 `c06-coverage.json`。
- 真实 httpx 与临时 loopback HTTP 服务验证实际 POST/JSON、固定端点、原样正文、无代理；其余模型行为为 synthetic transport。另用本机 `openssl` 在 pytest 临时目录生成一天有效的合成 TLS 证书/私钥（不入库），验证内部 CA、错误信任链/IP SAN 拒绝、连续/并发及取消后的再调用；HTTPX INFO / HTTPCore DEBUG 不记录私有状态/headers。无真实 LLM、模型下载/加载或外网访问。
- socket-only PostgreSQL 18.6 临时集群：211 passed（包含既有 PG 测试及 adapter unit 测试，不称全部为真实 PG），基础设施 coverage 93.30%。四个新增 PG 场景验证真实 sealed 源、等待期无 runtime PG 连接、历史不变、project/transcript 删除接受后拒绝迟到候选、后续校对使候选失效。最终 `stopped=true`，独立 `pg_ctl status=3` 且 pid 文件不存在。证据 `.runtime/team-c/c06-pg.json`、`c06-pg-terminal.json` 及其指向的日志。
- Python format、ruff、mypy（286 source files）、7 项依赖边界通过；当前后端/契约868 passed、聚焦111 passed。全仓 unit/contract 为1503 passed/1 failed（下述独立网关测试时钟问题），不能称当前全仓门禁通过；结果见 `.runtime/team-c/c06-{format,lint,mypy,imports,python}.log`；契约校验28文件和 drift 通过，工作区生成产物未变。
- Web 源码与先前174文件包一致，复用 a98 集成时的512项测试、构建与相同产物浏览器证据；本轮没有重新执行 Web 门禁或新增浏览器 LLM 页面验证。
- 源冻结后一次独立只读终审发现两项 Important（transport 日志泄露风险、内部 CA/连接所有权缺失）；两项均以六个失败场景 RED→GREEN 在一轮修复中解决，未再分派复审，Critical/Minor 均无。累计183文件补丁 hash / clean-base apply / 本地链接 / index 不变检查记录在 `.runtime/team-c/c06-review*`、`c06-package*`。

## 全仓回归的独立阻塞与候选

`tests/unit/gateway/test_ingress.py::test_seal_cannot_succeed_while_a_previously_persisted_send_can_still_resume` 将 ledger 固定于 `2026-10-24T00:00:00Z`，但 service/engine 用 wall clock。本轮跨过 UTC 午夜后，全仓与隔离单测稳定失败：observed_at 晚于 ledger 记录时间。这与 C06 源码/日志过滤无关，生产校验不应放宽。

网关与其测试由 B/A 维护，本轮未修改该源文件。提供[时钟对齐候选](2026-10-24-team-c-gateway-clock-candidate.patch)，仅为 engine/service 注入同一 NOW，保留全部屏障/超时断言。候选是零上下文补丁，维护者先核对原文件 SHA-256 `d3ae26696ba36b96f92aeab7a45f670e38383a39df6e81c6ab3ffe40bdeb2399`（元数据另见 `.runtime/team-c/c06-gateway-clock-original.json`），再使用 `git apply --unidiff-zero --check` 检查并评审；C 未应用到权威工作区、未发送外部消息。最新183文件预览在临时组合副本中应用该候选后，全仓1504 passed（31.96s），并核对全部待交付 Python 文件 hash、实际 import 来自临时目录；当前网关源 SHA 未变。候选组合副本的验证与输入 hash 见 `.runtime/team-c/c06-python-candidate.json` 和日志；它不等于当前工作区已修复。解除条件：B/A 审核集成时钟修正，再在当前整合基线上运行全库门禁。

## 契约与接入

公开 workbench OpenAPI 保持 `0.1.0.dev0`，MinutesCreateRequest / MinutesJob 均未改。新增类型是内部应用对象，不是 wire DTO；没有手写其公开请求/响应 validator。`contracts/bindings.json` 当前 workbench runtime roots 仍只有 ProjectCreateRequest / ProjectPatch，需 A 协调扩展生成边界并由 C 验证生产者与消费者。

尚待完成：Meeting → sealed transcript 的生产映射、独立持久 MinutesJob/attempt/取消/用量、原意图重试与最终发布事务、显式保存/导出和派生删除恢复；公开纪要路由与 UI 内容范围展示；助手 turn、插话、稳定文本到 TTS 和迟到播放 generation 隔离。已有内部核心会用于这些路径，不能作为完整 C06 或任何 Gate 已通过的证据。

没有部署或启用 LLM。资格引用只是可信部署配置字段，不证明其指向证据真实有效；应用、LLM、代理与观测的实际出站限制和同设备父物理预算/混合负载仍由 C 与部署负责人联合验证。HTTP 连接取消不等于模型计算已停止或可释放物理预算。

[实施计划](2026-10-24-team-c-c06-llm-plan.md) · [持续目标](2026-10-24-team-c-goal.md) · [ADR 0003](../decisions/0003-workbench-llm-network-boundary.md)
