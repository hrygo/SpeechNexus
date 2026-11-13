---
title: 隔离租约的运维释放：持久证据、原子释放与幂等
status: active
version: "1.0.0"
date: 2026-11-13
---

# 隔离租约的运维释放：持久证据、原子释放与幂等

本页规定 `POST /v1/nexus/admin/admission-leases/{lease_id}/quarantine-releases`
（`releaseQuarantinedAdmissionLease`）的持久面、事务边界、幂等判定与权限面。它接在
[生产租约隔离与释放事务](production-leases.md)之后，补上机器证据不可得时由运维确认
终止的唯一持久入口。

**交付状态**：已交付（#635）。契约、迁移
`0052_quarantine_releases`、释放事务、专用非 owner 角色、启动期逐列校验与 Compose 透传
已装配；`tests/contract/test_profile_http_bindings.py` 的
`KNOWN_UNSERVED_ADMIN_OPERATIONS` 已清空并保留门禁。实现与验证证据见
`tests/integration/test_quarantine_release.py`、`tests/unit/backend/test_quarantine_release_*.py`。
启用该端点需要按[最小部署手册 §6](../operations/compose-minimal.md#6-创建非-owner-运行角色与最小授权)
预置 `sn_quarantine_release` 并设置 `SPEECHNEXUS_QUARANTINE_RELEASE_DATABASE_URL`；未配置时
端点返回 `503 quarantine_release_unavailable`。任何调用仍不得手工 `INSERT`/`UPDATE` 模拟
本页的事务；证据与账本由本入口独占写入。

四条不变量，任一不成立都不得声明该路径可用：

1. 释放证据、意图关闭与三处容量投影在**同一个 PostgreSQL 事务**提交；不存在"扣了容量
   但没释放"或"释放了但证据没落"的中间态。
2. 该事务不做任何外发调用（网关、控制器、对象存储、HTTP）。它没有分布式步骤，因此也
   没有补偿事务。
3. 一条 `admission_leases` 行最多被释放一次；释放事实与证据一经写入不再改写。
4. 只有运维独立确认旧进程/容器已不存在、且网关与节点控制器都无法产出机器可读终态证据
   时才使用本入口。机器证据由[后台释放消费者](#6-与后台释放消费者的竞争)处理。

## 1. 边界与所有权

### 1.1 适用与不适用

| 情形 | 结论 | 依据 |
| --- | --- | --- |
| 租约 `quarantined/quarantined`，其提交意图 `unknown_quarantined/quarantined`，无机器停止证据 | 本入口适用 | 契约正文 `contracts/admin-openapi.yaml:1128` |
| 租约已由后台消费者释放，请求证据与已存释放事实一致 | 只读幂等查询，200 且 `idempotent_replay: true` | 契约 `contracts/admin-openapi.yaml:1144` |
| 租约已由后台消费者用机器证据释放 | `409 quarantine_release_conflict` | §6.2 |
| 提交意图已 `aborted/complete` 且停止证据来源是网关或控制器 | 不进本入口；走既有 `release_lease` | `docs/architecture/runtime-governance.md:96` |
| 提交意图仍 `submitting`/`executing`，或租约未隔离 | 拒绝；本入口不制造停止结论 | §3.2 步骤 6 |
| 只想知道租约状态 | 用 `query_outcome(lease)` 只读查询 | `production-leases.md` |

本入口不接受调用方声明"已停止""已释放"的布尔值，也不接受调用方指定要扣减多少容量。
容量扣减量由租约行自身的 `reserved_*` 与当前投影推导，调用方只能提供证据四元组
（`evidence_kind`、`evidence_digest`、`observed_at`、`reason`）。

### 1.2 证据写侧：一个事务同时写 B 的停止事实与 A 的容量账目

契约要求释放前"该租约下每个提交意图都以完整清理证据关闭"
（`contracts/admin-openapi.yaml:1136`），而运维证据在持久面上没有写入路径：唯一的
生产者是节点控制器支持的可信停止证据，`InstanceTerminationEvidence` 只由
`execution-gateway/src/speechnexus_gateway/infrastructure/node_stop_evidence.py:316`
构造；网关域虽然接受 `operator_verified_instance_terminated`
（`execution-gateway/src/speechnexus_gateway/domain/submission.py:880`、
`execution-gateway/src/speechnexus_gateway/domain/recovery.py:366`），但没有对应的 HTTP 入口，`record_stop` 是进程内边界。

因此本入口的释放事务同时写两方事实：

- **B 侧**：向 `submit_intents` 追加该执行唯一的一条停止证据，并把意图从
  `unknown_quarantined/quarantined` 收口为 `aborted/complete`。
- **A 侧**：扣减 `workspace_quotas`、`host_nodes`（六维）、`engine_instances` 槽位与
  `admission_leases`，并写释放证据/幂等账本。

选择单事务的理由是它可以同时满足两条硬约束：契约要求的"原子释放三处"是跨表原子写，
只有同一事务能保证；而"释放了但证据没落"与"扣了容量但没释放"两个中间态在同一事务下
都不存在。代价是释放事务对 B 的两张表取得窄写权限（§8），由专用非 owner 角色和启动期
精确列校验限制在最小集合。

被判否的替代方案，以及为什么：

| 替代方案 | 失败原因 |
| --- | --- |
| 先调网关内部 HTTP 记停止证据，再单独提交 A 的释放 | 内部执行控制契约没有记录运维证据的路由（`contracts/internal/execution-control.openapi.yaml` 的 path 清单只有 submit/GET/staging/abort/barrier/drain），新增路由是契约变更，本卡不做；即使做了，两个提交之间仍是"可能多占"窗口，且需要补偿流程 |
| 只在 A 侧记证据并释放，让 B 的执行保持 `unknown_quarantined` | 直接落进[对账恢复](../operations/reconciliation-recovery.md)第 7 节判为"事实矛盾"的状态：租约已释放而执行仍未知。该状态必须升级处理，不能由本入口批量生产 |
| 由网关进程承接该 admin 路由 | 网关若要写 A 的账目必须持有 A 的锁序与扣减规则，且反向违反 `pyproject.toml:189` 的独立部署约束（`speechnexus_backend` / `speechnexus_gateway` 互不导入） |
| 给 `admission_leases` 加 `quarantine_release_*` 列（旧原型做法） | 见 §2.1 |

**B 侧规则的重复与等价性**：`pyproject.toml:189` 禁止 backend 导入 gateway，所以释放
事务在 backend 内实现一份运维证据收口规则，而不是复用
`execution-gateway/src/speechnexus_gateway/domain/submission.py:880` 的
`apply_stop_evidence`。两处规则必须逐条等价：证据归属同一 `submit_intent_id`、终态只
允许 `aborted`、清理态只允许 `complete`、观察时间不早于 `recorded_at`（已派发时还不早于
`dispatched_at`）、不晚于数据库当前时间、终态不得反转、版本不得耗尽。等价性由新增的
`tests/integration/test_quarantine_release.py` 对照用例固定：同一行输入分别喂给网关域
函数与释放事务，允许/拒绝结论必须相同。网关域函数是规则的所有者，本侧是实现副本；规则
变化时两侧同一 PR 改。

### 1.3 跨所有者并发栅栏

释放事务对意图的收口是一条带条件的 CAS 更新：
`WHERE submit_intent_id = %s AND execution_state = 'unknown_quarantined'
AND cleanup_state = 'quarantined' AND version = %s`，`rowcount` 必须为 1。网关侧的每次
执行事实写入都带 `expected_version`（`execution-gateway/src/speechnexus_gateway/application/submit_execution.py:195`），
因此两侧对同一意图的写最多一方成功：

- 网关先提交终态 → 本事务的 CAS 失败，整笔回滚，返回冲突。
- 本事务先提交 → 网关后续写入版本冲突，拒绝采用迟到的引擎结果。

后半条是有意的语义：运维已独立确认旧实例不存在，之后到达的引擎成功回执**被丢弃，
不改写已提交的 `aborted`**。这正是契约"证据与理由随释放持久化，永不重写"在竞态下的
含义，也与[对账恢复](../operations/reconciliation-recovery.md)第 5.1 节"旧结果不得改写
新上下文"一致。

## 2. 持久面

### 2.1 为什么不把释放证据加列到 `admission_leases`

隔离实测（PostgreSQL 16.15，迁移到 `0051_binding_publication_events`）：
`admission_leases` 有 `quarantine_observed_at_us`、`quarantine_reason_code`、
`budget_released_at_us`，没有任何 `quarantine_release_*` 列，全库
`column_name LIKE '%quarantine_release%'` 为 0 行。旧原型
（`tests/fixtures/coordination/legacy_postgresql.sql:642`）把证据四元组放在该表上，本设计
不沿用：

1. **幂等账本无处安放**。判重需要"键摘要 + 规范证据摘要"两个值，且键可能缺省；把三列
   以上塞进租约行会污染一条被所有释放/隔离判定读取的热路径行。
2. **释放是一个事件，不是租约的一个属性**。租约行的 `version` 已被准入、隔离、释放、
   接管反复 CAS，加列等于要求每一处 `SELECT *` 的读取者容忍新语义。
3. **不可变性边界不同**。租约行必须可变（状态推进），释放证据必须不可变；两者的
   `GRANT UPDATE` 与触发器策略无法在同一张表上同时表达。
4. **同一行上的列级授权无法区分两种写者。** 租约行的 `UPDATE` 是按列授予的，把证据列
   加在同一行就等于让每个持有租约 `UPDATE` 的角色（准入、隔离、释放、接管）都能写证据；
   独立表可以把证据的 `INSERT` 精确限定给释放角色，把租约表限制为 4 个可变列。

### 2.2 DDL 草案

迁移编号接在 `0051` 之后（实现卡决定具体号），对象归 `speech` 命名空间、A 的 schema
模块管理（与 `backend/src/speechnexus_backend/speech/infrastructure/gateway_abort_intent_schema.py`
同类：backend 迁移链可以创建跨所有者写入的表）。

```sql
CREATE TABLE admission_quarantine_releases (
    lease_id VARCHAR(128) COLLATE "C" PRIMARY KEY
        REFERENCES admission_leases (lease_id),
    operation_id VARCHAR(128) COLLATE "C" NOT NULL,
    attempt_id VARCHAR(128) COLLATE "C" NOT NULL,
    stage_id VARCHAR(128) COLLATE "C" NOT NULL,
    host_node_id VARCHAR(128) COLLATE "C" NOT NULL,
    instance_id VARCHAR(128) COLLATE "C" NOT NULL,
    instance_generation BIGINT NOT NULL CHECK (instance_generation >= 1),
    fencing_token BIGINT NOT NULL CHECK (fencing_token >= 1),
    idempotency_key_digest VARCHAR(71) COLLATE "C"
        CHECK (idempotency_key_digest ~ '^sha256:[0-9a-f]{64}$'),
    request_digest VARCHAR(71) COLLATE "C" NOT NULL
        CHECK (request_digest ~ '^sha256:[0-9a-f]{64}$'),
    evidence_kind VARCHAR(64) COLLATE "C" NOT NULL
        CHECK (evidence_kind = 'operator_verified_instance_terminated'),
    evidence_digest VARCHAR(71) COLLATE "C" NOT NULL
        CHECK (evidence_digest ~ '^sha256:[0-9a-f]{64}$'),
    evidence_observed_at_us BIGINT NOT NULL CHECK (evidence_observed_at_us >= 0),
    reason VARCHAR(2000) COLLATE "C" NOT NULL
        CHECK (char_length(btrim(reason)) BETWEEN 1 AND 2000),
    budget_released_at_us BIGINT NOT NULL CHECK (budget_released_at_us >= 0),
    released_submit_intent_ids TEXT NOT NULL,
    CHECK (evidence_observed_at_us <= budget_released_at_us),
    CHECK (jsonb_typeof(released_submit_intent_ids::jsonb) = 'array'),
    FOREIGN KEY (lease_id, operation_id, attempt_id)
        REFERENCES admission_leases (lease_id, operation_id, attempt_id)
);

CREATE OR REPLACE FUNCTION admission_quarantine_releases_reject_mutation()
    RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      RAISE EXCEPTION 'quarantine release evidence is append-only'
        USING ERRCODE = '23514';
    END
    $$;

CREATE TRIGGER admission_quarantine_releases_append_only
    BEFORE UPDATE OR DELETE ON admission_quarantine_releases
    FOR EACH ROW EXECUTE FUNCTION admission_quarantine_releases_reject_mutation();
```

设计要点：

| 项 | 决策 | 理由 |
| --- | --- | --- |
| 主键 `lease_id` | 一租约一行 | 释放是租约的唯一事件，PK 同时是幂等第二条防线 |
| `idempotency_key_digest` 可空 | 缺省表示未提供 `Idempotency-Key` | 契约的 `IdempotencyKey` 参数 `required: false`（`contracts/admin-openapi.yaml:1272`）；缺省时不伪造随机键，否则该列的比较语义失真 |
| 无键的全局 `UNIQUE (idempotency_key_digest)` | 不建 | 契约的幂等范围是"部署 + 租约"；同一键用在另一个租约上不属于冲突 |
| `evidence_kind` 常量 CHECK | 只允许 `operator_verified_instance_terminated` | 机器证据的种类由网关写，本表只承载运维证据 |
| `evidence_observed_at_us <= budget_released_at_us` | 表级 CHECK | 契约明确 `observed_at` 不得晚于服务端 `budget_released_at` |
| `released_submit_intent_ids` 存规范 JSON 数组 | 文本 + `jsonb_typeof` CHECK | 沿用 `admission_attempts.required_stage_ids` 的既有形态；§5 说明恒为 0/1 元素 |
| 索引 | 只保留主键 | 唯一查询键是 `lease_id`；本表不做时间范围扫描 |
| 触发器 | `BEFORE UPDATE OR DELETE` 拒绝 | "永不重写"是数据库级保证，不能只靠应用；沿用 `0034_public_job_cancellations` 的 `ERRCODE = '23514'` 惯例 |

**不新增跨表触发器**。`submit_intents` 的状态机仍由应用事务负责，这与[协调事务](../architecture/coordination-transactions.md)第 8 节"旧数据库函数迁入应用事务"的方向一致：
数据库提供原子性、基础约束与最小权限，跨记录业务规则由受控事务执行。运维证据写入的
最终防线是 §1.3 的 CAS 与列级授权，不是新触发器。

**校验结论**（隔离 PostgreSQL 16.15，全量迁移到 `0051` 后在同一实例验证，随后回滚）：

| 用例 | 结果 |
| --- | --- |
| 上述 DDL 原样执行（含两个 FK 目标约束） | 成功 |
| 合法行写入 | 成功 |
| `reason = '  '` | 违反 `reason_check` |
| `evidence_kind = 'engine_terminal_response'` | 违反 `evidence_kind_check` |
| `observed_at_us > budget_released_at_us` | 违反表级 CHECK |
| `UPDATE ... SET reason = ...` | 触发器拒绝 |
| `DELETE` | 触发器拒绝 |

### 2.3 复用的既有对象

释放事务额外写 B 的 `execution_stop_evidence`，不改该表 DDL：
`execution_stop_evidence.evidence_kind` 是 `VARCHAR(64)`、没有值域 CHECK，
`reason`、`execution_stopped`、`resources_released` 列已存在
（`backend/src/speechnexus_backend/speech/infrastructure/coordination_schema.py:301`）。
运维证据行与机器证据行共用同一形状，因此既有读取方无需改动：

- A 的释放判定已经按 `evidence_kind ∈ {instance_terminated_by_controller,
  operator_verified_instance_terminated}` 接受隔离租约
  （`backend/src/speechnexus_backend/speech/domain/lease_release.py:149`），不需要新分支。
- `execution_stop_evidence_submit_intent_id_key` 唯一约束
  （`backend/src/speechnexus_backend/speech/infrastructure/coordination_schema.py:303`）保证"同一执行只有一条不可变证据"；运维证据与机器证据
  不能并存，因此不存在两套事实互相覆盖。
- 只读排查（`docs/operations/reconciliation-recovery.md` 查询 3）继续可用。

证据行主键按网关同一惯例确定性派生，而不是随机生成：
`evidence_id = 'stop-operator-' || substr(sha256(canonical_json(["operator-stop/v1",
submit_intent_id])), 1, 48)`。确定性 id 使同一执行的重跑不会产生第二条证据行，也让重放
在 PK 或唯一约束上暴露而不是静默插入。

## 3. 原子释放事务

### 3.1 锁序

释放事务沿用[协调事务](../architecture/coordination-transactions.md)第 4 节的全局锁序，
不引入新的顺序；与既有 `release_in_transaction`
（`backend/src/speechnexus_backend/speech/infrastructure/lease_release_sql.py:30`）完全一致：

```text
resource_pools
→ workspace_quotas
→ host_nodes
→ engine_instances
→ admission_attempts
→ admission_leases
→ submit_intents
→ execution_stop_evidence（INSERT 时 FK 父行已持有，不取该表的 FOR UPDATE）
→ admission_quarantine_releases（新表 INSERT）
```

约束：

- 定位父键的读取在取锁前执行，**取锁后必须重核对**身份、状态、版本（同一事务内的
  `hint` 与 `FOR UPDATE` 结果逐字段比较，不一致即 `reconciliation_required`）。
- 同一表的参与键按字典序排序；一次只释放一条租约，不得为"顺手处理"去锁兄弟租约。
- `execution_stop_evidence` 只 INSERT，不加 `FOR UPDATE`：既有释放路径同样只读它
  （`backend/src/speechnexus_backend/speech/infrastructure/lease_release_sql.py:131`），写侧靠唯一约束而非行锁防重。
- 新账本行不参与锁序竞争：租约行锁已经是同一租约的串行化点，账本 PK 是第二道防线。
- 隔离级别为 Read Committed，事务短小；不带 `SERIALIZABLE`、不做跨表延迟约束。
- 每条语句带 `statement_timeout`，整体请求预算与既有租约事务一致（默认 3 s、最多 3 次
  事务尝试，仅对数据库已确认回滚的 deadlock/serialization 重试）。重试带短抖动，不越
  请求 deadline。

### 3.2 事务步骤

| 步 | 动作 | 失败即整笔回滚 |
| --- | --- | --- |
| 0 | 事务外：解析请求、按 RFC 8785 计算规范证据摘要与键摘要；无数据库访问 | 400 |
| 1 | `READ ONLY` 事务读账本行；命中 B/C/D/E 直接返回，不取业务锁（§4.4） | 200 / 409 |
| 2 | 无锁读取租约、attempt、该租约的提交意图，定位父键并判存在 | 404 |
| 3 | 按 §3.1 顺序取全部 `FOR UPDATE` | 冲突/重试 |
| 4 | 锁后重核对冻结字段（`lease_id`/`operation_id`/`attempt_id`/`stage_id`/`pool_id`/`host_node_id`/`instance_id`/`instance_generation`/`gateway_id`/`fencing_token`/`reserved_*`）与 `attempt.workspace_id` | `reconciliation_required` |
| 5 | 锁后**普通 `SELECT`**（不取行锁）重读账本行，首次为空；重做 §4.2 判定，命中 B/C/D/E 则本事务只返回、不写入 | 200 / 409 |
| 6 | 状态前提：租约 `quarantined/quarantined`、`quarantine_observed_at_us` 非空、提交意图恰为 1 条且 `unknown_quarantined/quarantined` | 409 `quarantine_release_conflict` |
| 7 | 取数据库时间 `now_us`（最后一次可能等待之后）；校验 `recorded_at_us (或 dispatched_at_us) <= observed_at_us <= now_us` | 400 |
| 8 | INSERT `execution_stop_evidence` 运维证据行（§2.3） | `reconciliation_required` |
| 9 | CAS 收口提交意图：`execution_state='aborted'`、`cleanup_state='complete'`、`terminated_at_us=observed_at_us`、`updated_at_us=now_us`、`version=version+1`，`rowcount` 必须为 1 | `reconciliation_required` |
| 10 | 用既有纯函数 `release()` 计算投影与租约新状态（此时 `execution=aborted/complete`、`stop.kind=operator_*`，隔离分支放行） | `execution_cleanup_pending` |
| 11 | 按 version CAS 依次更新 `workspace_quotas`→`host_nodes`→`engine_instances`→`admission_attempts`→`admission_leases`，每条 `rowcount` 必须为 1 | `reconciliation_required` |
| 12 | INSERT 账本行（证据四元组 + 两个摘要 + `budget_released_at_us=now_us` + 意图 ID 列表） | 唯一冲突 |
| 13 | COMMIT | 见 §3.3 |

步骤 10 复用 `backend/src/speechnexus_backend/speech/domain/lease_release.py:105` 的
`release()`，因此"哪些容量允许归还、最后 stage 才结算 attempt quota、host 不自动恢复
active"这些规则只有一份实现。本事务新增的只有步骤 6–9、12。

**步骤 5 必须是普通 `SELECT`，不能加 `FOR UPDATE`。** PostgreSQL 的
`SELECT ... FOR UPDATE` 要求对目标表至少一列持有 `UPDATE` 权限，而 §8.1 给账本表的授权
只有 `SELECT + INSERT`——这正是 append-only 的授权前提。要 `FOR UPDATE` 就必须授予账本表
的 `UPDATE`，那会把第 3 条不变量（"释放事实与证据一经写入不再改写"）从数据库保证降级成
应用承诺，与 §2.2 的触发器设计直接冲突。两者的取舍没有中间态：最小授权 + 普通 `SELECT`
是自洽解，`FOR UPDATE` + `UPDATE` 授权是自毁解。

不加行锁仍能保证正确性，因为它依赖三条已成立的事实：

1. **账本只有本事务一个写入者，且写入排在租约行锁之后**（步骤 12）。同租约的两个写事务
   必然在锁序第 6 位的 `admission_leases` 行锁处串行化，后者在 READ COMMITTED 下逐语句取
   新快照，因此步骤 5 的重读一定看得见前者已提交的行——不存在"两个写者都没读到账本就各写
   一行"的窗口。
2. **账本行提交后不可改写**（§2.2 的 append-only 触发器），所以步骤 5 读到的行就是终态
   事实，不会在两次读取之间发生语义漂移。
3. **`lease_id` 是主键**：绕过应用直写的并发插入会拿到唯一冲突（适配器映射为 409），因此
   即便上述两条同时失效，也不会产生第二条释放记录。

此外账本行对 `admission_leases` 的复合外键 `(lease_id, operation_id, attempt_id)` 在
`INSERT` 时对父行取 `FOR KEY SHARE`，与租约上的 `FOR UPDATE` 互斥，所以**即使绕过应用
直接写账本，也必须排在租约行锁之后**——这比"主键是第二道防线"更强一层。删除第 2 级重读
本身是可观测的：并发同键不同证据的落败方会从 `idempotency_conflict` 退化成
`quarantine_release_conflict`（见 §9.3 反例表）。

### 3.3 失败边界与中间态

| 失败点 | 已提交事实 | 中间态 | 处理 |
| --- | --- | --- | --- |
| 步骤 2/4/6/7 拒绝 | 无 | 租约与执行均保持 `quarantined`，容量仍隔离 | 返回对应错误，不入账 |
| 步骤 8–12 任一失败 | 无（同事务回滚） | 同左：证据、意图状态、三处投影、账本全部回到事务前 | 适配器按已确认回滚分类；客户端可用同键重试 |
| COMMIT 前连接断开 | 无 | 同上 | 适配器返回 unknown，客户端查原租约或重试同键 |
| COMMIT 已发送但确认丢失 | 可能已全部提交 | 全有或全无，不存在部分提交 | 返回 503 + `quarantine_release_unknown`；同键重试必命中账本（§4） |
| 提交后发现账本行缺失 | 逻辑上不可能（同事务） | — | 由 §9 的"账本行与租约一致性"用例守护 |

三条具体保证：

- **"扣了容量但没释放"不存在**：所有 `UPDATE` 与证据、意图收口同事务；任一步 `rowcount
  不等于 1` 直接抛错回滚，不留下已扣减的计数。
- **"释放了但证据没落"不存在**：证据 INSERT 在投影更新之前，且同事务；提交顺序不改变
  可见性。
- **"重复扣减"不存在**：租约行锁串行化同租约的两次请求；第二次进入步骤 5 时要么命中
  账本重放、要么读到 `lease_state='released'` 后按 §4.2 判定，两条路径都不再执行
  步骤 8–12。

## 4. 幂等与判定

### 4.1 规范摘要

两个摘要都用仓库既有的规范化与摘要惯例（RFC 8785 规范 JSON +
`sha256:` 前缀，见 `execution-gateway/src/speechnexus_gateway/domain/canonical.py:45`）：

```text
键摘要      idempotency_key_digest = "sha256:" + sha256(Idempotency-Key 原始字节)
            仅当请求带该头时存在；长度 8–128 字节，超出即 400

规范证据摘要 request_digest = "sha256:" + sha256(canonical_json([
                "quarantine-release-evidence/v1",
                {
                  "lease_id":                 <路径参数>,
                  "evidence_kind":             <请求体>,
                  "evidence_digest":            <请求体，原样小写>,
                  "observed_at_us":             <请求体 RFC3339 转 UTC 微秒整数>,
                  "reason":                     <请求体，原样（不做 trim）>
                }
            ]))
```

`request_digest` 覆盖四个字段的全部内容，因此"同键不同理由"与"同键不同证据"落入同一
判据。`evidence_digest` 本身是运维核验材料（例如容器终止记录）的摘要，服务端不重算、
不解释其内容，只做等值比较与格式校验（`^sha256:[0-9a-f]{64}$`）。

`observed_at` 的服务端表示是 UTC 微秒整数，与 `admission_leases.*_us`、
`execution_stop_evidence.observed_at_us` 一致；响应回写时再还原成微秒精度的 RFC3339
UTC，不做本地时区换算。

### 4.2 判定表

`key_eq` 表示请求带了键且与账本行的 `idempotency_key_digest` 相等；`ev_eq` 表示
`request_digest` 与账本行的 `request_digest` 相等。判定按 §4.4 的两级读取执行：账本行
已在库里时全部结论都只依赖这条不可变行，无需取业务锁；账本行为空时才进入 §3.2 的
加锁路径，并在锁后重做同一次判定。

| 编号 | 状态 | 结论 |
| --- | --- | --- |
| A | 租约不存在 | `404 quarantine_release_not_found` |
| B | 账本行存在，`key_eq ∧ ev_eq` | `200`，`idempotent_replay: true`，回显账本的 `budget_released_at`、意图列表、证据四元组 |
| C | 账本行存在，`key_eq ∧ ¬ev_eq` | `409 idempotency_conflict`，零写入 |
| D | 账本行存在，未带键或 `¬key_eq`，`ev_eq` | `200`，`idempotent_replay: true`，零写入（只读重放） |
| E | 账本行存在，`¬key_eq ∧ ¬ev_eq` | `409 quarantine_release_conflict`，零写入 |
| F | 无账本行，`lease_state='released'` | 若请求证据与**已存释放事实**一致则 `200` 只读重放，否则 `409 quarantine_release_conflict`（§6.2） |
| G | 无账本行，租约未释放 | 走 §3.2 的写入路径（成功即 `200`，`idempotent_replay: false`） |

注意 B 与 D 的区别只在是否能证明"同一个键"；两者都不重新释放、不重写证据。D 之所以
不写一条新账本行：它是对既有释放事实的查询，写账本会制造"第二个键也拥有这次释放"的
伪事实，并让后续重放多一条无意义的状态。

### 4.3 不带 `Idempotency-Key` 的请求

契约把该头标为可选（`contracts/admin-openapi.yaml:1272`），因此缺省不得当作 400。缺省
时账本行的 `idempotency_key_digest` 为 `NULL`，判定退化到只看 `ev_eq`：

- 首次无键请求提交后，再次无键同证据请求落入 D，返回同一 `budget_released_at`。
- 无键请求与带键请求可以互相重放（都不构成"同键不同证据"），因为它们对应的都是同一次
  释放。
- 一旦账本行由带键请求写入，后续无键请求不会覆盖或清空该键摘要。

### 4.4 两级读取

| 级 | 事务形态 | 判定范围 | 说明 |
| --- | --- | --- | --- |
| 1 | `READ ONLY`，不取业务锁 | B/C/D/E | 只读账本行。账本行一经提交不再改写（§2.2 触发器），因此读到的行就是终态事实；读不到（并发事务尚未提交）不算结论，落到第 2 级 |
| 2 | 读写事务，按 §3.1 锁序取锁 | A/F/G，以及并发下新出现的 B/C/D/E | 锁后重读账本行再判定；空行才执行写入路径 |

第 1 级不是优化细节而是必要边界：若重放也必须遍历整条锁链，一个运维脚本的重复提交会
在每次轮询里与准入争抢 pool/host 行锁。第 2 级仍然先读账本再决定是否写入，因此
"重放不产生写入"在两级上都成立。

反过来，不能把第 1 级做成"只锁租约行"的捷径：租约是全局锁序的第 6 位，只锁它会与准入
的 pool→…→lease 顺序反转，制造死锁。要么不取锁，要么按完整顺序取锁。

## 5. 意图关闭完整性：契约收窄，模型不动

`submit_intents.lease_id` 上是 `NOT NULL UNIQUE`
（`backend/src/speechnexus_backend/speech/infrastructure/coordination_schema.py:247`，
实测约束名 `submit_intents_lease_id_key`），因此契约要求的"按字典序列出该租约下所有已
关闭的提交意图"**最多只能返回 1 条**。这是数据模型与契约语义的直接冲突。

**裁决：改契约，不改模型。**

理由：

1. **"一租约一次提交"是容量账目的前提。** 租约是某个 stage 的一份预留
   （`UNIQUE (operation_id, attempt_id, stage_id)` 加 `reserved_slots`/`reserved_*`）。
   允许一条租约承载两次提交就等于允许同一份预留被两次外发，容量守恒在准入阶段就失效。
2. **既有事务全部依赖 0..1。** 隔离与释放都在读提交意图后断言"至多一条，否则
   `reconciliation_required`"（`backend/src/speechnexus_backend/speech/infrastructure/leases_sql.py:149`、
   `backend/src/speechnexus_backend/speech/infrastructure/lease_release_sql.py:105`）；派发侧遇到多 stage 直接放弃单 stage 释放
   （`backend/src/speechnexus_backend/speech/infrastructure/job_dispatch_postgres.py:530`）。
   放开唯一约束不是加一条能力，而是要求这些路径改成 N 条证据的循环，风险与收益不成比例。
3. **重试语义不需要它。** 新尝试走新 attempt + 新租约 + 新提交意图，这正是
   `admission_leases UNIQUE (operation_id, attempt_id, stage_id)` 与网关
   `create_submit` 的唯一身份所表达的。
4. **响应数组的 0..1 与契约自洽。** `released_submit_intent_ids` 声明
   `uniqueItems: true` 且未声明 `maxItems`；元素为 0 或 1 个仍是合法实例，因此实现
   不需要先改契约即可承接。问题只是"看起来能有多条"。

影响面：只有响应数组（`contracts/admin-openapi.yaml:2029`）与契约正文的一句措辞。没有
任何消费者按 2 条以上分支处理；测试与前端不需要改动。

建议的契约收窄（单独 issue，不在 #635 内实施）：把
`released_submit_intent_ids` 加 `maxItems: 1`，并在正文写明"当前每个租约最多一条已关闭
的提交意图"。更彻底的做法是改成单值字段 `released_submit_intent_id`（可空），但那会
破坏已生成的消费者类型，收益不足以抵消改动面，因此只作为后续选项保留。

回退方式：如果将来确实需要一条租约承载多次提交（例如同 stage 分片），回退顺序是
① 新增 `(lease_id)` 上的可空外键或多对一关系替代唯一约束，② 把 `release()` 的隔离分支
改成遍历全部意图、要求每条都有完整证据，③ 才放宽响应数组。持久面已为此留了余地：
账本的 `released_submit_intent_ids` 本来就是 JSON 数组，不需要改列。

## 6. 与后台释放消费者的竞争

### 6.1 两种先后顺序

"后台释放消费者"指按机器证据结算既有租约的调用方：TTS 结算 worker
（`backend/src/speechnexus_backend/speech/application/tts_settlement.py:88` 的
`_reconcile`）与派发结算器
（`backend/src/speechnexus_backend/speech/infrastructure/job_dispatch_postgres.py:507`）。它们
周期性重读网关执行事实，在 `aborted/complete` 时调用同一条 `release_lease` 事务。

| 顺序 | 可见状态与结论 |
| --- | --- |
| ① 运维释放先提交，消费者后扫描 | 消费者读到 `lease_state='released'`，`release_already_recorded` 走只读重放（`backend/src/speechnexus_backend/speech/domain/lease_release.py:79`），返回原事实；不重复扣减，不重写证据。消费者侧 `mark_settled` 幂等。 |
| ② 消费者先释放（凭网关/控制器机器证据），运维请求后到 | 运维事务在步骤 5 命中分支 F：请求证据与已存释放事实不一致（证据种类是机器种类、`reason` 为空），返回 `409 quarantine_release_conflict`，零写入。 |
| ③ 两者并发 | 租约行锁串行化；先取到锁的一方提交，另一方按 ①/② 处理。不会出现两方各扣一半。 |

顺序 ② 的 409 不是"操作失败"，而是正确结论：系统已经用机器证据完成了释放，运维证据
对这条租约不再适用。响应 schema 也从另一侧排除了"把机器释放回报成运维释放"的可能：
`QuarantineRelease.evidence_kind` 是常量 `operator_verified_instance_terminated`
（`contracts/admin-openapi.yaml:2014`），若按 200 返回一条机器证据的释放，服务端只能
伪造证据种类。

### 6.2 证据匹配口径

分支 F 的比较对象是**已存释放事实**，逐字段比较：

| 比较项 | 运维释放（账本行） | 机器释放（网关事实） |
| --- | --- | --- |
| `evidence_kind` | 账本 `evidence_kind` | `execution_stop_evidence.evidence_kind` |
| `evidence_digest` | 账本 `evidence_digest` | `execution_stop_evidence.evidence_digest` |
| `evidence_observed_at_us` | 账本 `evidence_observed_at_us` | `execution_stop_evidence.observed_at_us` |
| `reason` | 账本 `reason`（非空） | `execution_stop_evidence.reason`（机器种类为空） |

四项全部相等才算匹配。由于运维证据行只能由本入口写入，而本入口的证据与释放同事务提交，
"租约已释放且没有账本行"只可能来自机器释放，此时 `evidence_kind` 与 `reason` 必然不同，
结论恒为 409。匹配分支仍然实现并测试：它是对"证据与释放被拆成两次提交"这一未来设计的
正确兜底，而不是当前的死代码。

规范摘要的口径见 §4.1：比较用规范证据摘要做整体等值，逐字段表只用于说明摘要覆盖了
哪些字段和失败时该报哪个冲突码。

### 6.3 `observed_at <= budget_released_at` 在两种顺序下都成立

- **运维释放**：`budget_released_at_us` 就是步骤 7 取的 `now_us`，且写入前已校验
  `observed_at_us <= now_us`，因此 `observed_at_us <= budget_released_at_us` 由构造
  保证；账本的 CHECK 再兜一次。回放时返回账本里的原值，不会用当前时间覆盖。
- **机器释放**：既有 `release()` 同样要求 `stop.observed_at_us <= now_us`
  （`backend/src/speechnexus_backend/speech/domain/lease_release.py:139`），`budget_released_at_us` 也取锁后的数据库时间，因此该不变量
  与既有释放路径一致，不因本入口改变。
- **混合**：分支 F 的只读重放不写任何 `budget_released_at`；若请求的 `observed_at` 晚于
  已存 `budget_released_at_us`，则证据不可能相等，落入 409，不会产生违反不变量的数据。
- 服务端时钟是判据：`observed_at` 来自运维，但"不得晚于服务端释放时间"由数据库
  `clock_timestamp()` 裁决；客户端时钟、事务开始前的旧时间都不作依据。

## 7. 未知结果与 HTTP 映射

### 7.1 提交后断连与响应丢失

事务提交后客户端断开、代理超时或响应被丢弃时，服务端**已经**持久化了释放。客户端按同一
`Idempotency-Key` 与原请求体重试：

- 命中分支 B：`200`、`idempotent_replay: true`、`budget_released_at` 与意图列表与首次
  完全相同（来自账本，不是重新计算）。
- 若客户端换了键但保留了原证据：命中分支 D，同样是 `200` + 原事实，零写入。
- 若客户端改了证据或理由：命中分支 C（同键）或 E（换键）→ 409，零写入。已提交的释放
  保持不变，409 不带任何"回滚"含义。

服务端在本事务内不产生"部分成功"：连接断开只影响本次响应的投递，不影响数据库的
全有或全无。适配器的 unknown 分类沿用既有规则——提交确认不明返回 unknown、只在数据库
已确认回滚时重试（`backend/src/speechnexus_backend/speech/infrastructure/leases_postgres.py:124`）。

### 7.2 状态码与错误码

`ErrorResponse.error.code` 只受 `^[a-z][a-z0-9_]{2,63}$` 约束
（`contracts/shared/errors.schema.json`），因此下列码不需要改契约即可承接；契约正文
点名的两个码（`idempotency_conflict`、`quarantine_release_conflict`）与其它 admin
endpoint 的用法一致。

| HTTP | `code` | 触发条件 | `retryable` |
| --- | --- | --- | --- |
| 200 | — | 分支 B/D/F 匹配或 G 成功 | — |
| 400 | `quarantine_release_invalid` | 证据四元组形状不合法（含 `evidence_kind` 不是常量）、`reason` 空白或超长、`observed_at` 早于 `recorded_at`/`dispatched_at` 或晚于服务端当前时间 | `false` |
| 400 | `idempotency_key_invalid` | `Idempotency-Key` 存在但长度不在 8–128 | `false` |
| 401 | 既有共享凭据错误 | 未带或错凭据 | `false` |
| 404 | `quarantine_release_not_found` | 分支 A | `false` |
| 409 | `idempotency_conflict` | 分支 C | `false` |
| 409 | `quarantine_release_conflict` | 分支 E/F：证据不匹配、租约未隔离、意图不在 `unknown_quarantined/quarantined`、提交意图数不是 1 | `false` |
| 503 | `quarantine_release_unknown` | 事务结果不明（提交确认丢失、连接中断、语句被取消） | `false`（先查，不盲重） |
| 503 | `quarantine_release_unavailable` | 连接池/存储不可用，事务未开始 | `true` |
| 503 | `quarantine_release_schema_not_migrated` | 目标关系或列不存在（库未迁移到 `0052`，或 `SPEECHNEXUS_DATABASE_SCHEMA` 指错） | `false` |
| 503 | `quarantine_release_privilege_missing` | 运行期授权被回收：`insufficient_privilege`（42501） | `false` |
| 503 | `quarantine_release_failed` | 其它未声明的存储拒绝；事务已确认回滚，未释放任何容量 | `false` |

409 一律 `retryable: false`：同请求重试不会改变结论；客户端必须换证据、换租约或转人工
协调。`observed_at` 落在未来属于 400 而非 409——时间参数是请求内容，服务器状态变化永远
不会让它变合法。

后三个 503 码是"部署缺陷不是业务冲突"的显式分类。表缺失、列缺失与授权被回收在启动期
校验之后仍可能发生（运行期被 `REVOKE`、表被 `DROP ... CASCADE`），把它们映射成
`409 quarantine_release_conflict` 会让运维去换证据或升级租约，而真正要修的是迁移或授权；
这也与"把实现违约包装成可重试 503"是同一个错误的反方向。只有真实的状态不匹配
（证据不符、租约未隔离、意图状态不符、同键不同证据）才返回 409。除
`quarantine_release_unavailable` 外所有码都 `retryable: false`，包括后三个：重试同一个
请求不会修好缺失的表或权限。

**预算耗尽的两种终局。** 语句内超时（`statement_timeout` 触发的 `QueryCanceled`）属于
`OperationalError` 家族，写事务里返回 `quarantine_release_unknown`、只读第 1 级返回
`quarantine_release_unavailable`；语句之间的预算检查（`_BudgetExpiredError`）在两级都返回
`quarantine_release_unavailable`。这两条分支的数据库状态相同（事务未开始或已确认回滚），
差异只在"能否排除 COMMIT 语句本身被取消"，因此 `retryable` 只是提示而不是承诺；客户端
在两种情况下都按同键查询或重试，不会重复释放。

**契约出口**：该 operation 的 responses 声明了 `503
$ref: "#/components/responses/ServiceUnavailable"`（`contracts/admin-openapi.yaml`，
与服务端 5xx 出口一一对应），因此上表全部 503 码都在已声明状态内；`error.code` 由
`contracts/shared/errors.schema.json` 的 `^[a-z][a-z0-9_]{2,63}$` 约束，新增码不需要再改
契约。

## 8. 权限面

### 8.1 写角色最小集

释放事务跑在**专用非 owner 角色**上，与既有独立 worker 的形态一致：独立 DSN
（形如 `SPEECHNEXUS_QUARANTINE_RELEASE_DATABASE_URL`）、独立连接池、启动期校验，不复用
API 进程的运行时角色。原因见 §1.2：它必须能写 B 的两张表，而 API 运行时角色不应持有
该权限。

`GRANT USAGE ON SCHEMA`（不授 `CREATE`）之外，列级最小集：

| 表 | SELECT | INSERT | UPDATE |
| --- | --- | --- | --- |
| `resource_pools` | `(pool_id)` | — | `(version)`（仅为取行锁） |
| `workspace_quotas` | 全表 | — | `(held_attempts, quarantined_attempts, version)` |
| `host_nodes` | 全表 | — | `(held_*, quarantined_* 六维, version)` |
| `engine_instances` | 全表 | — | `(held_slots, quarantined_slots, version)` |
| `admission_attempts` | 全表 | — | `(quota_state, version)` |
| `admission_leases` | 全表 | — | `(lease_state, cleanup_state, budget_released_at_us, version)` |
| `submit_intents` | `(submit_intent_id, lease_id, operation_id, attempt_id, execution_state, cleanup_state, recorded_at_us, dispatched_at_us, terminated_at_us, version)` | — | `(execution_state, cleanup_state, terminated_at_us, updated_at_us, version)` |
| `execution_stop_evidence` | 全表 | 全表 | — |
| `admission_quarantine_releases` | 全表 | 全表 | — |

明确**不授予**，并由启动期校验强制：

- `admission_leases` 不授 `INSERT`/`DELETE`；`UPDATE` 只有
  `lease_state`、`cleanup_state`、`budget_released_at_us`、`version` 四列。不授
  `quarantine_observed_at_us`、`quarantine_reason_code`、`reserved_*`、`fencing_token`、
  `deadline_at_us`：隔离观察与原因是隔离事务的事实，预留向量冻结，本入口都不改写。
- `submit_intents` 不授 `request_document`、`engine_request_id`、`staging_*`、
  `generated_pcm_samples`、`command_*` 的读；不授 `DELETE`/`TRUNCATE`。私有命令正文与
  引擎身份不属于释放判定所需。
- 不授 `admission_operations`、`admission_requests`（A 的私有命令存储）。
- 不授任何控制面配置列的写权：`host_nodes.node_state`、
  `host_nodes.quarantine_threshold_ppm`、`host_nodes.allocatable_*`、
  `workspace_quotas.max_attempts`、`engine_instances.max_slots`。降低隔离阈值、放量或
  自动恢复 host 接单属于控制面，释放事务不得自证放宽。
- 不授任一表的 `DELETE`/`TRUNCATE`/`TRIGGER`/`REFERENCES`，不授 schema `CREATE`。
- 不授 `execution_release_guards` 视图：`failed/not_required` 零外发分支在本事务里不可
  达（步骤 6 要求 `unknown_quarantined`），授予它只会扩大读取面。缺该视图不影响本入口，
  这也是与既有 `release_lease` 权限面的**有意差异**。

### 8.2 启动期校验"不多给"

启动校验沿用既定形态（`backend/src/speechnexus_backend/tts_settlement_worker.py:89` 的
逐列枚举 + 精确比对，`backend/src/speechnexus_backend/binding_disable_worker.py:200` 的
身份约束），在建立连接池之后、注册路由之前执行一次，失败即终止进程：

1. `server_version_num >= 160000`；`current_user` 非 owner（`pg_has_role(... 'MEMBER')`
   为假）；schema 上无 `CREATE`。
2. 拒绝任何可切换/管理/预定义身份：不存在 `pg_has_role(SESSION_USER, other, 'SET')`、
   不存在 `MEMBER WITH ADMIN OPTION`、`rolsuper/createrole/createdb/replication/bypassrls`
   全假、不以 `USAGE` 继承任何 `pg_` 前缀角色。
3. **逐列精确等式**：枚举 schema 内的 `relkind IN ('r','p','v','m')` 关系与列，对每列取
   `has_column_privilege(..., 'SELECT'/'INSERT'/'UPDATE')`，与该角色的声明白名单逐项
   比较，`rd == allowed_read`、`ins == allowed_insert`、`upd == allowed_update` 必须同时
   成立；`ref` 必须为假；表级 `has_table_privilege(..., 'DELETE,TRUNCATE,TRIGGER')`
   必须为假。任一列"多给"或"少给"都终止启动——这正是"多给一列必须红"的守卫来源。
4. **序列单独判定**：序列（`relkind='S'`）不在上面的枚举里，也没有列级 ACL，必须用
   `has_sequence_privilege` 单独查：schema 内任何序列上的 `USAGE`/`SELECT`/`UPDATE` 都
   终止启动（本事务不碰任何序列，声明集为空）。不能用 `has_table_privilege` 代替——序列
   与表的权限字母含义不同，会误读同样的 ACL 位。
5. 关键负向断言：`submit_intents.request_document` 与 `admission_leases.reserved_slots`
   的 `SELECT` 必须为假，`execution_stop_evidence` 的 `UPDATE` 必须为假。
6. 校验不可变保证真实存在：`pg_trigger` 中存在
   `admission_quarantine_releases_append_only` 且已启用。缺触发器说明迁移没装齐，
   此时"永不重写"只是文档承诺。

**校验边界**：覆盖范围是**配置 schema 内的表、分区表、视图、物化视图、外表与序列**。
其他 schema（含 `public`）、数据库级权限、函数 `EXECUTE` 与 `ALTER DEFAULT PRIVILEGES`
产生的未来对象不在这条等式内——函数 `EXECUTE` 无法按同样的方式否定，因为 PostgreSQL
默认把新函数的 `EXECUTE` 授予 `PUBLIC`；真正承重的守卫是 §1.3 的 CAS、列级授权与
append-only 触发器，启动校验只保证"没有多给的表面"。

启动快照不替代后续授权变更的持续核验：授权被放宽后，进程在重启前不会察觉；这与既有
worker 的边界相同，运维侧须把授权变更纳入发布检查。

## 9. 验证与测试策略

真实 PostgreSQL 是唯一判据。全部用例在隔离实例执行全量迁移后运行，DSN 见
`tests/integration/README.md`；跳过不计通过。复用既有夹具
（`tests/integration/test_production_leases.py` 的 `lease_database`、
`tests/integration/test_gateway_stop_evidence.py` 的 `stop_ledger`/`evidence_for`）与既有
故障注入手法（`tests/integration/test_production_jobs.py:387` 的 `pg_terminate_backend`、
`tests/integration/test_binding_defaults_storage.py:456` 的 `pg_cancel_backend`）。

### 9.1 真实 PG 用例

| # | 用例 | 断言 |
| --- | --- | --- |
| 1 | 正常释放 | 三处投影各扣减一次且与租约预留严格相等；租约 `released/complete`；`budget_released_at` 等于响应；证据 1 条（运维种类、`reason` 一致）；账本 1 条；意图 `aborted/complete`、`terminated_at_us == observed_at` |
| 2 | 同键同证据重放 | 200、`idempotent_replay: true`、`budget_released_at` 与意图列表逐字节相同；三处投影、证据条数、账本条数完全不变 |
| 3 | 同键不同证据（分别改 digest / observed_at / reason） | 409 `idempotency_conflict`；全表快照（三处投影 + 证据 + 账本 + 意图）与请求前完全相等 |
| 4 | 换键同证据 | 200 重放；同上零写入断言 |
| 5 | 换键不同证据 | 409 `quarantine_release_conflict`；零写入 |
| 6 | 消费者先释放（机器证据） | 409 `quarantine_release_conflict`；证据仍只有 1 条机器证据；投影未被二次扣减 |
| 7 | 运维先释放，消费者后扫描 | 消费者的 `release_lease` 返回 replayed；投影不变；`mark_settled` 幂等 |
| 8 | 意图仍 `executing` | 409；零写入（证明"每个意图必须以完整证据关闭"的门槛生效） |
| 9 | 租约非隔离（`reserved`/`executing`） | 409；零写入 |
| 10 | 提交意图 0 条 | 409；零写入（身份漂移不得静默释放） |
| 11 | 非法证据四元组：`evidence_kind` 非常量、`reason` 空白或超长、`observed_at < recorded_at`、`observed_at < dispatched_at`、`observed_at > 服务端当前时间` | 400；零写入 |
| 12 | 意图 `version` 或租约 `version` 耗尽（`MAX_VERSION`） | 拒绝并整笔回滚；投影不变 |
| 13 | 兄弟 stage 已释放的 last-stage 结算 | attempt quota 恰好归还一次；quota 版本推进一次 |
| 14 | 权限负例 | 用一个多授一列的测试角色启动 → 启动校验拒绝，事务根本不会运行（见 §9.3 反例 9） |
| 15 | 账本与租约一致性 | 每行账本都存在对应的 `released/complete` 租约，且 `budget_released_at_us` 与租约列相等（本设计不建跨表约束，靠此用例守住） |
| 23 | 租约不存在 | 404 `quarantine_release_not_found`，零写入 |

### 9.2 故障注入用例

| # | 注入 | 断言 |
| --- | --- | --- |
| 16 | 事务在步骤 9 与 11 之间被 `pg_terminate_backend` 杀掉 | 全量回滚：证据 0 条、账本 0 条、投影与租约与事务前逐行相等；同键重试一次成功 |
| 17 | 服务端 COMMIT 已发出、客户端确认丢失 | **服务级模拟**：包装层在真正提交之后抛 `QuarantineUnknownError`，证明"unknown 之后按原键查询拿到重放"这条业务结论。它不是真实网络故障证据——适配器把"COMMIT 确认丢失"分类成 unknown 的那条分支由 16 覆盖（同 `except OperationalError`） |
| 18 | 两连接并发同键同证据 | 一方 200 提交、另一方 200 重放；总扣减一次；无唯一约束异常泄漏成 500 |
| 19 | 两连接并发同键不同证据 | 一方 200、一方 409 `idempotency_conflict`（顺序不定）；数据库里只有一份证据与一份账本 |
| 20 | 运维事务与同 attempt 另一 stage 的释放并发 | 不死锁（锁序一致），两笔都提交，attempt quota 只归还一次 |
| 21 | 证据 INSERT 违反唯一约束（预置一条机器证据） | 整笔回滚、返回冲突，不产生第二条证据行 |
| 22 | 另一个会话对同一租约行长期持有 `FOR UPDATE`，重放请求带 1 s `statement_timeout` | 重放仍按时返回 200（§4.4 第 1 级不加锁）；把第 1 级去掉则被锁等待打断、超时红 |
| 23 | 另一个会话按网关顺序先持 `resource_pools` 行锁，再发起释放 | 释放会话在等待 pool 时 `pg_locks` 里没有任何 `submit_intents` 行锁；放行后释放成功、容量只扣一次。锁序反转时该用例红（见 §9.3） |
| 24 | 运行期撤销释放角色对某表的 `SELECT`（启动校验早已通过） | 503 `quarantine_release_privilege_missing`，不是 409；零写入 |
| 25 | 把账本表或释放事务读到的某表改名（等价于迁移未到位） | 503 `quarantine_release_schema_not_migrated`，不是 409；零写入 |
| 26 | 启动前给释放角色多授 schema 内一个序列的 `USAGE`/`SELECT` | 启动校验拒绝（§8.2 第 4 项），进程不注册路由 |

### 9.3 反例：删掉哪个守卫，哪条用例会红

每条都必须"先红后绿"：先删守卫跑出红，再恢复并确认绿。**只有下表列出的结论是可观测的**；
未列出的守卫属于下一小节说明的纵深防御。

| # | 删掉的守卫 | 变红的用例 |
| --- | --- | --- |
| 1 | 步骤 9 的 CAS **与**步骤 6 的"意图必须 `unknown_quarantined/quarantined`"前提同时删除 | 8（把 `executing` 意图改写成 `aborted`）。单独删任一条仍 42 绿，见下节 |
| 2 | 步骤 5 与 §4.4 第 1 级的同键摘要比较（`key_eq ∧ ¬ev_eq` 放行成 200） | 3 |
| 3 | 步骤 5 的第 2 级账本重读 | 19（落败方从 `idempotency_conflict` 退化成 `quarantine_release_conflict`） |
| 4 | 步骤 1/5 的两级账本读取（每次请求都重新释放） | 2（`budget_released_at` 变成新时间且投影二次扣减） |
| 5 | 步骤 7 的 `observed_at` 上下界 | 11 |
| 6 | 表级 CHECK `evidence_observed_at_us <= budget_released_at_us` | 11 的第三条（改库直写时能落库） |
| 7 | `admission_quarantine_releases_append_only` 触发器 | 证据重写用例；§8.2 第 6 项启动校验同时变红 |
| 8 | 步骤 6 的"意图必须 `unknown_quarantined`"前提（与 1 同一对） | 6（把机器证据的释放当成运维释放重放第二次） |
| 9 | 启动校验的逐列精确等式（改成只查"必须有的"） | 14 |
| 10 | 启动校验的序列判定（§8.2 第 4 项） | 26 |
| 11 | 锁序（把 `submit_intents` 的 `FOR UPDATE` 提到 `resource_pools` 之前） | 23（释放会话在等待 pool 时已持有 `submit_intents` 行锁） |
| 12 | 步骤 10 复用 `release()`（改成手写扣减） | 13（last-stage quota 归还两次）、隔离维度的架构分支用例 |
| 13 | `except` 链里把存储类异常并回业务冲突分支 | 24/25（错误码退化成 409） |

#### 9.3.1 未被用例观测的纵深防御

以下四层在真实 PG 上单独删除**不改变任何用例结论**（实测 42 passed）。它们不是死代码，
但也不能当作"有守护"：

| 层 | 位置 | 为何不可观测 | 何时才可观测 |
| --- | --- | --- | --- |
| 意图状态前提 | `domain/quarantine_release.py` 的 `require_releasable` | 与步骤 9 的 CAS 互为兜底：删掉它时 CAS（`rowcount != 1`）仍在写之前拦下同一状态 | 与 CAS 同时删除，或用例构造出"前提为真但 CAS 失败"的路径 |
| 意图 CAS | `quarantine_release_sql.py` 的 `_close_intent` | 同上，删掉它时状态前提在写之前就拒绝 | 同上 |
| 锁后冻结字段重核对 | `quarantine_release_sql.py` 的 `frozen` 逐字段比较 | 准入之后没有任何写入路径修改 `operation_id`/`host_node_id`/`reserved_*` 等冻结列，`hint` 与锁后行不可能不同 | 出现"准入后改写租约身份或预留向量"的迁移或新事务（例如引入租约接管改写预留） |
| `result.outcome != "committed"` 守卫 | `quarantine_release_sql.py` 末尾 | 步骤 6 已要求租约为 `quarantined/quarantined`，`release_in_transaction` 在同一事务里只会返回 `committed` | `release()` 增加新的返回值（例如"部分结算"）或本事务允许非隔离租约 |

锁序曾属于这一组（反转后 42 绿），现已由用例 23 观测。

### 9.4 契约与装配面

- 该 operation 已从 `tests/contract/test_profile_http_bindings.py` 的
  `KNOWN_UNSERVED_ADMIN_OPERATIONS` 移除，清单为空且门禁保留（已承接的 operation 再出现
  会红）。
- 路由一致性由 `scripts/dev-env/check-route-conformance.py` 校验；该 operation 不在
  `backend/src/speechnexus_backend/speech/interfaces/http/unimplemented.py` 的占位表里。
- 契约声明 `503 ServiceUnavailable`，并把 `released_submit_intent_ids` 收窄为
  `maxItems: 1`（§5）。
- **装配面**：`deploy/compose.yaml` 的 `api` 环境以 `${VAR:-}` 空默认透传
  `SPEECHNEXUS_QUARANTINE_RELEASE_DATABASE_URL`，未配置的空值使端点停在
  `503 quarantine_release_unavailable`，而不是让整栈渲染失败；
  `deploy/compose.env.example` 声明该键与
  `SPEECHNEXUS_QUARANTINE_RELEASE_ROLE_PASSWORD`；角色预置步骤在
  [最小部署手册 §6](../operations/compose-minimal.md#6-创建非-owner-运行角色与最小授权)
  （`quarantine_release_grants`、DSN 写回、列级核对 SQL、已有部署的授权升级）。装配面由
  `tests/unit/backend/test_reader_dsn_wiring.py` 与
  `tests/ci/test_compose_minimal_role_provisioning.py` 守护。
- 术语与行文以[协调事务](../architecture/coordination-transactions.md)、
  [运行治理](../architecture/runtime-governance.md)与
  [对账恢复](../operations/reconciliation-recovery.md)为准。

## 10. 明确不做

- 不提供绕过停止证据的强制释放：`evidence_kind` 只有运维核验一种，没有"忽略证据"开关。
- 不做自动扫描：本入口只处理被显式点名的租约；批量对账属于后台消费者职责。
- 不归还 `engine_instances` 的基线预算：实例基线仍须独立 runtime 停止流程核对退出、
  资源释放与全部租约后才能归还（`production-leases.md` 的既有边界）。
- 不自动把 host 从 `quarantined` 恢复成 `active`：隔离阈值以下也不自动接单。
- 不删除或脱敏任何执行证据；本入口只追加。
- 不改变 `release_lease`、`quarantine_lease` 与网关执行事实的既有语义；本入口是同一
  数据模型上的第三条**证据来源**，不是第三套释放规则。

## 11. 证据边界

已在本卡实测的事实（隔离 PostgreSQL 16.15，`postgres:16.15-alpine`，全量迁移到
`0051_binding_publication_events`，容器用后即删）：

- `admission_leases` 的 24 个列、`submit_intents` 的 27 个列与
  `execution_stop_evidence` 的 8 个列；全库没有 `%quarantine_release%` 列。
- 约束名 `submit_intents_lease_id_key`（`UNIQUE (lease_id)`）、
  `execution_stop_evidence_submit_intent_id_key`（`UNIQUE (submit_intent_id)`）。
- `execution_stop_evidence.evidence_kind` 没有值域 CHECK，`reason` 可空。
- §2.2 的 DDL 草案与触发器原样执行成功；合法行可写入；`reason` 空白、`evidence_kind`
  非运维种类、`observed_at > budget_released_at` 三种写入被拒；`UPDATE`/`DELETE` 被
  触发器拒绝。
- `docs` 内 `路径:行号` 引用全部有效（`scripts/dev-env/check-doc-refs.py`）。

本卡**未**验证、需由实现卡取证的项：

- 上述 DDL 作为 Alembic 迁移在 CI 迁移链上的表现（本卡不建迁移）；回滚脚本对既有行的
  处理，以及旧版本消费者遇到新表时的兼容性。
- 事务在真实并发与真实故障下的行为：本卡只给出设计与用例，§9 的 23 条用例均未运行。
- 网关侧对本入口写入的 `aborted/complete` 事实的后续读取行为（只会按版本 CAS 拒绝迟到
  写入是设计推论，未在真实网关进程上实测）。
- 专用角色的实际授权安装与启动校验：`GRANT` 语句与校验查询均未在真实部署角色上执行。
- InnoDB 行为：本页的约束与触发器使用 PostgreSQL 专有表达式（正则 `~`、
  `jsonb_typeof`、plpgsql 触发器）。若该路径要求与既有协调端口一致的 InnoDB 行为
  oracle，需要各自的等价实现（`REGEXP`、`JSON_TYPE`、`BEFORE UPDATE` 触发器）；本页
  不声明 MySQL 可移植性，也不把 PostgreSQL 行为记为双库结论。
- 客户端时钟偏差下的 `observed_at` 拒绝率：服务端以数据库时间裁决，但真实运维时钟偏差
  的分布未测量，因此 §7.2 的 400 占比未知。
