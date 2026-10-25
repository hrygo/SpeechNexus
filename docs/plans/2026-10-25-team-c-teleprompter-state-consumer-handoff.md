---
title: "团队 C 交接：部署内提词状态的生产消费者"
status: active
version: "0.1.0"
date: 2026-10-25
---

# 团队 C 交接：部署内提词状态的生产消费者

`getTeleprompterState` / `updateTeleprompterState` 两条操作在 2026-10-24 已按计划接入独立 PG（应用层、schema、适配器、启动检查都在），但**Web 侧没有任何消费者**：`TeleprompterPanel` 只做本地阅读与浏览器存储，副本还写着"不保存到服务端"。生产消费者缺失就是 C 目标完成条件里的缺口，本轮补上。

## 基线

- `base_sha` = `head_sha` = `4b08c396c0fe8fc43c7e85683e534befab6bb22b`（未提交、未推送）。
- 上一冻结单元 `C01-admin-deployment-diagnostics`（470 路径，补丁 `fb3208ef…`）。
- 本单元 `C05-teleprompter-state-consumer`。

## 契约语义（实现前先读）

- `TeleprompterState` = `{object, project_id, script_revision, visible_position, manual_hold, updated_at}`——**不含稿件正文**；正文由稿件本身承载。
- GET 的 404 表示"项目未知或尚未保存"，描述明确要求"**a missing state is not position zero and must not be reported as a successful empty state**"。
- PUT 带 `Idempotency-Key`；严格更旧的 `script_revision` 由后端判 `revision_conflict`（409），且有"更旧修订或更早 epoch 的匹配不得移动当前位置"的语义。
- 请求键长度 8–128（应用层校验）。

## 本轮改动

- `workbench-client.ts`：新增 `teleprompterState`（断言 `project_id` 一致）与 `updateTeleprompter`（断言 `project_id` 一致，且**存储的修订不倒退**：`value.script_revision >= body.script_revision`——更旧就是冲突，不是写入）。
- 新增 `teleprompter/teleprompter-state.ts`：`load` 把 404 归为 `missing`（state 保持 null，绝不补 0 或编造修订）、`save` 把 409 归为 `conflict` 并**保留上一次读取到的状态**、传输失败归为 `unavailable`（结果未确认，提示按原键重放）、卸载后迟到应答不再发布。
- `TeleprompterPanel.tsx`：新增"部署内提词状态"区块——显式"读取部署项目"→ 选项目 → "读取部署状态"（未保存时给 404 说明而不是位置 0）→ 修订/可见位置/手动接管/请求键 → "保存到当前部署"；保存成功后回填服务端返回的值；冲突/拒绝/未确认分别给可核对文案且不自动重试。本地区块与部署区块的 live region 各自命名（`本地提词状态`/`部署提词状态`、`本地提词错误`/`部署提词错误`），使两处状态可分别寻址。

## 证据

| 证据 | 内容 | 结果 |
| --- | --- | --- |
| Web 单元 6 项 | 未保存项目报 missing 且不出现任何位置/修订；读取回填修订/位置/接管；写入被接受并发布；409 保留上次读取且**只发一次请求**；传输失败与拒绝区分；dispose 后迟到应答不发布 | 通过（workbench 574→**580**） |
| 真实浏览器 1 项 | Chromium 156.0.8078.4 + 生产包 + 真实组合根 + 隔离 PG：创建项目 → 打开提词阅读 → 读项目 → 读状态（**404 报"尚无已保存状态"，页面不出现"位置 0"**）→ 保存修订 2/位置 42/接管 → 再把修订填回 1 触发 **409** 且界面仍显示已保存的修订 2 | 通过 |
| 真实 HTTP 复核 | 冲突拒绝后由 HTTP 直读该行：`script_revision="2"`、`visible_position=42`、`manual_hold=true`，即**拒绝没有移动位置** | 通过 |
| 门禁 | 全量 `--browser` 探针 **394 通过**（393+1）、`pnpm format:check`/`lint`/`typecheck`/`test`（workbench 580、admin 118）/`build`、`ruff`、`mypy`(526) | 全绿 |

制品：`.runtime/team-c/browser/real-runtime-prompter-state/`（`result.json`、`events.json`、`stderr.log`、`prompter-state.png`）。探针只出现两个非 2xx，且都是本轮**故意触发**的：未保存状态的 404 与更旧修订的 409；`outbound=0`、`unexpectedErrors=0`。

## 未验证与边界

- 部署状态**不含正文**，因此本消费者不还原文本；本地稿件与浏览器存储仍是独立机制（文案已如实区分）。
- 未接语音跟随/公开实时（依赖 A/B）；未跑真实设备或目标拓扑。
- 本机 Chromium 与 macOS 宿主不代表 Linux 后端资格。

## 下一接入动作

1. 音色候选/试听/校验/发布 UI 状态（试听与发布需 A06/B05）继续。
2. 公开实时接线与真实语音跟随待 A/B 具名制品。
