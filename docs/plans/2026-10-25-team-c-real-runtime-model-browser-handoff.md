---
title: "团队 C 交接：真实运行时 × 真实本机模型 × 真实浏览器"
status: active
version: "0.1.0"
date: 2026-10-25
---

# 团队 C 交接：真实运行时 × 真实本机模型 × 真实浏览器

上一单元把浏览器证据接到部署自己的组合根（真实运行时、真实启动检查、真实账本重放），但纪要仍是测试内假模型。本轮把**配置的本机模型**接进同一条真实链路：页面里排队的纪要由运行时自己的派发器认领并完成，页面显示的正文就是本机模型实际返回的内容。

## 基线

- `base_sha` = `head_sha` = `4b08c396c0fe8fc43c7e85683e534befab6bb22b`（未提交、未推送）。
- 上一冻结单元 `C08-real-runtime-browser-qualification`（443 路径，补丁 `1c20ebe9…`）。
- 本单元 `C08-real-runtime-model-browser-qualification`：新增 `tests/integration/workbench_real_runtime.py`（共享前端层夹具）与 `tests/integration/test_workbench_real_runtime_minutes_browser.py`，修改 `scripts/dev-env/workbench-meetings-probe.mjs`、`tests/integration/test_workbench_real_runtime_browser.py` 与本文件。最终哈希见 `.runtime/team-c/team-c-current.manifest.json`。

## 本轮改动

- `workbench_real_runtime.py`：把上一单元的"前端层 + 可重启真实 API"抽成共享夹具（`Tier`、`settings_for`、`bind`、`erased`），故障注入改为按**路径后缀**匹配（删除用例用 `deletion-plans`/`deletions`，纪要用例用 `seal`/`minutes`）。上一单元的用例改为复用，不再自带副本。
- `test_workbench_real_runtime_minutes_browser.py`：真实 Chromium + 真实生产包 + `create_runtime_app`（真实启动检查、**真实纪要派发器**、真实清理 sweeper）+ 隔离 PG + **本机配置模型**。测试在 `execute` 握手处**不执行任何任务**，而是轮询 `GET /v1/workbench/meetings/{id}/minutes/{id}` 直到运行时自己把任务做完，并断言 `status=succeeded`、`llm_model` 等于配置模型、`minutes_text` 非空；随后用真实 `POST /transcripts/{id}/revisions` 触发过期、用真实删除预检+确认触发级联，等 sweeper 真的清掉再让页面刷新。
- `workbench-meetings-probe.mjs`：新增两个**默认关闭**的开关。`--llm-always-on` 跳过"测试内关掉模型"的握手（真实部署无需在流程中停用模型）；`--minutes-any-text` 接受模型实际返回的文本（等待 60s）；`execute` 事件补充 `meetingId`；always-on 模式下把"纪要：已排队"放宽为"已排队|正在生成|已完成"——因为真实派发器可能在页面刷新前就认领了任务。默认路径逐字未变。
- 模型文本以 `model-text.txt` 落盘（合成输入，无私有内容），便于复核。

## 证据

| 证据 | 环境 | 结果 |
| --- | --- | --- |
| 新用例 | 真实 Chromium + 真实生产包 + 真实组合根 + 隔离 PG + 本机模型 | **通过**（8.67s） |
| 既有会议浏览器用例（默认路径回归） | 手工装配 ASGI + 假模型 | **通过**（6.22s）；与新用例同 session 连跑亦通过（14.62s） |
| 上一单元删除资格用例（夹具抽取回归） | 真实组合根 + 隔离 PG | **通过** |
| `--browser` 全量探针 | 隔离 PG + MinIO + 三份浏览器用例 | **382 通过**（71.65s，干净实例） |
| 静态/类型 | `ruff check .`、`mypy`（520 文件） | 全绿 |

关键实证：页面状态先显示"纪要：正在生成 · 来源 v2 · 模板 v1"，由**运行时的派发器**推进；`MinutesJob` 读回 `status=succeeded`、`llm_model=KAT-Coder-V2.5-Dev-OptiQ-4bit`；页面正文非空。落盘文本为 `您好！您似乎只发送了一个表情符号，没有附上需要校对的正文内容。…` —— 合成稿正文几乎只有空白与一个表情，模型的这句反问恰好证明**封存正文确实流到了模型**，而不是空提示词。

## 未验证与边界

- 仍不是语音、真实设备、音色或真实 A/B 闭环的证据；也不证明 Linux 后端与 GPU。
- 前端层注入的两次 503（封存与纪要重放）是**测试装置**，运行时的业务语义未被替换。
- 观察到一次**未定根因的抖动**：三份浏览器用例在同一 session 连跑时，既有会议用例失败一次；随后单跑、两两组合与三连顺序（18.16s）均通过，未再复现。记录为待观察项，不作为通过依据。
- 另有一次 `--browser` 探针出现 `test_workbench_exports_http` 单项失败：原因是本机自己的隔离 PG 实例仍占用探针固定端口 55439，与探针自建实例冲突；停掉该实例后重跑为 **382 通过**。属本机环境占位，非代码问题。
- 本用例需要 `SPEECHNEXUS_TEST_LLM_ENDPOINT`/`MODEL`，且会加载本机模型，因此**不在**常规 `--browser` 选择集内；跳过不计为通过。

## 下一接入动作

1. 等 A 绑定下载/事件流操作与非 JSON 响应体 → 导出下载、已保存副本下载、事件流 UI。
2. 等 A Job API `generateRenderUnit` → 生成派发与代次写入；导出 worker 的 producer 一就绪即可放量。
3. 等 A06/B05 → 音色候选/试听/校验/发布（含助手所需已发布音色）；助手状态机在拿到真实语音 Job 身份前无法达到"已回答"。
4. 生产迁移/部署（A07）与 Linux/GPU（B07）就绪后，在同一套真实拓扑上取得跨内容网络与目标环境资格。
