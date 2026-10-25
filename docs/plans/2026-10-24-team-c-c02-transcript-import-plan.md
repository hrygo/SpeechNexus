---
title: "SpeechNexus C02 原始转写导入"
status: active
version: "0.1.0"
date: 2026-10-24
---

# C02 原始转写导入实施计划

依据[团队目标](2026-10-24-team-c-goal.md)、[工作台契约](../../contracts/workbench-openapi.yaml)的 speech_artifact 来源及[公开语音契约](../../contracts/speech-openapi.yaml)的 Artifact 元数据/content。基线 `cbf4a8ca1cf808fbc5ff4b125f7ff1e271496995`，本轮 fetch 无新远端提交，保留原有 83 个 C 文件。实时生成 validator 仍缺失，继续不依赖它的真实文件导入前置。

## 设计与边界

生成 `parse("Artifact", ...)` 校验公开元数据；只允许 ready/transcript、项目所在 workspace、请求指定 Artifact/receipt、未过期来源。对 `text/plain` UTF-8 制品实现完整导入；JSON/verbose/SRT/VTT 需要各自内容契约/解析后续接入，不能按文本冒充正文。元数据 Content-Type 为 JSON、content 路由按公开契约为 `application/octet-stream`；Artifact 自身 `media_type` 指定 plain UTF-8。拒绝压缩内容、重定向和 partial response。

服务端注入获准公共服务的 `httpx.AsyncClient`，浏览器不能提供端点。只读公开 `/v1/nexus/artifacts/{id}` 和 `/content`，无引擎/底座表访问。客户端由组合根拥有；适配器逐请求关闭流，不关闭共享客户端。元数据最多 64 KiB，正文最多 4,000,000 bytes/1,000,000 Unicode 字符，逐块计数、SHA256 和严格 UTF-8 解码；不信任 Content-Length。默认整体 30 秒期限和 HTTP 每次 15 秒超时，取消传播且关闭 response，异常只暴露稳定本地错误，不包含原响应/内容/地址。

新增应用端口 `TranscriptSourceReader.read(artifact_id, receipt_id, workspace_id) -> str`；`WorkbenchVersions(..., source_reader=...)` 注入。`import_speech_transcript` 先短事务检查历史回执/父项目，网络读取在事务外，最终事务重查回执并锁父项目、建立 sealed revision 1、原子保存 head/回执。与手工创建共用 `transcript.create` 幂等 scope，不同来源或标题冲突；同键重放返回历史快照且不再访问已失效来源。并发可重复 GET，但只能提交一个身份；取消或失败不产生半成品/回执。父项目在下载期间删除后不能复活。

这是生产适配器与内部用例，未接公开请求/响应 DTO、HTTP 工作台路由、对象区或生产组合根；这些仍依赖 A 生成入口和集成，不宣称 C02 完成。来源 receipt 校验仅证明与公开 Artifact 元数据关联，不声称签名/执行资格认证。导入显式保存封存原文，校对仍追加 draft，不写源制品。

## Task 1：应用事务与回执

文件：新增 `application/transcript_sources.py`，扩展 `application/versions.py`；新增 `tests/unit/backend/test_workbench_transcript_import.py`。

- [x] 先编写并运行 RED：封存原文、追加校对、重放不再下载、同键不同源/手工源冲突、并发创建、下载失败/取消、下载期间父删除、最终提交失败。
- [x] 实现注入端口和两阶段用例，共享既有回执规则，网络不能在数据库事务内。
- [x] 集中测试 GREEN，保持原版本/删除测试不退化。

## Task 2：公开 HTTP 读取与隔离 PostgreSQL

文件：新增 `infrastructure/public_transcript_source.py` 和 `tests/unit/backend/test_workbench_public_transcript_source.py`、`tests/integration/test_workbench_transcript_import_postgres.py`；扩展 C 自有 `scripts/dev-env/workbench-pg-probe.py`。

- [x] 先写 HTTP 适配测试并观察 RED；覆盖生成校验、身份/receipt/workspace、来源就绪/过期、摘要/长度/格式、压缩/重定向/部分响应、流取消关闭及期限。
- [x] 实现有界 HTTP 元数据与内容读取，不复制 DTO，不跟随 Location 或内网以外 fallback。
- [x] 隔离 PG 验证真实运行角色 sealed/draft/历史回执、并发、下载期间无 DB 连接、父删除与失败回滚；通过 socket-only 探针确认清理。
- [x] 完成 Python/契约检查、独立 mutation 和一次只读最终审查；记录证据和未验证范围，打包全部 C 文件及应用验证。不 commit/push，不改 A 的生成/共享配置。

## 审查重点

1. 源下载和重试不能占长事务，同键历史回执在来源过期后仍可恢复原始结果。
2. 来源身份/receipt/workspace/格式/摘要一致，任何失败不落半个版本或误记回执。
3. 取消、超时、畸形/过大 body 和重定向不泄漏内容、连接或凭据。
4. 下载期间删除父项目，或与另一个导入竞争，不得产生复活/重复身份。
5. sealed 原文与后续 draft 独立，公开 profile/HTTP、JSON 转写、对象区和真实 GPU 仍待验，不能把内网 URL 本身当作出站资格。

沿用 writing-plans、executing-plans、TDD、async-python-patterns 与 PostgreSQL 技能；缺失共享 helper 继续使用独立手动 ledger `.runtime/team-c/c02-transcript-import-progress.md`，在整个单元结束后只派一次只读审查。

本单元实现及一次独立审查已完成；P2 NUL 修复经来源单测与真实 HTTP/PG RED→GREEN 验证，详情见[交接](2026-10-24-team-c-c02-transcript-import-handoff.md)。完整 C02/C08 和团队目标仍为未完成，最终全套/补丁结果以本轮执行记录为准。
