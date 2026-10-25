---
title: "SpeechNexus C00 项目与稿件元数据分页"
status: active
version: "0.1.0"
date: 2026-10-24
---

# C00 元数据列表实施计划

沿用[团队 C 目标](2026-10-24-team-c-goal.md)、[版本交接](2026-10-24-team-c-c00-version-handoff.md)及 [workbench 契约](../../contracts/workbench-openapi.yaml)的项目和稿件列表范围。基线 `cbf4a8ca1cf808fbc5ff4b125f7ff1e271496995`；已核对快进前后原有 77 个 C 文件完全一致。

## 设计与边界

增加可供公开适配器消费的内部查询用例，按 `(created_at, id)` 降序 keyset 分页，`limit` 为整数 1–100，默认 50，最多读取 `limit + 1` 条元数据。内部位置携带资源种类、父作用域、时间和 ID，拒绝跨列表复用、无时区时间及非法边界。位置不是公开 `cursor`，不作鉴权或不可伪造保证。

项目结果为当前不可变版本；稿件结果采用独立 `TranscriptSummary`，只投影当前 head 的元数据和固定来源引用，不加载正文、历史或执行逐行查询。父项目存在性、其 tombstone、子稿件 tombstone 和稿件当前版本均在一条 SQL 的同一 statement snapshot 中读取；父项目不存在/删除返回资源不存在，合法空项目返回空页。

每页是当前数据库视图，跨页不是历史快照：修改不改变创建排序，新插入且位于已读位置之前的项目需刷新首屏才能看到；删除或清理已读锚点后仍可继续。SQL 不锁行、不执行 DDL，复用已存在的复合索引及最小运行角色权限。workspace 作用域仅是共享部署分组，不形成用户隔离。

本单元不改 A 的生成产物、组合根、迁移 runner、共享锁或部署。公开排序/游标及非法游标错误语义须由 C 提供契约变更并经 A 评审；生成响应入口尚缺，不能手写替代。HTTP/公开游标编码和客户端接入在后续完成，C00/C08 仍未完成。已有目标授权覆盖内部实现选择，不重复请求批准；不创建 commit/push。

## Task 1：内部页与应用用例

文件：新增 `domain/listings.py`、`application/listings.py`、`tests/unit/backend/test_workbench_listings.py`；扩展 `application/version_ports.py`、`application/versions.py`、`tests/fake/workbench_versions.py`。

- [x] 编写并运行失败测试：并列时间无漏项/重复、父作用域、默认/边界 limit、错误位置、空页、缺失父项目、修改与插入、元数据不包含正文。
- [x] 实现 `list_projects(workspace_id, limit=50, after=None)`、`list_transcripts(project_id, limit=50, after=None)`，返回 `ListingPage[T]`；`next_position` 仅在存在额外行时出现。
- [x] 运行集中单元测试，观察 GREEN；验证校验在事务前完成。

## Task 2：真实 PostgreSQL 查询与交接

文件：扩展 `infrastructure/postgres_versions.py`；新增 `tests/integration/test_workbench_listings_postgres.py`，由 C 自有 `scripts/dev-env/workbench-pg-probe.py`纳入隔离运行。

- [x] 先写真实运行角色测试并观察缺失实现失败；覆盖排序、scope、当前版本、来源引用、百万字正文、删除及清理锚点、父项目缺失/空列表、单语句投影与并发提交可见性。
- [x] 实现有界投影，验证只读查询在写事务行锁期间可完成；不引入 N+1、OFFSET 或内容日志。
- [x] 运行 Python 格式/lint/mypy/imports、单元/契约套件及隔离 socket-only PostgreSQL 探针，确认集群停止；针对遗漏过滤进行独立进程 mutation 证明。
- [x] 按 executing-plans 对本单元做一次独立只读审查，实质问题 RED→GREEN 修复；交接注明实际证据和未验证项，打包全部 C 文件并应用校验。

## 审查重点

1. 删除与列表并发时，不能出现父项目不存在而仍返回子内容的混合 statement snapshot。
2. 当前 head 的版本号、更新时间与来源引用一致，列表不得物化正文或全部历史。
3. 并列时间、不同 scope、被清理的锚点和新插入均不产生错误的跨页身份。
4. `has_more` 证据来自额外一行，不能因过滤删除后空洞产生伪下一页。
5. 内部位置不是公开协议、鉴权或真实端到端资格；公共契约依赖必须明确记录。

执行使用既有 writing-plans、executing-plans、TDD 和 PostgreSQL 技能。本机共享 helper 缺失，继续手动 ledger `.runtime/team-c/c00-listing-progress.md`，保留与其他计划隔离的记录。

内部实现、验证及一次独立审查已完成；完整边界和证据见[交接](2026-10-24-team-c-c00-listing-handoff.md)。最终审查未发现 P1/P2/P3，公开协议/HTTP、跨页非快照及首屏刷新语义继续按文档保留，不关闭 C00 或持续目标。
