---
title: "团队 C 交接：工作台状态上报真实的部署能力三态"
status: active
version: "0.1.0"
date: 2026-10-25
---

# 团队 C 交接：工作台状态上报真实的部署能力三态

契约要求 `GET /v1/workbench/status` 的 `capabilities` 数组上报 `transcription`、`meeting_minutes`、`assistant`、`teleprompter`、`audiobook_export` 五个能力，取值 `available` / `disabled` / `unavailable`。实现**恒返回空数组**，所以任何消费端都无从得知部署到底能做什么，只能靠猜——这正是"未实现的能力不得用虚构任务、模型或容量填充界面"最容易出问题的地方。

## 基线

- `base_sha` = `head_sha` = `4b08c396c0fe8fc43c7e85683e534befab6bb22b`（未提交、未推送）。
- 上一冻结单元 `C07-export-production-pre-wiring`（451 路径，补丁 `00f99f64…`）。
- 本单元 `C00-workbench-capability-report`。

## 本轮改动

- `http_management.py`：`create_management_router(..., capabilities=None)`，状态响应按契约顺序输出 `[{"name", "status"}]`。
- `app.py`：`create_app(..., capabilities=None)` 透传。
- `runtime.py`：由**装配事实**推导五个状态，不读取客户端输入、不探测外部服务：
  - `transcription`：无语音客户端 → `disabled`；有客户端但无对象区 → `unavailable`；两者都有 → `available`。
  - `meeting_minutes`：有批准的 LLM profile → `available`，否则 `disabled`。
  - `assistant`：无 LLM → `disabled`；有 LLM 无语音客户端 → `unavailable`；两者都有 → `available`。
  - `teleprompter`：本地阅读可用 → `available`（语音跟随不属于该能力，不并入状态）。
  - `audiobook_export`：未开启导出循环 → `disabled`；开启但缺 producer 或对象区 → `unavailable`；两者齐备 → `available`。
- 新增 `tests/integration/test_workbench_runtime_capabilities.py`（3 项），并纳入 `--browser` 探针门禁清单。

## 证据（真实组合根 + 隔离 PostgreSQL）

| 用例 | 断言 | 结果 |
| --- | --- | --- |
| 未配置部署不得虚构能力 | `workbench_enabled=true`、`llm_enabled=false`、无 `minutes_profile`，五态恰为 `disabled/disabled/disabled/available/disabled` | 通过 |
| 配置了内网 LLM 与语音底座 | `llm_enabled=true`、`minutes_profile.llm_model` 等于配置值，五态为 `available/available/available/available/disabled` | 通过 |
| 导出能力只在既有 producer 又真正运行循环时可用 | 默认 → `disabled`；开启循环但无 producer/对象区 → `unavailable`；producer + 对象区 + 循环 → `available` | 通过 |

其余门禁：`ruff format/check` 干净（1983 文件）、`mypy` **523** 文件无问题、`lint-imports` 7 kept/0 broken、`tests/unit` + `tests/contract` **3997 通过**、`tests/integration/test_workbench_runtime.py` + 本单元 **27 通过**；`--browser` 全量探针见本轮记录。

## 未验证与边界

- 能力状态只描述**本部署是否会承接**该能力，不证明真实语音、真实模型质量、真实设备或目标拓扑。
- `transcription` / `assistant` 的 `available` 仅表示配置齐备（语音客户端 + 对象区 + 批准 LLM），真实语音调用仍属 A/B 边界：`assistant` 的创建路径依赖 A 的公开 Job API，`transcription` 依赖真实引擎。
- 契约本身未改动：`capabilities` 早已在 `WorkbenchStatus` 中要求，本次只是让实现与其一致。

## 下一接入动作

1. 助手消费面直接读取该状态渲染诚实态（未配置就禁用创建，并给出服务端判定的原因），再实现取消/失败路径。
2. 音色候选/试听/校验/发布、有声书 TXT/章节/角色与 Admin 观测同样以该状态为准，不自行推断就绪。
3. A 就绪后把真实语音/生成接入，届时 `available` 才是端到端证据。
