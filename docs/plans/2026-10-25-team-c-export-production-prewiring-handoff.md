---
title: "团队 C 交接：导出产线在服务运行时中的预接线"
status: active
version: "0.1.0"
date: 2026-10-25
---

# 团队 C 交接：导出产线在服务运行时中的预接线

导出队列、认领、对象发布与结算都在工作台侧，**只有 producer 属于语音生成边界**（A）。此前 `ExportWorker` 只在测试里被手工调用，生产组合根从不构造它——也就是说队列里的导出任务永远不会被任何人认领。本单元把它接进服务运行时，并保持**默认关闭**。

## 基线

- `base_sha` = `head_sha` = `4b08c396c0fe8fc43c7e85683e534befab6bb22b`（未提交、未推送）。
- 上一冻结单元 `C02-transcript-create-consumer-fix-and-authoring-browser-qualification`（449 路径，补丁 `8a7f3164…`）。
- 本单元 `C07-export-production-pre-wiring`。

## 本轮改动

- `WorkbenchSettings` 新增 `export_interval_seconds`（默认 `0`，范围 `0..300`；`0` 表示关闭）。
- `create_runtime_app(settings, *, export_producer=None)` 新增可选注入点：**producer 由语音生成边界提供**，工作台只消费。
- 运行时构造唯一的 `PostgresExports` 适配器，同时供 HTTP 面与 `ExportWorker` 使用；对象区沿用同一 `MinioObjectZone`。
- 新增 `_dispatch_exports(worker, interval)`：有界批次（4）、失败记录后重试、取消即结束，与既有纪要派发器同形；lifespan 仅在 `export_interval_seconds > 0` **且** `worker.enabled`（既有 producer 又有对象区）时启动，退出时取消。
- 新增 `tests/integration/test_workbench_runtime_export_dispatch.py`（3 项）。

## 证据（隔离 PostgreSQL + 真实 MinIO + 真实组合根）

| 用例 | 断言 | 结果 |
| --- | --- | --- |
| 服务运行时产出排队导出 | 全程无任何测试调用 worker：HTTP 建项目/书/章/渲染单元/导出（202 queued）→ 运行时自有循环认领并完成 → `succeeded`、`progress 1/1`、`artifact.media_type=zip`、`bytes`/`sha256` 与合成载荷一致、`error=None`，并按 `exports/<book>/<export>/<artifact>` 读回对象字节 | 通过 |
| 未配置的运行时不动队列（默认关闭） | `export_interval_seconds=0` 时等待 1.5s，导出仍为 `queued`、`artifact=None`、`error=None` | 通过 |
| 无 producer 的运行时不得认领 | `interval=0.2` 但 `export_producer=None` 时同样保持 `queued`（宁可不做，也不留下无法完成的认领） | 通过 |

其余门禁：`ruff format/check` 干净、`mypy` **522** 文件无问题、`lint-imports` 7 kept/0 broken、`tests/unit` **2282 通过**、`tests/integration/test_workbench_runtime.py` **24 通过**；`--browser` 全量探针见本轮记录。

## 未验证与边界

- producer 是测试替身，**不代表任何真实语音或音频制品**：本轮证明的是"队列—认领—发布—结算—运行时生命周期"这条工作台自有链路，真实字节来源仍待 A 的生成边界（`generateRenderUnit` 未绑定）。
- 未验证：导出产物经公开下载操作取回（`downloadBookExport` 未绑定）、真实 ffmpeg/编码产物、Linux/GPU、真实设备。
- 默认关闭意味着**未配置的部署不会宣称具备导出能力**；这是有意的诚实默认，不是缺陷。

## 下一接入动作

1. 纯 C 收尾：保存副本 `getSavedAsset` 回读（创建路径已确认需 A 的真实制品 `SpeechArtifactSource.copy`，只读回读不足以构成生产证据）。
2. 独立切片：音色候选/试听/校验/发布 UI 状态、助手 UI"语音底座未配置"诚实态、有声书 TXT/章节/角色领域、Admin 诊断/观测与 capability 三态。
3. A 就绪后：注入真实 producer 并联验公开下载、账本水位与目标环境资格。
