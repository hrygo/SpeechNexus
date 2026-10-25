---
title: "团队 C 交接：语音助手诚实消费面（按部署能力三态）"
status: active
version: "0.1.0"
date: 2026-10-25
---

# 团队 C 交接：语音助手诚实消费面（按部署能力三态）

工作台此前**没有任何助手界面**（连导航项都没有）：契约与后端早已提供创建/读取/取消三条操作，但 C 侧没有消费者。本轮建立该消费面，并让它严格以服务端上报的能力三态决定能否提交——未启用就保持关闭，不虚构模型、任务或回答。

## 基线

- `base_sha` = `head_sha` = `4b08c396c0fe8fc43c7e85683e534befab6bb22b`（未提交、未推送）。
- 上一冻结单元 `C00-workbench-capability-report`（453 路径，补丁 `e2d769d6…`）。
- 本单元 `C06-assistant-honest-consumer-surface`。

## 本轮改动

- `workbench-client.ts`：新增 `createAssistantTurn`（202，断言 `queued`/无文本/无错误）、`assistantTurn`、`cancelAssistantTurn`（断言同 id 且 `playback_generation` 不倒退）。
- 新增 `assistant/assistant-session.ts`：先读 `/status`，按 `capabilities.assistant` 与 `workbench_enabled` 得到 `available|disabled|unavailable|unknown`；仅 `available` 时才读取已发布音色目录；提交后按 `queued|running` 轮询到终态，取消用请求键（重复取消不再发起），终态不再变化，迟到的响应不覆盖较新状态；严格断言契约：`failed` 必须有 `error`，非 `failed` 必须 `error=null`，`succeeded` 必须有 `speech_job_id`，且**只要携带 `speech_job_id` 就必须带非空 `output_text`**。
- 新增 `assistant/copy.ts`：能力值到设计三态的**呈现映射**（available→ready、unavailable→contract、disabled/unknown→planned）与诚实文案；映射只降级、不升级。
- 新增 `assistant/AssistantHost.tsx` + `assistant.css`：按三态渲染（未启用/缺依赖时表单根本不出现），可用时才显示项目、已发布音色、文本与请求键；无已发布音色时明确说明；回合区显示状态、文本、服务端错误码与是否可重试，不自动重试。
- `App.tsx`：新增“语音助手”页签与懒加载宿主，并将“按部署能力三态呈现；缺少语音底座或内网模型时保持关闭”写入状态说明。

## 证据（合成消费者，除能力来源外不触及真实服务）

| 证据 | 断言 | 结果 |
| --- | --- | --- |
| 会话 7 项 | 未启用时**不提交也不读目录**；缺依赖与主动关闭区分；工作台整体关闭时降级为不可用；提交一次→轮询到成功并显示文本；失败显示服务端错误且**不再重试**；重复取消失效；409 冲突映射为可见问题且不产生回合 | 通过 |
| 宿主 4 项 | 三态映射不升级；未启用/缺依赖时**表单不存在**且不发起创建；可用但目录为空时给出说明并禁用提交 | 通过 |
| 全量 Web | Workbench vitest **574 通过**（原 563 + 11）、`pnpm format:check`、`pnpm lint`（含设计 Token、文案边界与能力三态门禁）、`pnpm typecheck`、`pnpm build` 全绿 | 通过 |

能力数据本身已在上一单元用真实组合根 + 隔离 PG 钉住；本单元消费该数据。

## 未验证与边界

- **尚无真实浏览器证据**：本轮只有 jsdom 单元证据，真实 Chromium 下未启用/缺依赖两种诚实态的呈现仍待补（下一轮）。
- 创建—轮询—取消链路只有合成证据：真实运行需要 A 的公开 Job API 与已发布音色，当前部署的 `assistant` 能力不会是 `available`。
- 不代替 A/B 宣称语音质量或资格；界面不显示任何未经服务端确认的文本。

## 下一接入动作

1. 真实运行时浏览器资格：默认部署（`disabled`）与“配置内网 LLM 但无语音底座”（`unavailable`）两态在真实 Chromium 上的呈现。
2. A 就绪后：注入真实语音客户端与已发布音色，跑通真实创建—轮询—取消。
