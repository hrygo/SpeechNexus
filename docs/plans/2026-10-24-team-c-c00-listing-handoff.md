---
title: "SpeechNexus C00 项目与稿件元数据分页交接"
status: active
version: "0.1.0"
date: 2026-10-24
---

# C00 元数据列表交接

已实现 C00 内部项目/稿件列表用例和真实 PostgreSQL 查询，供后续公开路由接入；C00、C08 和[团队 C 持续目标](2026-10-24-team-c-goal.md)仍未完成。范围及设计见[实施单元](2026-10-24-team-c-c00-listing-plan.md)。

## 基线与契约

- 本地 `base_sha = head_sha = cbf4a8ca1cf808fbc5ff4b125f7ff1e271496995`。原有 77 个 C 文件逐字节校验后，安全快进 B02 控制器交付；无 commit/push，改动以累计补丁交接。
- workbench OpenAPI `0.1.0.dev0`，SHA256 `38b8f6776392f541c0be085b5fedd49937f3c16486934d55970d154309390068`。本轮没有改变公开协议或生成产物。
- `contracts/bindings.json` SHA256 `7fba8b209e634442621e30cd6277f984ca9547f1baee9883520d131fa6468c3c`；A 的 workbench Python runtime profile 仍只有 `ProjectCreateRequest` / `ProjectPatch`。缺少响应与稿件入口，见[上轮依赖清单](2026-10-24-team-c-c00-project-boundary-handoff.md#剩余接入)。

## 实现与语义

`WorkbenchVersions.list_projects(workspace_id=..., limit=50, after=None)` 和 `list_transcripts(project_id=..., limit=50, after=None)` 返回内部 `ListingPage`。有效 `limit` 是整数 1–100（拒绝 bool/float），内部 `ListingPosition` 绑定种类和父作用域；无时区时间、空/过长 ID 及作用域错配在开启事务前拒绝。位置仅是内部 seek 状态，未编码为公开 `cursor`，也不构成身份或安全隔离。

排序为 `(created_at, id)` 降序，数据库最多返回 `limit + 1` 条元数据，以额外一行决定是否给出 `next_position`。元数据修改不会改变创建排序；锚点物理清理后仍能续页。跨页不是历史快照，新提交且位于已读位置之前的项目需刷新首屏才能看到。每页数据与“还有下一页”均描述其查询时点，后续删除可能使下一页变空。

项目列表 join 当前元数据版本；稿件列表投影 `TranscriptSummary` 的身份、展示名、当前 head 号、固定 Artifact/receipt 引用及创建/更新时间。查询不加载正文和历史、不逐行调用详情。稿件父项目存在性、parent/child tombstone、当前 head 使用一条 SQL 的同一 statement snapshot。已删除/缺失父项目报资源不存在，存在的空项目返回空页；删除在查询完成后提交不会追溯改变该次已读取的快照。

SQL 使用参数、限定 schema 和已有 `(workspace_id, created_at, id)` / `(project_id, created_at, id)` 索引结构，不新增 DDL、权限或依赖，不锁业务行。workspace 是共享部署分组，不是个人隔离。生产组合根、公开协议适配和迁移 runner 仍未接入。

## 本轮实测

核验日期：2026-10-24（Asia/Shanghai）；数据库探针 UTC 时间 `2026-10-23T19:31:04.465566+00:00`。

| 检查 | 实际结果 |
| --- | --- |
| `uv sync --locked --all-packages --all-groups` | 锁定同步完成，适配远端更新的 node-controller 本地包 |
| `uv run --locked pytest tests/unit/backend/test_workbench_listings.py -q` | 16 passed；先观察缺模块 RED，再实现 GREEN |
| `uv run --locked pytest tests/unit tests/contract -q` | 674 passed，32.58s；包含远端新增控制器用例 |
| `uv run --locked ruff format --check .` / `uv run --locked ruff check .` | 249 文件格式通过，lint 通过 |
| `uv run --locked mypy` / `uv run --locked lint-imports` | 178 源文件通过，7 条依赖约束 kept |
| `make contracts-check` / `make contracts-drift` | 28 个契约通过；生成产物 up to date |
| `uv run --locked python scripts/dev-env/workbench-pg-probe.py --pg-bin /opt/homebrew/bin --coverage --output .runtime/team-c/c00-listing-pg-probe-final.json` | PostgreSQL 18.6，57 passed、无 skip；workbench infrastructure 分支/语句合并覆盖 90.84%；socket-only 集群已停止 |

PG 新增 7 项：并列时间与 scope；当前 head/来源引用；实际撤销正文 SELECT 权限后仍可读百万字稿件的元数据；项目和稿件 tombstone 在 LIMIT 前过滤；清理已读锚点后继续；锁住项目/稿件并更新未提交版本时，列表不等待且只见已提交版本；新插入的 seek 行为；查询结果读取后、返回前并发完成级联删除及清理，仍维持单语句快照，下次读取返回资源不存在。这些由多个测试组合覆盖，不表示已完成 HTTP 或负载资格。

探针 JSON 为 `.runtime/team-c/c00-listing-pg-probe-final.json`，集群目录 `.runtime/team-c/pg-vyzesdb0` 含日志/覆盖报告。开发环境初次失败来自未来 UTC fixture 和未完成子清理的级联前置，已修正 fixture；没有放宽时间单调性或删除资格。

Mutation 在独立 Python 进程中把项目 tombstone 谓词改为错误资源种类：删除过滤测试确实失败，捕获 `project-002` 泄漏。`.runtime/team-c/c00-listing-mutant.log` / `c00-listing-mutant-probe.json` 记录失败与集群停止；生产源码 SHA256 前后一致。

一次独立只读审查覆盖本单元源代码、测试和设计，未发现 P1/P2/P3；审查者独立运行 16 项单测及 diff 检查通过，未重跑 PG。本轮未重复执行原有 Web/浏览器探针，也未验证 Linux、生产服务、HTTP、跨浏览器/真实麦克风、GPU、部署负载和恢复资格。

## 后续接入与边界

1. C/A00：冻结公开列表排序、cursor 作用域/版本/编码与错误语义，补齐查询参数拒绝路径。目前两个 GET 未声明 `400`；本单元不自行决定公开非法游标返回码，也不把内部 position 暴露为客户端协议。
2. A00：补齐生成 `Project` / `ProjectList`、稿件请求/响应、版本、列表和错误入口。C 继续消费生成边界；不能手写 response DTO 或独立 validator 替代。
3. A/C：工作台 HTTP、默认 workspace 配置、服务开关、模块迁移和生产组合根接入；验证真实公共客户端可分页、冲突和删除恢复。内部检查不证明 workspace 生命周期、认证或公开路由已经实现。
4. C08/A07：对象区、独立删除账本/恢复水位及开放读取前重放屏障、生产恢复演练继续待交付。已做的是当前版本表的数据库删除隐藏与清理，不是完整备份恢复证明。

累计补丁 `.runtime/team-c/team-c-current.patch` 与 manifest 包含先前 C 单元及本轮新增文件。实际文件数、base、SHA256、应用检查及逐文件比对结果以 manifest 为准，不从 tracked diff 的摘要推断新增文件已交接。
