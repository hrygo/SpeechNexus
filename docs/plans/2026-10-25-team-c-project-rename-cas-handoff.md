---
title: "团队 C 交接：项目改名（元数据版本 CAS）与一处诚实性修正"
status: active
version: "0.1.0"
date: 2026-10-25
---

# 团队 C 交接：项目改名（元数据版本 CAS）与一处诚实性修正

`updateProject`（`PATCH /v1/workbench/projects/{project_id}`）此前没有任何 Web 消费者：项目能创建、能选择，但**不能改名**。本轮补齐，并顺带修掉一个把"明确冲突"报成"结果未确认"的诚实性缺陷。

## 一个刻意的落点选择

改名**没有**接进 `ManuscriptHost`（那里已经有"项目名称/创建项目"）。原因是 manuscripts 的编辑状态由**持久恢复日志**（`editor-journal`）承载，其记录形状是精确校验的；为改名加一个表单字段会改变日志格式，从而可能让用户**在飞的未保存草稿**被判为不可恢复。为一个改名功能冒丢稿风险不划算，因此落在**没有日志的书籍页项目栏**（`BooksHost`），用组件内状态即可。

## 契约语义（实现前先读）

`ProjectPatch` 要求 `expected_metadata_revision`（≥1）且 `minProperties: 2`（必须至少再带一个字段），可选 `name`/`description`/`status`；支持 `Idempotency-Key`；响应是 `Project`，其 `metadata_revision` 随成功更新推进。

## 本轮改动

- `workbench-client.ts`：新增 `updateProject(projectId, body, key)`，断言返回 `id` 与请求一致，并断言 `metadata_revision > expected_metadata_revision`——**返回的修订没有前进就意味着这次修改没有被应用**，不能让界面显示一个并未发生的改名。
- `BooksHost.tsx`：项目栏新增"新项目名称 + 重命名项目"；成功后用**服务端返回的项目**替换列表项（不猜新名字）；失败时给出诚实文案并**保留请求键**；被拒绝的新名字**绝不显示为已保存**。
- **诚实性修正**：`BooksHost` 的错误映射此前没有 `revision_conflict` 分支，409 会落到 `unknown`（"结果尚未确认。请核对连接后按原键重试"）——把一次**明确的版本冲突**说成"结果未知"，会诱导操作者盲目重试。现在映射为 `conflict` 并给出"该项目已被修改（修订冲突）。请刷新后按新修订重试，本页不会自动重试。"

## 证据

| 证据 | 内容 | 结果 |
| --- | --- | --- |
| Web 单元 | 客户端：路径/方法/`Idempotency-Key`，`id` 不符与**修订未前进**都拒绝；宿主：改名后项目栏显示服务端返回的名字、请求带正确 `expected_metadata_revision`；**409 显示冲突文案、被拒名字不出现、重试沿用同一键** | workbench **591 通过**（588+3） |
| 真实浏览器 | 生产包 + 真实组合根 + 隔离 PG：界面改名成功（项目栏显示新名）→ **测试在带外推进部署的元数据修订** → 界面用旧修订再次改名 → 显示"修订冲突"、被拒名字不出现在项目栏；随后真实 HTTP 复核：部署里存的是**带外注入的名字**、`metadata_revision=3`——**旧修订的改名没有赢** | 通过 |
| 全量探针 | 本轮为扩展既有浏览器用例 | **398 通过** |
| 门禁 | `pnpm format:check`/`lint`/`typecheck`/`test`（workbench 591、admin 118）/`build`、`ruff format --check .`（1133 文件）、`ruff check .`、`mypy`（528 文件） | 全绿（format:check 首次失败于新测试文件未格式化，已 `--write` 后复跑） |

制品：`.runtime/team-c/browser/real-runtime-chapter-create/`（新增 `rename-conflict.png`）。

## 过程修正

- 新测试引用了不存在的 `project()` 夹具助手（`ReferenceError`）；改为从 `setup` 返回的**真实 fixture 项目列表**取第一项，断言随 fixture 变化而不是硬编码 id。
- 探针的"非 2xx 只能有一条"断言过时（冲突注入必然新增一条 409）；改为钉住"恰好 409 与 503 两条、且各自路径正确"，而不是放宽成"不检查"。

## 未验证与边界

- 冲突是**测试在带外注入**的（真实建链尚无第二个操作者），界面侧的应对是实测的，但"两个真实操作者并发改名"未验证。
- `getVoiceRevision` 仍无消费者（目录摘要缺少退役/禁用原因等单修订字段）。
- 改名只覆盖 `name`；`description`/`status`（归档）未接。
- 浏览器证据来自 macOS + 本机 Chromium，不代表 Linux 后端或目标拓扑资格。

## 下一接入动作

1. `getVoiceRevision`：音色修订核对（含 `retired_at`/`disabled_reason`/`model_binding`）。
2. 项目归档（`status: archived`）与描述编辑，沿用同一 CAS 与冲突文案。
