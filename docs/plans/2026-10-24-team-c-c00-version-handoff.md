---
title: "SpeechNexus C00 项目与转写版本单元交接"
status: in-progress
version: "0.1.0"
date: 2026-10-24
---

# SpeechNexus C00 项目与转写版本单元交接

本页保留首轮领域/应用单元的时点记录。后续生产适配进展与隔离数据库证据见 [C00 PostgreSQL 事务交接](2026-10-24-team-c-c00-postgres-handoff.md)、[删除事务交接](2026-10-24-team-c-c00-deletion-handoff.md)和[生成项目请求消费交接](2026-10-24-team-c-c00-project-boundary-handoff.md)；尚未接入公开 HTTP。

## 交付身份与范围

- 团队/父任务：C / C00 #35；持续目标见[团队 C 目标](2026-10-24-team-c-goal.md)。本单元不关闭 C00 或任何 Gate。
- 工作基线：`8333e4ba7c722a36cc70545fd5a0ed2beee38eea`；本机分支 `codex/team-c-c00-versions`。这是独立开发基线，不替代下一轮共同集成 SHA 的确认。
- 当前代码为未提交工作区改动，没有承载本单元的 `head_sha`，未执行 commit/push 或远端 Issue 更新。
- 本地补丁：仓库根目录下 `.runtime/team-c/c00-version-unit.patch`，包含新增源码、测试和文档；SHA-256 随本轮交接返回。补丁在基线文件副本中通过 `git apply --check` 并应用后逐文件比对，不代表已跨机集成。
- 协议参照：基线中的 `contracts/workbench-openapi.yaml`（`0.1.0.dev0`）和共享 Identifier/Revision 契约；未修改机器契约，未生成或手写公开 DTO/客户端。

## 已实现的独立单元

- `backend/src/speechnexus_backend/workbench/domain/versions.py`：不可变项目/转写/版本；项目 metadata revision 前提、PATCH 缺省与显式 null 区分；原文与校对分离、同转写来源约束、Artifact/receipt 成对引用保留、decimal revision 上界和带时区的单调时间。
- `backend/src/speechnexus_backend/workbench/application/version_ports.py` 与 `application/versions.py`：项目创建/修改、手工转写创建/校对、历史版本读取；短事务内先认领幂等键，再进行版本检查和原子保存；异摘要冲突，同键重放原始快照，成功返回须晚于提交。
- `tests/fake/workbench_versions.py`：仅供测试的复制后提交存储；串行化缺省幂等键与资源写入，失败/取消整笔回滚。产品代码不导入该 fake。

来源 `SpeechSource` 表示已由公开协议适配器核验的引用；本单元没有实现 speech Artifact 下载、receipt 核验或语音导入用例。领域对象可表达导入原文及其后续校对，不代表真实导入已通过。

## 本地验证

核验日期：2026-10-24（Asia/Shanghai）。Python `3.14.9`，macOS；只使用合成文本和测试 fake，未访问数据库、对象存储或模型服务。

| 命令 | 结果与覆盖边界 |
| --- | --- |
| `uv run --locked pytest tests/unit/backend/test_workbench_versions.py tests/unit/backend/test_workbench_version_use_cases.py --cov=speechnexus_backend.workbench --cov-report=term-missing --cov-fail-under=90 -q` | 32 passed；工作台领域/应用单元分支覆盖率 100%，不代表生产事务或 API 已实现 |
| `uv run --locked pytest tests/unit tests/contract -q` | 270 passed；未包含隔离服务集成测试 |
| `uv run --locked ruff format --check .` | 116 files already formatted |
| `uv run --locked ruff check .` | passed |
| `uv run --locked mypy` | 68 source files，passed |
| `uv run --locked lint-imports` | 5 kept，0 broken；workbench 与 speech 保持独立 |
| `make contracts-check` | 28 个契约通过语法验证；不证明新增运行行为已冻结 |

先写测试并观察缺失实现导致失败，再实现并验证通过。另在独立 Python 进程中移除 metadata revision 检查，原有回归测试按预期失败；源文件未变更，证明测试能捕获丢失更新。取消回滚测试在写入后、提交前主动取消任务；并发测试实际争用 fake 事务锁。

独立只读审查未发现必须修复的问题；指出全局 fake 锁会掩盖应用忘记请求行锁的回归。已补三种写入的锁定读取约束及回归测试（先观察 3 项失败再修复）。独立进程中将应用三处 `for_update=True` 改为 `False`，用例测试出现 11 项预期失败；源文件未修改。此证据证明锁请求的回归可被发现，不证明真实数据库锁已生效。

图谱服务没有本仓库索引，`check_index_coverage` 返回 project not indexed，当前只读工具面不提供 `index_repository`；本轮直接读取相关源码，不声称有图谱覆盖证据。

## 待集成与下一步

1. A00：交付生成类型/客户端、运行时 validator、契约样例及其确定版本。C 随后实现边界映射和消费验证，不维护独立 DTO 副本。
2. A/C：按 workbench 契约维护流程确认省略 `source_revision` 时基于当前预期 head 的规则，以及导入原文的 sealed 语义。当前为内部实现选择，尚未开放公共路由。
3. A/C：接入迁移执行框架和工作台独立表/运行角色；实现生产 `VersionUnitOfWork`，验证缺省幂等键认领、行锁、提交未知恢复、不可变历史和删除竞争。数据库时间由 `VersionSession.now()` 提供，不能套用 fake 的固定时钟。
4. C/A07：从首个持久资源接入版本化删除预检/确认、tombstone 和恢复。幂等快照包含业务内容，须受工作台自身保留/清理策略约束；删除后不得经重放重新公开内容。
5. C：补齐项目/转写列表与游标、导出协议缺口、真实 speech Artifact/receipt 适配，以及 C01/C02 fake 页面；C04 合成媒体单元可独立推进。

未验证：生产 PG/MinIO、Linux 后端/镜像、HTTP/生成客户端、浏览器 UI/设备、GPU、跨主机联调、持久删除恢复和未知提交恢复。本轮没有运行态变更；工作包保持未完成。
