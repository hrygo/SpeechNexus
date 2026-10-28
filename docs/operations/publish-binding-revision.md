---
title: 发布 binding revision（运维命令）
status: active
version: "1.0.0"
date: 2026-10-28
---

# 发布 binding revision（运维命令）

公开读路径先查 `speech_publications`，缺失即 `model_not_found`；草稿写入不会自动发布。本命令是当前**受支持的发布路径**，在冻结的 `publishModelBindingRevision` 端点具备证据存储之前替代它。它只把**已存在的不可变 revision** 提升为公开，不创建、不修改修订，也不提供 HTTP 面。

## 前置条件

- 数据库已迁移到当前 head，且目标 revision 已由草稿写入产生；
- 数据库已应用 `0021_publication_evidence`、`0022_catalog_revision_state` 与 `0023_publication_lifecycle`；
- 独立非 owner 角色同时具备 `binding_draft_grants`（读取 `speech_binding_revisions`）、`publication_grants`（写 publication/retirement 并选择 head）与 `publication_evidence_grants`（读写证据表）；不得复用 Compose bootstrap owner；
- admin GET 另需只读角色与 `SPEECHNEXUS_BINDING_ADMIN_READER_DATABASE_URL`，见[证据回填与投影](backfill-binding-publication-evidence.md)；
- 显式设置发布写入 DSN，缺省不回退到通用数据库身份：

  ```bash
  export SPEECHNEXUS_PUBLICATION_WRITER_DATABASE_URL="postgresql://<writer-role>:<secret>@<host>:5432/<db>"
  export SPEECHNEXUS_DATABASE_SCHEMA="speech"   # 可选，缺省 speech
  ```

缺少 `SPEECHNEXUS_PUBLICATION_WRITER_DATABASE_URL` 时命令以退出码 `2` 明确失败，不会静默跳过。

## 命令

```bash
uv run speechnexus-publish-binding \
  --binding-id asr-quality \
  --revision 1 \
  --revision-id mbrev-<实际存储的 revision_id> \
  --capacity-manifest-id cap-<该 revision execution_recipe 中的值> \
  --qualification-report-id qrep-<可信资格报告 ID>
```

`--capacity-manifest-id` 与 `--qualification-report-id` **必填、没有默认值**；缺任一参数时 argparse 以退出码 `2` 失败，且不写 publication 或证据。`--capacity-manifest-digest` / `--qualification-report-digest` 可选，用于固定证据内容版本（`sha256:<64 位小写十六进制>`）。

等价入口：`uv run python -m speechnexus_backend.publish_binding ...`。`--schema` 可覆盖本次调用的 `SPEECHNEXUS_DATABASE_SCHEMA`。

成功输出一行稳定事实：

```text
published binding_id=asr-quality revision=1 revision_id=mbrev-... published_at_us=... \
  capacity_manifest_id=cap-... qualification_report_id=qrep-... created=true
```

`created=false` 表示同一三元组此前已发布，本次是幂等回放，`published_at_us` 仍是原发布时间，`catalog_revision` 与 ETag 也不会再次推进。

## 校验、事务与幂等

命令在**同一个事务**里先校验后写入，不存在“校验通过但写入失败”的中间态：

1. `(binding_id, revision)` 已发布且 `revision_id` 不同 → 冲突错误；
2. `speech_binding_revisions` 中不存在精确的 `(binding_id, revision, revision_id)` 三元组 → 拒绝，并报出缺失的 binding / revision，或报出实际存储的 `revision_id`；
3. 三元组存在但 `capacity_manifest_id` 与 revision 的 `execution_recipe.capacity_manifest_id` 不一致 → 拒绝，不写任何行；
4. 三元组存在且容量一致 → 在同一事务里写 `speech_publications`、选择 `speech_publication_heads`、再写 `speech_binding_publication_evidence`；publication 插入触发器同时推进 `speech_catalog_state`，证据写入失败则 publication、head、版本推进与证据一起回滚；
5. 同一 publication 已有证据：内容相同则幂等回放，内容不同则证据冲突；并发写入者先落库时回放其已存事实。

`speech_publications` 没有任何外键，因此“revision 必须存在”只能由本命令在写入端查库保证。发布是 insert-only：同一三元组重复执行**不新增行**；替换版本 = 发布更大的 revision。

| 结果 | 退出码 | stderr / stdout |
| --- | --- | --- |
| 首次发布或幂等回放 | 0 | `published ... created=true|false` |
| 参数、配置或标识非法 | 2 | usage 或 `invalid request: ...` |
| 三元组未存储 / `revision_id` 不匹配 | 3 | `not published: ...` |
| 同 revision 已发布为另一 `revision_id`，或已有不同证据 | 4 | `conflict: ...` / `evidence conflict: ...` |
| 存储不可用或提交结果未知 | 5 | `publication unavailable: ...` |
| `capacity_manifest_id` 与 revision execution recipe 不一致 | 6 | `evidence mismatch: ...` |

退出码 `5` 时写入可能已提交也可能未提交；用**完全相同的三元组**重跑即可幂等收敛，不要换 `revision_id` 重试。

## 现状与已知缺口

1. **admin GET 已如实投影**：有 publication 且有匹配证据时返回 `published`；只有 publication、没有证据时返回 `503 binding_unavailable`，不再谎报 `draft`。历史无证据行必须先按[证据回填](backfill-binding-publication-evidence.md)处理。
2. **binding 生命周期已接线**：`0023_publication_lifecycle` 提供 head、append-only 审计事件及事务端点；退役不会删除 publication、证据或历史 Job，激活/回滚只选择既有 published revision。配置与请求见[binding 生命周期操作](binding-lifecycle.md)。voice 仍只有契约声明与目录墓碑，尚无生产写路径或激活契约。
3. **该命令证明的是链路，不构成生产资格**：证据 ID 是发布时记录的事实，不是完整的资格验证；不代表容量、真实引擎或产品准入通过。
4. **过渡期两个接口可能不一致**：在历史无证据 publication 尚未回填时，公开解析仍能选中它，而 admin GET 返回 `503`。这是已知过渡状态；回填后 unresolved 必须为 0。

## 可见性与生效边界

- 公开读路径没有应用缓存或持久 pending：每次请求新开事务（`PostgresPublicJobs` 为 `READ_COMMITTED`，`PostgresModelCatalog.snapshot()` 为每请求一个 `REPEATABLE READ` 快照）。提交后新请求立即可见；唯一例外是提交前已开始的 `REPEATABLE READ` 事务，到事务结束前看不到，重试即可。HTTP 返回 `Cache-Control: no-store`。
- 仓库没有读副本配置；若部署把 reader 指向物理 replica，复制延迟是唯一无上界延迟，本命令不核部署拓扑。
- **解析成功 ≠ 可执行**：同步 ASR admission 还要求私有 recipe 的 `binding_publication_id` 与发布 `revision_id` 一致、instance 的 `binding_revision_id` 对齐；recipe 只在 API 启动时读取一次。发布 recipe 未指向的新 revision 会立即消除 `model_not_found`，但执行仍可能返回 `backend_unavailable`，直到更新 recipe、重启 API 并对齐 instance。验收必须区分“解析信号”和“执行信号”。

## 验证范围

`tests/integration/test_binding_publication_cli.py` 在隔离 PostgreSQL 上用产品草稿事务生成 revision，再用本命令发布，断言：发布前 `latest_binding` 为空、发布后可解析；重复发布前后 `speech_publications` 行数不变；同 revision 不同 `revision_id` 返回冲突且不写行；未存储三元组被拒绝。`tests/integration/test_publications_storage.py` 另外覆盖退役后目录 revision/ETag 推进、并发写入版本不重复及证据写入不推进版本。合成 fixture 只证明链路，不构成生产资格，也不证明真实引擎、部署拓扑或读副本行为。
