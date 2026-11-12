---
title: binding 发布、退役、激活与回滚
status: active
version: "1.1.0"
date: 2026-11-13
---

# binding 发布、退役、激活与回滚

发布、退役、激活三个写命令共用一个生命周期写入角色和一个 append-only 事件账本；
每个成功命令在推进 head 的同一事务里追加一条 `model_binding.*` 事件，原始
`Idempotency-Key` 的重放由该账本判定。

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

角色使用 `binding_lifecycle_grants` 与 `binding_publication_grants` 两组声明：

`binding_lifecycle_grants` 获得：

- `speech_publications`、`speech_publication_retirements`、
  `speech_publication_lifecycle_events` 和 `speech_binding_disable_intents` 的
  `SELECT, INSERT`；
- `speech_publication_heads` 的 `SELECT, INSERT, DELETE` 及
  `revision, revision_id, updated_at_us` 列级 `UPDATE`；
- binding revision/evidence 的 `SELECT`；
- `speech_catalog_state` 的 `SELECT` 及 `revision, generated_at_us` 列级 `UPDATE`；
- `speech_asset_references` 的 `consumer_kind, consumer_id, consumer_revision,
  released_at_us` 列级 `SELECT` 及 `released_at_us` 列级 `UPDATE`（voice 退休释放引用，
  无 INSERT/DELETE）。

`binding_publication_grants` 追加发布命令需要的权限，且不扩大不可变事实的写面：

- `speech_publications`、`speech_publication_lifecycle_events` 的 `SELECT, INSERT`；
- `speech_binding_publication_evidence` 的 `SELECT, INSERT`（证据行只追加，无 UPDATE/DELETE）；
- `speech_binding_revisions`、`speech_publication_retirements` 的 `SELECT`；
- `speech_publication_heads` 的 `SELECT, INSERT` 及
  `revision, revision_id, updated_at_us` 列级 `UPDATE`（无 DELETE）；
- `speech_catalog_state` 的 `SELECT` 及 `revision, generated_at_us` 列级 `UPDATE`。

启动会拒绝 owner、schema CREATE、序列权限、其它 speech 表权限和更宽的列级写权限；
证据表上多出一条列级 `UPDATE`、或 head 上多出 `DELETE`，`BindingLifecycleRuntime.open()`
都会拒绝启动。升级已有部署后必须由 owner 重新执行两组声明，否则启动失败。
`0047_voice_publication_evidence` 增加了素材引用的列权限；
`0051_binding_publication_events` 把发布命令纳入事件账本。
未配置 DSN 时三个生命周期端点 fail closed，返回
`503 binding_result_unknown`；不会复用 draft writer 或 owner 身份。

## 发布

把已存草稿 revision 发布为新的选中 head：

```bash
curl -sS -X POST \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: publish-asr-quality-2-20261113' \
  -d '{"expected_revision":"2","expected_current_revision":"1",
       "capacity_manifest_id":"cap-asr-quality-2",
       "qualification_report_id":"qrep-asr-quality-2"}' \
  http://127.0.0.1:8000/v1/nexus/admin/model-bindings/asr-quality/publications
```

`expected_revision` 是目标草稿 revision，`expected_current_revision` 是提交前观察到的
head（无 head 时为 `null`）。成功返回 `201` 与更新后的 `ModelBindingRevision`；
`capacity_manifest_id` 必须与草稿 `execution_recipe` 声明的值一致，否则 `422
capacity_manifest_mismatch`。两个 evidence ID 是调用方声明的事实，不证明容量清单或
资格报告的内容、真实引擎或生产准入。

发布的行为是显式的：

- 相同完整意图与相同 `Idempotency-Key`：返回 `200` 与原结果，不新增发布、证据或事件，
  目录版本不再次推进；并发同 key 请求在事件账本 advisory lock 上串行化；
- 相同 key、不同意图：`409 idempotency_conflict`，零写入；
- 目标 revision 已发布或已退役、换新 key 再发布：`409 binding_not_draft`；
  head 只指向已发布版本，重复发布会建立第二条事实行，因此被拒绝；
- 观察的 head 已变化：`409 configuration_changed`，消息带实际观察到的 revision；
- 目标 revision 不存在：`404 binding_not_found`。

一次真实发布在同一事务中只写一行 `speech_publications` 和一行
`speech_binding_publication_evidence`（`evidence_source=publish_http`）、把 head 指向该
revision、由触发器推进一次目录版本，并追加一条 `model_binding.publish` 事件。
未配置写入角色时返回 `503 binding_result_unknown`，不落库。

发布与运维命令 `speechnexus-publish-binding` 是两条独立路径：该命令还要求
`expected_catalog_revision` 与 installation/database 观察值，公共 HTTP 契约无法表达
这些字段，见[发布命令说明](publish-binding-revision.md)。

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

服务端报告提交结果未知时返回 `503 binding_result_unknown`，`retryable: false`；
客户端超时或连接中断也可能收不到响应，同样不能判断提交结果。客户端不得换 key 或伪造新意图；
保留原路径、完整 body 和原 `Idempotency-Key` 显式重放同一 POST，以收敛到原结果。
完整 body 包括原 `expected_current_revision`；不可套用提示值重基 CAS。后一次拒绝
或当前 head GET 都不能证明先前未知提交的结果。未保留原 key 时不承诺安全自动恢复。
dev5 的兼容影响与同版测试矩阵见[激活验证说明](binding-activation-verification.md)。

## 验证与边界

`tests/integration/test_binding_lifecycle_pg.py` 在隔离 PostgreSQL 上使用真实
HTTP ASGI 请求覆盖：退役后 `/v1/models` 为空且 ETag 变化、新 Job 不可选中、
在途 Job 的固定配置不变、回滚到 revision 1 后目录恢复、重复/未知/CAS 冲突、
最小权限启动及审计事件写入。它不证明真实部署网络、真实引擎、容量或生产资格。

voice 有独立的[内建声音资格与发布路径](builtin-voice-qualification.md)，可在满足资格条件后持久发布固定 revision；不能将它等同于 binding 的管理生命周期。`retireVoiceRevision` 已实现 `retire`：它复用本页的 publication writer 写入 `speech_publication_retirements`，写作与读取证据见[契约一致性审计 §3.3](contract-conformance-audit.md#33-490-新增-retirevoicerevisionretire)。`disable` 仍返回 422 `voice_disable_unsupported`，因为不存在按 `voice_revision_id` 捕获在飞 target 的协调路径；不能从 binding disable 或内建声音发布推断 voice disable 已完成。
