---
title: "团队 C 交接：助手诚实态的真实浏览器资格"
status: active
version: "0.1.0"
date: 2026-10-25
---

# 团队 C 交接：助手诚实态的真实浏览器资格

上一单元交付了助手消费面，但只有 jsdom 证据——"未启用就不要出现创建入口"这句话在真实浏览器里从未被确认过。本单元在生产包 + 真实 C 组合根 + 隔离 PG 上，用真实 Chromium 把两种诚实态钉住。

## 基线

- `base_sha` = `head_sha` = `4b08c396c0fe8fc43c7e85683e534befab6bb22b`（未提交、未推送）。
- 上一冻结单元 `C06-assistant-honest-consumer-surface`（460 路径，补丁 `e0e6a267…`）。
- 本单元 `C06-assistant-honest-state-browser-qualification`。

## 本轮改动

- 新增 `scripts/dev-env/workbench-assistant-probe.mjs`：驱动生产包，点击“语音助手”，断言服务端能力值原样呈现、对应的诚实文案可见、**创建表单与提交按钮都不存在**、页面重载后结论不变；同时记录任何 `/assistant-turns` POST（必须为 0）、跨源出站（0）、未处理页面错误（0）与 4xx/5xx 响应（空），并截图。
- 新增 `tests/integration/test_workbench_real_runtime_assistant_browser.py`：两种部署各起一个真实组合根（真实 lifespan、隔离 PG、web tier 前置），先由 HTTP 读取 `capabilities.assistant` 与 `llm_enabled` 作为期望，再让浏览器确认同一结论。
- `scripts/dev-env/workbench-pg-probe.py`：把该文件加入 `--browser` 门禁清单。

## 证据（真实 Chromium 156.0.8078.4 + 生产包 + 真实组合根 + 隔离 PG）

| 场景 | 部署配置 | 服务端判定 | 浏览器确认 |
| --- | --- | --- | --- |
| 默认部署 | 未配置文本模型、无语音底座 | `capabilities.assistant = disabled`、`llm_enabled=false` | 文案“本部署未启用语音助手”可见；**无表单、无提交按钮**；`/assistant-turns` 请求 0；重载后仍为 `disabled` |
| 已配置内网模型 | loopback 端点（`http://127.0.0.1:59999/v1/chat/completions`）+ 批准引用，仍无语音底座 | `capabilities.assistant = unavailable`、`llm_enabled=true` | 文案“本部署缺少语音助手依赖”可见；**无表单**；请求 0；重载后仍为 `unavailable` |

两场景均为 `outbound=0`、`unexpectedErrors=0`、`failed=[]`；制品：`.runtime/team-c/browser/real-runtime-assistant-{disabled,unavailable}/`（`result.json`、`events.json`、`stderr.log`、截图）。

## 未验证与边界

- 仍**没有**“可用”路径的真实证据：真实创建—轮询—取消需要 A 的公开 Job API 与已发布音色，当前部署不可能判定 `available`；该链路目前只有合成消费者证据（上一单元）。
- 本单元不产生语音、不调用模型：loopback 端点无需存活，能力判定只看服务端配置。
- 浏览器版本与 macOS 宿主是本机环境，不代表目标拓扑或 Linux 后端。

## 下一接入动作

1. A 提供 Job API 派发与已发布音色后，在 `available` 部署上跑真实创建—轮询—取消，并把同一探针扩展到该态。
2. 继续 ROI-2 未完成切片：音色候选/试听/校验/发布 UI 状态、Admin 诊断/观测页（同样以能力三态为准）。
