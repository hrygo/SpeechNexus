---
title: "团队 C 交接：已保存副本回读的生产消费者"
status: active
version: "0.1.0"
date: 2026-10-25
---

# 团队 C 交接：已保存副本回读的生产消费者

第 64 轮用机械比对找到"契约已绑定但 Web 无消费者"的缺口后，本轮把同一方法跑全。

> **更正（第 68 轮）**：本轮写下的"44 条里只剩 3 条没有消费者"是**错的**——该方法只比对契约**路径**、不区分 HTTP 方法，同路径不同动词会被误判为已消费。按"方法 + 路径"重做后真实结果是 **7 条**，其中 `getSavedAsset` 正是 ROI-1 明列的"保存副本回读"，本轮补它。完整的更正表与后续处理见 [章节创建补线交接](2026-10-25-team-c-chapter-create-consumer-handoff.md)。

## 基线

- `base_sha` = `head_sha` = `4b08c396c0fe8fc43c7e85683e534befab6bb22b`（未提交、未推送）。
- 上一冻结单元 `C-issue-four-part-refresh`（476 路径，补丁 `085150dc…`）。
- 本单元 `C07-saved-asset-read-back-consumer`。

## 缺口是怎么定位的

对 36 条路径 / 44 条操作逐条检查 `web/workbench/src` 是否引用契约里的**字面路径**，再扣掉 4 条 A 未绑定的操作（`downloadBookExport`、`downloadSavedAsset`、`streamExportEvents`、`generateRenderUnit`）。结果只剩 3 条：`getSavedAsset`、`getTranscriptRevision`、`getVoiceRevision`。这比人工巡检可靠，也解释了上一轮为什么会漏掉提词状态。

## 契约语义（实现前先读）

`SavedAsset` 要求 `id / object / project_id / media_type / bytes / sha256 / source_artifact_id / source_receipt_id / created_at`，`object` 固定 `nexus.saved_asset`，`bytes ≥ 1`，`sha256` 是 `sha256:` + 64 位十六进制。读取是**按 id 的本地读**：不重新向底座取产物，也不由客户端指定路径或桶。

## 本轮改动

- `workbench-client.ts`：新增 `savedAsset(assetId)`，路径参数来自调用方、返回后断言 `id === assetId`。
- `RenderUnitPanel.tsx`：在"已保存副本 <id>"旁新增显式"读取副本"按钮；点击后展示服务端自己的记录（媒体类型、字节数、sha256、来源 artifact）；404 如实报"该已保存副本已不可读取"，连接失败报"结果未确认"，**不自动重试、不用前端推算填充**。
- 单元证据：面板 2 项（未点击前不发请求；成功显示记录；404 显示诚实错误且不渲染信息块）。

## 证据

| 证据 | 内容 | 结果 |
| --- | --- | --- |
| Web 单元 | workbench **582 通过**（580+2） | 通过 |
| 真实 PG + MinIO 集成 | 复用既有真实回环产物源夹具：`save` 后按 id 回读等于提交记录；回读**不再次向底座取产物**；未知 id 与另一 workspace 的 id 都是 `ResourceNotFoundError`，不返回空记录 | 4 项通过（新 1 项） |
| 门禁 | `ruff format --check .`（1128 文件）、`ruff check .`、`mypy`（526 文件）、`pnpm format:check`/`lint`/`typecheck`/`test`（workbench 582、admin 118）/`build` | 全绿 |
| 全量 `--browser` 探针 | 重新在生产包上跑真实 Chromium 用例（确认新构建不破坏已冻结断言，并纳入新的读路径用例） | **395 通过**（394+1） |

## 一处自我修正

面板测试首版只给 `saved_asset_id` 而不带 `source_artifact_id`/`source_receipt_id`，被 `RenderUnit` 契约判非法（`ContractValidationError`）——说明该字段组是条件联动的。修的是测试夹具，不是放宽校验。另外 `readCopy` 最初被我放在模块顶层（分别引用组件作用域的 `client`/`setCopy`），`tsc` 之前就被自查改到组件内。

## 未验证与边界

- 回读的 UI 只有 jsdom 证据：**尚未**在真实浏览器里点击"读取副本"（下轮可接）。真实 PG/MinIO/HTTP 侧已覆盖读路径本身。
- `getTranscriptRevision`、`getVoiceRevision` 仍无消费者（已识别，未实现）。
- 保存副本的**创建**依赖 A 的 `SpeechArtifactSource`；集成用例中的回环产物源是测试替身，与真实 A 制品不等价。
- `bytes`/`sha256` 是工作台自己复制时记下的值，本次回读只证明记录一致，不重新校验对象字节。

## 下一接入动作

1. 把"读取副本"接进真实浏览器探针（生产包 + 真实运行时 + 隔离 PG，副本行由真实 save 路径产生或按既有先例直写）。
2. `getTranscriptRevision` / `getVoiceRevision` 的消费者：修订历史当前只列不读，补"按 revision 读取"用于归档核对。
3. 等 A 绑定 4 条非 JSON 操作后接下载/事件流。

## 第 67 轮补充：读路径的真实 HTTP 证据与一处预期修正

第 66 轮的回读只有"服务层 + 真实 PG"证据，**没有走到 HTTP 层**（而浏览器客户端恰恰只跟 HTTP 说话）。本轮补上。

- 新增 `tests/integration/test_workbench_runtime_saved_assets_http.py`，用真实组合根 `create_runtime_app` + 真实 PG + 隔离 MinIO + **回环底座替身**：
  - `POST /v1/workbench/projects/{id}/saved-assets` → **201**，返回 `object=nexus.saved_asset`、`sha256`/`bytes` 与底座字节一致；
  - `GET /v1/workbench/saved-assets/{id}` → **200**，与创建响应**逐字段相等**；且**底座读取次数在回读后不变**（HTTP 层同样证明不重新取产物）；
  - 未知 id → **404** `resource_not_found`。
- **预期修正**：我原以为未配置对象区时会返回 503 `unavailable`，实测该部署**根本不挂载**副本路由，返回 404 `resource_not_found`。改的是断言与用例名（"该部署不提供该读取路径"），不是实现——客户端的 404 文案"该已保存副本已不可读取"正好是这个语义的诚实读法。
- 探针：把该文件接进 `--workbench-runtime` 门禁，全量 `--browser` 探针 **397 通过**（395+2），`ruff format`（1130 文件）/`ruff check`/`mypy`（527 文件）全绿。

仍未验证：真实浏览器点击"读取副本"（需要真实 save 路径，依赖 A 的产物源）；`bytes`/`sha256` 只证明工作台记录一致，未重新校验对象字节。
