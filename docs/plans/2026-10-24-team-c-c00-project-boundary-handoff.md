---
title: "SpeechNexus C00 生成项目请求消费交接"
status: in-progress
version: "0.1.0"
date: 2026-10-24
---

# SpeechNexus C00 生成项目请求消费交接

本单元把 A 已发布的两个生成请求接入工作台版本用例；C00、C08 和[团队 C 目标](2026-10-24-team-c-goal.md)仍未完成。此前版本、数据库和删除证据分别保留在[版本交接](2026-10-24-team-c-c00-version-handoff.md)、[PostgreSQL 交接](2026-10-24-team-c-c00-postgres-handoff.md)和[删除交接](2026-10-24-team-c-c00-deletion-handoff.md)。

## 身份与契约

- 基线/当前 HEAD：`e09abadf87fbc202b25e78b2698bdf9e95764adb`，分支 `codex/team-c-c00-versions`；C 的交付仍在未提交工作区，没有本单元的提交或远端更新。
- 消费 [A PR #51](https://github.com/hrygo/SpeechNexus/pull/51)，生成产物提交 `88fdacccc5372b7fdd077441490f40bd1dfc78ba`；[workbench OpenAPI](../../contracts/workbench-openapi.yaml) 的版本为 `0.1.0.dev0`。
- [bindings manifest](../../contracts/bindings.json) 中 workbench 的运行时入口目前只有 `ProjectCreateRequest` 和 `ProjectPatch`。TypeScript 的全量 OpenAPI 类型不表示 Python/JS 已有全量运行时 validator。
- 本轮没有改动机器契约、生成器、生成产物、共享锁文件或组合根。新基线依赖通过锁定安装；此前 C 的 60 个交付文件在 fast-forward 前后逐文件校验一致。

## 实现与验证边界

[ProjectCommandBoundary](../../backend/src/speechnexus_backend/workbench/interfaces/project_commands.py) 先调用 `speechnexus_contracts.workbench.parse`，再映射到已有 `WorkbenchVersions`。不移除未知字段、不补写请求、不把字符串或布尔值当 revision；无效 body 在开启业务事务前被拒绝。

创建时由组合根显式提供 `default_workspace_id`，合法 body 中的 `workspace_id` 优先。这是本地接线选择，没有硬编码公共默认值，也不证明工作区存在或其删除并发已协调。公开创建入口上线前，A/C 仍需冻结工作区选择规则并接入权威生命周期检查。

PATCH 把缺省字段映射为领域 `Unset`，保留显式 `description: null`；映射 metadata revision 前提，保留同键历史快照、异意图冲突、未找到与 CAS 冲突。JSON Schema 接受整数值 `1.0`，所以有效 revision 在 DTO→domain 边界规范为 `int`，避免 `1.0` 与 `1` 形成不同的幂等摘要，原请求保持不变。

返回值仍为领域快照。本单元没有注册 HTTP 路由，不提供公开响应 DTO、错误映射或 path/header validator；组合根、迁移执行和真实 Web 消费尚未接入。

## 实测证据

核验于 2026-10-24（Asia/Shanghai），macOS、Python `3.14.9`、PostgreSQL `18.6`。仅使用合成文本；数据库探针使用一次性、Unix socket-only 集群，并已确认停止，未访问共享数据库。

| 检查 | 结果与范围 |
| --- | --- |
| `uv run --locked pytest tests/unit/backend/test_workbench_project_boundary.py -q` | 27 passed；缺省/null、严格输入、工作区作用域、并发 CAS、原始快照重放、未找到、整数值规范化 |
| `uv run --locked pytest tests/unit tests/contract -q` | 607 passed；不包含真实服务集成测试 |
| `uv run --locked python scripts/dev-env/workbench-pg-probe.py --pg-bin /path/to/postgresql/bin --coverage --output .runtime/team-c/c00-project-boundary-pg-probe-final.json` | 50 passed，无跳过；workbench infrastructure 分支/语句合计覆盖率 90.19%；新增生成请求→真实运行角色→版本/receipt 的消费链测试 |
| `uv run --locked ruff format --check .` / `uv run --locked ruff check .` | 222 个文件格式通过；lint 通过 |
| `uv run --locked mypy` / `uv run --locked lint-imports` | 156 个源文件通过；7 个依赖契约保持 |
| `make contracts-check` / `make contracts-drift` | 28 个契约通过；生成产物无漂移 |
| `pnpm test` | workbench 102、admin 3、contracts 105，共 210 passed |
| `pnpm format:check` / `pnpm lint` / `pnpm typecheck` / `pnpm build` | 均通过；不是新增业务 HTTP 或真实浏览器联调证据 |

先观察缺失适配器导致测试失败，再实现。整数值回归先观察 `IdempotencyConflictError`，修复后通过，并在最终 PG 用例中验证 `1.0` 写入、`1` 同键重放。另在独立进程删除缺省 description 的 `Unset` 映射，回归测试按预期失败；源文件字节未变。

独立只读审查复现同一整数值 P2，未发现其他问题；主代理完成其 RED→GREEN 和最终套件验证。图谱 `index_status` / `check_index_coverage` 返回本仓库未索引，结论来自直接源码检查。

最终 PG 证据位于 `.runtime/team-c/c00-project-boundary-pg-probe-final.json`，其引用的 `pytest.log` / `coverage.json` 记录完整结果。全量 C 补丁位于 `.runtime/team-c/team-c-current.patch`，基线、逐文件摘要和补丁摘要见同目录 `team-c-current.manifest.json`；包含新增文件，交接时在基线副本中应用并比对，不代表已跨机集成。

## 剩余接入

1. A00：补齐 C00 所需运行时入口，包括 `Project` / `ProjectList`、`TranscriptCreateRequest` / `TranscriptRevisionCreateRequest`、`Transcript` / `TranscriptRevision` / `TranscriptView` / `TranscriptList`，以及相关错误和删除契约。C 消费同版生成产物，不另建公开 DTO 语义副本。
2. A/C：冻结工作区选择与存活检查、错误状态映射，串行接入迁移框架和组合根；随后注册公开路由并用生成客户端验证。当前 body adapter 不代替这些步骤。
3. C：继续稿件来源核验、列表/游标、导出、删除 worker 调度和恢复账本，并把工作台实际消费接入文件闭环。真实浏览器/设备、Linux 后端和 GPU 联调仍分别待验，不关闭任何 Gate。
