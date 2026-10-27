---
title: 回填 binding 发布证据并如实投影 admin GET
status: active
version: "1.0.0"
date: 2026-10-28
---

# 回填 binding 发布证据并如实投影 admin GET

`0021_publication_evidence` 新增独立的证据表。它记录的是**发布时事实**
（`capacity_manifest_id` / `qualification_report_id`），不写入不可变 revision 的
`definition_json`，也不扩宽公共目录 reader 的权限。

admin GET（`GET /v1/nexus/admin/model-bindings/{binding_id}/revisions/{revision}`）
现在按存储事实投影：

| 存储状态 | 响应 |
| --- | --- |
| 无 publication 的 draft | `status: draft`，全部发布字段为 `null` |
| publication + 匹配证据 | `status: published`，`published_at`、两个证据 ID 原样返回 |
| publication 无证据 | `503 binding_unavailable`（不返回 draft，不伪造 published） |
| publication / 证据 / `revision_id` / 容量不一致 | `503 binding_unavailable`（按存储损坏处理） |

POST 创建草稿的 `201` 响应仍保持 draft 形状。

## 权限与配置

- 发布写入角色：`publication_grants` + `binding_draft_grants` + `publication_evidence_grants`
  （证据表 `SELECT, INSERT`）。
- admin GET 独立只读角色：`binding_admin_reader_grants`，仅对三张 binding 历史表、
  `speech_publications` 与证据表持有 `SELECT`；不得有 INSERT/UPDATE/DELETE。
- **公共目录 reader 不获得证据表权限**；`model_catalog_reader_grants` 不变。
- API 进程设置 `SPEECHNEXUS_BINDING_ADMIN_READER_DATABASE_URL` 才会启用投影；
  未配置时固定 binding GET 返回 `503 binding_unavailable`（fail closed），不会回退到 writer，
  也不会把已发布 revision 谎报为 draft。启动只验证权限，不执行迁移或 grant。

## 生产 preflight（硬闸门）

迁移或上线前，对**生产库**执行只读 preflight：

```bash
export SPEECHNEXUS_PUBLICATION_WRITER_DATABASE_URL="postgresql://<writer-role>:<secret>@<host>:5432/<db>"
uv run speechnexus-backfill-binding-evidence --preflight
```

输出：

```text
preflight publications=<N> evidence=<M> unresolved=<K>
unresolved object_id=... revision=... revision_id=...
```

规则：

- `publications > 0` 且 `unresolved > 0` 且没有可用映射 → 退出码 `10`，**硬停**；
- 有映射但校验失败、或 apply 后仍有未解决行 → 同样硬停；
- 只有 `unresolved = 0`，阶段 1 才能作为生产候选；
- 测试库 0 行不能推断生产库。`503` 只保留给存储损坏或未预见的遗留，不是正常发布路径。

## 映射文件

显式 JSON 数组，每条至少包含：

```json
[
  {
    "kind": "binding",
    "object_id": "asr-quality",
    "revision": "1",
    "revision_id": "mbrev-...",
    "capacity_manifest_id": "cap-...",
    "qualification_report_id": "qrep-...",
    "capacity_manifest_digest": "sha256:...",
    "qualification_report_digest": "sha256:..."
  }
]
```

- `kind` 可省略，缺省且只能是 `binding`；
- `revision` 可用十进制字符串或整数；
- 两个证据 ID **没有默认值**，digest 可省略；
- 同一 `(object_id, revision)` 重复映射且内容不同 → 拒绝；
- `capacity_manifest_id` 必须等于该 revision 的 `execution_recipe.capacity_manifest_id`。

## dry-run / apply / export

```bash
uv run speechnexus-backfill-binding-evidence --dry-run --mapping evidence.json
uv run speechnexus-backfill-binding-evidence --apply --mapping evidence.json
uv run speechnexus-backfill-binding-evidence --export
```

- `--dry-run` 只读，输出 `insert` / `skip` / `unresolved_before` / `unresolved_after`；
  仍有未解决行时退出码 `11`。
- `--apply` 在**单个事务**内校验并写入；任一校验失败、冲突或 apply 后 `unresolved != 0`
  都会整体回滚。输出同时给出证据表 `count(*)` 前后值。
- 幂等：相同映射重复 apply，`inserted=0`、`skipped=N`，前后计数不变；
  内容不同则冲突失败，不静默覆盖。
- `--export` 在回滚前导出当前证据映射（JSON 到 stdout），用于保留证据事实。
- 回填来源固定记为 `backfill`，不伪装成 `operator_cli`。

退出码：

| 结果 | 退出码 |
| --- | --- |
| preflight 清零 / dry-run 可清零 / apply 成功 / export | 0 |
| 参数或配置非法 | 2 |
| preflight 仍有未解决 publication | 10 |
| 映射非法、冲突、容量不一致或 apply 后仍未清零 | 11 |
| 存储不可用或 apply 结果未知 | 12 |

## 迁移回滚

`downgrade` 只在证据表为空时允许 `DROP TABLE`；非空时抛出
`Publication evidence requires export and preservation`。回滚只删除证据表与索引，
不触碰 `speech_publications`、revision 或 draft 行。回滚前必须 `--export` 并保留字节与 SHA-256。
回滚是维护态操作，不等于恢复生产资格。

## 验证范围

`tests/integration/test_binding_publication_evidence.py` 在隔离 PostgreSQL 上覆盖：
draft / published / 无证据 503 三种 GET；公开链仍可解析无证据 publication 的过渡矛盾；
容量不一致与证据冲突；回填 dry-run/apply 幂等与全回滚；preflight 硬停；
迁移 up/down；writer / admin reader / 公共 reader 的权限负向。
合成 fixture 只证明链路与存储语义，不构成真实引擎、部署拓扑或产品准入。
