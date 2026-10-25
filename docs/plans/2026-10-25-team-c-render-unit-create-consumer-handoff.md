---
title: "团队 C 交接：渲染单元创建补线（并把消费者清点再收紧一格）"
status: active
version: "0.1.0"
date: 2026-10-25
---

# 团队 C 交接：渲染单元创建补线（并把消费者清点再收紧一格）

## 清点方法再收紧一次（并因此划掉一个"假缺口"）

第 68 轮把比对从"只比路径"改成"方法 + 路径"，查出 7 条。本轮再收紧一格：**排除测试文件**——测试里出现的调用不算生产消费者。结果剩 **6 条**：

| 操作 | 判定 |
| --- | --- |
| `createRenderUnit` | **真实功能洞，本轮补齐**（章节能建、单元不能建 → 有声书仍然产不出任何东西） |
| `updateProject` | 真实缺口（项目改名未接） |
| `getVoiceRevision` | 轻微缺口：目录只给摘要，缺 `retired_at`/`disabled_reason`/`model_binding` 等单修订字段 |
| `getTranscriptRevision` | **不是缺口**：`GET /transcripts/{id}` 带 `revision` query，界面的"读取固定版本"走的就是它（既有真实浏览器探针已断言历史原文可还原） |
| `getChapter` / `getRenderUnit` | 不是缺口：列表已给全字段，且本轮浏览器用例用 HTTP 直读单条做复核 |

也就是说：**"某个操作没有 Web 消费者"不能直接当成缺陷**——必须先确认是否有等价消费者。这条比上一轮的结论更重要，因为它防止我为"看起来缺"的东西造重复界面。

## 基线

- `base_sha` = `head_sha` = `4b08c396c0fe8fc43c7e85683e534befab6bb22b`（未提交、未推送）。
- 上一冻结单元 `C07-chapter-create-consumer`（481 路径，补丁 `9cc94cbb…`）。
- 本单元 `C07-render-unit-create-consumer`。

## 契约语义（实现前先读）

`RenderUnitCreateRequest` 要求 `ordinal`（≥0）/`text`（≤200,000）/`voice{id,revision}`/`model`/`model_binding{id,revision}`，可选 `pronunciation`；响应 `RenderUnit` 带 `chapter_id`、`status`、`revision`。**登记单元不需要活动音色目录**——本轮实测：静态部署（语音底座未配置，`GET /voices` 503）下显式填写 pin 仍返回 201。

## 本轮改动

- `workbench-client.ts`：新增 `createRenderUnit(chapterId, body, key)`，断言返回的 `chapter_id` 与请求一致。
- `RenderUnitPanel.tsx`：新增"新增渲染单元"表单（序号/文本/音色 id+修订/模型/绑定 id+修订），并在**音色目录可用**时提供"从已发布音色填入"下拉；目录不可用时表单**依然可用**（显式登记引用），并在表单说明里写明"能否生成由部署决定"。成功后并入单元列表、给出"单元 N 已创建为修订 M"提示，**失败保留请求键**。
- 未接生成：新单元状态为 `pending`（登记完成），生成仍是 A 的 `generateRenderUnit`。

## 证据

| 证据 | 内容 | 结果 |
| --- | --- | --- |
| Web 单元 | 客户端：路径/方法/`Idempotency-Key`，`chapter_id` 不符时拒绝；面板：按输入的 pin/模型/绑定创建并出现在列表；**被拒后重试沿用同一键** | workbench **588 通过**（585+3） |
| 真实浏览器 | 同一生产包流程继续：建书 → 建章节 → **新增渲染单元**（显式 pin）→ 页面显示该单元与"修订 1"提示；随后真实 HTTP 复核：单元列表恰为 `(文本, 0)`、`voice`/`model`/`model_binding` 与输入**逐字段相等**、`status="pending"`、`GET /render-units/{id}` 与列表逐字段相等 | 通过 |
| 全量探针 | 该用例已在 `--browser` 门禁内（本轮为扩展而非新增） | 见报告 |
| 门禁 | `pnpm format:check`/`lint`/`typecheck`/`test`/`build`、`ruff format --check .`、`ruff check .`、`mypy` | 全绿 |

制品：`.runtime/team-c/browser/real-runtime-chapter-create/`（新增 `unit-created.png`）。

## 三处过程修正（都是断言/用法，不是实现）

- `splitVoiceKey` 返回对象而非常量元组，我按 `[id, revision]` 解构 → `tsc` 报 `TS2488`；改为取 `pin.voiceId/voiceRevision`。
- `VoiceSummary` 的字段是 `voice_id` 不是 `id`（目录摘要与单修订结构不同名）→ 选项键与值随之修正。
- 我预期新单元 `status == "draft"`，实测是 `"pending"`；改断言并在注释里写明"登记≠生成"。

## 未验证与边界

- 只有 **UI 登记** 有真实证据；**生成**（`generateRenderUnit`）仍由 A 负责且未绑定，因此"界面能产出音频"不成立，界面也没有这样声称。
- `updateProject`、`getVoiceRevision` 仍无消费者（前者真实缺口，后者缺单修订字段）。
- 浏览器用例在 macOS + 本机 Chromium 上运行，不代表 Linux 后端或目标拓扑资格。

## 下一接入动作

1. `updateProject`：项目改名（版本 CAS + 冲突文案）。
2. `getVoiceRevision`：在音色修订核对处按 revision 读取（含退役/禁用原因）。
3. 等 A 的 `generateRenderUnit` 与音色协议就绪后接真实生成。
