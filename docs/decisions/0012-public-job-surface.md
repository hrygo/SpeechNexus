---
title: 公开 Job 面与执行装配的所有权
status: accepted
date: 2026-10-27
---

# ADR 0012 公开 Job 面与执行装配的所有权

## 背景

`contracts/speech-openapi.yaml` 已冻结 `/v1/nexus/jobs`、`/v1/nexus/jobs/{job_id}`、
`/v1/nexus/artifacts/{artifact_id}` 等公开操作，工作台的正式消费者也已按该契约调用
（`workbench/infrastructure/public_speech_render_jobs.py` 的提交与读取、
`public_transcript_source.py` 的制品读取）。但后端目前只实现三个资产操作，
这些路由不存在，消费者得到 404。

协调层并非空白：`coordination_jobs`、`admission_attempts`、`admission_leases`、
`submit_intents`、`coordination_outbox`、`execution_stop_evidence` 与对应 PostgreSQL
适配器都已存在。缺的是把公开 Job 面接到协调事实上的服务层与路由层。

`coordination_jobs` 刻意只保存协调身份（job/operation/attempt/event 标识与摘要），
没有业务载荷、没有公开状态、没有结果引用。如果直接把它当作公开读模型，就必须在协调表里
加入业务字段，破坏“协调事实”与“业务读模型”的边界。

## 决定

1. **公开读模型独立于协调事实。** 新增 `speech_jobs` 与 `speech_job_events` 保存公开请求的
   固定摘要、生命周期状态、结果引用与事件序号；`coordination_jobs` 继续只保存协调身份。
   两者以 `job_id` 关联，不互写对方字段，不做跨表复制。
2. **状态只前进，终态不回头。** 公开状态为 `pending → running → succeeded | failed |
   cancelled | expired`。`succeeded` 必须同时具备已发布 artifact 引用与执行回执标识；
   失败、取消、过期不得携带 artifact。读取路由只读持久事实，绝不在读路径触发执行。
3. **提交幂等绑定原身份。** `Idempotency-Key` 与请求摘要共同决定业务键；同键异体返回
   `409`，同键同体返回原 Job 身份。结果未知时按原身份查询，不新建执行、不伪造成功。
4. **执行装配经由持久意图。** 提交在**同一关系事务**内完成 Job、完整配方租约/预算、
   `submit_intent` 与 outbox 写入；网关从持久意图取活；backend 依据 observation 落终态并
   发布 artifact。业务 API 不直连引擎、不持有容器权限、不在请求线程内等推理。
5. **未建成的面如实返回。** 未实现的操作返回 `503`/`501` 与既有错误包封，不用
   “永远 pending”的假 Job 冒充可用；不以占位响应通过契约检查。
6. **门禁不变。** 本决定只确定所有权与装配顺序，不降低任何 issue 的 AC；公开 Job 面可用
   不等于两条链验收通过，真实引擎、停止/释放证据与目标拓扑仍分别取证。

## 后果

- 新增迁移与适配器只写 `speech_jobs` / `speech_job_events`，协调表 DDL 不变。
- 工作台的 `submit`/`read_job` 可以直接对接，不需要改消费者契约。
- 读取路径与执行路径解耦：Job 状态由执行观察推进，不由 HTTP 请求推进，恢复语义与
  outbox 重放保持单一权威。
- 未实现的 realtime、voice-design/clone 等面继续返回明确错误，不因新增路由而被误认为可用。
- 实施顺序：先公开 Job 提交与读取（含幂等与终态约束），再制品下载，然后才是模型/能力发现
  （后者需要权威发布目录），最后是 OpenAI 兼容外壳。