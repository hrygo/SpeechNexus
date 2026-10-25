# C02 固定转写版本文本导出实施计划

> 执行：沿用已批准 C02 设计与本会话 inline 实施，使用 executing-plans、TDD 和一次独立只读审查。不提交、推送、发送消息或接入生产路由。

**Goal:** 从工作台已保存的明确 revision 导出逐字节保真的 UTF-8 文本，后续校对不改变导出内容，删除后的新读取不得取得正文。

**Architecture:** 新增进程内 `TranscriptTextExports`，注入只读 `get_revision(transcript_id, revision)` port，复用 `WorkbenchVersions` 的短事务和删除可见性；事务退出后编码和摘要。返回私有冻结快照，无公开 DTO、副本表、对象写入、任务或发布。公开响应与字幕契约待 A00/C 冻结后接线。

**Tech Stack:** Python 3.14 / existing workbench VersionUnitOfWork / hashlib / pytest / isolated PostgreSQL。

**Spec:** [三团队 C02/C08](2026-10-23-three-team-delivery-plan.md#43-c-团队应用与体验)、[产品设计](../speechnexus-product-architecture.md)、[原文导入后续接入](2026-10-24-team-c-c02-transcript-import-handoff.md#后续接入)。C02 完整公开链路和字幕格式仍未完成，本单元不缩小父目标。

## 设计决定

- 请求必须显式指定 revision，不用会漂移的 head 默认值。导出 sealed 原文或 draft 校对均可，来源/状态随内部快照保留。
- 返回 `TranscriptTextExport`：transcript_id / revision / status / source_revision / source / 私有 content bytes / sha256；固定 `speech-transcript.txt` 和 UTF-8 媒体类型，长度由 bytes 计算。文件名不使用标题/用户路径。该类型不是 wire DTO 或 Book ExportArtifact。
- 不改换行、Unicode、BOM、空校对稿；不添加换行来伪造非空 Artifact。字符上限沿用领域 1,000,000，输出上限 4,000,000 bytes；损坏文本/错误版本身份以无正文的稳定本地错误拒绝。
- 只读，不新增幂等键或持久化导出。固定 revision 下载与独立保存副本/异步有声书导出分别实现；前者不能冒充后两者完成。
- 真实 PostgreSQL 仅使用既有一次性 socket-only 探针，不触碰共享实例，不变更迁移/schema。读取在删除接受后必须 NotFound；已读入内存的内容不宣称可撤回。

## Review Focus

- 请求版本与 reader 返回身份不一致时，不把其他转写或当前 head 冒充指定版本。
- 未知/删除资源、取消与存储错误不被降级为空文本成功，异常不包含正文或源错误字符串。
- 空 draft、BOM、CRLF、组合字符与非 BMP 字符保真，超限、NUL、孤立 surrogate 明确失败。
- 编码和摘要不占事务；导出不写 receipt、head、修订或对象，不使删除内容复活。
- 公开工作台运行时 validator 仍只有 ProjectCreateRequest/ProjectPatch；不手写替代 validator，不自造字幕时间戳或生产 API。

## Task 1：固定版本导出用例

Files: 新建 `backend/src/speechnexus_backend/workbench/application/transcript_exports.py`、`tests/unit/backend/test_workbench_transcript_exports.py`。

Interfaces: `TranscriptRevisionReader.get_revision(transcript_id: str, revision: int) -> TranscriptRevision`；`TranscriptTextExports(reader).export_revision(transcript_id: str, revision: int) -> TranscriptTextExport`。`TranscriptExportError.code` 固定 `invalid_revision` / `invalid_content` / `revision_mismatch` / `source_unavailable`。

- [x] RED：旧版本/新 head 不混淆、来源不变、所有保真和损坏文本、reader 身份失配、缺失/取消/失败、事务外编码、无写入。
- [x] GREEN：实现只读 port、冻结私有快照、精确编码/摘要与安全失败。

## Task 2：持久化消费与交接

Files: 新增 `tests/integration/test_workbench_transcript_exports_postgres.py`；将其加入现有 `scripts/dev-env/workbench-pg-probe.py` 的精确测试列表；新建本单元 handoff，更新索引/goal。

- [x] 真实隔离 PG：原始导入 sealed→校对 draft→固定旧/新版本导出；原文/来源不变，非 ASCII/换行/空稿；删除接受后新导出拒绝。不写新表或绑定服务。
- [x] Python format/lint/mypy/imports、单元/契约套件和隔离 PG 探针；必要变异证明版本身份检查有效，一次独立审查后重要问题单次 RED→GREEN。
- [x] 记录 A00 具体生成入口与公开文本/字幕导出契约缺口；累计补丁干净基线应用、逐文件摘要、真实 index 不变，完整 goal 保持 active。
