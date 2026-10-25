---
title: "团队 C 交接：稿件创建消费者缺陷修复与稿件真实浏览器资格"
status: active
version: "0.1.0"
date: 2026-10-25
---

# 团队 C 交接：稿件创建消费者缺陷修复与稿件真实浏览器资格

把"稿件/校对"写作面接进真实运行时浏览器资格时，**发现并修复了一个真实生产缺陷**：在真实部署上，手工新建稿件永远无法进入编辑器。缺陷由 Web 消费端与契约 fixture 共同写错，单元测试因此一直通过。

## 基线

- `base_sha` = `head_sha` = `4b08c396c0fe8fc43c7e85683e534befab6bb22b`（未提交、未推送）。
- 上一冻结单元 `C08-real-runtime-model-browser-qualification`（446 路径，补丁 `bde365e6…`）。
- 本单元 `C02-transcript-create-consumer-fix-and-authoring-browser-qualification`。最终哈希见 `.runtime/team-c/team-c-current.manifest.json`。

## 缺陷与根因

真实服务端对**手工**稿件返回原始修订 `status="draft"`（契约正文明确"manual transcript starts as a draft revision"；领域规则 `"draft" if source is None else "sealed"` —— 只有语音制品导入才是 `sealed`）。但：

- `web/workbench/src/manuscripts/workbench-client.ts` 的 `createTranscript` 无条件断言 `status === "sealed"`，导入与手工共用一条断言；
- `tests/fixtures/contracts/workbench-http.json` 的 `createTranscript-response-declared-201` 把**手工**请求配了 `sealed` 响应，示例自相矛盾；
- 两个 Web 测试 fixture 也把 revision 1 写成 `sealed`。

后果：手工新建稿件得到 201，却被客户端判为未知结果 → 页面显示"提交结果尚未确认。已保留原请求，请按原键重放" → 重放仍得到同一响应 → **永久卡在未确认状态**，`校对草稿` 编辑器不会出现（用户可见的功能完全不可用）。

## 本轮改动

- 契约 fixture：`createTranscript-response-declared-201` 的修订改为 `draft`，与其手工请求示例一致。
- 客户端：断言按来源分支——手工 → `draft`，语音制品导入 → `sealed`；两者都要求 `source_revision === null`。
- 界面：`ManuscriptHost` 的"已保存"文案改为按**修订号**区分（原始修订 vs 追加修订），不再用 `status` 区分（手工原稿本就是 draft）；创建表单文案由"原稿保存后封存"改为"原稿保存后成为初始修订"。
- 测试：新增客户端用例（接受手工 draft、拒绝手工 sealed）；两个 Web fixture 的 revision 1 改为 `draft`。
- 新增 `scripts/dev-env/workbench-manuscripts-real-probe.mjs` + `tests/integration/test_workbench_real_runtime_manuscripts_browser.py`，并纳入常规 `--browser` 选择集。

## 证据

| 证据 | 环境 | 结果 |
| --- | --- | --- |
| 稿件真实浏览器用例 | 真实 Chromium + 真实生产包 + `create_runtime_app` + 隔离 PG | **通过**（4.65s） |
| Workbench Web 测试 | vitest | **563 通过**（原 562，新增 1 条缺陷用例） |
| 契约检查 / 契约测试 | `make contracts-check`、`tests/contract` | 28 文件通过；**1715 通过** |
| Web 门禁 | `pnpm format:check`、`pnpm lint`（含设计 Token）、`pnpm typecheck`、`pnpm build` | 全绿 |
| `--browser` 全量探针 | 隔离 PG + MinIO + 四份浏览器用例 | 见本轮 manifest 记录 |

用例内真实交互：通过 UI 创建项目与原稿；`核对当前版本` 是保存前置门槛；提交修订后页面显示"编辑来源版本 2，保存基线版本 2"；读取固定版本 1 后显示"编辑来源版本 1，保存基线版本 2"与"正在查看历史版本…"；从历史版本保存的请求携带 `source_revision=1`、`expected_revision=2`，保存后新草稿重基到 3；刷新后本地草稿保留；360px 无横向滚动；零 4xx/5xx、零 pageerror、零出站。

## 未验证与边界

- 仍不是语音、真实设备、音色或真实 A/B 的证据；也不证明 Linux 与 GPU。
- 既有合成稿件探针 `workbench-manuscripts-probe.mjs` **从未被任何测试调用**，其 locator 已过期（`服务端最新版本` 实为冲突面板标签；创建后需先"核对当前版本"才会出现稿件列表）。已如实记录，未删除；真实后端资格由新探针承担。
- 本轮的 UI 文案与断言取自当前实现，属于消费者契约的一部分，后续改动需同步。

## 下一接入动作

1. 纯 C 收尾：保存副本 `getSavedAsset` 回读；导出 worker 预接线（配置默认关闭）。
2. 独立切片：音色候选/试听/校验/发布 UI 状态、助手 UI"语音底座未配置"诚实态、有声书 TXT/章节/角色领域、Admin 诊断/观测与 capability 三态。
3. A/B 制品就绪后接入下载/事件流、Job API 生成派发、A06/B05 音色、公开实时与真实设备、A07 迁移部署。
