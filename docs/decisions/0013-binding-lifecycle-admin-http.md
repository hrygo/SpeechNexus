---
title: binding 生命周期管理 HTTP 与激活冲突提示
status: proposed
date: 2026-10-29
---

# ADR 0013 binding 生命周期管理 HTTP 与激活冲突提示

## 背景

[GitHub #273](https://github.com/hrygo/SpeechNexus/issues/273) 要求保留发布历史、
允许回滚 head、拒绝陈旧的 `expected_current_revision`，并要求新增 binding 激活
HTTP 路由另走契约变更记录。[PR #296](https://github.com/hrygo/SpeechNexus/pull/296)
已在 `ab740baa703df754d9f40c145a3d4e3667ffc81c` 合入生命周期路由、
admin `0.1.0.dev4` 契约、生成消费者及增量迁移 `0023_publication_lifecycle`。
但该基线的冲突提示只说配置变化，且现有 ADR 0001–0012 未记录这组管理路由的选择。

本文补录已实现路由及其取舍，并提出冲突文案修补供非作者审查；`proposed`
不追认历史流程已完成，也不代表 #273 AC6 或投产 Gate 已获批准。

## 决定（拟议）

1. 沿用 canonical [admin OpenAPI](../../contracts/admin-openapi.yaml) 的
   `retireModelBindingRevision` 与 `activatePublishedModelBindingRevision`：
   `POST /v1/nexus/admin/model-bindings/{binding_id}/revisions/{revision}/retirements`
   和同一路径下的 `/activations`。生产者为 backend 的 binding lifecycle router /
   `PostgresBindingLifecycle`；消费者为管理操作调用方及生成的 Python `binding_http`、
   TypeScript `@speechnexus/contracts/binding-http`。不据此宣称已有管理 UI 接入。
2. 激活显式传入 `expected_current_revision`，在既有 binding 事务锁下比较 head；
   `null` 表示空 head。目标必须已经 published 且未退役；回滚选择旧的不可变版本，
   不新增 publication，不改历史 Job 的有效配置。同一初始 head 上两个都要改变 head
   的新命令，只有一个可成功（无第三方写入且目标均可激活）；no-op 与原结果重放另计。
3. CAS 失败仍为 `409 configuration_changed`、`retryable: false`、
   `X-Should-Retry: false`、`Cache-Control: no-store`。仅补足既有 `error.message`：
   说明拒绝时持锁观察到的公开 revision（十进制字符串，或 JSON `null`），明确这是
   `expected_current_revision` 需要的值，并要求重新核实状态、确认激活目标后再提交。
   值可能在响应到达前再次变化；它不承诺下次请求成功，不触发自动重试。文案不是机器
   恢复字段；客户端继续依据稳定错误码分支，不从文案提取 CAS 值。
4. 按 2026-10-30 用户在 DEL-327 的决定，key 只绑定已提交生命周期结果（激活包括成功 no-op）；
   拒绝不占 key，也不保存拒绝意图或错误结果。尚无已提交结果时按当前状态重新判断。
   激活、retire 与 disable 共用部署范围的 binding 生命周期 key 命名空间，跨 binding、
   revision、调用方与 credential label；实现中 key 摘要在同一数据库/schema 的事件表唯一，
   不按 action 或身份分区。先前任一操作成功后，换操作、binding、revision 或 canonical body
   都返回 `idempotency_conflict`。已提交后，同键同意图重放在当前状态/CAS 前返回原结果；同键异意图返回
   `409 idempotency_conflict`。no-op 仍记录事件，但 head/catalogue 不变，不消费额外 epoch。
   `503 binding_result_unknown` 保持未知、不可自动重试；只允许持原 key 和完整意图
   （含原 path、body 和 expected）的调用方显式重放；不能换 key、改 body 或重基 CAS
   消除未知。后一次拒绝或当前 head GET 不能结算前一次未知提交。
5. 保留共享公开错误包封，不增加 `current_revision` 等响应字段、不修改 DTO 类型或
   请求约束；只传递并验证公开 revision，禁止输出内部 revision ID、端点、凭据、
   SQL/异常文本和执行状态。admin 草案从 `0.1.0.dev4` 推进至 `0.1.0.dev5`；
   从源重新生成镜像与 inventory，不手改生成文件。依赖固定错误全文的调用方需改为
   使用错误码；无法据本仓库测试保证所有外部调用方都未依赖旧文案。

## 替代方案

- 仅保留 CLI：减少 HTTP 写面，但不能为已合入的正式管理协议提供同一激活与回滚入口。
- 增加结构化恢复字段：可供机器读取，但本次只需补足人类可读提示；新字段会扩大共享错误
  契约及消费者升级范围，还需另外决定其时效、恢复授权和查询方式。
- HTTP 层在冲突后另查 head：实现表面简单，但可能把拒绝时状态替换成另一次观察，无法
  说明导致本次拒绝的值。直接传递既有持锁检查的值更准确，不新增数据库查询。

## 后果

### 语义澄清、历史与版本

DEL-318 原候选是 dev4 文案修补。DEL-323 指出无条件“同键异意图冲突”和“两个
activate 只有一个成功”的歧义；DEL-325 在基线与候选中合成复现相同行为。
本次把用户选择写入 canonical，不改既有事务。旧计划或 ADR 中未加限定的表述
不能继续解释成“拒绝也占 key”或“no-op 消耗一次激活机会”。这些更强保证需要新的
命令账本/epoch、迁移和消费者版本，不能由当前 revision CAS 提供。

dev5 的请求/响应结构、稳定码与 DTO 类型沿用 dev4；这是既有运行行为的显式化，
但对依赖旧歧义的消费者属于语义兼容风险，不能仅称为无影响文案修改。生成的
inventory、OpenAPI 镜像与 TS 注释一起升级；外部消费者须核对 key 与未知恢复策略，
不能只凭结构兼容检查认定升级完成。不增加 v2 路由或更强的持久化承诺。

DEL-330 指出共享生命周期 key 的说明缺口。DEL-332 补充既有全局作用域与跨操作冲突，
不改变运行语义、wire 结构或生成 DTO。dev5 仍是未合入、未发布的同一拟议草案版本，
本轮保留 dev5 并以固定补丁摘要区分候选；重新生成相关消费者注释和镜像后复审。

原 #273 AC 正文与历史证据保留；用户对 AC5 的解释作为附加说明，不回写或重勾
远端 AC。扩展场景见[验证说明](../operations/binding-activation-verification.md)。
DEL-326 的 P3 意见保留：DEL-325 并发表的“200”是 adapter 成功标记，HTTP 未运行；
本次不把该表当 HTTP 或真实 PG 证据。SM 组织非作者复审后再决定 ADR 整体状态。

- 本修补不修改锁序、CAS、事务读写集合、审计/幂等表、预算/停止语义、DDL 或权限。
  `0023` 保持既有增量历史；本次无新迁移，也不执行生产迁移。
- [共享合成样例](../../tests/fixtures/contracts/binding-http.json) 由 Python / TypeScript
  校验；单元测试覆盖普通、大 revision 与空 head 的公开提示、禁止写入及信息边界。
  真实 PG 的 HTTP / 并发测试补充同版断言，是否实际运行以候选交付报告为准。
- 结构兼容检查只证明其比较范围；ASGI 与合成 SQL 不能替代真实 PG、真实网络、引擎停止
  或多机资格。#273 AC3 的取消派发崩溃窗口与真实停止验收继续保留，本文不结算整个 issue。
- 下一接收人为非作者 reviewer；审查覆盖本候选补丁及本拟议记录，通过后再由授权方决定
  ADR 状态、AC4/AC6 和集成安排。操作细节见
  [生命周期指南](../operations/binding-lifecycle.md) 与
  [实现说明](../architecture/model-binding-drafts.md#selected-head-and-lifecycle-0023)。
