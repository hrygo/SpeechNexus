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

API 进程需显式配置独立非 owner 角色：

```bash
export SPEECHNEXUS_BINDING_LIFECYCLE_DATABASE_URL='postgresql://<lifecycle-role>:<secret>@<host>:5432/<db>'
export SPEECHNEXUS_DATABASE_SCHEMA=speech
```

角色使用 `binding_lifecycle_grants`，获得：

- `speech_publications`、`speech_publication_retirements` 与
  `speech_publication_lifecycle_events` 的 `SELECT, INSERT`；
- `speech_publication_heads` 的 `SELECT, INSERT, DELETE` 及
  `revision, revision_id, updated_at_us` 列级 `UPDATE`；
- binding revision/evidence 的 `SELECT`；
- `speech_catalog_state` 的 `SELECT` 及 `revision, generated_at_us` 列级 `UPDATE`。

启动会拒绝 owner、schema CREATE、序列权限、其它 speech 表权限和更宽的列级写权限。
未配置 DSN 时两个生命周期端点 fail closed，返回
`503 binding_result_unknown`；不会复用 draft writer 或 owner 身份。

## 退役

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
不匹配返回 `409 configuration_changed`。当前 head 已被退役清空时传 `null`：

```json
{"expected_current_revision": null}
```

成功只更新 head/目录版本并追加审计，不创建新 publication。对外目录和 Job
选择立即回到目标 revision 当时的不可变定义、任务、recipe 与证据；ETag 再次变化。
已退役或 disabled 的 revision 本身不能激活，应回滚到另一个仍然 published 的
revision。draft 也不能激活。

## 在途 Job 与审计

Job 在准入时把完整有效配置固化到 `speech_job_submissions.effective_configuration_json`。
退役或回滚只影响后续选择；在途 Job 不重读 head，不因退役取消、改写或失败。
退役也不会自动停止或释放执行算力。

每次成功变更写入 append-only `speech_publication_lifecycle_events`，至少包含
action、binding/revision、result status、请求 ID、actor 类型/标签、发生时间、
policy revision、intent digest、幂等键摘要与包含 reason 的
`change_summary_json`。核对命令：

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

## 验证与边界

`tests/integration/test_binding_lifecycle_pg.py` 在隔离 PostgreSQL 上使用真实
HTTP ASGI 请求覆盖：退役后 `/v1/models` 为空且 ETag 变化、新 Job 不可选中、
在途 Job 的固定配置不变、回滚到 revision 1 后目录恢复、重复/未知/CAS 冲突、
最小权限启动及审计事件写入。它不证明真实部署网络、真实引擎、容量或生产资格。

当前 voice 仅有 `retireVoiceRevision` 契约声明和 publication retirement 墓碑，
没有生产 revision 写路径、handler 或 activation 声明；因此本次没有伪造 voice
生命周期实现。补齐 voice 的持久发布/读取所有者后，可复用同一 head 和审计语义。
