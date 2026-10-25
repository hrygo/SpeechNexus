---
title: "团队 C C02 固定转写版本文本导出交接"
status: active
date: 2026-10-24
---

# 团队 C C02 固定转写版本文本导出交接

[本单元计划](2026-10-24-team-c-c02-text-export-plan.md)接在已保存/校对稿件之后，提供固定 revision 的 UTF-8 文本导出用例。C02 的公开页面、生成校验器、字幕和完整生产闭环仍未完成，[C00–C08 goal](2026-10-24-team-c-goal.md)保持 active。

## 基线、边界与行为

`base_sha` / `head_sha` 为 `3380a76c89998b42be29b0c25e9f49443d23e15b`。先核对上一 109 个 C 文件摘要和空 stage，再 fast-forward 接入不重叠的 B #60。C 改动仍未提交或推送；未发送外部消息。speech/workbench OpenAPI `0.1.0.dev0`、bindings `0.1.0` 保持原版，无生成器、共享锁、schema、迁移或生产路由变更。

新增进程内 `TranscriptTextExports`，只通过只读 `TranscriptRevisionReader.get_revision` 消费已存在的 `WorkbenchVersions`。请求必须明确指定 revision；拒绝 bool、非整数、0、负数及超出 19 位范围的版本。reader 返回其他转写或版本时拒绝，不回退当前 head。缺失/删除记录继续 NotFound，取消传播；存储异常转为不含源错误的 `source_unavailable`。

读取事务退出后编码和计算 SHA256；输出为私有冻结 `TranscriptTextExport`，固定 `speech-transcript.txt` 和 `text/plain; charset=utf-8`，长度从实际 bytes 计算。保留 revision/status/source_revision 和源 Artifact/receipt 关联，正文不进入 repr。严格 UTF-8，原样保留 BOM、CRLF、组合字符、非 BMP、首尾空白及空校对稿；不加换行、不改原文。上限 1,000,000 字符 / 4,000,000 bytes；损坏 NUL/surrogate/超限正文明确失败。

这是固定版本读取，没有新 Job、幂等键、对象、回执、导出表或持久化发布，不等同 Book ExportArtifact 或独立保存副本。删除接受后新读取被拒绝；已返回内存的 bytes 不宣称能撤回。公开下载、内容响应头/错误映射与出站配置仍须后续宿主接入。

## 验证证据

2026-10-24（Asia/Shanghai），Python `3.14.9`、PostgreSQL `18.6`，仅用合成内容。

- 先观察缺失模块导致 RED，随后 20 项单元测试通过：明确旧稿/校对稿与后续 head 分离、无状态写入、来源/字节/摘要/空稿、版本与内容拒绝、身份失配、缺失与安全失败/取消。
- 实际公开 loopback HTTP 导入原文→隔离 PG 封存→校对→旧/新/空 revision 导出，只有首次导入的两次源 GET；之后导出不重新取底座内容。项目/转写删除接受后、正文清理前的新导出均 NotFound。
- 实际编码和 SHA256 调用期间，用独立 owner 连接检查 `pg_stat_activity`，工作台 runtime 角色连接数均为 0。runtime 仍仅使用其受限角色，未执行 DDL。
- 隔离探针 106 passed，无 skip；其中本单元 4 条真实 PG/HTTP 消费测试，其余沿用 C 的事务、来源和删除回归。workbench infrastructure 语句/分支合计覆盖率 91.92%，不代表新应用模块或完整产品覆盖率。集群 `.runtime/team-c/pg-k5ndvmdd`，socket-only、`tcp=false`、`stopped=true`，未访问本机共享数据库。
- `uv run --locked ruff format --check .`：298 文件；ruff check 通过；mypy 213 源文件通过；7 条 imports 契约 kept。`uv run --locked pytest tests/unit tests/contract -q`：967 passed in 35.71s。
- 独立 Python 进程内移除版本/转写身份检查，两条回归均失败；生产源码 SHA256 前后相同，没有在工作区落盘变异。初次 PG 用例误用了删除记录的 status 字段，已按实际 `result is None` 修正；该次 2 failed / 104 passed 不计通过，后续完整探针 106 通过并停止。

```bash
uv run --locked pytest tests/unit/backend/test_workbench_transcript_exports.py -q
uv run --locked python scripts/dev-env/workbench-pg-probe.py \
  --pg-bin /path/to/postgresql/bin --coverage \
  --output .runtime/team-c/c02-text-export-pg-probe.json
```

日志/JSON/变异和执行决定见 `.runtime/team-c/c02-text-export-*`。Web 内容未变，沿用上一单元 303 项 Web 与 ASR/TTS 浏览器证据；未把这些旧证据扩展为本单元公开页面或字幕验证。新增导出用例尚未接真实语音、设备/GPU、Linux 部署或生产入口。

## A00/C02 接入清单

当前 `contracts/bindings.json` 的 workbench runtime 入口仍只有 `ProjectCreateRequest` / `ProjectPatch`。完整 OpenAPI 类型不是运行时校验器；C 不修改 A 独占生成流水线或手写第二套 wire validator。

1. A00 为 `Project` / `ProjectList`、`TranscriptCreateRequest` / `TranscriptRevisionCreateRequest`、`Transcript` / `TranscriptRevision` / `TranscriptView` / `TranscriptList` 与相关共享错误提供同版 Python/JS 运行时入口及正负样例。公开 path/query/header 校验也需确定，不能把本用例的私有 int 参数当作 wire 校验。
2. C 主责提出并与 A 评审冻结固定转写 revision 下载契约：明确显式版本、文本媒体类型/编码、空校对稿、Content-Disposition、摘要/长度、NotFound/删除与服务不可用语义；冻结前不注册 URL。本单元内部快照不能挪作已冻结响应 DTO。
3. 字幕须先冻结声学时间段/时间基准、校对文本与时间段的版本关系、SRT/VTT 内容及错误样例；不能按文字长度造时间戳。当前 TranscriptRevision 仅 text，无时间段，字幕保持待实现。
4. 现有 ASR v1 Job 固定 `transcript_format=json`，公开原文导入只接受 UTF-8 text/plain。接线必须选择并冻结新的 text 任务意图版本，或由 A 提供结构化转写内容校验入口；不得改写已持久化 v1 原意图或把 JSON 整体当原文。
5. 生成和协议就绪后接“任务结果→导入→校对/CAS→固定版本下载”公开 UI/HTTP，验证生成客户端、实际浏览器、生产迁移/角色与删除竞争。有声书独立保存/异步导出另按 C07 验收。

## 执行决定与累计交付

沿用已批准 C02 设计和 inline 实施；缺失共享 helper/template 以手工 ledger/package 与一次独立只读审查替代。代价是显式维护摘要、索引和完整验证，保留全部旧改动，不清理或提交。

优先固定 revision 的只读用例而非漂移 head 或新对象发布，明确推进 C02 文本输出；代价是仍需公开契约、生成、生产宿主、字幕和独立副本接力。固定文件名和保真空稿避免路径/标题响应头注入及伪造非空制品，代价是自定义文件名和非文本格式要另行冻结。复用既有短事务/删除可见性，代价是无法撤回已经读出的内存内容。源存储异常安全化，代价是宿主后续须按冻结错误契约映射。

图谱 coverage 返回未索引，结论来自具名源码和实测，不创建索引。最终独立审查、累计补丁 SHA256、干净基线应用/逐文件字节和真实 index 检查见 `.runtime/team-c/c02-text-export-progress.md` 与 `team-c-current.manifest.json`；完整目标不因该单元通过而关闭。

一次独立只读审查未发现高置信 P1/P2/P3，独立重跑 20 项 focused tests 通过，核对了既存 106 项 PG 日志和生产源码摘要；未启动服务或重新执行变异。无新增 minor。

审查边界经主代理明确：删除接受后发起的新读取拒绝，但 tombstone 检查前已开始的读取不保证在删除时中止；本用例不提供撤回保证。内部 reader 须返回有效 TranscriptRevision，不承担恶意对象/str 子类隔离职责。公开 wire/响应头/字幕/有声书、引擎和部署仍待集成；图谱未索引，结构结论限具名源码。代价分别是宿主需要明确并发下载策略、持久化适配器须保持领域前提、生产资格仍开放，以及广泛测试/变异/最终补丁核验由主代理负责。
