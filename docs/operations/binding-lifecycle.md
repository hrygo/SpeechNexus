---
title: binding 退役、激活与回滚
status: active
version: "1.0.0"
date: 2026-10-28
---

# binding 退役、激活与回滚

`0023_publication_lifecycle` 让新执行通过 append-only publication 之上的
`speech_publication_heads` 选择版本。退役只移除选择和可见性，不删除历史；
激活既有 published revision 就是显式回滚，不复制、不改写 revision。

## 配置与权限

API 进程需显式配置独立非 owner 角色。通过部署批准的秘密配置向进程提供
`SPEECHNEXUS_BINDING_LIFECYCLE_DATABASE_URL`；不要把含凭据的 DSN 写入命令参数或终端历史。
schema 可按部署配置，例如：

```bash
export SPEECHNEXUS_DATABASE_SCHEMA=speech
```

角色使用 `binding_lifecycle_grants`，获得：

- `speech_publications`、`speech_publication_retirements`、
  `speech_publication_lifecycle_events` 和 `speech_binding_disable_intents` 的
  `SELECT, INSERT`；
- `speech_publication_heads` 的 `SELECT, INSERT, DELETE` 及
  `revision, revision_id, updated_at_us` 列级 `UPDATE`；
- binding revision/evidence 的 `SELECT`；
- `speech_catalog_state` 的 `SELECT` 及 `revision, generated_at_us` 列级 `UPDATE`。

启动会拒绝 owner、schema CREATE、序列权限、其它 speech 表权限和更宽的列级写权限。
未配置 DSN 时两个生命周期端点 fail closed，返回
`503 binding_result_unknown`；不会复用 draft writer 或 owner 身份。

## 退役

以下地址、binding、revision、原因和 key 均为示例。实际操作前须核对目标与当前状态；
每个新意图使用新的高熵唯一 `Idempotency-Key`，并保存完整请求以便结果未知时按原意图
协调。不要跨操作直接复用示例中的固定 key。

```bash
curl -sS -X POST \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: retire-asr-quality-2-20261028' \
  -d '{"mode":"retire","reason":"superseded by revision 3"}' \
  http://127.0.0.1:8000/v1/nexus/admin/model-bindings/asr-quality/revisions/2/retirements
```

`mode: retire` 是正常退役；`mode: disable` 使用同一选择边界，但必须给出
`reason`，审计状态为 `disabled`。成功响应是更新后的
`ModelBindingRevision`，其中 `publication.status` 为 `retired` 或 `disabled`。
`disable` 还在同一事务登记持久停用意图，响应头 `X-Binding-Disable-Cleanup` 为
`pending_or_unknown`；这不表示在途执行已经停止或容量已经释放。后续处理依赖
[停用 worker](binding-disable-worker.md)及其停止与释放证据。

退役要求 revision 是当前 head。成功后 head 被删除、墓碑写入、目录版本在同一
事务推进；下一真实 `GET /v1/models` 不再包含该 binding，ETag 变化。重复退役的
行为是显式的：

- 相同完整意图与相同 `Idempotency-Key`：返回原结果，版本不再次推进；
- 已退役 revision 换新 key 再退役：`409 revision_already_retired`；
- 未知 revision：`404 binding_not_found`；
- revision 已发布但不是当前 head：`409 revision_not_current`。

publication、revision、证据和历史 Job 均保留，不执行删除。

## 激活与回滚

把 head 切回任一仍然 published、未退役的既有 revision：

```bash
curl -sS -X POST \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: activate-asr-quality-1-20261028' \
  -d '{"expected_current_revision":"2"}' \
  http://127.0.0.1:8000/v1/nexus/admin/model-bindings/asr-quality/revisions/1/activations
```

`expected_current_revision` 是操作者看到的当前 head，用于 compare-and-swap；
不匹配返回 `409 configuration_changed`，`retryable: false`。错误 `message`
说明本次事务持锁检查时需要的 `expected_current_revision`：例如当前 head 为
revision 2 时显示 `expected_current_revision="2"`，已清空时显示
`expected_current_revision=null`。此值是拒绝时的观察，收到响应时可能已过时；
重新核实 binding 状态并确认激活目标后再提交，不自动套用提示值重试，也不解析
文案驱动客户端逻辑。当前 head 已被退役清空时传 `null`：

```json
{"expected_current_revision": null}
```

成功激活不创建新 publication。目标不同于当前 head 时更新 head/目录版本并追加审计；
对外目录和后续 Job 选择随即切换至目标 revision 当时的不可变定义、任务、recipe 与证据，
ETag 随之变化。目标已为当前 head 时，新的成功 no-op 只追加审计，不改变目录版本或 ETag。
已退役或 disabled 的 revision 本身不能激活，应回滚到另一个仍然 published 的
revision。draft 也不能激活。

## 在途 Job 与审计

Job 在准入时把完整有效配置固化到 `speech_job_submissions.effective_configuration_json`。
普通 `retire` 或回滚只影响后续选择；在途 Job 不重读 head，不因普通退役取消、改写或失败。
普通退役不会自动停止或释放执行算力。`disable` 另有停用屏障和持久清理流程，不应按普通
退役推断在途执行会继续；停止与容量释放仍须分别取得证据。

激活、retire 和 disable 共用部署范围的 binding 生命周期 key 命名空间，跨 binding、
revision、调用方与 credential label；只绑定已提交结果。先前任一操作成功后，换操作、
binding、revision 或 canonical body 均返回 `idempotency_conflict`。
明确拒绝不占 key，也不记拒绝意图或错误回执。
未提交时后续请求重新判断当前状态；已提交后，同键同完整意图在 CAS 前重放原结果，
异意图返回 `idempotency_conflict`。这是服务端边界，不授权客户端自动修改被拒绝请求。
新激活目标已是当前 head 时为成功 no-op：写事件、绑定 key，head/catalogue 不变。
两个 no-op 可均成功；no-op 先于变更时可均成功，变更先于期待旧 head 的命令时后者拒绝。
同一初始 head 上两个都要变更的新命令才受单一赢家约束；原结果重放不再改变 head。

每个新的成功激活（包括 no-op）和退役写入 append-only
`speech_publication_lifecycle_events`，至少包含
action、binding/revision、result status、请求 ID、actor 类型/标签、发生时间、
policy revision、intent digest、幂等键摘要与包含 reason 的
`change_summary_json`。

当前路由将审计主体记录为 `anonymous`、credential label 记录为 `null`；客户端提供的
`Authorization` 头不会被视为已认证身份。部署入口的访问限制须单独核实，不能据此审计字段
推断操作者身份。

核对命令：

```sql
SELECT action, binding_id, revision, result_status, occurred_at_us,
       request_id, actor_type, credential_label, change_summary_json
  FROM speech_publication_lifecycle_events
 ORDER BY occurred_at_us DESC, event_id DESC
 LIMIT 20;
```

`retired_at_us` 与 `disabled_reason` 也单独保存，便于按状态查询。审计表不接受
UPDATE/DELETE。

## 结果未知

超时、连接中断或提交结果未知返回
`503 binding_result_unknown`，`retryable: false`。客户端不得换 key 或伪造新意图；
保留原路径、完整 body 和原 `Idempotency-Key` 显式重放同一 POST，以收敛到原结果。
完整 body 包括原 `expected_current_revision`；不可套用提示值重基 CAS。后一次拒绝
或当前 head GET 都不能证明先前未知提交的结果。未保留原 key 时不承诺安全自动恢复。
dev5 的兼容影响与同版测试矩阵见[激活验证说明](binding-activation-verification.md)。

## 验证与边界

`tests/integration/test_binding_lifecycle_pg.py` 在隔离 PostgreSQL 上使用真实
HTTP ASGI 请求覆盖：退役后 `/v1/models` 为空且 ETag 变化、新 Job 不可选中、
在途 Job 的固定配置不变、回滚到 revision 1 后目录恢复、重复/未知/CAS 冲突、
最小权限启动及审计事件写入。它不证明真实部署网络、真实引擎、容量或生产资格。

voice 有独立的[内建声音资格与发布路径](builtin-voice-qualification.md)，可在满足资格条件后持久发布固定 revision；不能将它等同于 binding 的管理生命周期。`retireVoiceRevision` 仍是语音契约声明，voice 的退役 / 激活能力与其他声音来源必须分别核对实现和验收，不能从 binding 路由或内建声音发布推断已完成。
