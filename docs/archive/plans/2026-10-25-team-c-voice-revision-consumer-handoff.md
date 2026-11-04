---
title: "团队 C 交接：音色单修订核对（含退役/禁用原因）——消费者清点归零"
status: active
version: "0.1.0"
date: 2026-10-25
---

# 团队 C 交接：音色单修订核对（含退役/禁用原因）——消费者清点归零

`getVoiceRevision` 是清点名单上的最后一条。它此前没有任何 Web 消费者，而**退役与禁用原因只存在于单修订上**——目录摘要是 `VoiceSummary`（`voice_id`/`revision`/`display_name`/`source_type`/`status`/`languages`/`audio_formats`），没有 `retired_at`、`disabled_reason`、`model_binding`、`published_at`。也就是说：只靠目录，界面**无法说明一个被固定引用的音色为什么不可用**，只能看到 `status`。

## 契约语义

`GET /v1/workbench/voices/{voice_id}/revisions/{revision}` 返回 `VoiceRevision`：`voice_id`/`revision`/`display_name`/`status`/`languages`/`audio_formats`/`model_binding{id,revision}`/`published_at`/`retired_at`/`disabled_reason`（无 `id`/`object`）。

## 本轮改动

- `workbench-client.ts`：新增 `voiceRevision(voiceId, revision)`，断言返回的 `voice_id` 与 `revision` 都与请求一致——**读了别的修订就等于读错**。
- `RenderUnitPanel.tsx`：新增"音色修订核对"表单（音色 id + 修订号 → 读取），展示 `display_name`/`status`/语言/格式/`model_binding`/发布/退役/禁用原因；目录不可用（503）与修订不存在（404）各有独立诚实文案，且**出错时不显示任何修订记录**。

一次过程修正值得记下：表单的 `aria-label="核对音色修订"` 与内部输入框的可见标签**同名**，导致 `getByLabelText("核对音色修订")` 同时匹配输入框与表单（`getByLabelText` 也匹配 `aria-label`），两个用例因此失败。把表单名改为"音色修订核对"后唯一——**可访问名不是越多越好，重复会直接破坏定位**。

## 证据

| 证据 | 内容 | 结果 |
| --- | --- | --- |
| Web 单元 | 客户端：路径 + 修订不一致拒绝；面板：**未点击前不发起读取**、成功后显示退役/禁用原因与绑定、503 只显示"音色目录不可用"且不显示记录 | workbench **594 通过**（591+3） |
| 真实浏览器 | 同一生产包：填入音色 id 与修订 → 读取 → 因部署无语音底座，页面显示"音色目录不可用"且**不显示任何修订信息**；探针的非 2xx 集合钉住为**三条且路径各自正确**（目录 503、单修订 503、旧修订改名 409），按页面真实到达顺序断言 | 通过 |
| 全量探针 | 本轮为扩展既有浏览器用例 | **398 通过** |
| 门禁 | `pnpm format:check`/`lint`/`test`（workbench 594、admin 118）/`build`、`ruff format --check .`（1134 文件）、`ruff check .`、`mypy`（528 文件） | 全绿 |

制品：`.runtime/team-c/browser/real-runtime-chapter-create/`。

## 清点结论（重要）

收紧到"方法 + 路径 + 排除测试文件"后，**已绑定操作的生产消费者缺口归零**：剩下的 4 条（`downloadBookExport`、`downloadSavedAsset`、`streamExportEvents`、`generateRenderUnit`）是 A 尚未绑定的操作，不在 C 的清点范围内；`getTranscriptRevision`/`getChapter`/`getRenderUnit` 已确认为**有等价消费者**而非缺口。

## 未验证与边界

- 单修订读取的**成功路径只有 jsdom 证据**：任何 C 可达的真实部署都没有语音底座（`GET /voices` 503），因此真实浏览器只能证明**拒绝路径**。真实成功路径需要 A 的音色目录（A06/B05）。
- 面板的核对表单与"新增单元"表单尚不联动（不自动带入 pin）。
- 浏览器证据来自 macOS + 本机 Chromium，不代表 Linux 后端或目标拓扑资格。

## 下一接入动作

1. 等 A 的音色目录就绪后，把单修订读取接成成功路径的真实证据（含退役/禁用两条业务分支）。
2. 核对结果与新增单元表单联动：核对通过后一键带入 pin。
