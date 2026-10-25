# C05 固定稿件封存与持久恢复实施计划

> 执行：沿用已授权 native inline / TDD，按 executing-plans 实施，一次独立只读终审；不提交或推送。

**Goal:** 让工作台把当前 draft 原样或以显式最终正文封存为新的不可变 revision，供会议保存和后续固定源消费者使用；保留全部历史并验证真实事务/删除/重放。

**Architecture:** 在既有 Transcript / WorkbenchVersions 上新增 seal_transcript 和 get_sealed_revision；复用短事务、头版本 CAS、历史回执和删除屏障。PG v2 owner-only 迁移片段允许后续 sealed revision 和独立 transcript.seal 回执；runtime 不执行 DDL。尚不注册公开会议/封存路由或复制 DTO。

**Tech Stack:** 当前 Python 3.14、psycopg、PostgreSQL 与现有 pytest/fake；不新增依赖。

**Spec:** [三队 C05/C06](2026-10-23-three-team-delivery-plan.md#43-c-团队应用与体验)、[产品 9.1](../speechnexus-product-architecture.md#91-转写与会议)、现行 workbench MeetingSealRequest / TranscriptRevision。这是完整 C05 的必要基础，不是完整会议交付。

## Global Constraints

- 保留当前159文件；base/head dc14cf8，契约/生成/锁和公共组合根不改。A/B 上游 README 并行修改仍不自动合并。
- domain 不依赖生成 DTO/数据库；工作台不写底座表；不启动共享数据库、模型或服务。验证只新建 socket-only 临时 PG 并停止。
- 原文、校对、封存分别不可变。封存追加 revision，不 UPDATE 旧正文/状态；原 Artifact/receipt 配对保留，无声音/时间戳推测。
- 新方法仅接私有已类型化参数。公开请求/响应、path/header/错误映射与生成入口仍待 A/C 冻结；不把这些方法当 wire validator。
- 模块迁移为 C 提供的 owner-only v2 fragment，由 A 串行纳入生产迁移；不擅自改旧 v1 fragment 或自动升级生产。

## Task 1：封存领域与用例

Files：修改 domain/versions.py、application/versions.py；新增 tests/unit/backend/test_workbench_transcript_sealing.py。

Interfaces：Transcript.seal(expected_revision:int, revision_id:str, now:datetime, text:str|Unset=Unset.VALUE) -> Transcript；仅当前 draft 可封存，已有 sealed 拒绝重复新建（同键重放先于检查）。新 number=head+1、status sealed、source_revision=旧 head.number，text 缺省保留/空串显式清空，来源配对不变。WorkbenchVersions.seal_transcript 同名参数加 transcript_id/idempotency_key；get_sealed_revision 只取明确版本、拒绝 draft，不随 head 漂移。

- [x] RED：新增测试覆盖原稿/校对/来源不变、显式空稿、Unicode 原样、NUL/surrogate/超限拒绝、严格 int 非 bool、CAS/溢出/时间、重复 sealed 拒绝、定版读取与 body repr 保密。
- [x] GREEN：新增领域方法和短事务用例；复用 _mutate 原历史回执，但 transcript.seal 独立 scope，省略/显式正文在 intent 中区分。
- [x] RED→GREEN fake：同键并发只发布一次、异键 seal/revise 竞争、旧键重放历史、同键改输入冲突、失败/取消原子回滚、原版本导出和明确 sealed 读取。
- [x] Run `uv run --locked pytest tests/unit/backend/test_workbench_transcript_sealing.py -q`；Expected 全通过。

## Task 2：PG 追加与 v2 升级

Files：修改 infrastructure/postgres_versions.py、scripts/dev-env/workbench-pg-probe.py；新增 infrastructure/sealing_schema.py、tests/integration/test_workbench_sealing_postgres.py。

Interfaces：sealing_migration_sql(schema)->sql.Composed，由 owner 在显式事务执行，v1→v2 一次升级，既有 v1 SQL 不改；旧内容与回执保持。adapter 对 sealed 只接受 previous.seal 生成的准确快照，新增独立回执 scope/target。新 PG module 复用既有严格一次性 Store fixture；只该 module 在 fixture 中执行 v2。

- [x] RED：未实现/未升级时真实 PG 场景失败；Expected 模块缺失或封存不可用，不计环境失败为 RED。
- [x] GREEN：保留 v1 历史的升级、旧原始回执重放；draft→sealed 原子提交与历史导出；同键并发/异键CAS、实际提交成功但确认丢失后显式原键恢复、删除接受/cleanup 后拒绝读取和重放、runtime 无 DDL/旧正文UPDATE权限；未升级 adapter 拒绝写入、不偷偷升级。
- [x] Run 一次性 PG probe --coverage；Expected 全通过无 skip、退出后 stopped=true，runtime 角色无 DDL，不访问本机共享实例。
- [x] Run Python format/lint/mypy/imports、unit/contract、contracts-check/drift；Expected 全通过。Web 未变则复用上一轮454及实际浏览器证据，不扩大其范围。
- [x] mutation 实验并恢复源码；一次独立只读终审；Critical/Important/Minor 均为0，无需修复。
- [x] 更新交接/目标/索引；累计包检查归属/hash、干净基线apply、链接及真实index。Goal仍active。

## Review Focus

1. 追加 sealed 保留旧 draft/原文与 Artifact/receipt；不存在改旧 revision 的旁路。
2. 同键恢复先于 stale head；省略与空正文不混淆；新 scope 不会回放 revise 的不同操作。
3. PG v2 迁移在保留旧数据时通过；未升级或漂移不能自动接受，runtime 无DDL，生产仍需A集成。
4. 提交未知、竞争、取消、删除和清理不产生重复版本/遗留回执或正文复活。
5. sealed source 是明确历史版本，不跟随新head；本单元不授予会议/实时/LLM/字幕和Linux部署资格。
