---
title: 对账恢复：reconciliation_required 与 unknown 执行
status: active
version: "1.0.0"
date: 2026-10-28
---

# 对账恢复：reconciliation_required 与 unknown 执行

本文是 `reconciliation_required`、`unknown_quarantined`、租约不释放和准入持续
`503 backend_unavailable` 的只读排查与升级路径。基线为
`c25772189f3c4c9d3a4cc241eef3f1b8338c4870`；数据库证据来自隔离 `del109-pg`
（`127.0.0.1:55442`，迁移 `0021_publication_evidence`）。

**先给结论：当前没有面向运维的、可任意指定一条 unknown 执行并把它改成
`succeeded` 的产品命令。** 能自动闭合 unknown 的受支持事实是同一实例代次的
“进程/容器已退出且资源已释放”停止证明；它只把执行收口为 `aborted`。已有结果对象
不等于停止证明，也不会把 unknown 倒推成成功。`reconciliation_required` 是拒绝/恢复
提示，不是可以查询或清理的数据库状态。

## 1. 触发条件

| 现象 | 判定依据 | 首先做什么 |
| --- | --- | --- |
| 数据库出现 `unknown_quarantined` | `submit_intents.execution_state`；当前应为 0，出现即进入本文 | 按原 `submit_intent_id` 查完整事实，不新建执行 |
| 执行网关或准入返回 `reconciliation_required` | 503 错误信封的 `recovery=reconcile`，或进程 trace/日志中的同名字段 | 保留原身份，先查原始意图、租约、停止证据是否一致 |
| 租约长期为 `quarantined` / `cleanup_pending` | `admission_leases.lease_state`、`cleanup_state`、`budget_released_at_us` | 核对执行的 terminal + stop evidence 是否完整；不要从计数器直接归还 |
| 准入持续 `503 backend_unavailable` | HTTP 面只给稳定拒绝码；进程内原因可能是 `quota_exceeded`、`capacity_exhausted` 或 `reconciliation_required` | 分开记录 HTTP 事实与进程内 trace，不把 503 都归因于对账 |
| 结果对象存在但执行没有终态 | `staging_output_object_key` / `staging_result_document` 有值，`execution_stop_evidence` 为 0 | 先校验对象版本、长度和摘要；仍不要直接写成功 |
| 租约已 `released/complete`，执行仍是 unknown | 两张表的事实互相矛盾，不是“容量已干净” | 停止释放动作，按第 6 节升级；不要重复释放或改回租约 |

`reconciliation_required` 不等于“可以重试一次”。只有 `retry_transaction` 明确表示同一
事务没有成功提交且没有未知副作用时才允许有限重试；出现原身份事实不一致时，应保持
现场并协调。

## 2. 禁止动作

- **不要 `UPDATE` / `DELETE` / 手工 `INSERT`** `submit_intents`、
  `admission_leases`、`execution_stop_evidence`、`engine_instances` 或配额表。删掉
  unknown 行会丢失“是否已向引擎外发”的唯一证据；改状态或补证据会制造不可审计的结论。
- **不要用同一或新 operation/attempt 重跑“修补”**。旧执行可能已经在引擎侧运行，
  重放可能造成重复执行、重复计费或双份输出；新 attempt 不是旧执行结果的查询。
- **不要因为进程重启、连接断开、超时、404 或容器“看起来没了”就归还容量**。这些只
  证明观察失败，不证明旧执行已停止且资源已释放。
- **不要把对象存在当成 `engine_terminal_response`**，尤其不要把 unknown 直接写成
  `succeeded`。当前状态机明确拒绝 unknown → succeeded
  （`execution-gateway/src/speechnexus_gateway/domain/submission.py:756` 的
  `apply_stop_evidence`）。
- **不要手工构造、复制固定摘要或插入第二条 stop evidence**。同一 execution 只有一条
  不可变证据；重放同一事实与换一条事实必须由原事务区分。
- **不要直接改预算/隔离计数器**。容量归还必须由租约释放事务在锁定父账目后完成；只改
  计数会留下无法恢复的跨表矛盾。
- **不要调用测试辅助脚本清理生产或共享 unknown**。`local-end-to-end.md` 第 11.2.2 节
  的人工脚本只针对 `executing` + 已登记 staging 的本机测试样本，且明确不是产品能力。

## 3. 查证步骤

### 3.1 只读会话与输入

优先使用 `submit_intent_id`；只有它丢失时，才用 `lease_id` 反查，不能反过来新建一个
identity。下面命令以 `SN_READONLY_DSN` 指向批准的只读连接，并强制只读事务：

```bash
psql "$SN_READONLY_DSN" -X -P pager=off -v ON_ERROR_STOP=1 \
  -v submit_intent_id='intent-example' \
  -v lease_id='lease-example' \
  -v instance_id='instance-example' \
  -v instance_generation='1' \
  -v workspace_id='workspace-example' <<'SQL'
BEGIN TRANSACTION READ ONLY;
-- 本节的 SELECT 放在这里
ROLLBACK;
SQL
```

本地 `del109-pg` 的端口映射为 `127.0.0.1:55442 -> 5432`。本 checkout 没有
`.paperclip-runtime/del109/owner.env`，本次没有可用的 TCP 只读口令；因此下列 SQL 实际
通过 `docker exec del109-pg psql` 在同一实例、同一数据库和显式
`BEGIN TRANSACTION READ ONLY` 中执行。面向运维的 TCP 命令未在本次实测，标注为
`not_run`；不要猜口令或改用写身份。

### 3.2 查询 1：当前 unknown 与容量投影

```sql
SELECT execution_state, cleanup_state, count(*) AS intents
FROM speech.submit_intents
GROUP BY 1, 2
ORDER BY 1, 2;

SELECT instance_id, instance_generation, instance_state,
       max_slots, held_slots, quarantined_slots,
       last_fencing_token, stopped_at_us, quarantine_reason
FROM speech.engine_instances
WHERE instance_id = :'instance_id'
  AND instance_generation = :'instance_generation';

SELECT workspace_id, max_attempts, held_attempts,
       quarantined_attempts, version
FROM speech.workspace_quotas
WHERE workspace_id = :'workspace_id';
```

期望字段：各状态计数；实例的 `held_slots` / `quarantined_slots`；工作区的
`held_attempts` / `quarantined_attempts`。unknown 会让“执行事实”不干净；只有
quarantined 计数非零才说明容量仍被隔离。两者不能混为一谈。

### 3.3 查询 2：按原始身份还原执行、租约和配额

```sql
SELECT s.submit_intent_id, s.operation_id, s.attempt_id, s.lease_id,
       a.workspace_id, l.stage_id, l.host_node_id, l.pool_id,
       l.instance_id, l.instance_generation, l.gateway_id, l.fencing_token,
       s.execution_state, s.cleanup_state, s.error_code, s.version,
       s.engine_request_id, s.dispatched_at_us, s.terminated_at_us,
       l.lease_state, l.cleanup_state AS lease_cleanup_state,
       l.budget_released_at_us, l.quarantine_observed_at_us,
       l.quarantine_reason_code,
       EXISTS (
         SELECT 1 FROM speech.output_sample_intervals i
         WHERE i.submit_intent_id = s.submit_intent_id
       ) AS has_output_intervals
FROM speech.submit_intents s
JOIN speech.admission_attempts a
  ON a.operation_id = s.operation_id AND a.attempt_id = s.attempt_id
LEFT JOIN speech.admission_leases l ON l.lease_id = s.lease_id
WHERE s.submit_intent_id = :'submit_intent_id';
```

该行必须同时回答：这是哪个 operation/attempt；是否真的外发（`dispatched_at_us`、
`engine_request_id`）；租约是否已释放；是否有输出区间；版本和错误码是什么。若只拿
到 `lease_id`，用下面的租约反查，不要按时间或 workspace“找一条最像的”：

```sql
SELECT s.submit_intent_id, s.operation_id, s.attempt_id, s.lease_id,
       s.execution_state, s.cleanup_state, s.engine_request_id,
       l.fencing_token, l.lease_state, l.cleanup_state AS lease_cleanup_state
FROM speech.submit_intents s
JOIN speech.admission_leases l USING (lease_id)
WHERE l.lease_id = :'lease_id'
ORDER BY s.submit_intent_id;
```

### 3.4 查询 3：停止证据、staging 回执与对象身份

```sql
SELECT s.submit_intent_id,
       count(e.evidence_id) AS stop_evidence_rows,
       max(e.evidence_kind) AS evidence_kind,
       max(e.observed_at_us) AS evidence_observed_at_us,
       (s.staging_output_object_key IS NOT NULL) AS has_staging_key,
       (s.staging_result_document IS NOT NULL) AS has_staging_document,
       s.staging_output_object_key,
       CASE WHEN s.staging_result_document IS NULL THEN NULL
            ELSE s.staging_result_document::jsonb -> 'reference' ->> 'storage_bucket'
       END AS storage_bucket,
       CASE WHEN s.staging_result_document IS NULL THEN NULL
            ELSE s.staging_result_document::jsonb -> 'reference' ->> 'content_sha256'
       END AS content_sha256,
       CASE WHEN s.staging_result_document IS NULL THEN NULL
            ELSE (s.staging_result_document::jsonb -> 'reference' ->> 'byte_length')::bigint
       END AS byte_length
FROM speech.submit_intents s
LEFT JOIN speech.execution_stop_evidence e USING (submit_intent_id)
WHERE s.submit_intent_id = :'submit_intent_id'
GROUP BY s.submit_intent_id;
```

期望：`stop_evidence_rows` 只能是 0 或 1；有证据时 `execution_stopped=1`、
`resources_released=1`，且 kind 与终态匹配。staging 只有在 key 和 document 同时存在时
才可做对象核验；document 的 `storage_bucket`、`content_sha256`、`byte_length` 是读回时
的期望值。

### 3.5 对象只读核验（有 staging 时才做）

以下命令用于本地隔离 MinIO；生产必须替换为部署批准的只读 S3 身份。它只读 HEAD 和
流式摘要，不把正文写入日志：

```bash
MINIO_USER="$(docker exec del109-minio printenv MINIO_ROOT_USER)"
MINIO_PASS="$(docker exec del109-minio printenv MINIO_ROOT_PASSWORD)"
trap 'unset MINIO_USER MINIO_PASS' EXIT

curl -sS -I --aws-sigv4 'aws:amz:us-east-1:s3' \
  --user "$MINIO_USER:$MINIO_PASS" \
  'http://127.0.0.1:59010/del109-file-asr/<OBJECT_KEY>' \
  | sed -n '1,30p'

curl -sS --aws-sigv4 'aws:amz:us-east-1:s3' \
  --user "$MINIO_USER:$MINIO_PASS" \
  'http://127.0.0.1:59010/del109-file-asr/<OBJECT_KEY>' \
  | tee >(wc -c >&2) | shasum -a 256
```

通过条件：HEAD 为 200，`Content-Length` 等于 DB 的 `byte_length`，流式 SHA-256 等于
DB 的 `content_sha256`（去掉 `sha256:` 前缀），并记录 `x-amz-version-id`。只有 HEAD
成功或对象 key 存在，不足以证明内容完整。

## 4. 判定分支

| 分支 | 只读判据 | 下一步与依据 |
| --- | --- | --- |
| A. 引擎可能从未外发 | `dispatched_at_us IS NULL`、`engine_request_id IS NULL`、无 staging/document、无输出区间 | 仍需证明该实例代次已终止才能把 unknown 收口为 `aborted`；当前不能把 unknown 降级为 `failed/not_required`。没有停止证明就升级，不新建执行 |
| B. 引擎可能已运行 | `dispatched_at_us` 或 `engine_request_id` 有值，停止证据为 0 | 用原 `engine_request_id` 查询引擎/网关原始事实；若仍有终止适配器，可走 abort；否则等待节点控制器的实例停止证明。任何查询超时都保持 unknown |
| C. 结果已在对象存储 | staging key 和 document 都有；HEAD 200；长度、SHA-256 与 document 一致 | 保留固定版本对象和回执；这仍不是停止证明。当前 unknown 不能因结果存在而写 `succeeded`，应升级为“结果存在、终态写者缺失”的实现缺口 |
| D. 证据缺失或互相矛盾 | stop evidence >1、document 与对象摘要/大小不一致、lease 已释放但 execution 仍 unknown、身份字段不一致、时间早于 dispatch | 立即停止恢复动作，保留现场并升级。不要改任何表、不要补第二条证据、不要重复释放 |
| E. 只有真实实例终止证明 | 同一 instance/generation；进程或容器已退出；资源已释放；观察时间不早于 recorded/dispatched | 这是唯一能把 unknown 收口为 `aborted` 的机器事实；由网关恢复扫描消费，不由运维直接写库 |

分支 A 的“没看到 dispatch”不是“确定没有提交”：崩溃可能发生在持久 intent 与
dispatch 记录之间。当前状态机要求未观测执行的 unknown 也必须有实例终止证据，不能
用 `mark_not_dispatched_failure` 降级。

## 5. 恢复动作

### 5.1 当前受支持路径

| 接口/路径 | 当前事实 | 可以做什么 | 不会做什么 |
| --- | --- | --- | --- |
| 网关周期恢复扫描 | `ExecutionRecoveryService` 读取本 gateway/node 的 unknown；需要 `SPEECHNEXUS_GATEWAY_RECOVERY_CONFIG_FILE` 和独立节点凭据才有 worker（`execution-gateway/src/speechnexus_gateway/infrastructure/recovery_configuration.py:58`） | 向节点控制器查询 `verified-stop`；取得完整实例终止证明后调用 `record_stop(..., ABORTED)` | 不释放 A 的 lease/quota，不把成功结果改成成功终态，不在查询失败时猜停止 |
| `POST /internal/v1/executions/{submit_intent_id}/abort` | 网关已注册的组件鉴权 HTTP 入口 | 用原始 `operation_id`、`attempt_id`、`fencing_token` 发起一次有界 abort；适配器返回完整停止证据时记录 aborted | 不保证 file worker 能提供证据（`file_execution_worker.py:382` 只取消本进程任务，未返回 stop evidence），不会直接归还预算 |
| `record_stop` / `mark_execution_unknown` | 可信进程内的 `ExecutionFactBoundary`；文件头明确 no HTTP route（`execution-gateway/src/speechnexus_gateway/interfaces/submit_transactions.py:3`） | 供生产 adapter/恢复 worker 写入不可变执行事实；`mark_execution_unknown` 只能让风险保持/升级 | 不是运维 CLI；不能用来改写任意已有 unknown，不能绕开证据或版本守卫 |
| 后端 `release_lease` / `release_in_transaction` | `LeaseTransactionBoundary` 是进程内端口，没有 HTTP route；当前 `backend/src/speechnexus_backend/app.py` 的路由清单未装配该服务 | 在原始执行已是 terminal + complete、停止/资源释放证据齐全且 version 匹配时原子释放 lease、槽位和配额 | 不接受 unknown、timeout、404、重启或调用方自报的布尔值；不能在租约已释放后重扣预算 |
| OpenAPI 声明的 `POST /v1/nexus/admin/admission-leases/{lease_id}/quarantine-releases` | **契约先行，生产未实现**：`contracts/admin-openapi.yaml:878` 有声明，但后端 router/服务中没有对应 handler；当前 `speech.admission_leases` 也没有 `quarantine_release_*` 列 | 无。不要调用或伪造该入口 | 这不是当前受支持路径；缺 API、服务和数据库证据列，属于 G7 缺口 |

`record_stop` 写入的是“执行已停止”的不可变事实；`release_lease` 才归还 A 侧的
lease、instance/host 和 workspace 配额。两者分属不同权威，不能互相替代。只把
`group`/`count` 改回 0 不是释放。

### 5.2 必须人工双证据确认的动作

只有在机器证据不可得、且任务明确授权人工核验时才考虑：

1. **执行已停止**：旧进程/容器确实退出（不是仅仅“进程列表看不到”）；
2. **资源已释放**：该实例代次的槽位、显存/统一内存、host/quota 计数已有对应释放事实；
3. 证据必须绑定同一 `submit_intent_id`、`instance_id`、`instance_generation`、
   `fencing_token`，时间不早于 dispatch，且摘要来自原始证据而不是当前时钟；
4. 原子地关闭全部未解决 submit intent，再释放 lease；只写证据或只释放租约都不算完成。

当前产品没有实现这条人工双证据入口。即使满足以上事实，也只能升级给实现 owner；
不要直接执行 SQL 或复制测试脚本。

## 6. 升级路径

出现以下任一情况，停止本 runbook 的自动动作，交唯一 SM+PO 决定并标注接收人：

- staging 对象与 document 的 bucket/key/长度/SHA-256 不一致，或对象版本无法固定；
- 租约已释放但执行仍 unknown，或 `budget_released_at_us` 与 release 证据不一致；
- `execution_stop_evidence` 超过 1 条、证据时间早于 dispatch、或 instance/generation 不匹配；
- 准入持续 503，且无法把 HTTP 拒绝码与进程内 `reconciliation_required` 分开；
- 当前网关没有恢复配置/节点凭据，节点控制器无法返回同一代次的 `verified-stop`；
- 需要调用 admin `quarantine-releases` 或直接复用后端 `release_lease` 才能推进。

升级包至少包含：完整 base SHA、原 `submit_intent_id` / `lease_id` /
`fencing_token`、第 3 节三组查询输出、对象 `x-amz-version-id` / 长度 / SHA-256、
实例 generation 与停止观察、实际执行过的命令和未验项。不要附音频正文、完整转写、
凭据或内部地址。平台/网关 owner 负责机器停止与恢复配置；后端 owner 负责 admin
入口、schema 与 lease 释放装配；产品顺序和是否接受“结果存在但终态无法恢复”由
唯一 SM+PO 决定。

## 7. 当前两条 unknown 的只读判定

2026-10-28 对 `del109-pg` 的只读快照（查询结果见文末）如下：

| submit_intent_id | 原始事实 | 当前 lease/quota | 分支与结论 |
| --- | --- | --- | --- |
| `intent-del109` | dispatched `2026-10-27T23:20:29.512442Z`；`engine_request_id=file-command-664100f692f048d5806a9ccf22460c73`；staging document 为 185 B，摘要 `sha256:c707b707…4477b8`；对象 HEAD 200、版本 `d83608ea-1762-4902-bbff-ba55da84dbb8`、流式摘要一致；stop evidence 0 | lease `released/complete`，budget released；instance/quota 当前均为 0 | **主分支 C，同时命中 D 的租约/执行矛盾**：结果和对象都在，但没有停止证明，当前状态机禁止 unknown→succeeded；租约又已释放。保留对象与回执，不能重跑，不能写成功；升级给终态写者与 admission owner |
| `submit-18ca366b5656e9f0b38d8d3117b0f04192df490fe4adce2c` | dispatched `2026-10-28T00:32:04.026682Z`；`engine_request_id=file-command-f0b0c8c015b8495590e9c13d5abc5ca3`；无 staging key/document、无输出区间；stop evidence 0 | lease `released/complete`，budget released；instance/quota 当前均为 0 | **主分支 B，并命中 D**：引擎可能运行过但没有结果回执；没有实例终止证明，也没有可登记的停止事实。保留现场、按原 engine request 查询并升级；禁止重放、禁止用“lease 已释放”推定执行已停止 |

两条记录的 lease 都已 `released/complete`、`budget_released_at_us` 有值，而执行仍是
`unknown_quarantined`；按当前 `release()` 规则，unknown 没有 stop evidence 时不能走
这条释放路径。因此这是**当前快照的事实矛盾**，也说明“当前不占容量”不能作为对账完成
的证据。不要把这两条改回 quarantined，也不要再释放一次；应由 owner 查明历史写入者。

## 8. 本次证据与未验项

执行入口：`docker exec del109-pg psql -U postgres -d speechnexus`；每条查询都位于
`BEGIN TRANSACTION READ ONLY` 与 `ROLLBACK` 之间。实际结果：

```text
 execution_state      | cleanup_state | intents
----------------------+---------------+---------
 failed               | not_required  |       1
 succeeded            | complete      |       2
 unknown_quarantined  | quarantined   |       2

instance-del109 | 1 | ready | max_slots=1 | held_slots=0 | quarantined_slots=0
workspace-del109 | max_attempts=1 | held_attempts=0 | quarantined_attempts=0
```

```text
submit_intent_id                                         | stop_rows | has_staging_key | has_staging_document | byte_length
---------------------------------------------------------+-----------+-----------------+----------------------+------------
intent-del109                                            |         0 | t               | t                    |         185
submit-18ca366b5656e9f0b38d8d3117b0f04192df490fe4adce2c  |         0 | f               | f                    |
```

按 `lease_id` 反查命中同一组身份，两条都是 `fencing_token=1`、
`lease_state=released`、`lease_cleanup_state=complete`：

```text
lease-del109 -> intent-del109, unknown_quarantined, file-command-664100f692f048d5806a9ccf22460c73
lease-18ca366b5656e9f0b38d8d3117b0f04192df490fe4adce2c -> submit-18ca366b5656e9f0b38d8d3117b0f04192df490fe4adce2c, unknown_quarantined, file-command-f0b0c8c015b8495590e9c13d5abc5ca3
```

对象核验：

```text
HTTP/1.1 200 OK
Content-Length: 185
ETag: "d2ef00376459750a20b0723979bab62f"
x-amz-version-id: d83608ea-1762-4902-bbff-ba55da84dbb8
     185
c707b7078ba7f8161ea3738e14beaa3b97d627193e147c009c193cb2cc4477b8  -
```

已实测：上述查证 SQL 的只读事实、staging/stop 关系、当前实例/配额投影、MinIO HEAD
与摘要。未实测：TCP 55442 的认证命令（本 checkout 没有只读口令引用）、节点控制器
当前是否配置、真实 GPU/实例停止、生产 admin 入口（当前没有实现）、以及任何恢复写。

代码锚点（可直接 `rg`）：

- `execution-gateway/src/speechnexus_gateway/interfaces/submit_transactions.py:3`：
  `record_stop` / `mark_execution_unknown` 是 trusted process-local，no HTTP route。
- `execution-gateway/src/speechnexus_gateway/domain/submission.py:756`：unknown 只能
  在实例终止证据下收口为 `aborted`，不能变成 `succeeded`。
- `execution-gateway/src/speechnexus_gateway/infrastructure/recovery_configuration.py:58`：
  恢复 worker 需要私有配置和独立节点凭据。
- `execution-gateway/src/speechnexus_gateway/infrastructure/file_execution_worker.py:382`：
  当前 file worker 的 abort 只取消本进程任务，不产出 stop evidence。
- `backend/src/speechnexus_backend/speech/interfaces/lease_transactions.py:1`：
  lease 释放是进程内边界，没有 HTTP route。
- `backend/src/speechnexus_backend/speech/domain/lease_release.py:103`：release 必须消费
  terminal + complete + 完整停止证据。
- `contracts/admin-openapi.yaml:878`：只声明了 quarantine release 契约；生产实现和
  `quarantine_release_*` 列当前缺失。
