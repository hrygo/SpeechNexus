---
title: 发布 binding revision（运维命令）
status: active
version: "1.1.0"
date: 2026-10-28
---

# 发布 binding revision（运维命令）

该命令发布已存在的不可变 binding revision，并以显式观察值保护当前选择。它不创建草稿，也不实现 `publishModelBindingRevision` HTTP 端点。证据 ID 是操作员提供的事实，不证明资格报告内容、真实引擎或生产准入。

## 前置条件

数据库使用当前迁移 head（包括 `0024_binding_publish_intents`）。发布角色是独立的非 owner 角色，具备 `binding_draft_grants`、`publication_grants` 和 `publication_evidence_grants`；升级后须重新按声明配置新增表的最小权限。观察和提交必须访问同一权威主库及同一 schema，不使用缓存或副本。配置 `SPEECHNEXUS_PUBLICATION_WRITER_DATABASE_URL` 和可选的 `SPEECHNEXUS_DATABASE_SCHEMA`；凭据通过部署的秘密配置提供，不写入命令参数、日志或工单。

可通过 `SPEECHNEXUS_PUBLICATION_TRANSACTION_TIMEOUT_SECONDS` 设置观察和发布事务的超时，
默认 3 秒，须为大于 0 且不超过 30 的有限数。提交阶段超时或连接中断不能证明事务回滚，
应按下文的结果未知流程使用原意图恢复。

切换前必须停止或隔离旧版本发布者和可直接写 head 的旁路 SQL，并核实角色权限。新应用内的 CAS 不约束绕过该代码的写者；未完成切换前不能保证并发安全。克隆或恢复数据库会复制 installation UUID，因此 UUID、数据库名与 schema 相同也不能证明是同一实际环境；还须由运维核对原部署及数据库实例的非秘密身份。

## 观察并形成意图

在操作员决定发布之前运行：

```bash
uv run speechnexus-publish-binding observe --binding-id asr-quality --schema speech
```

该命令在主库同一语句读取 `installation_id`、`database_name`、`schema_name`、`current_revision` 和 `catalog_revision`。输出的 `current_revision: null` 表示该 family 存在且当前无 head；family 不存在、当前 head 已退役或读取不可靠时命令以退出码 5 报告只读观察不可用，不得解释为空。配置或 schema 无效时退出码为 2，不产生观察结果。将观察结果与目标 binding/revision/revision_id、两个 evidence ID、可选 digest 和一个新 idempotency key 一起保留，供确认和未知结果恢复。不要在提交时重新读取并替换观察值；观察后状态变化，应重新检查并形成新意图。

## 发布

以下参数均来自**先前观察和已确认的发布意图**，方括号仅作说明：

```bash
uv run speechnexus-publish-binding \
  --binding-id asr-quality \
  --revision 1 \
  --revision-id '[已存 revision_id]' \
  --capacity-manifest-id '[capacity manifest ID]' \
  --qualification-report-id '[qualification report ID]' \
  --expected-current-revision none \
  --expected-catalog-revision '[先前观察的 catalog_revision]' \
  --observed-installation-id '[先前观察的 installation_id]' \
  --observed-database '[先前观察的 database_name]' \
  --schema '[先前观察的 schema_name]' \
  --idempotency-key '[此次意图的固定 key]'
```

`--expected-current-revision none` 仅表示先前明确观察到空 head；有 head 时传其十进制 revision。`--capacity-manifest-digest` 和 `--qualification-report-digest` 可选，格式为 `sha256:<64 位小写十六进制>`。等价入口为 `uv run python -m speechnexus_backend.publish_binding`。

首次登记意图时，事务在对象锁内校验固定草稿和证据；新建 publication 时检查先前 head 与 catalogue revision，并写 publication、head、证据和原意图。已有 publication 的 `existing_fact` 路径不会更新 head，也不会执行这项 head/catalogue CAS，不能把成功登记当作观察值仍然有效。原意图重放先比对目标及完整意图，读取已有 publication 和证据；它不重新执行草稿、证据内容或 head 的新建校验，不能代替当前状态核验。事务在提交前失败时不会提交其中的写入；提交回执丢失须按下文处理。catalogue revision 也是 ABA 保护：受控写者在 head 经中间状态返回原值时仍会推进它。其他对象的变更也可能推进全局 revision 并导致保守冲突，此时重新观察和确认。直接修改 head 而不推进版本的旧写者仍可绕过保护。

结果中的 `outcome=created` 是新建事实，`outcome=replay` 是同 key、同完整意图的已登记结果，`original_created` 保留原操作是否新建事实。`outcome=existing_fact` 表示所请求的 publication 早已存在，本次登记的是当前调用，**不证明历史 publication 由这个 key 或观察产生**；若此前无证据，本次会补录证据，但不会重设 head。已有不同证据保持冲突。重放不会把后续变更过的 head 改回原值。

| 结果 | 退出码 |
| --- | --- |
| 创建、已登记意图重放或已有事实登记 | 0 |
| 参数、配置或标识非法 | 2 |
| 目标草稿不存在或其 revision_id 不匹配 | 3 |
| head/版本/目标变化、同 key 意图不同、已有 publication 的 revision_id 不匹配或证据冲突 | 4 |
| 存储不可用或提交结果未知 | 5 |
| 容量清单与 revision 不匹配 | 6 |

上述退出码是命令显式处理的分类，并不覆盖所有异常：例如连接池初始化或打开时抛出的未捕获异常不保证返回 `5`。任何未得到明确成功的提交都须保留原意图并按下节核对；不能仅凭其他退出码推断没有提交。

首次登记意图时先检查已有 publication 的 revision_id，再检查目标草稿；因此已有 publication 的 revision_id 不匹配时返回 `4`，即使草稿也不匹配。已登记意图的重放走前述重放路径，不重新执行这两项检查。

## 提交结果未知

退出码 `5` 或其他未得到明确成功的结果可能需要核对原意图。不要换 key、目标、schema、观察、binding/revision/revision_id 或证据；先核对原非秘密部署和数据库实例身份，再用保存的**完整原意图**在原目标同 key 重放。相同 key 与不同意图会冲突，不能由固定 revision 的存在推断原命令成功。无法确认目标或原意图时停止并协调，不对另一数据库尝试恢复。

`0024` 的安装身份与发布意图是恢复证据，迁移拒绝向旧版本降级；回退须另行设计和审查。当前实现只在 PostgreSQL 测试环境验证；旧写者切换、恢复/克隆环境识别、部署权限、HTTP 发布消费者和真实拓扑仍需单独验收。公共 HTTP 契约没有本 CLI 使用的 catalogue revision 字段，不能把该 CLI 证明当作 HTTP 端到端实现。

公开目录和 Job 读取 head；退役不删除既有 publication、证据和已固定的 Job。内容解析成功不代表 recipe、instance、真实引擎或容量已经就绪；相关验证见 [binding 生命周期操作](binding-lifecycle.md) 和 [证据回填](backfill-binding-publication-evidence.md)。
